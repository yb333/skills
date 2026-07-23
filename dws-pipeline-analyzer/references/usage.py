"""运营埋点（usage tracking）。

记录命令执行情况，支持本地存档 + 内网上报，用于运营数据统计。
基于 architecture.md 任务层定位：有 I/O、有 CLI、从 engine 取数据。

设计三原则（不可破坏）：
1. 用户不可感知 —— 无 stdout、无日志噪音；config.json 静默存在
2. 不影响分析逻辑 —— record() 全程 try/except 吞异常；只在所有交付物落盘之后调用
3. 无性能影响 —— CSV 写入 <10ms；HTTP POST 末尾调用、2s 超时；失败落队列补发

数据流：
    命令结束(main 的 finally，交付物已落盘后)
       → 构建一行记录(record dict)
       → 写本地 usage.csv (完整存档，永远在)
       → 同步 POST 到服务端 (超时 2s，失败静默)
           ├ 成功 → 结束
           └ 失败 → 追加 usage_queue.jsonl
                      → 下次启动 flush_queue 补发 → 成功则删
"""

import csv
import json
import os
import platform
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

# ── 常量 ──────────────────────────────────────────────────────────────────

__version__ = "1.0"

# 内网服务端默认地址（端口与参考项目一致，路径用 /api/usage 区分）
DEFAULT_ENDPOINT = "http://10.96.160.123:3000/api/usage"

# HTTP 上报超时（秒）—— LAN 内网通常 30-50ms，2s 足够兜底
HTTP_TIMEOUT = 2.0

# 本地存档目录（与 opencode 运行时隔离，是 analyzer-agent 自己的数据）
USAGE_DIR = Path.home() / ".analyzer-agent"
CONFIG_PATH = USAGE_DIR / "config.json"
CSV_PATH = USAGE_DIR / "usage.csv"
QUEUE_PATH = USAGE_DIR / "usage_queue.jsonl"

# 数据字典 —— 17 字段，分 5 组（顺序即 CSV 列顺序，勿改）
FIELD_NAMES = [
    # A. 标识与上下文
    "run_id", "timestamp", "install_id", "user",
    # B. 命令与输入
    "command", "input_type", "asset", "target_table", "batch_id",
    # C. 规模指标
    "rule_count", "field_count", "source_count",
    # D. 执行结果
    "elapsed_sec", "elapsed_detail", "status", "error_type",
    # E. 质量与环境
    "quality_issues", "agent_version", "python_version", "os",
]


# ── 配置管理 ─────────────────────────────────────────────────────────────

def _ensure_dir():
    """确保存档目录存在（静默，失败不抛）。"""
    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def load_config() -> dict:
    """读取配置，不存在或损坏则生成默认。

    返回 dict 至少含 install_id / telemetry_enabled / created_at。
    全程吞异常 —— 埋点配置绝不能影响主功能。
    """
    try:
        _ensure_dir()
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 关键字段缺失则补齐
            if "install_id" not in cfg:
                cfg["install_id"] = str(uuid.uuid4())
            if "telemetry_enabled" not in cfg:
                cfg["telemetry_enabled"] = True
            if "created_at" not in cfg:
                cfg["created_at"] = datetime.now().isoformat(timespec="seconds")
            return cfg
        # 首次运行，生成
        cfg = {
            "install_id": str(uuid.uuid4()),
            "telemetry_enabled": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return cfg
    except Exception:
        # 配置完全不可用时返回内存默认值（不落盘，下次再试）
        return {
            "install_id": "unknown",
            "telemetry_enabled": True,
            "created_at": "",
        }


def set_enabled(enabled: bool) -> None:
    """开关埋点（改 config.json）。仅供手动配置用，主流程不调。"""
    try:
        cfg = load_config()
        cfg["telemetry_enabled"] = bool(enabled)
        _ensure_dir()
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _is_enabled() -> bool:
    """是否启用埋点：config.json 的 telemetry_enabled 且无环境变量禁用。"""
    try:
        if os.environ.get("ANALYZER_NO_TELEMETRY") == "1":
            return False
        return bool(load_config().get("telemetry_enabled", True))
    except Exception:
        return False


# ── 环境信息 ─────────────────────────────────────────────────────────────

def _get_user() -> str:
    """取系统用户名（内部团队工具，需知道谁在用）。

    优先用 getpass.getuser()（跨平台，Windows 走 GetUserName API），
    比手动查环境变量可靠——子进程/AI agent 调用时环境变量可能没透传。
    """
    try:
        import getpass
        name = getpass.getuser()
        return name or os.environ.get("USER") or os.environ.get("USERNAME") or ""
    except Exception:
        try:
            return os.environ.get("USER") or os.environ.get("USERNAME") or ""
        except Exception:
            return ""


def _get_os() -> str:
    """标准化 OS 名。"""
    try:
        s = platform.system().lower()
        if s == "windows":
            return "win"
        if s == "darwin":
            return "darwin"
        if s.startswith("linux"):
            return "linux"
        return s or "unknown"
    except Exception:
        return "unknown"


def _detect_endpoint() -> str:
    """取上报地址：环境变量优先，否则默认内网。"""
    try:
        return os.environ.get("ANALYZER_TELEMETRY_ENDPOINT", DEFAULT_ENDPOINT)
    except Exception:
        return DEFAULT_ENDPOINT


# ── 错误分类 ─────────────────────────────────────────────────────────────

def _classify_error(exc) -> str:
    """把异常映射成 error_type 枚举值（成功时传 None，不入库）。"""
    if exc is None:
        return ""
    try:
        name = type(exc).__name__
        msg = str(exc).lower()
        if name == "MemoryError" or "memory" in msg:
            return "memory_error"
        if "timeout" in msg or name == "TimeoutError":
            return "timeout"
        if "ddl" in msg or "parse_ddl" in name.lower():
            return "ddl_error"
        if any(k in msg for k in ("parse", "sqlglot", "syntax", "tokenize")):
            return "parse_error"
        if any(k in msg for k in ("io", "file", "permission", "notfound",
                                   "errno", "is a directory")):
            return "io_error"
        return "unknown"
    except Exception:
        return "unknown"


# ── 本地存档（CSV） ──────────────────────────────────────────────────────

def _write_csv(row: dict) -> None:
    """追加一行到 usage.csv，文件不存在则带表头创建。"""
    try:
        _ensure_dir()
        file_exists = CSV_PATH.exists()
        with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELD_NAMES,
                                    extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        pass


# ── 上报（HTTP） ─────────────────────────────────────────────────────────

def _post_one(row: dict, endpoint: str = None) -> bool:
    """同步 POST 一条记录到服务端，返回是否成功。

    用 urllib（不引入 requests 依赖）。超时 HTTP_TIMEOUT 秒。
    任何异常返回 False，由调用方决定是否落队列。
    """
    try:
        url = endpoint or _detect_endpoint()
        payload = json.dumps(row, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib_request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _enqueue(row: dict) -> None:
    """失败记录追加到本地队列（每行一个 JSON）。"""
    try:
        _ensure_dir()
        with open(QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_queue() -> list:
    """读取队列所有记录。损坏则返回空并删除损坏文件。"""
    try:
        if not QUEUE_PATH.exists():
            return []
        rows = []
        bad = False
        with open(QUEUE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    bad = True
        if bad:
            # 有损坏行，整体重建（只保留能解析的）
            try:
                QUEUE_PATH.unlink()
                for r in rows:
                    _enqueue(r)
            except Exception:
                pass
        return rows
    except Exception:
        return []


def _clear_queue() -> None:
    """清空队列文件。"""
    try:
        if QUEUE_PATH.exists():
            QUEUE_PATH.unlink()
    except Exception:
        pass


def flush_queue(max_attempts: int = 20) -> int:
    """补发队列里的失败记录，成功补发的从队列移除。

    每条最多尝试一次（按队列顺序），最多发 max_attempts 条避免启动卡太久。
    返回成功补发条数。fire-and-forget，任何异常静默。
    """
    if not _is_enabled():
        return 0
    try:
        rows = _read_queue()
        if not rows:
            return 0
        endpoint = _detect_endpoint()
        ok_ids = set()
        sent = 0
        for row in rows[:max_attempts]:
            rid = row.get("run_id")
            if rid in ok_ids:
                continue  # 队列内去重
            if _post_one(row, endpoint):
                ok_ids.add(rid)
                sent += 1
        # 只保留未成功的（含去重）
        remaining = [r for r in rows if r.get("run_id") not in ok_ids]
        _clear_queue()
        for r in remaining:
            _enqueue(r)
        return sent
    except Exception:
        return 0


# ── 核心对外接口 ──────────────────────────────────────────────────────────

def record(data: dict) -> None:
    """记录一次命令执行（唯一对外接口，fire-and-forget，全程吞异常）。

    参数 data 至少含 command；其余字段可选，缺的留空。
    会自动补充：run_id / timestamp / install_id / user / agent_version
              / python_version / os

    顺序：
        1. 检查开关
        2. 补充运行时字段
        3. 写本地 CSV（完整存档，无论是否上报都保留）
        4. 同步 POST（超时 2s，失败落 usage_queue.jsonl）

    任何异常静默丢弃 —— 这是硬约束。
    """
    try:
        if not _is_enabled():
            return
        if not isinstance(data, dict):
            return
        # 补充运行时字段
        cfg = load_config()
        row = {k: "" for k in FIELD_NAMES}
        row.update(data)
        row["run_id"] = row.get("run_id") or str(uuid.uuid4())
        row["timestamp"] = datetime.now().isoformat(timespec="seconds")
        row["install_id"] = cfg.get("install_id", "unknown")
        row["user"] = row.get("user") or _get_user()
        row["agent_version"] = __version__
        row["python_version"] = platform.python_version()
        row["os"] = _get_os()
        # elapsed_detail 序列化为 JSON 字符串（CSV 存文本，服务端存 TEXT）
        detail = row.get("elapsed_detail")
        if isinstance(detail, dict):
            row["elapsed_detail"] = json.dumps(detail, ensure_ascii=False)
        # CSV 先落（本地永远有完整存档）
        _write_csv(row)
        # 同步上报（失败落队列，不抛）
        if not _post_one(row):
            _enqueue(row)
    except Exception:
        pass


# ── CLI（手动查看/导出本地数据） ─────────────────────────────────────────

def main():
    """手动查看本地埋点数据。

    用法:
        python run.py usage                # 显示配置 + 本地统计摘要
        python run.py usage --export PATH  # 导出 CSV 副本到指定路径
        python run.py usage --off          # 关闭埋点
        python run.py usage --on           # 开启埋点
    """
    import argparse
    parser = argparse.ArgumentParser(description="运营埋点管理")
    parser.add_argument("--export", default="", help="导出 usage.csv 副本到指定路径")
    parser.add_argument("--off", action="store_true", help="关闭埋点")
    parser.add_argument("--on", action="store_true", help="开启埋点")
    args = parser.parse_args()

    if args.off:
        set_enabled(False)
        print("埋点已关闭")
        return
    if args.on:
        set_enabled(True)
        print("埋点已开启")
        return
    if args.export:
        import shutil
        try:
            shutil.copy2(CSV_PATH, args.export)
            print(f"已导出到: {args.export}")
        except Exception as e:
            print(f"导出失败: {e}")
        return

    # 默认：显示配置 + 统计
    cfg = load_config()
    print(f"=== 运营埋点配置 ===")
    print(f"配置文件: {CONFIG_PATH}")
    print(f"install_id: {cfg.get('install_id', '?')}")
    print(f"启用状态: {cfg.get('telemetry_enabled', True)}")
    print(f"上报地址: {_detect_endpoint()}")
    print(f"环境变量 ANALYZER_NO_TELEMETRY={os.environ.get('ANALYZER_NO_TELEMETRY', '未设置')}")
    print()
    if CSV_PATH.exists():
        try:
            with open(CSV_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            data_lines = [l for l in lines if l.strip()]
            print(f"=== 本地存档 ===")
            print(f"CSV 路径: {CSV_PATH}")
            print(f"总记录数: {len(data_lines) - 1}")  # 减表头
            # 最近 5 条
            print(f"最近记录:")
            for line in data_lines[-5:]:
                print(f"  {line.strip()[:120]}")
        except Exception as e:
            print(f"读取失败: {e}")
    else:
        print(f"本地存档: 尚无记录")
    if QUEUE_PATH.exists():
        try:
            with open(QUEUE_PATH, encoding="utf-8") as f:
                qlines = [l for l in f if l.strip()]
            if qlines:
                print(f"\n待补发队列: {len(qlines)} 条")
        except Exception:
            pass


if __name__ == "__main__":
    main()
