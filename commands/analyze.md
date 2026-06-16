---
description: DWS 制品包分析（分析制品包→生成知识文档+三视图）
---

# /analyze — 制品包分析命令

用户提供执行平台制品包（execution_tasks.xlsx），自动分析并生成全部交付物。

## 触发方式

```
/analyze                              # 分析当前工作目录下的制品包
/analyze @execution_tasks.xlsx        # 指定文件
/analyze path/to/execution_tasks.xlsx # 指定路径
```

AI 也可以在用户拖入 xlsx 或说"分析这个表""帮我看看这个ETL"时自动触发此流程。

## 执行流程（2 步，全自动）

### Step 1: 分析 + AI 增强 + 生成全部视图

```bash
# 1. 自动定位文件（按优先级）
# - 用户指定的路径
# - 当前目录下 *execution_tasks*.xlsx
# - docs/output/*/09_export/execution_tasks.xlsx

# 2. 自动检测 DDL 目录（同级的 04_ddl/）
# - 有则自动传入 --ddl-dir
# - 没有则跳过（字段类型/中文名留空）

# 3. 执行分析脚本
python {skill_dir}/run.py analyzer \
    --input {input_xlsx} \
    --output {output_dir} \
    [--ddl-dir {ddl_dir}]

# 输出: {output_dir}/knowledge_draft.json
```

### Step 1b: AI 补充业务理解（必做，不能跳过）

读取 `{output_dir}/knowledge_draft.json`，**必须**补充以下内容并保存为 `knowledge_final.json`：

**business_logic 补充**：
- `summary`：整体概述（这张表是什么、干什么、什么粒度、怎么更新，2-3句话）
- `step_descriptions`：遍历已有数组，为每个 step 补充：
  - `purpose`：这步的业务目的（如"构建合同pu分析事实表"）
  - `logic`：加工逻辑描述（如"以合同pu指标表为主表，通过3个CTE预处理..."）
  - 如果脚本已自动生成兜底描述，AI 可以优化但不可以删除
- `key_transforms`：关键字段的业务含义（把 SQL 表达式翻成人话）

**quality.ai_insights 补充**：
- 只补充有实际问题的洞察（severity 非 info）
- 正面观察不需要写

**保存**：将补充后的完整 JSON 写入 `{output_dir}/knowledge_final.json`

```bash
# Step 1c: 生成全部 3 个视图（用 knowledge_final.json 不是 draft）
python {skill_dir}/run.py view_generator \
    --input {output_dir}/knowledge_final.json \
    --output {output_dir} \
    --views all
```

### Step 2: 报告结果

向用户展示：

```
分析完成！

目标表：{target_table}
步骤数：{steps} | 字段数：{fields} | 源表数：{sources}
加工模式：{patterns}

已生成：
- mapping.xlsx          — 字段映射（实体级+属性级，CTE穿透）
- asset_report.html     — 资产说明书（交互式，含血缘图+SQL高亮）
- tech_design.md        — 技术设计文档（9 章节）

路径：{output_dir}/
```

## 关键规则

1. **第一步必须执行分析脚本**，不能跳过直接用 AI 分析
2. **Step 1b（AI 增强）不能跳过**，弱模型也要执行，即使只是复制 draft 加少量修改
3. **视图用 knowledge_final.json 生成**（不是 draft）
4. **视图全部自动生成**，不询问用户选哪些
5. **DDL 自动检测**，同级 04_ddl/ 有就读，没有就跳过
6. **输出目录**直接用 --output 指定的目录，不嵌套子目录

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `.opencode/skills/dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `.opencode/commands/`
- 确保已安装 `dws-run`（skill 入口脚本）
