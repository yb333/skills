"""
端到端用例 — 合成 Excel → 完整 analyzer + view_generator → 三视图产物验证

用 A 类用例的 case 定义动态生成 Excel，不依赖外部文件。

运行:
    pytest tests/test_end_to_end.py -v
"""

import sys
import json
import shutil
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(FIXTURES / "cases"))

from analyzer import (
    read_excel, detect_dialect, parse_single_sql,
    build_topology, build_data_flow, build_field_mappings, analyze_quality,
    detect_patterns, build_source, generate_step_description,
)
from view_generator import generate_mapping, generate_asset_report, generate_tech_design
from _build_xlsx import build_xlsx


def run_full_analysis(xlsx_path, output_dir):
    """完整分析流程"""
    from datetime import datetime
    raw = read_excel(xlsx_path)
    rules = raw["rules"]
    dialect = detect_dialect([r.query_sql for r in rules if r.query_sql])
    parsed_map = {r.rule_code: parse_single_sql(r.query_sql, dialect) for r in rules}
    topology = build_topology(rules, parsed_map)
    data_flow = build_data_flow(rules, parsed_map)
    field_mappings = build_field_mappings(rules, parsed_map, raw["target_fields"])
    quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)
    patterns = detect_patterns(parsed_map, topology)
    scenarios = topology.get("scenarios", [])
    auto_step_desc = []
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code)
        desc = generate_step_description(rule, parsed, scenarios, rules)
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        auto_step_desc.append({"step_id": step["step_id"] if step else "", "rule_code": rule.rule_code, "purpose": desc["purpose"], "logic": desc["logic"]})
    knowledge = {"meta": {"source_type": "execution_tasks.xlsx", "analysis_time": datetime.now().isoformat(), "dialect": dialect, "total_rules": len(rules), "target_table": rules[0].target_table or "", "patterns": patterns, "target_field_types": {}, "target_field_comments": {}}, "topology": topology, "data_flow": data_flow, "field_mappings": field_mappings, "quality": quality, "business_logic": {"summary": "", "step_descriptions": auto_step_desc, "key_transforms": []}, "source": build_source(rules, raw["target_fields"], raw["group_variables"], parsed_map)}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "knowledge_final.json").write_text(json.dumps(knowledge, ensure_ascii=False, indent=2))
    results = {}
    results["mapping"] = generate_mapping(knowledge, str(out))
    results["asset"] = generate_asset_report(knowledge, str(out))
    results["techspec"] = generate_tech_design(knowledge, str(out))
    return knowledge, results


class TestEndToEnd:

    @pytest.fixture
    def tmp_output(self):
        d = tempfile.mkdtemp()
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def _make_xlsx(self, case_name, tmp_dir):
        mod = __import__(case_name)
        xlsx_path = Path(tmp_dir) / f"{case_name}.xlsx"
        build_xlsx(str(xlsx_path), rules=mod.rules, target_fields=getattr(mod, "target_fields", None), group_variables=getattr(mod, "group_variables", None))
        return str(xlsx_path)

    def test_cte_pivot_e2e(self, tmp_output):
        """CTE场景：三视图全部生成成功。"""
        xlsx = self._make_xlsx("case_02_cte_basic", tmp_output)
        knowledge, results = run_full_analysis(xlsx, tmp_output)
        assert knowledge is not None
        assert results["mapping"] == True
        assert results["asset"] == True
        assert results["techspec"] == True
        assert (Path(tmp_output) / "mapping.xlsx").exists()
        assert (Path(tmp_output) / "asset_report.html").exists()
        assert (Path(tmp_output) / "tech_design.md").exists()
        html = (Path(tmp_output) / "asset_report.html").read_text()
        assert "REPORT_DATA" in html
        assert len(html) > 5000

    def test_multi_scenario_e2e(self, tmp_output):
        """多场景：场景分组正确，三视图生成。"""
        xlsx = self._make_xlsx("case_19_multi_scenario_2", tmp_output)
        knowledge, results = run_full_analysis(xlsx, tmp_output)
        assert knowledge is not None
        assert results["mapping"] == True
        assert results["asset"] == True
        assert results["techspec"] == True
        scenarios = knowledge["topology"].get("scenarios", [])
        non_common = [s for s in scenarios if not s.get("is_common")]
        assert len(non_common) >= 2
        html = (Path(tmp_output) / "asset_report.html").read_text()
        assert "is_multi_scenario" in html

    def test_mapping_xlsx_structure(self, tmp_output):
        """mapping.xlsx 结构验证。"""
        xlsx = self._make_xlsx("case_02_cte_basic", tmp_output)
        knowledge, results = run_full_analysis(xlsx, tmp_output)
        assert results["mapping"] == True
        import openpyxl
        wb = openpyxl.load_workbook(Path(tmp_output) / "mapping.xlsx", read_only=True)
        assert "实体级mapping" in wb.sheetnames
        assert "属性级mapping" in wb.sheetnames
        assert wb["实体级mapping"].max_row >= 2
        assert wb["属性级mapping"].max_row >= 2
        wb.close()

    def test_tech_design_structure(self, tmp_output):
        """tech_design.md 结构验证。"""
        xlsx = self._make_xlsx("case_04_pivot", tmp_output)
        knowledge, results = run_full_analysis(xlsx, tmp_output)
        assert results["techspec"] == True
        md = (Path(tmp_output) / "tech_design.md").read_text()
        assert len(md) > 500
