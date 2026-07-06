# DWS Pipeline Analyzer

从执行平台制品包（execution_tasks.xlsx）反向提取 ETL 知识，自动生成字段映射、资产说明书、技术设计文档。支持单资产深度分析、批量资产文档化、字段使用检索。

## 能力总览

| 能力 | 命令 | 适用场景 |
|------|------|----------|
| 资产文档化 | `/analyze` | 单个规则组：生成 mapping + 资产说明书 + 技术设计文档 |
| 批量文档化 | `/analyze-batch` | 多个规则组：循环分析，每个规则组出三件套 |
| 字段使用检索 | `/field-search` | 搜字段在多个表里的用法，输出 Excel |

## 安装

```bash
# macOS / Linux
bash install.sh

# Windows
install.bat
```

也可只装本 skill：`bash install.sh dws-pipeline-analyzer`

### 依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | Windows 用 `python`，macOS/Linux 用 `python3` |
| openpyxl 3.1+ | Excel 读写 |
| sqlglot 23.0+ | SQL AST 解析 |

> 首次运行脚本时自动检测，缺失会提示 `pip install openpyxl sqlglot`。

---

## 快速开始

在 AI 对话中输入命令即可，AI 会自动调用脚本并生成文档：

### 单个资产分析

```
/analyze @execution_tasks.xlsx
```

或自然语言：
- "分析这个制品包" / "帮我看看这个 ETL"
- "这个表是干什么的？"
- "生成 mapping 文件"

### 批量分析

```
/analyze-batch @execution_tasks.xlsx
```

### 字段搜索

```
/field-search @execution_tasks.xlsx amount
/field-search @execution_tasks.xlsx amount,user_id
```

---

## 输入说明

### 必需输入

| 文件 | 说明 |
|------|------|
| `execution_tasks.xlsx` | 执行平台导出的制品包，至少含 RULE、TargetFields 两个 sheet |

制品包里有什么（工具会自动提取）：

| Sheet | 内容 | 用途 |
|-------|------|------|
| RULE | 规则编码、规则类型、执行顺序、目标表、SQL | 加工逻辑 |
| TargetFields | 目标表字段定义 | 字段映射双源交叉 |
| GroupVariables | 组变量 | 调度参数 |
| VARIABLES | 全局变量 | 平台变量 |

### 可选输入

| 文件 | 位置 | 作用 |
|------|------|------|
| DDL 文件（`*.sql`） | `execution_tasks.xlsx` 同级的 `04_ddl/` 目录 | 补充字段类型+中文名 |

> DDL 是可选的。没有 DDL 时字段类型和中文名留空，不影响分析。

---

## 输出产物

所有产物输出到 `--output` 指定目录下，按**规则组英文名**建子目录：

```
{output_dir}/
└── {规则组英文名}/
    ├── knowledge_draft.json    # 结构化知识（脚本产出，事实层）
    ├── knowledge_summary.md    # 摘要（2-4KB，供 AI 读取补充业务理解）
    ├── knowledge_ai.md         # AI 增强的自然语言（可选，AI 产出）
    ├── mapping.xlsx            # 字段映射（实体级 + 属性级，CTE 穿透）
    ├── asset_report.html       # 资产说明书（7 section 交互式 HTML）
    └── tech_design.md          # 技术设计文档（9 章节 Markdown）

# 批量分析额外产出：
{output_dir}/batch_logs/        # 每批详细日志（逐组 [OK]/错误）
    └── batch_1.log
```

### mapping.xlsx — 字段映射

给业务和开发看的字段对照表。

- **实体级 mapping**（Sheet 1）：物理源表 → 目标表，含 JOIN 类型和关联条件，CTE 内部物理表不遗漏
- **属性级 mapping**（Sheet 2）：物理源表字段 → 目标表字段，CTE 穿透到物理源表原始字段

### asset_report.html — 资产说明书

给领导和新手看的交互式报告，浏览器打开即可。

7 个 section：资产概览（指标卡片+加工模式）/ 数据流向图（SVG 血缘图）/ 目标表结构 / 加工逻辑详情（含数据块） / 字段映射（CTE 穿透血缘链） / 质量评估。

### tech_design.md — 技术设计文档

给接手维护的人看，9 章节：概述 / 复杂度分析 / 分段策略 / 表级血缘（Mermaid 图）/ 字段映射对照表 / 数据处理逻辑 / 质量评估 / 上游任务依赖 / 执行平台配置。

---

## 各能力详解

### 1. 资产文档化（/analyze）

对单个规则组做深度分析 + 生成三件套。

```bash
python {skill_dir}/run.py analyzer \
    --input execution_tasks.xlsx \
    --output docs/ \
    [--dialect auto] \
    [--ddl-dir 04_ddl/]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | execution_tasks.xlsx 路径 |
| `--output` | 是 | 输出基础目录 |
| `--dialect` | 否 | oracle/dws/auto，默认自动检测 |
| `--ddl-dir` | 否 | DDL 目录（同级 `04_ddl/` 会自动检测） |

**工作流程**：
1. 脚本分析：读 Excel → 解析 SQL → 构建拓扑/数据流/字段血缘 → 产出 knowledge_draft.json
2. AI 增强：AI 读摘要补充业务理解，保存 knowledge_ai.md
3. 生成视图：合并 knowledge + AI 增强，生成 mapping/report/techdoc

### 2. 批量文档化（/analyze-batch）

对含多个规则组的 Excel 批量分析，每个规则组生成三件套。

```bash
python {skill_dir}/run.py batch \
    --input execution_tasks.xlsx \
    --output docs/ \
    [--batch-size 20] \
    [--no-ai] \
    [--ddl-dir 04_ddl/]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | 含多个规则组的 xlsx |
| `--output` | 是 | 输出基础目录 |
| `--batch-size` | 否 | 每批处理数量（默认 20）。复杂 SQL 可调小到 10 |
| `--no-ai` | 否 | 跳过 AI 增强（只生成脚本产物，速度快） |
| `--ddl-dir` | 否 | DDL 目录（可选） |

**批量处理的关键机制**：

| 机制 | 说明 |
|------|------|
| 子进程隔离 | 每批在独立子进程执行，退出即归还内存，避免大批量内存超限 |
| 同名去重 | 规则组英文名相同但编码不同时（如实时区/离线区），目录名追加编码防互相覆盖 |
| 单组隔离 | 单个规则组处理崩溃不拖垮同批其他组 |
| stdout 精简 | stdout 只输出批次级进度，逐组详细写 batch_logs/（避免 stdout 累积超限被杀） |

> 批量分析的解析逻辑与单条完全一致（共用 `analyze_pipeline`），不会有内容缺失。

### 3. 字段使用检索（/field-search）

搜索关键字在多个规则组（目标表）里的使用情况，输出 Excel。

```bash
python {skill_dir}/run.py field_search \
    --input execution_tasks.xlsx \
    --keyword amount,user_id \
    --output field_usage.xlsx
```

输出 Excel 列说明：

| 列 | 说明 |
|----|------|
| 目标表 | schema.table |
| 字段名 | 匹配到的字段 |
| 字段角色 | 写入目标表 / 临时过程使用 / 辅助字段 |
| 字段情况 | 直取 / 加工 / 关联键 / 过滤条件 |
| 最初来源 | 物理源表.字段（写入字段）或使用步骤（辅助字段）|
| 详情 | 加工表达式 / 关联条件 / 过滤条件 |

---

## 常见场景

### 接手不熟悉的 ETL

```
/analyze @execution_tasks.xlsx
```
看 asset_report.html 的「资产概览」和「加工逻辑详情」。

### 给业务出字段映射

```
/analyze @execution_tasks.xlsx
```
把 mapping.xlsx 发给业务。

### 批量资产盘点

```
/analyze-batch @execution_tasks.xlsx
```
每个规则组生成 tech_design.md，用于存量文档化。

### 排查字段来源

```
/field-search @execution_tasks.xlsx amount
```
看 amount 字段在哪些表里、怎么用的、最初来源是哪。

### 批量分析某个组失败了

读 `{output}/batch_logs/batch_N.log`，里面有该组逐组详细状态和错误信息。

---

## 技术细节

### 支持的 SQL 构造

| 构造 | 支持情况 |
|------|---------|
| SELECT / WITH...SELECT | ✓ |
| UNION / UNION ALL / INTERSECT / EXCEPT | ✓ |
| CTE（单层 / 嵌套 / 递归穿透） | ✓ |
| JOIN（LEFT/INNER/FULL/CROSS/子查询） | ✓ |
| 窗口函数（ROW_NUMBER/LAG/LEAD/SUM OVER） | ✓ |
| 行转列（SUM(CASE WHEN...)） | ✓ |
| Oracle 方言（NVL/DECODE） | ✓（自动检测） |
| 注释别名（/* field_name */） | ✓ |
| 审计字段推断（'N'→del_flag 等） | ✓ |

### 加工模式自动检测

CTE 预聚合 / 行转列 / 窗口函数 / 聚合汇总 / NULL 兼容 / 增量去重 / NOT EXISTS 排除 / 审计字段 / SCD2 取最新 / 多步骤串行并行。

### 字段加工类型（transform_type）

value(赋值) < direct(直取) < expression(表达式) < fallback(兜底) < case_when(条件) < aggregate(聚合) < pivot(行转列) < window(窗口)。优先级高的覆盖低的（CTE 穿透时升级）。

### 三层架构

详见 [architecture.md](../architecture.md)。

```
① 数据层（analyzer.py）— read_excel / CLI
② 理解引擎（engine.py）— SQL 解析 / 字段血缘 / 物理穿透（单一真相）
③ 任务层 — 文档化(view_generator) / 字段检索(field_search) / 批量(batch)
```

### 目录结构

```
dws-pipeline-analyzer/
├── SKILL.md              Skill 定义（AI 触发条件 + 能力描述）
├── architecture.md       架构设计文档
├── run.py                脚本调度器（统一入口）
├── references/
│   ├── engine.py         理解引擎（SQL解析/拓扑/血缘/物理穿透）
│   ├── analyzer.py       数据层（read_excel + CLI）
│   ├── batch.py          批量编排
│   ├── view_generator.py 文档渲染
│   ├── field_search.py   字段检索
│   └── templates/        HTML 报告模板
└── commands/             命令定义（/analyze 等）
```

---

## FAQ

**Q: 没有 DDL 文件能用吗？**
能。DDL 是可选增强，没有时字段类型和中文名留空，分析照常进行。

**Q: SQL 里有 CTE，mapping 能穿透吗？**
能。属性级 mapping 穿透 CTE，直接显示物理源表原始字段。

**Q: 批量分析时某组没生成文件夹？**
读 `batch_logs/batch_N.log` 查看该组的错误信息。单组崩溃不会影响其他组。

**Q: 批量分析规则组英文名相同（实时区/离线区）？**
目录名会自动追加规则组编码去重（如 `DWB_SAME_F__RT001`），不会互相覆盖。

**Q: 支持多步骤 ETL 吗？**
支持。步骤间依赖关系（串行/并行）在拓扑图和报告里展示。

**Q: 在其他 AI agent 上能用吗？**
能。基于 opencode 标准，复制 skill 目录 + 命令文件 + 装依赖即可。
