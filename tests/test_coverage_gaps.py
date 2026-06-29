"""P2: 测试覆盖盲区补充。

覆盖之前排查发现的盲区：
- enrich_* 穿透增强
- Oracle (+) 外连接
- 多列 SQL 拼接
- SELECT * 预警
- 空字段映射 / SQL 解析失败

运行:
    pytest tests/test_coverage_gaps.py -v
"""

import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from analyzer import (
    parse_single_sql, detect_dialect, build_topology, build_data_flow,
    build_field_mappings, analyze_quality, enrich_join_key_lineage,
    enrich_field_physical_sources, RawRule,
)


# ═══════════════════════════════════════════════════════════════
# 1. enrich_* 穿透增强
# ═══════════════════════════════════════════════════════════════

class TestEnrichPenetration:
    """enrich_join_key_lineage + enrich_field_physical_sources 端到端验证。"""

    def test_enrich_join_key_lineage_injects_trace(self):
        """enrich 后 data_flow step 应含 join_key_lineage"""
        rules = [
            RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp1", delete_mode="1",
                    query_sql="SELECT a.id, (a.code || b.seq) AS bid FROM ods.t1 a LEFT JOIN ods.t2 b ON a.k=b.k"),
            RawRule(rule_code="R2", rule_name="s2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_f", delete_mode="1",
                    query_sql="SELECT t.id, d.name FROM dws.tmp1 t LEFT JOIN ods.dim d ON t.bid = d.bid"),
        ]
        pm = {r.rule_code: parse_single_sql(r.query_sql, "dws") for r in rules}
        topo = build_topology(rules, pm)
        df = build_data_flow(rules, pm)
        fm = build_field_mappings(rules, pm, {})
        enrich_join_key_lineage(df, rules, pm, topo, fm)

        step2 = df["steps"][1]
        jkl = step2.get("join_key_lineage", {})
        assert jkl, f"step_2 应含 join_key_lineage，实际空"

    def test_enrich_field_physical_sources_injects(self):
        """enrich 后字段应含 physical_source"""
        rules = [
            RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp1", delete_mode="1",
                    query_sql="SELECT a.id, a.amount FROM ods.t1 a"),
            RawRule(rule_code="R2", rule_name="s2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_f", delete_mode="1",
                    query_sql="SELECT t.id, t.amount FROM dws.tmp1 t"),
        ]
        pm = {r.rule_code: parse_single_sql(r.query_sql, "dws") for r in rules}
        topo = build_topology(rules, pm)
        df = build_data_flow(rules, pm)
        fm = build_field_mappings(rules, pm, {})
        enrich_field_physical_sources(fm, df, rules, pm, topo)

        for f in fm["fields"]:
            if f["target_field"] == "amount" and f.get("producing_step") == "step_2":
                ps = f.get("physical_source", [])
                assert ps, "amount 应有 physical_source"
                assert any("t1" in p.get("table", "") for p in ps), \
                    f"应穿透到 t1，实际 {ps}"


# ═══════════════════════════════════════════════════════════════
# 2. Oracle (+) 外连接
# ═══════════════════════════════════════════════════════════════

class TestOracleOuterJoin:
    """Oracle (+) 外连接语法解析 + 质量预警。"""

    def test_plus_join_usage_detected(self):
        """(+) 关联条件应进 join_usage（不是 where_usage）"""
        sql = "SELECT a.id, b.name FROM ods.t1 a, ods.t2 b WHERE a.id = b.id(+)"
        p = parse_single_sql(sql, "oracle")
        join_fields = [ju["field"] for ju in p.join_usage]
        where_fields = [wu["field"] for wu in p.where_usage]
        assert "id" in join_fields, f"id 应在 join_usage（(+) 关联），实际 join={join_fields}"
        assert "id" not in where_fields or "name" not in where_fields, \
            f"(+) 条件不应全进 where_usage"

    def test_plus_join_quality_warning(self):
        """(+) 语法应在质量评估里报 code_quality"""
        sql = "SELECT a.id, b.name FROM ods.t1 a, ods.t2 b WHERE a.id = b.id(+)"
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1", query_sql=sql)
        pm = {"R1": parse_single_sql(sql, "oracle")}
        topo = build_topology([rule], pm)
        df = build_data_flow([rule], pm)
        fm = build_field_mappings([rule], pm, {})
        q = analyze_quality(topo, df, fm, pm)
        plus_issues = [i for i in q["issues"] if "(+)" in i.get("title", "")]
        assert len(plus_issues) >= 1, f"应报 (+) 语法告警"

    def test_plus_no_join_missing_on_false_positive(self):
        """(+) 关联的表不应报'JOIN 缺少 ON 条件'"""
        sql = "SELECT a.id, b.name FROM ods.t1 a, ods.t2 b WHERE a.id = b.id(+)"
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1", query_sql=sql)
        pm = {"R1": parse_single_sql(sql, "oracle")}
        topo = build_topology([rule], pm)
        df = build_data_flow([rule], pm)
        fm = build_field_mappings([rule], pm, {})
        q = analyze_quality(topo, df, fm, pm)
        missing_on = [i for i in q["issues"] if "JOIN 缺少 ON" in i.get("title", "")]
        assert len(missing_on) == 0, f"(+) 关联不应报缺 ON，实际 {missing_on}"


# ═══════════════════════════════════════════════════════════════
# 3. 多列 SQL 拼接
# ═══════════════════════════════════════════════════════════════

class TestMultiColumnSQL:
    """多列查询语句拼接测试。"""

    def test_multi_column_concatenation(self, tmp_path):
        """超长 SQL 分散在多列，拼接后应完整"""
        import openpyxl
        from analyzer import read_excel, _read_query_sql

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "RULE"
        ws.append(["执行序列", "规则类型", "目标Schema", "目标表",
                    "(生成的）查询语句1", "(生成的）查询语句2", "规则编码", "规则中文名称"])
        ws.append([1, 1, "dws", "f",
                   "SELECT a.id,\r\n",
                   "       a.name FROM ods.t1 a\r\n",
                   "R1", "测试"])
        xlsx = str(tmp_path / "multi.xlsx")
        wb.save(xlsx)
        wb.close()

        raw = read_excel(xlsx)
        sql = raw["rules"][0].query_sql
        assert "SELECT a.id" in sql, f"拼接应含 SELECT，实际 {sql!r}"
        assert "a.name FROM ods.t1" in sql, f"拼接应含 FROM，实际 {sql!r}"
        assert "ods.t1" in sql, f"拼接应含表名，实际 {sql!r}"


# ═══════════════════════════════════════════════════════════════
# 4. SELECT * 预警
# ═══════════════════════════════════════════════════════════════

class TestSelectStarWarning:
    """SELECT * 检测 + 质量预警。"""

    def test_select_star_detected(self):
        """SELECT * 应标记 has_star"""
        p = parse_single_sql("SELECT * FROM ods.t1 a", "oracle")
        assert p.has_star, "SELECT * 应标记 has_star"

    def test_partial_star_detected(self):
        """SELECT a.* 应标记 has_star"""
        p = parse_single_sql("SELECT a.id, b.* FROM ods.t1 a LEFT JOIN ods.t2 b ON a.id=b.id", "oracle")
        assert p.has_star, "b.* 应标记 has_star"

    def test_no_star_not_detected(self):
        """正常 SELECT 不应标记 has_star"""
        p = parse_single_sql("SELECT a.id, a.name FROM ods.t1 a", "oracle")
        assert not p.has_star, "正常 SELECT 不应标记 has_star"

    def test_select_star_quality_warning(self):
        """SELECT * 应在质量评估报 critical"""
        rule = RawRule(rule_code="R1", rule_name="star", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1",
                       query_sql="SELECT * FROM ods.t1 a")
        pm = {"R1": parse_single_sql(rule.query_sql, "oracle")}
        topo = build_topology([rule], pm)
        df = build_data_flow([rule], pm)
        fm = build_field_mappings([rule], pm, {})
        q = analyze_quality(topo, df, fm, pm)
        star_issues = [i for i in q["issues"] if "SELECT *" in i.get("title", "")]
        assert len(star_issues) >= 1, f"应报 SELECT * 告警"
        assert star_issues[0]["severity"] == "critical"


# ═══════════════════════════════════════════════════════════════
# 5. SQL 解析失败提示
# ═══════════════════════════════════════════════════════════════

class TestParseErrorWarning:
    """SQL 解析失败的质量预警。"""

    def test_parse_error_quality_warning(self):
        """解析失败应在质量评估报 critical"""
        rule = RawRule(rule_code="R1", rule_name="bad", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1",
                       query_sql="这不是SQL (((( ")
        pm = {"R1": parse_single_sql(rule.query_sql, "oracle")}
        topo = build_topology([rule], pm)
        df = build_data_flow([rule], pm)
        fm = build_field_mappings([rule], pm, {})
        q = analyze_quality(topo, df, fm, pm)
        parse_issues = [i for i in q["issues"] if "解析失败" in i.get("title", "")]
        assert len(parse_issues) >= 1, f"应报解析失败告警"
        assert parse_issues[0]["severity"] == "critical"


# ═══════════════════════════════════════════════════════════════
# 6. 字面量字段不报"无别名前缀"
# ═══════════════════════════════════════════════════════════════

class TestLiteralFieldNoAliasWarning:
    """字面量字段（value/expression）不应报"无别名前缀"。"""

    def test_literal_field_no_false_positive(self):
        """'N' AS del_flag 不应报无别名"""
        sql = "SELECT 'N' AS del_flag, CURRENT_TIMESTAMP AS ts, a.id FROM ods.t a"
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="f", delete_mode="1", query_sql=sql)
        pm = {"R1": parse_single_sql(sql, "dws")}
        topo = build_topology([rule], pm)
        df = build_data_flow([rule], pm)
        fm = build_field_mappings([rule], pm, {})
        q = analyze_quality(topo, df, fm, pm)
        alias_issues = [i for i in q["issues"] if "无别名" in i.get("title", "")]
        assert len(alias_issues) == 0, f"字面量字段不应报无别名，实际 {alias_issues}"


# ═══════════════════════════════════════════════════════════════
# 7. 嵌套子查询 JOIN/WHERE 提取
# ═══════════════════════════════════════════════════════════════

class TestNestedSubqueryUsage:
    """嵌套子查询内部的 JOIN ON 和 WHERE 条件应正确提取到 join_usage/where_usage。"""

    NESTED_SQL = """SELECT t.region, t.total_amt
FROM (
    SELECT m.region, SUM(m.amt) AS total_amt
    FROM (
        SELECT a.region, a.amt, a.cust_id
        FROM ods.fact_a a
        INNER JOIN ods.dim_b b ON a.cust_id = b.cust_id
        INNER JOIN ods.dim_c c ON a.cat_id = c.cat_id
        WHERE a.del_flag = 'N'
    ) m
    INNER JOIN (
        SELECT d.cust_id, COUNT(*) AS cnt
        FROM ods.fact_d d
        LEFT JOIN ods.dim_e e ON d.cust_id = e.cust_id
        WHERE d.sts = 'A'
    ) s ON m.cust_id = s.cust_id
) t
WHERE t.total_amt > 0"""

    def test_inner_join_conditions_extracted(self):
        """内层 JOIN 的 ON 条件应出现在 join_usage"""
        p = parse_single_sql(self.NESTED_SQL, "dws")
        join_fields = {ju["field"] for ju in p.join_usage}
        join_conds = [ju.get("on_condition", "") for ju in p.join_usage]
        # cust_id 出现在多层 JOIN 里
        assert "cust_id" in join_fields, f"cust_id 应在 join_usage，实际 {join_fields}"
        # cat_id 只在内层 JOIN
        assert "cat_id" in join_fields, f"cat_id 应在 join_usage（内层），实际 {join_fields}"
        # 确认 ON 条件文本
        assert any("b.cust_id" in c for c in join_conds), f"应含 a.cust_id=b.cust_id"

    def test_inner_where_extracted(self):
        """内层 WHERE 条件应出现在 where_usage"""
        p = parse_single_sql(self.NESTED_SQL, "dws")
        where_fields = {wu["field"] for wu in p.where_usage}
        assert "del_flag" in where_fields, f"del_flag 应在 where_usage（内层），实际 {where_fields}"
        assert "sts" in where_fields, f"sts 应在 where_usage（内层），实际 {where_fields}"
        assert "total_amt" in where_fields, f"total_amt 应在 where_usage（外层）"

    def test_no_duplicate_usage(self):
        """不应有重复的 JOIN/WHERE 条件"""
        p = parse_single_sql(self.NESTED_SQL, "dws")
        # 每个唯一的 ON 条件只出现一次
        on_conds = [ju.get("on_condition", "") for ju in p.join_usage]
        assert len(on_conds) == len(set(on_conds)), f"JOIN 条件有重复: {on_conds}"
