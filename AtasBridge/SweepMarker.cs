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
    // ENCODING NOTE (2026-08-12): the user facing strings in this file
    // (DisplayName / GroupName / Description / on-chart panel text) are Chinese
    // by explicit request, so this file is UTF-8 rather than pure ASCII.
    // Code COMMENTS stay English/ASCII, which is what the v5.1 convention was
    // actually about: PowerShell here-string editing has corrupted Chinese
    // comments in this repo before. When editing this file use a UTF-8 aware
    // editor or scp it in - do not pipe it through PowerShell.
    [DisplayName("SweepMarker")]
    [Category("Setups")]
    [Description(
        "Setup C 流动性扫除反转标记　" + AtasBridgeVersion.Tag + "\n" +
        "\n" +
        "■ 这个指标做什么\n" +
        "找「假突破 + 反转」的入场点。价格刺穿一处流动性池（前高/前低，也就是止损\n" +
        "堆积的地方）把止损扫掉，然后迅速收回 —— 这是反转信号。指标把入场/止损/\n" +
        "TP1/TP2 四条线直接画在图上，由你手动执行。不自动下单，不推送任何数据。\n" +
        "\n" +
        "■ 必须用 M5 图\n" +
        "主逻辑按 5 分钟周期设计。挂到其它周期时左上角会显示红色警告，此时数值\n" +
        "不可信，仅供观察。\n" +
        "\n" +
        "■ 图上各元素含义\n" +
        "　· 红色横线 = BSL 买方流动性池（前高，空头止损堆积处）\n" +
        "　· 绿色横线 = SSL 卖方流动性池（前低，多头止损堆积处）\n" +
        "　· 线右端 xN = 有 N 个摆动点合并成这一条（等高/等低）。N>=2 时线会加粗，\n" +
        "　　因为双顶/双底的止损更密集、信息量更高。若普遍出现 x8 以上，说明\n" +
        "　　「等高等低合并容差」太宽，应调小。\n" +
        "　· 灰色虚线 = 已被扫除过的池，不再触发信号\n" +
        "　· 黄色小三角 = 阶段一预警：扫除正在发生（未确认，不要立刻进）\n" +
        "　· 灰色小 X = 该次扫除已作废；鼠标悬停到那根 K 上会显示作废原因\n" +
        "　· 绿线=入场　红线=止损　蓝虚线=TP1(1.5R)　蓝实线=TP2(对面最近的池)\n" +
        "　· 信号右端标签：方向 | R值 | 盈亏比 | 建议仓位。淡色=弱信号，灰色=盈亏比不足\n" +
        "\n" +
        "■ 两阶段怎么配合（重要）\n" +
        "扫除只持续几十秒，等 M5 收盘再提示最佳入场价已经跑掉，所以拆成两级：\n" +
        "　阶段一（tick 级，不等收盘）：响预警音 + 画黄三角。此时只准备，不进场。\n" +
        "　阶段二（M5 收盘判定）：收盘收回池内 + 主动量衰竭 + 出现吸收 → 响确认音\n" +
        "　并画出完整的入场/止损/目标；不满足则响作废音（默认关闭）并画灰色 X。\n" +
        "看到黄三角就把手放到键盘上，听到确认音再动手。\n" +
        "\n" +
        "■ 三个核心判据\n" +
        "　· 穿透深度：太浅算触碰不算扫除；太深说明是真突破 → 见「最小/最大穿透」\n" +
        "　· ADR 主动量衰竭比 = 扫除后累计|Delta| ÷ 扫除那根K的|Delta|。\n" +
        "　　低 = 突破方力气用尽（好）；高 = 主动量还在持续（真突破，作废）。\n" +
        "　　ADR 落在「达标阈值」与「作废阈值」之间会画出来但标 WEAK，\n" +
        "　　这是特意留给你校准用的中间地带。\n" +
        "　· 吸收：收回过程中某个价位有大额被动挂单吃掉主动单，说明有人在这里护盘。\n" +
        "\n" +
        "■ 上手建议\n" +
        "　1. 先只看不做，确认池线位置与你自己读的前高前低一致\n" +
        "　2. 池太多就加大「摆动点左右K数」；合并数普遍过大就减小「合并容差」\n" +
        "　3. 信号太少就放宽「Delta尖峰分位」(5→10) 或「爆量倍数」(3→2)\n" +
        "　4. 「账户权益」和「单笔风险」填你自己的真实数字，仓位标签才有意义\n" +
        "\n" +
        "■ 注意\n" +
        "　· 仓位计算假设图表成交量口径是 BTC（币安 BTCUSDT 永续成立）\n" +
        "　· 同一个池确认或作废后不再触发；仅「超时未收回」允许再触发 1 次\n" +
        "　· 统计只用已收盘K线（不含当前未收盘K），因此复盘与实盘表现一致")]
    public class SweepMarker : Indicator
    {
        // ---- internal constants (deliberately not settings) ---------------
        // ATR periods are fixed: the card enumerates the exact settings list
        // and these are not on it. 14 is the conventional Wilder length.
        private const int ATR_M5_PERIOD = 14;
        private const int ATR_D1_PERIOD = 14;
        // Sound file names, WITHOUT the .wav extension.
        //
        // v2026.08.12-3 passed "alert1.wav" and nothing ever played, with no
        // exception thrown. ATAS's own alert-file pickers list the contents of
        // <install>\Sounds with the extension stripped ("xishouAbs", "geiger",
        // "tap"), which means the platform appends ".wav" itself - so passing
        // "alert1.wav" made it look for "alert1.wav.wav" and fail silently.
        // These three are the defaults; the actual values are settings so the
        // user can point them at their own sounds (this install has custom ones
        // like xishou / gengdan / qifei / zapan in the Platform Sounds folder).
        // alert1 / alert3 / beep_2_1 exist in BOTH ATAS and ATAS X.
        private const string SND_ALERT   = "alert1";
        private const string SND_CONFIRM = "alert3";
        private const string SND_INVALID = "beep_2_1";
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
        [Display(Name = "摆动点左右K数", GroupName = "1 流动性池", Order = 1,
                 Description = "某根K的高点要高过左右各N根才算摆动高点。调大=池更少但更重要；调小=池更多也更嘈杂")]
        public int PivotBars { get; set; } = 5;

        // How far back pools stay relevant. Older liquidity is usually already
        // taken and no longer attracts price.
        [Display(Name = "池回溯天数", GroupName = "1 流动性池", Order = 2,
                 Description = "超过这个天数的池视为过期。更早的流动性通常已被取走，不再吸引价格")]
        public int LookbackDays { get; set; } = 3;

        // Two swings closer than this (in daily ATR) are the same liquidity
        // shelf: equal highs / equal lows. Merging them avoids double signals
        // and flags the stronger double top / double bottom pattern.
        [Display(Name = "等高等低合并容差(×日ATR)", GroupName = "1 流动性池", Order = 3,
                 Description = "两个同类摆动点价差小于此值即视为同一条流动性架并合并，线右端 xN 就是合并数。若普遍出现 x8 以上说明此值太宽，建议改 0.03~0.05")]
        public decimal EqualTolerance { get; set; } = 0.1m;

        // ================= settings: sweep detection ====================

        // Price must come this close to a pool before we watch it at all.
        // Pure performance / noise filter: far away pools cannot be swept now.
        [Display(Name = "预备距离(×5分ATR)", GroupName = "2 扫除检测", Order = 1,
                 Description = "价格进入池的这个距离内才开始监控该池。纯降噪与性能过滤：离得远的池当下不可能被扫")]
        public decimal ArmDistance { get; set; } = 0.5m;

        // Minimum penetration to call it a sweep rather than a touch.
        [Display(Name = "最小穿透(×5分ATR)", GroupName = "2 扫除检测", Order = 2,
                 Description = "至少要刺穿这么深才算扫除，否则只是触碰。调大=只认明确的插针")]
        public decimal MinPenetration { get; set; } = 0.05m;

        // Beyond this the move is a real breakout, not a stop run.
        [Display(Name = "最大穿透(×5分ATR)", GroupName = "2 扫除检测", Order = 3,
                 Description = "刺穿超过这么深就是真突破而不是扫止损，该次直接作废")]
        public decimal MaxPenetration { get; set; } = 1.5m;

        // Delta spike percentile. A sweep is one-sided aggression, so the
        // sweeping bar's delta should sit in the tail of the recent
        // distribution (low tail for a long setup, high tail for a short).
        [Display(Name = "Delta尖峰分位(%)", GroupName = "2 扫除检测", Order = 4,
                 Description = "扫除是单边猛攻，所以扫除那根K的Delta要落在近期分布的尾部：做多看低尾(5%)，做空看高尾(95%)。信号太少可放宽到 10")]
        public decimal DeltaPercentile { get; set; } = 5m;

        // Volume burst multiple over the recent median. Stop runs trade a lot.
        [Display(Name = "爆量倍数", GroupName = "2 扫除检测", Order = 5,
                 Description = "当前K成交量需达到近期中位数的这个倍数。扫止损必然伴随放量。信号太少可降到 2")]
        public decimal VolMultiple { get; set; } = 3.0m;

        // Sample size for the percentile and the median above.
        [Display(Name = "分位与中位回看K数", GroupName = "2 扫除检测", Order = 6,
                 Description = "计算上面分位数与中位数的样本量。只取已收盘K线，不含当前未收盘K（避免前视偏差）")]
        public int DeltaVolLookback { get; set; } = 50;

        // ================= settings: confirmation ======================

        // How long price may stay outside the pool before we give up. A real
        // sweep snaps back fast.
        [Display(Name = "收回时限(K数)", GroupName = "3 确认", Order = 1,
                 Description = "价格最多允许在池外停留几根K。真扫除会很快收回；超时即作废")]
        public int ReclaimBars { get; set; } = 3;

        // ADR = aggression decay ratio: cumulative |delta| after the sweep bar
        // divided by the sweep bar |delta|. Low ADR means the aggression died
        // out, which is what a failed breakout looks like.
        [Display(Name = "ADR达标阈值", GroupName = "3 确认", Order = 2,
                 Description = "ADR = 扫除后累计|Delta| ÷ 扫除那根K的|Delta|。低于此值算干净信号（突破方力气用尽）")]
        public decimal AdrPass { get; set; } = 0.8m;

        // Above this the aggression is still running: treat as real breakout.
        [Display(Name = "ADR作废阈值", GroupName = "3 确认", Order = 3,
                 Description = "ADR 高于此值说明主动量还在持续，判定为真突破并作废。介于达标与作废之间会画出来但标 WEAK")]
        public decimal AdrInvalidate { get; set; } = 1.5m;

        // Passive side / aggressive side volume at one price. High ratio means
        // limit orders absorbed the market orders, i.e. someone defended.
        [Display(Name = "吸收比", GroupName = "3 确认", Order = 4,
                 Description = "同一价位上被动挂单量 ÷ 主动成交量。比值高说明限价单吃掉了市价单，即有人在此护盘")]
        public decimal AbsorptionRatio { get; set; } = 2.0m;

        // Absorption below this size is noise, not a real defender.
        [Display(Name = "吸收最小量(BTC)", GroupName = "3 确认", Order = 5,
                 Description = "单一价位吸收量低于此值视为噪声，不算真正的护盘方")]
        public decimal AbsorptionMinBtc { get; set; } = 5.0m;

        // ================= settings: trade math ========================

        // Stop sits this far beyond the sweep extreme so a retest does not
        // clip it.
        [Display(Name = "止损缓冲(×5分ATR)", GroupName = "4 交易", Order = 1,
                 Description = "止损放在扫除最低/最高点之外这么远，避免回踩时被扫掉")]
        public decimal StopBuffer { get; set; } = 0.3m;

        // Too tight a stop gets taken out by noise.
        [Display(Name = "最小止损距离(%)", GroupName = "4 交易", Order = 2,
                 Description = "止损比这还近就不画信号：太紧会被正常波动打掉")]
        public decimal MinStopPct { get; set; } = 0.15m;

        // Too wide a stop makes position size meaningless.
        [Display(Name = "最大止损距离(%)", GroupName = "4 交易", Order = 3,
                 Description = "止损比这还远就不画信号：太宽会让仓位计算失去意义")]
        public decimal MaxStopPct { get; set; } = 0.8m;

        // Below this reward/risk the trade is drawn but greyed out.
        [Display(Name = "最低盈亏比", GroupName = "4 交易", Order = 4,
                 Description = "盈亏比低于此值仍会画出来，但整组线变灰并标注「RR LOW」")]
        public decimal MinRR { get; set; } = 2.0m;

        // Manual account size, used only for the position size label.
        [Display(Name = "账户权益(USD)", GroupName = "4 交易", Order = 5,
                 Description = "手动填入你的真实账户资金，仅用于计算仓位标签")]
        public decimal AccountEquity { get; set; } = 10000m;

        // Risk per trade as a percentage of equity.
        [Display(Name = "单笔风险(%)", GroupName = "4 交易", Order = 6,
                 Description = "单笔愿承担的风险占权益的百分比。仓位 = 权益×风险% ÷ 止损距离，向下取整到 0.001 BTC")]
        public decimal RiskPct { get; set; } = 1.0m;

        // ================= settings: display ==========================

        [Display(Name = "显示作废标记", GroupName = "5 显示", Order = 1,
                 Description = "在作废的扫除K上画灰色小X，鼠标悬停显示作废原因")]
        public bool ShowInvalidated { get; set; } = true;

        [Display(Name = "显示池线", GroupName = "5 显示", Order = 2,
                 Description = "画出流动性池的价格水平线")]
        public bool ShowPoolLines { get; set; } = true;

        [Display(Name = "信号线延伸K数", GroupName = "5 显示", Order = 3,
                 Description = "入场/止损/目标三条线从确认K向右延伸多少根")]
        public int SignalExtendBars { get; set; } = 20;

        [Display(Name = "BSL颜色(前高/空头止损)", GroupName = "5 显示", Order = 4,
                 Description = "买方流动性池（摆动高点）的线条颜色")]
        public SeriesColor BslColor { get; set; } = MakeColor(255, 242, 56, 90);

        [Display(Name = "SSL颜色(前低/多头止损)", GroupName = "5 显示", Order = 5,
                 Description = "卖方流动性池（摆动低点）的线条颜色")]
        public SeriesColor SslColor { get; set; } = MakeColor(255, 8, 153, 129);

        // Must be a font that actually contains CJK glyphs, see DEFAULT_FONT.
        [Display(Name = "面板字体", GroupName = "5 显示", Order = 6,
                 Description = "画在图上的文字用的字体。必须是含中文字形的字体，否则中文显示为方框（ATAS 绘制层不做字体回退）。本机可用：Microsoft YaHei UI。若换机器后变方框，改成该机装有的中文字体名")]
        public string PanelFont
        {
            get => _fontFamily;
            set
            {
                var v = (value ?? "").Trim();
                if (v.Length == 0) return;
                _fontFamily = v;
                _font = new RenderFont(v, 11f);
                _fontSmall = new RenderFont(v, 10f);
                RedrawChart();
            }
        }

        // ================= settings: sound ============================

        [Display(Name = "预警音(扫除进行中)", GroupName = "6 声音", Order = 1,
                 Description = "阶段一：扫除正在发生。听到后做准备，不要立刻进场")]
        public bool EnableSoundAlert { get; set; } = true;

        [Display(Name = "确认音(可入场)", GroupName = "6 声音", Order = 2,
                 Description = "阶段二：已收回且条件齐备，这才是动手的时刻")]
        public bool EnableSoundConfirm { get; set; } = true;

        [Display(Name = "作废音(别等了)", GroupName = "6 声音", Order = 3,
                 Description = "该次扫除已作废。默认关闭，避免震荡行情里噪音过多")]
        public bool EnableSoundInvalid { get; set; } = false;

        // File name only, no .wav - see the SND_* constants for why.
        [Display(Name = "预警音文件", GroupName = "6 声音", Order = 4,
                 Description = "音效文件名，不要带 .wav 后缀。文件放在 ATAS 安装目录的 Sounds 文件夹里。可填自定义音效名，例如 xishou / gengdan / qifei")]
        public string SoundFileAlert { get; set; } = SND_ALERT;

        [Display(Name = "确认音文件", GroupName = "6 声音", Order = 5,
                 Description = "音效文件名，不带 .wav。建议选一个与预警音明显不同的，确认音是真正该动手的时刻")]
        public string SoundFileConfirm { get; set; } = SND_CONFIRM;

        [Display(Name = "作废音文件", GroupName = "6 声音", Order = 6,
                 Description = "音效文件名，不带 .wav。建议用低沉或短促的音效")]
        public string SoundFileInvalid { get; set; } = SND_INVALID;

        // Needed because ATAS market replay keeps CurrentBar at the end of the
        // whole loaded series, so the right-edge gate can never open there.
        [Display(Name = "历史与回放也播声音", GroupName = "6 声音", Order = 7,
                 Description = "默认关闭：加载历史时不会把过去的事件全部补响。做复盘验证时打开它，回放中的每个事件都会出声（可能连续响，属正常）")]
        public bool SoundOnHistory { get; set; } = false;

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

        private string _lastEvent = "暂无";
        private int _todayConfirmed, _todayInvalidated;
        private DateTime _todayDate = DateTime.MinValue;
        private bool _periodOk = true;
        private string _periodText = "";
        private bool _soundsAllowed;    // set per OnCalculate, see the gating note
        private int _soundLogCount;     // caps the sound diagnostic lines
        // These two must NOT be cleared by ResetAll(), see the gating note.
        private int _maxBarProcessed = -1;
        private bool _firstPassDone;
        private int _totalConfirmed, _totalInvalidated;   // whole series, no day reset

        // OFT.Rendering's RenderFont does NO font fallback: a family without CJK
        // glyphs draws Chinese as tofu boxes. Arial (what AtasBridge and
        // AtasLiquidations use) has no CJK coverage - they only ever drew ASCII,
        // so nobody noticed until this indicator's panel went Chinese. ATAS's
        // own settings dialog renders Chinese fine because that WPF layer does
        // fall back; the custom drawing layer does not.
        //
        // The family is a SETTING rather than a probe. The obvious probe,
        // System.Drawing.Text.InstalledFontCollection, compiles on ATAS X but
        // breaks the ATAS Platform build with
        //   CS0012: IPointer<> is defined in an unreferenced assembly
        //           System.Private.Windows.Core
        // because Platform references the WindowsDesktop copy of
        // System.Drawing.Common, which drags in that internal assembly.
        // Exposing it as a setting avoids the API entirely and lets the user fix
        // it themselves on a machine with different fonts - which matters: this
        // box is a trimmed Windows IoT LTSC image with only 153 fonts, it has
        // "Microsoft YaHei UI" but NOT "Microsoft YaHei" / "SimSun" / "SimHei".
        private const string DEFAULT_FONT = "Microsoft YaHei UI";
        private string _fontFamily = DEFAULT_FONT;
        private RenderFont _font = new(DEFAULT_FONT, 11f);
        private RenderFont _fontSmall = new(DEFAULT_FONT, 10f);

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
            _lastEvent = "暂无";
            _todayConfirmed = _todayInvalidated = 0;
            _todayDate = DateTime.MinValue;
            _logCount = 0;
            _soundsAllowed = false;
            _soundLogCount = 0;
            _totalConfirmed = _totalInvalidated = 0;
            // _maxBarProcessed / _firstPassDone are intentionally preserved.
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

                // BUGFIX 2026-08-12 (v2026.08.12-3): this block used to read
                //     while (_lastClosedProcessed < CurrentBar - 1) ...
                //     if (bar == CurrentBar) RealtimeStageOne(CurrentBar);
                // which was wrong in two ways and produced ZERO signals over a
                // 3 day replay:
                //
                //  (a) During a historical pass ATAS calls OnCalculate for
                //      bar = 0..N while CurrentBar already equals N. So
                //      "bar == CurrentBar" was true only on the very last call,
                //      and stage 1 was therefore evaluated for exactly one bar
                //      of the whole chart. Pools were still built (that is why
                //      the pool lines looked fine) but nothing ever entered
                //      Watch, so stage 2 had nothing to confirm or invalidate.
                //
                //  (b) The while bound used CurrentBar instead of bar, so on the
                //      very first call every bar was closed out at once, and
                //      ClosedStats() - which was also bounded by CurrentBar -
                //      computed its percentiles over the ENTIRE chart including
                //      bars in the future of the bar being evaluated. That is
                //      look-ahead, exactly what the card warned about.
                //
                // Both are fixed by making everything relative to `bar`, never
                // to CurrentBar. Live and replay now share one code path for
                // real: per tick ATAS calls with bar == CurrentBar repeatedly,
                // during history bar simply walks forward.
                while (_lastClosedProcessed < bar - 1)
                    ProcessClosedBar(++_lastClosedProcessed);

                // SOUND GATING - third attempt, this time driven by measurement.
                //
                // -3 used (_historyDone && bar >= CurrentBar), -4 used
                // (bar >= CurrentBar). Both are silent in ATAS market replay,
                // and the diagnostic log from -4 shows exactly why:
                //   sound suppressed (not right edge) evt=alert bar=54 CurrentBar=8539
                // During replay ATAS keeps CurrentBar at the END OF THE WHOLE
                // LOADED SERIES (8539 M5 bars, about 30 days) and re-walks the
                // series for each replay step, so "bar >= CurrentBar" is never
                // true where the events are. 18 suppressed lines = 3 full
                // recalculations x the 6-line diagnostic cap.
                //
                // So the gate can no longer lean on CurrentBar. It now tracks
                // the highest bar this instance has ever calculated:
                //   - initial pass: every bar is new, but _firstPassDone is
                //     still false, so it stays silent (no machine-gunning the
                //     30 days of history that just loaded)
                //   - later re-walks of the same series: bar < _maxBarProcessed
                //     for everything except the last bar, so silent
                //   - a genuinely new bar (live, or a replay step appending
                //     one): bar >= _maxBarProcessed, so it rings
                // _maxBarProcessed and _firstPassDone deliberately survive
                // ResetAll(): a recalculation must not re-arm the history burst.
                //
                // SoundOnHistory bypasses all of it, which is what makes replay
                // validation possible at all.
                _soundsAllowed = SoundOnHistory
                                 || (_firstPassDone && bar >= _maxBarProcessed);
                if (bar > _maxBarProcessed) _maxBarProcessed = bar;

                StageOne(bar);

                if (bar >= CurrentBar) _firstPassDone = true;
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

        // Bounded by evalBar-1, i.e. only bars that closed BEFORE the bar being
        // evaluated. Including the bar itself would leak its outcome into the
        // threshold, and using CurrentBar (as this did before v2026.08.12-3)
        // leaks the whole rest of the chart during a historical pass. Both are
        // look-ahead; the second one is the nastier of the two because it only
        // shows up in replay.
        private bool ClosedStats(int evalBar, out decimal loQ, out decimal hiQ, out decimal volMedian)
        {
            loQ = hiQ = volMedian = 0m;
            int last = evalBar - 1;
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

        // ===================== stage 1: sweep detection ================

        // Runs for the bar currently being calculated - live that is the open
        // bar on every tick, during a historical pass or a replay it walks
        // forward one bar at a time. Same code, same inputs, hence same result.
        private void StageOne(int bar)
        {
            var c = SafeCandle(bar);
            if (c == null) return;
            decimal atr = AtrM5();
            if (atr <= 0m) return;
            if (!ClosedStats(bar, out var loQ, out var hiQ, out var volMed)) return;

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
                _lastEvent = "预警 " + (isLong ? "SSL " : "BSL ") + Fmt(p.Price) +
                             (isLong ? "（多头 setup 形成中）" : "（空头 setup 形成中）");
                Log(_lastEvent + " bar=" + bar + " delta=" + Fmt(c.Delta) + " vol=" + Fmt(c.Volume));
                PlaySound(SoundFileAlert, EnableSoundAlert, p.Id, isLong, bar, "alert",
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
            _totalInvalidated++;
            _lastEvent = "作废 " + ReasonText(reason) + " 池=" + Fmt(p.Price) + " ADR=" + adr.ToString("0.00");
            Log(_lastEvent + " bar=" + bar + " retrigger=" + p.RetriggerCount);
            PlaySound(SoundFileInvalid, EnableSoundInvalid, p.Id, isLong, bar, "invalid",
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
            // "上方最近的摆动高点" is read as "the nearest one that is actually
            // USABLE as a target", i.e. far enough to clear MinRR. Taking the
            // literally nearest pool made most signals grey: 2026-08-12 Sea
            // tightened EqualTolerance, which produced more and denser pools, so
            // the nearest one sat very close to entry and RR collapsed (the same
            // 63801 long went from RR 2.9 to 1.7). Falling back to 3R when no
            // pool qualifies is what the card already specifies for "no usable
            // BSL".
            decimal tp2 = NearestUsableOppositePool(entry, isLong, r);
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
            _totalConfirmed++;

            _lastEvent = (isLong ? "确认 做多 " : "确认 做空 ") + Fmt(entry) +
                         " 盈亏比=" + rr.ToString("0.0") + " ADR=" + adr.ToString("0.00") +
                         (sig.Weak ? " 弱信号" : "") + (sig.RrLow ? " 盈亏比不足" : "");
            Log(_lastEvent + " bar=" + bar + " stop=" + Fmt(stop) + " tp2=" + Fmt(tp2) +
                " size=" + size.ToString("0.000"));
            PlaySound(SoundFileConfirm, EnableSoundConfirm, p.Id, isLong, bar, "confirm",
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
            _totalInvalidated++;
            _lastEvent = "不画信号 " + ReasonText(reason) + " 止损距离=" + stopPct.ToString("0.00") + "%";
            Log(_lastEvent + " bar=" + bar);
        }

        // Nearest opposite-side pool that is at least MinRR away, so it is worth
        // using as TP2. Returns 0 when none qualifies; the caller then falls back
        // to 3R.
        private decimal NearestUsableOppositePool(decimal entry, bool isLong, decimal r)
        {
            if (r <= 0m) return 0m;
            decimal need = MinRR * r;
            decimal best = 0m;
            foreach (var p in _pools)
            {
                if (p.State != PoolState.Active) continue;
                if (isLong)
                {
                    if (p.Kind != PoolKind.Bsl) continue;
                    if (p.Price - entry < need) continue;
                    if (best == 0m || p.Price < best) best = p.Price;
                }
                else
                {
                    if (p.Kind != PoolKind.Ssl) continue;
                    if (entry - p.Price < need) continue;
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
            if (string.IsNullOrWhiteSpace(file)) return;
            // Dedup first so a bar silenced by the right-edge gate below cannot
            // ring later on a recalculation of the same bar.
            string key = poolId + "|" + (isLong ? "L" : "S") + "|" + bar + "|" + evt;
            if (!_soundDedup.Add(key)) return;
            if (!_soundsAllowed)
            {
                if (_soundLogCount < 6)
                {
                    _soundLogCount++;
                    Log("sound suppressed (not right edge) evt=" + evt + " bar=" + bar
                        + " CurrentBar=" + CurrentBar);
                }
                return;
            }
            // Diagnostic for the first few dispatches: if a sound still does not
            // play, this line tells us whether we even got here, which file name
            // was used, and whether the platform has alerts switched on at all.
            if (_soundLogCount < 6)
            {
                _soundLogCount++;
                bool ae;
                try { ae = AlertsEnabled; }
                catch (Exception ex) { LogEx("read AlertsEnabled", ex); ae = false; }
                Log("sound dispatch evt=" + evt + " file=" + file
                    + " bar=" + bar + " AlertsEnabled=" + ae);
            }
            try { AddAlert(file.Trim(), message); }
            catch (Exception ex) { LogEx("AddAlert " + evt, ex); }
        }

        // Shown in the panel and on hover over the grey X, so Chinese.
        private static string ReasonText(InvalidReason r) => r switch
        {
            InvalidReason.Timeout => "超时未收回",
            InvalidReason.TooDeep => "穿透过深(真突破)",
            InvalidReason.AdrHigh => "ADR过高(主动量仍在)",
            InvalidReason.SecondBreak => "二次破位",
            InvalidReason.StopTooClose => "止损过近",
            InvalidReason.StopTooFar => "止损过远",
            _ => "未知"
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

                string label = (s.IsLong ? "做多" : "做空") +
                               " | R=" + s.R.ToString("0.#") +
                               " | 盈亏比 " + s.Rr.ToString("0.0") +
                               " | 仓位 " + s.SizeBtc.ToString("0.000") + " BTC";
                if (s.Weak) label += " | 弱信号";
                if (s.RrLow) label += " | 盈亏比不足";

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
            // The English warning text is fixed by the task card; the Chinese
            // hint is appended rather than replacing it.
            if (!_periodOk)
                lines.Add("SweepMarker requires M5 chart"
                          + (_periodText.Length > 0 ? "  (当前 " + _periodText + ")" : "")
                          + "  请切回 5 分钟图");
            lines.Add("监控中的池：BSL " + bsl + " 条 / SSL " + ssl + " 条");
            lines.Add("最近事件：" + _lastEvent);
            // "今日" counters reset at every day boundary while walking the
            // series, so on a 30 day chart they show the LAST day only - which
            // read as a bug ("最近事件" showed a confirm while 今日 said 0).
            // The whole-series totals are what you actually want when reviewing.
            lines.Add("今日：确认 " + _todayConfirmed + " 个 / 作废 " + _todayInvalidated
                      + " 个　　全图累计：确认 " + _totalConfirmed + " 个 / 作废 " + _totalInvalidated + " 个");

            int w = 0;
            foreach (var l in lines) w = Math.Max(w, ctx.MeasureString(l, _font).Width);
            int lh = ctx.MeasureString("Ag", _font).Height + 2;
            int boxW = w + 12;
            int boxH = lh * lines.Count + 6;

            // Top CENTER, not top left (2026-08-12, Sea: the left corner box was
            // covering price action). Coordinates are absolute canvas
            // coordinates, not panel relative, and ClipBounds starts at (0,0) -
            // AtasLiquidations hit that trap - so anchor on the container region
            // when it is available and centre horizontally inside it.
            int regionX = 0, regionY = 0, regionW = 0;
            try
            {
                var region = Container?.RelativeRegion ?? default;
                if (region.Height > 0)
                {
                    regionX = region.X; regionY = region.Y; regionW = region.Width;
                }
            }
            catch (Exception ex) { LogEx("panel anchor", ex); }
            if (regionW <= 0)
            {
                var clip = ctx.ClipBounds;
                regionX = clip.X; regionY = clip.Y; regionW = clip.Width;
                if (regionW <= 0) { regionX = 0; regionY = 0; regionW = ctx.Size.Width; }
            }

            int boxX = regionX + Math.Max(0, (regionW - boxW) / 2);
            int boxY = regionY + 4;

            ctx.FillRectangle(Color.FromArgb(190, 0, 0, 0), new Rectangle(boxX, boxY, boxW, boxH));
            for (int i = 0; i < lines.Count; i++)
            {
                var col = (!_periodOk && i == 0) ? Color.OrangeRed : Color.Gainsboro;
                int tw = ctx.MeasureString(lines[i], _font).Width;
                // centre each line inside the box so it reads as one block
                ctx.DrawString(lines[i], _font, col,
                               boxX + Math.Max(6, (boxW - tw) / 2), boxY + 3 + i * lh);
            }
        }

#pragma warning restore CA1416
    }
}
