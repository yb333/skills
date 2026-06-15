# DWS Skills

适用于 [OpenCode](https://github.com/sst/opencode) 的技能集。

## 安装

下载本仓库 zip → 解压 → 运行安装命令：

```bash
# macOS / Linux
bash install.sh

# Windows（双击 install.bat 或命令行执行）
install.bat
```

安装命令会自动：扫描所有 skill → 创建 venv → 装依赖 → 复制 skill + 命令到 opencode 目录。

> 也可以只装指定 skill：`bash install.sh dws-pipeline-analyzer`
>
> 也可以装到当前项目：`bash install.sh -l`

安装完成后，在 opencode AI 对话中直接使用：

```
/analyze @execution_tasks.xlsx
```

## 内容

| 目录 | 说明 |
|------|------|
| [dws-pipeline-analyzer/](dws-pipeline-analyzer/) | 制品包分析器：从 execution_tasks.xlsx 提取 ETL 知识，生成字段映射、资产说明书、技术设计文档 |
| [frontend-slides/](frontend-slides/) | HTML 演示文稿生成器 |
| [commands/](commands/) | 命令定义（/analyze 等） |
| [install.sh](install.sh) | macOS/Linux 安装器 |
| [install.bat](install.bat) | Windows 安装器（双击即可执行） |
| [install.ps1](install.ps1) | Windows 安装器（PowerShell 版，备用） |

## 手工安装

如果不想用安装器，手工复制也可以：

```bash
# 1. 复制 skill
cp -r dws-pipeline-analyzer/ ~/.config/opencode/skills/

# 2. 复制命令
cp commands/analyze.md ~/.config/opencode/commands/

# 3. 装依赖
pip install openpyxl sqlglot
```

详细文档：[dws-pipeline-analyzer/README.md](dws-pipeline-analyzer/) · [用户指南](dws-pipeline-analyzer/user-guide.md)
