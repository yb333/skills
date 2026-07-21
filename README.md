# Analyzer Agent

DWS 数据仓库 ETL 资产理解 Agent——从执行平台制品包或代码仓反向提取 ETL 知识，自动生成字段映射、资产说明书、技术设计文档，并支持关联影响分析。

基于 [OpenCode](https://github.com/sst/opencode) 标准，在 AI 对话中以自然语言或斜杠命令触发。

## 能力

| 能力 | 命令 | 做什么 |
|------|------|--------|
| 资产文档化 | `/analyze` | 分析一个 ETL 资产，生成字段映射 + 资产说明书 + 技术设计文档 |
| 批量文档化 | `/analyze-batch` | 批量分析多个资产，每个出三件套 |
| 字段使用检索 | `/field-search` | 搜索字段在多个表里的使用情况，输出 Excel |
| 关联影响分析 | `/impact-analysis` | 源端变更 → 本资产受什么影响，输出影响清单 |
| 跨资产影响分析 | `/impact-analysis --cross-asset` | 按表名自动定位代码仓目录，批量分析受影响资产 |

## 快速开始

### 安装

```bash
# macOS / Linux
bash install.sh

# Windows（双击 install.bat 或命令行执行）
install.bat
```

安装脚本自动：扫描 skill → 创建 venv → 装依赖 → 复制 skill + 命令到 opencode 目录。

> 也可只装指定 skill：`bash install.sh dws-pipeline-analyzer`
>
> 也可装到当前项目（不建 venv）：`bash install.sh -l`

### 依赖

- Python 3.10+（Windows 用 `python`，macOS/Linux 用 `python3`）
- openpyxl 3.1+（Excel 读写）
- sqlglot 23.0+（SQL AST 解析）

### 使用

安装完成后，在 AI 对话中直接使用：

```
/analyze @execution_tasks.xlsx          # 分析制品包
/analyze DWB_TRADE_ORDER_D              # 只说表名，自动定位代码仓目录
/analyze-batch @execution_tasks.xlsx    # 批量分析
/impact-analysis @变更清单.xlsx          # 源端变更影响分析
```

自然语言也能触发：`"分析这个ETL"`、`"这个表的数据从哪来"`、`"源端字段变了对我的资产有什么影响"`。

## 输入

| 输入 | 说明 |
|------|------|
| execution_tasks.xlsx | 执行平台导出的制品包（RULE + TargetFields + GroupVariables） |
| 代码仓 yml 目录 | 规则组目录下所有 `*.yml`（一个 yml = 一条规则），DDL 自动发现 |
| 变更清单 Excel | 影响分析专用：表级变更 + 字段级变更 + 受影响表清单（四 Sheet 模板） |
| DDL 文件（`*.sql`） | 可选增强：补充字段类型+中文名。代码仓场景自动发现 |

## 输出

```
output/{规则组英文名}/
├── knowledge_draft.json    # 结构化知识
├── mapping.xlsx            # 字段映射（实体级 + 属性级，CTE 穿透）
├── asset_report.html       # 资产说明书（交互式，含血缘图 + SQL 高亮）
├── tech_design.md          # 技术设计文档（9 章节）
└── impact.xlsx             # 影响清单（影响分析产出）
```

## 核心技术能力

- **SQL 理解引擎**：CTE 穿透、UNION 多分支、子查询字段穿透、I 视图封装链路
- **加工模式检测**：CTE 预聚合 / 行转列 / SCD2 / 增量去重等 7-8 个模式自动标注
- **加载策略检测**：全量 / 增量 / 分区交换，用于影响分析风险判定
- **类型兼容性判定**：源新类型 vs 目标 DDL + cast 关口，精确判兼容/截断/溢出
- **质量评估**：类型一致性、JOIN 缺 ON、SELECT *、调度过度约束等检查
- **关联影响分析**：16 种变化类型映射表，三层过滤 + 逐跳传播

## 文档

- [用户指南](user-guide.md) — 面向使用者的详细说明
- [架构设计](architecture.md) — 面向开发/维护者的架构文档
- [影响分析模板](dws-pipeline-analyzer/templates/) — 变更清单模板和跨资产案例

## 目录结构

```
├── dws-pipeline-analyzer/   核心 agent
│   ├── references/          engine/analyzer/view_generator/impact_analyzer
│   ├── templates/           变更清单模板 + 跨资产案例
│   └── SKILL.md             Skill 定义（AI 触发条件）
├── commands/                命令定义（/analyze /impact-analysis 等）
├── install.sh / install.bat 安装脚本
├── sample_rule.yml          yml 样例
├── architecture.md          架构设计文档
└── user-guide.md            用户指南
```

## 在其他 AI agent 上使用

基于 OpenCode 标准，复制 skill 目录 + 命令文件 + 装依赖即可：

```bash
# 1. 复制 skill
cp -r dws-pipeline-analyzer/ ~/.config/opencode/skills/

# 2. 复制命令
cp commands/*.md ~/.config/opencode/commands/

# 3. 装依赖
pip install openpyxl sqlglot
```
