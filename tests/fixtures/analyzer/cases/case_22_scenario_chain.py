"""Case 22: 单场景串行依赖链 — 3步串行写入同一目标表。

seq=0: UR001 TRUNCATE TABLE → 写 step1 数据
seq=1: UR002 NO DELETE → 追加 step2 数据
seq=2: UR003 NO DELETE → 追加 step3 数据

验证点: 3步串行依赖(step1→step2→step3), 单场景不分场景
"""

rules = [
    {
        "rule_code": "UR001", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_chain_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": "SELECT a.id, a.name FROM ods.source_a a WHERE a.del_flag = 'N'",
        "rule_group_code": "GR001", "rule_name": "初始化加载",
    },
    {
        "rule_code": "UR002", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_chain_f",
        "delete_mode": "2", "delete_condition": "",
        "query_sql": "SELECT b.id, b.name FROM ods.source_b b WHERE b.del_flag = 'N'",
        "rule_group_code": "GR002", "rule_name": "追加加载B",
    },
    {
        "rule_code": "UR003", "rule_type": 1, "exec_sequence": 2,
        "target_schema": "dws", "target_table": "dwb_chain_f",
        "delete_mode": "2", "delete_condition": "",
        "query_sql": "SELECT c.id, c.name FROM ods.source_c c WHERE c.del_flag = 'N'",
        "rule_group_code": "GR003", "rule_name": "追加加载C",
    },
]

target_fields = [
    {"rule_code": "UR001", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR001", "target_field": "name", "source_field": "name"},
    {"rule_code": "UR002", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR002", "target_field": "name", "source_field": "name"},
    {"rule_code": "UR003", "target_field": "id", "source_field": "id"},
    {"rule_code": "UR003", "target_field": "name", "source_field": "name"},
]

group_variables = []
