"""构造代码仓 yml 规则组目录的测试工具（对应 _build_xlsx.py 的 yml 版本）。

代码仓 yml 格式：一个 yml 文件 = 一条规则，一个规则组目录 = 多个 yml。
格式参照 sample_rule.yml（执行平台 Excel 转化）。

使用:
    from _build_yml import build_yml_group
    build_yml_group("/tmp/DWB_TEST_F/", rules=[...])
"""

import yaml
from pathlib import Path


# yml key（中文，和执行平台 Excel 表头对应）
_YML_RULE_KEYS = {
    "rule_code": "规则编码",
    "rule_name": "规则中文名称",
    "rule_type": "规则类型",
    "exec_sequence": "执行序列",
    "target_schema": "目标Schema",
    "target_table": "目标表",
    "delete_mode": "删除模式",
    "delete_condition": "删除条件",
    "query_sql": "(生成的)查询语句",
    "project_code": "项目编码",
    "data_source": "数据源",
    "business_owner": "业务责任人",
    "rule_group_code": "规则组编码",
    "rule_group_en": "规则组英文名称",
    "exchange_source_table": "交换分区来源表",
}


def build_yml_group(group_dir, rules):
    """在 group_dir 目录下生成多个 yml 文件（一个规则一个）。

    Args:
        group_dir: 规则组目录路径（str 或 Path）
        rules: 规则列表，每项是 dict，key 同 _build_xlsx 的 rules（英文 key）
               如 {"rule_code": "R001", "rule_type": 1, "query_sql": "SELECT ...", ...}
               支持 target_fields 子列表和 group_variables 子列表（嵌入额外信息）

    生成的 yml 结构和真实代码仓一致：
        顶层 = RULE 字段
        额外信息（其他sheet页信息）.TargetFields = [...]
        额外信息（其他sheet页信息）.GroupVariables = [...]
    """
    group_dir = Path(group_dir)
    group_dir.mkdir(parents=True, exist_ok=True)

    for rule in rules:
        rc = rule.get("rule_code", "R0001")
        yml_path = group_dir / f"{rc}.yml"

        # 顶层 RULE 字段
        data = {}
        for eng_key, yml_key in _YML_RULE_KEYS.items():
            if eng_key in rule:
                val = rule[eng_key]
                # 数字类型加引号（模拟真实 yml 序列化：'1'/'5' 等）
                if eng_key in ("rule_type", "exec_sequence", "delete_mode") and val != "":
                    data[yml_key] = str(val)
                else:
                    data[yml_key] = val

        # 额外信息（TargetFields / GroupVariables）
        extra = {}
        tfs = rule.get("target_fields")
        if tfs:
            extra["TargetFields"] = [
                {
                    "规则编码": tf.get("rule_code", rc),
                    "目标字段名称": tf.get("target_field", ""),
                    "来源字段名称": tf.get("source_field", ""),
                    "加密方式": str(tf.get("encryption", "0")),
                    "别名": tf.get("alias", ""),
                    "字段类型": tf.get("field_type", ""),
                    "备注": tf.get("remark", ""),
                }
                for tf in tfs
            ]
        gvs = rule.get("group_variables")
        if gvs:
            extra["GroupVariables"] = [
                {
                    "规则编码": gv.get("rule_code", rc),
                    "动态参数/变量名": gv.get("var_name", ""),
                    "变量默认值": gv.get("default_value", ""),
                }
                for gv in gvs
            ]
        if extra:
            data["额外信息（其他sheet页信息）"] = extra

        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return str(group_dir)
