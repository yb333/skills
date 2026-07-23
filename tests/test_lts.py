"""LTS 调度任务发现测试。

覆盖：F+I 双任务匹配 / 命名推导 / 不匹配返回 None /
损坏 yml 不崩溃 / knowledge 注入 / 干扰项不误匹配。

运行:
    pytest tests/test_lts.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_repo import build_mock_repo
from analyzer import _discover_lts_from_repo, _parse_lts_task


@pytest.fixture
def mock_repo(tmp_path):
    """构造含真实 LTS 结构的 mock 代码仓。"""
    return build_mock_repo(tmp_path / "repo")


# ════════════════════════════════════════════════════════════════════════
# 1. 发现函数
# ════════════════════════════════════════════════════════════════════════

class TestDiscoverLts:
    """LTS 调度任务发现。"""

    def test_find_f_task_by_group_code(self, mock_repo):
        """按 V_GROUP_CODE 找到 F 任务"""
        group_dir = mock_repo["group_dir"]
        result = _discover_lts_from_repo(group_dir, "GR_TRADE_ORDER")
        assert result is not None
        assert result["f_task"]["task_name"] == "TASK_TRADE_ORDER_F"
        assert result["f_task"]["group_code"] == "GR_TRADE_ORDER"

    def test_find_i_task_by_naming(self, mock_repo):
        """从 F 任务名推导 I 任务（_F → _I）"""
        group_dir = mock_repo["group_dir"]
        result = _discover_lts_from_repo(group_dir, "GR_TRADE_ORDER")
        assert result["i_task"] is not None
        assert result["i_task"]["task_name"] == "TASK_TRADE_ORDER_I"

    def test_i_task_depends_on_f(self, mock_repo):
        """I 任务的 job 依赖指向 F"""
        group_dir = mock_repo["group_dir"]
        result = _discover_lts_from_repo(group_dir, "GR_TRADE_ORDER")
        i_jobs = result["i_task"]["jobs"]
        # I 任务的第二个 job 是 tskdep 依赖
        dep_jobs = [j for j in i_jobs if j["type"] == "tskdep"]
        assert len(dep_jobs) >= 1

    def test_no_match_returns_none(self, mock_repo):
        """不匹配的 rule_group_code 返回 None"""
        group_dir = mock_repo["group_dir"]
        result = _discover_lts_from_repo(group_dir, "GR_NOT_EXIST")
        assert result is None

    def test_empty_group_code_returns_none(self, mock_repo):
        """空 rule_group_code 返回 None"""
        group_dir = mock_repo["group_dir"]
        assert _discover_lts_from_repo(group_dir, "") is None

    def test_outside_repo_returns_none(self, tmp_path):
        """不在代码仓里返回 None"""
        random_dir = tmp_path / "not_a_repo"
        random_dir.mkdir()
        result = _discover_lts_from_repo(random_dir, "GR_TRADE_ORDER")
        assert result is None

    def test_disturb_task_not_matched(self, mock_repo):
        """干扰任务（GROUP_OTHER）不会被误匹配"""
        group_dir = mock_repo["group_dir"]
        # GR_OTHER_NOT_MATCHED 存在于干扰 yml 里，但不该匹配 GR_TRADE_ORDER
        result = _discover_lts_from_repo(group_dir, "GR_TRADE_ORDER")
        assert result["f_task"]["task_name"] == "TASK_TRADE_ORDER_F"
        assert result["f_task"]["task_name"] != "TASK_OTHER_F"


# ════════════════════════════════════════════════════════════════════════
# 2. yml 解析
# ════════════════════════════════════════════════════════════════════════

class TestParseLtsTask:
    """单个 LTS yml 解析。"""

    def test_parse_extracts_key_fields(self, mock_repo):
        """解析出关键字段"""
        lts_file = mock_repo["repo_root"] / "LTS" / "BCNB_DAILY" / "GROUP_TRADE" / "TASK_TRADE_ORDER_F.yml"
        task = _parse_lts_task(lts_file)
        assert task is not None
        assert task["task_name"] == "TASK_TRADE_ORDER_F"
        assert task["task_type"] == "周期任务"
        assert task["schedule_cron"] == "0 30 2 * * ?"
        assert task["owner"] == "test_user"
        assert task["group_code"] == "GR_TRADE_ORDER"
        assert len(task["jobs"]) == 2
        assert len(task["params"]) == 2

    def test_parse_corrupted_yml_returns_none(self, tmp_path):
        """损坏的 yml 返回 None 不崩溃"""
        bad_file = tmp_path / "bad.yml"
        bad_file.write_text("{{{{not valid yaml", encoding="utf-8")
        assert _parse_lts_task(bad_file) is None

    def test_parse_empty_yml_returns_none(self, tmp_path):
        """空 yml 返回 None"""
        empty_file = tmp_path / "empty.yml"
        empty_file.write_text("", encoding="utf-8")
        assert _parse_lts_task(empty_file) is None


# ════════════════════════════════════════════════════════════════════════
# 3. knowledge 注入（端到端）
# ════════════════════════════════════════════════════════════════════════

class TestLtsInAnalysis:
    """LTS 注入到 knowledge（端到端集成测试）。"""

    def test_schedule_injected_into_knowledge(self, mock_repo, tmp_path):
        """分析 yml 目录后 knowledge 含 schedule"""
        import json
        from analyzer import main
        out_dir = tmp_path / "output"

        sys.argv = [
            "analyzer", "--input", str(mock_repo["group_dir"]),
            "--output", str(out_dir),
        ]
        main()

        kj_path = out_dir / mock_repo["group_dir"].name / "knowledge_draft.json"
        knowledge = json.loads(kj_path.read_text(encoding="utf-8"))

        assert "schedule" in knowledge.get("meta", {}), "knowledge.meta 应含 schedule"
        sched = knowledge["meta"]["schedule"]
        assert sched["f_task"]["task_name"] == "TASK_TRADE_ORDER_F"
        assert sched["i_task"]["task_name"] == "TASK_TRADE_ORDER_I"

    def test_no_schedule_when_xlsx_mode(self, tmp_path):
        """xlsx 模式不发现 LTS（只有 yml 模式才发现）"""
        import json
        from _build_xlsx import build_xlsx
        from analyzer import main

        xlsx_path = tmp_path / "test.xlsx"
        build_xlsx(str(xlsx_path), rules=[
            {"rule_code": "R001", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "tmp_test",
             "delete_mode": "1", "query_sql": "SELECT 1 AS id FROM dual",
             "rule_name": "test", "rule_group_code": "GR_TEST",
             "rule_group_en": "DWB_TEST_F"},
        ])
        out_dir = tmp_path / "output"
        sys.argv = ["analyzer", "--input", str(xlsx_path), "--output", str(out_dir)]
        main()

        kj_path = out_dir / "DWB_TEST_F" / "knowledge_draft.json"
        knowledge = json.loads(kj_path.read_text(encoding="utf-8"))
        assert "schedule" not in knowledge.get("meta", {}), "xlsx 模式不该有 schedule"
