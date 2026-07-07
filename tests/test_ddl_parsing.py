"""DDL 解析鲁棒性测试（parse_ddl_for_metadata）。

覆盖真实 GaussDB DDL 的各种复杂写法：
- WITH 子句（orientation/compression）
- DISTRIBUTE BY
- PARTITION BY（含分区定义的嵌套括号）
- IF NOT EXISTS
- 行内注释 / COMMENT ON COLUMN
- 多空格/tab/换行容错
- schema 前缀
- PRIMARY KEY / CONSTRAINT 等非字段行跳过

运行:
    pytest tests/test_ddl_parsing.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from engine import parse_ddl_for_metadata, _extract_create_table_body


def _write_ddl(tmp_path, table_name, ddl_content):
    """写一个 DDL 文件到临时目录，返回目录路径。"""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir(exist_ok=True)
    (ddl_dir / f"{table_name}.sql").write_text(ddl_content, encoding="utf-8")
    return str(ddl_dir)


class TestExtractCreateTableBody:
    """_extract_create_table_body 括号配平提取。"""

    def test_simple(self):
        body = _extract_create_table_body("CREATE TABLE t (a INT, b VARCHAR(64))")
        assert "a INT" in body
        assert "b VARCHAR(64)" in body

    def test_with_clause_not_included(self):
        """WITH 子句不应进入 body。"""
        ddl = "CREATE TABLE t (a INT) WITH (orientation=column, compression=high)"
        body = _extract_create_table_body(ddl)
        assert "orientation" not in body
        assert "compression" not in body
        assert "a INT" in body

    def test_distribute_by_not_included(self):
        """DISTRIBUTE BY 不应进入 body。"""
        ddl = "CREATE TABLE t (a INT) DISTRIBUTE BY HASH (a)"
        body = _extract_create_table_body(ddl)
        assert "DISTRIBUTE" not in body.upper()
        assert "a INT" in body

    def test_partition_by_not_included(self):
        """PARTITION BY（含嵌套括号）不应进入 body。"""
        ddl = ("CREATE TABLE t (a INT, b DATE) "
               "PARTITION BY RANGE (b) (\n"
               "  PARTITION p1 VALUES LESS THAN ('20240101'),\n"
               "  PARTITION p2 VALUES LESS THAN ('20240201')\n"
               ")")
        body = _extract_create_table_body(ddl)
        assert "PARTITION" not in body.upper()
        assert "a INT" in body
        assert "b DATE" in body

    def test_field_type_with_paren_not_broken(self):
        """字段类型含括号（DECIMAL(18,2)）不应被误判为表定义闭合。"""
        ddl = "CREATE TABLE t (amount DECIMAL(18,2), name VARCHAR(64))"
        body = _extract_create_table_body(ddl)
        assert "amount DECIMAL(18,2)" in body
        assert "name VARCHAR(64)" in body

    def test_if_not_exists(self):
        ddl = "CREATE TABLE IF NOT EXISTS t (a INT)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body

    def test_schema_prefix(self):
        ddl = "CREATE TABLE dws.dwb_t (a INT)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body

    def test_multiline_and_tabs(self):
        """多行/tab/换行容错。"""
        ddl = "CREATE\tTABLE\nt (\n\ta INT,\n\tb VARCHAR(64)\n)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body


class TestParseDdlGaussDb:
    """真实 GaussDB DDL 解析。"""

    def test_full_gaussdb_ddl(self, tmp_path):
        """完整 GaussDB DDL（WITH + DISTRIBUTE + PARTITION）能正确解析字段。"""
        ddl = """CREATE TABLE dws.dwb_trade_order_d (
  order_id VARCHAR(64) NOT NULL,
  cust_id VARCHAR(64),
  total_amount DECIMAL(18,2),
  order_time TIMESTAMP,
  PRIMARY KEY (order_id)
) WITH (orientation=column, compression=high)
DISTRIBUTE BY HASH (order_id)
PARTITION BY RANGE (order_time) (
  PARTITION p202401 VALUES LESS THAN ('20240201')
);
COMMENT ON COLUMN dws.dwb_trade_order_d.order_id IS '订单ID';
COMMENT ON COLUMN dws.dwb_trade_order_d.cust_id IS '客户ID';
COMMENT ON COLUMN dws.dwb_trade_order_d.total_amount IS '订单总额';
COMMENT ON TABLE dws.dwb_trade_order_d IS '订单汇总';"""
        ddl_dir = _write_ddl(tmp_path, "dwb_trade_order_d", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "dwb_trade_order_d")

        # 字段类型正确（不被 WITH/PARTITION 干扰）
        assert "order_id" in meta
        assert "VARCHAR" in meta["order_id"]["type"]
        assert "total_amount" in meta
        assert "DECIMAL(18,2)" in meta["total_amount"]["type"], \
            f"类型应含精度，实际 {meta['total_amount']['type']}"
        assert "order_time" in meta
        assert "TIMESTAMP" in meta["order_time"]["type"]

        # COMMENT ON COLUMN 注释正确
        assert meta["order_id"]["comment"] == "订单ID"
        assert meta["total_amount"]["comment"] == "订单总额"

        # PRIMARY KEY 不应被当成字段
        assert "primary" not in meta

    def test_inline_comment(self, tmp_path):
        """行内注释 /* 中文名 */ 能解析。"""
        ddl = """CREATE TABLE t (
  order_id VARCHAR(64) /* 订单编号 */,
  amount DECIMAL(18,2) /* 金额 */
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["order_id"]["comment"] == "订单编号"
        assert meta["amount"]["comment"] == "金额"

    def test_comment_on_column_overrides_inline(self, tmp_path):
        """COMMENT ON COLUMN 覆盖行内注释。"""
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2) /* 旧行内注释 */
);
COMMENT ON COLUMN t.amount IS '新注释';"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "新注释"

    def test_comment_double_quotes(self, tmp_path):
        """防回归：COMMENT ON COLUMN 支持双引号（真实 DDL 导出工具常用）。

        历史 bug：正则只支持单引号 '([^']*)'，双引号 COMMENT 解析不到注释，
        导致 HTML 报告/mapping 里字段业务含义为空。
        """
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2)
);
COMMENT ON COLUMN t.amount IS "金额";"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "金额", \
            f"双引号 COMMENT 应解析，实际 {meta['amount']['comment']!r}"

    def test_inline_dash_comment(self, tmp_path):
        """防回归：行内 -- 注释能解析。"""
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2) -- 金额
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "金额", \
            f"行内 -- 注释应解析，实际 {meta['amount']['comment']!r}"

    def test_default_with_dash_not_misinterpreted(self, tmp_path):
        """防回归：DEFAULT 值里的 -- 不被误匹配为注释。

        历史 bug：行内 -- 注释正则太宽泛，DEFAULT 'http://--test' 里的 --
        被当成注释，导致 default_value 丢失 + comment 污染。
        """
        ddl = """CREATE TABLE t (
  url VARCHAR(256) DEFAULT 'http://--test',
  amount DECIMAL(18,2) -- 金额
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        # url 不应有注释（DEFAULT 里的 -- 不应匹配）
        assert not meta["url"].get("comment"), \
            f"url 不应有注释，实际 {meta['url'].get('comment')!r}"
        # url 的 DEFAULT 值应保留
        assert meta["url"]["default_value"], "url 的 DEFAULT 值不应丢失"
        # amount 有注释
        assert meta["amount"]["comment"] == "金额"

    def test_constraint_skipped(self, tmp_path):
        """CONSTRAINT/UNIQUE/CHECK 行不被当成字段。"""
        ddl = """CREATE TABLE t (
  id VARCHAR(64),
  amount DECIMAL(18,2),
  CONSTRAINT pk_t PRIMARY KEY (id),
  UNIQUE (id),
  CHECK (amount >= 0)
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert "id" in meta
        assert "amount" in meta
        # 约束相关的不应被当成字段
        meta_keys = set(meta.keys())
        assert "constraint" not in meta_keys
        assert "check" not in meta_keys
        assert "unique" not in meta_keys

    def test_empty_ddl_dir(self, tmp_path):
        """空 DDL 目录返回空 dict。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert parse_ddl_for_metadata(str(empty), "t") == {}

    def test_no_ddl_dir(self, tmp_path):
        """不存在的路径返回空 dict。"""
        assert parse_ddl_for_metadata(str(tmp_path / "noexist"), "t") == {}

    def test_table_not_in_ddl_file(self, tmp_path):
        """DDL 文件里没有目标表，返回空。"""
        ddl = "CREATE TABLE other_table (a INT);"
        ddl_dir = _write_ddl(tmp_path, "other", ddl)
        assert parse_ddl_for_metadata(ddl_dir, "not_exist_table") == {}

    def test_multiple_ddl_files(self, tmp_path):
        """DDL 目录有多个文件，只解析含目标表的。"""
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "dwb_order_f.sql").write_text(
            "CREATE TABLE dwb_order_f (order_id INT);\n"
            "COMMENT ON COLUMN dwb_order_f.order_id IS '订单ID';",
            encoding="utf-8")
        (ddl_dir / "dwb_cust_f.sql").write_text(
            "CREATE TABLE dwb_cust_f (cust_id VARCHAR(64));\n"
            "COMMENT ON COLUMN dwb_cust_f.cust_id IS '客户ID';",
            encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "dwb_cust_f")
        assert "cust_id" in meta
        assert "order_id" not in meta
