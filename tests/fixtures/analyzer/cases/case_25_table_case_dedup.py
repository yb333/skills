"""Case 25: 表名大小写不一致 — SQL 大写 vs topology 小写。

验证:
- data_flow.tables 不重复（大小写归一化去重）
- data_dependencies 不因大小写丢失
- 数据流图节点不重复
"""

rules = [
    {
        "rule_code": "UR025A", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "DWS", "target_table": "DWB_TEMP_F",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": """SELECT
    t.CONTRACT_NO,
    t.AMOUNT
FROM ODS.SOURCE_TABLE_A t
WHERE t.DEL_FLAG = 'N'""",
        "rule_group_code": "GR001", "rule_name": "加载到临时表",
    },
    {
        "rule_code": "UR025B", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "DWS", "target_table": "DWB_FINAL_F",
        "delete_mode": "1", "delete_condition": "",
        # SQL 里用大写引用上一步的小写表名（模拟大小写不一致）
        "query_sql": """SELECT
    t.CONTRACT_NO,
    SUM(t.AMOUNT) AS TOTAL_AMOUNT
FROM dws.dwb_temp_f t
GROUP BY t.CONTRACT_NO""",
        "rule_group_code": "GR001", "rule_name": "汇总",
    },
]

target_fields = [
    {"rule_code": "UR025A", "target_field": "contract_no", "source_field": "contract_no"},
    {"rule_code": "UR025A", "target_field": "amount", "source_field": "amount"},
    {"rule_code": "UR025B", "target_field": "contract_no", "source_field": "contract_no"},
    {"rule_code": "UR025B", "target_field": "total_amount", "source_field": "amount"},
]

group_variables = []
