"""Case 12: Oracle 方言 — NVL/DECODE 检测。

预期：dialect=oracle，NVL(x, 0) 识别为 fallback。
"""

rules = [
    {
        "rule_code": "UR012",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_oracle_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    NVL(p.product_name, 'UNKNOWN') AS product_name,
    NVL(p.price, 0) AS price,
    DECODE(p.status, 1, 'ACTIVE', 0, 'INACTIVE', 'UNKNOWN') AS status
FROM dim_product p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR012",
        "rule_name": "Oracle方言测试",
    },
]

target_fields = [
    {"rule_code": "UR012", "target_field": "product_id", "source_field": "product_id", "field_type": "NUMBER"},
    {"rule_code": "UR012", "target_field": "product_name", "source_field": "product_name", "field_type": "VARCHAR2(200)"},
    {"rule_code": "UR012", "target_field": "price", "source_field": "price", "field_type": "NUMBER"},
    {"rule_code": "UR012", "target_field": "status", "source_field": "status", "field_type": "VARCHAR2(20)"},
]

group_variables = [
    {"rule_code": "UR012", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
