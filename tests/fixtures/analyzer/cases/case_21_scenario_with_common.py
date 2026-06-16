"""Case 21: 场景+公共混合 — 前2步分场景，第3步不分场景。

seq=0: UR001(part_fwd) + UR002(part_cr)  → 分场景
seq=1: UR003(part_fwd) + UR004(part_cr)  → 分场景
seq=2: UR005(TRUNCATE TABLE)             → 公共步骤

验证点: 2个分区场景 + 1个公共步骤
"""

rules = [
    {
        "rule_code": "UR001", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_mix_f",
        "delete_mode": "5", "delete_condition": "part_fwd",
        "query_sql": "SELECT a.id, a.val FROM ods.src_fwd a WHERE a.del_flag = 'N'",
        "rule_group_code": "GR001", "rule_name": "正向加载",
    },
    {
        "rule_code": "UR002", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_mix_f",
        "delete_mode": "5", "delete_condition": "part_cr",
        "query_sql": "SELECT b.id, b.val FROM ods.src_cr b WHERE b.del_flag = 'N'",
        "rule_group_code": "GR002", "rule_name": "贷项加载",
    },
    {
        "rule_code": "UR003", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_mix_agg_f",
        "delete_mode": "5", "delete_condition": "part_fwd",
        "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.dwb_mix_f PARTITION(part_fwd) t GROUP BY t.id",
        "rule_group_code": "GR001", "rule_name": "正向汇总",
    },
    {
        "rule_code": "UR004", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_mix_agg_f",
        "delete_mode": "5", "delete_condition": "part_cr",
        "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.dwb_mix_f PARTITION(part_cr) t GROUP BY t.id",
        "rule_group_code": "GR002", "rule_name": "贷项汇总",
    },
    {
        "rule_code": "UR005", "rule_type": 1, "exec_sequence": 2,
        "target_schema": "dws", "target_table": "dwb_mix_final_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": "SELECT id, SUM(total) AS grand_total FROM dws.dwb_mix_agg_f GROUP BY id",
        "rule_group_code": "GR003", "rule_name": "最终汇总",
    },
]

target_fields = [
    {"rule_code": "UR001", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR001", "target_field": "val", "source_field": "val"},
    {"rule_code": "UR003", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR003", "target_field": "total", "source_field": "val"},
    {"rule_code": "UR005", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR005", "target_field": "grand_total", "source_field": "total"},
]

group_variables = []
