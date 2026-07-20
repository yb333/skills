#!/usr/bin/env python3
"""理解引擎层（engine）— SQL 理解与血缘解析的单一真相。

三层架构（详见 architecture.md）：
    ① 数据层（analyzer.py）— read_excel / CLI
    ② 理解引擎（engine.py）— 本模块
    ③ 任务层 — 文档化 / 字段检索 / 关联影响分析 / ...

引擎职责（确定性解析，无 AI）：
    - analyze_pipeline()   执行规则 → knowledge（过程视角）
    - 数据类                ParsedSQL / RawRule / TableRef ...（领域模型）
    - build_* / enrich_*   拓扑 / 数据流 / 字段映射 / 血缘 / 物理穿透

引擎边界（铁律）：
    - 纯函数：无 print、不读 args、不写文件
    - 只懂单资产：接收一个规则组的数据，不关心批量编排
    - 确定性：血缘解析靠代码精确计算，不依赖 AI
    - 单向依赖：engine 不 import analyzer（analyzer import engine）
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    pass  # 引擎调用方（analyzer.py main）已有依赖检查，这里不重复

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
# 行注释：-- 到行尾（-- 后至少一个空格或直接到行尾，兼容 --xxx 和 -- xxx）
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# 块注释：/* */（非贪婪，跨行）
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

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
    rule_group_en: str = ""  # 规则组英文名称（每行，供批量按组命名目录）
    exchange_source_table: str = ""
    is_view_step: bool = False  # I视图封装步骤标记（追加的视图步骤=True，正常规则=False）


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
    join_type: str = ""  # FROM / LEFT / INNER / FULL / CROSS / FROM_SUBQUERY / JOIN_SUBQUERY_INNER
    join_condition: str = ""
    subquery_sql: str = ""  # 子查询的完整 SQL（仅子查询类型）
    subquery_tables: list = field(default_factory=list)
    subquery_role: str = ""  # 主表(FROM) 或 从表(JOIN)


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
    clean_sql: str = ""  # 预处理后的 SQL（strip + replace_placeholders），供 build_data_blocks 复用
    parse_error: str = ""
    has_star: bool = False  # SELECT * 或 t.* 检测
    # UNION/集合操作：每个分支独立记录（分支=步骤内场景）
    # 每个 branch: {"branch_index": 1, "op": "UNION ALL",
    #               "source_tables": [ParsedJoin...], "columns": [ParsedColumn...],
    #               "alias_table_map": {ALIAS(UPPER): physical_table}}
    union_branches: list = field(default_factory=list)
    # 字段使用信息（关联/过滤/分组）
    join_usage: list = field(default_factory=list)
    where_usage: list = field(default_factory=list)
    groupby_usage: list = field(default_factory=list)
    join_paths: dict = field(default_factory=dict)  # {alias: {table, is_primary, path, subquery_*}}


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


_SYSDATE_PATTERN = re.compile(r"\bSYSDATE\s*\(\s*\)", re.IGNORECASE)
_SYSDATE_NOPAREN_PATTERN = re.compile(r"\bSYSDATE\b(?!\s*\()", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """剔除 SQL 注释（行注释 -- 和块注释 /* */）。

    必须在按分号分割语句之前调用——注释里的分号会导致 split(";")
    错误截断 SQL，使截断点之后的 FROM/JOIN 表名全部丢失。

    注意：字符串字面量内的 -- 或 /* 不应被误删，但 SQL 查询里
    字符串包含注释语法的场景极少，MVP 不做字符串感知的剔除。
    """
    # 先剔块注释（可能跨行），再剔行注释
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


def _strip_dws_clauses(sql: str) -> str:
    """移除 DWS 特有语法，供 sqlglot 解析"""
    sql = _DIST_BY_PATTERN.sub("", sql)
    sql = _TO_GROUP_PATTERN.sub("", sql)
    sql = _PARTITION_PATTERN.sub("", sql)
    sql = _WITH_PARAMS_PATTERN.sub(")", sql)
    # sysdate() / sysdate → CURRENT_TIMESTAMP（sqlglot oracle 方言不兼容）
    sql = _SYSDATE_PATTERN.sub("CURRENT_TIMESTAMP", sql)
    sql = _SYSDATE_NOPAREN_PATTERN.sub("CURRENT_TIMESTAMP", sql)
    return sql


def _replace_placeholders(sql: str) -> str:
    """替换平台变量占位符为合法 SQL 值（保留原始变量名）

    支持两种语法:
    1. ${XXX}        → '${XXX}'（字符串字面量，保留变量名）
    2. #var = value# → '#var = value#'（字符串字面量）

    处理 ${XXX} 已在引号内的情况（'${XXX}' → '${XXX}'，不双层引号）
    """
    # 先去掉 '${...}' 的外层引号，避免双层引号
    sql = re.sub(r"'\$\{([^}]+)\}'", r"${\1}", sql)
    sql = re.sub(r"'#[^#]*#'", lambda m: m.group(0)[1:-1], sql)  # '#...#' 去引号
    # 再统一加引号
    sql = _PARAM_PLACEHOLDER.sub(lambda m: "'" + m.group(0) + "'", sql)
    sql = _PLATFORM_VAR_PATTERN.sub(lambda m: "'" + m.group(0) + "'", sql)
    return sql


def _normalize_table_name(schema: str, table: str) -> str:
    """标准化表名为 schema.table（统一小写）"""
    s = _clean_name(schema).lower()
    t = _clean_name(table).lower()
    if s:
        return f"{s}.{t}"
    return t


def _norm_table(name: str) -> str:
    """表名归一化（统一小写，去空格）。所有表名比较都必须走这个函数。"""
    if not name:
        return ""
    return name.strip().lower()


# 临时表/中间表判断的正则（匹配 tmp/temp 开头，或含 _tmp/_temp + 数字/分隔符）
_INTERMEDIATE_TABLE_PATTERN = re.compile(
    r"(?:^tmp\d*$|_tmp\d*$|^temp\d*$|_temp\d*$|^tmp_|_tmp_|^temp_|_temp_)",
    re.IGNORECASE,
)


def _is_intermediate_table(table_name: str) -> bool:
    """判断表名是否为临时表/中间表（需要穿透追溯的）。

    覆盖命名格式：
    - tmp开头: tmp, tmp1, tmp_xxx
    - temp开头: temp, temp1, temp_xxx
    - tmp/temp后缀: xxxx_xxxx_tmp1, xxxx_temp2（实际生产常见格式）
    - 带分隔符: xxxx_tmp_xxx, xxxx_temp_xxx

    所有临时表判断都必须走这个函数，禁止直接 startswith。
    """
    if not table_name:
        return False
    # 去掉 schema 前缀，只判断表名部分
    short = _norm_table(table_name).split(".")[-1]
    return bool(_INTERMEDIATE_TABLE_PATTERN.search(short))


def _table_match(name1: str, name2: str) -> bool:
    """表名匹配（大小写不敏感）。所有表名比较都用这个函数，禁止直接 == 或 in。

    支持:
    - 完整名匹配: 'dws.tbl' == 'DWS.TBL' → True
    - 短名匹配:   'dws.tbl' == 'TBL'     → True（右操作数是短名时）
    """
    n1 = _norm_table(name1)
    n2 = _norm_table(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    # 短名匹配（去掉 schema 前缀比较）
    n1_short = n1.split(".")[-1]
    n2_short = n2.split(".")[-1]
    return n1_short == n2_short


def _infer_layer(schema: str, table: str) -> str:
    """推断数仓层级"""
    combined = f"{schema}.{table}".lower()
    for pattern, layer in LAYER_PATTERNS:
        if re.search(pattern, combined):
            return layer
    return "UNKNOWN"

# ═══════════════════════════════════════════════════════════════
# Step 2: detect_dialect()
# ═══════════════════════════════════════════════════════════════

def detect_dialect(sql_texts: list[str]) -> str:
    """自动检测 SQL 方言"""
    # 过滤 None/空字符串，避免 join 崩溃
    safe_texts = [str(t) for t in (sql_texts or []) if t]
    combined = " ".join(safe_texts).upper()
    oracle_score = sum(1 for sign in ORACLE_SIGNS if sign.upper() in combined)
    dws_score = sum(1 for sign in DWS_SIGNS if sign.upper() in combined)

    if oracle_score > dws_score:
        return "oracle"
    return "dws"


# ═══════════════════════════════════════════════════════════════
# Step 3: parse_single_sql() — sqlglot AST
# ═══════════════════════════════════════════════════════════════
# 递归解析核心（重构）：统一处理 SELECT / UNION / CTE / 子查询的任意嵌套组合
# ═══════════════════════════════════════════════════════════════


@dataclass
class TableRef:
    """表引用（FROM/JOIN 的表）。"""
    table: str = ""
    alias: str = ""
    join_type: str = ""       # FROM / LEFT JOIN / INNER JOIN / ...
    on_condition: str = ""    # ON 条件


@dataclass
class ColumnRef:
    """列引用。"""
    alias: str = ""
    expression: str = ""
    source_fields: list = None  # [{alias, field}]
    transform_type: str = "direct"

    def __post_init__(self):
        if self.source_fields is None:
            self.source_fields = []


@dataclass
class QueryUnit:
    """一个 SQL 查询单元（递归解析的核心结构）。

    支持三种类型：
    - "select": 普通 SELECT（含 FROM 子查询 / JOIN 子查询 / CTE）
    - "union": UNION/INTERSECT/EXCEPT（含 branches）
    - "cte": CTE 定义（含 cte_name + cte_body）
    """
    type: str = "select"

    # SELECT 类型
    tables: list = None          # [TableRef] FROM 主表 + JOIN 从表
    columns: list = None         # [ColumnRef]
    where: str = ""
    group_by: list = None
    from_subquery: object = None  # QueryUnit（FROM 子查询，如果有）
    join_subqueries: list = None # [{alias, on_condition, body: QueryUnit}]
    cte_defs: list = None        # [{name, body: QueryUnit}] 该 SELECT 里定义的 CTE

    # UNION 类型
    branches: list = None        # [QueryUnit] UNION 分支

    # CTE 类型
    cte_name: str = ""
    cte_body: object = None      # QueryUnit

    # 通用
    depth: int = 0               # 嵌套深度

    def __post_init__(self):
        if self.tables is None:
            self.tables = []
        if self.columns is None:
            self.columns = []
        if self.group_by is None:
            self.group_by = []
        if self.join_subqueries is None:
            self.join_subqueries = []
        if self.cte_defs is None:
            self.cte_defs = []
        if self.branches is None:
            self.branches = []


def parse_query_unit(node, dialect="oracle", depth=0, comment_alias_map=None):
    """递归解析任意 AST 节点为 QueryUnit。

    不管节点在顶层、子查询里、CTE 里、还是 UNION 分支里，统一处理。
    """
    if comment_alias_map is None:
        comment_alias_map = {}

    # UNION / INTERSECT / EXCEPT
    if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        branches_raw = []
        _collect_set_branches(node, branches_raw)
        unit = QueryUnit(type="union", depth=depth)
        for b in branches_raw:
            if isinstance(b, exp.Select):
                unit.branches.append(parse_query_unit(b, dialect, depth + 1, comment_alias_map))
        return unit

    # SELECT
    if isinstance(node, exp.Select):
        unit = _parse_select_to_unit(node, node, dialect, depth, comment_alias_map)
        # CTE 定义可能在 WITH 节点上（sqlglot 用 "with_" 键）
        with_node = node.args.get("with") or node.args.get("with_")
        if with_node:
            for cte_expr in with_node.expressions:
                cte_name = (cte_expr.alias or "").lower()
                cte_inner = cte_expr.this
                cte_unit = parse_query_unit(cte_inner, dialect, depth + 1, comment_alias_map)
                cte_unit.cte_name = cte_name
                cte_unit.type = "cte_def"
                unit.cte_defs.append({"name": cte_name, "body": cte_unit})
        return unit

    # 有 WITH 的顶层（tree 本身可能是 With + Select）
    # 这种情况在 parse_single_sql 里已处理（tree.find(exp.Select)）
    return QueryUnit(type="select", depth=depth)


def _enhance_with_query_unit(parsed_sql, tree, dialect, comment_alias_map):
    """用 QueryUnit 递归解析补充 ParsedSQL 的盲区。

    补充内容：
    - UNION 在子查询内部时的 union_branches
    - CTE 内部的 JOIN/WHERE 条件（collect_all_usage 递归覆盖）
    - 顶层 UNION 分支的 JOIN/WHERE（collect_all_usage 覆盖）
    - 所有层级的物理表（collect_all_tables 递归覆盖）
    """
    try:
        unit = parse_query_unit(tree, dialect, 0, comment_alias_map)

        # 补充 UNION 分支（FROM 子查询内部 UNION 的情况）
        if not parsed_sql.union_branches:
            if unit.type == "select" and unit.from_subquery:
                sub = unit.from_subquery
                if sub.type == "union":
                    for idx, b in enumerate(sub.branches):
                        all_tables = collect_all_tables(b)
                        parsed_sql.union_branches.append({
                            "branch_index": idx + 1,
                            "source_tables": all_tables,
                            "columns": [],  # 列由原有逻辑处理
                        })

        # 补充 JOIN/WHERE（递归所有层级，去重）
        all_join, all_where = collect_all_usage(unit)

        # 合并到 parsed_sql（去重）
        existing_join_keys = {(j["field"], j.get("on_condition", "")) for j in parsed_sql.join_usage}
        for j in all_join:
            key = (j["field"], j.get("on_condition", ""))
            if key not in existing_join_keys:
                parsed_sql.join_usage.append(j)
                existing_join_keys.add(key)

        existing_where_keys = {(w["field"], w.get("condition", "")) for w in parsed_sql.where_usage}
        for w in all_where:
            key = (w["field"], w.get("condition", ""))
            if key not in existing_where_keys:
                parsed_sql.where_usage.append(w)
                existing_where_keys.add(key)

        # 补充 source_tables（递归收集所有物理表）
        existing_tables = {j.source_table.lower() for j in parsed_sql.source_tables}
        all_tables = collect_all_tables(unit)
        # CTE 名集合（CTE 名不是物理表，不加入）
        cte_names = set()
        if unit.type == "select":
            for cte_def in unit.cte_defs:
                cte_names.add(cte_def["name"])
        for t in all_tables:
            if t.table and t.table.lower() not in existing_tables and t.table.lower() not in cte_names:
                from analyzer import ParsedJoin
                parsed_sql.source_tables.append(ParsedJoin(
                    source_table=t.table, alias=t.alias,
                    join_type=t.join_type if t.join_type != "FROM" else "FROM_SUBQUERY_MAIN",
                    join_condition=t.on_condition,
                ))
                existing_tables.add(t.table.lower())

    except Exception:
        pass  # QueryUnit 补充失败不影响原有解析结果


def _parse_select_to_unit(tree, select_node, dialect, depth, comment_alias_map):
    """解析 SELECT 节点为 QueryUnit。"""
    unit = QueryUnit(type="select", depth=depth)

    # FROM
    from_clause = select_node.args.get("from_")
    if from_clause:
        main_expr = from_clause.this
        if isinstance(main_expr, exp.Table):
            unit.tables.append(TableRef(
                table=".".join(_clean_name(p.name) for p in main_expr.parts),
                alias=(main_expr.alias or "").lower(),
                join_type="FROM",
            ))
        elif isinstance(main_expr, exp.Subquery):
            inner = main_expr.this
            sub_unit = parse_query_unit(inner, dialect, depth + 1, comment_alias_map)
            sub_unit.cte_name = (main_expr.alias or "").lower()  # 复用 cte_name 存子查询别名
            unit.from_subquery = sub_unit

    # JOIN
    for jn in select_node.args.get("joins", []):
        jt_node = jn.this
        on_expr = jn.args.get("on")
        on_sql = on_expr.sql(dialect=dialect) if on_expr else ""
        join_kind = (jn.args.get("kind") or jn.args.get("side") or "JOIN").strip()
        join_type = f"{join_kind} JOIN" if join_kind and join_kind != "JOIN" else "INNER JOIN"

        if isinstance(jt_node, exp.Table):
            unit.tables.append(TableRef(
                table=".".join(_clean_name(p.name) for p in jt_node.parts),
                alias=(jt_node.alias or "").lower(),
                join_type=join_type,
                on_condition=on_sql,
            ))
        elif isinstance(jt_node, exp.Subquery):
            inner = jt_node.this
            sub_unit = parse_query_unit(inner, dialect, depth + 1, comment_alias_map)
            sub_unit.cte_name = (jt_node.alias or "").lower()
            unit.join_subqueries.append({
                "alias": (jt_node.alias or "").lower(),
                "on_condition": on_sql,
                "join_type": join_type,
                "body": sub_unit,
            })

    # WHERE
    where_node = select_node.args.get("where")
    if where_node:
        unit.where = where_node.sql(dialect=dialect).replace("WHERE ", "")

    # GROUP BY
    group_node = select_node.args.get("group")
    if group_node:
        unit.group_by = [g.sql(dialect=dialect) for g in group_node.expressions]

    # CTE 定义（WITH ... AS (...)）
    with_node = tree.args.get("with") if hasattr(tree, "args") else None
    if with_node:
        for cte_expr in with_node.expressions:
            cte_name = cte_expr.alias or ""
            cte_inner = cte_expr.this  # CTE 内部的 SELECT 或 UNION
            cte_unit = parse_query_unit(cte_inner, dialect, depth + 1, comment_alias_map)
            cte_unit.cte_name = cte_name.lower()
            cte_unit.type = "cte_def"
            unit.cte_defs.append({"name": cte_name.lower(), "body": cte_unit})

    # SELECT 列
    for i, proj in enumerate(select_node.expressions):
        col = _parse_column_ref(proj, i, comment_alias_map)
        unit.columns.append(col)

    return unit


def _parse_column_ref(proj, position, comment_alias_map):
    """解析 SELECT 投影列为 ColumnRef。"""
    if isinstance(proj, exp.Alias):
        alias = _clean_name(proj.alias).lower()
        inner = proj.this
    elif isinstance(proj, exp.Column):
        alias = _clean_name(proj.name).lower()
        inner = proj
    else:
        # 字面量/表达式
        alias = comment_alias_map.get(position, f"_col_{position}")
        if isinstance(proj, exp.Alias):
            alias = _clean_name(proj.alias).lower()
            inner = proj.this
        else:
            inner = proj

    col = ColumnRef(alias=alias, expression=inner.sql(dialect="oracle"))
    col.transform_type = classify_transform(inner, inner.sql(dialect="oracle"))

    # source_fields
    if isinstance(inner, exp.Column):
        col.source_fields = [{
            "alias": _clean_name(inner.table).lower() if inner.table else "",
            "field": _clean_name(inner.name).lower(),
        }]
    else:
        # 表达式：提取引用的列
        for c in inner.find_all(exp.Column):
            col.source_fields.append({
                "alias": _clean_name(c.table).lower() if c.table else "",
                "field": _clean_name(c.name).lower(),
            })

    return col


def collect_all_tables(unit, exclude_cte_names=None):
    """递归收集 QueryUnit 里所有物理表（含子查询/CTE/UNION 分支内部）。

    Returns: [TableRef, ...]
    """
    if exclude_cte_names is None:
        exclude_cte_names = set()

    result = []
    if unit.type == "select":
        for t in unit.tables:
            if t.table.lower() not in exclude_cte_names:
                result.append(t)
        if unit.from_subquery:
            result.extend(collect_all_tables(unit.from_subquery, exclude_cte_names))
        for jsq in unit.join_subqueries:
            result.extend(collect_all_tables(jsq["body"], exclude_cte_names))
        for cte_def in unit.cte_defs:
            result.extend(collect_all_tables(cte_def["body"], exclude_cte_names))
    elif unit.type == "union":
        for b in unit.branches:
            result.extend(collect_all_tables(b, exclude_cte_names))
    elif unit.type in ("cte", "cte_def"):
        if unit.cte_body:
            result.extend(collect_all_tables(unit.cte_body, exclude_cte_names))

    return result


def collect_all_usage(unit, depth=0):
    """递归收集 QueryUnit 里所有 JOIN ON 和 WHERE 条件。

    Returns: (join_usage, where_usage)
    """
    join_usage = []
    where_usage = []

    if unit.type in ("select", "cte_def", "cte"):
        # JOIN 条件
        for t in unit.tables:
            if t.on_condition:
                # 提取 ON 条件里的字段
                for m in re.finditer(r'(\w+)\.(\w+)', t.on_condition):
                    field = m.group(2).lower()
                    join_usage.append({"field": field, "alias": m.group(1).lower(),
                                       "join_type": t.join_type, "on_condition": t.on_condition})

        # WHERE 条件
        if unit.where:
            for m in re.finditer(r'(\w+)\.(\w+)', unit.where):
                field = m.group(2).lower()
                where_usage.append({"field": field, "alias": m.group(1).lower(),
                                    "condition": unit.where})

        # 递归子查询
        if unit.from_subquery:
            j, w = collect_all_usage(unit.from_subquery, depth + 1)
            join_usage.extend(j)
            where_usage.extend(w)
        for jsq in unit.join_subqueries:
            j, w = collect_all_usage(jsq["body"], depth + 1)
            join_usage.extend(j)
            where_usage.extend(w)
        for cte_def in unit.cte_defs:
            j, w = collect_all_usage(cte_def["body"], depth + 1)
            join_usage.extend(j)
            where_usage.extend(w)
        for cte_def in unit.cte_defs:
            j, w = collect_all_usage(cte_def["body"], depth + 1)
            join_usage.extend(j)
            where_usage.extend(w)

    elif unit.type == "union":
        for b in unit.branches:
            j, w = collect_all_usage(b, depth + 1)
            join_usage.extend(j)
            where_usage.extend(w)

    return join_usage, where_usage


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

    # 预处理：先剔注释（必须在分号分割之前，否则注释里的分号会截断 SQL）
    # 再清理 DWS 语法 + 替换占位符
    clean = _strip_sql_comments(sql)
    clean = _strip_dws_clauses(clean)
    clean = _replace_placeholders(clean)
    clean = clean.strip().rstrip(";").strip()

    # 防御：如果 SQL 含多条语句（DDL 文件被误传，含 COMMENT/CREATE 等），
    # 只保留 SELECT/WITH 语句。COMMENT ON COLUMN 的双引号会导致 sqlglot
    # ParseError，必须在解析前去掉。
    if ";" in clean:
        stmts = [s.strip() for s in clean.split(";") if s.strip()]
        select_stmts = [s for s in stmts
                        if s.upper().startswith(("SELECT", "WITH"))]
        if select_stmts:
            clean = "; ".join(select_stmts)
        elif stmts and not stmts[0].upper().startswith(("SELECT", "WITH")):
            # 整个 SQL 不是 SELECT（如纯 DDL），标记错误
            result.parse_error = "非 SELECT/WITH 语句（DDL？）"
            return result

    # 在清理注释之前，先提取 SQL 注释中的字段名映射
    # 用于给无别名的列（审计字段等）赋予正确名称
    comment_alias_map = _extract_comment_aliases(sql)

    if not clean:
        result.parse_error = "空 SQL"
        return result

    # sqlglot 解析（固定用 oracle 方言）。
    # DWS/GaussDB 兼容 Oracle 语法，用 oracle 方言能保持 NVL/DECODE/SUBSTR 等函数
    # 原样解析，不被转换为 COALESCE/CASE。dialect 参数当前仅用于 meta 记录，
    # 不影响实际解析（如需切换方言，修改此处 sqlglot_dialect）。
    sqlglot_dialect = "oracle"
    # 整个解析流程统一兜底：制品包 SQL 质量不可控，深度嵌套可能触发
    # RecursionError，异常 AST 可能触发 AttributeError 等。任何异常都
    # 不应让 analyzer 主循环中断（一条坏 SQL 不该拖垮整个制品包分析），
    # 一律降级为带 parse_error 的 ParsedSQL，让该规则标记失败后继续。
    try:
        tree = sqlglot.parse_one(clean, dialect=sqlglot_dialect)
        result.clean_sql = clean  # 存预处理后的 SQL，供 build_data_blocks 复用（不重新 strip/replace）
        # SELECT * / t.* 检测
        if tree.find(exp.Star) is not None:
            result.has_star = True
    except Exception as e:
        result.parse_error = f"{type(e).__name__}: {e}"
        print(f"  [SQL解析错误] {e}", file=sys.stderr)
        return result

    try:
        # ── 检测 UNION/INTERSECT/EXCEPT（SetOperation）──
        if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
            r = _parse_set_operation(tree, sqlglot_dialect, comment_alias_map, sql)
            r.has_star = result.has_star
            r.clean_sql = clean  # 传递 clean_sql
            # 用 QueryUnit 递归补充 JOIN/WHERE（顶层 UNION 分支的遗漏）
            _enhance_with_query_unit(r, tree, sqlglot_dialect, comment_alias_map)
            return r

        # ── 普通 SELECT / WITH...SELECT ──
        select_node = tree.find(exp.Select)
        if not select_node:
            result.parse_error = "未找到 SELECT 节点"
            return result

        r = _parse_select(tree, select_node, sqlglot_dialect, comment_alias_map, sql)
        r.has_star = result.has_star
        r.clean_sql = clean  # 传递 clean_sql
        # 用 QueryUnit 递归补充（CTE 内部结构、UNION 分支等遗漏）
        _enhance_with_query_unit(r, tree, sqlglot_dialect, comment_alias_map)
        return r
    except RecursionError:
        result.parse_error = "RecursionError: SQL 嵌套层级过深"
        print(f"  [SQL解析错误] 嵌套层级过深，已跳过", file=sys.stderr)
        return result
    except Exception as e:
        result.parse_error = f"{type(e).__name__}: {e}"
        print(f"  [SQL解析错误] {e}", file=sys.stderr)
        return result


def _parse_select(tree, select_node, sqlglot_dialect: str, comment_alias_map: dict, raw_sql: str) -> ParsedSQL:
    """解析普通 SELECT 语句（非 UNION）。"""
    result = ParsedSQL(raw_sql=raw_sql)

    # ── 提取源表和 JOIN ──
    result.source_tables = _extract_joins(tree, select_node)

    # ── 检测 FROM 子查询内部是否为 UNION（常见场景：FROM (SELECT... UNION SELECT...) t）──
    from_clause = select_node.args.get("from_")
    if from_clause and isinstance(from_clause.this, exp.Subquery):
        inner_node = from_clause.this.this  # Subquery 内部的节点
        if isinstance(inner_node, (exp.Union, exp.Intersect, exp.Except)):
            # FROM 子查询内部是 UNION —— 解析 UNION 分支
            union_result = _parse_set_operation(inner_node, sqlglot_dialect, comment_alias_map, inner_node.sql(dialect=sqlglot_dialect))
            result.union_branches = union_result.union_branches
            # UNION 内部的 JOIN/WHERE 也要提取（子查询内部关联和过滤）
            inner_join_usage, inner_where_usage, _ = _extract_field_usage(
                inner_node, inner_node, [], sqlglot_dialect)
            # 这些 JOIN/WHERE 已经被 _extract_subquery_usage 覆盖，不重复

    # ── 提取 SELECT 列 ──
    result.select_columns = _extract_select_columns(select_node, comment_alias_map, result.source_tables)

    # ── FROM 子查询字段穿透（递归到内层找物理来源）──
    _penetrate_subquery_columns(tree, result.select_columns)

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

    # ── 字段使用信息（JOIN ON / WHERE / GROUP BY）──
    result.join_usage, result.where_usage, result.groupby_usage = _extract_field_usage(
        tree, select_node, result.source_tables, sqlglot_dialect
    )

    # ── JOIN 桥接链（每个表到主表的完整关联路径）──
    result.join_paths = _build_join_paths(result.source_tables)

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

    # 合并所有分支的 source_tables（保留原有行为，兼容下游）
    all_joins = []
    # 同时记录每个分支独立的 source_tables + columns（分支=步骤内场景）
    union_branches = []
    for idx, branch in enumerate(branches):
        if not isinstance(branch, exp.Select):
            continue
        branch_joins = _extract_joins(branch, branch)
        all_joins.extend(branch_joins)
        branch_columns = _extract_select_columns(branch, comment_alias_map)
        # 为每个分支构建 join_paths（主表→从表关联路径），避免 UNION 场景丢失关联信息
        branch_join_paths = _build_join_paths(branch_joins)
        union_branches.append({
            "branch_index": idx + 1,
            "source_tables": branch_joins,
            "columns": branch_columns,
            "join_paths": branch_join_paths,
        })

    # 去重（同名同别名）
    seen = set()
    deduped = []
    for j in all_joins:
        key = (j.source_table, j.alias)
        if key not in seen:
            seen.add(key)
            deduped.append(j)
    result.source_tables = deduped
    result.union_branches = union_branches
    # result.join_paths 取第一分支的（与 select_columns 取第一分支保持一致口径）
    if union_branches:
        result.join_paths = dict(union_branches[0]["join_paths"])

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
    cte_alias_map = {}
    if result.select_columns and result.ctes:
        if isinstance(first_branch, exp.Select):
            cte_alias_map = _build_cte_alias_map(first_branch, result.ctes)
    _apply_cte_penetration(result.select_columns, result.ctes, cte_alias_map)

    # 对每个 UNION 分支的 columns 也做 CTE 穿透（分支可能直接引用 CTE）
    if result.ctes:
        for idx, ub in enumerate(union_branches):
            branch_node = branches[idx] if idx < len(branches) else None
            if isinstance(branch_node, exp.Select):
                b_cte_alias_map = _build_cte_alias_map(branch_node, result.ctes)
            else:
                b_cte_alias_map = cte_alias_map
            _apply_cte_penetration(ub["columns"], result.ctes, b_cte_alias_map)

    # 分支字段穿透：把每个分支的 columns 解析到物理表字段
    # （子查询别名 t1.order_id → 物理表 orders_a.order_id）
    _resolve_branch_columns_physical(union_branches)

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


def _resolve_branch_columns_physical(union_branches: list) -> None:
    """给每个 UNION 分支的 columns 补上物理表字段来源（穿透子查询别名）。

    对每个分支：
    1. 从 source_tables 建 alias→物理表 映射（跳过子查询占位）
    2. 对子查询占位项，解析其 SQL 建立 子查询输出字段→(内部物理表,内部字段) 映射
    3. 每个 column 的 expr 形如 "t1.order_id"，解析 alias.field：
       - alias 是子查询占位 → 用子查询字段映射穿透到内部物理表
       - alias 是普通物理表别名 → 直接映射
    结果写入 column.source_tables（物理表名列表）和 source_fields（物理来源详情）
    """
    for branch in union_branches:
        source_tables = branch["source_tables"]
        # 1. alias → 物理表 映射（非占位项）
        alias_to_table = {}
        subquery_placeholders = []  # 子查询占位 ParsedJoin
        for j in source_tables:
            if j.source_table.startswith("(subquery:"):
                subquery_placeholders.append(j)
            elif j.alias and j.source_table:
                alias_to_table[j.alias.upper()] = j.source_table

        # 2. 子查询输出字段 → 内部物理来源
        #    subquery_field_map: {子查询别名(UPPER): {输出字段(UPPER): (物理表, 内部字段)}}
        subquery_field_map = {}
        for sq in subquery_placeholders:
            sq_alias = (sq.alias or "sub").upper()
            if not sq.subquery_sql:
                continue
            try:
                sq_parsed = parse_single_sql(sq.subquery_sql, "oracle")
            except Exception:
                continue
            if sq_parsed.parse_error:
                continue
            # 子查询内部 alias → 物理表
            sq_inner_alias_map = {}
            for ij in sq_parsed.source_tables:
                if not ij.source_table.startswith("(subquery:") and ij.alias:
                    sq_inner_alias_map[ij.alias.upper()] = ij.source_table
            # 子查询输出列 → 物理来源
            field_map = {}
            for col in sq_parsed.select_columns:
                col_name = (col.alias or "").upper()
                if not col_name or not col.source_fields:
                    continue
                # 取第一个来源（子查询内字段通常单来源）
                sf = col.source_fields[0]
                sf_alias = (sf.get("alias", "") or "").upper()
                sf_field = sf.get("field", "")
                physical = sq_inner_alias_map.get(sf_alias, sf_alias)
                field_map[col_name] = (physical, sf_field)
            subquery_field_map[sq_alias] = field_map

        # 3. 解析每个 column 的物理来源
        for col in branch["columns"]:
            expr = col.expression or ""
            # 解析 alias.field（支持 t1.order_id 或 order_id 等简单形式）
            phys_sources = []
            for sf in col.source_fields:
                sf_alias = (sf.get("alias", "") or "").upper()
                sf_field = sf.get("field", "")
                if sf_alias in subquery_field_map:
                    # 子查询穿透：alias 用内部物理表的别名（join_paths 的 key 是内部别名）
                    fm = subquery_field_map[sf_alias]
                    key = (sf_field or col.alias or "").upper()
                    if key in fm:
                        # 查内部物理表的别名（用于 join_paths 桥接查找）
                        inner_table = fm[key][0]
                        inner_alias = next((a.upper() for a, t in alias_to_table.items()
                                            if _norm_table(t) == _norm_table(inner_table)), sf_alias)
                        phys_sources.append({"table": inner_table, "field": fm[key][1],
                                             "alias": inner_alias, "branch": branch["branch_index"]})
                        continue
                # 普通物理表别名：保留外层 alias（join_paths 的 key 就是这个别名）
                physical = alias_to_table.get(sf_alias, sf_alias)
                phys_sources.append({"table": physical, "field": sf_field,
                                     "alias": sf_alias, "branch": branch["branch_index"]})
            # 写回 column：source_fields 在 UNION 分支中被替换为物理穿透来源
            # （{table, field, branch} 结构，替代原有的别名层 {alias, field}）。
            # 这是设计意图：union_branches 的 columns 专供物理穿透，不再保留别名层。
            # 下游 build_data_flow 将其序列化为 physical_sources。
            col.source_tables = [p["table"] for p in phys_sources]
            if phys_sources:
                col.source_fields = phys_sources


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


def _extract_subquery_tables(subquery_node, depth=0) -> list[tuple[str, str, str]]:
    """递归提取子查询内部的所有物理表，并标记内部主从角色。

    支持嵌套子查询（子查询内部的子查询）。
    最大递归深度 10 层。

    Returns: [(table_name, alias, inner_role), ...]
        inner_role: "main"（内部 FROM 主表）或 "secondary"（内部 JOIN 从表）
    """
    if depth > 10:
        return []

    tables = []
    inner_select = subquery_node.find(exp.Select)
    if not inner_select:
        return tables

    # 内部 FROM 表（主表）
    inner_from = inner_select.args.get("from_")
    if inner_from:
        inner_main = inner_from.this
        if isinstance(inner_main, exp.Table):
            tname = ".".join(_clean_name(p.name) for p in inner_main.parts)
            alias = _clean_name(inner_main.alias).lower() if inner_main.alias else ""
            tables.append((tname, alias or tname.split(".")[-1], "main"))
        elif isinstance(inner_main, exp.Subquery):
            # 嵌套子查询：递归（嵌套层的主表在更深处，但相对本层也是主表）
            for tn, al, _ in _extract_subquery_tables(inner_main, depth + 1):
                tables.append((tn, al, "main"))

        # 内部逗号 JOIN（从表）
        for extra in inner_from.expressions:
            if isinstance(extra, exp.Table):
                tname = ".".join(_clean_name(p.name) for p in extra.parts)
                alias = _clean_name(extra.alias).lower() if extra.alias else ""
                tables.append((tname, alias or tname.split(".")[-1], "secondary"))

    # 内部 JOIN 表（从表）
    for join_node in inner_select.args.get("joins", []):
        jt = join_node.this
        if isinstance(jt, exp.Table):
            tname = ".".join(_clean_name(p.name) for p in jt.parts)
            alias = _clean_name(jt.alias).lower() if jt.alias else ""
            tables.append((tname, alias or tname.split(".")[-1], "secondary"))
        elif isinstance(jt, exp.Subquery):
            for tn, al, _ in _extract_subquery_tables(jt, depth + 1):
                tables.append((tn, al, "secondary"))

    return tables


def _extract_joins(tree, select_node) -> list[ParsedJoin]:
    """提取 FROM 和 JOIN 信息。

    ⚠️ 不递归进入 CTE / 子查询。只取主 SELECT 的直接 FROM 和 JOIN，
    避免把 CTE 内部的 JOIN 表误归入主查询的 source_tables。
    """
    joins = []
    sqlglot_dialect = "oracle"

    # 收集 CTE 名称（用于排除 CTE 别名）
    cte_names = set()
    with_clause = tree.args.get("with_")
    if with_clause:
        for cte_node in with_clause.expressions:
            cte_alias = cte_node.alias
            if cte_alias:
                cte_names.add(_clean_name(str(cte_alias)).upper())

    # 主表 (FROM) — 处理普通表和子查询
    from_clause = select_node.args.get("from_")
    if from_clause:
        main_expr = from_clause.this
        if isinstance(main_expr, exp.Table):
            table_name = ".".join(_clean_name(p.name) for p in main_expr.parts)
            # 过滤 CTE 名作为 FROM 表（CTE 内部表已追溯，不需要 CTE 节点）
            short_name = table_name.split(".")[-1] if "." in table_name else table_name
            if short_name.upper() in cte_names:
                pass  # CTE 不加入 source_tables，但其内部表会通过 _extract_ctes 加入
            else:
                alias = _clean_name(main_expr.alias).lower() if main_expr.alias else ""
                joins.append(ParsedJoin(
                    source_table=table_name,
                    alias=alias or table_name.split(".")[-1],
                    join_type="FROM",
                    join_condition="",
                ))
        elif isinstance(main_expr, exp.Subquery):
            # FROM 子查询：记录子查询别名 + 递归提取内部表
            sub_alias = _clean_name(main_expr.alias).lower() if main_expr.alias else ""
            sub_sql = main_expr.sql(dialect="oracle")
            sub_tables = _extract_subquery_tables(main_expr)
            # 子查询别名作为虚拟 FROM 表（让字段来源能匹配）
            joins.append(ParsedJoin(
                source_table=f"(subquery:{sub_alias})",
                alias=sub_alias or "sub",
                join_type="FROM",
                join_condition="",
                subquery_sql=sub_sql,
                subquery_tables=sub_tables,
                subquery_role="主表",
            ))
            # 递归提取子查询内部的物理表
            # 透传规则：子查询是主表 → 内部主表也是主表(FROM_SUBQUERY_MAIN)，内部从表是从表(FROM_SUBQUERY)
            for inner_table_name, inner_alias, inner_role in sub_tables:
                joins.append(ParsedJoin(
                    source_table=inner_table_name,
                    alias=inner_alias,
                    join_type="FROM_SUBQUERY_MAIN" if inner_role == "main" else "FROM_SUBQUERY",
                    join_condition="",
                ))

        # from_.expressions 是逗号 JOIN 的额外表（FROM a, b）
        for extra in from_clause.expressions:
            if isinstance(extra, exp.Table):
                table_name = ".".join(_clean_name(p.name) for p in extra.parts)
                alias = _clean_name(extra.alias).lower() if extra.alias else ""
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
        join_expr = join_node.this

        # JOIN 子查询：提取子查询别名 + 内部表
        if isinstance(join_expr, exp.Subquery):
            sub_alias = _clean_name(join_expr.alias).lower() if join_expr.alias else ""
            on_node = join_node.args.get("on")
            join_condition = on_node.sql(dialect="oracle") if on_node else ""
            sub_sql2 = join_expr.sql(dialect="oracle")
            sub_tables2 = _extract_subquery_tables(join_expr)
            joins.append(ParsedJoin(
                source_table=f"(subquery:{sub_alias})",
                alias=sub_alias or "sub",
                join_type="JOIN_SUBQUERY",
                join_condition=join_condition,
                subquery_sql=sub_sql2,
                subquery_tables=sub_tables2,
                subquery_role="从表",
            ))
            # 透传规则：子查询是从表 → 内部所有表都是从表(JOIN_SUBQUERY_INNER)
            for inner_table_name, inner_alias, _inner_role in sub_tables2:
                joins.append(ParsedJoin(
                    source_table=inner_table_name,
                    alias=inner_alias,
                    join_type="JOIN_SUBQUERY_INNER",
                    join_condition="",
                ))
            continue

        # 普通 JOIN 表
        table = join_expr if isinstance(join_expr, exp.Table) else (join_expr.find(exp.Table) if join_expr else None)
        if not table:
            continue
        table_name = ".".join(_clean_name(p.name) for p in table.parts).lower()
        alias = _clean_name(table.alias).lower() if table.alias else ""

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


def _extract_select_columns(select_node, comment_alias_map: dict | None = None, joins: list = None) -> list[ParsedColumn]:
    """提取 SELECT 投影列

    joins: _extract_joins 的返回值，用于回填无表前缀列的来源表别名
    """
    columns = []
    sqlglot_dialect = "oracle"

    # 构建别名回填表：如果只有一张表（或一个 FROM 主表），无前缀的列回填为该表
    fallback_alias = ""
    if joins:
        from_joins = [j for j in joins if j.join_type == "FROM"]
        if len(from_joins) == 1:
            fallback_alias = from_joins[0].alias or from_joins[0].source_table.split(".")[-1]

    for i, proj in enumerate(select_node.expressions):
        proj_sql = proj.sql(dialect=sqlglot_dialect)

        # 提取别名
        alias = _resolve_select_alias(proj, proj_sql, i, comment_alias_map)

        # 提取源字段引用
        source_fields = []
        source_tables_list = []
        seen = set()
        for col in proj.walk():
            if isinstance(col, exp.Column):
                col_name = _clean_name(col.name)
                tbl_alias = _clean_name(col.table).lower() if col.table else ""
                # 无表前缀时回填（只适用于单表 FROM）
                if not tbl_alias and fallback_alias:
                    tbl_alias = fallback_alias
                key = f"{tbl_alias}.{col_name}"
                if key not in seen:
                    seen.add(key)
                    source_fields.append({"alias": tbl_alias, "field": col_name})
                    if tbl_alias and tbl_alias not in source_tables_list:
                        source_tables_list.append(tbl_alias)

        columns.append(ParsedColumn(
            position=i,
            alias=alias,
            expression=proj_sql,
            transform_type=classify_transform(proj, proj_sql),
            source_tables=source_tables_list,
            source_fields=source_fields,
        ))

    return columns


def _penetrate_subquery_columns(tree, columns: list, depth=0):
    """对 FROM 子查询的 SELECT 字段做穿透——递归到内层子查询找物理来源。

    当 FROM 是子查询（如 FROM (...) t），顶层列 t.field 的 source_fields 停在
    子查询别名层。这里递归进入子查询，找到 field 在内层的真实物理表来源。
    支持多层嵌套（递归穿透直到物理表）。
    """
    if depth > 8 or not columns:
        return

    # 找 FROM 子查询
    select_node = tree.find(exp.Select)
    if not select_node:
        return
    from_clause = select_node.args.get("from_")
    if not from_clause:
        return
    main_expr = from_clause.this
    if not isinstance(main_expr, exp.Subquery):
        return

    sub_alias = (main_expr.alias or "").lower()
    if not sub_alias:
        return

    # 内层子查询：可能是普通 SELECT，也可能是 UNION（集合操作）
    # UNION 时不能用 find(exp.Select)——只拿第一分支，其余分支的表全丢
    inner_node = main_expr.this  # Subquery 内部的节点
    if isinstance(inner_node, (exp.Union, exp.Intersect, exp.Except)):
        # 子查询内部是 UNION：收集所有分支
        inner_branches = []
        _collect_set_branches(inner_node, inner_branches)
    else:
        inner_branches = [inner_node] if isinstance(inner_node, exp.Select) else []
    if not inner_branches:
        # fallback
        fallback = main_expr.find(exp.Select)
        inner_branches = [fallback] if fallback else []
    if not inner_branches:
        return
    # 字段映射从第一分支取（UNION 按位置对齐，各分支列结构一致）
    inner_select = inner_branches[0]

    # 内层别名 → 物理表映射（合并所有分支的表，UNION 各分支可能有不同源表）
    inner_alias_map = {}
    for branch in inner_branches:
        if not isinstance(branch, exp.Select):
            continue
        inner_from = branch.args.get("from_")
        if inner_from:
            if isinstance(inner_from.this, exp.Table):
                t = inner_from.this
                talias = t.alias.lower() if t.alias else ""
                if talias and talias not in inner_alias_map:
                    inner_alias_map[talias] = ".".join(_clean_name(p.name) for p in t.parts)
            elif isinstance(inner_from.this, exp.Subquery):
                inner_sub = inner_from.this
                salias = inner_sub.alias.lower() if inner_sub.alias else ""
                if salias and salias not in inner_alias_map:
                    inner_alias_map[salias] = salias
        for jn in branch.args.get("joins", []):
            jt = jn.this
            if isinstance(jt, exp.Table):
                jalias = jt.alias.lower() if jt.alias else ""
                if jalias and jalias not in inner_alias_map:
                    inner_alias_map[jalias] = ".".join(_clean_name(p.name) for p in jt.parts)

    # 内层 SELECT 列 → {列名(LOWER): (别名, 字段名)}
    # 从第一分支取（UNION 按位置对齐）
    # 注意：单 Column 的表达式（如 cast(user_id as bigint)）可以穿透，
    # 但多 Column 的表达式（如 a.x||a.y）不能穿透为 direct——会丢失"表达式加工"语义
    inner_cols = {}
    for proj in inner_select.expressions:
        if isinstance(proj, exp.Alias):
            alias_name = proj.alias.lower()
            col_node = proj.this
        elif isinstance(proj, exp.Column):
            alias_name = proj.name.lower()
            col_node = proj
        else:
            continue
        # 统计表达式内引用的 Column 数量
        all_cols = list(col_node.find_all(exp.Column)) if not isinstance(col_node, exp.Column) else [col_node]
        if len(all_cols) == 1:
            # 单 Column（直传或 cast/单参数函数）：可穿透
            source_col = all_cols[0]
            inner_cols[alias_name] = (source_col.table.lower() if source_col.table else "",
                                      source_col.name.lower())
        elif isinstance(col_node, exp.Column):
            # 直传 Column（all_cols=1 已覆盖，这里是防御）
            inner_cols[alias_name] = (col_node.table.lower() if col_node.table else "",
                                      col_node.name.lower())
        # 多 Column 表达式（a.x||a.y / coalesce(a,b)）：不穿透，保留在子查询层

    # 对顶层 columns 穿透
    changed = False
    for col in columns:
        for sf in col.source_fields:
            sf_alias = (sf.get("alias", "") or "").lower()
            sf_field = (sf.get("field", "") or "").lower()
            if sf_alias == sub_alias and sf_field in inner_cols:
                inner_alias, inner_field = inner_cols[sf_field]
                phys_table = inner_alias_map.get(inner_alias, inner_alias)
                sf["alias"] = inner_alias
                sf["field"] = inner_field
                if phys_table and phys_table != sub_alias:
                    col.source_tables = [phys_table]
                changed = True

    # 如果穿透后仍有 source_fields 的别名指向子查询（多层嵌套），递归
    if changed:
        _penetrate_subquery_columns(main_expr, columns, depth + 1)


# ═══════════════════════════════════════════════════════════════
# 字段使用信息提取（JOIN ON / WHERE / GROUP BY）
# ═══════════════════════════════════════════════════════════════

def _extract_subquery_usage(tree, join_usage: list, where_usage: list, depth=0):
    """递归提取所有嵌套子查询内部的 JOIN 条件和 WHERE 条件。

    嵌套子查询（FROM (SELECT ... INNER JOIN ... WHERE ...)）的内部 JOIN 和 WHERE
    不在顶层 select_node 上，_extract_field_usage 看不到。这里递归进入所有子查询，
    把内层的 JOIN ON 和 WHERE 条件提取出来，合并到外层。
    """
    if depth > 8:
        return

    # 遍历所有子查询
    for sq in tree.find_all(exp.Subquery):
        inner_select = sq.find(exp.Select)
        if not inner_select:
            continue

        # 提取内层 JOIN 的 ON 条件里的字段
        for join_node in inner_select.args.get("joins", []):
            on_expr = join_node.args.get("on")
            if not on_expr:
                continue
            on_sql = on_expr.sql(dialect="oracle")
            for col in on_expr.find_all(exp.Column):
                col_name = _clean_name(col.name).lower()
                col_alias = _clean_name(col.table).lower() if col.table else ""
                if col_name:
                    # 避免重复
                    if not any(ju["field"] == col_name and ju.get("on_condition","") == on_sql for ju in join_usage):
                        join_usage.append({
                            "field": col_name,
                            "alias": col_alias,
                            "join_type": "INNER JOIN",
                            "on_condition": on_sql,
                            "tables": [],
                        })

        # 提取内层 WHERE 的字段
        where_node = inner_select.args.get("where")
        if where_node:
            # 检查是否含 (+) 外关联
            has_join_mark = any(
                col.args.get("join_mark") for col in where_node.find_all(exp.Column)
            )
            if has_join_mark:
                conditions = _split_where_conditions(where_node.this)
                for cond in conditions:
                    cond_sql = cond.sql(dialect="oracle")
                    is_join = any(c.args.get("join_mark") for c in cond.find_all(exp.Column))
                    for col in cond.find_all(exp.Column):
                        col_name = _clean_name(col.name).lower()
                        if not col_name:
                            continue
                        if is_join:
                            if not any(ju["field"] == col_name and ju.get("on_condition","") == cond_sql for ju in join_usage):
                                join_usage.append({"field": col_name, "alias": "", "join_type": "LEFT JOIN", "on_condition": cond_sql, "tables": []})
                        else:
                            if not any(wu["field"] == col_name and wu.get("condition","") == cond_sql for wu in where_usage):
                                where_usage.append({"field": col_name, "alias": "", "condition": cond_sql})
            else:
                where_sql = where_node.sql(dialect="oracle")
                for col in where_node.find_all(exp.Column):
                    col_name = _clean_name(col.name).lower()
                    col_alias = _clean_name(col.table).lower() if col.table else ""
                    if col_name:
                        if not any(wu["field"] == col_name and wu.get("condition","") == where_sql for wu in where_usage):
                            where_usage.append({"field": col_name, "alias": col_alias, "condition": where_sql})

        # 递归进入更深层子查询
        _extract_subquery_usage(inner_select, join_usage, where_usage, depth + 1)


def _extract_cte_usage(tree, join_usage: list, where_usage: list):
    """提取 CTE 内部的 JOIN ON 和 WHERE 条件。

    CTE body 里的 JOIN/WHERE 不在顶层 select_node 上，也不在 Subquery 节点里
    （CTE 在 AST 里是 exp.CTE，挂在 with_ 子句上）。
    这里遍历所有 CTE 定义，把内部的 JOIN ON 和 WHERE 条件提取出来合并到外层。
    """
    with_clause = tree.args.get("with_")
    if not with_clause:
        return

    for cte_node in with_clause.expressions:
        cte_body = cte_node.this
        # CTE body 可能是 Union/Intersect/Except，遍历所有分支的 SELECT
        if isinstance(cte_body, (exp.Union, exp.Intersect, exp.Except)):
            branches = []
            _collect_set_branches(cte_body, branches)
            cte_selects = branches
        elif isinstance(cte_body, exp.Select):
            cte_selects = [cte_body]
        else:
            inner = cte_body.find(exp.Select) if hasattr(cte_body, 'find') else None
            cte_selects = [inner] if inner else []

        for cte_select in cte_selects:
            if not cte_select:
                continue

            # 提取 CTE 内 JOIN ON 字段
            for join_node in cte_select.args.get("joins", []):
                on_expr = join_node.args.get("on")
                if not on_expr:
                    continue
                on_sql = on_expr.sql(dialect="oracle")
                for col in on_expr.find_all(exp.Column):
                    col_name = _clean_name(col.name).lower()
                    col_alias = _clean_name(col.table).lower() if col.table else ""
                    if col_name:
                        if not any(ju["field"] == col_name and ju.get("on_condition", "") == on_sql for ju in join_usage):
                            join_usage.append({
                                "field": col_name, "alias": col_alias,
                                "join_type": "INNER JOIN", "on_condition": on_sql, "tables": [],
                            })

            # 提取 CTE 内 WHERE 字段
            where_node = cte_select.args.get("where")
            if where_node:
                where_sql = where_node.sql(dialect="oracle")
                for col in where_node.find_all(exp.Column):
                    col_name = _clean_name(col.name).lower()
                    col_alias = _clean_name(col.table).lower() if col.table else ""
                    if col_name:
                        if not any(wu["field"] == col_name and wu.get("condition", "") == where_sql for wu in where_usage):
                            where_usage.append({"field": col_name, "alias": col_alias, "condition": where_sql})


def _split_where_conditions(node) -> list:
    """把 WHERE 条件按 AND 拆分成独立条件列表。

    Oracle (+) 外关联场景: WHERE a.id=b.id(+) AND a.del='N'
    需要拆成 [a.id=b.id(+), a.del='N'] 分别判断是关联还是过滤。
    """
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _split_where_conditions(node.left) + _split_where_conditions(node.right)
    if isinstance(node, exp.Paren):
        return _split_where_conditions(node.this)
    return [node]


def _extract_field_usage(tree, select_node, joins: list, sqlglot_dialect: str) -> tuple:
    """提取 JOIN ON / WHERE / GROUP BY 中的字段级使用信息。

    Returns: (join_usage, where_usage, groupby_usage)
    - join_usage: [{field, alias, join_type, on_condition, tables: [{alias, table}]}]
    - where_usage: [{field, alias, condition}]
    - groupby_usage: [{field, alias}]
    """
    join_usage = []
    where_usage = []
    groupby_usage = []

    # 提取嵌套子查询内部的 JOIN/WHERE 条件（内层子查询的关联键和过滤条件）
    _extract_subquery_usage(tree, join_usage, where_usage)

    # 提取 CTE 内部的 JOIN/WHERE 条件（CTE body 里的关联键和过滤条件）
    _extract_cte_usage(tree, join_usage, where_usage)

    # ── 构建 alias → 物理表名 映射（含子查询）──
    alias_map = {}  # {alias_lower: table_name}
    for j in joins:
        alias = (j.alias or "").lower()
        tbl = j.source_table
        if alias and tbl:
            alias_map[alias] = tbl

    # ── JOIN ON 字段使用 ──
    joins_list = select_node.args.get("joins", [])
    for join_node in joins_list:
        on_node = join_node.args.get("on")
        if not on_node:
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

        # JOIN 表别名 → 物理表名
        join_expr = join_node.this
        tables_info = []
        if isinstance(join_expr, exp.Table):
            j_tbl_name = ".".join(_clean_name(p.name) for p in join_expr.parts).lower()
            j_alias = _clean_name(join_expr.alias).lower() if join_expr.alias else j_tbl_name.split(".")[-1]
            tables_info.append({"alias": j_alias, "table": j_tbl_name})
        elif isinstance(join_expr, exp.Subquery):
            j_alias = _clean_name(join_expr.alias).lower() if join_expr.alias else "sub"
            # 找子查询内部主表
            inner_tables = _extract_subquery_tables(join_expr)
            inner_names = [t[0] for t in inner_tables]
            tables_info.append({"alias": j_alias, "table": f"(子查询: {', '.join(inner_names[:3])})"})

        # FROM 主表也加入 tables_info
        from_clause = select_node.args.get("from_")
        if from_clause and isinstance(from_clause.this, exp.Table):
            main_tbl = ".".join(_clean_name(p.name) for p in from_clause.this.parts).lower()
            main_alias = _clean_name(from_clause.this.alias).lower() if from_clause.this.alias else main_tbl.split(".")[-1]
            tables_info.insert(0, {"alias": main_alias, "table": main_tbl})
        elif from_clause and isinstance(from_clause.this, exp.Subquery):
            sub_alias = _clean_name(from_clause.this.alias).lower() if from_clause.this.alias else "t"
            inner_tables = _extract_subquery_tables(from_clause.this)
            inner_names = [t[0] for t in inner_tables]
            tables_info.insert(0, {"alias": sub_alias, "table": f"(子查询: {', '.join(inner_names[:3])})"})

        # 从 ON 条件提取字段
        on_sql = on_node.sql(dialect=sqlglot_dialect)
        for col in on_node.find_all(exp.Column):
            col_name = _clean_name(col.name).lower()
            col_alias = _clean_name(col.table).lower() if col.table else ""
            if col_name:
                join_usage.append({
                    "field": col_name,
                    "alias": col_alias,
                    "join_type": jt,
                    "on_condition": on_sql,
                    "tables": tables_info,
                })

    # ── WHERE 字段使用 ──
    # Oracle (+) 外关联语法: WHERE a.id = b.id(+) 的条件，b 的 Column 有 join_mark=True
    # 这种条件本质是 JOIN 条件，应归到 join_usage，不是 where_usage
    where_node = select_node.args.get("where")
    if where_node:
        where_sql = where_node.sql(dialect=sqlglot_dialect)
        # 检查 WHERE 条件里有没有 join_mark（Oracle (+) 外关联）
        has_join_mark = any(
            col.args.get("join_mark") for col in where_node.find_all(exp.Column)
        )
        if has_join_mark:
            # 有 (+) 的条件：拆分成关联条件和真正的过滤条件
            # 遍历 WHERE 的 AND 连接的条件
            conditions = _split_where_conditions(where_node.this)
            for cond in conditions:
                cond_sql = cond.sql(dialect=sqlglot_dialect)
                cond_cols = list(cond.find_all(exp.Column))
                is_join_cond = any(c.args.get("join_mark") for c in cond_cols)
                for col in cond_cols:
                    col_name = _clean_name(col.name).lower()
                    col_alias = _clean_name(col.table).lower() if col.table else ""
                    if not col_name:
                        continue
                    if is_join_cond:
                        # (+) 关联条件 → join_usage
                        # 构建简化的 tables_info（从 alias_map）
                        plus_tables = [
                            {"alias": a, "table": alias_map.get(a, a)}
                            for a in {c.table.lower() for c in cond_cols if c.table}
                        ]
                        join_usage.append({
                            "field": col_name,
                            "alias": col_alias,
                            "join_type": "LEFT JOIN",
                            "on_condition": cond_sql,
                            "tables": plus_tables,
                        })
                    else:
                        # 真正的过滤条件 → where_usage
                        where_usage.append({
                            "field": col_name,
                            "alias": col_alias,
                            "condition": cond_sql,
                        })
        else:
            for col in where_node.find_all(exp.Column):
                col_name = _clean_name(col.name).lower()
                col_alias = _clean_name(col.table).lower() if col.table else ""
                if col_name:
                    where_usage.append({
                        "field": col_name,
                        "alias": col_alias,
                        "condition": where_sql,
                    })

    # ── GROUP BY 字段使用 ──
    group_node = select_node.args.get("group")
    if group_node:
        for expr in group_node.expressions:
            if isinstance(expr, exp.Column):
                col_name = _clean_name(expr.name).lower()
                col_alias = _clean_name(expr.table).lower() if expr.table else ""
                if col_name:
                    groupby_usage.append({
                        "field": col_name,
                        "alias": col_alias,
                    })

    # 去重（同字段同步骤可能多次出现）
    def _dedup(items, key_func):
        seen = set()
        result = []
        for item in items:
            key = key_func(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    join_usage = _dedup(join_usage, lambda x: f"{x['field']}.{x['alias']}.{x['join_type']}")
    where_usage = _dedup(where_usage, lambda x: f"{x['field']}.{x['alias']}")
    groupby_usage = _dedup(groupby_usage, lambda x: f"{x['field']}.{x['alias']}")

    return join_usage, where_usage, groupby_usage


def _build_join_paths(joins: list) -> dict:
    """构建每个 JOIN 表到主表的桥接链。

    输入: _extract_joins 返回的 ParsedJoin 列表
    输出: {alias: {table, role, join_type, on_condition, subquery_sql, subquery_tables, subquery_role, is_primary, path: [{from_alias, to_alias, from_table, to_table, join_type, on_condition}]}}

    path 是从主表到该表的完整桥接路径（每一步的 ON 条件）。
    """
    result = {}

    # 找主表（FROM）
    main_alias = ""
    main_table = ""
    for j in joins:
        if j.join_type == "FROM" and not j.source_table.startswith("(subquery:"):
            main_alias = j.alias
            main_table = j.source_table
            break

    if not main_alias:
        # 可能主表是子查询
        for j in joins:
            if j.join_type == "FROM":
                main_alias = j.alias
                main_table = j.source_table
                break

    # 构建邻接表: {join_alias: {table, on_condition, join_type, subquery info}}
    join_info = {}
    for j in joins:
        if j.join_type in ("FROM",) or "SUBQUERY_INNER" in j.join_type:
            continue
        if "SUBQUERY" in j.join_type:
            # 子查询 JOIN
            alias = j.alias
            join_info[alias] = {
                "table": j.source_table,
                "join_type": j.join_type,
                "on_condition": j.join_condition,
                "subquery_sql": j.subquery_sql,
                "subquery_tables": j.subquery_tables,
                "subquery_role": j.subquery_role,
            }
        elif j.join_type != "FROM":
            # 普通 JOIN
            alias = j.alias
            join_info[alias] = {
                "table": j.source_table,
                "join_type": j.join_type,
                "on_condition": j.join_condition,
            }

    # 构建 ON 条件里的别名依赖关系
    # ON t.id = a.id → a 依赖 t
    # ON a.bid = b.bid → b 依赖 a
    alias_deps = {}  # {join_alias: [depended_aliases]}
    for alias, info in join_info.items():
        on_cond = info.get("on_condition", "")
        if not on_cond:
            continue
        # 从 ON 条件提取所有别名引用（alias.field 模式）
        refs = set()
        for m in re.finditer(r'(\w+)\.\w+', on_cond):
            ref = m.group(1).lower()
            if ref != alias.lower():
                refs.add(ref)
        alias_deps[alias] = refs

    # 构建每个 JOIN 表到主表的路径
    def find_path(target_alias, visited=None):
        """从 target_alias 反向追溯到主表"""
        if visited is None:
            visited = set()
        if target_alias in visited:
            return []
        visited.add(target_alias)

        if target_alias.lower() == main_alias.lower():
            return []

        deps = alias_deps.get(target_alias, [])
        # 找到依赖的别名（优先找主表别名）
        path = []
        for dep in deps:
            if dep.lower() == main_alias.lower():
                # 直接关联主表
                break
            # 递归找 dep 的路径
            dep_path = find_path(dep, visited.copy())
            if dep_path is not None:
                path = dep_path
                break

        # 当前这一步的信息
        info = join_info.get(target_alias, {})
        step = {
            "to_alias": target_alias,
            "to_table": info.get("table", ""),
            "join_type": info.get("join_type", ""),
            "on_condition": info.get("on_condition", ""),
            "subquery_sql": info.get("subquery_sql", ""),
            "subquery_tables": info.get("subquery_tables", []),
            "subquery_role": info.get("subquery_role", ""),
        }
        result_path = path + [step] if path is not None else [step]
        return result_path

    # 主表信息
    main_info = {"table": main_table, "is_primary": True, "path": []}
    # 查主表是否有子查询信息
    for j in joins:
        if j.join_type == "FROM" and j.subquery_sql:
            main_info["subquery_sql"] = j.subquery_sql
            main_info["subquery_tables"] = j.subquery_tables
            main_info["subquery_role"] = j.subquery_role
    result[main_alias] = main_info

    # 每个 JOIN 表的路径
    for alias in join_info:
        path = find_path(alias)
        info = join_info[alias]
        result[alias] = {
            "table": info.get("table", ""),
            "is_primary": False,
            "join_type": info.get("join_type", ""),
            "subquery_sql": info.get("subquery_sql", ""),
            "subquery_tables": info.get("subquery_tables", []),
            "subquery_role": info.get("subquery_role", ""),
            "path": path,
        }

    # 子查询内部表也加入（无路径）
    for j in joins:
        if "SUBQUERY_INNER" in j.join_type:
            result[j.alias] = {
                "table": j.source_table,
                "is_primary": False,
                "join_type": j.join_type,
                "path": [],
            }

    return result



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

        # CTE body 可能是普通 SELECT，也可能是 UNION/INTERSECT/EXCEPT（集合操作）
        # 集合操作时不能用 find(exp.Select)——深度优先只拿第一个分支，其余分支的表/字段全丢
        is_set_op = isinstance(cte_query, (exp.Union, exp.Intersect, exp.Except))
        if is_set_op:
            # 收集所有分支，合并源表（去重），字段取第一分支（UNION 按位置对齐）
            branches = []
            _collect_set_branches(cte_query, branches)
            cte_select = branches[0] if branches else None  # 字段从第一分支取
            all_branch_selects = branches  # 源表从所有分支取
        elif isinstance(cte_query, exp.Select):
            cte_select = cte_query
            all_branch_selects = [cte_query]
        else:
            cte_select = cte_query if isinstance(cte_query, exp.Select) else cte_query.find(exp.Select)
            all_branch_selects = [cte_select] if cte_select else []

        # CTE 内的源表 — 只取直接 FROM/JOIN（不递归进入嵌套子查询）
        # 含 join_type，用于复杂度统计（JOIN 数）和来源表统计
        # 遍历所有分支（CTE 内 UNION 时每个分支都可能有不同的源表）
        cte_tables = []
        cte_seen_tables = set()
        for branch_select in all_branch_selects:
            if not branch_select:
                continue
            # FROM
            cte_from = branch_select.args.get("from_")
            if cte_from and isinstance(cte_from.this, exp.Table):
                tname = ".".join(_clean_name(p.name) for p in cte_from.this.parts).lower()
                talias = _clean_name(cte_from.this.alias).lower() if cte_from.this.alias else ""
                tkey = tname
                if tkey not in cte_seen_tables:
                    cte_seen_tables.add(tkey)
                    cte_tables.append({"name": tname, "alias": talias, "join_type": "FROM"})
            for extra in cte_from.expressions if cte_from else []:
                if isinstance(extra, exp.Table):
                    tname = ".".join(_clean_name(p.name) for p in extra.parts).lower()
                    talias = _clean_name(extra.alias).lower() if extra.alias else ""
                    tkey = tname
                    if tkey not in cte_seen_tables:
                        cte_seen_tables.add(tkey)
                        cte_tables.append({"name": tname, "alias": talias, "join_type": "FROM"})
            # JOIN（不递归进入 CTE 内的嵌套子查询）
            for cte_join in branch_select.args.get("joins", []):
                t = cte_join.find(exp.Table)
                if t:
                    tname = ".".join(_clean_name(p.name) for p in t.parts).lower()
                    talias = _clean_name(t.alias).lower() if t.alias else ""
                    tkey = tname
                    if tkey in cte_seen_tables:
                        continue
                    cte_seen_tables.add(tkey)
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
        if not cte_tables and cte_query:
            # fallback: find_all（覆盖非标准结构）
            for table in cte_query.find_all(exp.Table):
                tname = ".".join(_clean_name(p.name) for p in table.parts)
                talias = _clean_name(table.alias).lower() if table.alias else ""
                cte_tables.append({"name": tname, "alias": talias})

        # CTE 输出字段（含 transform_type 和 source_fields）
        # 字段从第一分支取（UNION 按位置对齐，取任一分支即可）
        cte_fields = []
        cte_select_for_fields = cte_select
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
                        tbl_alias = _clean_name(col.table).lower() if col.table else ""
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


DELETE_MODE_LABEL = {
    "0": "追加写入", "1": "覆盖写入", "2": "清空后写入",
    "3": "按条件删除后写入", "4": "增量写入",
}


def build_data_blocks(step: dict, df_step: dict, parsed, fields: list) -> list:
    """构建步骤的逻辑块（嵌套树形结构）——体现子查询层级。

    返回顶层块列表，每个块可含 children 表示嵌套:
    [{type, table, alias, role, join_type, on_condition, brought_fields, ops, children}]
    """
    import sqlglot
    from sqlglot import exp as _exp

    # 从 AST 递归构建嵌套结构
    # 复用 parse_single_sql 存的 clean_sql（已 strip + replace_placeholders），
    # 不再重新预处理——消除两次解析不一致的风险
    if parsed and not parsed.parse_error:
        try:
            clean = parsed.clean_sql or _replace_placeholders(_strip_dws_clauses(parsed.raw_sql))
            tree = sqlglot.parse_one(clean, dialect="oracle")
        except Exception:
            tree = None
    else:
        tree = None

    if tree:
        # 检测顶层是否为 UNION（走 UNION 块构建，不走普通 SELECT）
        if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
            blocks = _build_union_blocks_top(tree, df_step, fields, step.get("step_id", ""))
        else:
            blocks = _build_blocks_from_ast(tree, df_step, fields, step.get("step_id", ""))
            # 用 QueryUnit 补充 CTE 内部结构（AST 构建不展示 CTE 内部的表/JOIN）
            _enhance_blocks_with_cte(blocks, tree, df_step, fields, step.get("step_id", ""))
    else:
        blocks = _build_blocks_flat(df_step, fields, step.get("step_id", ""))

    return blocks


def _build_union_blocks_top(tree, df_step, fields, step_id):
    """顶层 UNION 的逻辑块构建——每个分支独立构建，WHERE 不混在一起。"""
    from sqlglot import exp
    branches = []
    _collect_set_branches(tree, branches)

    alias_fields = _build_alias_fields(fields, step_id)

    union_block = {
        "type": "union", "table": "UNION ALL", "alias": "",
        "role": "合并来源", "join_type": "", "on_condition": "",
        "brought_fields": [], "ops": ["合并"], "children": [],
    }

    for idx, branch in enumerate(branches):
        if not isinstance(branch, exp.Select):
            continue

        branch_block = {
            "type": "union_branch", "table": f"UNION 分支{idx+1}", "alias": "",
            "role": f"UNION 分支{idx+1}", "join_type": "", "on_condition": "",
            "brought_fields": [], "ops": [], "children": [],
        }

        # 分支内部的表和 JOIN
        from_clause = branch.args.get("from_")
        if from_clause:
            main_expr = from_clause.this
            if isinstance(main_expr, exp.Table):
                child = _make_table_block(main_expr, "inner_main", "分支主表", "", "", alias_fields)
                if child:
                    branch_block["children"].append(child)
            elif isinstance(main_expr, exp.Subquery):
                inner = main_expr.this
                if isinstance(inner, (exp.Union,)):
                    sub_blk = _make_union_block(main_expr, inner, alias_fields)
                else:
                    sub_blk = _make_subquery_block(main_expr, "分支子查询", "", alias_fields)
                if sub_blk:
                    branch_block["children"].append(sub_blk)

        for jn in branch.args.get("joins", []):
            jt_node = jn.this
            on_expr = jn.args.get("on")
            on_sql = on_expr.sql(dialect="oracle") if on_expr else ""
            join_kind = (jn.args.get("kind") or jn.args.get("side") or "JOIN").strip()
            join_type = f"{join_kind} JOIN" if join_kind and join_kind != "JOIN" else "INNER JOIN"
            if isinstance(jt_node, exp.Table):
                is_inner = join_type.upper() in ("INNER JOIN", "CROSS JOIN", "JOIN")
                role = "分支关联表" if is_inner else "分支从表"
                child = _make_table_block(jt_node, "inner_secondary", role, join_type, on_sql, alias_fields)
                if child:
                    branch_block["children"].append(child)
            elif isinstance(jt_node, exp.Subquery):
                child = _make_subquery_block(jt_node, "分支关联子查询", on_sql, alias_fields)
                if child:
                    child["join_type"] = join_type
                    branch_block["children"].append(child)

        # 分支的过滤和收敛（各自独立，不混在一起）
        inner_where = branch.args.get("where")
        inner_group = branch.args.get("group")
        if inner_where:
            branch_block["ops"].append("过滤")
            branch_block["where_clause"] = inner_where.sql(dialect="oracle").replace("WHERE ", "")
        if inner_group:
            branch_block["ops"].append("收敛")
            branch_block["group_by"] = [g.sql(dialect="oracle") for g in inner_group.expressions]

        union_block["children"].append(branch_block)

    # CTE 补充
    _enhance_blocks_with_cte([union_block], tree, df_step, fields, step_id)

    return [union_block]


def _enhance_blocks_with_cte(blocks, tree, df_step, fields, step_id):
    """用 QueryUnit 补充 CTE 内部结构到逻辑块。

    当前 _build_blocks_from_ast 只展示 CTE 名（如 tm），不展示 CTE 内部的表/JOIN。
    这里用 QueryUnit 解析 CTE 定义，把内部结构作为 children 加到 CTE 块上。
    """
    try:
        unit = parse_query_unit(tree, "oracle", 0, {})
        alias_fields = _build_alias_fields(fields, step_id)

        # 收集所有 CTE 定义
        cte_map = {}  # {cte_name(LOWER): QueryUnit}
        _collect_cte_defs(unit, cte_map)

        if not cte_map:
            return

        # 遍历 blocks，找到 CTE 块（table 名匹配 CTE 名），补充 children
        def _enhance_recursive(block_list):
            for blk in block_list:
                tbl = blk.get("table", "").lower()
                # CTE 名可能是 tm，块 table 也可能是 tm
                if tbl in cte_map:
                    cte_unit = cte_map[tbl]
                    if not blk.get("children"):
                        children = _unit_to_blocks(cte_unit, alias_fields)
                        blk["children"] = children
                        # CTE 内部的操作标签加到 children 的第一个块（内部主表），不加到 CTE 块本身
                        if children:
                            if cte_unit.where and "过滤" not in children[0].get("ops", []):
                                children[0]["ops"].append("过滤")
                            if children[0] and cte_unit.where and not children[0].get("where_clause"):
                                children[0]["where_clause"] = cte_unit.where
                            if cte_unit.group_by and "收敛" not in children[0].get("ops", []):
                                children[0]["ops"].append("收敛")
                _enhance_recursive(blk.get("children", []))

        _enhance_recursive(blocks)
    except Exception:
        pass


def _collect_cte_defs(unit, cte_map):
    """递归收集 QueryUnit 里所有 CTE 定义。"""
    if unit.type == "select":
        for cte_def in unit.cte_defs:
            cte_map[cte_def["name"]] = cte_def["body"]
            # CTE 内部可能还有 CTE
            _collect_cte_defs(cte_def["body"], cte_map)
        if unit.from_subquery:
            _collect_cte_defs(unit.from_subquery, cte_map)
        for jsq in unit.join_subqueries:
            _collect_cte_defs(jsq["body"], cte_map)
    elif unit.type == "union":
        for b in unit.branches:
            _collect_cte_defs(b, cte_map)


def _unit_to_blocks(unit, alias_fields):
    """从 QueryUnit 递归构建逻辑块（用于 CTE 内部结构展示）。"""
    blocks = []

    if unit.type in ("select", "cte_def", "cte"):
        # 主表
        for t in unit.tables:
            if t.join_type == "FROM":
                is_inner = False
                brought = _dedup_fields(alias_fields.get(t.alias, []))
                blocks.append({
                    "type": "inner_main", "table": t.table, "alias": t.alias,
                    "role": "内部主表", "join_type": "", "on_condition": "",
                    "brought_fields": brought, "ops": [], "children": [],
                })
            else:
                is_inner = t.join_type.upper() in ("INNER JOIN", "CROSS JOIN", "JOIN")
                role = "内部关联表" if is_inner else "内部从表"
                brought = _dedup_fields(alias_fields.get(t.alias, []))
                blocks.append({
                    "type": "inner_secondary", "table": t.table, "alias": t.alias,
                    "role": role, "join_type": t.join_type, "on_condition": t.on_condition,
                    "brought_fields": brought, "ops": [], "children": [],
                })

        # FROM 子查询
        if unit.from_subquery:
            sub = unit.from_subquery
            sub_blocks = _unit_to_blocks(sub, alias_fields)
            blocks.append({
                "type": "subquery", "table": f"({sub.cte_name})", "alias": sub.cte_name,
                "role": "内部子查询", "join_type": "", "on_condition": "",
                "brought_fields": [], "ops": [], "children": sub_blocks,
            })

        # JOIN 子查询
        for jsq in unit.join_subqueries:
            sub = jsq["body"]
            sub_blocks = _unit_to_blocks(sub, alias_fields)
            blocks.append({
                "type": "subquery", "table": f"({sub.cte_name})", "alias": sub.cte_name,
                "role": "内部关联子查询", "join_type": jsq.get("join_type", ""),
                "on_condition": jsq.get("on_condition", ""),
                "brought_fields": [], "ops": [], "children": sub_blocks,
            })

        # 操作标签
        if blocks:
            if unit.where:
                blocks[0]["ops"].append("过滤")
                blocks[0]["where_clause"] = unit.where
            if unit.group_by:
                blocks[0]["ops"].append("收敛")

    elif unit.type == "union":
        union_block = {
            "type": "union", "table": "UNION", "alias": "",
            "role": "合并", "join_type": "", "on_condition": "",
            "brought_fields": [], "ops": ["合并"], "children": [],
        }
        for idx, b in enumerate(unit.branches):
            branch_blocks = _unit_to_blocks(b, alias_fields)
            union_block["children"].append({
                "type": "union_branch", "table": f"UNION 分支{idx+1}", "alias": "",
                "role": f"UNION 分支{idx+1}", "join_type": "", "on_condition": "",
                "brought_fields": [], "ops": [], "children": branch_blocks,
            })
        blocks.append(union_block)

    return blocks


def _build_blocks_from_ast(tree, df_step, fields, step_id):
    """从 AST 递归构建嵌套逻辑块。"""
    from sqlglot import exp
    # 用最外层 SELECT（tree 本身），不用 find（find 深度优先会找到内层子查询的 SELECT）
    select_node = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select_node:
        return _build_blocks_flat(df_step, fields, step_id)

    # alias → 带出字段
    alias_fields = _build_alias_fields(fields, step_id)
    where_clause = (df_step.get("where_clause", "") or "").replace("WHERE ", "").strip()
    group_by = df_step.get("group_by", [])

    blocks = []

    # 处理 FROM
    from_clause = select_node.args.get("from_")
    if from_clause:
        main_expr = from_clause.this
        if isinstance(main_expr, exp.Table):
            blk = _make_table_block(main_expr, "main", "主表", "", "", alias_fields)
            if blk:
                blocks.append(blk)
        elif isinstance(main_expr, exp.Subquery):
            # 检查子查询内部是否为 UNION
            inner_node = main_expr.this
            if isinstance(inner_node, (exp.Union, exp.Intersect, exp.Except)):
                # FROM 子查询内部是 UNION —— 构建 UNION 块
                blk = _make_union_block(main_expr, inner_node, alias_fields)
                if blk:
                    blocks.append(blk)
            else:
                blk = _make_subquery_block(main_expr, "主查询来源", "", alias_fields)
                if blk:
                    blocks.append(blk)

    # 处理 JOIN
    for jn in select_node.args.get("joins", []):
        jt_node = jn.this
        on_expr = jn.args.get("on")
        on_sql = on_expr.sql(dialect="oracle") if on_expr else ""
        join_kind = (jn.args.get("kind") or jn.args.get("side") or "JOIN").strip()
        join_type = f"{join_kind} JOIN" if join_kind and join_kind != "JOIN" else "INNER JOIN"

        if isinstance(jt_node, exp.Table):
            # INNER JOIN 不分主从，标"关联表"；LEFT/RIGHT/FULL JOIN 标"从表"
            is_inner = join_type.upper() in ("INNER JOIN", "CROSS JOIN", "JOIN")
            role = "关联表" if is_inner else "从表"
            blk = _make_table_block(jt_node, "secondary", role, join_type, on_sql, alias_fields)
            if blk:
                blocks.append(blk)
        elif isinstance(jt_node, exp.Subquery):
            blk = _make_subquery_block(jt_node, "关联子查询", on_sql, alias_fields)
            if blk:
                blk["join_type"] = join_type
                blocks.append(blk)

    # 给第一个块加操作标签 + 过滤条件（主表/UNION块/子查询块都需要）
    if blocks:
        ops = blocks[0].get("ops", [])
        if where_clause:
            if "过滤" not in ops:
                ops.append("过滤")
            blocks[0]["where_clause"] = where_clause
        if group_by:
            if "收敛" not in ops:
                ops.append("收敛")
            blocks[0]["group_by"] = group_by
        blocks[0]["ops"] = ops

    return blocks


def _make_table_block(table_node, block_type, role, join_type, on_condition, alias_fields):
    """构建物理表块。"""
    from sqlglot import exp
    if not isinstance(table_node, exp.Table):
        return None
    tname = ".".join(_clean_name(p.name) for p in table_node.parts)
    alias = (table_node.alias or "").lower()
    brought = _dedup_fields(alias_fields.get(alias, []))
    return {
        "type": block_type,
        "table": tname,
        "alias": alias,
        "role": role,
        "join_type": join_type,
        "on_condition": on_condition,
        "brought_fields": brought,
        "ops": [],
        "children": [],
    }


def _make_union_block(subquery_node, union_node, alias_fields):
    """构建 UNION 块（FROM 子查询内部是 UNION ALL/UNION/INTERSECT/EXCEPT）。

    每个分支作为一个子块，展示分支内的表和关联。
    """
    from sqlglot import exp
    alias = (subquery_node.alias or "").lower()

    blk = {
        "type": "union",
        "table": f"UNION ({alias})",
        "alias": alias,
        "role": "合并来源",
        "join_type": "",
        "on_condition": "",
        "brought_fields": _dedup_fields(alias_fields.get(alias, [])),
        "ops": ["合并"],
        "children": [],
    }

    # 提取 UNION 分支
    branches = []
    _collect_set_branches(union_node, branches)

    for idx, branch in enumerate(branches):
        if not isinstance(branch, exp.Select):
            continue

        branch_blk = {
            "type": "union_branch",
            "table": f"UNION 分支{idx+1}",
            "alias": "",
            "role": f"UNION 分支{idx+1}",
            "join_type": "",
            "on_condition": "",
            "brought_fields": [],
            "ops": [],
            "children": [],
        }

        # 分支内部的表
        inner_from = branch.args.get("from_")
        if inner_from:
            main_expr = inner_from.this
            if isinstance(main_expr, exp.Table):
                child = _make_table_block(main_expr, "inner_main", "分支主表", "", "", alias_fields)
                if child:
                    branch_blk["children"].append(child)
            elif isinstance(main_expr, exp.Subquery):
                # 分支内部还有子查询（递归）
                inner_inner = main_expr.this
                if isinstance(inner_inner, (exp.Union,)):
                    child = _make_union_block(main_expr, inner_inner, alias_fields)
                else:
                    child = _make_subquery_block(main_expr, "分支子查询", "", alias_fields)
                if child:
                    branch_blk["children"].append(child)

        for jn in branch.args.get("joins", []):
            jt_node = jn.this
            on_expr = jn.args.get("on")
            on_sql = on_expr.sql(dialect="oracle") if on_expr else ""
            join_kind = (jn.args.get("kind") or jn.args.get("side") or "JOIN").strip()
            join_type = f"{join_kind} JOIN" if join_kind and join_kind != "JOIN" else "INNER JOIN"
            if isinstance(jt_node, exp.Table):
                is_inner = join_type.upper() in ("INNER JOIN", "CROSS JOIN", "JOIN")
                role = "分支关联表" if is_inner else "分支从表"
                child = _make_table_block(jt_node, "inner_secondary", role, join_type, on_sql, alias_fields)
                if child:
                    branch_blk["children"].append(child)

        # 分支的过滤和收敛
        inner_where = branch.args.get("where")
        inner_group = branch.args.get("group")
        if inner_where:
            branch_blk["ops"].append("过滤")
            branch_blk["where_clause"] = inner_where.sql(dialect="oracle").replace("WHERE ", "")
        if inner_group:
            branch_blk["ops"].append("收敛")
            branch_blk["group_by"] = [g.sql(dialect="oracle") for g in inner_group.expressions]

        blk["children"].append(branch_blk)

    return blk


def _make_subquery_block(subquery_node, role, on_condition, alias_fields):
    """构建子查询块（含内部嵌套结构）。"""
    from sqlglot import exp
    alias = (subquery_node.alias or "").lower()
    inner_select = subquery_node.find(exp.Select)
    if not inner_select:
        return None

    # 子查询别名作为展示名
    blk = {
        "type": "subquery",
        "table": f"({alias})",
        "alias": alias,
        "role": role,
        "join_type": "",
        "on_condition": on_condition,
        "brought_fields": _dedup_fields(alias_fields.get(alias, [])),
        "ops": [],
        "children": [],
    }

    # 递归提取子查询内部的表和 JOIN
    inner_from = inner_select.args.get("from_")
    if inner_from:
        main_expr = inner_from.this
        if isinstance(main_expr, exp.Table):
            child = _make_table_block(main_expr, "inner_main", "内部主表", "", "", alias_fields)
            if child:
                blk["children"].append(child)
        elif isinstance(main_expr, exp.Subquery):
            # 更深层嵌套
            child = _make_subquery_block(main_expr, "内部子查询", "", alias_fields)
            if child:
                blk["children"].append(child)

    for jn in inner_select.args.get("joins", []):
        jt_node = jn.this
        on_expr = jn.args.get("on")
        on_sql = on_expr.sql(dialect="oracle") if on_expr else ""
        join_kind = (jn.args.get("kind") or jn.args.get("side") or "JOIN").strip()
        join_type = f"{join_kind} JOIN" if join_kind and join_kind != "JOIN" else "INNER JOIN"
        if isinstance(jt_node, exp.Table):
            is_inner = join_type.upper() in ("INNER JOIN", "CROSS JOIN", "JOIN")
            role = "内部关联表" if is_inner else "内部从表"
            child = _make_table_block(jt_node, "inner_secondary", role, join_type, on_sql, alias_fields)
            if child:
                blk["children"].append(child)
        elif isinstance(jt_node, exp.Subquery):
            child = _make_subquery_block(jt_node, "内部关联子查询", on_sql, alias_fields)
            if child:
                child["join_type"] = join_type
                blk["children"].append(child)

    # 子查询内部操作标签 + 过滤条件
    inner_where = inner_select.args.get("where")
    inner_group = inner_select.args.get("group")
    if inner_where:
        blk["ops"].append("过滤")
        blk["where_clause"] = inner_where.sql(dialect="oracle").replace("WHERE ", "")
    if inner_group:
        blk["ops"].append("收敛")
        blk["group_by"] = [g.sql(dialect="oracle") for g in inner_group.expressions] if hasattr(inner_group, "expressions") else []

    return blk


def _build_alias_fields(fields, step_id):
    """构建 alias → 带出字段映射（含加工类型）。"""
    alias_fields = {}
    for f in fields:
        if f.get("producing_step") != step_id:
            continue
        tt = f.get("transform_type", "direct")
        tt_short = {"direct": "直取", "aggregate": "聚合", "expression": "加工",
                    "case_when": "条件", "fallback": "兜底", "window": "窗口",
                    "pivot": "行转列", "value": "赋值"}.get(tt, "加工")
        for l in f.get("lineage", []):
            src_alias = (l.get("source_table", "") or "").lower()
            if src_alias:
                alias_fields.setdefault(src_alias, []).append({
                    "name": f["target_field"],
                    "type": tt_short if tt != "direct" else "",
                })
    return alias_fields


def _dedup_fields(field_list):
    """字段去重（dict 列表）。"""
    seen = set()
    result = []
    for item in field_list:
        fn = item["name"] if isinstance(item, dict) else item
        if fn not in seen:
            seen.add(fn)
            result.append(item)
    return result


def _build_blocks_flat(df_step, fields, step_id):
    """回退：解析失败时用 data_flow 的 joins 平铺构建。"""
    joins = df_step.get("joins", [])
    alias_fields = _build_alias_fields(fields, step_id)
    blocks = []
    for j in joins:
        src_table = j.get("source_table", "")
        if src_table.startswith("(subquery:"):
            continue
        jt = (j.get("join_type", "") or "").upper()
        alias = (j.get("alias", "") or "").lower()
        if jt in ("FROM", "FROM_SUBQUERY_MAIN"):
            blocks.append({
                "type": "main", "table": src_table, "alias": alias, "role": "主表",
                "join_type": "", "on_condition": "",
                "brought_fields": _dedup_fields(alias_fields.get(alias, [])),
                "ops": [], "children": [],
            })
        elif "SUBQUERY" not in jt:
            blocks.append({
                "type": "secondary", "table": src_table, "alias": alias, "role": "从表",
                "join_type": j.get("join_type", ""), "on_condition": j.get("join_condition", ""),
                "brought_fields": _dedup_fields(alias_fields.get(alias, [])),
                "ops": [], "children": [],
            })
    return blocks


def build_structured_step_summary(step: dict, df_step: dict, fields: list) -> str:
    """生成结构化步骤概述（自然语言，精简归类）。

    格式:
        从 <主表>，过滤 <条件>。
        关联 <从表> 带出 <字段>；
        关联 <从表> 带出 <字段>。
        <加工归类>。
        <写入方式> <目标表>

    主表自带字段省略，只强调从表带出和加工字段。
    """
    joins = df_step.get("joins", [])
    where_clause = (df_step.get("where_clause", "") or "").replace("WHERE ", "").strip()
    target_table = step.get("target_table_full", "") or step.get("target_table", "")
    dm = (step.get("delete_mode", "") or "").strip()
    delete_label = DELETE_MODE_LABEL.get(dm, "写入")
    rule_type = step.get("rule_type", 1)

    # 分离主表和从表
    main_table = ""
    main_alias = ""
    secondary_tables = []  # [{table, alias, join_type, on_condition}]
    for j in joins:
        if j.get("source_table", "").startswith("(subquery:"):
            continue
        jt = (j.get("join_type", "") or "").upper()
        if jt == "FROM" or jt == "FROM_SUBQUERY_MAIN":
            main_table = j.get("source_table", "")
            main_alias = j.get("alias", "")
        else:
            secondary_tables.append({
                "table": j.get("source_table", ""),
                "alias": j.get("alias", ""),
                "join_type": j.get("join_type", ""),
                "on_condition": j.get("join_condition", ""),
            })

    parts = []

    # 1. 主表 + 过滤条件
    main_short = main_table.split(".")[-1] if main_table else ""
    if rule_type == 9:  # 分区交换
        parts.append(f"交换分区数据")
    elif main_short:
        if where_clause:
            parts.append(f"从 {main_table}，过滤 {where_clause}。")
        else:
            parts.append(f"从 {main_table}。")

    # 2. 从表关联带出字段（按从表归类）
    # 建别名→表名映射
    alias_map = {}
    for j in joins:
        if j.get("alias") and j.get("source_table"):
            alias_map[j["alias"].upper()] = j["source_table"]
    # 主表别名
    main_aliases = {main_alias.upper()} if main_alias else set()
    # 找从表带出的字段
    for sec in secondary_tables:
        sec_alias = sec["alias"].upper()
        sec_short = sec["table"].split(".")[-1] if sec["table"] else ""
        # 该从表带出的字段
        brought_fields = []
        for f in fields:
            if f.get("producing_step") != step.get("step_id"):
                continue
            for l in f.get("lineage", []):
                src_alias = (l.get("source_table", "") or "").upper()
                if src_alias == sec_alias:
                    brought_fields.append(f["target_field"])
                    break
        # 加工类型归类（该从表的加工字段）
        field_str = "、".join(dict.fromkeys(brought_fields)) if brought_fields else "字段"
        parts.append(f"关联 {sec_short} 带出 {field_str}；")

    # 3. 加工归类（聚合/拼接等，非从表直取的加工）
    processing_types = set()
    for f in fields:
        if f.get("producing_step") != step.get("step_id"):
            continue
        tt = f.get("transform_type", "direct")
        if tt not in ("direct", "unknown"):
            processing_types.add(tt)
    if processing_types:
        from view_generator import _describe_transform
        type_descs = []
        for tt in sorted(processing_types):
            type_descs.append(_describe_transform(tt, "", ""))
        parts.append("包含" + "、".join(type_descs) + "。")

    # 4. 写入方式
    if rule_type == 9:
        parts.append(f"交换至 {target_table}")
    else:
        parts.append(f"{delete_label} {target_table}")

    return " ".join(parts)


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
    if rule.rule_type == 2:
        write_mode = "DELETE"
    elif rule.rule_type == 9:
        write_mode = "EXCHANGE PARTITION"
    else:
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

        # SQL 中解析出的源表（主查询 FROM/JOIN + 子查询内部物理表 + CTE 内部物理表）
        parsed = parsed_map.get(rule.rule_code)
        # 收集 CTE 名（CTE 名不是物理表，要过滤）
        cte_name_set = {_norm_table(c.name) for c in parsed.ctes if c.name}
        sql_source_tables = []
        for j in parsed.source_tables:
            # 过滤子查询假名（不是物理表）；子查询内部的物理表是真实源表，保留
            if j.source_table.startswith("(subquery:"):
                continue
            # 过滤 CTE 名（CTE 不是物理表，其内部表在下面合并）
            if _norm_table(j.source_table) in cte_name_set:
                continue
            if _norm_table(j.source_table) not in [_norm_table(t) for t in sql_source_tables]:
                sql_source_tables.append(j.source_table)
        # 合并 CTE 内部的物理表（CTE 内部 UNION 的所有分支表也在这里）
        for cte in parsed.ctes:
            for ct in cte.source_tables:
                tname = ct.get("name", "")
                if not tname:
                    continue
                # 过滤 CTE 间互相引用（CTE 名不是物理表）
                if _norm_table(tname) in cte_name_set:
                    continue
                if _norm_table(tname) not in [_norm_table(t) for t in sql_source_tables]:
                    sql_source_tables.append(tname)

        # 全树扫描所有表（含子查询内部），用于自引用检测
        # parse 只做一次（在 source_tables 循环之外），用 _norm_table 归一化去重
        all_sql_tables = []
        _all_sql_seen = set()
        try:
            clean = _strip_dws_clauses(parsed.raw_sql)
            clean = _replace_placeholders(clean)
            tree = sqlglot.parse_one(clean, dialect="oracle")
            if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
                # 集合操作：收集所有分支的表
                branches = []
                _collect_set_branches(tree, branches)
                for branch in branches:
                    for table in branch.find_all(exp.Table):
                        tname = ".".join(_clean_name(p.name) for p in table.parts)
                        if tname and _norm_table(tname) not in _all_sql_seen:
                            all_sql_tables.append(tname)
                            _all_sql_seen.add(_norm_table(tname))
            else:
                # 全树扫描所有表（含 CTE 内部、子查询内部），
                # 不用 find(exp.Select) —— 深度优先在 CTE 内 UNION 时只命中第一分支
                for table in tree.find_all(exp.Table):
                    tname = ".".join(_clean_name(p.name) for p in table.parts)
                    if tname and _norm_table(tname) not in _all_sql_seen:
                        all_sql_tables.append(tname)
                        _all_sql_seen.add(_norm_table(tname))
        except Exception:
            all_sql_tables = list(sql_source_tables)  # fallback

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
            "is_view_step": getattr(rule, "is_view_step", False),  # I视图封装步骤
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

    # ── 索引：目标表 → 写入它的步骤（统一 _norm_table 归一化）──
    target_writers = {}  # {norm_table: [step_id, ...]}
    for s in steps:
        key = _norm_table(s["target_table_full"])
        target_writers.setdefault(key, []).append(s["step_id"])

    # ── 数据依赖图（用 _table_match 比较，大小写不敏感）──
    data_dependencies = []
    for s in steps:
        # 交换分区步骤：依赖写入临时表的步骤
        if s.get("is_exchange") and s.get("exchange_temp_table"):
            temp_table = s["exchange_temp_table"]
            for writer_key, writer_steps in target_writers.items():
                if _table_match(temp_table, writer_key):
                    for writer_step in writer_steps:
                        if writer_step != s["step_id"]:
                            data_dependencies.append({
                                "from": writer_step,
                                "to": s["step_id"],
                                "type": "exchange",
                                "intermediate_table": temp_table,
                            })
            continue

        for src_table in s["source_tables_from_sql"]:
            for writer_key, writer_steps in target_writers.items():
                if _table_match(src_table, writer_key):
                    for writer_step in writer_steps:
                        if writer_step != s["step_id"]:
                            data_dependencies.append({
                                "from": writer_step,
                                "to": s["step_id"],
                                "type": "data_flow",
                                "intermediate_table": src_table,
                            })

    # ── 自引用检测 ──
    # 排除删数规则（rule_type=2: TRUNCATE/DELETE），它们的目标表出现在 SQL 里是正常的
    self_references = []
    for s in steps:
        if s.get("rule_type") == 2:
            continue
        target = s["target_table_full"]
        all_tables = s.get("all_tables_from_sql", s["source_tables_from_sql"])
        # 用 _table_match 检测自引用（大小写不敏感）
        if any(_table_match(target, t) for t in all_tables):
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
                if any(_table_match(table, t) for t in s["source_tables_from_sql"]):
                    later_readers.append(s["step_id"])

        # 自引用的步骤也依赖前面的写入
        for sr in self_references:
            if _table_match(sr["table"], table) and sr["step"] in writers:
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

    # ── 删数步骤 → 后续写入步骤的隐式依赖 ──
    # 删数步骤(type=2)和写入步骤(type=1)同目标表时，建立依赖
    for s_del in steps:
        if s_del.get("rule_type") != 2:
            continue
        del_target = s_del["target_table_full"]
        del_seq = s_del["exec_sequence"]
        for s_write in steps:
            if s_write.get("rule_type") not in SELECT_RULE_TYPES:
                continue
            if s_write["step_id"] == s_del["step_id"]:
                continue
            write_target = s_write["target_table_full"]
            # 同目标表 + 写入在删数之后
            if _table_match(del_target, write_target) and s_write["exec_sequence"] > del_seq:
                # 检查是否已有依赖
                already = any(
                    d["from"] == s_del["step_id"] and d["to"] == s_write["step_id"]
                    for d in data_dependencies
                )
                if not already:
                    data_dependencies.append({
                        "from": s_del["step_id"],
                        "to": s_write["step_id"],
                        "type": "delete_before_write",
                        "intermediate_table": del_target,
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
                _table_match(sr["table"], table) for sr in self_references
            ):
                pattern = "parallel_then_serial_with_self_ref"
            else:
                pattern = "parallel_then_serial"

            target_write_groups.append({
                "target_table": table,
                "writers": writers,
                "write_pattern": pattern,
                "has_self_reference": any(
                    _table_match(sr["table"], table) for sr in self_references
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
    table_catalog: dict | None = None,
) -> dict:
    """双源交叉：TargetFields + SQL AST + DDL 类型下注。

    table_catalog（可选）：build_table_catalog 的输出，含过程表+目标表的 DDL 字段
    结构。有则给每个字段注入 field_type/field_comment；无则字段无类型（容错，不阻塞）。

    Returns: field_mappings section of knowledge.json
    """
    table_catalog = table_catalog or {}
    all_fields = []
    all_warnings = []
    # step_id → 该步写入表的 DDL 字段结构（供返回前统一注入字段类型/注释）
    step_ddl_map = {}

    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        # 本步骤写入的目标表 → 从 catalog 查 DDL 字段结构
        table_key = _normalize_table_name(rule.target_schema, rule.target_table).lower()
        table_ddl = table_catalog.get(table_key, {})
        # 交换分区：target_table 是临时表，exchange_source_table 才是真正的 F 表
        # F 表的字段结构用于字段类型下注
        if not table_ddl and rule.rule_type == 9 and rule.exchange_source_table:
            ex_key = _normalize_table_name(rule.target_schema, rule.exchange_source_table).lower()
            table_ddl = table_catalog.get(ex_key, {})
        step_ddl_map[step_id] = table_ddl
        parsed = parsed_map.get(rc)

        # 分区交换步骤：从上游步骤（写入临时表的步骤）继承字段（全部直取）
        if rule.rule_type == 9:
            # 找写入临时表的上游步骤
            temp_table = (rule.target_table or "").lower()
            temp_full = _normalize_table_name(rule.target_schema, rule.target_table).lower()
            upstream_fields = []
            for j, prev_rule in enumerate(rules[:i]):
                prev_target = _normalize_table_name(prev_rule.target_schema, prev_rule.target_table).lower()
                if prev_target == temp_full or prev_target == temp_table:
                    # 复制上游步骤的字段
                    for f in all_fields:
                        if f.get("producing_step") == f"step_{j + 1}":
                            upstream_fields.append(dict(f))  # 浅拷贝

            for uf in upstream_fields:
                field_entry = {
                    "target_field": uf["target_field"],
                    "producing_step": step_id,
                    "rule_code": rc,
                    "in_target_fields": uf.get("in_target_fields", False),
                    "excel_source_field": uf.get("excel_source_field"),
                    "transform_type": "direct",  # 交换分区 = 直取
                    "lineage": [{
                        "step": step_id,
                        "source_table": temp_table,
                        "source_field": uf["target_field"],
                        "transform": "direct",
                        "raw_sql": f"EXCHANGE PARTITION (source: {temp_table})",
                    }],
                    "validation": {"excel_vs_sql_match": None},
                }
                all_fields.append(field_entry)
            continue

        if not parsed:
            continue

        # TargetFields 按 target_field 小写建索引（大小写不敏感）
        tf_index = {}
        for tf in target_fields_map.get(rc, []):
            tf_index[tf.target_field.lower()] = tf

        # SQL SELECT 列按 alias 匹配（大小写归一化）
        sql_aliases_lower = set()  # 小写集合，用于 only_in_excel 检查
        for col in parsed.select_columns:
            alias = col.alias
            sql_aliases_lower.add(alias.lower())

            tf_data = tf_index.get(alias.lower())

            # 构建 lineage
            if col.source_fields:
                # 有源字段引用（direct/aggregate/pivot 等）
                field_lineage = [
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
                ]
            else:
                # 无源字段引用（value 类型如 'N' AS del_flag、CURRENT_TIMESTAMP）
                # 用表达式本身作为内容，确保链路不为空
                field_lineage = [{
                    "step": step_id,
                    "source_table": "",
                    "source_field": alias,
                    "transform": col.transform_type,
                    "raw_sql": col.expression,
                    "cte_name": "",
                    "cte_transform_type": "",
                    "cte_source_fields": [],
                    "cte_expression": "",
                }]

            field_entry = {
                "target_field": alias,
                "producing_step": step_id,
                "rule_code": rc,
                "in_target_fields": tf_data is not None,
                "excel_source_field": tf_data.source_field if tf_data else None,
                "transform_type": col.transform_type,
                "lineage": field_lineage,
                "validation": {
                    "excel_vs_sql_match": tf_data is not None,
                },
            }
            all_fields.append(field_entry)

        # TargetFields 里有但 SQL 没匹配到的（大小写不敏感比较）
        only_in_excel = []
        for tf in target_fields_map.get(rc, []):
            if tf.target_field.lower() not in sql_aliases_lower:
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

        # 差异预警（跳过非真实加工步骤：I视图无TargetFields，交换分区无SQL）
        if not getattr(rule, "is_view_step", False) and rule.rule_type != 9:
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

    # ── 统计（排除非真实加工步骤——视图无 TargetFields，交换分区无 SQL）──
    # I 视图步骤(is_view_step)：无 TargetFields，对比无意义
    # 交换分区步骤(rule_type=9)：无 SQL，字段从上游继承，不是自己 SELECT 的
    skip_steps = {f"step_{i+1}" for i, r in enumerate(rules)
                  if getattr(r, "is_view_step", False) or r.rule_type == 9}
    stat_fields = [f for f in all_fields if f.get("producing_step") not in skip_steps]
    total_in_sql = len([f for f in stat_fields if f.get("transform_type") != "unknown"])
    total_in_excel = len([f for f in stat_fields if f.get("in_target_fields")])
    match_count = len([f for f in stat_fields if f.get("validation", {}).get("excel_vs_sql_match") is True])
    only_in_sql = [f["target_field"] for f in stat_fields if f.get("in_target_fields") is False]
    only_in_excel_list = [f["target_field"] for f in stat_fields if f.get("note")]

    # ── 字段级 DDL 类型/注释下注（P2）──
    # 遍历所有字段，按 producing_step 找到对应表的 DDL，注入 field_type/field_comment。
    # DDL 没有（catalog 为空或该字段不在 DDL）→ 留空，不影响（DDL 是可选增强）。
    for f in all_fields:
        ddl = step_ddl_map.get(f.get("producing_step", ""), {})
        if ddl:
            tf_name = (f.get("target_field") or "").lower()
            meta = ddl.get(tf_name)
            if meta:
                f["field_type"] = meta.get("type", "")
                f["field_comment"] = meta.get("comment", "")

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
        "max_subquery_count": 0,
        "total_subquery_count": 0,
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

        # 子查询统计（只数 (subquery:xxx) 占位项，不数内部物理表）
        subquery_count = sum(1 for j in parsed.source_tables if j.source_table.startswith("(subquery:"))
        complexity_metrics["total_subquery_count"] = complexity_metrics.get("total_subquery_count", 0) + subquery_count
        complexity_metrics["max_subquery_count"] = max(complexity_metrics.get("max_subquery_count", 0), subquery_count)

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

        # 1. JOIN 缺少 ON 条件（排除子查询/逗号关联/CROSS JOIN/USING 等正常无 ON 场景）
        # 先收集 Oracle (+) 逗号关联的表（这些表没有 JOIN 节点，条件在 WHERE 里）
        plus_join_tables = set()
        raw_sql_check = parsed.raw_sql or ""
        if "(+)" in raw_sql_check:
            # (+) 语法：逗号关联的表，条件在 WHERE 里，不算缺 ON
            for j in parsed.source_tables:
                jt = (j.join_type or "").upper()
                if jt not in ("FROM", "FROM_SUBQUERY_MAIN") and not j.join_condition:
                    plus_join_tables.add(j.source_table)

        for j in parsed.source_tables:
            if j.source_table.startswith("(subquery:"):
                continue
            if "SUBQUERY" in j.join_type.upper():
                continue
            jt = (j.join_type or "").upper()
            # CROSS JOIN 本来就没有 ON 条件
            if "CROSS" in jt:
                continue
            # Oracle (+) 逗号关联的表（条件在 WHERE 里）
            if j.source_table in plus_join_tables:
                continue
            # join_condition 可能为 USING 形式或 ON 形式，检查 SQL 里是否有 USING
            if not j.join_condition and raw_sql_check:
                # 检查是不是 USING 语法（sqlglot 不设 join_condition 但 SQL 有 USING）
                tbl_short = j.source_table.split(".")[-1].upper()
                if f"USING(" in raw_sql_check.upper().replace(" ", "") or "USING (" in raw_sql_check.upper():
                    continue
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

        # 1b. Oracle (+) 外关联语法检测（不推荐，建议改用标准 LEFT JOIN）
        raw_sql = parsed.raw_sql or ""
        if "(+)" in raw_sql:
            import re as _re
            plus_count = len(_re.findall(r'\(\+\)', raw_sql))
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "code_quality",
                "title": f"使用 Oracle (+) 外关联语法（{plus_count}处），建议改用标准 LEFT JOIN",
                "description": "(+) 是 Oracle 老式外关联语法，DWS 虽兼容但可读性差、易出错，建议改用标准 JOIN ... LEFT OUTER JOIN ... ON 语法",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 1c. SELECT * / t.* 检测（规范不允许，必须明确列出字段）
        rule_type_val = s.get("rule_type", 1)
        if parsed.has_star and rule_type_val in SELECT_RULE_TYPES:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "critical",
                "category": "code_quality",
                "title": "使用 SELECT *，违反编码规范",
                "description": "SELECT * 无法追踪字段血缘，必须明确列出所需字段",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 1d. SQL 解析失败检测（仅 SELECT 类规则，非 SELECT 类的空 SQL 是正常的）
        if parsed.parse_error and rule_type_val in SELECT_RULE_TYPES:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "critical",
                "category": "parse_error",
                "title": f"SQL 解析失败: {parsed.parse_error[:60]}",
                "description": f"该规则的 SQL 无法正常解析，字段映射和血缘可能不完整。错误: {parsed.parse_error}",
                "rule_code": rc,
                "step_id": step_id,
            })

        # 2. 单规则 JOIN 过多（含 CTE 内部 JOIN，阈值翻倍）
        if total_join_count > 16:
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "medium",
                "category": "performance",
                "title": f"单规则 JOIN {total_join_count} 张表（主查询{join_count} + CTE内部{cte_join_count}）",
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
                "title": f"CTE 数量 {cte_count}，建议拆分",
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
            # 过滤子查询假名（不是物理表，不该进 schema 检查）
            if j.source_table.startswith("(subquery:"):
                continue
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
    # 排除字面量字段（value/expression 类型无别名是正常的，不是从表取的）
    for f in field_mappings.get("fields", []):
        step_id = f.get("producing_step", "")
        # 字面量/赋值/无来源的字段不报"无别名前缀"
        if f.get("transform_type") in ("value", "expression", "unknown"):
            continue
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
            # 找 rule_code
            oc_step_info = next((s for s in topology.get("steps", []) if s.get("step_id") == oc["step"]), {})
            oc_rule = oc_step_info.get("rule_code", oc["step"])
            issue_id += 1
            issues.append({
                "id": f"ISS_{issue_id:03d}",
                "severity": "info",
                "category": "scheduling",
                "title": f"规则 {oc_rule} 调度过度约束，不必要等待 {len(oc['over_constrained_on'])} 个步骤",
                "rule_code": oc_rule,
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
        "ai_insights": [],  # AI 增强时补充
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
    seen_tables = set()  # 统一用 _norm_table 去重

    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        # 分区交换：真正目标表是 exchange_source_table
        real_target = rule.exchange_source_table if (rule.rule_type == 9 and rule.exchange_source_table) else rule.target_table
        target_full = _normalize_table_name(rule.target_schema, real_target)

        # 目标表
        if _norm_table(target_full) not in seen_tables:
            seen_tables.add(_norm_table(target_full))
            all_tables.append({
                "schema": rule.target_schema,
                "name": real_target,
                "role": "target",
                "layer": _infer_layer(rule.target_schema, real_target),
            })

        parsed = parsed_map.get(rc)
        if not parsed:
            continue

        # 源表（主查询 + 子查询内部物理表；只过滤子查询假名）
        for j in parsed.source_tables:
            # 过滤子查询假名（不是物理表）；子查询内部的物理表(FROM_SUBQUERY* / JOIN_SUBQUERY_INNER)是真实源表，保留
            if j.source_table.startswith("(subquery:"):
                continue
            if _norm_table(j.source_table) not in seen_tables:
                seen_tables.add(_norm_table(j.source_table))
                parts = j.source_table.split(".")
                s_schema = parts[0] if len(parts) > 1 else ""
                s_name = parts[-1] if len(parts) > 1 else parts[0]
                all_tables.append({
                    "schema": s_schema,
                    "name": s_name,
                    "role": "intermediate" if _is_intermediate_table(j.source_table) else "source",
                    "layer": _infer_layer(s_schema, s_name),
                })

        # CTE 内部源表（也纳入来源表统计，过滤 CTE 间互相引用）
        cte_name_set = {_norm_table(c.name) for c in parsed.ctes if c.name}
        for cte in parsed.ctes:
            for ct in cte.source_tables:
                tname = ct.get("name", "")
                # 过滤掉引用其他 CTE 的（CTE 名不是物理表）
                if not tname or _norm_table(tname) in cte_name_set:
                    continue
                if _norm_table(tname) not in seen_tables:
                    seen_tables.add(_norm_table(tname))
                    parts = tname.split(".")
                    s_schema = parts[0] if len(parts) > 1 else ""
                    s_name = parts[-1] if len(parts) > 1 else parts[0]
                    all_tables.append({
                        "schema": s_schema,
                        "name": s_name,
                        "role": "intermediate" if _is_intermediate_table(tname) else "source",
                        "layer": _infer_layer(s_schema, s_name),
                    })

    # 每步骤详情
    steps_detail = []
    for i, rule in enumerate(rules):
        step_id = f"step_{i + 1}"
        rc = rule.rule_code
        # 分区交换：真正目标表是 exchange_source_table
        real_target_detail = rule.exchange_source_table if (rule.rule_type == 9 and rule.exchange_source_table) else rule.target_table
        target_full = _normalize_table_name(rule.target_schema, real_target_detail)
        parsed = parsed_map.get(rc)
        if not parsed:
            # 分区交换步骤无SQL，也需要记录
            if rule.rule_type == 9:
                step_entry = {
                    "step_id": step_id,
                    "rule_code": rc,
                    "target_table": target_full,
                    "write_mode": "EXCHANGE PARTITION",
                    "joins": [],
                    "where_clause": "",
                    "group_by": [],
                    "having_clause": "",
                    "ctes": [],
                    "raw_sql": "",
                }
                steps_detail.append(step_entry)
            continue

        # 写入模式
        dm = rule.delete_mode
        if rule.rule_type == 9:
            write_mode = "EXCHANGE PARTITION"
        elif dm == "1":
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
            "join_usage": parsed.join_usage,
            "where_usage": parsed.where_usage,
            "groupby_usage": parsed.groupby_usage,
            "join_paths": parsed.join_paths,
            # UNION 分支（分支=步骤内场景，字段来源已穿透到物理表）
            "union_branches": [
                {
                    "branch_index": b["branch_index"],
                    "source_tables": [
                        {"source_table": j.source_table, "alias": j.alias,
                         "join_type": j.join_type, "join_condition": j.join_condition}
                        for j in b["source_tables"]
                    ],
                    "columns": [
                        {"alias": c.alias, "expression": c.expression,
                         "transform_type": c.transform_type,
                         "physical_sources": c.source_fields}
                        for c in b["columns"]
                    ],
                    "join_paths": b.get("join_paths", {}),
                }
                for b in parsed.union_branches
            ] if parsed.union_branches else [],
        }
        steps_detail.append(step_entry)

    return {
        "tables": all_tables,
        "steps": steps_detail,
    }


def build_join_key_lineage(
    step_id: str,
    field_name: str,
    table_alias: str,
    rules: list[RawRule],
    parsed_map: dict,
    topology: dict,
    data_flow: dict,
    field_mappings: dict,
    visited: set = None,
    depth: int = 0,
) -> dict:
    """跨步骤追溯关联键的来源链。

    从某步骤的关联键（如 step_4 的 tmp3.bid）出发，沿数据依赖反向追溯，
    穿透中间表的直取/加工，追到物理源表的原始字段。

    追溯规则：
    - 中间表的 direct 字段 → 继续向上追溯
    - 中间表的加工字段（拼接/截取/兜底）→ 展示加工，对每个源字段继续追溯
    - 物理源表字段（ods/dim 层）→ 停止（叶节点）

    Args:
        step_id: 起始步骤（如 "step_4"）
        field_name: 关联键字段名（如 "bid"）
        table_alias: 该字段在起始 SQL 里的表别名（如 "t"，指向 tmp3）
        visited: 已访问的 (step_id, table, field) 防循环
        depth: 递归深度（最大 15）

    Returns: 追溯链树节点
        {
            "step_id": "step_4",
            "field": "bid",
            "table": "dws.tmp3",        # 解析后的物理表名
            "transform": "direct",       # 加工类型
            "raw_sql": "t.bid",          # 加工表达式
            "is_physical": False,        # 是否物理源表（叶节点标识）
            "children": [ ... ]          # 上游来源（加工字段可能多源）
        }
    """
    if visited is None:
        visited = set()
    if depth > 15:
        return None

    # 1. 解析 table_alias → 物理表名（用 data_flow 的 joins）
    df_steps = data_flow.get("steps", [])
    df_step = next((s for s in df_steps if s.get("step_id") == step_id), {})
    alias_map = {}
    for j in df_step.get("joins", []):
        if j.get("alias") and j.get("source_table"):
            alias_map[j["alias"].upper()] = j["source_table"]
    resolved_table = alias_map.get((table_alias or "").upper(), table_alias or "")

    # 2. 判断是否物理源表（ods/dim 层或非中间表，子查询假名不算物理表）
    norm_table = _norm_table(resolved_table)
    is_physical = (not _is_intermediate_table(resolved_table)
                   and not resolved_table.startswith("(subquery:"))

    # steps_list 提前定义（子查询穿透和 lineage 查找都要用）
    steps_list = topology.get("steps", [])

    # 2b. 子查询穿透：resolved_table 是 (subquery:xxx) 时，解析子查询内部找物理来源
    if resolved_table.startswith("(subquery:"):
        sq_children = _trace_subquery_sources(
            step_id, field_name, table_alias, parsed_map, steps_list,
            rules, topology, data_flow, field_mappings, visited, depth,
        )
        if sq_children is not None:
            node = {
                "step_id": step_id,
                "field": field_name,
                "table": resolved_table,
                "transform": "direct",
                "raw_sql": "",
                "is_physical": False,
                "children": sq_children,
            }
            return node

    # 3. 在 field_mappings 找该步骤该字段的 lineage
    fields_list = field_mappings.get("fields", [])
    step_to_rule = {s["step_id"]: s.get("rule_code", "") for s in steps_list}
    rule_code = step_to_rule.get(step_id, "")

    node = {
        "step_id": step_id,
        "field": field_name,
        "table": resolved_table,
        "transform": "direct",
        "raw_sql": "",
        "is_physical": is_physical,
        "children": [],
    }

    # 物理源表 → 叶节点，停止
    if is_physical:
        return node

    # 防循环
    visit_key = (step_id, norm_table, field_name.lower())
    if visit_key in visited:
        node["transform"] = "cycle"
        return node
    visited.add(visit_key)

    # 4. 找该字段的 lineage
    # 关联键通常不在 SELECT 里，而是产出该中间表的步骤才有它的 lineage。
    # 所以先定位产出 resolved_table 的步骤，从那个步骤查 field_mappings。
    producing_step = _find_producing_step(resolved_table, field_name, steps_list, rules)
    lookup_step = producing_step if producing_step else step_id

    target_field_match = None
    for f in fields_list:
        if (f.get("target_field", "").lower() == field_name.lower()
                and f.get("producing_step", "") == lookup_step):
            target_field_match = f
            break

    if not target_field_match:
        # 兜底：在所有字段里找该 step 的 lineage 里含此字段的
        for f in fields_list:
            for l in f.get("lineage", []):
                if l.get("step") == lookup_step and l.get("source_field", "").lower() == field_name.lower():
                    target_field_match = f
                    break
            if target_field_match:
                break

    if not target_field_match:
        return node  # 找不到 lineage，返回当前节点（无 children）

    lineages = target_field_match.get("lineage", [])
    # 只取属于产出步骤的 lineage（lookup_step 是产出 resolved_table 的步骤）
    step_lineages = [l for l in lineages if l.get("step") == lookup_step]
    if not step_lineages:
        step_lineages = lineages

    # 产出步骤的 alias→table 映射（用于解析 lineage 里的别名）
    producing_df_step = next((s for s in df_steps if s.get("step_id") == lookup_step), {})
    producing_alias_map = {}
    for j in producing_df_step.get("joins", []):
        if j.get("alias") and j.get("source_table"):
            producing_alias_map[j["alias"].upper()] = j["source_table"]

    # 5. 对每个 lineage 来源，递归追溯
    for l in step_lineages:
        src_field = l.get("source_field", "")
        src_table_alias = l.get("source_table", "")
        transform = l.get("transform", "direct")
        raw_sql = l.get("raw_sql", "")

        # 解析 src_table_alias（用产出步骤的 alias 映射）
        src_resolved = producing_alias_map.get(src_table_alias.upper(), src_table_alias)
        src_norm = _norm_table(src_resolved)
        src_is_physical = (not _is_intermediate_table(src_resolved)
                          and not src_resolved.startswith("(subquery:"))

        child_node = {
            "step_id": lookup_step,
            "field": src_field,
            "table": src_resolved,
            "alias": src_table_alias,  # 原始别名（如 a/b），让用户看清 a.code 是哪个表
            "transform": transform,
            "raw_sql": raw_sql,
            "is_physical": src_is_physical,
            "children": [],
        }

        if not src_is_physical and src_field:
            if src_resolved.startswith("(subquery:"):
                # 子查询穿透：解析子查询内部找物理来源
                sq_children = _trace_subquery_sources(
                    lookup_step, src_field, src_table_alias, parsed_map, steps_list,
                    rules, topology, data_flow, field_mappings, visited, depth,
                )
                if sq_children is not None:
                    child_node["children"] = sq_children
                    child_node["is_physical"] = False
            else:
                # 中间表字段 → 找它产生的步骤，继续追溯
                upstream_step = _find_producing_step(src_resolved, src_field, steps_list, rules)
                if upstream_step:
                    child = build_join_key_lineage(
                        upstream_step, src_field, src_resolved, rules, parsed_map,
                        topology, data_flow, field_mappings, visited.copy(), depth + 1,
                    )
                    if child:
                        child_node = child
                        child_node["transform"] = transform  # 保留当前跳的加工类型
                        child_node["raw_sql"] = raw_sql

        node["children"].append(child_node)

    # 多源加工节点（如拼接 a.code||b.seq）附上该步骤的关联关系，
    # 让用户知道参与加工的表之间是怎么 JOIN 的
    if len(node["children"]) > 1:
        node["join_relations"] = _extract_join_relations(lookup_step, producing_df_step)

    return node


def _trace_subquery_sources(
    step_id, field_name, table_alias, parsed_map, steps_list,
    rules, topology, data_flow, field_mappings, visited, depth,
):
    """穿透子查询：从 (subquery:xxx) 占位的 subquery_sql 解析字段的物理来源。

    Returns: 子节点列表（物理来源），或 None（无法穿透）。
    """
    step_to_rule = {s["step_id"]: s.get("rule_code", "") for s in steps_list}
    rule_code = step_to_rule.get(step_id, "")
    parsed = parsed_map.get(rule_code)
    if not parsed:
        return None

    # 找子查询占位（alias 匹配 table_alias）
    sq_placeholder = None
    for j in parsed.source_tables:
        if j.source_table.startswith("(subquery:") and j.subquery_sql:
            if not table_alias or j.alias.upper() == (table_alias or "").upper():
                sq_placeholder = j
                break
    if not sq_placeholder or not sq_placeholder.subquery_sql:
        return None

    # 重新解析子查询 SQL，找 field_name 的来源
    try:
        sq_parsed = parse_single_sql(sq_placeholder.subquery_sql, "oracle")
    except Exception:
        return None
    if sq_parsed.parse_error:
        return None

    # 子查询内部 alias → 物理表
    sq_alias_map = {}
    for ij in sq_parsed.source_tables:
        if not ij.source_table.startswith("(subquery:") and ij.alias:
            sq_alias_map[ij.alias.upper()] = ij.source_table

    # 找该字段在子查询内的列
    sq_col = None
    for c in sq_parsed.select_columns:
        if (c.alias or "").lower() == field_name.lower():
            sq_col = c
            break
    if not sq_col or not sq_col.source_fields:
        return None

    # 对每个来源构建子节点
    children = []
    for sf in sq_col.source_fields:
        sf_alias = (sf.get("alias", "") or "").upper()
        sf_field = sf.get("field", "")
        src_table = sq_alias_map.get(sf_alias, sf_alias)
        src_norm = _norm_table(src_table)
        src_is_physical = (not _is_intermediate_table(src_table)
                          and not src_table.startswith("(subquery:"))

        child = {
            "step_id": step_id,
            "field": sf_field,
            "table": src_table,
            "alias": sf_alias,
            "transform": sq_col.transform_type,
            "raw_sql": sq_col.expression,
            "is_physical": src_is_physical,
            "children": [],
        }

        if not src_is_physical and sf_field:
            upstream = _find_producing_step(src_table, sf_field, steps_list, rules)
            if upstream:
                sub = build_join_key_lineage(
                    upstream, sf_field, src_table, rules, parsed_map,
                    topology, data_flow, field_mappings, visited.copy(), depth + 1,
                )
                if sub:
                    child = sub
                    child["transform"] = sq_col.transform_type
                    child["raw_sql"] = sq_col.expression

        children.append(child)

    return children if children else None


def _extract_join_relations(step_id: str, df_step: dict) -> list:
    """从 step 的 join_paths 提取关联关系摘要（哪些表 JOIN，ON 条件）。

    Returns: [{alias, table, join_type, on_condition}, ...]
    """
    relations = []
    join_paths = df_step.get("join_paths", {})
    for alias, info in join_paths.items():
        for p in info.get("path", []):
            on_cond = p.get("on_condition", "")
            if on_cond:
                relations.append({
                    "alias": p.get("to_alias", alias),
                    "table": p.get("to_table", ""),
                    "join_type": p.get("join_type", ""),
                    "on_condition": on_cond,
                })
    return relations


def _find_producing_step(table_name: str, field_name: str, steps_list: list, rules: list[RawRule]) -> str:
    """找到产出某表某字段的步骤 ID。

    通过 data_dependencies 或 target_table 反查。
    """
    norm = _norm_table(table_name)
    for s in steps_list:
        target_full = _norm_table(s.get("target_table_full", "") or
                                  _normalize_table_name(s.get("target_schema", ""), s.get("target_table", "")))
        if target_full == norm:
            return s.get("step_id", "")
        # 短名匹配
        if norm.endswith("." + target_full.split(".")[-1]) or target_full.endswith("." + norm.split(".")[-1]):
            return s.get("step_id", "")
    return ""


def enrich_join_key_lineage(
    data_flow: dict,
    rules: list[RawRule],
    parsed_map: dict,
    topology: dict,
    field_mappings: dict,
) -> None:
    """对 data_flow 的每个 step，计算关联键的跨步骤追溯链，注入 step.join_key_lineage。

    只对"中间表关联键"（join_usage 里 table 是中间表的字段）算追溯，
    物理源表的关联键不需要追溯（它就是源端）。

    直接修改 data_flow["steps"] 的每个 step，加 "join_key_lineage" 字段：
        {field_name: [追溯链树, ...]}  # 同名关联键可能多个（不同 JOIN）
    """
    steps_list = topology.get("steps", [])
    for step in data_flow.get("steps", []):
        step_id = step.get("step_id", "")
        join_usage = step.get("join_usage", [])
        if not join_usage:
            continue
        key_lineage = {}
        seen_keys = set()
        for ju in join_usage:
            field = ju.get("field", "")
            if not field:
                continue
            # 只追溯中间表的关联键（物理源表不需要）
            for tbl_info in ju.get("tables", []):
                tbl = tbl_info.get("table", "")
                alias = tbl_info.get("alias", "")
                norm_tbl = _norm_table(tbl)
                is_intermediate = _is_intermediate_table(tbl)
                if not is_intermediate:
                    continue
                trace_key = (field.lower(), norm_tbl)
                if trace_key in seen_keys:
                    continue
                seen_keys.add(trace_key)
                chain = build_join_key_lineage(
                    step_id, field, alias, rules, parsed_map,
                    topology, data_flow, field_mappings,
                )
                if chain and chain.get("children"):
                    key_lineage.setdefault(field.lower(), []).append(chain)
        if key_lineage:
            step["join_key_lineage"] = key_lineage


def enrich_field_physical_sources(
    field_mappings: dict,
    data_flow: dict,
    rules: list[RawRule],
    parsed_map: dict,
    topology: dict,
) -> None:
    """对 field_mappings 的每个字段，追溯跨步骤物理来源，注入 physical_source。

    直接修改 field_mappings["fields"] 的每个 field，加 "physical_source" 字段：
        [{
            "table": "ods.tbl_b",       # 物理源表
            "field": "bname",           # 物理源字段
            "alias": "b",               # 别名
            "transform": "aggregate",   # 追溯链上最重的加工类型
            "raw_sql": "SUM(t.bname)",  # 加工表达式
        }, ...]
    多源加工（如拼接）返回多个物理来源。
    """
    steps_list = topology.get("steps", [])
    # step_id → df_step 的 alias_map 缓存
    step_alias_maps = {}
    for ds in data_flow.get("steps", []):
        sid = ds.get("step_id", "")
        amap = {}
        for j in ds.get("joins", []):
            if j.get("alias") and j.get("source_table"):
                amap[j["alias"].upper()] = j["source_table"]
        step_alias_maps[sid] = amap

    for f in field_mappings.get("fields", []):
        step_id = f.get("producing_step", "")
        fname = f.get("target_field", "")
        if not step_id or not fname:
            continue

        # 从该字段的 lineage 取第一个来源的别名，作为追溯起点
        lineages = f.get("lineage", [])
        if not lineages:
            continue
        first_src = lineages[0]
        src_alias = first_src.get("source_table", "")
        # 追溯用 source_field（加工前的字段名），不是 target_field
        # 因为加工后字段名可能变了（如 SUM(amount) AS total_amount），
        # 上游步骤里存的是 amount 不是 total_amount
        src_field = first_src.get("source_field", "") or fname

        chain = build_join_key_lineage(
            step_id, src_field, src_alias, rules, parsed_map,
            topology, data_flow, field_mappings,
        )
        if not chain:
            continue

        # 提取叶节点（物理源表）+ 链上最重加工
        physical_sources = _extract_physical_sources_from_chain(chain)
        if physical_sources:
            f["physical_source"] = physical_sources


def _extract_physical_sources_from_chain(chain: dict) -> list:
    """从追溯链提取物理来源（叶节点）+ 加工信息。

    Returns: [{table, field, alias, transform, raw_sql}, ...]
    """
    result = []
    # 收集链上所有非 direct 的加工（取第一个作为代表）
    processing = None
    for node in _walk_chain_for_extract(chain):
        tt = node.get("transform", "direct")
        if tt != "direct":
            processing = node
            break

    # 收集叶节点
    leaves = [n for n in _walk_chain_for_extract(chain) if not n.get("children")]
    for leaf in leaves:
        result.append({
            "table": leaf.get("table", ""),
            "field": leaf.get("field", ""),
            "step_id": leaf.get("step_id", ""),  # 叶节点所在步骤（CTE 穿透用）
            "alias": leaf.get("alias", ""),
            "transform": (processing or leaf).get("transform", "direct"),
            "raw_sql": (processing or leaf).get("raw_sql", ""),
        })
    return result


def _walk_chain_for_extract(chain):
    """遍历追溯链所有节点。"""
    if not chain:
        return
    yield chain
    for child in chain.get("children", []):
        yield from _walk_chain_for_extract(child)


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

def _extract_create_table_body(content: str) -> str:
    r"""从 DDL 内容里提取 CREATE TABLE 字段定义块（括号内内容）。

    用括号配平精确定位表定义的闭合括号，避免贪婪正则把 WITH(...)/DISTRIBUTE BY/
    PARTITION BY 等表定义后的子句包进来。

    支持多种写法：
        CREATE TABLE t (...)
        CREATE TABLE IF NOT EXISTS t (...)
        CREATE TABLE schema.t (...)
        CREATE TABLE t /* 注释 */ (...)
    多空格/tab/换行都容错（靠 ``\s+`` 和 ``\S*``）。
    """
    # 定位 CREATE TABLE 后的第一个左括号（跳过表名/schema/IF NOT EXISTS/注释）
    ct = re.search(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[^\(]*\(',
        content, re.IGNORECASE | re.DOTALL
    )
    if not ct:
        return ""
    start = ct.end()  # 第一个 ( 之后
    # 从 start 开始括号配平，找到配对的闭合 )
    depth = 1
    i = start
    while i < len(content):
        ch = content[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return content[start:i]
        i += 1
    return content[start:]  # 配平失败兜底（取到末尾）


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

    # 递归扫描（rglob）：支持 ddl_dir 是 table/ 或 schema/（含 table/+view/ 子目录）
    # 多扩展名：.sql/.ddl/.txt（大小写不敏感）
    sql_files = []
    for ext in ("*.sql", "*.ddl", "*.txt", "*.SQL", "*.DDL", "*.TXT"):
        sql_files.extend(ddl_path.rglob(ext))
    # 去重
    seen = set()
    for sql_file in sql_files:
        real = str(sql_file.resolve())
        if real in seen:
            continue
        seen.add(real)
        content = sql_file.read_text(encoding="utf-8", errors="ignore")
        content_lower = content.lower()

        if target_lower not in content_lower:
            continue
        if "create table" not in content_lower:
            continue

        # ── 1. 提取字段名+类型+约束 ──
        # 用括号配平提取 CREATE TABLE 的字段定义块，比贪婪正则健壮：
        # GaussDB DDL 表定义后常有 WITH(...) / DISTRIBUTE BY / PARTITION BY 等子句，
        # 贪婪正则 (.*) 会把这些包进来；括号配平精确停在表定义的闭合括号。
        body = _extract_create_table_body(content)
        if not body:
            continue

        # 字段名 + 类型：类型可含空格（如 "character varying"）和括号参数（如 (10) 或 (18,2)）
        # 字段名允许大写（DDL 导出可能全大写）
        pattern = r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+(.+)'
        skip_words = ('create', 'table', 'view', 'as', 'select', 'from', 'where', 'and', 'or')
        # 类型后面可能跟的约束关键字（NOT NULL / DEFAULT / PRIMARY / COMMENT 等）
        # 类型匹配到这里为止，避免把约束也吃进类型里
        _TYPE_STOP_RE = re.compile(
            r'\s+(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|COMMENT|CHECK|UNIQUE)'
            r'\b', re.IGNORECASE
        )
        # 纯类型名 + 括号参数的提取：从类型字符串里取"类型名 + 可选(参数)"
        # 贪婪匹配类型名（含空格如 "character varying"，含数字如 "nvarchar2"）
        _TYPE_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9\s]*[a-zA-Z0-9])(\s*\([^)]*\))?')

        # 按顶层逗号拆分字段定义（不拆类型里的逗号如 DECIMAL(18,2)）
        # 用括号深度判断：只在 depth==0 的逗号处拆分
        field_lines = []
        current = ""
        depth = 0
        for ch in body:
            if ch == "(":
                depth += 1
                current += ch
            elif ch == ")":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                field_lines.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            field_lines.append(current.strip())

        # 先收集 PRIMARY KEY 字段（行级 PRIMARY KEY (a, b) 形式）
        pk_fields = set()
        for line in field_lines:
            line = line.strip()
            if line.upper().startswith("PRIMARY"):
                # PRIMARY KEY (field1, field2)
                pk_match = re.search(r'PRIMARY\s+KEY\s*\(([^)]+)\)', line, re.IGNORECASE)
                if pk_match:
                    for f in pk_match.group(1).split(","):
                        pk_fields.add(f.strip().lower())

        for line in field_lines:
            line = line.strip().rstrip(",")
            if line.upper().startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "KEY", "CHECK", ")", "(", "/")):
                continue
            m_re = re.match(pattern, line)
            if m_re:
                fname = m_re.group(1).lower()
                if fname in skip_words:
                    continue
                raw_type = m_re.group(2)

                # 从 raw_type 里提取干净的类型名 + 参数：
                # 1. 先在约束关键字处截断（去掉 NOT NULL / DEFAULT 等）
                stop_m = _TYPE_STOP_RE.search(raw_type)
                type_str = raw_type[:stop_m.start()].strip() if stop_m else raw_type.strip()
                # 2. 再用正则提取"类型名 + 可选(参数)"，去掉行内注释/多余空格
                type_m = _TYPE_RE.match(type_str)
                if type_m:
                    type_name = type_m.group(1).strip()
                    type_params = (type_m.group(2) or "").strip()
                    ftype = (type_name + type_params).strip()
                else:
                    ftype = type_str.split()[0] if type_str.split() else ""

                # 尝试提取行内注释：/* 中文名 */ 或 -- 中文名
                # 注意：-- 注释要排除引号内的 --（如 DEFAULT 'http://--test'），
                # 只在行尾、且 -- 前面是空格时才匹配（避免误匹配 DEFAULT 值）
                inline_comment = ""
                cm = re.search(r'/\*\s*(.+?)\s*\*/', line)
                if cm:
                    inline_comment = cm.group(1).strip()
                else:
                    # 行尾 -- 注释：要求 -- 前是空格（字段类型定义后的注释）
                    # 先去掉引号内的内容（避免引号里的 -- 干扰）
                    line_no_quotes = re.sub(r"'[^']*'", "''", line)
                    line_no_quotes = re.sub(r'"[^"]*"', '""', line_no_quotes)
                    dm = re.search(r'\s--\s*(.+?)\s*$', line_no_quotes)
                    if dm:
                        # 从原始 line 取注释内容（保持中文不丢）
                        orig_dm = re.search(r'\s--\s*(.+?)\s*$', line)
                        if orig_dm:
                            inline_comment = orig_dm.group(1).strip()

                # 解析 NOT NULL（默认 nullable=True，有 NOT NULL 则 nullable=False）
                nullable = "not null" not in line.lower()
                # 解析 DEFAULT 值
                default_value = ""
                dm = re.search(r"DEFAULT\s+(\S+)", line, re.IGNORECASE)
                if dm:
                    default_value = dm.group(1).rstrip(",").strip("'\"")

                result[fname] = {
                    "type": ftype,
                    "comment": inline_comment,
                    "nullable": nullable,
                    "default_value": default_value,
                    "is_pk": fname in pk_fields,
                }

        # ── 2. 提取 COMMENT ON COLUMN（覆盖行内注释）──
        # 支持单引号和双引号两种写法（真实 DDL 导出工具可能用双引号）
        for cm_match in re.finditer(
            r"COMMENT\s+ON\s+COLUMN\s+\S+\.(\w+)\s+IS\s*['\"]([^'\"]*)['\"]",
            content, re.IGNORECASE
        ):
            fname = cm_match.group(1).lower()
            comment = cm_match.group(2).strip()
            if fname in result:
                result[fname]["comment"] = comment
            else:
                result[fname] = {"type": "", "comment": comment,
                                 "nullable": True, "default_value": "", "is_pk": False}

    return result


# 向后兼容旧调用
def parse_ddl_for_types(ddl_dir: str, target_table: str) -> dict[str, str]:
    """已废弃，使用 parse_ddl_for_metadata。保留向后兼容。"""
    metadata = parse_ddl_for_metadata(ddl_dir, target_table)
    return {k: v["type"] for k, v in metadata.items() if v.get("type")}


def build_table_catalog(rules: list, ddl_dir: str, parsed_map: dict = None) -> dict:
    """构建多表结构目录（过程表 + 目标表的 DDL 结构）。

    从 rules 遍历每一步的 target_table（含过程表/中间表），批量解析 DDL，
    返回按表名索引的字段结构目录。这是字段级类型下注(P2)和跨表一致性
    检查(P3)的数据基础。

    Args:
        rules: RawRule 列表（每步的 target_schema/target_table 是表名来源）
        ddl_dir: DDL 文件目录（parse_ddl_for_metadata 扫描此目录）

    Returns: {"schema.table(小写)": {field(小写): {type, comment, nullable, ...}}}
        找不到 DDL 或 ddl_dir 为空 → 返回空 dict（容错，不阻塞分析）

    容错设计（DDL 是可选增强，永远不阻塞分析）：
        - ddl_dir 为空/不存在 → 返回 {}
        - 某张表没 DDL → 该表不在 catalog 里，其他表照常
        - DDL 解析失败 → 该表跳过，其他表照常
    """
    if not ddl_dir:
        return {}

    catalog = {}
    for i, rule in enumerate(rules):
        # 收集这一步涉及的所有表名（target_table + 交换分区的 exchange_source_table）
        tables_to_parse = []
        if rule.target_table:
            tables_to_parse.append((rule.target_schema, rule.target_table))
        # 交换分区：exchange_source_table 是真正的目标表（F表），必须纳入 catalog
        if rule.rule_type == 9 and rule.exchange_source_table:
            tables_to_parse.append((rule.target_schema, rule.exchange_source_table))

        # ★ 源表也纳入 catalog（如果 DDL 目录里有对应 DDL）
        # 用于跨表类型一致性检查：需要对比源表字段类型 vs 目标表字段类型
        # parsed.source_tables 含本步所有源表（ParsedJoin 列表）
        parsed = parsed_map.get(rule.rule_code) if parsed_map else None
        if parsed:
            for j in parsed.source_tables:
                src_table = j.source_table
                if not src_table or src_table.startswith("(subquery:"):
                    continue
                # 解析 schema.table
                parts = src_table.split(".")
                if len(parts) >= 2:
                    tables_to_parse.append((parts[0], ".".join(parts[1:])))
                else:
                    tables_to_parse.append(("", src_table))

        for schema, table in tables_to_parse:
            if not table:
                continue
            full_table = _normalize_table_name(schema, table)
            table_key = full_table.lower()

            if table_key in catalog:
                continue  # 同一张表不重复解析

            try:
                meta = parse_ddl_for_metadata(ddl_dir, table)
                if meta:
                    catalog[table_key] = meta
            except Exception:
                continue

    return catalog


def check_type_consistency(field_mappings: dict, table_catalog: dict,
                           rules: list, parsed_map: dict) -> list:
    """跨表字段类型一致性检查（P3）。

    同一个字段在过程表和目标表的类型/长度不一致 = 潜在数据质量问题
    （如 DECIMAL(18,4) → DECIMAL(18,2) 精度丢失，VARCHAR(128) → VARCHAR(64) 截断）。

    检查逻辑：
        遍历 field_mappings.fields，每个字段有 lineage（追溯链）。
        如果 lineage 的 source_table 能解析为一张在 catalog 里的表，
        且该源表字段有 DDL 类型，则对比源表类型 vs 当前字段类型。
        类型不一致（归一化后）→ 记入 issues。

    Args:
        field_mappings: build_field_mappings 的输出（字段已含 field_type）
        table_catalog: build_table_catalog 的输出（多表 DDL 结构）
        rules: RawRule 列表（用于解析 lineage 里的别名→真实表名）
        parsed_map: 解析结果（含 source_tables 的 alias→table 映射）

    Returns: [issue, ...] issue 结构同 analyze_quality 的 issue
        catalog 为空 → 返回空列表（无 DDL 不检查，容错）
    """
    if not table_catalog:
        return []

    # 构建 step_id → {alias_upper: real_table_full} 的别名映射
    # lineage 里的 source_table 是别名（如 t/a），需要解析为真实表名
    step_alias_map = {}
    # step_id → 该步写入的目标表（producing table），用于 issue 描述里指出"在哪"
    step_target_table = {}
    for i, rule in enumerate(rules):
        sid = f"step_{i+1}"
        step_target_table[sid] = _normalize_table_name(rule.target_schema, rule.target_table)
        parsed = parsed_map.get(rule.rule_code)
        if not parsed:
            continue
        amap = {}
        for j in parsed.source_tables:
            if j.alias and j.source_table:
                amap[j.alias.upper()] = j.source_table
        step_alias_map[sid] = amap

    issues = []
    fields = field_mappings.get("fields", [])

    for f in fields:
        ftype = f.get("field_type", "")
        if not ftype:
            continue  # 当前字段没类型，跳过

        target_field = f.get("target_field", "")
        producing_step = f.get("producing_step", "")
        current_table = step_target_table.get(producing_step, "")

        # ★ 类型一致性检查的范围控制：
        # 按"输出类型的来源"决定怎么取源类型、是否检查。
        #
        # 检查方式：
        #   direct / SUM/MIN/MAX → 从 catalog 查源表字段类型，走 lineage 对比
        #   value（常量赋值）    → 从常量本身推断类型（'N'→varchar(1)），直接对比
        #   expression 的 CAST   → 从 cast 提取目标类型（cast(x as bigint)→bigint），直接对比
        #   case_when            → 提取 THEN/ELSE 分支里的字段，按 direct 处理
        # 不检查：
        #   COUNT（输出恒 bigint）、普通 expression（a||b / a+b 难以推断）、pivot/window
        transform_type = f.get("transform_type", "")

        if transform_type == "value":
            # 常量赋值：从 raw_sql 推断常量类型，跟目标 DDL 比
            for lin in f.get("lineage", []):
                raw_sql = lin.get("raw_sql") or ""
                const_type = _infer_constant_type(raw_sql)
                if const_type:
                    _add_type_issue_if_mismatch(
                        issues, target_field, ftype, const_type,
                        "常量赋值", producing_step, current_table,
                    )
                break  # value 只有一个 lineage 条目
            continue  # value 不走下面的 lineage 循环

        if transform_type == "expression":
            # 区分 CAST 表达式 vs 普通 expression
            expr_sql = ""
            for lin in f.get("lineage", []):
                expr_sql = (lin.get("raw_sql") or "").upper()
                if expr_sql:
                    break
            cast_type = _extract_cast_target_type(expr_sql)
            if cast_type:
                # CAST 表达式：用 cast 目标类型跟字段 DDL 比
                _add_type_issue_if_mismatch(
                    issues, target_field, ftype, cast_type,
                    "CAST转换", producing_step, current_table,
                )
                continue
            else:
                # 普通 expression（a||b / a+b）：输出类型难推断，跳过
                continue

        if transform_type == "case_when":
            # case_when：提取 THEN/ELSE 分支里的字段，按 direct 对比
            # （条件 WHEN 里的字段不参与，那是判断逻辑不是输出来源）
            branch_fields = _extract_case_when_branch_fields(f.get("lineage", []))
            for bf in branch_fields:
                src_alias, src_field, lin_step = bf
                src_table = _resolve_alias_to_table(src_alias, lin_step, step_alias_map)
                if not src_table:
                    continue
                src_type = _lookup_catalog_type(src_table, src_field, table_catalog)
                if src_type:
                    _add_type_issue_if_mismatch(
                        issues, target_field, ftype, src_type,
                        f"CASE分支字段({src_table}.{src_field})", producing_step, current_table,
                    )
            continue  # case_when 不走下面的 lineage 循环

        if transform_type == "aggregate":
            # 区分 COUNT（跳过）vs SUM/MIN/MAX（检查）
            expr_sql = ""
            for lin in f.get("lineage", []):
                expr_sql = (lin.get("raw_sql") or "").upper()
                if expr_sql:
                    break
            if "COUNT(" in expr_sql or "COUNT (" in expr_sql:
                continue  # COUNT 输出 bigint
            # SUM/MIN/MAX 走下面的 lineage 对比
        elif transform_type not in ("direct",):
            continue  # pivot/window 等跳过

        # 遍历 lineage，找指向 catalog 表的源字段（direct + SUM/MIN/MAX 走这里）
        for lin in f.get("lineage", []):
            src_alias = lin.get("source_table", "")
            src_field = lin.get("source_field", "") or target_field
            lin_step = lin.get("step", producing_step)

            if not src_alias or not src_field:
                continue

            # 别名 → 真实表名
            amap = step_alias_map.get(lin_step, {})
            src_table = amap.get(src_alias.upper(), "")
            if not src_table:
                continue

            # 查 catalog
            src_table_key = _norm_table(src_table).lower()
            src_meta = table_catalog.get(src_table_key, {})
            src_type = src_meta.get(src_field.lower(), {}).get("type", "")
            if not src_type:
                continue

            _add_type_issue_if_mismatch(
                issues, target_field, ftype, src_type,
                f"{src_table}（{lin_step}）", producing_step, current_table,
                extra={"source_table": src_table, "source_field": src_field,
                       "source_step": lin_step, "field": target_field},
            )

    return issues


def _add_type_issue_if_mismatch(
    issues: list, target_field: str, target_type: str, source_type: str,
    source_desc: str, producing_step: str, current_table: str,
    extra: dict = None,
):
    """类型不一致时追加 issue（归一化对比 + 严重度判定）。

    target_type 是目标字段 DDL 类型；source_type 是源类型（源表字段/常量/cast目标/分支字段）。
    extra 额外字段（如 source_table/source_field，给 direct/aggregate 路径用）。
    """
    ftype_norm = _normalize_type(target_type)
    src_type_norm = _normalize_type(source_type)
    if ftype_norm == src_type_norm:
        return  # 类型一致，不报

    # 同家族的整数类型兼容（int/bigint/smallint 互转不丢数据，sqlglot 会把 bigint 标准化成 int）
    if _same_int_family(ftype_norm, src_type_norm):
        return

    severity = "medium"
    mismatch_kind = "类型不一致"
    if _is_precision_change(ftype_norm, src_type_norm):
        severity = "high"
        mismatch_kind = "精度不一致"
    elif ftype_norm.split("(")[0] != src_type_norm.split("(")[0]:
        severity = "high"
        mismatch_kind = "类型不一致"

    issue = {
        "category": "type_consistency",
        "severity": severity,
        "title": f"字段{mismatch_kind}: {target_field} ({source_desc} → {current_table})",
        "description": (
            f"字段 '{target_field}' 来源 {source_desc} 类型为 {source_type}，"
            f"写入 {current_table}（{producing_step}）后类型为 {target_type}，"
            f"{mismatch_kind}可能导致数据{'精度丢失或截断' if severity == 'high' else '异常'}"
        ),
        "field": target_field,
        "source_type": source_type,
        "current_table": current_table,
        "current_type": target_type,
        "current_step": producing_step,
        "mismatch_kind": mismatch_kind,
    }
    if extra:
        issue.update(extra)
    issues.append(issue)


def _infer_constant_type(raw_sql: str) -> str:
    """从赋值表达式的常量推断类型。

    'N' AS flag      → varchar(1)
    'UNKNOWN' AS x   → varchar(7)
    0 AS status      → integer
    1.5 AS factor    → numeric(2,1)
    CAST(0 AS ...)   → 走 _extract_cast_target_type
    """
    import re
    if not raw_sql:
        return ""
    sql = raw_sql.strip()
    # 去掉 AS alias 部分
    sql_no_alias = re.sub(r'\s+AS\s+\w+\s*$', '', sql, flags=re.IGNORECASE).strip()

    # 字符串常量
    str_m = re.match(r"^'([^']*)'$", sql_no_alias)
    if str_m:
        val = str_m.group(1)
        return f"varchar({len(val)})" if val else "varchar(0)"
    # 数字常量（带小数）
    if re.match(r"^-?\d+\.\d+$", sql_no_alias):
        int_part, dec_part = sql_no_alias.lstrip("-").split(".")
        return f"numeric({len(int_part) + len(dec_part)},{len(dec_part)})"
    # 整数常量
    if re.match(r"^-?\d+$", sql_no_alias):
        return "integer"
    return ""


def _extract_cast_target_type(expr_sql_upper: str) -> str:
    """从 CAST(x AS TYPE) 提取 TYPE（含长度/精度）。"""
    import re
    # cast(x as varchar(50)) / cast(x as bigint)
    m = re.search(r'CAST\s*\([^)]*?\s+AS\s+([A-Z][A-Z0-9\s]*(?:\([^)]*\))?)',
                  expr_sql_upper, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_case_when_branch_fields(lineage: list) -> list:
    """从 case_when 字段的 lineage 提取 THEN/ELSE 分支里的源字段。

    返回 [(src_alias, src_field, step), ...]
    注意：WHEN 条件里的字段不提取（那是判断逻辑，不是输出来源）。
    """
    import sqlglot
    from sqlglot import exp as EXP

    # 从 lineage 的 raw_sql 里拿 case_when 表达式
    raw_sql = ""
    step = ""
    for lin in lineage:
        raw_sql = lin.get("raw_sql") or ""
        step = lin.get("step") or ""
        if raw_sql:
            break
    if not raw_sql:
        return []

    # 去掉 AS alias 后解析
    import re
    expr = re.sub(r'\s+AS\s+\w+\s*$', '', raw_sql, flags=re.IGNORECASE).strip()
    try:
        tree = sqlglot.parse_one(expr, dialect="oracle")
    except Exception:
        return []

    branch_fields = []
    for case_node in tree.find_all(EXP.Case):
        # THEN/ELSE 分支
        for branch in [case_node.args.get("default")] + list(case_node.find_all(EXP.If)):
            pass  # sqlglot Case 结构：whens 是 (this=condition, expression=result) 对
        # 正确遍历：Case 的 whens 是 If 节点列表，每个 If 的 this 是条件，expression 是结果
        for if_node in case_node.args.get("whens", []):
            result = if_node.args.get("expression") or if_node.this
            if result:
                for col in result.find_all(EXP.Column):
                    branch_fields.append((
                        col.table or "",
                        col.name,
                        step,
                    ))
        # ELSE 分支（default）
        default = case_node.args.get("default")
        if default:
            for col in default.find_all(EXP.Column):
                branch_fields.append((
                    col.table or "",
                    col.name,
                    step,
                ))
    return branch_fields


def _resolve_alias_to_table(src_alias: str, lin_step: str, step_alias_map: dict) -> str:
    """别名 → 真实表名。"""
    if not src_alias:
        return ""
    amap = step_alias_map.get(lin_step, {})
    return amap.get(src_alias.upper(), "")


def _lookup_catalog_type(src_table: str, src_field: str, table_catalog: dict) -> str:
    """从 catalog 查源表字段类型。"""
    src_table_key = _norm_table(src_table).lower()
    src_meta = table_catalog.get(src_table_key, {})
    return src_meta.get(src_field.lower(), {}).get("type", "")


def _same_int_family(type1: str, type2: str) -> bool:
    """两个类型是否都是整数家族（int/bigint/smallint/tinyint 等）。

    sqlglot 解析时会把 bigint 标准化成 int，导致 cast(x as bigint) 提取出 int，
    跟 DDL 的 bigint 归一化后不等。但整数互转不丢数据，不该报。
    """
    INT_TYPES = {"int", "integer", "bigint", "smallint", "tinyint"}
    base1 = type1.split("(")[0]
    base2 = type2.split("(")[0]
    return base1 in INT_TYPES and base2 in INT_TYPES


def _normalize_type(type_str: str) -> str:
    """归一化类型字符串用于对比（去空格、统一大小写、去精度内部空格）。"""
    if not type_str:
        return ""
    # 去空格、统一小写
    t = type_str.replace(" ", "").lower()
    return t


def _is_precision_change(type1: str, type2: str) -> bool:
    """判断两个类型是否仅精度/长度不同（类型族相同）。"""
    base1 = type1.split("(")[0]
    base2 = type2.split("(")[0]
    return base1 == base2 and type1 != type2


def detect_load_strategy(rules: list) -> dict:
    """判断资产的加工方式（增量/全量/分区全量）。

    三种分类：
      full        全量（TRUNCATE 整表后重写）
      incremental 增量（DELETE/MERGE/追加，按条件写入或只增不改）
      partition   分区全量（TRUNCATE PARTITION，按分区粒度的全量覆写）

    判断依据：最终目标表相关步骤的 delete_mode。
    交换分区（rule_type=9）的特殊处理：交换分区只是一种写入技术手段
    （为了业务无感/减少不可用时间），不是加工方式的判断依据。
    需要往前推导——看写入临时表（交换源）那一步的 delete_mode。

    Returns: {
        "strategy": "full" | "incremental" | "partition" | "unknown",
        "label": "全量" | "增量" | "分区全量" | "未知",
        "detail": str,              # 判断依据说明
        "delete_mode": str,         # 判断所依据的 delete_mode
        "delete_mode_label": str,   # delete_mode 的中文标签
    }
    """
    if not rules:
        return {"strategy": "unknown", "label": "未知", "detail": "无规则",
                "delete_mode": "", "delete_mode_label": ""}

    # delete_mode → 加工方式映射（三类）
    # 1=TRUNCATE 全量, 2=追加(归入增量), 3/5=分区全量, 4=DELETE增量, 6=MERGE增量
    FULL_MODES = {"1"}
    PARTITION_MODES = {"3", "5"}
    INCREMENTAL_MODES = {"2", "4", "6"}  # 2追加/4DELETE/6MERGE 都归入增量

    # 找最终目标表步骤（max exec_sequence 的非中间表）
    # 跳过 I 视图步骤（rule_code 含 _VIEW，它是视图封装不是加工步骤，无 delete_mode）
    # 如果最后一步是交换分区，往前找写入临时表的步骤
    target_rule = None
    exchange_rule = None
    for rule in reversed(rules):
        # 跳过 I 视图步骤（视图封装，不是真正的加工步骤，不参与加工方式判断）
        if getattr(rule, "is_view_step", False):
            continue
        is_exchange = rule.rule_type == 9 and rule.exchange_source_table
        if is_exchange and not exchange_rule:
            exchange_rule = rule
            continue  # 交换分区不是判断依据，往前找
        if not _is_intermediate_table(rule.target_table):
            target_rule = rule
            break

    # 交换分区场景：往前找写入临时表（exchange 的 target_table）的步骤
    if exchange_rule and not target_rule:
        temp_table = exchange_rule.target_table.lower()
        for rule in reversed(rules):
            if rule.rule_type == 9 or getattr(rule, "is_view_step", False):
                continue  # 跳过交换分区和视图步骤
            if rule.target_table and rule.target_table.lower() == temp_table:
                target_rule = rule
                break

    if not target_rule:
        target_rule = exchange_rule or rules[-1]

    dm = (target_rule.delete_mode or "").strip()
    dm_label = DELETE_MODE_MAP.get(dm, f"delete_mode={dm}" if dm else "未配置")
    dc = (target_rule.delete_condition or "").strip()

    if dm in FULL_MODES:
        strategy, label = "full", "全量"
        detail = f"TRUNCATE TABLE（先清空整表再写入）"
    elif dm in PARTITION_MODES:
        strategy, label = "partition", "分区全量"
        detail = f"分区级覆写（{dm_label}）" + (f"，分区条件：{dc}" if dc else "")
    elif dm in INCREMENTAL_MODES:
        strategy, label = "incremental", "增量"
        if dm == "2":
            detail = f"追加写入（不删只加）"
        elif dm == "6":
            detail = f"MERGE INTO（增量合并/upsert）"
        else:
            detail = f"按条件删除后写入（{dm_label}）" + (f"，删除条件：{dc}" if dc else "")
    else:
        strategy, label = "unknown", "未知"
        detail = f"无法确定加工方式，{dm_label}"

    # 交换分区补充说明（不影响 strategy，只补充 detail）
    if exchange_rule:
        detail += f"，通过交换分区写入（{exchange_rule.exchange_source_table}）"

    return {
        "strategy": strategy,
        "label": label,
        "detail": detail,
        "delete_mode": dm,
        "delete_mode_label": dm_label,
    }


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

def _append_block_summary(lines, blk, idx, indent=1):
    """在 summary 里输出逻辑块的结构信息（供 AI 读）。"""
    prefix = "  " * indent
    role = blk.get("role", "")
    table = blk.get("table", "")
    alias = blk.get("alias", "")
    ops = blk.get("ops", [])
    on_cond = blk.get("on_condition", "")
    join_type = blk.get("join_type", "")
    block_id = f"块{idx+1}"

    desc = f"{prefix}  - {block_id}: {role} {table}"
    if alias:
        desc += f" ({alias})"
    if join_type and join_type != "FROM":
        desc += f" {join_type}"
    if on_cond:
        desc += f" ON {on_cond}"
    if ops:
        desc += f" [{', '.join(ops)}]"
    lines.append(desc)

    # 带出字段
    brought = blk.get("brought_fields", [])
    if brought:
        field_names = [f["name"] if isinstance(f, dict) else f for f in brought]
        lines.append(f"{prefix}    带出: {', '.join(field_names)}")

    # 递归子块
    for cidx, child in enumerate(blk.get("children", [])):
        _append_block_summary(lines, child, cidx, indent + 1)


def _append_block_template(lines, blk, idx, indent=0):
    """在 AI 输出模板里列出块（让 AI 为每个块写目的）。"""
    prefix = "  " * indent
    block_id = f"块{idx+1}"
    table = blk.get("table", "")
    role = blk.get("role", "")
    lines.append(f"{prefix}- {block_id} ({role} {table}): （这个块的业务目的）")

    for cidx, child in enumerate(blk.get("children", [])):
        _append_block_template(lines, child, cidx, indent + 1)


def analyze_pipeline(
    rules: list,
    target_fields: dict,
    group_variables: dict,
    dialect: str,
    *,
    ddl_dir: str = "",
    source_file: str = "",
    rule_group_code: str = "",
) -> dict:
    """Step 3~7 核心解析，返回完整 knowledge dict。

    单条路径（main）和批量路径（batch._process_group）共用此函数，避免「两套逻辑、
    改一处漏一处」。历史 bug：批量路径曾独立复制了这段逻辑，单条路径后续新增的
    Step 5e（data_blocks/structured_summary）、auto_step_desc、DDL 元数据、meta 字段
    等都没同步到批量，导致批量产出的 knowledge 缺数据块、缺步骤描述。

    本函数为纯函数（无 print、不读 args、不写文件），进度输出由调用方负责。

    Args:
        rules: 规则列表（RawRule）
        target_fields: raw["target_fields"]（按规则编码分组的 TargetFields）
        group_variables: raw["group_variables"]（按规则编码分组的组变量）
        dialect: SQL 方言（调用方已决定，main 用 args.dialect，批量用自动检测）
        ddl_dir: DDL 文件目录（可选，为空则跳过 DDL 元数据解析）
        source_file: 源文件名（可选，写入 meta.source_file）
        rule_group_code: 规则组编码（可选，写入 meta.rule_group_code）

    Returns: (knowledge, parsed_map)
        knowledge: 完整 knowledge dict（与原 main 组装结构完全一致）
        parsed_map: {rule_code: ParsedSQL}，供调用方做 _generate_ai_summary 等下游复用，
            避免调用方重复构造 parsed_map（否则又回到两套解析逻辑）。
    """
    # ── Step 3: SQL 解析（分层：SELECT类深度解析，其他记录） ──
    parsed_map = {}
    for rule in rules:
        if rule.rule_type in SELECT_RULE_TYPES and rule.query_sql:
            parsed_map[rule.rule_code] = parse_single_sql(rule.query_sql, dialect)
        elif rule.query_sql:
            # 非 SELECT 类但有 SQL（删数/分区交换等）：记录但不深度解析
            parsed_map[rule.rule_code] = ParsedSQL(raw_sql=rule.query_sql)
        else:
            parsed_map[rule.rule_code] = ParsedSQL(parse_error="空 SQL")

    # ── Step 4: 拓扑构建 ──
    topology = build_topology(rules, parsed_map)

    # ── Step 5: 数据流 ──
    data_flow = build_data_flow(rules, parsed_map)

    # ── Step 5a: 构建多表结构目录（DDL，可选增强，容错）──
    # catalog 含过程表+目标表的 DDL 字段结构，供字段级类型下注(P2)和
    # 跨表一致性检查(P3)使用。DDL 找不到 → catalog 为空 → 字段无类型，不阻塞。
    table_catalog = build_table_catalog(rules, ddl_dir, parsed_map) if ddl_dir else {}

    # ── Step 5b: 字段映射（双源交叉：target_fields + SQL AST + DDL类型下注）──
    field_mappings = build_field_mappings(rules, parsed_map, target_fields, table_catalog)

    # ── Step 5c: 关联键跨步骤追溯 ──
    enrich_join_key_lineage(data_flow, rules, parsed_map, topology, field_mappings)

    # ── Step 5d: 字段物理来源穿透（供 mapping 用）──
    enrich_field_physical_sources(field_mappings, data_flow, rules, parsed_map, topology)

    # ── Step 5e: 结构化步骤概述 + 数据块（步骤卡片的加工逻辑展示）──
    topo_steps = topology.get("steps", [])
    df_steps = data_flow.get("steps", [])
    fields_list = field_mappings.get("fields", [])
    for ts in topo_steps:
        ds = next((s for s in df_steps if s.get("step_id") == ts.get("step_id")), None)
        if ds:
            ds["structured_summary"] = build_structured_step_summary(ts, ds, fields_list)
            rc = ts.get("rule_code", "")
            parsed = parsed_map.get(rc)
            ds["data_blocks"] = build_data_blocks(ts, ds, parsed, fields_list)

    # ── Step 6: 质量分析 ──
    quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)

    # ── Step 6b: 跨表字段类型一致性检查（P3，依赖 DDL catalog）──
    # 同一字段在过程表和目标表类型不一致 → 精度丢失/截断风险
    # DDL 找不到（catalog 为空）→ 不检查，不阻塞
    type_issues = check_type_consistency(field_mappings, table_catalog, rules, parsed_map)
    if type_issues:
        quality["issues"].extend(type_issues)
        # 重新统计 issue_statistics（high 归入 medium 档，因为 statistics 只有4档）
        from collections import Counter as _Counter
        sev_count = _Counter(iss["severity"] for iss in quality["issues"])
        quality["issue_statistics"]["critical"] = sev_count.get("critical", 0)
        quality["issue_statistics"]["medium"] = sev_count.get("medium", 0) + sev_count.get("high", 0)
        quality["issue_statistics"]["low"] = sev_count.get("low", 0)
        quality["issue_statistics"]["info"] = sev_count.get("info", 0)

    # ── Step 7: 组装输出 ──
    # 最终目标表（最大 exec_sequence 的步骤目标表，考虑交换分区）
    target_name = "unknown"
    if rules:
        max_seq_rule = max(rules, key=lambda r: r.exec_sequence)
        if max_seq_rule.rule_type == 9 and max_seq_rule.exchange_source_table:
            target_name = max_seq_rule.exchange_source_table
        else:
            target_name = max_seq_rule.target_table or "unknown"

    # 加工模式标签自动检测
    patterns = detect_patterns(parsed_map, topology)

    # 加工方式判断（增量/全量/分区/追加）
    load_strategy = detect_load_strategy(rules)

    # DDL 字段元数据（从多表 catalog 取目标表的结构，可选增强）
    # catalog 在 Step 5a 已构建（含过程表+目标表），这里只取目标表部分
    target_schema = ""
    target_metadata = {}
    if rules:
        max_seq_r = max(rules, key=lambda r: r.exec_sequence)
        target_schema = max_seq_r.target_schema
    if table_catalog:
        target_full = _normalize_table_name(target_schema, target_name).lower()
        target_metadata = table_catalog.get(target_full, {})

    # 生成兜底 step_descriptions（脚本自动，不依赖 AI）
    scenarios = topology.get("scenarios", [])
    auto_step_desc = []
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code, ParsedSQL())
        desc = generate_step_description(rule, parsed, scenarios, rules)
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        step_id = step["step_id"] if step else ""
        auto_step_desc.append({
            "step_id": step_id,
            "rule_code": rule.rule_code,
            "purpose": desc["purpose"],
            "logic": desc["logic"],
            "is_auto_generated": True,
        })

    knowledge = {
        "meta": {
            "source_type": "execution_tasks.xlsx",
            "source_file": source_file,
            "analysis_time": datetime.now().isoformat(),
            "dialect": dialect,
            "rule_group_code": rule_group_code,
            "total_rules": len(rules),
            "total_target_fields": sum(len(v) for v in target_fields.values()),
            "total_sql_columns": sum(
                len(parsed_map.get(r.rule_code, ParsedSQL()).select_columns)
                for r in rules
            ),
            "target_table": target_name,
            "version": "1.0.0",
            "patterns": patterns,
            "load_strategy": load_strategy,
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
        "source": build_source(rules, target_fields, group_variables, parsed_map),
    }
    return knowledge, parsed_map


