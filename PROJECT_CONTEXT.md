# BTC AI 永续合约辅助交易系统 · 完整项目文档 v2
> 更新日期：2026-06-27 · 新对话直接粘贴本文档即可快速上下文同步

---

## 一、项目基础信息

| 项目 | 信息 |
|---|---|
| GitHub 仓库 | https://github.com/saiy829/btc-trader （**公开**） |
| VPS 服务商 | Hetzner，Ubuntu 22.04 LTS，德国法兰克福 |
| VPS Hostname | `206507`（root@206507） |
| 项目目录 | `/opt/btc-trader/` |
| Python 环境 | pyenv 管理，venv 在 `/opt/btc-trader/venv/` |
| 实时面板 | https://mb.661688.xyz |
| 简报站（WP） | https://jianbao.661688.xyz |
| 交易品种 | Binance BTCUSDT 永续合约 |
| 时区约定 | 所有显示时间以 **北京时间 UTC+8（SGT）** 为准 |

---

## 二、快速上下文协议（新对话必读）

### 核心约定：
- AtasBridge.dll 版本号自 2026-07-06 起采用 `v日期-当日序号` 格式
  （如 `v2026.07.08-1`），`CHANGELOG_AtasBridge.md` 同步遵循；
  此前的旧版本号（如 v5.0/v5.1）不追溯改写。
- **AtasBridge.dll 每次升级必须同时交付两个平台的构建（2026-07-06 起，
  任务卡7I 确立）**：本机同时装了 ATAS X（Avalonia渲染，SDK v8.0.14.644，
  指标目录 `%APPDATA%\ATAS X\Indicators\`）和普通版 ATAS Platform
  （WPF渲染，SDK v8.0.14.290，指标目录 `%APPDATA%\ATAS\Indicators\`）。
  两版本SDK核心API一致，但个别类型有差异（如 `LineTillTouch` 构造函数的
  Pen参数：ATAS X 用 `Utils.Common.UniversalPen`，普通版用标准
  `System.Drawing.Pen`）。源码目录下 `AtasBridge.csproj`（ATAS X）和
  `AtasBridge.Platform.csproj`（普通版）共用同一份 `AtasBridge.cs`，
  仅用 `#if ATAS_PLATFORM` 分支处理差异点，编译后各自自动复制到对应
  指标目录。以后任何 AtasBridge.cs 改动，两个 csproj 都要重新编译验证，
  不能只更新其中一个平台。
- **AtasBridge.dll "渲染字符串须纯ASCII"规则的适用范围（2026-07-11
  Phase 7K 澄清）**：该规则源自7I的教训——自定义图表画布绘图
  （`RenderContext.DrawString`画的✓/✗等符号）在Sea机器上渲染成方块，
  这是ATAS底层绘图API的字体覆盖问题。适用范围是：图表画布绘图内容、
  ATAS日志（`LoggerHelper.LogInfo`）、推送给VPS的JSON payload字符串
  字段。**不适用于**设置面板的`[Display(Name=/GroupName=)]`文本——那
  是ATAS原生WPF/Avalonia界面渲染，跟图表画布是完全不同的链路，面板
  自身"关于"/"设置"这些原生文字本来就是中文，已证明这条链路对中文
  渲染没有问题（7K已把全部设置项名称改成中文）。同理也不适用于枚举
  值本身（`ExchangeName`/`MarketKind`等C#标识符，这些改中文会破坏
  `.ToString()`回读逻辑，不是纯UI问题，原则上也不该改）。

### Claude 读取 GitHub 文件方法：
```bash
# 直接 curl 原始内容（在 Claude 沙盒内运行）
curl -s "https://raw.githubusercontent.com/saiy829/btc-trader/main/文件路径"
```

### VPS 常用命令：
```bash
# 进入项目目录
cd /opt/btc-trader

# 用 venv Python 运行（不能用系统 python3）
venv/bin/python3 脚本.py

# 查看所有服务状态
supervisorctl status

# 重启单个服务
supervisorctl restart btc-briefing

# 查看日志
tail -50 logs/scheduler.log
tail -50 logs/daily-briefing.log

# 手动触发简报（测试）
venv/bin/python3 -c "from daily_briefing import run; run('ondemand')"
```

### 传文件到 VPS（Windows PowerShell）：
```cmd
scp 本地文件 root@VPS_IP:/opt/btc-trader/目标路径
```

### ⚠️ 重要：GitHub 与 VPS 的同步关系
- **git_sync.sh** 每天北京时间 03:00 自动将 VPS 改动推送到 GitHub
- 白天手动部署到 VPS 的文件，要等次日凌晨才出现在 GitHub
- 因此 GitHub 上的代码可能比 VPS 滞后最多 24 小时
- **新对话读 GitHub 文件后，务必询问用户"今天是否有新部署"**

### VPS 上的一次性脚本存放位置：
- `/root/btc-deploy/` — 所有已执行完毕的补丁/部署脚本
- `/opt/btc-trader/` — 只放正式项目文件

---

## 三、系统架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    /opt/btc-trader/                             │
│                                                                 │
│  8 个 Supervisor 托管服务（持续运行）：                           │
│                                                                 │
│  btc-briefing          scheduler.py          Telegram Bot+定时  │
│  btc-api               api/main.py :8001     实时面板后端        │
│  btc-binance-data      monitor/binance_data_service.py          │
│                                              5分钟采集→SQLite    │
│  btc-structure-monitor monitor/structure_monitor.py             │
│                                              象限+多空比预警     │
│  btc-liq-monitor       monitor/liquidation_monitor.py           │
│                                              OKX WS 清算监控    │
│  btc-dom-monitor       monitor/dom_monitor.py  DOM深度监控      │
│  btc-funding-monitor   monitor/funding_monitor.py 费率预警      │
│  btc-oi-monitor        monitor/oi_monitor.py   OI异动预警       │
│                                                                 │
│  数据库：/opt/btc-trader/btc_history.db  （SQLite）             │
│  日志：  /opt/btc-trader/logs/                                  │
│  配置：  /opt/btc-trader/.env   ← 绝不进 Git！含所有密钥        │
│  临时脚本：/root/btc-deploy/    ← 已执行的一次性脚本             │
└─────────────────────────────────────────────────────────────────┘

数据流向：
  Binance REST/WS ─────→ data_collector/* + btc_binance_data_service
  OKX WebSocket ────────→ liquidation_monitor（Binance WS 德国IP被封）
  SoSoValue + Farside ──→ data_collector/etf_data
  SQLite（btc_history.db）→ briefing/binance_briefing_data.py → AI Prompt
  所有数据 → AI Prompt → Claude API → Telegram Bot + WordPress
```

---

## 四、完整目录结构

```
/opt/btc-trader/
│
├── scheduler.py              ★ 系统主入口（Supervisor 管理）
│                               Telegram Bot + 定时任务
│
├── daily_briefing.py         ★ 简报主流程 v8
│
├── startup_guard.py          ★ 启动授权验证（2026-06-27 新增）
│                               密钥+hostname 双重验证，防止代码被他人运行
│
├── git_sync.sh               ★ VPS→GitHub 每日自动同步
│                               cron 每天北京时间 03:00 运行
│
├── ai_analyst/
│   ├── briefing.py           ★ Claude AI Prompt 构建 v6
│   └── liq_briefing.py       大额清算 AI 分析 Prompt
│
├── briefing/
│   └── binance_briefing_data.py  ★ Binance 市场结构数据摘要 v2
│                               从 SQLite 查数据，计算 Z-score + 三因子状态
│
├── data_collector/
│   ├── binance_data.py       Binance 价格/OI/IB/VP 采集
│   ├── multi_funding.py      5交所费率聚合（Binance/OKX/Bybit/Bitget/Gate）
│   ├── etf_data.py           ETF资金流（SoSoValue+Farside双源）
│   └── cme_data.py           ★ CME 历史缺口追踪 v2（2026-06-27 更新）
│                               24/7上线后改为追踪3个历史遗留缺口
│
├── monitor/
│   ├── liquidation_monitor.py  OKX WS 清算监控（Binance德IP被封）
│   ├── funding_monitor.py      资金费率极端预警
│   ├── oi_monitor.py           OI异动预警（1H变化>5%触发）
│   ├── dom_monitor.py          DOM深度监控
│   ├── structure_monitor.py    象限+多空比实时预警
│   └── binance_data_service.py ★ 5分钟采集服务（btc-binance-data）
│                                 将 OI/Funding/L/S/象限 写入 SQLite
│
├── services/                     （7M起已清空：etf_confirm_push.py 退役，
│                                  与utils/etf_timing.py一并归档至
│                                  /root/btc-deploy/retired/，见Bug#36）
│       （注：5分钟采集服务实际在 monitor/binance_data_service.py，
│         此前文档误写成 services/btc_binance_data_service.py，7N订正）
│
├── publisher/
│   └── wordpress.py          ★ WordPress 发布 v7（带彩色HTML）
│
├── alert_bot/
│   └── send.py               Telegram 消息发送（支持长消息自动拆分）
│
├── api/
│   └── main.py               FastAPI 实时面板后端 v5（端口 8001）
│
├── web/
│   └── index.html            实时面板前端（Vue3 + WebSocket）
│
├── utils/
│   └── helpers.py            通用工具（logger/get_env/时间格式/金额格式）
│
├── btc_history.db            SQLite 数据库（不进 Git）
├── .env                      密钥配置（不进 Git）
├── requirements.txt          Python 依赖
└── PROJECT_CONTEXT.md        旧版项目文档（已被本文档替代）
```

---

## 五、.env 配置文件说明

```bash
# Telegram Bot
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Claude API
ANTHROPIC_API_KEY=...

# WordPress（本地 WP-CLI，REST API 被德国IP封锁）
WP_PATH=/www/wwwroot/jianbao.661688.xyz

# ETF 数据源
SOSOVALUE_API_KEY=...      # SoSoValue 官方 API

# 清算监控阈值（改后重启 btc-liq-monitor 生效）
LIQ_SINGLE_USD=100000      # 单笔清算预警：$10万
LIQ_HOURLY_USD=3000000     # 1小时累计预警：$300万

# 启动安全验证（2026-06-27 新增）
BTC_TRADER_KEY=...         # 64位随机密钥，不在GitHub上

# /pos 仓位风控命令默认值（2026-07-04 Phase 7B 新增，改后重启 btc-briefing 生效）
POS_ACCOUNT_USDT=10000     # 默认账户资金（USDT），命令不填第3个参数时使用
POS_RISK_PCT=1.0           # 默认单笔风险百分比，命令不填第4个参数时使用
```

---

## 六、SQLite 数据库（btc_history.db）

由 `monitor/binance_data_service.py` 每 5 分钟采集写入：

| 表名 | 内容 | 主要字段 |
|---|---|---|
| `binance_oi` | 持仓量快照 | `ts`, `oi_usd`, `oi_btc` |
| `binance_funding` | 资金费率历史 | `ts`, `rate`, `next_settle`, `premium_pct` |
| `binance_ls_top` | 大户多空比 | `ts`, `ls_ratio`, `long_pct`, `short_pct` |
| `binance_ls_global` | 全市场多空比 | `ts`, `ls_ratio`, `long_pct`, `short_pct` |
| `binance_structure` | 5分钟象限 | `ts`, `quadrant`, `note`, `oi_chg`, `px_chg` |
| `signal_scores` | 综合信号分历史（2026-07-04 Phase 7A-2新增） | `ts`, `session`, `composite`, `label`, `etf_s`, `fr_s`, `quad_s`, `ls_s`, `cb_s`, `regime_s`, `detail_json` |
| `engine_signals` | 信号引擎模拟信号历史（2026-07-06 Phase 7G新增） | `id`, `created_at`, `direction`, `score`, `dims_json`, `entry`, `stop`, `t1`, `t2`, `atr`, `status`, `t1_touched`, `outcome_price`, `outcome_at` |
| `engine_scores` | 信号引擎每轮评分遥测（2026-07-13 Phase 7N新增，每5分钟成功评分落一行，跳过轮不写；与signal_scores严格分离——那张表只属于简报链路，引擎绝不写入） | `ts`(主键,epoch秒), `composite`, `label`, `etf_s`, `fr_s`, `quad_s`, `ls_s`, `cb_s`, `regime_s`, `detail_json` |
| `atas_bars` | AtasBridge.dll 推送的四路(币安/OKX现货/永续)K线+footprint（`(exchange,market_type,timestamp,timeframe)`唯一索引，`/atas/bar`用INSERT OR REPLACE幂等写入，2026-07-06 Phase 7H加固） | `timestamp`, `timeframe`, `exchange`, `market_type`, `open/high/low/close`, `volume`, `delta`, `cumulative_delta`, `max_oi`, `min_oi`, `poc_price`, `footprint_json` |
| `atas_large_trades` | AtasBridge.dll 推送的大额/鲸鱼级成交 | `timestamp`, `exchange`, `market_type`, `price`, `volume`, `direction`, `threshold_level` |
| `atas_absorption` | AtasBridge.dll 原生吸收信号（2026-07-06 Phase 7F新增，取代旧ATAS内置Webhook） | `timestamp`, `exchange`, `market_type`, `side`, `price`, `absorbed_btc`, `bid_vol`, `ask_vol`, `ratio` |

`briefing/binance_briefing_data.py` 在每次简报前读取这些表，计算 Z-score 和三因子状态。

`signal_scores` 由 `utils/signal_score.py` 写入，每次生成简报前计算一次（五种session都会写）。
`detail_json` 存原始输入快照（quadrant/oi_chg_1h/ls_ratio_r/fr_zscore/cb_premium/regime_label/
etf净流原始值）和各维度备注，供 Phase 5B 回测分数与实际胜率的相关性时反查用。
建表用 `CREATE TABLE IF NOT EXISTS` 后紧跟 `CREATE INDEX`（同一 executescript），
不要拆开顺序颠倒——历史教训见 `api/main.py` 的 `_atas_db_init()` 注释
（索引依赖的字段如果表还没建好会直接报错中断整个初始化）。

---

## 七、简报系统（每日三次 + 随时触发）

### 触发方式：

| 会话名 | 触发时间 | 触发方式 |
|---|---|---|
| `morning` | 周二至周日 09:30（UTC 01:30） | 自动定时（周一自动路由为 weekly） |
| `weekly` | 周一 09:30（UTC 01:30） | 自动定时（2026-07-13 7P新增：周报·上周复盘与下周展望，取代已退役的 morning_monday） |
| `noon` | 周二至周六 12:00（UTC 04:00） | 自动定时（2026-07-13 7M新增：ETF确认+亚盘复盘） |
| `europe` | 北京时间 15:00（UTC 07:00） | 自动定时 |
| `evening` | 北京时间 20:30（UTC 12:30） | 自动定时 |
| `ondemand` | 随时 | TG 发送 `/b` 或 `简报` |

### Telegram 命令清单：

| 命令 | 功能 | chat_id 校验 |
|---|---|---|
| `/b` 或 `B` 或发"简报" | 立刻生成实时简报（ondemand） | 是 |
| `/status` | 查看系统运行状态 | 是 |
| `/pos <入场价> <止损价> [资金USDT] [风险%]` | 仓位风控计算（2026-07-04 Phase 7B 新增，纯本地计算不调用外部API，见 `utils/position_calc.py`） | 是 |

### 简报主流程 `daily_briefing.py` — 7步骤：

```
[1] binance_collect()          → 价格/OI/Funding/IB/VP
[2] collect_multi_funding()    → 5交所费率聚合
[3] fetch_etf_flows()          → ETF资金流（SoSoValue+Farside）
[4] get_cme_gap()              → CME历史缺口状态
[5] get_todays_ib()            → 今日IB（北京时间08:00起首小时）
    get_yesterday_volume_profile() → 昨日VP（POC/VAH/VAL/HVN/LVN）
[6] get_binance_context()      → 从SQLite读OI/FR/LS/象限，计算Z-score+三因子
    get_market_meta()          → 提取Z-score和市场状态供Header使用
[7] generate_briefing()        → 调Claude API生成分析
    → send(TG)                → 发Telegram
    → publish_briefing(WP)    → 发WordPress
```

### TG Header 格式（build_header 输出）：

```
====================================
BTC 早盘简报·当日交易计划
2026-06-27 09:30 SGT
------------------------------------
永续合约 ：$59,996  UP +1.17%
现货价格 ：$59,992（+1.17%）  基差：+4
资金费率 ：+0.0041%  均值：+0.0054%
费率Z分  ：-0.80（中性，信号相对干净）   ← v8 新增
24H 成交额：$167亿
CB 溢价  ：-96 USD（机构偏空）
OI 24H   ：-1.54%
市场状态 ：多头拥挤承压 ⚠️              ← v8 新增
====================================
```

---

## 八、Binance 市场结构数据模块（v2 核心功能）

文件：`briefing/binance_briefing_data.py`

### 资金费率 Z-score（2026-06-27 新增）：
- **数据源**：`binance_funding` 表，近 24 小时记录（约 288 条@5分钟）
- **计算方式**：`Z = (当前费率 - 24H均值) / 24H标准差`
- **解读阈值**：
  - `Z > +2.0`：极端偏高 ⚠️ 多头严重拥挤，不宜追多
  - `+1.0 ~ +2.0`：中度偏高，谨慎追多
  - `-1.0 ~ +1.0`：中性，信号干净
  - `-2.0 ~ -1.0`：中度偏低，谨慎追空
  - `Z < -2.0`：极端偏低 ⚠️ 空头严重拥挤，关注轧空

### 三因子市场状态分类（2026-06-27 新增）：

| 因子 | 数据来源 | 分类规则 |
|---|---|---|
| OI动向 | `binance_oi` 近1小时变化 | >+0.3%=↑上升 / <-0.3%=↓下降 / 其余=→平稳 |
| 费率极端度 | Z-score | >2=极高 / 1~2=偏高 / -1~1=中性 / -2~-1=偏低 / <-2=极低 |
| 多空拥挤 | `binance_ls_global` long_pct | >60%=多头拥挤 / <40%=空头拥挤 / 其余=多空均衡 |

**12 种状态标签：**

| 状态 | 触发条件 | 操作导向 |
|---|---|---|
| 过热/顶部风险 ⚠️ | OI↑ + FR极高 + 多头拥挤 | 不追多，等待轧多信号 |
| 挤压酝酿中 🔄 | OI↑ + FR极低 + 空头拥挤 | 关注空头挤压，轻多方向 |
| 真实趋势建仓中 📊 | OI↑ + FR中性 + 多空均衡 | 可顺势跟随 |
| 多头被迫平仓 ⬇️ | OI↓ + FR极高/偏高 + 多头拥挤 | 不接多，等OI企稳 |
| 空头被迫平仓 ⬆️ | OI↓ + FR极低/偏低 + 空头拥挤 | 不接空，等OI企稳 |
| 去杠杆/清洗中 📉 | OI↓ + FR中性 | 降低仓位，等待确认 |
| 横盘多头拥挤 ⚠️ | OI平稳 + FR极高/偏高 + 多头拥挤 | 警惕向下清算 |
| 横盘空头拥挤 ⚠️ | OI平稳 + FR极低/偏低 + 空头拥挤 | 警惕向上挤压 |
| 健康趋势（多方主导）✅ | OI平稳 + FR偏高 + 多空均衡 | 顺势做多 |
| 健康趋势（空方主导）✅ | OI平稳 + FR偏低 + 多空均衡 | 顺势做空 |
| 趋势延续（均衡）✅ | OI平稳 + FR中性 + 多空均衡 | 顺象限方向操作 |
| 多头拥挤承压 ⚠️ | OI平稳 + 多头拥挤（任意FR） | 不宜追多，关注向下扫止损 |
| 空头拥挤承托 ⚠️ | OI平稳 + 空头拥挤（任意FR） | 不宜追空，关注挤压信号 |
| 混合信号 | 未匹配以上任何状态 | 轻仓观望 |

---

## 九、AI Prompt 系统（ai_analyst/briefing.py v6）

### Claude API 参数：
- 模型：`claude-sonnet-4-6`（或当前最新 Sonnet）
- max_tokens：早盘 8192 / 欧盘 2500 / 美盘 3000 / 随时 2000

### 早盘简报 12 节结构（morning，周二至周日；2026-07-13 7M起去ETF节）：
```
1. 宏观背景评级（A/B/C/D）        ← 综合信号分ETF维度用稳定视图并标注数据日
2. CME 历史缺口追踪
3. 今日 IB 分析·开盘类型确认
4. 昨日 Market Profile 结构
5. 昨日 Volume Profile 概览
6. 流动性分布·Stop Hunt 分析
7. 衍生品深度解读              ← 含Z-score + 三因子状态解读
8. AMT 市场状态·今日框架       ← 引用三因子分类判断趋势质量
9. 今日关键价格层
10. 今日完整交易计划（保守/稳健/激进三档）
11. ATAS 订单流确认重点
12. 一句话总结
```
（原第2节【BTC现货ETF资金流向解读】已移至正午简报第1节：早盘09:30处于美股
披露窗口内数据未确认，DATA块ETF段也换成单行提示不再注入未确认数据；
morning_monday 已于2026-07-13 7P正式退役——7M拆分时逐字节保留它正是为7P
整体替换，周一09:30改由 weekly 周报接管，见下方周报结构）

### 正午简报 7 节（noon，2026-07-13 7M新增，周二至周六 SGT 12:00）：
```
1. ETF确认数据·机构动向        ← 完整净流+已确认完整标注+主力品种+周月累计
2. 亚盘四小时复盘（08:00-12:00区间/IB突破/早盘计划核对）
3. 市场结构午间快照            ← 象限4H分布/Z-score/OI/多空比/三因子切换
4. 订单流午间摘要              ← ATAS 4小时窗口 Delta/CVD/POC/吸收区/大单
5. 午后关键触发位（2-3个，欧盘开盘前视角）
6. 计划修正（维持或修正早盘方案+多空触发位）
7. 一句话（15字以内）
```

### 周报 12 节结构（weekly，2026-07-13 7P新增，周一 SGT 09:30）：
```
1. 上周行情总览          7. 清算与风险偏好
2. ETF资金流周报         8. 流动性地图
3. 衍生品周报            9. 上周简报复盘（评分vs实际+引擎信号周报）
4. 市场结构周报          10. 下周情景推演（牛/熊/震荡三情景）
5. 周Volume Profile      11. 下周交易计划·周一开局
6. 订单流周报            12. 一句话总结
```
- 背景：UTC+8周一=纽约周日，全球传统金融静默，唯一适合全局视角的时刻
- 数据背书：`briefing/weekly_briefing_data.py` 的 `get_weekly_context()`
  生成 W1-W10 权威数据块整体注入 prompt（W1上周行情/W2 ETF逐日/W3衍生品/
  W4象限占比/W5周VP(15m×7天自实现70%VA算法)/W6订单流每日Delta/W7清算与
  F&G(模块自调alternative.me)/W8流动性地图(日K摆动点+CME缺口)/W9上周简报
  复盘(signal_scores按日聚合+engine_signals周终态)/W10周一开局(窗口实测+IB)）
- 周边界=周一北京08:00（与项目日边界08:00对齐，Binance日K UTC00:00天然对齐）；
  每块独立try/except，单源失败只标注"该块数据不足"不拖垮整报，宁缺勿假
- 已知数据缺口（第0步实情，如实标注在块内）：三因子状态无历史落库（W4）；
  OKX清算无历史落库、Binance清算WS被封（W7只用gate_liquidations）
- `_monday_window_stats()` 本体留在 ai_analyst/briefing.py，7P起由W10块调用

### 欧盘简报 6 节（europe）：
```
1. 欧盘前市场更新·价格结构
2. ETF 资金流向最新动态
3. 衍生品实时更新             ← 含Z-score变化 + 三因子状态切换提示
4. 欧盘关键触发价位
5. 欧盘操作方案
6. 一句话更新
```

### 美盘简报 5 节（evening）：
```
1. 当前市场评级
2. 价格结构·收盘前分析
3. 美盘前衍生品状态           ← 含Z-score全日演变 + 三因子最终状态
4. 流动性更新·NY Kill Zone 预警
5. 美盘最终操作方案
```

### 随时简报 4 节（ondemand）：
```
1. 当前市场评级
2. 价格结构
3. 衍生品快照                 ← 含Z-score + 三因子状态
4. 当前最优操作思路
```

### 周一早盘专项（morning_monday）额外数据块：
```
【周一结构回顾 & CME 历史缺口追踪】
注意：2026-05-29 CME 切换 24/7，周一不再有"开盘跳空"效应
重点改为：周末走势延续分析 + CME 历史缺口追踪
```

### 综合信号分（2026-07-04 Phase 7A-2 新增，utils/signal_score.py）：

背景：AI 自己心算综合信号分不可靠（2026-07-04 早盘曾出现 AI 自报综合分 -12
但其自报的六维加权和是 -4.8，两个数字互相矛盾）。现在六维打分和加权求和全部
改成 `utils/signal_score.py` 里的确定性代码计算，AI 只负责引用结果并解释市场
含义，不再自己算数。

六维权重（`SCORE_CONFIG["weights"]`，全部集中在文件顶部方便校准）：
ETF流向25% / 资金费率Z-score15% / OI象限20% / 大户多空比15% / CB溢价10% /
三因子市场状态15%。各维映射公式见 `SCORE_CONFIG` 具体字典和函数注释。

数据来源：
- ETF净流 / CB溢价：由 build_prompt() 参数直接传入（外部API采集，不在本地表）
- **ETF稳定视图规则（2026-07-13 7M + 7M-2，单日+累计全口径）**：量化评分
  的ETF维度只用"最近一个已确认完整交易日"（stable_date）口径的数据——
  单日分量用 `stable_flow_m`（7M，is_settling=False 时落入
  data/etf_state.json 持久化，顺带修正了该state文件被整体覆盖写的bug）；
  周累计分量用 `stable_week_m`（7M-2，截至stable_date的逐日区间求和，
  每次fetch重算无需state）。北京04:00-12:00披露窗口内的当日阶段值只供
  简报正文展示，不进入任何量化评分。stable_flow_m缺失（首次部署）→
  简报侧整维记0分+detail_json标etf_source=missing，引擎侧宁缺勿假跳过；
  stable_week_m单独缺失→仅该分量按0计+etf_week_source=missing。
  detail_json溯源字段：etf_source、etf_stable_date、etf_week_source
  （stable/realtime/missing）。已知副作用（Sea裁定正确性优先）：该维从
  连续更新变为每日12:00后阶跃一次（7M时周分量的阶段值残余已由7M-2消除）。**2026-07-22 无状态化修复（Bug#37）**：stable_flow_m/stable_date 改为每次从 parsed 逐日行推导（确认日=非窗口parsed[-1]/窗口内parsed[-2]），不再依赖 etf_state.json；原设计"稳定键只窗口外写、窗口内只读"遇 state 被并发非原子写损坏时会到12:00才自愈，期间引擎宁缺勿假整轮跳过（实测停摆3.5h）
- 资金费率Z-score：复用 `binance_briefing_data.get_market_meta()["fr_zscore"]`，
  不重新计算，避免和其他地方显示的 Z 值数值漂移
- OI象限 / 近1小时OI变化率 / 大户多空比：`signal_score.py` 直接查
  `binance_structure` / `binance_oi` / `binance_ls_top` 三张表。
  注意大户多空比用的是 `binance_ls_top.ls_ratio` 字段本身（Binance按持仓量算的
  比值），不是账户占比（long_pct/short_pct）换算出来的比值——实测两者差异很大
  （2026-07-04早盘实例：ls_ratio字段=1.231，但61.7%/38.3%换算=1.611），
  账户占比换算会导致分类档位判断错误

三因子状态14档完整映射（2026-07-04 补充裁定，权威定义，见 SCORE_CONFIG.regime_map，
数值改动需用户确认）：真实建仓+50 / 健康趋势(多方)+40 / 趋势延续±30(按近1小时
价格变化符号，|变化|<0.05%记0，需要 `binance_structure.mark_px` 历史) /
空头拥挤承托+35 / 横盘空头拥挤+25 / 空头被迫平仓+15 / 混合信号0 / 挤压酝酿-10 /
横盘多头拥挤-25 / 多头拥挤承压-35 / 过热-40 / 去杠杆清洗-40 /
健康趋势(空方)-40 / 多头被迫平仓-45。
防御规则：未来出现表里没有的新标签 → 记0分 + WARNING日志 +
detail_json标注"未映射状态:标签名"，绝不猜测赋分（与 AtasBridge 的 Unset
默认值同一设计哲学：宁可报警不可编数）。

大户多空比新鲜度降级（2026-07-04 补充裁定）：若 `binance_ls_top` 最新记录超过
`SCORE_CONFIG["ls_stale_sec"]`（15分钟）未更新，视为 STALE，自动降级改用
build_prompt 传入的实时REST快照（binance["ls_ratio"]），并在 detail_json
标注 ls_source 字段说明用的是哪个来源，方便回溯。

环比对比：读 `signal_scores` 表最新一条记录，不再依赖 AI"记住"上一次简报的数字。

---

## 十、CME 缺口模块（v2 — 历史追踪模式）

**背景**：2026年5月29日，CME Group 正式切换 BTC 期货为 7×24 交易，
每周六 UTC 03:00-05:00（北京时间 11:00-13:00）仅保留 2 小时维护窗口。
**不再产生新的周末缺口。**

**文件**：`data_collector/cme_data.py`（v2，已部署 VPS，待 git_sync 同步 GitHub）

**3 个历史遗留缺口（截至 2026-06-27 全部未填）：**

| 缺口 | 价格区间 | 形成时间 | 当前距离（@$60,300） |
|---|---|---|---|
| 缺口① 1月末高位 | $79,200 - $80,400 | 2026-01 周末 | +31.3%（$+18,900） |
| 缺口② Q1次高位 | $78,000 - $78,500 | 2026-Q1 | +29.3%（$+17,700） |
| 缺口③ Q1中段 | $69,000 - $70,000 | 2026-Q1 | **+14.4%（$+8,700）← 最近** |

**自动退休机制**：当 `all_filled=True`（BTC 涨过 $80,400），`_cme_block()` 输出退休提示，可从简报中移除此节。

---

## 十一、WordPress 发布系统（v7 彩色）

文件：`publisher/wordpress.py`

**发布方式**：WP-CLI（REST API 被德国 IP 封锁，改用本地 CLI）
```python
wp_path = "/www/wwwroot/jianbao.661688.xyz"
subprocess.run(['wp', 'post', 'create', ...])
```

### 颜色系统：

| 元素 | 颜色 | 十六进制 |
|---|---|---|
| 做多方向 / 目标价 | 绿色 | `#1e8449` |
| 做空方向 / 止损价 | 红色 | `#c0392b` |
| 关键价位 $XX,XXX | 橙色 | `#d35400` |
| 入场/触发价 | 蓝色 | `#1565c0` |
| IB/MP 数据（PDH/PDL/POC 等） | 紫色 | `#6a1b9a` |
| 评级 A | 绿色 | 同做多 |
| 评级 B | 蓝色 | 同入场 |
| 评级 C | 橙黄 | `#e67e22` |
| 评级 D | 红色 | 同做空 |
| Z-score \|z\|>2 | 红色 | 同做空 |
| Z-score \|z\|>1 | 橙黄 | `#e67e22` |
| Z-score 中性 | 绿色 | 同做多 |
| 市场状态 badge | 按风险类型 | 红/橙/绿/蓝 |
| BSL / SSL | 橙色 | 同关键价位 |

### Header Card（WP文章顶部数据卡片）：
- 价格 + 24H涨跌（绿/红）
- 永续 vs 现货对比表格
- 资金费率 + OI 24H
- **Z-score 行 + 市场状态行**（v7 新增）
- Coinbase 溢价（含绿/红背景）

---

## 十二、实时监控服务

### 清算监控（btc-liq-monitor）：
- **数据源**：OKX WebSocket（Binance WS 德国IP被封）
- **阈值**（从 .env 读取）：单笔 > $10万 发预警；1H累计 > $300万 发预警
- 大额清算触发 AI 分析（`ai_analyst/liq_briefing.py`）

### 资金费率监控（btc-funding-monitor）：
- 每 5 分钟轮询 5 家交所
- 触发：单所 |rate| > 0.05%（30分钟冷却）
- 强触发：3所同时极端（15分钟冷却）
- 紧急：单所 |rate| > 0.10%（10分钟冷却）

### OI 监控（btc-oi-monitor）：
- 每 5 分钟检查 1H OI 变化
- 触发：1H 变化 > 5%（标准）/ > 10%（紧急）

### DOM 深度监控（btc-dom-monitor）：
- 实时监控挂单深度异动

### 象限+多空比监控（btc-structure-monitor）：
- 结合 5 分钟象限（Q1/Q2/Q3/Q4）和多空比发送预警
- 周一 TradFi 周初开盘窗口插针检测（2026-07 新增）：仅北京时间周一
  夏令时05:00/冬令时06:00 至 08:00 激活，检测扫过 PDH/PDL 又收回的插针
  （突破深度≥0.03%，可用 .env 的 SWEEP_BREACH_MIN_PCT 配置），
  同方向每个周一窗口只报一次

### Binance 数据服务（btc-binance-data）：
- 每 5 分钟采集并写入 SQLite：OI / Funding / 多空比 / 象限
- 为 `binance_briefing_data.py` 的 Z-score 和三因子计算提供历史数据

### 信号引擎（btc-signal-engine，monitor/signal_engine.py，Phase 7G 新增）：
- 每 5 分钟独立计算一次 `utils/signal_score.py` 综合分（六维输入与简报
  同源：`briefing.binance_briefing_data` 的 fr_zscore/regime + `data_collector`
  的 etf/cb_premium/大户多空比REST快照），任一维度数据获取失败或过期
  本轮直接跳过（宁缺勿假，不用旧值凑数）
- 状态机（迟滞+冷却，常量集中在文件顶部供 Phase 5B 回测后调参）：
  | 常量 | 默认值 | 含义 |
  |---|---|---|
  | `THRESH_LONG` | +25（7N校准，原+60） | 综合分上穿 → LONG 信号 |
  | `THRESH_SHORT` | -25（7N校准，原-60） | 综合分下穿 → SHORT 信号 |
  | `REARM_BAND` | ±15（7N校准，原±40） | 迟滞带：分数回落到此范围内才允许再次武装 |
  | `COOLDOWN_MIN` | 90分钟 | 同方向信号最小间隔，只影响是否发TG，不影响是否记库 |
  | `ATR_STOP_MULT` | 1.5× | 止损距离（15分钟ATR(14)倍数） |
  | `ATR_T1_MULT` / `ATR_T2_MULT` | 1.5× / 3.0× | 目标1(1R) / 目标2(2R) |
  | `EXPIRE_HOURS` | 24小时 | 开仓超过此时长未触及任何边界标记到期 |
  - 引擎重启后以"未武装"状态启动，需分数先回落到 ±15 以内才武装，
    避免重启瞬间对着高分立即补发信号
  - 2026-07-13 任务卡7N循证校准：阈值±60→±25、迟滞±40→±15。依据：
    signal_scores 9日54样本区间[-16,+33]（|分|≥40零次）+引擎日志218样本
    mean=13.0/std=7.1/p95=25/max=27，±60对LONG≈6.6σ实证不可达；±25≈p95。
    **新阈值冻结至 engine_signals 累积≥30个终态样本，期间不再调参**
  - 每轮遥测（7N新增）：每轮成功评分后写一行 `engine_scores`（跳过轮
    不写，写失败仅告警不影响主循环）；与 `signal_scores` 严格分离——
    那张表只属于简报链路，环比对比读它的最新一条，引擎绝不写入
  - ETF维度进程内TTL缓存（7N新增）：成功取数缓存1小时，TTL内不发网络
    请求（此前每5分钟全量爬Farside+SoSoValue，288次/天有封IP风险，ETF
    本为日粒度数据）；缓存过期且实取失败仍按宁缺勿假跳过本轮，日志区分
    "ETF(实取)"/"ETF(缓存)"
- 信号自跟踪（同一服务内完成，不动 `signal_tracker.py`）：每轮循环检查
  所有 `status='open'` 的历史信号是否触及 stop/t1/t2/24小时到期，终态
  （stopped/t2_hit/t1_then_stop/expired）自动写库并发一条简短TG回执
- 红线：本服务产出全部是【模拟·纸面交易】信号，不接任何下单接口
- 只读查询：`GET /api/signal/latest` 返回 `engine_signals` 最新一条
  （为 Phase 7H 图表显示预埋，main.py 其余部分未改动）
- 只读查询：`GET /api/signal/history?days=7`（2026-07-11 Phase 7J新增）
  返回 `engine_signals` 最近N天全部记录，`{"count":N,"signals":[...]}`；
  为面板信号展示区块 + AtasBridge 图表历史信号标记共用同一数据源，
  `/api/signal/latest` 未废弃但 AtasBridge 已改用这个新端点（一次轮询
  同时拿到当前信号+历史信号）

### 吸收信号结果追踪（signal_tracker.py，Phase 5A，此前文档未详细记录）：
- 独立 Supervisor 服务，与 `signal_engine.py`（综合分模拟信号）是完全
  不同的两套系统——本服务追踪的是 AtasBridge 推上来的**原生吸收信号**
  （`atas_signals` 表，`indicator_name LIKE '%Absorption%'`），不是
  综合分信号
- 每5分钟：把过去2小时内新出现、还没登记过的吸收信号写入
  `atas_signal_outcomes`（记录触发时刻的 POC/Delta/CVD、与POC的相对
  位置），并回查所有已到期（`check_4h_at`/`check_24h_at`已过）但还
  未结算的信号，用涨跌幅+0.5%(4H)/±1%(24H)阈值分类 up/down/flat，
  为验证"吸收信号是否有统计意义上的预测力"积累样本
- 固定只看币安永续这一路（`atas_bars`/`get_latest_bar()` 过滤
  `exchange='binance' AND market_type='perp'`），原因见文件头注释：
  AtasBridge同时接四路数据写进同一张表，不过滤会把OKX/现货的bar
  错记成触发时刻的上下文，污染胜率样本
- 只在日志里输出统计（`print_stats()`，每小时一次），未对接Telegram/
  面板/图表——目前是纯研究性质的数据积累，还没有变成"交易信号"

---

## 十三、实时面板（mb.661688.xyz）

- **后端**：FastAPI v5，端口 8001，WebSocket 推送
- **前端**：`web/index.html`，Vue3 + 深色/浅色主题
- **数据**：价格、OI、Funding、CB溢价、CVD、清算列表、IB、VP、引擎信号
  （当前信号+近7天历史，2026-07-11 Phase 7J新增，独立REST轮询
  `/api/signal/history?days=7`，不接WebSocket快照）
- **CVD**：通过 Binance aggTrades WebSocket 实时累积，每 UTC 自然日重置

### ⚠️ 重要：`web/index.html` 部署方式（2026-07-11 才发现并补记，此前文档一直没写）
- **仓库里的 `web/index.html` 不会被直接 serve！** `mb.661688.xyz` 的
  nginx 配置（宝塔面板管理，`/www/server/panel/vhost/nginx/
  mb.661688.xyz.conf`）`root` 指向 `/www/wwwroot/mb.661688.xyz/`，是一份
  **独立部署的拷贝**，不是 `/opt/btc-trader/web/` 的软链接
- 改了仓库里的 `web/index.html`（无论是本地改完 scp，还是 VPS 上直接改），
  **网页不会自动更新**，必须额外手动执行：
  ```bash
  cp /opt/btc-trader/web/index.html /www/wwwroot/mb.661688.xyz/index.html
  ```
  （建议先备份：`cp .../index.html .../index.html.bak.$(date +%Y%m%d_%H%M%S)`，
  该目录下已经有一批这种备份文件，说明这个手动部署模式此前就存在）
- `api/main.py` 是 Supervisor 服务（`btc-api`），改完直接
  `supervisorctl restart btc-api` 生效；`web/index.html` 是纯静态文件，
  不需要重启任何服务，`cp` 完立刻生效（浏览器可能需要强制刷新绕过缓存）
- `web/history.html` 同理，也在 `/www/wwwroot/mb.661688.xyz/` 下独立部署

---

## 十四、安全保护（2026-06-27 新增）

文件：`startup_guard.py`（部署在 VPS，不在 GitHub）

```python
# scheduler.py 的 main() 最顶部调用：
from startup_guard import verify
verify()
```

**双重验证逻辑：**
1. **密钥验证**：`.env` 中的 `BTC_TRADER_KEY` 必须存在且 ≥ 32 字符
2. **主机绑定**：hostname 必须包含 `206507`

**防护效果**：别人 clone GitHub 代码后，没有 `.env` 密钥且不在指定 VPS 上，运行时立即退出。

---

## 十五、Git 自动同步（2026-06-27 新增）

文件：`/opt/btc-trader/git_sync.sh`（在 VPS，不在 GitHub）

**触发时间**：每天北京时间 03:00（cron：`0 19 * * *` UTC）

**同步范围**（自动 git add 的目录/文件）：
```
briefing/  data_collector/  publisher/  ai_analyst/
alert_bot/  utils/  daily_briefing.py  scheduler.py
startup_guard.py（若已加入）
```

**不同步**（.gitignore 排除）：
```
*.db  *.log  logs/  __pycache__/  *.pyc  .env  venv/  data/
```

**查看同步日志**：
```bash
tail -20 /opt/btc-trader/logs/git_sync.log
```

---

## 十六、数据源清单与已知限制

| 数据 | 来源 | 备注 |
|---|---|---|
| BTC永续价格/OI/IB/VP | Binance REST | 正常 |
| 资金费率（实时） | Binance `/fapi/v1/premiumIndex` | v2修复：原用历史接口有延迟 |
| 5交所费率 | 各所公开API（无需Key） | Binance/OKX/Bybit/Bitget/Gate |
| 实时清算 | OKX WebSocket | Binance WS 德国IP被封 |
| ETF资金流 | SoSoValue API（需Key） + Farside 爬虫 | 双源交叉验证 |
| ETF 12:00 确认 | noon正午简报（scheduler.py run_daily UTC04:00 周二至周六） | 2026-07-13 7M起接管；原etf_confirm_push.py cron三重失效已退役（Bug#36） |
| CME缺口 | Binance现货近似（v2改为静态历史追踪） | CME 24/7后不再动态计算 |
| Coinbase溢价 | Binance+Coinbase价差计算 | 机构动向指标 |
| Fear & Greed | alternative.me /fng/ | 仅周报W7块采集（7P起，模块内自带try/except）；7P核实此前文档称"已集成简报数据块"与代码实情不符，已订正 |

---

## 十七、交易方法论背景（Sea 的交易框架）

项目服务于 Sea 的 BTC 永续合约日内交易，核心方法论：

**四层框架**：AMT（拍卖市场理论）→ Market Profile → Volume Profile → Order Flow

**交易软件**：ATAS（订单流分析，包含 Footprint/CVD/ClusterSearch/TrappedTraders/PhantomFlow/DOM）

**核心概念**：
- IB（Initial Balance）= 每日北京时间 08:00-09:00 第一小时价格区间
- 开盘类型：OD（开盘驱动）/ORR（区间返回）/OA（区间扩展）/OTD（趋势日开盘）
- Kill Zones：亚盘 / 伦敦开盘 / NY 开盘
- PDH/PDL/PDC：昨日高/低/收
- POC/VAH/VAL：Volume Profile 成交量峰值/区间上下沿
- HVN/LVN：高/低成交量节点
- BSL/SSL：Buy Side / Sell Side Liquidity（流动性猎取目标）
- ICT 概念：AMD 模型、Order Block、FVG（公允价值缺口）、Judas Swing
- 周一TradFi周初开盘窗口：北京夏令时05:00-07:00/冬令时06:00-08:00 = 全球FX周初开盘+CME Globex股指期货开盘；
  周末薄流动性切换回正常深度，IB形成前（08:00前）的插针优先视为BSL/SSL流动性清扫，勿追第一波方向

---

## 十八、近期重大更新记录

| 日期 | 更新内容 |
|---|---|
| 2026-06-27 | 新增资金费率 Z-score（24H滚动窗口） |
| 2026-06-27 | 新增三因子市场状态分类（12种状态） |
| 2026-06-27 | TG Header 新增「费率Z分」和「市场状态」行 |
| 2026-06-27 | WordPress 发布升级为 v7 彩色系统 |
| 2026-06-27 | AI Prompt 全会话更新（早/欧/美/随时四个会话） |
| 2026-06-27 | CME 缺口模块改为历史遗留缺口追踪模式 |
| 2026-06-27 | startup_guard.py 密钥+主机双重验证 |
| 2026-06-27 | git_sync.sh VPS→GitHub 每日 03:00 自动同步 |
| 2026-06-27 | /root/btc-deploy 管理一次性脚本，保持项目目录整洁 |
| 2026-07-04 | Phase 7A：AI Prompt 升级——第1节评级新增综合信号分（-100~+100，早盘展开六维拆解，欧盘/美盘/按需一行对比）；早盘第11节交易计划升级为保守/稳健/激进三档方案（多空各三档，激进档仓位减半，D评级全部观望） |
| 2026-07-04 | Phase 7B：新增 Telegram /pos 仓位风控计算命令（utils/position_calc.py 纯计算模块），固定风险比例计算仓位+2/3/5/10/20x保证金/估算强平价+危险杠杆预警+止损过近提示 |
| 2026-07-04 | Phase 7A-2：综合信号分改为 utils/signal_score.py 代码确定性计算（修复AI心算不一致），落库 signal_scores 表供Phase 5B回测，早盘三档标题改纯文本行禁止Markdown标题 |
| 2026-07-04 | Phase 7A-2补充裁定：三因子状态扩展为14档完整映射+未映射标签防御规则；大户多空比新增15分钟新鲜度降级(STALE改用REST快照) |
| 2026-07-04 | Phase 7A-3：ai_analyst/briefing.py 新增 _sanitize() 代码兜底，清洗AI偶尔残留的###标题和**加粗**Markdown符号，不依赖AI是否听话 |
| 2026-07-06 | Phase 7E：morning_monday 增加 TradFi 周初开盘窗口提示（全球外汇+CME Globex股指期货开盘，北京时间夏令时05:00-07:00/冬令时06:00-08:00自动切换，_monday_open_window()按美东dst()判断），提示常见BSL/SSL集中清扫 |
| 2026-07-06 | 7E v2：structure_monitor 新增周一插针TG预警（扫PDH/PDL又收回，独立协程monday_sweep_loop与现有monitor_loop并行）；方法论文档同步补充窗口说明 |
| 2026-07-06 | 7F：新增 _monday_window_stats() 用Binance 5m K线代码计算周一开盘窗口实测数据（开高低收/涨跌幅/振幅/PDH-PDL清扫判定），修复7E点评幻觉（AI未获窗口K线时编造走势） |
| 2026-07-06 | 订正文档笔误：OKX面值系数 0.001→0.01（代码本来就是0.01，monitor/liquidation_monitor.py从未错），错误公式实际写在 PROJECT_CONTEXT-v1.md 历史Bug表里，已订正两处 |
| 2026-07-06 | Phase 7G：新增常驻信号引擎(btc-signal-engine)，综合分阈值触发+模拟信号登记+结果自跟踪(stop/t1/t2/expired)，新表engine_signals，新增只读端点/api/signal/latest，为Phase 5B积累引擎信号样本 |
| 2026-07-06 | Bug#30（7G发现）：supervisor用绝对路径启动子目录脚本(monitor/signal_engine.py)时sys.path[0]是脚本自身目录而非directory=cwd设置，导致import utils等同级包失败(ModuleNotFoundError)；修复：脚本顶部显式sys.path.insert(0,"/opt/btc-trader") |
| 2026-07-06 | Bug#31（7H发现）：atas_bars表长期缺少唯一约束，AtasBridge.dll重连/重推同一根K线会被当成新行插入，产生同时间戳多行的隐性重复；修复：加(exchange,market_type,timestamp,timeframe)唯一索引+/atas/bar改INSERT OR REPLACE，历史5857→5760行(97条重复)去重，07-01两行OKX永续×100污染值订正 |
| 2026-07-06 | Phase 7H 阶段1（侦察）：AtasBridge.dll v2026.07.06-1新增身份侦察模式(ShowIdentityLabel)，图表角标+日志摊开显示InstrumentInfo/TradingManager.Security全部原始身份字段，只加不改，交付后等Sea四张图截图确认真实取值再做阶段2自动解析(7F教训：不凭API文档假设解析规则) |
| 2026-07-06 | Phase 7H 阶段2（正式）：AtasBridge.dll v2026.07.06-2，基于四图真实截图确认的规则——仅用InstrumentInfo.Exchange精确匹配(Binance/BinanceFutures/OkxSpot/OkxPerpFutures四值，OKX两图TradingManager.Security恒为null不可用)，新增IdentityMode(Auto默认/Manual)，OKX×0.01换算与三个推送方法的exchange/market_type字段统一改用ResolveEffectiveIdentity()最终生效身份，Auto解析失败等同Unset(不猜测)，Auto与手动下拉框冲突时角标变黄但数据仍按Auto值；角标从阶段1原始字段摊开改为运营状态显示(AUTO/MANUAL+推送✓/✗+时间) |
| 2026-07-06 | Bug#32（7I发现）：AtasBridge.dll只在ATAS X(Avalonia渲染,SDK v8.0.14.644)编译测试过，Sea导入普通版ATAS Platform(WPF渲染,SDK v8.0.14.290)时报ReflectionTypeLoadException缺Avalonia.Base；反射逐项核对两版本SDK发现核心API一致，仅LineTillTouch构造函数的Pen参数类型不同(ATAS X用UniversalPen/普通版用System.Drawing.Pen)，且普通版Platform目录自带的System.Drawing.Common.dll是过时的v8.0.0.0(它自己的ATAS.Indicators.dll实际要v10.0.0.0，需从.NET共享框架解析)；修复：新增AtasBridge.Platform.csproj共用同一份AtasBridge.cs，用#if ATAS_PLATFORM分支处理Pen差异，两个csproj各自编译产出双平台DLL
| 2026-07-06 | 任务卡7I：DLL信号显示层+角标改进+双平台构建支持——AtasBridge.dll v2026.07.06-3~5，轮询GET /api/signal/latest(7G预埋,服务器零改动)仅在Binance|Perp图绘制entry/stop/t1/t2四条价格线(HorizontalLinesTillTouch)+图表上方ENGINE信号行，终态信号变灰30分钟后自动清除，轮询失败不清线；角标位置改可设置(LabelPosition+OffsetX/Y，默认左下)，状态字符从✓/✗/≠改纯ASCII(OK/ERR(n)/!=，此前非ASCII字符在Sea机器上渲染成方块)；新增AtasBridge.Platform.csproj支持普通版ATAS(Bug#32)，确立"每次升级须同时交付两平台构建"的核心约定
| 2026-07-11 | 任务卡7J：面板信号展示+图表历史信号标记——新增只读端点GET /api/signal/history?days=7(main.py纯新增)；web/index.html新增信号展示区块(当前信号+近7天历史，独立REST轮询)；AtasBridge.dll v2026.07.11-1改用history端点轮询，当前信号仍完整四线，历史信号简化文字标记(entry价位+方向+结果，按时间戳二分查找K线定位)；发现并补记重要部署缺口：web/index.html实际部署在/www/wwwroot/mb.661688.xyz/(独立拷贝非软链接)，改仓库文件不会自动生效，需手动cp部署
| 2026-07-11 | 任务卡7K：数据推送总开关+设置面板中文化——AtasBridge.dll v2026.07.11-2，新增EnableDataPush总开关(默认true，关闭后K线/大单/吸收三路推送全停，引擎信号显示不受影响；身份角标显示原本受影响——见2026-07-12 Bug#34修复)，解决Sea在ATAS X和普通版ATAS两平台重复挂图但只想一边推数据的需求；设置面板24处Display(Name=/GroupName=)全部改中文(6个分组+各设置项)，枚举值本身保留英文(避免破坏ToString()回读逻辑)；澄清"渲染字符串须ASCII"规则适用范围仅限图表画布绘图+日志+JSON payload，不含原生设置面板文本
| 2026-07-12 | Bug#33：signal_tracker.py(Phase 5A吸收信号结果追踪)的get_current_price()查binance_structure表的price列(真实列名是mark_px)，异常被except吞掉返回None，check_outcomes()拿到None直接跳过整轮，导致4H/24H结果回查从服务上线(2026-06-30)起从未真正跑通，11万3千+条信号积压12天；同时修正设计缺陷——原逻辑整轮用同一个"现价"给所有到期信号结算，若有积压会导致全部信号被错误地按补跑那一刻的价格计算涨跌；改为新增get_price_at_time()按atas_bars历史K线回填每条信号自己到期时刻的真实历史价，每500条提交一次避免长时间占写锁；补算前备份atas_signal_outcomes全量；修复后立即补算完成4H结果98710条+24H结果67834条，历史价100%查到零跳过
| 2026-07-12 | Bug#34：AtasBridge.dll身份角标颜色逻辑缺陷——EnableDataPush总开关关闭(双开ATAS只想一边推送时的正常用法)会导致_lastPushOk永远停在默认值false，角标被误显示成"推送失败"的橙红色，实际只是"从未推送过"；ComputeIdentityLabel()状态拆成三档：总开关关闭→灰色OFF，开着但还没推送过→灰色...，真正推送过→绿色OK/橙红色ERR(n)；仅改颜色判断逻辑，不影响推送行为；版本v2026.07.12-1，已双平台编译部署
| 2026-07-13 | 任务卡7N：信号引擎循证校准+每轮遥测落库+ETF缓存——阈值±60→±25、迟滞带±40→±15（循证依据：signal_scores 9日54样本区间[-16,+33]、|分|≥40零次；引擎日志218样本mean=13.0/std=7.1/p95=25/max=27，±60对LONG≈6.6σ实证不可达，±25≈p95，配迟滞+90分钟冷却预估1-4信号/日，30终态样本约2-5周）；新增engine_scores遥测表(每轮成功评分落一行，跳过轮不写，与signal_scores严格分离防污染简报环比)；ETF维度进程内1小时TTL缓存(此前每5分钟全量爬双源288次/天有封IP风险，缓存过期且实取失败仍宁缺勿假跳过)；新阈值冻结至engine_signals累积≥30个终态样本；顺带订正采集服务真实路径monitor/binance_data_service.py(文档此前误写services/btc_binance_data_service.py) |
| 2026-07-13 | Bug#36：ETF 12:00确认推送(etf_confirm_push.py)三重失效——(1)cron时区错配：系统时区Asia/Shanghai，cron行"0 4 * * 2-6"的注释按UTC假设(=北京12:00)，实际按本地时区在北京04:00触发，正是披露窗口开启时刻；(2)零执行迹象：cron自2026-07-02装入后7+次调度机会，logs/etf_confirm_push.log从未被创建；(3)v1设计从SQLite etf_flow表/morning_brief_cache.json读数但从无代码写入过这两处，v1从未真正出过数据(v2已改调真实fetch_etf_flows，但因(1)(2)未证实跑过)；处置：cron行删除，脚本与utils/etf_timing.py(仅注释引用零代码引用)一并git rm退役归档/root/btc-deploy/retired/，功能由7M正午简报整体取代 |
| 2026-07-13 | 任务卡7M：正午简报+早盘去ETF+ETF稳定视图+cron清退——新增noon会话(周二至周六SGT12:00即UTC04:00，run_daily days=(2,3,4,5,6)按PTB v22.8实测约定0=周日6=周六，7节结构，max_tokens=2500，WP标题"BTC 正午简报·时间")；morning拆独立分支去ETF节13→12节(DATA块ETF段换单行提示，综合信号分ETF维度标注稳定数据日)，morning_monday独立分支原13节逐字节保留(V9源码diff零差异，待7P整体替换)；ETF稳定视图：etf_data.py v5新增stable_flow_m/stable_date返回字段(is_settling=False时落state持久化，修正state整体覆盖写bug)，signal_score._score_etf单日分量只用stable值(缺失→记0+etf_source=missing标注)，signal_engine stable缺失时宁缺勿假跳过本轮(7N的1小时ETF缓存保留，stable字段随缓存)；publisher收窄解禁(Sea补充裁定)：publish_briefing仅新增可选session_title参数，默认None标题行为与既往完全一致(V11实测验证)，仅noon调用点传"正午简报"(V10实测验证)；已知副作用已注释入码：ETF维度每日12:00后阶跃、周累计分量仍含披露窗口内阶段值(残余≤10综合分点待裁定) |
| 2026-07-13 | 任务卡7M-2：ETF稳定视图补全累计口径——消除7M遗留的周累计残余(披露窗口内total_week混入当日阶段值，最大≈10综合分点，±25新阈值下足以误触发)；etf_data.py v6新增stable_week_m(实现选①逐日行按日期<=stable_date区间求和，parsed本就是逐日行无需等价算法；周初边界stable_date属上周时区间为空和为0天然非负；月累计不参与评分不加stable_month_m)；signal_score._score_etf周分量改用stable_week_m(缺失→该分量按0计)，detail_json增记etf_week_source(stable/realtime/missing)与etf_stable_week_m；引擎零改动(7N缓存的是fetch结果整体，stable_week_m随缓存自动生效)；简报正文周/月累计展示保持实时口径；V1实测重启前后etf_s均43.0且非窗口期stable_week_m==total_week数值恒等 |
| 2026-07-13 | 任务卡7P：周报体系+morning_monday退役——新建briefing/weekly_briefing_data.py周数据聚合模块(W1-W10权威数据块，周边界=周一北京08:00，每块独立容错宁缺勿假；W6交易日分组用SQLite date()把ISO+08:00转UTC日期恰等于08:00日界)；briefing.py删morning_monday分支(V6实测morning/noon/europe/evening/ondemand五分支与备份零差异)原位换weekly 12节分支(MAX_TOKENS 8192)；daily_briefing.py周一路由morning→weekly、SESSION_NAMES换键、weekly注入get_weekly_context()(失败降级继续+ERROR日志)、WP标题传session_title=周报；实测：周报6084字end_turn 12节齐全(W4象限占比合计100.0%、W2逐日5行、W6每日Delta 7行、W9引擎段全库终态0/30容错文案)；顺带发现既有问题：morning本体9412字撞8192上限stop_reason=max_tokens截尾(非7P引入，morning分支零改动，留待后续卡处理)；F&G文档不符已订正(实情：7P前代码无任何F&G采集点) |
| 2026-07-22 | Bug#37：ETF稳定视图state单点故障——7M/7M-2的stable_flow_m/stable_date存在data/etf_state.json，且"只在is_settling=False(披露窗口外)写、窗口内只读"；_save_state非原子写(open+dump)且被signal_engine/各简报/weekly多进程并发调用，一次半截写→_load_state读到无效JSON返回{}→丢稳定键；键在披露窗口(北京04-12)内丢失后要等12:00窗口外才自愈，期间ETF维度→missing→信号引擎宁缺勿假整轮跳过(2026-07-22实测停摆3.5h、engine_scores断档)。修复(仅改data_collector/etf_data.py)：①稳定值改为每次从parsed逐日行推导(确认日=非窗口parsed[-1]/窗口内parsed[-2])彻底去掉state依赖，正常日推导值与原state存值完全一致不改评分；②_save_state改pid临时文件+os.replace原子写。实测修复后引擎当场恢复出分(etf_source=stable/数据日2026-07-20)，_score_etf得78分非缺失，signal_scores未被污染 |
| 2026-07-22 | Bug#38：信号引擎结果跟踪与评分输入耦合——check_outcomes()(盯已开信号止盈止损，只依赖当前价)原排在run_cycle里compute_current_score()的"if result is None:return"之后，任一评分输入缺失(如ETF稳定视图暂缺/Bug#37停摆)就连带把持仓跟踪一起跳过；实证后果：#29(06:40 LONG)在08:04真实触及T2(+2R)却因停摆漏记，修好Bug#37后11:15检测到跌破止损被错记成stopped(-1.12R)，一笔赢单记成亏单。修复(仅改monitor/signal_engine.py)：check_outcomes()提到评分门之前无条件每轮执行，评分中断只影响"发不发新信号"不影响"盯不盯持仓"。并手工订正#29为t2_hit(首次触及T2在08:04:59,outcome_price=66576=+2R,有atas_bars铁证)。订正后28终态样本：胜率8/28=28.6%、R累计-6.12R |
| 2026-07-22 | P0/Bug#39：冷却期改为拦"信号生成"而非只拦TG——策略审核发现引擎同一方向状态被反复计数(3h内20对同向连发、31条信号全部ETF单维主导)，根源是原设计"冷却90分钟只影响发TG不影响记库"，导致一个偏多状态灌成一堆强相关样本污染胜率统计。修复(仅改monitor/signal_engine.py)：冷却闸移到fire_signal顶部拦截信号生成本身(同向距上次生成不足90分钟→不记库不发TG直接返回)；迟滞状态机check_trigger一字未动(返回direction时仍disarm，故被冷却拦下后需分数回落±15重新武装才可能再触发，迟滞+冷却双重闸)；last_tg_ts语义改为last_signal_ts。离线单测验证：一段震荡分数序列旧逻辑生成7条连发、新逻辑只放行2条(首条+90分钟后新episode)。这是攒干净独立样本的前置(P0)；ETF单维主导(P1)、追高+紧止损(P2)属信号质量调优，待P0攒够样本后再动 |

### 路线图（7系列，本表未覆盖的更早阶段详见上表）
- [x] 7A / 7A-2 / 7A-3：简报综合信号分代码化 + 14档三因子映射 + Markdown清洗
- [x] 7B：Telegram /pos 仓位风控计算命令
- [x] 7E / 7E v2：周一 TradFi 周初开盘窗口提示 + 插针TG预警
- [x] 7F：AtasBridge 原生吸收检测 + 全字段 Telegram 推送
- [x] 7G：VPS 常驻信号引擎（阈值触发 + 模拟信号登记 + 结果自跟踪）
- [x] 7H：图表身份自动识别（阶段1侦察 + 阶段2正式）+ atas_bars 去重与订正
- [x] 7I：DLL 信号显示层（轮询 `/api/signal/latest`，Binance|Perp 图画
  entry/stop/t1/t2 价格线 + ENGINE 信号行，终态30分钟后自动清除）+ 角标
  位置可设置 + 状态字符改纯ASCII + 双平台构建支持（ATAS X / ATAS
  Platform，见 Bug#32 与核心约定）
- [x] 7J：面板信号展示（mb.661688.xyz新增信号卡片：当前信号+近7天历史）
  + 图表历史信号标记（AtasBridge.dll改轮询/api/signal/history，历史
  信号简化文字标记，7天窗口自动增删）+ 补记web/index.html独立部署位置
- [x] 7K：AtasBridge.dll 数据推送总开关（EnableDataPush，双平台分工
  推送 vs 展示）+ 设置面板中文化（24项Display标签，枚举值保留英文）
- [x] 7N：信号引擎循证校准（±25/±15，冻结至终态样本≥30）+ engine_scores
  每轮遥测落库 + ETF维度1小时TTL缓存
- [x] 7M：正午简报noon(周二至周六12:00，ETF确认+亚盘复盘) + 早盘去ETF节
  (13→12节) + ETF稳定视图(量化评分只用已确认完整交易日数据) +
  etf_confirm_push/etf_timing退役(Bug#36)
- [x] 7M-2：ETF稳定视图补全累计口径（stable_week_m，单日+累计全口径，
  消除披露窗口内周分量阶段值残余）
- [x] 7P：周报体系（周一09:30周报取代morning_monday，12节全数据背书，
  briefing/weekly_briefing_data.py W1-W10聚合模块）

---

## 十九、注意事项与已知问题

1. **德国IP限制**：Binance 永续合约 WebSocket 被地理封锁，清算监控改用 OKX WS
2. **WordPress REST API 封锁**：德国IP无法访问，改用 WP-CLI 本地命令行发布
3. **ETF 数据延迟**：各发行商报告有时间差，12:00正午简报承担当日数据确认（7M起；原etf_confirm_push二次推送从未跑通已退役，见Bug#36）
4. **CME 缺口自动退休**：当 BTC 涨过 $80,400，三个历史缺口全部填补，`_cme_block()` 自动输出退休提示
5. **GitHub 同步滞后**：白天手动部署的文件要等次日 03:00 才同步到 GitHub
6. **startup_guard.py 和 git_sync.sh**：这两个文件在 VPS 有，在 GitHub 暂时没有（需手动加入 git add 列表）

### AI 简报历史 Bug 修复记录（AI 心算/编造导致的输出错误，编号顺延）：

| # | Bug | 原因 | 修复 |
|---|---|---|---|
| 1 | 综合信号分与六维加权和对不上 | AI 自己心算综合分，2026-07-04实例：自报 composite=-12 但六维加权和=-4.8，两个数字互相矛盾 | 7A-2：改为 utils/signal_score.py 代码确定性计算，AI 只解读不计算 |
| 2 | 周一窗口点评幻觉 | prompt 要求点评周一开盘窗口价格行为，但未注入窗口K线数据，AI 拼接其他区块数字编造走势（2026-07-06实例：编造63,115→63,617温和上行，实际62,610→约63,900强拉2%） | 7F：新增 _monday_window_stats() 用 Binance 5m K线代码计算窗口实测数据，作为权威数据块注入，AI 只解读不推算 |

---

## 二十、新对话开始时的标准流程

```
1. 粘贴本文档到对话开头
2. 说明需求
3. Claude 读取 GitHub 最新代码
4. 确认"今天是否有新部署未同步到GitHub"
5. 以 VPS 实际状态为准进行修改
6. 生成新文件 → 用户 scp 上传 → supervisorctl restart → 验证
```

| 2026-07-11 | 7J：新增 scripts/gen-index.sh，自动生成 PROJECT_INDEX.md（文件树/API路由/环境变量名/Supervisor状态）；CLAUDE.md 增加会话开始必读索引规则 |
