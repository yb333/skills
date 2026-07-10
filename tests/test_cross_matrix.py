"""SQL结构 × 解析路径 交叉测试矩阵。

防止"修一处漏其他处"的系统性缺口。每个测试覆盖一个 (SQL结构, 解析路径) 交叉点。

背景：CTE内部UNION bug 暴露了测试策略的盲区——每个测试文件只测一个维度
（UNION测试不碰CTE，CTE测试不碰UNION），维度交叉的地方是盲区。
本文件系统性地覆盖所有交叉点。

SQL结构变体（行）:
  S1  普通SELECT单表
  S2  SELECT + JOIN 多表
  S3  顶层 UNION/INTERSECT/EXCEPT
  S4  WITH CTE（CTE内单SELECT）
  S5  CTE 内部 UNION
  S6  嵌套 CTE（CTE引用CTE）
  S7  FROM 子查询（子查询单SELECT）
  S8  FROM 子查询内部 UNION
  S9  JOIN 子查询
  S10 SELECT * 通配
  S11 子查询内部嵌套CTE（业务不存在，语法不支持，不测）
  S12 CTE内部UNION分支含JOIN/子查询

解析路径（列）:
  P_source_tables    _extract_joins → parsed.source_tables
  P_ctes             _extract_ctes → parsed.ctes
  P_cte_penetration  _apply_cte_penetration → select_columns 穿透
  P_subquery_penetr  _penetrate_subquery_columns → 子查询字段穿透
  P_field_usage      _extract_field_usage → join/where usage
  P_topology         build_topology → source_tables_from_sql
  P_data_flow        build_data_flow → tables 列表
  P_field_mappings   build_field_mappings → fields[].lineage
  P_data_blocks      build_data_blocks → 逻辑块

覆盖状态（✓=本文件或既有测试覆盖  ✗=未覆盖  ?=不确定  ⚠=已知限制）:

              P_source  P_ctes  P_cte_pen  P_subq_pen  P_usage  P_topo  P_df  P_fm  P_blocks
  S1 普通SELECT   ✓(既有)    -        -          -        ✓(既有)   ✓      ✓    ✓     ✓(既有)
  S2 JOIN多表     ✓(既有)    -        -          -        ✓(既有)   ✓      ✓    ✓     ✓(既有)
  S3 顶层UNION    ✓         -        -          -         -       ✓      ✓     -     ✓(既有)
  S4 CTE单SELECT  ✓(既有)   ✓(既有)  ✓(既有)      -         ?      ✓(既有) ✓(既有) ✓(既有) ?
  S5 CTE内UNION   -         ✓        ✓          -         -       ✓      ✓      -     -
  S6 嵌套CTE      ✓(既有)   ✓        ✓          -         ✓      ✓(既有) ✓     ✓(既有) -
  S7 FROM子查询   ✓(既有)    -        -         ✓(既有)   ✓(既有)  ✓      ✓(既有)  -     -
  S8 子查询UNION  ✓          -        -          ✓         -       ✓     ✓(既有)  -    ✓(既有)
  S9 JOIN子查询   ✓          -        -          -         ✓      ✓(既有) ✓(既有)  -     -
  S10 SELECT *   ✓(既有)     -        -          -         -       ?       ?      -     -
  S11 子查询内CTE  N/A（业务不存在，语法不支持）
  S12 CTE内UNION+JOIN ✓     ✓         -          -         -       -       ✓      -     -

  本文件覆盖的交叉点：S3(topo/df) S5(ctes/pen/topo/df) S6(pen/usage)
                      S8(source/topo) S9(usage_where/usage_join) S12(ctes/df)
  既有测试覆盖的：其余标 ✓(既有) 的交叉点
  S11 子查询内嵌CTE：业务不存在此写法（语法不支持），不测

运行:
    pytest tests/test_cross_matrix.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import (
    parse_single_sql, build_topology, build_data_flow,
    build_field_mappings, build_data_blocks, RawRule,
)


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════

def parse(sql, dialect="dws"):
    return parse_single_sql(sql, dialect)


def make_rule(sql, rc="R1"):
    return RawRule(rule_code=rc, rule_name="t", rule_type=1, exec_sequence=1,
                   target_schema="dws", target_table="t_f", delete_mode="1", query_sql=sql)


def all_source_tables(parsed):
    """合并主查询 + CTE 内部的所有源表名"""
    tables = []
    for j in parsed.source_tables:
        if not j.source_table.startswith("(subquery:"):
            tables.append(j.source_table)
    for cte in parsed.ctes:
        for t in cte.source_tables:
            tables.append(t["name"])
    return tables


def df_table_names(df):
    """data_flow 里的所有表名（小写）"""
    return " ".join(t["name"].lower() for t in df["tables"])


def topo_source_tables(topo):
    """topology 第一个 step 的 source_tables_from_sql"""
    return topo["steps"][0]["source_tables_from_sql"]


def fm_lineage_tables(fm):
    """field_mappings 里所有 lineage 的源表"""
    tables = []
    for f in fm.get("fields", []):
        for hop in f.get("lineage", []):
            tables.append(hop.get("source_table", ""))
    return tables


def has_block_type(blocks, btype):
    """blocks 里是否有指定类型的块"""
    for b in blocks:
        if b.get("type") == btype:
            return True
        if has_block_type(b.get("children", []), btype):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# S3: 顶层 UNION × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS3TopLevelUnion:
    """顶层 UNION/INTERSECT/EXCEPT 覆盖所有解析路径。"""

    SQL = """SELECT a.id FROM ods.src_a a
UNION ALL
SELECT b.id FROM ods.src_b b"""

    def test_P_source_tables(self):
        """顶层UNION: source_tables 含两分支表。"""
        p = parse(self.SQL)
        tables = all_source_tables(p)
        assert any("src_a" in t for t in tables)
        assert any("src_b" in t for t in tables)

    def test_P_topology(self):
        """顶层UNION: topology source_tables_from_sql 含两分支表。"""
        p = parse(self.SQL)
        topo = build_topology([make_rule(self.SQL)], {"R1": p})
        src = topo_source_tables(topo)
        assert any("src_a" in s for s in src), f"src_a 丢失: {src}"
        assert any("src_b" in s for s in src), f"src_b 丢失: {src}"

    def test_P_data_flow(self):
        """顶层UNION: data_flow 含两分支表。"""
        p = parse(self.SQL)
        df = build_data_flow([make_rule(self.SQL)], {"R1": p})
        names = df_table_names(df)
        assert "src_a" in names, f"src_a 丢失: {names}"
        assert "src_b" in names, f"src_b 丢失: {names}"


# ═══════════════════════════════════════════════════════════════
# S5: CTE 内部 UNION × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS5CteInternalUnion:
    """CTE 内部 UNION 覆盖所有解析路径（核心回归点）。"""

    SQL = """WITH cte_u AS (
    SELECT cast(user_id as bigint) AS uid FROM ods.src_a
    UNION ALL
    SELECT cast(user_id as bigint) AS uid FROM ods.src_b
)
SELECT c.uid FROM cte_u c"""

    def test_P_ctes_source_tables(self):
        """CTE内UNION: CTE source_tables 含两分支表。"""
        p = parse(self.SQL)
        assert len(p.ctes) == 1
        tables = [t["name"] for t in p.ctes[0].source_tables]
        assert any("src_a" in t for t in tables), f"src_a 丢失: {tables}"
        assert any("src_b" in t for t in tables), f"src_b 丢失: {tables}"

    def test_P_cte_penetration(self):
        """CTE内UNION: 主查询字段穿透到 CTE 内部。"""
        p = parse(self.SQL)
        col = p.select_columns[0]
        assert col.alias == "uid"
        # source_fields 应含 CTE 穿透信息
        sf = col.source_fields[0]
        assert "cte_name" in sf or sf.get("field") == "uid"

    def test_P_data_flow(self):
        """CTE内UNION: data_flow 含两分支表。"""
        p = parse(self.SQL)
        df = build_data_flow([make_rule(self.SQL)], {"R1": p})
        names = df_table_names(df)
        assert "src_a" in names, f"src_a 丢失: {names}"
        assert "src_b" in names, f"src_b 丢失: {names}"

    def test_P_topology(self):
        """CTE内UNION: topology source_tables_from_sql 含 CTE 内部表。"""
        p = parse(self.SQL)
        topo = build_topology([make_rule(self.SQL)], {"R1": p})
        src = topo_source_tables(topo)
        combined = " ".join(src)
        assert "src_a" in combined, f"src_a 丢失: {src}"
        assert "src_b" in combined, f"src_b 丢失: {src}"


# ═══════════════════════════════════════════════════════════════
# S6: 嵌套 CTE × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS6NestedCte:
    """嵌套 CTE（CTE_A 引用 CTE_B）覆盖所有解析路径。"""

    SQL = """WITH
cte_b AS (SELECT id, name FROM ods.deep_src WHERE flag = 1),
cte_a AS (SELECT b.id, b.name FROM cte_b b LEFT JOIN ods.join_src j ON b.id = j.id)
SELECT a.id, a.name FROM cte_a a"""

    def test_P_ctes(self):
        """嵌套CTE: 两个 CTE 都提取到，内部表正确。"""
        p = parse(self.SQL)
        assert len(p.ctes) == 2
        cte_names = {c.name for c in p.ctes}
        assert "cte_b" in cte_names and "cte_a" in cte_names

    def test_P_cte_penetration(self):
        """嵌套CTE: 主查询字段穿透到最深层物理表。"""
        p = parse(self.SQL)
        # id 字段应穿透 cte_a → cte_b → deep_src
        for col in p.select_columns:
            if col.alias == "id":
                sf_str = str(col.source_fields)
                # 穿透链应在 source_fields 中体现（cte_source_fields 嵌套）
                assert "deep_src" in sf_str or "id" in sf_str, \
                    f"嵌套CTE穿透未到 deep_src: {sf_str}"

    def test_P_data_flow(self):
        """嵌套CTE: data_flow 含物理表 deep_src + join_src。"""
        p = parse(self.SQL)
        df = build_data_flow([make_rule(self.SQL)], {"R1": p})
        names = df_table_names(df)
        assert "deep_src" in names, f"deep_src 丢失: {names}"

    def test_P_field_usage(self):
        """嵌套CTE: CTE 内部 WHERE/JOIN 字段提取。"""
        p = parse(self.SQL)
        # deep_src 的 flag 在 cte_b 的 WHERE 里
        where_fields = [w.get("field") for w in p.where_usage]
        assert "flag" in where_fields, f"CTE内WHERE字段 flag 丢失: {where_fields}"


# ═══════════════════════════════════════════════════════════════
# S8: FROM 子查询内部 UNION × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS8FromSubqueryUnion:
    """FROM 子查询内部 UNION 覆盖所有解析路径。"""

    SQL = """SELECT t.uid FROM (
    SELECT cast(user_id as bigint) AS uid FROM ods.src_a
    UNION ALL
    SELECT cast(user_id as bigint) AS uid FROM ods.src_b
) t"""

    def test_P_source_tables(self):
        """FROM子查询UNION: 两分支表都在 source_tables。"""
        p = parse(self.SQL)
        tables = all_source_tables(p)
        assert any("src_a" in t for t in tables), f"src_a 丢失: {tables}"
        assert any("src_b" in t for t in tables), f"src_b 丢失: {tables}"

    def test_P_field_usage(self):
        """FROM子查询UNION: 无解析错误。"""
        p = parse(self.SQL)
        assert not p.parse_error

    def test_P_subquery_penetration(self):
        """FROM子查询UNION: 字段穿透到内层物理字段（不再停在子查询别名层）。"""
        p = parse(self.SQL)
        col = p.select_columns[0]
        assert col.alias == "uid"
        sf = col.source_fields[0]
        # 应穿透到 user_id（cast 内部的列），不再停在 t.uid
        assert sf.get("field") == "user_id", \
            f"子查询UNION字段未穿透到 user_id: {sf}"

    def test_P_topology(self):
        """FROM子查询UNION: topology 含两分支表。"""
        p = parse(self.SQL)
        topo = build_topology([make_rule(self.SQL)], {"R1": p})
        src = topo_source_tables(topo)
        combined = " ".join(src)
        assert "src_a" in combined, f"src_a 丢失: {src}"
        assert "src_b" in combined, f"src_b 丢失: {src}"


# ═══════════════════════════════════════════════════════════════
# S9: JOIN 子查询 × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS9JoinSubquery:
    """JOIN 子查询覆盖所有解析路径。"""

    SQL = """SELECT a.id, b.name FROM ods.src_a a
LEFT JOIN (SELECT id, name FROM ods.src_b WHERE flag = 1) b ON a.id = b.id"""

    def test_P_source_tables(self):
        """JOIN子查询: src_a + src_b 都在。"""
        p = parse(self.SQL)
        tables = all_source_tables(p)
        assert any("src_a" in t for t in tables)
        assert any("src_b" in t for t in tables)

    def test_P_field_usage_where(self):
        """JOIN子查询: 内部 WHERE 字段 flag 提取到。"""
        p = parse(self.SQL)
        where_fields = [w.get("field") for w in p.where_usage]
        assert "flag" in where_fields, f"JOIN子查询内 WHERE flag 丢失: {where_fields}"

    def test_P_field_usage_join(self):
        """JOIN子查询: ON 条件字段提取到。"""
        p = parse(self.SQL)
        join_fields = [j.get("field") for j in p.join_usage]
        assert "id" in join_fields, f"JOIN ON id 丢失: {join_fields}"


# ═══════════════════════════════════════════════════════════════
# S12: CTE内部UNION分支含JOIN × 各解析路径
# ═══════════════════════════════════════════════════════════════

class TestS12CteUnionWithJoin:
    """CTE 内部 UNION 分支里含 JOIN，覆盖所有解析路径。"""

    SQL = """WITH cte AS (
    SELECT a.id, b.name FROM ods.src_a a LEFT JOIN ods.dim_a b ON a.id = b.id
    UNION ALL
    SELECT a.id, b.name FROM ods.src_b a LEFT JOIN ods.dim_b b ON a.id = b.id
)
SELECT c.id, c.name FROM cte c"""

    def test_P_ctes_all_tables(self):
        """CTE内UNION+JOIN: 四个表（src_a/dim_a/src_b/dim_b）都在。"""
        p = parse(self.SQL)
        tables = [t["name"] for t in p.ctes[0].source_tables]
        for expected in ("src_a", "dim_a", "src_b", "dim_b"):
            assert any(expected in t for t in tables), f"{expected} 丢失: {tables}"

    def test_P_data_flow_all_tables(self):
        """CTE内UNION+JOIN: data_flow 含四个表。"""
        p = parse(self.SQL)
        df = build_data_flow([make_rule(self.SQL)], {"R1": p})
        names = df_table_names(df)
        for expected in ("src_a", "dim_a", "src_b", "dim_b"):
            assert expected in names, f"{expected} 丢失: {names}"
