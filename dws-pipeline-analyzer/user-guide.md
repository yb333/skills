# DWS 制品包分析器 — 用户指南

> 从执行平台导出的制品包（execution_tasks.xlsx）中，自动提取 ETL 知识并生成文档。

---

## 这是什么

你手上有一个从执行平台导出的 Excel 文件（`execution_tasks.xlsx`），里面包含了 ETL 的表结构、字段映射、SQL 逻辑。

这个工具能帮你：

- **理解这个 ETL 是干什么的**（数据从哪来、怎么加工、写到哪去）
- **生成字段映射表**（mapping.xlsx，给业务/开发看）
- **生成交互式报告**（asset_report.html，给领导/新人看）
- **生成技术设计文档**（tech_design.md，给接手的人看）

全部自动，不需要手写。

---

## 快速开始

### 方式 1：用命令（推荐）

在 AI 对话中输入：

```
/analyze @execution_tasks.xlsx
```

或者指定完整路径：

```
/analyze docs/output/dwl_con_pu_any_f/09_export/execution_tasks.xlsx
```

### 方式 2：自然语言触发

直接把你的需求说出来，AI 会自动识别并触发：

| 你说的话 | AI 会怎么做 |
|---------|-----------|
| "分析这个制品包" | 全自动分析 + 生成全部文档 |
| "帮我看看这个ETL" | 分析 + 生成报告 |
| "这个表是干什么的？" | 分析 + 业务逻辑说明 |
| "生成mapping文件" | 分析 + 输出 mapping.xlsx |
| "出个技术设计文档" | 分析 + 输出 tech_design.md |
| "字段血缘是什么" | 分析 + HTML 报告的血缘图 |

### 方式 3：拖拽文件

把 `execution_tasks.xlsx` 拖到对话框，然后说"分析一下"。

---

## 输入要求

### 必须有

| 文件 | 说明 |
|------|------|
| `execution_tasks.xlsx` | 执行平台导出的制品包，至少包含 RULE、TargetFields 两个 sheet |

### 可选（有更好）

| 文件 | 说明 | 作用 |
|------|------|------|
| DDL 文件（`*.sql`） | 用 `--ddl-dir` 指定目录 | 补充字段类型和中文名（mapping 里会用到） |

DDL 文件用 `--ddl-dir` 指定一个目录（里面放 `*.sql`），工具会扫描匹配目标表。

---

## 输出产物

分析完成后，在 `--output` 指定的**基础目录**下，按**规则组英文名称**（取自 Excel「规则组英文名称」列）建子目录，所有产物输出到那里：

```
{base_dir}/
└── {规则组英文名称}/
    ├── knowledge_draft.json    # 结构化知识（脚本产出，事实层）
    ├── knowledge_summary.md    # 摘要（2-4KB，供 AI 读取补充业务理解）
    ├── knowledge_ai.md         # AI 增强后的自然语言（可选，由 AI 产出）
    ├── mapping.xlsx            # 字段映射
    ├── asset_report.html       # 资产说明书
    └── tech_design.md          # 技术设计文档
```

> `--output` 只需给基础目录（如当前工作目录或 `docs/`），脚本自动在其下按规则组英文名建子目录。规则组英文名缺失时回退到规则组编码。
>
> AI 增强结果保存在 `knowledge_ai.md`，通过 view_generator 的 `--ai-input` 参数注入，与 `knowledge_draft.json` 合并后生成视图，**不会单独落盘成一个 final 文件**。

### mapping.xlsx — 字段映射

给业务和开发看的字段对照表。

**实体级 mapping**（Sheet 1）：物理源表 → 目标表的关系
- 包含 CTE 内部的物理源表（不遗漏任何数据来源）
- 标注 JOIN 类型和关联条件
- CTE 内部表标注归属（如"CTE afr_inv 主表"）

**属性级 mapping**（Sheet 2）：物理源表字段 → 目标表字段
- CTE 穿透：不显示 CTE 别名，直接显示物理源表的原始字段
- 字段类型和中文名以 DDL 为准（DDL 没有则留空）
- 过滤纯视图步骤（CREATE VIEW），目标字段不重复

### asset_report.html — 资产说明书

给领导和新手看的交互式报告。浏览器打开即可，不需要安装任何软件。

**7 个 section**：

| Section | 内容 | 默认状态 |
|---------|------|---------|
| Header | 表名、schema、中文名 | 展开 |
| 资产概览 | 4 个指标卡片 + 加工模式标签 + 一句话定位 | 展开 |
| 数据流向图 | 分层 SVG 血缘图（源表→CTE→步骤→目标表） | 展开 |
| 目标表结构 | 字段表格（类型/业务含义/加工类型），可排序 | 可折叠 |
| 加工逻辑详情 | 按步骤展开，含业务逻辑描述 + SQL 语法高亮 | 可折叠 |
| 字段映射 | 可搜索列表，点击字段弹出 CTE 穿透血缘链 | 可折叠 |
| 质量评估 | 问题按类别分组，AI 洞察只显示有问题的 | 可折叠 |

**血缘图怎么读**：
- 从左到右分层：物理源表 → CTE → 步骤 → 目标表 → 下游视图
- 不同颜色代表不同类型（蓝=源表、橙=CTE、紫=步骤、绿=目标表）
- 鼠标悬停节点显示完整名称
- 复杂场景支持滚动和缩放

**字段详情面板**：
点击目标表结构或字段映射里的任意字段，右侧弹出详情面板，展示：
- 加工类型和业务含义
- CTE 穿透血缘链（目标字段 → CTE → 物理源表字段）
- 加工表达式（SQL 片段）

### tech_design.md — 技术设计文档

给接手维护的人看的 Markdown 文档，9 个章节：

1. 概述（表名/步骤数/源表数/字段数）
2. 复杂度分析（JOIN 数/CTE 数/CASE WHEN 分支数/转换类型分布）
3. 分段策略（每个步骤的源表/写入模式/并行串行关系）
4. 表级血缘（Mermaid 图，含 CTE 内部物理表）
5. 字段映射对照表（穿透 CTE 到物理源表字段）
6. 数据处理逻辑（每步的业务逻辑 + 完整 SQL）
7. 质量评估（问题列表 + AI 建议）
8. 上游任务依赖（源表/别名/关联方式/CTE 归属）
9. 执行平台配置

---

## 常见场景

### 场景 1：接手一个不熟悉的 ETL

```
/analyze @execution_tasks.xlsx
```

看 `asset_report.html` 的「资产概览」和「加工逻辑详情」，30 秒理解全貌。

### 场景 2：给业务出字段映射

```
/analyze @execution_tasks.xlsx
```

把 `mapping.xlsx` 发给业务，他们能看到每个字段的数据来源。

### 场景 3：优化前现状分析

```
这个ETL有什么问题？优化前先分析一下
/analyze @execution_tasks.xlsx
```

看「质量评估」section 的问题列表和 AI 洞察。

### 场景 4：存量资产盘点

批量分析多个制品包，用 `tech_design.md` 做资产文档化。

---

## 技术细节

### 支持的 SQL 构造

| 构造 | 支持情况 |
|------|---------|
| SELECT / WITH...SELECT | 支持 |
| UNION / UNION ALL / INTERSECT / EXCEPT | 支持 |
| CTE（单层 / 嵌套） | 支持（穿透传播 + 递归） |
| JOIN（LEFT/INNER/FULL/CROSS） | 支持 |
| 窗口函数（ROW_NUMBER/LAG/LEAD/SUM OVER） | 支持 |
| 行转列（SUM(CASE WHEN...)） | 支持 |
| COALESCE / NVL 兜底 | 支持 |
| 审计字段推断（'N'→del_flag 等） | 支持 |
| 注释别名（/* field_name */） | 支持 |
| Oracle 方言（NVL/DECODE） | 支持（自动检测方言） |

### 加工模式自动检测

分析脚本会自动检测并标注以下模式：

- CTE 预聚合 / CTE 预处理
- 行转列
- 窗口函数
- 聚合汇总
- NULL 兼容
- 增量去重（NOT EXISTS 自引用）
- NOT EXISTS 排除
- 审计字段
- SCD2 取最新
- 多步骤串行 / 并行

### 字段加工类型（transform_type）

| 类型 | 含义 | 优先级 |
|------|------|--------|
| value | 赋值（字面量/变量） | 0 |
| direct | 直接取字段 | 1 |
| expression | 表达式加工 | 2 |
| fallback | NULL 兜底 | 3 |
| case_when | 条件加工 | 4 |
| aggregate | 聚合 | 5 |
| pivot | 行转列 | 6 |
| window | 窗口函数 | 7 |

优先级高的覆盖低的。例如 CTE 内做了 SUM 聚合，主查询直接引用 CTE 字段，穿透后 transform_type 从 direct 升级为 aggregate。

---

## FAQ

**Q: 没有 DDL 文件能用吗？**

可以。DDL 是可选的，没有 DDL 时字段类型和中文名留空，不影响分析。

**Q: SQL 里有 CTE，mapping 能穿透吗？**

可以。mapping.xlsx 的属性级 mapping 会穿透 CTE，直接显示物理源表的原始字段。例如 `inv_tol_amt_usd` 的来源不是 CTE 别名 `im_agg`，而是物理表 `dwl_inv_mtr_i` 的字段 `inv_inst_amt_usd`。

**Q: 支持多步骤的 ETL 吗？**

支持。每个步骤单独分析，步骤间的依赖关系（串行/并行）在拓扑图和报告里展示。

**Q: 在其他 AI agent 上能用吗？**

能。这个 skill 基于 opencode 标准，把 `.opencode/skills/dws-pipeline-analyzer/` 目录和 `.opencode/commands/analyze.md` 复制到目标项目，确保 `run.py` 入口脚本与 `references/` 同级即可。

**Q: 分析一个 ETL 需要多长时间？**

通常 1-2 分钟（脚本分析 + AI 增强 + 视图生成）。
