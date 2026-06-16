"""Case 05: 窗口函数 — 验证 window 类型识别。

场景：ROW_NUMBER() OVER + LAG() OVER。
预期：rn 和 prev_login 的 transform_type=window。
"""

rules = [
    {
        "rule_code": "UR005",
        "rule_type": 1,
        "exec_sequence": 1,
        "target_schema": "dws",
        "target_table": "dwb_window_f",
        "delete_mode": "1",
        "query_sql": """SELECT
    t.user_id,
    t.login_date,
    ROW_NUMBER() OVER (PARTITION BY t.user_id ORDER BY t.login_date DESC) AS rn,
    LAG(t.login_date) OVER (PARTITION BY t.user_id ORDER BY t.login_date) AS prev_login
FROM ods.user_login t
WHERE t.del_flag = 'N'""",
        "rule_group_code": "GR005",
        "rule_name": "窗口函数测试",
    },
]

target_fields = [
    {"rule_code": "UR005", "target_field": "user_id", "source_field": "user_id", "field_type": "bigint"},
    {"rule_code": "UR005", "target_field": "login_date", "source_field": "login_date", "field_type": "date"},
    {"rule_code": "UR005", "target_field": "rn", "source_field": "login_date", "field_type": "int"},
    {"rule_code": "UR005", "target_field": "prev_login", "source_field": "login_date", "field_type": "date"},
]

group_variables = [
    {"rule_code": "UR005", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
