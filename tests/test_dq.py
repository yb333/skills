"""DQ 质量检查发现测试。

覆盖：目录+目标表双确认定位 / 多条DQ规则 / 无DQ容错 / knowledge注入。

运行:
    pytest tests/test_dq.py -v
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
from analyzer import _discover_dq_from_repo, _parse_dq_yml


@pytest.fixture
def mock_repo(tmp_path):
    return build_mock_repo(tmp_path / "repo")


# ════════════════════════════════════════════════════════════════════════
# 1. 发现函数
# ════════════════════════════════════════════════════════════════════════

class TestDiscoverDq:
    """DQ 质量检查发现。"""

    def test_find_dq_rules_by_target_table(self, mock_repo):
        """按目标表找到 DQ 规则"""
        group_dir = mock_repo["group_dir"]
        rules = _discover_dq_from_repo(group_dir, "dwb_trade_order_d")
        assert rules is not None
        assert len(rules) == 2
        numbers = [r["rule_number"] for r in rules]
        assert "DQ_000001" in numbers
        assert "DQ_000002" in numbers

    def test_dq_rule_has_key_fields(self, mock_repo):
        """DQ 规则提取了关键字段"""
        group_dir = mock_repo["group_dir"]
        rules = _discover_dq_from_repo(group_dir, "dwb_trade_order_d")
        r = rules[0]
        assert r["rule_number"]
        assert r["rule_name"]
        assert r["sql"]
        assert r["target_table"] == "dwb_trade_order_d"

    def test_no_match_returns_none(self, mock_repo):
        """不匹配的目标表返回 None"""
        group_dir = mock_repo["group_dir"]
        result = _discover_dq_from_repo(group_dir, "table_not_exist")
        assert result is None

    def test_empty_target_returns_none(self, mock_repo):
        """空目标表返回 None"""
        group_dir = mock_repo["group_dir"]
        assert _discover_dq_from_repo(group_dir, "") is None

    def test_outside_repo_returns_none(self, tmp_path):
        """不在代码仓里返回 None"""
        random_dir = tmp_path / "not_a_repo"
        random_dir.mkdir()
        assert _discover_dq_from_repo(random_dir, "dwb_trade_order_d") is None


# ════════════════════════════════════════════════════════════════════════
# 2. yml 解析
# ════════════════════════════════════════════════════════════════════════

class TestParseDqYml:
    """单个 DQ yml 解析。"""

    def test_parse_extracts_fields(self, mock_repo):
        """解析出关键字段"""
        dq_file = mock_repo["repo_root"] / "DQ" / "FIN_DWB" / "FIN_DWB_COMMEN" / "dwb_trade_order_d" / "DQ_000001.yml"
        rule = _parse_dq_yml(dq_file)
        assert rule is not None
        assert rule["rule_number"] == "DQ_000001"
        assert rule["rule_name"] == "空值检查"
        assert "order_id" in rule["sql"]
        assert rule["target_table"] == "dwb_trade_order_d"

    def test_parse_corrupted_yml(self, tmp_path):
        """损坏的 yml 返回 None 不崩溃"""
        bad = tmp_path / "bad.yml"
        bad.write_text("{{{{not yaml", encoding="utf-8")
        assert _parse_dq_yml(bad) is None


# ════════════════════════════════════════════════════════════════════════
# 3. knowledge 注入（端到端）
# ════════════════════════════════════════════════════════════════════════

class TestDqInAnalysis:
    """DQ 注入到 knowledge（端到端）。"""

    def test_dq_injected_into_knowledge(self, mock_repo, tmp_path):
        """分析 yml 目录后 knowledge 含 dq_rules"""
        import json
        from analyzer import main
        out_dir = tmp_path / "output"
        sys.argv = ["analyzer", "--input", str(mock_repo["group_dir"]), "--output", str(out_dir)]
        main()

        kj_path = out_dir / mock_repo["group_dir"].name / "knowledge_draft.json"
        knowledge = json.loads(kj_path.read_text(encoding="utf-8"))
        assert "dq_rules" in knowledge.get("meta", {}), "knowledge.meta 应含 dq_rules"
        rules = knowledge["meta"]["dq_rules"]
        assert len(rules) == 2

    def test_no_dq_when_xlsx_mode(self, tmp_path):
        """xlsx 模式不发现 DQ"""
        import json
        from _build_xlsx import build_xlsx
        from analyzer import main

        xlsx = tmp_path / "test.xlsx"
        build_xlsx(str(xlsx), rules=[
            {"rule_code": "R001", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "tmp_test",
             "delete_mode": "1", "query_sql": "SELECT 1 AS id FROM dual",
             "rule_name": "test", "rule_group_code": "GR_TEST",
             "rule_group_en": "DWB_TEST_F"},
        ])
        out = tmp_path / "output"
        sys.argv = ["analyzer", "--input", str(xlsx), "--output", str(out)]
        main()

        kj = out / "DWB_TEST_F" / "knowledge_draft.json"
        knowledge = json.loads(kj.read_text(encoding="utf-8"))
        assert "dq_rules" not in knowledge.get("meta", {})
