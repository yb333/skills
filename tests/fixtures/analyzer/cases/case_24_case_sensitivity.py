"""Case 24: 大小写不一致 — SQL 里字段大写，TargetFields 里小写。

验证大小写归一化：
- SQL: SELECT t.CONTRACT_NO, t.USER_ID ...
- TargetFields: contract_no, user_id ...
- 应该匹配成功，不产生重复字段
"""

rules = [
    {
        "rule_code": "UR024", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "DWS", "target_table": "DWB_CASE_TEST_F",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": """SELECT
    t.CONTRACT_NO,
    t.USER_ID,
    t.AMOUNT_USD,
    t.AMOUNT_RMB,
    f.PROJ_NAME,
    'N' AS DEL_FLAG,
    CURRENT_TIMESTAMP AS DW_LAST_UPDATE_DATE
FROM ODS.SOURCE_TABLE_A t
LEFT JOIN ODS.SOURCE_TABLE_B f
    ON t.PROJ_ID = f.PROJ_ID
WHERE t.DEL_FLAG = 'N'""",
        "rule_group_code": "GR001", "rule_name": "大小写测试",
    },
]

# TargetFields 全小写
target_fields = [
    {"rule_code": "UR024", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR024", "target_field": "user_id", "source_field": "user_id", "field_type": "bigint"},
    {"rule_code": "UR024", "target_field": "amount_usd", "source_field": "amount_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR024", "target_field": "amount_rmb", "source_field": "amount_rmb", "field_type": "decimal(18,2)"},
    {"rule_code": "UR024", "target_field": "proj_name", "source_field": "proj_name", "field_type": "varchar(200)"},
]

group_variables = []
