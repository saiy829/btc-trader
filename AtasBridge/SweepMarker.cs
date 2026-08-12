using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;

using ATAS.Indicators;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;
using Utils.Common.Logging;

// Platform split: the ATAS settings editor needs its own native color type to
// render a color picker. ATAS Platform (WPF rendering) uses
// System.Windows.Media.Color, ATAS X uses System.Drawing.Color. The alias keeps
// business code free of #if. Note that RenderContext drawing calls take
// System.Drawing.Color on BOTH platforms (OFT.Rendering is identical), so only
// the settings properties need the alias; ToDrawing() bridges the two.
#if ATAS_PLATFORM
using SeriesColor = System.Windows.Media.Color;
#else
using SeriesColor = System.Drawing.Color;
#endif

namespace AtasBridge
{
    // ==================================================================
    //  SweepMarker (2026-08-12, task card 9G)
    //
    //  Setup C: liquidity sweep reversal marker. Draws entry / stop / TP on
    //  the chart so it can be executed by hand. No order routing, no VPS
    //  push, no database, no network access at all.
    //
    //  IMPORTANT: this file does NOT produce its own dll. It is compiled into
    //  AtasBridge.dll together with AtasBridge.cs and AtasLiquidations.cs.
    //  ATAS discovers every Indicator subclass in that assembly by reflection
    //  and registers them as separate indicators. Consequences:
    //    1. AtasBridgeVersion.Tag is the version of the WHOLE dll. Editing
    //       this file means bumping it too.
    //    2. All three indicators ship and upgrade together.
    //
    //  Design notes that matter for correctness:
    //
    //  * Two-stage detection. A sweep is over in seconds; waiting for the M5
    //    close to alert means the good entry is already gone. Stage 1 fires on
    //    ticks (bar still open) and only warns. Stage 2 runs at bar close and
    //    decides confirm / invalidate.
    //
    //  * No look-ahead. Percentile and median are computed from CLOSED bars
    //    only, never including the bar being evaluated. Getting this wrong
    //    makes the indicator look great in replay and fail live.
    //
    //  * Replay == live. Stage 1 reads only (a) the current bar running
    //    totals and (b) statistics over closed bars. During a historical pass
    //    OnCalculate is invoked once per bar with final totals, which equals
    //    the last tick state of that bar, so markers land on the same bars.
    //
    //  * Dedup is mandatory. Tick level detection without a
    //    (poolId, direction, barIndex) guard alerts continuously and makes
    //    the indicator unusable. Every sound goes through the same guard,
    //    extended with the event type.
    // ==================================================================
    [DisplayName("SweepMarker")]
    [Category("Setups")]
    [Description("Setup C liquidity sweep reversal marker - entry/stop/TP, long and short symmetric ("
                 + AtasBridgeVersion.Tag + ")")]
    public class SweepMarker : Indicator
    {
        // ---- internal constants (deliberately not settings) ---------------
        // ATR periods are fixed: the card enumerates the exact settings list
        // and these are not on it. 14 is the conventional Wilder length.
        private const int ATR_M5_PERIOD = 14;
        private const int ATR_D1_PERIOD = 14;
        // Built-in ATAS sound files, present in <install>\Sounds on both
        // platforms. AddAlert takes the bare file name.
        private const string SND_ALERT   = "alert1.wav";
        private const string SND_CONFIRM = "alert3.wav";
        private const string SND_INVALID = "beep_2_1.wav";
        // Retrigger window for the timeout case, see Pool.RetriggerCount.
        private const int RETRIGGER_WINDOW_MIN = 30;
        // Hard cap on how many pools we keep, so a pathological chart cannot
        // grow the collections without bound.
        private const int MAX_POOLS = 400;
        private const int LOG_MAX = 40;

        private const decimal M5_SECONDS = 300m;

        // ================= settings: pool detection =====================

        // Bars required on each side of a swing point. Higher = fewer but more
        // significant pools; lower = more pools, more noise.
        [Display(Name = "PivotBars", GroupName = "1 Pools", Order = 1,
                 Description = "Bars on each side of a swing high/low")]
        public int PivotBars { get; set; } = 5;

        // How far back pools stay relevant. Older liquidity is usually already
        // taken and no longer attracts price.
        [Display(Name = "LookbackDays", GroupName = "1 Pools", Order = 2,
                 Description = "Days of history to keep pools for")]
        public int LookbackDays { get; set; } = 3;

        // Two swings closer than this (in daily ATR) are the same liquidity
        // shelf: equal highs / equal lows. Merging them avoids double signals
        // and flags the stronger double top / double bottom pattern.
        [Display(Name = "EqualTolerance", GroupName = "1 Pools", Order = 3,
                 Description = "Equal high/low merge tolerance, x ATR(D1)")]
        public decimal EqualTolerance { get; set; } = 0.1m;

        // ================= settings: sweep detection ====================

        // Price must come this close to a pool before we watch it at all.
        // Pure performance / noise filter: far away pools cannot be swept now.
        [Display(Name = "ArmDistance", GroupName = "2 Sweep", Order = 1,
                 Description = "Distance at which a pool becomes armed, x ATR(M5)")]
        public decimal ArmDistance { get; set; } = 0.5m;

        // Minimum penetration to call it a sweep rather than a touch.
        [Display(Name = "MinPenetration", GroupName = "2 Sweep", Order = 2,
                 Description = "Minimum penetration beyond the pool, x ATR(M5)")]
        public decimal MinPenetration { get; set; } = 0.05m;

        // Beyond this the move is a real breakout, not a stop run.
        [Display(Name = "MaxPenetration", GroupName = "2 Sweep", Order = 3,
                 Description = "Above this it is a genuine breakout, x ATR(M5)")]
        public decimal MaxPenetration { get; set; } = 1.5m;

        // Delta spike percentile. A sweep is one-sided aggression, so the
        // sweeping bar's delta should sit in the tail of the recent
        // distribution (low tail for a long setup, high tail for a short).
        [Display(Name = "DeltaPercentile", GroupName = "2 Sweep", Order = 4,
                 Description = "Delta spike percentile, %")]
        public decimal DeltaPercentile { get; set; } = 5m;

        // Volume burst multiple over the recent median. Stop runs trade a lot.
        [Display(Name = "VolMultiple", GroupName = "2 Sweep", Order = 5,
                 Description = "Volume burst multiple over recent median")]
        public decimal VolMultiple { get; set; } = 3.0m;

        // Sample size for the percentile and the median above.
        [Display(Name = "DeltaVolLookback", GroupName = "2 Sweep", Order = 6,
                 Description = "Closed bars used for percentile and median")]
        public int DeltaVolLookback { get; set; } = 50;

        // ================= settings: confirmation ======================

        // How long price may stay outside the pool before we give up. A real
        // sweep snaps back fast.
        [Display(Name = "ReclaimBars", GroupName = "3 Confirm", Order = 1,
                 Description = "Bars allowed to reclaim the pool")]
        public int ReclaimBars { get; set; } = 3;

        // ADR = aggression decay ratio: cumulative |delta| after the sweep bar
        // divided by the sweep bar |delta|. Low ADR means the aggression died
        // out, which is what a failed breakout looks like.
        [Display(Name = "AdrPass", GroupName = "3 Confirm", Order = 2,
                 Description = "ADR at or below this is a clean signal")]
        public decimal AdrPass { get; set; } = 0.8m;

        // Above this the aggression is still running: treat as real breakout.
        [Display(Name = "AdrInvalidate", GroupName = "3 Confirm", Order = 3,
                 Description = "ADR above this invalidates the setup")]
        public decimal AdrInvalidate { get; set; } = 1.5m;

        // Passive side / aggressive side volume at one price. High ratio means
        // limit orders absorbed the market orders, i.e. someone defended.
        [Display(Name = "AbsorptionRatio", GroupName = "3 Confirm", Order = 4,
                 Description = "Passive/aggressive volume ratio at one price")]
        public decimal AbsorptionRatio { get; set; } = 2.0m;

        // Absorption below this size is noise, not a real defender.
        [Display(Name = "AbsorptionMinBtc", GroupName = "3 Confirm", Order = 5,
                 Description = "Minimum absorbed size at one price, BTC")]
        public decimal AbsorptionMinBtc { get; set; } = 5.0m;

        // ================= settings: trade math ========================

        // Stop sits this far beyond the sweep extreme so a retest does not
        // clip it.
        [Display(Name = "StopBuffer", GroupName = "4 Trade", Order = 1,
                 Description = "Stop buffer beyond the sweep extreme, x ATR(M5)")]
        public decimal StopBuffer { get; set; } = 0.3m;

        // Too tight a stop gets taken out by noise.
        [Display(Name = "MinStopPct", GroupName = "4 Trade", Order = 2,
                 Description = "Reject the signal if the stop is closer than this, %")]
        public decimal MinStopPct { get; set; } = 0.15m;

        // Too wide a stop makes position size meaningless.
        [Display(Name = "MaxStopPct", GroupName = "4 Trade", Order = 3,
                 Description = "Reject the signal if the stop is wider than this, %")]
        public decimal MaxStopPct { get; set; } = 0.8m;

        // Below this reward/risk the trade is drawn but greyed out.
        [Display(Name = "MinRR", GroupName = "4 Trade", Order = 4,
                 Description = "Minimum reward/risk before the signal is greyed")]
        public decimal MinRR { get; set; } = 2.0m;

        // Manual account size, used only for the position size label.
        [Display(Name = "AccountEquity", GroupName = "4 Trade", Order = 5,
                 Description = "Account equity in USD, manual input")]
        public decimal AccountEquity { get; set; } = 10000m;

        // Risk per trade as a percentage of equity.
        [Display(Name = "RiskPct", GroupName = "4 Trade", Order = 6,
                 Description = "Risk per trade, % of equity")]
        public decimal RiskPct { get; set; } = 1.0m;

        // ================= settings: display ==========================

        [Display(Name = "ShowInvalidated", GroupName = "5 Display", Order = 1,
                 Description = "Draw grey X marks for invalidated sweeps")]
        public bool ShowInvalidated { get; set; } = true;

        [Display(Name = "ShowPoolLines", GroupName = "5 Display", Order = 2,
                 Description = "Draw the liquidity pool levels")]
        public bool ShowPoolLines { get; set; } = true;

        [Display(Name = "SignalExtendBars", GroupName = "5 Display", Order = 3,
                 Description = "How far right the entry/stop/TP lines extend")]
        public int SignalExtendBars { get; set; } = 20;

        [Display(Name = "BslColor", GroupName = "5 Display", Order = 4,
                 Description = "Buy side liquidity (swing high) pool color")]
        public SeriesColor BslColor { get; set; } = MakeColor(255, 242, 56, 90);

        [Display(Name = "SslColor", GroupName = "5 Display", Order = 5,
                 Description = "Sell side liquidity (swing low) pool color")]
        public SeriesColor SslColor { get; set; } = MakeColor(255, 8, 153, 129);

        // ================= settings: sound ============================

        [Display(Name = "EnableSoundAlert", GroupName = "6 Sound", Order = 1,
                 Description = "Stage 1 warning sound, sweep in progress")]
        public bool EnableSoundAlert { get; set; } = true;

        [Display(Name = "EnableSoundConfirm", GroupName = "6 Sound", Order = 2,
                 Description = "Stage 2 confirmation sound, entry is valid")]
        public bool EnableSoundConfirm { get; set; } = true;

        [Display(Name = "EnableSoundInvalid", GroupName = "6 Sound", Order = 3,
                 Description = "Invalidation sound, stop waiting")]
        public bool EnableSoundInvalid { get; set; } = false;

        // ===================== internal state =========================

        private enum PoolKind { Bsl, Ssl }
        private enum PoolState { Active, Swept, Expired }
        private enum Stage { Idle, Watch }

        private sealed class Pool
        {
            public int Id;
            public PoolKind Kind;
            public decimal Price;
            public int MergeCount = 1;
            public int PivotBar;
            public DateTime PivotTimeUtc;
            public PoolState State = PoolState.Active;

            // explicit per pool state machine
            public Stage Stage = Stage.Idle;
            public int SweepBarIndex = -1;
            public decimal SweepExtremePrice;
            public decimal SweepBarDelta;
            public bool SweepBarDeltaFinal;      // true once the sweep bar closed
            public DateTime SweepTimeUtc;
            public int RetriggerCount;
            public bool Reclaimed;               // close came back inside
            public decimal PostSweepAbsDelta;    // running sum for ADR
        }

        private enum InvalidReason { None, Timeout, TooDeep, AdrHigh, SecondBreak, StopTooClose, StopTooFar }

        private sealed class Signal
        {
            public int Bar;
            public bool IsLong;
            public decimal Entry, Stop, Tp1, Tp2, R, Rr, SizeBtc, Adr;
            public bool Weak;
            public bool RrLow;
            public int PoolId;
        }

        private sealed class InvalidMark
        {
            public int Bar;
            public bool IsLong;
            public decimal Price;
            public InvalidReason Reason;
            public int PoolId;
        }

        private sealed class AlertMark
        {
            public int Bar;
            public bool IsLong;
            public decimal Price;
        }

        private readonly List<Pool> _pools = new();
        private readonly List<Signal> _signals = new();
        private readonly List<InvalidMark> _invalids = new();
        private readonly List<AlertMark> _alertMarks = new();
        private readonly HashSet<string> _dedup = new();      // stage guard
        private readonly HashSet<string> _soundDedup = new();  // sound guard

        private readonly List<decimal> _trM5 = new();          // true range per closed bar
        private readonly List<decimal> _dayTr = new();          // true range per closed day
        private DateTime _curDay = DateTime.MinValue;
        private decimal _dayHigh, _dayLow, _prevDayClose, _curDayClose;

        private int _nextPoolId = 1;
        private int _lastClosedProcessed = -1;
        private int _logCount;

        private string _lastEvent = "none";
        private int _todayConfirmed, _todayInvalidated;
        private DateTime _todayDate = DateTime.MinValue;
        private bool _periodOk = true;
        private string _periodText = "";

        private static readonly RenderFont _font = new("Arial", 11f);
        private static readonly RenderFont _fontSmall = new("Arial", 10f);

        public SweepMarker() : base(true)
        {
            // The indicator draws price levels, so it belongs on the price
            // panel. Do NOT move it to a new panel.
            EnableCustomDrawing = true;
            // Final, not Historical. Subscribing to Historical takes over the
            // historical drawing layer and blanks out anything the platform
            // draws there - AtasLiquidations already paid for that lesson.
            SubscribeToDrawingEvents(DrawingLayouts.Final);
            DenyToChangePanel = true;
        }

        private static SeriesColor MakeColor(byte a, byte r, byte g, byte b)
        {
#if ATAS_PLATFORM
            return SeriesColor.FromArgb(a, r, g, b);
#else
            return SeriesColor.FromArgb(a, r, g, b);
#endif
        }

        private static Color ToDrawing(SeriesColor c) => Color.FromArgb(c.A, c.R, c.G, c.B);

        private void Log(string msg)
        {
            if (_logCount >= LOG_MAX) return;
            _logCount++;
            try { LoggerHelper.LogInfo(this, "{0}", new object[] { "[SweepMarker] " + msg }); }
            catch (Exception ex) { Console.WriteLine("[SweepMarker] log failed " + ex.GetType().Name + ": " + ex.Message); }
        }

        private void LogEx(string where, Exception ex)
        {
            if (_logCount >= LOG_MAX) return;
            _logCount++;
            try
            {
                LoggerHelper.LogError(this, "{0}", new object[] {
                    "[SweepMarker] EX at " + where + ": " + ex.GetType().Name + ": " + ex.Message });
            }
            catch (Exception inner)
            {
                Console.WriteLine("[SweepMarker] logging failure " + inner.GetType().Name + ": " + inner.Message);
            }
        }

        private void ResetAll()
        {
            _pools.Clear(); _signals.Clear(); _invalids.Clear(); _alertMarks.Clear();
            _dedup.Clear(); _soundDedup.Clear();
            _trM5.Clear(); _dayTr.Clear();
            _curDay = DateTime.MinValue;
            _dayHigh = _dayLow = _prevDayClose = _curDayClose = 0m;
            _nextPoolId = 1;
            _lastClosedProcessed = -1;
            _lastEvent = "none";
            _todayConfirmed = _todayInvalidated = 0;
            _todayDate = DateTime.MinValue;
            _logCount = 0;
        }

        // ===================== main entry point ========================

        protected override void OnCalculate(int bar, decimal value)
        {
            try
            {
                if (bar == 0)
                {
                    ResetAll();
                    CheckPeriod();
                }

                // Close out every bar that finished since the last call. This
                // single path serves both the historical pass and live ticks,
                // which is what keeps replay and live identical.
                while (_lastClosedProcessed < CurrentBar - 1)
                    ProcessClosedBar(++_lastClosedProcessed);

                if (bar == CurrentBar)
                    RealtimeStageOne(CurrentBar);
            }
            catch (Exception ex)
            {
                LogEx("OnCalculate bar=" + bar, ex);
            }
        }

        private void CheckPeriod()
        {
            _periodOk = true;
            _periodText = "";
            try
            {
                if (ChartInfo == null) return;
                var tf = ATAS.Indicators.Extensions.GetTimeFrameTypeChartPeriod(ChartInfo);
                if (tf > TimeSpan.Zero)
                {
                    _periodText = tf.ToString();
                    _periodOk = Math.Abs(tf.TotalSeconds - (double)M5_SECONDS) < 1.0;
                }
            }
            catch (Exception ex)
            {
                LogEx("CheckPeriod", ex);
            }
        }

        // ===================== closed bar pipeline =====================

        private void ProcessClosedBar(int i)
        {
            var c = SafeCandle(i);
            if (c == null) return;

            UpdateAtrM5(i, c);
            UpdateDailyAtr(i, c);
            RollTodayCounters(c);

            // Pivot at i - PivotBars is now confirmed: it has PivotBars closed
            // bars on both sides.
            DetectPivot(i - PivotBars);
            ExpirePools(c);

            // The sweep bar's own delta is the ADR denominator. Freeze it the
            // moment that bar closes so live and replay agree.
            foreach (var p in _pools)
            {
                if (p.Stage == Stage.Watch && !p.SweepBarDeltaFinal && p.SweepBarIndex <= i)
                {
                    var sc = SafeCandle(p.SweepBarIndex);
                    if (sc != null) { p.SweepBarDelta = sc.Delta; p.SweepBarDeltaFinal = true; }
                }
            }

            StageTwo(i);
        }

        // Returns null on any out of range / throwing access. Every caller
        // checks for null; that is deliberate, a missing candle must never
        // take the indicator down.
        private IndicatorCandle? SafeCandle(int i)
        {
            if (i < 0 || i > CurrentBar) return null;
            try { return GetCandle(i); }
            catch (Exception ex) { LogEx("GetCandle " + i, ex); return null; }
        }

        private void UpdateAtrM5(int i, IndicatorCandle c)
        {
            decimal tr;
            var prev = SafeCandle(i - 1);
            if (prev == null) tr = c.High - c.Low;
            else tr = Math.Max(c.High - c.Low,
                      Math.Max(Math.Abs(c.High - prev.Close), Math.Abs(c.Low - prev.Close)));
            _trM5.Add(tr);
            if (_trM5.Count > ATR_M5_PERIOD * 4) _trM5.RemoveAt(0);
        }

        private decimal AtrM5()
        {
            if (_trM5.Count == 0) return 0m;
            int n = Math.Min(ATR_M5_PERIOD, _trM5.Count);
            decimal s = 0m;
            for (int k = _trM5.Count - n; k < _trM5.Count; k++) s += _trM5[k];
            return s / n;
        }

        // Daily ATR is aggregated from the M5 stream: there is no second data
        // series to query, and building one would add API surface for no gain.
        // Day boundaries use the chart timezone so the buckets line up with
        // what is on screen.
        private void UpdateDailyAtr(int i, IndicatorCandle c)
        {
            DateTime t;
            try { t = c.Time.AddHours(InstrumentInfo?.TimeZone ?? 0); }
            catch (Exception ex) { LogEx("bar time", ex); return; }
            var day = t.Date;

            if (_curDay == DateTime.MinValue)
            {
                _curDay = day; _dayHigh = c.High; _dayLow = c.Low; _curDayClose = c.Close;
                return;
            }
            if (day != _curDay)
            {
                decimal tr = _prevDayClose == 0m
                    ? _dayHigh - _dayLow
                    : Math.Max(_dayHigh - _dayLow,
                      Math.Max(Math.Abs(_dayHigh - _prevDayClose), Math.Abs(_dayLow - _prevDayClose)));
                _dayTr.Add(tr);
                if (_dayTr.Count > ATR_D1_PERIOD * 3) _dayTr.RemoveAt(0);
                _prevDayClose = _curDayClose;
                _curDay = day; _dayHigh = c.High; _dayLow = c.Low;
            }
            else
            {
                if (c.High > _dayHigh) _dayHigh = c.High;
                if (c.Low < _dayLow) _dayLow = c.Low;
            }
            _curDayClose = c.Close;
        }

        private decimal AtrD1()
        {
            if (_dayTr.Count == 0)
            {
                // Fall back to an M5 based estimate so EqualTolerance still
                // does something sensible on a freshly loaded chart.
                return AtrM5() * 12m;
            }
            int n = Math.Min(ATR_D1_PERIOD, _dayTr.Count);
            decimal s = 0m;
            for (int k = _dayTr.Count - n; k < _dayTr.Count; k++) s += _dayTr[k];
            return s / n;
        }

        private void RollTodayCounters(IndicatorCandle c)
        {
            try
            {
                var d = c.Time.AddHours(InstrumentInfo?.TimeZone ?? 0).Date;
                if (_todayDate == DateTime.MinValue) { _todayDate = d; return; }
                if (d != _todayDate) { _todayDate = d; _todayConfirmed = 0; _todayInvalidated = 0; }
            }
            catch (Exception ex) { LogEx("RollTodayCounters", ex); }
        }

        // ===================== pools ==================================

        private void DetectPivot(int p)
        {
            if (p - PivotBars < 0) return;
            var c = SafeCandle(p);
            if (c == null) return;

            bool isHigh = true, isLow = true;
            for (int k = p - PivotBars; k <= p + PivotBars; k++)
            {
                if (k == p) continue;
                var o = SafeCandle(k);
                if (o == null) return;
                if (o.High >= c.High) isHigh = false;
                if (o.Low <= c.Low) isLow = false;
                if (!isHigh && !isLow) return;
            }

            DateTime tUtc;
            try { tUtc = DateTime.SpecifyKind(c.Time, DateTimeKind.Utc); }
            catch (Exception ex) { LogEx("pivot time", ex); return; }

            if (isHigh) AddOrMerge(PoolKind.Bsl, c.High, p, tUtc);
            if (isLow) AddOrMerge(PoolKind.Ssl, c.Low, p, tUtc);
        }

        private void AddOrMerge(PoolKind kind, decimal price, int bar, DateTime tUtc)
        {
            decimal tol = EqualTolerance * AtrD1();
            foreach (var p in _pools)
            {
                if (p.Kind != kind || p.State != PoolState.Active) continue;
                if (Math.Abs(p.Price - price) > tol) continue;
                // Merged shelf: BSL keeps the higher edge, SSL the lower one,
                // because that is the price that actually holds the stops.
                if (kind == PoolKind.Bsl) { if (price > p.Price) p.Price = price; }
                else { if (price < p.Price) p.Price = price; }
                p.MergeCount++;
                p.PivotBar = bar;
                p.PivotTimeUtc = tUtc;
                return;
            }

            if (_pools.Count >= MAX_POOLS)
            {
                var oldest = _pools.OrderBy(x => x.PivotTimeUtc).FirstOrDefault();
                if (oldest != null) _pools.Remove(oldest);
            }
            _pools.Add(new Pool
            {
                Id = _nextPoolId++, Kind = kind, Price = price,
                PivotBar = bar, PivotTimeUtc = tUtc, State = PoolState.Active
            });
        }

        private void ExpirePools(IndicatorCandle now)
        {
            DateTime nowUtc;
            try { nowUtc = DateTime.SpecifyKind(now.LastTime, DateTimeKind.Utc); }
            catch (Exception ex) { LogEx("ExpirePools time", ex); return; }
            var cutoff = nowUtc.AddDays(-LookbackDays);
            foreach (var p in _pools)
                if (p.State == PoolState.Active && p.PivotTimeUtc < cutoff)
                    p.State = PoolState.Expired;
        }

        // ===================== statistics over CLOSED bars =============

        // Both helpers deliberately stop at CurrentBar-1. Including the open
        // bar would leak the outcome into the threshold: classic look-ahead.
        private bool ClosedStats(out decimal loQ, out decimal hiQ, out decimal volMedian)
        {
            loQ = hiQ = volMedian = 0m;
            int last = CurrentBar - 1;
            if (last < 1) return false;
            int n = Math.Min(DeltaVolLookback, last + 1);
            if (n < 5) return false;

            var deltas = new List<decimal>(n);
            var vols = new List<decimal>(n);
            for (int k = last - n + 1; k <= last; k++)
            {
                var c = SafeCandle(k);
                if (c == null) continue;
                deltas.Add(c.Delta);
                vols.Add(c.Volume);
            }
            if (deltas.Count < 5) return false;

            deltas.Sort();
            vols.Sort();
            loQ = Percentile(deltas, DeltaPercentile);
            hiQ = Percentile(deltas, 100m - DeltaPercentile);
            volMedian = Percentile(vols, 50m);
            return true;
        }

        private static decimal Percentile(List<decimal> sorted, decimal pct)
        {
            if (sorted.Count == 0) return 0m;
            if (pct <= 0m) return sorted[0];
            if (pct >= 100m) return sorted[sorted.Count - 1];
            decimal pos = (sorted.Count - 1) * pct / 100m;
            int lo = (int)Math.Floor(pos);
            int hi = (int)Math.Ceiling(pos);
            if (lo == hi) return sorted[lo];
            decimal frac = pos - lo;
            return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
        }

        // ===================== stage 1: realtime warning ===============

        private void RealtimeStageOne(int bar)
        {
            var c = SafeCandle(bar);
            if (c == null) return;
            decimal atr = AtrM5();
            if (atr <= 0m) return;
            if (!ClosedStats(out var loQ, out var hiQ, out var volMed)) return;

            bool volOk = volMed > 0m && c.Volume >= volMed * VolMultiple;
            if (!volOk) return;

            decimal price = c.Close;   // last traded price of the open bar

            foreach (var p in _pools)
            {
                if (p.State != PoolState.Active || p.Stage != Stage.Idle) continue;

                // ArmDistance is a pre-filter: pools far from price cannot be
                // swept right now, so do not even look at them.
                if (Math.Abs(price - p.Price) > ArmDistance * atr + MaxPenetration * atr) continue;

                bool isLong = p.Kind == PoolKind.Ssl;
                bool penetrated = isLong
                    ? price < p.Price - MinPenetration * atr
                    : price > p.Price + MinPenetration * atr;
                if (!penetrated) continue;

                bool deltaOk = isLong ? c.Delta <= loQ : c.Delta >= hiQ;
                if (!deltaOk) continue;

                string key = p.Id + "|" + (isLong ? "L" : "S") + "|" + bar;
                if (!_dedup.Add(key)) continue;   // mandatory dedup

                p.Stage = Stage.Watch;
                p.SweepBarIndex = bar;
                p.SweepExtremePrice = isLong ? c.Low : c.High;
                p.SweepBarDelta = c.Delta;
                p.SweepBarDeltaFinal = false;
                p.Reclaimed = false;
                p.PostSweepAbsDelta = 0m;
                try { p.SweepTimeUtc = DateTime.SpecifyKind(c.LastTime, DateTimeKind.Utc); }
                catch (Exception ex) { LogEx("sweep time", ex); p.SweepTimeUtc = DateTime.UtcNow; }

                _alertMarks.Add(new AlertMark { Bar = bar, IsLong = isLong, Price = isLong ? c.Low : c.High });
                _lastEvent = "ALERT " + (isLong ? "SSL " : "BSL ") + Fmt(p.Price) +
                             (isLong ? " (long setup forming)" : " (short setup forming)");
                Log(_lastEvent + " bar=" + bar + " delta=" + Fmt(c.Delta) + " vol=" + Fmt(c.Volume));
                PlaySound(SND_ALERT, EnableSoundAlert, p.Id, isLong, bar, "alert",
                          "SweepMarker: " + _lastEvent);
            }
        }

        // ===================== stage 2: bar close decision =============

        private void StageTwo(int i)
        {
            decimal atr = AtrM5();
            if (atr <= 0m) return;

            foreach (var p in _pools)
            {
                if (p.Stage != Stage.Watch) continue;
                if (i <= p.SweepBarIndex) continue;

                var c = SafeCandle(i);
                if (c == null) continue;
                bool isLong = p.Kind == PoolKind.Ssl;

                // running extreme and post sweep aggression
                if (isLong) { if (c.Low < p.SweepExtremePrice) p.SweepExtremePrice = c.Low; }
                else { if (c.High > p.SweepExtremePrice) p.SweepExtremePrice = c.High; }
                p.PostSweepAbsDelta += Math.Abs(c.Delta);

                decimal denom = Math.Abs(p.SweepBarDelta);
                decimal adr = denom > 0m ? p.PostSweepAbsDelta / denom : 0m;
                int barsSince = i - p.SweepBarIndex;
                decimal depth = isLong
                    ? (p.Price - p.SweepExtremePrice) / atr
                    : (p.SweepExtremePrice - p.Price) / atr;
                bool insideNow = isLong ? c.Close > p.Price : c.Close < p.Price;

                // ---- invalidation checks, cheapest first ----
                if (depth > MaxPenetration) { Invalidate(p, i, InvalidReason.TooDeep, isLong, adr); continue; }
                if (adr > AdrInvalidate) { Invalidate(p, i, InvalidReason.AdrHigh, isLong, adr); continue; }

                if (p.Reclaimed)
                {
                    // second break after a reclaim: the level is genuinely gone
                    bool broke = isLong
                        ? (c.Close < p.Price && c.Low < p.SweepExtremePrice)
                        : (c.Close > p.Price && c.High > p.SweepExtremePrice);
                    if (broke) { Invalidate(p, i, InvalidReason.SecondBreak, isLong, adr); continue; }
                }

                if (insideNow) p.Reclaimed = true;

                if (!insideNow)
                {
                    if (barsSince >= ReclaimBars)
                        Invalidate(p, i, InvalidReason.Timeout, isLong, adr);
                    continue;
                }

                // ---- confirmation: reclaimed + ADR ok + absorption seen ----
                if (adr > AdrInvalidate) { Invalidate(p, i, InvalidReason.AdrHigh, isLong, adr); continue; }
                if (!HasAbsorption(p.SweepBarIndex, i, isLong)) continue;

                Confirm(p, i, isLong, adr, atr, c);
            }
        }

        // Absorption inside the reclaim window. For a long setup we want the
        // bid (passive buyers) to dominate the ask (aggressive sellers) at some
        // price, which is the footprint signature of a defended low.
        private bool HasAbsorption(int fromBar, int toBar, bool isLong)
        {
            for (int k = fromBar; k <= toBar; k++)
            {
                var c = SafeCandle(k);
                if (c == null) continue;
                IEnumerable<PriceVolumeInfo> levels;
                try { levels = c.GetAllPriceLevels(); }
                catch (Exception ex) { LogEx("GetAllPriceLevels " + k, ex); continue; }
                if (levels == null) continue;

                foreach (var l in levels)
                {
                    if (l == null) continue;
                    decimal passive = isLong ? l.Bid : l.Ask;
                    decimal aggressive = isLong ? l.Ask : l.Bid;
                    if (passive < AbsorptionMinBtc) continue;
                    if (aggressive <= 0m) return true;   // one sided, fully absorbed
                    if (passive / aggressive >= AbsorptionRatio) return true;
                }
            }
            return false;
        }

        private void Invalidate(Pool p, int bar, InvalidReason reason, bool isLong, decimal adr)
        {
            p.Stage = Stage.Idle;

            // A pool that timed out without reclaiming gets exactly one more
            // chance inside RETRIGGER_WINDOW_MIN. Everything else is done for
            // good, otherwise a chopping market re-signals the same level
            // forever.
            bool allowRetrigger = reason == InvalidReason.Timeout && p.RetriggerCount < 1;
            if (allowRetrigger)
            {
                p.RetriggerCount++;
                p.State = PoolState.Active;
            }
            else
            {
                p.State = PoolState.Swept;
            }

            _invalids.Add(new InvalidMark
            {
                Bar = p.SweepBarIndex, IsLong = isLong, PoolId = p.Id,
                Price = p.SweepExtremePrice, Reason = reason
            });
            _todayInvalidated++;
            _lastEvent = "INVALID " + ReasonText(reason) + " pool=" + Fmt(p.Price) + " ADR=" + adr.ToString("0.00");
            Log(_lastEvent + " bar=" + bar + " retrigger=" + p.RetriggerCount);
            PlaySound(SND_INVALID, EnableSoundInvalid, p.Id, isLong, bar, "invalid",
                      "SweepMarker: " + _lastEvent);
        }

        private void Confirm(Pool p, int bar, bool isLong, decimal adr, decimal atr, IndicatorCandle c)
        {
            p.Stage = Stage.Idle;
            p.State = PoolState.Swept;   // never signal the same pool twice

            decimal entry = c.Close;
            decimal stop = isLong
                ? p.SweepExtremePrice - StopBuffer * atr
                : p.SweepExtremePrice + StopBuffer * atr;
            decimal r = Math.Abs(entry - stop);
            if (r <= 0m)
            {
                Log("confirm skipped: zero risk, pool=" + Fmt(p.Price));
                return;
            }

            decimal stopPct = r / entry * 100m;
            if (stopPct < MinStopPct)
            {
                RecordRejected(p, bar, isLong, InvalidReason.StopTooClose, stopPct);
                return;
            }
            if (stopPct > MaxStopPct)
            {
                RecordRejected(p, bar, isLong, InvalidReason.StopTooFar, stopPct);
                return;
            }

            decimal tp1 = isLong ? entry + 1.5m * r : entry - 1.5m * r;
            decimal tp2 = NearestOppositePool(entry, isLong);
            if (tp2 <= 0m) tp2 = isLong ? entry + 3.0m * r : entry - 3.0m * r;

            decimal rr = Math.Abs(tp2 - entry) / r;
            decimal size = AccountEquity * RiskPct / 100m / r;
            size = Math.Floor(size / 0.001m) * 0.001m;   // round down to 0.001 BTC

            var sig = new Signal
            {
                Bar = bar, IsLong = isLong, Entry = entry, Stop = stop, Tp1 = tp1, Tp2 = tp2,
                R = r, Rr = rr, SizeBtc = size, Adr = adr, PoolId = p.Id,
                // ADR between AdrPass and AdrInvalidate is the calibration
                // grey zone: drawn, but visibly marked as weak.
                Weak = adr > AdrPass, RrLow = rr < MinRR
            };
            _signals.Add(sig);
            _todayConfirmed++;

            _lastEvent = (isLong ? "CONFIRM LONG " : "CONFIRM SHORT ") + Fmt(entry) +
                         " RR=" + rr.ToString("0.0") + " ADR=" + adr.ToString("0.00") +
                         (sig.Weak ? " WEAK" : "") + (sig.RrLow ? " RR-LOW" : "");
            Log(_lastEvent + " bar=" + bar + " stop=" + Fmt(stop) + " tp2=" + Fmt(tp2) +
                " size=" + size.ToString("0.000"));
            PlaySound(SND_CONFIRM, EnableSoundConfirm, p.Id, isLong, bar, "confirm",
                      "SweepMarker: " + _lastEvent);
        }

        private void RecordRejected(Pool p, int bar, bool isLong, InvalidReason reason, decimal stopPct)
        {
            _invalids.Add(new InvalidMark
            {
                Bar = p.SweepBarIndex, IsLong = isLong, PoolId = p.Id,
                Price = p.SweepExtremePrice, Reason = reason
            });
            _todayInvalidated++;
            _lastEvent = "REJECT " + ReasonText(reason) + " stop=" + stopPct.ToString("0.00") + "%";
            Log(_lastEvent + " bar=" + bar);
        }

        private decimal NearestOppositePool(decimal entry, bool isLong)
        {
            decimal best = 0m;
            foreach (var p in _pools)
            {
                if (p.State != PoolState.Active) continue;
                if (isLong)
                {
                    if (p.Kind != PoolKind.Bsl || p.Price <= entry) continue;
                    if (best == 0m || p.Price < best) best = p.Price;
                }
                else
                {
                    if (p.Kind != PoolKind.Ssl || p.Price >= entry) continue;
                    if (best == 0m || p.Price > best) best = p.Price;
                }
            }
            return best;
        }

        // Every sound goes through the same guard as the stage machine, plus
        // the event type. Without it a tick level detector screams.
        private void PlaySound(string file, bool enabled, int poolId, bool isLong, int bar,
                               string evt, string message)
        {
            if (!enabled) return;
            string key = poolId + "|" + (isLong ? "L" : "S") + "|" + bar + "|" + evt;
            if (!_soundDedup.Add(key)) return;
            try { AddAlert(file, message); }
            catch (Exception ex) { LogEx("AddAlert " + evt, ex); }
        }

        private static string ReasonText(InvalidReason r) => r switch
        {
            InvalidReason.Timeout => "no reclaim in time",
            InvalidReason.TooDeep => "penetration too deep",
            InvalidReason.AdrHigh => "ADR too high",
            InvalidReason.SecondBreak => "second break",
            InvalidReason.StopTooClose => "stop too close",
            InvalidReason.StopTooFar => "stop too far",
            _ => "unknown"
        };

        private string Fmt(decimal v) => v.ToString("0.##");

        // ===================== drawing =================================

        // System.Drawing.Drawing2D.DashStyle is annotated windows-only, which
        // trips CA1416 on a plain net10.0 target. ATAS itself only runs on
        // Windows, and the existing AtasBridge build has the same situation,
        // so the analyzer warning is noise here rather than a real portability
        // problem. Scoped to the drawing region only.
#pragma warning disable CA1416

        protected override void OnRender(RenderContext context, DrawingLayouts layout)
        {
            try
            {
                var cc = ChartInfo?.PriceChartContainer;
                if (cc == null) return;

                int firstBar = cc.FirstVisibleBarNumber;
                int lastBar = cc.LastVisibleBarNumber;

                if (ShowPoolLines) DrawPools(context, cc, firstBar, lastBar);
                DrawAlertMarks(context, cc, firstBar, lastBar);
                if (ShowInvalidated) DrawInvalids(context, cc, firstBar, lastBar);
                DrawSignals(context, cc, firstBar, lastBar);
                DrawPanel(context);
            }
            catch (Exception ex)
            {
                LogEx("OnRender", ex);
            }
        }

        private void DrawPools(RenderContext ctx, IChartContainer cc, int firstBar, int lastBar)
        {
            int xRight = cc.GetXByBar(lastBar, false);
            foreach (var p in _pools)
            {
                if (p.State == PoolState.Expired) continue;
                int y = cc.GetYByPrice(p.Price, false);
                int xLeft = cc.GetXByBar(Math.Max(p.PivotBar, firstBar), false);
                if (xLeft >= xRight) continue;

                RenderPen pen;
                if (p.State == PoolState.Swept)
                    pen = new RenderPen(Color.FromArgb(150, 130, 130, 130), 1f, DashStyle.Dash);
                else
                {
                    var col = ToDrawing(p.Kind == PoolKind.Bsl ? BslColor : SslColor);
                    // Merged shelves (equal highs / equal lows) are the stronger
                    // pools, so they get a thicker line.
                    pen = new RenderPen(col, p.MergeCount >= 2 ? 3f : 1.5f, DashStyle.Solid);
                }
                ctx.DrawLine(pen, xLeft, y, xRight, y);

                if (p.MergeCount >= 2 && p.State == PoolState.Active)
                {
                    var col = ToDrawing(p.Kind == PoolKind.Bsl ? BslColor : SslColor);
                    ctx.DrawString("x" + p.MergeCount, _fontSmall, col, xRight - 26, y - 14);
                }
            }
        }

        private void DrawAlertMarks(RenderContext ctx, IChartContainer cc, int firstBar, int lastBar)
        {
            var warn = Color.FromArgb(230, 255, 190, 60);
            foreach (var a in _alertMarks)
            {
                if (a.Bar < firstBar || a.Bar > lastBar) continue;
                int x = cc.GetXByBar(a.Bar, false);
                int y = cc.GetYByPrice(a.Price, false);
                int s = 5;
                Point[] tri = a.IsLong
                    ? new[] { new Point(x, y + 2 * s), new Point(x - s, y + s), new Point(x + s, y + s) }
                    : new[] { new Point(x, y - 2 * s), new Point(x - s, y - s), new Point(x + s, y - s) };
                ctx.FillPolygon(warn, tri);
            }
        }

        private void DrawInvalids(RenderContext ctx, IChartContainer cc, int firstBar, int lastBar)
        {
            var grey = Color.FromArgb(200, 140, 140, 140);
            var pen = new RenderPen(grey, 1.5f);
            int hoverBar = HoverBar();
            foreach (var m in _invalids)
            {
                if (m.Bar < firstBar || m.Bar > lastBar) continue;
                int x = cc.GetXByBar(m.Bar, false);
                int y = cc.GetYByPrice(m.Price, false);
                int s = 4;
                ctx.DrawLine(pen, x - s, y - s, x + s, y + s);
                ctx.DrawLine(pen, x - s, y + s, x + s, y - s);
                if (hoverBar == m.Bar)
                    ctx.DrawString(ReasonText(m.Reason), _fontSmall, grey, x + 8, y - 6);
            }
        }

        private void DrawSignals(RenderContext ctx, IChartContainer cc, int firstBar, int lastBar)
        {
            foreach (var s in _signals)
            {
                int endBar = s.Bar + SignalExtendBars;
                if (endBar < firstBar || s.Bar > lastBar) continue;
                int x1 = cc.GetXByBar(Math.Max(s.Bar, firstBar), false);
                int x2 = cc.GetXByBar(Math.Min(endBar, lastBar), false);
                if (x2 <= x1) continue;

                bool dim = s.RrLow;
                var entryCol = dim ? Color.FromArgb(170, 150, 150, 150)
                                   : (s.Weak ? Color.FromArgb(190, 120, 200, 160)
                                             : Color.FromArgb(255, 20, 200, 120));
                var stopCol = dim ? Color.FromArgb(170, 150, 150, 150) : Color.FromArgb(255, 235, 60, 80);
                var tpCol = dim ? Color.FromArgb(170, 150, 150, 150) : Color.FromArgb(255, 70, 150, 255);

                ctx.DrawLine(new RenderPen(entryCol, 2f), x1, cc.GetYByPrice(s.Entry, false), x2, cc.GetYByPrice(s.Entry, false));
                ctx.DrawLine(new RenderPen(stopCol, 2f), x1, cc.GetYByPrice(s.Stop, false), x2, cc.GetYByPrice(s.Stop, false));
                ctx.DrawLine(new RenderPen(tpCol, 1.5f, DashStyle.Dash), x1, cc.GetYByPrice(s.Tp1, false), x2, cc.GetYByPrice(s.Tp1, false));
                ctx.DrawLine(new RenderPen(tpCol, 2f), x1, cc.GetYByPrice(s.Tp2, false), x2, cc.GetYByPrice(s.Tp2, false));

                string label = (s.IsLong ? "LONG" : "SHORT") +
                               " | R=" + s.R.ToString("0.#") +
                               " | RR=" + s.Rr.ToString("0.0") +
                               " | Size=" + s.SizeBtc.ToString("0.000") + " BTC";
                if (s.Weak) label += " | WEAK";
                if (s.RrLow) label += " | RR LOW";

                int ye = cc.GetYByPrice(s.Entry, false);
                var sz = ctx.MeasureString(label, _fontSmall);
                ctx.FillRectangle(Color.FromArgb(190, 0, 0, 0), new Rectangle(x2 + 4, ye - sz.Height / 2 - 2, sz.Width + 6, sz.Height + 4));
                ctx.DrawString(label, _fontSmall, entryCol, x2 + 7, ye - sz.Height / 2);
            }
        }

        private int HoverBar()
        {
            try
            {
                var m = MouseLocationInfo;
                if (m != null && !m.IsMouseLeave && m.BarBelowMouse >= 0 && m.BarBelowMouse <= CurrentBar)
                    return m.BarBelowMouse;
            }
            catch (Exception ex) { LogEx("HoverBar", ex); }
            return -1;
        }

        private void DrawPanel(RenderContext ctx)
        {
            int bsl = 0, ssl = 0;
            foreach (var p in _pools)
            {
                if (p.State != PoolState.Active) continue;
                if (p.Kind == PoolKind.Bsl) bsl++; else ssl++;
            }

            var lines = new List<string>();
            if (!_periodOk)
                lines.Add("SweepMarker requires M5 chart" + (_periodText.Length > 0 ? " (current " + _periodText + ")" : ""));
            lines.Add("Pools: BSL " + bsl + " / SSL " + ssl);
            lines.Add("Last: " + _lastEvent);
            lines.Add("Today: confirmed " + _todayConfirmed + " / invalidated " + _todayInvalidated);

            // Coordinates are absolute canvas coordinates, not panel relative,
            // and ClipBounds starts at (0,0) - AtasLiquidations hit that trap.
            // Anchor on the container region when it is available.
            int baseX = 5, baseY = 20;
            try
            {
                var region = Container?.RelativeRegion ?? default;
                if (region.Height > 0) { baseX = region.X + 5; baseY = region.Y + 20; }
            }
            catch (Exception ex) { LogEx("panel anchor", ex); }

            int w = 0;
            foreach (var l in lines) w = Math.Max(w, ctx.MeasureString(l, _font).Width);
            int lh = ctx.MeasureString("Ag", _font).Height + 2;
            ctx.FillRectangle(Color.FromArgb(190, 0, 0, 0),
                new Rectangle(baseX - 4, baseY - 3, w + 12, lh * lines.Count + 6));

            for (int i = 0; i < lines.Count; i++)
            {
                var col = (!_periodOk && i == 0) ? Color.OrangeRed : Color.Gainsboro;
                ctx.DrawString(lines[i], _font, col, baseX, baseY + i * lh);
            }
        }

#pragma warning restore CA1416
    }
}
