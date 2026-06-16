"""Case 11: UNION ALL — 预期暴露缺口（第二分支字段丢失）。

预期：analyzer 只取第一个 SELECT 节点，UNION 后的字段丢失。
这是 xfail 标记的已知缺口。
"""

rules = [
    {
        "rule_code": "UR011",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_union_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    a.order_id,
    a.source,
    a.amount
FROM ods.orders_a a
WHERE a.del_flag = 'N'
UNION ALL
SELECT
    b.order_id,
    b.source,
    b.amount
FROM ods.orders_b b
WHERE b.del_flag = 'N'""",
        "rule_group_code": "GR011",
        "rule_name": "UNION ALL测试",
    },
]

target_fields = [
    {"rule_code": "UR011", "target_field": "order_id", "source_field": "order_id", "field_type": "bigint"},
    {"rule_code": "UR011", "target_field": "source", "source_field": "source", "field_type": "varchar(20)"},
    {"rule_code": "UR011", "target_field": "amount", "source_field": "amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR011", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
