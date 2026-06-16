"""Case 04: 行转列（SUM + CASE WHEN）— 验证 pivot 类型识别。

场景：1 step，3 个 SUM(CASE WHEN...) 行转列字段。
预期：3 个字段 transform_type=pivot，total_case_when_branches=3。
"""

rules = [
    {
        "rule_code": "UR004",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_pivot_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    t.product_id,
    SUM(CASE WHEN t.month = '202401' THEN t.amount ELSE 0 END) AS jan_amt,
    SUM(CASE WHEN t.month = '202402' THEN t.amount ELSE 0 END) AS feb_amt,
    SUM(CASE WHEN t.month = '202403' THEN t.amount ELSE 0 END) AS mar_amt
FROM ods.monthly_sales t
WHERE t.del_flag = 'N'
GROUP BY t.product_id""",
        "rule_group_code": "GR004",
        "rule_name": "行转列测试",
    },
]

target_fields = [
    {"rule_code": "UR004", "target_field": "product_id", "source_field": "product_id", "field_type": "bigint"},
    {"rule_code": "UR004", "target_field": "jan_amt", "source_field": "amount", "field_type": "decimal(18,2)"},
    {"rule_code": "UR004", "target_field": "feb_amt", "source_field": "amount", "field_type": "decimal(18,2)"},
    {"rule_code": "UR004", "target_field": "mar_amt", "source_field": "amount", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR004", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
