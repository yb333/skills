"""批量分析测试。

验证多规则组批量解析 + 交付件生成 + 分批控制。

运行:
    pytest tests/test_batch.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_xlsx import build_xlsx


def _make_multi_group_xlsx(path, num_groups=3):
    """构造含多个规则组的 Excel。"""
    rules = []
    for g in range(num_groups):
        grp = f"GR{g+1:03d}"
        en = f"DWB_TEST_{g+1}_F"
        rules.append({"rule_code": f"R{g*2+1:04d}", "rule_type": 1, "exec_sequence": 1,
                      "target_schema": "dws", "target_table": f"tmp_{g}", "delete_mode": "1",
                      "query_sql": f"SELECT a.id, a.amount FROM ods.src_{g} a WHERE a.del='N'",
                      "rule_name": "源头", "rule_group_code": grp, "rule_group_en": en})
        rules.append({"rule_code": f"R{g*2+2:04d}", "rule_type": 1, "exec_sequence": 2,
                      "target_schema": "dws", "target_table": f"dwb_test_{g}_f", "delete_mode": "1",
                      "query_sql": f"SELECT t.id, SUM(t.amount) AS total FROM dws.tmp_{g} t GROUP BY t.id",
                      "rule_name": "汇总", "rule_group_code": grp, "rule_group_en": en})
    build_xlsx(str(path), rules=rules)
    return str(path)


@pytest.fixture
def multi_group_xlsx(tmp_path):
    return _make_multi_group_xlsx(tmp_path / "multi.xlsx")


class TestBatchAnalysis:
    """批量分析基本功能。"""

    def test_batch_generates_all_groups(self, multi_group_xlsx, tmp_path):
        """批量生成所有规则组的交付件"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        assert len(results) == 3, f"应处理 3 个规则组，实际 {len(results)}"
        for r in results:
            assert r.success, f"{r.rule_group_en} 应成功，错误: {r.error}"

    def test_batch_creates_output_dirs(self, multi_group_xlsx, tmp_path):
        """每个规则组应有独立输出目录"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        for r in results:
            out_path = Path(r.output_dir)
            assert out_path.exists(), f"输出目录应存在: {out_path}"

    def test_batch_dirs_named_per_group_not_global(self, multi_group_xlsx, tmp_path):
        """防回归：不同规则组必须用各自英文名建目录，不能用全局英文名撞名覆盖。

        历史 bug：RawRule 没存每行 rule_group_en，batch 用了 read_excel 返回的
        全局 rule_group_en（取第一个非空），导致所有 code 不同的组撞同一个目录名
        → 全写进同一目录互相覆盖。此用例锁定该回归。
        """
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        # 3 个组的 output_dir 必须两两不同（目录名各自独立）
        dirs = {Path(r.output_dir).name for r in results}
        assert len(dirs) == 3, f"3 个组应有 3 个独立目录，实际 {dirs}"
        # 目录名应是各自英文名，且都落在 output 基础目录下
        for r in results:
            name = Path(r.output_dir).name
            assert name == r.rule_group_en, f"目录名 {name} 应等于规则组英文名 {r.rule_group_en}"
            assert str(Path(r.output_dir).parent) == out, f"应在基础目录下: {r.output_dir}"

    def test_batch_generates_deliverables(self, multi_group_xlsx, tmp_path):
        """每个规则组应生成三个交付件"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        for r in results:
            if not r.success:
                continue
            out_path = Path(r.output_dir)
            assert (out_path / "mapping.xlsx").exists(), f"mapping.xlsx 应存在"
            assert (out_path / "asset_report.html").exists(), f"asset_report.html 应存在"
            assert (out_path / "tech_design.md").exists(), f"tech_design.md 应存在"
            assert (out_path / "knowledge_draft.json").exists(), f"knowledge_draft.json 应存在"

    def test_batch_size_split(self, multi_group_xlsx, tmp_path):
        """分批处理：batch_size=2 时 3 个组应分 2 批"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=2, no_ai=True)
        assert len(results) == 3, f"应处理 3 个规则组"

    def test_batch_no_ai_skips_summary(self, multi_group_xlsx, tmp_path):
        """--no-ai 时不生成 knowledge_summary.md"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)
        for r in results:
            if r.success:
                assert not r.has_ai, "no_ai 时不应标记 has_ai"

    def test_batch_with_ai_generates_summary(self, multi_group_xlsx, tmp_path):
        """启用 AI 时生成 knowledge_summary.md"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=False)
        for r in results:
            if r.success:
                assert r.has_ai, "启用 AI 时应标记 has_ai"
                assert (Path(r.output_dir) / "knowledge_summary.md").exists(), \
                    "knowledge_summary.md 应存在"

    def test_batch_error_handling(self, tmp_path):
        """有错误规则组时，其他组应正常处理"""
        import openpyxl
        # 构造一个有问题的 Excel（空 SQL）
        rules = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f1", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.t a", "rule_name": "正常",
             "rule_group_code": "GR001", "rule_group_en": "OK_F"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f2", "delete_mode": "1",
             "query_sql": "", "rule_name": "空SQL",
             "rule_group_code": "GR002", "rule_group_en": "EMPTY_F"},
        ]
        xlsx = str(tmp_path / "mixed.xlsx")
        build_xlsx(xlsx, rules=rules)

        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(xlsx, out, batch_size=50, no_ai=True)
        # 至少一个应成功
        success = [r for r in results if r.success]
        assert len(success) >= 1, f"至少一个应成功，实际 {results}"
