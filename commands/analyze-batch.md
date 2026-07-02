---
description: 批量分析多个规则组，生成全部交付件（资产说明书/字段映射/技术设计文档）
---

# /analyze-batch — 批量分析

对多个规则组批量执行分析，每个规则组生成三个交付件（asset_report.html / mapping.xlsx / tech_design.md）。

## 触发方式

```
/analyze-batch @execution_tasks.xlsx
```

用户也可以自然语言触发：
- "批量分析这些表"
- "把所有规则组都生成报告"

## 执行流程

### Step 1: 执行批量分析脚本

```bash
python {skill_dir}/run.py batch \
    --input {input_xlsx} \
    --output {base_dir} \
    [--batch-size 50] \
    [--no-ai] \
    [--verbose]
```

- `--output` 给基础目录，脚本在其下按规则组英文名建子目录
- `--batch-size` 每批处理数量（默认 50），超出自动分批
- `--no-ai` 跳过 AI 增强（只生成脚本产物，速度快）
- `--verbose` 逐组详细输出到 stdout（终端调试用）。**默认不开启**：逐组详细状态
  写入 `{base_dir}/batch_logs/batch_*.log`，stdout 只保留批次级进度，避免大批量
  时 stdout 输出累积超出上游捕获管道上限导致整个进程被杀（典型表现：前两批正常、
  第三批起步即被杀）。需要排查某批/某组细节时，读对应 `batch_*.log` 即可

### Step 2: AI 增强（如未跳过）

对每个规则组的 `knowledge_summary.md`，AI 读取后补充业务理解，保存为 `{output_dir}/{规则组英文名}/knowledge_ai.md`。

AI 增强分批进行（每批 5-10 个规则组），避免一次处理过多。

### Step 3: 告知用户结果

脚本输出每个规则组的处理状态，交付件位置：
```
{base_dir}/
├── {规则组1英文名}/
│   ├── knowledge_draft.json
│   ├── knowledge_summary.md
│   ├── mapping.xlsx
│   ├── asset_report.html
│   └── tech_design.md
├── {规则组2英文名}/
│   └── ...
```

## 关键规则

1. **分批处理**：超过 `--batch-size` 的规则组自动分批，每批连续处理
2. **输出目录**：每个规则组在 `--output` 下按英文名建子目录
3. **AI 增强**：默认启用，`--no-ai` 跳过；AI 分批增强（每批 5-10 个）
4. **进度提示**：每批处理完输出进度（批次号 + 成功/失败数）
5. **此命令依赖** `dws-pipeline-analyzer` skill
