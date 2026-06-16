"""Case 15: 注释别名提取 — /* field_name */ 格式无 AS alias。

预期：_extract_comment_aliases 提取正确字段名。
"""

rules = [
    {
        "rule_code": "UR015",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_comment_alias_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    'N',                                          /* del_flag */
    CURRENT_TIMESTAMP,                            /* dw_last_update_date */
    p.product_name
FROM dim.dim_product p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR015",
        "rule_name": "注释别名测试",
    },
]

target_fields = [
    {"rule_code": "UR015", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR015", "target_field": "del_flag", "source_field": "", "field_type": "varchar(1)"},
    {"rule_code": "UR015", "target_field": "dw_last_update_date", "source_field": "", "field_type": "timestamp"},
    {"rule_code": "UR015", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"},
]

group_variables = [
    {"rule_code": "UR015", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
