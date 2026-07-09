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

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # skills 仓库根
ANALYZER_SKILL = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
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

    def test_from_subquery(self):
        """FROM 子查询：内部表应被提取"""
        sql = """SELECT t.x FROM (
    SELECT a.x FROM tbl_a a LEFT JOIN tbl_b b ON a.id = b.id
) t LEFT JOIN dim_proj f ON t.id = f.id"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert any("tbl_a" in t for t in tables), f"子查询内部 tbl_a 应被提取，实际 {tables}"
        assert any("tbl_b" in t for t in tables), f"子查询内部 tbl_b 应被提取，实际 {tables}"
        assert any("dim_proj" in t for t in tables), f"外部 JOIN 表应被提取，实际 {tables}"

    def test_join_subquery(self):
        """JOIN 子查询：内部表应被提取"""
        sql = """SELECT t.x FROM main_tbl t
LEFT JOIN (SELECT b.id FROM b_table b) sub ON t.id = sub.id"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert any("main_tbl" in t for t in tables), f"主表应被提取，实际 {tables}"
        assert any("b_table" in t for t in tables), f"JOIN子查询内部 b_table 应被提取，实际 {tables}"

    def test_nested_subquery(self):
        """嵌套子查询：最深层的表应被提取"""
        sql = """SELECT t.x FROM (
    SELECT a.x FROM (SELECT c.x FROM c_table c) a LEFT JOIN b_table b ON a.x = b.x
) t"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert any("c_table" in t for t in tables), f"最内层 c_table 应被提取，实际 {tables}"
        assert any("b_table" in t for t in tables), f"中间层 b_table 应被提取，实际 {tables}"


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
        """${P_CYCLE_ID} 占位符不报错，且保留变量名"""
        sql = """SELECT t.id, '${P_CYCLE_ID}' AS cycle_id FROM my_table t"""
        parsed = parse(sql)
        assert not parsed.parse_error
        assert col_types(parsed)["cycle_id"] == "value"
        # 表达式应保留原始变量名
        expr = parsed.select_columns[1].expression
        assert "P_CYCLE_ID" in expr, f"表达式应保留 P_CYCLE_ID，实际 {expr}"


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


# ═══════════════════════════════════════════════════════════════
# 7. 字段使用信息（JOIN ON / WHERE / GROUP BY）
# ═══════════════════════════════════════════════════════════════

class TestFieldUsage:

    def test_join_usage_extraction(self):
        """JOIN ON 条件里的字段被提取为关联键"""
        sql = "SELECT t.x, f.z FROM main_tbl t LEFT JOIN dim_proj f ON t.proj_id = f.proj_id"
        parsed = parse(sql)
        join_fields = [j["field"] for j in parsed.join_usage]
        assert "proj_id" in join_fields, f"proj_id 应在 join_usage 里，实际 {join_fields}"

    def test_join_usage_has_on_condition(self):
        """join_usage 包含完整 ON 条件"""
        sql = "SELECT t.x FROM main_tbl t LEFT JOIN dim_proj f ON t.proj_id = f.proj_id AND f.del_flag = 'N'"
        parsed = parse(sql)
        assert len(parsed.join_usage) > 0
        on_cond = parsed.join_usage[0]["on_condition"]
        assert "proj_id" in on_cond, f"ON 条件应含 proj_id"
        assert "del_flag" in on_cond, f"ON 条件应含 del_flag（附加限制）"

    def test_join_usage_has_table_mapping(self):
        """join_usage 包含别名→物理表映射"""
        sql = "SELECT t.x FROM main_tbl t LEFT JOIN dim_proj f ON t.id = f.id"
        parsed = parse(sql)
        tables = parsed.join_usage[0]["tables"]
        aliases = [t["alias"] for t in tables]
        assert "t" in aliases, f"主表别名 t 应在 tables 里"
        assert "f" in aliases, f"JOIN 表别名 f 应在 tables 里"

    def test_where_usage_extraction(self):
        """WHERE 条件里的字段被提取"""
        sql = "SELECT t.x FROM main_tbl t WHERE t.status = 'A' AND t.del_flag = 'N'"
        parsed = parse(sql)
        where_fields = [w["field"] for w in parsed.where_usage]
        assert "status" in where_fields, f"status 应在 where_usage 里"
        assert "del_flag" in where_fields, f"del_flag 应在 where_usage 里"

    def test_groupby_usage_extraction(self):
        """GROUP BY 里的字段被提取"""
        sql = "SELECT t.x, SUM(t.y) FROM main_tbl t GROUP BY t.x"
        parsed = parse(sql)
        groupby_fields = [g["field"] for g in parsed.groupby_usage]
        assert "x" in groupby_fields, f"x 应在 groupby_usage 里"

    def test_field_all_roles(self):
        """一个字段同时有多个角色"""
        sql = """SELECT t.contract_no, f.proj_name
FROM main_tbl t
LEFT JOIN dim_proj f ON t.contract_no = f.contract_key AND f.del_flag = 'N'
WHERE t.contract_no IS NOT NULL
GROUP BY t.contract_no, f.proj_name"""
        parsed = parse(sql)
        join_fields = [j["field"] for j in parsed.join_usage]
        where_fields = [w["field"] for w in parsed.where_usage]
        groupby_fields = [g["field"] for g in parsed.groupby_usage]
        assert "contract_no" in join_fields, "contract_no 应在 join_usage"
        assert "contract_no" in where_fields, "contract_no 应在 where_usage"
        assert "contract_no" in groupby_fields, "contract_no 应在 groupby_usage"

    def test_auxiliary_field_not_in_select(self):
        """仅用作关联键的字段不在 SELECT 里（辅助字段）"""
        sql = "SELECT t.x FROM main_tbl t LEFT JOIN dim_proj f ON t.proj_id = f.proj_id"
        parsed = parse(sql)
        select_aliases = [c.alias for c in parsed.select_columns]
        assert "proj_id" not in select_aliases, "proj_id 不应在 SELECT 里"
        join_fields = [j["field"] for j in parsed.join_usage]
        assert "proj_id" in join_fields, "proj_id 应在 join_usage（辅助字段）"


# ═══════════════════════════════════════════════════════════════
# 8. 解析健壮性 —— 异常 SQL 不应让整个分析崩溃
# ═══════════════════════════════════════════════════════════════

class TestSqlParsingRobustness:
    """parse_single_sql 必须兜底所有异常，绝不向上抛。

    生产环境的制品包 SQL 质量不可控，一条格式异常的 SQL 若逃逸异常，
    会让 analyzer.py 主循环中断，整个制品包分析失败。
    正确行为：返回带 parse_error 的 ParsedSQL，让该规则标记为解析失败，
    其余规则继续分析。
    """

    def test_garbage_sql_does_not_raise(self):
        """完全无法解析的垃圾文本：记录错误，不抛异常"""
        parsed = parse("这不是SQL (((((( ")
        # 不抛异常即通过；必须记录了 parse_error
        assert parsed.parse_error is not None, "垃圾 SQL 应记录 parse_error"
        # 但 select_columns 应为空（无任何有效解析结果）
        assert len(parsed.select_columns) == 0

    def test_incomplete_sql_does_not_raise(self):
        """不完整的 SQL（只有半个语句）：不抛异常"""
        parsed = parse("SELECT FROM WHERE")
        assert parsed.parse_error is not None
        assert len(parsed.select_columns) == 0

    def test_deeply_nested_expression_does_not_raise(self):
        """深度嵌套表达式：可能触发 RecursionError，必须兜底，不抛异常"""
        # 构造深度嵌套的 CASE WHEN / 算术表达式
        inner = "1"
        for _ in range(60):
            inner = f"CASE WHEN 1=1 THEN {inner} ELSE 0 END"
        sql = f"SELECT {inner} AS deep_col FROM t"
        # 关键断言：调用本身不抛（无论是否解析成功）
        parsed = parse(sql)
        # 不抛异常即通过

    def test_oversized_chained_expression_does_not_raise(self):
        """超长链式表达式：同样可能触发递归，必须兜底"""
        # 构造超长链式 OR/AND 表达式
        conditions = " OR ".join([f"a.col{i} = 1" for i in range(200)])
        sql = f"SELECT * FROM a WHERE {conditions}"
        parsed = parse(sql)
        # 不抛异常即通过

    def test_null_byte_sql_does_not_raise(self):
        """含空字节等特殊字符的 SQL：不抛异常"""
        sql = "SELECT t.id\x00 FROM t"
        parsed = parse(sql)
        # 不抛异常即通过（可能成功也可能 parse_error）

    def test_parse_error_result_has_raw_sql(self):
        """解析失败时，ParsedSQL 应保留原始 SQL 便于排查"""
        raw = "INVALID GARBAGE"
        parsed = parse(raw)
        assert parsed.parse_error is not None
        assert parsed.raw_sql == raw, "失败时应保留原始 SQL"


# ═══════════════════════════════════════════════════════════════
# 9. CTE 引用顺序（CTE_A 引用后定义的 CTE_B）
# ═══════════════════════════════════════════════════════════════

class TestCTEOrdering:
    """CTE 互相引用时，被引用的 CTE 名不应被当成物理源表。

    Bug: cte_source_map 边遍历边收集 CTE 名，当 cte_a 引用后定义的 cte_b 时，
    cte_b 还没进集合，被当成物理表塞进 data_flow.tables。
    """

    def test_cte_reference_not_treated_as_physical(self):
        """CTE_A 引用 CTE_B，data_flow.tables 不应出现 cte_b"""
        sql = """WITH cte_b AS (SELECT id FROM ods.t2),
cte_a AS (SELECT id FROM cte_b)
SELECT a.id FROM cte_a a"""
        from analyzer import build_data_flow, RawRule
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=0,
                       target_schema="dws", target_table="t_f", delete_mode="1", query_sql=sql)
        df = build_data_flow([rule], {"R1": parse_single_sql(sql, "dws")})
        table_names = {t["name"].lower() for t in df["tables"]}
        # cte_b 不应作为物理表出现（它是 CTE，不是物理源表）
        assert "cte_b" not in table_names, \
            f"cte_b 是 CTE 名不应出现在 tables，实际 {table_names}"
        # 真正的物理源表 t2 应在
        assert "t2" in table_names


# ═══════════════════════════════════════════════════════════════
# 注释含分号（回归测试：注释里的分号不能截断 SQL）
# ═══════════════════════════════════════════════════════════════

class TestCommentSemicolon:
    """注释（-- 行注释 / /* */ 块注释）里包含分号时，不能导致 SQL 被错误分割。

    背景：parse_single_sql 在 split(";") 前会先剔注释。
    如果忘了剔注释，注释里的分号会把 SQL 截断，JOIN 表名全部丢失，
    最终导致影响分析静默失效（全部未命中）。
    """

    def test_line_comment_with_semicolon(self):
        """行注释含分号：JOIN 表不能丢失。"""
        sql = """SELECT a.user_id, a.amount, b.product_name
FROM ods.user_src a
-- 这个表;很关键;来自DWD层
LEFT JOIN ods.product_src b ON a.product_id = b.product_id
WHERE a.del_flag = 'N'"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert "ods.user_src" in " ".join(tables), f"user_src 应在 {tables}"
        assert any("product_src" in t for t in tables), \
            f"product_src 不应因注释分号丢失，实际 {tables}"

    def test_block_comment_with_semicolon(self):
        """块注释含分号：JOIN 表不能丢失。"""
        sql = """SELECT a.user_id, b.product_name
FROM ods.user_src a
/* 这个块注释;包含分号 */
LEFT JOIN ods.product_src b ON a.product_id = b.product_id"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert len(tables) >= 2, f"块注释分号导致表丢失，实际 {tables}"
        assert any("product_src" in t for t in tables)

    def test_mixed_comments_with_semicolon(self):
        """混合注释都含分号：所有表都应在。"""
        sql = """SELECT a.id, b.name
FROM ods.src_a a
-- 注释;有分号
JOIN ods.src_b b ON a.id = b.id
/* 块注释;也有分号 */
WHERE a.flag = 1"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert any("src_a" in t for t in tables), f"src_a 应在 {tables}"
        assert any("src_b" in t for t in tables), f"src_b 不应丢失 {tables}"

    def test_comment_with_semicolon_no_parse_error(self):
        """注释含分号不应导致解析报错。"""
        sql = """SELECT a.id FROM ods.tbl_a a
-- 备注;分号
WHERE a.flag = 1"""
        parsed = parse(sql)
        assert not parsed.parse_error, f"不应有解析错误: {parsed.parse_error}"

    def test_normal_comment_without_semicolon_still_works(self):
        """正常注释（无分号）不受影响。"""
        sql = """SELECT a.id, b.name
FROM ods.src_a a
-- 正常注释无分号
JOIN ods.src_b b ON a.id = b.id"""
        parsed = parse(sql)
        tables = source_tables(parsed)
        assert len(tables) == 2
