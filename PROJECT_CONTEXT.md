# BTC AI 永续合约辅助交易系统 · 项目全局文档

```
GitHub  : https://github.com/saiy829/btc-trader （Public）
VPS     : Hetzne | Ubuntu 22.04 LTS
项目目录 : /opt/btc-trader/
Python  : pyenv 管理，venv 在 /opt/btc-trader/venv/
面板    : https://mb.661688.xyz
简报站  : https://jianbao.661688.xyz
```

---

## 一、新对话协议（必读）

**每次开新对话，把本文档内容粘贴到开头，然后说明需求。**

Claude 会直接 fetch GitHub 上任何文件：
```
https://github.com/saiy829/btc-trader/blob/main/ai_analyst/briefing.py
```

**VPS 常用命令：**
```bash
cd /opt/btc-trader && source venv/bin/activate
supervisorctl status
git add . && git commit -m "说明" && git push
```

**传文件到 VPS（Windows执行）：**
```cmd
scp -P 23456 本地文件 root@91.16.118.7:/opt/btc-trader/目标路径
```

---

## 二、系统架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    /opt/btc-trader/                         │
│                                                             │
│  8个 Supervisor 托管服务：                                   │
│  btc-briefing          scheduler.py                         │
│  btc-api               api/main.py  :8001                   │
│  btc-binance-data      monitor/binance_data_service.py      │
│  btc-structure-monitor monitor/structure_monitor.py         │
│  btc-liq-monitor       monitor/liquidation_monitor.py       │
│  btc-dom-monitor       monitor/dom_monitor.py               │
│  btc-funding-monitor   monitor/funding_monitor.py           │
│  btc-oi-monitor        monitor/oi_monitor.py                │
│                                                             │
│  数据库 : /opt/btc-trader/btc_history.db (SQLite)          │
│  日志   : /opt/btc-trader/logs/                            │
│  配置   : /opt/btc-trader/.env  ← 不进 Git！               │
└─────────────────────────────────────────────────────────────┘

数据流向：
  Binance REST API ──→ data_collector/* + monitor/binance_data_service
  OKX WebSocket ────→ monitor/liquidation_monitor
  SoSoValue+Farside→ data_collector/etf_data
  btc_history.db ──→ api/main.py → mb.661688.xyz
  所有数据 → AI Prompt → Claude API → Telegram + WordPress
```

---

## 三、完整目录结构

```
/opt/btc-trader/
│
├── scheduler.py              ★ 系统入口
│                               Telegram Bot + 三时段定时任务
│                               命令：/b 立即简报 / /status 系统状态
│                               定时：UTC 01:30(早盘) / 07:00(欧盘) / 12:30(美盘)
│
├── daily_briefing.py         ★ 简报主流程（v7）
│                               7步骤：数据采集→市场结构→AI分析→TG+WP发布
│                               会话类型：morning/morning_monday/europe/evening/ondemand
│
├── ai_analyst/
│   └── briefing.py           ★ Claude AI 核心（v6）
│                               build_prompt()：构建4种会话的完整提示词
│                               generate_briefing()：调用 Anthropic API
│                               MODEL: claude-sonnet-4-5
│                               MAX_TOKENS: 早盘8192/欧盘2500/美盘3000/按需2000
│
├── alert_bot/
│   └── send.py               Telegram 消息发送
│                               parse_mode='HTML'（重要，不能改）
│
├── api/
│   └── main.py               FastAPI 后端（端口 8001）
│                               /api/data         面板全量数据
│                               /api/health       健康检查
│                               /ws/live          WebSocket实时推送
│                               /api/history/metrics  历史快照
│                               /api/binance/*    Binance新增接口（内联）
│
├── briefing/
│   └── binance_briefing_data.py  ★ 新增 2026-06
│                               get_binance_context()
│                               读 binance_* 表 → 格式化字符串 → 注入AI简报
│
├── data_collector/
│   ├── binance_data.py       Binance数据采集（价格/OI/K线/IB/VP/CB溢价）
│   ├── multi_funding.py      5交所资金费率（Binance/OKX/Bybit/Bitget/Gate.io）
│   ├── etf_data.py           BTC ETF资金流（SoSoValue主+Farside备，差异>8%降级）
│   └── cme_data.py           CME期货缺口计算
│
├── monitor/
│   ├── liquidation_monitor.py  OKX WebSocket爆仓（主力实时清算源）
│   │                           坑：sz*0.001*price 才是真实USD金额
│   ├── funding_monitor.py    资金费率极端值预警
│   ├── oi_monitor.py         OI突变预警
│   ├── dom_monitor.py        大单挂单监控（Bybit订单簿）
│   ├── binance_data_service.py  ★ 新增 2026-06
│   │                           直连 fapi.binance.com（德国IP REST正常）
│   │                           采集：OI/资金费率/大户多空比/全市场多空比
│   │                           计算：市场象限（Q1/Q2/Q3/Q4）
│   │                           写入：binance_oi/funding/ls_*/structure 表
│   │                           轮询：每5分钟，无WebSocket
│   └── structure_monitor.py  ★ 新增 2026-06
│                               Q1/Q2确认（连续2次=10分钟）→ TG推送
│                               大户多空比 >3.0或<0.5 → TG推送
│                               冷却：象限60分钟 / 多空比120分钟
│
├── publisher/
│   └── wordpress.py          WordPress发布（WP-CLI，REST API被德国IP封）
│
├── services/                 辅助服务（待补充）
├── data/                     运行时数据（.gitignore排除）
│   └── etf_state.json        ETF状态缓存
│
├── utils/
│   └── helpers.py            setup_logger/get_env/now_sgt/fmt_time/fmt_usd
│
├── web/                      同步自 /www/wwwroot/mb.661688.xyz/
│   ├── index.html            主面板（Vue3 CDN）
│   └── history.html          历史趋势图（lightweight-charts v4.1.3）
│                               新增Tab（2026-06）：OI趋势(亿$) / 大户多空比
│
├── run_dom.py                各监控启动入口（Supervisor command用）
├── run_funding.py
├── run_liquidation.py
├── run_oi.py
├── etf_confirm_push.py       ETF确认推送
├── etf_timing.py             ETF时间调度
├── deploy_etf_fix.sh         ETF修复脚本
├── daily_briefing.py.bak.*   旧备份（可清理）
├── requirements.txt          57个依赖包
├── .gitignore
└── PROJECT_CONTEXT.md        本文件
```

---

## 四、数据库表（btc_history.db）

### 原有表
| 表名 | 内容 |
|------|------|
| snapshots | 5分钟快照（价格/OI/Funding/CVD/CB溢价）|
| daily_summary | 每日汇总（IB/VP/MP/ETF/清算统计）|

### 新增表（2026-06-24）
| 表名 | 关键字段 | 频率 |
|------|----------|------|
| binance_oi | ts, oi_btc, oi_usd, mark_px | 每5分钟 |
| binance_funding | ts, rate, next_settle, mark_px, index_px, premium_pct | 每5分钟 |
| binance_ls_global | ts, long_pct, short_pct, ls_ratio | 每5分钟 |
| binance_ls_top | ts, long_pct, short_pct, ls_ratio（大户Top20%）| 每5分钟 |
| binance_structure | ts, quadrant, oi_chg, px_chg, oi_usd, mark_px, funding, top_ls, note | 每5分钟 |

---

## 五、市场象限分析框架

| 象限 | OI | 价格 | 含义 | 操作 |
|------|----|------|------|------|
| Q1 | ↑ | ↑ | 多头新仓·趋势上涨 | 顺势做多（最强）|
| Q2 | ↑ | ↓ | 空头新仓·趋势下跌 | 顺势做空（最强）|
| Q3 | ↓ | ↑ | 空头爆仓·轧空反弹 | 谨慎追多（弱）|
| Q4 | ↓ | ↓ | 多头爆仓·去杠杆 | 谨慎追空（弱）|
| FLAT | 微变 | 微变 | 震荡积累 | 等待方向 |

判定阈值：OI变化 ±0.05%，价格变化 ±0.05%（5分钟粒度）

---

## 六、API 接口

```
GET  /api/data                     面板全量数据
GET  /api/health                   健康检查
WS   /ws/live                      WebSocket实时推送
GET  /api/history/metrics          历史快照（?metric=price&period=1d）
GET  /api/binance/summary          最新：OI/费率/多空比/象限
GET  /api/binance/oi/history       OI历史（?hours=24）
GET  /api/binance/funding/history  费率历史（?hours=48）
GET  /api/binance/ls/history       多空比历史（?hours=24）
GET  /api/binance/structure        象限历史（?hours=24）
```

---

## 七、简报时间表（北京时间/SGT）

| 时段 | 时间 | 会话 | max_tokens | 节数 |
|------|------|------|------------|------|
| 早盘 | 09:30 | morning | 8192 | 13节 |
| 早盘·周一 | 09:30 | morning_monday | 8192 | 13节+CME专项 |
| 欧盘 | 15:00 | europe | 2500 | 6节 |
| 美盘 | 20:30 | evening | 3000 | 6节 |
| 按需 | /b命令 | ondemand | 2000 | 5节 |

**简报7步骤流程（daily_briefing.py）：**
```
[1/7] Binance永续+现货数据
[2/7] 多交所Funding（5所）
[3/7] ETF资金流
[4/7] CME缺口
[5/7] IB(60min) + VP(昨日)
      ↓ 新增：binance_briefing_data.get_binance_context()
[6/7] Claude AI分析 → generate_briefing()
[7/7] TG发送 + WordPress发布
```

---

## 八、TG预警体系

| 服务 | 触发条件 | 冷却 |
|------|----------|------|
| btc-liq-monitor | 单笔爆仓>$10万 / 1小时累计>$100万 | - |
| btc-funding-monitor | 资金费率极端值 | - |
| btc-oi-monitor | OI突变 | - |
| btc-dom-monitor | 大单>$50万，持续>15秒，距价>0.5% | - |
| btc-structure-monitor | Q1/Q2确认（连续2次=10分钟）| 60分钟 |
| btc-structure-monitor | 大户多空比>3.0或<0.5 | 120分钟 |

---

## 九、德国IP限制

| 服务 | 状态 | 方案 |
|------|------|------|
| Binance WS (fstream) | 静默封锁 | 无解，改用OKX |
| Binance REST API | ✅ 正常 | 直连 |
| /fapi/v1/allForceOrders | ❌ 端点已下线 | 放弃 |
| Binance爆仓数据 | 无法获取 | OKX WS替代 |
| CF Worker → Binance REST | ❌ 403 | 放弃 |
| CF Worker → fstream WS | ❌ 502 | 放弃 |
| WordPress REST API | ❌ 封锁 | WP-CLI替代 |

---

## 十、.env 变量结构

```bash
ANTHROPIC_API_KEY=        # Claude API（当前用 claude-sonnet-4-5）
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
WP_PATH=/www/wwwroot/jianbao.661688.xyz
SOSOVALUE_API_KEY=
BINANCE_WORKER_URL=       # 暂停使用
BINANCE_PROXY_KEY=        # 暂停使用
LIQ_SINGLE_USD=100000
LIQ_HOURLY_USD=1000000
DOM_ALERT_USD=500000
DOM_STRONG_USD=1000000
DOM_MEGA_USD=5000000
DOM_PENDING_SEC=15
DOM_MIN_DIST_PCT=0.5
```

---

## 十一、关键依赖（requirements.txt 57包）

```
anthropic==0.109.1       Claude API
python-telegram-bot==22.8
websockets==16.0         OKX WS爆仓
aiohttp==3.14.1          Binance REST异步
python_binance==1.0.37   Binance封装
beautifulsoup4==4.15.0   Farside爬取
pandas==3.0.3
loguru==0.7.3
python-dotenv==1.2.2
SQLAlchemy==2.0.50
psycopg2-binary          PostgreSQL驱动（备用，当前用SQLite）
redis==8.0.0             Redis（备用）
```

---

## 十二、交易方法论

**三层框架：Context → Map → Trigger**

**关键术语：**
- IB：首60分钟 | POC：最大成交量价格 | VAH/VAL：价值区上下沿
- HVN：高量节点（支撑阻力）| LVN：低量真空区（速度区）
- PDH/PDL/PDC：前日高低收 | CME缺口：周末价差
- Kill Zone：亚洲08:00/伦敦16:00/纽约21:30 SGT
- BSL/SSL：多空止损密集区 | OD/ORR/OA/OTD：开盘类型

---

## 十三、历史Bug记录

| Bug | 原因 | 修复 |
|-----|------|------|
| OKX爆仓金额100×虚高 | sz字段是张数不是BTC | sz×0.001×price |
| Funding显示历史值 | 用了/fundingRate端点 | 改用/premiumIndex |
| MP数据源错误 | 误用现货API | 改用/fapi/v1/klines |
| ETF数据三倍重复 | 日/周/月三种累计 | 取同日期最小绝对值 |
| IB跨日不更新 | 模块缓存 | importlib.reload() |
| TG显示**标记 | parse_mode未设 | 加HTML模式 |
| 早盘简报截断 | max_tokens=3000 | 早盘改为8192 |
| allForceOrders 400 | 端点永久下线 | 放弃 |
| btc-api spawn error | binance_routes导入路径错 | 改为内联到main.py |

---

## 十四、变更日志

### 2026-06-24
- 新增 btc-binance-data 服务（OI/费率/多空比采集）
- 新增 btc-structure-monitor 服务（象限+多空比TG预警）
- 简报集成市场象限数据（第8节出现Q1/Q2/Q3/Q4分析）
- history.html 新增 OI趋势(亿$) 和 大户多空比 图表Tab
- 建立 GitHub 公开仓库 saiy829/btc-trader
- 建立本项目文档 PROJECT_CONTEXT.md

---

*更新规则：每次重大改动后同步更新本文件并 git push*
