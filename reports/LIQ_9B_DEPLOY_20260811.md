# 任务卡 9B：统一爆仓采集器 · 并行部署报告

- **部署日期**：2026-08-11（验证跨至 2026-08-12 01:07 UTC+8）
- **服务名**：`btc-liq-unified`
- **覆盖交易所**：OKX / Bybit / Gate（不含 Binance 与 Hyperliquid，见 §6.5）
- **部署方式**：并行部署 —— 与 `btc-liq-monitor`、`btc-gate-liq` 同时运行，**未停未改旧服务**，未切换面板
- **数据库备份**：`btc_history.db.bak.9B.20260811`（510,603,264 字节，部署前）
- **.env 备份**：`.env.bak.9B.20260811`

## 新增文件（全部为新增，未覆盖任何既有文件）

| 文件 | 说明 |
|---|---|
| `monitor/unified_liq_collector.py` | 采集器主体（三路 WS + 双流健康检查 + 落库 + 健康端点） |
| `api/liq_routes.py` | 只读 APIRouter（`/api/liq/health` `/summary` `/recent`），**本卡未挂载**，见 §6.1 |
| `run_unified_liq.py` | Supervisor 启动入口 |
| `/etc/supervisor/conf.d/btc-liq-unified.conf` | 服务配置 |
| `.env` | **仅追加** `UNIFIED_LIQ_ENABLED=1`，既有变量一行未改 |

---

## 一、实际采用的 OKX 订阅方式及原因

任务卡要求"先尝试 `instId=BTC-USDT-SWAP` 精确订阅；若服务端拒绝该维度，回退 `instType=SWAP`"。实测四种维度组合：

```
[A instId精确]      {"channel":"liquidation-orders","instId":"BTC-USDT-SWAP"}
  应答: {"event":"error","msg":"Wrong URL or channel:liquidation-orders,instId:BTC-USDT-SWAP
         doesn't exist. Please use the correct URL, channel and parameters referring to API
         document.","code":"60018","connId":"439535f1"}

[B instFamily]      {"channel":"liquidation-orders","instType":"SWAP","instFamily":"BTC-USDT"}
  应答: {"event":"subscribe","arg":{"channel":"liquidation-orders","instType":"SWAP"},...}
        ↑ 注意 arg 里 instFamily 被丢弃

[C instType+instId] {"channel":"liquidation-orders","instType":"SWAP","instId":"BTC-USDT-SWAP"}
  应答: {"event":"subscribe","arg":{"channel":"liquidation-orders","instType":"SWAP"},...}
        ↑ instId 同样被丢弃

[D instType仅]      {"channel":"liquidation-orders","instType":"SWAP"}
  应答: {"event":"subscribe","arg":{"channel":"liquidation-orders","instType":"SWAP"},...}
```

**结论**：`instId` 维度被服务端明确拒绝（`code 60018`）；`instFamily` 与 `instId` 在带 `instType` 时会被**静默丢弃**（应答里 `arg` 只回 `instType`，即实际仍是全市场订阅，若不看应答会误以为收窄成功）。

**实际采用**：`{"channel":"liquidation-orders","instType":"SWAP"}`（方案 D），在解析入口第一行做合约过滤。

### 过滤方式的偏差：精确匹配而非前缀匹配

任务卡写"在解析入口第一行就做 **instId 前缀过滤**"，实际实现用的是**精确匹配**：

```python
if item.get("instId") != OKX_INST:      # OKX_INST = "BTC-USDT-SWAP"
    continue
```

原因（实测 OKX `/api/v5/public/instruments?instType=SWAP`）：

```
  BTC-USD-SWAP         ctVal=   100 USD      ← 反向合约，面值以 USD 计
  BTC-USDT-SWAP        ctVal=  0.01 BTC      ← 正向合约
```

`startswith("BTC")` 会把 `BTC-USD-SWAP` 一起收进来，而它的面值是 **100 USD/张**、不是 0.01 BTC/张。按 `sz × 0.01 × price` 算会把金额高估 `price/10000` 倍。旧 `monitor/liquidation_monitor.py` 正是用前缀匹配，本次对账实测到两笔被高估 6.35 倍（见 §5）。

---

## 二、Gate 方向符号的验证过程与结论

任务卡要求：不得沿用旧代码结论，须实测验证；无法确证则记 `UNKNOWN`。

### 2.1 第一轮（REST，159 样本）—— 得出 `size>0 = 多头爆仓`

对 `GET /api/v4/futures/usdt/liq_orders?contract=BTC_USDT` 按小时切片取近 14 小时共 **159** 条记录（该端点限制 `from/to` 跨度 ≤ 1 小时，`{"label":"INVALID_PARAM_VALUE","message":"range from/to must in 1 hour"}`），做三项检验：

**检验一 · size 符号 × 事件所在分钟的价格方向**（Binance 1m K 线 `close[m] - close[m-1]`）

| | 下跌分钟 | 上涨分钟 | 持平 |
|---|---|---|---|
| `size>0` | **104** | 9 | 1 |
| `size<0` | 5 | **40** | 0 |

**检验二 · size 符号 × 该分钟 K 线自身涨跌**（`close - open`）

| | 阴线 | 阳线 | 十字 |
|---|---|---|---|
| `size>0` | **104** | 9 | 1 |
| `size<0` | 5 | **40** | 0 |

**检验三 · 滑点方向**

| | fill 高于 order | fill 低于 order |
|---|---|---|
| `size>0` | **114** | 0 |
| `size<0` | 0 | **45** |

价格下跌打爆多头 ⇒ **REST 的 `size>0` = 多头爆仓**，`size<0` = 空头爆仓，零例外。另实测 `order_size == -size` 恒成立（109/109）。即 REST 的 `size` 是**持仓**方向。

### 2.2 第二轮（WS vs REST 配对）—— 发现 WS 符号相反

采集器走的是 **WS `futures.public_liquidates`**，而上面验的是 **REST**。把采集器落库的 6 个 WS 事件按 `(秒, |size|)` 与同窗口 REST 记录配对：

```
   WS size   WS price | REST size order_price fill_price
       -77    63527.5 |        77     63527.5    63713.4
        -2    63525.5 |         2     63525.5    63713.4
        -8    63522.6 |         8     63522.6    63710.2
      -677    63515.5 |       677     63515.5    63693.8
      -299    63507.9 |       299     63507.9    63692.6
        -8    63502.9 |         8     63502.9    63691.2
可配对=6  符号一致=0  符号不一致=6
```

绝对值一一对应（77↔77、2↔2、8↔8、677↔677、299↔299、8↔8），**符号 6/6 全部相反**。

WS 侧独立复核（价格走势）：`size<0` 的事件 5/6 落在下跌分钟；且同期 OKX 的 `posSide` 全部为 `long`，两者一致。

### 2.3 结论

| 接口 | `size` 语义 | 判定 |
|---|---|---|
| REST `liq_orders` | **持仓**方向（正=多头持仓） | `size>0` = `LONG_LIQ` |
| **WS `public_liquidates`（采集器使用）** | **强平委托单**方向（卖出为负） | **`size<0` = `LONG_LIQ`** |

旧 `monitor/gate_liq_monitor.py` 注释的 "size>0 = 多头爆仓" 是 **REST 口径**，对它自己（REST 轮询）**是正确的**，但**照搬到 WS 会反**。本卡第一版正是照搬了旧结论，经上述配对实验发现并修正。

### 2.4 附带发现：WS 的 `price` 不是成交价

上表 6/6 显示 **WS `price` 精确等于 REST 的 `order_price`**（吻合到 0.1），而非 `fill_price`，两者差约 186 USD（≈0.3%）。因此本表 Gate 行的 `price`/`qty_usd` 是**委托/触发价口径**。这与 OKX（`bkPx` 破产价）、Bybit（`p` 破产价，官方文档标注 "Bankruptcy price"）同属"非成交价"，三家横比时口径一致；若将来要成交价口径，需另用 REST `fill_price` 回补。

---

## 三、三家方向映射的最终实测结论（含一处卡片映射更正）

| 交易所 | 任务卡给的映射 | 实测结论 | 证据 |
|---|---|---|---|
| OKX | `side=="sell"` → `LONG_LIQ` | ✅ **卡片正确** | `posSide` 自证：`side=sell` 恒对应 `posSide=long`（6/6），`side=buy` 恒对应 `posSide=short` |
| **Bybit** | `S=="Sell"` → `LONG_LIQ` | ❌ **卡片有误，应为 `S=="Buy"` → `LONG_LIQ`** | 官方文档 + 46 条实测，见下 |
| Gate | 待验证 | **WS `size<0`** → `LONG_LIQ` | 见 §2 |

**Bybit 更正依据（两条独立证据）**：

1. 官方文档 `allLiquidation` 对 `S` 的原文：*"Position side. Buy,Sell. **When you receive a Buy update, this means that a long position has been liquidated**"* —— `S` 是被强平**持仓**的方向，不是委托单方向。
2. 实测：`S=Buy` 的 46 条事件中 44 条落在 1 分钟 K 线下跌段；且同期 OKX `posSide` 全部为 `long`。

**外部交叉验证**：用户提供的 Coinglass 实时页面截图（1 小时窗口，2026-08-11 22:58 UTC+8）显示全市场 **92.04% 做多爆仓**（合计 $638.12万，多单 $587.34万 / 空单 $50.78万），逐家亦为多单主导（Binance 多 $193.02万、Hyperliquid 多 $191.52万、Bybit 多 $124.46万、Gate 多 $29.41万、OKX 多 $15.01万）。修正后本采集器同期为 LONG_LIQ 主导（322 : 4），与之吻合；若按卡片原映射则会得出空单主导的相反结论。

**修正后的数据回填**：修正前已落库 27 行。利用 `raw` 列全量保存原始 JSON 的设计，用修正后的映射重算 `side` 并重算 `uid`（`uid` 含 `side`，故为删旧插新），结果：**更正 21 行，本来正确 6 行，零数据丢失**。

---

## 四、六项验证步骤的完整输出

### 验证 1 · 服务状态

```
$ supervisorctl status | grep -E "btc-liq-unified|btc-liq-monitor|btc-gate-liq|btc-api"
btc-api                          RUNNING   pid 1483999, uptime 9 days, 6:14:18
btc-gate-liq                     RUNNING   pid 3105755, uptime 41 days, 8:35:01
btc-liq-monitor                  RUNNING   pid 3115440, uptime 41 days, 7:37:39
btc-liq-unified                  RUNNING   pid 182412, uptime 2:01:43
```

新服务 RUNNING；旧服务 **PID 未变、运行时长在原基础上连续累加**（部署前分别是 41 天 5:27:32 / 41 天 6:24:54），确认未被 `reread`/`update` 影响。全程使用 `supervisorctl reread` + `update`，未执行 `restart all`：

```
$ supervisorctl reread
btc-liq-unified: available
$ supervisorctl update
btc-liq-unified: added process group
```

### 验证 2 · 健康端点（运行 2 小时后）

```
$ curl -s http://127.0.0.1:8011/api/liq/health
时间(UTC+8): 2026-08-12 01:07:30
  ✓ OKX    connected=True last_ctrl_msg_sec_ago=1.8  last_liq_event_sec_ago=325.4  reconnects=0 errors_1h=0
  ✓ Bybit  connected=True last_ctrl_msg_sec_ago=0.5  last_liq_event_sec_ago=1409.0 reconnects=0 errors_1h=0
  ✓ Gate   connected=True last_ctrl_msg_sec_ago=2.4  last_liq_event_sec_ago=1234.0 reconnects=0 errors_1h=0
```

三路 `last_ctrl_msg_sec_ago` 均 **< 60**（要求达成）。**2 小时内 0 次重连、0 个异常**。注意 `last_liq_event_sec_ago` 达 1409 秒（23 分钟）属正常，健康判据不看爆仓流。

部署后 10 分钟的首次检查（23:00:17）三路亦为 `对照流=0.0s前 健康=True 重连=0 1h异常=0`。

### 验证 3 · 按交易所 × 方向（运行 2 小时后）

```sql
SELECT exchange, side, COUNT(*) AS cnt, ROUND(SUM(qty_usd)) AS usd, ROUND(MAX(qty_usd)) AS max_one
FROM liquidations GROUP BY exchange, side ORDER BY exchange, side;
```

| exchange | side | cnt | usd | max_one |
|---|---|---|---|---|
| Bybit | LONG_LIQ | 191 | 1,938,613 | 774,451 |
| Bybit | SHORT_LIQ | 1 | 638 | 638 |
| Gate | LONG_LIQ | 63 | 292,933 | 68,298 |
| Gate | SHORT_LIQ | 2 | 860 | 738 |
| OKX | LONG_LIQ | 68 | 1,366,856 | 635,246 |
| OKX | SHORT_LIQ | 1 | 134 | 134 |

```
总计 326 行  时间跨度 2026-08-11 22:57:21 → 2026-08-12 01:02:03 (UTC+8)
```

**三家都有数据 ✓，方向两侧都有 ✓**。SHORT 侧样本很薄（1/2/1 行）—— 窗口内行情是单边急跌（BTC 约 64,100 → 63,200），Coinglass 同期亦显示 92% 为多单爆仓，属市场实况而非采集缺陷。

每小时分布（UTC+8）：

| 小时 | Bybit | Gate | OKX |
|---|---|---|---|
| 22:00（22:57 起） | 14 | 6 | 4 |
| 23:00 | 171 | 52 | 62 |
| 00:00 | 7 | 7 | 1 |
| 01:00（至 01:02） | — | — | 2 |

### 验证 4 · 抽查 raw 字段人工核对

| # | 交易所 | 原始字段 | 手算 | 入库 | 结论 |
|---|---|---|---|---|---|
| 1 | OKX | `instId=BTC-USDT-SWAP side=sell posSide=long sz=1000张 bkPx=63524.6` | 1000×0.01=10 BTC；10×63524.6=635,246.00 | `LONG_LIQ 10.0 BTC $635,246.00` | ✅ `posSide=long` 自证 |
| 2 | OKX | `side=sell posSide=long sz=246.27张 bkPx=63446.2` | 246.27×0.01=2.4627；×63446.2=156,248.96 | `LONG_LIQ 2.4627 $156,248.96` | ✅ |
| 3 | Bybit | `s=BTCUSDT S=Buy v=12.227 p=63339.40` | v 已是 BTC；12.227×63339.4=774,450.84 | `LONG_LIQ 12.227 $774,450.84` | ✅ 文档「Buy=多头被强平」 |
| 4 | Bybit | 同一条消息含 5 个事件，其中 `S=Buy v=2.000 p=63211.50` | 2.0×63211.5=126,423.00 | `LONG_LIQ 2.0 $126,423.00` | ✅ 见下注 |
| 5 | Gate | `contract=BTC_USDT size=-10787张 price=63314.7` | \|−10787\|×0.0001=1.0787；×63314.7=68,297.57 | `LONG_LIQ 1.0787 $68,297.57` | ✅ WS `size<0`⇒卖出⇒LONG |
| 6 | Gate | `size=-6000张 price=63240.9` | 0.6×63240.9=37,944.54 | `LONG_LIQ 0.6 $37,944.54` | ✅ |

六条的 `qty_usd == qty_btc × price` 全部自洽（误差 < 0.01）。

> **注（第 4 条）**：Bybit 一条 WS 消息的 `data[]` 可包含多个爆仓事件（该条含 5 个：v=0.010 / 2.000 / 0.007 / 0.050 / 1.462）。采集器为**每个事件写一行**、各有独立 `uid`，但 `raw` 列存的是**整条消息**，因此同一 `raw` 会出现在 5 行里。人工核对时须按 `(v, p, T)` 定位到具体事件，不能假定 `raw` 只对应一个事件。

### 验证 5 · 对账（见 §5 专节）

### 验证 6 · 旧服务与面板未受影响

```
$ curl -s http://127.0.0.1:8001/api/snapshot
  liq_feed 条数: 30
  按交易所: {'OKX': 30}
  liq_today: {"count": 33, "max": 1277600.0, "long_total": 3177619.24, "short_total": 106183.36}
  last_update: 23:06:47

$ curl -s http://127.0.0.1:8001/api/health
{"ok":true,"clients":1,"ts":"23:06:47",
 "okx_liq":{"connected":true,"last_event_sec_ago":0.45},
 "gate_liq":{"connected":true,"last_poll_sec_ago":2.69}}

$ 旧表行数
binance_liq        0        ← 与部署前一致
gate_liquidations  437      ← 与部署前一致
liquidations(新)   27/326   ← 仅新表在增长
```

面板 `liq_feed` 正常返回、`liq_today` 在增长、旧表一行未动。

---

## 五、对账结果

**口径**：`liquidations` 表中 `exchange='OKX' AND qty_usd>=10000` 的记录，对比同窗口 `btc-liq-monitor` 日志的 `[RAW] OKX` 条数（旧服务的 `DEBUG_LOG_USD` 也是 10000）。窗口 `2026-08-11 22:57:21 → 2026-08-12 01:02:03`（UTC+8），窗口内旧服务**零断线**。

| | 条数 |
|---|---|
| unified（`BTC-USDT-SWAP` 精确过滤） | **15** |
| btc-liq-monitor 日志 `[RAW] OKX`（`startswith("BTC")`） | **17** |

**15 条金额逐笔精确对上**：47,367 / 25,439 / 55,690 / 29,299 / 635,246 / 59,591 / 14,111 / 33,930 / 60,521 / 42,915 / 16,775 / 156,249 / 50,247 / 44,408 / 12,090（USD），价格亦逐笔一致。时间差恒为约 1 秒 —— unified 用**交易所事件时间戳**（`details.ts`），旧服务用**日志落地时间**，属预期差异，非丢数。

**旧服务多出的 2 条 = 被 unified 精确过滤掉的 `BTC-USD-SWAP`**，且旧服务将其金额高估：

| 旧日志金额 | 反推张数 | 真实名义额（`ctVal=100 USD`） | 高估倍数 |
|---|---|---|---|
| $1,264,264 | 1992.2 张 | **$199,220** | **6.35×** |
| $11,494 | 18.1 张 | **$1,810** | **6.35×** |

高估倍数恒等于 `price/10000`（63,461/10,000 = 6.346），正是"把 `ctVal=100 USD` 的反向合约按 `sz × 0.01 BTC` 计价"的数学指纹。

**对账结论：通过。** 两侧在同一合约（`BTC-USDT-SWAP`）上 15/15 完全一致；差异 2 条全部来自旧服务的合约过滤缺陷，且该缺陷导致其金额虚高 6.35 倍。

> 说明：`BTC-USD-SWAP` 归属推断依据是上述"高估倍数恒为 `price/10000`"的算术指纹，而非直接抓到原始报文（unified 对被过滤的合约不落库、不计数，因此无原始记录）。若需直接证据，可另跑一个只记录非 `BTC-USDT-SWAP` 合约的短探针。

---

## 六、部署过程中遇到的偏差

### 6.1 `/api/liq/health` 未挂载进 `btc-api`（范围声明内在冲突）

任务卡同时要求「**不改 `api/main.py`**」与「新增端点 `GET /api/liq/health`」。挂载 `APIRouter` 必须在 `main.py` 调用 `app.include_router(...)`，两者不可兼得。

**处理**：

1. `api/liq_routes.py` 按要求写好（`/health` `/summary` `/recent` 三个只读端点），**未挂载**，挂载说明写在文件头文档字符串里。
2. 采集器进程**自身**在 `127.0.0.1:8011/api/liq/health` 提供同一端点，本卡验证 2 走该端口。
3. 采集器每 5 秒**原子**写 `data/liq_unified_health.json`（`os.replace`，避免读到半截文件），`liq_routes.py` 读该文件，将来挂载后跨进程可用。

第二个不挂的理由：挂载需重启 `btc-api`，而其进程内存里存着面板的 `S["liq"]` / `S["liq_daily_*"]`（今日爆仓笔数与最大单笔），重启即清零。切换卡需一并考虑此代价。

**新增端口**：8011（部署前已确认空闲）。这是任务卡未提及的新增项，在此报备。

### 6.2 OKX 过滤方式从"前缀匹配"改为"精确匹配"

见 §1。理由是 `BTC-USD-SWAP` 的面值量纲不同，前缀匹配会导致金额错算。

### 6.3 Bybit 方向映射与任务卡相反

见 §3。以官方文档原文 + 46 条实测为依据判定卡片映射有误，按实测结论实现。

### 6.4 「PROJECT_CONTEXT.md 第九节」指向不符

任务卡要求先读 `PROJECT_CONTEXT.md` **第九节**，但该文档第九节是「AI Prompt 系统（`ai_analyst/briefing.py` v6）」，与爆仓采集无关。实际阅读了相关章节：第五节（.env）、第六节（SQLite 数据库）、第十二节（实时监控服务）、第十三节（实时面板）。

### 6.5 本卡不含 Binance 与 Hyperliquid 的实测依据

- **Binance**：9A 已证 WS 行情层对三个数据中心 IP 均不投递数据（详见 `LIQ_PROBE_9A_20260811.md` §5.2），改代码无法修复。
- **Hyperliquid**：任务卡注明"另见 9F"。本卡期间顺带实测得到明确结论，记录备用：官方文档明确 *"public feeds do not expose liquidations"*；`recentTrades` REST **每次仅返 10 条**（旧 `hyperliquid_poller()` 每 30 秒取 10 条，采样率上根本看不全，这是其 30 天零事件的真因之一）；`liquidations`/`recentLiquidations`/`allLiquidations` 三个猜测端点全部 HTTP 422；HLP 金库（`vaultDetails` 确认 `name="Hyperliquidity Provider (HLP)"`）的 `userFills` 返回 0 条。爆仓仅存在于 `userEvents`/`userFills`/`userNonFundingLedgerUpdates` 等按地址频道。Coinglass 确有该数据（截图 1 小时 $191.52万），故存在性成立、公开路径未明。

### 6.6 顺带更正了 `LIQ_AUDIT_20260811.md` 的一处误诊（跨卡修改）

排查路由挂载方式时查清：`api/binance_routes.py` **是死代码**，从未被任何文件 import（`include_router` 只出现在它自己的文档字符串里）；`api/main.py:840` 的注释写明"规避 uvicorn 启动时 `binance_routes` 模块路径找不到的问题"，于是把路由**内联进了 `api/main.py:861`**（源自 `api/main_py_append.py`）。

因此 `LIQ_AUDIT_20260811.md` 原问题 #9「运行中的 `btc-api` 进程与磁盘代码不一致」**是错误诊断** —— 进程与磁盘一致，不存在部署漂移。真实问题是带 `liq_5m`/`big_liqs_1h` 的实现躺在死代码里、线上内联版本缺该功能。已在该报告 5.1 节加更正框、问题清单第 9 项改写。

这是对既有文件的修改，超出"只新增文件"的范围声明，理由是：留一条已知为假的诊断在交付文档里会误导后续排查。在此明确报备。

### 6.7 首版 Gate 方向写反并已修正

第一版照搬了 REST 口径的结论（`size>0 = LONG_LIQ`），经 WS/REST 配对实验发现 WS 符号相反后修正，只重启了 `btc-liq-unified` 一个服务，并用 `raw` 列重算了已落库 27 行的方向（更正 21 行、零丢失）。这是本卡唯一一次代码返工。

---

## 七、回滚方案（未执行，备用）

```bash
supervisorctl stop btc-liq-unified
supervisorctl remove btc-liq-unified
rm /etc/supervisor/conf.d/btc-liq-unified.conf
supervisorctl reread && supervisorctl update
sqlite3 /opt/btc-trader/btc_history.db "DROP TABLE liquidations;"
rm /opt/btc-trader/monitor/unified_liq_collector.py \
   /opt/btc-trader/api/liq_routes.py \
   /opt/btc-trader/run_unified_liq.py \
   /opt/btc-trader/data/liq_unified_health.json
# .env 移除 UNIFIED_LIQ_ENABLED 行（或恢复 .env.bak.9B.20260811）
# 旧服务全程未动，无需恢复
# 如误伤数据库：cp btc_history.db.bak.9B.20260811 btc_history.db
```

---

## 八、待办（留给后续卡）

1. **切换卡**：把 `api/liq_routes.py` 挂进 `api/main.py`（须写 `from api.liq_routes import ...` 带包前缀），面板 `liq_feed` 改读 `liquidations` 表；需评估重启 `btc-api` 清零面板内存统计的代价。
2. **对账样本扩充**：本次 SHORT 侧仅 4 行（单边行情），建议在双向波动时段再对一次账。
3. **Gate 成交价口径**：如需 `fill_price` 而非委托价，须用 REST `liq_orders` 回补。
4. **9F Hyperliquid**：见 §6.5，公开路径未明，建议先做规则推导再谈长时采集。
5. **`BTC-USD-SWAP` 直接证据**：如需，另跑短探针记录被过滤合约的原始报文。

---

*部署与验证：2026-08-11 → 2026-08-12。旧服务 `btc-liq-monitor` / `btc-gate-liq` 全程未停未改，面板未切换，`binance_liq` / `gate_liquidations` 两表未动。*
