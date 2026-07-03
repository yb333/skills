#!/usr/bin/env python3
"""dws-pipeline-analyzer — 制品包分析器（数据层 + CLI）。

三层架构（详见 architecture.md）：
    ① 数据层（analyzer.py）— 本模块：read_excel / CLI
    ② 理解引擎（engine.py）— SQL 理解与血缘解析
    ③ 任务层 — 文档化 / 字段检索 / 关联影响分析 / ...

本模块职责：
    - read_excel()  读取 execution_tasks.xlsx → rules / target_fields / 配置
    - main()        CLI 入口，调 engine.analyze_pipeline + 写文件
    - _generate_ai_summary()  AI 兜底摘要生成

Usage:
    python analyzer.py --input execution_tasks.xlsx --output docs/output/
    python analyzer.py --input execution_tasks.xlsx --output docs/ --ddl-dir 04_ddl/

Author: 院博
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import openpyxl
except ImportError:
    print("错误: 需要 openpyxl。pip install openpyxl", file=sys.stderr)
    sys.exit(1)

# 引擎层（单向依赖：analyzer → engine）
import re
from engine import (
    analyze_pipeline,
    detect_dialect, parse_single_sql,
    ParsedSQL, RawRule,
    SELECT_RULE_TYPES, RULE_TYPE_MAP,
    _strip_dws_clauses, _replace_placeholders,
    _normalize_table_name, _norm_table, _is_intermediate_table,
    _table_match, _infer_layer, _clean_name,
)

# ═══════════════════════════════════════════════════════════════
# Excel 列映射（read_excel 专用）
# ═══════════════════════════════════════════════════════════════

RULE_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "exec_sequence": "执行序列",
    "target_schema": "目标Schema",
    "target_table": "目标表",
    "delete_mode": "删除模式",
    "delete_condition": "删除条件",
    "query_sql": "(生成的）查询语句1",
    "project_code": "项目编码",
    "data_source": "数据源",
    "business_owner": "业务责任人",
    "rule_group_code": "规则组编码",
    "rule_group_en": "规则组英文名称",
    "rule_type": "规则类型",
    "rule_name": "规则中文名称",
    "exchange_source_table": "交换分区来源表",
}

# 规则类型语义映射
RULE_TYPE_MAP = {
    1: "取数规则",
    2: "删数规则",
    3: "备份规则",
    4: "查询规则",
    5: "逻辑视图",
    6: "物理视图",
    7: "度量规则",
    8: "物理表规则",
    9: "分区交换",
    10: "SP规则",
    11: "API规则",
    12: "参数变量",
    13: "维护类",
    14: "Spark取数",
    15: "判断类",
}

# SELECT 类规则（需要完整解析 SQL + 字段映射 + 血缘）
SELECT_RULE_TYPES = {1, 14}

# 记录类规则（不解析 SQL，但记录操作信息）
RECORD_RULE_TYPES = {2, 9}

# 参数变量（记录到 variables，不算 step）
VARIABLE_RULE_TYPES = {12}

# 删除模式语义映射
DELETE_MODE_MAP = {
    "1": "TRUNCATE TABLE",
    "2": "NO DELETE (追加)",
    "3": "TRUNCATE SUBPARTITION",
    "4": "DELETE",
    "5": "TRUNCATE PARTITION",
    "6": "MERGE INTO",
    "7": "RPT_ITEM",
}

# 分区级删除模式（有分区场景标识）
PARTITION_DELETE_MODES = {"3", "5"}

# TargetFields sheet 列名
TF_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "target_field": "目标字段名称",
    "source_field": "来源字段名称",
    "encryption": "加密方式",
    "alias": "别名",
    "field_type": "字段类型",
    "remark": "备注",
}

# GroupVariables sheet 列名
GV_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "var_name": "动态参数/变量名",
    "default_value": "变量默认值",
}

# ═══════════════════════════════════════════════════════════════
# read_excel 专用辅助函数（其余工具函数已在 engine.py）
# ═══════════════════════════════════════════════════════════════

def _safe_str(val) -> str:
    """安全转字符串"""
    if val is None:
        return ""
    return str(val).strip()


def _find_col(col_idx: dict, name: str) -> int | None:
    """查找列索引。先精确匹配，再模糊匹配（去空格+全角半角归一化）。"""
    # 精确匹配
    if name in col_idx:
        return col_idx[name]
    # 模糊匹配：去空格、全角括号转半角
    def normalize(s):
        # 全角括号 → 半角
        s = s.replace("（", "(").replace("）", ")")
        # 去所有空格
        s = s.replace(" ", "").replace("\u3000", "")
        return s
    norm_name = normalize(name)
    for actual, idx in col_idx.items():
        if normalize(actual) == norm_name:
            return idx
    # 包含匹配（期望列名是实际列名的子串，或反过来）
    for actual, idx in col_idx.items():
        na = normalize(actual)
        if norm_name in na or na in norm_name:
            return idx
    return None


def _get_val(row: tuple, idx: int | None) -> str:
    """安全获取行值"""
    if idx is None or idx >= len(row):
        return ""
    val = row[idx]
    return _safe_str(val)


def _read_query_sql(row: tuple, col_idx: dict) -> str:
    """读取查询语句（支持多列拼接）。

    超长 SQL 会分散在「查询语句1」「查询语句2」... 多列。
    按列序号拼接非空内容，最后一个非空列去掉末尾 \\r\\n。
    不 strip 中间列（保留列内的空格和换行，避免拼接时 SQL 断裂）。
    """
    import re as _re
    # 找所有「查询语句N」列，按 N 排序
    sql_cols = []
    for col_name, idx in col_idx.items():
        m = _re.match(r'.*查询语句\s*(\d+)', str(col_name))
        if m:
            sql_cols.append((int(m.group(1)), idx))
    sql_cols.sort()

    if not sql_cols:
        return ""

    # 直接读取原始值（不走 _get_val 的 strip），保留列内空格换行
    parts = []
    for _, idx in sql_cols:
        if idx is None or idx >= len(row):
            continue
        val = row[idx]
        if val is None:
            continue
        val_str = str(val)
        if val_str.strip():  # 整列只有空白/换行则跳过，但保留非空内容
            parts.append(val_str)

    if not parts:
        return ""

    # 完整拼接，保留所有原始字符（包括末尾 \r\n，它是完整 SQL 的一部分）
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════
# Step 1: read_excel()
# ═══════════════════════════════════════════════════════════════

def read_excel(excel_path: str) -> dict:
    """读取制品包 Excel，返回结构化数据。

    Returns: {
        "rules": [RawRule, ...],
        "target_fields": {"规则编码": [RawTargetField, ...]},
        "group_variables": {"规则编码": [RawGroupVariable, ...]},
        "variables": ["P_CYCLE_ID", ...],
        "rule_group_code": "GR123456",
    }
    """
    # read_only=False 更可靠（部分 Excel 文件在 read_only=True 时列读取不完整）
    wb = openpyxl.load_workbook(excel_path, read_only=False, data_only=True)
    result = {
        "rules": [],
        "target_fields": {},
        "group_variables": {},
        "variables": [],
        "rule_group_code": "",
        "rule_group_en": "",
    }

    # ── RULE sheet ──
    if "RULE" not in wb.sheetnames:
        print("错误: Excel 中没有 RULE sheet", file=sys.stderr)
        wb.close()
        return result

    ws = wb["RULE"]
    col_idx = {}
    for cell in next(ws.iter_rows(min_row=1, max_row=1, values_only=False)):
        if cell.value:
            col_idx[cell.value.strip()] = cell.column - 1

    # 映射列名到索引
    ci = {k: _find_col(col_idx, v) for k, v in RULE_COLUMNS_MAP.items()}
    col_rule_type = ci.get("rule_type")

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue

        rule_type_str = _get_val(row, col_rule_type)
        try:
            rt = int(float(rule_type_str)) if rule_type_str else 0
        except (ValueError, TypeError):
            rt = 0

        query = _read_query_sql(row, col_idx)
        exec_seq_str = _get_val(row, ci.get("exec_sequence"))
        try:
            # int(float()) 兼容数值、字符串 "1"、字符串 "1.0" 三种格式
            exec_seq = int(float(exec_seq_str)) if exec_seq_str else 0
        except (ValueError, TypeError):
            exec_seq = 0

        # 类型 12（参数变量）→ 记录到 variables
        if rt in VARIABLE_RULE_TYPES:
            var_name = _get_val(row, ci.get("rule_name")) or _get_val(row, ci.get("rule_code"))
            if var_name:
                result["variables"].append(var_name)
            continue

        # 类型 10/11/13/15（SP/API/维护/判断）→ 完全跳过
        if rt in {10, 11, 13, 15}:
            continue

        # 类型 1/14（取数/Spark取数）→ 完整解析，必须有 SQL
        # 类型 2/9（删数/分区交换）→ 记录操作，SQL 可选
        # 其他类型 → 记录但不解析

        rule = RawRule(
            rule_code=_get_val(row, ci.get("rule_code")),
            rule_name=_get_val(row, ci.get("rule_name")),
            rule_type=rt,
            exec_sequence=exec_seq,
            target_schema=_get_val(row, ci.get("target_schema")),
            target_table=_get_val(row, ci.get("target_table")),
            delete_mode=_get_val(row, ci.get("delete_mode")),
            delete_condition=_get_val(row, ci.get("delete_condition")),
            query_sql=query.strip() if query else "",
            project_code=_get_val(row, ci.get("project_code")),
            data_source=_get_val(row, ci.get("data_source")),
            business_owner=_get_val(row, ci.get("business_owner")),
            rule_group_code=_get_val(row, ci.get("rule_group_code")),
            rule_group_en=str(_get_val(row, ci.get("rule_group_en")) or "").strip(),
            exchange_source_table=_get_val(row, ci.get("exchange_source_table")),
        )

        # SELECT 类规则必须有 SQL
        if rt in SELECT_RULE_TYPES and not rule.query_sql:
            continue

        result["rules"].append(rule)

        if rule.rule_group_code and not result["rule_group_code"]:
            result["rule_group_code"] = rule.rule_group_code

        # 规则组英文名称（取第一个非空值，作为输出目录名）
        rule_group_en = _get_val(row, ci.get("rule_group_en"))
        if rule_group_en and not result["rule_group_en"]:
            result["rule_group_en"] = str(rule_group_en).strip()

    # ── TargetFields sheet ──
    if "TargetFields" in wb.sheetnames:
        ws_tf = wb["TargetFields"]
        tf_col_idx = {}
        for cell in next(ws_tf.iter_rows(min_row=1, max_row=1, values_only=False)):
            if cell.value:
                tf_col_idx[cell.value.strip()] = cell.column - 1

        tf_ci = {k: _find_col(tf_col_idx, v) for k, v in TF_COLUMNS_MAP.items()}

        for tf_row in ws_tf.iter_rows(min_row=2, values_only=True):
            if not tf_row:
                continue
            rc = _get_val(tf_row, tf_ci.get("rule_code"))
            tf = RawTargetField(
                rule_code=rc,
                target_field=_get_val(tf_row, tf_ci.get("target_field")),
                source_field=_get_val(tf_row, tf_ci.get("source_field")),
                encryption=_get_val(tf_row, tf_ci.get("encryption")),
                alias=_get_val(tf_row, tf_ci.get("alias")),
                field_type=_get_val(tf_row, tf_ci.get("field_type")),
                remark=_get_val(tf_row, tf_ci.get("remark")),
            )
            if rc:
                result["target_fields"].setdefault(rc, []).append(tf)

    # ── GroupVariables sheet ──
    if "GroupVariables" in wb.sheetnames:
        ws_gv = wb["GroupVariables"]
        gv_col_idx = {}
        for cell in next(ws_gv.iter_rows(min_row=1, max_row=1, values_only=False)):
            if cell.value:
                gv_col_idx[cell.value.strip()] = cell.column - 1

        gv_ci = {k: _find_col(gv_col_idx, v) for k, v in GV_COLUMNS_MAP.items()}
        all_vars = set()

        for gv_row in ws_gv.iter_rows(min_row=2, values_only=True):
            if not gv_row:
                continue
            rc = _get_val(gv_row, gv_ci.get("rule_code"))
            var_name = _get_val(gv_row, gv_ci.get("var_name"))
            gv = RawGroupVariable(
                rule_code=rc,
                var_name=var_name,
                default_value=_get_val(gv_row, gv_ci.get("default_value")),
            )
            if rc:
                result["group_variables"].setdefault(rc, []).append(gv)
            if var_name:
                all_vars.add(var_name)

        result["variables"] = sorted(all_vars)

    wb.close()
    return result

def _generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality, data_flow) -> str:
    """生成 AI 增强用的精简摘要 markdown。

    包含:
    - 目标表 + 场景结构
    - 每个步骤的关键信息（规则名/来源表/CTE/加工类型/SQL前200字符）
    - 字段加工类型分布
    - 质量问题
    """
    lines = []
    lines.append("# ETL 分析摘要（AI 增强用）")
    lines.append("")
    lines.append("> AI 请基于以下信息，补充每个步骤的业务目的和加工逻辑，")
    lines.append("> 每个步骤的逻辑块结构已经列出，请基于块结构推理：")
    lines.append("> 1. 每个块的业务目的（块目的）")
    lines.append("> 2. 整个步骤的业务目的（块目的的组合概括）")
    lines.append("> 输出格式见文末模板，保存为 knowledge_ai.md")
    lines.append("")

    # ── 基本信息 ──
    meta = knowledge.get("meta", {})
    lines.append(f"## 基本信息")
    lines.append(f"- 目标表: {meta.get('target_table', '')}")
    lines.append(f"- 规则数: {len(rules)}")
    lines.append(f"- 加工模式: {', '.join(p.get('label','') for p in meta.get('patterns', []))}")
    lines.append("")

    # ── 场景结构 ──
    scenarios = topology.get("scenarios", [])
    if scenarios:
        lines.append("## 场景结构")
        for sc in scenarios:
            label = "公共步骤" if sc.get("is_common") else sc["name"]
            lines.append(f"- {label}: {sc['rule_count']} 个规则 ({', '.join(sc['rule_codes'])})")
        lines.append("")

    # ── 步骤详情 ──
    lines.append("## 步骤详情")
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code)
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        sid = step["step_id"] if step else ""

        # 兜底描述
        auto_desc = next((d for d in knowledge.get("business_logic", {}).get("step_descriptions", [])
                         if d.get("step_id") == sid), {})

        lines.append(f"### {sid} ({rule.rule_code}) {rule.rule_name or ''}")
        rt_label = RULE_TYPE_MAP.get(rule.rule_type, "")
        lines.append(f"- 规则类型: {rt_label}")
        lines.append(f"- 执行序列: {rule.exec_sequence}")
        lines.append(f"- 目标表: {rule.target_table}")
        dm_label = DELETE_MODE_MAP.get((rule.delete_mode or "").strip(), "")
        dc = rule.delete_condition or ""
        lines.append(f"- 写入方式: {dm_label}" + (f" → 分区[{dc}]" if dc else ""))

        if rule.rule_type == 9 and rule.exchange_source_table:
            lines.append(f"- 分区交换: {rule.target_table} → {rule.exchange_source_table}")

        if parsed:
            src_tables = [j.source_table for j in parsed.source_tables]
            lines.append(f"- 来源表: {', '.join(src_tables[:5])}")
            if parsed.ctes:
                cte_names = [c.name for c in parsed.ctes]
                lines.append(f"- CTE: {', '.join(cte_names)}")
            # 加工类型分布
            tt_dist = Counter(c.transform_type for c in parsed.select_columns)
            tt_str = ", ".join(f"{k}={v}" for k, v in tt_dist.most_common())
            lines.append(f"- 字段加工: {len(parsed.select_columns)} 列 ({tt_str})")

        # SQL 前 200 字符（不完整 SQL）
        if rule.query_sql:
            sql_preview = rule.query_sql[:200].replace("\n", " ")
            lines.append(f"- SQL 摘要: {sql_preview}...")

        # SQL 注释（帮助 AI 理解业务含义）
        if rule.query_sql and parsed and not parsed.parse_error:
            import re as _re
            comments = _re.findall(r'/\*\s*(.*?)\s*\*/', rule.query_sql)
            if comments:
                lines.append(f"- SQL 注释: {'; '.join(comments[:5])}")

        # 逻辑块结构（供 AI 补充块目的）
        df_step = next((s for s in data_flow.get("steps", []) if s.get("step_id") == sid), None)
        if df_step and df_step.get("data_blocks"):
            lines.append(f"- 逻辑块:")
            for idx, blk in enumerate(df_step["data_blocks"]):
                _append_block_summary(lines, blk, idx, indent=1)

        # 兜底描述（脚本已生成）
        if auto_desc.get("purpose"):
            lines.append(f"- 脚本兜底 purpose: {auto_desc['purpose']}")
        if auto_desc.get("logic"):
            lines.append(f"- 脚本兜底 logic: {auto_desc['logic']}")
        lines.append("")

    # ── 质量问题 ──
    issues = quality.get("issues", [])
    if issues:
        lines.append("## 质量问题")
        for iss in issues[:10]:
            lines.append(f"- [{iss.get('severity','')}] {iss.get('title','')}")
        lines.append("")

    # ── AI 输出模板 ──
    lines.append("---")
    lines.append("")
    lines.append("## AI 输出模板（按此格式输出，保存为 knowledge_ai.md）")
    lines.append("")
    lines.append("```markdown")
    lines.append("# 整体描述")
    lines.append("（2-3句话描述这个ETL是干什么的）")
    lines.append("")
    for rule in rules:
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        sid = step["step_id"] if step else ""
        lines.append(f"## {sid}")
        lines.append(f"（基于以下逻辑块推理这步的业务目的，1-2句话）")
        lines.append(f"### 块目的")
        lines.append(f"（为每个块补充业务目的，格式: - 块N (角色 表): 目的）")
        # 列出该步骤的逻辑块，让 AI 为每个块补充目的
        df_step_tpl = next((s for s in data_flow.get("steps", []) if s.get("step_id") == sid), None)
        if df_step_tpl and df_step_tpl.get("data_blocks"):
            for idx, blk in enumerate(df_step_tpl["data_blocks"]):
                _append_block_template(lines, blk, idx, indent=0)
        lines.append("")
    lines.append("## 关键字段")
    lines.append("- 字段名: 业务含义")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="dws-pipeline-analyzer — 制品包深度分析器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="execution_tasks.xlsx 文件路径")
    parser.add_argument("--output", required=True, help="输出基础目录（脚本会在此目录下按规则组英文名称建子目录）")
    parser.add_argument("--dialect", default="", help="SQL 方言 (oracle/dws/auto)，默认自动检测")
    parser.add_argument("--ddl-dir", default="", help="DDL 文件目录（可选，用于补充字段类型）")
    args = parser.parse_args()

    input_path = Path(args.input)
    base_output_dir = Path(args.output)

    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"=== dws-pipeline-analyzer ===")
    print(f"输入: {input_path}")
    print(f"输出基础目录: {base_output_dir}")
    print()

    # ── Step 1: 读取 Excel ──
    print("Step 1: 读取制品包 Excel...")
    raw = read_excel(str(input_path))
    rules = raw["rules"]

    # 确定输出目录：基础目录 / 规则组英文名称（兜底用规则组编码或 output）
    group_en = (raw.get("rule_group_en") or "").strip()
    if not group_en:
        group_en = (raw.get("rule_group_code") or "").strip() or "output"
    # 清理目录名（去掉非法字符）
    safe_group_en = re.sub(r'[<>:"/\\|?*\s]', "_", group_en)
    output_dir = base_output_dir / safe_group_en
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {output_dir}")
    print()

    if not rules:
        # 详细诊断
        print("错误: 未找到有效的 RULE 行", file=sys.stderr)
        print("", file=sys.stderr)
        print("诊断信息:", file=sys.stderr)

        # 检查 RULE sheet 是否存在
        wb_diag = openpyxl.load_workbook(str(input_path), read_only=False, data_only=True)
        if "RULE" not in wb_diag.sheetnames:
            print(f"  - Excel 中没有 'RULE' sheet，实际 sheet: {wb_diag.sheetnames}", file=sys.stderr)
        else:
            ws_diag = wb_diag["RULE"]
            diag_headers = []
            for cell in next(ws_diag.iter_rows(min_row=1, max_row=1, values_only=False)):
                if cell.value:
                    diag_headers.append(str(cell.value).strip())

            data_rows = 0
            skipped_no_sql = 0
            skipped_rule_type = []
            for row in ws_diag.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                data_rows += 1

            # 找规则类型列
            rt_col_idx = None
            for h_idx, h in enumerate(diag_headers):
                if "规则类型" in h:
                    rt_col_idx = h_idx
                    break

            # 找查询语句列
            sql_col_idx = None
            for h_idx, h in enumerate(diag_headers):
                if "查询语句" in h:
                    sql_col_idx = h_idx
                    break

            print(f"  - RULE sheet 存在，数据行数: {data_rows}", file=sys.stderr)
            print(f"  - 表头列数: {len(diag_headers)}", file=sys.stderr)

            if rt_col_idx is None:
                print(f"  - [FAIL] 找不到 '规则类型' 列！表头: {diag_headers[:10]}", file=sys.stderr)
            else:
                print(f"  - 规则类型列 idx={rt_col_idx} ('{diag_headers[rt_col_idx]}')", file=sys.stderr)
                # 检查规则类型值
                rt_values = set()
                for row in ws_diag.iter_rows(min_row=2, values_only=True):
                    if row and rt_col_idx < len(row):
                        rt_val = row[rt_col_idx]
                        rt_values.add(str(rt_val) if rt_val is not None else "None")
                print(f"  - 规则类型值: {rt_values}", file=sys.stderr)

            if sql_col_idx is None:
                print(f"  - [FAIL] 找不到 '查询语句' 列！表头含'查询': {[h for h in diag_headers if '查询' in h or 'sql' in h.lower()]}", file=sys.stderr)
            else:
                print(f"  - 查询语句列 idx={sql_col_idx} ('{diag_headers[sql_col_idx]}')", file=sys.stderr)
                # 检查 SQL 是否为空
                empty_sql = 0
                for row in ws_diag.iter_rows(min_row=2, values_only=True):
                    if row and sql_col_idx < len(row):
                        sql_val = row[sql_col_idx]
                        if not sql_val or not str(sql_val).strip():
                            empty_sql += 1
                if empty_sql > 0:
                    print(f"  - [WARN] {empty_sql} 行的 SQL 为空", file=sys.stderr)

        wb_diag.close()
        sys.exit(1)

    print(f"  RULE 行: {len(rules)}")
    print(f"  TargetFields: {sum(len(v) for v in raw['target_fields'].values())} 行")
    print(f"  GroupVariables: {sum(len(v) for v in raw['group_variables'].values())} 行")
    print(f"  变量: {raw['variables']}")
    print()

    # ── Step 2: 方言检测 ──
    dialect = args.dialect
    if not dialect or dialect == "auto":
        sql_texts = [r.query_sql for r in rules if r.query_sql]
        dialect = detect_dialect(sql_texts)
    print(f"Step 2: 方言 = {dialect}")
    print()

    # ── Step 3~7: 核心解析（与批量路径共用 analyze_pipeline，避免两套逻辑漂移）──
    print("Step 3-7: 解析 + 组装 knowledge...")
    knowledge, parsed_map = analyze_pipeline(
        rules, raw["target_fields"], raw["group_variables"], dialect,
        ddl_dir=args.ddl_dir, source_file=input_path.name,
        rule_group_code=raw["rule_group_code"],
    )
    # 从 knowledge 取回 AI summary 需要的中间结构
    topology = knowledge["topology"]
    data_flow = knowledge["data_flow"]
    field_mappings = knowledge["field_mappings"]
    quality = knowledge["quality"]
    target_name = knowledge["meta"]["target_table"]
    stats = field_mappings["statistics"]
    print(f"  步骤数: {len(rules)}, 字段数: {stats['total_in_sql']}, "
          f"问题数: {len(quality['issues'])}")
    print()

    # 写入文件
    output_file = output_dir / "knowledge_draft.json"
    output_file.write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )

    # ── 生成 AI 增强用摘要 ──
    summary_file = output_dir / "knowledge_summary.md"
    summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality, data_flow)
    summary_file.write_text(summary_text, encoding="utf-8", newline="\n")

    print(f"\n=== 完成 ===")
    print(f"输出: {output_file}")
    print(f"摘要: {summary_file}")
    print(f"目标表: {target_name}")
    print(f"步骤数: {len(rules)}")
    print(f"字段数: {stats['total_in_sql']}")
    print(f"问题数: {len(quality['issues'])}")
    print(f"\n下一步: AI 读 knowledge_summary.md，输出自然语言补充，保存为 knowledge_ai.md")
    print(f"        然后: python run.py view_generator --input knowledge_draft.json --ai-input knowledge_ai.md ...")


# ═══════════════════════════════════════════════════════════════
# 兼容层：re-export engine 的符号
# 引擎代码已物理搬入 engine.py。以下 re-export 保证现有代码
# `from analyzer import xxx` 继续可用（过渡期，新代码请直接 from engine import）。
# ═══════════════════════════════════════════════════════════════
from engine import (  # noqa: E402,F401
    ParsedSQL, ParsedJoin, RawRule, RawTargetField, RawGroupVariable,
    ParsedColumn, ParsedCTE, TableRef, ColumnRef, QueryUnit,
    analyze_pipeline,
    detect_dialect, parse_single_sql, classify_transform,
    build_topology, build_data_flow, build_field_mappings, analyze_quality,
    build_join_key_lineage, enrich_join_key_lineage, enrich_field_physical_sources,
    build_data_blocks, build_structured_step_summary, generate_step_description,
    detect_patterns, build_source, parse_ddl_for_metadata,
    _append_block_summary, _append_block_template,
    _is_intermediate_table, _strip_dws_clauses, _replace_placeholders,
    _normalize_table_name, _norm_table, _table_match, _infer_layer, _clean_name,
    SELECT_RULE_TYPES, RULE_TYPE_MAP, DELETE_MODE_MAP,
)


if __name__ == "__main__":
    main()
