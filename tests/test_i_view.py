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

from _build_yml import build_yml_group

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


class TestExchangePartitionWithIView:
    """交换分区 + I 视图组合场景（暴露过两个 bug 的关键回归用例）。

    Bug1：加工方式显示"未知"——detect_load_strategy 把 I 视图步骤当 target_rule，
          I 视图没 delete_mode → 未知。
    Bug2：view_step 错位——用 exec_sequence 算 step_id，但 step_id 是按列表位置
          生成的，两者不一定一致，导致 view_step 指向错误的步骤。
    """

    def _build_exchange_i_view_repo(self, tmp_path):
        """构造：3步加工 + 交换分区 + I 视图。"""
        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        view_dir = repo / "DDL" / "DWS_EDW" / "dws" / "view"
        view_dir.mkdir(parents=True)
        table_dir = repo / "DDL" / "DWS_EDW" / "dws" / "table"
        table_dir.mkdir(parents=True)
        group = repo / "BFT" / "grp"
        build_yml_group(group, rules=[
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "tmp1", "delete_mode": "1",
             "query_sql": "SELECT a.id, a.val FROM ods.src a",
             "rule_name": "s1", "rule_group_code": "GR", "rule_group_en": "DWB_TEST_F"},
            {"rule_code": "R3", "rule_type": 1, "exec_sequence": 3,
             "target_schema": "dws", "target_table": "tmp_f", "delete_mode": "1",
             "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.tmp2 t GROUP BY t.id",
             "rule_name": "s3", "rule_group_code": "GR", "rule_group_en": "DWB_TEST_F"},
            {"rule_code": "R4", "rule_type": 9, "exec_sequence": 4,
             "target_schema": "dws", "target_table": "tmp_f", "delete_mode": "",
             "query_sql": "", "exchange_source_table": "dwb_test_f",
             "rule_name": "交换", "rule_group_code": "GR", "rule_group_en": "DWB_TEST_F"},
        ])
        (view_dir / "dwb_test_i.sql").write_text(
            "CREATE VIEW dws.dwb_test_i AS SELECT id, total FROM dws.dwb_test_f;",
            encoding="utf-8")
        (table_dir / "dwb_test_f.sql").write_text(
            "CREATE TABLE dwb_test_f (id VARCHAR(64), total DECIMAL(18,2));",
            encoding="utf-8")
        return group

    def test_load_strategy_not_unknown(self, tmp_path):
        """Bug1 防回归：交换分区+I视图场景，加工方式不是'未知'。"""
        from analyzer import main
        import sys as _sys

        group = self._build_exchange_i_view_repo(tmp_path)
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group), "--output", str(tmp_path / "out")]
        try:
            main()
        finally:
            _sys.argv = old_argv

        kj = json.loads((tmp_path / "out" / "DWB_TEST_F" /
                         "knowledge_draft.json").read_text(encoding="utf-8"))
        ls = kj["meta"]["load_strategy"]
        assert ls["strategy"] != "unknown", \
            f"加工方式不应是未知（I视图步骤不该干扰判断），实际 {ls}"

    def test_view_step_matches_topology(self, tmp_path):
        """Bug2 防回归：view_step 与 topology 里 I 视图的 step_id 一致。"""
        from analyzer import main
        import sys as _sys

        group = self._build_exchange_i_view_repo(tmp_path)
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group), "--output", str(tmp_path / "out")]
        try:
            main()
        finally:
            _sys.argv = old_argv

        kj = json.loads((tmp_path / "out" / "DWB_TEST_F" /
                         "knowledge_draft.json").read_text(encoding="utf-8"))
        ai = kj["meta"].get("asset_info", {})
        assert ai.get("view_step"), "应有 view_step"

        # view_step 必须和 topology 里 I 视图步骤的 step_id 一致
        view_step_in_topo = None
        for s in kj["topology"]["steps"]:
            if "_VIEW" in s.get("rule_code", ""):
                view_step_in_topo = s["step_id"]
                break
        assert view_step_in_topo, "topology 里应有 I 视图步骤"
        assert ai["view_step"] == view_step_in_topo, \
            f"view_step({ai['view_step']}) 应与 topology({view_step_in_topo}) 一致"


class TestIViewFieldPenetration:
    """I 视图字段穿透合并（以 I 为终点，F 表字段穿透上来）。

    核心原则：资产终点 = I 视图，加工终点 = F 表。
    字段以 I 为基准，F 表的 transform_type/类型/链路穿透合并进来，
    不做场景区分，线性穿透。
    """

    def test_view_inherited_fields_merged(self, mock_repo, tmp_path):
        """I 和 F 都有的字段：穿透合并，transform_type 取 F 表的。"""
        from analyzer import main
        from view_generator import build_report_data
        import sys as _sys

        f_group = mock_repo["f_group_dir"]
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(f_group), "--output", str(tmp_path / "out")]
        try:
            main()
        finally:
            _sys.argv = old_argv

        kj = json.loads((tmp_path / "out" / "DWB_TRADE_SUM_F" /
                         "knowledge_draft.json").read_text(encoding="utf-8"))
        report = build_report_data(kj)

        # total 字段：F 表是 aggregate（SUM），I 视图是 direct，穿透后应为 aggregate
        total_field = next((f for f in report["fields"]
                            if f["target_field"] == "total"), None)
        assert total_field, "应有 total 字段"
        assert total_field["transform_type"] == "aggregate", \
            f"穿透后 transform_type 应为 aggregate（F表的），实际 {total_field['transform_type']}"
        assert total_field.get("is_view_inherited"), "应标注穿透自F表"

        # 不应有两个 total 字段（去重合并）
        total_count = sum(1 for f in report["fields"] if f["target_field"] == "total")
        assert total_count == 1, f"total 应只有1个（合并），实际 {total_count}"

    def test_view_inherited_fields_final(self, mock_repo, tmp_path):
        """穿透后的字段 is_final_field=True（以 I 视图为终点）。"""
        from analyzer import main
        from view_generator import build_report_data
        import sys as _sys

        f_group = mock_repo["f_group_dir"]
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(f_group), "--output", str(tmp_path / "out")]
        try:
            main()
        finally:
            _sys.argv = old_argv

        kj = json.loads((tmp_path / "out" / "DWB_TRADE_SUM_F" /
                         "knowledge_draft.json").read_text(encoding="utf-8"))
        report = build_report_data(kj)

        for f in report["fields"]:
            if f.get("is_view_inherited"):
                assert f["is_final_field"], \
                    f"穿透字段 {f['target_field']} 应 is_final_field=True"

    def test_no_view_no_penetration(self, mock_repo, tmp_path):
        """无 I 视图时（excel 模式或 F 表无视图），不做穿透合并。"""
        from analyzer import main
        from view_generator import build_report_data
        import sys as _sys

        # DWB_TRADE_ORDER_D 不以 _f 结尾，无 I 视图发现
        group = mock_repo["group_dir"]
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group), "--output", str(tmp_path / "out")]
        try:
            main()
        finally:
            _sys.argv = old_argv

        kj = json.loads((tmp_path / "out" / "DWB_TRADE_ORDER_D" /
                         "knowledge_draft.json").read_text(encoding="utf-8"))
        report = build_report_data(kj)

        # 无 I 视图 → 无穿透标注
        for f in report["fields"]:
            assert not f.get("is_view_inherited"), \
                f"无 I 视图时不应有穿透标注: {f['target_field']}"
            assert not f.get("is_view_extra")
            assert not f.get("is_base_only")

