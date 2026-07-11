#!/bin/bash
# 生成 PROJECT_INDEX.md —— 每次开发前跑一次
# 项目：BTC AI 永续合约辅助交易系统（/opt/btc-trader/）
# 探查结论（2026-07-11）：
#   - 代码在仓库根目录 + 功能子目录（api/ monitor/ ai_analyst/ 等），无 src/
#   - 框架为 FastAPI，路由风格 @app.xxx（api/main.py）与 @router.xxx（api/binance_routes.py 遗留）
#   - 环境变量文件只有 .env（不进 Git，无 .env.example）
#   - AtasBridge/ 为 C# 项目，不参与 Python 路由扫描
set -u
cd "$(dirname "$0")/.." || exit 1

{
  echo "# PROJECT_INDEX（自动生成于 $(TZ='Asia/Shanghai' date '+%F %T') 北京时间，勿手改）"
  echo
  echo "## 文件结构"
  echo '```'
  if command -v tree >/dev/null 2>&1; then
    tree -I 'node_modules|__pycache__|.git|venv|.venv|dist|build|logs|data|_deployed' -L 3
  else
    find . -maxdepth 3 -type f \
      -not -path '*/node_modules/*' -not -path '*/.git/*' \
      -not -path '*/__pycache__/*' -not -path '*/venv/*' \
      -not -path '*/.venv/*' -not -path '*/logs/*' \
      -not -path '*/data/*' -not -path '*/_deployed/*' \
      -not -name '*.pyc' -not -name '*.log' -not -name '*.bak*' | sort
  fi
  echo '```'
  echo
  echo "## API 路由（FastAPI）"
  echo '```'
  # 本项目为 FastAPI：@app.get/post/put/delete/patch/websocket + 遗留 @router.xxx
  grep -rn "@app\.\(get\|post\|put\|delete\|patch\|websocket\)\|@router\.\(get\|post\|put\|delete\|patch\)" \
    --include="*.py" . \
    --exclude-dir=venv --exclude-dir=.venv --exclude-dir=__pycache__ \
    --exclude-dir=_deployed 2>/dev/null | head -80
  echo '```'
  echo
  echo "## 环境变量（只列变量名，值绝不进入索引）"
  echo '```'
  # 本项目只有 .env（不进 Git）；列出全部变量名，cut 去掉等号后的值
  if [ -f .env ]; then
    grep -h "^[A-Za-z_][A-Za-z0-9_]*=" .env 2>/dev/null | cut -d= -f1 | sort -u
  else
    echo "(未找到 .env —— 本机可能不是 VPS 生产环境)"
  fi
  echo '```'
  echo
  echo "## 入口文件与启动方式"
  echo '```'
  echo "系统入口   : scheduler.py（Telegram Bot + 三时段定时简报，Supervisor: btc-briefing）"
  echo "API 服务   : api/main.py（FastAPI，端口 8001，Supervisor: btc-api）"
  echo "监控服务   : monitor/ 下各服务由 Supervisor 托管（btc-binance-data / btc-structure-monitor /"
  echo "             btc-liq-monitor / btc-dom-monitor / btc-funding-monitor / btc-oi-monitor /"
  echo "             signal_engine 等，以 supervisorctl status 实际输出为准）"
  echo "手动测试   : 必须用 venv/bin/python3，不要用系统 python3"
  echo '```'
  echo
  echo "## Supervisor 服务实时状态"
  echo '```'
  supervisorctl status 2>/dev/null || echo "(supervisorctl 不可用 —— 本机可能不是 VPS 生产环境)"
  echo '```'
} > PROJECT_INDEX.md

echo "已生成 PROJECT_INDEX.md（$(wc -l < PROJECT_INDEX.md) 行）"
