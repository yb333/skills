"""多表 DDL catalog + 字段级类型下注测试（P1+P2）。

验证：
- build_table_catalog 能发现过程表+目标表的 DDL（多表）
- parse_ddl_for_metadata 扩展输出（nullable/default/is_pk）
- field_mappings.fields[] 每个字段带 field_type/field_comment（P2 字段级下注）
- 容错：DDL 找不到/部分表缺 DDL/DDL 解析失败 → 不阻塞分析

运行:
    pytest tests/test_table_catalog.py -v
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


class TestParseDdlExtendedFields:
    """parse_ddl_for_metadata 扩展输出（nullable/default/is_pk）。"""

    def test_nullable_detection(self, tmp_path):
        """NOT NULL 解析。"""
        from engine import parse_ddl_for_metadata
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "t.sql").write_text(
            "CREATE TABLE t (\n"
            "  a VARCHAR(64) NOT NULL,\n"
            "  b VARCHAR(64)\n"
            ");", encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "t")
        assert meta["a"]["nullable"] is False
        assert meta["b"]["nullable"] is True

    def test_default_value(self, tmp_path):
        """DEFAULT 值解析。"""
        from engine import parse_ddl_for_metadata
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "t.sql").write_text(
            "CREATE TABLE t (\n"
            "  status VARCHAR(10) DEFAULT 'ACTIVE',\n"
            "  count INT DEFAULT 0\n"
            ");", encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "t")
        assert meta["status"]["default_value"] == "ACTIVE"
        assert meta["count"]["default_value"] == "0"

    def test_primary_key(self, tmp_path):
        """PRIMARY KEY 字段标记。"""
        from engine import parse_ddl_for_metadata
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "t.sql").write_text(
            "CREATE TABLE t (\n"
            "  id VARCHAR(64) NOT NULL,\n"
            "  name VARCHAR(64),\n"
            "  PRIMARY KEY (id)\n"
            ");", encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "t")
        assert meta["id"]["is_pk"] is True
        assert meta["name"]["is_pk"] is False

    def test_backwards_compatible_type_comment(self, tmp_path):
        """扩展后仍保留 type/comment（向后兼容）。"""
        from engine import parse_ddl_for_metadata
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "t.sql").write_text(
            "CREATE TABLE t (\n  amount DECIMAL(18,2) NOT NULL\n);\n"
            "COMMENT ON COLUMN t.amount IS '金额';", encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "t")
        assert meta["amount"]["type"] == "DECIMAL(18,2)"
        assert meta["amount"]["comment"] == "金额"
        assert meta["amount"]["nullable"] is False


class TestBuildTableCatalog:
    """build_table_catalog 多表发现（过程表+目标表）。"""

    def test_catalog_has_both_tables(self, mock_repo_data):
        """catalog 同时含过程表和目标表。"""
        from engine import build_table_catalog
        ddl_dir = mock_repo_data["ddl_dir"]
        rules = mock_repo_data["raw"]["rules"]
        catalog = build_table_catalog(rules, str(ddl_dir))

        # 应含过程表 + 目标表
        assert "dws.tmp_trade_order" in catalog, "过程表应在 catalog 里"
        assert "dws.dwb_trade_order_d" in catalog, "目标表应在 catalog 里"

    def test_catalog_empty_when_no_ddl_dir(self, mock_repo_data):
        """ddl_dir 为空 → catalog 为空（容错）。"""
        from engine import build_table_catalog
        catalog = build_table_catalog(mock_repo_data["raw"]["rules"], "")
        assert catalog == {}

    def test_catalog_empty_when_dir_not_exist(self, mock_repo_data):
        """ddl_dir 不存在 → catalog 为空（容错）。"""
        from engine import build_table_catalog
        catalog = build_table_catalog(mock_repo_data["raw"]["rules"], "/nonexistent/path")
        assert catalog == {}

    def test_catalog_partial_when_some_table_missing_ddl(self, tmp_path):
        """部分表没有 DDL → 其他表照常进 catalog（容错）。"""
        from engine import build_table_catalog
        from engine import RawRule
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        # 只有 table_a 的 DDL，table_b 没有
        (ddl_dir / "table_a.sql").write_text(
            "CREATE TABLE table_a (x INT);", encoding="utf-8")

        rules = [
            RawRule(rule_code="R1", target_schema="dws", target_table="table_a", rule_type=1),
            RawRule(rule_code="R2", target_schema="dws", target_table="table_b", rule_type=1),  # 无DDL
        ]
        catalog = build_table_catalog(rules, str(ddl_dir))
        assert "dws.table_a" in catalog
        assert "dws.table_b" not in catalog  # 没DDL的不在
        # table_a 的字段照常有
        assert "x" in catalog["dws.table_a"]


class TestFieldLevelTypeInjection:
    """P2：field_mappings.fields[] 字段级类型/注释下注。"""

    def test_fields_have_type_and_comment(self, mock_repo_data):
        """每个目标字段带 field_type 和 field_comment（有 DDL 时）。"""
        from engine import analyze_pipeline, detect_dialect
        raw = mock_repo_data["raw"]
        ddl_dir = str(mock_repo_data["ddl_dir"])
        dialect = detect_dialect([r.query_sql for r in raw["rules"] if r.query_sql])

        kj, _ = analyze_pipeline(raw["rules"], raw["target_fields"],
                                  raw["group_variables"], dialect, ddl_dir=ddl_dir)

        # 目标表字段应有类型+注释
        fields = kj["field_mappings"]["fields"]
        target_fields = [f for f in fields
                         if f.get("producing_step") == "step_2"]  # step_2 写目标表
        assert target_fields, "应有 step_2 的字段"
        for f in target_fields:
            if f.get("target_field") in ("order_id", "total_amount"):
                assert f.get("field_type"), f"{f['target_field']} 应有 field_type"
                assert f.get("field_comment"), f"{f['target_field']} 应有 field_comment"

    def test_fields_no_type_when_ddl_missing(self, mock_repo_data):
        """没有 DDL 时，字段无 field_type（容错，不报错）。"""
        from engine import analyze_pipeline, detect_dialect
        raw = mock_repo_data["raw"]
        dialect = detect_dialect([r.query_sql for r in raw["rules"] if r.query_sql])

        # 不传 ddl_dir
        kj, _ = analyze_pipeline(raw["rules"], raw["target_fields"],
                                  raw["group_variables"], dialect, ddl_dir="")

        fields = kj["field_mappings"]["fields"]
        # 没有 DDL，字段不应有 field_type（或为空）
        for f in fields:
            assert not f.get("field_type"), "无 DDL 时不应有 field_type"

    def test_intermediate_table_fields_have_type(self, mock_repo_data):
        """过程表（中间表）的字段也有类型下注。"""
        from engine import analyze_pipeline, detect_dialect
        raw = mock_repo_data["raw"]
        ddl_dir = str(mock_repo_data["ddl_dir"])
        dialect = detect_dialect([r.query_sql for r in raw["rules"] if r.query_sql])

        kj, _ = analyze_pipeline(raw["rules"], raw["target_fields"],
                                  raw["group_variables"], dialect, ddl_dir=ddl_dir)

        # step_1 写过程表 tmp_trade_order，其字段应有类型
        fields = kj["field_mappings"]["fields"]
        step1_fields = [f for f in fields if f.get("producing_step") == "step_1"]
        # tmp_trade_order 的 DDL 有 order_id
        order_id_field = next((f for f in step1_fields if f.get("target_field") == "order_id"), None)
        if order_id_field:
            assert order_id_field.get("field_type"), "过程表字段 order_id 应有类型"


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def mock_repo_data(tmp_path):
    """构造模拟代码仓，返回关键数据和路径。"""
    from analyzer import read_yml, _auto_discover_ddl_from_repo
    info = build_mock_repo(tmp_path / "repo")
    raw = read_yml(str(info["group_dir"]))
    ddl_dir = _auto_discover_ddl_from_repo(info["group_dir"], raw["rules"])
    return {
        "raw": raw,
        "ddl_dir": Path(ddl_dir) if ddl_dir else None,
        "repo_info": info,
    }
