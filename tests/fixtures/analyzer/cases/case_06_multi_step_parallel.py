"""Case 06: 多步骤并行 — 两个 rule exec_sequence 相同，不同目标表。

预期：schedule_plan[0].parallel_steps 长度=2。
"""

rules = [
    {
        "rule_code": "UR006A",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_order_daily_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    o.order_id,
    o.order_date,
    o.amount
FROM ods.orders o
WHERE o.del_flag = 'N'""",
        "rule_group_code": "GR006",
        "rule_name": "订单日表",
    },
    {
        "rule_code": "UR006B",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_refund_daily_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    r.refund_id,
    r.order_id,
    r.refund_amount
FROM ods.refunds r
WHERE r.del_flag = 'N'""",
        "rule_group_code": "GR006",
        "rule_name": "退款日表",
    },
]

target_fields = [
    {"rule_code": "UR006A", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR006A", "target_field": "order_date", "source_field": "order_date", "field_type": "date"},
    {"rule_code": "UR006A", "target_field": "amount", "source_field": "amount", "field_type": "decimal(18,2)"},
    {"rule_code": "UR006B", "target_field": "refund_id", "source_field": "refund_id", "field_type": "bigint"},
    {"rule_code": "UR006B", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR006B", "target_field": "refund_amount", "source_field": "refund_amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR006A", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
