"""Case 08: 自引用 — WHERE EXISTS(SELECT 1 FROM 目标表)。

预期：self_references 非空，pattern 含 'EXISTS'。
"""

rules = [
    {
        "rule_code": "UR008",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_incremental_f",
        "delete_mode": "0",
        "query_sql": """SELECT
    s.order_id,
    s.order_date,
    s.amount
FROM ods.staging_orders s
WHERE NOT EXISTS (
    SELECT 1
    FROM dws.dwb_incremental_f t
    WHERE t.order_id = s.order_id
)""",
        "rule_group_code": "GR008",
        "rule_name": "增量去重表",
    },
]

target_fields = [
    {"rule_code": "UR008", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR008", "target_field": "order_date", "source_field": "order_date", "field_type": "date"},
    {"rule_code": "UR008", "target_field": "amount", "source_field": "amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR008", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
