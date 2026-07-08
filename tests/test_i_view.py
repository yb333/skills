"""I 视图发现与链路扩展测试。

资产是 I 视图（dwb_xxx_i），F 表是底表。I 视图作为链路最后一步（F→I）加入分析。

覆盖场景：
- 直封 I 视图（按名字 _f→_i 发现，Step1 快路径）
- 有逻辑 I 视图（全局搜索来源表发现，Step2 兜底）
- 无 I 视图（容错，以 F 表为终点）
- 端到端：I 视图加入后 target_table 变成 I 视图，链路含 F→I 两步

运行:
    pytest tests/test_i_view.py -v
"""

import sys
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_repo import build_mock_repo


@pytest.fixture
def mock_repo(tmp_path):
    return build_mock_repo(tmp_path / "repo")


class TestExtractViewSql:
    """_extract_view_sql 从 CREATE VIEW 提取 SELECT。"""

    def test_simple_create_view(self):
        from analyzer import _extract_view_sql
        sql = _extract_view_sql("CREATE VIEW v AS SELECT a FROM t")
        assert "SELECT a FROM t" in sql

    def test_create_or_replace_view(self):
        from analyzer import _extract_view_sql
        sql = _extract_view_sql("CREATE OR REPLACE VIEW v AS SELECT a FROM t")
        assert "SELECT a FROM t" in sql

    def test_multiline(self):
        from analyzer import _extract_view_sql
        sql = _extract_view_sql(
            "CREATE OR REPLACE VIEW dws.dwb_i AS\n"
            "SELECT a, b\nFROM dws.dwb_f")
        assert "SELECT" in sql
        assert "dws.dwb_f" in sql

    def test_not_a_view(self):
        from analyzer import _extract_view_sql
        sql = _extract_view_sql("CREATE TABLE t (a INT)")
        assert sql == ""


class TestDiscoverIView:
    """I 视图发现（两步走）。"""

    def test_discover_by_name(self, mock_repo):
        """Step1: 按名字 _f→_i 发现直封 I 视图。"""
        from analyzer import discover_i_view
        f_group = mock_repo["f_group_dir"]
        result = discover_i_view(f_group, "dws", "dwb_trade_sum_f")

        assert result is not None, "应发现 I 视图"
        assert result["view_name"] == "dwb_trade_sum_i"
        assert "dws.dwb_trade_sum_f" in result["view_sql"], "SQL 应引用 F 表"

    def test_discover_by_source_search(self, mock_repo):
        """Step2: 命名不规律时，全局搜索来源表发现。"""
        from analyzer import discover_i_view
        # dwb_risk_alert_f 没有对应的 _i 直封视图，
        # 但有 v_trade_summary 引用了它（有逻辑的 I 视图）
        risk_group = mock_repo["other_group_dir"]
        result = discover_i_view(risk_group, "dws", "dwb_risk_alert_f")

        assert result is not None, "应通过全局搜索发现 I 视图"
        assert "dwb_risk_alert_f" in result["view_sql"].lower(), "SQL 应引用 F 表"

    def test_no_i_view_returns_none(self, mock_repo):
        """没有 I 视图时返回 None（容错，以 F 表为终点）。"""
        from analyzer import discover_i_view
        # 用一个不存在且没有视图引用的表名
        group = mock_repo["group_dir"]
        result = discover_i_view(group, "dws", "dwb_nonexistent_f")
        assert result is None

    def test_no_repo_root_returns_none(self, tmp_path):
        """不在代码仓里时返回 None。"""
        from analyzer import discover_i_view
        d = tmp_path / "no_repo"
        d.mkdir()
        assert discover_i_view(d, "dws", "dwb_xxx_f") is None


class TestExchangePartitionIView:
    """交换分区（rule_type=9）的 I 视图发现。

    交换分区的 target_table 是临时表，exchange_source_table 才是真正的 F 表。
    历史 bug：用 target_table（临时表）去找 I 视图，结果找不到。
    """

    def test_exchange_partition_uses_source_table(self, tmp_path):
        """交换分区时用 exchange_source_table 找 I 视图，不用 target_table。"""
        from _build_yml import build_yml_group
        from analyzer import discover_i_view

        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        view_dir = repo / "DDL" / "DWS_EDW" / "dws" / "view"
        view_dir.mkdir(parents=True)
        # I 视图引用的是真正的 F 表（dwb_exchange_f），不是临时表
        (view_dir / "dwb_exchange_i.sql").write_text(
            "CREATE VIEW dws.dwb_exchange_i AS SELECT * FROM dws.dwb_exchange_f;",
            encoding="utf-8")

        group = repo / "BFT" / "grp"
        build_yml_group(group, rules=[{
            "rule_code": "X1", "rule_type": 9, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "tmp_exchange",
            "query_sql": "ALTER TABLE dwb_exchange_f EXCHANGE PARTITION p1",
            "rule_name": "交换", "rule_group_code": "GR", "rule_group_en": "DWB_EXCHANGE_F",
            "exchange_source_table": "dwb_exchange_f",
        }])

        # 用真正的 F 表名找，应找到
        result = discover_i_view(group, "dws", "dwb_exchange_f")
        assert result is not None, "用 exchange_source_table 应找到 I 视图"
        assert result["view_name"] == "dwb_exchange_i"

        # 用临时表名找，应找不到（临时表没有视图）
        result_wrong = discover_i_view(group, "dws", "tmp_exchange")
        assert result_wrong is None, "临时表不应有 I 视图"


class TestIViewInAnalysisPipeline:
    """端到端：I 视图加入分析链路。"""

    def test_i_view_appended_to_pipeline(self, mock_repo, tmp_path):
        """I 视图作为最后一步加入，target_table 变成 I 视图。"""
        from analyzer import main
        import sys as _sys

        f_group = mock_repo["f_group_dir"]
        output_base = tmp_path / "output"
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(f_group),
                     "--output", str(output_base)]
        try:
            main()
        finally:
            _sys.argv = old_argv

        out_dir = output_base / "DWB_TRADE_SUM_F"
        kj = json.loads((out_dir / "knowledge_draft.json").read_text(encoding="utf-8"))

        # target_table 应是 I 视图（不是 F 表）
        assert kj["meta"]["target_table"] == "dwb_trade_sum_i", \
            f"target_table 应是 I 视图，实际 {kj['meta']['target_table']}"
        # 链路应有 2 步：F表 + I视图
        steps = kj["topology"]["steps"]
        assert len(steps) == 2, f"应有 2 步（F+I），实际 {len(steps)}"
        assert steps[0]["target_table"] == "dwb_trade_sum_f"
        assert steps[1]["target_table"] == "dwb_trade_sum_i"

    def test_no_i_view_keeps_f_as_target(self, tmp_path):
        """没有 I 视图时，target_table 仍是 F 表（容错）。"""
        from analyzer import main, read_yml
        from _build_yml import build_yml_group
        import sys as _sys

        # 构造一个没有任何视图引用的 F 表规则组
        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        (repo / "DDL").mkdir()
        group = repo / "BFT" / "grp"
        build_yml_group(group, rules=[
            {"rule_code": "X1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_noview_f",
             "query_sql": "SELECT a.x FROM ods.src a",
             "rule_group_code": "GR", "rule_group_en": "DWB_NOVIEW_F"},
        ])

        output_base = tmp_path / "output"
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group),
                     "--output", str(output_base)]
        try:
            main()
        finally:
            _sys.argv = old_argv

        out_dir = output_base / "DWB_NOVIEW_F"
        kj = json.loads((out_dir / "knowledge_draft.json").read_text(encoding="utf-8"))
        # 没有视图，target_table 仍是 F 表
        assert kj["meta"]["target_table"] == "dwb_noview_f"

    def test_i_view_fields_exist(self, mock_repo, tmp_path):
        """I 视图步骤有字段（从 SELECT 解析，类型暂从 F 表继承或留空）。"""
        from analyzer import main
        import sys as _sys

        f_group = mock_repo["f_group_dir"]
        output_base = tmp_path / "output3"
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(f_group),
                     "--output", str(output_base)]
        try:
            main()
        finally:
            _sys.argv = old_argv

        out_dir = output_base / "DWB_TRADE_SUM_F"
        kj = json.loads((out_dir / "knowledge_draft.json").read_text(encoding="utf-8"))

        # I 视图步骤（step_2）应有字段
        step2_fields = [f for f in kj["field_mappings"]["fields"]
                        if f.get("producing_step") == "step_2"]
        assert step2_fields, "I 视图步骤应有字段"
        # I 视图字段名应来自 SELECT（cust_id, total）
        field_names = {f.get("target_field") for f in step2_fields}
        assert "cust_id" in field_names or "total" in field_names, \
            f"I 视图字段应含 cust_id/total，实际 {field_names}"
