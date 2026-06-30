"""生产复杂度 SQL 测试。

用接近真实制品包的"脏 SQL"测试——函数嵌套、复杂 WHERE、
注释、占位符、NOT 条件、CASE WHEN 套聚合等。

目的：确保简单测试 SQL 无法暴露的问题被捕获。

运行:
    pytest tests/test_production_sql.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import (
    parse_single_sql, build_data_flow, build_field_mappings,
    build_data_blocks, RawRule,
)


def _test_sql_full(sql, label):
    """完整跑一遍解析 + 逻辑块构建，返回 (parsed, blocks)。"""
    p = parse_single_sql(sql, "dws")
    if p.parse_error:
        pytest.fail(f"【{label}】解析失败: {p.parse_error[:80]}")
    rule = RawRule(rule_code="R1", rule_name=label, rule_type=1, exec_sequence=1,
                   target_schema="dws", target_table="dwl_test_f", delete_mode="1",
                   query_sql=sql)
    df = build_data_flow([rule], {"R1": p})
    fm = build_field_mappings([rule], {"R1": p}, {})
    step = {"step_id": "step_1", "rule_code": "R1"}
    blocks = build_data_blocks(step, df["steps"][0], p, fm["fields"])
    return p, blocks


def _flatten(blocks, result=None):
    if result is None:
        result = []
    for b in blocks:
        result.append(b)
        _flatten(b.get("children", []), result)
    return result


# ═══════════════════════════════════════════════════════════════
# 1. 函数嵌套
# ═══════════════════════════════════════════════════════════════

class TestFunctionNesting:
    """DECODE/TO_NUMBER/TO_CHAR/ROUND 等多层嵌套函数。"""

    def test_decode_to_number_to_char(self):
        """DECODE(TO_NUMBER(TO_CHAR(...))) 三层嵌套"""
        sql = """SELECT a.cust_id,
    SUM(DECODE(TO_NUMBER(TO_CHAR(a.type_id)), 1, a.amt, 0)) AS typed_amt
FROM ods.fact_a a
LEFT JOIN ods.dim_b b ON a.k = b.k
GROUP BY a.cust_id"""
        p, blocks = _test_sql_full(sql, "DECODE嵌套")
        # 不崩溃 + 有字段
        assert len(p.select_columns) >= 1
        # typed_amt 应识别为聚合或加工
        tt = {c.alias: c.transform_type for c in p.select_columns}
        assert "typed_amt" in tt

    def test_case_when_with_aggregate(self):
        """CASE WHEN 里套 SUM 聚合"""
        sql = """SELECT a.region,
    CASE WHEN SUM(a.amt) = 0 THEN 0 ELSE ROUND(SUM(a.xamt) / SUM(a.amt), 4) END AS ratio
FROM ods.fact_a a
GROUP BY a.region"""
        p, blocks = _test_sql_full(sql, "CASE WHEN套聚合")
        tt = {c.alias: c.transform_type for c in p.select_columns}
        assert "ratio" in tt

    def test_nested_function_in_join_on(self):
        """JOIN ON 条件里有 TO_NUMBER"""
        sql = """SELECT a.id, b.name FROM ods.t1 a
LEFT JOIN ods.t2 b ON TO_NUMBER(a.code) = TO_NUMBER(b.code)"""
        p, blocks = _test_sql_full(sql, "JOIN ON套函数")
        assert len(p.join_usage) >= 1


# ═══════════════════════════════════════════════════════════════
# 2. 复杂 WHERE
# ═══════════════════════════════════════════════════════════════

class TestComplexWhere:
    """NOT(...)、IN 列表、AND 串联多条件。"""

    def test_not_condition(self):
        """NOT(app.x IS NULL AND app.y=1)"""
        sql = """SELECT a.id FROM ods.t1 a
WHERE NOT(a.x IS NULL AND a.y = 1 AND a.z IN ('001','002','003'))"""
        p, blocks = _test_sql_full(sql, "NOT条件")
        assert len(p.where_usage) >= 1
        # 逻辑块应有过滤
        mains = [b for b in _flatten(blocks) if "主表" in b.get("role", "")]
        assert mains and "过滤" in mains[0].get("ops", [])

    def test_multi_and_where(self):
        """AND 串联 5+ 条件"""
        sql = """SELECT a.id FROM ods.t1 a
WHERE a.del = 'N' AND a.sts = 'A' AND a.amt > 0 AND a.type = 'X' AND a.region IS NOT NULL"""
        p, blocks = _test_sql_full(sql, "多AND条件")
        assert len(p.where_usage) >= 3  # 至少提取到部分字段

    def test_in_list_where(self):
        """IN ('001','002') 列表"""
        sql = """SELECT a.id FROM ods.t1 a WHERE a.type IN ('001','002','003')"""
        p, blocks = _test_sql_full(sql, "IN列表")
        assert len(p.where_usage) >= 1


# ═══════════════════════════════════════════════════════════════
# 3. 注释
# ═══════════════════════════════════════════════════════════════

class TestSQLComments:
    """SQL 内嵌注释。"""

    def test_inline_comment(self):
        """行内注释 /* xxx */"""
        sql = """SELECT a.id, /* 主键 */ 'N' AS del_flag FROM ods.t1 a"""
        p, blocks = _test_sql_full(sql, "行内注释")
        aliases = {c.alias for c in p.select_columns}
        # 注释里的字段名应被提取为列别名
        assert "id" in aliases

    def test_comment_between_lines(self):
        """多行之间的注释"""
        sql = """SELECT a.id, a.amt
/* 这个字段是金额 */
FROM ods.t1 a WHERE a.del = 'N'"""
        p, blocks = _test_sql_full(sql, "多行注释")
        assert not p.parse_error


# ═══════════════════════════════════════════════════════════════
# 4. 占位符
# ═══════════════════════════════════════════════════════════════

class TestPlaceholders:
    """变量占位符 ${xxx} / #xxx#。"""

    def test_dollar_placeholder(self):
        """${P_CYCLE_ID} 占位符"""
        sql = """SELECT a.id, '${P_CYCLE_ID}' AS cycle_id FROM ods.t1 a WHERE a.cycle = ${P_CYCLE_ID}"""
        p, blocks = _test_sql_full(sql, "美元占位符")
        assert not p.parse_error
        aliases = {c.alias for c in p.select_columns}
        assert "cycle_id" in aliases

    def test_hash_placeholder(self):
        """#p_period_id# 占位符"""
        sql = """SELECT a.id FROM ods.t1 a WHERE a.period = TO_NUMBER('#p_period_id#')"""
        p, blocks = _test_sql_full(sql, "井号占位符")
        assert not p.parse_error


# ═══════════════════════════════════════════════════════════════
# 5. 完整生产级 SQL（综合）
# ═══════════════════════════════════════════════════════════════

class TestProductionLevelSQL:
    """接近真实制品包的完整 SQL。"""

    def test_cte_union_with_functions_and_not(self):
        """你的实际结构：CTE + UNION + DECODE + NOT + 注释 + 占位符"""
        sql = """WITH tmp2 AS (
    SELECT a.id, a.amt, a.type_id
    FROM ods.fact_a a
    LEFT JOIN ods.dim_h h ON a.h_id = h.h_id
    WHERE a.del_flag = 'N'
)
SELECT SUM(t.amt) AS total_amt, t.cust_id,
    CASE WHEN SUM(t.amt) = 0 THEN 0 ELSE TO_NUMBER(SUM(t.xamt) / SUM(t.amt)) END AS ratio
FROM (
    SELECT SUM(app.amt) AS amt,
        SUM(DECODE(TO_NUMBER(TO_CHAR(app.type_id, 'YYYYMM')), TO_NUMBER('#p_period_id#'), app.xamt, 0)) AS xamt,
        app.cust_id, null as uv
    FROM tmp2 app
    INNER JOIN ods.dim_cre cre ON app.cre_id = cre.cre_id AND NOT(app.x1 IS NULL) and cre.status >= 10
    INNER JOIN ods.dim_com com ON app.com_id = com.com_id
    WHERE NOT(app.x2 IS NULL and app.x1 = 1 and app.x3 in ('001','002'))
    GROUP BY app.cust_id
    UNION ALL
    SELECT SUM(app.amt) AS amt,
        SUM(DECODE(TO_NUMBER(TO_CHAR(app.type_id)), 2, app.xamt, 0)) AS xamt,
        app.cust_id
    FROM tmp2 app
    INNER JOIN ods.dim_cre2 cre ON app.cre_id = cre.cre_id
    INNER JOIN ods.dim_com2 com ON app.com_id = com.com_id
    GROUP BY app.cust_id
) t
GROUP BY t.cust_id"""
        p, blocks = _test_sql_full(sql, "完整生产级SQL")

        # UNION 分支必须正确
        all_flat = _flatten(blocks)
        branches = [b for b in all_flat if "UNION 分支" in b.get("role", "")]
        assert len(branches) == 2, f"应有2个UNION分支，实际 {len(branches)}"

        # 分支1 有过滤条件
        assert "过滤" in branches[0].get("ops", []), "分支1 应有过滤"

        # CTE 内部展开
        tables = [b["table"].lower() for b in all_flat]
        assert any("fact_a" in t for t in tables), "CTE 内部 fact_a 应出现"
        assert any("dim_h" in t for t in tables), "CTE 内部 dim_h 应出现"

        # 分支1 和分支2 各有 JOIN 表
        branch1_children = branches[0].get("children", [])
        branch1_tables = [b["table"].lower() for b in branch1_children]
        assert any("dim_cre" in t for t in branch1_tables), "分支1 应有 dim_cre"

        # 外层有收敛
        top_block = blocks[0]
        assert "收敛" in top_block.get("ops", []), "外层应有收敛"

    def test_multi_cte_complex_joins(self):
        """多CTE + 复杂JOIN（每个JOIN都有额外AND条件）"""
        sql = """WITH agg1 AS (
    SELECT a.id, SUM(a.amt) AS total
    FROM ods.fact_a a WHERE a.del = 'N' GROUP BY a.id
),
agg2 AS (
    SELECT b.id, COUNT(*) AS cnt
    FROM ods.fact_b b WHERE b.sts = 'A' GROUP BY b.id
)
SELECT a.id, a.total, b.cnt, c.name
FROM agg1 a
INNER JOIN agg2 b ON a.id = b.id AND b.cnt > 0
LEFT JOIN ods.dim_c c ON a.id = c.id AND c.type = 'MAIN'
WHERE a.total > 100 AND c.name IS NOT NULL"""
        p, blocks = _test_sql_full(sql, "多CTE复杂JOIN")

        # CTE 内部展开
        all_flat = _flatten(blocks)
        tables = [b["table"].lower() for b in all_flat]
        assert any("fact_a" in t for t in tables), "CTE agg1 内部应出现"
        assert any("fact_b" in t for t in tables), "CTE agg2 内部应出现"

        # 外层有过滤
        mains = [b for b in all_flat if b.get("role") in ("主表", "关联表", "从表")]
        assert any("过滤" in b.get("ops", []) for b in mains), "应有过滤"

    def test_subquery_with_rank_and_case(self):
        """子查询 + ROW_NUMBER + CASE WHEN 取最新"""
        sql = """SELECT t.cust_id, t.amt, t.rn
FROM (
    SELECT a.cust_id, a.amt,
        ROW_NUMBER() OVER (PARTITION BY a.cust_id ORDER BY a.day_id DESC) AS rn,
        CASE WHEN a.type = 'X' THEN 1 ELSE 0 END AS is_x
    FROM ods.fact_a a
    WHERE a.del = 'N' AND a.sts = 'A'
) t
WHERE t.rn = 1"""
        p, blocks = _test_sql_full(sql, "子查询+ROW_NUMBER+CASE")

        # 窗口函数应识别
        tt = {c.alias: c.transform_type for c in p.select_columns}
        assert "rn" in tt, "rn 字段应存在"

        # 逻辑块应有子查询来源（FROM子查询会标为"主查询来源"或含内部表）
        all_flat = _flatten(blocks)
        has_inner = any("内部" in b.get("role", "") or "子查询" in b.get("role", "") or "来源" in b.get("role", "")
                        for b in all_flat)
        assert has_inner, f"应有子查询相关块，实际 roles: {[b.get('role','') for b in all_flat]}"

    def test_long_column_list(self):
        """超长列列表（20+ 字段）"""
        cols = ", ".join([f"a.col_{i}" for i in range(20)])
        sql = f"""SELECT {cols} FROM ods.fact_a a
LEFT JOIN ods.dim_b b ON a.id = b.id WHERE a.del = 'N'"""
        p, blocks = _test_sql_full(sql, "20字段")
        assert len(p.select_columns) == 20

    def test_complex_group_by_multiple_keys(self):
        """GROUP BY 多维度（5+ 字段）"""
        sql = """SELECT a.region, a.province, a.city, a.type, a.level,
    SUM(a.amt) AS total, COUNT(*) AS cnt
FROM ods.fact_a a
INNER JOIN ods.dim_b b ON a.k = b.k
WHERE a.del = 'N'
GROUP BY a.region, a.province, a.city, a.type, a.level"""
        p, blocks = _test_sql_full(sql, "5维度GROUP BY")
        # 逻辑块主表应有收敛
        mains = [b for b in _flatten(blocks) if "主表" in b.get("role", "")]
        assert mains and "收敛" in mains[0].get("ops", [])


# ═══════════════════════════════════════════════════════════════
# 6. 刁钻占位符位置（build_data_blocks 二次解析陷阱）
# ═══════════════════════════════════════════════════════════════

class TestDirtyPlaceholderPositions:
    """占位符放在会导致 sqlglot 解析失败的刁钻位置。

    build_data_blocks 第二次解析 SQL 时如果不做 _replace_placeholders，
    这些位置的占位符会导致解析失败 → 回退 flat → 逻辑块丢数据。
    """

    def test_hash_in_to_number(self):
        """占位符在 TO_NUMBER(#xxx#) 里"""
        sql = """SELECT a.id, SUM(DECODE(TO_NUMBER(TO_CHAR(a.type_id)), TO_NUMBER('#p_period_id#'), a.amt, 0)) AS xamt
FROM ods.fact_a a WHERE a.del = 'N' GROUP BY a.id"""
        p, blocks = _test_sql_full(sql, "TO_NUMBER里的井号占位符")
        assert len(p.select_columns) >= 1
        # 逻辑块不回退 flat（有主表+操作）
        mains = [b for b in _flatten(blocks) if "主表" in b.get("role", "")]
        assert mains, "不应回退 flat"

    def test_hash_in_cte_union_full_structure(self):
        """完整生产结构：CTE + UNION + DECODE + 占位符在 TO_NUMBER 里"""
        sql = """WITH tmp2 AS (
    SELECT a.id, a.amt, a.type_id FROM ods.fact_a a
    LEFT JOIN ods.dim_h h ON a.h_id = h.h_id WHERE a.del = 'N'
)
SELECT SUM(t.amt) AS total, t.cust_id FROM (
    SELECT SUM(app.amt) AS amt,
        SUM(DECODE(TO_NUMBER(TO_CHAR(app.type_id, 'YYYYMM')), TO_NUMBER('#p_period_id#'), app.xamt, 0)) AS xamt,
        app.cust_id
    FROM tmp2 app
    INNER JOIN ods.dim_cre cre ON app.cre_id = cre.cre_id AND NOT(app.x1 IS NULL)
    WHERE NOT(app.x2 IS NULL)
    GROUP BY app.cust_id
    UNION ALL
    SELECT SUM(app.amt) AS amt,
        SUM(DECODE(TO_NUMBER(TO_CHAR(app.type_id)), 2, app.xamt, 0)) AS xamt,
        app.cust_id
    FROM tmp2 app
    INNER JOIN ods.dim_com com ON app.com_id = com.com_id
    GROUP BY app.cust_id
) t GROUP BY t.cust_id"""
        p, blocks = _test_sql_full(sql, "完整生产结构+占位符")

        # UNION 分支必须正确（不能因为占位符导致回退 flat）
        all_flat = _flatten(blocks)
        branches = [b for b in all_flat if "UNION 分支" in b.get("role", "")]
        assert len(branches) == 2, f"占位符不应导致 UNION 分支丢失，实际 {len(branches)} 个分支"

        # CTE 内部展开
        tables = [b["table"].lower() for b in all_flat]
        assert any("fact_a" in t for t in tables), "CTE 内部 fact_a 应出现"

        # 分支1 有过滤
        assert "过滤" in branches[0].get("ops", []), "分支1 应有过滤"

    def test_dollar_in_where_and_select(self):
        """${占位符} 同时出现在 WHERE 和 SELECT 里"""
        sql = """SELECT a.id, '${P_CYCLE_ID}' AS cycle, a.amt
FROM ods.fact_a a WHERE a.cycle = ${P_CYCLE_ID} AND a.del = 'N'"""
        p, blocks = _test_sql_full(sql, "美元占位符多位置")
        aliases = {c.alias for c in p.select_columns}
        assert "cycle" in aliases

    def test_nested_hash_in_decode_in_union_branch(self):
        """占位符在 UNION 分支的 DECODE 嵌套里"""
        sql = """SELECT t.id, t.xamt FROM (
    SELECT a.id, SUM(DECODE(a.type, TO_NUMBER('#pid#'), a.amt, 0)) AS xamt
    FROM ods.fact_a a WHERE a.del = 'N' GROUP BY a.id
    UNION ALL
    SELECT b.id, SUM(b.amt) AS xamt FROM ods.fact_b b GROUP BY b.id
) t"""
        p, blocks = _test_sql_full(sql, "UNION分支DECODE里的占位符")
        all_flat = _flatten(blocks)
        branches = [b for b in all_flat if "UNION 分支" in b.get("role", "")]
        assert len(branches) == 2, "UNION 分支不应丢失"

    def test_clean_sql_stored(self):
        """parse_single_sql 应存 clean_sql（预处理后）"""
        sql = "SELECT a.id FROM ods.t a WHERE a.cycle = ${P_CYCLE_ID}"
        p = parse_single_sql(sql, "dws")
        assert p.clean_sql, "clean_sql 应非空"
        # _replace_placeholders 把 ${xxx} 包成字符串 '${xxx}'，不是删除
        assert "'${P_CYCLE_ID}'" in p.clean_sql or "P_CYCLE_ID" in p.clean_sql, \
            f"clean_sql 应含处理后的占位符，实际 {p.clean_sql}"
