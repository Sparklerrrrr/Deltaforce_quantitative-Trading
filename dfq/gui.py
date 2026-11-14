"""dfq 图形界面（tkinter，Python 自带，无需额外安装）。

打开方式：
  - 双击打包好的 DFQ.exe（见 scripts/build_exe.bat）；
  - 或在项目目录运行  python -m dfq gui

设计原则：
  - 每个按钮都只是调用一条现有的 dfq 命令，输出实时显示在窗口里。
    界面不含任何交易逻辑，所以命令行有的校验、护栏、两本账的隔离，这里全都一样生效。
  - 会花钱（消耗接口次数）或改动账本的操作，执行前一律弹窗确认。
  - 密钥只能输入、不能查看：输入框用圆点遮住，保存后立刻清空，界面上只显示“已设置/未设置”。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, gui_core

PAD = 6


class App:
    def __init__(self, root: tk.Tk, config_path: Path, store: gui_core.SecretStore | None = None):
        self.root = root
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        self.store = store or gui_core.SecretStore()
        self.out_q: queue.Queue = queue.Queue()
        self.busy = False
        self.continue_event = threading.Event()
        self.cfg = None
        self.target: ScrolledText | None = None   # 当前命令的输出除了控制台，还要显示在哪个页面
        self.action_buttons: list[ttk.Button] = []

        loaded = self.store.load_into_process()

        root.title(f"DFQ 子弹交易决策助手 {__version__}")
        root.geometry("1040x760")
        root.minsize(860, 600)
        self._style()

        top = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        self.mode_label = ttk.Label(top, text="", font=("", 10, "bold"))
        self.mode_label.pack(side="left")
        self.mock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="使用模拟数据（不联网、不花接口次数，数据单独存放）",
                        variable=self.mock_var, command=self.refresh_all).pack(side="right")

        bar = ttk.Frame(root, padding=(PAD, 0, PAD, PAD))
        bar.pack(side="bottom", fill="x")             # 先占住底部，窗口再小也不会被挤掉
        self.status = ttk.Label(bar, text="就绪")
        self.status.pack(side="left")
        self.continue_btn = ttk.Button(bar, text="我已完成，继续", command=self.continue_event.set)
        ttk.Button(bar, text="清空输出", command=self.clear_console).pack(side="right")

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)

        con_frame = ttk.LabelFrame(paned, text="运行输出")
        paned.add(con_frame, weight=2)
        self.console = ScrolledText(con_frame, height=10, wrap="word", font=("Consolas", 10))
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")

        self._tab_home()
        self._tab_plan()
        self._tab_ledger()
        self._tab_review()
        self._tab_intel()
        self._tab_settings()

        self.root.after(80, self._drain)
        self.refresh_all()
        if loaded:
            self.note(f"已从系统读取到：{'、'.join(loaded)}（只读取是否存在，不显示内容）\n")
        if not self.config_path.exists():
            self.note(f"还没有配置文件 {self.config_path.name}，当前使用内置默认配置。"
                     f"可以在“设置”页点“生成 config.yaml”。\n")

    # ------------------------------------------------------------------ 外观
    def _style(self):
        st = ttk.Style(self.root)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
        st.configure("Big.TButton", padding=(10, 6))
        st.configure("Hint.TLabel", foreground="#666666")

    # ------------------------------------------------------------------ 公共
    def common_args(self) -> list[str]:
        args = ["--config", str(self.config_path)]
        if self.mock_var.get():
            args.append("--mock")
        return args

    def log(self, text: str):
        """命令的输出：显示在下方，也显示在当前结果页（如果有）。"""
        self.out_q.put(("console", text))

    def note(self, text: str):
        """界面自己的提示（开始/结束标记等）：只显示在下方的运行输出里。"""
        self.out_q.put(("note", text))

    def clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _append(self, widget: ScrolledText, text: str, follow: bool = True):
        widget.configure(state="normal")
        widget.insert("end", text)
        if follow:
            widget.see("end")
        widget.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "console":
                    self._append(self.console, payload)
                    if self.target is not None:
                        self._append(self.target, payload, follow=False)
                elif kind == "note":
                    self._append(self.console, payload)
                elif kind == "done":
                    payload()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def set_busy(self, busy: bool, label: str = ""):
        self.busy = busy
        self.status.configure(text=f"运行中：{label} …" if busy else "就绪")
        for b in self.action_buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        if not busy:
            self.continue_btn.pack_forget()

    def run(self, label: str, argv: list[str], *, target: ScrolledText | None = None,
            on_done=None, needs_continue: bool = False):
        """在后台线程执行一条 dfq 命令。"""
        if self.busy:
            messagebox.showinfo("请稍候", "上一个操作还没结束。")
            return
        full = [argv[0], *self.common_args(), *argv[1:]]
        if target is not None:
            target.configure(state="normal")
            target.delete("1.0", "end")
            target.configure(state="disabled")
        self.target = target
        self.set_busy(True, label)
        if needs_continue:
            self.continue_btn.pack(side="right", padx=PAD)
        self.note(f"\n▶ {label}\n")

        def work():
            code = gui_core.run_cli(full, self.log,
                                    continue_event=self.continue_event if needs_continue else None)

            def finish():
                self.note(f"■ 结束（{'成功' if code == 0 else f'返回码 {code}'}）\n")
                if self.target is not None:
                    self.target.see("1.0")            # 计划、绩效这类结果从头看
                self.target = None
                self.set_busy(False)
                self.refresh_all()
                if on_done:
                    on_done(code)
            self.out_q.put(("done", finish))

        threading.Thread(target=work, daemon=True).start()

    def btn(self, parent, text, command, *, big=False, **pack):
        b = ttk.Button(parent, text=text, command=command, style="Big.TButton" if big else "TButton")
        b.pack(**({"side": "left", "padx": 3, "pady": 3} | pack))
        self.action_buttons.append(b)
        return b

    def confirm(self, title: str, text: str) -> bool:
        return messagebox.askyesno(title, text, icon="warning")

    def refresh_all(self):
        self.cfg, err = gui_core.load_config(self.config_path)
        if self.cfg is not None and self.mock_var.get():
            self.cfg.set("general.data_dir", str(self.cfg.data_dir / "mock"))
        if err:
            self.mode_label.configure(text="⚠ 配置有误，请到“设置”页检查", foreground="#b00020")
        else:
            mode = "模拟模式（paper_mode）" if self.cfg.paper else "实盘记账模式"
            data = "模拟数据" if self.mock_var.get() else "真实行情"
            self.mode_label.configure(text=f"{mode}　·　{data}　·　文件夹：{self.base.name}",
                                      foreground="#1f6f3f" if self.cfg.paper else "#b35c00")
        self._refresh_items()
        self._refresh_trades()
        self._refresh_secrets()

    # ------------------------------------------------------------------ 首页
    def _tab_home(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="首页")

        g = ttk.LabelFrame(f, text="每日流程", padding=PAD)
        g.pack(fill="x", pady=(0, PAD))
        self.force_var = tk.BooleanVar(value=False)
        self.btn(g, "▶ 演示一遍（模拟数据）", self.do_demo, big=True)
        self.btn(g, "▶ 运行（只用规则）", lambda: self.do_run(offline=True), big=True)
        self.btn(g, "▶ 运行（规则 + 大模型）", lambda: self.do_run(offline=False), big=True)
        ttk.Checkbutton(g, text="忽略运行间隔", variable=self.force_var).pack(side="left", padx=8)

        g2 = ttk.LabelFrame(f, text="检查", padding=PAD)
        g2.pack(fill="x", pady=(0, PAD))
        self.btn(g2, "查看状态", lambda: self.run("查看状态", ["status"]))
        self.btn(g2, "接口实测（probe）", self.do_probe)

        g3 = ttk.LabelFrame(f, text="邮件", padding=PAD)
        g3.pack(fill="x", pady=(0, PAD))
        self.btn(g3, "检查邮件回复（只看不记账）", lambda: self.run("检查邮件回复", ["mail-poll", "--dry-run"]))
        self.btn(g3, "读取邮件回复并记账", self.do_mail_poll)
        self.btn(g3, "发一封测试邮件", lambda: self.run("发送测试邮件", ["mail-test"]))

        hint = ("第一次使用：先点“演示一遍”看看效果；再到“设置”页填写小涛查 token，回到这里点“接口实测”。\n"
                "“运行”默认是模拟模式：计划只用于记录和打分，不计入真实持仓。所有操作的输出显示在下方。")
        ttk.Label(f, text=hint, style="Hint.TLabel", justify="left").pack(anchor="w", pady=PAD)

    def do_demo(self):
        self.mock_var.set(True)
        self.refresh_all()
        self.run("演示（模拟数据、只用规则）", ["run", "--offline", "--force", "--show"],
                 on_done=lambda c: self.load_plan())

    def do_run(self, offline: bool):
        args = ["run", "--show"] + (["--offline"] if offline else []) + (["--force"] if self.force_var.get() else [])
        label = "运行（只用规则）" if offline else "运行（规则 + 大模型）"
        if not self.mock_var.get():
            what = ("会请求小涛查接口（每晚约 13 次，首次约 21 次，按次计费，每日上限见配置）"
                    + ("" if offline else "，并调用大模型（按 token 计费）")
                    + "；如果开了邮件和 moligod 同步，还会发邮件、改提醒。")
            if not self.confirm("确认运行", f"{label}{what}\n\n确定继续吗？"):
                return
        self.run(label, args, on_done=lambda c: self.load_plan())

    def do_probe(self):
        if self.mock_var.get():
            self.run("接口实测（模拟数据演练）", ["probe"])
            return
        if not self.store.is_set("DFQ_XTC_TOKEN"):
            messagebox.showwarning("还没有 token", "请先到“设置”页填写小涛查 token。")
            return
        if self.confirm("确认接口实测", "接口实测会真实请求小涛查，约 11～13 次，程序自带 15 次上限，按次计费。\n\n"
                        "结果会写到 docs/接口探测报告-实测.md。确定继续吗？"):
            self.run("接口实测（probe）", ["probe"])

    def do_mail_poll(self):
        if self.confirm("确认读取并记账", "将读取邮箱里对计划邮件的回复，并把成交写入账本\n"
                        "（模拟计划的回复只进模拟账本）。\n\n建议先点“检查邮件回复（只看不记账）”。确定继续吗？"):
            self.run("读取邮件回复并记账", ["mail-poll"])

    # ------------------------------------------------------------------ 计划
    def _tab_plan(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="最新计划")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.btn(row, "刷新", self.load_plan)
        ttk.Label(row, text="这里显示最近一次生成的计划（与邮件内容相同）。过期的计划会标明作废。",
                  style="Hint.TLabel").pack(side="left", padx=8)
        self.plan_text = ScrolledText(f, wrap="word", font=("Microsoft YaHei UI", 10))
        self.plan_text.pack(fill="both", expand=True, pady=(PAD, 0))
        self.plan_text.configure(state="disabled")

    def load_plan(self):
        self.run("查看最新计划", ["plan"], target=self.plan_text)

    # ------------------------------------------------------------------ 记账
    def _tab_ledger(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="记账")

        form = ttk.LabelFrame(f, text="记一笔成交", padding=PAD)
        form.pack(fill="x")
        self.side_var = tk.StringVar(value="buy")
        ttk.Radiobutton(form, text="买入", value="buy", variable=self.side_var).grid(row=0, column=0, padx=3)
        ttk.Radiobutton(form, text="卖出", value="sell", variable=self.side_var).grid(row=0, column=1, padx=3)
        ttk.Label(form, text="品种").grid(row=0, column=2, padx=(12, 3))
        self.item_var = tk.StringVar()
        self.item_box = ttk.Combobox(form, textvariable=self.item_var, width=22)
        self.item_box.grid(row=0, column=3)
        ttk.Label(form, text="数量").grid(row=0, column=4, padx=(12, 3))
        self.qty_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.qty_var, width=9).grid(row=0, column=5)
        ttk.Label(form, text="成交单价").grid(row=0, column=6, padx=(12, 3))
        self.price_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.price_var, width=10).grid(row=0, column=7)
        self.paper_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="模拟成交（记入模拟账本）", variable=self.paper_var).grid(row=0, column=8, padx=12)
        b = ttk.Button(form, text="记账", command=self.do_trade)
        b.grid(row=0, column=9, padx=3)
        self.action_buttons.append(b)

        bal = ttk.LabelFrame(f, text="哈夫币余额", padding=PAD)
        bal.pack(fill="x", pady=PAD)
        ttk.Label(bal, text="当前游戏里的余额").pack(side="left")
        self.bal_var = tk.StringVar()
        ttk.Entry(bal, textvariable=self.bal_var, width=14).pack(side="left", padx=4)
        self.btn(bal, "设为余额基准", self.do_balance)
        self.btn(bal, "查看余额", lambda: self.run("查看余额", ["balance"]))
        ttk.Label(bal, text="之后的真实成交会在此基础上自动加减。", style="Hint.TLabel").pack(side="left", padx=8)

        tl = ttk.LabelFrame(f, text="最近成交（两本账都在这里，“账本”一列区分）", padding=PAD)
        tl.pack(fill="both", expand=True)
        cols = ("id", "ts", "side", "name", "qty", "price", "book", "source", "note")
        heads = ("编号", "时间", "方向", "品种", "数量", "单价", "账本", "来源", "备注")
        widths = (50, 130, 50, 160, 70, 80, 70, 55, 260)
        self.trade_tree = ttk.Treeview(tl, columns=cols, show="headings", height=8)
        for c, h, w in zip(cols, heads, widths):
            self.trade_tree.heading(c, text=h)
            self.trade_tree.column(c, width=w, anchor="w" if c in ("name", "note", "ts") else "center")
        sb = ttk.Scrollbar(tl, orient="vertical", command=self.trade_tree.yview)
        self.trade_tree.configure(yscrollcommand=sb.set)
        side = ttk.Frame(tl)                          # 先摆右侧按钮，表格再占剩下的空间
        side.pack(side="right", fill="y", padx=(PAD, 0))
        self.btn(side, "刷新", self._refresh_trades, side="top", fill="x")
        self.btn(side, "撤销选中的成交", self.do_undo, side="top", fill="x")
        sb.pack(side="right", fill="y")
        self.trade_tree.pack(side="left", fill="both", expand=True)

    def _refresh_items(self):
        names = gui_core.item_choices(self.cfg)
        self.item_box.configure(values=names)
        if self.cfg is not None:
            self.paper_var.set(self.cfg.paper)

    def _refresh_trades(self):
        if not hasattr(self, "trade_tree"):
            return
        self.trade_tree.delete(*self.trade_tree.get_children())
        if self.cfg is None:
            return
        try:
            rows = gui_core.recent_trades(self.cfg)
        except Exception as e:                       # 数据库被占用等：不影响界面
            self.note(f"读取成交记录失败：{type(e).__name__}\n")
            return
        for r in rows:
            self.trade_tree.insert("", "end", iid=str(r["id"]), values=(
                r["id"], (r["ts"] or "")[:16], "买" if r["side"] == "buy" else "卖", r["name"], r["qty"],
                f"{r['price']:.0f}", "模拟" if r["paper"] else "真实", r["source"] or "", r["note"] or ""))

    def do_trade(self):
        side, item = self.side_var.get(), self.item_var.get().strip()
        qty, price = self.qty_var.get().strip().replace(",", ""), self.price_var.get().strip().replace(",", "")
        if not item:
            messagebox.showwarning("缺少品种", "请选择或输入品种。")
            return
        try:
            q, p = int(qty), float(price)
            if q <= 0 or p <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("数字不对", "数量要填正整数，单价要填正数。")
            return
        paper = self.paper_var.get()
        book = "模拟账本" if paper else "真实账本"
        if not self.confirm("确认记账", f"{'买入' if side == 'buy' else '卖出'} {item} × {q} @ {p:.0f}\n记入：{book}\n\n确定吗？"):
            return
        args = ["trade", side, item, str(q), str(p), "--source", "gui"] + (["--paper"] if paper else [])

        def done(code):
            if code == 0:
                self.qty_var.set("")
                self.price_var.set("")
        self.run("记账", args, on_done=done)

    def do_undo(self):
        sel = self.trade_tree.selection()
        if not sel:
            messagebox.showinfo("先选中一行", "请先在表格里点选要撤销的那笔成交。")
            return
        vals = self.trade_tree.item(sel[0], "values")
        if self.confirm("确认撤销", f"撤销成交 #{vals[0]}：{vals[2]} {vals[3]} × {vals[4]} @ {vals[5]}（{vals[6]}账本）？\n\n"
                        "持仓和余额会恢复到没记这一笔之前。"):
            self.run("撤销成交", ["trade-undo", str(vals[0])])

    def do_balance(self):
        raw = self.bal_var.get().strip().replace(",", "")
        try:
            amount = float(raw)
            if amount < 0 or amount != amount:
                raise ValueError
        except ValueError:
            messagebox.showwarning("数字不对", "余额要填不小于 0 的数字。")
            return
        if self.confirm("确认设置余额", f"把余额基准设为 {amount:,.0f}？"):
            self.run("设置余额", ["balance", "set", str(amount)], on_done=lambda c: self.bal_var.set(""))

    # ------------------------------------------------------------------ 复盘
    def _tab_review(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="复盘")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.btn(row, "全部绩效", lambda: self.run("复盘绩效", ["perf"], target=self.review_text))
        self.btn(row, "近 7 天绩效", lambda: self.run("近 7 天绩效", ["perf", "--days", "7"], target=self.review_text))
        self.btn(row, "立即给到期建议打分", lambda: self.run("打分", ["review"], target=self.review_text))
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn(row, "反思记录", lambda: self.run("反思记录", ["reflect", "list"], target=self.review_text))
        self.btn(row, "现在做一次反思（不用模型）",
                 lambda: self.run("反思", ["reflect", "run", "--offline"], target=self.review_text))
        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(PAD, 0))
        ttk.Label(row2, text="反思编号").pack(side="left")
        self.reflect_id = tk.StringVar()
        ttk.Entry(row2, textvariable=self.reflect_id, width=6).pack(side="left", padx=4)
        self.btn(row2, "查看", lambda: self._reflect("show"))
        self.btn(row2, "采纳（参数下次运行生效）", lambda: self._reflect("accept"))
        self.btn(row2, "不采纳", lambda: self._reflect("reject"))
        self.review_text = ScrolledText(f, wrap="word", font=("Consolas", 10))
        self.review_text.pack(fill="both", expand=True, pady=(PAD, 0))
        self.review_text.configure(state="disabled")

    def _reflect(self, action: str):
        rid = self.reflect_id.get().strip()
        if not rid.isdigit():
            messagebox.showwarning("编号", "请先填反思编号（点“反思记录”可以看到）。")
            return
        if action == "accept" and not self.confirm("确认采纳", f"采纳反思 #{rid} 里的参数建议？下次运行起生效，"
                                                   f"之后可在 data/overrides.yaml 里删除对应行撤销。"):
            return
        self.run(f"反思 {action}", ["reflect", action, rid], target=self.review_text)

    # ------------------------------------------------------------------ 资讯
    def _tab_intel(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="资讯录入")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="来源").pack(side="left")
        self.src_var = tk.StringVar(value="bili")
        ttk.Combobox(row, textvariable=self.src_var, values=["bili", "qq", "news", "other"], width=7,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(row, text="信源名（UP 主 / 群名）").pack(side="left", padx=(8, 0))
        self.src_name = tk.StringVar()
        ttk.Entry(row, textvariable=self.src_name, width=18).pack(side="left", padx=4)
        ttk.Label(row, text="标题").pack(side="left", padx=(8, 0))
        self.src_title = tk.StringVar()
        ttk.Entry(row, textvariable=self.src_title, width=28).pack(side="left", padx=4)
        ttk.Label(f, text="把视频文字稿、群聊记录、公告粘贴到下面。同一信源请始终用同一个名字（信用分按名字统计）。"
                         "像是在对 AI 下指令的文字会被自动隔离。", style="Hint.TLabel").pack(anchor="w", pady=(PAD, 0))
        self.note_text = tk.Text(f, wrap="word", height=12, font=("Microsoft YaHei UI", 10))
        self.note_text.pack(fill="both", expand=True, pady=PAD)
        row2 = ttk.Frame(f)
        row2.pack(fill="x")
        self.btn(row2, "保存入库", self.do_note)
        self.btn(row2, "查看已录入", lambda: self.run("已录入资讯", ["note", "list"]))

    def do_note(self):
        text = self.note_text.get("1.0", "end").strip()
        name = self.src_name.get().strip()
        if not text or not name:
            messagebox.showwarning("内容不全", "请填写信源名并粘贴正文。")
            return
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="dfq_note_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        args = ["note", "add", "--source", self.src_var.get(), "--name", name, "--file", path]
        if self.src_title.get().strip():
            args += ["--title", self.src_title.get().strip()]

        def done(code):
            try:
                os.remove(path)
            except OSError:
                pass
            if code == 0:
                self.note_text.delete("1.0", "end")
                self.src_title.set("")
        self.run("录入资讯", args, on_done=done)

    # ------------------------------------------------------------------ 设置
    def _tab_settings(self):
        f = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(f, text="设置")

        sec = ttk.LabelFrame(f, text=f"密钥（保存在：{self.store.backend.where}；只能填写，不能查看）", padding=PAD)
        sec.pack(fill="x")
        self.secret_labels: dict[str, ttk.Label] = {}
        for i, (name, desc) in enumerate(gui_core.SECRETS):
            ttk.Label(sec, text=desc).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(sec, text=name, style="Hint.TLabel").grid(row=i, column=1, sticky="w", padx=8)
            lab = ttk.Label(sec, text="", width=8)
            lab.grid(row=i, column=2)
            self.secret_labels[name] = lab
            b1 = ttk.Button(sec, text="填写 / 更换…", command=lambda n=name, d=desc: self.ask_secret(n, d))
            b1.grid(row=i, column=3, padx=3)
            b2 = ttk.Button(sec, text="清除", command=lambda n=name: self.clear_secret(n))
            b2.grid(row=i, column=4, padx=3)
            if not self.store.backend.available:
                b1.state(["disabled"])
                b2.state(["disabled"])
        if not self.store.backend.available:
            ttk.Label(sec, text="当前系统不支持在界面里保存密钥，请按 README 用命令设置环境变量。",
                      style="Hint.TLabel").grid(row=len(gui_core.SECRETS), column=0, columnspan=5, sticky="w")

        cfg = ttk.LabelFrame(f, text="配置文件", padding=PAD)
        cfg.pack(fill="x", pady=PAD)
        ttk.Label(cfg, text=str(self.config_path)).pack(anchor="w")
        row = ttk.Frame(cfg)
        row.pack(fill="x", pady=(4, 0))
        self.btn(row, "生成 config.yaml", lambda: self.run("生成配置文件", ["init"]))
        self.btn(row, "用记事本打开", self.open_config)
        self.btn(row, "检查配置", self.check_config)
        self.btn(row, "打开数据文件夹", lambda: self.open_path(self.base / "data"))
        ttk.Label(cfg, text="改完配置保存后，点“检查配置”。比例一律写小数：5% 写 0.05。",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        other = ttk.LabelFrame(f, text="其他", padding=PAD)
        other.pack(fill="x")
        self.btn(other, "登录 moligod（需要 Playwright）", self.do_moligod_login)
        self.btn(other, "创建 Windows 计划任务", self.do_install_tasks)
        ttk.Label(other, text="计划任务每晚自动运行；它调用项目里 .venv 的 Python，与本程序共用同一份数据。",
                  style="Hint.TLabel").pack(side="left", padx=8)

    def _refresh_secrets(self):
        if not hasattr(self, "secret_labels"):
            return
        for name, lab in self.secret_labels.items():
            ok = self.store.is_set(name)
            lab.configure(text="已设置" if ok else "未设置", foreground="#1f6f3f" if ok else "#888888")

    def ask_secret(self, name: str, desc: str):
        win = tk.Toplevel(self.root)
        win.title("填写密钥")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=f"{desc}\n（{name}）", padding=PAD).pack(anchor="w")
        var = tk.StringVar()
        ent = ttk.Entry(win, textvariable=var, show="•", width=48)
        ent.pack(padx=PAD, pady=PAD)
        ent.focus_set()
        ttk.Label(win, text="粘贴后点保存。内容不会显示、不会写进任何文件或日志；\n"
                            "保存到 Windows 用户环境变量，本程序立即生效；\n"
                            "计划任务等其他程序一般在下次启动时读到（如果没有，注销重新登录一次）。",
                  style="Hint.TLabel", padding=PAD, justify="left").pack(anchor="w")

        def save():
            value = var.get()
            var.set("")                              # 立刻清掉输入框里的值
            try:
                self.store.set(name, value)
            except Exception as e:
                messagebox.showerror("没有保存", str(e), parent=win)
                return
            finally:
                value = None                         # noqa: F841  不再持有
            win.destroy()
            self._refresh_secrets()
            self.note(f"已保存 {name}（内容不显示）。\n")

        row = ttk.Frame(win, padding=PAD)
        row.pack(fill="x")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=3)
        ttk.Button(row, text="保存", command=save).pack(side="right", padx=3)
        win.bind("<Return>", lambda e: save())
        win.bind("<Escape>", lambda e: win.destroy())

    def clear_secret(self, name: str):
        if not self.confirm("确认清除", f"从系统里删除 {name}？"):
            return
        try:
            self.store.clear(name)
        except Exception as e:
            messagebox.showerror("没有清除", str(e))
            return
        self._refresh_secrets()
        self.note(f"已清除 {name}。\n")

    def open_path(self, path: Path):
        path = Path(path)
        if not path.exists():
            messagebox.showinfo("还没有", f"{path} 还不存在（运行过一次之后才会有）。")
            return
        if sys.platform == "win32":
            os.startfile(str(path))                  # noqa: S606  打开文件或文件夹
        else:
            messagebox.showinfo("位置", str(path))

    def open_config(self):
        if not self.config_path.exists():
            if self.confirm("还没有配置文件", "还没有 config.yaml，现在生成一份（带中文注释）吗？"):
                self.run("生成配置文件", ["init"], on_done=lambda c: c == 0 and self.open_config())
            return
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(self.config_path)])
        else:
            messagebox.showinfo("位置", str(self.config_path))

    def check_config(self):
        self.refresh_all()
        cfg, err = gui_core.load_config(self.config_path)
        if err:
            messagebox.showerror("配置有问题", err)
            self.note(err + "\n")
        else:
            notes = "\n".join(f"· {n}" for n in cfg.notes) or "没有需要注意的地方。"
            messagebox.showinfo("配置正确", f"配置检查通过。\n\n{notes}")

    def do_moligod_login(self):
        messagebox.showinfo("登录 moligod", "接下来会打开一个浏览器窗口。请在里面手动登录 moligod，\n"
                            "看到“设置 XXX 的提醒”按钮后，回到本窗口点右下角的“我已完成，继续”。")
        self.run("登录 moligod", ["moligod-login"], needs_continue=True)

    def do_install_tasks(self):
        script = self.base / "scripts" / "install_tasks.ps1"
        if sys.platform != "win32":
            messagebox.showinfo("仅限 Windows", "计划任务只能在 Windows 上创建。")
            return
        if not script.exists():
            messagebox.showwarning("找不到脚本", f"没有找到 {script}。\n请把 DFQ.exe 放在项目根目录（和 scripts 文件夹同级）。")
            return
        if not (self.base / ".venv").exists():
            if not self.confirm("没有 .venv", "计划任务需要项目里的 .venv（见 README 安装步骤），现在没有找到。仍要创建吗？"):
                return
        if not self.confirm("确认创建", "将创建两个计划任务：每晚 21:30 运行一次、每 30 分钟读取一次邮件回复。\n确定吗？"):
            return
        self.set_busy(True, "创建计划任务")

        def work():
            try:
                p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   cwd=str(self.base), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.log((p.stdout or "") + (p.stderr or "") + f"■ 结束（返回码 {p.returncode}）\n")
            except Exception as e:
                self.log(f"创建失败：{type(e).__name__}: {e}\n")
            self.out_q.put(("done", lambda: self.set_busy(False)))
        threading.Thread(target=work, daemon=True).start()


def main(config: str | Path | None = None) -> int:
    base = gui_core.default_base_dir()
    cfg_path = Path(config) if config else base / "config.yaml"
    if not cfg_path.is_absolute():
        cfg_path = base / cfg_path
    try:
        os.chdir(cfg_path.parent)                    # 让相对路径（data/、manual_notes/）都落在这里
    except OSError:
        pass
    root = tk.Tk()
    App(root, cfg_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
