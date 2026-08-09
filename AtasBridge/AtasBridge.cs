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
using System.Collections.Concurrent;
using System.Threading;
using ATAS.Indicators;
using ATAS.Indicators.Drawing;
using Utils.Common;
using Utils.Common.Logging;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;
using OFT.Coinglass.Models;
using OFT.Coinglass.Providers;
using OFT.Coinglass.Requests;

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
        // 注意：这个版本号是**整个 dll 的**（AtasBridge 与 AtasLiquidations 同在
        // 一个程序集里），所以改动任一文件都要更新它，否则设置面板显示的版本会
        // 与 CHANGELOG 对不上 —— -33 只改了 AtasLiquidations.cs 却漏了这里，
        // 就出现过一次。
        public const string Tag  = "v2026.08.09-33";
        public const string Desc = "Liquidation label falls back to last non-empty bar";
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
        // 2026-08-09 修复：原来这是个普通读写属性，值会被存进图表模板，于是
        // 升级 dll 后设置面板里显示的仍是**保存模板那一刻的旧版本号**
        // （Sea 截图里 dll 已经是 -31、面板却显示 v2026.08.09-7）。排查时据此
        // 判断"新版没加载"，把人带偏过好几轮。
        // 改成 getter 恒返回当前常量、setter 空实现：模板里存的旧值反序列化
        // 时被丢弃，面板永远显示真实运行版本。
        [Display(Name = "版本号", GroupName = "1. 基础配置", Order = 0)]
        [System.ComponentModel.ReadOnly(true)]
        public string VersionInfo
        {
            get => AtasBridgeVersion.Tag + " (" + AtasBridgeVersion.Desc + ")";
            set { /* 丢弃模板里的历史值 */ }
        }

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
        // Phase 7K follow-up: defaults changed from 0/0 to 10/-150 - Sea
        // confirmed these values work well on their own setup and wants
        // them baked in as the out-of-the-box default for new chart
        // instances, instead of starting at 0/0 and needing a manual nudge
        // every time.
        [Display(Name = "角标水平偏移", GroupName = "5. 身份角标", Order = 3)]
        public int LabelOffsetX { get; set; } = 10;

        [Display(Name = "角标垂直偏移", GroupName = "5. 身份角标", Order = 4)]
        public int LabelOffsetY { get; set; } = -150;

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

        // ── 2026-08-09 Phase 7L: Coinglass 三路指标直连 ────────────────────────
        // ATAS 自带的三个 Crypto Metrics 指标（Crypto Open Interest / Long-Short
        // Ratio / Aggregated Liquidations）本身并不持有数据，它们只是从平台的
        // DI 容器里取 Coinglass Provider 单例，再自己订阅+补历史。反编译确认
        // （IL 扫描 ATAS.Indicators.Other.dll，两个平台 SDK 8.0.14.297 /
        // 8.0.14.646 的接口签名逐项一致，无需 #if 分支）：
        //   Crypto Open Interest    -> TryGetService<ICoinglassOIProvider>
        //   Long/Short Ratio        -> TryGetService<ICoinglassLSRatioProvider>
        //   Aggregated Liquidations -> TryGetService<ICoinglassAggregatedLiquidationsProvider>
        //                            + TryGetService<ICoinglassLiquidationOrdersProvider>
        // AtasBridge 继承自 Indicator，同样能调 TryGetService，因此这三路数据
        // 不需要把那三个指标挂到图表上就能直接拿到——省掉三个副图，也避免了
        // "从别的指标 DataSeries 里读渲染值"这种脆弱做法，拿到的是原始模型
        // （OpenInterestOhlc / LongShortRatio / LiquidationOrder）。
        //
        // Provider 是容器单例：即使图表上仍然开着那三个官方指标，这里的订阅
        // 也复用同一条连接，不会额外占用 Coinglass 配额。
        [Display(Name = "启用Coinglass数据接入", GroupName = "7. Crypto指标(Coinglass)", Order = 1)]
        public bool EnableCryptoMetrics { get; set; } = true;

        // 对应官方 Aggregated Liquidations 指标的「类型」设置项。三档一一对应
        // （官方指标内部也有一个同名成员之间的转换方法，因为两个枚举的**数值
        // 顺序不一样**，只能按名字对应，不能按 int 值强转）：
        //   Local        = 当前工具和交易所
        //   SymbolGlobal = 当前工具（所有交易所）   ← 官方 UI 下拉第二项
        //   Global       = 全球（所有符号和交易所）
        // 官方 UI 枚举 LiquidationTypes 是 Local=0/SymbolGlobal=1/Global=2，
        // 而这里用的 Coinglass 枚举是 Local=0/Global=1/SymbolGlobal=2 —— 顺序
        // 不同，看下拉里的第几项来选会选错，认准名字。
        // 默认 SymbolGlobal：Sea 要的就是「当前工具（所有交易所）」这一档。
        [Display(Name = "清算聚合范围(Local=本交易所/SymbolGlobal=本币种全交易所/Global=全市场)",
                 GroupName = "7. Crypto指标(Coinglass)", Order = 2)]
        public LiquidationsAggregationModes LiquidationMode { get; set; } = LiquidationsAggregationModes.SymbolGlobal;

        // 启动时向 Coinglass 补多少历史。只影响首次填充。
        [Display(Name = "历史回溯(小时)", GroupName = "7. Crypto指标(Coinglass)", Order = 3)]
        public int CryptoHistoryHours { get; set; } = 24;

        // 2026-08-09 实测（三轮诊断的最终结论），Coinglass 这边有三条通道：
        //
        //  1) 逐笔实时订阅 Subscribe(LiquidationSubscriptionParams)
        //     —— 订阅成功但一条回调都不来。ATAS 自带的 Aggregated Liquidations
        //     指标有同样的毛病（加载时画出历史、之后随 K 线前进再不更新），
        //     所以是 ATAS 这条通道本身死了，不是我们订阅写错。
        //
        //  2) 聚合历史 ICoinglassAggregatedLiquidationsProvider.GetHistoryAsync
        //     —— 无视传入的 From/To，按天对齐返回，且**只到昨天**：不论请求
        //     哪个窗口，返回的永远止于前一天 23:xx，窗口小于一天直接返回 0。
        //     是个 T+1 接口，拿不到当天数据。
        //
        //  3) 逐笔历史 ICoinglassLiquidationOrdersProvider.GetHistoryAsync
        //     —— 官方指标画"今天"那部分用的就是它（探针确认前两条都拿不到
        //     今天的数据，而官方图上今天是有柱子的）。它认 From/To，返回
        //     逐笔 LiquidationOrder，自己按 K 线聚合即可。
        //
        // 所以主数据源用 (3)：每 60 秒（UpdatePeriodLimit 是 1 分钟，比这更密
        // 会被限流返回空）拉一次最近一段的逐笔，按 Id 去重后累加到对应 K 线。
        // (1) 仍然保留，只统计条数不参与数值——哪天 ATAS 修好了角标上能立刻
        // 看出来。
        // 2026-08-09 结论：爆仓在 ATAS 这一层拿不到当天数据，三条通道全废
        // （实时订阅不推送 / 聚合历史 T+1 只到昨天 / 逐笔历史 500 或空，三个
        // 聚合档位都试过）。所以**默认关闭**，免得每次加载都去撞一遍必然失败
        // 的请求。留着开关是因为哪天 ATAS 修好了，打开就能用。
        // 当天爆仓数据要用的话，直连 Coinglass 官方 API 比走 ATAS 这层封装
        // 现实得多。
        [Display(Name = "启用爆仓接入", GroupName = "7. Crypto指标(Coinglass)", Order = 4)]
        public bool EnableLiquidations { get; set; } = true;

        // 实测 CoinglassDatafeedParameters.UpdatePeriodLimit = 1 分钟，比这更密
        // 的请求会被限流（返回空），所以下限锁死 60 秒。
        [Display(Name = "爆仓刷新间隔(秒,最低60)", GroupName = "7. Crypto指标(Coinglass)", Order = 5)]
        public int LiquidationRefreshSeconds { get; set; } = 60;

        // 默认关闭：打开后 /atas/bar 的 JSON 会多出 cg_oi_close / cg_lsr /
        // cg_liq_long / cg_liq_short 四个字段。VPS 侧如果用 pydantic 且没有
        // 放开 extra 字段，多余字段会被拒（422），所以必须等服务端先加好字段
        // 再开——关闭时这四个字段为 null，被 _serOpts 的 WhenWritingNull 直接
        // 略掉，推送内容跟接入前一字节不差。
        // 一次性诊断开关：加载指标时对聚合爆仓接口跑一组对照请求，把结果打进
        // ATAS 日志。实测发现这个接口**无视传入的 From/To**（请求最近 2 小时，
        // 返回的却是前一整天），而且第二次之后一律返回空——两种行为都不是靠
        // 读接口签名能知道的，只能实测。诊断完成、行为摸清后应关掉：每次加载
        // 都会多打几个请求。
        [Display(Name = "运行爆仓接口诊断(一次性)", GroupName = "7. Crypto指标(Coinglass)", Order = 6)]
        public bool RunLiquidationProbe { get; set; } = false;

        [Display(Name = "推送Coinglass字段到VPS", GroupName = "7. Crypto指标(Coinglass)", Order = 7)]
        public bool PushCryptoMetrics { get; set; } = false;

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

        // ══ 2026-08-02: 入场箭头 + 最近10条历史(OnRender 坐标绘制) ══════════
        // 旧版用 Labels/HorizontalLinesTillTouch 在"当前 K(CurrentBar)"处画横
        // 线+文字，看不出信号究竟发生在哪根 K。Sea 要求：入场点用上下箭头直接
        // 指在对应 K 上（做多↑标 K 下方、做空↓标 K 上方），并保留最近 10 条
        // 信号的 entry/stop/t1/t2 在盘面上，一眼看清历史。改为在 OnRender 里用
        // ChartInfo.PriceChartContainer.GetXByBar / GetYByPrice 做 bar→X、
        // price→Y 转换，FillPolygon 画三角箭头、DrawLine 画各位价短线段。
        // 探针确认(AtasProbe2)：两个 SDK(ATAS X / ATAS Platform)的
        // OFT.Rendering 完全一致，此段绘制代码双平台通用，无需 #if 分支。
        private sealed class RenderSig
        {
            public int      Id;
            public string   Dir = "";
            public decimal  Entry, Stop, T1, T2;
            public string   Status = "";
            public bool     IsOpen;
            public DateTime CreatedUtc;
            public DateTime? OutcomeUtc;   // 已结束信号的结束时间(可空)
        }
        // poll 线程整体替换、OnRender 线程读取——引用赋值原子，volatile 保证可见性。
        private volatile List<RenderSig>? _renderSigs = null;
        private const int  SIG_RENDER_MAX  = 10;   // 盘面最多保留最近 N 条
        private const int  HIST_SEG_BARS   = 6;    // 已结束信号的位价短线段横跨 K 数
        private static readonly RenderFont _sigLabelFont = new RenderFont("Arial", 10f);

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
            // 2026-08-02: 信号绘制已改为 OnRender 直接绘制(不再往 Labels/
            // HorizontalLinesTillTouch 添加持久对象)，卸载时无残留可清理。

            // Phase 7L: Coinglass 订阅必须退订——Provider 是平台单例，指标被
            // 卸载/重载时不退订会在单例里累积死回调。
            DisposeCrypto();
            base.OnDispose();
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

            // Phase 7L: Coinglass 三路数据。初始化只在第一次跑到这里时触发一次
            // （此时 DataProvider/InstrumentInfo 已就绪），之后每 tick 只是把
            // 网络线程入队的数据搬到本地状态，是纯内存操作。
            if (EnableCryptoMetrics)
            {
                EnsureCryptoInit();
                MaybeRefreshOiLsr();
                MaybeResubscribeCrypto();
                MaybeRefreshLiquidations();
                DrainCryptoQueues();
            }

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

            if (EnableDataPush && EnableBarPush) _ = PostBarAsync(candle, closedBar);
            _lastBar = bar;
        }

        private async Task PostBarAsync(IndicatorCandle c, int barIndex)
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
                    // Phase 7L：这一段在第一个 await 之前执行，仍在 ATAS 计算
                    // 线程上，读 _cgLiq* 字典是安全的（写入方 DrainCryptoQueues
                    // 也只在这个线程跑）。爆仓量取的是刚收那根 K 的累计值，
                    // 没有就留 null，不写 0——0 表示"这根 K 确实没有爆仓"，
                    // 跟"没接到数据"是两回事，不能混。
                    CgOiClose        = (EnableCryptoMetrics && PushCryptoMetrics && _cgOiClose.HasValue)
                                       ? (double)_cgOiClose.Value : null,
                    CgLsr            = (EnableCryptoMetrics && PushCryptoMetrics && _cgLsrValue.HasValue)
                                       ? (double)_cgLsrValue.Value : null,
                    CgLiqLong        = (EnableCryptoMetrics && PushCryptoMetrics && EnableLiquidations
                                        && _cgLiqLongs.TryGetValue(barIndex, out var cgLl)) ? (double)cgLl : null,
                    CgLiqShort       = (EnableCryptoMetrics && PushCryptoMetrics && EnableLiquidations
                                        && _cgLiqShorts.TryGetValue(barIndex, out var cgLs)) ? (double)cgLs : null,
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
            if (ShowEngineSignals) RenderSignalArrows(context);
        }

        // 2026-08-02: 在每条信号的入场 K 上画方向箭头 + entry/stop/t1/t2 位价
        // 短线段。做多↑箭头标在 K 线低点下方、做空↓箭头标在 K 线高点上方；
        // 未结束信号(open)用方向色(青/红)、位价线段延伸到当前 K；已结束信号
        // 按结果着色(见 HistOutcomeStyle)、线段精确画到真正结束的那根 K(有
        // outcome_at 时)，看出这单跑了多久；无 outcome_at 时回退 HIST_SEG_BARS。
        // 仅在币安永续图上绘制(与轮询同一门槛)——_renderSigs 只在该图被填充。
        private void RenderSignalArrows(RenderContext context)
        {
            var sigs = _renderSigs;                  // volatile 读，取当前引用快照
            if (sigs == null || sigs.Count == 0) return;

            var (exch, mkt, _, _) = ResolveEffectiveIdentity();
            if (!(exch == ExchangeName.Binance && mkt == MarketKind.Perp)) return;

            var container = ChartInfo?.PriceChartContainer;
            if (container == null) return;

            foreach (var s in sigs)
            {
                try
                {
                    int bar = FindBarForUtcTime(s.CreatedUtc);
                    var candle = GetCandle(bar);
                    if (candle == null) continue;

                    // 该 K 的中心 X 与像素宽度(用 bar 与 bar+1 的左沿求得，
                    // 不依赖 isStartOfBar 的具体语义，稳妥)。
                    int xL = container.GetXByBar(bar,     true);
                    int xR = container.GetXByBar(bar + 1, true);
                    int xc = (xL + xR) / 2;
                    int barW = Math.Abs(xR - xL);

                    bool isLong = s.Dir.IndexOf("LONG", StringComparison.OrdinalIgnoreCase) >= 0
                                  || s.Dir.Contains("多");

                    Color col = s.IsOpen
                        ? (isLong ? Color.FromArgb(0, 229, 204) : Color.FromArgb(255, 61, 110))
                        : HistOutcomeStyle(s.Status).color;

                    int aw  = Math.Max(4, Math.Min(9, barW / 2 - 1));  // 箭头半宽
                    int ah  = 13;                                       // 箭头高
                    int gap = 5;                                        // 与 K 高低点的间隙

                    Point[] arrow;
                    int labelY;
                    if (isLong)
                    {
                        int lowY  = container.GetYByPrice(candle.Low, false);
                        int apexY = lowY + gap;          // 尖端朝上、贴在低点下方
                        int baseY = apexY + ah;
                        arrow = new[] { new Point(xc, apexY), new Point(xc - aw, baseY), new Point(xc + aw, baseY) };
                        labelY = baseY + 2;
                    }
                    else
                    {
                        int highY = container.GetYByPrice(candle.High, false);
                        int apexY = highY - gap;         // 尖端朝下、贴在高点上方
                        int baseY = apexY - ah;
                        arrow = new[] { new Point(xc, apexY), new Point(xc - aw, baseY), new Point(xc + aw, baseY) };
                        labelY = baseY - 14;
                    }
                    context.FillPolygon(col, arrow);

                    // entry/stop/t1/t2 位价短线段右端 xEnd：
                    //   open        → 延伸到当前 K(跟随行情)
                    //   已结束+有时间 → 精确画到真正止盈/止损的那根 K(看出跑了多久)
                    //   已结束+无时间 → 回退固定横跨 HIST_SEG_BARS 根 K
                    int xEnd;
                    if (s.IsOpen)
                    {
                        xEnd = container.GetXByBar(Math.Max(bar + 1, CurrentBar), true);
                    }
                    else if (s.OutcomeUtc.HasValue)
                    {
                        int ob = FindBarForUtcTime(s.OutcomeUtc.Value);
                        int obL = container.GetXByBar(ob,     true);
                        int obR = container.GetXByBar(ob + 1, true);
                        xEnd = (obL + obR) / 2;                       // 结束 K 中心
                        if (xEnd < xc + barW) xEnd = xc + barW;       // 至少一根 K 宽(极速结束时)
                    }
                    else
                    {
                        xEnd = container.GetXByBar(bar + HIST_SEG_BARS, true);
                    }

                    Color entryC = s.IsOpen ? Color.White      : Dim(Color.Gainsboro);
                    Color stopC  = s.IsOpen ? Color.Red        : Dim(Color.OrangeRed);
                    Color tgtC   = s.IsOpen ? Color.LightGreen : Dim(Color.LightGreen);
                    // 标签一律用 ASCII——ATAS 的绘制字体(Arial)无中文字形，
                    // 中文会渲染成 □ 方框(Phase 7I 已踩过的坑)。E=入场 SL=止损。
                    DrawLevelSeg(context, xc, xEnd, s.Entry, entryC, "E");
                    DrawLevelSeg(context, xc, xEnd, s.Stop,  stopC,  "SL");
                    DrawLevelSeg(context, xc, xEnd, s.T1,    tgtC,   "T1");
                    DrawLevelSeg(context, xc, xEnd, s.T2,    tgtC,   "T2");

                    string txt = $"#{s.Id} {s.Dir}"
                               + (s.IsOpen ? "" : " " + HistOutcomeStyle(s.Status).text);
                    context.DrawString(txt, _sigLabelFont, col, xc - 12, labelY);
                }
                catch { }
            }
        }

        private void DrawLevelSeg(RenderContext context, int x1, int x2, decimal price, Color color, string tag)
        {
            if (price <= 0) return;
            try
            {
                int y = ChartInfo!.PriceChartContainer.GetYByPrice(price, false);
                context.DrawLine(new RenderPen(color, 1.4f), x1, y, x2, y);
                // 价格数字标在线段右端；靠右留 60px 边，避免压住价格轴刻度。
                int lblX = Math.Min(x2 + 3, context.Size.Width - 60);
                context.DrawString($"{tag} {price:0.#}", _sigLabelFont, color, lblX, y - 7);
            }
            catch { }
        }

        // 已结束信号的位价线段半透明处理，避免与未结束信号抢视觉。
        private static Color Dim(Color c) => Color.FromArgb(130, c.R, c.G, c.B);

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
            // 2026-07-12 fix: EnableDataPush=false (intentional, e.g. dual-
            // chart setups where only one side pushes to the VPS) used to
            // fall through to the same "..." + OrangeRed as a genuine never-
            // succeeded/failing push, making an intentionally-idle chart
            // look like it was erroring. Now OFF gets its own neutral Gray
            // state, and OrangeRed is reserved for an actual attempted-and-
            // failed push (ERR(n)).
            string statusSym;
            Color okColor;
            if (!EnableDataPush)
            {
                statusSym = "OFF";
                okColor   = Color.Gray;
            }
            else if (!_lastPushBjTime.HasValue)
            {
                statusSym = "...";
                okColor   = Color.Gray;
            }
            else
            {
                statusSym = _lastPushOk ? "OK" : $"ERR({_pushFailCount})";
                okColor   = _lastPushOk ? Color.LightGreen : Color.OrangeRed;
            }

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

            // Phase 7L: Coinglass 接入状态。跟 SIG 段同样的"诚实报告"原则——
            // 拿不到就明说是哪一类拿不到（N/A=平台没这服务，NOSUP=这个币种
            // Coinglass 不支持，ERR=请求异常），不显示成好像有数据的样子。
            // OK 之后进一步区分 WAIT（已订阅但一条数据都还没到）和实际值。
            if (EnableCryptoMetrics)
            {
                string cgSym = _cgStatus;
                if (_cgInitState == 2)
                {
                    if (!_cgLastDataUtc.HasValue)
                    {
                        cgSym = "WAIT";
                    }
                    else
                    {
                        // 爆仓单独标状态：三路是各自独立的订阅，OI/LSR 通了不
                        // 代表爆仓也通了（此前只显示 OI/LSR，爆仓那一路是死是
                        // 活完全看不出来）。LIQ 段的含义：
                        //   WAIT      = 一条爆仓记录都没收到过（这一路没通）
                        //   L../S..   = 收到过数据，显示当前这根 K 的多/空爆仓
                        //               累计；这根 K 没爆仓就是 0，跟"没数据"
                        //               是两回事，所以才要靠上面的 WAIT 区分
                        string liqPart;
                        if (_cgLiqRefreshFails >= CG_LIQ_FAIL_GIVEUP)
                        {
                            // 已放弃重试（逐笔历史接口持续 500），如实标出来，
                            // 不要让它看起来像"还在等数据"
                            liqPart = $"LIQ ERR({_cgLiqRefreshFails})";
                        }
                        else if (_cgLiqAggCount == 0)
                        {
                            liqPart = "LIQ WAIT";
                        }
                        else
                        {
                            // 按**整点小时**分桶，与 Coinglass 网页 1H 框架的柱子
                            // 完全对齐（此前用的是"最近 12 根 K"滚动窗口，整点
                            // 附近会跟网页对不上：实测 9876 vs 网页 10.076K，
                            // 差的就是滚出窗口的那几根）。
                            var (sumL, sumS) = LiquidationHourSum();
                            // 括号里是诊断计数：n=聚合刷新累计收到的记录条数
                            // （应随刷新间隔持续增长，不增长说明刷新失败了），
                            // 后面那个是逐笔实时流的条数（当前 ATAS 恒为 0）。
                            // 两个年龄各管一件事，缺一个就会误判：
                            //   ~xx = 最后一条爆仓记录**自身**的时间戳距今。市场
                            //         安静时它会一直变老（没人爆仓＝没有新记录），
                            //         所以它大**不等于**出故障，没有正常上限。
                            //   /yy = 上次**成功刷新**距今，这才是链路健康度，
                            //         正常必须稳定在 60 秒（刷新间隔）以内。
                            liqPart = $"LIQ1h {sumL:0}/{sumS:0} ~{Age(_cgLiqDataTime)}/{Age(_cgLiqLastOkUtc)} " +
                                      $"(n{_cgLiqAggCount}+{_cgLiqLiveCount})";
                        }

                        // 数值后面跟"距上次推送多少秒"。数值本身看不出新鲜度，
                        // 而这两条订阅实测会中途停推——不标年龄的话，卡住的旧值
                        // 和实时值在角标上长得一模一样。
                        cgSym = $"OI {(_cgOiClose.HasValue ? _cgOiClose.Value.ToString("0") : "-")}@{Age(_cgOiLastUtc)}" +
                                $" LSR {(_cgLsrValue.HasValue ? _cgLsrValue.Value.ToString("0.00") : "-")}@{Age(_cgLsrLastUtc)}" +
                                (EnableLiquidations ? $" {liqPart}" : "");
                    }
                }
                text += $" | CG {cgSym}";
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

            // 2026-08-02: 信号的盘面绘制已移入 OnRender(按 bar/price 坐标画箭头
            // 与位价线段)，OnRender 本就每帧调用，这里不再需要每 tick 重画横线，
            // 也不再需要 30 分钟终态宽限清理(终态信号自然随更新的信号挤出 10 条
            // 窗口而消失)。_activeSignal 仍由 poll 维护，供屏幕角落 ENGINE 头用。

            int pollSec = Math.Max(5, SignalPollSeconds);
            if ((DateTime.UtcNow - _lastSignalPollUtc).TotalSeconds < pollSec) return;
            _lastSignalPollUtc = DateTime.UtcNow;
            _ = PollSignalAsync();
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
                var req = new HttpRequestMessage(HttpMethod.Get, $"{VpsUrl.TrimEnd('/')}/api/signal/history?days=30");
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

                // 屏幕角落 ENGINE 头仍用 _activeSignal(当前未结束信号)。
                var open = resp.Signals.FirstOrDefault(s =>
                    s.Id.HasValue && string.Equals(s.Status, "open", StringComparison.OrdinalIgnoreCase));
                if (open != null) ApplySignal(open);
                else              _activeSignal = null;

                // 2026-08-02: 组装 OnRender 用的最近 N 条(按 id 倒序取最新)，
                // 含当前未结束的与已结束的，箭头与位价线段由 OnRender 绘制。
                var render = new List<RenderSig>();
                foreach (var s in resp.Signals
                                      .Where(s => s.Id.HasValue)
                                      .OrderByDescending(s => s.Id!.Value)
                                      .Take(SIG_RENDER_MAX))
                {
                    if (!TryParseBjTimeToUtc(s.CreatedAt, out var utc)) continue;
                    render.Add(new RenderSig
                    {
                        Id         = s.Id!.Value,
                        Dir        = s.Direction ?? "",
                        Entry      = (decimal)(s.Entry ?? 0),
                        Stop       = (decimal)(s.Stop  ?? 0),
                        T1         = (decimal)(s.T1    ?? 0),
                        T2         = (decimal)(s.T2    ?? 0),
                        Status     = s.Status ?? "",
                        IsOpen     = string.Equals(s.Status, "open", StringComparison.OrdinalIgnoreCase),
                        CreatedUtc = utc,
                        OutcomeUtc = TryParseBjTimeToUtc(s.OutcomeAt, out var outUtc) ? outUtc : (DateTime?)null,
                    });
                }
                _renderSigs = render;
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

        // 2026-08-02: 旧的 bar 锚定绘制(DrawSignalLines/UpsertLine/
        // SetSignalLabel/ClearSignalDrawing/UpdateHistoricalMarkers)已整体退役，
        // 由 OnRender 里的 RenderSignalArrows(箭头 + 位价线段)取代。HistOutcomeStyle
        // 仍保留——RenderSignalArrows 用它给已结束信号着色/取结果短标签。
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

        // ══ Phase 7L: Coinglass 三路数据接入 ══════════════════════════════════
        // 设计要点（与官方三个指标的做法一致，反编译 IL 逐句核对过）：
        //  1. Provider 从 DI 拿：TryGetService<T>，拿不到就整体降级（状态显示
        //     N/A），绝不抛异常影响 AtasBridge 本职的推送功能。
        //  2. 历史用 GetHistoryAsync 补，实时用 Subscribe 订阅，Handler 回调
        //     在网络线程触发——所以回调里只做入队，全部解析/聚合都放到
        //     OnCalculate（ATAS 计算线程）里做，避免跨线程访问 K 线数据。
        //  3. 清算是双 Provider 混合：历史走 AggregatedLiquidations（返回值带
        //     LastOrderId），实时走 LiquidationOrders 逐笔流，用 LastOrderId
        //     做历史/实时衔接去重，再按 K 线归属本地累加多空爆仓量。
        //  4. OnDispose 必须 Unsubscribe，否则反复加载指标会累积订阅泄漏。
        private ICoinglassOIProvider?                     _cgOiProv;
        private ICoinglassLSRatioProvider?                _cgLsrProv;
        private ICoinglassLiquidationOrdersProvider?      _cgLiqOrdProv;
        private ICoinglassAggregatedLiquidationsProvider? _cgLiqAggProv;

        private CoinglassSubscriptionParams<OpenInterestOhlc>? _cgOiSub;
        private CoinglassSubscriptionParams<LongShortRatio>?   _cgLsrSub;
        private LiquidationSubscriptionParams?                 _cgLiqSub;

        private readonly ConcurrentQueue<OpenInterestOhlc>      _cgOiQueue     = new();
        private readonly ConcurrentQueue<LongShortRatio>        _cgLsrQueue    = new();
        // 爆仓的权威数据：周期性重拉的**逐笔历史**（按 id 去重后累加到 K 线）
        private readonly ConcurrentQueue<LiquidationOrder> _cgLiqQueue     = new();
        // 逐笔**实时流**：只用于诊断计数，不参与数值（见上面设置项处的说明）
        private readonly ConcurrentQueue<LiquidationOrder> _cgLiqLiveQueue = new();
        // 聚合历史：逐笔那条拿不到数据时的退路，覆盖式写入对应 K 线
        private readonly ConcurrentQueue<AggregatedLiquidations> _cgLiqAggQueue = new();

        // 0=未开始 1=初始化中 2=已订阅 3=不可用（服务未注册/币种不支持/异常）
        private int    _cgInitState = 0;
        private string _cgStatus    = "...";

        // 最新值：由 OnCalculate 消费队列时更新，OnRender/推送直接读
        private decimal?  _cgOiClose;
        private decimal?  _cgLsrValue;
        private DateTime? _cgLastDataUtc;
        // 分路记录最后一次收到数据的时间。OI 和多空比是两条独立订阅，必须
        // 分开看——2026-08-09 实测到过"OI 推着推着就不推了、角标数值卡在
        // 旧值上"（与官方指标对不上才发现）。陈旧数据比没有数据更危险：
        // 推给 VPS 的话看不出它是几十分钟前的。
        private DateTime? _cgOiLastUtc;
        private DateTime? _cgLsrLastUtc;
        private DateTime  _cgLastResubUtc = DateTime.MinValue;
        private DateTime  _cgOiLastRefreshUtc  = DateTime.MinValue;
        private bool      _cgOiRefreshInFlight = false;
        private int       _cgOiRefreshLogCount = 0;
        // 超过这个时长没收到任何推送就重新订阅一次（看门狗）
        private static readonly TimeSpan CG_STALE_RESUB = TimeSpan.FromMinutes(10);

        // 按 K 线存放的爆仓量（只在 OnCalculate 线程读写）。逐笔累加式 —— 靠
        // 下面的 id 集合去重，所以反复拉取重叠的时间段不会把数字越滚越大。
        private readonly Dictionary<int, decimal> _cgLiqLongs  = new();
        private readonly Dictionary<int, decimal> _cgLiqShorts = new();

        // 逐笔去重。集合有上界，超了就整体清空重来——爆仓单 id 单调递增，
        // 清空后最多让极少数旧单重复计一次，比无上界地长期吃内存要安全。
        private readonly HashSet<long> _cgLiqSeenIds = new();
        private const int CG_LIQ_SEEN_MAX = 50000;

        // 诊断计数：_cgLiqAggCount = 逐笔历史拉取并计入的条数（数值来源），
        // _cgLiqLiveCount = 逐笔**实时流**收到的条数（当前已知恒为 0，见上面
        // 说明；哪天不为 0 了说明 ATAS 把那条通道修好了）。
        private long _cgLiqAggCount  = 0;
        private long _cgLiqLiveCount = 0;
        // 服务端返回的最后一条爆仓记录**自身的时间戳**（不是我们收到它的时间）。
        // 用来量化接口滞后：Coinglass 网页 18:13 已经有爆仓、我们本小时还是 0，
        // 到底是"这段确实没爆仓"还是"接口数据就是慢十几分钟"，只有拿它跟当前
        // 时间比才能分清。
        private DateTime? _cgLiqDataTime;
        // 上一次**成功**刷新（拿到非空结果）的时刻。与 _cgLiqDataTime 是两回事：
        //   _cgLiqDataTime  = 最后一条爆仓记录自身的时间戳 → 市场安静时会一直变老，
        //                     它变老不代表出故障
        //   _cgLiqLastOkUtc = 链路健康度，正常必须稳定在刷新间隔（60 秒）以内
        // 只显示前者会误导：早先把"~9m"当成链路变慢，其实只是那几分钟没人爆仓。
        private DateTime? _cgLiqLastOkUtc;

        // 一次性诊断：把前若干条聚合记录的原始时间戳/数值/落到哪根 K 打进
        // ATAS 日志。爆仓值落不到当前 K 上时，光看角标分不清是"这根 K 确实
        // 没爆仓"还是"时间口径不一致导致整体偏到几小时前的 K"——这条日志
        // 把两者一次性区分开（对比 agg 的 t 和 currentBar 的 barTime 即可）。
        private int _cgLiqDiagCount = 0;
        private const int CG_LIQ_DIAG_MAX = 200;

        // 每次刷新记一条汇总（限前若干次）。用来区分三种完全不同的故障：
        //   a) 根本没触发刷新   -> 日志里一条 refresh# 都没有
        //   b) 触发了但返回空   -> got 0
        //   c) 返回了但落错 K   -> got N，配合上面 agg 那条日志看时间戳
        // 缺了这条的话，角标上的 n 不增长这一个现象对应上面三种可能，没法区分。
        private int _cgLiqRefreshLogCount = 0;
        // 放宽到 200 次（60 秒一轮 = 约 3 小时）。判断接口滞后需要连续观察
        // range 末尾随时间怎么推进，只记前 10 次根本看不出来。
        private const int CG_LIQ_REFRESH_LOG_MAX = 200;

        private DateTime _cgLiqLastRefreshUtc  = DateTime.MinValue;
        private bool     _cgLiqRefreshInFlight = false;
        private int      _cgLiqRefreshFails    = 0;
        private const int CG_LIQ_FAIL_GIVEUP   = 5;
        // 订阅时用的 symbol/exchange，刷新时要复用（初始化时保存下来）
        private string   _cgSymbol   = "";
        private string   _cgExchange = "";

        private void EnsureCryptoInit()
        {
            if (_cgInitState != 0) return;
            _cgInitState = 1;

            // TryGetService 走 DataProvider.GetService<T>()，DataProvider 在指标
            // 挂到图表时才注入——OnCalculate 里调用时必然已就绪。
            bool okOi  = TryGetService(out _cgOiProv);
            bool okLsr = TryGetService(out _cgLsrProv);
            bool okLiq = TryGetService(out _cgLiqOrdProv);
            bool okAgg = TryGetService(out _cgLiqAggProv);

            if (!okOi && !okLsr && !okLiq)
            {
                // 平台没注册 Coinglass 服务（授权等级不含 Crypto Metrics 时会
                // 这样）。不是错误，只是这台机器上没这份数据。
                _cgInitState = 3;
                _cgStatus    = "N/A";
                return;
            }

            string symbol   = "";
            string exchange = "";
            try
            {
                symbol   = InstrumentInfo?.Instrument ?? "";
                exchange = InstrumentInfo?.Exchange   ?? "";
            }
            catch { }

            _cgSymbol   = symbol;
            _cgExchange = exchange;

            if (string.IsNullOrEmpty(symbol))
            {
                _cgInitState = 3;
                _cgStatus    = "NOSYM";
                return;
            }

            _ = InitCryptoAsync(symbol, exchange);
        }

        private async Task InitCryptoAsync(string symbol, string exchange)
        {
            try
            {
                // 官方指标也是先查 SupportedInstruments 再决定要不要订阅。
                // 2026-08-09 实测修正：SupportedInstruments 里的元素不是裸
                // symbol，而是 "SYMBOL@EXCHANGE" 复合格式（实际取值例：
                // BTCUSDT@BinanceFutures / BTCUSDT@Bybit / BTC-USDT-SWAP@
                // OkxPerpFutures），初版按裸 "BTCUSDT" 去 Contains 必然落空，
                // 币安永续图表被误判成 NOSUP。交易所段的字面值恰好就等于
                // InstrumentInfo.Exchange（BinanceFutures / OkxPerpFutures），
                // 所以拼起来即可，不需要任何映射表。
                // 注意只有这个校验用复合 key，下面 Request/Subscribe 的
                // Symbol / Exchange 仍然是分开的两个字段。
                var pars = _cgOiProv is not null
                    ? await _cgOiProv.GetFeedParametersAsync(CancellationToken.None).ConfigureAwait(false)
                    : null;

                string feedKey = $"{symbol}@{exchange}";

                if (pars?.SupportedInstruments is not null &&
                    !pars.SupportedInstruments.Contains(feedKey))
                {
                    _cgInitState = 3;
                    _cgStatus    = "NOSUP";
                    try
                    {
                        LoggerHelper.LogInfo(this, "{0}", new object[]
                        {
                            $"[AtasBridge/Coinglass] \"{feedKey}\" not in SupportedInstruments. " +
                            $"Supported sample: " +
                            string.Join(", ", pars.SupportedInstruments.Take(20))
                        });
                    }
                    catch { }
                    return;
                }

                var to   = DateTime.UtcNow;
                var from = to.AddHours(-Math.Max(1, CryptoHistoryHours));
                var tf   = ChartTimeFrameSpan();

                // ── 历史 ─────────────────────────────────────────────────────
                if (_cgOiProv is not null)
                {
                    var hist = await _cgOiProv.GetHistoryAsync(new OpenInterestCoinglassRequest
                    {
                        Symbol = symbol, Exchange = exchange,
                        From = from, To = to, Timeframe = tf
                    }, CancellationToken.None).ConfigureAwait(false);

                    if (hist is not null)
                        foreach (var x in hist) _cgOiQueue.Enqueue(x);
                }

                if (_cgLsrProv is not null)
                {
                    var hist = await _cgLsrProv.GetHistoryAsync(new LongShortRatioCoinglassRequest
                    {
                        Symbol = symbol, Exchange = exchange,
                        From = from, To = to, Timeframe = tf
                    }, CancellationToken.None).ConfigureAwait(false);

                    if (hist is not null)
                        foreach (var x in hist) _cgLsrQueue.Enqueue(x);
                }

                // 爆仓首次填充：走和后续周期刷新同一条路径，不另写一份逻辑。
                if (EnableLiquidations)
                    await RefreshLiquidationsAsync(from, to).ConfigureAwait(false);

                // ── 实时订阅 ─────────────────────────────────────────────────
                // Handler 在网络线程触发，只允许入队。
                if (_cgOiProv is not null)
                {
                    _cgOiSub = new CoinglassSubscriptionParams<OpenInterestOhlc>
                    {
                        Symbol = symbol, Exchange = exchange,
                        Handler = x => { if (x is not null) _cgOiQueue.Enqueue(x); }
                    };
                    _cgOiProv.Subscribe(_cgOiSub);
                }

                if (_cgLsrProv is not null)
                {
                    _cgLsrSub = new CoinglassSubscriptionParams<LongShortRatio>
                    {
                        Symbol = symbol, Exchange = exchange,
                        Handler = x => { if (x is not null) _cgLsrQueue.Enqueue(x); }
                    };
                    _cgLsrProv.Subscribe(_cgLsrSub);
                }

                if (_cgLiqOrdProv is not null && EnableLiquidations)
                {
                    _cgLiqSub = new LiquidationSubscriptionParams
                    {
                        Symbol = symbol, Exchange = exchange,
                        Mode = LiquidationMode,
                        Handler = x => { if (x is not null) _cgLiqLiveQueue.Enqueue(x); }
                    };
                    _cgLiqOrdProv.Subscribe(_cgLiqSub);
                }

                _cgInitState = 2;
                _cgStatus    = "OK";

                if (RunLiquidationProbe) _ = RunLiquidationProbeAsync(pars);
            }
            catch (Exception ex)
            {
                _cgInitState = 3;
                _cgStatus    = "ERR";
                try
                {
                    LoggerHelper.LogInfo(this, "{0}", new object[]
                    { $"[AtasBridge/Coinglass] init failed: {ex.Message}" });
                }
                catch { }
            }
        }

        // 一次性对照实验：同一个聚合接口，换不同的 Timeframe / 时间窗口各请求
        // 一次，把返回的条数和实际时间范围打进日志。要回答的问题：
        //   1) UpdatePeriodLimit 到底是多少（怀疑第二次起返回空是被它限流）
        //   2) From/To 到底认不认（#1 请求 2 小时却返回了前一整天）
        //   3) 换 Timeframe 能不能拿到今天的数据
        //   4) 返回的 Time 是 UTC 还是本地时间（日志里同时打印 utcNow/localNow
        //      作为标尺，避免再靠猜时区）
        // 每组之间隔几秒，免得请求本身触发限流反而污染结论。
        private async Task RunLiquidationProbeAsync(CoinglassDatafeedParameters? pars)
        {
            if (_cgLiqAggProv is null) return;
            try
            {
                Log($"probe start: UpdatePeriodLimit={pars?.UpdatePeriodLimit?.ToString() ?? "null"} " +
                    $"utcNow={DateTime.UtcNow:MM-dd HH:mm:ss} localNow={DateTime.Now:MM-dd HH:mm:ss} " +
                    $"sym={_cgSymbol} exch={_cgExchange} mode={LiquidationMode}");

                var cases = new (string name, TimeSpan tf, TimeSpan window)[]
                {
                    ("tf5m_30min", TimeSpan.FromMinutes(5), TimeSpan.FromMinutes(30)),
                    ("tf5m_2h",    TimeSpan.FromMinutes(5), TimeSpan.FromHours(2)),
                    ("tf5m_24h",   TimeSpan.FromMinutes(5), TimeSpan.FromHours(24)),
                    ("tf1m_2h",    TimeSpan.FromMinutes(1), TimeSpan.FromHours(2)),
                    ("tf1h_24h",   TimeSpan.FromHours(1),   TimeSpan.FromHours(24)),
                    ("tf1h_7d",    TimeSpan.FromHours(1),   TimeSpan.FromDays(7)),
                };

                foreach (var c in cases)
                {
                    await Task.Delay(4000).ConfigureAwait(false);
                    var to   = DateTime.UtcNow;
                    var from = to - c.window;
                    try
                    {
                        var r = await _cgLiqAggProv.GetHistoryAsync(new AggregatedLiquidationsCoinglassRequest
                        {
                            Symbol = _cgSymbol, Exchange = _cgExchange,
                            From = from, To = to, Timeframe = c.tf,
                            AggregationMode = LiquidationMode
                        }, CancellationToken.None).ConfigureAwait(false);

                        var l   = r?.Aggregations;
                        int cnt = l?.Count ?? -1;
                        Log($"probe {c.name}: req from={from:MM-dd HH:mm} to={to:MM-dd HH:mm} => got={cnt} " +
                            $"range={(cnt > 0 ? l[0].Time.ToString("MM-dd HH:mm") : "-")}.." +
                            $"{(cnt > 0 ? l[cnt - 1].Time.ToString("MM-dd HH:mm") : "-")} " +
                            $"lastTail={(cnt > 0 ? $"L={l[cnt - 1].Longs} S={l[cnt - 1].Shorts}" : "-")} " +
                            $"lastOrderId={r?.LastOrderId}");
                    }
                    catch (Exception ex) { Log($"probe {c.name}: EX {ex.GetType().Name} {ex.Message}"); }
                }

                // 逐笔历史 × 聚合档位对照组。上一轮三个窗口全是 500，而三组用的
                // 都是 SymbolGlobal —— 逐笔单子天然属于"某交易所的某币种"，
                // SymbolGlobal/Global 这种跨交易所聚合对逐笔可能根本不成立，
                // 服务端因此 500。这一轮固定窗口、只变档位，把这个可能性排掉。
                var modes = new[]
                {
                    LiquidationsAggregationModes.Local,
                    LiquidationsAggregationModes.SymbolGlobal,
                    LiquidationsAggregationModes.Global,
                };

                if (_cgLiqOrdProv is not null)
                {
                    foreach (var m in modes)
                    {
                        await Task.Delay(8000).ConfigureAwait(false);
                        var to   = DateTime.UtcNow;
                        var from = to.AddHours(-2);
                        try
                        {
                            var r = await _cgLiqOrdProv.GetHistoryAsync(new LiquidationOrdersCoinglassRequest
                            {
                                Symbol = _cgSymbol, Exchange = _cgExchange,
                                From = from, To = to,
                                AggregationMode = m
                            }, CancellationToken.None).ConfigureAwait(false);

                            int cnt = r?.Count ?? -1;
                            Log($"probe ord_mode_{m}: req from={from:MM-dd HH:mm} to={to:MM-dd HH:mm} => got={cnt} " +
                                $"range={(cnt > 0 ? r[0].Time.ToString("MM-dd HH:mm:ss") : "-")}.." +
                                $"{(cnt > 0 ? r[cnt - 1].Time.ToString("MM-dd HH:mm:ss") : "-")} " +
                                $"lastTail={(cnt > 0 ? $"side={r[cnt - 1].LiquidationSide} vol={r[cnt - 1].Volume} id={r[cnt - 1].Id}" : "-")}");
                        }
                        catch (Exception ex) { Log($"probe ord_mode_{m}: EX {ex.GetType().Name} {ex.Message}"); }
                    }
                }

                // 聚合接口也过一遍档位：上一轮只测了 SymbolGlobal，结论是 T+1。
                // 万一 Local 档能拿到当天数据，那爆仓就还有救。
                if (_cgLiqAggProv is not null)
                {
                    foreach (var m in modes)
                    {
                        await Task.Delay(8000).ConfigureAwait(false);
                        var to   = DateTime.UtcNow;
                        var from = to.AddHours(-24);
                        try
                        {
                            var r = await _cgLiqAggProv.GetHistoryAsync(new AggregatedLiquidationsCoinglassRequest
                            {
                                Symbol = _cgSymbol, Exchange = _cgExchange,
                                From = from, To = to, Timeframe = TimeSpan.FromMinutes(5),
                                AggregationMode = m
                            }, CancellationToken.None).ConfigureAwait(false);

                            var l   = r?.Aggregations;
                            int cnt = l?.Count ?? -1;
                            Log($"probe agg_mode_{m}: req from={from:MM-dd HH:mm} to={to:MM-dd HH:mm} => got={cnt} " +
                                $"range={(cnt > 0 ? l[0].Time.ToString("MM-dd HH:mm") : "-")}.." +
                                $"{(cnt > 0 ? l[cnt - 1].Time.ToString("MM-dd HH:mm") : "-")}");
                        }
                        catch (Exception ex) { Log($"probe agg_mode_{m}: EX {ex.GetType().Name} {ex.Message}"); }
                    }
                }

                Log("probe done");
            }
            catch (Exception ex) { Log($"probe failed: {ex.Message}"); }
        }

        // "距今多久"的紧凑写法，给角标用：45s / 12m / 3h / -（从没收到过）
        private static string Age(DateTime? utc)
        {
            if (!utc.HasValue) return "-";
            var d = DateTime.UtcNow - utc.Value;
            if (d.TotalSeconds < 90)   return $"{d.TotalSeconds:0}s";
            if (d.TotalMinutes < 90)   return $"{d.TotalMinutes:0}m";
            return $"{d.TotalHours:0}h";
        }

        private void Log(string msg)
        {
            try { LoggerHelper.LogInfo(this, "{0}", new object[] { "[AtasBridge/Coinglass] " + msg }); }
            catch { }
        }

        // OI / 多空比的**兜底**数据源：周期性拉历史。
        //
        // 分工（2026-08-09 实测厘清）：正常情况下**订阅是主数据源**，角标年龄
        // 稳定在几十秒内、数值与官方指标逐位吻合。但订阅会偶发静默——曾出现
        // 数值卡死在 107229、年龄一路涨到 @4m 仍在涨，而官方指标同期 106430
        // 且正常更新（诱因很可能是短时间内反复重载指标 + 密集探针请求把连接
        // 搞坏或触发限流）。静默时数值会停在旧值上，比没有数据更危险。
        //
        // 所以这条轮询存在的意义就是订阅静默时接住它。每 60 秒一次
        // （UpdatePeriodLimit 是 1 分钟），队列消费时自然取到最后一条即最新值；
        // 订阅活着的时候这条只是重复喂同样的值，无副作用。
        private void MaybeRefreshOiLsr()
        {
            if (_cgInitState != 2) return;
            if (_cgOiRefreshInFlight) return;
            if ((DateTime.UtcNow - _cgOiLastRefreshUtc).TotalSeconds < 60) return;

            _cgOiLastRefreshUtc = DateTime.UtcNow;
            _ = RefreshOiLsrAsync();
        }

        private async Task RefreshOiLsrAsync()
        {
            _cgOiRefreshInFlight = true;
            try
            {
                // 与官方指标同一套参数口径（见 CoinglassFromTime 处的说明）：
                // from=图表首根 K 的时间、to=DateTime.MaxValue、周期取自 ChartInfo。
                // 之前用 to=UtcNow + 30 分钟窗口时实测 got=0。
                var from = IncrementalFrom(_cgOiFullPulled);
                var to   = DateTime.MaxValue;
                var tf   = CoinglassTimeframe();
                int oiN = -1, lsrN = -1;
                string oiLast = "-", lsrLast = "-";

                if (_cgOiProv is not null)
                {
                    var h = await _cgOiProv.GetHistoryAsync(new OpenInterestCoinglassRequest
                    {
                        Symbol = _cgSymbol, Exchange = _cgExchange,
                        From = from, To = to, Timeframe = tf
                    }, CancellationToken.None).ConfigureAwait(false);

                    if (h is not null)
                    {
                        oiN = h.Count;
                        foreach (var x in h) _cgOiQueue.Enqueue(x);
                        if (oiN > 0) oiLast = $"{h[oiN - 1].Time:MM-dd HH:mm}/{h[oiN - 1].Close}";
                    }
                }

                if (_cgLsrProv is not null)
                {
                    var h = await _cgLsrProv.GetHistoryAsync(new LongShortRatioCoinglassRequest
                    {
                        Symbol = _cgSymbol, Exchange = _cgExchange,
                        From = from, To = to, Timeframe = tf
                    }, CancellationToken.None).ConfigureAwait(false);

                    if (h is not null)
                    {
                        lsrN = h.Count;
                        foreach (var x in h) _cgLsrQueue.Enqueue(x);
                        if (lsrN > 0) lsrLast = $"{h[lsrN - 1].Time:MM-dd HH:mm}/{h[lsrN - 1].LongShortRatioValue}";
                    }
                }

                // 有数据就转入增量窗口；两条都空则复位，下一轮退回全量。
                if (oiN > 0 || lsrN > 0) _cgOiFullPulled = true;
                else                     _cgOiFullPulled = false;

                if (_cgOiRefreshLogCount < 5)
                {
                    _cgOiRefreshLogCount++;
                    Log($"oi/lsr refresh #{_cgOiRefreshLogCount}: oi got={oiN} last={oiLast} | " +
                        $"lsr got={lsrN} last={lsrLast} | req from={from:MM-dd HH:mm} to=MaxValue tf={tf}");
                }
            }
            catch (Exception ex)
            {
                _cgOiFullPulled = false;   // 下一轮退回全量窗口再试
                if (_cgOiRefreshLogCount < 5)
                {
                    _cgOiRefreshLogCount++;
                    Log($"oi/lsr refresh failed: {ex.Message}");
                }
            }
            finally { _cgOiRefreshInFlight = false; }
        }

        // 看门狗：超过 CG_STALE_RESUB 没收到任何数据就退订重订一次。
        // 注意：上面那条 60 秒轮询接管成为主数据源之后，_cgLastDataUtc 会被
        // 轮询不断刷新，所以这个看门狗正常情况下**不会触发**——它现在的意义
        // 是兜底：只有连轮询也一起失效（网络断了之类）时才会走到这里。
        private void MaybeResubscribeCrypto()
        {
            if (_cgInitState != 2) return;

            var last = _cgLastDataUtc ?? DateTime.MinValue;
            if (DateTime.UtcNow - last < CG_STALE_RESUB) return;
            if (DateTime.UtcNow - _cgLastResubUtc < CG_STALE_RESUB) return;

            _cgLastResubUtc = DateTime.UtcNow;
            Log($"feed stale (last data {(_cgLastDataUtc.HasValue ? (DateTime.UtcNow - last).TotalSeconds.ToString("0") + "s ago" : "never")}), resubscribing");

            try
            {
                if (_cgOiSub is not null)
                {
                    _cgOiProv?.Unsubscribe(_cgOiSub);
                    _cgOiProv?.Subscribe(_cgOiSub);
                }
                if (_cgLsrSub is not null)
                {
                    _cgLsrProv?.Unsubscribe(_cgLsrSub);
                    _cgLsrProv?.Subscribe(_cgLsrSub);
                }
            }
            catch (Exception ex) { Log($"resubscribe failed: {ex.Message}"); }
        }

        // 周期性重拉聚合爆仓。只刷最近一小段（默认 2 小时），够覆盖当前这根
        // 还在形成的 K 和刚收的几根即可，不必每次重拉 24 小时。
        private void MaybeRefreshLiquidations()
        {
            if (!EnableLiquidations) return;
            if (_cgInitState != 2 || _cgLiqOrdProv is null) return;
            if (_cgLiqRefreshInFlight) return;
            // 连续失败够多次就彻底停手：逐笔历史接口实测会直接 500，没必要
            // 每分钟撞一次、把 ATAS 日志刷满。角标会停在 ERR 上，看得见。
            if (_cgLiqRefreshFails >= CG_LIQ_FAIL_GIVEUP) return;

            // 下限 60 秒：UpdatePeriodLimit 实测就是 1 分钟，比它密只会被限流
            // 返回空（v-6 那轮 refresh #2..#10 全空就是这么来的）。
            int period = Math.Max(60, LiquidationRefreshSeconds);
            if ((DateTime.UtcNow - _cgLiqLastRefreshUtc).TotalSeconds < period) return;

            _cgLiqLastRefreshUtc = DateTime.UtcNow;
            // 首轮全量，之后用**每轮都在变**的 24 小时滚动窗口。
            //
            // 2026-08-09 实测到的关键行为：重复请求**完全相同**的窗口，服务端
            // 直接返回缓存。日志实证——连续 5 轮 `agg=6793`、range 末尾都停在
            // `08-09 10:00` 一个字不变，而 lag 从 40m 一路涨到 44m；同一份日志
            // 里 OI 用的是每轮递增的 from（08:40 / 08:41 / 08:42），返回值就在
            // 持续变化。
            //
            // 所以 -17 退回固定全量窗口是退错了方向：全量窗口能拿到**请求那一刻**
            // 的最新数据（官方指标只在加载时拉一次，所以它是对的），但每 60 秒
            // 重复同一个请求就只吃缓存。要持续更新，From 必须每轮都不同。
            //
            // 窗口取 24 小时：-15 用过的 2 小时太小（这接口对窗口大小敏感，早先
            // To 传错时小于一天的窗口直接返回空），24 小时是验证过有数据的量级。
            _ = RefreshLiquidationsAsync(IncrementalFrom(_cgLiqFullPulled, 24), DateTime.MaxValue);
        }

        private async Task RefreshLiquidationsAsync(DateTime from, DateTime to)
        {
            if (_cgLiqOrdProv is null) return;
            _cgLiqRefreshInFlight = true;
            try
            {
                // 官方是按 ChartInfo.ChartType 在这两条之间二选一，但那个用来
                // 比较的字符串常量被混淆加密了读不出来。所以这里两条都试：
                // 先逐笔（粒度细），拿不到再退聚合。哪条真正生效看日志的 via=。
                int ordCnt = -1, aggCnt = -1;
                string ordRange = "-", aggRange = "-", err = "";

                try
                {
                    var orders = await _cgLiqOrdProv.GetHistoryAsync(new LiquidationOrdersCoinglassRequest
                    {
                        Symbol = _cgSymbol, Exchange = _cgExchange,
                        From = from, To = to,
                        AggregationMode = LiquidationMode
                    }, CancellationToken.None).ConfigureAwait(false);

                    ordCnt = orders?.Count ?? -1;
                    if (ordCnt > 0)
                    {
                        foreach (var o in orders)
                            if (o is not null) _cgLiqQueue.Enqueue(o);
                        ordRange = $"{orders[0].Time:MM-dd HH:mm:ss}..{orders[ordCnt - 1].Time:MM-dd HH:mm:ss}";
                        _cgLiqDataTime = DateTime.SpecifyKind(orders[ordCnt - 1].Time, DateTimeKind.Utc);
                    }
                }
                catch (Exception ex) { err += $"ord:{ex.Message}; "; }

                if (ordCnt <= 0 && _cgLiqAggProv is not null)
                {
                    try
                    {
                        var agg = await _cgLiqAggProv.GetHistoryAsync(new AggregatedLiquidationsCoinglassRequest
                        {
                            Symbol = _cgSymbol, Exchange = _cgExchange,
                            From = from, To = to, Timeframe = CoinglassTimeframe(),
                            AggregationMode = LiquidationMode
                        }, CancellationToken.None).ConfigureAwait(false);

                        var l = agg?.Aggregations;
                        aggCnt = l?.Count ?? -1;
                        if (aggCnt > 0)
                        {
                            foreach (var a in l)
                                if (a is not null) _cgLiqAggQueue.Enqueue(a);
                            aggRange = $"{l[0].Time:MM-dd HH:mm}..{l[aggCnt - 1].Time:MM-dd HH:mm}";
                            _cgLiqDataTime = DateTime.SpecifyKind(l[aggCnt - 1].Time, DateTimeKind.Utc);
                        }
                    }
                    catch (Exception ex) { err += $"agg:{ex.Message}; "; }
                }

                if (_cgLiqRefreshLogCount < CG_LIQ_REFRESH_LOG_MAX)
                {
                    _cgLiqRefreshLogCount++;
                    try
                    {
                        string via = ordCnt > 0 ? "orders" : aggCnt > 0 ? "agg" : "none";
                        LoggerHelper.LogInfo(this, "{0}", new object[]
                        {
                            $"[AtasBridge/Coinglass] liq refresh #{_cgLiqRefreshLogCount} via={via}: " +
                            $"ord={ordCnt} [{ordRange}] agg={aggCnt} [{aggRange}] " +
                            $"| req sym={_cgSymbol} exch={_cgExchange} mode={LiquidationMode} " +
                            $"tf={CoinglassTimeframe()} from={from:MM-dd HH:mm} to=MaxValue " +
                            $"utcNow={DateTime.UtcNow:MM-dd HH:mm:ss} lag={Age(_cgLiqDataTime)} {err}"
                        });
                    }
                    catch { }
                }

                if (ordCnt <= 0 && aggCnt <= 0) throw new Exception("both endpoints empty: " + err);

                // 拿到数据就认为全量已完成，之后走增量；增量落空则复位重来。
                _cgLiqFullPulled = true;
                _cgLiqLastOkUtc  = DateTime.UtcNow;
                _cgLiqRefreshFails = 0;
            }
            catch (Exception ex)
            {
                _cgLiqFullPulled = false;   // 下一轮退回全量窗口再试
                _cgLiqRefreshFails++;
                // 只在头几次失败时记日志，避免刷屏——持续失败的话角标上的
                // 计数会停止增长，一眼能看出来。
                if (_cgLiqRefreshFails <= 3)
                {
                    try
                    {
                        LoggerHelper.LogInfo(this, "{0}", new object[]
                        { $"[AtasBridge/Coinglass] liquidation refresh failed: {ex.Message}" });
                    }
                    catch { }
                }
            }
            finally { _cgLiqRefreshInFlight = false; }
        }

        // ── 2026-08-09：按官方指标的参数口径请求历史 ─────────────────────────
        // 反编译 AggregatedLiquidations 初始化后的主流程，它是这么填的：
        //     from = GetCandle(0).Time         图表第一根 K 的时间
        //     to   = DateTime.MaxValue         ★不是 UtcNow★
        //     tf   = Extensions.GetTimeFrameTypeChartPeriod(ChartInfo)
        // 然后按 ChartInfo.ChartType 在"逐笔历史"和"聚合历史"之间二选一。
        //
        // 之前所有失败（窗口小于一天返回 0、聚合只到昨天、逐笔 500）很可能
        // 都是同一个错误的不同表现：我一直传 To=DateTime.UtcNow。服务端多半
        // 拿 To 当"截止到某个已完成边界"来解释，于是把当天数据整段截掉。
        private DateTime CoinglassFromTime()
        {
            try
            {
                var c = GetCandle(0);
                if (c is not null) return c.Time;
            }
            catch { }
            return DateTime.UtcNow.AddHours(-Math.Max(1, CryptoHistoryHours));
        }

        // 首次拉全量（从图表第一根 K 开始），之后只拉最近一小段。
        //
        // 官方指标只在初始化时拉一次，我们是每 60 秒一轮，用同样的全量窗口就
        // 太浪费了：实测图表加载了一个多月历史时，一次就是 5245 条聚合 + 8470
        // 条 OI + 8470 条多空比，每分钟重复解析两万多条记录只为拿最后几条。
        // 所以后续轮次改用 2 小时窗口（To 仍然必须是 MaxValue —— 那才是拿到
        // 当天数据的关键）。万一某次增量返回空，就把标志复位，下一轮退回全量，
        // 不至于因为窗口收窄而卡住不更新。
        private bool _cgLiqFullPulled = false;
        private bool _cgOiFullPulled  = false;

        // 注意 DateTime.UtcNow 精确到秒以下，所以每轮的 From 天然都不一样 ——
        // 这正是绕开服务端缓存所必需的（见 MaybeRefreshLiquidations 处的说明）。
        private DateTime IncrementalFrom(bool fullPulled, int hours = 2)
            => fullPulled ? DateTime.UtcNow.AddHours(-hours) : CoinglassFromTime();

        // 官方用的周期来源。拿不到就退回按"时间周期标签"设置项解析。
        private TimeSpan CoinglassTimeframe()
        {
            try
            {
                if (ChartInfo is not null)
                {
                    var tf = ATAS.Indicators.Extensions.GetTimeFrameTypeChartPeriod(ChartInfo);
                    if (tf > TimeSpan.Zero) return tf;
                }
            }
            catch { }
            return ChartTimeFrameSpan();
        }

        // Coinglass 的历史请求要一个周期长度。ATAS 的 TimeFrame 是
        // "5m"/"H1"/"1 Hour" 这类展示用字符串，格式随图表类型变化，解析不可靠；
        // 这里直接用指标自己那个"时间周期标签"设置项（本来就是给 VPS 用的、
        // Sea 手填的权威值），解析失败退回 5 分钟，只影响历史粒度，不影响实时。
        private TimeSpan ChartTimeFrameSpan()
        {
            var s = (Timeframe ?? "").Trim().ToLowerInvariant();
            if (s.Length >= 2 && int.TryParse(s.Substring(0, s.Length - 1), out int n) && n > 0)
            {
                switch (s[s.Length - 1])
                {
                    case 'm': return TimeSpan.FromMinutes(n);
                    case 'h': return TimeSpan.FromHours(n);
                    case 'd': return TimeSpan.FromDays(n);
                }
            }
            return TimeSpan.FromMinutes(5);
        }

        // 在 OnCalculate（ATAS 计算线程）里消费三条队列。所有 K 线归属计算
        // 都在这里做，网络回调线程永远不碰 GetCandle/CurrentBar。
        private void DrainCryptoQueues()
        {
            while (_cgOiQueue.TryDequeue(out var oi))
            {
                _cgOiClose     = oi.Close;
                _cgOiLastUtc   = DateTime.UtcNow;
                _cgLastDataUtc = DateTime.UtcNow;
            }

            while (_cgLsrQueue.TryDequeue(out var lsr))
            {
                _cgLsrValue    = lsr.LongShortRatioValue;
                _cgLsrLastUtc  = DateTime.UtcNow;
                _cgLastDataUtc = DateTime.UtcNow;
            }

            // 爆仓：逐笔历史拉回来的单子，按 id 去重后累加到所属 K 线。
            // 反复拉取重叠时间段是常态（每次都拉最近一段），去重保证不重复计。
            while (_cgLiqQueue.TryDequeue(out var liq))
            {
                if (liq.Volume <= 0m) continue;
                if (liq.LiquidationSide == LiquidationOrderSides.None) continue;

                if (_cgLiqSeenIds.Count >= CG_LIQ_SEEN_MAX) _cgLiqSeenIds.Clear();
                if (!_cgLiqSeenIds.Add(liq.Id)) continue;

                var utc = DateTime.SpecifyKind(liq.Time, DateTimeKind.Utc);
                int b   = FindBarContainingUtcTime(utc);

                // LiquidationOrderSides 实际取值是 None/Longs/Shorts（反射确认，
                // 不是常见的 Buy/Sell），语义就是"哪一边被爆"，无需方向反推。
                var dict = liq.LiquidationSide == LiquidationOrderSides.Longs
                    ? _cgLiqLongs : _cgLiqShorts;
                dict[b] = dict.TryGetValue(b, out var cur) ? cur + liq.Volume : liq.Volume;

                if (_cgLiqDiagCount < CG_LIQ_DIAG_MAX)
                {
                    _cgLiqDiagCount++;
                    try
                    {
                        var bc = GetCandle(b);
                        var cc = GetCandle(CurrentBar);
                        LoggerHelper.LogInfo(this, "{0}", new object[]
                        {
                            $"[AtasBridge/Coinglass] ord t={liq.Time:yyyy-MM-dd HH:mm:ss} kind={liq.Time.Kind} " +
                            $"side={liq.LiquidationSide} vol={liq.Volume} -> bar {b} (barTime={bc?.Time:yyyy-MM-dd HH:mm:ss}) | " +
                            $"currentBar={CurrentBar} (barTime={cc?.Time:yyyy-MM-dd HH:mm:ss}) utcNow={DateTime.UtcNow:yyyy-MM-dd HH:mm:ss}"
                        });
                    }
                    catch { }
                }

                _cgLiqAggCount++;
                _cgLastDataUtc = DateTime.UtcNow;
            }

            // 聚合历史（逐笔拿不到时的退路）：按 K 线覆盖式写入。覆盖而非累加，
            // 所以反复刷同一段不会把数字越滚越大；跟上面逐笔累加的结果同写一对
            // 字典，同一时刻只会有一条路径在供数（日志里的 via= 指明是哪条）。
            AggregatedLiquidations? lastAgg = null;
            int lastAggBar = -1;

            while (_cgLiqAggQueue.TryDequeue(out var a))
            {
                var utc = DateTime.SpecifyKind(a.Time, DateTimeKind.Utc);
                int b   = FindBarContainingUtcTime(utc);
                _cgLiqLongs[b]  = a.Longs;
                _cgLiqShorts[b] = a.Shorts;

                lastAgg = a; lastAggBar = b;

                _cgLiqAggCount++;
                _cgLastDataUtc = DateTime.UtcNow;
            }

            // 只记每批的**最后一条**。此前记的是前 20 条，全是几周前的老数据，
            // 恰恰看不到最新那条落在哪——而"数据拿到了但归属错位"正是要靠最新
            // 一条才能判断：把 t / 落到的 bar / currentBar 的 barTime / 当前
            // UTC 时间摆在一起，时区口径对不对一眼可见。
            if (lastAgg is not null && _cgLiqDiagCount < CG_LIQ_DIAG_MAX)
            {
                _cgLiqDiagCount++;

                // 分两段拼：核心字段（时间戳、bar 号、当前 bar 存值）不依赖
                // 任何可能抛异常的调用，先拿到手；GetCandle 单独包 try——上一版
                // 整条日志裹在一个 try 里，GetCandle 一抛就把整行吞了，结果日志
                // 里一条 agg LAST 都没有，白等一轮。
                string core =
                    $"agg LAST t={lastAgg.Time:MM-dd HH:mm:ss} kind={lastAgg.Time.Kind} " +
                    $"L={lastAgg.Longs} S={lastAgg.Shorts} -> bar {lastAggBar} | currentBar={CurrentBar} " +
                    $"utcNow={DateTime.UtcNow:MM-dd HH:mm:ss} localNow={DateTime.Now:MM-dd HH:mm:ss}";

                decimal cvl = 0m, cvs = 0m;
                _cgLiqLongs.TryGetValue(CurrentBar, out cvl);
                _cgLiqShorts.TryGetValue(CurrentBar, out cvs);
                core += $" curBarVal={cvl}/{cvs}";

                string times = "";
                try
                {
                    var bc = GetCandle(lastAggBar);
                    var cc = GetCandle(CurrentBar);
                    times = $" barTime={bc?.Time:MM-dd HH:mm:ss} curBarTime={cc?.Time:MM-dd HH:mm:ss}";
                }
                catch (Exception ex) { times = $" [GetCandle EX {ex.GetType().Name}]"; }

                // 本小时各 bar 的存值，直接看数据到底落在哪几根上
                string hourDump = "";
                try
                {
                    var sb = new StringBuilder(" hourBars=");
                    for (int i = Math.Max(0, CurrentBar - 11); i <= CurrentBar; i++)
                    {
                        _cgLiqLongs.TryGetValue(i, out var hl);
                        _cgLiqShorts.TryGetValue(i, out var hs);
                        if (hl != 0m || hs != 0m) sb.Append($"[{i}:{hl:0}/{hs:0}]");
                    }
                    hourDump = sb.ToString();
                }
                catch { }

                Log(core + times + hourDump);
            }

            // 逐笔**实时流**（Subscribe 那条）：目前 ATAS 这条通道不推送，
            // 只数条数，不参与数值，免得跟上面拉回来的历史重复计算。
            while (_cgLiqLiveQueue.TryDequeue(out _)) _cgLiqLiveCount++;

            // 老 bar 的爆仓累计值留着没用（推送只看刚收的那根），
            // 定期裁掉，避免图表长期开着无限增长。
            if (_cgLiqLongs.Count > 5000)  TrimBarDict(_cgLiqLongs);
            if (_cgLiqShorts.Count > 5000) TrimBarDict(_cgLiqShorts);
        }

        private void TrimBarDict(Dictionary<int, decimal> d)
        {
            int keep = CurrentBar - 500;
            foreach (var k in d.Keys.Where(k => k < keep).ToList()) d.Remove(k);
        }

        // 当前**整点小时**内的爆仓合计，用来和 Coinglass 网页 1H 框架的柱子
        // 逐位对齐。
        //
        // 之前用的是"最近 12 根 K"的滚动窗口，跟网页的整点柱口径不同：越靠近
        // 整点差得越多（实测角标 9876 vs 网页 10.076K，差的就是已经滚出窗口、
        // 但仍属于本小时的那几根）。这里改成取 [本小时 00 分, 现在] 区间。
        //
        // 时区上不必换算：Coinglass 按 UTC 整点分桶，北京时间是整数小时偏移，
        // 两边的"整点"落在同一时刻。
        //
        // 图表周期 >= 1 小时时（4H/日线之类），一根 K 本身就跨越多个整点，
        // 分桶无意义，直接返回当前这根 K 的值。
        private (decimal longs, decimal shorts) LiquidationHourSum()
        {
            decimal sumL = 0m, sumS = 0m;

            if (CoinglassTimeframe() >= TimeSpan.FromHours(1))
            {
                _cgLiqLongs.TryGetValue(CurrentBar, out sumL);
                _cgLiqShorts.TryGetValue(CurrentBar, out sumS);
                return (sumL, sumS);
            }

            var now       = DateTime.UtcNow;
            var hourStart = new DateTime(now.Year, now.Month, now.Day, now.Hour, 0, 0, DateTimeKind.Utc);

            // 从当前 K 往回走，直到 K 的起始时间早于本整点小时为止。5 分钟
            // 粒度下最多 12 根，成本可以忽略；上限 500 根纯粹是防御。
            //
            // ⚠️ 取不到 K 线时必须 continue 而不是 break：`GetCandle(CurrentBar)`
            // 会抛 ArgumentOutOfRangeException（实测，CurrentBar 指向的那根在
            // 某些时刻还没建出来）。上一版写的是 catch { break; }，于是**第一次
            // 调用就退出循环、函数恒返回 0/0** —— 数据明明都在字典里
            // （日志 hourBars=[8474:586/53708]...），角标却一直显示 0/0，
            // 整轮排查全被这一个 break 带偏。
            for (int i = CurrentBar; i >= 0 && i > CurrentBar - 500; i--)
            {
                IndicatorCandle? c = null;
                try { c = GetCandle(i); } catch { }
                if (c is null) continue;

                DateTime t;
                try { t = DateTime.SpecifyKind(c.Time, DateTimeKind.Utc); } catch { continue; }
                if (t < hourStart) break;   // 只有确实读到了更早的 K 才停

                if (_cgLiqLongs.TryGetValue(i, out var vl))  sumL += vl;
                if (_cgLiqShorts.TryGetValue(i, out var vs)) sumS += vs;
            }

            return (sumL, sumS);
        }

        // 找"这个时间落在哪根 K 里"，即最后一根**开盘时间** <= target 的 K。
        // 跟上面 FindBarForUtcTime（比的是收盘时间 LastTime，用于把已经结束的
        // 信号标到它结束的那根 K 上）口径不同，不能混用：Coinglass 聚合值的
        // Time 是这根 K 的**起始**时间，拿去跟 LastTime 比会整体偏到前一根。
        private int FindBarContainingUtcTime(DateTime targetUtc)
        {
            int hi = CurrentBar;
            if (hi < 0) return 0;
            int lo = 0, result = 0;
            while (lo <= hi)
            {
                int mid = (lo + hi) / 2;
                var candle = GetCandle(mid);
                if (candle == null) { hi = mid - 1; continue; }

                DateTime openUtc;
                try { openUtc = DateTime.SpecifyKind(candle.Time, DateTimeKind.Utc); }
                catch { hi = mid - 1; continue; }

                if (openUtc <= targetUtc) { result = mid; lo = mid + 1; }
                else { hi = mid - 1; }
            }
            return result;
        }

        private void DisposeCrypto()
        {
            try { if (_cgOiSub  is not null) _cgOiProv?.Unsubscribe(_cgOiSub);   } catch { }
            try { if (_cgLsrSub is not null) _cgLsrProv?.Unsubscribe(_cgLsrSub); } catch { }
            try { if (_cgLiqSub is not null) _cgLiqOrdProv?.Unsubscribe(_cgLiqSub); } catch { }
            _cgOiSub = null; _cgLsrSub = null; _cgLiqSub = null;
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
        // Phase 7L: Coinglass 三路数据。只有指标里的"推送Coinglass字段到VPS"
        // 打开、且当路数据确实拿到了才会有值；否则为 null，被 _serOpts 的
        // WhenWritingNull 略掉，JSON 跟接入前完全一致。
        // cg_oi_close  = Coinglass 口径的未平仓合约收盘值（不同于上面的
        //                max_oi/min_oi——那两个是 ATAS 从交易所原生行情里
        //                拿的 K 线内 OI 极值，两者不要混用）
        // cg_lsr       = 多空账户比（Coinglass Long/Short Ratio 指标同源）
        // cg_liq_long / cg_liq_short = 这根 K 线内的多头/空头爆仓量
        public double?       CgOiClose        { get; set; }
        public double?       CgLsr            { get; set; }
        public double?       CgLiqLong        { get; set; }
        public double?       CgLiqShort       { get; set; }
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
        // 2026-08-02: 已结束信号的成交/失效时间(北京时间字符串)，用于把盘面上
        // 的位价线段精确画到"真正结束的那根K"，一眼看出这单跑了多久。
        public string? OutcomeAt { get; set; }
    }
}
