---
name: dws-pipeline-analyzer
description: |-
  DWS ETL 制品包分析器。从执行平台制品包（execution_tasks.xlsx）反向提取完整 ETL 知识，
  自动生成知识文档、字段映射、资产说明书和技术设计文档。

  Use proactively when:
  - 用户提供了 execution_tasks.xlsx（或提及"制品包""交付件""导出包"）
  - 用户说"分析这个表""这个ETL是干什么的""帮我理解这个逻辑"
  - 用户说"生成mapping""出个报告""文档化""资产盘点"
  - 用户说"这个表的数据从哪来""字段血缘""数据流向"
  - 用户拖入 xlsx 文件并询问关于 ETL 的问题
  - 优化/改造前的现状分析
  - 存量资产文档化

  Examples:
  - user: "分析这个制品包" → 全自动分析+生成三视图
  - user: "帮我看看这个ETL" → 分析+资产说明书
  - user: "这个表是干什么的？" → 分析+业务逻辑说明
  - user: "生成mapping文件" → 分析+mapping.xlsx
  - user: "出个技术设计文档" → 分析+tech_design.md
---

# DWS ETL 制品包分析器

## 依赖检查（首次执行前）

脚本依赖 `openpyxl` 和 `sqlglot`。首次运行时自动检测，缺失则自动安装：

```bash
pip install openpyxl sqlglot
```

AI 执行脚本前，先检测依赖是否可用：
```bash
python -c "import openpyxl, sqlglot; print('OK')"
```
如果报 ImportError，运行 `pip install openpyxl sqlglot` 后重试。

> **注意**：本文档统一用 `python`。Windows 直接用 `python`；macOS/Linux 如果只有 `python3`，请用 `python3` 替代。

---

## 工作流程（2 步全自动）

### Step 1: 分析 + AI 增强 + 生成全部视图

#### 1a. 执行分析脚本

**核心分析脚本是 `references/analyzer.py`**，通过 `run.py` 分发器调用：

```bash
python {skill_dir}/run.py analyze \
    --input {input_xlsx} \
    --output {output_dir} \
    [--ddl-dir {ddl_dir}]
```

如果已安装 dws-run（opencode 平台），也可以用 `dws-run analyzer analyze ...`，效果一样。

DDL 目录自动检测：同级的 `04_ddl/` 有则传入，没有则跳过。

输出：`knowledge_draft.json`

#### 1b. AI 补充业务理解

读取 `knowledge_draft.json`，补充：

**business_logic（L4）**：
- `summary`：整体概述（2-3 句话）
- `step_descriptions`：每个步骤的业务目的和加工逻辑
- `key_transforms`：关键字段的业务含义（SQL 翻译成人话）

**quality.ai_insights（L5 AI 部分）**：
- 只补充有实际问题的洞察（severity 非 info）

保存为 `knowledge_final.json`。

#### 1c. 生成全部视图

```bash
python {skill_dir}/run.py view_generator \
    --input knowledge_final.json \
    --output {output_dir} \
    --views all
```

自动生成 3 个视图，不询问用户选哪些。

### Step 2: 报告结果

```
分析完成！

目标表：{target_table}
步骤数：{steps} | 字段数：{fields} | 源表数：{sources}
加工模式：{patterns}

已生成：
- mapping.xlsx       — 字段映射
- asset_report.html  — 资产说明书
- tech_design.md     — 技术设计文档

路径：{output_dir}/analyzer/views/
```

## 三视图说明

| 视图 | 文件 | 给谁看 |
|------|------|--------|
| 字段映射 | mapping.xlsx | 业务/开发（实体级+属性级，CTE 穿透到物理源表） |
| 资产说明书 | asset_report.html | 领导/新人（7 section 交互式 HTML） |
| 技术设计文档 | tech_design.md | 接手维护（9 章节 Markdown） |

## 核心能力

- **CTE 穿透传播**：主查询引用 CTE 字段时，自动穿透加工逻辑（transform_type 升级）
- **UNION ALL 支持**：集合操作多分支全部解析
- **加工模式检测**：自动标注 CTE 预聚合/行转列/SCD2/增量去重等 7-8 个模式
- **DDL 元数据**：字段类型+中文名从 DDL COMMENT 提取
- **双源交叉验证**：TargetFields vs SQL AST 不一致预警
- **双图模型**：调度图（平台配置）vs 数据依赖图（SQL 推导），差异 = 优化空间

## 命令参数

### analyze

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | execution_tasks.xlsx 路径 |
| `--output` | 是 | 输出目录 |
| `--dialect` | 否 | oracle/dws/auto，默认自动检测 |
| `--ddl-dir` | 否 | DDL 目录（补充字段类型和注释） |

### view_generator

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | knowledge_final.json 路径 |
| `--output` | 是 | 输出目录 |
| `--views` | 否 | mapping/asset/techspec，默认 all |

## 禁止事项

- 修改 L1-L3 的任何事实数据（脚本产出）
- 跳过脚本直接用 AI 分析 SQL
