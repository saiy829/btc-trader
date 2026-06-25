# BTC AI 交易系统 · 项目上下文文档
# 版本：v8（最终完整版）| 更新时间：2026-06-17
# 用途：在新对话中粘贴给 Claude，立刻恢复全部上下文

────────────────────────────────────────────────────────────

## 一、项目概述

BTC USDT 永续合约日内交易 AI 辅助系统
交易者：Sea（是 BTC 永续合约交易者）
交易框架：AMT → Market Profile → Volume Profile → Order Flow
主交易平台：ATAS（订单流分析软件）
主要交易标的：Binance USDT永续合约（BTCUSDT）
AI 模型：Claude claude-sonnet-4-5（Anthropic API）
目标：AI 自动完成开盘前分析、实时预警，人工做最终交易判断

────────────────────────────────────────────────────────────

## 二、VPS 服务器

服务商：Hetzner（法兰克福节点）
规格：4核 / 8GB DDR5 / 256GB NVMe
系统：Ubuntu 22.04.5 LTS
项目目录：/opt/btc-trader/
Binance REST API：可直连（已验证）
【重要】Binance 期货 WebSocket（fstream.binance.com）
       对德国 IP 静默封锁，已移除，改用 OKX/Bybit/Hyperliquid

────────────────────────────────────────────────────────────

## 三、已安装软件

运行时：
  Python 3.11.9（pyenv，位于 /root/.pyenv/）
  虚拟环境：/opt/btc-trader/venv/
  Node.js 20 LTS（nvm）

数据库：
  PostgreSQL 16 + TimescaleDB
  Redis 7
  MySQL 8.0（aaPanel 管理）

Web：
  Nginx + PHP 8.2 + WordPress（aaPanel）
  WP-CLI 2.12.0（/usr/local/bin/wp）
  网站：https://jianbao.661688.xyz
  WP 路径：/www/wwwroot/jianbao.661688.xyz

工具：
  Supervisor 4.x（守护所有 Python 服务）
  Git（SSH Key：~/.ssh/github_btc → btc-ai-trader 私有仓库）
  Netdata（监控，端口 19999）

Python 依赖（venv 已安装）：
  anthropic, python-binance, aiohttp, pandas, numpy,
  psycopg2-binary, redis, python-telegram-bot[job-queue],
  APScheduler, requests, websockets, python-dotenv,
  loguru, httpx, tenacity, pendulum, beautifulsoup4,
  SQLAlchemy, pydantic, aiofiles

────────────────────────────────────────────────────────────

## 四、项目文件结构

/opt/btc-trader/
├── .env                      # API 密钥（不上传 Git）
├── daily_briefing.py         # 简报主程序 v5（5步 + 3种会话类型）
├── scheduler.py              # 调度器 v3（python-telegram-bot JobQueue）
├── run_liquidation.py        # 清算监控入口
├── run_funding.py            # Funding 监控入口
├── run_oi.py                 # OI 监控入口
├── run_dom.py                # 订单簿监控入口
├── PROJECT_CONTEXT.md        # 本文档
├── requirements.txt
├── venv/
├── utils/
│   └── helpers.py            # setup_logger/get_env/fmt_time/fmt_usd/now_sgt/SGT
├── data_collector/
│   ├── binance_data.py       # 永续价格/K线/Funding/OI/多空比/IB计算/现货/CB溢价
│   └── multi_funding.py      # 5所 Funding Rate（Binance/OKX/Bybit/Bitget/Gate）
├── ai_analyst/
│   ├── briefing.py           # Claude 简报（morning/evening/ondemand 三种 prompt）
│   └── liq_briefing.py       # 清算事件专项 Claude 分析
├── alert_bot/
│   └── send.py               # send()同步 + async_send()异步
├── monitor/
│   ├── liquidation_monitor.py # 清算监控 v5（OKX WS + Bybit WS + Hyperliquid 轮询）
│   ├── funding_monitor.py    # Funding Rate 监控（5所，每5分钟）
│   ├── oi_monitor.py         # OI 异动监控（每5分钟）
│   └── dom_monitor.py        # 订单簿大额挂单 v3（OKX books + Bybit orderbook.200）
├── publisher/
│   └── wordpress.py          # WP-CLI 发布 v6（表格式头部）
└── logs/

────────────────────────────────────────────────────────────

## 五、.env 配置（已配置并验证）

BINANCE_API_KEY        已配置（只读，IP 白名单）
BINANCE_API_SECRET     已配置
ANTHROPIC_API_KEY      已配置（claude-sonnet-4-5 验证）
TELEGRAM_BOT_TOKEN     已配置（已验证）
TELEGRAM_CHAT_ID       已配置（个人 User ID）
COINGLASS_API_KEY      已配置（免费版，接口受限，暂未使用）
WP_URL                 https://jianbao.661688.xyz
WP_USERNAME            xinghe
WP_APP_PASSWORD        已配置
DB_HOST/PORT/NAME      localhost/5432/btctrader
REDIS_HOST/PORT        localhost/6379

# 可调阈值（改后重启对应服务生效）
LIQ_SINGLE_USD=50000       # 清算单笔预警（当前5万）
LIQ_HOURLY_USD=3000000     # 清算1H累计（当前300万）
DOM_ALERT_USD=10000000     # 挂单标准预警（1000万）
DOM_STRONG_USD=50000000    # 挂单强力预警（5000万）
DOM_MEGA_USD=100000000     # 挂单紧急预警（1亿）
DOM_PENDING_SEC=15         # 挂单持续秒数才预警
DOM_MIN_DIST_PCT=0.5       # 忽略距价格0.5%以内的挂单

────────────────────────────────────────────────────────────

## 六、Supervisor 服务（5个，均 RUNNING）

btc-briefing
  /etc/supervisor/conf.d/btc-briefing.conf
  程序：scheduler.py v3（python-telegram-bot JobQueue）
  定时：UTC 01:30（SGT 09:30）早盘简报 含 IB 分析
        UTC 12:30（SGT 20:30）晚盘简报 美盘前准备
  命令：/b 或 /status（Telegram Bot 响应）

btc-liq-monitor
  /etc/supervisor/conf.d/btc-liq-monitor.conf
  程序：run_liquidation.py → liquidation_monitor.py v5
  覆盖：OKX WS + Bybit WS + Hyperliquid REST 轮询
  触发：单笔 > $5万 立即预警 | 1H累计 > $300万 Claude AI 简报
  注意：Binance 期货 WS 德国IP封锁，已移除

btc-funding-monitor
  /etc/supervisor/conf.d/btc-funding-monitor.conf
  程序：run_funding.py → funding_monitor.py
  覆盖：Binance/OKX/Bybit/Bitget/Gate.io 5所，每5分钟
  触发：单所超 ±0.05% | 3所共识极端 | 单所超 ±0.10% 紧急

btc-oi-monitor
  /etc/supervisor/conf.d/btc-oi-monitor.conf
  程序：run_oi.py → oi_monitor.py
  触发：1H OI 变化 > 5% 标准 | > 10% 紧急
  自动判断：真实建仓 vs 挤空/挤多 + Funding 一致性

btc-dom-monitor
  /etc/supervisor/conf.d/btc-dom-monitor.conf
  程序：run_dom.py → dom_monitor.py v3
  覆盖：OKX books（400档）+ Bybit orderbook.200
  触发：挂单持续15秒才预警，3档（$1000万/$5000万/$1亿）
  额外：Spoof 幽灵单检测（$5000万+60秒内撤销）
  注意：Binance 期货 WS 德国IP封锁，已移除

────────────────────────────────────────────────────────────

## 七、已完成功能（全部验证可用）

简报系统：
  [✅] 早盘简报（SGT 09:30）含 IB 分析 + 当日交易计划
  [✅] 晚盘简报（SGT 20:30）欧盘收盘 + 美盘前准备
  [✅] /b 命令随时触发实时简报
  [✅] 数据：Binance 永续 + 现货 + 5所Funding + OI + 多空比
            Coinbase BTC/USD 溢价（机构资金流向指标）
            今日 IB（Binance永续5分钟K线，精确startTime）
  [✅] WordPress 自动发布（WP-CLI，表格式头部，绿涨红跌）
  [✅] 标题格式：BTC 交易简报 · YYYY-MM-DD HH:MM

预警系统：
  [✅] 清算预警（OKX/Bybit/Hyperliquid）含交所标注 + 1H累计
  [✅] Funding 极端预警（3档：±0.05%/多所共识/±0.10%）
  [✅] OI 异动预警（5%/10%，自动分类真实建仓vs挤仓）
  [✅] 订单簿大额挂单（OKX+Bybit，15秒确认，Spoof检测）

全局规范：
  时间：SGT（UTC+8），fmt_time()/now_sgt()
  金额：fmt_usd() 含中文亿/万单位
  颜色：绿涨红跌（西方惯例）
  名称：永续合约（非"期货"）

────────────────────────────────────────────────────────────

## 八、已知限制

Binance 期货 WebSocket（fstream.binance.com）：
  德国 IP 静默封锁，连接成功但不推送数据
  影响：实时清算监控和订单簿监控不含 Binance 数据
  不影响：REST API（价格/OI/Funding/日报数据全部正常）

Bybit 清算 WebSocket v5：
  liquidation.BTCUSDT topic 已从 v5 API 移除
  现用：Bybit REST 轮询或 WS publicTrade

Coinglass 免费 API：
  主要接口需要付费，免费版接口受限
  现用：各交所公开 API 自行聚合

────────────────────────────────────────────────────────────

## 九、常用运维命令

supervisorctl status                      # 查看所有服务
supervisorctl restart btc-briefing        # 重启调度器+Bot
supervisorctl restart btc-liq-monitor     # 重启清算监控
supervisorctl restart btc-dom-monitor     # 重启订单簿监控

tail -f /opt/btc-trader/logs/liq_monitor_out.log
tail -f /opt/btc-trader/logs/dom_monitor_out.log
tail -f /opt/btc-trader/logs/funding_monitor_out.log
tail -f /opt/btc-trader/logs/supervisor_out.log

# 手动测试简报
cd /opt/btc-trader && source venv/bin/activate
python -c "from daily_briefing import run; run('morning')"
python -c "from daily_briefing import run; run('evening')"
python -c "from daily_briefing import run; run('ondemand')"

# 调整清算阈值（改后重启生效）
nano /opt/btc-trader/.env
supervisorctl restart btc-liq-monitor

────────────────────────────────────────────────────────────

## 十、GitHub 仓库

仓库：btc-ai-trader（Private）
SSH Key：~/.ssh/github_btc（已配置）
.env 不上传（.gitignore 已排除）

────────────────────────────────────────────────────────────

## 十一、给新 Claude 对话的开场白（直接复制使用）

"我有一个正在运行的 BTC AI 交易系统项目，以下是完整的项目文档，
请阅读后继续协助我开发。当前需要做的是：[说明任务]"

然后把本文档全文粘贴即可。
Claude 阅读后可以立刻接续所有工作，无需重新解释背景。


---
## 更新记录 v9（2026-06-17）—— 简报系统重大升级

新增数据模块：
  data_collector/etf_data.py    - BTC ETF资金流量（Farside+备用SoSoValue）
  data_collector/cme_data.py    - CME期货缺口计算（Binance近似CME价格）

修改数据模块：
  data_collector/binance_data.py
    - get_todays_ib()：修正为60分钟IB + 30分钟观察期
      正确数据：12根5分钟K线(UTC00:00-01:00) + 6根观察期(UTC01:00-01:30)
      输出：IB High/Low/Mid/Open + 开盘类型判断(OD/ORR/OA/OTD)
    - get_spot_and_extras()：现货价格 + 永续成交额 + Coinbase溢价
    - _fmt_vol()：成交额格式化（亿单位）

简报会话类型（5种）：
  morning        → 标准早盘（周二至周五）
  morning_monday → 周一加强版（含CME缺口专项）
  europe         → 欧盘（SGT 15:00）
  evening        → 美盘（SGT 20:30）
  ondemand       → /b 随时触发

简报内容结构（早盘12节）：
  1. 宏观背景评级 A/B/C/D
  2. BTC现货ETF资金流向（昨日/本周/本月/连续天数）
  3. CME期货缺口分析
  4. 今日IB分析（60分钟）+ 开盘类型确认（30分钟观察期）
  5. 昨日Market Profile结构（PDH/PDL/PDC）
  6. 流动性分布 + Stop Hunt分析（BSL/SSL）
  7. 衍生品深度解读（Funding+OI+CB溢价）
  8. AMT市场状态（平衡市/失衡市）
  9. 今日关键价格层（6-8个，含来源）
  10. 今日完整交易计划（含具体价格）
  11. ATAS订单流确认提示
  12. 一句话总结

调度时间（UTC/SGT）：
  UTC 01:30 = SGT 09:30 → 早盘（IB+ETF+CME缺口，周一自动升级加强版）
  UTC 07:00 = SGT 15:00 → 欧盘（伦敦开盘，London Kill Zone前）
  UTC 12:30 = SGT 20:30 → 美盘（NY Kill Zone前1小时）
  随时      = /b 命令   → 实时快速简报

WordPress文章标题格式：BTC 交易简报 · YYYY-MM-DD HH:MM（无SGT后缀）
颜色规范：绿涨红跌（西方惯例）











# BTC AI 永续合约辅助交易系统
> GitHub: https://github.com/saiy829/btc-trader
> VPS: Hetzner 德国法兰克福 | Ubuntu 22.04 | 项目目录: /opt/btc-trader
> 面板: https://mb.661688.xyz | 简报站: https://jianbao.661688.xyz

---

## 新对话协议

**每次开新对话时，把本文件内容粘贴到开头，然后说明需求。**
Claude 会直接 fetch GitHub 上的任何文件（仓库已设为 Public）：

```
我需要看 ai_analyst/briefing.py：
https://raw.githubusercontent.com/saiy829/btc-trader/main/ai_analyst/briefing.py
```

**VPS 操作前必做：**
```bash
supervisorctl status          # 确认服务状态
cd /opt/btc-trader && source venv/bin/activate
```

**代码修改后必做：**
```bash
cd /opt/btc-trader
git add .
git commit -m "说明改了什么"
git push
```

---

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                /opt/btc-trader (VPS)                │
│                                                     │
│  8个 Supervisor 服务                                 │
│  ├─ btc-api              FastAPI 后端 :8001          │
│  ├─ btc-briefing         简报调度（scheduler.py）     │
│  ├─ btc-binance-data     Binance数据采集（新增）       │
│  ├─ btc-structure-monitor 象限+多空比预警（新增）      │
│  ├─ btc-liq-monitor      OKX爆仓监控                 │
│  ├─ btc-dom-monitor      大单挂单监控                 │
│  ├─ btc-funding-monitor  资金费率极端值预警            │
│  └─ btc-oi-monitor       OI突变预警                  │
│                                                     │
│  数据库: /opt/btc-trader/btc_history.db (SQLite)    │
│  日志:   /opt/btc-trader/logs/                      │
│  配置:   /opt/btc-trader/.env（不进Git）             │
└─────────────────────────────────────────────────────┘
```

---

## 目录结构（完整）

```
/opt/btc-trader/
│
├── scheduler.py              # Telegram Bot + 三时段定时任务调度
├── daily_briefing.py         # 简报主流程（数据采集→AI分析→发送）
│
├── ai_analyst/
│   └── briefing.py           # Claude prompt构建 + API调用（核心）
│
├── alert_bot/
│   └── send.py               # Telegram消息发送（parse_mode='HTML'）
│
├── api/
│   └── main.py               # FastAPI后端（/api/data、/ws/live、/api/binance/*）
│
├── briefing/
│   └── binance_briefing_data.py  # Binance市场结构数据摘要（供简报注入）【新增】
│
├── data_collector/
│   ├── binance_data.py       # 价格/OI/K线/IB/VP数据采集
│   ├── multi_funding.py      # 5交所资金费率采集
│   ├── etf_data.py           # ETF资金流（SoSoValue+Farside双源）
│   └── cme_data.py           # CME缺口计算
│
├── monitor/
│   ├── liquidation_monitor.py  # OKX WebSocket爆仓监控
│   ├── funding_monitor.py      # 资金费率极端值预警
│   ├── oi_monitor.py           # OI突变预警
│   ├── dom_monitor.py          # 大单挂单监控
│   ├── binance_data_service.py # Binance数据采集服务（OI/费率/多空比）【新增】
│   └── structure_monitor.py    # 市场象限+多空比TG预警【新增】
│
├── publisher/
│   └── wordpress.py          # WP-CLI发布到jianbao.661688.xyz
│
├── utils/
│   └── helpers.py            # fmt_time/now_sgt/fmt_usd/setup_logger等工具
│
├── web/                      # 同步自 /www/wwwroot/mb.661688.xyz/
│   ├── index.html            # 主面板（Vue3 CDN，无需构建）
│   └── history.html          # 历史趋势图（lightweight-charts）
│
├── services/                 # 其他辅助服务
├── data/                     # 数据文件
├── ai_analyst/               # AI分析模块
│
├── .gitignore                # 排除 .env / *.db / logs/ / venv/
├── requirements.txt
└── PROJECT_CONTEXT.md        # 本文件
```

---

## 数据库表（btc_history.db）

### 原有表
| 表名 | 内容 |
|------|------|
| snapshots | 5分钟快照（价格/OI/Funding/CVD/CB溢价）|
| daily_summary | 每日汇总（IB/VP/MP/ETF/清算统计）|

### 新增表（2026-06 本次改造）
| 表名 | 内容 | 更新频率 |
|------|------|----------|
| binance_oi | OI持仓量（BTC+USD+mark_px）| 每5分钟 |
| binance_funding | 资金费率+溢价率+下次结算时间 | 每5分钟 |
| binance_ls_global | 全市场账户多空比 | 每5分钟 |
| binance_ls_top | 大户持仓多空比（Top 20%）| 每5分钟 |
| binance_structure | 市场象限快照（Q1/Q2/Q3/Q4/FLAT）| 每5分钟 |

---

## 四象限分析框架

| 象限 | OI | 价格 | 含义 | 信号强度 |
|------|-----|------|------|----------|
| Q1 | ↑ | ↑ | 多头新仓·趋势上涨 | 最强做多 |
| Q2 | ↑ | ↓ | 空头新仓·趋势下跌 | 最强做空 |
| Q3 | ↓ | ↑ | 空头爆仓·轧空反弹 | 弱多/谨慎 |
| Q4 | ↓ | ↓ | 多头爆仓·去杠杆 | 弱空/谨慎 |
| FLAT | - | - | 震荡积累 | 无明显方向 |

---

## API 接口（FastAPI :8001，Nginx反代）

### 原有接口
- `GET /api/data` → 面板全量数据
- `GET /api/health` → 健康检查
- `WS /ws/live` → WebSocket实时推送
- `GET /api/history/metrics` → 历史快照数据

### 新增接口（2026-06）
- `GET /api/binance/summary` → OI/费率/多空比/象限最新值
- `GET /api/binance/oi/history?hours=N` → OI历史时序
- `GET /api/binance/funding/history?hours=N` → 资金费率历史
- `GET /api/binance/ls/history?hours=N` → 多空比历史
- `GET /api/binance/structure?hours=N` → 象限历史

---

## Telegram 预警体系

| 服务 | 触发条件 | 冷却期 |
|------|----------|--------|
| btc-liq-monitor | 单笔爆仓 >10万USD / 1小时累计 >100万USD | - |
| btc-funding-monitor | 资金费率极端值 | - |
| btc-oi-monitor | OI突变异常 | - |
| btc-dom-monitor | 大单挂单 >50万USD | - |
| btc-structure-monitor | Q1/Q2象限确认（连续2次=10分钟稳定）| 60分钟 |
| btc-structure-monitor | 大户多空比 >3.0 或 <0.5 | 120分钟 |

---

## 简报时间表（北京时间）

| 时段 | 时间 | 会话类型 | 特殊规则 |
|------|------|----------|----------|
| 早盘 | 09:30 | morning | 周一升级为 morning_monday（含CME缺口专项）|
| 欧盘 | 15:00 | europe | 伦敦开盘前 |
| 美盘 | 20:30 | evening | NY Kill Zone前1小时 |
| 按需 | /b命令 | ondemand | Telegram随时触发 |

---

## 已知限制（德国IP）

| 服务 | 状态 | 解决方案 |
|------|------|----------|
| Binance 期货 WebSocket | 静默封锁 | 用 /fapi/v1/allForceOrders REST（已下线，已放弃）|
| Binance REST API | ✅ 正常 | 直连，无需代理 |
| Cloudflare Worker→Binance REST | 403封锁 | 放弃，VPS直连 |
| Cloudflare Worker→fstream.binance.com | 502封锁 | 放弃 |
| OKX WebSocket | ✅ 正常 | 主力爆仓数据源 |
| WordPress REST API | 封锁 | 改用 WP-CLI |

---

## 环境变量结构（.env）

```bash
# AI
ANTHROPIC_API_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# WordPress
WP_PATH=/www/wwwroot/jianbao.661688.xyz

# ETF数据
SOSOVALUE_API_KEY=

# Cloudflare Worker（WebSocket代理，目前未成功）
BINANCE_WORKER_URL=
BINANCE_PROXY_KEY=

# 预警阈值
LIQ_SINGLE_USD=100000
LIQ_HOURLY_USD=1000000
DOM_ALERT_USD=500000
DOM_STRONG_USD=1000000
DOM_MEGA_USD=5000000
DOM_PENDING_SEC=15
DOM_MIN_DIST_PCT=0.5
```

---

## 变更日志

### 2026-06-24（本次改造）
- 新增 `monitor/binance_data_service.py`：Binance OI/资金费率/大户多空比5分钟采集
- 新增 `briefing/binance_briefing_data.py`：市场结构数据摘要（供AI简报使用）
- 新增 `monitor/structure_monitor.py`：Q1/Q2象限确认+大户极端值TG预警
- 修改 `ai_analyst/briefing.py`：prompt中加入市场象限数据块，第8节指令更新
- 修改 `daily_briefing.py`：注入 binance["market_ctx"] 到数据流
- 修改 `api/main.py`：内联5个 /api/binance/* 接口
- 修改 `web/history.html`：新增「OI趋势(亿$)」和「大户多空比」图表Tab
- 新增5个数据库表（binance_oi/funding/ls_global/ls_top/structure）
- 建立 GitHub 私有仓库 saiy829/btc-trader（后改为Public）

---

## 交易方法论参考

**三层分析框架：**
1. Context（情境）：月线/周线结构，趋势/盘整/反转区间
2. Map（地图）：日线 Market Profile + Volume Profile，关键价格区域
3. Trigger（触发）：盘中 Order Flow（清算/OI/CVD/DOM）确认入场

**关键术语：**
- IB（Initial Balance）：首60分钟，判断当日波动范围基准
- POC：成交量最大价格 | VAH/VAL：价值区上下沿
- HVN/LVN：高/低成交量节点 | PDH/PDL/PDC：前日高低收
- CME缺口：周末BTC现货与周五CME期货收盘价之差
- Kill Zone：流动性最强时段（亚洲/伦敦/纽约开盘前后）
- BSL/SSL：多空双方止损密集区（机构扫流动性目标）

---

*文档生成：2026-06-24 | 下次更新：每次重大改动后 git commit 同步*
