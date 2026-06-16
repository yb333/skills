"""Case 01: 最简基线 — 验证 happy path。

场景：1 step，1 source table，纯直取 5 字段。
预期：所有字段 transform_type=direct，0 CTE，0 issues。
"""

rules = [
    {
        "rule_code": "UR001",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_product_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    p.product_name,
    p.category_id,
    p.price,
    p.status
FROM dim.dim_product_f p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR001",
        "rule_name": "商品表",
    },
]

target_fields = [
    {"rule_code": "UR001", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR001", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"},
    {"rule_code": "UR001", "target_field": "category_id", "source_field": "category_id", "field_type": "bigint"},
    {"rule_code": "UR001", "target_field": "price", "source_field": "price", "field_type": "decimal(18,2)"},
    {"rule_code": "UR001", "target_field": "status", "source_field": "status", "field_type": "varchar(20)"},
]

group_variables = [
    {"rule_code": "UR001", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
