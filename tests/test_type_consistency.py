"""跨表字段类型一致性检查测试（P3）。

验证 check_type_consistency 能检出过程表→目标表的类型/精度不一致，
以及容错场景（无 DDL 不检查）。

运行:
    pytest tests/test_type_consistency.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from engine import RawRule, analyze_pipeline, detect_dialect


def _run_analysis(rules, ddl_contents, tmp_path):
    """辅助：跑完整分析（rules + DDL），返回 knowledge。"""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    for table, sql in ddl_contents.items():
        (ddl_dir / f"{table}.sql").write_text(sql, encoding="utf-8")
    dialect = detect_dialect([r.query_sql for r in rules])
    kj, _ = analyze_pipeline(rules, {}, {}, dialect, ddl_dir=str(ddl_dir))
    return kj


def _type_issues(kj):
    """从 knowledge 提取 type_consistency 类的 issues。"""
    return [i for i in kj["quality"]["issues"] if i.get("category") == "type_consistency"]


class TestTypeConsistencyDetection:
    """类型不一致检出。"""

    def test_precision_loss_detected(self, tmp_path):
        """DECIMAL(18,4) → DECIMAL(18,2) 精度丢失应检出（high）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.amount FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT SUM(t.amount) AS total FROM dws.tmp_t t"),
        ]
        ddls = {
            "tmp_t": "CREATE TABLE tmp_t (amount DECIMAL(18,4) NOT NULL);",
            "final_t": "CREATE TABLE final_t (total DECIMAL(18,2));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)

        assert len(issues) >= 1, "应检出精度不一致"
        iss = issues[0]
        assert iss["severity"] == "high"
        # 描述里明确指出两边表名和类型
        assert iss["current_table"], "应指明当前表"
        assert iss["source_table"], "应指明来源表"
        assert "DECIMAL(18,2)" in iss["current_type"]
        assert "DECIMAL(18,4)" in iss["source_type"]
        assert iss["source_step"], "应指明来源步骤"
        assert iss["current_step"], "应指明当前步骤"
        assert iss["mismatch_kind"] == "精度不一致"
        # title 里含两边表名
        assert iss["source_table"] in iss["title"]
        assert iss["current_table"] in iss["title"]

    def test_length_truncation_detected(self, tmp_path):
        """VARCHAR(128) → VARCHAR(64) 长度截断应检出。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.name FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT t.name FROM dws.tmp_t t"),
        ]
        ddls = {
            "tmp_t": "CREATE TABLE tmp_t (name VARCHAR(128));",
            "final_t": "CREATE TABLE final_t (name VARCHAR(64));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)

        assert len(issues) >= 1, "应检出长度不一致"
        assert issues[0]["severity"] == "high"

    def test_type_family_change_detected(self, tmp_path):
        """VARCHAR → INT 类型族变化应检出（high）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.code FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT t.code FROM dws.tmp_t t"),
        ]
        ddls = {
            "tmp_t": "CREATE TABLE tmp_t (code VARCHAR(32));",
            "final_t": "CREATE TABLE final_t (code INT);",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)

        assert len(issues) >= 1, "应检出类型族不一致"
        assert issues[0]["severity"] == "high"

    def test_consistent_types_no_issue(self, tmp_path):
        """类型一致时不应报 type_consistency issue。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.amount FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT t.amount FROM dws.tmp_t t"),
        ]
        ddls = {
            "tmp_t": "CREATE TABLE tmp_t (amount DECIMAL(18,2));",
            "final_t": "CREATE TABLE final_t (amount DECIMAL(18,2));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)

        assert len(issues) == 0, "类型一致不应报 issue"


class TestTypeConsistencyFallback:
    """容错：无 DDL 不检查，不阻塞。"""

    def test_no_ddl_no_check(self, tmp_path):
        """没有 DDL 时，不检查类型一致性（不报错，不阻塞）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.amount FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT t.amount FROM dws.tmp_t t"),
        ]
        dialect = detect_dialect([r.query_sql for r in rules])
        # 不传 ddl_dir
        kj, _ = analyze_pipeline(rules, {}, {}, dialect, ddl_dir="")

        issues = _type_issues(kj)
        assert len(issues) == 0, "无 DDL 不应检查类型一致性"
        # 分析正常完成
        assert kj["meta"]["target_table"] == "final_t"

    def test_partial_ddl_only_checks_available(self, tmp_path):
        """只有部分表有 DDL 时，只检查有 DDL 的（其他跳过）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t",
                    query_sql="SELECT a.amount FROM ods.src a"),
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT t.amount FROM dws.tmp_t t"),
        ]
        # 只有 final_t 的 DDL（tmp_t 没有），lineage 指向 tmp_t 但 catalog 没它 → 不报
        ddls = {"final_t": "CREATE TABLE final_t (amount DECIMAL(18,2));"}
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        # tmp_t 没 DDL，无法对比 → 不报（不是漏报，是确实没数据可比）
        assert len(issues) == 0


# ═══════════════════════════════════════════════════════════════
# 类型检查范围控制（哪些加工类型该查/不该查）
# ═══════════════════════════════════════════════════════════════

class TestTypeCheckScope:
    """类型一致性检查的范围：只查 direct + SUM/MIN/MAX，跳过 case_when/COUNT/expression。

    背景：CASE WHEN flag=1 THEN 'Y' END AS del_flag，拿条件里的 flag(int)
    跟输出 del_flag(varchar) 比类型是错的——输出类型由 SQL 语义决定，跟源字段无关。
    """

    def test_case_when_not_checked(self, tmp_path):
        """case_when 加工的字段不做类型对比（条件字段类型跟输出无关）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT CASE WHEN a.flag = 1 THEN 'Y' ELSE 'N' END AS del_flag FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (flag INT NOT NULL);",
            "final_t": "CREATE TABLE final_t (del_flag VARCHAR(1));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        # flag(int) → del_flag(varchar) 是 case_when 语义决定，不该报
        assert len(issues) == 0, f"case_when 不该报类型不一致: {issues}"

    def test_count_not_checked(self, tmp_path):
        """COUNT 聚合不做类型对比（输出恒为 bigint，跟源类型无关）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT COUNT(*) AS cnt FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (cnt BIGINT);",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) == 0, f"COUNT 不该报类型不一致: {issues}"

    def test_sum_precision_checked(self, tmp_path):
        """SUM 精度丢失该报（SUM不改基础类型，精度丢失是真问题）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT SUM(a.amount) AS total FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (amount DECIMAL(18,4));",
            "final_t": "CREATE TABLE final_t (total DECIMAL(18,2));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) >= 1, "SUM 精度丢失应该报"

    def test_expression_not_checked(self, tmp_path):
        """表达式加工（a||b）不做类型对比（运算后类型变了）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT (a.first_name || a.last_name) AS full_name FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (first_name VARCHAR(20), last_name VARCHAR(20));",
            "final_t": "CREATE TABLE final_t (full_name VARCHAR(100));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        # 表达式输出的类型跟源字段无关，不该报
        assert len(issues) == 0, f"表达式不该报类型不一致: {issues}"


# ═══════════════════════════════════════════════════════════════
# 精细化检查（value/cast/case_when分支）
# ═══════════════════════════════════════════════════════════════

class TestTypeCheckRefined:
    """精细化类型检查：value查常量、cast查目标类型、case_when查分支字段。"""

    def test_value_constant_too_long(self, tmp_path):
        """常量赋值超长该报（'UNKNOWN_STATUS' → varchar(1)）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT 'UNKNOWN_STATUS' AS flag FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (flag VARCHAR(1));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) >= 1, "常量超长应该报"

    def test_value_constant_fits(self, tmp_path):
        """常量赋值能塞进目标不报（'N' → varchar(1)）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT 'N' AS flag FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (flag VARCHAR(1));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) == 0, f"'N'能塞进varchar(1)，不该报: {issues}"

    def test_cast_target_matches(self, tmp_path):
        """CAST目标类型跟DDL一致不报（cast(x as bigint) → bigint）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT CAST(a.uid AS BIGINT) AS uid_big FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (uid INT);",
            "final_t": "CREATE TABLE final_t (uid_big BIGINT);",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) == 0, f"cast bigint → bigint 不该报: {issues}"

    def test_cast_target_mismatch(self, tmp_path):
        """CAST目标类型跟DDL不一致该报（cast(x as varchar(5)) → varchar(1)）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT CAST(a.code AS VARCHAR(5)) AS short_code FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (code INT);",
            "final_t": "CREATE TABLE final_t (short_code VARCHAR(1));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) >= 1, "cast varchar(5) → varchar(1) 该报截断"

    def test_case_when_branch_field_checked(self, tmp_path):
        """case_when 的 THEN/ELSE 分支字段参与检查，WHEN条件字段不参与。"""
        # THEN 分支取 src.name(varchar50)，目标 varchar(50) 一致 → 不报
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT CASE WHEN a.status = 1 THEN a.name ELSE '未知' END AS name_out FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (status INT, name VARCHAR(50));",
            "final_t": "CREATE TABLE final_t (name_out VARCHAR(50));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        # status(int) 是 WHEN 条件，不该报；name(varchar50) 一致也不报
        assert len(issues) == 0, f"case_when 条件字段status不该报，分支name一致也不报: {issues}"

    def test_int_family_compatible(self, tmp_path):
        """整数家族互转不报（int → bigint）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT a.id AS out_id FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (out_id BIGINT);",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        # int → bigint 是安全扩大，不该报
        assert len(issues) == 0, f"int→bigint整数家族兼容不该报: {issues}"


# ═══════════════════════════════════════════════════════════════
# 类型兼容判定（与影响分析同口径：源能被目标冗余兜底就不报）
# ═══════════════════════════════════════════════════════════════

class TestTypeCompatUnified:
    """类型兼容判定两边统一：engine.check_type_consistency 和 impact_analyzer._assess_type_change 同口径。

    核心规则：源类型能被目标冗余兜底（不丢数据）就不报。
    - 整数家族互转（int/bigint/smallint）
    - integer → numeric 安全跨类（目标精度够容纳）
    - 同家族目标更宽
    """

    def test_int_to_numeric_compatible(self, tmp_path):
        """int → numeric(18,2) 该不报（整数可安全转数值，目标精度够）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT a.id AS out_id FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (out_id NUMERIC(18,2));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) == 0, f"int→numeric(18,2) 安全跨类不该报: {issues}"

    def test_int_to_numeric_precision_too_small(self, tmp_path):
        """int → numeric(2,0) 该报（目标精度装不下整数）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT a.id AS out_id FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (out_id NUMERIC(2,0));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) >= 1, "int→numeric(2,0) 精度不够应该报"

    def test_value_constant_type_consistency_field_present(self, tmp_path):
        """value 路径的 issue 含 source_table 字段（高亮渲染统一）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="final_t",
                    query_sql="SELECT 'UNKNOWN_STATUS' AS flag FROM ods.src_a a"),
        ]
        ddls = {
            "src_a": "CREATE TABLE src_a (id INT);",
            "final_t": "CREATE TABLE final_t (flag VARCHAR(1));",
        }
        kj = _run_analysis(rules, ddls, tmp_path)
        issues = _type_issues(kj)
        assert len(issues) >= 1
        # value 路径也要有 source_table（HTML 高亮判定需要）
        assert issues[0].get("source_table"), f"value issue 缺 source_table: {issues[0]}"
