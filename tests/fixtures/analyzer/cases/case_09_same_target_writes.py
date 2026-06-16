"""Case 09: 同表多次写入 — 两个 rule 写同一张表，exec_sequence 不同。

预期：target_write_groups 非空，write_pattern=parallel_then_serial。
"""

rules = [
    {
        "rule_code": "UR009A",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_split_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    s.order_id,
    s.order_date,
    s.amount
FROM ods.orders_part_a s
WHERE s.del_flag = 'N'""",
        "rule_group_code": "GR009",
        "rule_name": "分片A写入",
    },
    {
        "rule_code": "UR009B",
        "rule_type": 1,
        "exec_sequence": 2,
        "target_schema": "dws",
        "target_table": "dwb_split_f",
        "delete_mode": "0",
        "query_sql": """SELECT
    s.order_id,
    s.order_date,
    s.amount
FROM ods.orders_part_b s
WHERE s.del_flag = 'N'""",
        "rule_group_code": "GR009",
        "rule_name": "分片B追加",
    },
]

target_fields = [
    {"rule_code": "UR009A", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR009A", "target_field": "order_date", "source_field": "order_date", "field_type": "date"},
    {"rule_code": "UR009A", "target_field": "amount", "source_field": "amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR009A", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
