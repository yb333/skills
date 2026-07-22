---
description: 多规则组链路分析（从最终F表规则组出发，自动追溯上游mid规则组，分析完整链路）
---

# /analyze-chain — 多规则组链路分析命令

一个资产可能由多个规则组协作加工完成（mid规则组→最终F规则组）。本命令自动追溯上游 mid 规则组，合并为一条完整链路分析。

## 触发方式

```
/analyze-chain @最终F表规则组目录
/analyze-chain DWB_TRADE_ORDER_F --repo-root 代码仓根目录
"分析 DWB_TRADE_ORDER_F 的完整加工链路"
```

### AI 判断规则

| 输入 | AI 判断 | 处理 |
|------|---------|------|
| 最终F表规则组目录 | 直接追溯 | 从该目录出发，读SQL找上游mid表 |
| 最终F表名 | 先定位目录 | 在代码仓搜索匹配目录，再追溯 |
| "分析完整链路" | 触发本命令 | 识别为多规则组场景 |

## 执行流程

> **⚠ 直接执行脚本，不要先读内容。** 脚本自动完成追溯+合并+分析+生成报告。

### Step 1: 执行链路分析

```bash
python {skill_dir}/run.py analyzer --chain \
    --input {最终F表规则组目录或表名} \
    --output ./output/ \
    [--repo-root {代码仓根目录}] \
    [--ddl-dir {DDL目录}]
```

脚本自动完成：
1. 从最终F表规则组出发，读SQL找 mid 源表
2. 在同子项目下找写这些mid表的规则组，递归追溯
3. 合并所有规则组，exec_sequence拓扑排序重编号
4. 作为一个整体跑 analyze_pipeline + 生成三件套

### Step 2: 报告结果

```
多规则组链路分析完成！

规则组数：{N}
步骤数：{N}
字段数：{N}
目标表：{target_table}

追溯到的规则组：
  depth=1 DWB_TRADE_MID_F → 写 dwb_trade_mid_f
  depth=1 DWB_DETAIL_MID_F → 写 dwb_detail_mid_f
  depth=0 DWB_TRADE_ORDER_F → 写 dwb_trade_order_f

报告：./output/chain_{规则组名}/
  - asset_report.html    资产说明书（含多规则组链路标识）
  - mapping.xlsx         字段映射（完整链路，穿透mid表）
  - tech_design.md       技术设计文档
```

## 产出说明

跟单规则组 `/analyze` 的区别：

| 区别点 | /analyze（单规则组） | /analyze-chain（多规则组） |
|--------|---------------------|--------------------------|
| 标题区 | 普通标题 | 紫色"多规则组链路"标识 + 链路总览 |
| 概览卡片 | 加工步骤数 | 规则组数 |
| 步骤卡片 | 普通卡片 | 每步标紫色标签显示所属规则组 |
| 字段映射 | 单规则组内穿透 | 穿透mid表到ods源表 |
| 数据流图 | 单规则组节点 | 完整链路（ods→mid→最终F） |

## 追溯规则

- **串联靠数据依赖**：不靠表名尾缀，靠"谁写了这张表"
- **mid表尾缀** `_mid_f`：常见但不是必须的，追溯逻辑不依赖它
- **递归到顶**：mid也依赖别的mid时，继续往上追，直到ods源表
- **I视图**：只对最终F表触发，mid/tmp规则组不触发
- **找不到上游**：某张表在同子项目和项目级都找不到写入者时，标"未定位"，不中断

## 关键规则

1. **给最终F表规则组**（有I视图的那个），不是mid规则组
2. **直接执行脚本**，AI不预读内容
3. **mid规则组跟F规则组在同一子项目下**时自动找到；不在时提示用户手动指定
4. **exec_sequence重编号**：合并后按拓扑排序（上游在前），保证步骤间依赖正确

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `commands/`
- 确保 `run.py` 入口脚本与 `references/` 同级
