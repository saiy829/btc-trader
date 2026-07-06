using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using System.Drawing;
using ATAS.Indicators;
using Utils.Common.Logging;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;

namespace AtasBridge
{
    // 挂载此指标的图表连的是哪个交易所 / 哪个市场。Phase 7H Stage2 起默认
    // Auto 模式：自动从 InstrumentInfo.Exchange 识别（见 IdentityMode/
    // TryParseAutoIdentity），Manual 模式才需要下面手动选择。
    // 四个图表（币安现货/永续、OKX现货/永续）分别挂载时，Auto 模式下无需
    // 手动配置，/atas/bar 和 /atas/trade 推送的 JSON 会自动带上这两个字段，
    // VPS 侧凭这两个字段把四路数据分别存进 atas_bars/atas_large_trades，
    // 不再混算。
    //
    // 2026-07-01 修复：Unset 现在是默认值（原来默认 Binance/Perp）。原因：
    // 这两个设置是"每个图表实例各自独立"的，不是全局生效——如果某个图表
    // 忘了手动选一次，原来会悄悄冒充"我是币安永续"，把别的市场的数据
    // 污染进币安永续的统计里，而且从数据本身完全看不出来是哪个图表漏配置了。
    // 默认改成 Unset 后，漏配置的图表会诚实地报 "unset"，VPS 侧会记警告日志，
    // 一眼就能看出该去哪个图表补设置，不会悄悄污染真实数据。
    //
    // Phase 7H Stage2（2026-07-06）：新增 Auto 识别后，同样的"诚实报告"
    // 原则延续——Auto 解析失败时不猜测，直接等同 Unset 路径（见
    // ResolveEffectiveIdentity），角标红色显示 UNSET，绝不静默瞎猜。
    public enum ExchangeName { Unset, Binance, Okx }
    public enum MarketKind   { Unset, Spot, Perp }

    // Phase 7H Stage2: Auto（默认）从 InstrumentInfo.Exchange 自动解析
    // exchange/market_type；Manual 完全等同 7H 之前的版本行为，下拉框
    // 手动选的值直接生效，不经过任何自动判断。Auto 模式下即使手动下拉框
    // 也设了值，实际推送数据永远以 Auto 解析结果为准（见
    // ResolveEffectiveIdentity）——Manual 下拉框此时只用来做冲突提示对比，
    // 不参与数据本身。
    public enum IdentityMode { Auto, Manual }

    [DisplayName("AtasBridge")]
    [Description("BTC AI Bridge - Bar+Footprint+LargeTrade+Absorption (v2026.07.06-2, Auto Identity Detection)")]
    public class AtasBridge : Indicator
    {
        [Display(Name = "VPS URL", GroupName = "1. Config", Order = 1)]
        public string VpsUrl { get; set; } = "https://mb.661688.xyz";

        [Display(Name = "Auth Token", GroupName = "1. Config", Order = 2)]
        public string AuthToken { get; set; } = "";

        [Display(Name = "Timeframe Label", GroupName = "1. Config", Order = 3)]
        public string Timeframe { get; set; } = "5m";

        // ── v5.0 新增：这张图表的身份标签（默认Unset，必须手动选一次）────
        [Display(Name = "Exchange", GroupName = "1. Config", Order = 4)]
        public ExchangeName Exchange { get; set; } = ExchangeName.Unset;

        [Display(Name = "Market Type", GroupName = "1. Config", Order = 5)]
        public MarketKind MarketType { get; set; } = MarketKind.Unset;

        // Phase 7H Stage2: Auto (default) parses exchange/market_type from
        // InstrumentInfo.Exchange automatically (see TryParseAutoIdentity).
        // Manual ignores auto-detection entirely, behaving exactly like the
        // pre-7H version (Exchange/MarketType dropdowns above used as-is,
        // unconditionally). In Auto mode the parsed value always wins for
        // actual data (push payloads + the OKX x0.01 conversion trigger)
        // regardless of what the dropdowns say - Manual exists purely as a
        // fallback channel for instruments Auto cannot recognize.
        [Display(Name = "Identity Mode", GroupName = "1. Config", Order = 6)]
        public IdentityMode IdentityModeSetting { get; set; } = IdentityMode.Auto;

        [Display(Name = "Enable Bar Push", GroupName = "2. Switch", Order = 1)]
        public bool EnableBarPush { get; set; } = true;

        [Display(Name = "Enable Trade Push", GroupName = "2. Switch", Order = 2)]
        public bool EnableTradePush { get; set; } = true;

        [Display(Name = "Enable Footprint", GroupName = "2. Switch", Order = 3)]
        public bool EnableFootprint { get; set; } = true;

        // Phase 7F: native absorption detection, replaces the old ATAS built-in
        // Absorption webhook (/atas/signal) which cannot carry price/volume.
        [Display(Name = "Enable Absorption Push", GroupName = "2. Switch", Order = 4)]
        public bool EnableAbsorptionPush { get; set; } = true;

        [Display(Name = "Medium BTC (db only)", GroupName = "3. Thresholds", Order = 1)]
        public decimal ThresholdMedium { get; set; } = 20m;

        [Display(Name = "Large BTC (db+TG)", GroupName = "3. Thresholds", Order = 2)]
        public decimal ThresholdLarge { get; set; } = 100m;

        [Display(Name = "Whale BTC (urgent TG)", GroupName = "3. Thresholds", Order = 3)]
        public decimal ThresholdWhale { get; set; } = 300m;

        [Display(Name = "Min level volume BTC", GroupName = "3. Thresholds", Order = 4)]
        public decimal FpMinVolume { get; set; } = 3m;

        // Phase 7F: absorption thresholds. Dominant side volume (BTC, already
        // converted for OKX perp) must reach AbsorbMinBtc AND be at least
        // AbsorbRatio times the opposite side to count as absorption.
        [Display(Name = "Absorb Min BTC", GroupName = "4. Absorption", Order = 1)]
        public decimal AbsorbMinBtc { get; set; } = 15.0m;

        [Display(Name = "Absorb Ratio", GroupName = "4. Absorption", Order = 2)]
        public decimal AbsorbRatio { get; set; } = 3.0m;

        // Phase 7H Stage1 (recon) -> Stage2 (formal): master on/off switch for
        // the corner overlay. Stage1 showed a raw field dump so Sea could
        // screenshot all four charts and confirm real values (7F lesson:
        // never guess parsing rules from API docs alone); Stage2 replaced the
        // on-chart content with the operational Auto/Manual status label
        // (see ComputeIdentityLabel) now that the parsing rule is confirmed.
        [Display(Name = "Show Identity Label", GroupName = "5. Identity Recon", Order = 1)]
        public bool ShowIdentityLabel { get; set; } = true;

        private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };

        private decimal  _cvd     = 0m;
        private string   _cvdDate = "";
        private int      _lastBar = -1;
        private decimal? _pocPrice  = null;
        private decimal  _barDelta  = 0m;

        // Phase 7F: absorption dedup state. Tracked separately from _lastBar
        // (which gates the closed-bar push) because absorption must be checked
        // on every tick of the still-forming current bar, not once per close.
        private int _absorbBar = -1;
        private readonly HashSet<(decimal price, string side)> _absorbSeen = new();

        // Phase 7H Stage1: identity recon state. Logged a few times (not just
        // once) because TradingManager.Security can still be null on the very
        // first bar before ATAS finishes connecting - capping at 3 avoids log
        // spam while still catching late-populated fields.
        private int _identityLogCount = 0;
        private const int IDENTITY_LOG_MAX = 3;

        // Phase 7H Stage2: tracks the /atas/bar push outcome, used by the
        // corner label's status indicator (checkmark/cross + last time).
        // _lastPushBjTime stays null until the first attempt so the label can
        // show a neutral "..." instead of a misleading premature checkmark.
        private bool      _lastPushOk     = false;
        private DateTime? _lastPushBjTime = null;
        private int       _pushFailCount  = 0;

        // --- large trade dedup ---
        // 2026-07-01 重构：原来用单变量 _lastTrade/_lastLevel 只记"最近一笔"，
        // 如果两笔不同方向/不同价位的单子几乎同时在累积（买方在A价位堆量的
        // 同时卖方在B价位也在堆量，市场里很常见），后触发的会把前一笔的追踪
        // 状态覆盖掉——前一笔继续更新时会被误判成"全新的单子"，导致重复推送。
        // 改用 ConditionalWeakTable 按每个 trade 对象独立追踪，互不干扰；
        // 同时顺便记录"首次识别时的量/首次识别时间/更新次数"，用于诊断——
        // 下次再出现"消息报了个大数字但盘面看不出来"这种情况，可以直接从
        // 消息里的"累计轨迹"判断：是几秒内平缓涨上去的（大概率真实），
        // 还是几乎瞬间跳上去的（值得怀疑 ATAS 内部把不相关的东西合并了）。
        private readonly ConditionalWeakTable<CumulativeTrade, TradeTrack> _tracked = new();

        private sealed class TradeTrack
        {
            public string   LastLevel    = "";
            public decimal  FirstVolume;
            public DateTime FirstSeenUtc;
            public int      UpdateCount;
        }

        // Phase 7H Stage1: EnableCustomDrawing defaults to false on a plain
        // Indicator (confirmed via reflection - ATAS.Indicators.Technical's
        // built-in Watermark explicitly sets it true in its own constructor).
        // Without this, ATAS never invokes OnRender at all, so the identity
        // corner label silently never appears - this was the actual bug
        // behind Sea's "no label visible after redeploy" report, not the
        // OnRender/DrawingLayouts logic itself.
        public AtasBridge() : base(true) { DenyToChangePanel = true; EnableCustomDrawing = true; }

        // ── OKX 永续合约"张→BTC"换算 ─────────────────────────────────────
        // OKX 永续合约(SWAP)成交量单位是"张"(contract)，1张=0.01 BTC（OKX官方
        // 文档；这个项目在爆仓监控那条独立管线里已经踩过一次同样的坑并修过，
        // 参见 monitor/liquidation_monitor.py 里 sz*0.01*price 那处）。ATAS 从
        // OKX 拿到的原始 Volume/Delta/Bid/Ask/OI 等字段大概率也是"张数"未转换
        // 成 BTC——现货和币安都是直接以 BTC 计价，不受影响。
        //
        // 这个换算是根据"OKX永续单笔动辄千万级别、且正好是100倍(对应0.01这个
        // 系数)"这个现象反推出来的，不是查了ATAS官方文档确认的，需要部署后
        // 用 ATAS 自带的 Big trades / Cluster Search 指标交叉核对同一笔OKX
        // 永续大单的数量级来验证方向对不对，如果反了这个乘数很容易撤回。
        //
        // 换算必须在"判断是否达到大单门槛"之前就应用，不能只在推送给VPS的
        // 最后一刻才转换——否则 CheckAndPost 里会拿"张数"直接跟以BTC为单位
        // 的 Medium/Large/Whale 门槛比较，把很多稀松平常的小额OKX成交(比如
        // 3-5 BTC)误判成"大额"甚至"鲸鱼级"，这很可能也是 OKX 这边消息明显
        // 比其他三路更频繁的原因之一。
        private const decimal OKX_CONTRACT_TO_BTC = 0.01m;

        private decimal VolumeUnitMultiplier
        {
            get
            {
                var (exch, mkt, _, _) = ResolveEffectiveIdentity();
                return (exch == ExchangeName.Okx && mkt == MarketKind.Perp)
                    ? OKX_CONTRACT_TO_BTC : 1.0m;
            }
        }

        // ── Phase 7H Stage2: auto identity detection ─────────────────────────
        // Parsing rule confirmed from real observed values across all four
        // charts (Sea's screenshots, 2026-07-06) - not guessed from ATAS API
        // docs (7F lesson). Only InstrumentInfo.Exchange is used:
        // TradingManager.Security was null on both OKX charts at recon time,
        // so a rule relying on Security.Type/ConnectorId would never resolve
        // for OKX. Exact match only (case-insensitive), no substring/prefix
        // matching, so a future connector string like "BinanceFuturesCoin"
        // cannot be silently swallowed into an existing rule.
        private bool TryParseAutoIdentity(out ExchangeName exch, out MarketKind mkt)
        {
            exch = ExchangeName.Unset;
            mkt  = MarketKind.Unset;

            string? raw = null;
            try { raw = InstrumentInfo?.Exchange; } catch { }
            if (string.IsNullOrEmpty(raw)) return false;

            switch (raw.Trim().ToLowerInvariant())
            {
                case "binance":        exch = ExchangeName.Binance; mkt = MarketKind.Spot; return true;
                case "binancefutures": exch = ExchangeName.Binance; mkt = MarketKind.Perp; return true;
                case "okxspot":        exch = ExchangeName.Okx;     mkt = MarketKind.Spot; return true;
                case "okxperpfutures": exch = ExchangeName.Okx;     mkt = MarketKind.Perp; return true;
                default: return false;
            }
        }

        // The identity actually used for pushes + the OKX conversion trigger.
        // Manual mode: just the dropdowns, unchanged from the pre-7H version.
        // Auto mode: parse failure falls back to Unset (same warning-log path
        // as the pre-7H "forgot to configure" case) rather than guessing.
        private (ExchangeName exch, MarketKind mkt, bool autoOk, bool conflict) ResolveEffectiveIdentity()
        {
            if (IdentityModeSetting == IdentityMode.Manual)
                return (Exchange, MarketType, false, false);

            bool ok = TryParseAutoIdentity(out var aExch, out var aMkt);
            if (!ok)
                return (ExchangeName.Unset, MarketKind.Unset, false, false);

            bool conflict = aExch != Exchange || aMkt != MarketType;
            return (aExch, aMkt, true, conflict);
        }

        // ══ Phase 2A + 2B: Bar + Footprint ═══════════════════════════════════

        protected override void OnCalculate(int bar, decimal value)
        {
            // Phase 7H Stage1: pure reconnaissance, runs first and touches
            // nothing below. Only adds a corner label + log lines.
            if (ShowIdentityLabel) UpdateIdentityRecon(bar);

            // Phase 7F: must run before the "bar <= _lastBar" early return below,
            // because absorption needs to be checked on every tick of the
            // still-forming current bar, not just once when a bar closes.
            if (EnableAbsorptionPush) CheckAbsorption(bar);

            if (bar <= _lastBar) return;
            if (bar == 0) { _lastBar = 0; return; }

            int closedBar = bar - 1;
            var candle = GetCandle(closedBar);
            if (candle is null) { _lastBar = bar; return; }

            try
            {
                // 2026-07-01 修复：跟 bjTime 那处是同一个bug——如果 candle.LastTime
                // 也存在 Kind=Unspecified 但取值其实是UTC的情况，.ToUniversalTime()
                // 会把它误当成本地(北京)时间倒扣8小时，导致算出来的"K线年龄"凭空多了
                // 8小时，8小时远超下面10分钟的阈值，等于每一根K线都会被判定"太旧"而
                // 直接跳过、永远推不到 /atas/bar。改用 SpecifyKind 避免这个误判。
                var candleUtc = DateTime.SpecifyKind(candle.LastTime, DateTimeKind.Utc);
                if ((DateTime.UtcNow - candleUtc).TotalMinutes > 10)
                {
                    _pocPrice = candle.MaxVolumePriceInfo?.Price;
                    _barDelta = candle.Delta * VolumeUnitMultiplier;
                    _lastBar  = bar;
                    return;
                }
            }
            catch { _lastBar = bar; return; }

            string today = DateTime.UtcNow.ToString("yyyy-MM-dd");
            if (_cvdDate != today) { _cvd = 0m; _cvdDate = today; }
            _cvd += candle.Delta * VolumeUnitMultiplier;

            _pocPrice = candle.MaxVolumePriceInfo?.Price;
            _barDelta = candle.Delta * VolumeUnitMultiplier;

            if (EnableBarPush) _ = PostBarAsync(candle);
            _lastBar = bar;
        }

        private async Task PostBarAsync(IndicatorCandle c)
        {
            try
            {
                // 2026-07-01 修复：c.LastTime 的 DateTimeKind 是 Unspecified，但取值
                // 其实已经是 UTC（交易所时间戳）。用 .ToUniversalTime() 会被 .NET
                // 误判成"这是本机所在时区(北京)的当地时间"，先倒扣8小时"转成UTC"，
                // 之后再 .AddHours(8) 加回来，两步相互抵消，最终结果还是原始UTC值，
                // 只是被打上了错误的"+08:00"标签——这就是之前 Telegram 推送里显示的
                // 时间比真实北京时间整整慢8小时的原因。改用 SpecifyKind 明确声明
                // 原始值就是 UTC，不经过 .NET 的本地时区猜测，再加8小时得到真正北京时间。
                var bjTime = DateTime.SpecifyKind(c.LastTime, DateTimeKind.Utc).AddHours(8);
                var (idExch, idMkt, _, _) = ResolveEffectiveIdentity();
                var mult   = VolumeUnitMultiplier;
                double? poc  = c.MaxVolumePriceInfo?.Price        is decimal p1 ? (double)p1 : null;
                double? mpd  = c.MaxPositiveDeltaPriceInfo?.Price is decimal p2 ? (double)p2 : null;
                double? mnd  = c.MaxNegativeDeltaPriceInfo?.Price is decimal p3 ? (double)p3 : null;
                double? mtk  = c.MaxTickPriceInfo?.Price          is decimal p4 ? (double)p4 : null;

                // ── Footprint: top 10 by volume + top 5 bid-absorb + top 5 ask-absorb
                List<FpLevel>? topLevels = null;
                if (EnableFootprint)
                {
                    // 最小量门槛用换算后的量比较（FpMinVolume是以BTC为单位设置的）
                    var allRaw = c.GetAllPriceLevels()
                        .Where(l => l != null && l.Volume * mult >= FpMinVolume)
                        .ToList();

                    if (allRaw.Count > 0)
                    {
                        var byVol = allRaw
                            .OrderByDescending(l => l.Volume)
                            .Take(10)
                            .Select(l => ToFpLevel(l, "vol", mult));

                        var bidAbsorb = allRaw
                            .Where(l => l.Ask > 0 && l.Bid / l.Ask >= 2.0m)
                            .OrderByDescending(l => l.Bid)
                            .Take(5)
                            .Select(l => ToFpLevel(l, "bid_absorb", mult));

                        var askAbsorb = allRaw
                            .Where(l => l.Bid > 0 && l.Ask / l.Bid >= 2.0m)
                            .OrderByDescending(l => l.Ask)
                            .Take(5)
                            .Select(l => ToFpLevel(l, "ask_absorb", mult));

                        topLevels = byVol
                            .Concat(bidAbsorb)
                            .Concat(askAbsorb)
                            .DistinctBy(l => l.Price)
                            .ToList();
                    }
                }

                var payload = new BarPayload
                {
                    Timestamp        = bjTime.ToString("yyyy-MM-ddTHH:mm:ss+08:00"),
                    Timeframe        = Timeframe,
                    Exchange         = idExch.ToString().ToLowerInvariant(),
                    MarketType       = idMkt.ToString().ToLowerInvariant(),
                    Open             = (double)c.Open,
                    High             = (double)c.High,
                    Low              = (double)c.Low,
                    Close            = (double)c.Close,
                    Volume           = (double)(c.Volume * mult),
                    AskVol           = (double)(c.Ask * mult),
                    BidVol           = (double)(c.Bid * mult),
                    Delta            = (double)(c.Delta * mult),
                    CumulativeDelta  = (double)_cvd,
                    MaxDelta         = (double)(c.MaxDelta * mult),
                    MinDelta         = (double)(c.MinDelta * mult),
                    MaxOi            = (double)(c.MaxOI * mult),
                    MinOi            = (double)(c.MinOI * mult),
                    OiChange         = (double)((c.MaxOI - c.MinOI) * mult),
                    PocPrice         = poc,
                    MaxVolPrice      = poc,
                    MaxPosDeltaPrice = mpd,
                    MaxNegDeltaPrice = mnd,
                    MaxTickPrice     = mtk,
                    TopLevels        = topLevels,
                    Source           = "AtasBridge/5.1"
                };
                await SendAsync("/atas/bar", payload);

                // Phase 7H Stage2: feeds the corner label's status indicator.
                _lastPushOk     = true;
                _pushFailCount  = 0;
                _lastPushBjTime = DateTime.UtcNow.AddHours(8);
            }
            catch
            {
                _lastPushOk = false;
                _pushFailCount++;
            }
        }

        private FpLevel ToFpLevel(PriceVolumeInfo l, string tag, decimal mult) => new FpLevel
        {
            Price  = (double)l.Price,
            Volume = (double)(l.Volume * mult),
            Bid    = (double)(l.Bid * mult),
            Ask    = (double)(l.Ask * mult),
            Delta  = (double)((l.Ask - l.Bid) * mult),
            Tag    = tag
        };

        // ══ Phase 7F: native absorption detection ══════════════════════════════
        // Runs on the still-forming current bar's footprint on every tick.
        // For each price level: whichever side (bid/ask) dominates is compared
        // against AbsorbMinBtc and AbsorbRatio; if both thresholds are met this
        // counts as absorption at that price. Same (price, side) only fires once
        // per bar — the dedup set is cleared whenever a new bar starts.

        private void CheckAbsorption(int bar)
        {
            if (bar != _absorbBar)
            {
                _absorbBar = bar;
                _absorbSeen.Clear();
            }

            var candle = GetCandle(bar);
            if (candle is null) return;

            var mult = VolumeUnitMultiplier;
            foreach (var level in candle.GetAllPriceLevels())
            {
                if (level is null) continue;

                decimal bid = level.Bid * mult;
                decimal ask = level.Ask * mult;

                string  side;
                decimal dominant, other;
                if (bid > ask)      { side = "bid_absorb"; dominant = bid; other = ask; }
                else if (ask > bid) { side = "ask_absorb"; dominant = ask; other = bid; }
                else continue;

                if (dominant < AbsorbMinBtc) continue;

                decimal ratio = other > 0 ? dominant / other : decimal.MaxValue;
                if (ratio < AbsorbRatio) continue;

                var key = (level.Price, side);
                if (!_absorbSeen.Add(key)) continue;   // already fired this bar

                _ = PostAbsorptionAsync(level.Price, side, dominant, bid, ask, ratio);
            }
        }

        private async Task PostAbsorptionAsync(decimal price, string side, decimal absorbedBtc,
                                                decimal bidVol, decimal askVol, decimal ratio)
        {
            try
            {
                var bjTime = DateTime.UtcNow.AddHours(8);
                var (idExch, idMkt, _, _) = ResolveEffectiveIdentity();
                var payload = new AbsorptionPayload
                {
                    Timestamp   = bjTime.ToString("yyyy-MM-ddTHH:mm:ss+08:00"),
                    Exchange    = idExch.ToString().ToLowerInvariant(),
                    MarketType  = idMkt.ToString().ToLowerInvariant(),
                    Side        = side,
                    Price       = (double)price,
                    AbsorbedBtc = (double)absorbedBtc,
                    BidVol      = (double)bidVol,
                    AskVol      = (double)askVol,
                    // Cap the reported ratio when the opposite side is ~0 so the
                    // JSON number stays sane instead of an astronomically large value
                    Ratio       = (double)Math.Min(ratio, 999m),
                    Source      = "AtasBridge/5.1"
                };
                await SendAsync("/atas/absorption", payload);
            }
            catch { }
        }

        // ══ Phase 7H Stage1: identity reconnaissance ═══════════════════════════
        // Read-only. Does not touch Exchange/MarketType settings or any push
        // payload. Purpose: dump every identity-related field ATAS actually
        // exposes for this chart's instrument (Indicator.Instrument,
        // InstrumentInfo, TradingManager.Security) to the chart corner and the
        // ATAS log, so Sea can screenshot all four charts and we can design
        // the real Stage2 auto-detection rules from what is actually observed
        // - not from guessing based on the SDK's property names (7F lesson).

        private void UpdateIdentityRecon(int bar)
        {
            // On-chart display is handled by OnRender (screen-anchored corner
            // overlay - see below). This method only owns the capped log dump,
            // triggered from OnCalculate on the normal bar-close cadence.
            if (_identityLogCount < IDENTITY_LOG_MAX)
            {
                _identityLogCount++;
                try { LoggerHelper.LogInfo(this, "{0}", new object[] { BuildIdentityDump() }); } catch { }
            }
        }

        // Fixed screen-space overlay in the chart's top-left corner, same
        // technique as ATAS's own built-in "Watermark" indicator (confirmed
        // via reflection: ATAS.Indicators.Technical.Watermark overrides this
        // same OnRender(RenderContext, DrawingLayouts) method declared on
        // ExtendedIndicator, which Indicator itself extends). Unlike
        // Labels/DrawingText (bar+price anchored, scrolls off-screen with the
        // chart), this stays pinned to the corner regardless of scroll/zoom.
        private static readonly RenderFont _identityRenderFont = new RenderFont("Arial", 13f);

        protected override void OnRender(RenderContext context, DrawingLayouts layout)
        {
            base.OnRender(context, layout);

            if (!ShowIdentityLabel) return;
            // Confirmed via a Stage1 diagnostic log that ATAS calls this with
            // layout=LatestBar(4) for this indicator, not Final(8) - drawing
            // unconditionally on every call remains the reliable choice since
            // it is a single line of text (harmless if it ever fires more
            // than once per frame; redraws just overlap at the same pixel).

            try
            {
                var (text, color) = ComputeIdentityLabel();
                var size = context.MeasureString(text, _identityRenderFont);
                const int x = 8, y = 8, pad = 4;

                context.FillRectangle(
                    Color.FromArgb(190, 0, 0, 0),
                    new Rectangle(x - pad, y - pad, size.Width + pad * 2, size.Height + pad * 2));
                context.DrawString(text, _identityRenderFont, color, x, y);
            }
            catch { }
        }

        // Phase 7H Stage2: operational status label (replaces Stage1's raw
        // field dump now that the parsing rule is confirmed from real data).
        // Four states:
        //   - Auto mode, parse failed        -> red "UNSET" (no guessing)
        //   - Auto mode, parsed but conflicts
        //     with the manual dropdowns       -> yellow, shows both values
        //   - Auto mode, parsed and resolved  -> green/orange-red by push status
        //   - Manual mode                     -> same status style, tagged MANUAL
        private (string text, Color color) ComputeIdentityLabel()
        {
            string statusSym = !_lastPushBjTime.HasValue
                ? "..."
                : (_lastPushOk ? "✓" : $"✗ x{_pushFailCount}");
            string timeStr = _lastPushBjTime.HasValue
                ? _lastPushBjTime.Value.ToString("HH:mm:ss")
                : "--:--:--";
            Color okColor = _lastPushOk ? Color.LightGreen : Color.OrangeRed;

            if (IdentityModeSetting == IdentityMode.Manual)
                return ($"{Exchange}|{MarketType} MANUAL {statusSym} {timeStr}", okColor);

            bool ok = TryParseAutoIdentity(out var aExch, out var aMkt);
            if (!ok)
                return ("UNSET (raw identity not recognized)", Color.Red);

            bool conflict = aExch != Exchange || aMkt != MarketType;
            if (conflict)
                return ($"AUTO {aExch}|{aMkt} ≠ 手动 {Exchange}|{MarketType}", Color.Yellow);

            return ($"{aExch}|{aMkt} AUTO {statusSym} {timeStr}", okColor);
        }

        // Full multi-line dump for the ATAS log - every identity-related field
        // reachable from the Indicator base class and TradingManager.Security.
        private string BuildIdentityDump()
        {
            var sb = new StringBuilder();
            sb.AppendLine("=== AtasBridge Phase7H Stage1 Identity Recon ===");

            try { sb.AppendLine($"Indicator.Instrument = {Instrument}"); }
            catch (Exception ex) { sb.AppendLine($"Indicator.Instrument = <error: {ex.Message}>"); }

            try
            {
                var ii = InstrumentInfo;
                if (ii != null)
                {
                    sb.AppendLine($"InstrumentInfo.Instrument = {ii.Instrument}");
                    sb.AppendLine($"InstrumentInfo.Exchange   = {ii.Exchange}");
                    sb.AppendLine($"InstrumentInfo.TickSize   = {ii.TickSize}");
                    sb.AppendLine($"InstrumentInfo.TimeZone   = {ii.TimeZone}");
                }
                else sb.AppendLine("InstrumentInfo = null");
            }
            catch (Exception ex) { sb.AppendLine($"InstrumentInfo access error: {ex.Message}"); }

            try
            {
                var sec = TradingManager?.Security;
                if (sec != null)
                {
                    sb.AppendLine($"Security.Instrument      = {sec.Instrument}");
                    sb.AppendLine($"Security.Exchange        = {sec.Exchange}");
                    sb.AppendLine($"Security.Code             = {sec.Code}");
                    sb.AppendLine($"Security.ConnectorId      = {sec.ConnectorId}");
                    sb.AppendLine($"Security.Type (SecType)   = {sec.Type}");
                    sb.AppendLine($"Security.IsInverseFutures = {sec.IsInverseFutures}");
                    sb.AppendLine($"Security.BaseCurrency     = {sec.BaseCurrency}");
                    sb.AppendLine($"Security.QuoteCurrency    = {sec.QuoteCurrency}");
                    sb.AppendLine($"Security.FundingRate      = {sec.FundingRate}");
                    sb.AppendLine($"Security.NextFundingTime  = {sec.NextFundingTime}");
                    sb.AppendLine($"Security.Expiration       = {sec.Expiration}");
                    sb.AppendLine($"Security.Id / SecurityId  = {sec.Id} / {sec.SecurityId}");
                }
                else sb.AppendLine("TradingManager.Security = null (not yet available)");
            }
            catch (Exception ex) { sb.AppendLine($"TradingManager.Security access error: {ex.Message}"); }

            sb.AppendLine("Current manual settings: Exchange=" + Exchange + " MarketType=" + MarketType);
            return sb.ToString();
        }

        // ══ Phase 3: Large trade + update tracking ════════════════════════════

        protected override void OnCumulativeTrade(CumulativeTrade trade)
        {
            if (!EnableTradePush) return;
            CheckAndPost(trade, isUpdate: false);
        }

        protected override void OnUpdateCumulativeTrade(CumulativeTrade trade)
        {
            if (!EnableTradePush) return;
            CheckAndPost(trade, isUpdate: true);
        }

        private void CheckAndPost(CumulativeTrade trade, bool isUpdate)
        {
            // 门槛判断必须用换算后的量——OKX永续原始trade.Volume是"张数"，
            // 不转换直接跟以BTC为单位的门槛比较，会把很多平常大小的成交
            // 误判成大额/鲸鱼级
            decimal volumeBtc = trade.Volume * VolumeUnitMultiplier;
            if (volumeBtc < ThresholdMedium) return;

            string level = volumeBtc >= ThresholdWhale ? "whale"
                         : volumeBtc >= ThresholdLarge ? "large"
                         : "medium";

            bool isFirstSeen = !_tracked.TryGetValue(trade, out var track);
            if (isFirstSeen)
            {
                track = new TradeTrack
                {
                    FirstVolume  = volumeBtc,
                    FirstSeenUtc = DateTime.UtcNow,
                };
                _tracked.Add(trade, track);
            }
            track!.UpdateCount++;

            // Only re-post if this trade crossed a new (higher) threshold level
            bool isUpgraded = !isFirstSeen && track.LastLevel != level &&
                               LevelRank(level) > LevelRank(track.LastLevel);

            if (!isFirstSeen && !isUpgraded) return;

            track.LastLevel = level;
            _ = PostTradeAsync(trade, volumeBtc, level, isUpdate && !isFirstSeen, track);
        }

        private static int LevelRank(string l) =>
            l == "whale" ? 3 : l == "large" ? 2 : 1;

        private async Task PostTradeAsync(CumulativeTrade trade, decimal volumeBtc, string level, bool isUpdate, TradeTrack track)
        {
            try
            {
                // 时区修复同 PostBarAsync，原因见那边的详细注释
                var bjTime     = DateTime.SpecifyKind(trade.Time, DateTimeKind.Utc).AddHours(8);
                var dirStr     = trade.Direction.ToString();
                var dir        = dirStr.IndexOf("Buy", StringComparison.OrdinalIgnoreCase) >= 0
                                 ? "buy" : "sell";
                var tradePrice = trade.FirstPrice;
                var volUsd     = (double)(volumeBtc * tradePrice);
                var (idExch, idMkt, _, _) = ResolveEffectiveIdentity();

                double? distPct = null;
                if (_pocPrice.HasValue && _pocPrice.Value > 0)
                    distPct = Math.Round(
                        (double)((tradePrice - _pocPrice.Value) / _pocPrice.Value * 100m), 3);

                var payload = new TradePayload
                {
                    Timestamp        = bjTime.ToString("yyyy-MM-ddTHH:mm:ss.fff+08:00"),
                    Exchange         = idExch.ToString().ToLowerInvariant(),
                    MarketType       = idMkt.ToString().ToLowerInvariant(),
                    Price            = (double)tradePrice,
                    Volume           = (double)volumeBtc,
                    VolumeUsd        = volUsd,
                    Direction        = dir,
                    ThresholdLevel   = level,
                    IsUpdate         = isUpdate,
                    NearPoc          = distPct.HasValue && Math.Abs(distPct.Value) < 0.15,
                    PocPrice         = _pocPrice.HasValue ? (double?)_pocPrice.Value : null,
                    DistFromPocPct   = distPct,
                    CurrentBarDelta  = (double)_barDelta,
                    CurrentCvd       = (double)_cvd,
                    // 诊断字段：这笔单子首次被识别到时的量 / 从首次识别到现在过了多久 /
                    // 期间 OnUpdateCumulativeTrade 触发了几次——帮助判断这个最终量
                    // 是平缓累积上来的，还是可疑地"凭空跳出来"的
                    FirstSeenVolume  = (double)track.FirstVolume,
                    GrowthSeconds    = (DateTime.UtcNow - track.FirstSeenUtc).TotalSeconds,
                    UpdateCount      = track.UpdateCount,
                    Source           = "AtasBridge/5.1"
                };
                await SendAsync("/atas/trade", payload);
            }
            catch { }
        }

        // ── HTTP helper ────────────────────────────────────────────────────────

        private async Task SendAsync<T>(string path, T payload)
        {
            var json = JsonSerializer.Serialize(payload, _serOpts);
            var req  = new HttpRequestMessage(HttpMethod.Post,
                           $"{VpsUrl.TrimEnd('/')}{path}")
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json")
            };
            if (!string.IsNullOrEmpty(AuthToken))
                req.Headers.Authorization =
                    new AuthenticationHeaderValue("Bearer", AuthToken);
            await _http.SendAsync(req).ConfigureAwait(false);
        }

        private static readonly JsonSerializerOptions _serOpts = new()
        {
            PropertyNamingPolicy   = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        };
    }

    // ── Data models ────────────────────────────────────────────────────────────

    public sealed class FpLevel
    {
        public double Price  { get; set; }
        public double Volume { get; set; }
        public double Bid    { get; set; }
        public double Ask    { get; set; }
        public double Delta  { get; set; }
        public string Tag    { get; set; } = "";
    }

    public sealed class BarPayload
    {
        public string        Timestamp        { get; set; } = "";
        public string        Timeframe        { get; set; } = "5m";
        // v5.0 新增：这两个字段序列化后是 "exchange"/"market_type"，
        // 值是纯小写字符串（"binance"/"okx"、"spot"/"perp"），在
        // PostBarAsync 里由 Exchange/MarketType 这两个设置项(枚举类型)
        // 转成字符串后手动赋值——特意不直接序列化枚举本身，因为
        // System.Text.Json 默认把枚举序列化成数字，那样VPS那边就要
        // 反过来猜0/1对应哪个交易所，容易出错，不如直接给字符串。
        public string        Exchange         { get; set; } = "";
        public string        MarketType       { get; set; } = "";
        public double        Open             { get; set; }
        public double        High             { get; set; }
        public double        Low              { get; set; }
        public double        Close            { get; set; }
        public double        Volume           { get; set; }
        public double        AskVol           { get; set; }
        public double        BidVol           { get; set; }
        public double        Delta            { get; set; }
        public double        CumulativeDelta  { get; set; }
        public double        MaxDelta         { get; set; }
        public double        MinDelta         { get; set; }
        public double        MaxOi            { get; set; }
        public double        MinOi            { get; set; }
        public double        OiChange         { get; set; }
        public double?       PocPrice         { get; set; }
        public double?       MaxVolPrice      { get; set; }
        public double?       MaxPosDeltaPrice { get; set; }
        public double?       MaxNegDeltaPrice { get; set; }
        public double?       MaxTickPrice     { get; set; }
        public List<FpLevel>? TopLevels       { get; set; }
        public string        Source           { get; set; } = "AtasBridge/5.1";
    }

    public sealed class TradePayload
    {
        public string  Timestamp       { get; set; } = "";
        // v5.0 新增，同 BarPayload 的处理方式（见上方注释）
        public string  Exchange        { get; set; } = "";
        public string  MarketType      { get; set; } = "";
        public double  Price           { get; set; }
        public double  Volume          { get; set; }
        public double  VolumeUsd       { get; set; }
        public string  Direction       { get; set; } = "";
        public string  ThresholdLevel  { get; set; } = "";
        public bool    IsUpdate        { get; set; }
        public bool    NearPoc         { get; set; }
        public double? PocPrice        { get; set; }
        public double? DistFromPocPct  { get; set; }
        public double  CurrentBarDelta { get; set; }
        public double  CurrentCvd      { get; set; }
        // v5.1 新增：累计过程诊断字段，帮助判断大单数值是否合理
        public double  FirstSeenVolume { get; set; }
        public double  GrowthSeconds   { get; set; }
        public int     UpdateCount     { get; set; }
        public string  Source          { get; set; } = "AtasBridge/5.1";
    }

    // Phase 7F: native absorption push payload. Field names serialize to
    // snake_case via _serOpts (same as BarPayload/TradePayload), matching
    // the /atas/absorption schema on the VPS side.
    public sealed class AbsorptionPayload
    {
        public string Timestamp   { get; set; } = "";
        public string Exchange    { get; set; } = "";
        public string MarketType  { get; set; } = "";
        public string Instrument  { get; set; } = "BTCUSDT";
        public string Side        { get; set; } = "";
        public double Price       { get; set; }
        public double AbsorbedBtc { get; set; }
        public double BidVol      { get; set; }
        public double AskVol      { get; set; }
        public double Ratio       { get; set; }
        public string Source      { get; set; } = "AtasBridge/5.1";
    }
}
