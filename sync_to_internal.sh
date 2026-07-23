#!/usr/bin/env bash
# ============================================================
# sync_to_internal.sh — 一键同步外网代码到内网 git 仓库
#
# 用法（在内网电脑上运行）：
#   ./sync_to_internal.sh
#   ./sync_to_internal.sh /path/to/internal/repo
#   ./sync_to_internal.sh --config /path/to/internal/repo
# ============================================================

set -e

EXTERNAL_REPO="${EXTERNAL_REPO:-https://github.com/yb333/analyzer-agent.git}"
CONFIG_FILE="$HOME/.analyzer-agent-sync.conf"

if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
fi

if [ "$1" = "--config" ] && [ -n "$2" ]; then
    INTERNAL_REPO="$2"
    echo "INTERNAL_REPO=\"$INTERNAL_REPO\"" > "$CONFIG_FILE"
    echo "[OK] 已保存内网仓库路径: $INTERNAL_REPO"
    exit 0
fi

if [ -n "$1" ]; then
    INTERNAL_REPO="$1"
fi

if [ -z "$INTERNAL_REPO" ]; then
    echo "[ERROR] 未指定内网 git 仓库路径"
    exit 1
fi

if [ ! -d "$INTERNAL_REPO/.git" ]; then
    echo "[ERROR] 不是 git 仓库: $INTERNAL_REPO"
    exit 1
fi

echo "============================================================"
echo "  同步外网代码 - 内网 git 仓库"
echo "============================================================"
echo "外网仓库: $EXTERNAL_REPO"
echo "内网仓库: $INTERNAL_REPO"
echo ""

# ── Step 1: clone 外网最新代码 ──
TEMP_DIR=$(mktemp -d)
echo "[Step 1] 拉取外网最新代码..."
git clone --depth 1 "$EXTERNAL_REPO" "$TEMP_DIR" 2>&1 | tail -3
COMMIT_SUBJECT=$(cd "$TEMP_DIR" && git log -1 --format="%s")
COMMIT_HASH=$(cd "$TEMP_DIR" && git rev-parse --short HEAD)
echo "  最新提交: $COMMIT_HASH $COMMIT_SUBJECT"
echo ""

# ── Step 2: rsync 同步到内网仓（排除开发文件，保留 .git）──
echo "[Step 2] 同步到内网仓库..."
rsync -a --delete \
    --exclude='.git' \
    --exclude='tests' --exclude='docs' --exclude='release' --exclude='dev' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='telemetry-server' \
    --exclude='architecture.md' \
    --exclude='sync_to_internal.*' \
    --exclude='start_telemetry.*' --exclude='stop_telemetry.*' \
    --exclude='sample_rule.yml' \
    --exclude='.gitignore' --exclude='.DS_Store' \
    "$TEMP_DIR/" "$INTERNAL_REPO/"
echo "  + 同步完成"

# 清理垃圾
find "$INTERNAL_REPO" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$INTERNAL_REPO" -name "*.pyc" -delete 2>/dev/null || true
find "$INTERNAL_REPO" -name ".DS_Store" -delete 2>/dev/null || true
echo ""

# ── Step 3: 在内网仓 git commit + push ──
echo "[Step 3] 提交到内网 git..."
cd "$INTERNAL_REPO"
git add -A

if git diff --cached --quiet; then
    echo "  无变更，内容已是最新。"
else
    echo "  提交信息: $COMMIT_SUBJECT"
    git commit -m "$COMMIT_SUBJECT ($COMMIT_HASH)" 2>&1 | tail -3
    echo ""
    echo "[Step 4] 推送到内网远端..."
    git push 2>&1 | tail -3
    echo ""
    echo "============================================================"
    echo "  ✅ 同步完成: $COMMIT_HASH $COMMIT_SUBJECT"
    echo "============================================================"
fi

# 清理临时目录
rm -rf "$TEMP_DIR"
