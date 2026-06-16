"""
B类单点用例 — SQL 解析精度校验

每个测试只验证一个 SQL 构造的解析准确性，不走完整流程。
输入是纯 SQL 字符串，直接调 parse_single_sql + build_field_mappings。

运行:
    pytest tests/test_sql_parsing.py -v
    pytest tests/test_sql_parsing.py -k cte -v
"""

import sys
import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
ANALYZER_SKILL = PROJECT_ROOT / ".opencode" / "skills" / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_SKILL))

from analyzer import parse_single_sql, classify_transform


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def parse(sql: str, dialect: str = "dws"):
    """解析 SQL，返回 ParsedSQL"""
    return parse_single_sql(sql, dialect)

def col_aliases(parsed):
    """提取所有列的 alias 列表"""
    return [c.alias for c in parsed.select_columns]

def col_types(parsed):
    """提取 alias → transform_type 映射"""
    return {c.alias: c.transform_type for c in parsed.select_columns}

def source_tables(parsed):
    """提取来源表名列表（去重）"""
    seen = []
    for j in parsed.source_tables:
        if j.source_table not in seen:
            seen.append(j.source_table)
    return seen


# ═══════════════════════════════════════════════════════════════
# 1. CTE 穿透传播
# ═══════════════════════════════════════════════════════════════

class TestCTEPenetration:

    def test_cte_aggregate_penetrates_to_direct(self):
        """CTE 内 SUM 聚合，主查询直接引用 → 穿透后应为 aggregate"""
        sql = """WITH agg AS (
    SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id
)
SELECT u.user_name, agg.total
FROM dim_user u INNER JOIN agg ON u.user_id = agg.user_id"""
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["total"] == "aggregate", f"total 应穿透为 aggregate，实际 {types['total']}"
        assert types["user_name"] == "direct"

    def test_cte_pivot_does_not_penetrate_to_groupby_field(self):
        """CTE 内 GROUP BY 字段不应被穿透升级"""
        sql = """WITH agg AS (
    SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id
)
SELECT agg.user_id, agg.total FROM agg"""
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["user_id"] == "direct"
        assert types["total"] == "aggregate"

    def test_cte_nested_penetration(self):
        """嵌套 CTE: base → agg(引用base) → 主查询(引用agg)"""
        sql = """WITH base AS (
    SELECT user_id, amount FROM orders WHERE status = 'OK'
),
agg AS (
    SELECT user_id, SUM(amount) AS total FROM base GROUP BY user_id
)
SELECT u.user_name, agg.total
FROM dim_user u INNER JOIN agg ON u.user_id = agg.user_id"""
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["total"] == "aggregate", f"嵌套CTE穿透后 total 应为 aggregate，实际 {types['total']}"

    def test_cte_with_alias_reference(self):
        """CTE 用别名引用: JOIN inv_mtr_agg im_agg → im_agg.total"""
        sql = """WITH inv_mtr_agg AS (
    SELECT contract_no, SUM(amount) AS total FROM invoices GROUP BY contract_no
)
SELECT t.contract_no, im_agg.total
FROM main_table t LEFT JOIN inv_mtr_agg im_agg ON t.contract_no = im_agg.contract_no"""
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["total"] == "aggregate", f"CTE别名引用穿透后应为 aggregate，实际 {types['total']}"


# ═══════════════════════════════════════════════════════════════
# 2. UNION / INTERSECT / EXCEPT
# ═══════════════════════════════════════════════════════════════

class TestSetOperations:

    def test_union_all_source_tables_merged(self):
        """UNION ALL 两个分支的来源表都应被提取"""
        sql = """SELECT a.id, a.name FROM table_a a WHERE a.del = 'N'
UNION ALL
SELECT b.id, b.name FROM table_b b WHERE b.del = 'N'"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert "table_a" in tables, f"应包含 table_a，实际 {tables}"
        assert "table_b" in tables, f"应包含 table_b，实际 {tables}"

    def test_union_columns_from_first_branch(self):
        """UNION 字段以第一个分支为准"""
        sql = """SELECT a.id AS user_id, a.name FROM table_a a
UNION ALL
SELECT b.id AS user_id, b.name FROM table_b b"""
        parsed = parse(sql)
        aliases = col_aliases(parsed)
        assert "user_id" in aliases
        assert "name" in aliases

    def test_intersect_source_tables(self):
        """INTERSECT 两个分支来源表都提取"""
        sql = """SELECT a.id FROM table_a a
INTERSECT
SELECT b.id FROM table_b b"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert "table_a" in tables
        assert "table_b" in tables


# ═══════════════════════════════════════════════════════════════
# 3. 加工类型分类
# ═══════════════════════════════════════════════════════════════

class TestTransformClassification:

    def test_pivot_classification(self):
        """SUM(CASE WHEN...) 行转列识别为 pivot"""
        sql = """SELECT t.product_id,
    SUM(CASE WHEN t.month = '202401' THEN t.amount ELSE 0 END) AS jan_amt
FROM sales t GROUP BY t.product_id"""
        parsed = parse(sql)
        assert col_types(parsed)["jan_amt"] == "pivot"

    def test_window_classification(self):
        """ROW_NUMBER() OVER 识别为 window"""
        sql = """SELECT t.user_id,
    ROW_NUMBER() OVER (PARTITION BY t.user_id ORDER BY t.date DESC) AS rn
FROM logins t"""
        parsed = parse(sql)
        assert col_types(parsed)["rn"] == "window"

    def test_aggregate_classification(self):
        """SUM() 识别为 aggregate"""
        sql = """SELECT t.dept_id, SUM(t.salary) AS total_salary
FROM employees t GROUP BY t.dept_id"""
        parsed = parse(sql)
        assert col_types(parsed)["total_salary"] == "aggregate"

    def test_fallback_classification(self):
        """COALESCE 识别为 fallback"""
        sql = """SELECT t.id, COALESCE(t.name, 'UNKNOWN') AS name
FROM items t"""
        parsed = parse(sql)
        assert col_types(parsed)["name"] == "fallback"

    def test_value_classification(self):
        """字面量赋值识别为 value"""
        sql = """SELECT t.id, 'N' AS del_flag, CURRENT_TIMESTAMP AS update_time
FROM items t"""
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["del_flag"] == "value"
        assert types["update_time"] == "value"

    def test_direct_classification(self):
        """直接取字段识别为 direct"""
        sql = "SELECT t.id, t.name FROM users t"
        parsed = parse(sql)
        types = col_types(parsed)
        assert types["id"] == "direct"
        assert types["name"] == "direct"

    def test_nested_case_when(self):
        """嵌套 CASE WHEN 分类"""
        sql = """SELECT t.id,
    CASE WHEN t.type = 'A' THEN
        CASE WHEN t.sub = '1' THEN 'A1' ELSE 'A2' END
    ELSE 'B'
    END AS category
FROM items t"""
        parsed = parse(sql)
        tt = col_types(parsed)["category"]
        assert tt in ("case_when", "pivot"), f"嵌套CASE应为 case_when 或 pivot，实际 {tt}"


# ═══════════════════════════════════════════════════════════════
# 4. JOIN 提取
# ═══════════════════════════════════════════════════════════════

class TestJoinExtraction:

    def test_left_join(self):
        """LEFT JOIN 提取正确"""
        sql = """SELECT a.id, b.name
FROM table_a a LEFT JOIN table_b b ON a.id = b.aid"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert "table_a" in tables
        assert "table_b" in tables

    def test_cte_join_not_leaked(self):
        """CTE 内部的 JOIN 表不混入主查询来源表"""
        sql = """WITH cte AS (
    SELECT a.id FROM table_a a INNER JOIN table_b b ON a.id = b.aid
)
SELECT cte.id FROM cte"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        # 主查询只 FROM cte，不含 table_a/table_b
        assert "table_a" not in tables, f"CTE内部表 table_a 不应出现在主查询来源表中: {tables}"
        assert "table_b" not in tables

    def test_self_join(self):
        """自连接（同表两个别名）"""
        sql = """SELECT a.id, b.parent_id
FROM tree a INNER JOIN tree b ON a.id = b.parent_id"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        # 同表只出现一次
        assert tables.count("tree") == 1


# ═══════════════════════════════════════════════════════════════
# 5. DWS 语法清理
# ═══════════════════════════════════════════════════════════════

class TestDWSSyntax:

    def test_partition_syntax(self):
        """PARTITION(part_name) 语法不报错"""
        sql = """SELECT t.id, t.name FROM my_table PARTITION(part_a) t WHERE t.del = 'N'"""
        parsed = parse(sql)
        assert not parsed.parse_error
        assert "my_table" in source_tables(parsed)

    def test_distribute_by_cleanup(self):
        """DISTRIBUTE BY 语法不报错"""
        sql = """SELECT t.id FROM my_table t WHERE t.del = 'N'
DISTRIBUTE BY HASH(t.id)"""
        parsed = parse(sql)
        assert not parsed.parse_error

    def test_placeholder_replacement(self):
        """${P_CYCLE_ID} 占位符不报错"""
        sql = """SELECT t.id, '${P_CYCLE_ID}' AS cycle_id FROM my_table t"""
        parsed = parse(sql)
        assert not parsed.parse_error
        assert col_types(parsed)["cycle_id"] == "value"


# ═══════════════════════════════════════════════════════════════
# 6. 审计字段推断
# ═══════════════════════════════════════════════════════════════

class TestAuditFields:

    def test_del_flag_inference(self):
        """'N' AS del_flag 推断正确"""
        sql = "SELECT t.id, 'N' AS del_flag FROM items t"
        parsed = parse(sql)
        aliases = col_aliases(parsed)
        assert "del_flag" in aliases

    def test_current_timestamp_inference(self):
        """CURRENT_TIMESTAMP AS dw_last_update_date 推断"""
        sql = "SELECT t.id, CURRENT_TIMESTAMP AS dw_last_update_date FROM items t"
        parsed = parse(sql)
        aliases = col_aliases(parsed)
        assert "dw_last_update_date" in aliases

    def test_comment_alias_extraction(self):
        """/* field_name */ 注释别名提取"""
        sql = """SELECT
    t.id,
    'N',                        /* del_flag */
    CURRENT_TIMESTAMP           /* dw_last_update_date */
FROM items t"""
        parsed = parse(sql)
        aliases = col_aliases(parsed)
        assert "del_flag" in aliases, f"应从注释提取 del_flag，实际 {aliases}"
        assert "dw_last_update_date" in aliases
