#!/usr/bin/env python3
"""Skill script dispatcher.

Usage:
    python run.py <script_name> [script_args...]

Examples:
    python run.py analyzer --input execution_tasks.xlsx --output output/
    python run.py view_generator --input knowledge_draft.json --output output/
"""

import sys
import subprocess
from pathlib import Path

# 命令别名映射（用户/AI 可能用简称）
ALIAS_MAP = {
    "analyze": "analyzer",     # analyze → analyzer.py
    "view": "view_generator",  # view → view_generator.py
}


def check_dependencies():
    """启动时检查依赖，缺失则友好提示安装（不抛 ImportError 堆栈）。

    无论用户用 install 脚本（建 venv）还是手工复制，最终都靠运行 run.py 的
    python 解释器来 import。这里统一兜底，避免依赖缺失时给出不友好的报错。
    """
    missing = []
    for pkg in ("openpyxl", "sqlglot"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[ERROR] 缺少依赖: {', '.join(missing)}", file=sys.stderr)
        print(f"  请安装: pip install {' '.join(missing)}", file=sys.stderr)
        print(f"  （当前 Python: {sys.executable}）", file=sys.stderr)
        sys.exit(1)


def main():
    check_dependencies()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        references_dir = Path(__file__).resolve().parent / "references"
        print("Usage: python run.py <script_name> [script_args...]")
        print(f"\nAvailable scripts in references/:")
        for f in sorted(references_dir.glob("*.py")):
            print(f"  - {f.stem}")
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    script_name = sys.argv[1]
    # 别名映射
    script_name = ALIAS_MAP.get(script_name, script_name)
    if not script_name.endswith(".py"):
        script_name += ".py"

    script_path = Path(__file__).resolve().parent / "references" / script_name
    if not script_path.exists():
        print(f"Error: Script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run([sys.executable, str(script_path)] + sys.argv[2:])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
