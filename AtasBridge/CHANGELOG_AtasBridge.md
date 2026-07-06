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
