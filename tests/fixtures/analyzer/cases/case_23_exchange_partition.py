"""Case 23: 分区交换场景 — 临时表写入 + 分区交换到目标表。

step_1(seq=0): 取数规则，写入临时表 dwl_temp_f (TRUNCATE TABLE)
step_2(seq=1): 分区交换，临时表 dwl_temp_f → 目标表 dwl_real_f (无SQL)

验证点:
  - step_2 的 target_table 应显示真正的目标表 dwl_real_f（不是临时表）
  - step_2 标注为分区交换
  - 字段映射不受分区交换影响（字段来自 step_1 的 SELECT）
"""

rules = [
    {
        "rule_code": "UR023", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwl_temp_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": """SELECT
    t.product_id,
    t.product_name,
    t.price
FROM ods.products t
WHERE t.del_flag = 'N'""",
        "rule_group_code": "GR001", "rule_name": "加载到临时表",
    },
    {
        "rule_code": "UR024", "rule_type": 9, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwl_temp_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": "",
        "rule_group_code": "GR001", "rule_name": "分区交换",
        "exchange_source_table": "dwl_real_f",
    },
]

target_fields = [
    {"rule_code": "UR023", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR023", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"},
    {"rule_code": "UR023", "target_field": "price", "source_field": "price", "field_type": "decimal(18,2)"},
]

group_variables = []
