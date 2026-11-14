# dfq —— 三角洲行动子弹交易决策助手

一个三角洲量化交易的尝试，仅供学习交流使用。

每晚定时运行一次：拉取行情 → 用规则（可选再加大模型）算出每种子弹的买入线、卖出线、止损线和数量 →
生成“交易计划” → 把价格阈值设到 moligod.com 的提醒里 → 把计划用一封邮件发给你。
白天由 moligod 替你盯盘、微信通知你；你进游戏**手动**操作，然后回复邮件记账。系统据此复盘、打分、逐步改进。

**它不会、也永远不该操作游戏客户端。** 唯一的执行方式是“发邮件，由人执行”。

> 第一次用？先看 [`docs/总结报告.md`](docs/总结报告.md) —— 那里按顺序列出了你需要做的每一件事。
> 接口细节见 [`docs/接口探测报告.md`](docs/接口探测报告.md)。

---

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

会生成 `config.yaml`（带中文注释）。**密钥不写进配置文件**，只放在 Windows 的“用户环境变量”里：

| 环境变量 | 用途 | 什么时候需要 |
|---|---|---|
| `DFQ_XTC_TOKEN` | 小涛查接口 token | 必需 |
| `DFQ_SMTP_PASSWORD` | 发信密码（QQ 邮箱填“授权码”） | 开启邮件时 |
| `DFQ_IMAP_PASSWORD` | 收信密码，不设则同上 | 一般不用设 |
| `DFQ_LLM_API_KEY` | 大模型 API Key | 开启大模型时 |
| `DFQ_TAVILY_API_KEY` | 新闻搜索 Key | 开启新闻搜索时 |

设置方法（PowerShell，把引号里换成你的值；**设完要重开终端才生效**）：

```powershell
[Environment]::SetEnvironmentVariable("DFQ_XTC_TOKEN", "你的token", "User")
```

检查是否设好了（只显示“已设置/未设置”，不会显示内容）：

```bash
python -m dfq status
```

## 四、首次运行

```bash
python -m dfq probe
```

实测小涛查接口（约 11～13 次请求，硬上限 15），生成 `docs/接口探测报告-实测.md`，并提示需要改哪些配置。

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
| `python -m dfq trade buy PS12B 200 3550` | 命令行记账（`sell` 同理；`--paper` 记为模拟） |
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
依次检查：是不是从 `mail.owner_address` 那个邮箱回的；是不是点的“回复”（而不是新写一封）；
运行 `python -m dfq mail-poll` 看输出。如果你的邮箱客户端回复时会丢掉引用关系，可以把
`mail.require_thread_match` 改成 `false`（代价是别人伪造你的地址也能记账）。

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
  config.py             配置加载（默认值 + config.yaml + 你确认过的反思调整）
  pipeline.py nodes.py  任务流水线引擎和各个节点
  sources/              数据源：xtc.py 小涛查客户端、mock.py 模拟行情、catalog.py 子弹参考表
  collect.py            行情采集入库
  features.py rules.py  特征计算、规则基线
  llm.py                大模型分析（OpenAI 兼容格式）
  guardrails.py         护栏（代码强制的风控）
  plan.py               计划文件
  mail/                 邮件：收发、正文生成、回复解析、记账
  executors/            执行器插件接口（只有 manual）
  moligod.py            moligod 提醒同步
  ledger.py funds.py    成交、持仓、资金
  review.py reflect.py  复盘打分、每周反思
  intel.py              资讯与观点
  probe.py watch.py     接口探测、每小时异动检查
  log.py http.py db.py timeutil.py fees.py   基础设施
tests/                  单元测试（不联网、不花接口次数）
scripts/                Windows 计划任务脚本
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
