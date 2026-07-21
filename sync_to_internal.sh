#!/usr/bin/env bash
# ============================================================
# sync_to_internal.sh — 一键同步外网代码到内网 git 仓库
#
# 用法（在内网电脑上运行）：
#   ./sync_to_internal.sh                  # 用默认配置
#   ./sync_to_internal.sh /path/to/internal/repo  # 指定内网仓库路径
#   ./sync_to_internal.sh --config /path/to/internal/repo  # 设置默认路径
#
# 功能：
#   1. 从外网 GitHub clone/pull 最新代码到临时目录
#   2. 把成品文件同步到内网 git 仓库（排除开发文件）
#   3. git add + commit + push 到内网远端
#
# 只同步成品文件（用户需要的），不同步开发文件（tests/docs/architecture 等）
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

# ── Step 1: 从外网拉最新代码 ──
TEMP_DIR=$(mktemp -d)
echo "[Step 1] 拉取外网最新代码..."
git clone --depth 1 "$EXTERNAL_REPO" "$TEMP_DIR" 2>&1 | tail -3
EXTERNAL_VERSION=$(cd "$TEMP_DIR" && git log --oneline -1)
echo "  最新提交: $EXTERNAL_VERSION"
echo ""

# ── Step 2: 同步成品文件到内网仓库 ──
echo "[Step 2] 同步成品文件到内网仓库..."

# 要同步的成品（目录或文件）
SYNC_ITEMS=(
    "dws-pipeline-analyzer"
    "commands"
    "README.md"
    "install.sh"
    "install.bat"
    "install.py"
    "sample_rule.yml"
)

# 先清理内网仓库里旧的同名成品（保持干净），再复制新的
for item in "${SYNC_ITEMS[@]}"; do
    if [ ! -e "$TEMP_DIR/$item" ]; then
        continue
    fi
    # 删除旧的（避免残留已删除的文件）
    rm -rf "$INTERNAL_REPO/$item"
    # 复制新的
    cp -r "$TEMP_DIR/$item" "$INTERNAL_REPO/$item"
    echo "  + $item"
done

# 清理内网仓库里不该有的开发文件（如果有）
DEV_FILES=("tests" "docs" "architecture.md" "pack_release.py" "sync_to_internal.sh" "sync_to_internal.bat" "sample_rule.yml" "release")
for df in "${DEV_FILES[@]}"; do
    if [ -e "$INTERNAL_REPO/$df" ]; then
        rm -rf "$INTERNAL_REPO/$df"
        echo "  - $df（移除开发文件）"
    fi
done

# 清理 __pycache__/.pyc/.DS_Store
find "$INTERNAL_REPO" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$INTERNAL_REPO" -name "*.pyc" -delete 2>/dev/null || true
find "$INTERNAL_REPO" -name ".DS_Store" -delete 2>/dev/null || true

echo ""

# ── Step 3: git commit + push ──
echo "[Step 3] 提交到内网 git..."
cd "$INTERNAL_REPO"
git add -A

# 检查有没有变更
if git diff --cached --quiet; then
    echo "  无变更，内容已是最新。"
else
    # 用外网最新 commit 的原始 message 做提交信息（不加"同步"前缀）
    # 从临时目录取完整 commit message
    cd "$TEMP_DIR"
    COMMIT_SUBJECT=$(git log -1 --format="%s")
    COMMIT_HASH=$(git rev-parse --short HEAD)
    COMMIT_BODY=$(git log -1 --format="%b")
    cd "$INTERNAL_REPO"

    echo "  提交信息: $COMMIT_SUBJECT"
    echo ""

    # 用外网原始 commit message（带 hash 标注来源）
    if [ -z "$COMMIT_BODY" ]; then
        git commit -m "$COMMIT_SUBJECT ($COMMIT_HASH)" 2>&1 | tail -3
    else
        git commit -m "$COMMIT_SUBJECT ($COMMIT_HASH)" -m "$COMMIT_BODY" 2>&1 | tail -3
    fi
    echo ""
    echo "[Step 4] 推送到内网远端..."
    git push 2>&1 | tail -3
    echo ""
    echo "═══════════════════════════════════════════════"
    echo "  ✅ 同步完成"
    echo "  $COMMIT_HASH $COMMIT_SUBJECT"
    echo "═══════════════════════════════════════════════"
fi

# 清理临时目录
rm -rf "$TEMP_DIR"
