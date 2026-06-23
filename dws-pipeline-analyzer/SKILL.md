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

脚本依赖 `openpyxl` 和 `sqlglot`。run.py 启动时会自检依赖，缺失时友好提示安装（不会抛 ImportError 堆栈）。

AI 执行脚本前，可先检测依赖是否可用：
```bash
python -c "import openpyxl, sqlglot; print('OK')"
```
如果报 ImportError，运行 `pip install openpyxl sqlglot` 后重试。

> **安装方式说明**：
> - 用 `install.sh` / `install.bat` 安装：会创建 venv 并装依赖（偏好隔离环境）
> - 手工复制 skill 目录：只要系统 python 装好依赖即可，run.py 会自检提示
> - 本文档统一用 `python`。Windows 直接用 `python`；macOS/Linux 如果只有 `python3`，请用 `python3` 替代。

---

## 工作流程

> **路径约定**：`--output` 指定**基础目录**（工作目录或 `docs/` 等）。脚本会自动在其下按
> **规则组英文名称**（取自 Excel「规则组英文名称」列，缺失则回退规则组编码）建子目录。
> 下文 `{output_dir}` 代指该自动创建的子目录，从 Step 1 输出日志读取真实路径。

### Step 1: 执行分析脚本

```bash
python {skill_dir}/run.py analyzer --input {input_xlsx} --output {base_dir} [--ddl-dir {ddl_dir}]
```

DDL 目录自动检测：同级的 `04_ddl/` 有则传入，没有则跳过。

产出：`{output_dir}/knowledge_draft.json` + `{output_dir}/knowledge_summary.md`

### Step 2: AI 补充业务理解

**AI 只读 `knowledge_summary.md`（2-4KB 摘要），不读 34KB 的 JSON。**

AI 基于摘要，按模板格式输出自然语言，保存为 `knowledge_ai.md`：

```markdown
# 整体描述
（2-3句话描述这个ETL是干什么的）

## step_1
（这步的业务目的和加工逻辑）

## step_2
...

## 关键字段
- 字段名: 业务含义
```

### Step 3: 生成全部视图

```bash
python {skill_dir}/run.py view_generator \
    --input {output_dir}/knowledge_draft.json \
    --ai-input {output_dir}/knowledge_ai.md \
    --output {output_dir} \
    --views all
```

`--ai-input` 是可选的（没有 AI 增强时跳过，用脚本兜底描述）。

### Step 4: 报告结果

```
分析完成！

目标表：{target_table}
步骤数：{steps} | 字段数：{fields} | 源表数：{sources}
加工模式：{patterns}

已生成：
- mapping.xlsx       — 字段映射
- asset_report.html  — 资产说明书
- tech_design.md     — 技术设计文档

路径：{output_dir}/
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

### analyzer

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | execution_tasks.xlsx 路径 |
| `--output` | 是 | 输出目录 |
| `--dialect` | 否 | oracle/dws/auto，默认自动检测 |
| `--ddl-dir` | 否 | DDL 目录（补充字段类型和注释） |

### view_generator

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | knowledge_draft.json 路径 |
| `--output` | 是 | 输出目录 |
| `--ai-input` | 否 | knowledge_ai.md 路径（AI 增强结果，可选） |
| `--views` | 否 | mapping/asset/techspec，默认 all |

## 禁止事项

- 修改 L1-L3 的任何事实数据（脚本产出）
- 跳过脚本直接用 AI 分析 SQL
