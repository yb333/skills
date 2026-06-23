#!/usr/bin/env python3
"""DWS Skills 安装器 — 跨平台核心逻辑。

被 install.bat / install.sh 调用，也可直接执行：
    python install.py
    python install.py dws-pipeline-analyzer    # 只装指定 skill
    python install.py --local                   # 装到当前项目 .opencode/
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path


def find_python() -> str:
    """找到 Python 3.10+ 解释器"""
    candidates = []
    if os.name == "nt":
        candidates = ["py -3", "python", "python3"]
    else:
        candidates = ["python3", "python"]

    for cmd in candidates:
        parts = cmd.split()
        try:
            r = subprocess.run(
                parts + ["-c", "import sys; exit(0 if sys.version_info >= (3,10) else 1)"],
                capture_output=True,
            )
            if r.returncode == 0:
                return cmd
        except FileNotFoundError:
            continue
    return ""


def scan_skills(script_dir: Path) -> list[str]:
    """扫描所有含 SKILL.md 的目录"""
    skills = []
    for d in sorted(script_dir.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            skills.append(d.name)
    return skills


def collect_requirements(script_dir: Path, skills: list[str]) -> list[str]:
    """汇总所有 skill 的 requirements.txt，去重"""
    reqs = set()
    for s in skills:
        req_file = script_dir / s / "requirements.txt"
        if req_file.exists():
            for line in req_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    reqs.add(line)
    return sorted(reqs)


def copy_skill(src: Path, dst: Path):
    """复制 skill 目录，排除 __pycache__/.venv/.git/.DS_Store"""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", ".venv", ".git", ".DS_Store", "*.pyc"
    ))


def main():
    script_dir = Path(__file__).resolve().parent

    # 解析参数
    skill_filter = ""
    mode = "global"
    for arg in sys.argv[1:]:
        if arg in ("-l", "--local"):
            mode = "local"
        elif arg in ("-h", "--help"):
            print(__doc__)
            return
        else:
            skill_filter = arg

    print("=" * 50)
    print("  DWS Skills 安装器")
    print("=" * 50)
    print()

    # ── 1. 扫描 skill ──
    print("[1/4] 扫描可用 skill...")
    all_skills = scan_skills(script_dir)
    if not all_skills:
        print("  未找到任何 skill（需要 SKILL.md 文件）")
        input("按回车退出...")
        sys.exit(1)

    if skill_filter:
        skills = [s for s in all_skills if skill_filter in s]
        if not skills:
            print(f"  未找到匹配 '{skill_filter}' 的 skill")
            print(f"  可用: {', '.join(all_skills)}")
            input("按回车退出...")
            sys.exit(1)
    else:
        skills = all_skills

    print(f"  将安装 {len(skills)} 个 skill: {', '.join(skills)}")
    dest_label = "项目级 (.opencode/)" if mode == "local" else "全局 (~/.config/opencode/)"
    print(f"  模式: {dest_label}")
    print()

    # ── 2. 检测 Python ──
    print("[2/4] 检测 Python...")
    python_cmd = find_python()
    if not python_cmd:
        print("  未找到 Python 3.10+")
        print()
        if os.name == "nt":
            print("  请安装: winget install Python.Python.3.12")
            print("  或: https://www.python.org/downloads/")
            print('  安装时勾选 "Add Python to PATH"')
        else:
            print("  macOS:  brew install python3")
            print("  Ubuntu: sudo apt install python3 python3-venv")
        input("按回车退出...")
        sys.exit(1)

    py_parts = python_cmd.split()
    ver = subprocess.run(py_parts + ["--version"], capture_output=True, text=True)
    print(f"  {python_cmd} ({ver.stdout.strip()})")
    print()

    # ── 3. venv + 依赖 ──
    # 注意：SKILL 运行时（run.py）用调用它的 python 解释器，不强制使用此 venv。
    # venv 供偏好隔离环境的用户使用；手工复制的用户只要系统 python 装好依赖即可
    # （run.py 启动时会自检依赖并友好提示安装）。
    print("[3/4] 创建虚拟环境 + 安装依赖...")
    if mode == "global":
        config_dir = Path.home() / ".config" / "opencode"
    else:
        config_dir = Path.cwd() / ".opencode"

    venv_dir = config_dir / "venv"
    if not venv_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(py_parts + ["-m", "venv", str(venv_dir)], check=True)

    if os.name == "nt":
        venv_py = venv_dir / "Scripts" / "python.exe"
    else:
        venv_py = venv_dir / "bin" / "python"

    reqs = collect_requirements(script_dir, skills)
    if reqs:
        subprocess.run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
                       capture_output=True)
        print(f"  安装依赖: {' '.join(reqs)}")
        r = subprocess.run([str(venv_py), "-m", "pip", "install"] + reqs + ["--quiet"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  依赖安装失败: {r.stderr}")
            input("按回车退出...")
            sys.exit(1)
        print(f"  依赖安装完成")
    print()

    # ── 4. 复制 skill + 命令 ──
    print("[4/4] 安装 skill 文件...")
    skills_dir = config_dir / "skills"
    commands_dir = config_dir / "commands"
    skills_dir.mkdir(parents=True, exist_ok=True)
    commands_dir.mkdir(parents=True, exist_ok=True)

    for s in skills:
        src = script_dir / s
        dst = skills_dir / s
        copy_skill(src, dst)
        print(f"  skill: {s}")

    # 复制命令
    cmd_source = script_dir / "commands"
    if cmd_source.exists():
        for cmd_file in cmd_source.glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
            print(f"  命令: {cmd_file.name}")

    print()
    print("=" * 50)
    print("  安装完成！")
    print("=" * 50)
    print()
    print(f"Python 依赖: {' '.join(reqs) if reqs else '无'}")
    print(f"安装位置: {skills_dir}")
    print()
    print("在 opencode 中使用:")
    print("  /analyze @execution_tasks.xlsx")
    print()

    if os.name == "nt":
        input("按回车退出...")


if __name__ == "__main__":
    main()
