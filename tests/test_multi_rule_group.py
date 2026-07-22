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
        """合并后最终F的seq大于所有mid的seq（按依赖拓扑排序）。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        merged = merge_rule_groups(result, repo)

        # 最终 F 的 seq 应大于所有 mid
        mid_seqs = [r.exec_sequence for r in merged if r.target_table != "dwb_trade_order_f"]
        final_seqs = [r.exec_sequence for r in merged if r.target_table == "dwb_trade_order_f"]
        assert min(final_seqs) > max(mid_seqs), \
            f"最终F seq应大于mid: mid={mid_seqs} final={final_seqs}"

    def test_merged_seq_upstream_before_downstream(self, tmp_path):
        """合并后上游规则组的 seq 整体小于下游。"""
        repo, final_dir, _ = _make_chain_repo(tmp_path)
        result = trace_upstream_rule_groups(final_dir, repo)
        merged = merge_rule_groups(result, repo)

        # 最终F的 seq 应该大于所有 mid 规则组的 seq
        mid_seqs = [r.exec_sequence for r in merged if r.target_table != "dwb_trade_order_f"]
        final_seqs = [r.exec_sequence for r in merged if r.target_table == "dwb_trade_order_f"]
        assert max(mid_seqs) < min(final_seqs), \
            f"上游seq应小于下游: mid={mid_seqs} final={final_seqs}"

    def test_merged_preserves_internal_parallel(self, tmp_path):
        """合并后规则组内部的并行结构保留（同 seq 的规则不被拍平成串行）。"""
        repo = tmp_path / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        sub.mkdir(parents=True)

        # mid规则组：seq=0 两条并行 + seq=1 串行
        build_yml_group(sub / "DWB_MID_F", rules=[
            {"rule_code": "M1", "rule_type": 1, "exec_sequence": 0,
             "target_schema": "dws", "target_table": "tmp_mid_a", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.src_a a",
             "rule_group_en": "DWB_MID_F", "rule_group_code": "G1"},
            {"rule_code": "M2", "rule_type": 1, "exec_sequence": 0,
             "target_schema": "dws", "target_table": "tmp_mid_b", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.src_b a",
             "rule_group_en": "DWB_MID_F", "rule_group_code": "G1"},
            {"rule_code": "M3", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_mid_f", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM dws.tmp_mid_a a",
             "rule_group_en": "DWB_MID_F", "rule_group_code": "G1"},
        ])
        # 最终F规则组：seq=0 两条并行 + seq=1 串行
        build_yml_group(sub / "DWB_FINAL_F", rules=[
            {"rule_code": "F1", "rule_type": 1, "exec_sequence": 0,
             "target_schema": "dws", "target_table": "tmp_final_a", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM dws.dwb_mid_f a",
             "rule_group_en": "DWB_FINAL_F", "rule_group_code": "G2"},
            {"rule_code": "F2", "rule_type": 1, "exec_sequence": 0,
             "target_schema": "dws", "target_table": "tmp_final_b", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM dws.dwb_mid_f a",
             "rule_group_en": "DWB_FINAL_F", "rule_group_code": "G2"},
            {"rule_code": "F3", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_final_f", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM dws.tmp_final_a a",
             "rule_group_en": "DWB_FINAL_F", "rule_group_code": "G2"},
        ])

        result = trace_upstream_rule_groups(sub / "DWB_FINAL_F", repo)
        merged = merge_rule_groups(result, repo)

        # mid组：seq=0 应有2条（M1/M2并行），seq=1 应有1条（M3）
        mid_seq0 = [r for r in merged if r.rule_group_en == "DWB_MID_F" and r.exec_sequence == 0]
        assert len(mid_seq0) == 2, f"mid组seq=0应有2条并行: {len(mid_seq0)}"

        # final组：偏移后 seq=2 应有2条（F1/F2并行）
        final_min_seq = min(r.exec_sequence for r in merged if r.rule_group_en == "DWB_FINAL_F")
        final_parallel = [r for r in merged if r.rule_group_en == "DWB_FINAL_F" and r.exec_sequence == final_min_seq]
        assert len(final_parallel) == 2, f"final组最小seq应有2条并行: {len(final_parallel)}"


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

        # 数据依赖：两个 mid 的 step 都连到最终 F 的 step
        deps = kj["topology"].get("data_dependencies", [])
        # 找最终 F 的 step_id（按 target_table 找，不硬编码 step_3）
        final_step = next(
            (s for s in kj["topology"]["steps"]
             if s["target_table"] == "dwb_trade_order_f"), None
        )
        assert final_step, "应找到最终F的step"
        final_step_id = final_step["step_id"]
        # 两个 mid 的 step 都应有依赖连到最终 F
        to_final = [d for d in deps if d["to"] == final_step_id]
        assert len(to_final) >= 2, f"两个mid都应连到最终F({final_step_id}): {to_final}"

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
