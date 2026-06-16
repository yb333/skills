"""Case 18: 多 CASE WHEN（21个）— 触发复杂度告警。

预期：ISS: CASE WHEN/PIVOT 分支共 21 个（severity=medium）。
"""


def _build_many_pivot_sql() -> str:
    """生成 21 个行转列字段的 SQL。"""
    lines = ["SELECT t.product_id,"]
    for i in range(1, 22):
        lines.append(f"    SUM(CASE WHEN t.month = '2024{i:02d}' THEN t.amount ELSE 0 END) AS amt_{i:02d},")
    lines.append("    t.product_name")
    lines.append("FROM ods.monthly_sales t")
    lines.append("GROUP BY t.product_id, t.product_name")
    return "\n".join(lines)


def _build_target_fields():
    fields = [{"rule_code": "UR018", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"}]
    for i in range(1, 22):
        fields.append({
            "rule_code": "UR018",
            "target_field": f"amt_{i:02d}",
            "source_field": "amount",
            "field_type": "decimal(18,2)",
        })
    fields.append({"rule_code": "UR018", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"})
    return fields


rules = [
    {
        "rule_code": "UR018",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_many_pivot_f",
        "delete_mode": "1",
        "query_sql": _build_many_pivot_sql(),
        "rule_group_code": "GR018",
        "rule_name": "多CASE WHEN测试",
    },
]

target_fields = _build_target_fields()

group_variables = [
    {"rule_code": "UR018", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
