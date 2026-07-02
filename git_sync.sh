#!/bin/bash
# ============================================================
# git_sync.sh — VPS → GitHub 自动同步脚本
# 每天凌晨 03:00（服务器本地时区 Asia/Shanghai）运行
# cron: 0 3 * * *
# 路径：/opt/btc-trader/git_sync.sh
# ============================================================

REPO="/opt/btc-trader"
LOG="/opt/btc-trader/logs/git_sync.log"
TIMESTAMP=$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST')

# 确保日志目录存在
mkdir -p "$(dirname "$LOG")"

echo "=============================" >> "$LOG"
echo "[$TIMESTAMP] 开始同步" >> "$LOG"

cd "$REPO" || {
    echo "[$TIMESTAMP] 错误：目录不存在 $REPO" >> "$LOG"
    exit 1
}

# ── 检查是否有变更 ─────────────────────────────────────────
if git diff --quiet && git diff --cached --quiet && \
   [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "[$TIMESTAMP] 无变更，跳过提交" >> "$LOG"
    exit 0
fi

# ── 显示变更文件 ──────────────────────────────────────────
echo "[$TIMESTAMP] 变更文件：" >> "$LOG"
git status --short >> "$LOG"

# ── 暂存所有变更（.gitignore 负责排除敏感/临时文件）────────
git add -A 2>> "$LOG"

# ── 提交 ─────────────────────────────────────────────────
COMMIT_MSG="auto: 每日同步 $TIMESTAMP"
git commit -m "$COMMIT_MSG" >> "$LOG" 2>&1

# ── 推送 ─────────────────────────────────────────────────
git push origin main >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
    echo "[$TIMESTAMP] ✅ 推送成功" >> "$LOG"
else
    echo "[$TIMESTAMP] ❌ 推送失败，请检查 SSH 密钥和网络" >> "$LOG"
    exit 1
fi

echo "=============================" >> "$LOG"
