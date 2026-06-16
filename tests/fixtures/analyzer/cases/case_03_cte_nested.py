"""Case 03: 嵌套 CTE（CTE_A 引用 CTE_B）— 验证 CTE 嵌套递归穿透。

场景：WITH base AS(...), agg AS(SELECT ... FROM base ...)，主查询 JOIN agg。
预期：CTE 数量=2，agg.total 的穿透链应展开到 base 的源字段。
"""

rules = [
    {
        "rule_code": "UR003",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_nested_f",
        "delete_mode": "1",
        "query_sql": """WITH base AS (
    SELECT
        user_id,
        amount
    FROM ods.orders
    WHERE status = 'OK'
),
agg AS (
    SELECT
        user_id,
        SUM(amount) AS total
    FROM base
    GROUP BY user_id
)
SELECT
    u.user_name,
    agg.total
FROM dim.dim_user u
INNER JOIN agg
    ON u.user_id = agg.user_id""",
        "rule_group_code": "GR003",
        "rule_name": "嵌套CTE测试",
    },
]

target_fields = [
    {"rule_code": "UR003", "target_field": "user_name", "source_field": "user_name", "field_type": "varchar(100)"},
    {"rule_code": "UR003", "target_field": "total", "source_field": "total", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR003", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
