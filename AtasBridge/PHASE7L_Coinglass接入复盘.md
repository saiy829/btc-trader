# Phase 7L：Coinglass 三路数据接入复盘（2026-08-09）

一天之内从 `v2026.08.09-1` 迭代到 `-33`。这份文档记录**走过的路**——包括绕的
弯路和错误判断，按版本流水的细节见 `CHANGELOG_AtasBridge.md`。

---

## 一、起点

Sea 的问题：

> ATAS 自带三个指标——多空比、OI（币安 BTCUSDT）、聚合 liquidations（Coinglass
> 数据）。我们做的 AtasBridge 能否**不打开这三个指标**，直接调用它的数据？

## 二、结论（先说结果）

**能，而且全部做到了。**

| 数据 | 状态 | 实现方式 |
|---|---|---|
| Crypto Open Interest | ✅ 与官方指标逐位一致 | 订阅为主 + 60 秒历史轮询兜底 |
| Long/Short Ratio | ✅ 同上 | 同上 |
| Aggregated Liquidations | ✅ 与官方指标逐位一致 | 60 秒历史轮询（实时订阅通道是坏的） |

另外新增独立副图指标 `AtasLiquidations`，可直接替掉官方那个——**而且比官方强**：
官方指标加载后数据就不再更新（它依赖的实时订阅通道失效），我们持续刷新。

与 Coinglass 网页存在约 3.7% 的差异，已定性为 **ATAS 数据源层面的差异**：我们
与 ATAS 官方指标完全一致，说明取数、档位、K 线归属都正确，差异不在我们这一层。

---

## 三、核心机制

三个官方指标本身**不持有数据**，只是从平台 DI 容器取 Coinglass Provider 单例：

| 官方指标 | 实际服务 |
|---|---|
| Crypto Open Interest | `TryGetService<ICoinglassOIProvider>` |
| Long/Short Ratio | `TryGetService<ICoinglassLSRatioProvider>` |
| Aggregated Liquidations | `TryGetService<ICoinglassAggregatedLiquidationsProvider>` + `TryGetService<ICoinglassLiquidationOrdersProvider>` |

解析链：`ExtendedIndicator.TryGetService<T>()` → `DataProvider.GetService<T>()`
→ `IIndicatorServiceProvider.GetService<T>()`。

`TryGetService` 是 `protected`，任何继承 `Indicator` 的自定义指标都能调用，
**所以那三个指标不需要挂在图表上**。Provider 是容器单例，即使官方指标同时开着
也是复用同一条连接，不额外占 Coinglass 配额。

双平台（ATAS X 8.0.14.646 / ATAS Platform 8.0.14.297）的 `OFT.Coinglass` 接口
逐项一致，共用同一份源码。

---

## 四、Coinglass 接口的真实行为（全是实测，没有一条来自文档）

这部分是今天最有价值的产出。**改动取数逻辑前务必先读这一节。**

### 1. `To` 必须是 `DateTime.MaxValue`

最大的坑，害我们绕了十几轮。传 `DateTime.UtcNow`（看起来天经地义）会让服务端
把它当"截止到某个已完成边界"，**当天数据整段被截掉**。

表现出来是三种看似无关的故障，一度被误判成"三条通道全废"：

- 窗口小于一天 → 返回空
- 聚合接口"只有昨天的数据"（误判为 T+1 接口）
- 逐笔历史接口 500

改成 `MaxValue` 后全部消失。

### 2. 三条通道的实际可用性

| 通道 | 状态 |
|---|---|
| 逐笔实时订阅 `Subscribe` | **不推送**，订阅成功但一条回调都不来。官方指标同病 |
| 聚合历史 `AggregatedLiquidationsProvider.GetHistoryAsync` | ✅ 可用，当前主数据源 |
| 逐笔历史 `LiquidationOrdersProvider.GetHistoryAsync` | 500 InternalServerError（三个档位都试过） |

OI / 多空比的订阅通道**是可用的**，但会偶发静默（曾卡死在旧值上，角标年龄一路
涨到 4 分钟），所以加了轮询兜底 + 看门狗。

### 3. 其它参数细节

- `SupportedInstruments` 的元素是 **`SYMBOL@EXCHANGE`** 复合格式
  （`BTCUSDT@BinanceFutures`），不是裸 symbol
- `UpdatePeriodLimit = 1 分钟`，刷新间隔低于 60 秒会被限流返回空
- `Timeframe` 用 `Extensions.GetTimeFrameTypeChartPeriod(ChartInfo)`，跟随图表周期
- `From` 首轮用 `GetCandle(0).Time`（全量补齐），之后 24 小时滚动窗口
- 聚合值的 `Time` 是那根 K 的**起始**时间 → 归属必须比 `candle.Time`（开盘），
  比 `LastTime`（收盘）会整体偏一根
- 数据本身有几分钟延迟，最新一两根 K 偏小是正常的

### 4. 两个枚举的数值顺序不一样（容易选错档）

| | Local | SymbolGlobal | Global |
|---|---|---|---|
| 官方 UI 的 `LiquidationTypes`（下拉顺序） | 0 | **1** | 2 |
| Coinglass 的 `LiquidationsAggregationModes` | 0 | 2 | **1** |

中文对照：`Local`=当前工具和交易所、`SymbolGlobal`=当前工具（所有交易所）、
`Global`=全球。**按名字选，别按下拉里的第几项选。**

`LiquidationOrderSides` 的取值是 `None/Longs/Shorts`，不是常见的 `Buy/Sell`。

---

## 五、绕过的弯路与教训

按时间顺序，每条都对应一次或多次返工。

### 1. `To=UtcNow`：一个参数错误伪装成三个独立故障

`-4` 到 `-12` 期间，我先后归因到时区口径、聚合档位、窗口大小、服务端缓存、
接口滞后，甚至在 `-10` 下了"ATAS 侧三条通道全废、建议放弃"的结论。

**当时反例已经摆在眼前**：官方指标拿得到当天数据，我们拿不到。正确动作是立刻
反编译比对参数，而不是接受"平台没这能力"。是 Sea 追问"新开图表它就能拿到最新
数据，是怎么实现的"才把方向掰回来。

> **出现反例时，先怀疑自己的用法，别急着给平台定罪。**

### 2. 凭直觉试参数，而不是读官方 IL

有官方实现可反编译比对的情况下，我在这些地方靠猜：

- `Buy/Sell` vs `None/Longs/Shorts`（编译报错才发现）
- `DrawingLayouts.Final` vs `Historical`（试错三个版本）
- 时区、聚合档位、窗口大小（多轮）

> **有现成实现能比对时，先读 IL。** 反编译一次的成本远低于一轮"改-部署-重启-观察"。

### 3. `catch{}` 吞掉诊断信息（同一个错误犯了两次）

- `agg LAST` 诊断日志：整条裹在一个 `try` 里，中间 `GetCandle` 抛异常就把整行
  吞掉，日志里一条都没有，白等一轮
- `OnRender` 读数：同样的结构，画不出来且原因不明

> **诊断代码本身必须比被诊断的代码更健壮。** 核心字段先拼好，可能抛异常的调用
> 单独包 try 并降级成标记。

### 4. `LiquidationHourSum()` 里的一个 `break`

`-15` 引入整点分桶时写下：

```csharp
try { c = GetCandle(i); } catch { break; }   // 从 i = CurrentBar 开始
```

`GetCandle(CurrentBar)` 会抛 `ArgumentOutOfRangeException`，于是**第一次调用就
退出循环，函数恒返回 0/0**。

之后 `-16` 到 `-20` 连续五个版本，我都在数据链路上找原因（时区、档位、窗口、
缓存、滞后），**没有一次怀疑过展示端的求和函数**。角标显示 `0/0`，我默认了
"0 是算出来的结果"，而它其实是"异常退出的默认值"。

> **数据落地与数据展示是两个独立环节，都可能坏，不能只查一头。**
> 后来把 `hourBars`（字典原始内容）和 `LIQ1h`（求和结果）一起打日志，一行定位。

### 5. "只记前 N 次"对高频函数是错误的采样

`OnRender` 每帧调用，日志只记前 3 次 → 采到的全是启动瞬间（数据还没到，值恒为
0），完全反映不了稳定后的状态，据此又误判了一轮。改成按时间节流（每 10 秒一条）。

### 6. 主图坐标写法照搬到副图

`RenderContext` 的坐标是**画布绝对坐标**，不是相对本面板。AtasBridge 在主图直接
用 `(5,20)` 没问题，照搬到副图就画到主图顶部去了——日志显示"画了"，屏幕上却
看不见。`ClipBounds` 实测返回 `(0,0)` 帮不上忙，最终用 `Container.RelativeRegion`
并把偏移开放成设置项。

### 7. 自作主张的"顺带优化"引入回归

`-15` 我主动把爆仓刷新改成 2 小时增量窗口（理由是"每分钟解析两万条太浪费"），
但那条链路刚跑通、行为还没摸清，而且是个已知对参数敏感的接口。结果引入回归，
又花两轮排查。

> **在刚跑通、行为未明的链路上，不要顺手做未被要求的优化。**

### 8. 每版同时改两处，无法归因

`-24` 到 `-28` 五个版本卡在"读数不显示"上，根源是每版同时动了绘制层订阅和绘制
坐标两处。`-24` 用 `Final`+`y=3` 没显示（其实是被标题遮挡），我归因为"Final 不
触发"，于是 `-25` 同时改了 y 坐标又删了订阅，`-27` 又照抄官方的 `Historical`
——直接把柱子整片顶掉。

> **一次只改一个变量。**

### 9. 只读属性设成可写 → 版本号被模板锁死

`VersionInfo` 是普通读写属性，值随图表模板持久化。升级 dll 后设置面板仍显示
保存那一刻的旧版本号（dll 已是 `-31`，面板显示 `-7`）。我据此判断"新版没加载"，
和 Sea 对不上账，白折腾几轮。改成 getter 恒返回常量、setter 丢弃。

### 10. 替换 dll 后必须完全退出 ATAS 进程

只移除/重新添加指标不会重新加载程序集——旧 dll 已在进程内存里。这一条我很晚
才提醒，前面几轮验证很可能都受此影响。

---

## 六、当前实现要点

### AtasBridge（主图）

- `EnsureCryptoInit` / `InitCryptoAsync`：DI 取 provider → `SupportedInstruments`
  校验 → 补历史 → 订阅
- `MaybeRefreshOiLsr`：60 秒轮询 OI/多空比历史（订阅静默时的兜底）
- `MaybeRefreshLiquidations`：60 秒轮询爆仓聚合历史（主数据源）
- `MaybeResubscribeCrypto`：看门狗，10 分钟没数据则退订重订（轮询接管后基本不触发）
- 角标自证：`CG OI 106374@30s LSR 1.21@30s LIQ1h 3578/929176 ~8m/31s (n336+0)`
  - `@xx` = 该路数据距上次更新多久
  - `~xx/yy` = 爆仓数据年龄 / 上次成功刷新距今（**两个分开看**：前者大不代表故障，
    市场安静时它会一直涨；后者必须稳定在 60 秒内）
  - `(n+m)` = 聚合记录累计条数 + 逐笔实时流条数（后者恒为 0，见 §4.2）

### AtasLiquidations（副图，独立文件）

- 双向柱状图，多头向上、空头向下，配置逐项对齐官方（`UseMinimizedModeIfEnabled`
  必须是 `false`，设成 `true` 柱子会退化成细线）
- 悬停读数：鼠标在哪根 K 就显示那根，移开时显示最近有值的那根并标出时间
- 读数位置开放成设置项（X/Y 偏移），因为面板高度、DPI、副图数量都会影响

### 双平台差异

| 差异点 | ATAS X | ATAS Platform |
|---|---|---|
| `ValueDataSeries.Color` | `System.Drawing.Color` | `System.Windows.Media.Color` |

用 `SeriesColor` 类型别名 + `ATAS_PLATFORM` 编译符号隔离。注意 `RenderContext`
的颜色**两平台都是** `System.Drawing.Color`。

引 `PresentationCore` 不能用 `<UseWPF>true</UseWPF>`（会要求 TargetFramework 带
`-windows` 后缀），直接引用 dll 即可。

`AtasBridge.Platform.csproj` 用 `EnableDefaultCompileItems=false`，**新增源文件
必须手动登记 `<Compile Include>`**。

---

## 七、遗留事项

### VPS 落库（未完成）

`BarPayload` 已预留 `cg_oi_close` / `cg_lsr` / `cg_liq_long` / `cg_liq_short`
四个可空字段，「推送Coinglass字段到VPS」开关**默认关闭**。开之前要解决两件事：

1. VPS 侧先加字段（pydantic 未放开 extra 的话会 422，把正常 K 线推送一起搞挂）
2. **时间差**：爆仓数据比 K 线收盘晚几分钟到，照现在的推送时点存进去会系统性
   偏小且永不修正——库里的数字看着像真的，其实是残缺的

建议方案：单独推爆仓快照端点，带 Coinglass 自己的时间戳，每分钟一次，VPS 按
时间序列存、后到的覆盖先到的，不与 K 线收盘时点绑定。

### 与 Coinglass 网页的 3.7% 差异

已定性为数据源差异（我们与 ATAS 官方指标完全一致）。要消除只能绕开 ATAS 直连
Coinglass API，成本远大于收益。若后续观察到差异扩大（>10%）或系统性偏向一边，
再查。

### 诊断开关

`RunLiquidationProbe`（一次性接口诊断）默认已关。`_logCount` / `_renderLogCount`
等诊断日志有上限，不会长期刷屏，但接口行为摸清后可以进一步精简。
