---
description: 字段使用情况批量搜索（搜索字段在多个表里的用法，输出 Excel）
---

# /field-search — 字段使用情况搜索

搜索关键字匹配到的字段，在多个规则组（目标表）里的使用情况，输出一张 Excel。

## 触发方式

```
/field-search @execution_tasks.xlsx amount           # 搜索单个关键字
/field-search @execution_tasks.xlsx amount,user_id   # 搜索多个关键字（逗号分隔）
```

用户也可以自然语言触发：
- "查一下 amount 这个字段在这些表里怎么用的"
- "搜索 user_id 的使用情况"

## 执行流程

### Step 1: 执行搜索脚本

```bash
python {skill_dir}/run.py field_search \
    --input {input_xlsx} \
    --keyword {keyword} \
    --output field_usage.xlsx
```

- `--keyword` 多个关键字用逗号分隔
- 输入 Excel 可含多个规则组（按规则组编码自动分组）
- 支持上千行的大 Excel（轻量解析，不生成中间 JSON）

### Step 2: 告知用户结果

脚本输出 `field_usage.xlsx`，包含一个大 sheet：

| 列 | 说明 |
|---|---|
| 目标表 | schema.table |
| 字段名 | 匹配到的字段 |
| 字段角色 | 写入目标表 / 临时过程使用 / 辅助字段 |
| 字段情况 | 直取 / 加工 / 关联键 / 过滤条件 |
| 最初来源 | 物理源表.字段（写入字段）或使用步骤（辅助字段）|
| 详情 | 加工表达式 / 关联条件 / 过滤条件 |

## 关键规则

1. **关键字匹配**：字段名 + 加工表达式都匹配（如 `SUM(amount)` 能匹配 amount）
2. **字段角色**：
   - 写入目标表：字段在最终目标表的 SELECT 里
   - 临时过程使用：字段在中间表的 SELECT 里
   - 辅助字段：字段只在 JOIN ON 或 WHERE 条件里
3. **来源追溯**：写入字段追溯到物理源表（穿透中间表）
4. **此命令依赖** `dws-pipeline-analyzer` skill（复用其 SQL 解析能力）
