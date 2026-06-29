"""组合场景测试：CTE × UNION × 子查询 × 嵌套 的排列组合。

验证递归解析核心（QueryUnit）覆盖所有组合，JOIN/WHERE 不丢失。

运行:
    pytest tests/test_combination_scenarios.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import parse_single_sql


class TestCombinationScenarios:
    """CTE × UNION × 子查询 × 嵌套 组合场景。"""

    def test_cte_plus_union_subquery(self):
        """CTE + FROM 子查询(内含 UNION)"""
        sql = """WITH tm AS (
    SELECT a.id, a.amt FROM ods.fact_a a
    INNER JOIN ods.dim_b b ON a.k = b.k WHERE a.del_flag = 'N'
)
SELECT t.region, t.total FROM (
    SELECT m.region, SUM(m.amt) AS total FROM tm m
    INNER JOIN ods.fact_c c ON m.id = c.id WHERE m.region IS NOT NULL GROUP BY m.region
    UNION ALL
    SELECT n.region, SUM(n.amt) AS total FROM ods.fact_d n
    LEFT JOIN ods.dim_e e ON n.k = e.k WHERE n.sts = 'A' GROUP BY n.region
) t WHERE t.total > 0"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        assert len(p.union_branches) >= 2, f"UNION 分支应 >=2，实际 {len(p.union_branches)}"
        # JOIN 和 WHERE 应从所有层级收集
        join_fields = {j["field"] for j in p.join_usage}
        assert "k" in join_fields or "id" in join_fields, f"应有 JOIN 条件，实际 {join_fields}"
        where_fields = {w["field"] for w in p.where_usage}
        assert "del_flag" in where_fields, f"CTE 内部 WHERE 应收集，实际 {where_fields}"

    def test_top_level_union_join_where(self):
        """顶层 UNION 两分支都有 JOIN + WHERE"""
        sql = """SELECT a.region, SUM(a.amt) AS total FROM ods.fact_a a
INNER JOIN ods.dim_b b ON a.k = b.k WHERE a.del = 'N' GROUP BY a.region
UNION ALL
SELECT c.region, SUM(c.amt) AS total FROM ods.fact_c c
LEFT JOIN ods.dim_d d ON c.k = d.k WHERE c.sts = 'A' GROUP BY c.region"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        assert len(p.union_branches) >= 2
        # 两个分支的 JOIN 都应提取
        join_conds = [j.get("on_condition", "") for j in p.join_usage]
        assert any("b.k" in c for c in join_conds), f"分支1 JOIN 应提取，实际 {join_conds}"
        assert any("d.k" in c for c in join_conds), f"分支2 JOIN 应提取，实际 {join_conds}"

    def test_cte_with_subquery_inside(self):
        """CTE 内部含子查询"""
        sql = """WITH tm AS (
    SELECT t.id, t.amt FROM (
        SELECT a.id, a.amt FROM ods.fact_a a WHERE a.del = 'N'
    ) t INNER JOIN ods.dim_b b ON t.id = b.id
)
SELECT m.id, m.amt FROM tm m WHERE m.amt > 0"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        where_fields = {w["field"] for w in p.where_usage}
        assert "del" in where_fields, f"CTE 内部子查询的 WHERE 应收集，实际 {where_fields}"

    def test_union_branch_with_subquery(self):
        """UNION 分支内部含子查询"""
        sql = """SELECT t.region, t.total FROM (
    SELECT a.region, a.amt AS total FROM ods.fact_a a WHERE a.del = 'N'
    UNION ALL
    SELECT s.region, s.total FROM (
        SELECT b.region, SUM(b.amt) AS total FROM ods.fact_b b GROUP BY b.region
    ) s WHERE s.total > 0
) t"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        assert len(p.union_branches) >= 2

    def test_multi_cte_join_subquery(self):
        """多 CTE + JOIN + 子查询"""
        sql = """WITH agg1 AS (
    SELECT a.id, SUM(a.amt) AS total FROM ods.fact_a a GROUP BY a.id
),
agg2 AS (
    SELECT b.id, COUNT(*) AS cnt FROM ods.fact_b b WHERE b.sts = 'A' GROUP BY b.id
)
SELECT t.id, t.total, t.cnt, d.name FROM (
    SELECT a.id, a.total, b.cnt FROM agg1 a
    INNER JOIN agg2 b ON a.id = b.id
) t
LEFT JOIN ods.dim_d d ON t.id = d.id WHERE t.total > 0"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        # CTE 内部的 WHERE 应收集
        where_fields = {w["field"] for w in p.where_usage}
        assert "sts" in where_fields, f"CTE agg2 的 WHERE 应收集，实际 {where_fields}"

    def test_three_layer_nesting_with_cte(self):
        """三层嵌套 + CTE（最复杂组合）"""
        sql = """WITH base AS (
    SELECT a.id, a.region, a.amt FROM ods.fact_a a WHERE a.del = 'N'
)
SELECT t.region, t.total FROM (
    SELECT m.region, SUM(m.amt) AS total FROM (
        SELECT b.id, b.region, b.amt FROM base b
        INNER JOIN ods.dim_c c ON b.id = c.id WHERE b.amt > 0
    ) m
    GROUP BY m.region
) t
WHERE t.total > 100"""
        p = parse_single_sql(sql, "dws")
        assert not p.parse_error
        where_fields = {w["field"] for w in p.where_usage}
        assert "del" in where_fields, f"CTE base 的 WHERE 应收集，实际 {where_fields}"
        assert "amt" in where_fields, f"子查询内部 WHERE 应收集，实际 {where_fields}"

    def test_no_crash_on_complex_combination(self):
        """最复杂组合不崩溃（CTE + UNION + 三层嵌套 + 多 JOIN）"""
        sql = """WITH base AS (
    SELECT a.id, a.region FROM ods.fact_a a
    INNER JOIN ods.dim_b b ON a.k = b.k WHERE a.del = 'N'
)
SELECT t.region, t.total, t.cnt FROM (
    SELECT m.region, SUM(m.amt) AS total, COUNT(*) AS cnt FROM (
        SELECT b.id, b.region, c.amt FROM base b
        INNER JOIN ods.fact_c c ON b.id = c.id WHERE c.amt > 0
    ) m GROUP BY m.region
    UNION ALL
    SELECT n.region, SUM(n.amt) AS total, COUNT(*) AS cnt FROM ods.fact_d n
    LEFT JOIN ods.dim_e e ON n.k = e.k WHERE n.sts = 'A' GROUP BY n.region
) t WHERE t.total > 100"""
        p = parse_single_sql(sql, "dws")
        # 不崩溃即通过
        assert not p.parse_error, f"复杂组合不应解析失败: {p.parse_error}"

    def test_top_level_union_logic_blocks_separate(self):
        """顶层 UNION 的逻辑块：两个分支独立展示，WHERE 不混在一起"""
        from analyzer import build_data_flow, build_field_mappings, build_data_blocks, RawRule
        sql = """SELECT a.region, SUM(a.amt) AS total FROM ods.fact_a a
INNER JOIN ods.dim_b b ON a.k = b.k WHERE a.del = 'N' GROUP BY a.region
UNION ALL
SELECT c.region, SUM(c.amt) AS total FROM ods.fact_c c
LEFT JOIN ods.dim_d d ON c.k = d.k WHERE c.sts = 'A' GROUP BY c.region"""
        p = parse_single_sql(sql, "dws")
        rule = RawRule(rule_code="R1", rule_name="UNION", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1", query_sql=sql)
        df = build_data_flow([rule], {"R1": p})
        fm = build_field_mappings([rule], {"R1": p}, {})
        blocks = build_data_blocks(df["steps"][0], df["steps"][0], p, fm["fields"])

        # 应有一个 UNION 块，含两个分支
        assert len(blocks) == 1, f"应有1个UNION块，实际 {len(blocks)}"
        union_blk = blocks[0]
        assert union_blk["type"] == "union", f"块类型应为union"
        children = union_blk.get("children", [])
        assert len(children) == 2, f"应有2个分支，实际 {len(children)}"

        # 两个分支的 WHERE 应各自独立
        branch1_where = children[0].get("where_clause", "")
        branch2_where = children[1].get("where_clause", "")
        assert "del" in branch1_where, f"分支1 WHERE 应含 del，实际 {branch1_where}"
        assert "sts" in branch2_where, f"分支2 WHERE 应含 sts，实际 {branch2_where}"
        # WHERE 不应混在一起
        assert "sts" not in branch1_where, f"分支1 WHERE 不应含 sts（混在一起了）"
        assert "del" not in branch2_where, f"分支2 WHERE 不应含 del（混在一起了）"
