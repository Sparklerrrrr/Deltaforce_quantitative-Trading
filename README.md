# dfq —— 三角洲行动子弹交易决策助手

一个三角洲量化交易的尝试，仅供学习交流使用。

每晚定时运行一次：拉取行情 → 用规则（可选再加大模型）算出每种子弹的买入线、卖出线、止损线和数量 →
生成“交易计划” → 把价格阈值设到 moligod.com 的提醒里 → 把计划用一封邮件发给你。
白天由 moligod 替你盯盘、微信通知你；你进游戏**手动**操作，然后回复邮件记账。系统据此复盘、打分、逐步改进。

**它不会、也永远不该操作游戏客户端。** 唯一的执行方式是“发邮件，由人执行”。

> 第一次用？先看 [`docs/总结报告.md`](docs/总结报告.md) —— 那里按顺序列出了你需要做的每一件事。
> 接口细节见 [`docs/接口探测报告.md`](docs/接口探测报告.md)。

---

## 〇、图形界面（DFQ.exe）

不想敲命令的话，用图形界面就能完成日常的所有操作：运行、看计划、记账、复盘、录入资讯、填密钥。

### 安装（给使用者）

双击 `DFQ-Setup-版本号.exe`，按向导操作：

- **选择安装文件夹**：默认 `D:\DFQ`（没有 D 盘时放在当前用户的程序目录）。不需要管理员权限。
  不能选盘符根目录、Program Files、Windows 目录；选的文件夹里已有别的文件时会先提醒。
- **快捷方式**：桌面（默认勾选）、开始菜单（默认不勾选）。
- **程序产生的所有文件都只在安装文件夹里**：`config.yaml`、`.env`（密钥）、`data\` 下的数据库、行情缓存、
  日志、界面组件（WebView2）的数据、临时文件。程序本体是“文件夹版”，启动时不会解压到 C 盘临时目录。
  装在安装文件夹之外的只有你勾选的快捷方式，以及 Windows“应用和功能”里的卸载登记，卸载时都会删除。

界面用 Windows 自带的 Edge WebView2 显示（Windows 10/11 一般已经装好；如果打开后是空白窗口，
到微软官网安装“WebView2 Runtime”即可）。

### 卸载

在“应用和功能”里卸载，或双击安装文件夹里的 `unins000.exe`（和 `DFQ.exe` 在同一个文件夹）。会让你选：

| 选项 | 安装文件夹里留下什么 |
|---|---|
| 保留配置和交易数据，删除密钥（默认） | `config.yaml`、界面设置、`data\dfq.db`（交易记录与行情历史）、采纳的调参、历次计划、报告和备份 |
| 保留配置、交易数据和密钥 | 上面这些 + `.env` + moligod 登录状态 |
| 完全删除 | 什么都不留，连文件夹一起删除 |

无论选哪一项，程序本体、缓存、日志、临时文件和程序创建的计划任务都会删除。
以后重装到同一个文件夹，留下的配置和交易记录会直接接着用。
静默卸载：`unins000.exe /VERYSILENT /KEEP=data|data_env|none`（不写 `/KEEP` 时按 `data` 处理）。
删除时遇到符号链接 / 目录联接，只删除链接本身，不会进入它指向的位置。

### 程序文件夹里有什么

首次启动时程序会自动建好目录，并从模板生成 `config.yaml` 和 `.env`（`.env` 里的密钥全部为空，在“设置”页填写）；
已有的文件一概不覆盖，升级安装也只替换程序本体。每个文件的归属定义在 `dfq/layout.py`：

| 内容 | 位置 | 卸载默认 | 说明 |
|---|---|---|---|
| 程序本体 | `DFQ.exe`、`_internal\`、`scripts\` | 删除 | 安装 / 升级时覆盖 |
| 用户配置 | `config.yaml`、`data\ui-settings.json` | 保留 | 首次启动从模板生成 |
| 密钥 | `.env`、`data\moligod_profile\` | 删除（可选保留） | 首次启动生成空模板，由你填写 |
| 交易数据 | `data\dfq.db`、`overrides.yaml`、`plans\`、`outbox\`、`inbox\`、`reflections\`、`reports\`、`backups\` | 保留 | 自动初始化 |
| 缓存 | `data\cache\`、`data\probe\`、`data\webview\` | 删除 | 可以随时删除 |
| 日志 | `data\logs\`、`data\exports\` | 删除 | 日志已脱敏 |
| 临时文件 | `data\tmp\` | 删除 | 启动时自动清理一天前的残留 |
| 演示数据 | `data\mock\` | 删除 | 可以随时删除 |

配置文件写坏了：“设置 → 配置文件 → 恢复默认配置…”会先把现在的 `config.yaml` 备份到 `data\backups`，再用模板重新生成。
数据库损坏时程序只会提示、不做任何修改，请先备份 `data` 文件夹。

### 配置导入 / 导出

在“设置”页：

- **导出**：点“导出配置…”，选择保存到哪个文件夹，得到 `DFQ配置备份-时间.zip`。
  默认只含 `config.yaml`；勾选“同时导出密钥”才会加上 `.env`，并会再提醒一次（密钥是明文）。
- **导入**：把导出的 `.zip`、或者 `config.yaml` / `.env` 文件拖进虚线框（也可以点击选择）。
  导入前会用和启动时一样的严格规则检查配置，有问题会说明原因并且什么都不改；
  确认导入时，旧文件先备份到 `data\backups\时间\`。

### 生成安装包（给开发者）

在项目根目录双击（需要 Python 3.11+；要生成安装包还需要装 [Inno Setup](https://jrsoftware.org/isinfo.php)）：

```
scripts\build_exe.bat
```

它会自动建 `.venv`、装依赖和 PyInstaller，生成：

- `dist\DFQ\`：程序文件夹（`DFQ.exe` + `_internal\`），可以直接运行
- `dist\DFQ-Setup-版本号.exe`：安装包（没找到 Inno Setup 时跳过这一步；也可以用环境变量 `DFQ_ISCC` 指定 ISCC.exe）

安装脚本是 `installer\DFQ.iss`。`DFQ.exe` 带参数时就是命令行模式（例如计划任务里的 `DFQ.exe run`），
输出写到安装文件夹的 `data\logs\task-日期.log`。

| 页面 | 能做什么 |
|---|---|
| 首页 | 演示一遍（模拟数据）、运行（只用规则 / 规则 + 大模型）、查看状态、接口实测、读取邮件回复 |
| 最新计划 | 显示最近一次生成的计划（与邮件内容相同） |
| 记账 | 记一笔成交（可选模拟/真实账本）、设余额、查看最近成交、撤销记错的成交 |
| 复盘 | 绩效对比、给到期建议打分、反思记录与采纳 |
| 资讯录入 | 粘贴 UP 主文稿、群聊、公告并入库 |
| 设置 | 填写/清除密钥、生成和检查 config.yaml、配置导入/导出、打开数据文件夹、登录 moligod、创建计划任务 |

- 会消耗接口次数、调用大模型或改动账本的操作，执行前都会弹窗确认。
- 在右侧“运行环境”里选“模拟数据”后，所有操作都用模拟行情、写到 `data\mock\`，不联网、不花钱。
- **密钥只能填、不能看**：输入框用圆点遮住，保存到项目文件夹的 `.env` 文件（已加入 `.gitignore`，不会被提交），
  界面上只显示“已设置/未设置”。保存后本程序立即生效，命令行和计划任务下次运行时自动读取。
- 界面里每个按钮调用的都是下面的同一套命令，校验、护栏、两本账的隔离完全一样。
  会花钱或改账本的操作，确认是在程序后端强制执行的，不只是界面弹个窗。
- **关闭窗口**：点右上角 × 时会问你“最小化到托盘”还是“退出程序”，可以勾选“记住我的选择”。
  收进托盘后双击托盘图标重新打开、右键可以退出；以后想改，在“设置 → 窗口”里选“每次询问 / 最小化到托盘 / 退出程序”。
  如果还有操作在运行，即使设成“退出程序”也会先提醒。程序只会开一个：已经在运行（包括在托盘里）时再双击，
  只会把原来的窗口叫出来。这个偏好保存在 `data\ui-settings.json`。
- 右上角可以切换深色 / 浅色。遇到问题时，点“运行日志”右上角的导出按钮，会把日志打包成
  `data\exports\DFQ-logs-时间.zip`（日志已脱敏，不含 `.env`），反馈问题时发这个文件即可。
- 不想打包也可以在项目文件夹里直接运行：`.venv\Scripts\python -m dfq gui`（这时用的是项目文件夹里的 `config.yaml`、`.env`、`data\`）。
- 打包后的 exe 不含 Playwright，所以“登录 moligod”需要用 `python -m dfq gui` 方式打开、并按下文安装 Playwright 才能用。

## 一、安装

需要 Windows + Python 3.11 或更高版本。在项目根目录打开 PowerShell：

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

以后所有命令都用 `.venv\Scripts\python -m dfq ...`（下文简写成 `python -m dfq ...`）。

可选组件（需要时再装）：

```bash
.venv\Scripts\pip install playwright
```

```bash
.venv\Scripts\python -m playwright install chromium
```

## 二、不需要任何账号就能先看效果

```bash
python -m dfq run --mock --offline --show
```

这会用**模拟行情**（价格是假的）把整条流程跑一遍，把邮件正文打印出来，数据写到 `data/mock/`，
不花任何接口次数，也不发真邮件。

## 三、配置

```bash
python -m dfq init
```

会生成 `config.yaml`（带中文注释）。**密钥不写进配置文件**，放在项目文件夹里的 `.env` 文件中
（和 `config.yaml`、`DFQ.exe` 同级）。和常见的 AI 插件一样，`.env` 已加入 `.gitignore`，不会被提交或推送：

| 名字 | 用途 | 什么时候需要 |
|---|---|---|
| `DFQ_XTC_TOKEN` | 小涛查接口 token | 必需 |
| `DFQ_SMTP_PASSWORD` | 发信密码（QQ 邮箱填“授权码”） | 开启邮件时 |
| `DFQ_IMAP_PASSWORD` | 收信密码，不设则同上 | 一般不用设 |
| `DFQ_LLM_API_KEY` | 大模型 API Key | 开启大模型时 |
| `DFQ_TAVILY_API_KEY` | 新闻搜索 Key | 开启新闻搜索时 |

填写方法（任选一种）：

- 在 DFQ.exe 的“设置”页点“填写 / 更换”，程序会自动创建并写入 `.env`；
- 或者把 `.env.example` 复制一份改名为 `.env`，用记事本在等号后面填上值，例如 `DFQ_XTC_TOKEN=你的token`。

几点说明：

- 如果系统环境变量里也设置了同名的（例如以前用 `SetEnvironmentVariable` 设过），**以系统环境变量为准**。
- `.env` 是明文文件：它防的是不小心提交、上传、贴进日志，防不了本机上的恶意程序。不要把它发给别人或截图。
- 换电脑或重新克隆项目时，`.env` 不会跟着走，需要重新填写。

检查是否设好了（只显示“已设置/未设置”，不会显示内容）：

```bash
python -m dfq status
```

## 四、首次运行

```bash
python -m dfq probe
```

实测小涛查接口（约 11～13 次请求，硬上限 15），生成 `data/reports/接口探测报告-实测.md`，并提示需要改哪些配置。

```bash
python -m dfq balance set 5000000
```

把数字换成你的哈夫币余额（之后会按成交自动推算）。

```bash
python -m dfq run --offline --show
```

第一次真实运行（只用规则、不用大模型）。默认是**模拟模式**：邮件标题带“模拟”，建议只记录、用于打分。

## 五、命令一览

| 命令 | 作用 |
|---|---|
| `python -m dfq run` | 每晚主流程。`--offline` 不用大模型；`--dry-run` 不花次数、不发信、不改 moligod；`--mock` 用模拟行情；`--force` 忽略运行间隔；`--show` 打印邮件 |
| `python -m dfq probe` | 实测接口，生成探测报告 |
| `python -m dfq status` | 配置、环境变量、接口用量、数据覆盖、持仓 |
| `python -m dfq plan` | 查看最新计划 |
| `python -m dfq gui` | 打开图形界面 |
| `python -m dfq trade buy PS12B 200 3550` | 命令行记账（`sell` 同理；`--paper` 记为模拟） |
| `python -m dfq trade-undo 12` | 撤销编号为 12 的成交（编号见界面“记账”页） |
| `python -m dfq balance set 5000000` | 设置余额基准；不带参数则查看 |
| `python -m dfq mail-poll` | 读取邮件回复并记账 |
| `python -m dfq mail-test` | 发一封测试邮件 |
| `python -m dfq perf` | 复盘绩效：最终建议 vs 规则基线 vs 什么都不做 |
| `python -m dfq reflect list` | 查看反思建议；`show N` / `accept N` / `reject N` |
| `python -m dfq note add --source bili --name moligod --file 文稿.txt` | 手动录入资讯（也可以把文件放进 `manual_notes/`） |
| `python -m dfq moligod-login` | 打开浏览器，手动登录 moligod 一次 |
| `python -m dfq moligod-sync` | 按最新计划同步提醒（`--dry-run` 只看不改） |
| `python -m dfq watch` | 可选的每小时异动检查（默认关闭） |

所有命令都可以加 `--mock`，表示操作 `data/mock/` 里的模拟数据。

## 六、每天怎么用

1. 晚上计划任务自动跑，你收到一封邮件。**标题就是指令**，例如：
   `【计划】买 PS12B ≤3600 ×200｜卖 SS190 ≥5400｜有效至 11-14 21:00`
2. 正文按游戏里的操作顺序写：搜什么、什么价、买多少；然后是理由、风险、止损线。
3. moligod 提醒到线后微信通知你，你进游戏手动挂单。
4. 成交后**直接回复那封邮件**记账，一行一笔：

   ```
   买 PS12B 200 3550
   卖 SS190 150 5420
   ```

   格式很宽松：`买入 ps12b 200发 @3550`、`buy PS12B 200@3550`、`卖 M995 1.2k 4100` 都认。
   没看懂的地方会回信告诉你。不回复 = 视为没执行。
   回复【模拟计划】记入模拟账本，回复【计划】记入真实账本，两者互不影响。
   （首次使用前要先配置 `mail.auth.trusted_authserv`，见常见问题。）
5. 每周看一眼 `python -m dfq perf`：系统有没有用，只看这一个数。

## 七、计划任务

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
```

创建两个任务：每晚 21:30 运行 `run`，每 30 分钟运行 `mail-poll`。可调：
`-NightlyAt 22:00 -PollMinutes 60`；加 `-WithWatch` 额外创建（默认禁用的）每小时异动检查；`-Remove` 删除全部。
运行输出在 `data\logs\task-日期.log`。

## 八、常见问题

**邮件标题出现“⚠行情数据不全”**
行情没采到或没采全。看正文最后的“提示与降级”：通常是 token 未设置、当日次数用完、或接口故障。
这种情况下系统宁可全部观望，也不会用猜的数据出买卖线。

**提示“子弹列表返回为空”**
多半是分类索引不对。运行 `python -m dfq probe`，按它的提示修改 `xtc.ammo_key1` / `ammo_key2`。

**提示“今日小涛查请求已用 60/60 次”**
到了每日上限（`xtc.daily_limit`）。正常每晚只用十几次；如果你反复手动运行会更多，
不过同一请求 10 分钟内会走缓存。确认无误后可以调高上限。

**QQ 邮箱发不出去**
`DFQ_SMTP_PASSWORD` 要填“授权码”而不是 QQ 密码：QQ 邮箱网页版 → 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码。
然后 `python -m dfq mail-test`。

**回复了邮件但没记上账**
一封回复要连过四关才会记账，运行 `python -m dfq mail-poll --dry-run` 会告诉你卡在哪一关：

1. 发件人必须是 `mail.owner_address`（且 From 里只有这一个地址）。
2. 身份验证：你的邮箱服务商确认它确实来自你的邮箱域（SPF / DKIM / DMARC）。
   需要先在 `mail.auth.trusted_authserv` 里填服务商的验证标识——**没填之前所有回复都不会记账**。
   `mail-poll --dry-run` 会列出邮件里出现的标识供你确认。如果你的邮箱根本不提供这类信息，
   邮件记账就用不了，请改用命令行记账（`python -m dfq trade ...`）。
3. 必须是对某封**计划邮件**的回复（点“回复”，不要新写一封）。
4. 那份计划是模拟还是真实，决定这笔成交记进哪本账（见下一条）。对不上计划的不记账。

身份验证没通过的邮件不会收到任何回信；通过了但没记上的，会回信说明原因。

**模拟计划的成交记到哪里去了**
回复【模拟计划】邮件的成交只进**模拟账本**，回复【计划】邮件的成交只进**真实账本**，两本账互不影响：
真实持仓、真实余额、真实盈亏和实盘计划的风控只看真实账本。归属是发计划时记在本地数据库里的，
不看你回复里怎么写。`python -m dfq status` 和 `perf` 会把两本账分开列出。
命令行记账默认记真实账本，模拟成交要加 `--paper`。

**提示“配置有 N 处问题，已拒绝运行”**
配置会被严格检查：数值超出范围、类型不对、键名拼错都会报错，并一次列全。照着提示改即可。
最常见的是把比例写成了百分数——5% 要写 `0.05`，不是 `5`。

**大模型地址填 http:// 被拒绝**
API Key 不能走不加密的连接，所以 `llm.base_url` 必须是 `https://`。唯一的例外是模型跑在本机
（`http://localhost` 或 `http://127.0.0.1`），并且把 `llm.allow_http_local` 设为 `true`。
局域网里别的机器也不算本机。

**邮件里提示“有 N 篇资讯已隔离”**
文稿里出现了像是在对大模型下指令的话（“忽略以上规则”“你现在是……”之类），为防止它操纵模型，
整篇被隔离、没有参与分析。如果是误判，把那几句删掉后重新放入即可。
这道筛查只是减少风险，真正的保证是：模型的任何输出都必须经过代码风控，改不了仓位上限、止损范围和配置。

**moligod 同步失败**
任何失败都不会重试，而是把要设的阈值列在邮件里让你手动设。常见原因：登录过期（重新 `moligod-login`）、
页面改版（元素找不到）、两边子弹名称不一致（在 `watchlist` 里给该品种加 `moligod_name`）。

**想从模拟切到实盘**
先确认 `perf` 里有足够样本（至少 20～30 笔触发）且结果为正，再把 `general.paper_mode` 改成 `false`。

**怎么撤销已经采纳的反思建议**
打开 `data/overrides.yaml`，删掉对应那一行。

**终端里中文乱码**
PowerShell 里先执行 `chcp 65001`，或改用 Windows Terminal。

## 九、目录结构

```
dfq/                    程序本体
  cli.py                命令行入口
  webui/                图形界面：api.py 后端（可单测）、tasks.py 首页任务清单、app.py 开窗口、static/ 页面
  gui_core.py           界面与命令行之间的公共部分（执行命令、密钥存取、查询）
  envfile.py            本地密钥文件 .env 的读写
  config.py             配置加载（默认值 + config.yaml + 你确认过的反思调整）
  pipeline.py nodes.py  任务流水线引擎和各个节点
  sources/              数据源：xtc.py 小涛查客户端、mock.py 模拟行情、catalog.py 子弹参考表
  collect.py            行情采集入库
  features.py rules.py  特征计算、规则基线
  llm.py                大模型分析（OpenAI 兼容格式）
  guardrails.py         护栏（代码强制的风控）
  plan.py               计划文件
  mail/                 邮件：收发、正文生成、回复解析、发件人身份验证、记账
  config_check.py       配置的严格校验
  untrusted.py          外部文本的隔离、筛查、清洗（防提示词注入）
  executors/            执行器插件接口（只有 manual）
  moligod.py            moligod 提醒同步
  ledger.py funds.py    成交、持仓、资金
  review.py reflect.py  复盘打分、每周反思
  intel.py              资讯与观点
  probe.py watch.py     接口探测、每小时异动检查
  log.py http.py db.py timeutil.py fees.py   基础设施
tests/                  单元测试（不联网、不花接口次数）
scripts/                Windows 脚本：计划任务、打包 DFQ.exe
docs/                   接口探测报告、总结报告
manual_notes/           手动粘贴的资讯文稿
config.example.yaml     配置示例
data/                   运行数据（不入库）：数据库、缓存、计划、邮件、日志
```

## 十、开发

```bash
python -m pytest
```

测试会封死真实网络并清空所有 `DFQ_*` 环境变量，所以绝不会消耗接口次数。
