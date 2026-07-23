# Analyzer Agent

DWS 数据仓库 ETL 资产理解工具——帮你快速理解一个 ETL 资产的数据从哪来、怎么加工的、字段怎么映射的，自动生成文档。

在 AI 对话工具（codeagent / opencode）里用斜杠命令触发，比如输入 `/analyze` 就能分析一个 ETL 资产。

---

## 快速开始（3 步上手）

### 第 1 步：下载代码

```bash
git clone https://github.com/yb333/analyzer-agent.git
```

或者直接在 GitHub 页面点 **Code → Download ZIP**，解压到任意目录。

### 第 2 步：安装

**Windows**：双击 `install.bat`

**macOS / Linux**：终端里执行 `bash install.sh`

安装脚本会自动完成：创建 Python 虚拟环境 → 安装依赖（openpyxl、sqlglot）→ 复制工具文件到 codeagent 目录。

> **前提条件**：电脑上已装 Python 3.10+（[下载地址](https://www.python.org/downloads/)）。Windows 装完 Python 后需要重新打开命令行窗口。

### 第 3 步：在 codeagent 里使用

1. 打开 codeagent（就是你们平时用的 AI 对话工具）
2. 在对话框里输入命令，比如：

```
/analyze @execution_tasks.xlsx
```

把 `@` 后面换成你的实际文件（直接把文件拖进对话框也行，会自动变成 `@文件名`）。

等几秒钟，就会在 `output/` 目录下生成三个文件：
- **字段映射.xlsx** — 每个字段从哪个源表哪个字段来的
- **资产说明书.html** — 可视化报告（用浏览器打开），含数据流图、加工逻辑、质量评估、调度信息
- **技术设计.md** — 技术文档

就这么简单！下面是更多能力的说明。

---

## 能力总览

| 能力 | 命令 | 做什么 |
|------|------|--------|
| 资产文档化 | `/analyze` | 分析一个 ETL 资产，生成字段映射 + 资产说明书 + 技术设计文档 |
| 批量文档化 | `/analyze-batch` | 批量分析多个资产，每个出三件套 |
| 多规则组链路分析 | `/analyze-chain` | 追溯整个代码仓的关联规则组，分析从源表到最终资产的完整加工链路 |
| 字段使用检索 | `/field-search` | 搜索字段在多个表里的使用情况，输出 Excel |
| 关联影响分析 | `/impact-analysis` | 源端变更 → 本资产受什么影响，输出影响清单 |
| 跨资产影响分析 | `/impact-analysis --cross-asset` | 按表名自动定位代码仓目录，批量分析受影响资产 |

---

## 各能力详解

> **推荐用法**：在 AI 对话里用斜杠命令 + `@` 引用文件的方式触发，如 `/analyze @execution_tasks.xlsx`、`/analyze-chain @规则组目录`。这种方式输入明确、上下文完整、可复现。自然语言（"分析这个 ETL"）也支持，但识别输入类型时可能需要多轮确认。

### 1. 资产文档化（/analyze）

分析一个 ETL 资产，生成字段映射 + 资产说明书 + 技术设计文档。

**怎么用**：

```
/analyze @execution_tasks.xlsx              # 术加制品包 Excel
/analyze @代码仓规则组目录/                  # 代码仓 yml 目录
/analyze DWB_TRADE_ORDER_D                  # 只说表名，自动定位代码仓目录
```

把文件拖进对话框就行，AI 会自动判断是 Excel 还是代码仓目录。

**输入**：

| 输入 | 说明 |
|------|------|
| execution_tasks.xlsx | 执行平台导出的术加制品包（RULE + TargetFields + GroupVariables） |
| 代码仓 yml 目录 | 规则组目录下所有 `*.yml`（一个 yml = 一条规则） |
| DDL 文件（`*.sql`） | 可选增强：补充字段类型+中文名。代码仓场景自动发现 |

**产出**（在 `output/` 目录下）：

| 产物 | 给谁看 | 内容 |
|------|--------|------|
| mapping.xlsx | 业务/开发 | 实体级（源表→目标表，含 JOIN）+ 属性级（字段级，CTE 穿透到物理源表） |
| asset_report.html | 领导/新人 | 交互式报告：概览/数据流图/调度信息/目标表结构/加工逻辑/字段映射/质量评估 |
| tech_design.md | 接手维护者 | 10 章节：概述/复杂度/分段策略/表级血缘/字段映射/处理逻辑/质量评估/依赖/配置/调度 |

**常见场景**：

| 场景 | 怎么用 |
|------|--------|
| 接手不熟悉的 ETL | `/analyze`，看 asset_report.html 的概览和加工逻辑 |
| 给业务出字段映射 | `/analyze`，mapping.xlsx 发给业务 |
| 优化前现状分析 | `/analyze`，看质量评估 section 的问题列表 |
| 理解代码仓里的表 | `/analyze 表名`，自动定位目录分析 |

### 2. 批量文档化（/analyze-batch）

批量分析多个资产，每个出三件套。

```
/analyze-batch @execution_tasks.xlsx
```

### 3. 多规则组链路分析（/analyze-chain）

一个资产通常由代码仓里多个规则组协作加工完成（上游规则组写中间表 → 下游规则组读中间表 → …… → 最终 F 规则组写最终资产）。本命令自动在整个代码仓内追溯所有关联规则组，合并为一条从源表到最终资产的完整加工链路分析。

```
/analyze-chain @最终F表规则组目录
"分析 DWB_TRADE_ORDER_F 的完整加工链路"
```

### 4. 字段使用检索（/field-search）

搜索字段在多个表里的使用情况。

```
/field-search @execution_tasks.xlsx amount,user_id
```

### 5. 关联影响分析（/impact-analysis）

源端变更 → 本资产受什么影响，输出影响清单 Excel。

```
/impact-analysis @变更清单.xlsx @资产knowledge目录    # 单资产
"源端字段变了对我的资产有什么影响"                      # 自然语言触发
```

变更清单模板在 `dws-pipeline-analyzer/templates/` 目录下。

---

## 核心技术能力

### SQL 理解引擎

| 构造 | 支持情况 |
|------|---------|
| SELECT / WITH...SELECT | ✓ |
| UNION / UNION ALL / INTERSECT / EXCEPT（含CTE内部UNION） | ✓ |
| CTE（单层 / 嵌套 / 递归穿透） | ✓ |
| JOIN（LEFT/INNER/FULL/CROSS/子查询） | ✓ |
| 窗口函数 / 行转列 / LISTAGG / Oracle 方言 | ✓ |
| 注释别名（/* field_name */） | ✓ |
| I 视图封装（F→I 自动发现） | ✓ |
| 调度信息自动发现（LTS 调度任务） | ✓ |

### 自动检测

- **加工模式**：CTE 预聚合 / 行转列 / SCD2 / 增量去重等 7-8 个模式
- **加载策略**：全量 / 增量 / 分区交换，用于影响分析风险判定
- **调度信息**：自动从代码仓 LTS 目录发现 F+I 调度任务，展示调度周期/依赖/执行Job
- **质量评估**：类型一致性、JOIN 缺 ON、SELECT *、调度过度约束等检查

---

## FAQ

**Q: 没有 DDL 文件能用吗？**
能。DDL 是可选增强，没有时字段类型留空，分析照常进行。

**Q: SQL 里有 CTE，mapping 能穿透吗？**
能。属性级 mapping 穿透 CTE，直接显示物理源表原始字段。

**Q: 代码仓 yml 和术加制品包 Excel 有什么区别？**
输入格式不同但产出一致。代码仓 yml 是规则组的原始定义，术加制品包 Excel 是执行平台导出的。两种输入走同一引擎，产出完全一致。

**Q: 一个资产由多个规则组加工怎么办？**
用 `/analyze-chain`，自动在整个代码仓追溯关联规则组，合并成一条完整链路分析。

**Q: 批量分析时某组失败了怎么办？**
读 `batch_logs/batch_N.log` 查看错误。单组崩溃不影响其他组。

**Q: 影响分析怎么用？**
先跑 `/analyze` 生成 knowledge，再跑 `/impact-analysis` 传变更清单 + knowledge。跨资产模式在变更清单填 Sheet3 受影响表清单。

**Q: 分析一个 ETL 需要多长时间？**
通常几秒（脚本分析）。复杂资产（13规则+400字段+DDL）约 2-3 秒。

**Q: 调度信息是怎么来的？**
分析代码仓 yml 目录时，自动从 LTS 目录发现对应的调度任务（F+I），展示在资产说明书的"调度信息"区块。非代码仓模式（Excel 输入）不会有调度信息。
