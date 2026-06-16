"""
pytest 测试：dws-pipeline-analyzer 案例库回归测试

测试模式：
- Golden File 对比（结构完整性）：每个 case 断言关键指标
- xfail 标记（已知能力缺口）

运行：
    pytest tests/test_analyzer.py -v
    pytest tests/test_analyzer.py -k case_02 -v  # 单个 case
"""

import sys
import json
from pathlib import Path

import pytest

# ── 路径配置 ──────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # skills 仓库根
ANALYZER_SKILL = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"

# 添加路径
sys.path.insert(0, str(ANALYZER_SKILL))
sys.path.insert(0, str(FIXTURES_DIR))
sys.path.insert(0, str(FIXTURES_DIR / "cases"))

from analyzer import (
    read_excel, detect_dialect, parse_single_sql,
    build_topology, build_data_flow, build_field_mappings, analyze_quality,
)
from _build_xlsx import build_xlsx


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture(scope="session")
def ensure_xlsx_generated():
    """确保所有 case 的 Excel 已生成（session 级缓存）。"""
    cases = [
        "case_01_minimal", "case_02_cte_basic", "case_03_cte_nested",
        "case_04_pivot", "case_05_window",
        "case_06_multi_step_parallel", "case_07_multi_step_serial",
        "case_08_self_reference", "case_09_same_target_writes", "case_10_view_step",
        "case_11_union_all", "case_12_oracle_dialect", "case_13_field_mismatch",
        "case_14_audit_fields", "case_15_comment_alias",
        "case_16_many_joins", "case_17_many_ctes", "case_18_many_case_when",
    "case_19_multi_scenario_2", "case_20_multi_scenario_3",
    "case_21_scenario_with_common", "case_22_scenario_chain",
    ]
    for case_name in cases:
        xlsx_path = FIXTURES_DIR / case_name / "execution_tasks.xlsx"
        if not xlsx_path.exists():
            mod = __import__(case_name)
            build_xlsx(
                str(xlsx_path),
                rules=mod.rules,
                target_fields=getattr(mod, "target_fields", None),
                group_variables=getattr(mod, "group_variables", None),
            )
    yield


def run_analysis(case_name: str) -> dict:
    """运行 analyzer 并返回完整结果。"""
    xlsx_path = FIXTURES_DIR / case_name / "execution_tasks.xlsx"
    raw = read_excel(str(xlsx_path))
    rules = raw["rules"]
    sql_texts = [r.query_sql for r in rules if r.query_sql]
    dialect = detect_dialect(sql_texts)

    parsed_map = {}
    for rule in rules:
        parsed_map[rule.rule_code] = parse_single_sql(rule.query_sql, dialect)

    topology = build_topology(rules, parsed_map)
    data_flow = build_data_flow(rules, parsed_map)
    field_mappings = build_field_mappings(rules, parsed_map, raw["target_fields"])
    quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)

    return {
        "raw": raw,
        "rules": rules,
        "dialect": dialect,
        "parsed_map": parsed_map,
        "topology": topology,
        "data_flow": data_flow,
        "field_mappings": field_mappings,
        "quality": quality,
    }


def get_transform_types(result: dict) -> dict:
    """提取 target_field → transform_type 映射。"""
    return {
        f["target_field"]: f.get("transform_type", "unknown")
        for f in result["field_mappings"]["fields"]
    }


# ═══════════════════════════════════════════════════════════════
# Tier 1: 基础覆盖
# ═══════════════════════════════════════════════════════════════

class TestTier1Basic:
    """Tier 1: 基础 happy path 验证。"""

    def test_case_01_minimal(self, ensure_xlsx_generated):
        """最简基线：1 step，5 字段全 direct，0 issues。"""
        r = run_analysis("case_01_minimal")
        assert len(r["topology"]["steps"]) == 1
        assert len(r["field_mappings"]["fields"]) == 5
        assert len(r["quality"]["issues"]) == 0

        tt = get_transform_types(r)
        for field, t in tt.items():
            assert t == "direct", f"{field} 应为 direct，实际 {t}"

    def test_case_02_cte_basic(self, ensure_xlsx_generated):
        """单层 CTE：1 个 CTE 被正确提取。"""
        r = run_analysis("case_02_cte_basic")
        ctes = r["data_flow"]["steps"][0]["ctes"]
        assert len(ctes) == 1, f"应有 1 个 CTE，实际 {len(ctes)}"
        assert ctes[0]["name"] == "agg"

        # CTE 内字段含 transform_type
        agg_fields = {f["name"]: f for f in ctes[0]["fields"]}
        assert "total" in agg_fields
        assert agg_fields["total"].get("transform_type") == "aggregate"

    def test_case_03_cte_nested(self, ensure_xlsx_generated):
        """嵌套 CTE：CTE_A 引用 CTE_B，穿透传播生效。"""
        r = run_analysis("case_03_cte_nested")
        ctes = r["data_flow"]["steps"][0]["ctes"]
        assert len(ctes) == 2, f"应有 2 个 CTE（base, agg），实际 {len(ctes)}"

        # 验证 total 字段穿透后 transform_type=aggregate（不是 direct）
        tt = get_transform_types(r)
        assert tt.get("total") == "aggregate", \
            f"total 应穿透为 aggregate（CTE 内 SUM），实际 {tt.get('total')}"

        # 验证穿透链：total 的 lineage 应有 cte_name
        total_field = next(
            (f for f in r["field_mappings"]["fields"] if f["target_field"] == "total"),
            None
        )
        assert total_field is not None
        lineages = total_field.get("lineage", [])
        cte_lineages = [l for l in lineages if l.get("cte_name")]
        assert len(cte_lineages) > 0, "total 字段应有 CTE 穿透信息"

    def test_case_04_pivot(self, ensure_xlsx_generated):
        """行转列：3 个字段 transform_type=pivot。"""
        r = run_analysis("case_04_pivot")
        tt = get_transform_types(r)
        for field in ("jan_amt", "feb_amt", "mar_amt"):
            assert tt.get(field) == "pivot", f"{field} 应为 pivot，实际 {tt.get(field)}"

        cw = r["quality"]["complexity_metrics"]["total_case_when_branches"]
        assert cw == 3, f"CASE WHEN 分支应为 3，实际 {cw}"

    def test_case_05_window(self, ensure_xlsx_generated):
        """窗口函数：rn 和 prev_login transform_type=window。"""
        r = run_analysis("case_05_window")
        tt = get_transform_types(r)
        assert tt.get("rn") == "window", f"rn 应为 window，实际 {tt.get('rn')}"
        assert tt.get("prev_login") == "window", f"prev_login 应为 window，实际 {tt.get('prev_login')}"


# ═══════════════════════════════════════════════════════════════
# Tier 2: 结构复杂度
# ═══════════════════════════════════════════════════════════════

class TestTier2Structure:
    """Tier 2: 多步骤拓扑结构验证。"""

    def test_case_06_multi_step_parallel(self, ensure_xlsx_generated):
        """多步骤并行：2 个 rule exec_sequence 相同。"""
        r = run_analysis("case_06_multi_step_parallel")
        assert len(r["topology"]["steps"]) == 2
        parallel = r["topology"]["schedule_plan"][0]["parallel_steps"]
        assert len(parallel) == 2, f"应有 2 个并行步骤，实际 {len(parallel)}"

    def test_case_07_multi_step_serial(self, ensure_xlsx_generated):
        """多步骤串行：跨步骤数据依赖。"""
        r = run_analysis("case_07_multi_step_serial")
        deps = r["topology"]["data_dependencies"]
        assert len(deps) >= 1, "应有跨步骤数据依赖"
        # step_1 → step_2
        assert any(d["from"] == "step_1" and d["to"] == "step_2" for d in deps), \
            f"应存在 step_1→step_2 依赖，实际 {deps}"

    def test_case_08_self_reference(self, ensure_xlsx_generated):
        """自引用：WHERE EXISTS(SELECT 1 FROM 目标表)。"""
        r = run_analysis("case_08_self_reference")
        self_refs = r["topology"]["self_references"]
        assert len(self_refs) >= 1, "应检测到自引用"
        assert "EXISTS" in self_refs[0].get("pattern", "").upper(), \
            f"自引用模式应含 EXISTS，实际 {self_refs[0].get('pattern')}"

    def test_case_09_same_target_writes(self, ensure_xlsx_generated):
        """同表多次写入：target_write_groups 非空。"""
        r = run_analysis("case_09_same_target_writes")
        groups = r["topology"]["target_write_groups"]
        assert len(groups) >= 1, "应检测到同表多次写入组"

    def test_case_10_view_step(self, ensure_xlsx_generated):
        """视图步骤：CREATE VIEW + 跨步骤依赖。"""
        r = run_analysis("case_10_view_step")
        assert len(r["topology"]["steps"]) == 2
        # step_2 (view) 应依赖 step_1 (table)
        deps = r["topology"]["data_dependencies"]
        assert any(d["from"] == "step_1" and d["to"] == "step_2" for d in deps), \
            "视图步骤应依赖事实表步骤"


# ═══════════════════════════════════════════════════════════════
# Tier 3: 边界场景
# ═══════════════════════════════════════════════════════════════

class TestTier3Boundary:
    """Tier 3: 边界场景和已知缺口。"""

    def test_case_11_union_all(self, ensure_xlsx_generated):
        """UNION ALL：两个分支的 source_tables 都应被提取。"""
        r = run_analysis("case_11_union_all")
        
        # source_tables 应包含两个分支的表
        step1 = r["topology"]["steps"][0]
        all_tables = step1.get("all_tables_from_sql", [])
        assert "ods.orders_a" in all_tables, f"应包含 orders_a，实际 {all_tables}"
        assert "ods.orders_b" in all_tables, f"应包含 orders_b，实际 {all_tables}"

        # 字段以第一个分支为准，3 列
        fields = r["field_mappings"]["fields"]
        field_names = {f["target_field"] for f in fields}
        assert "order_id" in field_names
        assert "source" in field_names
        assert "amount" in field_names

    def test_case_12_oracle_dialect(self, ensure_xlsx_generated):
        """Oracle 方言：NVL 检测。"""
        r = run_analysis("case_12_oracle_dialect")
        assert r["dialect"] == "oracle", f"应检测为 oracle 方言，实际 {r['dialect']}"

        tt = get_transform_types(r)
        # NVL(x, 0) 应识别为 fallback
        assert tt.get("price") == "fallback", f"price (NVL) 应为 fallback，实际 {tt.get('price')}"

    def test_case_13_field_mismatch(self, ensure_xlsx_generated):
        """字段别名不匹配：differences 应非空。"""
        r = run_analysis("case_13_field_mismatch")
        stats = r["field_mappings"]["statistics"]
        # 应有 only_in_excel（extra_field）或 only_in_sql
        diff_count = len(stats.get("only_in_sql", [])) + len(stats.get("only_in_excel", []))
        assert diff_count > 0, "应检测到字段差异"

    def test_case_14_audit_fields(self, ensure_xlsx_generated):
        """审计字段推断：'N'→del_flag, CURRENT_TIMESTAMP→dw_last_update_date。"""
        r = run_analysis("case_14_audit_fields")
        tt = get_transform_types(r)
        assert "del_flag" in tt, "应推断出 del_flag 字段名"
        assert "dw_last_update_date" in tt, "应推断出 dw_last_update_date 字段名"
        assert tt["del_flag"] == "value", f"del_flag 应为 value，实际 {tt['del_flag']}"

    def test_case_15_comment_alias(self, ensure_xlsx_generated):
        """注释别名提取：/* field_name */ 格式。"""
        r = run_analysis("case_15_comment_alias")
        tt = get_transform_types(r)
        # 注释中的字段名应被提取
        assert "del_flag" in tt, "应从注释提取 del_flag"
        assert "dw_last_update_date" in tt, "应从注释提取 dw_last_update_date"


# ═══════════════════════════════════════════════════════════════
# Tier 4: 性能阈值
# ═══════════════════════════════════════════════════════════════

class TestTier4Performance:
    """Tier 4: 性能阈值告警。"""

    def test_case_16_many_joins(self, ensure_xlsx_generated):
        """多表 JOIN（9张）：触发 medium 性能告警。"""
        r = run_analysis("case_16_many_joins")
        cm = r["quality"]["complexity_metrics"]
        assert cm["max_join_count"] >= 9, f"JOIN 数应 ≥9，实际 {cm['max_join_count']}"

        medium_issues = [i for i in r["quality"]["issues"] if i["severity"] == "medium"]
        assert len(medium_issues) >= 1, "应触发 medium 级 JOIN 过多告警"

    def test_case_17_many_ctes(self, ensure_xlsx_generated):
        """多 CTE（4个）：触发 medium 复杂度告警。"""
        r = run_analysis("case_17_many_ctes")
        cm = r["quality"]["complexity_metrics"]
        assert cm["max_cte_count"] >= 4, f"CTE 数应 ≥4，实际 {cm['max_cte_count']}"

        medium_issues = [i for i in r["quality"]["issues"] if i["severity"] == "medium"]
        assert len(medium_issues) >= 1, "应触发 medium 级 CTE 嵌套过深告警"

    def test_case_18_many_case_when(self, ensure_xlsx_generated):
        """多 CASE WHEN（21个）：触发 medium 复杂度告警。"""
        r = run_analysis("case_18_many_case_when")
        cm = r["quality"]["complexity_metrics"]
        assert cm["total_case_when_branches"] >= 21, \
            f"CASE WHEN 分支应 ≥21，实际 {cm['total_case_when_branches']}"

        medium_issues = [i for i in r["quality"]["issues"] if i["severity"] == "medium"]
        assert len(medium_issues) >= 1, "应触发 medium 级 CASE WHEN 过多告警"


# ═══════════════════════════════════════════════════════════════
# Tier 5: 多场景综合
# ═══════════════════════════════════════════════════════════════

class TestTier5MultiScenario:
    """多场景分区写入、场景+公共混合、串行依赖链。"""

    def test_case_19_multi_scenario_2(self, ensure_xlsx_generated):
        """2场景分区写入 + 公共步骤：场景数=3。"""
        r = run_analysis("case_19_multi_scenario_2")
        scenarios = r["topology"]["scenarios"]
        # 2个分区场景 + 1个公共步骤
        non_common = [s for s in scenarios if not s.get("is_common")]
        common = [s for s in scenarios if s.get("is_common")]
        assert len(non_common) == 2, f"应有2个分区场景，实际 {len(non_common)}"
        assert len(common) == 1, f"应有1个公共步骤，实际 {len(common)}"
        # 每个分区场景含2个规则
        for sc in non_common:
            assert sc["rule_count"] == 2, f"场景 {sc['name']} 应含2个规则"

    def test_case_20_multi_scenario_3(self, ensure_xlsx_generated):
        """3场景分区写入：场景数=3。"""
        r = run_analysis("case_20_multi_scenario_3")
        scenarios = r["topology"]["scenarios"]
        non_common = [s for s in scenarios if not s.get("is_common")]
        assert len(non_common) == 3, f"应有3个分区场景，实际 {len(non_common)}"

    def test_case_21_scenario_with_common(self, ensure_xlsx_generated):
        """场景+公共混合：2个分区场景 + 1个公共步骤。"""
        r = run_analysis("case_21_scenario_with_common")
        scenarios = r["topology"]["scenarios"]
        non_common = [s for s in scenarios if not s.get("is_common")]
        common = [s for s in scenarios if s.get("is_common")]
        assert len(non_common) == 2
        assert len(common) == 1
        # 验证公共步骤的删除模式
        common_step = next(s for s in r["topology"]["steps"] if s.get("is_common_step"))
        assert common_step["delete_mode_label"] == "TRUNCATE TABLE"

    def test_case_22_scenario_chain(self, ensure_xlsx_generated):
        """单场景3步串行：TRUNCATE → APPEND → APPEND。"""
        r = run_analysis("case_22_scenario_chain")
        steps = r["topology"]["steps"]
        assert len(steps) == 3
        # 第1步 TRUNCATE TABLE
        assert steps[0]["delete_mode_label"] == "TRUNCATE TABLE"
        # 第2/3步 NO DELETE
        assert steps[1]["delete_mode_label"] == "NO DELETE (追加)"
        assert steps[2]["delete_mode_label"] == "NO DELETE (追加)"
        # 同目标表写入组（3个规则写同一张表）
        write_groups = r["topology"]["target_write_groups"]
        assert len(write_groups) >= 1, "应检测到同表多次写入组"
