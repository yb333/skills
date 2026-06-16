"""Case 10: 视图步骤 — step_1 INSERT _f 表，step_2 CREATE VIEW _i AS SELECT FROM _f。

预期：step_2 write_mode=APPEND，data_dependencies 含 step_1→step_2。
"""

rules = [
    {
        "rule_code": "UR010A",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_product_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    p.product_id,
    p.product_name,
    p.price
FROM dim.dim_product p
WHERE p.del_flag = 'N'""",
        "rule_group_code": "GR010",
        "rule_name": "商品事实表",
    },
    {
        "rule_code": "UR010B",
        "rule_type": 1,
        "exec_sequence": 2,
        "target_schema": "dws",
        "target_table": "dwb_product_i",
        "delete_mode": "0",
        "query_sql": """CREATE OR REPLACE VIEW dws.dwb_product_i AS
SELECT
    product_id,
    product_name,
    price
FROM dws.dwb_product_f""",
        "rule_group_code": "GR010",
        "rule_name": "商品消费视图",
    },
]

target_fields = [
    {"rule_code": "UR010A", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR010A", "target_field": "product_name", "source_field": "product_name", "field_type": "varchar(200)"},
    {"rule_code": "UR010A", "target_field": "price", "source_field": "price", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR010A", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
