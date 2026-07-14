# DWS Pipeline Analyzer

从执行平台制品包或代码仓 yml 反向提取 ETL 知识，自动生成字段映射、资产说明书、技术设计文档。支持单资产深度分析、批量文档化、字段检索、**关联影响分析**。

## 能力总览

| 能力 | 命令 | 适用场景 |
|------|------|----------|
| 资产文档化 | `/analyze` | 单个规则组：生成 mapping + 资产说明书 + 技术设计文档 |
| 批量文档化 | `/analyze-batch` | 多个规则组：循环分析，每个规则组出三件套 |
| 字段使用检索 | `/field-search` | 搜字段在多个表里的用法，输出 Excel |
| **关联影响分析** | `/impact-analysis` | 源端变更 → 本资产受什么影响，输出影响清单 Excel |

## 安装

```bash
# macOS / Linux
bash install.sh

# Windows
install.bat
```

也可只装本 skill：`bash install.sh dws-pipeline-analyzer`

### 依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | Windows 用 `python`，macOS/Linux 用 `python3` |
| openpyxl 3.1+ | Excel 读写 |
| sqlglot 23.0+ | SQL AST 解析 |

> 首次运行脚本时自动检测，缺失会提示 `pip install openpyxl sqlglot`。

---

## 快速开始

在 AI 对话中输入命令即可，AI 会自动调用脚本并生成文档：

### 单个资产分析

```
/analyze @execution_tasks.xlsx
/analyze DWB_TRADE_ORDER_D              # 只说表名，自动定位代码仓目录
```

### 批量分析

```
/analyze-batch @execution_tasks.xlsx
```

### 字段搜索

```
/field-search @execution_tasks.xlsx amount
```

### 关联影响分析

```
/impact-analysis @变更清单.xlsx @资产knowledge目录
```

---

## 输入说明

### 资产输入（文档化/检索用）

| 输入 | 说明 |
|------|------|
| `execution_tasks.xlsx` | 执行平台导出的制品包（RULE + TargetFields + GroupVariables） |
| 代码仓 yml 目录 | 规则组目录下所有 `*.yml`（一个 yml = 一条规则），DDL 自动发现 |

两种输入走同一引擎（`analyze_pipeline`），产出完全一致。

### 变更清单输入（影响分析用）

模板在 `templates/` 下：
- `变更清单_模板.xlsx` — 空模板（只有列头）
- `变更清单_示例.xlsx` — 带示例数据

四个 Sheet：
1. **表级变更**：切换前表名/切换后表名/是否平切/切换说明/表级变化类型
2. **字段级变更**：切换前后完整对照（库/schema/表/字段/类型）+ 字段变化类型
3. **受影响表清单**：从血缘平台整理的受影响表名（跨资产场景，一行一张表）
4. **变动类型说明**（可选）：类型字典，用于报告翻译

**重要**：资产 SQL 引用的是**切换前表名**（旧名），工具据此匹配。

### 可选输入

| 文件 | 作用 |
|------|------|
| DDL 文件（`*.sql`） | 补充字段类型+中文名。yml/代码仓场景自动发现，xlsx 场景需 `--ddl-dir` |

> DDL 是可选增强。没有 DDL 时字段类型留空，分析照常进行。

---

## 输出产物

所有产物输出到 `--output` 指定目录下，按**规则组英文名**建子目录：

```
{output_dir}/
└── {规则组英文名}/
    ├── knowledge_draft.json    # 结构化知识（脚本产出，事实层）
    ├── knowledge_summary.md    # 摘要（2-4KB，供 AI 读取补充业务理解）
    ├── knowledge_ai.md         # AI 增强的自然语言（可选，AI 产出）
    ├── mapping.xlsx            # 字段映射（实体级 + 属性级，CTE 穿透）
    ├── asset_report.html       # 资产说明书（交互式 HTML，含血缘图+SQL高亮）
    ├── tech_design.md          # 技术设计文档（9 章节 Markdown）
    └── impact.xlsx             # 影响清单（影响分析产出，四 Sheet）

# 批量分析额外产出：
{output_dir}/batch_logs/        # 每批详细日志（逐组 [OK]/错误）
```

### mapping.xlsx — 字段映射

给业务和开发看的字段对照表。

- **实体级 mapping**（Sheet 1）：物理源表 → 目标表，含 JOIN 类型和关联条件
- **属性级 mapping**（Sheet 2）：物理源表字段 → 目标表字段，CTE 穿透到物理源表原始字段

### asset_report.html — 资产说明书

交互式报告，浏览器打开即可。含：资产概览（指标卡片+加工模式+加载策略）/ 数据流向图（SVG 血缘图）/ 目标表结构 / 加工逻辑详情 / 字段映射（CTE 穿透血缘链）/ 质量评估。

I 视图场景（资产是对外 I 视图不是 F 表）：自动发现 F→I 封装链路，I 视图字段穿透 F 表，差异高亮。

### impact.xlsx — 影响清单（影响分析产出）

| Sheet | 内容 |
|-------|------|
| 统计摘要 | 一眼看总览：变更总数/有影响/待确认/无影响/未命中 |
| 影响清单 | 🔴有影响 + 🟡待确认的字段级影响（资产视角，以目标字段为一行）|
| 表级影响 | 平切/下线/初始化等整表维度影响 |
| 过滤摘要 | 🟢无影响 + ⚪未命中（可追溯不吵）|

---

## 各能力详解

### 1. 资产文档化（/analyze）

```bash
python {skill_dir}/run.py analyzer \
    --input execution_tasks.xlsx \
    --output ./output/ \
    [--dialect auto] \
    [--ddl-dir ddl/]
```

**工作流程**：
1. 脚本分析：读 Excel/yml → 解析 SQL → 构建拓扑/数据流/字段血缘 → 产出 knowledge_draft.json
2. AI 增强：AI 读摘要补充业务理解，保存 knowledge_ai.md
3. 生成视图：合并 knowledge + AI 增强，生成 mapping/report/techdoc

### 2. 批量文档化（/analyze-batch）

```bash
python {skill_dir}/run.py batch \
    --input execution_tasks.xlsx \
    --output ./output/ \
    [--batch-size 20] \
    [--ddl-dir ddl/]
```

| 机制 | 说明 |
|------|------|
| 子进程隔离 | 每批独立子进程，退出即归还内存，避免大批量内存超限 |
| 同名去重 | 规则组英文名相同但编码不同时，目录名追加编码防覆盖 |
| 单组隔离 | 单个规则组崩溃不拖垮同批其他组 |
| stdout 精简 | stdout 只输出批次进度，详情写 batch_logs/ |

### 3. 字段使用检索（/field-search）

```bash
python {skill_dir}/run.py field_search \
    --input execution_tasks.xlsx \
    --keyword amount,user_id \
    --output field_usage.xlsx
```

### 4. 关联影响分析（/impact-analysis）

源端变更 → 本资产受什么影响，输出影响清单 Excel。

```bash
python {skill_dir}/run.py impact_analyzer \
    --changes 变更清单.xlsx \
    --knowledge {output_dir}/{资产英文名}/knowledge_draft.json \
    --output {output_dir}/{资产英文名}/impact.xlsx \
    --asset {资产英文名}
```

**定位**：影响清单 + 定位器，不是权威影响报告。核心价值 = 自动化两端（确定有/无影响），把中间（判不了）留给人。

**核心能力**：
- 三层过滤：表级命中（平切短路）→ 字段级命中 → 未命中丢弃
- 逐跳传播：沿 field_mappings 追踪字段链路
- 类型兼容性判定：源新类型 vs 目标DDL + cast关口，精确判兼容/截断/溢出
- 16 种变化类型映射表驱动（7 字段级 + 9 表级）
- 数据初始化联动加载策略（刷/不刷时间戳 × 全量/增量）
- SELECT * 断链诚实标待确认

**变化类型 → 影响判定**：

| 类型 | 判定 | 说明 |
|------|------|------|
| 字段类型及长度变化 | 动态 | 兼容性链判定（cast吸收→🟢，截断→🔴） |
| 字段下线/删除 | 🔴 | 字段没了，取不到 |
| 字段值语义变化 | 🟡 | 需语义判断 |
| 表/视图下线 | 🔴 | 列出所有受波及字段 |
| 平切 | 🟡 | 需改表名+术+规则+调度 |
| 数据初始化（不刷时间戳） | 动态 | 全量🟢/增量🔴（隐蔽高风险） |

> 详见 [architecture.md](../architecture.md) §6.2。

---

## 技术细节

### 三层架构

```
① 数据层（analyzer.py）— read_excel / read_yml / CLI
② 理解引擎（engine.py）— SQL 解析 / 字段血缘 / 物理穿透（单一真相）
③ 任务层 — 文档化 / 字段检索 / 影响分析 / 批量编排
```

详见 [architecture.md](../architecture.md)。

### 支持的 SQL 构造

| 构造 | 支持情况 |
|------|---------|
| SELECT / WITH...SELECT | ✓ |
| UNION / UNION ALL / INTERSECT / EXCEPT（含CTE内部UNION） | ✓ |
| CTE（单层 / 嵌套 / 递归穿透） | ✓ |
| JOIN（LEFT/INNER/FULL/CROSS/子查询） | ✓ |
| 窗口函数 / 行转列 / Oracle 方言 | ✓ |
| 注释别名（/* field_name */） | ✓ |
| I 视图封装（F→I 自动发现） | ✓ |

### 加载策略检测

自动检测全量加载（TRUNCATE+INSERT）/ 增量加载（依赖时间戳）/ 分区交换，用于影响分析的数据初始化风险判定。

### 目录结构

```
dws-pipeline-analyzer/
├── SKILL.md              Skill 定义
├── architecture.md       架构设计文档
├── run.py                脚本调度器（统一入口）
├── references/
│   ├── engine.py         理解引擎（SQL解析/拓扑/血缘/物理穿透）
│   ├── analyzer.py       数据层（read_excel/read_yml + CLI）
│   ├── batch.py          批量编排
│   ├── view_generator.py 文档渲染
│   ├── field_search.py   字段检索
│   └── impact_analyzer.py 关联影响分析
├── templates/
│   ├── 变更清单_模板.xlsx  影响分析空模板
│   ├── 变更清单_示例.xlsx  影响分析示例
│   └── 跨资产案例.xlsx     跨资产场景案例
└── commands/             命令定义
    ├── analyze.md
    ├── analyze-batch.md
    ├── field-search.md
    └── impact-analysis.md
```

---

## FAQ

**Q: 没有 DDL 文件能用吗？**
能。DDL 是可选增强，没有时字段类型留空，分析照常进行。

**Q: SQL 里有 CTE，mapping 能穿透吗？**
能。属性级 mapping 穿透 CTE，直接显示物理源表原始字段。

**Q: 批量分析时某组没生成文件夹？**
读 `batch_logs/batch_N.log` 查看该组的错误信息。单组崩溃不会影响其他组。

**Q: 影响分析怎么用？**
先跑 `/analyze` 生成 knowledge，再跑 `/impact-analysis` 传变更清单 + knowledge。详见 `/impact-analysis` 命令。

**Q: 注释里的分号会影响解析吗？**
不会。SQL 预处理在最早一步剔除注释（行注释 -- 和块注释 /* */），注释里的分号不会截断 SQL。

**Q: 在其他 AI agent 上能用吗？**
能。基于 opencode 标准，复制 skill 目录 + 命令文件 + 装依赖即可。
