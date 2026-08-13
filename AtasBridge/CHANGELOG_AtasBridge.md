# AtasBridge.dll 变更日志

> 本文件此前不存在（GitHub 仓库根目录 404，本地也未找到），
> 2026-07-06（Phase 7F）新建并补记基线版本 + 本次改动。

## v5.0（2026-07-01，基线，追溯记录）

- 多市场支持：新增 `Exchange`（Unset/Binance/Okx）、`MarketType`（Unset/Spot/Perp）
  设置项，默认值改为 `Unset`（而非悄悄冒充 Binance/Perp），四个图表各自手动
  选一次身份后，`/atas/bar`、`/atas/trade` 推送的 JSON 自动带上这两个字段，
  VPS 侧凭此区分四路数据，不再混算
- OKX 永续合约"张→BTC"换算：`OKX_CONTRACT_TO_BTC = 0.01m`（1张=0.01 BTC，
  OKX BTC-USDT-SWAP 官方合约面值），仅在 `Exchange=Okx && MarketType=Perp`
  时生效，换算在门槛比较之前完成
- 时区修复：`candle.LastTime` / `trade.Time` 的 `DateTimeKind` 是 Unspecified
  但取值其实已经是 UTC，改用 `DateTime.SpecifyKind` 明确声明避免被
  `.ToUniversalTime()` 误判成本地时间导致时间戳偏差8小时
- 大单去重重构：从单变量记录改为 `ConditionalWeakTable` 按每个 trade 对象
  独立追踪，避免不同方向/价位的单子互相覆盖追踪状态导致误判重复推送；
  新增累计轨迹诊断字段（FirstSeenVolume/GrowthSeconds/UpdateCount）

## v5.1（2026-07-06，Phase 7F）

- **新增 AtasBridge 原生吸收检测**：在现有 footprint 数据流（`GetAllPriceLevels()`）
  上直接检测吸收信号，推送到新端点 `/atas/absorption`，取代此前 Absorption
  走 ATAS 内置 Webhook（`/atas/signal`）那条无价格/无数量的通道
  - 判定：对当前（仍在形成的）K线每个价位，主导方量（bid或ask，已按
    `VolumeUnitMultiplier` 换算为BTC口径）≥ `AbsorbMinBtc` 且
    主导方/对手方 ≥ `AbsorbRatio` 时触发；同一根K线同一价位同一方向只
    触发一次（`_absorbSeen` 去重集合，K线切换时清空）
  - bid远大于ask → `bid_absorb`（下方买盘吸收）；ask远大于bid → `ask_absorb`
  - 新增设置项：`Enable Absorption Push`（默认true）、`Absorb Min BTC`
    （默认15.0）、`Absorb Ratio`（默认3.0）
  - OKX 永续换算沿用现有 `OKX_CONTRACT_TO_BTC=0.01`，与 bar/trade 推送
    保持同一套系数（**注意**：7F 任务卡原文写的是"1张=0.001 BTC"，与现有
    已验证代码的 0.01 不一致，经与 Sea 确认后按现有 0.01 为准，任务卡
    数值为笔误）
  - `AbsorptionPayload` 新增字段：timestamp/exchange/market_type/instrument/
    side/price/absorbed_btc/bid_vol/ask_vol/ratio/source（snake_case序列化）
- 版本号统一升至 `AtasBridge/5.1`（Description、所有 Payload 的 Source 字段）
- 新增代码注释使用英文（ASCII），避免历史上 PowerShell 编码损坏中文注释的问题；
  现有中文注释保持不动

## v2026.07.06-1（2026-07-06，Phase 7H 阶段1，侦察版）

> **新版本号规则**：从本版本起，AtasBridge 版本号改为 `v<年.月.日>-<当日第N次发布>`
> （例如本次是 7 月 6 日当天第 1 次发布，即 `v2026.07.06-1`），不再使用
> `v主版本.次版本`（如 v5.1）的编号方式，原因是后者在同一天内多次修订时
> 无法区分先后顺序，也看不出距上次发布过了多久。

- **纯侦察，只加不改**：新增 `ShowIdentityLabel` 设置项（分组"5. Identity
  Recon (Stage1)"，默认 `true`），完全不影响现有 `Exchange`/`MarketType`
  手动设置、`/atas/bar`、`/atas/trade`、`/atas/absorption` 的推送逻辑
- 目的：为任务卡 7H"图表身份自动识别"做前期观察——在写自动解析规则之前，
  先把 ATAS SDK 实际能拿到的原始身份字段全部摊开看一遍，不凭 API 文档假设
  （7F 教训：OKX 换算系数那次任务卡文档与实测不一致）
- 新增 `BuildIdentityDump()` / `BuildIdentityShort()`：读取并汇总
  - `Indicator.Instrument`（已标记 Obsolete，但仍读出用于比对）
  - `InstrumentInfo.Instrument` / `.Exchange` / `.TickSize` / `.TimeZone`
  - `TradingManager.Security`（更丰富，来自 `ATAS.DataFeedsCore`）：
    `Instrument` / `Exchange` / `Code` / `ConnectorId` / `Type`（`SecType`
    枚举：Future/Forex/Stock/Bitcoin/CryptoFutures/Indexes/Option/Cfd）/
    `IsInverseFutures` / `BaseCurrency` / `QuoteCurrency` / `FundingRate` /
    `NextFundingTime` / `Expiration` / `Id` / `SecurityId`
  - 每个字段读取都单独 try/catch，某个属性在当前 ATAS 版本/连接状态下不可用
    时只记录 `<error>`，不影响其余字段和整个指标运行
- 图表左上角绘制角标，格式：
  `RAW: {instrument} | {exchange} | type={SecType} | conn={ConnectorId} | inverse={IsInverseFutures}`
  - **首版实现有误，已修正**：最初用 `Labels["..."] = new DrawingText(...)`
    锚定在"当前可见最左侧K线最高价上方"，这是K线/价格锚定，图表一滚动
    或缩放角标就跟着跑掉，Sea 实测反馈"只有移动K线瞬间截图才能看到"。
    改为 override `OnRender(RenderContext, DrawingLayouts)`——通过反射确认
    这正是 ATAS 内置 `Watermark` 指标（`ATAS.Indicators.Technical.Watermark`）
    本身固定角标的实现方式，该方法定义在 `ExtendedIndicator`（`Indicator`
    的基类），`AtasBridge : Indicator` 天然继承得到，不需要改基类。只在
    `DrawingLayouts.Final`（每帧绘制的HUD层）用 `RenderContext.DrawString`
    在固定像素坐标(8,8)绘制，与K线滚动/缩放完全无关
  - **第二次修正**：换成 `OnRender` 后 Sea 换上新DLL仍完全看不到角标。
    反射对比发现：普通 `Indicator` 的 `EnableCustomDrawing` 默认是
    `false`，而 ATAS 内置 `Watermark` 在自己的构造函数里显式设成
    `true`——这个属性不开，ATAS 根本不会调用 `OnRender`，角标代码本身
    没问题但从来没被执行过。修复：构造函数里加一行
    `EnableCustomDrawing = true;`
- 同时通过 `Utils.Common.Logging.LoggerHelper.LogInfo` 写入 ATAS 日志（完整
  多行字段列表），每个指标实例最多记录3次（应对 `TradingManager.Security`
  在指标刚挂载时可能还未就绪、需要等一两根K线才能取到值的情况），之后不再
  重复写日志，避免日志刷屏
- `AtasBridge.csproj` 新增两个引用：`ATAS.DataFeedsCore`（`Security`类型所在
  程序集）、`Utils.Common`（`SyncDictionary`/`LoggerHelper`所在程序集）——
  此前只引用了 `ATAS.Indicators`，编译时报 `CS0012`（类型定义于未引用的
  程序集），补上这两个引用后解决
- Description 特性同步更新为
  `"...（v2026.07.06-1, Stage1 Identity Recon)"`，方便 Sea 在 ATAS 指标
  列表里确认四个图表都已换上侦察版
- ⚠️ **阶段性交付，禁止先入为主**：本版本只负责"摊开看"，不写任何自动判断
  逻辑。交付后由 Sea 把四个图表（币安现货/永续、OKX现货/永续）依次换上
  这个DLL，各截一张角标图发回；收到四张真实截图、确认字段实际取值之前，
  不进入阶段2（自动解析规则实现）
- 构建前已备份阶段1之前的运行中 DLL 至
  `C:\AtasBridge_backups\AtasBridge_backup_5.1.dll`（7F 时忘记先备份、
  被 `.csproj` 的编译后自动复制目标覆盖过一次运行中DLL，这次改正）

## v2026.07.06-2（2026-07-06，Phase 7H 阶段2，正式构建）

Sea 部署阶段1 DLL 后，四个图表（币安现货/永续、OKX现货/永续）各截了一张
角标图，真实观察值：

| 图表 | `InstrumentInfo.Exchange` | `Security.Type` | `ConnectorId` | `IsInverseFutures` |
|---|---|---|---|---|
| 币安永续 | `BinanceFutures` | CryptoFutures | BTCUSDT | False |
| 币安现货 | `Binance` | Bitcoin | BTCUSDT | False |
| OKX永续 | `OkxPerpFutures` | `null`（取不到） | `null` | `null` |
| OKX现货 | `OkxSpot` | `null`（取不到） | `null` | `null` |

**关键发现**：`TradingManager.Security` 在 OKX 两个图表上是 `null`
（可能是连接建立时序或 OKX 连接器实现差异导致），如果解析规则依赖
`Security.Type`/`ConnectorId`，OKX 两路会永远识别失败。改为只依据
`InstrumentInfo.Exchange` 这一个字符串字段——四个真实值互不相同，足以
唯一区分四种组合，且在全部四个图表上都能稳定取到值。

- **新增 `IdentityMode` 设置**（`Auto`默认 / `Manual`），`TryParseAutoIdentity()`
  只做精确匹配（忽略大小写，不接受子串/前缀），规则：
  ```
  "Binance"        -> Exchange=Binance, MarketType=Spot
  "BinanceFutures" -> Exchange=Binance, MarketType=Perp
  "OkxSpot"        -> Exchange=Okx,     MarketType=Spot
  "OkxPerpFutures" -> Exchange=Okx,     MarketType=Perp
  其他任何字符串    -> 不判定，等同 Unset 路径（角标红色 UNSET + 不猜测）
  ```
- 新增 `ResolveEffectiveIdentity()`：Auto 模式下解析成功即为最终生效身份，
  解析失败则为 Unset；Manual 模式下就是下拉框原值（与7H之前版本完全一致）。
  `VolumeUnitMultiplier`（OKX ×0.01换算触发）与三个推送方法
  （`PostBarAsync`/`PostTradeAsync`/`PostAbsorptionAsync`）的
  `exchange`/`market_type` 字段全部改用这个最终生效身份，不再直接读
  手动下拉框——这样自动识别和OKX换算真正联动，而不是各算各的
- 角标（`OnRender`）从阶段1的原始字段摊开显示，改成运营状态指示：
  - Auto 且解析成功：`{Exchange}|{MarketType} AUTO ✓ 12:55:01`（绿色）
  - Auto 且解析失败：`UNSET (raw identity not recognized)`（红色）
  - Auto 解析结果与手动下拉框冲突：`AUTO Okx|Perp ≠ 手动 Binance|Perp`
    （黄色，数据仍按 Auto 值推送/换算，角标只是提示不一致）
  - Manual 模式：`{Exchange}|{MarketType} MANUAL ✓ 12:55:01`（同样风格，
    行为等同7H之前版本，纯下拉框驱动）
  - `✓`/`✗ x{失败次数}` 反映 `/atas/bar` 最近一次推送成功/失败；首次推送
    完成前显示 `...`，不提前显示误导性的对错状态
- 阶段1的原始字段摊开显示（`BuildIdentityDump`/`this.LogInfo`，每实例
  最多3条）继续保留，作为独立于角标的诊断轨迹，不受本次改动影响
- 版本号：`v2026.07.06-2`（同日第二次构建）
- 本次未改动：`AtasBridge/5.1` 这个 Source 版本字符串（写在每条推送
  payload里，仅作诊断标识，任务卡未要求同步这个字段，维持现状避免
  范围蔓延）

## v2026.07.06-3（2026-07-06，任务卡7I：DLL信号显示层 + 角标改进）

- **新增引擎信号轮询与绘制**：轮询 VPS 已有的 `GET /api/signal/latest`
  （7G预埋，服务器端零改动），只在最终生效身份为 Binance|Perp 的图表上
  实际轮询和绘图（其它三张图即使开关打开也静默不画，只记一次日志说明）
  - `ShowEngineSignals`（默认true）、`SignalPollSeconds`（默认10，代码里
    强制最小5秒，防止设置过小刷爆VPS）
  - `status='open'` 的信号画四条水平线：entry白实线/stop红实线/t1,t2绿
    虚线，用 ATAS 内置 `HorizontalLinesTillTouch`（`LineTillTouch`对象，
    `IsRay=true` 右侧无限延伸）；每条线右端配一个 `Labels` 文字标签
    （`DrawingText`），每帧跟随 `CurrentBar` 更新，标签始终贴着最新K线
  - 图表上方居中显示一行：`ENGINE #12 LONG score+64 (SIM)`
  - 终态信号（stopped/t1_then_stop/t2_hit/expired）：线条和标签变灰，
    标签追加 `[STATUS]` 后缀（如 `[T2_HIT]`），30分钟后自动清除
    （`SIGNAL_TERMINAL_GRACE_MINUTES`常量）
  - 轮询失败（网络/超时/服务器返回`{"status":"error"}`）：不清除已画
    线条，只是不刷新；`/api/signal/latest`返回`{"status":"empty"}`时才
    真正清空
  - `OnDispose()` 覆写，指标卸载时清理全部4条线+4个标签，不留残留
- **角标改进**：
  - `LabelPosition`设置（BottomLeft默认/TopLeft/BottomRight/TopRight），
    取代阶段2硬编码的左上角(8,8)
  - **ASCII修复**：状态字符从 `✓`/`✗`/`≠` 改成 `OK`/`ERR(n)`/`!=`——
    这几个 Unicode 符号在 Sea 的 ATAS 字体下渲染成了"□"方块，任务卡本身
    就是冲着修这个来的；本次连带把冲突提示里的中文"手动"也改成`MANUAL`
    （渲染字符串范围内不留非ASCII字符）
  - Binance|Perp 图表角标追加 `| SIG OK/ERR(n)` 段反映信号轮询状态

### v2026.07.06-3 之后的三轮现场修正（同一任务卡内，Sea实测反馈驱动）

- **v2026.07.06-4**：
  - Sea反馈BottomLeft角标几乎完全看不到——排查是ATAS图表自己的底部
    时间轴/滚动条区域吃掉了显示空间，原来给的8px边距不够。修复：
    底部锚定基础边距加到40px，同时新增 `LabelOffsetX`/`LabelOffsetY`
    两个设置项（默认0），允许手动微调，不再靠猜一个"万能边距"
  - Sea追加问了版本号可见性问题：新增只读 `VersionInfo` 设置项（"1.
    Config"组），角标文字末尾也带上短版本号
  - 单一版本号来源：新增 `AtasBridgeVersion` 静态类，`Tag`/`Desc`
    两个const，`[Description]`特性和角标/设置都引用它，避免多处手改
    版本号导致不一致
  - Sea同时问了多语言切换和DLL自动更新——反射确认ATAS SDK本身没有
    多语言基础设施（`DisplayAttribute.ResourceType`理论上支持但ATAS
    有没有真正用它做设置面板多语言，没有把握，不确定不动手）；自动
    更新建议不做（ATAS的DLL是进程启动时一次性加载，替换文件对运行中
    实例不生效，无论如何都要重启ATAS，"自动下载替换"相对"手动替换"
    省不了多少事，反而多一层执行代码的风险，建议如果要做也是"版本
    检查+提醒"而非自动替换，另开任务卡）
- **v2026.07.06-5**：Sea反馈角标"太长了"（带时间戳+版本号），版本号
  在设置面板能看到就够——去掉角标文字里的 `HH:mm:ss` 时间戳和版本号
  后缀，只保留`{Exchange}|{MarketType} AUTO/MANUAL OK/ERR(n) | SIG
  OK/ERR(n)`这种精简格式

### 双平台构建支持（同一任务卡内，Sea反馈"普通版ATAS导入报错"驱动）

- **发现**：ATAS 软件本身有两个版本共存——ATAS X（Avalonia渲染，
  SDK v8.0.14.644）和普通版 ATAS Platform（WPF渲染，SDK v8.0.14.290）。
  AtasBridge.dll 只在 ATAS X 上编译测试过，Sea 尝试导入普通版 ATAS 时
  报 `ReflectionTypeLoadException`，提示缺 `Avalonia.Base` 程序集
- **根因排查**（通过反射逐项对比两版本SDK的实际类型，不凭猜测）：
  - `Indicator`基类核心API（`EnableCustomDrawing`/`Labels`/
    `HorizontalLinesTillTouch`/`OnRender`签名/`TradingManager.Security`
    字段/`DrawingLayouts`枚举）在两版本间完全一致
  - `RenderContext`/`RenderFont`的`DrawString`/`FillRectangle`/
    `MeasureString`等方法签名也完全一致（都用`System.Drawing.Color`/
    `Rectangle`/`Size`），最初怀疑的"两套完全不同的渲染类型体系"是
    误判——第一次探测环境没带对WPF共享框架依赖，产生了假阳性
  - 真正的两处差异：
    1. `LineTillTouch`构造函数的Pen参数类型：ATAS X用
       `Utils.Common.UniversalPen`，普通版ATAS用标准
       `System.Drawing.Pen`
    2. 普通版ATAS Platform安装目录自带的`System.Drawing.Common.dll`
       是过时的v8.0.0.0，但它自己的`ATAS.Indicators.dll`实际依赖
       v10.0.0.0（运行时从.NET共享框架
       `Microsoft.WindowsDesktop.App`解析，不是用目录里那份）——
       这才是报错信息里出现Avalonia相关字样的真正原因（版本链解析
       失败牵连出的连锁错误，不是真的缺Avalonia）
- **方案**：新增 `AtasBridge.Platform.csproj`，与原 `AtasBridge.csproj`
  共用同一份 `AtasBridge.cs` 源码（`<Compile Include="..\AtasBridge\
  AtasBridge.cs" />`，不复制维护两份），只对上述Pen类型差异用
  `#if ATAS_PLATFORM`/`#else`切换，其余代码完全相同。
  `AtasBridge.Platform.csproj`引用 `D:\Program Files\ATAS Platform\`
  下的程序集（`System.Drawing.Common`例外，改引用
  `C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\10.0.2\
  System.Drawing.Common.dll`避开那份过时文件），编译后自动复制到
  `%APPDATA%\ATAS\Indicators\`（普通版ATAS自己的指标目录，与ATAS X
  的`%APPDATA%\ATAS X\Indicators\`分开，互不干扰）
- Sea验证：普通版ATAS重启后能正常搜到并加载AtasBridge，功能确认可用
- **⚠️ 标准约定（自本次起，长期有效）：AtasBridge.dll 以后每次升级，
  必须同时编译并交付 ATAS X 和 ATAS Platform（普通版）两个构建**，
  确保两边指标目录的版本保持同步，不能只更新其中一个。两个 csproj
  共用同一份`AtasBridge.cs`，只在真正有API差异的地方用`#if
  ATAS_PLATFORM`分支，绝大多数代码改动无需关心平台差异
- 交付版本对照（本次任务卡最终态）：
  - ATAS X: `v2026.07.06-5`，`AtasBridge_backup_v2026.07.06-5_ATASX.dll`
  - ATAS Platform: `v2026.07.06-5`，
    `AtasBridge_backup_v2026.07.06-5_ATASPlatform.dll`
  （备份均存于 `C:\AtasBridge_backups\`）

## v2026.07.11-1（2026-07-11，Phase 7J：面板信号展示 + 图表历史信号标记）

背景：Sea 发现 `engine_signals` 表近7天为空（综合分近7天波动 -16~+33，从未
碰到 ±60 触发线——排查确认是市场行情本身+大户多空比15%权重近一个月一直
卡在评分公式的中性区间共同导致，非bug），顺带问出两个此前一直没做的功能
缺口：信号能不能在 mb.661688.xyz 面板上看，历史信号能不能在图表上留痕。

- **服务器端新增只读端点 `GET /api/signal/history?days=7`**
  （`api/main.py`，纯新增，`/api/signal/latest` 不动）：返回
  `engine_signals` 最近N天全部记录（`{"count":N,"signals":[...]}`），
  为面板和 AtasBridge 图表历史标记共用同一个数据源
- **面板新增信号展示区块**（`web/index.html`，Vue3，独立REST轮询
  `/api/signal/history?days=7`，每30秒一次，不接入现有WebSocket快照）：
  - 当前信号（`status='open'`）：方向/综合分/entry-stop-t1-t2/开仓多久
  - 近7天历史信号：横向滚动条目，方向+综合分+结果徽章（T2命中=绿/
    止损=红/T1后止损=橙/到期=灰）
  - **⚠️ 重要发现（此前文档从未记录）**：`web/index.html` 不是直接被
    servce的——`mb.661688.xyz` 的 nginx 配置（宝塔面板管理，`/www/server/
    panel/vhost/nginx/mb.661688.xyz.conf`）`root` 指向
    `/www/wwwroot/mb.661688.xyz/`，是一份**独立部署的拷贝**，不是
    `/opt/btc-trader/web/` 的软链接。改了仓库里的 `web/index.html` **不会
    自动生效**，必须额外手动
    `cp /opt/btc-trader/web/index.html /www/wwwroot/mb.661688.xyz/index.html`
    才是真正部署（该目录下已有 `index.html.bak.<时间戳>` 系列文件，说明
    这个手动部署+备份的模式此前就存在，只是没写进文档）
- **AtasBridge.dll 图表历史信号标记**：
  - 轮询目标从 `/api/signal/latest` 切到 `/api/signal/history?days=7`
    （一次轮询同时拿到当前信号+历史信号，不用查两个接口）
  - 当前信号（`status='open'`）：沿用 7I 的完整四线显示，行为不变
  - 历史信号（近7天，非open状态）：不画完整四线（避免图表被历史信号
    的线条堆满），只在 entry 价位画一个简化文字标记，如
    `LONG #12 T2 OK`、`SHORT #15 SL`，按结果着色（T2命中=绿/止损=红/
    T1后止损=橙/到期=灰）
  - 时间戳→K线定位：`engine_signals.created_at` 是北京时间字符串，转
    UTC 后用二分查找在 `GetCandle(bar).LastTime`（同样要 SpecifyKind
    修正，见 v5.0 时区注释）里找"最新一根收盘时间<=目标时间"的K线，
    不要求逐tick精确，够定位历史信号大致发生的位置即可
  - 标记生命周期：每次轮询对比新旧信号id集合，不在7天窗口内的旧标记
    自动移除；`OnDispose()` 卸载时清空全部历史标记（新增
    `ClearAllHistoricalMarkers()`，与当前信号的 `ClearSignalDrawing()`
    分开管理——后者在"信号切换/清空/30分钟宽限到期"这些中途场景也会
    调用，不能碰历史标记，否则等于每次当前信号一变，历史全没了）
- 版本号：`v2026.07.11-1`（新的一天，序号重新从1开始，符合
  `v日期-当日序号` 约定）
- 交付版本对照：
  - ATAS X: `AtasBridge_backup_v2026.07.11-1_ATASX.dll`
  - ATAS Platform: `AtasBridge_backup_v2026.07.11-1_ATASPlatform.dll`
  （备份均存于 `C:\AtasBridge_backups\`；本次编译前已提前备份两平台
  升级前的DLL，流程上没有再漏掉这一步）

## v2026.07.11-2（2026-07-11，Phase 7K：数据推送总开关 + 设置面板中文化）

背景：Sea 会在 ATAS X 和普通版 ATAS 两个平台上分别挂载同一批图表，但只想
让其中一个平台真正往 VPS 推数据，另一个平台只用来看角标/引擎信号，不重复
推送；另外设置面板已经有6个分组，希望改成中文方便看。

- **新增数据推送总开关 `EnableDataPush`**（默认true，"2. 推送开关"分组
  第一项）：关闭后 K线/大单/吸收三路推送全部停止（`OnCalculate`里
  `PostBarAsync`/`CheckAbsorption`调用点、`OnCumulativeTrade`/
  `OnUpdateCumulativeTrade`里的大单推送调用点，各自补上
  `EnableDataPush &&`/`!EnableDataPush ||`前置判断），不用像以前那样
  分别关 Enable Bar Push / Enable Trade Push / Enable Absorption Push
  三个开关。**身份角标和引擎信号显示不受这个开关影响**（两者都是只读
  轮询/本地状态展示，本来就不依赖推送开关），Sea 可以在不推送数据的
  那个平台上正常看信号
- **设置面板全部改中文**（24处 `[Display(Name=/GroupName=)]`）：
  6个分组改成"1. 基础配置"/"2. 推送开关"/"3. 大单阈值"/"4. 吸收检测"/
  "5. 身份角标"/"6. 引擎信号"，每个设置项名称同步改中文
  - **不改的部分**：下拉框枚举值本身（`ExchangeName`/`MarketKind`/
    `IdentityMode`/`LabelPosition`）——这些C#标识符会被
    `.ToString().ToLowerInvariant()`读回来拼进推送JSON payload和内部
    逻辑判断（比如`TryParseAutoIdentity`里的字符串匹配），改中文会直接
    破坏功能，不是纯UI问题，所以保留英文
  - **ASCII规则的范围澄清**：7I 定的"渲染字符串必须ASCII"规则，起因是
    自己在图表画布上用 `RenderContext.DrawString` 画的 ✓/✗ 等符号在
    Sea机器上渲染成方块——这是 ATAS 底层绘图API的字体覆盖问题。设置
    面板走的是 ATAS 原生 WPF/Avalonia 界面渲染，跟图表画布是两条完全
    不同的链路，面板本身"关于"/"设置"/"默认模板"这些原生文字已经是
    中文，说明这条链路对中文没有渲染问题。因此本次改动没有违反7I那条
    规则的本意，只是把它的适用范围从"所有非ASCII"精确到"图表画布绘图
    + 日志 + JSON payload"，不含原生设置面板文本
- 版本号：`v2026.07.11-2`
- 交付版本对照：
  - ATAS X: `AtasBridge_backup_v2026.07.11-2_ATASX.dll`
  - ATAS Platform: `AtasBridge_backup_v2026.07.11-2_ATASPlatform.dll`
  （编译前已备份两平台升级前DLL，标记`_pre7K`后缀）

## v2026.07.11-3（2026-07-12，隐藏未使用的默认输出序列）

Sea 问设置面板最下方"绘图"那一块（视觉类型/颜色/线条样式等）是干什么的，
说自己一直没见它画出过任何东西。

- **排查**：反射验证——一个完全没有自定义代码的空白 `Indicator` 子类，
  构造完成后 `DataSeries.Count` 就已经是 1（类型 `ATAS.Indicators.
  ValueDataSeries`）。这是 ATAS SDK 给每个指标自动生成的默认输出序列，
  大多数简单的均线/震荡指标会通过它画线，AtasBridge 是数据桥接+绘图
  工具，代码里从来没有对它赋值过（没有任何`this[bar]=...`这种写法），
  所以它一直是空的，设置面板里能看到但永远不会画出东西——这正是 Sea
  说"一直没看到"的原因，不是配置错了，是这块设置本来就跟 AtasBridge
  的功能无关
- **修复**：构造函数里加一行 `DataSeries[0].IsHidden = true;`，把这个
  未使用的默认序列从设置面板隐藏掉，减少无关干扰。不影响任何现有功能
  （代码里从未读写过它）
- 版本号：`v2026.07.11-3`
- 交付版本对照：
  - ATAS X: `AtasBridge_backup_v2026.07.11-3_ATASX.dll`
  - ATAS Platform: `AtasBridge_backup_v2026.07.11-3_ATASPlatform.dll`

（另外 Sea 同时问到角标偏移 LabelOffsetX/Y 新指标默认显示10/-150，代码
里`LabelOffsetX/Y`的默认值确认就是 0——查了`IndicatorTemplates\AtasBridge\`
目录，是空的，没有保存过模板；grep 了几个候选的图表/工作区存档文件也没
找到明文的 10/-150。ATAS 的图表布局大概率是用二进制/序列化格式持久化每个
指标实例的属性值，不是纯文本、grep 不到，没能定位到具体是哪个文件在起
作用。可以确定的是：不是代码里写死的默认值，最可能是本次会话早前调试
角标位置可见性问题时，某次实测调整后被图表工作区自动记住了。这不是bug，
如果 Sea 想要恢复成0，在设置面板里手动改回0即可）

## v2026.07.11-4（2026-07-12，角标偏移默认值改为10/-150）

Sea 澄清了上一条记录里的问题：不是问"为什么显示10/-150"，是明确要求
把这两个值直接写成代码默认值，因为这个组合在他的机器上实测效果好，
不想每次新加图表都要手动调一遍。

- `LabelOffsetX` 默认值 `0` → `10`
- `LabelOffsetY` 默认值 `0` → `-150`
- 纯改数字，不涉及任何逻辑改动
- 版本号：`v2026.07.11-4`
- 交付版本对照：
  - ATAS X: `AtasBridge_backup_v2026.07.11-4_ATASX.dll`
  - ATAS Platform: `AtasBridge_backup_v2026.07.11-4_ATASPlatform.dll`

## v2026.07.12-1（2026-07-12，身份角标：区分"推送已关闭"与"推送真的失败"）

Sea 报告：某张图表设置全部正确（交易所/市场类型/身份识别模式都对），
但左下角身份角标一直显示橙红色，看起来像报错。排查发现根因是这张图
的"总开关：启用数据推送"（`EnableDataPush`）被有意关闭了（双开ATAS X
和普通版ATAS，只想让一边推数据给VPS，这正是 v2026.07.11-2/Phase 7K
加这个总开关时设计的用途）。

`ComputeIdentityLabel()` 原来的颜色逻辑只有两种状态：`_lastPushOk`
为 true→绿色，false（含"从未推送过"和"推送过但失败"两种情况共用
同一个默认值）→橙红色。总开关关闭时 `PostBarAsync` 从未被调用过，
`_lastPushOk` 永远停留在默认值 `false`，角标因此永远橙红——不是在报
一个真实错误，只是这个"未推送"状态被误显示成"推送失败"的颜色。
PROJECT_CONTEXT.md 里 Phase 7K 的记录"关闭后…身份角标显示不受影响"
其实不准确，颜色是受影响的。

- `ComputeIdentityLabel()` 状态拆成三档：
  - `EnableDataPush=false` → 灰色 `OFF`（总开关关闭，预期状态，不是错误）
  - `EnableDataPush=true` 但还没有任何一次推送尝试 → 灰色 `...`（等待中）
  - 真正尝试过推送 → 绿色 `OK` / 橙红色 `ERR(n)`（不变）
- 只改了颜色/文案判断逻辑，不影响推送本身的行为
- 版本号：`v2026.07.12-1`
- 交付版本对照：
  - ATAS X: `AtasBridge_backup_v2026.07.12-1_ATASX.dll`
  - ATAS Platform: `AtasBridge_backup_v2026.07.12-1_ATASPlatform.dll`

## v2026.08.02-1（2026-08-02，入场箭头 + 最近10条历史信号 + Platform 构建修复）

> Sea 反馈：ATAS X 上已触发的订单流信号 #1，旧显示是在"当前 K(CurrentBar)"
> 处画 entry/stop/t1/t2 横线 + 文字，看不出信号究竟发生在哪一根 K 上。
> 要求：入场点用上下箭头**直接指在对应 K 上**（做多 ↑ 标 K 线下方、做空 ↓
> 标 K 线上方），并保留最近 10 条信号的 entry/stop/t1/t2 在盘面上，一眼看清
> 历史发出的信号。

### 绘制层从"Labels/横线"改为"OnRender 坐标绘制"

- **入场箭头**：在每条信号的入场 K 上画方向三角箭头
  - 做多 LONG → 向上三角，尖端贴在该 K **低点下方**（青色 `#00E5CC`，未结束）
  - 做空 SHORT → 向下三角，尖端贴在该 K **高点上方**（红色 `#FF3D6E`，未结束）
  - 已结束信号按结果着色（复用 `HistOutcomeStyle`）：T2 OK 浅绿、T1>SL 橙、
    SL 橙红、EXP 灰
- **位价短线段**：每条信号在其入场 K 处画 entry/stop/t1/t2 水平短线
  - 未结束(open)：线段从入场 K 延伸到**当前 K**（跟随行情推进）
  - 已结束：线段固定横跨 `HIST_SEG_BARS=6` 根 K，半透明(alpha 130)避免抢视觉
  - entry 白、stop 红、t1/t2 浅绿
- **信号标签**：箭头旁标注 `#id 方向 [结果]`
- **最近 N=10 条**：poll 从 `/api/signal/history?days=30` 取回后按 id 倒序取
  最新 10 条（含当前未结束 + 已结束），整体替换 `_renderSigs`（volatile，
  poll 线程写、OnRender 线程读，引用赋值原子）
- 仅在**币安永续图**上绘制（与轮询同一门槛）；屏幕左上角 `ENGINE #id ...` 头
  与身份角标 `| SIG <status>` 段保持不变，仍由 `_activeSignal` 驱动

### 坐标转换 API（探针 AtasProbe2 确认，两个 SDK 完全一致）

- `ChartInfo.PriceChartContainer.GetXByBar(int bar, bool isStartOfBar)` → X 像素
  （K 中心 X 用 `(GetXByBar(bar) + GetXByBar(bar+1))/2` 求得，不依赖 bool 语义）
- `ChartInfo.PriceChartContainer.GetYByPrice(decimal price, bool)` → Y 像素
- `RenderContext.FillPolygon(Color, System.Drawing.Point[])` → 填充三角箭头
- `RenderContext.DrawLine(RenderPen, x1,y1,x2,y2)` → 位价线段
- `OFT.Rendering` 在 ATAS X / ATAS Platform 两套 SDK 中完全一致，此段绘制代码
  **双平台通用，无 `#if ATAS_PLATFORM` 分支**

### 退役的旧代码

- 删除 `DrawSignalLines` / `UpsertLine` / `SetSignalLabel` / `ClearSignalDrawing`
  / `UpdateHistoricalMarkers` / `ClearAllHistoricalMarkers` /
  `CheckTerminalGraceExpiry` 及相关字段（`_sigLine*`、`SIG_TAG_*`、
  `_histMarkerIds`、`SIG_HIST_PREFIX`、`SIGNAL_TERMINAL_GRACE_MINUTES`）
- 这些是基于 `Labels`/`HorizontalLinesTillTouch` 的旧显示，已被 OnRender 绘制
  取代；`OnDispose` 不再需要清理持久绘制对象（OnRender 不留残留）

### Platform 构建修复（顺带）

- `AtasBridge.Platform.csproj` 的 `System.Drawing.Common` HintPath 指向
  WindowsDesktop.App **10.0.2**，该运行时已升级为 **10.0.10**，旧路径失效导致
  `System.Drawing.Common` 解析不到、进而 `DashStyle`/`Color`/`RenderPen` 等
  全部 CS0012。已更新为 10.0.10（运行时再次小版本升级时需同步此处版本号）

### 交付

- 双平台编译 0 错误，已自动部署到各自 Indicators 目录：
  - ATAS X: `%APPDATA%\ATAS X\Indicators\AtasBridge.dll`
  - ATAS Platform: `%APPDATA%\ATAS\Indicators\AtasBridge.dll`
- 源码 + 旧 DLL 备份：`C:\AtasBridge\backup_20260802_212908\`
- 版本号：`v2026.08.02-1`

## v2026.08.02-2（2026-08-02，位价数字标注 + 已结束信号线段精确到结束K）

> 承接 v2026.08.02-1 的两点盘面反馈。

### 位价线段标价格

- `DrawLevelSeg` 在每条线段右端标注 `入/损/T1/T2 + 价格`（小字，跟线同色）；
  标签 X 靠右留 60px 边，避免压住价格轴刻度

### 已结束信号线段精确画到"真正结束的那根K"

- 之前已结束信号的位价线段固定横跨 6 根 K；现改为画到信号真正止盈/止损的
  那根 K，一眼看出这单跑了多久（例：#2 从 21:40 入场画到 21:45 打穿 T2）
- `/api/signal/history` **本就返回** `outcome_at` 字段，无需改 VPS/API
- DLL 侧改动：
  - `SignalItem` 新增 `OutcomeAt`（`outcome_at`，SnakeCaseLower 自动映射）
  - `RenderSig` 新增 `OutcomeUtc`（poll 时用 `TryParseBjTimeToUtc` 解析）
  - `RenderSignalArrows` 的 `xEnd`：open→当前K；已结束且有 outcome_at→
    结束K中心(极速结束时至少保 1 根K宽)；已结束无 outcome_at→回退 HIST_SEG_BARS
- 版本号：`v2026.08.02-2`；双平台编译 0 错误，已部署到各自 Indicators 目录

## v2026.08.02-3（2026-08-02，位价标签中文→ASCII 字形修复）

- v2026.08.02-2 把 entry/stop 标签写成中文"入"/"损"，在 ATAS 绘制字体(Arial)
  下渲染成 □ 方框(无中文字形)——正是 Phase 7I 已记录的同一个坑。
- 改回 ASCII：`E`=入场、`SL`=止损（T1/T2 本就是 ASCII，不受影响）。
- 版本号：`v2026.08.02-3`；双平台编译 0 错误，已部署。

## v2026.08.09-1（2026-08-09，Phase 7L：Coinglass 三路数据直连，免挂官方指标）

背景：Sea 问 ATAS 自带的三个 Crypto Metrics 指标（Crypto Open Interest /
Long-Short Ratio / Aggregated Liquidations）能否不挂到图表上、由 AtasBridge
直接取它们的数据。

### 反编译确认的取数路径（不是猜的）

对 `ATAS.Indicators.Other.dll` 做 IL 级扫描，三个官方指标本身**不持有数据**，
只是从平台 DI 容器取 Coinglass Provider 单例，再自己订阅 + 补历史：

| 官方指标 | 实际服务 |
|---|---|
| Crypto Open Interest | `TryGetService<ICoinglassOIProvider>` |
| Long/Short Ratio | `TryGetService<ICoinglassLSRatioProvider>` |
| Aggregated Liquidations | `TryGetService<ICoinglassAggregatedLiquidationsProvider>` + `TryGetService<ICoinglassLiquidationOrdersProvider>` |

解析链 `ExtendedIndicator.TryGetService<T>()` → `DataProvider.GetService<T>()`
→ `IIndicatorServiceProvider.GetService<T>()`。`TryGetService` 是 protected，
AtasBridge 继承 `Indicator` 直接可用，**所以那三个指标不需要挂在图表上**。
Provider 是容器单例，即使图表上仍开着官方指标也是复用同一条连接，不额外
占用 Coinglass 配额。

### 双平台一致性（先验证再动手）

`OFT.Coinglass` 在 ATAS X 是 8.0.14.646、ATAS Platform 是 8.0.14.297，DLL
大小差一倍多，但四个 Provider 接口 + 全部 Request/Model 类型逐项比对完全
一致 —— 因此双平台共用同一份源码，**无需 `#if ATAS_PLATFORM` 分支**。

### DLL 侧改动

- 新增设置组「7. Crypto指标(Coinglass)」：启用开关（默认开）、清算聚合范围
  （`LiquidationsAggregationModes`，默认 Local，同官方指标出厂值）、
  历史回溯小时数（默认 24）、推送到 VPS 开关（**默认关**，见下）
- `EnsureCryptoInit` / `InitCryptoAsync`：`GetHistoryAsync` 补历史 +
  `Subscribe` 订阅实时。Handler 在网络线程触发，**只入队**，全部解析/K线
  归属都放在 `DrainCryptoQueues`（ATAS 计算线程）里做
- 清算是双 Provider 混合（复刻官方做法）：历史走 AggregatedLiquidations
  （返回值带 `LastOrderId`），实时走 LiquidationOrders 逐笔流，用
  `LastOrderId` 做历史/实时衔接去重，再按 K 线本地累加多空爆仓量
- `LiquidationOrderSides` 实际取值是 `None/Longs/Shorts`（反射确认，不是
  常见的 Buy/Sell —— 初版按 Buy/Sell 写直接编译不过，又一次"别猜 API"）
- `OnDispose` 里 `Unsubscribe`：Provider 是平台单例，不退订会在单例里累积
  死回调
- 角标新增 ` | CG ...` 段，沿用既有"诚实报告"原则区分：`N/A`=平台没注册
  Coinglass 服务（授权不含 Crypto Metrics）、`NOSUP`=该币种不在
  `SupportedInstruments`（同时把服务端支持的前 20 个值写进 ATAS 日志，
  照着对齐即可）、`WAIT`=已订阅但数据未到、`ERR`=请求异常，拿到数据后
  直接显示 OI 和 LSR 实际值

### VPS 推送（默认关闭，需服务端先动）

`BarPayload` 新增四个**可空**字段 `cg_oi_close` / `cg_lsr` / `cg_liq_long` /
`cg_liq_short`，由「推送Coinglass字段到VPS」开关控制，默认关闭。关闭时四个
字段为 null，被 `_serOpts` 的 `WhenWritingNull` 略掉，**推送 JSON 与接入前
一字节不差**。VPS 侧如果用 pydantic 且未放开 extra 字段，多余字段会 422，
所以必须等服务端加好字段再开这个开关。

注意 `cg_oi_close` 与既有 `max_oi`/`min_oi` **不是一回事**：后者是 ATAS 从
交易所原生行情拿的 K 线内 OI 极值，前者是 Coinglass 口径，不要混用。

### 构建

- `AtasBridge.Platform.csproj` 此前在 C:\AtasBridge 下已丢失，按本文件
  v2026.07.06-5 记录的做法在 `C:\AtasBridge\Platform\` 重建（共用
  `..\AtasBridge.cs`，`EnableDefaultCompileItems=false`），并补上
  `OFT.Coinglass` 引用；`System.Drawing.Common` 仍走 dotnet shared
  （路径更新为现存的 `10.0.10`，原记录的 10.0.2 已不在）
- 两个 csproj 均新增 `OFT.Coinglass` 引用（`Private=false`）
- 版本号：`v2026.08.09-1`；双平台编译 0 错误，已部署到各自 Indicators 目录
  （注：`%APPDATA%\ATAS` 实为指向 `D:\bak\ATAS_Data` 的符号链接，Platform
  构建即直接落到实际使用的指标目录）
- **待 Sea 在图表上验证**：角标 CG 段显示什么。这一步不能靠代码自证——
  Coinglass 认的 symbol/exchange 字面值是否等于 ATAS 连接器里的写法，
  只有真实跑起来才知道，对不上会报 NOSUP 并在日志里给出支持列表

## v2026.08.09-2（2026-08-09，Phase 7L 修复：SupportedInstruments 是复合 key）

Sea 实测 v2026.08.09-1：币安永续图表角标报 `CG NOSUP`，无数据。

- **原因**：`CoinglassDatafeedParameters.SupportedInstruments` 里的元素不是
  裸 symbol，而是 `"SYMBOL@EXCHANGE"` 复合格式。日志里服务端返回的实际值：
  `BTCUSDT@BinanceFutures`、`BTCUSDT@Bybit`、`BTC-USDT-SWAP@OkxPerpFutures`……
  初版拿裸 `"BTCUSDT"` 去 `Contains` 必然落空，把本来完全支持的币安永续
  图表误判成不支持。
- **修复**：校验改用 `$"{symbol}@{exchange}"`。交易所段的字面值恰好就等于
  `InstrumentInfo.Exchange`（BinanceFutures / OkxPerpFutures），拼起来即可，
  不需要任何映射表。注意**只有这个校验用复合 key**，Request / Subscribe 的
  `Symbol` 和 `Exchange` 仍是分开的两个字段。
- 这一步正是 v2026.08.09-1 里特意留的诊断路径起了作用：不猜、如实报 NOSUP
  并把服务端支持列表打进日志，一条日志就定位了真正的格式。
- 版本号：`v2026.08.09-2`；双平台编译 0 错误，已部署。

## v2026.08.09-3（2026-08-09，Phase 7L：爆仓档位默认值 + 角标 LIQ 状态）

Sea 实测 v2026.08.09-2：OI 和多空比已完全对上官方指标（角标 `OI 106581
LSR 1.20` vs 官方指标 106580.6 / 1.2），但爆仓那一路看不出有没有数据。

### 两个枚举的数值顺序不一样（坑）

| | Local | SymbolGlobal | Global |
|---|---|---|---|
| 官方指标 UI 的 `LiquidationTypes`（下拉顺序） | 0 | **1** | 2 |
| Coinglass 的 `LiquidationsAggregationModes` | 0 | 2 | **1** |

官方指标内部专门有个方法在这两个枚举之间按**名字**转换，正因为 int 值对不上。
中文 UI 文案对照：`Local`=当前工具和交易所、`SymbolGlobal`=当前工具（所有
交易所）、`Global`=全球（所有符号和交易所）。

设置项直接暴露的是 Coinglass 那个枚举，所以「看下拉第几项」会选错档：Sea 想
要第二项「当前工具（所有交易所）」，选到的 `Global` 其实是第三项全市场。

- 默认值从 `Local` 改为 `SymbolGlobal`（= Sea 要的那一档）
- 设置项显示名补上三档中文对照，避免继续按位置选
- ⚠️ 改默认值不影响**已保存的图表模板**，已经挂着的实例要手动把该项改成
  `SymbolGlobal`（或删掉指标重新添加）

### 角标补上 LIQ 段

此前角标只显示 OI/LSR，爆仓是死是活完全看不出来——三路是各自独立的订阅，
OI 通了不代表爆仓也通。现在显示 `LIQ WAIT`（一条爆仓记录都没收到过，这一路
没通）或 `LIQ 多/空`（当前这根 K 的累计值）。这根 K 没爆仓显示 0，跟"没数据"
是两回事，所以才需要 WAIT 单独区分。

- 版本号：`v2026.08.09-3`；双平台编译 0 错误，已部署。

## v2026.08.09-4（2026-08-09，Phase 7L：爆仓改走轮询聚合，实时逐笔流是死的）

### 关键发现：ATAS 的逐笔爆仓实时流不推送

Sea 实测 v2026.08.09-3：角标显示 `LIQ 0/0` 而不是 `LIQ WAIT` —— 说明历史
数据进来了，但实时逐笔一条都没到。同时 Sea 自己观察到一个更关键的现象：

> ATAS 自带的 Aggregated Liquidations 指标加载之后，随着 K 线继续向前走，
> 这个指标的数据似乎不会更新，还是刚打开软件时的数据。Coinglass 官网自己的
> K 线上「币种爆仓」是有实时数据的。

即**官方指标有同样的毛病**：加载时把历史画出来，之后就不再更新。所以问题在
ATAS 的 `ICoinglassLiquidationOrdersProvider.Subscribe` 这条通道上，不是我们
的订阅代码写错了。OI 和多空比走的是各自独立的订阅通道，不受影响（实测正常
更新，数值与官方指标完全一致）。

### 方案：不依赖那条通道

爆仓改为**周期性重拉聚合历史**（`GetHistoryAsync`，就是官方指标画历史用的
那个接口，实测有数据），默认每 30 秒刷一次最近 2 小时，覆盖写入对应 K 线：

- 新增设置项「爆仓刷新间隔(秒)」，默认 30
- `_cgLiqLongs/_cgLiqShorts` 由累加式改为**覆盖式**写入 —— 同一根 K 被反复
  刷新不会把数字越滚越大
- 首次填充和后续刷新走同一条 `RefreshLiquidationsAsync`，不写两份逻辑
- 逐笔订阅仍然保留，但只统计条数、不参与数值。角标括号里 `(n{聚合}+{逐笔})`
  第二个数字当前恒为 0；哪天不为 0 了，说明 ATAS 把那条通道修好了
- 代价：爆仓值有一个刷新间隔的延迟。换来的是数值口径与官方指标完全一致，
  且不依赖已知失灵的通道

### 顺带修掉一个会整体偏一根 K 的 bug

Coinglass 聚合值的 `Time` 是这根 K 的**起始**时间，而既有的
`FindBarForUtcTime` 比的是 `candle.LastTime`（**收盘**时间，那是给"把已结束
的信号标到它结束的那根 K"用的）。拿起始时间去跟收盘时间比，每一条都会归到
前一根 K 上。新增 `FindBarContainingUtcTime`（比 `candle.Time`，即"这个时间
落在哪根 K 里"）专门给爆仓用，信号那边的原方法不动。

- 版本号：`v2026.08.09-4`；双平台编译 0 错误，已部署。

## v2026.08.09-5 / -6（2026-08-09，Phase 7L：爆仓定位诊断）

Sea 实测 v2026.08.09-4：角标 `LIQ 0/0 (n122+0)`。

关键：`n122` 说明聚合刷新**收到过 122 条记录**，数据是进来了的，所以问题不是
"没数据"而是"落到哪根 K 上"。而且 `LIQ 0/0` 只反映**当前这一根** K——5 分钟
粒度下单根没爆仓是常态（Coinglass 官网同一张图也是大片空柱），光看当前 K
分不清"这根确实没爆仓"和"数据落错了 K"。

### -5：角标改为最近 1 小时合计

`LIQ1h {多}/{空}`，跨 12 根 K。只要近一小时有过爆仓就非零，一眼能判断数据
有没有落在正确位置。（也顺便成了版本标识：显示 `LIQ1h` 才是 -5 及以后。）

### -5：聚合时间戳诊断日志

前 20 条聚合记录打印 `t=... kind=... L=... S=... -> bar N (barTime=...) |
currentBar=... (barTime=...) utcNow=...`。最可疑的就是时区口径：若 Coinglass
返回的 `Time` 不是 UTC 而被我们按 UTC 解释，每条会整体偏移 8 小时落到几小时
前的 K 上，当前 K 自然永远是 0。对比 `t` / `currentBar 的 barTime` / `utcNow`
三者关系即可确认偏了多少。

### -6：刷新汇总日志

Sea 的两张截图 `n` 都停在 122 没有增长，这一个现象对应三种完全不同的故障，
必须先区分开：

| 日志表现 | 结论 |
|---|---|
| 一条 `liq refresh#` 都没有 | 根本没触发刷新 |
| `got=0` | 触发了但服务端返回空 |
| `got=N nonzero=0` | 有记录但值全是 0（多半是聚合档位/symbol 不对） |
| `got=N nonzero>0` | 数据没问题，是落错 K，看 -5 那条时间戳日志 |

每次刷新记一条（限前 10 次），含返回条数、非零条数、时间范围、请求参数
（symbol/exchange/mode/timeframe/from/to）和 `LastOrderId`。

- 版本号：`v2026.08.09-6`；双平台编译 0 错误，已部署。

## v2026.08.09-7（2026-08-09，Phase 7L：聚合爆仓接口对照实验）

v2026.08.09-6 的诊断日志给出了决定性信息，且与此前所有假设都不同：

```
refresh #1: got=122 nonzero=122 range=08-08 00:00..08-08 23:50 | from=08-09 06:24 to=08-09 08:24
refresh #2..#8: got=0 nonzero=0 range=-..-
```

两个事实：

1. **服务端无视传入的 From/To**：请求的是最近 2 小时（08-09 06:24..08:24），
   返回的却是前一整天（08-08 00:00..23:50）。数据因此落到图表最左边、甚至
   范围之外的 K 上，最近 1 小时合计当然是 0 —— 之前怀疑的"时区口径偏 8
   小时"不是主因，主因是返回的时间范围压根不是请求的那段。
2. **第二次起一律返回空**：参数完全相同，只有第一次有数据。高度怀疑是
   `CoinglassDatafeedParameters.UpdatePeriodLimit` 在限流 —— 这个字段一直
   拿到了却从没读过它的值。

这也解释了官方 Aggregated Liquidations 指标"加载时有数据、之后再不更新"：
它一次性拿到这批历史，之后就没有下文了（实时逐笔流又是死的，见 -4）。

### 本版：一次性对照实验

新增设置项「运行爆仓接口诊断(一次性)」（默认开），加载指标时对同一个聚合
接口跑 6 组请求，每组间隔 4 秒（免得请求本身触发限流污染结论）：

| 组 | Timeframe | 窗口 |
|---|---|---|
| tf5m_30min | 5m | 30 分钟 |
| tf5m_2h | 5m | 2 小时 |
| tf5m_24h | 5m | 24 小时 |
| tf1m_2h | 1m | 2 小时 |
| tf1h_24h | 1h | 24 小时 |
| tf1h_7d | 1h | 7 天 |

日志同时打印 `UpdatePeriodLimit`、`utcNow` 和 `localNow`（当标尺，用来判断
返回的 `Time` 是 UTC 还是本地时间，不再靠猜时区）、每组的返回条数/实际时间
范围/末条数值/LastOrderId。

要回答的四个问题：UpdatePeriodLimit 是多少；From/To 到底认不认；换
Timeframe 能否拿到今天的数据；返回的 Time 是什么时区口径。

⚠️ 诊断完成、接口行为摸清后应把这个开关关掉（每次加载都会多打 6 个请求）。

- 版本号：`v2026.08.09-7`；双平台编译 0 错误，已部署。

## v2026.08.09-8（2026-08-09，Phase 7L：爆仓改走逐笔历史接口）

v2026.08.09-7 的对照实验给出了决定性结果：

```
UpdatePeriodLimit=00:01:00   utcNow=08-09 08:36  localNow=08-09 16:36
tf5m_30min → got=0
tf5m_2h    → got=0
tf5m_24h   → got=122  range=08-08 00:00..08-08 23:50
tf1m_2h    → got=0
tf1h_24h   → got=23   range=08-08 00:00..08-08 23:00
tf1h_7d    → got=167  range=08-02 00:00..08-08 23:00
```

### 三条结论

1. **`UpdatePeriodLimit` = 1 分钟**。此前刷新间隔设的 30 秒，比限流还密，
   `refresh #2` 之后一路返回空就是这么来的。刷新间隔默认改 60 秒并**锁死
   下限 60**（`Math.Max(60, ...)`），不给自己再踩一次的机会。
2. **聚合历史接口是 T+1 的**：无视 From/To，按天对齐，返回的永远止于前一天
   23:xx，窗口小于一天直接返回 0。拿不到当天数据。
3. 但官方 Aggregated Liquidations 指标图上**今天是有柱子的** —— 所以它画
   今天那部分用的根本不是这个接口。

### 漏掉的第三条通道

回头看最初那份 IL 扫描，`AggregatedLiquidations` 指标调了**三个**东西，前面
几版只用了两个：

| 通道 | 实测状态 |
|---|---|
| `Subscribe(LiquidationSubscriptionParams)` 逐笔实时 | 死的，一条回调都不来（官方指标同病） |
| `ICoinglassAggregatedLiquidationsProvider.GetHistoryAsync` 聚合历史 | T+1，只到昨天 |
| **`ICoinglassLiquidationOrdersProvider.GetHistoryAsync` 逐笔历史** | **一直没用过** ← 官方指标画"今天"靠的就是它 |

本版把主数据源换成逐笔历史：认 From/To，返回逐笔 `LiquidationOrder`，每 60
秒拉一次最近一段，按 `Id` 去重后累加到所属 K 线（去重集合上界 5 万，满了整体
清空——爆仓单 id 单调递增，清空后最多让极少数旧单重复计一次）。写入从上一版
的覆盖式改回累加式，因为逐笔天然要累加，去重负责保证不重复计。

实时订阅仍然保留，单独走 `_cgLiqLiveQueue` 只统计条数、不参与数值，角标括号
里第二个数字就是它——哪天不为 0 了说明 ATAS 修好了那条通道。

探针也补了 `ord_30min` / `ord_2h` / `ord_24h` 三组逐笔历史对照，万一这条路
也拿不到当天数据，日志里能立刻看出来，不用再多跑一轮。

- 版本号：`v2026.08.09-8`；双平台编译 0 错误，已部署。

## v2026.08.09-9（2026-08-09，Phase 7L：爆仓最后一轮 —— 聚合档位对照）

v2026.08.09-8 实测：逐笔历史接口**服务端直接 500**，连续 4 轮全部如此。

```
probe ord_30min / ord_2h / ord_24h: EX HttpRequestException
  Coinglass request failed: InternalServerError
```

不是"没数据"，是请求本身被拒。而这三组用的都是 `AggregationMode=SymbolGlobal`
（官方指标出厂默认是 `Local`）。逐笔单子天然属于"某个交易所的某个币种"，
`SymbolGlobal`/`Global` 这种跨交易所聚合对逐笔可能根本不成立，服务端因此崩。
成本很低，值得最后验一次再决定去留。

### 本版：档位维度对照

探针改为固定窗口、只变 `AggregationMode`：

- `ord_mode_{Local,SymbolGlobal,Global}`：逐笔历史，2 小时窗口
- `agg_mode_{Local,SymbolGlobal,Global}`：聚合历史，24 小时窗口，tf=5m
  （上一轮只测了 SymbolGlobal 得出 T+1 的结论，万一 Local 档有当天数据，
  爆仓这条路就还有救）

每组间隔 8 秒（UpdatePeriodLimit 是 1 分钟，间隔太密怕限流污染结论）。

### 停止无谓重试

逐笔历史持续 500 的情况下，主刷新路径连续失败 5 次后彻底停手，不再每分钟
撞一次把 ATAS 日志刷满。角标相应显示 `LIQ ERR(n)`，跟 `WAIT`（还在等数据）
区分开——已经放弃了就别显示得像还在等。

### 三条通道的最终状态（若本轮档位对照仍全败）

| 通道 | 状态 |
|---|---|
| 逐笔实时订阅 | 死的，一条回调都不来（官方指标同病） |
| 聚合历史 | T+1，无视 From/To，只到昨天 |
| 逐笔历史 | 服务端 500 |

若本轮六组全败，则 ATAS 这一层拿不到当天爆仓数据，建议不再在 ATAS 内继续
挖：OI 和多空比两路已完全可用（数值与官方指标一致），爆仓改从 Coinglass
官方 API 直连（Sea 有账号），成本远低于继续逆向 ATAS 这层封装。

- 版本号：`v2026.08.09-9`；双平台编译 0 错误，已部署。

## v2026.08.09-10（2026-08-09，Phase 7L 收尾：爆仓判定不可用 + OI/多空比看门狗）

### 爆仓：ATAS 侧确认无解，默认关闭

v2026.08.09-9 的档位对照六组全败：

```
ord_mode_Local        → got=0
ord_mode_SymbolGlobal → 500 InternalServerError
ord_mode_Global       → got=0
agg_mode_Local        → got=89   range=08-08 00:15..08-08 23:35
agg_mode_SymbolGlobal → got=122  range=08-08 00:00..08-08 23:50
agg_mode_Global       → got=288  range=08-08 00:00..08-08 23:55
```

逐笔历史三档全废（空或 500），聚合历史三档**全都只到前一天**。三条通道的
最终结论：

| 通道 | 状态 |
|---|---|
| 逐笔实时订阅 `Subscribe` | 死的，一条回调都不来（官方指标同病） |
| 聚合历史 `AggregatedLiquidationsProvider.GetHistoryAsync` | T+1，无视 From/To，只到昨天，三个档位都一样 |
| 逐笔历史 `LiquidationOrdersProvider.GetHistoryAsync` | 空（Local/Global）或 500（SymbolGlobal） |

因此新增「启用爆仓接入」开关并**默认关闭**，不再每次加载都去撞一遍必然失败
的请求；诊断探针默认也关掉。开关留着是因为哪天 ATAS 修好了打开就能用。要当天
爆仓数据的话，直连 Coinglass 官方 API 比继续逆向 ATAS 这层封装现实得多。

### OI / 多空比：加数据新鲜度 + 停推看门狗

Sea 发现角标 `OI 107229` 而官方 Crypto Open Interest 指标是 `106434.2` ——
**我们的值卡住不动了**（早先两边还完全吻合：106581 vs 106580.6）。即这两条
订阅会"推着推着就不推了"。

陈旧数据比没有数据更危险：数值本身看不出新鲜度，卡住的旧值和实时值在角标上
长得一模一样，推给 VPS 更是完全看不出它是几十分钟前的。

- 角标数值后面跟上距上次推送的时长：`OI 106434@12s LSR 1.15@45s`
- 看门狗 `MaybeResubscribeCrypto`：超过 10 分钟没收到任何推送就退订+重订一次，
  重订本身也限频（同样 10 分钟），免得服务端出问题时每个 tick 都在重订，
  并记日志
- OI 和多空比分开记时间戳（两条独立订阅，可能只坏一条）

- 版本号：`v2026.08.09-10`；双平台编译 0 错误，已部署。

## v2026.08.09-11（2026-08-09，Phase 7L：OI/多空比改走历史轮询）

v2026.08.09-10 的年龄标记立刻坐实了停推：角标 `OI 107229@3m` → `@4m`，
年龄一路涨、数值一动不动，同期官方 Crypto Open Interest 指标显示 106430.2
且仍在正常更新。**官方指标绝不是只靠订阅活着。**

结合爆仓那三条通道的结论，可以下一个总判断：**在 ATAS 这一层，Coinglass 的
订阅通道整体不可靠，能指望的是历史接口。**

区别在于 OI/LSR 的历史接口是好的、有当天数据的 —— 最初那个与官方完全吻合的
值（106581 vs 官方 106580.6）就是初始化拉历史拿到的；而爆仓的历史接口一个
T+1、一个 500，这才是爆仓无解、OI/LSR 有救的根本差别。

### 改动

- 新增 `MaybeRefreshOiLsr` / `RefreshOiLsrAsync`：每 60 秒（UpdatePeriodLimit
  是 1 分钟）拉一次最近 30 分钟的 OI 和多空比历史，入队后由既有的消费逻辑
  自然取到最后一条即最新值。**这条现在是主数据源。**
- 订阅继续保留当快速通道：活着就是秒级更新，死了也不影响轮询。
- 前 5 次刷新记日志（条数 + 最后一条的时间和数值），便于核对是否跟得上官方
  指标，也能验证历史返回的是升序。
- 看门狗改为兜底：轮询会不断刷新 `_cgLastDataUtc`，所以它正常不再触发，只有
  连轮询也一起失效（网络断了之类）时才走到那条重订阅路径。注释已写明，免得
  以后误以为它还在扛主要职责。

- 版本号：`v2026.08.09-11`；双平台编译 0 错误，已部署。

## v2026.08.09-12（2026-08-09，Phase 7L：修正 -11 的判断 + 兜底轮询窗口）

### 更正 v2026.08.09-11 的结论

-11 里写的"轮询现在是主数据源"**是错的**。Sea 实测后日志显示：

```
oi/lsr refresh #1..#5: oi got=0 last=- | lsr got=0 last=- | req from=08-09 08:49 to=08-09 09:19 tf=00:05:00
```

轮询五次全部返回空，一条都没拿到。而同时角标是 `OI 106415@39s LSR 1.20@39s`，
数值与官方指标 106414.7 逐位吻合、年龄 39 秒——**这是订阅自己恢复了推送**，
跟新加的轮询没有关系。

厘清后的真实分工：

- **订阅是主数据源**，正常情况下工作良好（秒级更新、数值与官方一致）
- 订阅会**偶发静默**（曾卡死在 107229、年龄涨到 @4m 仍在涨），诱因很可能是
  短时间内反复重载指标 + 密集探针请求把连接搞坏或触发限流；重启后自行恢复
- 轮询的唯一意义是**静默时接住它**

### 兜底轮询窗口：30 分钟 → 24 小时

-11 用的 30 分钟窗口实测 `got=0`，跟爆仓聚合接口"窗口小于一天就返回空"是
同一个毛病，等于兜底根本没生效——订阅再静默一次照样接不住。

改用 24 小时（`CryptoHistoryHours`）：这是**已验证可行**的窗口，初始化时用的
就是它，拿到的值与官方指标逐位吻合。5 分钟粒度下 288 条、每分钟一次，流量
可以忽略。订阅活着时这条只是重复喂同样的值，无副作用。

- 版本号：`v2026.08.09-12`；双平台编译 0 错误，已部署。

## v2026.08.09-13（2026-08-09，Phase 7L：照抄官方的请求参数口径 —— To=DateTime.MaxValue）

Sea 问了个关键问题：**新开图表加载 Aggregated Liquidations 时它能拿到最新
数据，是怎么做到的？** 这是"三条通道全废"结论的直接反例——同样的接口，
官方拿得到、我们拿不到，差别只可能在参数。

于是把该指标初始化后的主流程整个反汇编（状态机
`#=zxEQ$vRuqL$2VzpB732fQe72EjbWpl0EiQg==`）：

```
IL_0179: ldc.i4.0
IL_017A: call ExtendedIndicator::GetCandle     ← from = GetCandle(0).Time
IL_017F: callvirt IndicatorCandle::get_Time         图表第一根 K 的时间
IL_0186: ldsfld DateTime::MaxValue             ← to = DateTime.MaxValue ★
IL_019F: call Indicator::get_ChartInfo
IL_01A4: callvirt IChart::get_ChartType
IL_01B3: call String::op_Equality              ← 按图表类型二选一
IL_01BF: call ...(逐笔历史)
IL_0232: call ...(聚合历史)
```

### 根因：To 参数一直传错

官方传 **`To = DateTime.MaxValue`**，而我从第一版起一直传 `DateTime.UtcNow`。
服务端多半把 To 当"截止到某个已完成的边界"解释，于是把当天数据整段截掉。
前面那一长串现象——窗口小于一天返回 0、聚合"只到昨天"、逐笔 500——很可能
**全是这同一个参数错误的不同表现**，而不是三条独立的通道故障。

`From` 也不是"最近 N 小时"，而是图表第一根 K 线的时间（窗口远比我试过的大）。
`Timeframe` 取自 `ATAS.Indicators.Extensions.GetTimeFrameTypeChartPeriod(ChartInfo)`
（public static，可直接调用），不是自己解析"时间周期标签"。

### 改动

- 新增 `CoinglassFromTime()` / `CoinglassTimeframe()`，统一按官方口径取参数
- 爆仓、OI/多空比轮询**全部**改为 `From=GetCandle(0).Time, To=DateTime.MaxValue`
- 爆仓两条历史通道都试：先逐笔（粒度细），拿不到再退聚合——官方是按
  `ChartInfo.ChartType` 二选一，但那个比较用的字符串常量被混淆加密读不出来，
  所以运行时两条都试，日志里 `via=orders|agg|none` 指明实际生效的是哪条
- 聚合结果覆盖式写入、逐笔结果去重累加，同一时刻只有一条路径供数
- 「启用爆仓接入」**改回默认开启**（-10 里因判定不可用而关掉的）

### 教训

-10 那句"ATAS 侧三条通道全废"下得太早：三条通道用的是同一套错误参数，
一起失败并不意味着三个独立故障。反例出现时（官方拿得到而我们拿不到），
应该立刻去比对参数，而不是接受"平台没这能力"的结论。

- 版本号：`v2026.08.09-13`；双平台编译 0 错误，已部署。

## v2026.08.09-14（2026-08-09，Phase 7L 完成：三路数据全部打通）

### 结果：To=MaxValue 一改，全部通了

Sea 实测 v2026.08.09-13：

```
liq refresh #4 via=agg: ord=-1  agg=5245 [07-11 00:00..08-09 09:35]
   from=07-11 00:00 to=MaxValue  utcNow=08-09 09:47
oi/lsr refresh #3: oi got=8470 last=08-09 09:45/106402.469 | lsr got=8470 last=08-09 09:45/1.2
```

聚合接口返回到 `08-09 09:35`（UTC，距当时仅 12 分钟）——**它从来就不是 T+1
接口，纯粹是 `To` 参数传错**。角标 `LIQ1h 12309/9876` 对 Coinglass 网页
`12.309K / -10.076K`：多头完全一致，空头小差是因为我们算的是"最近 12 根 5
分钟 K"的滚动窗口，而网页是对齐整点的 1H 柱，口径不同，属正常。

前面 -4 到 -10 那一长串"三条通道全废"的结论**全部作废**：它们不是三个独立
故障，是同一个参数错误的三种表现形态。

（逐笔历史 `ord=-1` 仍然 500，但聚合这条已足够，`via=agg` 正常供数。）

### 本版：增量刷新窗口

-13 每轮都用全量窗口（图表第一根 K 至今），实测一次就是 5245 条聚合 + 8470
条 OI + 8470 条多空比 —— 每分钟重复解析两万多条记录只为取最后几条，浪费。

官方指标只在初始化时拉一次，我们是每 60 秒一轮，不能照抄同一个窗口：

- 首轮全量（`GetCandle(0).Time` 起），之后改用 2 小时窗口
- `To` 仍然必须是 `DateTime.MaxValue` —— 那才是拿到当天数据的关键，收窄的
  只有 From
- 增量返回空或抛异常就复位标志，下一轮退回全量，不会因窗口收窄而卡住

### Phase 7L 最终状态

| 数据 | 状态 |
|---|---|
| Crypto Open Interest | ✅ 订阅 + 60 秒轮询兜底，值与官方指标一致 |
| Long/Short Ratio | ✅ 同上 |
| Aggregated Liquidations | ✅ 聚合历史轮询，值与 Coinglass 网页一致 |

三路都不需要在图表上挂对应的官方指标。VPS 推送四个字段仍默认关闭，等服务端
加好 `cg_oi_close` / `cg_lsr` / `cg_liq_long` / `cg_liq_short` 再开。

### 教训（值得写下来）

1. `To=DateTime.UtcNow` 看着天经地义，实际是致命的：服务端把它当"截止到某个
   已完成边界"，直接截掉当天数据。**照抄官方实现的参数，不要想当然。**
2. 我在 -10 就下了"ATAS 侧无解、建议放弃"的结论，太早。当时已经有明确反例
   摆在眼前（官方指标拿得到、我们拿不到），正确动作是立刻反汇编比对参数，
   而不是接受"平台没这能力"。**出现反例时，先怀疑自己的用法。**
3. 角标上的数据年龄标记（`@21s`）是这轮最有价值的副产品：陈旧数据和实时数据
   在数值上长得一模一样，没有年龄标记根本发现不了 OI 卡住。

- 版本号：`v2026.08.09-14`；双平台编译 0 错误，已部署。

## v2026.08.09-15（2026-08-09，Phase 7L：LIQ1h 对齐 Coinglass 整点柱）

Sea 要求角标爆仓值与 Coinglass 完全一致。-14 的口径是"最近 12 根 K 的滚动
合计"，而 Coinglass 网页 1H 框架是**对齐整点**的柱子，两者在整点附近必然
对不上（实测角标 9876 vs 网页 10.076K，差的就是已经滚出 12 根窗口、但仍属于
本小时的那几根）。

- 新增 `LiquidationHourSum()`：取 `[本整点小时 00 分, 现在]` 区间内所有 K 线
  的爆仓合计，与网页当前那根 1H 柱同口径
- 时区不必换算：Coinglass 按 UTC 整点分桶，北京时间是整数小时偏移，两边的
  "整点"落在同一时刻
- 图表周期 >= 1 小时时（4H/日线等），一根 K 本身就跨多个整点，分桶无意义，
  直接返回当前 K 的值

- 版本号：`v2026.08.09-15`；双平台编译 0 错误，已部署。

## v2026.08.09-16（2026-08-09，Phase 7L：量化爆仓接口滞后）

Sea 实测：Coinglass 实时爆仓列表 18:13、18:14 都有单子，网页 1H 柱已是
`585.79 / -175.322K`，而角标 `LIQ1h 0/0`。

回看 -13 的日志有条线索之前被放过了：

```
agg=5245 [07-11 00:00..08-09 09:35]   utcNow=09:47
```

**服务端返回的最后一条比当前时间晚 12 分钟**。当时理解成"那 12 分钟恰好没
爆仓"，但结合这次的现象，更可能是接口本身有滞后：本小时才过 14 分钟，若滞后
10~15 分钟，本小时的桶自然还是空的。

### 本版：把滞后量化出来

- 记录服务端最后一条爆仓记录**自身的时间戳**（不是我们收到它的时间）
- 角标显示 `LIQ1h 0/0 ~12m`：`~` 后面就是这条链路的真实滞后。本小时显示 0/0
  时，看它立刻能分清是"确实没爆仓"（~小）还是"数据还没到"（~大）
- `liq refresh` 日志加 `lag=`，并把记录上限从 10 次放宽到 200 次（60 秒一轮
  约 3 小时）——判断滞后需要连续观察 range 末尾怎么随时间推进，只记前 10 次
  根本看不出来

### 顺带排除一个可能

-15 引入的 2 小时增量窗口本身也可能影响返回范围末端（这个接口对窗口大小很
敏感：早先 To 传错时，小于一天的窗口直接返回空）。爆仓的增量窗口暂时放宽到
12 小时——既远小于全量、又留足余量。OI/多空比保持 2 小时不动，那边已实测正常
（角标 `@21s`）。

- 版本号：`v2026.08.09-16`；双平台编译 0 错误，已部署。

## v2026.08.09-17（2026-08-09，Phase 7L：爆仓退回全量窗口）

Sea 实测（跑的仍是 -15，`~` 年龄标记未加载）：18:23 了，当前小时仍 `LIQ1h 0/0`。

### 不是接口滞后

决定性证据：**同一台机器上，ATAS 官方 Aggregated Liquidations 面板在
18:00~18:20 是有柱子的**。同一个数据源，官方拿得到当前小时，我们拿不到。
所以 -16 里"接口滞后 10~15 分钟"的猜测不成立。

差别只剩一个：官方用**全量窗口**（`from = GetCandle(0).Time`，加载时拉一次），
而 -15 把我们改成了每轮 2 小时增量窗口。这个聚合接口对窗口大小一向敏感——
早先 `To` 传错时，小于一天的窗口就直接返回空——收窄 `From` 同样会影响它返回
范围的**末端**。

### 改动

爆仓刷新每轮都用全量窗口，不做增量。每轮约 5000 条、每 60 秒一次，解析成本
几十毫秒，为正确性值得。OI/多空比的 2 小时增量已实测正常（角标 `@26s`），
保持不动。

`-16` 加的 `~lag` 年龄标记保留：即使数据正常，它也能持续回答"当前 0/0 到底是
真没爆仓还是数据没到"，不用每次都翻日志。

### 教训

`-15` 那次增量优化是我自己主动加的性能优化，不是 Sea 要求的，结果引入了回归、
又花掉两轮排查。**在一条刚刚跑通、行为还没完全摸清的链路上，不该顺手做"顺带
优化"** —— 尤其是这种已知对参数敏感的接口。

- 版本号：`v2026.08.09-17`；双平台编译 0 错误，已部署。

## v2026.08.09-18（2026-08-09，Phase 7L：记录最后一条聚合记录的 K 线归属）

Sea 实测 -17（角标已出现 `~` 说明加载成功）：`LIQ1h 0/0 ~30m (n6815+0)`，
图表已切到 **H1**。

`~30m` 这个数字本身就排除了"没数据"：服务端最后一条爆仓记录是 30 分钟前，
而当前 H1 K 线才走了一半——那条记录**应该落在当前这根 K 上**。所以数据是拿到
了的，问题在**归属**。

### 之前的诊断日志看错了地方

`agg t=... -> bar N` 那条只记前 20 条，而每轮返回 5000+ 条、按时间升序，
前 20 条全是几周前的老数据——恰恰看不到最新那条落在哪。

改为只记每批的**最后一条**，把 `t` / `kind` / 落到的 bar 及其 barTime /
currentBar 及其 barTime / `utcNow` / `localNow` / 当前 bar 的实际存值全部
摆在一起。时区口径对不对、是差 8 小时还是差一根 K，一眼可见。

- 版本号：`v2026.08.09-18`；双平台编译 0 错误，已部署。

## v2026.08.09-19（2026-08-09，Phase 7L：相同请求会命中服务端缓存）

直接读 ATAS 日志（Sea 那边 grep 无输出是因为**日志按重启分文件**，当天有
`app_20260809.log` 和 `app_2026080920260809.log` 两个，只 grep 其中一个会漏）。

### 根因：重复请求同一个窗口 = 吃缓存

```
liq refresh #3: agg=6793 [10-15 05:00..08-09 10:00]  utcNow=10:39:42  lag=40m
liq refresh #7: agg=6793 [10-15 05:00..08-09 10:00]  utcNow=10:43:44  lag=44m
```

五轮请求，条数和 range 末尾**一个字都没变**，lag 从 40m 一路涨到 44m。
而同一份日志里 OI：

```
oi/lsr refresh #3: oi last=08-09 10:00/106263.105   req from=08-09 08:40
oi/lsr refresh #4: oi last=08-09 10:00/106263.99    req from=08-09 08:41
oi/lsr refresh #5: oi last=08-09 10:00/106263.84    req from=08-09 08:42
```

**值在持续变化，且 from 每轮都不同。** 爆仓用的是固定全量窗口
（`from=GetCandle(0).Time`，每轮完全相同），服务端直接返回缓存。

所以 -17 退回固定全量窗口是退错了方向。正确的理解是：

- 全量窗口能拿到**请求那一刻**的最新数据（官方指标只在加载时拉一次，所以对）
- 但每 60 秒重复**同一个**请求只会吃缓存，末端永远停在第一次那一刻
- 要持续更新，`From` 必须每轮都不同

改为首轮全量、之后 24 小时滚动窗口（`DateTime.UtcNow` 精确到秒以下，每轮
天然不同）。窗口取 24 小时而非 -15 用过的 2 小时：这接口对窗口大小敏感，
24 小时是验证过有数据的量级。

### 另一个坑：替换 dll 后必须完全重启 ATAS 进程

Sea 这轮跑的其实还是 v-16/17（日志有 `lag=` 但没有 v-18 才加的 `agg LAST`）。
dll 18:32:36 已写入，指标 18:37 重新加载，但**旧 dll 已经在进程内存里**，
只移除/重新添加指标不会重新加载程序集。必须完全退出 ATAS 再启动。
前面几轮的验证很可能都受此影响。

- 版本号：`v2026.08.09-19`；双平台编译 0 错误，已部署。

## v2026.08.09-20（2026-08-09，Phase 7L：让归属诊断日志活下来）

### -19 的滚动窗口生效了，但问题不在缓存

```
liq refresh #3: agg=113 [08-08 00:00..08-09 10:30] from=08-08 10:51 utcNow=10:51:07 lag=21m
liq refresh #4: agg=113 [08-08 00:00..08-09 10:30] from=08-08 10:52 utcNow=10:52:08 lag=22m
```

`from` 每轮确实在变（10:51→10:52），但返回完全一样 —— 所以 -19 判断的"相同
请求吃缓存"不是主因。同一时刻 OI 是 `last=08-09 10:45`，爆仓只到 `10:30`：
这个聚合端点本身就比 OI 慢约 20 分钟。

但慢 20 分钟解释不了 `LIQ1h 0/0`：本小时的 10:00 / 10:15 / 10:30 三根桶服务端
都给了，合计不该是 0。所以仍然指向**归属**。

### -18 的诊断日志从来没输出过

`agg LAST` 在日志里一条都没有，而 `n` 计数在涨（聚合队列确实在消费）。原因是
整条日志裹在一个 `try` 里，中间的 `GetCandle` 一抛异常就把整行吞掉，还
照样递增计数器 —— 等于白等一轮。

本版重写：

- 核心字段（记录时间戳/Kind、落到的 bar 号、currentBar、当前 bar 的实际存值、
  utcNow、localNow）不依赖任何可能抛异常的调用，先拼好
- `GetCandle` 单独包 try，失败只降级成 `[GetCandle EX ...]` 而不吞掉整行
- 新增 `hourBars=[bar:多/空]`，直接列出本小时范围内所有非零 bar —— 数据到底
  落在哪几根上，一眼可见

- 版本号：`v2026.08.09-20`；双平台编译 0 错误，已部署。

## v2026.08.09-21（2026-08-09，Phase 7L：真正的根因 —— 一个 break）

-20 的诊断日志一行定案：

```
agg LAST t=08-09 10:55:00 L=1168.87 S=64.94 -> bar 8483 | currentBar=8485
  curBarVal=0/0  [GetCandle EX ArgumentOutOfRangeException]
  hourBars=[8474:586/53708][8475:0/202][8476:0/203631][8479:909/0][8483:1169/65]
```

三件事同时明确：

1. **归属一直是对的**：`t=10:55` 落到 bar 8483，紧挨着 `currentBar=8485`。
   不是时区问题（前面怀疑过好几轮）。
2. **数据一直都在**：`hourBars` 显示本小时有 5 根非零 bar，合计约 2664/257606。
3. **`GetCandle(CurrentBar)` 会抛 `ArgumentOutOfRangeException`** —— CurrentBar
   指向的那根在某些时刻还没建出来。

而 `LiquidationHourSum()` 里写的是：

```csharp
for (int i = CurrentBar; ...; i--) {
    try { c = GetCandle(i); } catch { break; }   // ← 第一次调用就 break
```

**第一次调用就抛异常 → 立即 break → 函数恒返回 0/0。** 数据全在字典里，求和
循环在第一步就退出了。改成 `continue`（只有确实读到更早的 K 才 break）。

### 教训

这个 bug 是 -15 引入整点分桶时写下的，之后连续 6 个版本（-16 到 -20）我都在
排查"为什么没数据"，方向全在数据链路上：时区口径、聚合档位、窗口大小、服务端
缓存、接口滞后 —— **没有一次怀疑过展示端的求和函数本身**。角标显示 0/0，我
默认了"0 是算出来的结果"，而它其实是"异常退出的默认值"。

正确做法：`LIQ1h` 与 `hourBars`（原始字典内容）应该从一开始就一起打日志。
数据落地与数据展示是两个独立环节，任何一个都可能坏，不能只查一头。

- 版本号：`v2026.08.09-21`；双平台编译 0 错误，已部署。

## v2026.08.09-22（2026-08-09，Phase 7L：区分"数据年龄"和"刷新年龄"）

-21 修好后数据完全对上了（角标 `0/163410` vs Coinglass 网页 `-163.41K` 逐位
一致）。但 Sea 观察到 `~` 涨到 8m、9m，超出上一轮口头说的"正常 1~7 分钟"。

对照两次截图：

```
n2113 → n2313    刷新正常工作
163475 → 163475  值一个字没变
~8m   → ~9m      持续增长
```

三者同时出现恰恰说明系统正常：**那几分钟市场上根本没有新爆仓单**，服务端最后
一条记录还是旧那条，`~` 自然一路涨（Coinglass 实时列表也印证：19:03、19:05
两笔之后就安静了）。

问题在于 `~` 这一个数字混了两种完全不同的情况：

| 情况 | 是否故障 |
|---|---|
| 链路慢/挂了 | 是 |
| 市场安静、没有新爆仓 | 否，且没有上限 |

所以"正常范围 1~7 分钟"这个说法从根上不成立。角标改为显示两个年龄：

- `~xx` = 最后一条爆仓记录**自身**时间戳距今（数据新鲜度，无上限，大不等于故障）
- `/yy` = 上次**成功刷新**距今（链路健康度，正常必须稳定在 60 秒内）

形如 `LIQ1h 0/163475 ~9m/32s (n2313+0)`：数据 9 分钟没更新，但链路 32 秒前
刚成功刷过 —— 一眼就能判定是市场安静而非故障。

- 版本号：`v2026.08.09-22`；双平台编译 0 错误，已部署。

## v2026.08.09-23（2026-08-09，新增 AtasLiquidations 副图指标）

数据链路打通后，Sea 提出"现在可以做个和 Coinglass 一样的爆仓指标了"。这才
真正闭环了最初的需求：**连 Aggregated Liquidations 那个副图也不用挂**。

### 新指标 AtasLiquidations（独立文件 AtasLiquidations.cs）

- 副图双向柱状图：多头向上（绿）、空头向下（红，存成负值），与 Coinglass
  网页「币种爆仓」同口径
- 数据每 60 秒重拉一次聚合历史 —— 官方那个指标加载后就不再更新（依赖的逐笔
  实时订阅通道是坏的），这是本指标存在的意义
- 取数参数完全沿用 AtasBridge 里验证过的那套，几个反直觉点都写在注释里：
  `To=DateTime.MaxValue`（传 UtcNow 会被服务端截掉当天数据）、首轮全量+之后
  24 小时滚动窗口、`SYMBOL@EXCHANGE` 复合 key 校验、按 K 线**开盘**时间归属
- 值同时写 series 和字典：ATAS 重算会清空 series，靠字典在 OnCalculate 回填
- 取不到数据时在面板上画出原因（N/A / NOSUP / NOSYM / ERR），不让用户对着
  空面板猜；正常工作时不画任何东西

为什么做成独立指标而不是并进 AtasBridge：AtasBridge 是 `DenyToChangePanel=true`
锁死在主图的（信号箭头、位价线依赖主图坐标），柱状图必须独立副图，两者放不进
同一个指标。两个指标共用同一个 Coinglass provider 单例，不额外占配额。

### 双平台差异：ValueDataSeries.Color

编译时暴露的新差异 —— 该属性在普通版 ATAS 是 `System.Windows.Media.Color`，
在 ATAS X 是 `System.Drawing.Color`。用 `SeriesColor` 类型别名 + `ATAS_PLATFORM`
编译符号隔离，业务代码不必到处 `#if`。

注意 `OnRender` 里 `RenderContext.DrawString` 的颜色**两平台都是**
`System.Drawing.Color`（OFT.Rendering 双平台一致），只有 DataSeries 那侧有差异。

引入 PresentationCore 时踩了一下：`<UseWPF>true</UseWPF>` 会要求 TargetFramework
带 `-windows` 后缀（NETSDK1136），改为直接引用
`Microsoft.WindowsDesktop.App\10.0.10\PresentationCore.dll`，与
`System.Drawing.Common` 的处理方式一致，TargetFramework 保持 net10.0 不变。

`AtasBridge.Platform.csproj` 已加入新文件的 `<Compile Include>`（该项目
`EnableDefaultCompileItems=false`，新增文件必须手动登记，别漏）。

- 版本号：`v2026.08.09-23`；双平台编译 0 错误，已部署。

## v2026.08.09-24（2026-08-09，AtasLiquidations：悬停读数 + 默认独立面板）

Sea 提的两点：

### 1. 鼠标悬停显示具体数值（对齐 Coinglass 的用法）

series 自带的 tooltip 一次只显示一条（悬到红柱只看到 `Shorts -1692225`），
看不到多空对照。改为在面板左上角画读数：

- 鼠标悬在哪根 K 上就显示那根的多空爆仓额，鼠标移出图表时显示最新那根
- 数量级缩写与 Coinglass 一致：`1.067M` / `-1.707M`
- 颜色跟随各自 series 的颜色设置（改了柱子颜色，读数颜色跟着变）

用 `MouseLocationInfo.BarBelowMouse` + `IsMouseLeave` 取悬停位置。

顺带补一个双平台辅助：`SeriesColor`（Media/Drawing 因平台而异）→
`System.Drawing.Color`（RenderContext 用）。两边都有 A/R/G/B，所以
`Color.FromArgb(c.A, c.R, c.G, c.B)` 一句话通吃，不需要 `#if`。

### 2. 默认落在独立副图

构造函数加 `Panel = IndicatorDataProvider.NewPanel;`（`IndicatorDataProvider`
的静态常量，值就是 `"NewPanel"`）。

⚠️ 只影响**新添加**的实例。已经加在图表上的那个，面板选择已随模板存下来了，
要改必须删掉重新添加。

- 版本号：`v2026.08.09-24`；双平台编译 0 错误，已部署。

## v2026.08.09-25（2026-08-09，AtasLiquidations：对齐官方 series 配置）

Sea 反馈两点：柱子比主图 K 线细一大截；悬停读数没出现。

直接反射实例化官方 `AggregatedLiquidations` 读它的 series 配置来比对：

| 属性 | 官方 | -24 的值 |
|---|---|---|
| `Width` | 1 | 2 |
| `UseMinimizedModeIfEnabled` | **false** | **true** ← 柱子变细的原因 |
| `ShowCurrentValue` | **true** | 未设 |
| `Panel` | NewPanel | NewPanel ✓ |

### 柱子太细：UseMinimizedModeIfEnabled

我想当然开了这个开关，它会让柱子退化成一条细线。`Width` 反而不是柱宽——
Histogram 本来就按 K 线宽度绘制，官方 `Width=1` 柱子照样和 K 线同宽。已全部
对齐官方取值，并补上 `ShowCurrentValue=true`（最新值显示在右侧价格轴）、
`Digits=0`（爆仓额是大数，不需要小数位）。

### 悬停读数不出现：多调了 SubscribeToDrawingEvents

`SubscribeToDrawingEvents(DrawingLayouts.Final)` 限定了绘制层，导致 OnRender
的读数根本不画。AtasBridge 那边只开 `EnableCustomDrawing`、从不调用它，
OnRender 一直正常——照抄即可。

读数位置同时下移一行（y=20）：面板左上角那行是 ATAS 自己画的指标名
（`AtasLiquidations (Bars, True)`），画在同一行会糊成一团。

- 版本号：`v2026.08.09-25`；双平台编译 0 错误，已部署。

## v2026.08.09-26 / -27（2026-08-09，AtasLiquidations：悬停读数终于画出来）

### 柱宽已修复（-25 生效）

Sea 确认柱子宽度与主图 K 线一致了，根因是 `UseMinimizedModeIfEnabled=true`。

### 悬停读数：OnRender 压根没被调用

-26 加了 OnRender 诊断日志（此前整个方法裹在 `catch{}` 里，出错会静默，
跟 `agg LAST` 日志消失是同一个坑，同一个错误犯了第二次）。结果一条日志都
没有 —— OnRender 根本没触发。

反编译官方 `AggregatedLiquidations` 构造函数，答案很直接：

```
set_Panel(...)
set_DenyToChangePanel(1)
set_EnableCustomDrawing(1)
SubscribeToDrawingEvents(2)    ← DrawingLayouts.Historical
```

`DrawingLayouts` 的取值是 `None=1, Historical=2, LatestBar=4, Final=8`。

前面绕了两版：-24 用 `Final(8)` 不触发，-25 索性去掉订阅也不触发。AtasBridge
只开 `EnableCustomDrawing` 就能画，是因为它在**主图**；副图指标必须显式
订阅绘制层，且官方用的是 `Historical`。

教训与前面几次同类：**有现成的官方实现可比对时，先去读它的 IL，不要凭
"应该是这样"去试**。这一个参数试错花掉了三个版本。

- 版本号：`v2026.08.09-27`；双平台编译 0 错误，已部署。

## v2026.08.09-28（2026-08-09，回退 -27 的回归：绘制层改用 Final）

-27 照抄官方的 `SubscribeToDrawingEvents(DrawingLayouts.Historical)` 造成回归：
**副图柱子整片消失**，面板全空。

订阅 Historical 等于接管历史层绘制，ValueDataSeries 的柱子就不再自动画了。
官方 AggregatedLiquidations 能这么写，是因为它的 OnRender 自己把柱子也画了；
我们这里只想在柱子之上叠加一行读数，柱子仍交给 series 自动绘制 —— 应该用
`Final`（最终叠加层），不影响 series 本身。

### 顺带纠正一个此前的误判

-24 用 `Final` 时读数没出现，当时归因为"Final 不触发 OnRender"。现在回看，
更可能是那版把读数画在 `y=3`，正好被 ATAS 自己画的指标名那行盖住了 ——
`y=20` 是 -25 才改的，而 -25 同时又把订阅删了。**两处改动撞在一起，把一个
本来可用的组合误判成不可用**，接着又照抄官方参数引入了更严重的回归。

教训：一次只改一个变量。这一段（-24 到 -28）连续五个版本都在同一个小功能上
反复，根源就是每版同时动了两处，无法归因。

- 版本号：`v2026.08.09-28`；双平台编译 0 错误，已部署。

## v2026.08.09-29（2026-08-09，读数其实一直在画）

-28 柱子恢复正常。读数 Sea 说"什么也没出现"，但日志显示它一直在工作：

```
OnRender ok: bar=9489 currentBar=9557 l=0 s=0 text="0 0" at(5,20) layout=LatestBar
```

两件事：

1. **OnRender 正常触发**（`Final` 订阅是对的），读数也画了，只是内容是 `"0 0"` ——
   这几条日志都发生在**首次 refresh 完成之前**（21:06:14 画，21:06:16 数据才到），
   那时字典还是空的。日志上限只有 3 条，而 OnRender 每帧都调，所以记到的全是
   启动瞬间的状态，反映不了稳定后的情况。**诊断日志只记前 N 次，对每帧调用的
   函数是错的采样方式** —— 这一点值得记住。
2. Sea 截图的是官方 `Aggregated Liquidations` 面板，我们的读数画在
   `AtasLiquidations` 面板里。

本版给读数垫了一层半透明黑底：面板里有网格线和柱子，纯文字（尤其值为 `"0 0"`
时只有两个字符）很容易被当成背景看漏。

- 版本号：`v2026.08.09-29`；双平台编译 0 错误，已部署。

## v2026.08.09-30（2026-08-09，读数不可见的真正原因：坐标是画布绝对坐标）

日志明明是 `OnRender ok ... at(5,20)`，屏幕上却什么都没有。原因：

**RenderContext 的坐标是画布绝对坐标，不是相对本面板的。** 副图指标绘制时会被
裁剪到 `ClipBounds`（本面板区域）内，写 `(5,20)` 等于画到主图顶部，整块被裁掉。
AtasBridge 那边直接用小坐标没事，是因为它本来就在主图 —— 照搬到副图就废了。

改为以 `context.ClipBounds` 左上角为基准：`x = clip.X + 5, y = clip.Y + 20`。

### 顺带修正诊断日志的采样方式

原来是"只记前 3 次"，而 OnRender 每帧都调用 —— 采到的全是启动瞬间（首次
refresh 还没完成，值恒为 0），完全反映不了稳定后的状态，上一轮就是被这个
误导的。改为**每 10 秒记一条**（上限 20 条），并把 `clip` / `size` 一起打出来。

**对高频调用的函数，"只记前 N 次"是错误的采样方式**，应该按时间节流。

- 版本号：`v2026.08.09-30`；双平台编译 0 错误，已部署。

## v2026.08.09-32（2026-08-09，读数坐标开放为设置项 + 版本号不再被模板锁死）

### 读数一直画着，只是画到主图去了

Sea 截图里主图左上角出现了 `1.106M -1.692M` —— 绘制从头到尾是好的，问题纯粹
是位置：OnRender 的坐标是**整块画布的绝对坐标**，而 `ClipBounds` 实测返回
`(0,0)` 起（等于画布原点），所以 -30 那次"以 ClipBounds 为基准"的修正等于没改。

本版：

- 基准优先取 `Container.RelativeRegion`（本指标容器区域）的左上角，拿不到
  再退回 ClipBounds
- 新增设置项「读数X偏移」「读数Y偏移」「显示读数」—— 面板高度、是否有其它
  副图、DPI 都会影响位置，与其我反复猜，不如开放出来让 Sea 直接拖到合适
- 日志补 `region=`，把 clip / region / size 三个值一起打出来

### 版本号被图表模板锁死（排查干扰源）

Sea 的设置面板显示 `v2026.08.09-7`，而实际 dll 已经是 -31。原因：`VersionInfo`
是普通读写属性，值会随图表模板持久化，升级 dll 后面板显示的仍是**保存模板那
一刻的旧版本号**。

前面几轮我据此判断"新版没加载"，双方对不上账，白折腾了好几次。改成 getter
恒返回当前常量、setter 空实现，模板里的历史值反序列化时直接丢弃。

**只读展示型属性不能设成可写**，否则会被持久化成误导信息。

- 版本号：`v2026.08.09-32`；双平台编译 0 错误，已部署。

## v2026.08.09-33（2026-08-09，读数：没悬停时回退到最近有值的 K）

-32 读数已正常显示在副图。Sea 反馈：不悬停时恒显示 `0 0`。

原因：没悬停时取的是 `CurrentBar`，而这条链路有几分钟滞后（角标同时显示
`~10m`），当前这根 K 通常还没数据 —— 尤其 M15 这种大周期，一根 K 空着的
时间更长。

改为往回找最近一根有值的 K（上限 500 根），并在数值后面标出该 K 的时间
（按 `InstrumentInfo.TimeZone` 转成图表时区），例如：

```
1.106M   -1.692M 15:10
```

标时间是必要的：否则会把十几分钟前的旧值误当成此刻的值。悬停时不显示时间
（鼠标位置本身就指明了是哪根 K）。

- 版本号：`v2026.08.09-33`；双平台编译 0 错误，已部署。

---

## 归档说明（2026-08-09 收尾）

本日工作已同步到项目仓库 `saiy829/btc-trader` 的 `AtasBridge/` 目录。

工作流：**本地开发 → scp 到 VPS `/opt/btc-trader/` → 从 VPS 推 GitHub**
（VPS 上另有 `git_sync.sh`，每天 03:00 自动同步一次）。

新增 `PHASE7L_Coinglass接入复盘.md`：本次接入的全貌、Coinglass 接口的实测行为
（改动取数逻辑前必读）、以及 10 条弯路与教训。按版本流水的细节仍看本文件。

另外补一条版本号纪律：`AtasBridgeVersion.Tag` 是**整个 dll 的**版本
（AtasBridge 与 AtasLiquidations 同在一个程序集），改动任一源文件都要更新它。
`-33` 只改了 `AtasLiquidations.cs` 漏了这里，导致代码显示 `-32` 而 CHANGELOG
记到 `-33`，已修正。

---

## v2026.08.12-1（2026-08-12，任务卡 9G：新增 SweepMarker 指标）

新增第三个指标 `SweepMarker`（Setup C 流动性扫除反转标记），与 AtasBridge、
AtasLiquidations 同在 AtasBridge.dll 里。纯标记指标：不推 VPS、不落库、不联网、
不下单，只把入场/止损/TP1/TP2 画在图上供手动执行。

**新增文件**：`AtasBridge/SweepMarker.cs`（纯 ASCII，注释全英文，符合 v5.1 起的约定）

**两阶段检测**（本卡的核心设计）：扫除稍纵即逝，等 M5 收盘再提示最佳入场已跑掉，
故拆成两级时间精度：

- 阶段一（tick 级，不等收盘）：价格越过池 > MinPenetration×ATR(M5)、当前 bar
  累计 Delta 落在近 50 根**已收盘** bar 的 5%/95% 分位尾部、累计成交量 ≥ 中位数
  ×VolMultiple → 播预警音 + 画小三角 + 面板提示 + 池进入 WATCH
- 阶段二（M5 收盘判定）：收回池内 + ADR ≤ AdrPass + 收回过程出现吸收 → 确认并
  画完整信号；超时未收回 / 穿透过深 / ADR 过高 / 二次破位 → 作废

**强制去重**：`(poolId, direction, barIndex)` 作阶段去重键，声音再叠加 eventType。
不做这条的话 tick 级检测会疯狂响，指标直接不可用。

**无前视偏差**：分位数与中位数只取 `CurrentBar-1` 及更早的已收盘 bar。所有 bar
收盘处理走同一条 `while (_lastClosedProcessed < CurrentBar - 1)` 路径，历史回放
与实盘因此得到一致结果。

**三处实测确认（不凭记忆写）**：

- `AddAlert` 真实签名反射确认为 `ExtendedIndicator.AddAlert(string soundFile,
  string message)`（另两个重载带 `Color`，双平台类型不同，故用两参数版规避）
- 音效文件取 ATAS 自带的 `alert1.wav` / `alert3.wav` / `beep_2_1.wav`，两平台
  `<install>\Sounds\` 下均存在
- 绘图坐标用 `ChartInfo.PriceChartContainer.GetXByBar(bar,false)` /
  `GetYByPrice(price,false)`（反射确认签名），绘制层用 `DrawingLayouts.Final`
  —— 沿用 -28 的教训，订阅 Historical 会顶掉历史层绘制

**ATR 来源**：ATR(M5) 由本指标自算真实波幅均值（周期常量 14）；ATR(D1) 从 M5 流
按图表时区聚合成日桶后再算，避免为了一条日线 ATR 去引入第二数据序列。两个周期
是内部常量而非设置项 —— 任务卡的参数清单里没有它们，不擅自加。

**双平台**：`AtasBridge.Platform.csproj` 已加 `<Compile Include="..\AtasBridge\
SweepMarker.cs" />`（该项目 `EnableDefaultCompileItems=false`，漏加会导致
Platform 版 dll 里没有 SweepMarker 而 ATAS X 版有）。`AtasBridge.csproj` 走默认
glob 自动纳入，只补了一段说明注释。

**编译验证**：双平台各 0 错误。用 MetadataReader 校验产物：两个 dll 都含
`AtasBridge / AtasLiquidations / SweepMarker` 三个类型；ATAS X 版不引用
PresentationCore（`SeriesColor` = `System.Drawing.Color`），Platform 版引用
（`SeriesColor` = `System.Windows.Media.Color`），证明 `#if ATAS_PLATFORM`
分支双向都真的走到了。

**未完成**：ATAS 内的运行时验证（加载、池线绘制、3 天回放、声音、M15 周期警告）
尚未执行 —— 需要覆盖指标目录并重启 ATAS，而 Sea 的 ATAS 正在使用中，未获授权
不动。部署命令与待验证清单见 `reports/SWEEP_9G_20260812.md`。

---

## v2026.08.12-2（2026-08-12，SweepMarker：界面中文化 + 关于页使用说明）

### 已完成的运行时验证（承接 -1）

Sea 关闭 ATAS 后完成部署，并在 ATAS X 里加载成功，截图确认：

- **验证 2 通过**：指标出现在 `Setups` 分组，加载到 BTCUSDT@BinanceFutures M5
  图不报错，设置面板 6 个分组顺序正确
- **验证 3 通过**：池线正常绘制，面板显示 `Pools: BSL 15 / SSL 15`，共 30 条，
  数量合理（不满屏也不是一条没有）；等高等低合并生效，线右端出现
  `x2 x3 x4 x5 x8 x9 x13` 标注，`>=2` 的线明显加粗

### 由截图发现的调参问题（非缺陷）

有一条池标注 `x13`，即 13 个摆动点被合并成一条"等高/等低"。这不可能是真的
双顶双底，说明 `EqualTolerance x ATR(D1)` 容差偏宽：BTC 日线 ATR 约 1500~2000，
乘 0.1 得 150~200 美元，在 M5 尺度上把一段震荡区里十几个不同摆动点糊成了一条。
默认值保持 0.1 不改（任务卡指定），但参数说明里写明"普遍出现 x8 以上说明太宽，
建议 0.03~0.05"，并写进关于页的上手建议。

### 界面中文化

Sea 明确要求参数用中文，故本版把**面向用户的字符串**全部改为中文：
`DisplayName` / `GroupName` / `Description`、图上信号标签、左上角面板文字、
作废原因（鼠标悬停显示）。分组名改为 `1 流动性池` / `2 扫除检测` / `3 确认` /
`4 交易` / `5 显示` / `6 声音`。

**代码注释仍保持英文 ASCII** —— v5.1 那条约定的真实动因是"PowerShell 编码
损坏中文注释"，针对的是注释与编辑方式，不是 UI 字符串（`AtasLiquidations.cs`
的 `DisplayName` 本来就是中文）。文件因此变成 UTF-8 无 BOM，与
`AtasBridge.cs` / `AtasLiquidations.cs` 一致（三者实测均无 BOM，Roslyn 默认按
UTF-8 读取）。文件头加了 ENCODING NOTE 说明：编辑此文件要用 UTF-8 编辑器或
scp 传输，**不要经 PowerShell 管道**。

### 关于页使用说明

`[Description]` 从一行扩成完整中文使用指南，ATAS 指标窗口的"关于"页直接显示。
含 7 节：做什么 / 必须 M5 / 图上各元素含义 / 两阶段怎么配合 / 三个核心判据 /
上手建议 / 注意事项。

### 构建脆弱点修复（顺带）

`AtasBridge.Platform.csproj` 里 `System.Drawing.Common` 与 `PresentationCore`
的 HintPath 原本把版本号写死成 `Microsoft.WindowsDesktop.App\10.0.10`。本会话
期间机器上的 .NET 被更新（WindowsDesktop.App 10.0.10 -> 10.0.11，SDK
10.0.302 -> 10.0.303），该目录随之消失，Platform 版构建立刻 `MSB3245` 找不到
程序集，紧接着 `CS0234 命名空间 System.Windows 中不存在 Media`。改为通配
`10.*` 由 MSBuild 求值时展开，下次打补丁不会再断。已在 csproj 注释里写明失效
模式与限制（若同时存在多个 10.x 运行时会因同名引用报 CS1704，届时需收窄）。

### 验证

双平台 `Clean,Compile` 均 exit 0、0 错误（仅 `AtasBridge.cs` 既有的 11 个
CS8602/CS0618 警告）。产物字符串校验：`[Description]`/`[Display]` 的中文以
UTF-8 存在自定义特性 blob 中，代码内字符串常量以 UTF-16 存在 #US 堆中，
两类分别按对应编码搜索全部命中、无乱码替换符。

---

## v2026.08.12-3（2026-08-12，SweepMarker：修零信号 bug + 中文字体）

Sea 回放最近 3 天，**一个信号、一声提示都没有**。ATAS Platform 日志里
`[SweepMarker]` 命中 **0 行**（也没有任何 `EX at`），面板显示
`确认 0 / 作废 0`、`最近事件 暂无`。池线画得好好的 —— 这个组合直接指向病灶。

### Bug A：阶段一在历史 K 线上从来没跑过（零信号的直接原因）

原代码：

```csharp
while (_lastClosedProcessed < CurrentBar - 1)
    ProcessClosedBar(++_lastClosedProcessed);
if (bar == CurrentBar)
    RealtimeStageOne(CurrentBar);
```

历史计算时 ATAS 以 `bar = 0..N` 逐根回调，而 `CurrentBar` 一开始就等于 N。
于是 `bar == CurrentBar` **只在最后一次回调成立**，阶段一在整张图上只被
评估了 1 根 K。池照常构建（所以池线看起来完全正常），但没有任何池进入
WATCH，阶段二也就无从确认或作废 —— 表现就是"画得好看但永远不出信号"。

### Bug B：`ClosedStats()` 用了未来数据（前视偏差）

同一段里 `while` 的上界也是 `CurrentBar`，导致第一次回调就把所有 K 一次性
收盘处理完；而 `ClosedStats()` 的取样上界同样写的 `CurrentBar - 1`，
于是分位数/中位数是**在整张图（含被评估K之后的未来K）上算的**。

这正是任务卡实现要求 3 明确警告的前视偏差。更麻烦的是它只在复盘时暴露：
实盘 `CurrentBar` 就是当前 K，看不出问题。**上一版报告里"复盘与实盘一致"
的结论因此是错的**，已在报告中更正。

### 修法

一切以**正在计算的那根 `bar`** 为基准，不再引用 `CurrentBar`：

```csharp
while (_lastClosedProcessed < bar - 1)
    ProcessClosedBar(++_lastClosedProcessed);
_soundsAllowed = _historyDone && bar >= CurrentBar;
StageOne(bar);
if (bar >= CurrentBar) _historyDone = true;
```

`ClosedStats(int evalBar, ...)` 增加参数，上界改为 `evalBar - 1`。
这样实盘（每 tick 以 `bar == CurrentBar` 反复回调）与历史/复盘（`bar` 逐根
前进）真正共用一条路径、同样的输入、同样的结果。

### 声音门控（顺带修正的设计缺陷）

Bug A 掩盖了另一个问题：修好之后，把指标加到 3 天历史图上会**一次性爆出
一串早已过期的提示音**。新增 `_soundsAllowed = _historyDone && bar >= CurrentBar`：
首次全量遍历期间静音，遍历完成后（实盘 tick 与 ATAS 回放逐根推进时）才响。
去重键仍照常消费，避免某根被静音的 K 在后续重算时补响。

### 中文字体（方框问题）

Sea 反馈面板中文变方框。**既不是 ATAS 不支持中文，也不是系统缺字体** ——
是字体写死成了 `Arial`，而 Arial 无 CJK 字形，`OFT.Rendering` 的
`RenderFont` **不做字体回退**。`AtasBridge` / `AtasLiquidations` 也用 Arial，
但它们只画 ASCII，所以这个坑一直没被踩到。ATAS 自己的设置窗口能正常显示
中文，是因为那层 WPF UI 会回退，自定义绘制层不会。

本想做运行时探测，但 `System.Drawing.Text.InstalledFontCollection` 在 ATAS X
能编过、在 ATAS Platform 直接
`CS0012: IPointer<> 在未引用的程序集 System.Private.Windows.Core 中定义`
—— Platform 引用的是 WindowsDesktop 那份 `System.Drawing.Common`，会牵出该
内部程序集。**改为开放成设置项「面板字体」**，默认 `Microsoft YaHei UI`。
这台机器是精简版 Windows IoT LTSC 镜像，实测**只有 `Microsoft YaHei UI`，
没有 `Microsoft YaHei` / `SimSun` / `SimHei`**（全系统仅 153 个字体），
所以写死任何一个具体中文字体名都可能在别的机器上再次变方框，开成设置项
让用户自己改最稳。

---

## v2026.08.12-4（2026-08-12，SweepMarker：声音终于响 + 音效可配置）

-3 修好了零信号，Sea 复盘拿到 **80 条事件、0 异常**，面板中文正常、
黄三角/四条线/灰X 都画出来了 —— 但**仍然一声都没响**。

### 根因：`AddAlert` 的文件名不能带 `.wav`

-3 传的是 `"alert1.wav"`，既不抛异常也不出声。线索来自 Sea 截图里另一个
指标（5F 的 Absorption）的「警报文件」下拉框：里面列的全是**不带后缀**的
名字（`xishouAbs` / `geiger` / `tap` / `Windows Ding`）。也就是说该下拉框
是枚举 `<install>\Sounds\*.wav` 后**去掉扩展名**填充的，平台自己会补
`.wav`。传 `"alert1.wav"` 等于让它去找 `alert1.wav.wav`，找不到就静默失败。

改为传裸文件名 `alert1` / `alert3` / `beep_2_1`（两个平台的 Sounds 目录里
都有），并**开放成三个设置项**「预警音文件 / 确认音文件 / 作废音文件」，
方便 Sea 换成自己的音效 —— 该机 Platform 的 Sounds 目录里有一批自定义音效
（`xishou` `xishouAbs` `gengdan` `laisheng` `maidan` `qifei` `qingcang`
`shiheng` `zapan` `chaojidadan` `chaojimaidan` `daemaidan` `daeqingsuan`
`Delta yichang` `TPOjujue`）。

### 顺带去掉 `_historyDone` 门控

-3 的门控是 `_historyDone && bar >= CurrentBar`。若 ATAS 在市场回放时对每
一步都重算整个序列，`bar == 0` 会把 `_historyDone` 重置，于是**永远静音**
—— 这也可能是零声音的第二个原因（与文件名问题叠加）。改为只判右边缘
`bar >= CurrentBar`：历史遍历时该条件仅在最后一次回调成立，所以加载 3 天
历史最多响最新那一根，不会机枪式补响；实盘与回放的新事件都在右边缘，照响。

### 新增声音诊断日志

`PlaySound` 现在会记录前 6 次的去向：派发时写
`sound dispatch evt=... file=... bar=... AlertsEnabled=...`，被右边缘门控
拦下时写 `sound suppressed (not right edge) ...`。若还是不响，这行日志能
直接区分"没走到派发"与"派发了但平台没出声"，不必再猜。

### -3 复盘实测数据（任务卡验证 5）

ATAS Platform 日志 `app_20260812.log`，`[SweepMarker]` 命中 80 行、
**0 个异常**：

| 事件 | 数量 |
|---|---|
| 预警（阶段一） | 41 |
| 确认 | 7（做多 5 / 做空 2） |
| 作废 | 31 |
| 不画信号（止损过近） | 1 |

作废原因分布：**穿透过深 29**、超时未收回 1、ADR过高 1、止损过近 1、
二次破位 0。

7 个确认里 3 个标「盈亏比不足」（1.8 / 2.0 / 1.5），4 个达标
（2.3 / 2.7 / 2.7 / 2.4）。ADR 全部落在 0.13~0.48，即都在
`AdrPass=0.8` 以内，没有触发"弱信号"档。

**结论：29/31 的作废都是"穿透过深"，这就是 Sea 看到"满屏灰色"的原因。**
病灶不是阈值 `MaxPenetration=1.5` 太小，而是 `MinPenetration=0.05` 太浅
—— 0.05×ATR(M5) 在 BTC 上只有几美元，价格稍微一探头就进 WATCH，而这类
浅探头大多是真突破的起点，随后自然穿透过深。调参方向见报告。

---

## v2026.08.12-5（2026-08-12，SweepMarker：回放声音门控 + 面板居中 + TP2 取法修正）

三件事，全部由实测反馈驱动。

### 1. 回放没声音的真正原因（-4 的诊断日志立了功）

-4 加的诊断日志直接给出答案：

```
sound dispatch  : 0
sound suppressed: 18
  sound suppressed (not right edge) evt=alert   bar=54  CurrentBar=8539
  sound suppressed (not right edge) evt=confirm bar=131 CurrentBar=8539
```

**ATAS 市场回放时 `CurrentBar` 始终等于整个已加载序列的末尾**（8539 根 M5
≈ 30 天），不是回放光标位置；而且每个回放步都在重算整个序列（18 条 = 3 次
全量重算 × 每次 6 条诊断上限）。所以 `bar >= CurrentBar` 在事件所在的位置
**永远不成立**，-3 和 -4 两版的门控都注定静音。

门控不再依赖 `CurrentBar`，改为跟踪本实例**处理过的最大 bar**：

```csharp
_soundsAllowed = SoundOnHistory || (_firstPassDone && bar >= _maxBarProcessed);
if (bar > _maxBarProcessed) _maxBarProcessed = bar;
```

- 首次遍历：每根都是新的，但 `_firstPassDone` 仍为 false → 静音（不会把刚
  加载的 30 天历史全部补响）
- 之后对同一序列的重算：除最后一根外 `bar < _maxBarProcessed` → 静音
- 真正新增的 bar（实盘、或回放步进追加）：`bar >= _maxBarProcessed` → 出声

`_maxBarProcessed` 与 `_firstPassDone` **故意不被 `ResetAll()` 清除** ——
否则每次重算都会重新武装历史补响。

另新增设置项**「历史与回放也播声音」**（默认关）：打开后完全绕过门控，
复盘验证去重逻辑时用。这也是 -3/-4 缺失的东西 —— 之前根本没有办法在回放里
听到声音。

### 2. 面板从左上角改为顶部居中

Sea 反馈左上角的框遮挡盘面。改为在容器区域内水平居中、贴顶 4px，每行文字
也在框内居中。区域取不到时依次退回 `ClipBounds` → 画布尺寸。

顺带修一个读起来像 bug 的地方：面板此前只显示「今日」计数，而"今日"会在
遍历序列时逐日重置，30 天图上只反映最后一天 —— 于是出现「最近事件：确认
做多 63801」而「今日：确认 0 个」这种自相矛盾的显示。现在同一行追加
**「全图累计」**，复盘时看这个。

### 3. TP2 取法修正（灰线问题的真因，而且此前给的调参建议是错的）

Sea 按建议把「等高等低合并容差」调小后，**灰线没减少**，同一笔 63801 做多的
盈亏比反而从 2.9 掉到 1.7。参数是生效了 —— 但方向错了：容差调小 ⇒ 池更多更
密 ⇒ 「对面最近的池」离入场更近 ⇒ TP2 更近 ⇒ 盈亏比更低 ⇒ 标灰的更多。

任务卡原文「TP2 = 上方最近的摆动高点」，此前实现为字面上最近的一个。现改为
**取最近的、且距离足够达到 MinRR 的那个池**；一个都不合格时按卡片既有规定
退回 3R（卡片对"若无可用 BSL"已有此约定，此处把"可用"理解为"能当目标用"）。

这样"灰线"就只剩下真正该灰的情形：作废（灰 X）与止损距离越界。

### 部署

双平台 0 错误，已部署（ATAS X 120320 字节 / Platform 120832 字节，
版本串、新设置项、新面板文案均校验通过）。

---

## v2026.08.12-6（2026-08-13，SweepMarker：SL/TP 线加文字 + 多空胜率统计）

-5 验证通过（Sea：声音响了、面板居中不挡盘、盈亏比回到 2.3~2.9，TP2 修正
见效）。本版按 Sea 要求加两项。

### 1. SL / TP1 / TP2 线加文字

此前只有入场线右端有标签，止损与两个目标要靠价格轴对着看。现在三条线右端
各自标出名称与价格：`止损 63627.4` / `TP1 63954.5 (1.5R)` /
`TP2 64147.0 (2.9R)`，颜色与线一致，带半透明黑底保证可读。
新增开关「显示线条文字」（默认开），线多时可关掉减少拥挤。
标签绘制抽成 `DrawRightLabel()`，入场标签也走同一条路径。

### 2. 多空胜率统计

新增结果追踪：每个已确认信号在其后的每根收盘K上判定是否触及 TP1 / 到达
TP2 / 打到止损（`TrackOutcomes()`，只看信号K之后的K，不影响信号本身的判定）。

面板追加两行：

```
做多：已结 12 笔　触及TP1 58%(7)　到达TP2 33%(4)　止损 67%(8)　进行中 2 笔
做空：已结 8 笔　 触及TP1 50%(4)　到达TP2 25%(2)　止损 75%(6)
```

三条口径约定，都写在设置项说明里：

- **分母只算"已结"**（到达 TP2 或打到止损）。还挂在 TP1 上的算"进行中"，
  不计入分母 —— 否则未结束的单子会把胜率算虚。
- **同一根K同时触及止损与目标时按止损计**。OHLC 无法还原盘中顺序，做命中率
  表就该往保守一侧取整：可能低估，绝不高估。
- 打到止损前曾触及 TP1 的，`触及TP1` 计数里仍然算 —— 所以信号标签会出现
  「触TP1后止损」这种状态，便于判断是否该在 TP1 减仓。

信号标签同时追加结局：`已到TP2` / `已止损` / `触TP1后止损` / `已触TP1`，
复盘时不用逐条追线也能一眼看出结果。

新增开关「显示胜率统计」（默认开）。

### 说明

胜率统计不在任务卡 9G 的范围声明里（卡片排除的是评分分级/池质量打分/状态
矩阵等），是 Sea 明确要求新增的。统计只读已收盘K，不引入任何前视。

---

## v2026.08.13-1（2026-08-13，SweepMarker：MFE/期望值统计 + TP2 距离上限）

-6 的胜率统计跑出第一份真实数据（Sea 回放约 30 天 M5，全图累计确认 38 /
作废 182）：

| | 已结 | 触及TP1 | 到达TP2 | 止损 |
|---|---|---|---|---|
| 做多 | 20 | 50%(10) | **40%(8)** | 60%(12) |
| 做空 | 18 | 39%(7) | **6%(1)** | **94%(17)** |

按平均盈亏比 2.5R 估算期望：做多 **+0.40R/笔**，做空 **−0.81R/笔**。
**整体的负期望全部来自做空一边**，做多本身是正期望的。

最有信息量的一条：触及 TP1 之后能走到 TP2 的比例，做多 8/10 = 80%，
做空只有 1/7 = 14% —— 空单摸到 TP1 之后几乎都被打回来。

### 1. 新增 MFE / MAE 与期望值统计

只看命中率无法判断"该不该把目标放这么远"。新增每笔信号的
**最大浮盈 MaxFavR / 最大浮亏 MaxAdvR**（以 R 为单位，逐根收盘K更新，
只看信号K之后的K）。面板统计行追加：

```
做空：已结 18 笔  触及TP1 39%(7)  到达TP2 6%(1)  止损 94%(17)
      期望 -0.81R  平均最大浮盈 1.1R（亏损单 0.9R）
```

判读方式：**若亏损单的平均最大浮盈明显低于 1.5R，说明 TP1 就设远了**，
问题在离场计划而不是入场信号。

同时新增 `LogOutcome()`，每笔结算写一行日志，便于离线分析：

```
结果 做空 入场=65025 R=143.5 盈亏比=2.8 结局=触TP1后止损
     最大浮盈=1.62R 最大浮亏=1.00R 用时=9根 ADR=0.13
```

### 2. TP2 距离上限（新增设置项「TP2最大R(超出则退回3R)」，默认 5）

回放里出现过一个 **盈亏比 11.1** 的信号：`NearestUsableOppositePool()` 取的是
"最近的、且够 MinRR 的池"，但没有上限，于是选中了 11R 之外的池 —— 那不是
目标，是许愿。该笔触 TP1 后止损。现在超过 `MaxTp2R` 倍 R 时退回 3R。
设 0 表示不限制。

### 说明

期望值按"到 TP2 全部平仓、止损 -1R"的口径估算，是判断某个方向值不值得做的
最直接指标；单看命中率没有意义（2.5R 的盈亏比下 40% 命中就是正期望，
而 94% 止损无论盈亏比多高都救不回来）。

统计仍只读已收盘K，不引入前视。
