"""构造模拟代码仓目录结构的测试工具。

完整还原代码仓结构 + 各种真实场景和干扰项，用于端到端测试代码仓 yml 分析。

代码仓结构（参照真实结构）:
    repo/
    ├── BFT/
    │   ├── BftMetric/          ← 指标（干扰项：不同类型，不应被分析）
    │   │   └── 项目/子项目/规则组/*.yml
    │   └── BftWideTable/       ← 宽表（我们的数据源）
    │       └── 项目/子项目/规则组/*.yml
    ├── DDL/
    │   ├── DWS_EDW/            ← 离线层
    │   │   └── schema/table/*.sql
    │   │                /view/*.sql
    │   └── DWS_RT_EDW/         ← 实时层（同名表在实时层的干扰项）
    │       └── schema/table/*.sql
    ├── DQ/                     ← 数据质量（干扰项）
    ├── LTS/                    ← 任务调度（干扰项）
    ├── ADMS/                   ← 跨库集成（干扰项）
    └── Release/                ← 每月增量变更（干扰项：可能含同名 yml）

使用:
    from _build_repo import build_mock_repo
    repo_dir = build_mock_repo("/tmp/repo/")  # 返回代码仓根路径
"""

import yaml
from pathlib import Path

from _build_yml import build_yml_group


def build_mock_repo(base_dir):
    """构造一个完整的模拟代码仓，含正常场景 + 干扰项。

    Args:
        base_dir: 代码仓根目录路径（str 或 Path）

    Returns:
        dict: 关键路径 {
            "repo_root": 代码仓根,
            "group_dir": 目标规则组目录（要分析的）,
            "ddl_dir": 目标表的 DDL 目录（应被自动发现）,
            "ddl_file": 目标表 DDL 文件路径,
            "other_group_dir": 另一个规则组（用于多规则组场景）,
        }
    """
    repo = Path(base_dir)

    # ════════════════════════════════════════════════════════════
    # 1. BFT/BftWideTable（宽表 = 我们的数据源）
    #    完整层级：项目/子项目/规则组/规则yml
    # ════════════════════════════════════════════════════════════
    # 主测试规则组：2步串行（tmp中间表 → 最终表）
    group1_dir = (repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
                  / "DWB_TRADE_ORDER_D")
    build_yml_group(group1_dir, rules=[
        {"rule_code": "R0001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "tmp_trade_order", "delete_mode": "1",
         "query_sql": "SELECT a.order_id, a.cust_id, a.amount "
                      "FROM ods.ods_trade_order_di a WHERE a.del='N'",
         "rule_name": "订单明细", "rule_group_code": "GR_TRADE_ORDER",
         "rule_group_en": "DWB_TRADE_ORDER_D",
         "target_fields": [
             {"rule_code": "R0001", "target_field": "order_id", "source_field": "a.order_id",
              "field_type": "VARCHAR(64)", "remark": "订单ID"},
             {"rule_code": "R0001", "target_field": "cust_id", "source_field": "a.cust_id",
              "field_type": "VARCHAR(64)", "remark": "客户ID"},
         ]},
        {"rule_code": "R0002", "rule_type": 1, "exec_sequence": 2,
         "target_schema": "dws", "target_table": "dwb_trade_order_d", "delete_mode": "5",
         "query_sql": "SELECT t.order_id, t.cust_id, SUM(t.amount) AS total_amount "
                      "FROM dws.tmp_trade_order t GROUP BY t.order_id, t.cust_id",
         "rule_name": "订单汇总", "rule_group_code": "GR_TRADE_ORDER",
         "rule_group_en": "DWB_TRADE_ORDER_D",
         "target_fields": [
             {"rule_code": "R0002", "target_field": "order_id", "source_field": "t.order_id",
              "field_type": "VARCHAR(64)"},
             {"rule_code": "R0002", "target_field": "total_amount", "source_field": "t.amount",
              "field_type": "DECIMAL(18,2)", "remark": "订单总额"},
         ]},
    ])

    # 第二个规则组（不同项目/子项目，用于测试目录遍历不串）
    group2_dir = (repo / "BFT" / "BftWideTable" / "P_RISK" / "SUB_RISK"
                  / "DWB_RISK_ALERT_F")
    build_yml_group(group2_dir, rules=[
        {"rule_code": "R1001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "dwb_risk_alert_f", "delete_mode": "1",
         "query_sql": "SELECT a.alert_id, a.risk_level FROM ods.ods_risk_alert a",
         "rule_name": "风险预警", "rule_group_code": "GR_RISK_ALERT",
         "rule_group_en": "DWB_RISK_ALERT_F"},
    ])

    # ════════════════════════════════════════════════════════════
    # 2. BFT/BftMetric（指标 = 干扰项，结构和宽表类似但不应被分析）
    # ════════════════════════════════════════════════════════════
    metric_dir = (repo / "BFT" / "BftMetric" / "P_TRADE" / "SUB_TRADE"
                  / "METRIC_TRADE_AMOUNT")
    build_yml_group(metric_dir, rules=[
        {"rule_code": "M0001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "metric_trade_amount", "delete_mode": "1",
         "query_sql": "SELECT count(*) AS cnt FROM dws.dwb_trade_order_d",
         "rule_name": "交易金额指标", "rule_group_code": "GR_METRIC",
         "rule_group_en": "METRIC_TRADE_AMOUNT"},
    ])

    # ════════════════════════════════════════════════════════════
    # 3. DDL/DWS_EDW（离线层 = 目标表 DDL 应该在这里被找到）
    # ════════════════════════════════════════════════════════════
    ddl_table_dir = repo / "DDL" / "DWS_EDW" / "dws" / "table"
    ddl_table_dir.mkdir(parents=True, exist_ok=True)
    # 目标表 DDL（标准 COMMENT ON COLUMN 格式，parse_ddl_for_metadata 能完整解析）
    (ddl_table_dir / "dwb_trade_order_d.sql").write_text(
        "-- 订单汇总表\n"
        "CREATE TABLE dws.dwb_trade_order_d (\n"
        "  order_id VARCHAR(64),\n"
        "  cust_id VARCHAR(64),\n"
        "  total_amount DECIMAL(18,2)\n"
        ");\n"
        "COMMENT ON COLUMN dws.dwb_trade_order_d.order_id IS '订单ID';\n"
        "COMMENT ON COLUMN dws.dwb_trade_order_d.cust_id IS '客户ID';\n"
        "COMMENT ON COLUMN dws.dwb_trade_order_d.total_amount IS '订单总额';\n"
        "COMMENT ON TABLE dws.dwb_trade_order_d IS '订单汇总';\n",
        encoding="utf-8")
    # 中间表 DDL
    (ddl_table_dir / "tmp_trade_order.sql").write_text(
        "CREATE TABLE dws.tmp_trade_order (\n  order_id VARCHAR(64)\n);",
        encoding="utf-8")
    # 第二个规则组的 DDL
    (ddl_table_dir / "dwb_risk_alert_f.sql").write_text(
        "CREATE TABLE dws.dwb_risk_alert_f (\n  alert_id VARCHAR(64)\n);",
        encoding="utf-8")
    # 视图 DDL（干扰项：在 view/ 目录下）
    ddl_view_dir = repo / "DDL" / "DWS_EDW" / "dws" / "view"
    ddl_view_dir.mkdir(parents=True, exist_ok=True)
    (ddl_view_dir / "dwb_trade_order_d_v.sql").write_text(
        "CREATE VIEW dws.dwb_trade_order_d_v AS SELECT * FROM dws.dwb_trade_order_d;",
        encoding="utf-8")

    # ════════════════════════════════════════════════════════════
    # 4. DDL/DWS_RT_EDW（实时层 = 干扰项，可能有同名表）
    # ════════════════════════════════════════════════════════════
    rt_ddl_dir = repo / "DDL" / "DWS_RT_EDW" / "dws" / "table"
    rt_ddl_dir.mkdir(parents=True, exist_ok=True)
    # 实时层同名表（测试 _auto_discover_ddl 先找到离线层 DWS_EDW）
    (rt_ddl_dir / "dwb_trade_order_d.sql").write_text(
        "CREATE TABLE dws.dwb_trade_order_d (\n  order_id VARCHAR(32)\n);",
        encoding="utf-8")

    # ════════════════════════════════════════════════════════════
    # 5. 干扰项：DQ / LTS / ADMS / Release
    # ════════════════════════════════════════════════════════════
    # DQ：数据质量规则 yml（结构类似但不是我们的执行平台数据）
    dq_dir = repo / "DQ" / "P_TRADE" / "SUB_TRADE" / "DQ_TRADE_ORDER"
    dq_dir.mkdir(parents=True, exist_ok=True)
    (dq_dir / "DQ0001.yml").write_text(
        yaml.dump({"规则编码": "DQ0001", "规则类型": "1",
                   "(生成的)查询语句": "SELECT count(*) FROM dws.dwb_trade_order_d"},
                  allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    # LTS：任务调度（干扰项，不含我们要的 yml）
    (repo / "LTS" / "P_TRADE").mkdir(parents=True, exist_ok=True)
    (repo / "LTS" / "P_TRADE" / "schedule.yml").write_text(
        yaml.dump({"任务编码": "TASK_TRADE", "调度类型": "daily"}, allow_unicode=True),
        encoding="utf-8")

    # ADMS：跨库集成（干扰项）
    (repo / "ADMS").mkdir(parents=True, exist_ok=True)

    # Release：每月增量变更（干扰项，可能含和规则组同名的目录/yml）
    release_dir = (repo / "Release" / "202401" / "BFT" / "BftWideTable"
                   / "P_TRADE" / "SUB_TRADE" / "DWB_TRADE_ORDER_D")
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "R0001.yml").write_text(
        yaml.dump({"规则编码": "R0001", "规则类型": "1",
                   "(生成的)查询语句": "SELECT 1",  # Release 里的旧版本 SQL
                   "目标表": "dwb_trade_order_d"},
                  allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    # ════════════════════════════════════════════════════════════
    # 6. I 视图测试数据（F 表 → I 视图链路）
    #    规则组写 _f 表，对应 _i 视图在 DDL/.../view/ 下
    # ════════════════════════════════════════════════════════════
    # F 表规则组
    f_group_dir = (repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
                   / "DWB_TRADE_SUM_F")
    build_yml_group(f_group_dir, rules=[
        {"rule_code": "F001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "dwb_trade_sum_f", "delete_mode": "1",
         "query_sql": "SELECT a.cust_id, SUM(a.amount) AS total FROM ods.ods_trade a GROUP BY a.cust_id",
         "rule_name": "交易汇总", "rule_group_code": "GR_TRADE_SUM",
         "rule_group_en": "DWB_TRADE_SUM_F"},
    ])

    # F 表 DDL
    (ddl_table_dir / "dwb_trade_sum_f.sql").write_text(
        "CREATE TABLE dws.dwb_trade_sum_f (\n"
        "  cust_id VARCHAR(64),\n"
        "  total DECIMAL(18,2)\n"
        ");\n"
        "COMMENT ON COLUMN dws.dwb_trade_sum_f.cust_id IS '客户ID';\n"
        "COMMENT ON COLUMN dws.dwb_trade_sum_f.total IS '交易总额';\n",
        encoding="utf-8")

    # I 视图（直封：SELECT * FROM F 表）—— 命名规则 _f → _i
    ddl_view_dir = repo / "DDL" / "DWS_EDW" / "dws" / "view"
    ddl_view_dir.mkdir(parents=True, exist_ok=True)
    (ddl_view_dir / "dwb_trade_sum_i.sql").write_text(
        "CREATE OR REPLACE VIEW dws.dwb_trade_sum_i AS\n"
        "SELECT cust_id, total FROM dws.dwb_trade_sum_f;\n",
        encoding="utf-8")

    # I 视图（有逻辑的，命名不规律）—— 用于测试全局搜索来源表
    (ddl_view_dir / "v_trade_summary.sql").write_text(
        "CREATE VIEW dws.v_trade_summary AS\n"
        "SELECT cust_id, total, 'Y' AS is_active FROM dws.dwb_risk_alert_f;\n",
        encoding="utf-8")

    return {
        "repo_root": repo,
        "group_dir": group1_dir,
        "ddl_dir": ddl_table_dir,
        "ddl_file": ddl_table_dir / "dwb_trade_order_d.sql",
        "other_group_dir": group2_dir,
        "metric_group_dir": metric_dir,
        # I 视图测试数据
        "f_group_dir": f_group_dir,
        "f_table": "dwb_trade_sum_f",
        "i_view_name": "dwb_trade_sum_i",
        "ddl_view_dir": ddl_view_dir,
    }
