"""Case 07: 多步骤串行（跨步骤依赖）— step_1 写 table_A，step_2 FROM table_A。

预期：data_dependencies 含 {from: step_1, to: step_2}。
"""

rules = [
    {
        "rule_code": "UR007A",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_order_base_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    o.order_id,
    o.order_date,
    o.amount
FROM ods.orders o
WHERE o.del_flag = 'N'""",
        "rule_group_code": "GR007",
        "rule_name": "订单基础表",
    },
    {
        "rule_code": "UR007B",
        "rule_type": 1,
        "exec_sequence": 2,
        "target_schema": "dws",
        "target_table": "dwb_order_summary_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    b.order_date,
    SUM(b.amount) AS daily_total
FROM dws.dwb_order_base_f b
GROUP BY b.order_date""",
        "rule_group_code": "GR007",
        "rule_name": "订单汇总表",
    },
]

target_fields = [
    {"rule_code": "UR007A", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR007A", "target_field": "order_date", "source_field": "order_date", "field_type": "date"},
    {"rule_code": "UR007A", "target_field": "amount", "source_field": "amount", "field_type": "decimal(18,2)"},
    {"rule_code": "UR007B", "target_field": "order_date", "source_field": "order_date", "field_type": "date"},
    {"rule_code": "UR007B", "target_field": "daily_total", "source_field": "amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR007A", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
