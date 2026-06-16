"""Case 17: 多 CTE（4个）— 触发复杂度告警。

预期：ISS: CTE 数量 4，嵌套过深（severity=medium）。
"""

rules = [
    {
        "rule_code": "UR017",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_many_cte_f",
        "delete_mode": "1",
        "query_sql": """WITH cte1 AS (
    SELECT user_id, amount FROM ods.orders WHERE status = 'A'
),
cte2 AS (
    SELECT user_id, SUM(amount) AS total FROM cte1 GROUP BY user_id
),
cte3 AS (
    SELECT user_id, COUNT(*) AS cnt FROM ods.events GROUP BY user_id
),
cte4 AS (
    SELECT user_id, MAX(login_date) AS last_login FROM ods.logins GROUP BY user_id
)
SELECT
    u.user_name,
    c2.total,
    c3.cnt,
    c4.last_login
FROM dim.dim_user u
LEFT JOIN cte2 c2 ON u.user_id = c2.user_id
LEFT JOIN cte3 c3 ON u.user_id = c3.user_id
LEFT JOIN cte4 c4 ON u.user_id = c4.user_id""",
        "rule_group_code": "GR017",
        "rule_name": "多CTE测试",
    },
]

target_fields = [
    {"rule_code": "UR017", "target_field": "user_name", "source_field": "user_name", "field_type": "varchar(100)"},
    {"rule_code": "UR017", "target_field": "total", "source_field": "amount", "field_type": "decimal(18,2)"},
    {"rule_code": "UR017", "target_field": "cnt", "source_field": "cnt", "field_type": "int"},
    {"rule_code": "UR017", "target_field": "last_login", "source_field": "login_date", "field_type": "date"},
]

group_variables = [
    {"rule_code": "UR017", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
