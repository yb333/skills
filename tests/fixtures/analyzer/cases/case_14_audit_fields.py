"""Case 14: 审计字段推断 — 'N'/CURRENT_TIMESTAMP 无 alias 靠推断。

预期：'N'→del_flag, CURRENT_TIMESTAMP→dw_last_update_date。
"""

rules = [
    {
        "rule_code": "UR014",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_audit_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    p.product_name,
    'N' AS del_flag,
    CURRENT_TIMESTAMP AS dw_last_update_date
FROM dim.dim_product p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR014",
        "rule_name": "审计字段测试",
    },
]

target_fields = [
    {"rule_code": "UR014", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR014", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"},
    {"rule_code": "UR014", "target_field": "del_flag", "source_field": "", "field_type": "varchar(1)"},
    {"rule_code": "UR014", "target_field": "dw_last_update_date", "source_field": "", "field_type": "timestamp"},
]

group_variables = [
    {"rule_code": "UR014", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
