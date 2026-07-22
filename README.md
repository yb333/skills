# Analyzer Agent

DWS 数据仓库 ETL 资产理解 Agent——从执行平台术加制品包或代码仓反向提取 ETL 知识，自动生成字段映射、资产说明书、技术设计文档，并支持关联影响分析和多规则组链路分析。

基于 [OpenCode](https://github.com/sst/opencode) 标准，在 AI 对话中以自然语言或斜杠命令触发。

## 能力总览

| 能力 | 命令 | 做什么 |
|------|------|--------|
| 资产文档化 | `/analyze` | 分析一个 ETL 资产，生成字段映射 + 资产说明书 + 技术设计文档 |
| 批量文档化 | `/analyze-batch` | 批量分析多个资产，每个出三件套 |
| 多规则组链路分析 | `/analyze-chain` | 自动追溯上游 mid 规则组，分析从源表到最终资产的完整链路 |
| 字段使用检索 | `/field-search` | 搜索字段在多个表里的使用情况，输出 Excel |
| 关联影响分析 | `/impact-analysis` | 源端变更 → 本资产受什么影响，输出影响清单 |
| 跨资产影响分析 | `/impact-analysis --cross-asset` | 按表名自动定位代码仓目录，批量分析受影响资产 |

---

## 安装

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

### 手工安装

```bash
cp -r dws-pipeline-analyzer/ ~/.config/opencode/skills/
cp commands/*.md ~/.config/opencode/commands/
pip install openpyxl sqlglot
```

---

## 各能力详解

### 1. 资产文档化（/analyze）

分析一个 ETL 资产，生成字段映射 + 资产说明书 + 技术设计文档。

**触发方式**：

```
/analyze @execution_tasks.xlsx              # 术加制品包 Excel
/analyze @代码仓规则组目录/                  # 代码仓 yml 目录
/analyze DWB_TRADE_ORDER_D                  # 只说表名，自动定位代码仓目录
"分析这个ETL" / "这个表的数据从哪来"          # 自然语言触发
```

AI 自动判断输入类型：`.xlsx` 走术加制品包解析，目录走代码仓 yml 解析，表名自动在代码仓搜索匹配目录。两种输入走同一引擎，产出完全一致。

**输入**：

| 输入 | 说明 |
|------|------|
| execution_tasks.xlsx | 执行平台导出的术加制品包（RULE + TargetFields + GroupVariables） |
| 代码仓 yml 目录 | 规则组目录下所有 `*.yml`（一个 yml = 一条规则） |
| DDL 文件（`*.sql`） | 可选增强：补充字段类型+中文名。代码仓场景自动发现，术加制品包场景用 `--ddl-dir` 指定 |

**产出**：

```
output/{规则组英文名}/
├── knowledge_draft.json    # 结构化知识（事实层）
├── mapping.xlsx            # 字段映射（实体级 + 属性级，CTE 穿透）
├── asset_report.html       # 资产说明书（交互式，含血缘图 + SQL 高亮）
└── tech_design.md          # 技术设计文档（9 章节）
```

**产出说明**：

| 产物 | 给谁看 | 内容 |
|------|--------|------|
| mapping.xlsx | 业务/开发 | 实体级（源表→目标表，含 JOIN）+ 属性级（字段级，CTE 穿透到物理源表） |
| asset_report.html | 领导/新人 | 7 section 交互式报告：概览/数据流图/目标表结构/加工逻辑/字段映射/质量评估 |
| tech_design.md | 接手维护者 | 9 章节：概述/复杂度/分段策略/表级血缘/字段映射/处理逻辑/质量评估/依赖/配置 |

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

**机制**：子进程隔离（每批独立进程，避免内存超限）、单组隔离（一个崩溃不拖垮其他）、同名去重（规则组英文名相同时追加编码）。

### 3. 多规则组链路分析（/analyze-chain）

一个资产可能由多个规则组协作加工完成（mid 规则组写中间表 → 最终 F 规则组读中间表写最终资产）。本命令自动追溯上游 mid 规则组，合并为一条完整链路分析。

**触发方式**：

```
/analyze-chain @最终F表规则组目录
/analyze-chain DWB_TRADE_ORDER_F --repo-root 代码仓根目录
"分析 DWB_TRADE_ORDER_F 的完整加工链路"
```

**自动完成**：
1. 从最终 F 表规则组出发，读 SQL 找源表里的 mid 表
2. 在代码仓内找写这些 mid 表的规则组，递归追溯（直到 ods 源表）
3. 合并所有规则组，exec_sequence 按数据依赖拓扑排序（不依赖的组并行保留）
4. 排除 `_init` 规则组（初始化数据不属于日常链路）
5. 作为一个整体跑 analyze_pipeline + 生成三件套

**报告差异化**（跟单规则组区分）：
- 标题区标"多规则组链路"标识 + 链路总览
- 数据流图工具栏有规则组标签，点击聚焦该组（其他变灰）
- 步骤卡片标紫色标签显示所属规则组

### 4. 字段使用检索（/field-search）

搜索字段在多个表里的使用情况。

```
/field-search @execution_tasks.xlsx amount,user_id
```

输出 Excel：目标表/字段名/字段角色/字段情况/最初来源/详情。

### 5. 关联影响分析（/impact-analysis）

源端变更 → 本资产受什么影响，输出影响清单 Excel。

**定位**：影响清单 + 定位器，不是权威影响报告。自动化两端（确定有/无影响），把中间（判不了）留给人。

**触发方式**：

```
/impact-analysis @变更清单.xlsx @资产knowledge目录    # 单资产
/impact-analysis @变更清单.xlsx --repo-root 代码仓路径  # 跨资产（含受影响表清单）
"源端字段变了对我的资产有什么影响"                      # 自然语言触发
```

**变更清单模板**（`dws-pipeline-analyzer/templates/`）：

| Sheet | 内容 |
|-------|------|
| 表级变更 | 切换前表名/切换后表名/是否平切/切换说明/表级变化类型 |
| 字段级变更 | 切换前后完整对照（库/schema/表/字段/类型）+ 字段变化类型 |
| 受影响表清单 | 从血缘平台整理的受影响表名（跨资产用，一行一张表） |
| 变动类型说明 | 类型字典（可选） |

**变化类型 → 影响判定**：

| 类型 | 判定 | 说明 |
|------|------|------|
| 字段类型及长度变化 | 动态 | 兼容性链判定（源新类型 vs 目标DDL + cast关口） |
| 字段下线/删除 | 🔴 | 字段没了，取不到 |
| 字段值语义变化 | 🟡 | 需语义判断 |
| 表/视图下线 | 🔴 | 来源消失，列出所有受波及字段 |
| 平切 | 🟡 | 需改表名+术+规则+调度 |
| 数据初始化（不刷时间戳） | 动态 | 全量🟢/增量🔴（隐蔽高风险） |

**产出 impact.xlsx**：

| Sheet | 内容 |
|-------|------|
| 统计摘要 | 变更总数/有影响/待确认/无影响/未命中 |
| 影响清单 | 🔴有影响 + 🟡待确认（资产视角，以目标字段为一行） |
| 表级影响 | 平切/下线/初始化等整表维度影响 |
| 过滤摘要 | 🟢无影响 + ⚪未命中（可追溯不吵） |

**跨资产模式**：读变更清单 Sheet3 的受影响表名 → 按表名在 `BFT/BftWideTable/` 下定位规则组目录 → 批量分析。

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
| 字符串内分号保护（LISTAGG(x,';') 不截断） | ✓ |

### 自动检测

- **加工模式**：CTE 预聚合 / 行转列 / SCD2 / 增量去重等 7-8 个模式
- **加载策略**：全量 / 增量 / 分区交换，用于影响分析风险判定
- **类型兼容性**：源新类型 vs 目标 DDL + cast 关口，精确判兼容/截断/溢出
- **质量评估**：类型一致性（含 value/cast/case_when 分支）、JOIN 缺 ON、SELECT *、调度过度约束等检查

### 字段加工类型（transform_type）

value < direct < expression < fallback < case_when < aggregate < pivot < window。优先级高的覆盖低的（CTE 穿透时升级）。

### 性能优化

- SQL AST 解析缓存（同一条 SQL 多处消费只解析一次）
- DDL 目录扫描缓存（build_table_catalog 批量查表只扫一次目录）
- 性能日志融入分析输出（慢阶段 >0.5s 自动显示）

---

## 文档

- [影响分析模板](dws-pipeline-analyzer/templates/) — 变更清单模板和跨资产案例

---

## 目录结构

```
├── dws-pipeline-analyzer/   核心 agent
│   ├── references/          engine/analyzer/view_generator/impact_analyzer
│   ├── templates/           变更清单模板 + 跨资产案例
│   └── SKILL.md             Skill 定义（AI 触发条件）
├── commands/                命令定义（/analyze /analyze-chain /impact-analysis 等）
├── install.sh / install.bat 安装脚本
└── architecture.md          架构设计文档（开发者用，不同步给用户）
```

---

## FAQ

**Q: 没有 DDL 文件能用吗？**
能。DDL 是可选增强，没有时字段类型留空，分析照常进行。

**Q: SQL 里有 CTE，mapping 能穿透吗？**
能。属性级 mapping 穿透 CTE，直接显示物理源表原始字段。

**Q: 代码仓 yml 和术加制品包 Excel 有什么区别？**
输入格式不同但产出一致。代码仓 yml 是规则组的原始定义，术加制品包 Excel 是执行平台导出的。两者走同一引擎（analyze_pipeline）。

**Q: 一个资产由多个规则组加工怎么办？**
用 `/analyze-chain`，自动追溯上游 mid 规则组，合并成一条完整链路分析。

**Q: 批量分析时某组失败了怎么办？**
读 `batch_logs/batch_N.log` 查看错误。单组崩溃不影响其他组。

**Q: 影响分析怎么用？**
先跑 `/analyze` 生成 knowledge，再跑 `/impact-analysis` 传变更清单 + knowledge。跨资产模式在变更清单填 Sheet3 受影响表清单。

**Q: 分析一个 ETL 需要多长时间？**
通常几秒（脚本分析）。复杂资产（13规则+400字段+DDL）约 2-3 秒。

**Q: 在其他 AI agent 上能用吗？**
能。基于 OpenCode 标准，复制 skill 目录 + 命令文件 + 装依赖即可。
