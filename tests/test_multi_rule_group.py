"""多规则组链路分析测试。

覆盖：
- 代码仓索引（target_table → 规则组目录）
- 数据依赖递归追溯（从最终 F 表往上游追 mid 规则组）
- 多规则组合并（exec_sequence 拓扑排序）
- 完整链路分析（合并后作为一个整体跑 analyze_pipeline）

运行:
    pytest tests/test_multi_rule_group.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_yml import build_yml_group
from analyzer import build_target_index, trace_upstream_rule_groups, merge_rule_groups
from engine import analyze_pipeline, _norm_table


def _make_chain_repo(base_dir):
    """构造多规则组代码仓：2个mid + 1个最终F。

    返回 (repo_root, final_group_dir, sub_project_dir)
    """
    repo = Path(base_dir) / "repo"
    sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
    sub.mkdir(parents=True)

    # mid 规则组 A：写 dwb_trade_mid_f（读 ods.order_src）
    build_yml_group(sub / "DWB_TRADE_MID_F", rules=[
        {"rule_code": "MA_R1", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "dwb_trade_mid_f", "delete_mode": "1",
         "query_sql": "SELECT a.order_id, a.user_id FROM ods.order_src a",
         "rule_group_en": "DWB_TRADE_MID_F", "rule_group_code": "GR_MID"},
    ])
    # mid 规则组 B：写 dwb_detail_mid_f（读 ods.detail_src）
    build_yml_group(sub / "DWB_DETAIL_MID_F", rules=[
        {"rule_code": "MB_R1", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "dwb_detail_mid_f", "delete_mode": "1",
         "query_sql": "SELECT a.detail_id, a.product_name FROM ods.detail_src a",
         "rule_group_en": "DWB_DETAIL_MID_F", "rule_group_code": "GR_DETAIL_MID"},
    ])
    # 最终 F 规则组 C：读两个 mid 表（读 dws.dwb_trade_mid_f + dws.dwb_detail_mid_f）
    build_yml_group(sub / "DWB_TRADE_ORDER_F", rules=[
        {"rule_code": "TC_R1", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "dwb_trade_order_f", "delete_mode": "1",
         "query_sql": ("SELECT a.order_id, a.user_id, b.product_name "
                       "FROM dws.dwb_trade_mid_f a "
                       "LEFT JOIN dws.dwb_detail_mid_f b ON a.order_id = b.detail_id"),
         "rule_group_en": "DWB_TRADE_ORDER_F", "rule_group_code": "GR_ORDER"},
    ])
    return repo, sub / "DWB_TRADE_ORDER_F", sub


# ═══════════════════════════════════════════════════════════════
# 代码仓索引
# ═══════════════════════════════════════════════════════════════

class TestBuildTargetIndex:
    """target_table → 规则组目录索引。"""

    def test_index_has_all_tables(self, tmp_path):
        """索引包含子项目下所有规则组的 target_table。"""
        repo, _, sub = _make_chain_repo(tmp_path)
        index = build_target_index(repo, sub)
        assert "dwb_trade_mid_f" in index
        assert "dwb_detail_mid_f" in index
        assert "dwb_trade_order_f" in index

    def test_index_maps_to_correct_dir(self, tmp_path):
        """索引指向正确的规则组目录。"""
        repo, _, sub = _make_chain_repo(tmp_path)
        index = build_target_index(repo, sub)
        writers = index["dwb_trade_mid_f"]
        assert len(writers) == 1
        assert "DWB_TRADE_MID_F" in writers[0]["dir"]


# ═══════════════════════════════════════════════════════════════
# 数据依赖递归追溯
# ═══════════════════════════════════════════════════════════════

class TestTraceUpstream:
    """从最终 F 表规则组追溯上游 mid 规则组。"""

    def test_finds_all_upstream(self, tmp_path):
        """找到两个 mid 规则组 + 最终 F。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        assert len(result["groups"]) == 3

        group_ens = {g["rule_group_en"] for g in result["groups"]}
        assert "DWB_TRADE_MID_F" in group_ens
        assert "DWB_DETAIL_MID_F" in group_ens
        assert "DWB_TRADE_ORDER_F" in group_ens

    def test_depth_correct(self, tmp_path):
        """最终 F 在 depth=0，mid 在 depth=1。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        for g in result["groups"]:
            if g["rule_group_en"] == "DWB_TRADE_ORDER_F":
                assert g["depth"] == 0
            else:
                assert g["depth"] == 1  # mid 规则组

    def test_ods_source_not_traced(self, tmp_path):
        """ods 源表不追溯（在 not_found 里，不在 groups 里）。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "ods.order_src" in result["not_found"]
        assert "ods.detail_src" in result["not_found"]

    def test_no_cycle(self, tmp_path):
        """不出现环（正常场景不会环）。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        assert not result["cycle_detected"]


# ═══════════════════════════════════════════════════════════════
# 三层链路（mid 也依赖另一个 mid）
# ═══════════════════════════════════════════════════════════════

class TestDeepChain:
    """三层链路：ods → mid1 → mid2 → 最终F。"""

    def test_recursive_traces_all_levels(self, tmp_path):
        """递归追溯到最深层。"""
        repo = tmp_path / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        sub.mkdir(parents=True)

        # 第一层 mid：读 ods
        build_yml_group(sub / "DWB_BASE_MID_F", rules=[
            {"rule_code": "L1_R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_base_mid_f", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.src_a a",
             "rule_group_en": "DWB_BASE_MID_F", "rule_group_code": "L1"},
        ])
        # 第二层 mid：读第一层 mid
        build_yml_group(sub / "DWB_AGG_MID_F", rules=[
            {"rule_code": "L2_R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_agg_mid_f", "delete_mode": "1",
             "query_sql": "SELECT b.id FROM dws.dwb_base_mid_f b",
             "rule_group_en": "DWB_AGG_MID_F", "rule_group_code": "L2"},
        ])
        # 最终 F：读第二层 mid
        build_yml_group(sub / "DWB_FINAL_F", rules=[
            {"rule_code": "L3_R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_final_f", "delete_mode": "1",
             "query_sql": "SELECT c.id FROM dws.dwb_agg_mid_f c",
             "rule_group_en": "DWB_FINAL_F", "rule_group_code": "L3"},
        ])

        result = trace_upstream_rule_groups(sub / "DWB_FINAL_F", repo)
        assert len(result["groups"]) == 3

        # depth 检查：final=0, agg=1, base=2
        depths = {g["rule_group_en"]: g["depth"] for g in result["groups"]}
        assert depths["DWB_FINAL_F"] == 0
        assert depths["DWB_AGG_MID_F"] == 1
        assert depths["DWB_BASE_MID_F"] == 2


# ═══════════════════════════════════════════════════════════════
# 合并 + 拓扑排序
# ═══════════════════════════════════════════════════════════════

class TestMergeRuleGroups:
    """多规则组合并 + exec_sequence 重编号。"""

    def test_merged_seq_upstream_first(self, tmp_path):
        """合并后上游（depth大的）排前面。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        merged = merge_rule_groups(result, repo)

        # seq 排序：mid 规则组在前，最终 F 在后
        target_by_seq = [(r.exec_sequence, r.target_table) for r in merged]
        # 最终 F 应该是最后一个
        assert target_by_seq[-1][1] == "dwb_trade_order_f"
        # mid 在前面
        mid_targets = [t for _, t in target_by_seq[:-1]]
        assert "dwb_trade_mid_f" in mid_targets
        assert "dwb_detail_mid_f" in mid_targets

    def test_merged_seq_continuous(self, tmp_path):
        """合并后 exec_sequence 连续编号（1,2,3...）。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        merged = merge_rule_groups(result, repo)

        seqs = [r.exec_sequence for r in merged]
        assert seqs == list(range(1, len(merged) + 1))


# ═══════════════════════════════════════════════════════════════
# 完整链路分析
# ═══════════════════════════════════════════════════════════════

class TestChainAnalysis:
    """合并后作为一个整体跑 analyze_pipeline。"""

    def test_full_chain_analysis(self, tmp_path):
        """完整链路分析：3个规则组合并后 topology 正确串联。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        merged = merge_rule_groups(result, repo)

        kj, pm = analyze_pipeline(merged, {}, {}, "dws")

        # 3 个步骤
        assert len(kj["topology"]["steps"]) == 3

        # 数据依赖：mid 规则组 → 最终 F
        deps = kj["topology"].get("data_dependencies", [])
        # step_1(写mid1) 和 step_2(写mid2) 都依赖到 step_3(最终F)
        to_step3 = [d for d in deps if d["to"] == "step_3"]
        assert len(to_step3) >= 2  # 两个 mid 都连到最终 F

        # 最终目标表是 dwb_trade_order_f
        assert kj["meta"]["target_table"] == "dwb_trade_order_f"

    def test_single_rule_group_no_chain(self, tmp_path):
        """没有上游时（单规则组），追溯只返回自己。"""
        repo = tmp_path / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P" / "S"
        sub.mkdir(parents=True)
        build_yml_group(sub / "DWB_SIMPLE_F", rules=[
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_simple_f", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.src_a a",
             "rule_group_en": "DWB_SIMPLE_F", "rule_group_code": "GR1"},
        ])
        result = trace_upstream_rule_groups(sub / "DWB_SIMPLE_F", repo)
        assert len(result["groups"]) == 1  # 只有自己
        assert result["groups"][0]["rule_group_en"] == "DWB_SIMPLE_F"
