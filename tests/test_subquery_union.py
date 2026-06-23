"""
子查询 + UNION 场景回归测试

锁定两个契约：
1. 子查询内部的物理表必须出现在 data_flow（不再被错误过滤）
2. 子查询内部表的主从标签必须按透传规则正确：
   - FROM 子查询（子查询是主表）→ 内部主表也是主表，内部从表也是从表
   - JOIN 子查询（子查询是从表）→ 内部所有表都是从表

运行:
    pytest tests/test_subquery_union.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import parse_single_sql, build_data_flow, RawRule


# ═══════════════════════════════════════════════════════════════
# 场景构造
# ═══════════════════════════════════════════════════════════════

# UNION 两段，每段：FROM 子查询（内部主表+从表）+ 外层关联从表
UNION_SUBQUERY_SQL = """SELECT
    t1.order_id, t1.amount, d1.region_name
FROM (
    SELECT a.order_id, a.amount, a.region_id
    FROM ods.orders_a a
    LEFT JOIN ods.dim_region_a b ON a.region_id = b.region_id
) t1
LEFT JOIN ods.dim_region d1 ON t1.region_id = d1.region_id
UNION ALL
SELECT
    t2.order_id, t2.amount, d2.region_name
FROM (
    SELECT a.order_id, a.amount, a.region_id
    FROM ods.orders_b a
    LEFT JOIN ods.dim_region_b b ON a.region_id = b.region_id
) t2
LEFT JOIN ods.dim_region d2 ON t2.region_id = d2.region_id"""

# 单段 FROM 子查询 + JOIN 子查询（验证 JOIN 子查询内部全是主/从）
FROM_AND_JOIN_SUBQUERY_SQL = """SELECT
    m.order_id, m.amount, j.extra_info
FROM (
    SELECT a.order_id, a.amount
    FROM ods.main_a a
    INNER JOIN ods.dim_a b ON a.id = b.id
) m
LEFT JOIN (
    SELECT c.ref_id, c.extra_info
    FROM ods.side_c c
    LEFT JOIN ods.dim_c d ON c.ref_id = d.ref_id
) j ON m.order_id = j.ref_id"""


def _make_rule(sql):
    return RawRule(
        rule_code="R1", rule_type=1, exec_sequence=0,
        target_schema="dws", target_table="t_f", query_sql=sql,
    )


# ═══════════════════════════════════════════════════════════════
# 契约 1：子查询内部物理表不丢失
# ═══════════════════════════════════════════════════════════════

class TestSubqueryTablesNotLost:
    """子查询内部的物理表必须出现在 data_flow.tables。"""

    def test_union_subquery_inner_tables_present(self):
        """UNION 两段，每段子查询内的 4 个物理表都应出现"""
        rule = _make_rule(UNION_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(UNION_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        table_names = {t["name"].lower() for t in df["tables"]}
        # 子查询内部的物理表
        for expected in ("orders_a", "dim_region_a", "orders_b", "dim_region_b"):
            assert expected in table_names, \
                f"子查询内部表 {expected} 应出现在 data_flow.tables，实际 {table_names}"
        # 外层关联表也在
        assert "dim_region" in table_names

    def test_from_subquery_inner_tables_present(self):
        """单段 FROM 子查询，内部主表和从表都应出现"""
        rule = _make_rule(FROM_AND_JOIN_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(FROM_AND_JOIN_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        table_names = {t["name"].lower() for t in df["tables"]}
        for expected in ("main_a", "dim_a", "side_c", "dim_c"):
            assert expected in table_names, \
                f"子查询内部表 {expected} 应出现，实际 {table_names}"


# ═══════════════════════════════════════════════════════════════
# 契约 2：子查询内部表主从标签正确透传
# ═══════════════════════════════════════════════════════════════

class TestSubqueryPrimarySecondaryPropagation:
    """子查询内部表的主从属性按透传规则正确标记。

    规则：
    - FROM 子查询（子查询是主表）→ 内部主表=主表，内部从表=从表
    - JOIN 子查询（子查询是从表）→ 内部所有表=从表
    """

    def test_from_subquery_inner_main_is_main(self):
        """FROM 子查询内的主表（内部 FROM）应标记为主表"""
        rule = _make_rule(FROM_AND_JOIN_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(FROM_AND_JOIN_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        joins = df["steps"][0]["joins"]
        # 找 main_a（FROM 子查询 m 的内部主表）
        main_a = next((j for j in joins if "main_a" in j.get("source_table", "").lower()), None)
        assert main_a is not None, "main_a 应在 joins 里"
        # 应标记为主表（FROM 子查询透传主表属性）
        assert main_a["join_type"] in ("FROM_SUBQUERY_MAIN", "FROM"), \
            f"FROM 子查询内的主表 main_a 应标记为主表，实际 {main_a['join_type']}"

    def test_from_subquery_inner_join_is_secondary(self):
        """FROM 子查询内的从表（内部 JOIN）应标记为从表"""
        rule = _make_rule(FROM_AND_JOIN_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(FROM_AND_JOIN_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        joins = df["steps"][0]["joins"]
        dim_a = next((j for j in joins if "dim_a" in j.get("source_table", "").lower()), None)
        assert dim_a is not None, "dim_a 应在 joins 里"
        assert dim_a["join_type"] == "FROM_SUBQUERY", \
            f"FROM 子查询内的从表 dim_a 应标记为从表，实际 {dim_a['join_type']}"

    def test_join_subquery_inner_all_secondary(self):
        """JOIN 子查询内的所有表（无论内部主从）都应标记为从表"""
        rule = _make_rule(FROM_AND_JOIN_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(FROM_AND_JOIN_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        joins = df["steps"][0]["joins"]
        # side_c 是 JOIN 子查询 j 的内部主表，但因 j 是从表，side_c 也是从表
        side_c = next((j for j in joins if "side_c" in j.get("source_table", "").lower()), None)
        assert side_c is not None, "side_c 应在 joins 里"
        assert side_c["join_type"] == "JOIN_SUBQUERY_INNER", \
            f"JOIN 子查询内的表 side_c 应标记为从表，实际 {side_c['join_type']}"
        # dim_c 同理
        dim_c = next((j for j in joins if "dim_c" in j.get("source_table", "").lower()), None)
        assert dim_c is not None
        assert dim_c["join_type"] == "JOIN_SUBQUERY_INNER", \
            f"JOIN 子查询内的表 dim_c 应标记为从表，实际 {dim_c['join_type']}"

    def test_union_both_branches_inner_main_is_main(self):
        """UNION 两段的 FROM 子查询，内部主表都应标记为主表"""
        rule = _make_rule(UNION_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(UNION_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        joins = df["steps"][0]["joins"]
        orders_a = next((j for j in joins if "orders_a" in j.get("source_table", "").lower()), None)
        orders_b = next((j for j in joins if "orders_b" in j.get("source_table", "").lower()), None)
        assert orders_a is not None and orders_b is not None, "两段 orders 表应在 joins"
        assert orders_a["join_type"] in ("FROM_SUBQUERY_MAIN", "FROM"), \
            f"orders_a（FROM子查询内部主表）应为主表，实际 {orders_a['join_type']}"
        assert orders_b["join_type"] in ("FROM_SUBQUERY_MAIN", "FROM"), \
            f"orders_b（FROM子查询内部主表）应为主表，实际 {orders_b['join_type']}"


# ═══════════════════════════════════════════════════════════════
# 契约 3：UNION 分支独立记录 + 子查询字段穿透（union_branches）
# ═══════════════════════════════════════════════════════════════

class TestUnionBranchesStructure:
    """UNION 每个分支独立记录 source_tables + columns，字段穿透到物理表。

    分支=步骤内场景。同一字段（order_id）在分支1来自 orders_a，分支2来自 orders_b。
    """

    def test_union_branches_count(self):
        """两段 UNION 应产生 2 个 union_branches"""
        parsed = parse_single_sql(UNION_SUBQUERY_SQL, "dws")
        assert len(parsed.union_branches) == 2, \
            f"应有 2 个 union_branches，实际 {len(parsed.union_branches)}"

    def test_non_union_no_branches(self):
        """非 UNION 的普通 SQL 不应有 union_branches"""
        sql = "SELECT a.x FROM ods.tab a"
        parsed = parse_single_sql(sql, "dws")
        assert parsed.union_branches == [], "普通 SELECT 不应有 union_branches"

    def test_branch_columns_penetrate_to_physical(self):
        """分支字段穿透：t1.order_id → ods.orders_a.order_id"""
        parsed = parse_single_sql(UNION_SUBQUERY_SQL, "dws")
        b1 = parsed.union_branches[0]
        order_id_col = next(c for c in b1["columns"] if c.alias == "order_id")
        # source_fields 应是穿透后的物理来源
        assert order_id_col.source_fields, "order_id 应有物理来源"
        src = order_id_col.source_fields[0]
        assert "orders_a" in src["table"], \
            f"分支1 order_id 应穿透到 orders_a，实际 {src['table']}"
        assert src["branch"] == 1

    def test_same_field_different_source_per_branch(self):
        """同一字段在两个分支来源不同（分支1=orders_a，分支2=orders_b）"""
        parsed = parse_single_sql(UNION_SUBQUERY_SQL, "dws")
        b1_order = next(c for c in parsed.union_branches[0]["columns"] if c.alias == "order_id")
        b2_order = next(c for c in parsed.union_branches[1]["columns"] if c.alias == "order_id")
        assert "orders_a" in b1_order.source_fields[0]["table"]
        assert "orders_b" in b2_order.source_fields[0]["table"]

    def test_union_branches_in_data_flow(self):
        """union_branches 应进入 data_flow step 详情"""
        rule = _make_rule(UNION_SUBQUERY_SQL)
        parsed = {"R1": parse_single_sql(UNION_SUBQUERY_SQL, "dws")}
        df = build_data_flow([rule], parsed)
        step = df["steps"][0]
        assert "union_branches" in step, "step 详情应有 union_branches"
        assert len(step["union_branches"]) == 2
        # 验证序列化后的字段穿透信息完整
        b1 = step["union_branches"][0]
        order_id = next(c for c in b1["columns"] if c["alias"] == "order_id")
        assert order_id["physical_sources"], "序列化后 physical_sources 应非空"
        assert "orders_a" in order_id["physical_sources"][0]["table"]
