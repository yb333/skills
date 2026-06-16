"""Case 19: 多场景2分区写入 — 2个场景各2步串行 + 1公共步骤。

场景A (part_fwd): UR001(seq=0) → UR003(seq=1)
场景B (part_cr):  UR002(seq=0) → UR004(seq=1)
公共:            UR005(seq=2, TRUNCATE TABLE)

验证点: 场景数=3(2场景+公共), 字段映射按场景分组不串
"""

rules = [
    {
        "rule_code": "UR001", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "fin_dwl_cnb", "target_table": "dwl_inv_intermediate_f",
        "delete_mode": "5", "delete_condition": "part_fwd",
        "query_sql": """SELECT
    inv.inv_id, inv.contract_no, inv.amount_usd, inv.amount_rmb
FROM fin_dwb_cnb.dwb_inv_fwd_head_i inv
WHERE inv.del_flag = 'N'""",
        "rule_group_code": "GR001", "rule_name": "正向发票加载",
    },
    {
        "rule_code": "UR002", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "fin_dwl_cnb", "target_table": "dwl_inv_intermediate_f",
        "delete_mode": "5", "delete_condition": "part_cr",
        "query_sql": """SELECT
    inv.inv_id, inv.contract_no, -inv.amount_usd AS amount_usd, -inv.amount_rmb AS amount_rmb
FROM fin_dwb_cnb.dwb_inv_cr_head_i inv
WHERE inv.del_flag = 'N'""",
        "rule_group_code": "GR002", "rule_name": "贷项发票加载",
    },
    {
        "rule_code": "UR003", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "fin_dwl_cnb", "target_table": "dwl_inv_final_f",
        "delete_mode": "5", "delete_condition": "part_fwd",
        "query_sql": """SELECT
    t.contract_no, SUM(t.amount_usd) AS total_usd, SUM(t.amount_rmb) AS total_rmb
FROM fin_dwl_cnb.dwl_inv_intermediate_f PARTITION(part_fwd) t
WHERE t.del_flag = 'N'
GROUP BY t.contract_no""",
        "rule_group_code": "GR001", "rule_name": "正向发票汇总",
    },
    {
        "rule_code": "UR004", "rule_type": 1, "exec_sequence": 1,
        "target_schema": "fin_dwl_cnb", "target_table": "dwl_inv_final_f",
        "delete_mode": "5", "delete_condition": "part_cr",
        "query_sql": """SELECT
    t.contract_no, SUM(t.amount_usd) AS total_usd, SUM(t.amount_rmb) AS total_rmb
FROM fin_dwl_cnb.dwl_inv_intermediate_f PARTITION(part_cr) t
WHERE t.del_flag = 'N'
GROUP BY t.contract_no""",
        "rule_group_code": "GR002", "rule_name": "贷项发票汇总",
    },
    {
        "rule_code": "UR005", "rule_type": 1, "exec_sequence": 2,
        "target_schema": "fin_dwl_cnb", "target_table": "dwl_inv_summary_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": """SELECT
    contract_no, SUM(total_usd) AS grand_total_usd, SUM(total_rmb) AS grand_total_rmb
FROM fin_dwl_cnb.dwl_inv_final_f
WHERE del_flag = 'N'
GROUP BY contract_no""",
        "rule_group_code": "GR003", "rule_name": "发票汇总",
    },
]

target_fields = [
    {"rule_code": "UR001", "target_field": "inv_id", "source_field": "inv_id", "field_type": "bigint"},
    {"rule_code": "UR001", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR001", "target_field": "amount_usd", "source_field": "amount_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR001", "target_field": "amount_rmb", "source_field": "amount_rmb", "field_type": "decimal(18,2)"},
    {"rule_code": "UR002", "target_field": "inv_id", "source_field": "inv_id", "field_type": "bigint"},
    {"rule_code": "UR002", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR002", "target_field": "amount_usd", "source_field": "amount_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR002", "target_field": "amount_rmb", "source_field": "amount_rmb", "field_type": "decimal(18,2)"},
    {"rule_code": "UR003", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR003", "target_field": "total_usd", "source_field": "amount_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR003", "target_field": "total_rmb", "source_field": "amount_rmb", "field_type": "decimal(18,2)"},
    {"rule_code": "UR004", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR004", "target_field": "total_usd", "source_field": "amount_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR004", "target_field": "total_rmb", "source_field": "amount_rmb", "field_type": "decimal(18,2)"},
    {"rule_code": "UR005", "target_field": "contract_no", "source_field": "contract_no", "field_type": "varchar(100)"},
    {"rule_code": "UR005", "target_field": "grand_total_usd", "source_field": "total_usd", "field_type": "decimal(18,2)"},
    {"rule_code": "UR005", "target_field": "grand_total_rmb", "source_field": "total_rmb", "field_type": "decimal(18,2)"},
]

group_variables = [
    {"rule_code": "UR001", "var_name": "P_CYCLE_ID", "default_value": "20260101"},
]
