"""Case 13: 字段别名不匹配 — TargetFields vs SQL alias 精确字符串不匹配。

预期：differences 非空，only_in_excel 含 'ProductID'（大小写不一致）。
"""

rules = [
    {
        "rule_code": "UR013",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_mismatch_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    p.product_name
FROM dim.dim_product p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR013",
        "rule_name": "字段不匹配测试",
    },
]

# 故意用大小写不一致的名字和额外字段，触发 mismatch
target_fields = [
    {"rule_code": "UR013", "target_field": "ProductID", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR013", "target_field": "PRODUCT_NAME", "source_field": "product_name", "field_type": "varchar(200)"},
    {"rule_code": "UR013", "target_field": "extra_field", "source_field": "nonexistent", "field_type": "varchar(50)"},
]

group_variables = [
    {"rule_code": "UR013", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
