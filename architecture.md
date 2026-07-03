# 架构设计

> 本文档是 dws-pipeline-analyzer 演进的顶层设计依据。所有代码变更应与本架构对齐。
>
> 状态：2026-07 确立，正在落地中。

---

## 1. 背景：为什么要重新设计架构

### 现状

dws-pipeline-analyzer 最初是一个"制品包文档化工具"——读 execution_tasks.xlsx，生成字段映射、资产说明书、技术设计文档。在这个过程中，它沉淀了一个相当完整的**SQL 理解引擎**（解析/拓扑/字段血缘/物理来源穿透）。

但这个引擎被"文档化任务"包裹着，没有作为独立能力暴露。随着业务需求扩展到**关联影响分析**等新任务，现有架构暴露了三个问题：

| 问题 | 表现 |
|------|------|
| 引擎被任务绑死 | 单条路径和批量路径曾各写一套解析逻辑（Step 3~7），单条更新后批量未同步，导致批量产出缺数据块 |
| 全貌信息缺失 | knowledge 只回答"怎么加工"，不回答"资产是什么"；表定义(DDL)做过解析但没利用起来；调度配置/归属信息散落在 raw 里用完即弃 |
| 输入单一 | 只有 xlsx 一种输入，无法承载表定义、业务背景、变更清单等多源输入 |

### 目标

将理解引擎确立为**显式的能力底座**，使其能服务多个上层任务（文档化、字段检索、关联影响分析、未来任务），而非仅服务于文档化。

---

## 2. 核心设计原则

1. **引擎只懂单资产** — analyze_pipeline 接收一个规则组的数据，产出一份 knowledge。它不需要知道有"批量"这回事，也不关心结果会被哪个任务消费。

2. **所有任务平等消费理解结果** — 文档化、字段检索、影响分析都是引擎之上的任务，任何一个任务的变更不影响其他任务。

3. **确定性解析与 AI 增强分离** — 血缘解析、反向匹配等是确定性逻辑，必须靠代码精确计算，不依赖 AI。AI 只用在非关键增强点（自然语言提取、措辞优化）。

4. **输入多源、各自独立加载** — 执行规则、表定义、业务背景、变更清单是不同类型的输入，各自独立加载、独立演进，不互相耦合。

5. **批量是编排层** — 单资产做好了，批量就是把循环加上。引擎本身不该为批量做特殊设计。

---

## 3. 三层架构

```
┌──────────────────────────────────────────────────────────────┐
│  ③ 任务层（commands/ + 任务模块）                              │
│  每个任务 = 对理解结果的一种消费方式                            │
│                                                              │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────┐      │
│  │ 资产文档化│ │ 字段检索 │ │ 关联影响分析│ │ 未来任务 │      │
│  │ (已有)   │ │ (已有)   │ │ (规划中)   │ │ ...      │      │
│  └────┬─────┘ └────┬─────┘ └─────┬──────┘ └────┬─────┘      │
│       │            │             │             │             │
├───────┴────────────┴─────────────┴─────────────┴─────────────┤
│  ② 理解引擎层（engine.py — 单一真相）                          │
│                                                              │
│  analyze_pipeline()      执行规则 → knowledge（过程视角）     │
│  build_table_catalog()   DDL → table_catalog（表结构画像）    │
│  build_asset_profile()   knowledge+raw+catalog → 全貌（实体） │
│  build_reverse_index()   knowledge → 反向血缘索引             │
│                                                              │
│  数据类：ParsedSQL / RawRule / TableDef / FieldDef ...       │
├──────────────────────────────────────────────────────────────┤
│  ① 数据层（analyzer.py — 数据读取）                            │
│                                                              │
│  read_excel()            xlsx → rules / target_fields / 配置 │
│  load_table_definitions  DDL 目录 → 原始表定义（供引擎解析）  │
└──────────────────────────────────────────────────────────────┘

   ④ 编排层（batch.py — 独立于引擎）
   循环调 analyze_pipeline，拼多个 knowledge
```

---

## 4. 理解引擎层（engine.py）详解

### 4.1 定位

理解引擎是整个系统的**单一真相**。所有上层任务对资产的"理解"都来自这里，不允许任何任务自己重新解析 SQL。

引擎是**纯函数**模块：无 print、不读 args、不写文件。进度输出和文件写入由调用方负责。

### 4.2 对外接口

| 函数 | 输入 | 输出 | 视角 |
|------|------|------|------|
| `analyze_pipeline()` | rules, target_fields, group_variables, dialect | (knowledge, parsed_map) | 过程：怎么加工 |
| `build_table_catalog()` | ddl_dir / ddl文本, 表名清单 | table_catalog | 实体：表结构 |
| `build_asset_profile()` | knowledge, raw, catalog | asset_profile | 实体：资产全貌 |
| `build_reverse_index()` | knowledge | {源表.字段 → 影响项} | 查询：反向血缘 |

### 4.3 knowledge 的定位（过程视角，保持不变）

knowledge 回答**"这个资产怎么加工数据"**：

```
knowledge = {
  meta:          目标表/规则数/方言/模式标签
  topology:      调度图/数据依赖/场景
  data_flow:     表/步骤/数据块/关联键追溯/结构化概述
  field_mappings: 字段映射/血缘/物理穿透
  quality:       质量问题
  business_logic: 步骤描述（自动生成）
  source:        源表/TargetFields/组变量
}
```

knowledge 的结构已经成熟（200+ 测试覆盖），**不往里塞实体信息**，避免破坏稳定性。实体维度的信息由 asset_profile 承担。

### 4.4 asset_profile 的定位（实体视角，新增）

asset_profile 回答**"这个资产是什么"**——所有任务的共同入口，关联影响分析的地基：

```
asset_profile = {
  identity: {            归属
    rule_group_code, rule_group_en,
    project_code, business_owner
  },
  schedule: {            调度
    exec_sequence 链,
    delete_mode / delete_condition,
    exchange_source_table（分区交换）,
    variables（平台变量）
  },
  structure: {           目标表完整结构
    target_table,
    fields: [{name, type, comment, nullable, ...}]（从DDL）
  },
  dependencies: {        依赖清单（从knowledge提炼）
    source_tables: [表名清单],
    source_fields: [{table, field, → 哪些目标字段}]（实体化依赖）
  }
}
```

**落地策略**：先在 engine.py 建骨架函数（从 knowledge + raw 提炼现有可得的字段），DDL 增强和完整结构后续填充。

---

## 5. 数据层（analyzer.py）详解

analyzer.py 瘦身为**数据读取 + CLI**：

| 职责 | 函数 |
|------|------|
| 读取制品包 Excel | `read_excel()` → rules / target_fields / group_variables / 平台配置 |
| CLI 入口 | `main()` — 组装参数、调 engine、写文件、输出进度 |
| 诊断输出 | Excel 格式异常时的诊断信息（现有逻辑保留） |
| 兼容层 | re-export engine 的符号（过渡期，保证现有 import 不破） |

### re-export 兼容策略

engine.py 抽离后，analyzer.py 末尾保留：

```python
# engine 已独立，以下 re-export 保证现有代码 `from analyzer import xxx` 不破。
# 新代码请直接 from engine import xxx。
from engine import (analyze_pipeline, build_topology, ParsedSQL, RawRule, ...)
```

现有 batch.py / field_search.py / 全部测试的 import 无需改动。稳定后逐步迁移到直接 import engine。

---

## 6. 任务层详解

### 6.1 资产文档化（已有）

```
输入: execution_tasks.xlsx (+ DDL目录 + 业务背景)
流程: read_excel → analyze_pipeline → [AI增强] → view_generator
产出: mapping.xlsx / asset_report.html / tech_design.md
```

### 6.2 关联影响分析（规划中，本次架构之后实施）

```
输入: execution_tasks.xlsx + 变更清单（结构化 + 自然语言）
流程: 
  1. 理解资产: analyze_pipeline + build_asset_profile
  2. 解析变更: 结构化机器解析 + 自然语言AI提取 → ChangeItem[]
  3. 反向匹配: build_reverse_index → 受影响的资产字段/步骤
  4. 影响分级: 按字段使用角色(JOIN键/WHERE/SELECT引用)分级
产出: 影响报告（HTML + JSON）
```

**一期范围**: 单资产 + 含自然语言变更。批量（多资产）等单资产验证 OK 后再加编排循环。

### 6.3 字段检索（已有，无需改动）

field_search 已在复用 engine 的解析能力，保持现状。

---

## 7. 输入层设计

不同任务接收不同输入，由各自的 command 工作流自包含处理：

| 输入类型 | 来源 | 加载方式 | 消费者 |
|----------|------|----------|--------|
| 执行规则 | execution_tasks.xlsx | read_excel() | 所有任务 |
| 表定义 | DDL目录 / *.sql | build_table_catalog() | 文档化、影响分析 |
| 业务背景 | md / wiki / 人工填写 | 任务层直接读取（不进引擎） | 文档化增强、影响分析增强 |
| 变更清单 | yaml / json / 自然语言 | impact_analyzer 专属 | 影响分析 |

**原则**: 业务背景是语义增强，不进 engine.py，由任务层按需消费。

---

## 8. 代码组织

```
dws-pipeline-analyzer/
├── SKILL.md              能力底座描述（触发条件 + 通用能力，工作流下沉到command）
├── architecture.md       本文档
├── references/
│   ├── engine.py         ★ 理解引擎（数据类 + analyze_pipeline + build_*）
│   ├── analyzer.py       数据读取 + CLI + re-export兼容层
│   ├── batch.py          批量编排（循环调engine）
│   ├── view_generator.py 文档化渲染
│   ├── field_search.py   字段检索（复用engine）
│   └── impact_analyzer.py 关联影响分析（后续新增）
├── run.py                脚本调度器（dispatch到references/*.py）

commands/（每个command = 一个完整任务工作流）
├── analyze.md            资产文档化工作流
├── analyze-batch.md      批量文档化
├── field-search.md       字段检索
└── impact-analysis.md    关联影响分析（后续新增）
```

---

## 9. 落地路线图

### 阶段一：地基重构（当前）

目标：把理解引擎显式化，确立模块级边界，零回归。

**实施策略**：facade 先行 + 一次性物理搬迁。

| 步骤 | 内容 | 状态 |
|------|------|------|
| 1 | 建 engine.py facade（确立模块边界） | ✅ 已完成 |
| 2 | facade 回归测试（符号同源 + 无循环依赖） | ✅ 已完成 |
| 3 | **物理搬迁**：引擎代码（5185行）从 analyzer 搬入 engine | ✅ 已完成 |
| 4 | analyzer.py 瘦身至 701 行（read_excel + main + re-export） | ✅ 已完成 |
| 5 | 验证零回归（217 测试全过） | ✅ 已完成 |

**搬迁成果**：
- `engine.py`（5185 行）：理解引擎真实代码，纯函数、单向无本地依赖
- `analyzer.py`（701 行）：数据层（read_excel）+ CLI（main）+ re-export 兼容层
- `from engine import xxx`：新代码推荐写法
- `from analyzer import xxx`：现有代码继续可用（re-export 兼容）
- 单向依赖：analyzer → engine，engine 不 import analyzer

### 阶段二：资产全貌 + 表定义（待讨论清楚后）

| 步骤 | 内容 |
|------|------|
| 1 | build_table_catalog()：增强 DDL 解析（源表+目标表，完整结构） |
| 2 | build_asset_profile()：从 knowledge + raw + catalog 提炼资产全貌 |
| 3 | build_reverse_index()：反向血缘索引 |

### 阶段三：关联影响分析

| 步骤 | 内容 |
|------|------|
| 1 | impact_analyzer.py：变更解析（结构化 + AI自然语言提取） |
| 2 | 反向匹配 + 影响分级 |
| 3 | 影响报告渲染 |
| 4 | impact-analysis.md command |

---

## 10. 设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| engine 边界 | 模块级（engine.py），含数据类 | 数据类是引擎领域模型，应随引擎走 |
| 现有 import | re-export 兼容层 | 零回归，过渡期安全 |
| knowledge 定位 | 过程视角，不膨胀 | 结构已成熟，实体信息由 asset_profile 承担 |
| 全貌结构 | asset_profile 独立于 knowledge | 过程视角与实体视角分离，互不污染 |
| 业务背景 | 不进引擎 | 语义增强，非确定性解析产物 |
| 批量 | 编排层，一期不做 | 单资产做好后水到渠成 |
| 影响分析一期 | 单资产 + 含自然语言 | 先验证核心引擎，批量后续加 |
| 目标表逻辑 | 统一 max(exec_sequence) | 单条批量共用，消除歧义 |
