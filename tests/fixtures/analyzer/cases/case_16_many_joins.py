"""Case 16: 多表 JOIN（9张表）— 触发性能告警。

预期：ISS: 单步骤 JOIN 9 张表（severity=medium）。
"""


def _build_many_join_sql() -> str:
    """生成 9 张表 JOIN 的 SQL。"""
    lines = ["SELECT t0.product_id,"]
    for i in range(1, 10):
        lines.append(f"    t{i}.attr_{i},")
    lines.append("    t0.product_name")
    lines.append("FROM dim.dim_main t0")
    for i in range(1, 10):
        lines.append(f"LEFT JOIN dim.dim_attr_{i} t{i}")
        lines.append(f"    ON t0.product_id = t{i}.product_id")
    lines.append("WHERE t0.del_flag = 'N'")
    return "\n".join(lines)


def _build_target_fields():
    fields = [{"rule_code": "UR016", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"}]
    for i in range(1, 10):
        fields.append({
            "rule_code": "UR016",
            "target_field": f"attr_{i}",
            "source_field": f"attr_{i}",
            "field_type": "varchar(100)",
        })
    fields.append({"rule_code": "UR016", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"})
    return fields


rules = [
    {
        "rule_code": "UR016",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_many_join_f",
        "delete_mode": "1",
        "query_sql": _build_many_join_sql(),
        "rule_group_code": "GR016",
        "rule_name": "多JOIN测试",
    },
]

target_fields = _build_target_fields()

group_variables = [
    {"rule_code": "UR016", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
