using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using ATAS.Indicators;
using OFT.Rendering.Context;
using Utils.Common.Logging;

// 双平台差异：ValueDataSeries.Color 的类型两边不一样 —— ATAS Platform（WPF
// 渲染）用 System.Windows.Media.Color，ATAS X 用 System.Drawing.Color。
// 用别名隔离，业务代码只写 SeriesColor，不必到处 #if。
// 注意 OnRender 里 RenderContext.DrawString 的颜色**两平台都是**
// System.Drawing.Color（OFT.Rendering 双平台一致），那里照常用 Color。
#if ATAS_PLATFORM
using SeriesColor = System.Windows.Media.Color;
#else
using SeriesColor = System.Drawing.Color;
#endif
using OFT.Coinglass.Models;
using OFT.Coinglass.Providers;
using OFT.Coinglass.Requests;

namespace AtasBridge
{
    // ══ AtasLiquidations（2026-08-09，Phase 7L 收尾）═══════════════════════
    //
    // 副图爆仓柱状图，多头向上、空头向下，与 Coinglass 网页「币种爆仓」同口径。
    // 用来替掉 ATAS 自带的 Aggregated Liquidations —— 那个指标的数据在加载后
    // 就不再更新（它依赖的逐笔实时订阅通道是坏的，订阅成功但一条回调都不来），
    // 本指标改为每 60 秒重拉一次聚合历史，所以会随行情持续更新。
    //
    // 取数这套参数是逐条对着官方指标的 IL 抠出来的，几个反直觉的点写在下面
    // 各自的位置上，改动前务必先读注释——尤其 To=DateTime.MaxValue，
    // 传成 UtcNow 会让服务端把当天数据整段截掉（排查了十几轮才定位到）。
    [DisplayName("AtasLiquidations")]
    [Category("Crypto Metrics")]
    [Description("Coinglass 聚合爆仓（多头/空头柱状图），数据每 60 秒刷新")]
    public class AtasLiquidations : Indicator
    {
        // ── 设置项 ────────────────────────────────────────────────────────
        // 档位含义与官方指标一致，但注意两个枚举的**数值顺序不同**，按名字选，
        // 别按下拉里的第几项选：
        //   Local        = 当前工具和交易所
        //   SymbolGlobal = 当前工具（所有交易所）
        //   Global       = 全球（所有符号和交易所）
        [Display(Name = "聚合范围(Local=本交易所/SymbolGlobal=本币种全交易所/Global=全市场)",
                 GroupName = "数据", Order = 1)]
        public LiquidationsAggregationModes Mode { get; set; } = LiquidationsAggregationModes.SymbolGlobal;

        // 实测 CoinglassDatafeedParameters.UpdatePeriodLimit = 1 分钟，
        // 比这更密的请求会被限流返回空，所以下限锁死 60 秒。
        [Display(Name = "刷新间隔(秒,最低60)", GroupName = "数据", Order = 2)]
        public int RefreshSeconds { get; set; } = 60;

        [Display(Name = "多头爆仓颜色", GroupName = "外观", Order = 1)]
        public SeriesColor LongColor
        {
            get => _longs.Color;
            set { _longs.Color = value; RedrawChart(); }
        }

        [Display(Name = "空头爆仓颜色", GroupName = "外观", Order = 2)]
        public SeriesColor ShortColor
        {
            get => _shorts.Color;
            set { _shorts.Color = value; RedrawChart(); }
        }

        // ── 读数位置 ──────────────────────────────────────────────────────
        // OnRender 的坐标是**整块画布的绝对坐标**，不是相对本面板的，而
        // ClipBounds 实测返回 (0,0) 起，帮不上忙。所以默认值只能是估的：
        // 优先用 Container.RelativeRegion 自动定位到本面板左上角，拿不到时
        // 退回画布顶部 + 下面这两个偏移。
        // 开放成设置项，是因为面板高度、是否有其它副图、DPI 都会影响位置 ——
        // 与其我反复猜，不如你直接拖到合适为止（Y 加大 = 往下移）。
        [Display(Name = "读数X偏移", GroupName = "读数", Order = 1)]
        public int LabelOffsetX { get; set; } = 5;

        [Display(Name = "读数Y偏移", GroupName = "读数", Order = 2)]
        public int LabelOffsetY { get; set; } = 34;

        [Display(Name = "显示读数", GroupName = "读数", Order = 3)]
        public bool ShowLabel { get; set; } = true;

        // 官方默认 1。Histogram 是按 K 线宽度画的，这个值调大只会让柱子超出
        // K 线宽度、互相挨上，一般不用动。
        [Display(Name = "柱宽(默认1)", GroupName = "外观", Order = 3)]
        public int BarWidth
        {
            get => _longs.Width;
            set
            {
                if (value < 1) return;
                _longs.Width = value; _shorts.Width = value; RedrawChart();
            }
        }

        // ── 数据系列 ──────────────────────────────────────────────────────
        // 空头存成负值，柱子自然朝下画，跟 Coinglass 网页一个观感。
        // ShowZeroValue=false：绝大多数 K 线没有爆仓，不关掉的话零值会在
        // 中轴连成一条实线，柱子反而看不清。
        // 配置逐项对齐官方 Aggregated Liquidations（反射读出来的），别凭感觉改：
        //  Width=1 并不是"柱子只有 1 像素"——Histogram 本来就按 K 线宽度画，
        //          官方也是 1，柱子照样和 K 线同宽。
        //  UseMinimizedModeIfEnabled=false 是关键：之前设成 true，柱子会退化成
        //          一条细线，跟主图 K 线宽度对不上（Sea 一眼看出来的那个问题）。
        //  ShowCurrentValue=true 让最新值显示在右侧价格轴上，和官方一致。
        private readonly ValueDataSeries _longs = new("liqLongs", "Longs")
        {
            VisualType       = VisualMode.Histogram,
            Color            = SeriesColor.FromArgb(255, 8, 153, 129),
            Width            = 1,
            ShowZeroValue    = false,
            ShowCurrentValue = true,
            ScaleIt          = true,
            Digits           = 0,
            UseMinimizedModeIfEnabled = false,
        };

        private readonly ValueDataSeries _shorts = new("liqShorts", "Shorts")
        {
            VisualType       = VisualMode.Histogram,
            Color            = SeriesColor.FromArgb(255, 242, 56, 90),
            Width            = 1,
            ShowZeroValue    = false,
            ShowCurrentValue = true,
            ScaleIt          = true,
            Digits           = 0,
            UseMinimizedModeIfEnabled = false,
        };

        // ── Coinglass 接入状态 ────────────────────────────────────────────
        private ICoinglassAggregatedLiquidationsProvider? _prov;
        private readonly ConcurrentQueue<AggregatedLiquidations> _queue = new();

        // 0=未开始 1=初始化中 2=已就绪 3=不可用
        private int    _initState = 0;
        private string _status    = "...";
        private string _symbol    = "";
        private string _exchange  = "";

        // 按 K 线索引保存，OnCalculate 里回填到 series —— ATAS 在重算时会清空
        // series，只写 series 不留底的话，一次重算就全没了。
        private readonly Dictionary<int, decimal> _barLongs  = new();
        private readonly Dictionary<int, decimal> _barShorts = new();

        private DateTime _lastRefreshUtc = DateTime.MinValue;
        private bool     _refreshInFlight = false;
        private bool     _fullPulled      = false;
        private int      _failCount       = 0;
        private int      _logCount        = 0;
        private const int LOG_MAX = 20;

        private static readonly OFT.Rendering.Tools.RenderFont _statusFont = new("Arial", 11f);

        public AtasLiquidations() : base(true)
        {
            // 基类默认建了一个输出 series，直接换成多头那条，再挂上空头那条。
            DataSeries[0] = _longs;
            DataSeries.Add(_shorts);

            // 默认落在独立副图，而不是叠加到主图 K 线上。
            // 注意：这只影响**新添加**的实例；已经加到图表上的那个，面板选择
            // 已随模板存下来了，要改得删掉重新添加。
            Panel = IndicatorDataProvider.NewPanel;

            // 绘制层用 Final，**不要用 Historical**。
            //
            // 官方 AggregatedLiquidations 订阅的确实是 Historical(2)，但照搬过来
            // 会出事：订阅 Historical 等于接管历史层的绘制，series 的柱子会被
            // 整片顶掉（-27 实测，副图直接空白）。官方能这么写，是因为它的
            // OnRender 自己把柱子也画了；我们这里只想叠加一行读数，柱子仍交给
            // ValueDataSeries 自动绘制，所以要用 Final —— 最终叠加层，不影响
            // series 本身的绘制。
            //
            // 顺带纠正一个此前的误判：-24 用 Final 时读数没出现，当时归因为
            // "Final 不触发 OnRender"，其实多半是因为那版读数画在 y=3，被 ATAS
            // 自己画的指标名那行盖住了（y=20 是 -25 才改的，而 -25 又同时把订阅
            // 删了）。两处改动撞在一起，把本来可用的组合误判成不可用。
            EnableCustomDrawing = true;
            SubscribeToDrawingEvents(DrawingLayouts.Final);
        }

        // 数据取不到时把原因显示在面板上，而不是让用户对着一个空面板猜：
        //   N/A   = 平台没注册 Coinglass 服务（授权不含 Crypto Metrics）
        //   NOSUP = 这个币种不在 Coinglass 支持列表里
        //   NOSYM = 还没拿到合约代码（通常是刚加载，稍等即可）
        //   ERR   = 请求异常，详情看 ATAS 日志里的 [AtasLiquidations]
        // 正常工作时什么都不画，不干扰看图。
        // OnRender 里任何一句抛异常都会让读数整块消失，而 catch{} 会把原因一起
        // 吞掉（这个坑在爆仓诊断日志上已经踩过一次）。这里记前几次的结果：
        // 完全没有这条日志 = OnRender 根本没被调用；有 EX = 绘制本身出错。
        private int _renderLogCount = 0;
        private DateTime _lastRenderLogUtc = DateTime.MinValue;

        protected override void OnRender(RenderContext context, DrawingLayouts layout)
        {
            try
            {
                if (_initState == 3)
                {
                    context.DrawString($"AtasLiquidations: {_status}", _statusFont, Color.OrangeRed, 5, 5);
                    return;
                }

                // 面板左上角读数，跟 Coinglass 一个用法：鼠标悬在哪根 K 上就显示
                // 那根的多空爆仓额，鼠标不在图上时显示最新那根。
                // （series 自带的 tooltip 一次只能显示一条，看不到多空对照。）
                int bar = -1;
                bool hovering = false;
                try
                {
                    var m = MouseLocationInfo;
                    if (m is not null && !m.IsMouseLeave && m.BarBelowMouse >= 0 && m.BarBelowMouse <= CurrentBar)
                    {
                        bar = m.BarBelowMouse;
                        hovering = true;
                    }
                }
                catch { }

                // 没悬停时不能直接取 CurrentBar：这条链路有几分钟滞后，当前这根
                // K 通常还没数据，读数会恒显示 "0 0"（Sea 反馈的问题）。改为
                // 往回找最近一根有值的 K，并在数值后标出它的时间，免得把旧值
                // 误当成此刻的值。
                if (!hovering)
                {
                    bar = CurrentBar;
                    for (int i = CurrentBar; i >= 0 && i > CurrentBar - 500; i--)
                    {
                        bool hasL = _barLongs.TryGetValue(i, out var vl)  && vl != 0m;
                        bool hasS = _barShorts.TryGetValue(i, out var vs) && vs != 0m;
                        if (hasL || hasS) { bar = i; break; }
                    }
                }

                _barLongs.TryGetValue(bar, out var l);
                _barShorts.TryGetValue(bar, out var s);   // 已经是负值

                var lText = Fmt(l);
                var sText = Fmt(s);

                // 显示的不是当前这根 K 时，把该 K 的时间跟出来（图表时区）
                string barTime = "";
                if (!hovering && bar != CurrentBar)
                {
                    try
                    {
                        var c = GetCandle(bar);
                        if (c is not null)
                            barTime = " " + c.Time.AddHours(InstrumentInfo?.TimeZone ?? 0).ToString("HH:mm");
                    }
                    catch { }
                }

                // y 往下让一行：面板左上角那行是 ATAS 自己画的指标名
                // （"AtasLiquidations (Bars, True)"），压上去会糊成一团。
                // ⚠️ 坐标是**画布绝对坐标**，不是相对本面板的。副图指标绘制时
                // 会被裁剪到 ClipBounds（本面板区域）内，直接写 (5,20) 会落到
                // 主图顶部、被整个裁掉 —— 日志显示 "OnRender ok ... at(5,20)"
                // 却什么都看不见，就是这么来的。所以要以 ClipBounds 左上角为基准。
                // （AtasBridge 那边直接用小坐标没问题，因为它本来就在主图。）
                if (!ShowLabel) return;

                // 基准：优先取本指标容器的区域左上角（副图面板），拿不到就退回
                // ClipBounds。实测 ClipBounds 是 (0,0) 起、等于画布原点，所以
                // 单靠它读数会画到主图左上角去 —— Sea 截图里那行
                // "1.106M -1.692M" 出现在主图顶部就是这个原因。
                var clip = context.ClipBounds;
                int baseX = clip.X, baseY = clip.Y;
                try
                {
                    var region = Container?.RelativeRegion ?? default;
                    if (region.Height > 0) { baseX = region.X; baseY = region.Y; }
                }
                catch { }

                int x = baseX + LabelOffsetX, y = baseY + LabelOffsetY;
                var lc = ToDrawing(_longs.Color);
                var sc = ToDrawing(_shorts.Color);

                var sFull = sText + barTime;
                var lSize = context.MeasureString(lText, _statusFont);
                var sSize = context.MeasureString(sFull, _statusFont);

                // 垫一层半透明黑底：面板里有网格线和柱子，纯文字（尤其值为
                // "0 0" 时只有两个字符）很容易被当成背景的一部分看漏。
                context.FillRectangle(
                    Color.FromArgb(190, 0, 0, 0),
                    new Rectangle(x - 4, y - 3,
                                  lSize.Width + sSize.Width + 18,
                                  Math.Max(lSize.Height, sSize.Height) + 6));

                context.DrawString(lText, _statusFont, lc, x, y);
                context.DrawString(sFull, _statusFont, sc, x + lSize.Width + 10, y);

                // 每 10 秒记一条，而不是"只记前 N 次"。OnRender 每帧都调，
                // 只记前几次采到的全是启动瞬间（数据还没到，值恒为 0），
                // 反映不了稳定后的状态 —— 上一轮就是被这个采样方式误导的。
                if (_renderLogCount < 20 && (DateTime.UtcNow - _lastRenderLogUtc).TotalSeconds >= 10)
                {
                    _renderLogCount++;
                    _lastRenderLogUtc = DateTime.UtcNow;
                    string reg = "n/a";
                    try { reg = (Container?.RelativeRegion ?? default).ToString(); } catch { }
                    Log($"OnRender ok: bar={bar} l={l} s={s} text=\"{lText} {sText}\" " +
                        $"at({x},{y}) clip={clip} region={reg} size={context.Size} layout={layout}");
                }
            }
            catch (Exception ex)
            {
                if (_renderLogCount < 3)
                {
                    _renderLogCount++;
                    Log($"OnRender EX {ex.GetType().Name}: {ex.Message}");
                }
            }
        }

        // 数量级缩写，跟 Coinglass 的读数写法一致（1.067M / -1.707M）
        private static string Fmt(decimal v)
        {
            var a = Math.Abs(v);
            if (a >= 1_000_000m) return (v / 1_000_000m).ToString("0.###") + "M";
            if (a >= 1_000m)     return (v / 1_000m).ToString("0.###") + "K";
            return v.ToString("0.##");
        }

        // SeriesColor（两平台类型不同）→ RenderContext 用的 System.Drawing.Color。
        // 两边都有 A/R/G/B，所以这一句不需要 #if 分支。
        private static Color ToDrawing(SeriesColor c) => Color.FromArgb(c.A, c.R, c.G, c.B);

        protected override void OnCalculate(int bar, decimal value)
        {
            EnsureInit();
            MaybeRefresh();
            Drain();

            // 回填当前 bar（重算时 series 被清空，靠字典恢复）
            if (_barLongs.TryGetValue(bar, out var l))  _longs[bar]  = l;
            if (_barShorts.TryGetValue(bar, out var s)) _shorts[bar] = s;
        }

        protected override void OnDispose()
        {
            _prov = null;
            base.OnDispose();
        }

        private void EnsureInit()
        {
            if (_initState != 0) return;
            _initState = 1;

            if (!TryGetService(out _prov))
            {
                // 平台没注册 Coinglass 服务（授权不含 Crypto Metrics 时会这样）
                _initState = 3;
                _status    = "N/A";
                return;
            }

            try
            {
                _symbol   = InstrumentInfo?.Instrument ?? "";
                _exchange = InstrumentInfo?.Exchange   ?? "";
            }
            catch { }

            if (string.IsNullOrEmpty(_symbol)) { _initState = 3; _status = "NOSYM"; return; }

            _ = InitAsync();
        }

        private async Task InitAsync()
        {
            try
            {
                // SupportedInstruments 里的元素是 "SYMBOL@EXCHANGE" 复合格式
                // （实际值形如 BTCUSDT@BinanceFutures），不是裸 symbol。
                var pars = await _prov!.GetFeedParametersAsync(CancellationToken.None).ConfigureAwait(false);
                var key  = $"{_symbol}@{_exchange}";

                if (pars?.SupportedInstruments is not null && !pars.SupportedInstruments.Contains(key))
                {
                    _initState = 3;
                    _status    = "NOSUP";
                    Log($"\"{key}\" not in SupportedInstruments. sample: " +
                        string.Join(", ", pars.SupportedInstruments.Take(10)));
                    return;
                }

                _initState = 2;
                _status    = "OK";
            }
            catch (Exception ex)
            {
                _initState = 3;
                _status    = "ERR";
                Log($"init failed: {ex.Message}");
            }
        }

        private void MaybeRefresh()
        {
            if (_initState != 2 || _prov is null || _refreshInFlight) return;
            if (_failCount >= 5) return;   // 持续失败就停手，别每分钟撞一次

            int period = Math.Max(60, RefreshSeconds);
            if ((DateTime.UtcNow - _lastRefreshUtc).TotalSeconds < period) return;

            _lastRefreshUtc = DateTime.UtcNow;
            _ = RefreshAsync();
        }

        private async Task RefreshAsync()
        {
            _refreshInFlight = true;
            try
            {
                // 三个参数都反直觉，逐条说明：
                //  From : 首轮取图表第一根 K 的时间（把历史一次补齐），之后用
                //         24 小时滚动窗口 —— 窗口太小服务端会返回空，太大则
                //         每轮白解析上万条。
                //  To   : **必须是 DateTime.MaxValue**。传 UtcNow 的话服务端会
                //         把它当"截止到某个已完成边界"，当天数据整段被截掉，
                //         表现为"这接口只有昨天的数据"。
                //  Timeframe: 取自 ChartInfo，跟着图表周期走。
                var from = _fullPulled
                    ? DateTime.UtcNow.AddHours(-24)
                    : (GetCandle(0)?.Time ?? DateTime.UtcNow.AddHours(-24));

                var res = await _prov!.GetHistoryAsync(new AggregatedLiquidationsCoinglassRequest
                {
                    Symbol = _symbol, Exchange = _exchange,
                    From = from, To = DateTime.MaxValue,
                    Timeframe = ChartPeriod(),
                    AggregationMode = Mode
                }, CancellationToken.None).ConfigureAwait(false);

                var list = res?.Aggregations;
                int cnt  = list?.Count ?? -1;

                if (cnt > 0)
                {
                    foreach (var a in list!)
                        if (a is not null) _queue.Enqueue(a);
                    _fullPulled = true;
                    _failCount  = 0;
                }
                else _failCount++;

                if (_logCount < LOG_MAX)
                {
                    _logCount++;
                    Log($"refresh #{_logCount}: got={cnt} " +
                        $"range={(cnt > 0 ? $"{list![0].Time:MM-dd HH:mm}..{list[cnt - 1].Time:MM-dd HH:mm}" : "-")} " +
                        $"mode={Mode} tf={ChartPeriod()} from={from:MM-dd HH:mm} to=MaxValue " +
                        $"utcNow={DateTime.UtcNow:MM-dd HH:mm:ss}");
                }
            }
            catch (Exception ex)
            {
                _fullPulled = false;   // 下轮退回全量窗口
                _failCount++;
                if (_logCount < LOG_MAX) { _logCount++; Log($"refresh failed: {ex.Message}"); }
            }
            finally { _refreshInFlight = false; }
        }

        private void Drain()
        {
            while (_queue.TryDequeue(out var a))
            {
                var utc = DateTime.SpecifyKind(a.Time, DateTimeKind.Utc);
                int b   = FindBarContaining(utc);
                if (b < 0) continue;

                // 覆盖而非累加：同一段时间被反复拉取是常态，累加会越滚越大
                _barLongs[b]  = a.Longs;
                _barShorts[b] = -a.Shorts;   // 负值 → 柱子朝下

                try
                {
                    _longs[b]  = a.Longs;
                    _shorts[b] = -a.Shorts;
                }
                catch { /* bar 越界，等 OnCalculate 回填 */ }
            }

            if (_barLongs.Count > 20000)  Trim(_barLongs);
            if (_barShorts.Count > 20000) Trim(_barShorts);
        }

        private void Trim(Dictionary<int, decimal> d)
        {
            int keep = CurrentBar - 5000;
            foreach (var k in d.Keys.Where(k => k < keep).ToList()) d.Remove(k);
        }

        // 找"这个时间落在哪根 K 里"，即最后一根**开盘时间** <= target 的 K。
        // 注意用 Time（开盘）而不是 LastTime（收盘）：聚合值的时间戳是那根 K 的
        // 起始时间，拿去跟收盘时间比会整体偏到前一根。
        private int FindBarContaining(DateTime targetUtc)
        {
            int hi = CurrentBar;
            if (hi < 0) return -1;
            int lo = 0, result = -1;
            while (lo <= hi)
            {
                int mid = (lo + hi) / 2;
                IndicatorCandle? c = null;
                try { c = GetCandle(mid); } catch { hi = mid - 1; continue; }
                if (c is null) { hi = mid - 1; continue; }

                DateTime t;
                try { t = DateTime.SpecifyKind(c.Time, DateTimeKind.Utc); } catch { hi = mid - 1; continue; }

                if (t <= targetUtc) { result = mid; lo = mid + 1; }
                else hi = mid - 1;
            }
            return result;
        }

        private TimeSpan ChartPeriod()
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
            return TimeSpan.FromMinutes(5);
        }

        private void Log(string msg)
        {
            try { LoggerHelper.LogInfo(this, "{0}", new object[] { "[AtasLiquidations] " + msg }); }
            catch { }
        }
    }
}
