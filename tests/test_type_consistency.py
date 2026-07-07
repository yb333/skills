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
        assert issues[0]["severity"] == "high"
        assert "DECIMAL(18,2)" in issues[0]["current_type"]
        assert "DECIMAL(18,4)" in issues[0]["source_type"]

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
