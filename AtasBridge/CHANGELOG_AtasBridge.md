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
