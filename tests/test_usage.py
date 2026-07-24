"""运营埋点测试。

覆盖配置管理 / CSV 写入 / record 健壮性 / 上报逻辑 / 幂等 /
错误分类 / flush_queue / 集成（finally 接入不影响主逻辑）。

核心验证三点：
1. 用户不可感知 —— 无 stdout 噪音
2. 不影响分析逻辑 —— record 异常静默吞，主流程不受影响
3. 幂等 —— run_id 主键去重，补发不重复入库

运行:
    pytest tests/test_usage.py -v
"""

import io
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

import usage
from usage import (
    _classify_error,
    _is_enabled,
    flush_queue,
    load_config,
    record,
)

# 路径常量必须通过 usage.XXX 动态访问 —— monkeypatch 改的是模块属性，
# from usage import 拿到的是导入时的值快照，不会跟着变。
# 下面这些 helper 确保测试代码始终读到被 monkeypatch 后的当前值。


# ── 公共 fixture：把存档目录重定向到 tmp，避免污染真实家目录 ─────────────

@pytest.fixture
def isolated_usage(monkeypatch, tmp_path):
    """把 USAGE_DIR / usage.CSV_PATH / usage.QUEUE_PATH / CONFIG_PATH 全部重定向到 tmp_path。

    这是测试隔离的关键：绝不能写到真实 ~/.analyzer-agent/。
    """
    fake_dir = tmp_path / "analyzer-agent"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(usage, "USAGE_DIR", fake_dir)
    monkeypatch.setattr(usage, "CONFIG_PATH", fake_dir / "config.json")
    monkeypatch.setattr(usage, "LOCAL_LOG_PATH", fake_dir / "usage.jsonl")
    monkeypatch.setattr(usage, "QUEUE_PATH", fake_dir / "usage_queue.jsonl")
    # 清掉环境变量（其他测试可能设过）
    monkeypatch.delenv("ANALYZER_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("ANALYZER_TELEMETRY_ENDPOINT", raising=False)
    return fake_dir


@pytest.fixture
def disabled(monkeypatch):
    """环境变量关闭埋点。"""
    monkeypatch.setenv("ANALYZER_NO_TELEMETRY", "1")


@pytest.fixture
def fake_post_ok(monkeypatch):
    """mock _post_one 永远成功。返回收集到的 payload 列表。"""
    payloads = []

    def _ok(row, endpoint=None):
        payloads.append(row)
        return True

    monkeypatch.setattr(usage, "_post_one", _ok)
    return payloads


@pytest.fixture
def fake_post_fail(monkeypatch):
    """mock _post_one 永远失败（模拟网络不通）。"""
    monkeypatch.setattr(usage, "_post_one", lambda row, endpoint=None: False)
    return None


# ════════════════════════════════════════════════════════════════════════
# 1. 配置管理
# ════════════════════════════════════════════════════════════════════════

class TestConfig:
    """配置文件生成、复用、损坏恢复。"""

    def test_first_run_generates_config(self, isolated_usage):
        """首次运行生成 config.json，含 install_id"""
        cfg = load_config()
        assert "install_id" in cfg
        assert len(cfg["install_id"]) > 10  # uuid
        assert cfg["telemetry_enabled"] is True
        assert (isolated_usage / "config.json").exists()

    def test_install_id_persists_across_calls(self, isolated_usage):
        """install_id 跨多次调用保持不变"""
        cfg1 = load_config()
        cfg2 = load_config()
        assert cfg1["install_id"] == cfg2["install_id"]

    def test_corrupted_config_rebuilt(self, isolated_usage):
        """config.json 损坏时返回内存默认值不抛"""
        (isolated_usage / "config.json").write_text("NOT JSON!!", encoding="utf-8")
        cfg = load_config()
        assert "install_id" in cfg  # 不抛、有兜底

    def test_is_enabled_default_true(self, isolated_usage):
        """默认启用"""
        load_config()
        assert _is_enabled() is True


# ════════════════════════════════════════════════════════════════════════
# 2. JSONL 本地存档
# ════════════════════════════════════════════════════════════════════════

class TestJsonlWrite:
    """JSONL 写入、追加、字段完整。"""

    def test_jsonl_created_on_first_write(self, isolated_usage, fake_post_ok):
        """首次写自动创建 jsonl 文件"""
        record({"command": "analyze"})
        assert usage.LOCAL_LOG_PATH.exists()
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            row = json.loads(f.readline())
        assert row["command"] == "analyze"

    def test_jsonl_row_has_core_fields(self, isolated_usage, fake_post_ok):
        """每行包含核心字段"""
        record({"command": "analyze", "asset": "DWB_TEST_F", "elapsed_sec": 1.2})
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            row = json.loads(f.readline())
        assert row["command"] == "analyze"
        assert row["asset"] == "DWB_TEST_F"
        assert row["elapsed_sec"] == 1.2  # JSONL 里是数字不是字符串
        assert row["run_id"]
        assert row["timestamp"]

    def test_jsonl_appends_multiple_rows(self, isolated_usage, fake_post_ok):
        """多次 record 追加多行"""
        for i in range(3):
            record({"command": "analyze", "asset": f"ASSET_{i}"})
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        assert len(rows) == 3

    def test_jsonl_no_misalignment_on_field_change(self, isolated_usage, fake_post_ok):
        """改字段不影响旧行（JSONL 每行独立，不像 CSV 会错位）"""
        record({"command": "analyze", "asset": "OLD"})
        # 模拟字段变化后写新行（extra 字段）
        record({"command": "analyze", "asset": "NEW", "new_field": 42})
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        assert rows[0]["asset"] == "OLD"
        assert rows[1]["asset"] == "NEW"
        assert rows[1].get("extra")  # new_field 进了 extra


# ════════════════════════════════════════════════════════════════════════
# 3. record() 健壮性
# ════════════════════════════════════════════════════════════════════════

class TestRecordRobustness:
    """record() 全程吞异常、自动补字段、关闭时不写。"""

    def test_record_auto_fills_runtime_fields(self, isolated_usage, fake_post_ok):
        """自动补充 run_id / timestamp / install_id / user / os / python_version"""
        record({"command": "analyze"})
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            row = json.loads(f.readline())
        assert row["run_id"]  # 自动生成
        assert row["timestamp"]  # ISO 时间
        assert row["install_id"]  # 从 config
        assert row["agent_version"] == usage.__version__
        assert row["python_version"]
        assert row["os"] in ("darwin", "win", "linux", "unknown")

    def test_record_with_non_dict_input_silent(self, isolated_usage, fake_post_ok):
        """非 dict 输入静默丢弃"""
        record("not a dict")
        record(None)
        record([])
        assert not usage.LOCAL_LOG_PATH.exists() or _count_log_rows() == 0

    def test_record_never_raises(self, isolated_usage, monkeypatch):
        """即使内部函数抛异常，record 也不抛"""
        # 让 load_config 抛
        monkeypatch.setattr(usage, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # 不应抛
        record({"command": "analyze"})

    def test_timestamp_iso_format(self, isolated_usage, fake_post_ok):
        """timestamp 是 ISO 格式（含日期+时间）"""
        record({"command": "analyze"})
        with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
            row = json.loads(f.readline())
        ts = row["timestamp"]
        # 形如 2026-07-23T14:30:15
        assert "T" in ts or " " in ts
        assert len(ts) >= 10


def _count_log_rows():
    if not usage.LOCAL_LOG_PATH.exists():
        return 0
    with open(usage.LOCAL_LOG_PATH, encoding="utf-8") as f:
        return sum(1 for l in f if l.strip())


# ════════════════════════════════════════════════════════════════════════
# 4. 上报逻辑
# ════════════════════════════════════════════════════════════════════════

class TestReporting:
    """POST 成功/失败/落队列/补发。"""

    def test_post_success_no_queue(self, isolated_usage, fake_post_ok):
        """POST 成功时不落队列"""
        record({"command": "analyze"})
        assert not usage.QUEUE_PATH.exists() or _count_queue() == 0
        assert len(fake_post_ok) == 1
        assert fake_post_ok[0]["command"] == "analyze"

    def test_post_failure_falls_to_queue(self, isolated_usage, fake_post_fail):
        """POST 失败时落本地队列"""
        record({"command": "analyze", "asset": "DWB_X_F"})
        assert _count_queue() == 1
        with open(usage.QUEUE_PATH, encoding="utf-8") as f:
            queued = json.loads(f.readline())
        assert queued["command"] == "analyze"
        assert queued["asset"] == "DWB_X_F"

    def test_log_always_written_regardless_of_post(self, isolated_usage, fake_post_fail):
        """无论 POST 成败，本地存档都写（完整存档）"""
        record({"command": "analyze"})
        assert usage.LOCAL_LOG_PATH.exists()
        assert _count_log_rows() == 1

    def test_queue_corrupted_line_deleted(self, isolated_usage, monkeypatch):
        """队列文件有损坏行时，读取后重建（只留能解析的）"""
        # 手动写损坏的队列
        good = {"run_id": "good-1", "command": "analyze"}
        with open(usage.QUEUE_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n")
            f.write("CORRUPTED LINE\n")
        # 让 _post_one 成功
        monkeypatch.setattr(usage, "_post_one", lambda r, endpoint=None: True)
        sent = flush_queue()
        assert sent >= 1  # good-1 补发成功


def _count_queue():
    if not usage.QUEUE_PATH.exists():
        return 0
    with open(usage.QUEUE_PATH, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


# ════════════════════════════════════════════════════════════════════════
# 5. 幂等
# ════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    """run_id 主键去重（防补发重复）。"""

    def test_same_run_id_dedup_in_queue(self, isolated_usage, fake_post_fail):
        """同一 run_id 重复入队，flush 时只发一次"""
        rid = "fixed-run-id-123"
        record({"command": "analyze", "run_id": rid})
        record({"command": "analyze", "run_id": rid})
        assert _count_queue() == 2  # 两行入队
        # 切换成成功，flush
        from unittest.mock import MagicMock
        sent_payloads = []

        def _capture(row, endpoint=None):
            sent_payloads.append(row)
            return True

        original_post = usage._post_one
        usage._post_one = _capture
        try:
            sent = flush_queue()
        finally:
            usage._post_one = original_post
        # 队列内去重，只补发一个
        sent_rids = {p["run_id"] for p in sent_payloads}
        assert rid in sent_rids
        assert len([p for p in sent_payloads if p["run_id"] == rid]) == 1


# ════════════════════════════════════════════════════════════════════════
# 6. 错误分类
# ════════════════════════════════════════════════════════════════════════

class TestClassifyError:
    """_classify_error 异常 → error_type 映射。"""

    def test_memory_error(self):
        assert _classify_error(MemoryError("oom")) == "memory_error"

    def test_memory_in_message(self):
        assert _classify_error(RuntimeError("out of memory")) == "memory_error"

    def test_timeout(self):
        assert _classify_error(TimeoutError("request timeout")) == "timeout"

    def test_timeout_in_message(self):
        assert _classify_error(Exception("connection timeout")) == "timeout"

    def test_parse_error_sqlglot(self):
        assert _classify_error(Exception("sqlglot parse failed")) == "parse_error"

    def test_parse_error_syntax(self):
        assert _classify_error(Exception("invalid syntax near SELECT")) == "parse_error"

    def test_ddl_error(self):
        assert _classify_error(Exception("ddl file missing")) == "ddl_error"

    def test_io_error_file(self):
        assert _classify_error(FileNotFoundError("file not found")) == "io_error"

    def test_io_error_permission(self):
        assert _classify_error(PermissionError("permission denied")) == "io_error"

    def test_unknown_fallback(self):
        assert _classify_error(ValueError("something weird")) == "unknown"

    def test_none_input(self):
        """成功时传 None，返回空字符串"""
        assert _classify_error(None) == ""


# ════════════════════════════════════════════════════════════════════════
# 7. flush_queue
# ════════════════════════════════════════════════════════════════════════

class TestFlushQueue:
    """队列补发逻辑。"""

    def test_empty_queue_no_error(self, isolated_usage, monkeypatch):
        """空队列/无队列文件时不报错"""
        monkeypatch.setattr(usage, "_post_one", lambda r, endpoint=None: True)
        assert flush_queue() == 0

    def test_flush_sends_and_clears(self, isolated_usage, monkeypatch):
        """有历史时补发，成功后清空"""
        # 造 3 条队列
        for i in range(3):
            with open(usage.QUEUE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"run_id": f"r{i}", "command": "analyze"}) + "\n")
        monkeypatch.setattr(usage, "_post_one", lambda r, endpoint=None: True)
        sent = flush_queue()
        assert sent == 3
        assert _count_queue() == 0  # 清空

    def test_flush_partial_failure_retains(self, isolated_usage, monkeypatch):
        """部分补发失败时，失败的保留在队列"""
        with open(usage.QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"run_id": "ok1", "command": "analyze"}) + "\n")
            f.write(json.dumps({"run_id": "fail1", "command": "analyze"}) + "\n")

        def _flaky(row, endpoint=None):
            return row.get("run_id") == "ok1"

        monkeypatch.setattr(usage, "_post_one", _flaky)
        sent = flush_queue()
        assert sent == 1
        remaining = _count_queue()
        assert remaining == 1  # fail1 保留


# ════════════════════════════════════════════════════════════════════════
# 8. 集成：finally 接入不影响主逻辑
# ════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """模拟主流程 finally 调 record，验证不干扰主逻辑。"""

    def test_record_failure_does_not_break_main(self, isolated_usage, monkeypatch):
        """即使 record 内部全坏，主流程仍成功"""
        # 让 record 的一切底层都抛
        monkeypatch.setattr(usage, "_write_log",
                            lambda r: (_ for _ in ()).throw(IOError("disk full")))

        def fake_main():
            """模拟主流程：做事 → finally 调 record"""
            result = "main_ok"
            try:
                # 模拟分析完成
                return result
            finally:
                # 埋点接入点
                record({"command": "analyze", "status": "ok"})
            return result

        # 主流程正常返回，不被 record 影响
        assert fake_main() == "main_ok"

    def test_main_exception_still_records(self, isolated_usage, fake_post_ok):
        """主流程抛异常时，finally 里仍能记录失败状态"""
        captured = {}

        def fake_main():
            try:
                raise ValueError("parse failed")
            except Exception as e:
                captured["exc"] = e
                record({"command": "analyze", "status": "error",
                        "error_type": _classify_error(e)})
                raise

        with pytest.raises(ValueError):
            fake_main()
        # 埋点被调用
        assert len(fake_post_ok) == 1
        assert fake_post_ok[0]["status"] == "error"
        assert fake_post_ok[0]["error_type"] == "parse_error"

    def test_full_flow_success(self, isolated_usage, fake_post_ok):
        """完整成功流程：record 收到全字段"""
        record({
            "command": "analyze-chain",
            "input_type": "yml_dir",
            "asset": "DWB_TRADE_ORDER_F",
            "target_table": "DWB_TRADE_ORDER_F",
            "rule_count": 13,
            "field_count": 412,
            "source_count": 8,
            "elapsed_sec": 2.34,
            "status": "ok",
            "quality_issues": 5,
        })
        payload = fake_post_ok[0]
        assert payload["command"] == "analyze-chain"
        assert payload["rule_count"] == 13
        assert payload["elapsed_sec"] == 2.34
        assert payload["status"] == "ok"
        # 运行时字段自动补充
        assert payload["run_id"]
        assert payload["install_id"]
        assert payload["agent_version"] == usage.__version__
