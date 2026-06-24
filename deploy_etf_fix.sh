#!/usr/bin/env bash
# ============================================================
# deploy_etf_fix.sh
# BTC 简报系统 — ETF 时间显示修复一键部署脚本
# 在 VPS 上以 root 或项目用户执行：
#   bash /opt/btc-trader/deploy_etf_fix.sh
# ============================================================
set -euo pipefail

PROJ="/opt/btc-trader"
VENV="$PROJ/venv"
PYTHON="$VENV/bin/python"
LOG_DIR="$PROJ/logs"
DATA_DIR="$PROJ/data"
UTILS_DIR="$PROJ/utils"
SERVICES_DIR="$PROJ/services"

echo "═══════════════════════════════════════════════════════"
echo "  BTC 简报 ETF 时间修复 — 部署脚本"
echo "═══════════════════════════════════════════════════════"
echo ""

# ─── 0. 前置检查 ─────────────────────────────────────────────────────────────
if [ ! -d "$PROJ" ]; then
  echo "❌ 项目目录 $PROJ 不存在，请检查路径后重试"
  exit 1
fi
if [ ! -f "$PYTHON" ]; then
  echo "❌ Python venv 未找到：$PYTHON"
  exit 1
fi

mkdir -p "$UTILS_DIR" "$SERVICES_DIR" "$LOG_DIR" "$DATA_DIR"

# ─── 1. 写入 utils/etf_timing.py ─────────────────────────────────────────────
echo "[1/4] 安装 etf_timing.py → $UTILS_DIR/etf_timing.py"
[ -f "$UTILS_DIR/etf_timing.py" ] && cp "$UTILS_DIR/etf_timing.py" "$UTILS_DIR/etf_timing.py.bak.$(date +%Y%m%d)"

# 确保 utils/ 是 Python 包
touch "$UTILS_DIR/__init__.py"

# 文件内容由下载的 etf_timing.py 提供，直接复制
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/etf_timing.py" ]; then
  cp "$SCRIPT_DIR/etf_timing.py" "$UTILS_DIR/etf_timing.py"
  echo "   ✅ 从同级目录复制"
else
  echo "   ⚠️  请手动将 etf_timing.py 上传至 $UTILS_DIR/"
fi

# ─── 2. 写入 services/etf_confirm_push.py ────────────────────────────────────
echo "[2/4] 安装 etf_confirm_push.py → $SERVICES_DIR/"
[ -f "$SERVICES_DIR/etf_confirm_push.py" ] && cp "$SERVICES_DIR/etf_confirm_push.py" "$SERVICES_DIR/etf_confirm_push.py.bak.$(date +%Y%m%d)"

if [ -f "$SCRIPT_DIR/etf_confirm_push.py" ]; then
  cp "$SCRIPT_DIR/etf_confirm_push.py" "$SERVICES_DIR/etf_confirm_push.py"
  echo "   ✅ 从同级目录复制"
else
  echo "   ⚠️  请手动将 etf_confirm_push.py 上传至 $SERVICES_DIR/"
fi

# ─── 3. 安装 cron 定时任务（北京时间每天 12:00 = UTC 04:00，周二~六）───────
echo "[3/4] 配置 cron 定时任务"

CRON_CMD="0 4 * * 2-6 $PYTHON $SERVICES_DIR/etf_confirm_push.py >> $LOG_DIR/etf_confirm_push.log 2>&1"
CRON_COMMENT="# BTC ETF confirm push (12:00 UTC+8, Tue-Sat)"

# 检查是否已存在
if crontab -l 2>/dev/null | grep -q "etf_confirm_push"; then
  echo "   ⚠️  cron 任务已存在，跳过（如需更新请手动 crontab -e）"
else
  (crontab -l 2>/dev/null; echo "$CRON_COMMENT"; echo "$CRON_CMD") | crontab -
  echo "   ✅ cron 任务已添加：$CRON_CMD"
fi

# ─── 4. 在现有简报代码中找 ETF 段落，给出集成指引 ───────────────────────────
echo ""
echo "[4/4] 扫描现有简报代码中的 ETF 相关函数..."
echo ""

ETF_FILES=$(grep -rl "etf_flow\|ETF\|Farside\|SoSoValue\|etf_section\|build_etf" \
  "$PROJ" --include="*.py" 2>/dev/null \
  | grep -v ".bak" | grep -v "__pycache__" | sort)

if [ -z "$ETF_FILES" ]; then
  echo "   未找到 ETF 相关 Python 文件，请手动搜索并集成"
else
  echo "   找到以下文件含 ETF 相关代码："
  for f in $ETF_FILES; do
    echo "   → $f"
    # 打印含 ETF 关键词的行（去掉注释）
    grep -n "etf_flow\|ETF.*净流量\|ETF.*flow\|etf_section\|SoSoValue\|Farside" "$f" \
      | grep -v "^\s*#" | head -8 | sed 's/^/      /'
    echo ""
  done
fi

# ─── 集成说明打印 ─────────────────────────────────────────────────────────────
cat << 'INTEGRATION_GUIDE'
═══════════════════════════════════════════════════════
 集成指引 — 在现有简报代码中替换 ETF 段落
═══════════════════════════════════════════════════════

在上面找到的简报生成文件中，找到构建 ETF 段落的函数，
然后按以下方式修改（最少改动原则）：

【修改前（示例）】
    etf_text = f"ETF单日净流量：{etf_flow:.2f}亿美元"

【修改后】
    from utils.etf_timing import format_etf_block, now_beijing
    etf_text = format_etf_block(
        etf_data=etf_dict,       # {代码: 流量(百万美元)} 字典
        now_bj=now_beijing(),    # 当前北京时间（自动）
    )

─────────────────────────────────────────────────────
 etf_dict 格式示例
─────────────────────────────────────────────────────
    etf_dict = {
        'IBIT': -172.0,    # BlackRock，单位百万美元
        'FBTC':  57.4,     # Fidelity
        'GBTC': -81.0,     # Grayscale
        'ARKB':  64.0,     # ARK
        'MSBT':   8.1,     # Morgan Stanley
        # 未到账的品种传 None 或 0.0
        'BITB': None,
        'BTCO':  3.7,
    }

─────────────────────────────────────────────────────
 同时需要在早盘简报末尾保存 ETF 数据供二次推送比对
─────────────────────────────────────────────────────
    import json
    from pathlib import Path
    cache = Path('/opt/btc-trader/data/morning_brief_cache.json')
    state = json.loads(cache.read_text()) if cache.exists() else {}
    from utils.etf_timing import get_etf_info, now_beijing
    info = get_etf_info(etf_dict, now_beijing())
    state[info.us_data_date_str] = {'etf_flow_m': info.total_flow_m}
    cache.write_text(json.dumps(state))

═══════════════════════════════════════════════════════

INTEGRATION_GUIDE

# ─── 5. 快速验证 ─────────────────────────────────────────────────────────────
echo ""
echo "─── 快速验证 etf_timing.py 是否可正常导入 ───"
$PYTHON - << 'PYEOF'
import sys
sys.path.insert(0, '/opt/btc-trader')
from utils.etf_timing import format_etf_block, now_beijing
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

# 模拟今天早盘 9:30 场景（用真实当前时间）
now_bj = now_beijing()
sample_data = {
    'IBIT': None,   # 模拟 IBIT 未到账
    'FBTC': 57.4, 'ARKB': 64.0, 'GBTC': -81.0, 'MSBT': 8.1,
    'BITB': 0.0, 'BTCO': 3.7, 'EZBC': 0.0, 'BRRR': 0.0,
    'HODL': 3.4, 'BTCW': 0.0, 'BTC': 48.1,
}
block = format_etf_block(sample_data, now_bj)
print(block)
print()
print("✅ 导入验证通过")
PYEOF

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  部署完成！"
echo "  cron 将在每天北京时间 12:00（周二~六）自动检查"
echo "  日志：$LOG_DIR/etf_confirm_push.log"
echo "═══════════════════════════════════════════════════════"
