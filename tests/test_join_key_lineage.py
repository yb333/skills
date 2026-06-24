"""
跨步骤关联键追溯测试

验证 build_join_key_lineage 能从某步骤的关联键出发，沿数据依赖反向追溯，
穿透中间表的直取/加工，追到物理源表的原始字段。

追溯规则：
- 中间表的 direct 字段 → 继续向上追溯
- 中间表的加工字段（拼接/截取/兜底）→ 展示加工，继续追溯每个源字段
- 物理源表字段（ods/dim）→ 停止

运行:
    pytest tests/test_join_key_lineage.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import (
    parse_single_sql, build_topology, build_data_flow,
    build_field_mappings, RawRule,
)


def _run_analysis(rules_sql):
    """从 SQL 列表跑完整分析，返回 field_mappings + data_flow + topology。"""
    rules = [RawRule(
        rule_code=f"R{i+1}", rule_name=sql_info["name"], rule_type=1,
        exec_sequence=i + 1, target_schema="dws", target_table=sql_info["target"],
        delete_mode="1", query_sql=sql_info["sql"],
    ) for i, sql_info in enumerate(rules_sql)]
    pm = {r.rule_code: parse_single_sql(r.query_sql, "dws") for r in rules}
    topo = build_topology(rules, pm)
    df = build_data_flow(rules, pm)
    fm = build_field_mappings(rules, pm, {})
    return rules, pm, topo, df, fm


# 场景：拼接→直取→直取→关联
SCENARIO = [
    {"name": "拼接bid", "target": "tmp1",
     "sql": "SELECT a.aid, (a.code || b.seq) AS bid FROM ods.tbl_a a LEFT JOIN ods.tbl_b b ON a.k = b.k"},
    {"name": "直取tmp2", "target": "tmp2",
     "sql": "SELECT t.aid, t.bid FROM dws.tmp1 t"},
    {"name": "直取tmp3", "target": "tmp3",
     "sql": "SELECT t.aid, t.bid FROM dws.tmp2 t"},
    {"name": "关联d", "target": "final_f",
     "sql": "SELECT t.aid, d.dname FROM dws.tmp3 t LEFT JOIN ods.tbl_d d ON t.bid = d.bid"},
]


class TestJoinKeyLineage:
    """跨步骤关联键追溯。"""

    def test_trace_finds_physical_source(self):
        """从 step_4 的 tmp3.bid 追溯，应追到 ods.tbl_a.code 和 ods.tbl_b.seq"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        # 从 step_4 追溯关联键 bid（tmp3.bid）
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        assert chain, "追溯结果不应为空"
        # 应追到物理源表
        leaf_tables = _collect_leaf_tables(chain)
        assert "tbl_a" in leaf_tables or "ods.tbl_a" in leaf_tables, \
            f"应追到 tbl_a，实际叶节点: {leaf_tables}"
        assert "tbl_b" in leaf_tables or "ods.tbl_b" in leaf_tables, \
            f"应追到 tbl_b，实际叶节点: {leaf_tables}"

    def test_trace_shows_processing(self):
        """追溯链应展示拼接加工（a.code || b.seq）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        # 链中应有一跳是 expression（拼接）
        transforms = _collect_transforms(chain)
        assert "expression" in transforms, \
            f"应包含拼接加工 expression，实际: {transforms}"

    def test_trace_includes_intermediate_steps(self):
        """追溯链应包含中间步骤（step_2/step_3 的直取）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        steps = _collect_steps(chain)
        # 至少追溯了 3 跳（step_3→step_2→step_1）
        assert len(steps) >= 3, f"应至少追溯3跳，实际 steps: {steps}"

    def test_trace_terminates_at_physical(self):
        """追溯应在物理源表停止，不无限递归"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        # 叶节点都应是物理源表（非中间表）
        leaves = _collect_leaves(chain)
        for leaf in leaves:
            table = leaf.get("table", "")
            # 物理源表：ods 层或非 dws 中间表
            assert "ods." in table.lower() or not table.lower().startswith("dws.tmp"), \
                f"叶节点应为物理源表，实际 {table}"


# ── 辅助函数：遍历追溯链树 ──

def _walk_chain(chain):
    """递归遍历追溯链。chain 格式见 build_join_key_lineage 返回。"""
    if not chain:
        return
    yield chain
    for child in chain.get("children", []):
        yield from _walk_chain(child)


def _collect_leaf_tables(chain):
    """收集所有叶节点的表名。"""
    result = []
    for node in _walk_chain(chain):
        if not node.get("children"):  # 叶节点
            tbl = node.get("table", "")
            result.append(tbl)
            result.append(tbl.split(".")[-1])  # 短名也加
    return result


def _collect_transforms(chain):
    """收集链中所有 transform_type。"""
    return list({node.get("transform", "") for node in _walk_chain(chain) if node.get("transform")})


def _collect_steps(chain):
    """收集链中所有 step_id。"""
    return list({node.get("step_id", "") for node in _walk_chain(chain) if node.get("step_id")})


def _collect_leaves(chain):
    """收集所有叶节点。"""
    return [node for node in _walk_chain(chain) if not node.get("children")]
