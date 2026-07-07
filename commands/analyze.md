---
description: DWS 制品包分析（分析制品包→生成知识文档+三视图）
---

# /analyze — 制品包分析命令

用户提供执行平台制品包（execution_tasks.xlsx），自动分析并生成全部交付物。

## 触发方式

支持三种输入形态，同一个 `/analyze` 命令：

### 形态 1：xlsx 文件（执行平台导出的制品包）

```
/analyze @execution_tasks.xlsx
/analyze path/to/execution_tasks.xlsx
```

### 形态 2：代码仓 yml 规则组目录

```
/analyze @BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_ORDER_D/
/analyze path/to/DWB_TRADE_ORDER_D/
```

指向规则组目录即可，工具自动读取目录下所有 `*.yml`（一个 yml = 一条规则）。

### 形态 3：只说表名，AI 自动定位代码仓目录

```
/analyze DWB_TRADE_ORDER_D
"分析 DWB_TRADE_ORDER_D 这个表"
```

AI 在代码仓 `BFT/BftWideTable/` 下搜索匹配的规则组目录，找到后走形态 2；
找到多个同名则让用户选；找不到则提示用户提供目录路径。

### AI 判断规则

| 输入 | AI 判断 | 脚本处理 |
|------|---------|---------|
| `.xlsx` 文件 | xlsx 场景 | `--input xxx.xlsx` → read_excel |
| 目录路径 | yml 场景 | `--input 目录/` → read_yml |
| 表名（非文件非目录） | AI 先找目录 | 找到后 `--input 目录/` → read_yml |

AI 也可以在用户拖入 xlsx 或说"分析这个表""帮我看看这个ETL"时自动触发。

## 执行流程

> **路径约定**：`--output` 指定的是**基础目录**（如工作目录或 `docs/`）。
> 脚本会自动在该目录下按**规则组英文名称**（取自 Excel/yml 的「规则组英文名称」，
> 缺失则回退到规则组编码）建子目录，所有产物输出到那里。
> 下面用 `{output_dir}` 代指这个自动创建的子目录，AI 从 Step 1 的输出日志读取其真实路径。

### Step 1: 执行分析脚本

```bash
# xlsx 场景
python {skill_dir}/run.py analyzer \
    --input {input_xlsx} \
    --output {base_dir}

# yml 场景（--input 指向规则组目录）
python {skill_dir}/run.py analyzer \
    --input {规则组目录} \
    --output {base_dir}
```

**输入自动分流**：脚本根据 `--input` 是文件还是目录，自动选择 read_excel 或 read_yml，
用户无需指定。两种路径产出完全一致（共用同一引擎）。

**输出目录建议**：`--output` 建议指定到**代码仓外**（如 `~/docs/` 或 `./output/`），
不要放代码仓内。产出文档放代码仓内会污染 git 状态（每次分析产生未跟踪文件），
和源码混在一起也不清晰。

**DDL 发现**：
- yml 场景：自动发现。从规则组目录向上定位代码仓根（含 `BFT/`+`DDL/`），再按
  `DDL/{DWS_EDW|DWS_RT_EDW}/{schema}/table/{目标表}.sql` 查找
- xlsx 场景：不自动发现（xlsx 是临时导出，DDL 位置不统一），需 `--ddl-dir` 指定
- 找不到时不阻塞分析（DDL 是可选增强）

`--output` 给基础目录即可，脚本会打印实际输出目录（`输出目录: ...`），后续步骤用它。

产出：`{output_dir}/knowledge_draft.json` + `{output_dir}/knowledge_summary.md`

### Step 2: AI 补充业务理解（必做，不能跳过）

**AI 读 `{output_dir}/knowledge_summary.md`**（2-4KB 摘要，不读 34KB JSON）。

基于摘要，按以下格式输出自然语言，保存为 `{output_dir}/knowledge_ai.md`：

```markdown
# 整体描述
（这张表是什么、干什么、什么粒度，2-3句话）

## step_1
（这步的业务目的和加工逻辑）

## step_2
...

## 关键字段
- 字段名: 业务含义
```

注意：脚本已自动生成兜底描述，AI 补充的会覆盖兜底版本。

### Step 3: 生成全部视图

```bash
python {skill_dir}/run.py view_generator \
    --input {output_dir}/knowledge_draft.json \
    --ai-input {output_dir}/knowledge_ai.md \
    --output {output_dir} \
    --views all
```

### Step 4: 报告结果

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
2. **Step 2（AI 增强）不能跳过**，弱模型也要执行，即使只是复制 draft 加少量修改
3. **视图用 knowledge_draft.json 生成**，AI 增强结果通过 `--ai-input knowledge_ai.md` 注入
4. **视图全部自动生成**，不询问用户选哪些
5. **输入自动分流**：`--input` 是 `.xlsx` 文件走 read_excel，是目录走 read_yml，用户无需区分
6. **DDL 发现**：yml 场景从代码仓根自动定位 `DDL/` 子树；xlsx 场景需 `--ddl-dir` 指定；
   找不到不阻塞（DDL 是可选增强）
7. **xlsx 与 yml 产出完全一致**：两种输入走同一引擎（analyze_pipeline），不会有内容差异
8. **输出目录**：`--output` 给基础目录，脚本自动在其下按**规则组英文名称**建子目录，产物输出到那里（缺失则回退规则组编码）

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `.opencode/skills/dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `.opencode/commands/`
- 确保 `run.py` 入口脚本与 `references/` 同级
