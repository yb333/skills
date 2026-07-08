"""加工方式判断测试（增量/全量/分区/追加）。

验证 detect_load_strategy 能根据 delete_mode 正确判断资产的加工方式。

运行:
    pytest tests/test_load_strategy.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from engine import detect_load_strategy, RawRule


class TestDetectLoadStrategy:
    """加工方式判断。"""

    def test_truncate_full(self):
        """delete_mode=1 TRUNCATE TABLE → 全量。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="1")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "full"
        assert result["label"] == "全量"

    def test_delete_incremental(self):
        """delete_mode=4 DELETE → 增量。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="4",
                       delete_condition="etl_date='${p_cycle_id}'")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "incremental"
        assert result["label"] == "增量"
        assert "etl_date" in result["detail"]

    def test_merge_incremental(self):
        """delete_mode=6 MERGE INTO → 增量。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="6")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "incremental"
        assert "MERGE" in result["detail"]

    def test_truncate_partition(self):
        """delete_mode=5 TRUNCATE PARTITION → 分区全量。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="5",
                       delete_condition="p202401")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "partition"
        assert result["label"] == "分区全量"

    def test_no_delete_append_is_incremental(self):
        """delete_mode=2 NO DELETE → 追加，归入增量。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="2")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "incremental", "追加应归入增量"

    def test_exchange_partition_traces_back_incremental(self):
        """交换分区往前推导：前步增量 → 增量（不看交换分区本身）。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_table="tmp_f", delete_mode="4",
                    delete_condition="etl_date='20240101'"),
            RawRule(rule_code="R2", rule_type=9, exec_sequence=2,
                    target_table="tmp_f", exchange_source_table="dwb_f",
                    delete_mode=""),
        ]
        result = detect_load_strategy(rules)
        assert result["strategy"] == "incremental", \
            "交换分区应往前推导，前步增量→增量"
        assert "交换分区" in result["detail"], "detail 应提及交换分区"

    def test_exchange_partition_traces_back_full(self):
        """交换分区往前推导：前步全量 → 全量。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_table="tmp_f", delete_mode="1"),
            RawRule(rule_code="R2", rule_type=9, exec_sequence=2,
                    target_table="tmp_f", exchange_source_table="dwb_f",
                    delete_mode=""),
        ]
        result = detect_load_strategy(rules)
        assert result["strategy"] == "full", \
            "交换分区应往前推导，前步全量→全量"

    def test_no_delete_mode_unknown(self):
        """无 delete_mode → 未知。"""
        rule = RawRule(rule_type=1, target_table="dwb_f", delete_mode="")
        result = detect_load_strategy([rule])
        assert result["strategy"] == "unknown"

    def test_empty_rules(self):
        """空规则列表 → 未知。"""
        result = detect_load_strategy([])
        assert result["strategy"] == "unknown"

    def test_uses_final_step_not_intermediate(self):
        """多步骤时看最终目标表（非中间表），不看中间步骤。"""
        rules = [
            RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                    target_table="tmp_x", delete_mode="1"),  # 中间表全量
            RawRule(rule_code="R2", rule_type=1, exec_sequence=2,
                    target_table="dwb_f", delete_mode="4",  # F表增量
                    delete_condition="etl_date='${p_cycle_id}'"),
        ]
        result = detect_load_strategy(rules)
        assert result["strategy"] == "incremental", "应以F表步骤为准（增量），非中间表"

    def test_strategy_in_meta(self, tmp_path):
        """analyze_pipeline 产出的 knowledge.meta 含 load_strategy。"""
        from engine import analyze_pipeline, detect_dialect
        rules = [RawRule(rule_code="R1", rule_type=1, exec_sequence=1,
                          target_schema="dws", target_table="dwb_f", delete_mode="4",
                          query_sql="SELECT a.x FROM ods.src a",
                          delete_condition="etl_date='20240101'")]
        dialect = detect_dialect([r.query_sql for r in rules])
        kj, _ = analyze_pipeline(rules, {}, {}, dialect)
        assert "load_strategy" in kj["meta"]
        assert kj["meta"]["load_strategy"]["strategy"] == "incremental"
