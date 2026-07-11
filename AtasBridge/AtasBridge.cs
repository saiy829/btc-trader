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
using System.Drawing.Drawing2D;
using ATAS.Indicators;
using ATAS.Indicators.Drawing;
using Utils.Common;
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

    // Phase 7I: corner label anchor. Default moved to BottomLeft per this
    // card's request (avoids overlapping ATAS's own top-left indicator name
    // list, which is where the Stage1/Stage2 corner label used to sit).
    public enum LabelPosition { BottomLeft, TopLeft, BottomRight, TopRight }

    // Phase 7I hotfix: single source of truth for the version tag, referenced
    // both by the class [Description] attribute below and by the corner
    // label / read-only Version setting, so they cannot drift out of sync.
    internal static class AtasBridgeVersion
    {
        public const string Tag  = "v2026.07.11-3";
        public const string Desc = "Hide Unused Default DataSeries";
    }

    [DisplayName("AtasBridge")]
    [Description("BTC AI Bridge - Bar+Footprint+LargeTrade+Absorption (" + AtasBridgeVersion.Tag + ", " + AtasBridgeVersion.Desc + ")")]
    public class AtasBridge : Indicator
    {
        // Phase 7I hotfix: read-only version display, requested so the build
        // in use is visible from the indicator's own settings without
        // needing to check the "About" tab or file properties externally.
        // Not wired into any logic - purely informational.
        // Phase 7K: settings panel labels translated to Chinese per Sea's
        // request. Only Display(Name=)/GroupName= (native ATAS settings UI
        // text, rendered through ATAS's own WPF/Avalonia UI - already shown
        // to render Chinese fine, e.g. the panel's own "关于"/"设置" labels)
        // - NOT enum member values (Binance/Okx/Auto/Manual/etc, which are
        // read back via ToString() for JSON payloads and internal logic, so
        // renaming those would break functionality, not just cosmetics).
        [Display(Name = "版本号", GroupName = "1. 基础配置", Order = 0)]
        [System.ComponentModel.ReadOnly(true)]
        public string VersionInfo { get; set; } = AtasBridgeVersion.Tag + " (" + AtasBridgeVersion.Desc + ")";

        [Display(Name = "VPS 地址", GroupName = "1. 基础配置", Order = 1)]
        public string VpsUrl { get; set; } = "https://mb.661688.xyz";

        [Display(Name = "认证令牌", GroupName = "1. 基础配置", Order = 2)]
        public string AuthToken { get; set; } = "";

        [Display(Name = "时间周期标签", GroupName = "1. 基础配置", Order = 3)]
        public string Timeframe { get; set; } = "5m";

        // ── v5.0 新增：这张图表的身份标签（默认Unset，必须手动选一次）────
        [Display(Name = "交易所", GroupName = "1. 基础配置", Order = 4)]
        public ExchangeName Exchange { get; set; } = ExchangeName.Unset;

        [Display(Name = "市场类型", GroupName = "1. 基础配置", Order = 5)]
        public MarketKind MarketType { get; set; } = MarketKind.Unset;

        // Phase 7H Stage2: Auto (default) parses exchange/market_type from
        // InstrumentInfo.Exchange automatically (see TryParseAutoIdentity).
        // Manual ignores auto-detection entirely, behaving exactly like the
        // pre-7H version (Exchange/MarketType dropdowns above used as-is,
        // unconditionally). In Auto mode the parsed value always wins for
        // actual data (push payloads + the OKX x0.01 conversion trigger)
        // regardless of what the dropdowns say - Manual exists purely as a
        // fallback channel for instruments Auto cannot recognize.
        [Display(Name = "身份识别模式", GroupName = "1. 基础配置", Order = 6)]
        public IdentityMode IdentityModeSetting { get; set; } = IdentityMode.Auto;

        // Phase 7K: master switch. Sea runs this indicator on both ATAS X and
        // the regular ATAS Platform (see the dual-build core convention) but
        // only wants ONE of them actually pushing data to the VPS - turning
        // this off on the non-pushing platform's charts disables ALL pushes
        // (bar/trade/absorption) in one click instead of three, while leaving
        // the identity label + engine signal display fully working (neither
        // depends on push settings at all).
        [Display(Name = "总开关：启用数据推送", GroupName = "2. 推送开关", Order = 0)]
        public bool EnableDataPush { get; set; } = true;

        [Display(Name = "启用K线推送", GroupName = "2. 推送开关", Order = 1)]
        public bool EnableBarPush { get; set; } = true;

        [Display(Name = "启用大单推送", GroupName = "2. 推送开关", Order = 2)]
        public bool EnableTradePush { get; set; } = true;

        [Display(Name = "启用足迹图数据", GroupName = "2. 推送开关", Order = 3)]
        public bool EnableFootprint { get; set; } = true;

        // Phase 7F: native absorption detection, replaces the old ATAS built-in
        // Absorption webhook (/atas/signal) which cannot carry price/volume.
        [Display(Name = "启用吸收信号推送", GroupName = "2. 推送开关", Order = 4)]
        public bool EnableAbsorptionPush { get; set; } = true;

        [Display(Name = "中单阈值(仅入库) BTC", GroupName = "3. 大单阈值", Order = 1)]
        public decimal ThresholdMedium { get; set; } = 20m;

        [Display(Name = "大单阈值(入库+TG) BTC", GroupName = "3. 大单阈值", Order = 2)]
        public decimal ThresholdLarge { get; set; } = 100m;

        [Display(Name = "鲸鱼单阈值(紧急TG) BTC", GroupName = "3. 大单阈值", Order = 3)]
        public decimal ThresholdWhale { get; set; } = 300m;

        [Display(Name = "最小价位量 BTC", GroupName = "3. 大单阈值", Order = 4)]
        public decimal FpMinVolume { get; set; } = 3m;

        // Phase 7F: absorption thresholds. Dominant side volume (BTC, already
        // converted for OKX perp) must reach AbsorbMinBtc AND be at least
        // AbsorbRatio times the opposite side to count as absorption.
        [Display(Name = "吸收最小量 BTC", GroupName = "4. 吸收检测", Order = 1)]
        public decimal AbsorbMinBtc { get; set; } = 15.0m;

        [Display(Name = "吸收比例", GroupName = "4. 吸收检测", Order = 2)]
        public decimal AbsorbRatio { get; set; } = 3.0m;

        // Phase 7H Stage1 (recon) -> Stage2 (formal): master on/off switch for
        // the corner overlay. Stage1 showed a raw field dump so Sea could
        // screenshot all four charts and confirm real values (7F lesson:
        // never guess parsing rules from API docs alone); Stage2 replaced the
        // on-chart content with the operational Auto/Manual status label
        // (see ComputeIdentityLabel) now that the parsing rule is confirmed.
        [Display(Name = "显示身份角标", GroupName = "5. 身份角标", Order = 1)]
        public bool ShowIdentityLabel { get; set; } = true;

        // Phase 7I: label position is now configurable, default BottomLeft
        // (previously hardcoded top-left at pixel 8,8).
        [Display(Name = "角标位置", GroupName = "5. 身份角标", Order = 2)]
        public LabelPosition LabelPositionSetting { get; set; } = LabelPosition.BottomLeft;

        // Phase 7I hotfix: Sea reported BottomLeft rendered almost entirely
        // off-screen (the chart's own bottom axis/scrollbar chrome eats into
        // RenderContext.Size.Height without being part of the visible candle
        // area, and an 8px margin was not enough clearance). Rather than
        // guess a "correct" margin for every theme/DPI, expose manual pixel
        // offsets so Sea can nudge the label to a visible spot themselves.
        [Display(Name = "角标水平偏移", GroupName = "5. 身份角标", Order = 3)]
        public int LabelOffsetX { get; set; } = 0;

        [Display(Name = "角标垂直偏移", GroupName = "5. 身份角标", Order = 4)]
        public int LabelOffsetY { get; set; } = 0;

        // Phase 7I/7J: polls the VPS's GET /api/signal/history and draws the
        // current open engine_signals row (entry/stop/t1/t2) as price lines
        // plus recent terminal signals as historical markers. Only runs on
        // the Binance|Perp chart - the engine's score is computed on Binance
        // perpetual data, so drawing it on the other three charts would be
        // misleading. Not gated by EnableDataPush above - this is read-only
        // polling, not pushing, and Sea explicitly wants it to keep working
        // on the platform where data push is turned off.
        [Display(Name = "显示引擎信号", GroupName = "6. 引擎信号", Order = 1)]
        public bool ShowEngineSignals { get; set; } = true;

        [Display(Name = "信号轮询间隔(秒)", GroupName = "6. 引擎信号", Order = 2)]
        public int SignalPollSeconds { get; set; } = 10;

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

        // Phase 7I: engine signal polling state. Polling only proceeds on the
        // Binance|Perp chart (checked against the effective identity, so this
        // also respects Auto/Manual mode). _signalChartUnsupportedLogged logs
        // the "not this chart" explanation exactly once, not every tick.
        private DateTime  _lastSignalPollUtc          = DateTime.MinValue;
        private bool      _signalPollInFlight          = false;
        private bool      _signalPollOk                = false;
        private DateTime? _lastSignalPollBjTime        = null;
        private int       _signalPollFailCount         = 0;
        private bool      _signalChartUnsupportedLogged = false;

        private const int SIGNAL_TERMINAL_GRACE_MINUTES = 30;

        private sealed class ActiveSignal
        {
            public int      Id;
            public string   Direction = "";
            public double   Score;
            public decimal  Entry, Stop, T1, T2;
            public string   Status = "open";
            public bool     IsTerminal;
            public DateTime? TerminalSinceUtc;
        }
        private ActiveSignal? _activeSignal = null;

        // Chart drawing objects for the four signal price lines + their
        // right-edge text labels. Kept as fields so DrawSignalLines can
        // mutate them in place (move to the current bar, recolor to gray on
        // terminal) instead of removing/re-adding every tick, and so
        // OnDispose can clean them up when the indicator is unloaded.
        private LineTillTouch? _sigLineEntry, _sigLineStop, _sigLineT1, _sigLineT2;
        private const string SIG_TAG_ENTRY = "AtasBridgeSigEntry";
        private const string SIG_TAG_STOP  = "AtasBridgeSigStop";
        private const string SIG_TAG_T1    = "AtasBridgeSigT1";
        private const string SIG_TAG_T2    = "AtasBridgeSigT2";

        // Phase 7J: lightweight markers for terminal signals from the last
        // 7 days (current signal keeps its full 4-line display above; full
        // lines for every historical signal would clutter the chart - Sea
        // confirmed simplified markers for history, full lines for current).
        // Tracks which ids currently have a marker so markers for signals
        // that age out of the 7-day window can be removed on the next poll.
        private readonly HashSet<int> _histMarkerIds = new();
        private const string SIG_HIST_PREFIX = "AtasBridgeSigHist";

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
        public AtasBridge() : base(true)
        {
            DenyToChangePanel = true;
            EnableCustomDrawing = true;

            // Phase 7K: the base Indicator class auto-creates one default
            // output DataSeries (confirmed via reflection: a bare Indicator
            // subclass with zero custom code already has DataSeries.Count==1)
            // - this is generic SDK boilerplate most simple line/oscillator
            // indicators plot through, not something AtasBridge ever writes
            // to (it's a data-bridge + drawing tool, not a per-bar value
            // series). Left visible it shows up as a confusing "绘图" section
            // in the settings panel that never draws anything - Sea asked
            // what it does. Hiding it removes the confusion; harmless since
            // nothing in this file ever reads or writes DataSeries[0].
            try { DataSeries[0].IsHidden = true; } catch { }
        }

        // Phase 7I: remove any signal drawing objects (price lines + labels)
        // this instance added, so unloading/replacing the indicator does not
        // leave stale lines behind on the chart.
        protected override void OnDispose()
        {
            ClearSignalDrawing();
            ClearAllHistoricalMarkers();
            base.OnDispose();
        }

        // Phase 7J: unlike ClearSignalDrawing (called mid-operation whenever
        // the current signal changes/expires), this only runs on unload -
        // clearing historical markers on every "no current signal" poll
        // result would defeat the point of keeping 7 days of history visible.
        private void ClearAllHistoricalMarkers()
        {
            try
            {
                foreach (var id in _histMarkerIds) Labels.Remove(SIG_HIST_PREFIX + id);
                _histMarkerIds.Clear();
            }
            catch { }
        }

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

            // Phase 7I: signal polling gate + grace-timer check + per-tick
            // redraw (keeps the price line labels tracking the current bar).
            // Runs every tick like the other Stage1/Stage2 additions above -
            // cheap (mostly a time comparison), only fires an actual HTTP
            // poll once every SignalPollSeconds.
            if (ShowEngineSignals) UpdateEngineSignals();

            // Phase 7F: must run before the "bar <= _lastBar" early return below,
            // because absorption needs to be checked on every tick of the
            // still-forming current bar, not just once when a bar closes.
            if (EnableDataPush && EnableAbsorptionPush) CheckAbsorption(bar);

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

            if (EnableDataPush && EnableBarPush) _ = PostBarAsync(candle);
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
            // Confirmed via a Stage1 diagnostic log that ATAS calls this with
            // layout=LatestBar(4) for this indicator, not Final(8) - drawing
            // unconditionally on every call remains the reliable choice since
            // these are single lines of text (harmless if it ever fires more
            // than once per frame; redraws just overlap at the same pixel).

            if (ShowIdentityLabel)
            {
                try
                {
                    var (text, color) = ComputeIdentityLabel();
                    var size = context.MeasureString(text, _identityRenderFont);
                    var (x, y) = ResolveCornerPosition(context, size);

                    context.FillRectangle(
                        Color.FromArgb(190, 0, 0, 0),
                        new Rectangle(x - 4, y - 4, size.Width + 8, size.Height + 8));
                    context.DrawString(text, _identityRenderFont, color, x, y);
                }
                catch { }
            }

            if (ShowEngineSignals) RenderEngineHeader(context);
        }

        // Phase 7I: corner label pixel position from the configurable
        // LabelPosition setting (previously hardcoded to (8,8) top-left).
        // Phase 7I hotfix: bottom-anchored positions get extra clearance
        // (bottomMargin) from the chart's own bottom axis/scrollbar chrome,
        // which Sea found ate into RenderContext.Size.Height enough that an
        // 8px margin rendered the label almost entirely off-screen.
        // LabelOffsetX/Y are applied on top of whatever this resolves to, so
        // Sea can nudge further for their specific theme/DPI.
        private (int x, int y) ResolveCornerPosition(RenderContext context, Size size)
        {
            const int margin = 8;
            const int bottomMargin = 40;

            int x, y;
            switch (LabelPositionSetting)
            {
                case LabelPosition.TopRight:
                    x = context.Size.Width - margin - size.Width; y = margin;
                    break;
                case LabelPosition.BottomRight:
                    x = context.Size.Width - margin - size.Width;
                    y = context.Size.Height - bottomMargin - size.Height;
                    break;
                case LabelPosition.BottomLeft:
                    x = margin;
                    y = context.Size.Height - bottomMargin - size.Height;
                    break;
                case LabelPosition.TopLeft:
                default:
                    x = margin; y = margin;
                    break;
            }
            return (x + LabelOffsetX, y + LabelOffsetY);
        }

        // Phase 7H Stage2 -> Phase 7I: operational status label. All status
        // characters are plain ASCII (Phase 7I fix: the earlier check/cross/
        // not-equal Unicode glyphs rendered as "[]" boxes on Sea's ATAS
        // build's font - see CHANGELOG for the report). Four states:
        //   - Auto mode, parse failed        -> red "UNSET" (no guessing)
        //   - Auto mode, parsed but conflicts
        //     with the manual dropdowns       -> yellow, shows both values
        //   - Auto mode, parsed and resolved  -> green/orange-red by push status
        //   - Manual mode                     -> same status style, tagged MANUAL
        // On the Binance|Perp chart only, a " | SIG <status>" segment is
        // appended reflecting the engine signal poll outcome (Phase 7I).
        // Phase 7I hotfix: dropped the HH:mm:ss timestamps and the version
        // tag from this on-chart text per Sea's feedback ("too long" -
        // the version is still visible in the read-only Version setting).
        private (string text, Color color) ComputeIdentityLabel()
        {
            string statusSym = !_lastPushBjTime.HasValue
                ? "..."
                : (_lastPushOk ? "OK" : $"ERR({_pushFailCount})");
            Color okColor = _lastPushOk ? Color.LightGreen : Color.OrangeRed;

            string text;
            Color color;

            if (IdentityModeSetting == IdentityMode.Manual)
            {
                text  = $"{Exchange}|{MarketType} MANUAL {statusSym}";
                color = okColor;
            }
            else
            {
                bool ok = TryParseAutoIdentity(out var aExch, out var aMkt);
                if (!ok)
                    return ("UNSET (unrecognized)", Color.Red);

                bool conflict = aExch != Exchange || aMkt != MarketType;
                if (conflict)
                {
                    text  = $"AUTO {aExch}|{aMkt} != MANUAL {Exchange}|{MarketType}";
                    color = Color.Yellow;
                }
                else
                {
                    text  = $"{aExch}|{aMkt} AUTO {statusSym}";
                    color = okColor;
                }
            }

            if (ShowEngineSignals)
            {
                var (exch, mkt, _, _) = ResolveEffectiveIdentity();
                if (exch == ExchangeName.Binance && mkt == MarketKind.Perp)
                {
                    string sigSym = !_lastSignalPollBjTime.HasValue
                        ? "..."
                        : (_signalPollOk ? "OK" : $"ERR({_signalPollFailCount})");
                    text += $" | SIG {sigSym}";
                }
            }

            return (text, color);
        }

        // Phase 7I: top-of-chart line summarizing the currently displayed
        // engine signal (if any). Screen-anchored like the corner label, but
        // always at the top regardless of LabelPositionSetting so it never
        // depends on / collides with the corner label's chosen corner.
        private void RenderEngineHeader(RenderContext context)
        {
            if (_activeSignal is null) return;

            try
            {
                string scoreStr = (_activeSignal.Score >= 0 ? "+" : "") + _activeSignal.Score.ToString("0");
                string suffix = _activeSignal.IsTerminal ? $" [{_activeSignal.Status.ToUpperInvariant()}]" : " (SIM)";
                string text = $"ENGINE #{_activeSignal.Id} {_activeSignal.Direction} score{scoreStr}{suffix}";
                var size = context.MeasureString(text, _identityRenderFont);
                int x = Math.Max(8, (context.Size.Width - size.Width) / 2);
                const int y = 8;
                Color color = _activeSignal.IsTerminal ? Color.Gray : Color.White;

                context.FillRectangle(
                    Color.FromArgb(190, 0, 0, 0),
                    new Rectangle(x - 4, y - 4, size.Width + 8, size.Height + 8));
                context.DrawString(text, _identityRenderFont, color, x, y);
            }
            catch { }
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

        // ══ Phase 7I: engine signal polling + on-chart display ════════════════
        // Polls the VPS's existing read-only GET /api/signal/latest (7G
        // pre-wired this endpoint - zero server-side changes for this card).
        // Only runs on the Binance|Perp chart (checked against the effective
        // identity, respecting Auto/Manual mode) since the engine's score is
        // computed from Binance perpetual data; drawing it on the other
        // three charts would misleadingly suggest it applies there too.

        private void UpdateEngineSignals()
        {
            var (exch, mkt, _, _) = ResolveEffectiveIdentity();
            bool supported = exch == ExchangeName.Binance && mkt == MarketKind.Perp;

            if (!supported)
            {
                if (!_signalChartUnsupportedLogged)
                {
                    _signalChartUnsupportedLogged = true;
                    try
                    {
                        LoggerHelper.LogInfo(this, "{0}", new object[]
                        {
                            $"AtasBridge: engine signal display only runs on the Binance|Perp chart; this chart resolved to {exch}|{mkt}, staying silent (no polling, no drawing)."
                        });
                    }
                    catch { }
                }
                return;
            }

            CheckTerminalGraceExpiry();

            // Re-apply every tick so the labels' Bar tracks CurrentBar (keeps
            // them near the right edge as new bars form) even between polls.
            if (_activeSignal != null) DrawSignalLines(_activeSignal);

            int pollSec = Math.Max(5, SignalPollSeconds);
            if ((DateTime.UtcNow - _lastSignalPollUtc).TotalSeconds < pollSec) return;
            _lastSignalPollUtc = DateTime.UtcNow;
            _ = PollSignalAsync();
        }

        private void CheckTerminalGraceExpiry()
        {
            if (_activeSignal?.IsTerminal == true && _activeSignal.TerminalSinceUtc.HasValue &&
                (DateTime.UtcNow - _activeSignal.TerminalSinceUtc.Value).TotalMinutes >= SIGNAL_TERMINAL_GRACE_MINUTES)
            {
                ClearSignalDrawing();
                _activeSignal = null;
            }
        }

        // Phase 7J: polls /api/signal/history (last 7 days, added alongside
        // this card - server change is purely additive, /api/signal/latest
        // from 7I is untouched) instead of /api/signal/latest, so a single
        // poll yields both the current open signal (if any) and the recent
        // terminal ones for the historical chart markers below.
        private async Task PollSignalAsync()
        {
            if (_signalPollInFlight) return;
            _signalPollInFlight = true;
            try
            {
                var req = new HttpRequestMessage(HttpMethod.Get, $"{VpsUrl.TrimEnd('/')}/api/signal/history?days=7");
                if (!string.IsNullOrEmpty(AuthToken))
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", AuthToken);

                var httpResp = await _http.SendAsync(req).ConfigureAwait(false);
                if (!httpResp.IsSuccessStatusCode) { MarkSignalPollFail(); return; }

                var json = await httpResp.Content.ReadAsStringAsync().ConfigureAwait(false);
                var resp = JsonSerializer.Deserialize<SignalHistoryResponse>(json, _serOpts);

                if (resp?.Signals == null)
                {
                    // {"status":"error",...} or an unrecognized response shape.
                    // Treated like a poll failure; existing drawing untouched.
                    MarkSignalPollFail();
                    return;
                }

                MarkSignalPollOk();

                var open = resp.Signals.FirstOrDefault(s =>
                    s.Id.HasValue && string.Equals(s.Status, "open", StringComparison.OrdinalIgnoreCase));
                if (open != null)
                {
                    ApplySignal(open);
                }
                else
                {
                    ClearSignalDrawing();
                    _activeSignal = null;
                }

                var historical = resp.Signals
                    .Where(s => s.Id.HasValue && !string.Equals(s.Status, "open", StringComparison.OrdinalIgnoreCase))
                    .ToList();
                UpdateHistoricalMarkers(historical);
            }
            catch
            {
                MarkSignalPollFail();
            }
            finally
            {
                _signalPollInFlight = false;
            }
        }

        private void MarkSignalPollOk()
        {
            _signalPollOk         = true;
            _signalPollFailCount  = 0;
            _lastSignalPollBjTime = DateTime.UtcNow.AddHours(8);
        }

        private void MarkSignalPollFail()
        {
            _signalPollOk = false;
            _signalPollFailCount++;
        }

        private void ApplySignal(SignalItem resp)
        {
            if (!resp.Id.HasValue) return;

            string status     = resp.Status ?? "open";
            bool   isTerminal  = !string.Equals(status, "open", StringComparison.OrdinalIgnoreCase);
            bool   isNewSignal = _activeSignal == null || _activeSignal.Id != resp.Id.Value;

            if (isNewSignal)
            {
                ClearSignalDrawing();
                _activeSignal = new ActiveSignal
                {
                    Id               = resp.Id.Value,
                    Direction        = resp.Direction ?? "",
                    Score            = resp.Score ?? 0,
                    Entry            = (decimal)(resp.Entry ?? 0),
                    Stop             = (decimal)(resp.Stop  ?? 0),
                    T1               = (decimal)(resp.T1    ?? 0),
                    T2               = (decimal)(resp.T2    ?? 0),
                    Status           = status,
                    IsTerminal       = isTerminal,
                    TerminalSinceUtc = isTerminal ? DateTime.UtcNow : null,
                };
            }
            else
            {
                bool wasTerminal = _activeSignal!.IsTerminal;
                _activeSignal.Status     = status;
                _activeSignal.IsTerminal = isTerminal;
                if (isTerminal && !wasTerminal)
                    _activeSignal.TerminalSinceUtc = DateTime.UtcNow;
            }
        }

        // Draws/updates the four price lines + right-edge labels for the
        // currently active signal. Called every tick (from
        // UpdateEngineSignals) so the labels keep tracking CurrentBar; the
        // underlying LineTillTouch/DrawingText objects are mutated in place
        // rather than removed/re-added, which is cheap and avoids flicker.
        private void DrawSignalLines(ActiveSignal sig)
        {
            try
            {
                int bar = Math.Max(0, CurrentBar);
                Color entryColor  = sig.IsTerminal ? Color.Gray : Color.White;
                Color stopColor   = sig.IsTerminal ? Color.Gray : Color.Red;
                Color targetColor = sig.IsTerminal ? Color.Gray : Color.LightGreen;

                _sigLineEntry = UpsertLine(_sigLineEntry, bar, sig.Entry, entryColor, DashStyle.Solid);
                _sigLineStop  = UpsertLine(_sigLineStop,  bar, sig.Stop,  stopColor,  DashStyle.Solid);
                _sigLineT1    = UpsertLine(_sigLineT1,    bar, sig.T1,    targetColor, DashStyle.Dash);
                _sigLineT2    = UpsertLine(_sigLineT2,    bar, sig.T2,    targetColor, DashStyle.Dash);

                string suffix = sig.IsTerminal ? $" [{sig.Status.ToUpperInvariant()}]" : "";
                SetSignalLabel(SIG_TAG_ENTRY, bar, sig.Entry, $"ENTRY {sig.Entry:0.##} #{sig.Id} {sig.Direction}{suffix}", entryColor);
                SetSignalLabel(SIG_TAG_STOP,  bar, sig.Stop,  $"STOP {sig.Stop:0.##}{suffix}", stopColor);
                SetSignalLabel(SIG_TAG_T1,    bar, sig.T1,    $"T1 {sig.T1:0.##}{suffix}", targetColor);
                SetSignalLabel(SIG_TAG_T2,    bar, sig.T2,    $"T2 {sig.T2:0.##}{suffix}", targetColor);
            }
            catch { }
        }

        private LineTillTouch UpsertLine(LineTillTouch? existing, int bar, decimal price, Color color, DashStyle dash)
        {
            if (existing != null)
            {
                existing.FirstPrice   = price;
                existing.Pen.Color    = color;
                existing.Pen.DashStyle = dash;
                return existing;
            }

            // Phase 7I dual-platform support: LineTillTouch's Pen parameter
            // type differs between the two ATAS SDK versions - ATAS X
            // (8.0.14.644) uses Utils.Common.UniversalPen, the regular ATAS
            // Platform build (8.0.14.290) uses plain System.Drawing.Pen.
            // AtasBridge.Platform.csproj defines ATAS_PLATFORM to select the
            // right one; everything else in this file is identical between
            // the two builds (confirmed via reflection - same API otherwise).
#if ATAS_PLATFORM
            var pen = new System.Drawing.Pen(color, 2f) { DashStyle = dash };
#else
            var pen = new UniversalPen(color, 2f) { DashStyle = dash };
#endif
            var line = new LineTillTouch(bar, price, pen) { IsRay = true };
            HorizontalLinesTillTouch.Add(line);
            return line;
        }

        private void SetSignalLabel(string tag, int bar, decimal price, string text, Color color)
        {
            Labels[tag] = new DrawingText(TickSize)
            {
                Text         = text,
                Bar          = bar,
                TextPrice    = price,
                IsAbovePrice = true,
                Textcolor    = color,
                FontSize     = 11f,
                Tag          = tag
            };
        }

        // Removes all four signal price lines + labels from the chart.
        // Called on: new signal replacing an old one, {"status":"empty"}
        // response, the 30-minute terminal grace timeout, and OnDispose
        // (indicator unload) - the last one is what step 3's self-check
        // ("no leftover drawing objects after unload") verifies.
        private void ClearSignalDrawing()
        {
            try
            {
                if (_sigLineEntry != null) { HorizontalLinesTillTouch.Remove(_sigLineEntry); _sigLineEntry = null; }
                if (_sigLineStop  != null) { HorizontalLinesTillTouch.Remove(_sigLineStop);  _sigLineStop  = null; }
                if (_sigLineT1    != null) { HorizontalLinesTillTouch.Remove(_sigLineT1);    _sigLineT1    = null; }
                if (_sigLineT2    != null) { HorizontalLinesTillTouch.Remove(_sigLineT2);    _sigLineT2    = null; }
                Labels.Remove(SIG_TAG_ENTRY);
                Labels.Remove(SIG_TAG_STOP);
                Labels.Remove(SIG_TAG_T1);
                Labels.Remove(SIG_TAG_T2);
            }
            catch { }
        }

        // Phase 7J: draws/updates a compact one-line marker at each
        // historical (non-open) signal's entry price/bar - direction + id +
        // outcome, no lines. Called once per poll (not every tick, unlike
        // the current signal's DrawSignalLines) since historical entries
        // don't need to track CurrentBar. Removes markers for any id that
        // has aged out of the 7-day window since the previous poll.
        private void UpdateHistoricalMarkers(List<SignalItem> historical)
        {
            try
            {
                var newIds = new HashSet<int>(historical.Select(s => s.Id!.Value));

                foreach (var oldId in _histMarkerIds.Where(id => !newIds.Contains(id)).ToList())
                {
                    Labels.Remove(SIG_HIST_PREFIX + oldId);
                    _histMarkerIds.Remove(oldId);
                }

                foreach (var s in historical)
                {
                    if (!TryParseBjTimeToUtc(s.CreatedAt, out var utcTime)) continue;
                    int bar = FindBarForUtcTime(utcTime);
                    var candle = GetCandle(bar);
                    if (candle == null) continue;

                    decimal entryPrice = s.Entry.HasValue ? (decimal)s.Entry.Value : candle.Close;
                    var (color, outcomeShort) = HistOutcomeStyle(s.Status);
                    string text = $"{s.Direction} #{s.Id} {outcomeShort}";
                    string tag = SIG_HIST_PREFIX + s.Id!.Value;

                    Labels[tag] = new DrawingText(TickSize)
                    {
                        Text         = text,
                        Bar          = bar,
                        TextPrice    = entryPrice,
                        IsAbovePrice = true,
                        Textcolor    = color,
                        FontSize     = 10f,
                        Tag          = tag
                    };
                    _histMarkerIds.Add(s.Id!.Value);
                }
            }
            catch { }
        }

        private static (Color color, string text) HistOutcomeStyle(string? status)
        {
            switch ((status ?? "").ToLowerInvariant())
            {
                case "t2_hit":       return (Color.LightGreen, "T2 OK");
                case "t1_then_stop": return (Color.Orange,      "T1>SL");
                case "stopped":      return (Color.OrangeRed,   "SL");
                case "expired":      return (Color.Gray,        "EXP");
                default:             return (Color.Gray,        status ?? "?");
            }
        }

        // engine_signals.created_at is a naive Beijing-time string
        // ("yyyy-MM-dd HH:mm:ss", see monitor/signal_engine.py's now_sgt())
        // - parse and convert to UTC so it is comparable to candle.LastTime.
        private static bool TryParseBjTimeToUtc(string? s, out DateTime utc)
        {
            utc = default;
            if (string.IsNullOrEmpty(s)) return false;
            if (!DateTime.TryParseExact(s, "yyyy-MM-dd HH:mm:ss",
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.None, out var bjTime))
                return false;
            utc = DateTime.SpecifyKind(bjTime.AddHours(-8), DateTimeKind.Utc);
            return true;
        }

        // Binary search for the latest bar whose close time is at or before
        // targetUtc - bars are chronologically ordered so this is safe.
        // "Latest bar at or before" is precise enough for a marker; this
        // isn't trying to hit the exact tick the signal fired on.
        private int FindBarForUtcTime(DateTime targetUtc)
        {
            int hi = CurrentBar;
            if (hi < 0) return 0;
            int lo = 0, result = 0;
            while (lo <= hi)
            {
                int mid = (lo + hi) / 2;
                var candle = GetCandle(mid);
                if (candle == null) { hi = mid - 1; continue; }

                DateTime candleUtc;
                try { candleUtc = DateTime.SpecifyKind(candle.LastTime, DateTimeKind.Utc); }
                catch { hi = mid - 1; continue; }

                if (candleUtc <= targetUtc) { result = mid; lo = mid + 1; }
                else { hi = mid - 1; }
            }
            return result;
        }

        // ══ Phase 3: Large trade + update tracking ════════════════════════════

        protected override void OnCumulativeTrade(CumulativeTrade trade)
        {
            if (!EnableDataPush || !EnableTradePush) return;
            CheckAndPost(trade, isUpdate: false);
        }

        protected override void OnUpdateCumulativeTrade(CumulativeTrade trade)
        {
            if (!EnableDataPush || !EnableTradePush) return;
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

    // Phase 7J: GET /api/signal/history response model (added alongside
    // this card; /api/signal/latest from 7G/7I is untouched, unused now).
    // On success: {"count":N,"signals":[...]}. On a server-side exception:
    // {"status":"error","detail":...} with no "signals" key - Signals comes
    // back null in that case, which PollSignalAsync treats as a poll failure.
    public sealed class SignalHistoryResponse
    {
        public int?               Count   { get; set; }
        public List<SignalItem>?  Signals { get; set; }
        public string?            Status  { get; set; }
    }

    public sealed class SignalItem
    {
        public int?    Id        { get; set; }
        public string? CreatedAt { get; set; }
        public string? Direction { get; set; }
        public double? Score     { get; set; }
        public double? Entry     { get; set; }
        public double? Stop      { get; set; }
        public double? T1        { get; set; }
        public double? T2        { get; set; }
        // Lifecycle status: "open"/"stopped"/"t1_then_stop"/"t2_hit"/"expired"
        public string? Status    { get; set; }
    }
}
