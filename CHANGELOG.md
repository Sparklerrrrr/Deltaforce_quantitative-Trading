# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号见 `dfq/__init__.py`。

## [未发布] — 发布前修复（分支 fix/pre-release-review）

外部评审提出的 14 条问题逐条核实后修复。各条结论见 `docs/安全审计报告.md`。

### 账本、风控与邮件
- 账本、费率与风控、成交统计、邮件回复处理、外部接口与大模型的修复（评审第 1–8 条）。详见 docs/发布前审查报告.md。

### 数据隔离与安装包（评审第 9、10 条）
- `dfq status` 显示每个密钥来自 `.env` 还是系统环境变量（系统环境变量会盖过 `.env`；只显示来源，不显示值）。
- 打包后的 `DFQ.exe` 在命令行模式下（计划任务）一律使用安装文件夹的 `config.yaml`，不会因为参数顺序退回到“当前目录的 config.yaml”。
- 安装包排除个人文件（`config.yaml`、`.env`、`data\`、数据库、日志、邮件），升级时先清掉旧的程序内部文件。
- 卸载时只删除指向本安装文件夹的计划任务，另一个安装文件夹的同名任务不受影响。
- 明确卸载归属：`data\backups\`（含数据库备份）保留，`data\exports\`（导出的日志包）删除。
- 中文安装界面的语言文件放进仓库，要求 Inno Setup 6.5 或更新版本。
- 新增安装包验收脚本 `scripts/release_test.ps1`。

### 敏感信息、CI 与发布（评审第 11–14 条）
- 新增 `scripts/scan_secrets.py`：扫描当前文件、完整 Git 历史、打包 / 安装目录，输出打码，区分疑似真实 / 占位符 / 测试假值 / 误报。
- 构建脚本打包后先扫描，发现个人文件就不生成安装包；支持无人值守构建（`CI`、`DFQ_REQUIRE_INSTALLER`）。
- 新增 GitHub Actions：Windows × Python 3.11–3.14 测试、ruff、vermin、mypy、pip-audit、敏感信息扫描、构建并验收安装包；
  推送 `v*` 标签时自动构建并生成带 SHA-256 校验值和构建记录的草稿 Release（两个工作流尚未在 GitHub 上实际运行过）。
- 开发工具移到 `requirements-dev.txt`（版本固定），`requirements.txt` 只留运行需要的依赖。
- 重写 `docs/安全审计报告.md`；新增 `docs/安装说明.md`（给用户）、`docs/发布流程.md`（给维护者）。

## [1.0.0] — 2026-11-15

- 首个带图形界面（`DFQ.exe`）和安装包的版本：定时采集行情、生成交易计划、邮件通知人工执行、记录成交与复盘。
