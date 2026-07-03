#!/usr/bin/env python3
"""理解引擎层（engine）— SQL 理解与血缘解析的单一真相。

本模块是 dws-pipeline-analyzer 三层架构的中间层（详见 architecture.md）：
    ① 数据层（analyzer.py）— read_excel / CLI
    ② 理解引擎（engine.py）— 本模块
    ③ 任务层 — 文档化 / 字段检索 / 关联影响分析 / ...

【当前状态：facade 门面层】
    现阶段 engine.py 是 facade——从 analyzer 导入并 re-export 所有引擎符号，
    确立「engine 是引擎模块」的边界。代码仍物理居住在 analyzer.py 中，
    后续按函数族（build_*、parse_*、数据类）渐进物理搬迁到本文件。
    搬迁过程中 analyzer.py 会反向 import 这些符号做兼容，保证零回归。

引擎的职责（确定性解析，无 AI）：
    - analyze_pipeline()   执行规则 → knowledge（过程视角：怎么加工）
    - 数据类                ParsedSQL / RawRule / TableRef ...（领域模型）
    - build_* / enrich_*   拓扑 / 数据流 / 字段映射 / 血缘 / 物理穿透
    - parse_single_sql()   SQL → AST 解析
    - detect_dialect()     方言检测

引擎的边界（铁律）：
    - 纯函数：无 print、不读 args、不写文件（进度输出和文件写入由调用方负责）
    - 只懂单资产：接收一个规则组的数据，不关心批量编排
    - 确定性：血缘解析靠代码精确计算，不依赖 AI

新代码请直接 from engine import xxx。
"""

# ═══════════════════════════════════════════════════════════════
# 引擎入口
# ═══════════════════════════════════════════════════════════════

from analyzer import analyze_pipeline

# ═══════════════════════════════════════════════════════════════
# 数据类（领域模型）
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    RawRule,
    RawTargetField,
    RawGroupVariable,
    ParsedColumn,
    ParsedJoin,
    ParsedCTE,
    ParsedSQL,
    TableRef,
    ColumnRef,
    QueryUnit,
)

# ═══════════════════════════════════════════════════════════════
# SQL 解析
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    detect_dialect,
    parse_single_sql,
    parse_query_unit,
    collect_all_tables,
    collect_all_usage,
    classify_transform,
)

# ═══════════════════════════════════════════════════════════════
# 拓扑 / 数据流 / 字段映射 / 质量
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    build_topology,
    build_data_flow,
    build_field_mappings,
    analyze_quality,
    build_scenarios,
)

# ═══════════════════════════════════════════════════════════════
# 血缘 / 物理穿透
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    build_join_key_lineage,
    enrich_join_key_lineage,
    enrich_field_physical_sources,
)

# ═══════════════════════════════════════════════════════════════
# 步骤卡片 / 数据块 / 步骤描述
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    build_data_blocks,
    build_structured_step_summary,
    generate_step_description,
)

# ═══════════════════════════════════════════════════════════════
# 模式检测 / 源构建 / DDL 元数据
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    detect_patterns,
    build_source,
    parse_ddl_for_metadata,
    parse_ddl_for_types,
)

# ═══════════════════════════════════════════════════════════════
# 跨层共享工具（解析 + 读取共用）
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    _strip_dws_clauses,
    _replace_placeholders,
    _normalize_table_name,
    _norm_table,
    _is_intermediate_table,
    _clean_name,
)

# ═══════════════════════════════════════════════════════════════
# 引擎常量
# ═══════════════════════════════════════════════════════════════

from analyzer import (
    ORACLE_SIGNS,
    DWS_SIGNS,
    LAYER_PATTERNS,
    RULE_TYPE_MAP,
    SELECT_RULE_TYPES,
    RECORD_RULE_TYPES,
    VARIABLE_RULE_TYPES,
    DELETE_MODE_MAP,
    PARTITION_DELETE_MODES,
    TRANSFORM_PRIORITY,
    DELETE_MODE_LABEL,
)
