"""read_yml 测试：代码仓 yml 加载（与 read_excel 产出一致性）。

验证 read_yml 能正确解析代码仓 yml 格式，产出和 read_excel 完全一致的数据结构。

运行:
    pytest tests/test_read_yml.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_xlsx import build_xlsx
from _build_yml import build_yml_group


def _make_rules(num_steps=2):
    """构造标准测试规则（英文 key，同时兼容 _build_xlsx 和 _build_yml）。"""
    rules = [
        {"rule_code": "R0001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "tmp_test", "delete_mode": "1",
         "query_sql": "SELECT a.id, a.amount FROM ods.src_test a WHERE a.del='N'",
         "rule_name": "源头", "rule_group_code": "GR_TEST", "rule_group_en": "DWB_TEST_F"},
        {"rule_code": "R0002", "rule_type": 1, "exec_sequence": 2,
         "target_schema": "dws", "target_table": "dwb_test_f", "delete_mode": "1",
         "query_sql": "SELECT t.id, SUM(t.amount) AS total FROM dws.tmp_test t GROUP BY t.id",
         "rule_name": "汇总", "rule_group_code": "GR_TEST", "rule_group_en": "DWB_TEST_F"},
    ]
    return rules[:num_steps]


class TestReadYml:
    """read_yml 基本解析。"""

    def test_read_yml_parses_rules(self, tmp_path):
        """read_yml 能解析规则组目录下的 yml 文件为 RawRule 列表。"""
        from analyzer import read_yml
        group_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(group_dir, rules=_make_rules(2))

        raw = read_yml(str(group_dir))

        assert len(raw["rules"]) == 2, f"应解析 2 条规则，实际 {len(raw['rules'])}"
        r1 = raw["rules"][0]
        assert r1.rule_code == "R0001"
        assert r1.rule_type == 1
        assert r1.exec_sequence == 1
        assert r1.target_table == "tmp_test"
        assert "SELECT" in r1.query_sql.upper()

    def test_read_yml_group_info(self, tmp_path):
        """read_yml 能提取规则组编码和英文名。"""
        from analyzer import read_yml
        group_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(group_dir, rules=_make_rules(2))

        raw = read_yml(str(group_dir))

        assert raw["rule_group_code"] == "GR_TEST"
        assert raw["rule_group_en"] == "DWB_TEST_F"

    def test_read_yml_type_conversion(self, tmp_path):
        """yml 里的字符串数字（'1'/'2'）应转为 int（和 read_excel 一致）。"""
        from analyzer import read_yml
        group_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(group_dir, rules=_make_rules(2))

        raw = read_yml(str(group_dir))

        for rule in raw["rules"]:
            assert isinstance(rule.rule_type, int), f"rule_type 应是 int，实际 {type(rule.rule_type)}"
            assert isinstance(rule.exec_sequence, int), f"exec_sequence 应是 int"

    def test_read_yml_target_fields(self, tmp_path):
        """read_yml 能解析额外信息里的 TargetFields。"""
        from analyzer import read_yml
        rules = _make_rules(1)
        rules[0]["target_fields"] = [
            {"rule_code": "R0001", "target_field": "id", "source_field": "a.id",
             "field_type": "VARCHAR(64)", "remark": "ID"},
            {"rule_code": "R0001", "target_field": "amount", "source_field": "a.amount",
             "field_type": "DECIMAL(18,2)", "remark": "金额"},
        ]
        group_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(group_dir, rules=rules)

        raw = read_yml(str(group_dir))

        tfs = raw["target_fields"].get("R0001", [])
        assert len(tfs) == 2, f"应解析 2 个 TargetFields，实际 {len(tfs)}"
        assert tfs[0].target_field == "id"
        assert tfs[1].field_type == "DECIMAL(18,2)"

    def test_read_yml_group_variables(self, tmp_path):
        """read_yml 能解析额外信息里的 GroupVariables。"""
        from analyzer import read_yml
        rules = _make_rules(1)
        rules[0]["group_variables"] = [
            {"rule_code": "R0001", "var_name": "p_cycle_id", "default_value": "20240101"},
        ]
        group_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(group_dir, rules=rules)

        raw = read_yml(str(group_dir))

        gvs = raw["group_variables"].get("R0001", [])
        assert len(gvs) == 1
        assert gvs[0].var_name == "p_cycle_id"
        assert "p_cycle_id" in raw["variables"]

    def test_read_yml_empty_dir(self, tmp_path):
        """空目录应返回空结果，不崩溃。"""
        from analyzer import read_yml
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        raw = read_yml(str(empty_dir))
        assert raw["rules"] == []
        assert raw["target_fields"] == {}


class TestReadYmlMatchesReadExcel:
    """防回归：同一份数据，read_yml 和 read_excel 产出一致。"""

    def test_same_data_same_output(self, tmp_path):
        """同一份规则数据，分别用 xlsx 和 yml 加载，产出结构应一致。"""
        from analyzer import read_excel, read_yml
        rules = _make_rules(2)
        # TargetFields 单独准备（xlsx 用 target_fields 参数，yml 嵌在 rule 里）
        tfs = [
            {"rule_code": "R0001", "target_field": "id", "source_field": "a.id",
             "field_type": "VARCHAR(64)"},
        ]
        rules_with_tf = [dict(r) for r in rules]
        rules_with_tf[0]["target_fields"] = tfs

        # xlsx（target_fields 作为独立参数）
        xlsx_path = tmp_path / "test.xlsx"
        build_xlsx(str(xlsx_path), rules=rules, target_fields=tfs)
        raw_xlsx = read_excel(str(xlsx_path))

        # yml（target_fields 嵌在 rule 的额外信息里）
        yml_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(yml_dir, rules=rules_with_tf)
        raw_yml = read_yml(str(yml_dir))

        # 规则数一致
        assert len(raw_xlsx["rules"]) == len(raw_yml["rules"])
        # 逐规则字段一致
        for rx, ry in zip(raw_xlsx["rules"], raw_yml["rules"]):
            assert rx.rule_code == ry.rule_code
            assert rx.rule_type == ry.rule_type
            assert rx.exec_sequence == ry.exec_sequence
            assert rx.target_table == ry.target_table
            assert rx.query_sql.strip() == ry.query_sql.strip()
            assert rx.rule_group_code == ry.rule_group_code
            assert rx.rule_group_en == ry.rule_group_en
        # 规则组信息一致
        assert raw_xlsx["rule_group_code"] == raw_yml["rule_group_code"]
        assert raw_xlsx["rule_group_en"] == raw_yml["rule_group_en"]
        # TargetFields 一致
        tf_x = raw_xlsx["target_fields"].get("R0001", [])
        tf_y = raw_yml["target_fields"].get("R0001", [])
        assert len(tf_x) == len(tf_y)
        if tf_x:
            assert tf_x[0].target_field == tf_y[0].target_field
            assert tf_x[0].field_type == tf_y[0].field_type

    def test_yml_and_excel_same_engine_output(self, tmp_path):
        """终极验证：同一份数据分别走 xlsx 和 yml，analyze_pipeline 产出结构一致。"""
        from analyzer import read_excel, read_yml
        from engine import analyze_pipeline, detect_dialect

        rules = _make_rules(2)
        tfs = [
            {"rule_code": "R0001", "target_field": "id", "source_field": "a.id"},
            {"rule_code": "R0001", "target_field": "amount", "source_field": "a.amount"},
        ]
        rules_with_tf = [dict(r) for r in rules]
        rules_with_tf[0]["target_fields"] = tfs

        # xlsx 路径
        xlsx_path = tmp_path / "test.xlsx"
        build_xlsx(str(xlsx_path), rules=rules, target_fields=tfs)
        raw_x = read_excel(str(xlsx_path))
        sqls = [r.query_sql for r in raw_x["rules"] if r.query_sql]
        dialect = detect_dialect(sqls)
        kj_x, _ = analyze_pipeline(raw_x["rules"], raw_x["target_fields"],
                                    raw_x["group_variables"], dialect)

        # yml 路径
        yml_dir = tmp_path / "DWB_TEST_F"
        build_yml_group(yml_dir, rules=rules_with_tf)
        raw_y = read_yml(str(yml_dir))
        kj_y, _ = analyze_pipeline(raw_y["rules"], raw_y["target_fields"],
                                    raw_y["group_variables"], dialect)

        # 顶层结构一致
        assert set(kj_x.keys()) == set(kj_y.keys())
        # meta 的 target_table 一致
        assert kj_x["meta"]["target_table"] == kj_y["meta"]["target_table"]
        # 步骤数一致
        assert len(kj_x["topology"]["steps"]) == len(kj_y["topology"]["steps"])
        # 字段映射数一致
        assert len(kj_x["field_mappings"]["fields"]) == len(kj_y["field_mappings"]["fields"])


class TestDdlDiscovery:
    """DDL 自动发现（yml 场景从代码仓根定位 DDL）。"""

    def test_find_repo_root(self, tmp_path):
        """能从规则组目录向上找到代码仓根（含 BFT/ + DDL/）。"""
        from analyzer import _find_repo_root

        # 模拟代码仓结构
        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        (repo / "DDL").mkdir()
        group_dir = repo / "BFT" / "BftWideTable" / "P" / "S" / "DWB_TEST_F"
        group_dir.mkdir(parents=True)

        root = _find_repo_root(group_dir)
        assert root == repo.resolve()

    def test_find_repo_root_not_found(self, tmp_path):
        """无 BFT/ + DDL/ 的目录树返回 None。"""
        from analyzer import _find_repo_root
        d = tmp_path / "no_repo" / "sub"
        d.mkdir(parents=True)
        assert _find_repo_root(d) is None

    def test_auto_discover_ddl(self, tmp_path):
        """能从代码仓结构定位目标表的 DDL 目录。"""
        from analyzer import _auto_discover_ddl_from_repo
        from engine import RawRule

        # 模拟代码仓结构
        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        # DDL 目录结构：DDL/DWS_EDW/dws/table/dwb_test_f.sql
        ddl_table_dir = repo / "DDL" / "DWS_EDW" / "dws" / "table"
        ddl_table_dir.mkdir(parents=True)
        (ddl_table_dir / "dwb_test_f.sql").write_text("-- DDL", encoding="utf-8")

        group_dir = repo / "BFT" / "BftWideTable" / "P" / "S" / "DWB_TEST_F"
        group_dir.mkdir(parents=True)

        rules = [RawRule(target_schema="dws", target_table="dwb_test_f", rule_type=1)]
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, rules)
        assert ddl_dir != "", "应找到 DDL 目录"
        assert "dwb_test_f.sql" in str(Path(ddl_dir) / "dwb_test_f.sql")

    def test_auto_discover_ddl_not_found(self, tmp_path):
        """DDL 不存在时返回空字符串，不崩溃。"""
        from analyzer import _auto_discover_ddl_from_repo
        from engine import RawRule

        repo = tmp_path / "repo"
        (repo / "BFT").mkdir(parents=True)
        (repo / "DDL").mkdir()
        group_dir = repo / "BFT" / "P" / "DWB_TEST_F"
        group_dir.mkdir(parents=True)

        rules = [RawRule(target_schema="dws", target_table="not_exist", rule_type=1)]
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, rules)
        assert ddl_dir == ""
