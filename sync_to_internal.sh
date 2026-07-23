#!/usr/bin/env bash
# ============================================================
# sync_to_internal.sh — 一键同步外网代码到内网 git 仓库
#
# 原理：clone 外网仓 → 删掉开发文件 → git push --force 到内网仓
# 用 git 自己处理 diff，不用 rsync，不会"看不出变化"
#
# 用法（在内网电脑上运行）：
#   ./sync_to_internal.sh
#   ./sync_to_internal.sh /path/to/internal/repo
#   ./sync_to_internal.sh --config /path/to/internal/repo
# ============================================================

set -e

# ── 配置 ──
EXTERNAL_REPO="${EXTERNAL_REPO:-https://github.com/yb333/analyzer-agent.git}"
CONFIG_FILE="$HOME/.analyzer-agent-sync.conf"

# 读取配置文件（记住上次设置的内网仓库路径）
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
fi

# 命令行参数处理
if [ "$1" = "--config" ] && [ -n "$2" ]; then
    INTERNAL_REPO="$2"
    echo "INTERNAL_REPO=\"$INTERNAL_REPO\"" > "$CONFIG_FILE"
    echo "[OK] 已保存内网仓库路径: $INTERNAL_REPO"
    echo "以后直接运行 ./sync_to_internal.sh 即可，不用再指定路径。"
    exit 0
fi

# 覆盖默认路径
if [ -n "$1" ]; then
    INTERNAL_REPO="$1"
fi

# 检查内网仓库路径
if [ -z "$INTERNAL_REPO" ]; then
    echo "[ERROR] 未指定内网 git 仓库路径"
    echo ""
    echo "用法："
    echo "  首次使用先配置：./sync_to_internal.sh --config /path/to/internal/repo"
    echo "  后续直接运行：./sync_to_internal.sh"
    exit 1
fi

if [ ! -d "$INTERNAL_REPO/.git" ]; then
    echo "[ERROR] 不是 git 仓库: $INTERNAL_REPO"
    echo "请确认路径正确，且该目录已 git init 或 clone 自内网远端"
    exit 1
fi

echo "═══════════════════════════════════════════════"
echo "  同步外网代码 → 内网 git 仓库"
echo "═══════════════════════════════════════════════"
echo "外网仓库: $EXTERNAL_REPO"
echo "内网仓库: $INTERNAL_REPO"
echo ""

# ── Step 1: 从外网 clone 最新代码 ──
TEMP_DIR=$(mktemp -d)
echo "[Step 1] 拉取外网最新代码..."
git clone --depth 1 "$EXTERNAL_REPO" "$TEMP_DIR" 2>&1 | tail -3
COMMIT_SUBJECT=$(cd "$TEMP_DIR" && git log -1 --format="%s")
COMMIT_HASH=$(cd "$TEMP_DIR" && git rev-parse --short HEAD)
echo "  最新提交: $COMMIT_HASH $COMMIT_SUBJECT"
echo ""

# ── Step 2: 取消浅克隆（shallow clone 不能 push）──
echo "[Step 2] 取消浅克隆..."
cd "$TEMP_DIR"
git fetch --unshallow 2>&1 || echo "  [INFO] unshallow 失败（可能已是完整克隆），继续..."
echo ""

# ── Step 3: 删掉不该给用户的文件 ──
echo "[Step 3] 清理开发文件..."
cd "$TEMP_DIR"
rm -rf tests docs release telemetry-server
rm -f architecture.md sync_to_internal.sh sync_to_internal.bat
rm -f start_telemetry.sh start_telemetry.bat stop_telemetry.bat
rm -f sample_rule.yml .gitignore
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true

# 提交这些删除
git add -A
git commit -m "清理开发文件（同步前预处理）" --allow-empty 2>&1 | tail -1
echo "  已清理"
echo ""

# ── Step 4: 直接 git push 到内网仓 ──
echo "[Step 4] 推送到内网仓库..."

# 添加内网仓为 remote
git remote add internal "$INTERNAL_REPO" 2>/dev/null || git remote set-url internal "$INTERNAL_REPO"

# 检测分支（外网是 main，内网可能是 master）
BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "  外网分支: $BRANCH"

# 检测内网仓的分支名
INTERNAL_BRANCH=$(cd "$INTERNAL_REPO" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "master")
echo "  内网分支: $INTERNAL_BRANCH"
echo ""

# 先 pull 内网仓的内容（合并历史，以内网仓分支名为准）
git pull internal "$INTERNAL_BRANCH" --allow-unrelated-histories --no-edit 2>&1 | tail -3 || true

# push 到内网仓（外网 main → 内网 master，--force 以外网代码为准）
git push --force internal "$BRANCH:$INTERNAL_BRANCH" 2>&1 | tail -3

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ 同步完成"
echo "  $COMMIT_HASH $COMMIT_SUBJECT"
echo "═══════════════════════════════════════════════"

# 清理临时目录
rm -rf "$TEMP_DIR"
