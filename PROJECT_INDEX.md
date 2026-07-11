# PROJECT_INDEX（自动生成于 2026-07-11 22:26:56 北京时间，勿手改）

## 文件结构
```
./ai_analyst/briefing.py
./ai_analyst/__init__.py
./ai_analyst/liq_briefing.py
./alert_bot/__init__.py
./alert_bot/send.py
./api/binance_routes.py
./api/__init__.py
./api/main.py
./api/main_py_append.py
./AtasBridge/AtasBridge.cs
./AtasBridge/AtasBridge.csproj
./AtasBridge/AtasBridge.Platform.csproj
./AtasBridge/CHANGELOG_AtasBridge.md
./briefing/atas_briefing_data.py
./briefing/binance_briefing_data.py
./btc_history.db
./CLAUDE.md
./daily_briefing.py
./data_collector/binance_data.py
./data_collector/cme_data.py
./data_collector/coinglass_data.py
./data_collector/etf_data.py
./data_collector/__init__.py
./data_collector/multi_funding.py
./.env
./.gitignore
./git_sync.sh
./monitor/binance_data_service.py
./monitor/dom_monitor.py
./monitor/funding_monitor.py
./monitor/gate_liq_monitor.py
./monitor/__init__.py
./monitor/liquidation_monitor.py
./monitor/oi_monitor.py
./monitor/signal_engine.py
./monitor/structure_monitor.py
./PROJECT_CONTEXT.md
./PROJECT_CONTEXT-v1.md
./PROJECT_INDEX.md
./publisher/wordpress.py
./requirements.txt
./run_dom.py
./run_funding.py
./run_liquidation.py
./run_oi.py
./scheduler.py
./scripts/gen-index.sh
./services/etf_confirm_push.py
./signal_tracker.py
./startup_guard.py
./utils/etf_timing.py
./utils/helpers.py
./utils/__init__.py
./utils/position_calc.py
./utils/signal_score.py
./web/history.html
./web/index.html
```

## API 路由（FastAPI）
```
./api/main.py:781:@app.get("/api/snapshot")
./api/main.py:784:@app.get("/api/health")
./api/main.py:800:@app.get("/api/history/metrics")
./api/main.py:805:@app.get("/api/history/daily-archive")
./api/main.py:810:@app.post("/api/refresh-ib")
./api/main.py:818:@app.websocket("/ws/live")
./api/main.py:861:@app.get("/api/binance/summary")
./api/main.py:885:@app.get("/api/binance/oi/history")
./api/main.py:896:@app.get("/api/binance/funding/history")
./api/main.py:907:@app.get("/api/binance/ls/history")
./api/main.py:922:@app.get("/api/binance/structure")
./api/main.py:1122:@app.post("/atas/signal")
./api/main.py:1209:@app.post("/atas/trade")
./api/main.py:1374:@app.get("/atas/status")
./api/main.py:1466:@app.post("/atas/bar")
./api/main.py:1534:@app.post("/atas/absorption")
./api/main.py:1637:@app.get("/api/signal/latest")
./api/binance_routes.py:37:@router.get("/summary")
./api/binance_routes.py:95:@router.get("/oi/history")
./api/binance_routes.py:110:@router.get("/funding/history")
./api/binance_routes.py:124:@router.get("/liquidations")
./api/binance_routes.py:164:@router.get("/market-structure")
./api/binance_routes.py:179:@router.get("/ls-ratio/history")
./api/main_py_append.py:25:@app.get("/api/binance/summary")
./api/main_py_append.py:49:@app.get("/api/binance/oi/history")
./api/main_py_append.py:60:@app.get("/api/binance/funding/history")
./api/main_py_append.py:71:@app.get("/api/binance/ls/history")
./api/main_py_append.py:86:@app.get("/api/binance/structure")
```

## 环境变量（只列变量名，值绝不进入索引）
```
ANTHROPIC_API_KEY
BINANCE_API_KEY
BINANCE_API_SECRET
BINANCE_PROXY_KEY
BINANCE_WORKER_URL
BTC_TRADER_KEY
COINGLASS_API_KEY
DB_HOST
DB_NAME
DB_PASSWORD
DB_PORT
DB_USER
DOM_ALERT_USD
DOM_MEGA_USD
DOM_MIN_DIST_PCT
DOM_PENDING_SEC
DOM_STRONG_USD
LIQ_HOURLY_USD
LIQ_SINGLE_USD
LOG_LEVEL
POS_ACCOUNT_USDT
POS_RISK_PCT
REDIS_HOST
REDIS_PASSWORD
REDIS_PORT
SOSOVALUE_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TIMEZONE
WP_APP_PASSWORD
WP_URL
WP_USERNAME
```

## 入口文件与启动方式
```
系统入口   : scheduler.py（Telegram Bot + 三时段定时简报，Supervisor: btc-briefing）
API 服务   : api/main.py（FastAPI，端口 8001，Supervisor: btc-api）
监控服务   : monitor/ 下各服务由 Supervisor 托管（btc-binance-data / btc-structure-monitor /
             btc-liq-monitor / btc-dom-monitor / btc-funding-monitor / btc-oi-monitor /
             signal_engine 等，以 supervisorctl status 实际输出为准）
手动测试   : 必须用 venv/bin/python3，不要用系统 python3
```

## Supervisor 服务实时状态
```
btc-api                          RUNNING   pid 399969, uptime 5 days, 3:51:15
btc-binance-data                 RUNNING   pid 3060699, uptime 10 days, 10:22:12
btc-briefing                     RUNNING   pid 293936, uptime 5 days, 12:18:05
btc-dom-monitor                  RUNNING   pid 3060697, uptime 10 days, 10:22:12
btc-funding-monitor              RUNNING   pid 3060695, uptime 10 days, 10:22:12
btc-gate-liq                     RUNNING   pid 3105755, uptime 10 days, 5:54:27
btc-liq-monitor                  RUNNING   pid 3115440, uptime 10 days, 4:57:05
btc-oi-monitor                   RUNNING   pid 3060696, uptime 10 days, 10:22:12
btc-signal-engine                RUNNING   pid 380511, uptime 5 days, 5:20:14
btc-signal-tracker               RUNNING   pid 3146305, uptime 10 days, 1:53:54
btc-structure-monitor            RUNNING   pid 262033, uptime 5 days, 14:58:30
sales_dashboard                  RUNNING   pid 4045391, uptime 6:11:27
trade-review-api                 RUNNING   pid 3896464, uptime 7 days, 6:58:24
```
