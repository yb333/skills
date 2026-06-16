"""Case 02: 单层 CTE — 验证 CTE 提取能力。

场景：1 step，1 CTE（agg），主查询 JOIN CTE。
预期：CTE 数量=1，CTE agg.total 的 transform_type=aggregate。
"""

rules = [
    {
        "rule_code": "UR002",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_sales_agg_f",
        "delete_mode": "1",
        "query_sql": """WITH agg AS (
    SELECT
        region_id,
        SUM(amount) AS total
    FROM ods.sales
    GROUP BY region_id
)
SELECT
    r.region_name,
    agg.total
FROM dim.dim_region r
INNER JOIN agg
    ON r.region_id = agg.region_id""",
        "rule_group_code": "GR002",
        "rule_name": "销售汇总表",
    },
]

target_fields = [
    {"rule_code": "UR002", "target_field": "region_name", "source_field": "region_name", "field_type": "varchar(100)"},
    {"rule_code": "UR002", "target_field": "total", "source_field": "total", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR002", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
