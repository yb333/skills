# DWS Pipeline Analyzer

从执行平台制品包（execution_tasks.xlsx）中反向提取 ETL 知识，自动生成字段映射、资产说明书、技术设计文档。

## 安装

在仓库根目录运行安装器即可（自动安装所有 skill + 命令 + 依赖）：

```bash
# macOS / Linux
bash install.sh

# Windows（双击 install.bat 或命令行执行）
install.bat
```

或手工安装：

```bash
# 1. 复制 skill
cp -r dws-pipeline-analyzer/ ~/.config/opencode/skills/

# 2. 复制命令
cp commands/analyze.md ~/.config/opencode/commands/

# 3. 装依赖
pip install openpyxl sqlglot
```

### 依赖

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | Windows 用 `python`，macOS/Linux 用 `python3` |
| openpyxl | 3.1+ | Excel 读写 |
| sqlglot | 23.0+ | SQL AST 解析 |

> AI 首次运行脚本时会自动检测依赖，缺失则自动 `pip install openpyxl sqlglot`。

## 使用

在 opencode AI 对话中：

```
/analyze @execution_tasks.xlsx
```

或直接说："分析这个制品包""帮我看看这个 ETL""生成 mapping 文件"。

详细用法见 [user-guide.md](user-guide.md)。

## 目录结构

```
dws-pipeline-analyzer/
├── SKILL.md                          # Skill 定义（AI 读取）
├── run.py                            # 脚本分发器（统一入口）
├── requirements.txt                  # Python 依赖
├── user-guide.md                     # 用户指南
└── references/
    ├── analyzer.py                   # 分析脚本（核心）
    ├── view_generator.py             # 视图生成脚本
    └── templates/
        └── asset_report.html         # HTML 报告模板
```
