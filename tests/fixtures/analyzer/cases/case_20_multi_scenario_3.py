"""Case 20: 多场景3分区写入 — 3个场景验证。

场景A (part_a): UR001 → UR004
场景B (part_b): UR002 → UR005
场景C (part_c): UR003 → UR006

验证点: 场景数=3, 3个分区各自独立
"""

rules = [
    {
        "rule_code": "UR001", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_multi_f",
        "delete_mode": "5", "delete_condition": "part_a",
        "query_sql": "SELECT a.id, a.name FROM ods.source_a a WHERE a.del_flag = 'N'",
        "rule_group_code": "GR001", "rule_name": "场景A加载",
    },
    {
        "rule_code": "UR002", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_multi_f",
        "delete_mode": "5", "delete_condition": "part_b",
        "query_sql": "SELECT b.id, b.name FROM ods.source_b b WHERE b.del_flag = 'N'",
        "rule_group_code": "GR002", "rule_name": "场景B加载",
    },
    {
        "rule_code": "UR003", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_multi_f",
        "delete_mode": "5", "delete_condition": "part_c",
        "query_sql": "SELECT c.id, c.name FROM ods.source_c c WHERE c.del_flag = 'N'",
        "rule_group_code": "GR003", "rule_name": "场景C加载",
    },
    {
        "rule_code": "UR004", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_multi_final_f",
        "delete_mode": "5", "delete_condition": "part_a",
        "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.dwb_multi_f PARTITION(part_a) t GROUP BY t.id",
        "rule_group_code": "GR001", "rule_name": "场景A汇总",
    },
    {
        "rule_code": "UR005", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_multi_final_f",
        "delete_mode": "5", "delete_condition": "part_b",
        "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.dwb_multi_f PARTITION(part_b) t GROUP BY t.id",
        "rule_group_code": "GR002", "rule_name": "场景B汇总",
    },
    {
        "rule_code": "UR006", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_multi_final_f",
        "delete_mode": "5", "delete_condition": "part_c",
        "query_sql": "SELECT t.id, SUM(t.val) AS total FROM dws.dwb_multi_f PARTITION(part_c) t GROUP BY t.id",
        "rule_group_code": "GR003", "rule_name": "场景C汇总",
    },
]

target_fields = [
    {"rule_code": "UR001", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR001", "target_field": "name", "source_field": "name"},
    {"rule_code": "UR004", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR004", "target_field": "total", "source_field": "val"},
]

group_variables = []
