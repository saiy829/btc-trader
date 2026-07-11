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
│  btc-binance-data      services/btc_binance_data_service.py     │
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
│   └── structure_monitor.py    象限+多空比实时预警
│
├── services/
│   ├── btc_binance_data_service.py  ★ 5分钟采集服务
│   │                                  将 OI/Funding/L/S/象限 写入 SQLite
│   └── etf_confirm_push.py          ETF 12:00 二次确认推送
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

由 `btc_binance_data_service.py` 每 5 分钟采集写入：

| 表名 | 内容 | 主要字段 |
|---|---|---|
| `binance_oi` | 持仓量快照 | `ts`, `oi_usd`, `oi_btc` |
| `binance_funding` | 资金费率历史 | `ts`, `rate`, `next_settle`, `premium_pct` |
| `binance_ls_top` | 大户多空比 | `ts`, `ls_ratio`, `long_pct`, `short_pct` |
| `binance_ls_global` | 全市场多空比 | `ts`, `ls_ratio`, `long_pct`, `short_pct` |
| `binance_structure` | 5分钟象限 | `ts`, `quadrant`, `note`, `oi_chg`, `px_chg` |
| `signal_scores` | 综合信号分历史（2026-07-04 Phase 7A-2新增） | `ts`, `session`, `composite`, `label`, `etf_s`, `fr_s`, `quad_s`, `ls_s`, `cb_s`, `regime_s`, `detail_json` |
| `engine_signals` | 信号引擎模拟信号历史（2026-07-06 Phase 7G新增） | `id`, `created_at`, `direction`, `score`, `dims_json`, `entry`, `stop`, `t1`, `t2`, `atr`, `status`, `t1_touched`, `outcome_price`, `outcome_at` |
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
| `morning` | 北京时间 09:30（UTC 01:30） | 自动定时 |
| `morning_monday` | 周一 09:30 | 自动（周一自动替换 morning） |
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

### 早盘简报 13 节结构（morning / morning_monday）：
```
1. 宏观背景评级（A/B/C/D）
2. BTC 现货 ETF 资金流向解读
3. CME 历史缺口追踪           ← 原"缺口分析"，2026-06-27 更新
4. 今日 IB 分析·开盘类型确认
5. 昨日 Market Profile 结构
6. 昨日 Volume Profile 概览
7. 流动性分布·Stop Hunt 分析
8. 衍生品深度解读              ← 含Z-score + 三因子状态解读
9. AMT 市场状态·今日框架       ← 引用三因子分类判断趋势质量
10. Kill Zone 时间窗口计划
11. 具体操作方案（做多/做空/观望）
12. 风险管理·止损设置
13. 一句话总结
```

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
  | `THRESH_LONG` | +60 | 综合分上穿 → LONG 信号 |
  | `THRESH_SHORT` | -60 | 综合分下穿 → SHORT 信号 |
  | `REARM_BAND` | ±40 | 迟滞带：分数回落到此范围内才允许再次武装 |
  | `COOLDOWN_MIN` | 90分钟 | 同方向信号最小间隔，只影响是否发TG，不影响是否记库 |
  | `ATR_STOP_MULT` | 1.5× | 止损距离（15分钟ATR(14)倍数） |
  | `ATR_T1_MULT` / `ATR_T2_MULT` | 1.5× / 3.0× | 目标1(1R) / 目标2(2R) |
  | `EXPIRE_HOURS` | 24小时 | 开仓超过此时长未触及任何边界标记到期 |
  - 引擎重启后以"未武装"状态启动，需分数先回落到 ±40 以内才武装，
    避免重启瞬间对着高分立即补发信号
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
| ETF 12:00 补发 | `/opt/btc-trader/services/etf_confirm_push.py` | Cron 每天周二至周六 12:00 |
| CME缺口 | Binance现货近似（v2改为静态历史追踪） | CME 24/7后不再动态计算 |
| Coinbase溢价 | Binance+Coinbase价差计算 | 机构动向指标 |
| Fear & Greed | 外部API | 已集成到简报数据块 |

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
| 2026-07-11 | 任务卡7K：数据推送总开关+设置面板中文化——AtasBridge.dll v2026.07.11-2，新增EnableDataPush总开关(默认true，关闭后K线/大单/吸收三路推送全停，身份角标和引擎信号显示不受影响)，解决Sea在ATAS X和普通版ATAS两平台重复挂图但只想一边推数据的需求；设置面板24处Display(Name=/GroupName=)全部改中文(6个分组+各设置项)，枚举值本身保留英文(避免破坏ToString()回读逻辑)；澄清"渲染字符串须ASCII"规则适用范围仅限图表画布绘图+日志+JSON payload，不含原生设置面板文本

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

---

## 十九、注意事项与已知问题

1. **德国IP限制**：Binance 永续合约 WebSocket 被地理封锁，清算监控改用 OKX WS
2. **WordPress REST API 封锁**：德国IP无法访问，改用 WP-CLI 本地命令行发布
3. **ETF 数据延迟**：各发行商报告有时间差，设计了 12:00 二次确认推送机制
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
