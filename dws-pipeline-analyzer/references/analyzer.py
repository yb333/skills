#!/usr/bin/env python3
"""
dws-pipeline-analyzer — 制品包深度分析器

从 execution_tasks.xlsx 提取完整的 ETL 知识，输出 knowledge_draft.json。

Usage:
    python analyzer.py --input execution_tasks.xlsx --output docs/output/{target_table}/
    python analyzer.py --input execution_tasks.xlsx --output docs/output/{target_table}/ --dialect dws
    python analyzer.py --input execution_tasks.xlsx --output docs/output/{target_table}/ --ddl-dir 04_ddl/

Author: 院博
Version: 1.0.0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import openpyxl
except ImportError:
    print("错误: 需要 openpyxl。pip install openpyxl", file=sys.stderr)
    sys.exit(1)

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    print("错误: 需要 sqlglot。pip install sqlglot", file=sys.stderr)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

# 方言检测特征
ORACLE_SIGNS = ["(+)", "NVL(", "DECODE(", "VARCHAR2", "NUMBER(", "CONNECT BY", "ROWNUM", "SYSDATE"]
DWS_SIGNS = ["DISTRIBUTE BY", "TO GROUP", "ORIENTATION=", "COMPRESSION=", "WITHOUT TIME ZONE"]

# 数仓层级推断
LAYER_PATTERNS = [
    (r"\bods\b|(?:^|\.)ods_", "ODS"),
    (r"\bdim\b|(?:^|\.)dim_", "DIM"),
    (r"\bdwb\b|(?:^|\.)dwb_|\bdwd\b|(?:^|\.)dwd_|(?:^|\.)dwl_", "DWB"),
    (r"\bdws\b|(?:^|\.)dws_", "DWS"),
    (r"\bads\b|(?:^|\.)ads_|\brpt\b|(?:^|\.)rpt_|\bslprd\b", "ADS"),
    (r"\btmp\b|(?:^|\.)tmp_|\btemp\b|(?:^|\.)temp_", "TMP"),
]

# RULE sheet 关键列名
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

# DWS 语法清理正则
_DIST_BY_PATTERN = re.compile(
    r"\bDISTRIBUTE[D]?\s+BY\s+(?:HASH\s*\([^)]+\)|REPLICATION|ROUNDROBIN)",
    re.IGNORECASE | re.DOTALL,
)
_TO_GROUP_PATTERN = re.compile(r"\bTO\s+GROUP\s+\S+", re.IGNORECASE)
# PARTITION(part_name) 语法（DWS/GaussDB 分区查询，sqlglot 不支持）
_PARTITION_PATTERN = re.compile(r"\s+PARTITION\s*\([^)]+\)", re.IGNORECASE)
_WITH_PARAMS_PATTERN = re.compile(
    r"\)\s*WITH\s*\(\s*"
    r"(?:ORIENTATION\s*=\s*(?:COLUMN|ROW)\s*,?\s*)?"
    r"(?:COMPRESSION\s*=\s*(?:LOW|MIDDLE|HIGH)\s*,?\s*)?"
    r"(?:\w+\s*=\s*\w+\s*,?\s*)*"
    r"\)",
    re.IGNORECASE,
)
_PARAM_PLACEHOLDER = re.compile(r"\$\{[^}]+\}")
# 平台变量替换语法: #var_name = value# （成对的 # 包裹）
_PLATFORM_VAR_PATTERN = re.compile(r"#[^#]*?#", re.DOTALL)
_INLINE_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


# ═══════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class RawRule:
    """RULE sheet 单行数据"""
    rule_code: str = ""
    rule_name: str = ""
    rule_type: int = 0
    exec_sequence: int = 0
    target_schema: str = ""
    target_table: str = ""
    delete_mode: str = ""
    delete_condition: str = ""
    query_sql: str = ""
    project_code: str = ""
    data_source: str = ""
    business_owner: str = ""
    rule_group_code: str = ""
    exchange_source_table: str = ""


@dataclass
class RawTargetField:
    """TargetFields sheet 单行数据"""
    rule_code: str = ""
    target_field: str = ""
    source_field: str = ""
    encryption: str = ""
    alias: str = ""
    field_type: str = ""
    remark: str = ""


@dataclass
class RawGroupVariable:
    """GroupVariables sheet 单行数据"""
    rule_code: str = ""
    var_name: str = ""
    default_value: str = ""


@dataclass
class ParsedColumn:
    """SQL SELECT 解析出的单列"""
    position: int = 0
    alias: str = ""
    expression: str = ""
    transform_type: str = "unknown"
    source_tables: list = field(default_factory=list)
    source_fields: list = field(default_factory=list)


@dataclass
class ParsedJoin:
    """SQL JOIN 信息"""
    source_table: str = ""
    alias: str = ""
    join_type: str = ""  # FROM / LEFT / INNER / FULL / CROSS
    join_condition: str = ""


@dataclass
class ParsedCTE:
    """CTE 信息"""
    name: str = ""
    source_tables: list = field(default_factory=list)
    fields: list = field(default_factory=list)


@dataclass
class ParsedSQL:
    """单条 SQL 的完整解析结果"""
    source_tables: list = field(default_factory=list)  # list[ParsedJoin]
    select_columns: list = field(default_factory=list)  # list[ParsedColumn]
    where_clause: str = ""
    group_by: list = field(default_factory=list)
    having_clause: str = ""
    ctes: list = field(default_factory=list)  # list[ParsedCTE]
    raw_sql: str = ""
    parse_error: str = ""


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _clean_name(name: str) -> str:
    """清理标识符：去除引号、注释、空白"""
    return _INLINE_COMMENT_RE.sub("", name).strip().strip('"').strip("`")


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
    import unicodedata
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


def _strip_dws_clauses(sql: str) -> str:
    """移除 DWS 特有语法，供 sqlglot 解析"""
    sql = _DIST_BY_PATTERN.sub("", sql)
    sql = _TO_GROUP_PATTERN.sub("", sql)
    sql = _PARTITION_PATTERN.sub("", sql)
    sql = _WITH_PARAMS_PATTERN.sub(")", sql)
    return sql


def _replace_placeholders(sql: str) -> str:
    """替换平台变量占位符为合法 SQL 值

    支持两种语法:
    1. ${XXX}        → NULL
    2. #var = value# → NULL（成对的 # 包裹）
    """
    sql = _PARAM_PLACEHOLDER.sub("NULL", sql)
    sql = _PLATFORM_VAR_PATTERN.sub("NULL", sql)
    return sql


def _normalize_table_name(schema: str, table: str) -> str:
    """标准化表名为 schema.table"""
    s = _clean_name(schema)
    t = _clean_name(table)
    if s:
        return f"{s}.{t}"
    return t


def _infer_layer(schema: str, table: str) -> str:
    """推断数仓层级"""
    combined = f"{schema}.{table}".lower()
    for pattern, layer in LAYER_PATTERNS:
        if re.search(pattern, combined):
            return layer
    return "UNKNOWN"


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

        query = _get_val(row, ci.get("query_sql"))
        exec_seq_str = _get_val(row, ci.get("exec_sequence"))
        try:
            exec_seq = int(exec_seq_str) if exec_seq_str else 0
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
            exchange_source_table=_get_val(row, ci.get("exchange_source_table")),
        )

        # SELECT 类规则必须有 SQL
        if rt in SELECT_RULE_TYPES and not rule.query_sql:
            continue

        result["rules"].append(rule)

        if rule.rule_group_code and not result["rule_group_code"]:
            result["rule_group_code"] = rule.rule_group_code

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


# ═══════════════════════════════════════════════════════════════
# Step 2: detect_dialect()
# ═══════════════════════════════════════════════════════════════

def detect_dialect(sql_texts: list[str]) -> str:
    """自动检测 SQL 方言"""
    combined = " ".join(sql_texts).upper()
    oracle_score = sum(1 for sign in ORACLE_SIGNS if sign.upper() in combined)
    dws_score = sum(1 for sign in DWS_SIGNS if sign.upper() in combined)

    if oracle_score > dws_score:
        return "oracle"
    return "dws"


# ═══════════════════════════════════════════════════════════════
# Step 3: parse_single_sql() — sqlglot AST
# ═══════════════════════════════════════════════════════════════

def parse_single_sql(sql: str, dialect: str = "dws") -> ParsedSQL:
    """用 sqlglot AST 解析单条 SQL（纯 SELECT/WITH/UNION）。

    制品包中的 SQL 是纯 SELECT、WITH...SELECT 或 SELECT...UNION ALL...SELECT，
    不包含 INSERT INTO / TRUNCATE 等。

    支持：
    - 普通 SELECT / WITH...SELECT
    - UNION / UNION ALL / INTERSECT / EXCEPT（exp.Union/Intersect/Except）
    - CTE + UNION 组合（WITH 在顶层，UNION 在 WITH 之后）
    """
    result = ParsedSQL(raw_sql=sql)

    # 预处理：清理 DWS 语法 + 替换占位符
    clean = _strip_dws_clauses(sql)
    clean = _replace_placeholders(clean)
    clean = clean.strip().rstrip(";").strip()

    # 在清理注释之前，先提取 SQL 注释中的字段名映射
    # 用于给无别名的列（审计字段等）赋予正确名称
    comment_alias_map = _extract_comment_aliases(sql)

    if not clean:
        result.parse_error = "空 SQL"
        return result

    # sqlglot 解析
    sqlglot_dialect = "oracle" if dialect == "oracle" else "postgres"
    try:
        tree = sqlglot.parse_one(clean, dialect=sqlglot_dialect)
    except sqlglot.ParseError as e:
        result.parse_error = str(e)
        print(f"  [SQL解析错误] {e}", file=sys.stderr)
        return result

    # ── 检测 UNION/INTERSECT/EXCEPT（SetOperation）──
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        return _parse_set_operation(tree, sqlglot_dialect, comment_alias_map, sql)

    # ── 普通 SELECT / WITH...SELECT ──
    select_node = tree.find(exp.Select)
    if not select_node:
        result.parse_error = "未找到 SELECT 节点"
        return result

    return _parse_select(tree, select_node, sqlglot_dialect, comment_alias_map, sql)


def _parse_select(tree, select_node, sqlglot_dialect: str, comment_alias_map: dict, raw_sql: str) -> ParsedSQL:
    """解析普通 SELECT 语句（非 UNION）。"""
    result = ParsedSQL(raw_sql=raw_sql)

    # ── 提取源表和 JOIN ──
    result.source_tables = _extract_joins(tree, select_node)

    # ── 提取 SELECT 列 ──
    result.select_columns = _extract_select_columns(select_node, comment_alias_map)

    # ── WHERE ──
    where_node = select_node.args.get("where")
    if where_node:
        result.where_clause = where_node.sql(dialect=sqlglot_dialect)

    # ── GROUP BY ──
    group_node = select_node.args.get("group")
    if group_node:
        result.group_by = [
            expr.sql(dialect=sqlglot_dialect) for expr in group_node.expressions
        ]

    # ── HAVING ──
    having_node = select_node.args.get("having")
    if having_node:
        result.having_clause = having_node.sql(dialect=sqlglot_dialect)

    # ── CTE ──
    result.ctes = _extract_ctes(tree, sqlglot_dialect)

    # ── 构建 CTE 别名映射 ──
    cte_alias_map = _build_cte_alias_map(select_node, result.ctes)

    # ── CTE 穿透传播 ──
    _apply_cte_penetration(result.select_columns, result.ctes, cte_alias_map)

    return result


def _parse_set_operation(tree, sqlglot_dialect: str, comment_alias_map: dict, raw_sql: str) -> ParsedSQL:
    """解析 UNION/UNION ALL/INTERSECT/EXCEPT 语句。

    SetOperation 结构：
      tree.this (left) = 第一个 SELECT
      tree.expression (right) = 第二个 SELECT
      tree.args["with_"] = 顶层 WITH（如果有）

    处理策略：
    - source_tables: 合并所有分支的 FROM/JOIN
    - select_columns: 以第一个分支为准（UNION 按位置对齐）
    - WHERE/GROUP BY: 合并所有分支的条件
    - CTE: 从顶层 tree 提取
    """
    result = ParsedSQL(raw_sql=raw_sql)

    # 获取操作类型
    op_type = type(tree).__name__.upper()  # UNION / INTERSECT / EXCEPT
    is_all = False
    if isinstance(tree, exp.Union):
        is_all = not tree.args.get("distinct", True)
    op_label = f"{op_type}{' ALL' if is_all else ''}"
    result.where_clause = f"-- 集合操作: {op_label}"

    # 获取左右分支
    left = tree.this
    right = tree.expression

    # 收集所有 SELECT 分支（支持链式 UNION: A UNION B UNION C）
    branches = []
    _collect_set_branches(tree, branches)

    # 合并所有分支的 source_tables
    all_joins = []
    for branch in branches:
        if isinstance(branch, exp.Select):
            branch_joins = _extract_joins(branch, branch)
            all_joins.extend(branch_joins)

    # 去重（同名同别名）
    seen = set()
    deduped = []
    for j in all_joins:
        key = (j.source_table, j.alias)
        if key not in seen:
            seen.add(key)
            deduped.append(j)
    result.source_tables = deduped

    # select_columns 以第一个分支为准
    first_branch = branches[0] if branches else None
    if isinstance(first_branch, exp.Select):
        result.select_columns = _extract_select_columns(first_branch, comment_alias_map)

        # WHERE: 合并所有分支的条件
        where_parts = []
        for i, branch in enumerate(branches):
            if not isinstance(branch, exp.Select):
                continue
            where_node = branch.args.get("where")
            if where_node:
                prefix = f"分支{i+1}" if i > 0 else ""
                where_parts.append(f"{prefix}: {where_node.sql(dialect=sqlglot_dialect)}")
        if where_parts:
            result.where_clause = f"{op_label} | " + " AND ".join(where_parts)

        # GROUP BY: 取第一个分支的
        group_node = first_branch.args.get("group")
        if group_node:
            result.group_by = [
                expr.sql(dialect=sqlglot_dialect) for expr in group_node.expressions
            ]

    # CTE: 从顶层 tree 提取（WITH 在 SetOperation 顶层）
    result.ctes = _extract_ctes(tree, sqlglot_dialect)

    # CTE 穿透传播
    if result.select_columns and result.ctes:
        # 找到第一个分支的 select_node 用于构建 alias_map
        if isinstance(first_branch, exp.Select):
            cte_alias_map = _build_cte_alias_map(first_branch, result.ctes)
            _apply_cte_penetration(result.select_columns, result.ctes, cte_alias_map)

    return result


def _collect_set_branches(node, branches: list) -> None:
    """递归收集 SetOperation 的所有 SELECT 分支。

    支持 A UNION B UNION C 链式结构。
    """
    if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        _collect_set_branches(node.this, branches)
        _collect_set_branches(node.expression, branches)
    elif isinstance(node, exp.Select):
        branches.append(node)
    elif isinstance(node, exp.Subquery):
        # 子查询包装的 SELECT
        inner = node.find(exp.Select)
        if inner:
            branches.append(inner)


def _build_cte_alias_map(select_node, ctes: list) -> dict[str, str]:
    """构建 CTE 别名映射: {别名(UPPER): CTE名(UPPER)}。

    主查询中 FROM/JOIN CTE 时可能用别名引用（如 inv_mtr_agg im_agg），
    需要将别名 im_agg 映射到 CTE 名 inv_mtr_agg 才能做穿透传播。
    """
    if not ctes:
        return {}

    cte_names_upper = {c.name.upper() for c in ctes if c.name}
    alias_map: dict[str, str] = {}

    # FROM 中的 CTE 引用
    from_clause = select_node.args.get("from_")
    if from_clause and isinstance(from_clause.this, exp.Table):
        t = from_clause.this
        short = t.name.upper()
        if short in cte_names_upper:
            alias = _clean_name(t.alias).upper() if t.alias else short
            alias_map[alias] = short

    # JOIN 中的 CTE 引用
    for join_node in select_node.args.get("joins", []):
        t = join_node.find(exp.Table)
        if t:
            short = t.name.upper()
            if short in cte_names_upper:
                alias = _clean_name(t.alias).upper() if t.alias else short
                alias_map[alias] = short

    return alias_map


def _extract_joins(tree, select_node) -> list[ParsedJoin]:
    """提取 FROM 和 JOIN 信息。

    ⚠️ 不递归进入 CTE / 子查询。只取主 SELECT 的直接 FROM 和 JOIN，
    避免把 CTE 内部的 JOIN 表误归入主查询的 source_tables。
    """
    joins = []
    sqlglot_dialect = "postgres"

    # 收集 CTE 名称（用于排除 CTE 别名）
    cte_names = set()
    with_clause = tree.args.get("with_")
    if with_clause:
        for cte_node in with_clause.expressions:
            cte_alias = cte_node.alias
            if cte_alias:
                cte_names.add(_clean_name(str(cte_alias)).upper())

    # 主表 (FROM) — 只取 from_ 的直接 Table，不递归进入子查询
    from_clause = select_node.args.get("from_")
    if from_clause:
        # from_.this 是主表
        main_table = from_clause.this
        if isinstance(main_table, exp.Table):
            table_name = ".".join(_clean_name(p.name) for p in main_table.parts)
            alias = _clean_name(main_table.alias) if main_table.alias else ""
            joins.append(ParsedJoin(
                source_table=table_name,
                alias=alias or table_name.split(".")[-1],
                join_type="FROM",
                join_condition="",
            ))
        # from_.expressions 是逗号 JOIN 的额外表（FROM a, b）
        for extra in from_clause.expressions:
            if isinstance(extra, exp.Table):
                table_name = ".".join(_clean_name(p.name) for p in extra.parts)
                alias = _clean_name(extra.alias) if extra.alias else ""
                joins.append(ParsedJoin(
                    source_table=table_name,
                    alias=alias or table_name.split(".")[-1],
                    join_type="FROM",
                    join_condition="",
                ))

    # JOIN 表 — 只取 select_node 的直接 JOIN（args["joins"]），
    # 不使用 find_all(exp.Join) 以避免递归进入 CTE 内部
    joins_list = select_node.args.get("joins", [])
    for join_node in joins_list:
        table = join_node.find(exp.Table)
        if not table:
            continue
        table_name = ".".join(_clean_name(p.name) for p in table.parts)
        alias = _clean_name(table.alias) if table.alias else ""

        # 过滤 CTE 引用（CTE 名作为表名出现在 JOIN 中）
        short_name = table_name.split(".")[-1] if "." in table_name else table_name
        if short_name.upper() in cte_names:
            continue

        # JOIN 类型
        join_kind = join_node.args.get("kind", "")
        join_side = join_node.args.get("side", "")
        if join_side:
            jt = f"{join_side} JOIN".upper()
        elif join_kind:
            jt = f"{join_kind} JOIN".upper()
        else:
            jt = "INNER JOIN"

        on_node = join_node.args.get("on")
        join_condition = on_node.sql(dialect=sqlglot_dialect) if on_node else ""

        joins.append(ParsedJoin(
            source_table=table_name,
            alias=alias or table_name.split(".")[-1],
            join_type=jt,
            join_condition=join_condition,
        ))

    return joins


def _extract_comment_aliases(sql: str) -> dict[int, str]:
    """从原始 SQL 中提取注释→字段名的映射。

    逐行扫描，找到 /* field_name */ 注释，记录该行的 SELECT 列位置。

    Returns: {position: field_name}
    """
    result = {}
    # 找到 SELECT 和 FROM 之间的行
    lines = sql.split("\n")
    in_select = False
    col_idx = 0
    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("SELECT"):
            in_select = True
            # SELECT 行本身可能也有列
        if not in_select:
            continue
        if upper.startswith("FROM") and not upper.startswith("FROM("):
            break

        # 检查注释中的字段名
        comment_match = re.search(r"/\*\s*([a-z_][a-z0-9_]*)\s*\*/", stripped)
        if comment_match:
            result[col_idx] = comment_match.group(1)

        # 计算逗号分隔的列数（简化版：非嵌套的逗号）
        # 只计算顶层逗号
        depth = 0
        for ch in stripped:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                col_idx += 1

    return result


def _resolve_select_alias(proj, proj_sql: str, position: int, comment_alias_map: dict | None = None) -> str:
    """从 SELECT 投影列解析别名。

    优先级：
    1. AS 别名
    2. 简单列名（如 t.product_id → product_id）
    3. SQL 注释中的字段名（如 /* del_flag */，从原始 SQL 提取）
    4. 从表达式推断审计字段
    5. 兜底：col_{position}
    """
    # 1. AS 别名
    if isinstance(proj, exp.Alias):
        return _clean_name(proj.alias)

    # 2. 简单列名
    if isinstance(proj, exp.Column):
        return _clean_name(proj.name)

    # 3. 从原始 SQL 注释映射中查找
    if comment_alias_map and position in comment_alias_map:
        return comment_alias_map[position]

    # 3b. sqlglot 生成的 SQL 中也检查注释
    comment_match = re.search(r"/\*\s*([a-z_][a-z0-9_]*)\s*\*/", proj_sql)
    if comment_match:
        return comment_match.group(1)

    # 4. 从表达式推断审计字段
    stripped = proj_sql.strip().strip("'\"")
    upper = stripped.upper()
    if upper == "N":
        return "del_flag"
    if "CURRENT_TIMESTAMP" in upper:
        return "dw_last_update_date"

    # 5. 兜底
    return f"_col_{position}"


def _extract_select_columns(select_node, comment_alias_map: dict | None = None) -> list[ParsedColumn]:
    """提取 SELECT 投影列"""
    columns = []
    sqlglot_dialect = "postgres"

    for i, proj in enumerate(select_node.expressions):
        proj_sql = proj.sql(dialect=sqlglot_dialect)

        # 提取别名
        alias = _resolve_select_alias(proj, proj_sql, i, comment_alias_map)

        # 提取源字段引用
        source_fields = []
        source_tables = []
        seen = set()
        for col in proj.walk():
            if isinstance(col, exp.Column):
                col_name = _clean_name(col.name)
                tbl_alias = _clean_name(col.table) if col.table else ""
                key = f"{tbl_alias}.{col_name}"
                if key not in seen:
                    seen.add(key)
                    source_fields.append({"alias": tbl_alias, "field": col_name})
                    if tbl_alias and tbl_alias not in source_tables:
                        source_tables.append(tbl_alias)

        columns.append(ParsedColumn(
            position=i,
            alias=alias,
            expression=proj_sql,
            transform_type=classify_transform(proj, proj_sql),
            source_tables=source_tables,
            source_fields=source_fields,
        ))

    return columns


def _extract_ctes(tree, sqlglot_dialect: str) -> list[ParsedCTE]:
    """提取 CTE 信息。

    每个 CTE field 含 transform_type 和 source_fields，用于主查询穿透传播。
    CTE 内的源表只取直接 FROM/JOIN（不递归进入嵌套子查询）。
    """
    ctes = []
    with_clause = tree.args.get("with_")
    if not with_clause:
        return ctes

    # 收集所有 CTE 名（用于 CTE 内源表过滤 — 嵌套 CTE 引用）
    all_cte_names = set()
    if with_clause:
        for cte_node in with_clause.expressions:
            cte_alias = cte_node.alias
            if cte_alias:
                all_cte_names.add(_clean_name(str(cte_alias)).upper())

    for cte_node in with_clause.expressions:
        cte_alias = cte_node.alias
        cte_name = _clean_name(str(cte_alias)) if cte_alias else ""
        cte_query = cte_node.this

        # CTE 内的 SELECT 节点
        cte_select = cte_query if isinstance(cte_query, exp.Select) else cte_query.find(exp.Select)

        # CTE 内的源表 — 只取直接 FROM/JOIN（不递归进入嵌套子查询）
        # 含 join_type，用于复杂度统计（JOIN 数）和来源表统计
        cte_tables = []
        if cte_select:
            # FROM
            cte_from = cte_select.args.get("from_")
            if cte_from and isinstance(cte_from.this, exp.Table):
                tname = ".".join(_clean_name(p.name) for p in cte_from.this.parts)
                talias = _clean_name(cte_from.this.alias) if cte_from.this.alias else ""
                cte_tables.append({"name": tname, "alias": talias, "join_type": "FROM"})
            for extra in cte_from.expressions if cte_from else []:
                if isinstance(extra, exp.Table):
                    tname = ".".join(_clean_name(p.name) for p in extra.parts)
                    talias = _clean_name(extra.alias) if extra.alias else ""
                    cte_tables.append({"name": tname, "alias": talias, "join_type": "FROM"})
            # JOIN（不递归进入 CTE 内的嵌套子查询）
            for cte_join in cte_select.args.get("joins", []):
                t = cte_join.find(exp.Table)
                if t:
                    tname = ".".join(_clean_name(p.name) for p in t.parts)
                    talias = _clean_name(t.alias) if t.alias else ""
                    # JOIN 类型
                    jk = cte_join.args.get("kind", "")
                    js = cte_join.args.get("side", "")
                    if js:
                        jt = f"{js} JOIN".upper()
                    elif jk:
                        jt = f"{jk} JOIN".upper()
                    else:
                        jt = "INNER JOIN"
                    cte_tables.append({"name": tname, "alias": talias, "join_type": jt})
        else:
            # fallback: find_all（覆盖非标准结构）
            for table in cte_query.find_all(exp.Table):
                tname = ".".join(_clean_name(p.name) for p in table.parts)
                talias = _clean_name(table.alias) if table.alias else ""
                cte_tables.append({"name": tname, "alias": talias})

        # CTE 输出字段（含 transform_type 和 source_fields）
        cte_fields = []
        cte_select_for_fields = cte_query if isinstance(cte_query, exp.Select) else cte_query.find(exp.Select)
        if cte_select_for_fields:
            for proj in cte_select_for_fields.expressions:
                proj_sql = proj.sql(dialect=sqlglot_dialect)
                if isinstance(proj, exp.Alias):
                    fname = _clean_name(proj.alias)
                elif isinstance(proj, exp.Column):
                    fname = _clean_name(proj.name)
                else:
                    fname = proj.alias_or_name if hasattr(proj, 'alias_or_name') else ""

                # 提取源字段引用
                source_fields = []
                seen = set()
                for col in proj.walk():
                    if isinstance(col, exp.Column):
                        col_name = _clean_name(col.name)
                        tbl_alias = _clean_name(col.table) if col.table else ""
                        key = f"{tbl_alias}.{col_name}"
                        if key not in seen:
                            seen.add(key)
                            source_fields.append({"alias": tbl_alias, "field": col_name})

                cte_fields.append({
                    "name": fname,
                    "expression": proj_sql,
                    "transform_type": classify_transform(proj, proj_sql),
                    "source_fields": source_fields,
                })

        ctes.append(ParsedCTE(
            name=cte_name,
            source_tables=cte_tables,
            fields=cte_fields,
        ))

    return ctes


# ═══════════════════════════════════════════════════════════════
# CTE 穿透传播
# ═══════════════════════════════════════════════════════════════

# transform_type 优先级（高优先级覆盖低优先级）
TRANSFORM_PRIORITY = {
    "unknown": -1,
    "value": 0,
    "direct": 1,
    "expression": 2,
    "fallback": 3,
    "case_when": 4,
    "aggregate": 5,
    "pivot": 6,
    "window": 7,
}


def _apply_cte_penetration(columns: list, ctes: list, cte_alias_map: dict = None) -> None:
    """CTE 穿透传播 + 嵌套递归。

    主查询引用 CTE 字段时：
    1. 按 TRANSFORM_PRIORITY 优先级覆盖 transform_type（CTE 内更重的加工类型升级主查询）
    2. 注入 cte_source / cte_transform_type / cte_context 到 source_fields
    3. 嵌套递归：CTE_A 引用 CTE_B 时，多跳解析

    直接修改 columns（in-place），不返回新列表。

    cte_alias_map: {别名(UPPER): CTE名(UPPER)}，从主查询 FROM/JOIN 收集。
                   主查询可能用别名引用 CTE（如 inv_mtr_agg im_agg）。
    """
    if not ctes:
        return

    # 构建 CTE 字段查找表: {CTE名(UPPER): {字段名(UPPER): field_dict}}
    cte_field_map: dict[str, dict[str, dict]] = {}
    for cte in ctes:
        cte_key = cte.name.upper()
        field_dict = {}
        for f in cte.fields:
            if isinstance(f, dict) and f.get("name"):
                field_dict[f["name"].upper()] = f
        cte_field_map[cte_key] = field_dict

    cte_names_upper = set(cte_field_map.keys())
    cte_alias_map = cte_alias_map or {}

    for col in columns:
        final_transform = col.transform_type

        for sf in col.source_fields:
            sf_alias = sf.get("alias", "")
            sf_field = sf.get("field", "")
            if not sf_alias or not sf_field:
                continue

            # 解析别名 → CTE 名
            # 1. 直接匹配 CTE 名（如 pl → 不匹配，但 pu_latest → 匹配）
            # 2. 通过 cte_alias_map 匹配别名（如 im_agg → INV_MTR_AGG）
            alias_upper = sf_alias.upper()
            if alias_upper in cte_names_upper:
                resolved_cte = alias_upper
            elif alias_upper in cte_alias_map:
                resolved_cte = cte_alias_map[alias_upper]
            else:
                continue

            cte_fields = cte_field_map.get(resolved_cte, {})
            cte_field = cte_fields.get(sf_field.upper())
            if not cte_field:
                continue

            # 找到 CTE 中对应的字段
            cte_transform = cte_field.get("transform_type", "unknown")
            cte_sources = cte_field.get("source_fields", [])
            cte_expr = cte_field.get("expression", "")

            # 传播规则：CTE 内有"更重"的加工类型时，覆盖主查询的 transform_type
            cte_prio = TRANSFORM_PRIORITY.get(cte_transform, 0)
            main_prio = TRANSFORM_PRIORITY.get(final_transform, 0)
            if cte_prio > main_prio:
                final_transform = cte_transform

            # 注入 CTE 穿透信息
            sf["cte_name"] = resolved_cte
            sf["cte_transform_type"] = cte_transform
            sf["cte_source_fields"] = cte_sources
            sf["cte_expression"] = cte_expr

            # 嵌套递归：如果 CTE 的 source_fields 又引用了另一个 CTE，递归穿透
            _resolve_nested_cte(sf, cte_field_map, cte_names_upper, cte_alias_map, visited={resolved_cte})

        col.transform_type = final_transform


def _resolve_nested_cte(
    source_field: dict,
    cte_field_map: dict[str, dict[str, dict]],
    cte_names_upper: set,
    cte_alias_map: dict = None,
    visited: set = None,
    max_depth: int = 10,
) -> None:
    """递归解析嵌套 CTE 引用（CTE_A → CTE_B → CTE_C）。

    将最深层的 CTE 源字段追加到 cte_source_fields 链。
    visited 防止循环引用，max_depth 防止无限递归。
    """
    if max_depth <= 0:
        return

    visited = visited or set()
    cte_alias_map = cte_alias_map or {}

    cte_sources = source_field.get("cte_source_fields", [])
    if not cte_sources:
        return

    # 收集所有穿透后的源字段（含递归展开的）
    resolved_all = []

    for csf in cte_sources:
        csf_alias = csf.get("alias", "")
        csf_field = csf.get("field", "")

        resolved_all.append(csf)

        # 检查这个 CTE 源字段是否引用了另一个 CTE
        if not csf_alias:
            continue
        alias_upper = csf_alias.upper()
        # 解析别名 → CTE 名（支持别名和直接 CTE 名）
        if alias_upper in cte_names_upper:
            resolved_cte = alias_upper
        elif alias_upper in cte_alias_map:
            resolved_cte = cte_alias_map[alias_upper]
        else:
            continue

        if resolved_cte in visited:
            continue

        nested_cte_fields = cte_field_map.get(resolved_cte, {})
        nested_field = nested_cte_fields.get(csf_field.upper())
        if not nested_field:
            continue

        # 递归穿透
        nested_sources = nested_field.get("source_fields", [])
        nested_transform = nested_field.get("transform_type", "unknown")

        # 如果嵌套 CTE 的加工类型更重，覆盖当前 CTE 的 transform_type
        cte_transform = source_field.get("cte_transform_type", "unknown")
        nested_prio = TRANSFORM_PRIORITY.get(nested_transform, 0)
        current_prio = TRANSFORM_PRIORITY.get(cte_transform, 0)
        if nested_prio > current_prio:
            source_field["cte_transform_type"] = nested_transform

        # 递归解析下一层
        visited.add(resolved_cte)
        _resolve_nested_cte(
            {"cte_source_fields": nested_sources, "cte_transform_type": nested_transform},
            cte_field_map,
            cte_names_upper,
            cte_alias_map,
            visited,
            max_depth - 1,
        )
        visited.discard(resolved_cte)

    source_field["cte_source_fields"] = resolved_all


# ═══════════════════════════════════════════════════════════════
# transform_type 分类
# ═══════════════════════════════════════════════════════════════

def classify_transform(expr_node, expr_sql: str) -> str:
    """分类转换类型。

    优先级（从高到低）：
    value → window → pivot → aggregate → case_when → fallback → direct → expression
    """
    # value: 字面量或 ${变量}（已经被替换为 NULL）
    if isinstance(expr_node, (exp.Literal, exp.Null)):
        return "value"
    if isinstance(expr_node, exp.CurrentTimestamp):
        return "value"
    # 检查原始 SQL 中是否有 ${...}（在替换前）
    if re.search(r"\$\{", _INLINE_COMMENT_RE.sub("", expr_sql)):
        return "value"
    # 纯字符串字面量 如 'N'
    if expr_sql.strip().startswith("'") and expr_sql.count("'") == 2:
        return "value"

    # 解包 Alias
    if isinstance(expr_node, exp.Alias):
        return classify_transform(expr_node.this, expr_sql)

    # window: 包含 OVER
    if _has_window(expr_node):
        return "window"

    # pivot: 聚合函数 + CASE WHEN 组合
    has_agg = _has_aggregate(expr_node)
    has_case = _has_case_when(expr_node)
    if has_agg and has_case:
        return "pivot"

    # aggregate: 聚合函数
    if has_agg:
        return "aggregate"

    # case_when: CASE WHEN
    if has_case:
        return "case_when"

    # fallback: COALESCE / NVL / IFNULL
    if _has_fallback(expr_node):
        return "fallback"

    # direct: 单一字段引用
    if isinstance(expr_node, exp.Column):
        return "direct"

    # 兜底: expression
    return "expression"


def _has_window(node) -> bool:
    return any(isinstance(n, exp.Window) for n in node.walk())


def _has_aggregate(node) -> bool:
    return any(isinstance(n, (exp.Sum, exp.Avg, exp.Count, exp.Max, exp.Min)) for n in node.walk())


def _has_case_when(node) -> bool:
    return any(isinstance(n, exp.Case) for n in node.walk())


def _has_fallback(node) -> bool:
    """检测 COALESCE / NVL / IFNULL"""
    if isinstance(node, exp.Coalesce):
        return True
    # sqlglot 可能将 NVL/IFNULL 解析为 Coalesce 或匿名函数
    for n in node.walk():
        if isinstance(n, exp.Anonymous):
            if n.name.upper() in ("NVL", "IFNULL", "ISNULL"):
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# Step 4: build_topology() — 双图 + 场景分组
# ═══════════════════════════════════════════════════════════════

def build_scenarios(rules: list[RawRule], parsed_map: dict) -> list[dict]:
    """场景分组：按分区名（删除条件）分组为场景。

    场景判定逻辑:
    1. 删除模式为分区级(3/5)且有删除条件 → 按删除条件(分区名)分组
    2. 同一个分区名的所有规则属于同一场景
    3. 删除模式=1(TRUNCATE TABLE) → 公共步骤，不属于任何场景
    4. 单场景（所有规则只有一个分区或无分区）→ 不分场景

    返回: [{id, name, partition, rule_codes, rule_count, is_common}, ...]
    """
    # 收集所有分区场景
    partition_groups: dict[str, list[str]] = {}  # {partition: [rule_codes]}
    common_rules: list[str] = []

    for rule in rules:
        rc = rule.rule_code
        dm = (rule.delete_mode or "").strip()
        dc = (rule.delete_condition or "").strip()

        if dm in PARTITION_DELETE_MODES and dc:
            # 分区级写入
            partition_groups.setdefault(dc, []).append(rc)
        else:
            # 非分区（TRUNCATE TABLE / NO DELETE 等）→ 公共步骤
            common_rules.append(rc)

    # 构建场景列表
    scenarios = []

    if len(partition_groups) <= 1:
        # 只有0或1个分区场景 → 不分场景
        all_rule_codes = []
        for partition, rcs in partition_groups.items():
            all_rule_codes.extend(rcs)
        all_rule_codes.extend(common_rules)

        scenarios.append({
            "id": "scenario_1",
            "name": "默认场景",
            "partition": "",
            "rule_codes": all_rule_codes,
            "rule_count": len(all_rule_codes),
            "is_common": False,
            "is_multi_scenario": False,
        })
        return scenarios

    # 多场景
    for i, (partition, rcs) in enumerate(sorted(partition_groups.items()), start=1):
        scenarios.append({
            "id": f"scenario_{i}",
            "name": f"场景{i} (分区: {partition})",
            "partition": partition,
            "rule_codes": rcs,
            "rule_count": len(rcs),
            "is_common": False,
            "is_multi_scenario": True,
        })

    # 公共步骤
    if common_rules:
        scenarios.append({
            "id": "scenario_common",
            "name": "公共步骤",
            "partition": "",
            "rule_codes": common_rules,
            "rule_count": len(common_rules),
            "is_common": True,
            "is_multi_scenario": True,
        })

    return scenarios


def generate_step_description(rule: RawRule, parsed, scenarios: list[dict], all_rules: list[RawRule]) -> dict:
    """脚本自动生成 purpose + logic 兜底描述（不依赖 AI）。

    Returns: {"purpose": str, "logic": str}
    """
    # 找到当前规则属于哪个场景
    scenario = None
    for s in scenarios:
        if rule.rule_code in s.get("rule_codes", []):
            scenario = s
            break

    # 写入模式
    dm = (rule.delete_mode or "").strip()
    write_mode = DELETE_MODE_MAP.get(dm, f"delete_mode={dm}")
    dc = (rule.delete_condition or "").strip()

    # 目标表
    target = rule.target_table or ""

    # 来源表
    source_tables = [j.source_table for j in parsed.source_tables] if parsed else []

    # purpose
    partition_desc = f" → 分区[{dc}]" if dc else ""
    scenario_desc = ""
    if scenario and scenario.get("is_multi_scenario") and not scenario.get("is_common"):
        scenario_desc = f"[{scenario['name']}] "

    # 规则类型语义
    rule_type_label = RULE_TYPE_MAP.get(rule.rule_type, "")
    type_prefix = f"[{rule_type_label}] " if rule_type_label and rule.rule_type not in SELECT_RULE_TYPES else ""

    purpose = f"{scenario_desc}{type_prefix}{write_mode}{partition_desc} 写入 {target}"

    # logic — 基于加工模式生成
    parts = []

    # 非 SELECT 类规则的 logic（删数/分区交换等）
    if rule.rule_type not in SELECT_RULE_TYPES:
        if rule.rule_type == 2:
            parts.append("删除操作")
        elif rule.rule_type == 9:
            temp = rule.target_table or ""
            target = rule.exchange_source_table or ""
            parts.append(f"分区交换: 临时表 {temp} → 目标表 {target}")
        if rule.query_sql:
            parts.append(f"SQL: {rule.query_sql[:60]}...")
    else:
        # SELECT 类规则的正常 logic
        if source_tables:
            if len(source_tables) == 1:
                parts.append(f"从 {source_tables[0]} 加载")
            else:
                parts.append(f"从 {len(source_tables)} 张表关联加载: {', '.join(source_tables[:3])}")

    # CTE
    cte_count = len(parsed.ctes) if parsed else 0
    if cte_count:
        cte_names = [c.name for c in parsed.ctes]
        parts.append(f"使用 {cte_count} 个 CTE ({', '.join(cte_names)})")

    # 加工类型
    if parsed:
        transform_types = set()
        for col in parsed.select_columns:
            transform_types.add(col.transform_type)
        interesting = transform_types - {"direct", "value", "unknown"}
        if interesting:
            type_map = {"pivot": "行转列", "aggregate": "聚合", "window": "窗口函数",
                       "case_when": "条件加工", "fallback": "NULL兜底"}
            desc_parts = [type_map.get(t, t) for t in interesting]
            parts.append("加工: " + ", ".join(desc_parts))

    logic = "，".join(parts) if parts else "直接加载"

    return {"purpose": purpose, "logic": logic}

def build_topology(rules: list[RawRule], parsed_map: dict[str, ParsedSQL]) -> dict:
    """构建调度图 + 数据依赖图。

    Returns: topology section of knowledge.json
    """
    # 构建 step_id → rule 映射
    steps = []
    rule_to_step = {}
    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rule_to_step[rule.rule_code] = step_id

        target_full = _normalize_table_name(rule.target_schema, rule.target_table)

        # SQL 中解析出的源表（主查询的 FROM/JOIN，不含 CTE/子查询内部）
        parsed = parsed_map.get(rule.rule_code)
        sql_source_tables = []
        all_sql_tables = []  # 含子查询内表（用于自引用检测）
        if parsed and parsed.raw_sql:
            # 主查询源表（同表名去重，自连接不重复计）
            for j in parsed.source_tables:
                if j.source_table not in sql_source_tables:
                    sql_source_tables.append(j.source_table)
            # 全树扫描所有表（含子查询），用于自引用检测
            # 限定在 SELECT 子树内，避免把 CREATE VIEW 的定义表名误判为引用
            # 对 UNION/INTERSECT/EXCEPT，扫描所有分支
            try:
                clean = _strip_dws_clauses(parsed.raw_sql)
                clean = _replace_placeholders(clean)
                tree = sqlglot.parse_one(clean, dialect="postgres")
                if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
                    # 集合操作：收集所有分支的表
                    branches = []
                    _collect_set_branches(tree, branches)
                    for branch in branches:
                        for table in branch.find_all(exp.Table):
                            tname = ".".join(_clean_name(p.name) for p in table.parts)
                            if tname and tname not in all_sql_tables:
                                all_sql_tables.append(tname)
                else:
                    select_node = tree.find(exp.Select)
                    if select_node:
                        for table in select_node.find_all(exp.Table):
                            tname = ".".join(_clean_name(p.name) for p in table.parts)
                            if tname and tname not in all_sql_tables:
                                all_sql_tables.append(tname)
                    else:
                        for table in tree.find_all(exp.Table):
                            tname = ".".join(_clean_name(p.name) for p in table.parts)
                            if tname and tname not in all_sql_tables:
                                all_sql_tables.append(tname)
            except Exception:
                all_sql_tables = sql_source_tables  # fallback

        # 分区交换（类型9）特殊处理：
        # target_table 是临时表，exchange_source_table 是真正的目标表（分区表）
        real_target_table = rule.target_table
        real_target_full = target_full
        is_exchange = rule.rule_type == 9
        if is_exchange and rule.exchange_source_table:
            # 交换分区来源表才是真正的目标表
            real_target_table = rule.exchange_source_table
            # 解析 schema.table
            parts = real_target_table.split(".")
            real_target_full = real_target_table if "." in real_target_table else f"{rule.target_schema}.{real_target_table}" if rule.target_schema else real_target_table

        steps.append({
            "step_id": step_id,
            "rule_code": rule.rule_code,
            "rule_type": rule.rule_type,
            "exec_sequence": rule.exec_sequence,
            "target_schema": rule.target_schema,
            "target_table": real_target_table,
            "target_table_full": real_target_full,
            "delete_mode": rule.delete_mode,
            "delete_condition": rule.delete_condition,
            "source_tables_from_sql": sql_source_tables,
            "all_tables_from_sql": all_sql_tables,
            "is_exchange": is_exchange,
            "exchange_temp_table": rule.target_table if is_exchange else "",  # 临时表名
        })

    # ── 调度图（执行序列直接读取）──
    schedule_groups = {}
    for s in steps:
        seq = s["exec_sequence"]
        schedule_groups.setdefault(seq, []).append(s["step_id"])

    schedule_plan = [
        {"sequence": seq, "parallel_steps": sids}
        for seq, sids in sorted(schedule_groups.items())
    ]

    # ── 索引：目标表 → 写入它的步骤（大写归一化，避免大小写不匹配）──
    target_writers = {}  # table_full(UPPER) → [step_id, ...]
    for s in steps:
        target_writers.setdefault(s["target_table_full"].upper(), []).append(s["step_id"])

    # ── 数据依赖图（SQL 交叉比对，大小写不敏感）──
    data_dependencies = []
    for s in steps:
        # 交换分区步骤：依赖写入临时表的步骤
        if s.get("is_exchange") and s.get("exchange_temp_table"):
            temp_table_upper = s["exchange_temp_table"].upper()
            # 也可能带 schema
            temp_full = s.get("target_schema", "") + "." + temp_table_upper if s.get("target_schema") else temp_table_upper
            for tbl_key in [temp_table_upper, temp_full]:
                if tbl_key in target_writers:
                    for writer_step in target_writers[tbl_key]:
                        if writer_step != s["step_id"]:
                            data_dependencies.append({
                                "from": writer_step,
                                "to": s["step_id"],
                                "type": "exchange",
                                "intermediate_table": s.get("exchange_temp_table", ""),
                            })
            continue  # 交换分区不再走常规来源表匹配

        for src_table in s["source_tables_from_sql"]:
            src_upper = src_table.upper()
            if src_upper in target_writers:
                for writer_step in target_writers[src_upper]:
                    if writer_step != s["step_id"]:
                        data_dependencies.append({
                            "from": writer_step,
                            "to": s["step_id"],
                            "type": "data_flow",
                            "intermediate_table": src_table,
                        })

    # ── 自引用检测 ──
    self_references = []
    for s in steps:
        target = s["target_table_full"].upper()
        # 用 all_tables_from_sql（含子查询）检测自引用
        all_tables = [t.upper() for t in s.get("all_tables_from_sql", s["source_tables_from_sql"])]
        if target in all_tables:
            # 检测具体模式
            parsed = parsed_map.get(s["rule_code"])
            pattern = ""
            if parsed and parsed.raw_sql:
                raw_upper = parsed.raw_sql.upper()
                if "EXISTS" in raw_upper:
                    pattern = "WHERE EXISTS (SELECT ... FROM 目标表)"
                elif "IN (" in raw_upper and target.upper() in raw_upper:
                    pattern = "WHERE ... IN (SELECT ... FROM 目标表)"
                else:
                    pattern = "FROM/JOIN 引用目标表"

            self_references.append({
                "step": s["step_id"],
                "table": target,
                "pattern": pattern,
            })

    # ── 隐式依赖（同目标表多步骤写入）──
    implicit_dependencies = []
    for table, writers in target_writers.items():
        if len(writers) <= 1:
            continue
        # 找出写入该表的最大执行序列之后，是否有步骤读取该表
        max_writer_seq = max(
            s["exec_sequence"] for s in steps if s["step_id"] in writers
        )
        # 后续步骤中读取该表的
        later_readers = []
        for s in steps:
            if s["step_id"] in writers:
                continue
            if s["exec_sequence"] > max_writer_seq:
                if table in s["source_tables_from_sql"]:
                    later_readers.append(s["step_id"])

        # 自引用的步骤也依赖前面的写入
        for sr in self_references:
            if sr["table"] == table and sr["step"] in writers:
                # 自引用步骤依赖同目标表的其他写入步骤
                earlier_writers = [
                    w for w in writers
                    if w != sr["step"]
                    and any(s["step_id"] == w and s["exec_sequence"] < next(
                        (st["exec_sequence"] for st in steps if st["step_id"] == sr["step"]), 999
                    ) for st in steps if st["step_id"] == w)
                ]
                if earlier_writers:
                    # 检查是否已经在 data_dependencies 里
                    already = any(
                        d["from"] in earlier_writers and d["to"] == sr["step"]
                        for d in data_dependencies
                    )
                    if not already:
                        implicit_dependencies.append({
                            "from": earlier_writers,
                            "to": sr["step"],
                            "reason": f"step {sr['step']} 读写目标表 {table}，该表由 {', '.join(earlier_writers)} 写入",
                            "confidence": "inferred",
                        })

        # 后续读者隐式依赖全部写入者
        if later_readers:
            for reader in later_readers:
                already = any(
                    d["to"] == reader for d in data_dependencies
                )
                if not already:
                    implicit_dependencies.append({
                        "from": writers,
                        "to": reader,
                        "reason": f"step {reader} 读取 {table}，该表由 {', '.join(writers)} 写入",
                        "confidence": "inferred",
                    })

    # ── 同目标表写入分组 ──
    target_write_groups = []
    for table, writers in target_writers.items():
        if len(writers) > 1:
            writer_seqs = sorted(set(
                s["exec_sequence"] for s in steps if s["step_id"] in writers
            ))
            if len(writer_seqs) == 1:
                pattern = "parallel"
            elif self_references and any(
                sr["table"] == table for sr in self_references
            ):
                pattern = "parallel_then_serial_with_self_ref"
            else:
                pattern = "parallel_then_serial"

            target_write_groups.append({
                "target_table": table,
                "writers": writers,
                "write_pattern": pattern,
                "has_self_reference": any(
                    sr["table"] == table for sr in self_references
                ),
            })

    # ── 过度约束分析 ──
    over_constraints = []
    for s in steps:
        my_seq = s["exec_sequence"]
        # 调度层面等谁：所有前置层级的步骤
        waits_for = []
        for seq, sids in schedule_groups.items():
            if seq < my_seq:
                waits_for.extend(sids)

        # 数据层面依赖谁
        actually_depends = []
        for d in data_dependencies:
            if d["to"] == s["step_id"]:
                actually_depends.append(d["from"])
        for imp in implicit_dependencies:
            if imp["to"] == s["step_id"]:
                if isinstance(imp["from"], list):
                    actually_depends.extend(imp["from"])
                else:
                    actually_depends.append(imp["from"])

        over = [w for w in waits_for if w not in actually_depends]
        if over and waits_for:
            over_constraints.append({
                "step": s["step_id"],
                "schedule_waits_for": waits_for,
                "actually_depends_on": list(set(actually_depends)),
                "over_constrained_on": over,
            })

    # ── 场景分组 ──
    scenarios = build_scenarios(rules, parsed_map)

    # 给每个 step 关联场景 ID、删除条件、规则中文名
    for s in steps:
        rc = s["rule_code"]
        # 找原始 rule
        orig_rule = next((r for r in rules if r.rule_code == rc), None)
        s["delete_condition"] = orig_rule.delete_condition if orig_rule else ""
        s["delete_mode_label"] = DELETE_MODE_MAP.get(
            (orig_rule.delete_mode or "").strip() if orig_rule else "", ""
        )
        s["rule_name"] = orig_rule.rule_name if orig_rule else ""
        # 关联场景
        for sc in scenarios:
            if rc in sc.get("rule_codes", []):
                s["scenario_id"] = sc["id"]
                s["scenario_name"] = sc["name"]
                s["is_common_step"] = sc.get("is_common", False)
                break
        else:
            s["scenario_id"] = ""
            s["scenario_name"] = ""
            s["is_common_step"] = False

    return {
        "steps": steps,
        "schedule_plan": schedule_plan,
        "data_dependencies": data_dependencies,
        "self_references": self_references,
        "implicit_dependencies": implicit_dependencies,
        "target_write_groups": target_write_groups,
        "over_constraints": over_constraints,
        "scenarios": scenarios,
    }


# ═══════════════════════════════════════════════════════════════
# Step 5: build_field_mappings() — 双源交叉
# ═══════════════════════════════════════════════════════════════

def build_field_mappings(
    rules: list[RawRule],
    parsed_map: dict[str, ParsedSQL],
    target_fields_map: dict[str, list[RawTargetField]],
) -> dict:
    """双源交叉：TargetFields + SQL AST。

    Returns: field_mappings section of knowledge.json
    """
    all_fields = []
    all_warnings = []

    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        parsed = parsed_map.get(rc)
        if not parsed:
            continue

        # TargetFields 按 target_field 小写建索引（大小写不敏感）
        tf_index = {}
        for tf in target_fields_map.get(rc, []):
            tf_index[tf.target_field.lower()] = tf

        # SQL SELECT 列按 alias 匹配
        sql_aliases = set()
        for col in parsed.select_columns:
            alias = col.alias
            sql_aliases.add(alias)

            tf_data = tf_index.get(alias.lower())

            field_entry = {
                "target_field": alias,
                "producing_step": step_id,
                "rule_code": rc,
                "in_target_fields": tf_data is not None,
                "excel_source_field": tf_data.source_field if tf_data else None,
                "transform_type": col.transform_type,
                "lineage": [
                    {
                        "step": step_id,
                        "source_table": sf.get("alias", ""),
                        "source_field": sf.get("field", ""),
                        "transform": col.transform_type,
                        "raw_sql": col.expression,
                        "cte_name": sf.get("cte_name", ""),
                        "cte_transform_type": sf.get("cte_transform_type", ""),
                        "cte_source_fields": sf.get("cte_source_fields", []),
                        "cte_expression": sf.get("cte_expression", ""),
                    }
                    for sf in col.source_fields
                ],
                "validation": {
                    "excel_vs_sql_match": tf_data is not None,
                },
            }
            all_fields.append(field_entry)

        # TargetFields 里有但 SQL 没匹配到的
        only_in_excel = []
        for tf in target_fields_map.get(rc, []):
            if tf.target_field not in sql_aliases:
                only_in_excel.append(tf.target_field)
                all_fields.append({
                    "target_field": tf.target_field,
                    "producing_step": step_id,
                    "rule_code": rc,
                    "in_target_fields": True,
                    "excel_source_field": tf.source_field,
                    "transform_type": "unknown",
                    "lineage": [],
                    "validation": {
                        "excel_vs_sql_match": None,
                    },
                    "note": "TargetFields 有记录但 SQL SELECT 中未找到对应别名",
                })

        # 差异预警
        only_in_sql = [
            col.alias for col in parsed.select_columns
            if col.alias and col.alias not in tf_index
        ]
        if only_in_sql or only_in_excel:
            all_warnings.append({
                "rule_code": rc,
                "type": "field_name_mismatch",
                "severity": "info",
                "title": f"规则 {rc} 存在 {len(only_in_excel)} 个 TargetFields 字段和 "
                         f"{len(only_in_sql)} 个 SQL 列无法通过别名精确匹配",
                "only_in_excel": only_in_excel,
                "only_in_sql": only_in_sql,
            })

    # ── 统计 ──
    total_in_sql = len([f for f in all_fields if f.get("transform_type") != "unknown"])
    total_in_excel = len([f for f in all_fields if f.get("in_target_fields")])
    match_count = len([f for f in all_fields if f.get("validation", {}).get("excel_vs_sql_match") is True])
    only_in_sql = [f["target_field"] for f in all_fields if f.get("in_target_fields") is False]
    only_in_excel_list = [f["target_field"] for f in all_fields if f.get("note")]

    return {
        "fields": all_fields,
        "statistics": {
            "total_in_sql": total_in_sql,
            "total_in_excel": total_in_excel,
            "match_count": match_count,
            "only_in_sql": only_in_sql,
            "only_in_excel": only_in_excel_list,
        },
        "differences": [
            {
                "field": f["target_field"],
                "in_excel": f.get("in_target_fields", False),
                "in_sql": f.get("transform_type", "unknown") != "unknown",
                "transform_type": f.get("transform_type"),
                "note": f.get("note", ""),
            }
            for f in all_fields
            if f.get("validation", {}).get("excel_vs_sql_match") is not True
        ],
        "warnings": all_warnings,
    }


# ═══════════════════════════════════════════════════════════════
# Step 6: analyze_quality() — 规则引擎
# ═══════════════════════════════════════════════════════════════

def analyze_quality(
    topology: dict,
    data_flow: dict,
    field_mappings: dict,
    parsed_map: dict[str, ParsedSQL],
) -> dict:
    """规则引擎：复杂度指标 + 反模式检测。

    Returns: quality section of knowledge.json
    """
    issues = []
    issue_id = 0
    complexity_metrics = {
        "max_join_count": 0,
        "max_cte_count": 0,
        "max_select_column_count": 0,
        "total_source_tables": 0,
        "total_case_when_branches": 0,
        "transform_distribution": {},
    }

    all_source_tables = set()
    all_transform_types = Counter()

    for s in topology["steps"]:
        rc = s["rule_code"]
        step_id = s["step_id"]
        parsed = parsed_map.get(rc)
        if not parsed:
            continue

        # ── 复杂度指标 ──
        # 主查询 JOIN 数
        join_count = len([j for j in parsed.source_tables if j.join_type != "FROM"])
        # CTE 内部 JOIN 数（CTE 内部的表也要统计）
        cte_join_count = 0
        for cte in parsed.ctes:
            for ct in cte.source_tables:
                if ct.get("join_type", "FROM") != "FROM":
                    cte_join_count += 1
        total_join_count = join_count + cte_join_count

        cte_count = len(parsed.ctes)
        select_count = len(parsed.select_columns)
        case_when_branches = sum(
            1 for col in parsed.select_columns if col.transform_type in ("case_when", "pivot")
        )

        complexity_metrics["max_join_count"] = max(complexity_metrics["max_join_count"], total_join_count)
        complexity_metrics["max_cte_count"] = max(complexity_metrics["max_cte_count"], cte_count)
        complexity_metrics["max_select_column_count"] = max(
            complexity_metrics["max_select_column_count"], select_count
        )
        complexity_metrics["total_case_when_branches"] += case_when_branches

        # 来源表统计：含 CTE 内部表
        for j in parsed.source_tables:
            all_source_tables.add(j.source_table)
        for cte in parsed.ctes:
            for ct in cte.source_tables:
                tname = ct.get("name", "")
                if tname:
                    all_source_tables.add(tname)

        for col in parsed.select_columns:
            all_transform_types[col.transform_type] += 1

        # ── 反模式检测 ──

        # 1. JOIN 缺少 ON 条件
        for j in parsed.source_tables:
            if j.join_type not in ("FROM",) and not j.join_condition:
                issue_id += 1
                issues.append({
                    "id": f"ISS_{issue_id:03d}",
                    "severity": "critical",
                    "category": "data_quality",
                    "title": f"JOIN 缺少 ON 条件: {j.source_table}",
                    "rule_code": rc,
                    "step_id": step_id,
                })

        # 2. 单步骤 JOIN 过多（含 CTE 内部 JOIN）
        if total_join_count > 8:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "performance",
                "title": f"单步骤 JOIN {total_join_count} 张表（主查询{join_count} + CTE内部{cte_join_count}）",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 3. CTE 嵌套过深
        if cte_count > 3:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "maintainability",
                "title": f"CTE 数量 {cte_count}，嵌套过深",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 4. CASE WHEN 分支过多
        if case_when_branches > 20:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "maintainability",
                "title": f"CASE WHEN/PIVOT 分支共 {case_when_branches} 个",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 5. 未解析来源表：无 schema 前缀且非 CTE 定义的表引用
        cte_names_in_step = {c.name.upper() for c in parsed.ctes}
        unresolved_tables = []
        for j in parsed.source_tables:
            has_schema = "." in j.source_table
            is_cte = j.source_table.split(".")[-1].upper() in cte_names_in_step
            if not has_schema and not is_cte:
                unresolved_tables.append(j.source_table)
        if unresolved_tables:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "data_lineage",
                "title": f"来源表无法追溯 schema: {', '.join(unresolved_tables)}",
                "detail": (
                    f"以下表引用缺少 schema 前缀且未在 WITH/CTE 中定义，"
                    f"可能是视图、同义词或制品包导出不完整: {unresolved_tables}"
                ),
                "rule_code": rc,
                "step_id": step_id,
            })

    # ── 字段差异检测 ──
    stats = field_mappings.get("statistics", {})
    diff_count = len(stats.get("only_in_sql", [])) + len(stats.get("only_in_excel", []))
    if diff_count > 4:
        issue_id += 1
        issues.append({
            "id": f"ISS_{issue_id:03d}",
            "severity": "info",
            "category": "field_mapping",
            "title": f"TargetFields({stats.get('total_in_excel', 0)}) 与 SQL SELECT({stats.get('total_in_sql', 0)}) 差异 {diff_count} 字段",
            "rule_code": "",
            "step_id": "",
        })

    # ── 字段无别名回溯预警 ──
    for f in field_mappings.get("fields", []):
        step_id = f.get("producing_step", "")
        for lin in f.get("lineage", []):
            if not lin.get("source_table"):
                field_name = f.get("target_field", "?")
                source_field = lin.get("source_field", "")
                step_num = step_id.replace("step_", "")
                # 检查该步骤有多少源表
                step_info = next(
                    (s for s in topology["steps"] if s["step_id"] == step_id), None
                )
                src_count = len(step_info["source_tables_from_sql"]) if step_info else 0
                if src_count > 1:
                    sev = "medium"
                    detail = (
                        f"步骤 {step_id} 有 {src_count} 个来源表，字段 {field_name} "
                        f"缺少别名前缀（引用 {source_field}），无法确定来源表"
                    )
                else:
                    sev = "info"
                    detail = (
                        f"字段 {field_name} 缺少别名前缀（引用 {source_field}），"
                        f"虽然步骤仅有 1 个来源表可推断，但建议显式标注"
                    )
                issue_id += 1
                issues.append({
                    "id": f"ISS_{issue_id:03d}",
                    "severity": sev,
                    "category": "data_lineage",
                    "title": f"字段 {field_name} 无别名前缀，无法回溯来源表",
                    "detail": detail,
                    "rule_code": f.get("rule_code", ""),
                    "step_id": step_id,
                })
                # 每个字段只报一次
                break

    # ── 调度过度约束 ──
    for oc in topology.get("over_constraints", []):
        if oc.get("over_constrained_on"):
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "info",
                "category": "scheduling",
                "title": f"step {oc['step']} 调度过度约束，不必要等待 {len(oc['over_constrained_on'])} 个步骤",
                "rule_code": "",
                "step_id": oc["step"],
                "detail": oc["over_constrained_on"],
            })

    # ── 自引用检测 ──
    for sr in topology.get("self_references", []):
        issue_id += 1
        issues.append({
            "id": f"ISS_{issue_id:03d}",
            "severity": "info",
            "category": "design",
            "title": f"step {sr['step']} 自引用目标表 {sr['table']}",
            "rule_code": "",
            "step_id": sr["step"],
            "detail": sr.get("pattern", ""),
        })

    complexity_metrics["total_source_tables"] = len(all_source_tables)
    complexity_metrics["transform_distribution"] = dict(all_transform_types)

    # 统计
    severity_count = Counter(iss["severity"] for iss in issues)

    return {
        "complexity_metrics": complexity_metrics,
        "issues": issues,
        "issue_statistics": {
            "critical": severity_count.get("critical", 0),
            "medium": severity_count.get("medium", 0),
            "low": severity_count.get("low", 0),
            "info": severity_count.get("info", 0),
        },
    }


# ═══════════════════════════════════════════════════════════════
# 辅助: build_data_flow()
# ═══════════════════════════════════════════════════════════════

def build_data_flow(
    rules: list[RawRule],
    parsed_map: dict[str, ParsedSQL],
) -> dict:
    """构建表级数据流。

    Returns: data_flow section of knowledge.json
    """
    all_tables = []
    seen_tables = set()

    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        target_full = _normalize_table_name(rule.target_schema, rule.target_table)

        # 目标表
        if target_full not in seen_tables:
            seen_tables.add(target_full)
            all_tables.append({
                "schema": rule.target_schema,
                "name": rule.target_table,
                "role": "target",
                "layer": _infer_layer(rule.target_schema, rule.target_table),
            })

        parsed = parsed_map.get(rc)
        if not parsed:
            continue

        # 源表（主查询）
        for j in parsed.source_tables:
            if j.source_table not in seen_tables:
                seen_tables.add(j.source_table)
                parts = j.source_table.split(".")
                s_schema = parts[0] if len(parts) > 1 else ""
                s_name = parts[-1] if len(parts) > 1 else parts[0]
                all_tables.append({
                    "schema": s_schema,
                    "name": s_name,
                    "role": "intermediate" if j.source_table.startswith("tmp") else "source",
                    "layer": _infer_layer(s_schema, s_name),
                })

        # CTE 内部源表（也纳入来源表统计）
        for cte in parsed.ctes:
            for ct in cte.source_tables:
                tname = ct.get("name", "")
                if tname and tname not in seen_tables:
                    seen_tables.add(tname)
                    parts = tname.split(".")
                    s_schema = parts[0] if len(parts) > 1 else ""
                    s_name = parts[-1] if len(parts) > 1 else parts[0]
                    all_tables.append({
                        "schema": s_schema,
                        "name": s_name,
                        "role": "intermediate" if tname.startswith("tmp") else "source",
                        "layer": _infer_layer(s_schema, s_name),
                    })

    # 每步骤详情
    steps_detail = []
    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        target_full = _normalize_table_name(rule.target_schema, rule.target_table)
        parsed = parsed_map.get(rc)
        if not parsed:
            continue

        # 写入模式
        dm = rule.delete_mode
        if dm == "1":
            write_mode = "TRUNCATE + INSERT"
        elif dm == "0":
            write_mode = "APPEND"
        else:
            write_mode = f"delete_mode={dm}"

        step_entry = {
            "step_id": step_id,
            "rule_code": rc,
            "target_table": target_full,
            "write_mode": write_mode,
            "joins": [
                {
                    "source_table": j.source_table,
                    "alias": j.alias,
                    "join_type": j.join_type,
                    "join_condition": j.join_condition,
                }
                for j in parsed.source_tables
            ],
            "where_clause": parsed.where_clause,
            "group_by": parsed.group_by,
            "having_clause": parsed.having_clause,
            "ctes": [
                {
                    "name": c.name,
                    "source_tables": c.source_tables,
                    "fields": c.fields,
                }
                for c in parsed.ctes
            ],
            "raw_sql": parsed.raw_sql,
        }
        steps_detail.append(step_entry)

    return {
        "tables": all_tables,
        "steps": steps_detail,
    }


# ═══════════════════════════════════════════════════════════════
# 辅助: detect_patterns() — 加工模式标签自动检测
# ═══════════════════════════════════════════════════════════════

def detect_patterns(
    parsed_map: dict[str, ParsedSQL],
    topology: dict,
) -> list[dict]:
    """从 SQL AST 模式自动检测加工模式标签。

    Returns: [{label, category, detail, steps}, ...]
    """
    patterns = []

    # 收集所有步骤的指标
    has_cte = False
    has_pivot = False
    has_window = False
    has_aggregate = False
    has_case_when = False
    has_fallback = False
    has_not_exists = False
    has_value_fields = False
    cte_count = 0

    for rc, p in parsed_map.items():
        if p.ctes:
            has_cte = True
            cte_count = max(cte_count, len(p.ctes))
        for col in p.select_columns:
            tt = col.transform_type
            if tt == "pivot":
                has_pivot = True
            elif tt == "window":
                has_window = True
            elif tt == "aggregate":
                has_aggregate = True
            elif tt == "case_when":
                has_case_when = True
            elif tt == "fallback":
                has_fallback = True
            elif tt == "value":
                has_value_fields = True

        # NOT EXISTS 检测（自引用或增量排除）
        raw_upper = (p.raw_sql or "").upper()
        if "NOT EXISTS" in raw_upper:
            has_not_exists = True

    # ── 生成标签 ──
    if has_cte:
        patterns.append({
            "label": "CTE预聚合" if has_aggregate else "CTE预处理",
            "category": "structure",
            "detail": f"使用 {cte_count} 个 CTE 预处理数据",
        })

    if has_pivot:
        patterns.append({
            "label": "行转列",
            "category": "transform",
            "detail": "SUM(CASE WHEN...) 将行数据转为列",
        })

    if has_window:
        patterns.append({
            "label": "窗口函数",
            "category": "transform",
            "detail": "ROW_NUMBER/LAG/LEAD OVER 分窗计算",
        })

    if has_aggregate and not has_pivot:
        patterns.append({
            "label": "聚合汇总",
            "category": "transform",
            "detail": "SUM/COUNT/AVG 聚合",
        })

    if has_case_when and not has_pivot:
        patterns.append({
            "label": "条件加工",
            "category": "transform",
            "detail": "CASE WHEN 条件分支",
        })

    if has_fallback:
        patterns.append({
            "label": "NULL兼容",
            "category": "data_quality",
            "detail": "COALESCE/NVL 空值兜底",
        })

    if has_not_exists:
        # 检查是否自引用（增量去重）
        self_refs = topology.get("self_references", [])
        if self_refs:
            patterns.append({
                "label": "增量去重",
                "category": "load_strategy",
                "detail": "NOT EXISTS 引用目标表，增量写入避免重复",
            })
        else:
            patterns.append({
                "label": "NOT EXISTS排除",
                "category": "filter",
                "detail": "NOT EXISTS 子查询排除特定数据",
            })

    if has_value_fields:
        patterns.append({
            "label": "审计字段",
            "category": "metadata",
            "detail": "固定值/CURRENT_TIMESTAMP 审计字段",
        })

    # 多步骤
    step_count = len(topology.get("steps", []))
    if step_count > 1:
        deps = topology.get("data_dependencies", [])
        if deps:
            patterns.append({
                "label": "多步骤串行",
                "category": "structure",
                "detail": f"{step_count} 个步骤，有跨步骤数据依赖",
            })
        else:
            patterns.append({
                "label": "多步骤并行",
                "category": "structure",
                "detail": f"{step_count} 个步骤并行执行",
            })

    # SCD2 检测（ROW_NUMBER + ORDER BY ... DESC + rn=1）
    for rc, p in parsed_map.items():
        for cte in p.ctes:
            for cf in cte.fields if isinstance(cte.fields, list) else []:
                if isinstance(cf, dict):
                    expr = (cf.get("expression", "") or "").upper()
                    if "ROW_NUMBER()" in expr and "OVER" in expr:
                        patterns.append({
                            "label": "SCD2取最新",
                            "category": "dimension",
                            "detail": "ROW_NUMBER 去重取维表最新有效行",
                        })
                        break

    return patterns


# ═══════════════════════════════════════════════════════════════
# 辅助: parse_ddl_for_metadata() — 从 DDL 文件读取字段类型+中文名
# ═══════════════════════════════════════════════════════════════

def parse_ddl_for_metadata(ddl_dir: str, target_table: str) -> dict[str, dict]:
    """从 DDL 文件读取目标表字段类型和中文名。

    支持两种注释格式：
    1. 行内注释: field_name TYPE /* 中文名 */
    2. COMMENT ON COLUMN table.field IS '中文名'

    Returns: {field_name(LOWER): {"type": str, "comment": str}}
    """
    if not ddl_dir:
        return {}

    ddl_path = Path(ddl_dir)
    if not ddl_path.exists():
        return {}

    result: dict[str, dict] = {}
    target_lower = target_table.lower()

    for sql_file in ddl_path.glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8", errors="ignore")
        content_lower = content.lower()

        if target_lower not in content_lower:
            continue
        if "create table" not in content_lower:
            continue

        import re

        # ── 1. 提取字段名+类型（正则，支持 NVARCHAR 等 DWS 方言）──
        ct_match = re.search(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[^\(]*\((.*)\)',
            content, re.DOTALL | re.IGNORECASE
        )
        if not ct_match:
            continue
        body = ct_match.group(1)
        last_paren = body.rfind(")")
        if last_paren > 0:
            body = body[:last_paren]

        pattern = r'^\s*([a-z_][a-z0-9_]*)\s+([a-zA-Z][a-zA-Z0-9]*(?:\([^)]*\))?)'
        skip_words = ('create', 'table', 'view', 'as', 'select', 'from', 'where', 'and', 'or')

        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if line.upper().startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "KEY", "CHECK", ")", "(", "/")):
                continue
            m_re = re.match(pattern, line)
            if m_re:
                fname = m_re.group(1).lower()
                if fname in skip_words:
                    continue
                ftype = m_re.group(2)

                # 尝试提取行内注释 /* 中文名 */
                inline_comment = ""
                cm = re.search(r'/\*\s*(.+?)\s*\*/', line)
                if cm:
                    inline_comment = cm.group(1).strip()

                result[fname] = {"type": ftype, "comment": inline_comment}

        # ── 2. 提取 COMMENT ON COLUMN（覆盖行内注释）──
        for cm_match in re.finditer(
            r"COMMENT\s+ON\s+COLUMN\s+\S+\.(\w+)\s+IS\s*'([^']*)'",
            content, re.IGNORECASE
        ):
            fname = cm_match.group(1).lower()
            comment = cm_match.group(2).strip()
            if fname in result:
                result[fname]["comment"] = comment
            else:
                result[fname] = {"type": "", "comment": comment}

    return result


# 向后兼容旧调用
def parse_ddl_for_types(ddl_dir: str, target_table: str) -> dict[str, str]:
    """已废弃，使用 parse_ddl_for_metadata。保留向后兼容。"""
    metadata = parse_ddl_for_metadata(ddl_dir, target_table)
    return {k: v["type"] for k, v in metadata.items() if v.get("type")}


# ═══════════════════════════════════════════════════════════════
# 辅助: build_source()
# ═══════════════════════════════════════════════════════════════

def build_source(
    rules: list[RawRule],
    target_fields_map: dict[str, list[RawTargetField]],
    group_variables_map: dict[str, list[RawGroupVariable]],
    parsed_map: dict[str, ParsedSQL],
) -> dict:
    """构建原始数据附录。"""
    return {
        "rule_sheet_raw": [
            {
                "rule_code": r.rule_code,
                "exec_sequence": r.exec_sequence,
                "target_schema": r.target_schema,
                "target_table": r.target_table,
                "delete_mode": r.delete_mode,
                "project_code": r.project_code,
                "data_source": r.data_source,
                "business_owner": r.business_owner,
                "rule_group_code": r.rule_group_code,
            }
            for r in rules
        ],
        "target_fields_raw": [
            {
                "rule_code": tf.rule_code,
                "target_field": tf.target_field,
                "source_field": tf.source_field,
                "encryption": tf.encryption,
                "alias": tf.alias,
                "field_type": tf.field_type,
                "remark": tf.remark,
            }
            for tf_list in target_fields_map.values()
            for tf in tf_list
        ],
        "group_variables_raw": [
            {
                "rule_code": gv.rule_code,
                "var_name": gv.var_name,
                "default_value": gv.default_value,
            }
            for gv_list in group_variables_map.values()
            for gv in gv_list
        ],
        "raw_sql": [
            {
                "step_id": f"step_{i + 1}",
                "rule_code": r.rule_code,
                "sql": parsed_map.get(r.rule_code, ParsedSQL()).raw_sql,
            }
            for i, r in enumerate(rules)
        ],
    }


# ═══════════════════════════════════════════════════════════════
# main()
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# AI 摘要生成（给 AI 增强用的精简输入，2-3KB）
# ═══════════════════════════════════════════════════════════════

def _generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality) -> str:
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
            from collections import Counter
            tt_dist = Counter(c.transform_type for c in parsed.select_columns)
            tt_str = ", ".join(f"{k}={v}" for k, v in tt_dist.most_common())
            lines.append(f"- 字段加工: {len(parsed.select_columns)} 列 ({tt_str})")

        # SQL 前 200 字符（不完整 SQL）
        if rule.query_sql:
            sql_preview = rule.query_sql[:200].replace("\n", " ")
            lines.append(f"- SQL 摘要: {sql_preview}...")

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
        lines.append(f"（描述这步的业务目的和加工逻辑）")
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
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--dialect", default="", help="SQL 方言 (oracle/dws/auto)，默认自动检测")
    parser.add_argument("--ddl-dir", default="", help="DDL 文件目录（可选，用于补充字段类型）")
    args = parser.parse_args()

    input_path = Path(args.input)
    # 直接用用户指定的 output 目录，不自动加子目录
    output_dir = Path(args.output)

    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"═══ dws-pipeline-analyzer ═══")
    print(f"输入: {input_path}")
    print(f"输出: {output_dir}")
    print()

    # ── Step 1: 读取 Excel ──
    print("Step 1: 读取制品包 Excel...")
    raw = read_excel(str(input_path))
    rules = raw["rules"]

    if not rules:
        # 详细诊断
        print("错误: 未找到有效的 RULE 行", file=sys.stderr)
        print("", file=sys.stderr)
        print("诊断信息:", file=sys.stderr)

        # 检查 RULE sheet 是否存在
        import openpyxl
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
                print(f"  - ✗ 找不到 '规则类型' 列！表头: {diag_headers[:10]}", file=sys.stderr)
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
                print(f"  - ✗ 找不到 '查询语句' 列！表头含'查询': {[h for h in diag_headers if '查询' in h or 'sql' in h.lower()]}", file=sys.stderr)
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
                    print(f"  - ⚠ {empty_sql} 行的 SQL 为空", file=sys.stderr)

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

    # ── Step 3: SQL 解析（分层：SELECT类深度解析，其他记录） ──
    print("Step 3: 解析 SQL...")
    parsed_map = {}
    for rule in rules:
        if rule.rule_type in SELECT_RULE_TYPES and rule.query_sql:
            # SELECT 类规则：完整解析
            parsed = parse_single_sql(rule.query_sql, dialect)
            parsed_map[rule.rule_code] = parsed
            if parsed.parse_error:
                print(f"  [!] {rule.rule_code}: {parsed.parse_error}")
            else:
                print(f"  {rule.rule_code} [{RULE_TYPE_MAP.get(rule.rule_type, '?')}]: "
                      f"{len(parsed.select_columns)} 列, "
                      f"{len(parsed.source_tables)} 表, {len(parsed.ctes)} CTE")
        elif rule.query_sql:
            # 非 SELECT 类但有 SQL（删数/分区交换等）：记录但不深度解析
            parsed_map[rule.rule_code] = ParsedSQL(raw_sql=rule.query_sql)
            print(f"  {rule.rule_code} [{RULE_TYPE_MAP.get(rule.rule_type, '?')}]: "
                  f"记录操作（不深度解析）")
        else:
            parsed_map[rule.rule_code] = ParsedSQL(parse_error="空 SQL")
    print()

    # ── Step 4: 拓扑构建 ──
    print("Step 4: 构建拓扑...")
    topology = build_topology(rules, parsed_map)
    print(f"  调度层级: {len(topology['schedule_plan'])}")
    print(f"  数据依赖: {len(topology['data_dependencies'])}")
    print(f"  自引用: {len(topology['self_references'])}")
    print(f"  场景数: {len(topology.get('scenarios', []))}")
    for sc in topology.get("scenarios", []):
        print(f"    {sc['name']}: {sc['rule_count']} 个规则")
    print()

    # ── Step 5: 数据流 ──
    print("Step 5: 构建数据流...")
    data_flow = build_data_flow(rules, parsed_map)
    print(f"  涉及表: {len(data_flow['tables'])}")
    print()

    # ── Step 5b: 字段映射 ──
    print("Step 5b: 构建字段映射（双源交叉）...")
    field_mappings = build_field_mappings(rules, parsed_map, raw["target_fields"])
    stats = field_mappings["statistics"]
    print(f"  SQL 列: {stats['total_in_sql']}")
    print(f"  TargetFields: {stats['total_in_excel']}")
    print(f"  精确匹配: {stats['match_count']}")
    print(f"  仅 SQL: {len(stats['only_in_sql'])}")
    print(f"  仅 Excel: {len(stats['only_in_excel'])}")
    print()

    # ── Step 6: 质量分析 ──
    print("Step 6: 质量分析...")
    quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)
    q_stats = quality["issue_statistics"]
    print(f"  问题: {len(quality['issues'])} "
          f"(critical={q_stats['critical']}, medium={q_stats['medium']}, "
          f"low={q_stats['low']}, info={q_stats['info']})")
    print()

    # ── Step 7: 组装输出 ──
    print("Step 7: 组装 knowledge_draft.json...")

    # 找目标表名（取第一个规则的目标表）
    first_rule = rules[0]
    target_name = first_rule.target_table or "unknown"

    # 加工模式标签自动检测
    patterns = detect_patterns(parsed_map, topology)
    print(f"  加工模式标签: {[p['label'] for p in patterns]}")

    # DDL 字段元数据（类型+中文名，可选）
    target_metadata = {}
    if args.ddl_dir:
        target_metadata = parse_ddl_for_metadata(args.ddl_dir, target_name)
        print(f"  DDL 字段元数据: {len(target_metadata)} 个字段")

    # ── 生成兜底 step_descriptions（脚本自动，不依赖 AI）──
    scenarios = topology.get("scenarios", [])
    auto_step_desc = []
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code, ParsedSQL())
        desc = generate_step_description(rule, parsed, scenarios, rules)
        # 找 step_id
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        step_id = step["step_id"] if step else ""
        auto_step_desc.append({
            "step_id": step_id,
            "rule_code": rule.rule_code,
            "purpose": desc["purpose"],
            "logic": desc["logic"],
            "is_auto_generated": True,  # 标记为自动生成，AI 可以覆盖
        })

    knowledge = {
        "meta": {
            "source_type": "execution_tasks.xlsx",
            "source_file": input_path.name,
            "analysis_time": datetime.now().isoformat(),
            "dialect": dialect,
            "rule_group_code": raw["rule_group_code"],
            "total_rules": len(rules),
            "total_target_fields": sum(len(v) for v in raw["target_fields"].values()),
            "total_sql_columns": sum(
                len(parsed_map.get(r.rule_code, ParsedSQL()).select_columns)
                for r in rules
            ),
            "target_table": target_name,
            "version": "1.0.0",
            "patterns": patterns,
            "target_field_types": {k: v["type"] for k, v in target_metadata.items() if v.get("type")},
            "target_field_comments": {k: v["comment"] for k, v in target_metadata.items() if v.get("comment")},
        },
        "topology": topology,
        "data_flow": data_flow,
        "field_mappings": field_mappings,
        "quality": quality,
        "business_logic": {
            "summary": "",
            "step_descriptions": auto_step_desc,
            "key_transforms": [],
        },
        "source": build_source(rules, raw["target_fields"], raw["group_variables"], parsed_map),
    }

    # 写入文件
    output_file = output_dir / "knowledge_draft.json"
    output_file.write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 生成 AI 增强用摘要 ──
    summary_file = output_dir / "knowledge_summary.md"
    summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality)
    summary_file.write_text(summary_text, encoding="utf-8")

    print(f"\n═══ 完成 ═══")
    print(f"输出: {output_file}")
    print(f"摘要: {summary_file}")
    print(f"目标表: {target_name}")
    print(f"步骤数: {len(rules)}")
    print(f"字段数: {stats['total_in_sql']}")
    print(f"问题数: {len(quality['issues'])}")
    print(f"\n下一步: AI 读 knowledge_summary.md，输出自然语言补充，保存为 knowledge_ai.md")
    print(f"        然后: python run.py view_generator --input knowledge_draft.json --ai-input knowledge_ai.md ...")


if __name__ == "__main__":
    main()
