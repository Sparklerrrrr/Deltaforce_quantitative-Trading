// DFQ 界面逻辑：状态、轮询、各页面、命令面板、快捷键、启动。所有操作都调用后端 window.pywebview.api.*（dfq/webui/api.py）。
// 会花钱 / 改账本的操作：后端第一次返回 {confirm}，这里弹窗，用户点“确定”后带 confirmed=true 再调一次（确认在后端强制）。
// 每个新接口都先判断“有没有”（typeof api.xxx === "function"）：后端还没有时那一块显示占位说明，不让整页报错。
"use strict";

(function () {
  const U = window.DFQUI;
  const { $, $$, h, icon, clear, toast, modal, seg, segOf, toggle, countUp, setText, flash, skeleton, emptyState,
    copyText, stagger, isNum, fmtInt, fmtNum, fmtMoney, fmtSigned, fmtPct, shortTime, signClass, debounce } = U;
  const T = window.DFQTheme;
  const root = document.documentElement;

  // ---------------------------------------------------------------- 状态
  const PREF_DEFAULTS = {
    close_action: "ask", theme: "", accent: "#78b49d", scale: 100, density: "comfortable", motion: "auto", glow: true, gpu: true,
    tray_saver: true, notify_done: true, remember_window: true, side_collapsed: false, backup_on_start: true, backup_keep: 7,
    log_keep_days: 30,
  };
  // 只影响本页面外观的偏好：后端没有 set_pref 时也能先存在本机（localStorage）
  const LOCAL_PREFS = new Set(["accent", "density", "motion", "glow", "side_collapsed"]);
  const S = {
    seq: 0, rseq: 0, busy: false, wasBusy: false, busyLabel: null, lastAction: null,
    state: null, prefs: { ...PREF_DEFAULTS }, prefsLoaded: false,
    page: "home", tasks: { home: [], review: [] },
    selectedTrade: null, trades: [], knownTrades: new Set(), tradeFilter: "all", paperTouched: false,
    posBook: null, planMode: "cards", plan: null, planLoaded: false, dash: null,
    healthOpen: true, narrow: false, sideOpenNarrow: false, results: {},
  };
  let api = null;
  const has = (m) => !!api && typeof api[m] === "function";
  const v2 = () => has("dashboard");                       // 新版后端（BRIDGE v2）：新动作（风控、期初库存等）才可用

  // ---------------------------------------------------------------- 调用后端
  // 需要确认时 confirmed 参数所在的位置（其余方法把 true 追加在最后）
  const CONFIRM_POS = { run: 2, install_tasks: 0, reset_config: 0, clear_secret: 1, set_config: 2, clean: 1, restore_backup: 1,
    remove_tasks: 0, check_update: 0, export_config: 1, import_config: 2 };
  function withConfirmed(method, args) {
    const pos = CONFIRM_POS[method];
    const a = [...args];
    if (pos === undefined) return [...a, true];
    while (a.length < pos) a.push(method === "run" && a.length === 1 ? {} : null);
    a[pos] = true;
    return a;
  }
  // 返回后端结果（对象），取消 / 出错返回 null；出错会提示，绝不静默
  async function invoke(method, ...args) {
    if (!has(method)) { toast(`当前后端版本没有这个功能（${method}），请更新 DFQ 后再试`, "warn"); return null; }
    let r;
    try { r = await api[method](...args); } catch (e) { toast(`调用后端失败（${method}）：${(e && e.message) || e}`, "error"); return null; }
    if (r && r.confirm) {
      const ok = await modal({ title: r.confirm.title || "请确认", text: r.confirm.text || "", okText: r.confirm.ok || "确定",
        danger: /删除|清除|恢复|撤销|解除|清理|实盘/.test((r.confirm.title || "") + (r.confirm.text || "")) });
      if (!ok) return null;
      try { r = await api[method](...withConfirmed(method, args)); }
      catch (e) { toast(`调用后端失败（${method}）：${(e && e.message) || e}`, "error"); return null; }
    }
    if (r && r.error) { toast(r.error, "error"); return null; }
    return r === undefined || r === null ? {} : r;
  }
  // 会开始一个后台动作的调用：成功后马上切到快速轮询
  async function call(method, ...args) {
    const r = await invoke(method, ...args);
    if (r) kickPoll();
    return !!r;
  }
  function run(action, params = {}) {
    S.lastAction = action;
    return call("run", action, params);
  }

  // ---------------------------------------------------------------- 外观偏好
  function cache(key, value) {
    try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* 忽略 */ }
  }
  function applyPrefs(p, { initial = false } = {}) {
    const pref = p.theme === "light" || p.theme === "dark" ? p.theme : "";
    root.dataset.themePref = pref;
    root.dataset.theme = T.resolveTheme(pref);
    const accent = T.applyAccent(p.accent);
    root.dataset.density = p.density === "compact" ? "compact" : "comfortable";
    root.dataset.motionPref = p.motion || "auto";
    root.dataset.motion = T.resolveMotion(p.motion);
    root.dataset.glow = p.glow === false ? "off" : "on";
    if (initial) root.dataset.gpu = p.gpu === false ? "off" : "on";     // GPU 改动要重启才生效：运行中不切换
    cache("dfq-theme", pref || "system");
    cache("dfq-accent", accent);
    cache("dfq-density", root.dataset.density);
    cache("dfq-motion", p.motion || "auto");
    cache("dfq-glow", root.dataset.glow);
    cache("dfq-gpu", p.gpu === false ? "off" : "on");
    cache("dfq-side", p.side_collapsed ? "1" : "0");
    updateThemeButton();
    layoutSide();
    requestAnimationFrame(placeTabIndicator);
    if (window.DFQSettings) window.DFQSettings.onPrefs(p);
  }
  function updateThemeButton() {
    const dark = root.dataset.theme === "dark";
    const b = $("#theme-btn");
    if (!b) return;
    b.innerHTML = window.DFQ_ICONS[dark ? "sun" : "moon"];
    b.title = dark ? "切换到浅色" : "切换到深色";
    b.setAttribute("aria-label", b.title);
  }
  // 窗口底色跟着变（拖动缩放时边缘不闪）：set_theme(t) 会同时保存 t，
  // 所以“跟随系统”时先用实际主题设底色，再把偏好改回 ""。
  async function syncWindowTheme(pref) {
    if (!has("set_theme")) return true;
    try {
      const r = await api.set_theme(T.resolveTheme(pref));
      if (r && r.error) { toast(r.error, "error"); return false; }
      if (!pref) { const r2 = await api.set_theme(""); if (r2 && r2.error) { toast(r2.error, "error"); return false; } }
      return true;
    } catch (e) { toast("保存主题失败：" + ((e && e.message) || e), "error"); return false; }
  }
  // 修改一个偏好：立即生效，保存失败时回滚并提示原因。返回 {ok, restart} 或 null。
  async function setPref(key, value, { quiet = false } = {}) {
    const prev = S.prefs[key];
    S.prefs = { ...S.prefs, [key]: value };
    applyPrefs(S.prefs);
    const rollback = (msg) => {
      S.prefs = { ...S.prefs, [key]: prev };
      applyPrefs(S.prefs);
      if (msg) toast(msg, "error");
      return null;
    };
    let restart = false;
    if (key === "theme") {
      if (!(await syncWindowTheme(value))) return rollback();
    } else if (has("set_pref")) {
      let r;
      try { r = await api.set_pref(key, value); } catch (e) { return rollback("保存失败：" + ((e && e.message) || e)); }
      if (!r || r.error) return rollback((r && r.error) || "保存失败");
      if (r.prefs) { S.prefs = { ...PREF_DEFAULTS, ...r.prefs }; applyPrefs(S.prefs); }
      restart = !!r.restart;
    } else if (key === "close_action" && has("set_close_action")) {
      const r = await api.set_close_action(value);
      if (!r || r.error) return rollback((r && r.error) || "保存失败");
    } else if (!LOCAL_PREFS.has(key)) {
      return rollback("当前后端版本不支持保存这一项，请更新 DFQ");
    }
    if (!quiet) toast(restart ? "已保存，重启 DFQ 后生效" : "已保存", "ok", 1800);
    return { ok: true, restart };
  }
  async function loadPrefs() {
    let p = {};
    if (has("ui_prefs")) {
      try { p = (await api.ui_prefs()) || {}; } catch (e) { p = {}; }
    }
    const local = {};
    // 后端还不认识的外观项（旧版后端）：沿用本机缓存
    if (!has("set_pref")) {
      try {
        const a = localStorage.getItem("dfq-accent"); if (T.validHex(a)) local.accent = a;
        const d = localStorage.getItem("dfq-density"); if (d) local.density = d;
        const m = localStorage.getItem("dfq-motion"); if (m) local.motion = m;
        const g = localStorage.getItem("dfq-glow"); if (g) local.glow = g !== "off";
        const s = localStorage.getItem("dfq-side"); if (s) local.side_collapsed = s === "1";
      } catch (e) { /* 忽略 */ }
    }
    S.prefs = { ...PREF_DEFAULTS, ...local, ...p };
    S.prefsLoaded = true;
    applyPrefs(S.prefs, { initial: true });
  }
  // 跟随系统：系统深浅色 / 减少动画设置变化时重新计算
  function watchSystem() {
    try {
      matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        if (S.prefs.theme) return;
        applyPrefs(S.prefs);
        syncWindowTheme("");
      });
      matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => {
        if ((S.prefs.motion || "auto") === "auto") applyPrefs(S.prefs);
      });
    } catch (e) { /* 老内核：不跟随 */ }
  }

  // ---------------------------------------------------------------- 页面切换
  const PAGES = ["home", "plan", "ledger", "review", "intel", "settings"];
  function placeTabIndicator(animate = false) {
    const t = $(`#tabs .tab[data-page="${S.page}"]`);
    U.placeIndicator($("#tab-ind"), t, { animate });
  }
  function showPage(name, { focus = false } = {}) {
    if (!PAGES.includes(name)) return;
    const changed = name !== S.page;
    S.page = name;
    $$("#tabs .tab").forEach((t) => {
      const on = t.dataset.page === name;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    $$(".page").forEach((p) => {
      const on = p.dataset.page === name;
      p.classList.toggle("active", on);
      if (on && changed) {
        p.classList.remove("entering");
        void p.offsetWidth;
        p.classList.add("entering");
        clearTimeout(p._enterT);
        p._enterT = setTimeout(() => p.classList.remove("entering"), 700);
      }
    });
    placeTabIndicator(true);
    refreshPage(name);
  }
  function refreshPage(name = S.page) {
    if (!api) return;
    if (name === "home") refreshDashboard();
    else if (name === "plan") refreshPlan();
    else if (name === "ledger") { refreshTrades(); refreshPositions(); }
    else if (name === "settings" && window.DFQSettings) window.DFQSettings.refresh();
  }

  // ---------------------------------------------------------------- 任务清单
  function renderTasks(el, list) {
    clear(el);
    for (const t of list || []) {
      const chips = h("div.task-chips", null, (t.chips || []).map((c) => h("span.chip", { text: c })));
      if (t.cost) chips.appendChild(h("span.badge-cost", { text: "真实数据时计费" }));
      const acts = h("div.task-actions", null, (t.actions || []).map((a) =>
        h("button.btn" + (a.style === "primary" ? ".primary" : ""), { type: "button", "data-action": a.action, text: a.label })));
      el.appendChild(h("div.task", { "data-task": t.id }, [
        h("div.task-icon", null, icon(window.DFQ_ICONS[t.icon] ? t.icon : "check")),
        h("div.task-main", null, [h("div.task-title", { text: t.title }), chips]),
        acts,
      ]));
    }
  }

  // ---------------------------------------------------------------- 运行环境（右栏）
  let sourceSeg = null;
  function renderState(st) {
    if (!st) return;
    S.state = st;
    setText($("#version"), st.version ? "v" + st.version : "", false);
    if (sourceSeg) sourceSeg.set(st.mock ? "mock" : "real");
    setText($("#source-desc"), st.mock
      ? "模拟数据：不联网、不花接口次数，数据单独存放在 data/mock。"
      : "真实行情：运行会请求小涛查接口，按次计费；执行前都会弹窗确认。");
    const mode = $("#mode");
    if (st.config_error) { setText(mode, "配置有误"); mode.className = "v danger-text"; mode.title = st.config_error; }
    else {
      setText(mode, st.paper ? "模拟（paper_mode）" : "实盘记账");
      mode.className = "v " + (st.paper ? "ok" : "warn");
      mode.title = "";
    }
    renderUsage(st.usage);
    const f = st.features || {};
    const feat = clear($("#features"));
    for (const [k, label] of [["llm", "大模型"], ["news", "新闻"], ["mail", "邮件"]]) {
      feat.appendChild(h("span.pill" + (f[k] ? ".on" : ""), { text: label, title: f[k] ? "已开启" : "未开启" }));
    }
    const mg = st.moligod || {};
    const auto = mg.mode === "auto" && f.moligod;
    feat.appendChild(h("span.pill." + (auto ? "on" : "manual"), { text: "moligod：" + (auto ? "自动" : "手动"),
      title: auto ? "自动设置价格提醒" : "价格提醒需要在 moligod 网页上手动设置" }));
    // 品种下拉
    const dl = clear($("#items"));
    for (const n of st.items || []) dl.appendChild(h("option", { value: n }));
    if (st.paper !== null && st.paper !== undefined && !S.paperTouched) { $("#paper").checked = !!st.paper; $("#inv-paper").checked = !!st.paper; }
    if (S.posBook === null && st.paper !== null && st.paper !== undefined) {
      S.posBook = st.paper ? "paper" : "real";
      const ps = segOf($("#pos-book"));
      if (ps) ps.set(S.posBook, false);
    }
    if (window.DFQSettings) window.DFQSettings.onState(st);
  }
  function renderUsage(u) {
    const bar = $("#usage-bar");
    if (!u || !isNum(u.limit)) { setText($("#usage"), "—"); bar.querySelector("i").style.transform = "scaleX(0)"; return; }
    setText($("#usage"), `${u.today} / ${u.limit} 次`);
    const k = u.limit > 0 ? Math.min(1, u.today / u.limit) : 0;
    bar.querySelector("i").style.transform = `scaleX(${k})`;
    bar.classList.toggle("warn", k > 0.8 && k < 1);
    bar.classList.toggle("danger", k >= 1);
  }

  // ---------------------------------------------------------------- 运行中状态
  const LOCKABLE = "[data-action], #trade-btn, #inv-btn, #balance-btn, #undo-btn, #note-btn, #reflect-show, #reflect-accept, "
    + "#reflect-reject, #source .seg, .busy-lock";
  function setBusy(busy, label, waiting) {
    S.busy = busy;
    S.busyLabel = label;
    $$(LOCKABLE).forEach((b) => { b.disabled = busy || b.dataset.locked === "1"; });
    $("#app").classList.toggle("is-busy", busy);
    const st = $("#status");
    st.className = "v status-v " + (busy ? "warn" : "ok");
    setText($("span", st), busy ? `运行中：${label || "…"}` : "就绪", false);
    $("#rail-dot").title = busy ? `运行中：${label || ""}` : "就绪";
    $("#continue-btn").classList.toggle("hidden", !waiting);
    $$(".task.running").forEach((t) => t.classList.remove("running"));
    if (busy && S.lastAction) {
      const b = $(`.task [data-action="${CSS.escape(S.lastAction)}"]`);
      if (b) b.closest(".task").classList.add("running");
    }
    if (!busy) S.lastAction = null;
  }

  // ---------------------------------------------------------------- 轮询
  // 运行中 0.15 秒一次；空闲 1.5 秒一次；窗口被隐藏（收进托盘）时完全不问
  const POLL_BUSY = 150, POLL_IDLE = 1500;
  let pollTimer = null, polling = false;
  function schedulePoll(ms) {
    clearTimeout(pollTimer);
    if (document.hidden) { pollTimer = null; return; }       // 看不见就不问，回到前台时由 visibilitychange 重新开始
    pollTimer = setTimeout(poll, ms);
  }
  function kickPoll() { schedulePoll(0); }                     // 刚点了按钮：马上问一次，不用等
  async function poll() {
    if (polling || !api) return;
    polling = true;
    try {
      const r = await api.poll(S.seq, S.rseq);
      if (r.seq < S.seq) window.DFQLog.clear();             // 后端清空过
      S.seq = r.seq;
      window.DFQLog.append(r.text);
      if (r.results) {
        S.rseq = r.results_seq;
        for (const [k, v] of Object.entries(r.results)) applyResult(k, v);
      }
      setBusy(r.busy, r.busy_label, r.waiting_continue);
      if (S.wasBusy && !r.busy) afterRun();
      S.wasBusy = r.busy;
    } catch (e) { /* 窗口关闭时会失败，忽略 */ }
    polling = false;
    schedulePoll(S.busy ? POLL_BUSY : POLL_IDLE);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) kickPoll(); });
  function afterRun() {
    refreshState();
    refreshTrades();
    refreshDashboard();
    if (S.page === "plan") refreshPlan();
    else S.planLoaded = false;
    if (S.page === "ledger") refreshPositions();
    if (S.page === "settings" && window.DFQSettings) window.DFQSettings.refresh();
  }
  function applyResult(k, v) {
    if (S.results[k] === v) return;
    S.results[k] = v;
    if (k === "plan") $("#out-plan").textContent = v;
    else if (k === "review") renderOutput($("#out-review"), v);
    else { const el = $("#out-" + k); if (el) el.textContent = v; }
  }

  async function refreshState() {
    if (!has("state")) return;
    try { renderState(await api.state()); } catch (e) { toast("读取状态失败：" + ((e && e.message) || e), "error"); }
  }

  // ---------------------------------------------------------------- 首页：指标条
  const RING_C = 2 * Math.PI * 19;
  function buildMetrics() {
    const box = clear($("#metrics"));
    box.append(...skeleton("metrics"));
  }
  function metricCard(id, iconName, label, go) {
    const tag = go ? "button" : "div";
    const el = h(tag + ".card.metric" + (go ? ".hoverable" : ""), { id: "m-" + id, type: go ? "button" : null,
      title: go ? "点击查看详情" : null });
    el.appendChild(h("div.metric-label", null, [icon(iconName), h("span", { text: label }), go ? icon("chevron", "go") : null]));
    if (go) el.addEventListener("click", () => showPage(go));
    return el;
  }
  function ensureMetrics() {
    const box = $("#metrics");
    if ($("#m-usage", box)) return box;
    clear(box);
    const usage = metricCard("usage", "gauge", "今日接口");
    const ring = h("div.metric-ring", null, [
      svgRing(),
      h("div", null, [h("div.metric-value", null, [h("span", { id: "mv-usage-today", text: "—" }), h("small", { id: "mv-usage-limit" })]),
        h("div.metric-sub", { id: "mv-usage-sub" })]),
    ]);
    usage.appendChild(ring);
    const plan = metricCard("plan", "plan", "最新计划", "plan");
    plan.append(h("div.metric-split", { id: "mv-plan" }), h("div.metric-sub", { id: "mv-plan-sub" }));
    const pos = metricCard("pos", "box", "持仓", "ledger");
    pos.append(h("div.metric-value", null, [h("span", { id: "mv-pos-count", text: "—" }), h("small", { text: "个品种" })]),
      h("div.metric-sub", { id: "mv-pos-sub" }));
    const last = metricCard("last", "clock", "上次运行");
    last.append(h("div.metric-value", null, h("span", { id: "mv-last", text: "—" })), h("div.metric-sub", { id: "mv-last-sub" }));
    box.append(usage, plan, pos, last);
    stagger(box);
    return box;
  }
  function svgRing() {
    // SVG 由代码固定生成（不含后端文字）
    const wrap = h("span");
    wrap.innerHTML = `<svg class="ring" id="mv-ring" viewBox="0 0 46 46"><circle class="track" cx="23" cy="23" r="19"/>`
      + `<circle class="bar" cx="23" cy="23" r="19" stroke-dasharray="${RING_C.toFixed(2)}" stroke-dashoffset="${RING_C.toFixed(2)}"/></svg>`;
    return wrap.firstChild;
  }
  function badge(text, kind = "", ico) {
    return h("span.badge" + (kind ? "." + kind : ""), null, [ico ? icon(ico) : null, h("span", { text })]);
  }
  async function refreshDashboard() {
    if (!api) return;
    if (!has("dashboard")) { renderDashboard(null); return; }
    let d = null;
    try { d = await api.dashboard(); } catch (e) { toast("读取首页数据失败：" + ((e && e.message) || e), "error"); }
    if (d && d.error) { toast(d.error, "error"); d = null; }
    renderDashboard(d || {});
  }
  function renderDashboard(d) {
    ensureMetrics();
    const legacy = d === null;                                  // 旧版后端：只有接口用量
    d = d || {};
    // 今日接口
    const u = d.usage || (S.state && S.state.usage) || null;
    const ring = $("#mv-ring");
    if (u && isNum(u.limit)) {
      countUp($("#mv-usage-today"), u.today);
      $("#mv-usage-limit").textContent = `/ ${u.limit} 次`;
      const k = u.limit > 0 ? Math.min(1, u.today / u.limit) : 0;
      ring.querySelector(".bar").style.strokeDashoffset = String(RING_C * (1 - k));
      ring.classList.toggle("warn", k > 0.8 && k < 1);
      ring.classList.toggle("danger", k >= 1);
      const sub = clear($("#mv-usage-sub"));
      sub.appendChild(k >= 1 ? badge("今日额度已用完", "danger") : k > 0.8 ? badge("接近上限", "warn")
        : h("span", { text: `剩余 ${Math.max(0, u.limit - u.today)} 次` }));
    } else {
      $("#mv-usage-today").textContent = "—";
      $("#mv-usage-limit").textContent = "";
      clear($("#mv-usage-sub")).appendChild(h("span", { text: "还没有数据库" }));
    }
    // 最新计划
    const pv = $("#mv-plan"), ps = clear($("#mv-plan-sub"));
    clear(pv);
    if (legacy) {
      pv.appendChild(h("span.metric-empty", { text: "—" }));
      ps.appendChild(h("span", { text: "更新后端后显示" }));
    } else if (d.plan) {
      const p = d.plan;
      const part = (n, label, id) => { const b = h("b", { id }); countUp(b, n); return h("span", null, [label + " ", b]); };
      pv.append(part(p.buy, "买", "mv-buy"), h("span.sep", { text: "·" }), part(p.sell, "卖", "mv-sell"), h("span.sep", { text: "·" }),
        part(p.watch, "观望", "mv-watch"));
      ps.appendChild(p.expired ? badge("已过期", "danger") : badge("有效至 " + shortTime(p.valid_until), "accent"));
      if (p.paper) ps.appendChild(badge("模拟", ""));
    } else {
      pv.appendChild(h("span.metric-empty", { text: "还没有计划" }));
      ps.appendChild(h("span", { text: "在下方运行一次" }));
    }
    // 持仓
    const ps2 = clear($("#mv-pos-sub"));
    if (legacy) { $("#mv-pos-count").textContent = "—"; ps2.appendChild(h("span", { text: "更新后端后显示" })); }
    else if (d.positions) {
      const p = d.positions;
      countUp($("#mv-pos-count"), p.count);
      if (isNum(p.unrealized)) ps2.appendChild(h("span.tnum." + (signClass(p.unrealized) || "muted"), { text: "浮动 " + fmtSigned(p.unrealized) }));
      else ps2.appendChild(h("span", { text: "成本 " + fmtMoney(p.cost) }));
      ps2.appendChild(badge(p.book === "real" ? "真实账本" : "模拟账本", p.book === "real" ? "warn" : ""));
    } else { $("#mv-pos-count").textContent = "0"; ps2.appendChild(h("span", { text: "没有持仓" })); }
    // 上次运行
    const ls = clear($("#mv-last-sub"));
    if (legacy) { $("#mv-last").textContent = "—"; ls.appendChild(h("span", { text: "更新后端后显示" })); }
    else if (d.last_run) {
      const r = d.last_run;
      setText($("#mv-last"), shortTime(r.at));
      const st = String(r.status || "");
      const kind = /ok|success|成功/.test(st) ? ["成功", "ok"] : /degrad|partial|降级/.test(st) ? ["降级", "warn"]
        : /fail|error|失败/.test(st) ? ["失败", "danger"] : [st || "未知", ""];
      ls.appendChild(badge(kind[0], kind[1]));
      if (r.mode) ls.appendChild(h("span", { text: r.mode === "llm" ? "规则 + 模型" : r.mode === "offline" ? "只用规则" : String(r.mode) }));
      if (isNum(r.cost)) ls.appendChild(h("span.tnum", { text: `接口 ${r.cost} 次` }));
    } else { $("#mv-last").textContent = "—"; ls.appendChild(h("span", { text: "还没有运行过" })); }
    if (d.risk && d.risk.halted) ls.appendChild(badge("风控：已暂停买入", "warn", "shield"));
    renderHealth(d.health || []);
  }
  function renderHealth(list) {
    const box = $("#health");
    const items = list.filter((x) => x && (x.level === "warn" || x.level === "error"));
    clear(box);
    box.classList.toggle("hidden", !items.length);
    if (!items.length) return;
    const err = items.some((x) => x.level === "error");
    box.className = "health" + (err ? " error" : "") + (S.healthOpen ? " open" : "");
    const head = h("button.health-head", { type: "button", "aria-expanded": S.healthOpen ? "true" : "false" }, [
      icon(err ? "error" : "warn"), h("span", { text: `有 ${items.length} 条需要注意` }), icon("chevron", "chev"),
    ]);
    head.addEventListener("click", () => {
      S.healthOpen = !S.healthOpen;
      box.classList.toggle("open", S.healthOpen);
      head.setAttribute("aria-expanded", S.healthOpen ? "true" : "false");
    });
    box.append(head, h("ul.health-list", null, items.map((x) => h("li" + (x.level === "error" ? ".error" : ""), { text: x.text }))));
  }

  // ---------------------------------------------------------------- 最新计划
  let planSeg = null;
  function setPlanMode(mode) {
    S.planMode = mode;
    $("#plan-cards").classList.toggle("hidden", mode !== "cards");
    $("#plan-text-card").classList.toggle("hidden", mode !== "text");
    cache("dfq-plan-view", mode);
  }
  async function refreshPlan(force = false) {
    if (!api) return;
    if (!has("plan_view")) {
      // 旧版后端：只有纯文本（运行 dfq plan），卡片视图显示占位说明
      renderPlanLegacy();
      if (force || !S.planLoaded) { S.planLoaded = true; run("plan"); }
      return;
    }
    const box = $("#plan-cards");
    if (!S.planLoaded) { clear(box).appendChild(skeleton("plan")); }
    let pv = null;
    try { pv = await api.plan_view(); } catch (e) { pv = { exists: false, error: "读取计划失败：" + ((e && e.message) || e) }; }
    S.planLoaded = true;
    S.plan = pv;
    renderPlan(pv || { exists: false });
    if (pv && pv.text) { $("#out-plan").textContent = pv.text; S.results.plan = pv.text; }
    if (force) toast("计划已刷新", "ok", 1500);
  }
  function renderPlanLegacy() {
    const box = clear($("#plan-cards"));
    box.appendChild(h("div.card", null, h("div.placeholder", null, [icon("info"),
      h("span", { text: "当前后端版本还不能按结构显示计划。已切换到“原文”视图；更新 DFQ 后这里会显示买入建议表、moligod 提醒等卡片。" })])));
    if (planSeg) planSeg.set("text");
    setPlanMode("text");
  }

  function renderPlan(pv) {
    const box = clear($("#plan-cards"));
    if (!pv.exists) {
      const go = h("button.btn.primary", { type: "button" }, [icon("play"), h("span", { text: "去首页运行" })]);
      go.addEventListener("click", () => showPage("home"));
      box.appendChild(h("div.card", null, emptyState("plan", "还没有计划", pv.error || "在首页运行一次（或先“演示一遍”），生成的计划会显示在这里。", go)));
      setText($("#plan-sub"), "内容与邮件相同；过期的计划会标明作废", false);
      return;
    }
    const p = pv.plan || {};
    const items = Array.isArray(p.items) ? p.items : [];
    const buys = items.filter((i) => i && i.action === "buy");
    const watches = items.filter((i) => i && i.action !== "buy");
    const sells = Array.isArray(p.sells) ? p.sells : [];
    setText($("#plan-sub"), p.plan_id ? `计划编号 ${p.plan_id}` : "", false);

    // 头部
    const hero = h("div.card.plan-hero" + (pv.expired ? ".expired" : ""));
    const meta = h("div.plan-meta", null, [
      p.paper ? badge("模拟计划", "") : badge("实盘计划", "warn"),
      p.mode === "llm" ? badge("规则 + 模型" + (p.model ? `（${p.model}）` : ""), "accent", "spark") : badge("规则", "accent", "rules"),
      pv.mock ? badge("模拟数据", "info") : null,
      pv.expired ? badge("已过期作废", "danger", "error") : null,
      h("span.valid", null, ["有效期 ", h("b", { text: shortTime(p.valid_from || p.created_at) }), " → ",
        h("b", { text: shortTime(p.valid_until) })]),
    ]);
    hero.append(h("div.plan-title", { text: pv.title || ("交易计划 " + (p.plan_id || "")) }), meta);
    if (p.market_view) hero.appendChild(h("div.quote", null, [h("span.q-label", { text: "整体判断" }), h("span", { text: p.market_view })]));
    box.appendChild(hero);

    // 买入建议
    const buyCard = h("div.card", null, h("div.card-head", null, [h("h3", null, [icon("ledger", "h-ico"), "买入建议"]),
      badge(`${buys.length} 个品种`, buys.length ? "accent" : ""), h("span.hint", { text: "点价格可复制数字" })]));
    if (buys.length) buyCard.appendChild(buyTable(buys));
    else buyCard.appendChild(h("div.hint", { text: "今天没有买入建议。" }));
    box.appendChild(buyCard);

    // 持仓卖出
    if (sells.length) {
      const c = h("div.card", null, h("div.card-head", null, [h("h3", null, [icon("export", "h-ico"), "持仓卖出"]),
        badge(`${sells.length} 笔`, "warn"), h("span.hint", { text: p.paper ? "模拟账本里的持仓" : "先卖后买" })]));
      c.appendChild(sellTable(sells));
      box.appendChild(c);
    }

    // 观望（默认折叠）
    if (watches.length) {
      const c = h("div.card.collapsible");
      const head = h("button.collapse-head", { type: "button", "aria-expanded": "false" }, [icon("chevron", "chev"),
        h("h3", { text: `观望 ${watches.length} 个品种` }), h("span.hint", { text: "展开查看参考线和理由" })]);
      head.addEventListener("click", () => {
        const open = c.classList.toggle("open");
        head.setAttribute("aria-expanded", open ? "true" : "false");
      });
      const body = h("div.collapse-body", null, watches.map((i) => h("div.watch-row", null, [
        h("span.name-cell", { text: i.name || "—" }),
        h("span.ref", { text: i.buy_price || i.sell_price ? `买 ${fmtInt(i.buy_price)} / 卖 ${fmtInt(i.sell_price)}` : "—" }),
        h("span.why", { text: (i.guard_notes || []).join("；") || i.reason || "—" }),
      ])));
      c.append(head, body);
      box.appendChild(c);
    }

    // moligod 价格提醒
    box.appendChild(moligodCard(pv.alerts || []));

    // 展望
    const st = Array.isArray(p.short_term) ? p.short_term : [], lt = Array.isArray(p.long_term) ? p.long_term : [];
    if (st.length || lt.length || p.next_run) {
      const c = h("div.card", null, h("div.card-head", null, h("h3", null, [icon("eye", "h-ico"), "展望"])));
      const col = (title, list, key) => h("div", null, [h("div.section-title", { text: title }),
        ...(list.length ? list.map((s) => h("div.outlook-item", null, [h("span.when", { text: (s && s[key]) || "—" }),
          h("span", { text: (s && s.note) || "" })])) : [h("div.hint", { text: "（无）" })])]);
      c.appendChild(h("div.outlook", null, [col("短期", st, "date"), col("长期", lt, "week")]));
      if (p.next_run) {
        c.appendChild(h("div.banner.info", { style: { marginTop: "10px", marginBottom: "0" } }, [icon("clock"),
          h("span", { text: `模型建议下次运行：${p.next_run}${p.next_run_reason ? "（" + p.next_run_reason + "）" : ""}` })]));
      }
      box.appendChild(c);
    }

    // 提示与降级
    const notes = mergeNotes([...(p.warnings || []), ...(p.guard_notes || [])]);
    if (notes.length) {
      const c = h("div.card", null, h("div.card-head", null, [h("h3", null, [icon("warn", "h-ico"), "提示与降级"]),
        badge(`${notes.length} 条`, "warn")]));
      c.appendChild(h("ul.note-list", null, notes.map((n) => {
        const li = h("li", { title: n.title || null }, n.text);
        return li;
      })));
      box.appendChild(c);
    }

    // 资金与风控
    const cap = p.capital || {}, caps = (p.risk && p.risk.caps) || {};
    const stats = [
      ["现金", cap.cash_known === false ? fmtMoney(cap.cash) + "（假定）" : fmtMoney(cap.cash)],
      ["持仓市值", fmtMoney(cap.positions_value)], ["总资产", fmtMoney(cap.equity)],
      ["今日可买上限", fmtMoney(caps.daily)], ["总敞口余量", fmtMoney(caps.total)], ["单品种上限", fmtMoney(caps.per_item)],
      ["卖出费率", isNum(p.fee_rate) ? fmtPct(p.fee_rate, 1) : "—"],
    ].filter(([, v]) => v !== "—");
    if (stats.length) {
      const c = h("div.card", null, h("div.card-head", null, h("h3", null, [icon("wallet", "h-ico"), "资金与风控"])));
      c.appendChild(h("div.stat-grid", null, stats.map(([k, v]) => h("div.stat", null, [
        h("div.s-label", { text: k }), h("div.s-value", { text: v })]))));
      box.appendChild(c);
    }
    stagger(box);
    const page = $("#page-plan");
    if (page.classList.contains("active")) { page.classList.remove("entering"); void page.offsetWidth; page.classList.add("entering"); }
  }
  // 重复的同类提示（例如 8 条“「某某」分钟曲线为空”）合并成一条
  function mergeNotes(list) {
    const groups = new Map();
    for (const raw of list) {
      if (raw === null || raw === undefined || raw === "") continue;
      const text = String(raw);
      const names = [...text.matchAll(/「([^」]{1,40})」/g)].map((m) => m[1]);
      const key = names.length === 1 ? text.replace(/「[^」]{1,40}」/, "「*」") : text;
      if (!groups.has(key)) groups.set(key, { text, names: [] });
      if (names.length === 1) groups.get(key).names.push(names[0]);
    }
    const out = [];
    for (const [key, g] of groups) {
      const uniq = [...new Set(g.names)];
      if (uniq.length > 1) {
        out.push({ text: [key.replace("「*」", `「${uniq[0]}」`), h("span.cnt", { text: `等 ${uniq.length} 个品种` })], title: uniq.join("、") });
      } else out.push({ text: g.text });
    }
    return out;
  }
  function copyCell(v, what) {
    if (!isNum(Number(v)) || v === null || v === undefined || v === "") return h("span", { text: "—" });
    const b = h("button.copy-cell", { type: "button", title: `复制${what}`, text: fmtInt(Number(v)) });
    b.addEventListener("click", () => copyText(String(Math.round(Number(v))), `${what} ${Math.round(Number(v))}`));
    return b;
  }
  function nameCell(name, search) {
    const td = h("td.name-cell", null, h("span", { text: name || "—" }));
    if (search || name) {
      const b = h("button.mini-copy", { type: "button", title: `复制搜索词：${search || name}`, "aria-label": "复制搜索词" }, icon("copy"));
      b.addEventListener("click", () => copyText(search || name, `搜索词「${search || name}」`));
      td.appendChild(b);
    }
    return td;
  }
  function confCell(c) {
    if (!isNum(c)) return h("td", { text: "—" });
    const k = Math.max(0, Math.min(1, c));
    const bar = h("span.conf-bar", null, h("i", { style: { transform: `scaleX(${k})` } }));
    return h("td", null, h("span.conf", null, [bar, h("small", { text: Math.round(k * 100) + "%" })]));
  }
  // 每个品种一组 tbody：第一行是数字（价格可点击复制），第二行是理由 / 风险 / 护栏备注（长文字换行，不把表格撑宽）
  function buyTable(buys) {
    const head = ["品种", "买入线 ≤", "卖出线 ≥", "止损线", "数量", "预算", "预期收益", "置信度"];
    const bodies = [];
    for (const i of buys) {
      const reason = String(i.reason || ""), risk = String(i.risk || "");
      const guards = Array.isArray(i.guard_notes) ? i.guard_notes.filter(Boolean) : [];
      const ret = i.exp_net_return;
      const tb = h("tbody.plan-item" + (i.qty === 0 ? ".zero" : ""));
      tb.appendChild(h("tr.main", null, [
        nameCell(i.name, i.search),
        h("td.num", null, copyCell(i.buy_price, "买入线")),
        h("td.num", null, copyCell(i.sell_price, "卖出线")),
        h("td.num", null, copyCell(i.stop_price, "止损线")),
        h("td.num", { text: fmtInt(i.qty) }),
        h("td.num", { text: fmtMoney(i.budget) }),
        h("td.num." + (signClass(ret) || "x"), { text: isNum(ret) ? fmtPct(ret, 1, true) : "—" }),
        confCell(i.confidence),
      ]));
      if (reason || risk || guards.length) {
        const rc = h("td.reason", { colspan: String(head.length) });
        const long = reason.length > 60 || !!risk;
        const line = h("div.reason-line", null, [h("span.reason-text", { text: reason || "（没有写理由）" })]);
        if (long) {
          const lb = h("button.link-btn", { type: "button", text: "展开", "aria-expanded": "false" });
          lb.addEventListener("click", () => {
            const o = rc.classList.toggle("open");
            lb.textContent = o ? "收起" : "展开";
            lb.setAttribute("aria-expanded", o ? "true" : "false");
          });
          line.appendChild(lb);
        }
        rc.appendChild(line);
        if (risk) rc.appendChild(h("div.reason-more", null, [h("b", { text: "风险：" }), h("span", { text: risk })]));
        for (const g of guards) rc.appendChild(h("div.guard", null, [icon("shield"), h("span", { text: "护栏：" + g })]));
        tb.appendChild(h("tr.detail", null, rc));
        tb.firstChild.classList.add("has-detail");
      }
      bodies.push(tb);
    }
    return h("div.table-wrap", null, h("table.table.plan-table", null, [h("thead", null, h("tr", null, head.map((t, k) =>
      h("th" + (k >= 1 && k <= 6 ? ".num" : ""), { text: t })))), ...bodies]));
  }
  // 持仓卖出：字段有什么显示什么（后端原样透传 latest.json）。mark 是估值用的现价；risk_flag 是超期 / 时间止损标记
  const SELL_FLAGS = { overdue: ["超期持仓", "warn"], time_stop: ["时间止损", "danger"] };
  function sellTable(sells) {
    const cols = [["name", "品种"], ["qty", "数量", "num"], ["sell_price", "卖出线 ≥", "num"], ["stop_price", "止损线 ≤", "num"],
      ["avg_cost", "持仓均价", "num"], ["breakeven", "回本价", "num"], ["mark", "现价", "num"], ["held_days", "持有", "num"],
      ["risk_flag", "标记"]];
    const present = cols.filter(([k]) => sells.some((s) => s && s[k] !== undefined && s[k] !== null && s[k] !== ""));
    // 备注是长句子：放在该品种下面单独一行（和买入建议的“理由”一样），不把表格撑出横向滚动条
    const bodies = [];
    for (const s of sells) {
      if (!s) continue;
      const tb = h("tbody.plan-item");
      bodies.push(tb);
      tb.appendChild(h("tr.main", null, present.map(([k, , cls]) => {
        const v = s[k];
        if (k === "name") return nameCell(v, null);
        if (k === "sell_price" || k === "stop_price") return h("td.num", null, copyCell(v, k === "sell_price" ? "卖出线" : "止损线"));
        if (k === "risk_flag") {
          const f = SELL_FLAGS[v] || (v ? [String(v), "warn"] : null);
          return h("td", null, f ? badge(f[0], f[1], f[1] === "danger" ? "error" : "warn") : "");
        }
        if (k === "held_days") return h("td.num", { text: isNum(v) ? `${Math.round(v)} 天` : "—" });
        return h("td" + (cls ? "." + cls : ""), { text: isNum(v) ? fmtInt(v) : (v === null || v === undefined ? "—" : String(v)) });
      })));
      if (s.note) {
        tb.appendChild(h("tr.detail", null, h("td.reason", { colspan: String(Math.max(1, present.length)) },
          h("div.reason-line", null, h("span.reason-text.wrap-text", { text: String(s.note) })))));
        tb.firstChild.classList.add("has-detail");
      }
    }
    return h("div.table-wrap", null, h("table.table.plan-table", null, [h("thead", null, h("tr", null, present.map(([, t, cls]) =>
      h("th" + (cls ? "." + cls : ""), { text: t })))), ...bodies]));
  }

  // ---------------------------------------------------------------- moligod：手动 / 自动
  const MOLIGOD_FALLBACK = {
    title: "自动设置暂不可用",
    message: "登录 moligod.com 网页自动设置交易区间的功能目前已停用，暂时只能使用手动模式：程序会在计划里列出每个品种的提醒价位，请到 moligod 网页上手动设置。",
  };
  // 两处（计划页、设置页）共用：点“自动”时询问后端，返回 unavailable 时弹提示并保持“手动”
  async function setMoligodMode(mode) {
    if (mode === "manual") {
      if (!has("set_moligod_mode")) return true;
      const r = await invoke("set_moligod_mode", "manual");
      if (r) { refreshState(); return true; }
      return false;
    }
    let r = null;
    if (has("set_moligod_mode")) {
      try { r = await api.set_moligod_mode("auto"); } catch (e) { r = { error: "调用后端失败：" + ((e && e.message) || e) }; }
    } else r = { unavailable: true };
    if (r && r.unavailable) {
      await modal({ title: r.title || MOLIGOD_FALLBACK.title, text: r.message || MOLIGOD_FALLBACK.message, okText: "知道了",
        single: true, icon: "info" });
      return false;
    }
    if (!r || r.error) { toast((r && r.error) || "切换失败", "error"); return false; }
    refreshState();
    return true;
  }
  function moligodSeg() {
    const el = h("div.segmented.small", { "aria-label": "moligod 提醒设置方式" }, [
      h("button.seg", { type: "button", "data-value": "manual", text: "手动" }),
      h("button.seg", { type: "button", "data-value": "auto", title: "自动设置暂不可用" }, ["自动", h("span.seg-tag", { text: "暂不可用" })]),
    ]);
    const s = seg(el, { value: "manual", onChange: (v) => setMoligodMode(v) });
    return s;
  }
  function moligodCard(alerts) {
    const openBtn = h("button.btn.sm", { type: "button" }, [icon("external"), h("span", { text: "打开 moligod 网页" })]);
    openBtn.addEventListener("click", () => openUrl("moligod"));
    const c = h("div.card", null, h("div.card-head", null, [h("h3", null, [icon("bell", "h-ico"), "moligod 价格提醒（手动设置）"]),
      h("div.spacer"), moligodSeg().el, openBtn]));
    if (!alerts.length) { c.appendChild(h("div.hint", { text: "今天没有需要设置的提醒。" })); return c; }
    c.appendChild(h("div.hint", { text: "在 moligod 网页上为每个品种设置两条提醒（提醒模式选“触发”）：", style: { marginBottom: "6px" } }));
    const tb = h("tbody");
    for (const a of alerts) {
      const line = (v, role) => h("td", null, v ? [copyCell(v, "提醒价"), h("span.alert-role", { text: role || "" })]
        : h("span.hint", { text: "不设" }));
      tb.appendChild(h("tr", null, [nameCell(a.moligod_name || a.name, a.moligod_name || a.name),
        line(a.low, a.low_role || "买入线"), line(a.high, a.high_role || "卖出线")]));
    }
    c.appendChild(h("div.table-wrap", null, h("table.table", null, [h("thead", null, h("tr", null, [h("th", { text: "品种" }),
      h("th", { text: "低于（≤）" }), h("th", { text: "高于（≥）" })])), tb])));
    return c;
  }
  async function openUrl(which) {
    const r = await invoke("open_url", which);
    if (r) toast("已在浏览器中打开", "info", 1800);
  }

  // ---------------------------------------------------------------- 记账：持仓
  async function refreshPositions() {
    const box = $("#positions");
    if (!has("positions")) {
      clear($("#pos-notes"));
      clear(box).appendChild(h("div.placeholder", null, [icon("info"), h("span", { text: "当前后端版本还不能显示持仓表。"
        + "可以点下面“查看余额”在日志里查看，更新 DFQ 后这里会显示每个品种的数量、均价和浮动盈亏。" })]));
      $("#pos-book").classList.add("hidden");
      return;
    }
    if (!box.dataset.loaded) clear(box).appendChild(skeleton("table"));
    let r = null;
    const paper = S.posBook === null ? null : S.posBook === "paper";
    try { r = await api.positions(paper); } catch (e) { r = { error: "读取持仓失败：" + ((e && e.message) || e) }; }
    box.dataset.loaded = "1";
    if (!r || r.error) {
      clear(box).appendChild(h("div.banner.danger", null, [icon("error"), h("span", { text: (r && r.error) || "读取持仓失败" })]));
      return;
    }
    if (S.posBook === null && r.book) { S.posBook = r.book; segOf($("#pos-book")).set(r.book, false); }
    const notes = clear($("#pos-notes"));
    if (r.notes && r.notes.length) {
      notes.appendChild(h("div.banner", null, [icon("warn"), h("div", null, r.notes.length === 1 ? h("span", { text: r.notes[0] })
        : h("ul", null, r.notes.map((n) => h("li", { text: n }))))]));
    }
    clear(box);
    const rows = r.rows || [];
    if (!rows.length) {
      box.appendChild(emptyState("box", "这本账里还没有持仓", r.book === "real"
        ? "真实账本：记一笔买入或录入期初库存后会显示在这里。" : "模拟账本：回复模拟计划或在下面记一笔后会显示在这里。"));
    }
    else {
      const head = [["品种"], ["数量", "num"], ["均价", "num"], ["回本价", "num"], ["现价", "num"], ["市值", "num"], ["浮动盈亏", "num"], ["持有", "num"]];
      const tb = h("tbody", null, rows.map((p) => {
        const un = p.unrealized, pct = p.unrealized_pct;
        const name = h("td.name-cell", null, [h("span", { text: p.name || "—" })]);
        if (p.uncovered_qty > 0) name.appendChild(h("span.badge.warn.sm", { text: `${p.uncovered_qty} 发无成本依据`, style: { marginLeft: "6px" } }));
        return h("tr", null, [
          name, h("td.num", { text: fmtInt(p.qty) }), h("td.num", { text: fmtNum(p.avg_cost, 0) }), h("td.num", { text: fmtInt(p.breakeven) }),
          h("td.num", { text: fmtInt(p.last_price), title: p.last_ts ? "价格时间：" + p.last_ts : null }),
          h("td.num", { text: fmtMoney(p.value) }),
          h("td.num." + (signClass(un) || "x"), null, [h("span", { text: fmtSigned(un) }),
            isNum(pct) ? h("span.sub", { text: " " + fmtPct(pct, 1, true) }) : null]),
          h("td.num", { text: isNum(p.held_days) ? `${p.held_days} 天` : "—" }),
        ]);
      }));
      const thead = h("thead", null, h("tr", null, head.map(([t, c]) => h("th" + (c ? "." + c : ""), { text: t }))));
      box.appendChild(h("div.table-wrap", null, h("table.table", null, [thead, tb])));
    }
    box.appendChild(h("div.pos-summary", null, [
      h("span", null, ["账本：", h("b", { text: r.book === "real" ? "真实" : "模拟" })]),
      isNum(r.realized) ? h("span", null, ["已实现盈亏：", h("b." + (signClass(r.realized) || "x"), { text: fmtSigned(r.realized) })]) : null,
      isNum(r.trades) ? h("span", null, ["成交笔数：", h("b", { text: fmtInt(r.trades) })]) : null,
    ]));
  }

  // ---------------------------------------------------------------- 记账：最近成交
  async function refreshTrades() {
    if (!has("trades")) return;
    try { S.trades = (await api.trades()) || []; } catch (e) { toast("读取成交记录失败：" + ((e && e.message) || e), "error"); return; }
    renderTrades();
  }
  const SOURCE_LABEL = { mail: "邮件", gui: "界面", cli: "命令行", inventory: "期初库存", opening: "期初库存" };
  function isOpening(r) { return r.kind === "opening" || r.source === "opening" || r.source === "inventory"; }
  function renderTrades() {
    const tb = clear($("#trades"));
    const rows = S.trades.filter((r) => S.tradeFilter === "all" || (S.tradeFilter === "paper") === !!r.paper);
    const empty = $("#trades-empty");
    empty.classList.toggle("hidden", rows.length > 0);
    if (!rows.length) {
      clear(empty).appendChild(h("span", { text: S.trades.length ? "这个筛选下没有成交记录" : "还没有成交记录" }));
    }
    const first = S.knownTrades.size === 0;
    const animate = rows.length < 200 && !first;
    for (const r of rows) {
      const tr = h("tr", { "data-id": r.id }, [
        h("td.num", { text: r.id }), h("td.tnum", { text: r.ts || "" }),
        h("td", null, h("span." + (r.side === "buy" ? "buy" : "sell"), { text: r.side === "buy" ? "买" : "卖" })),
        h("td.name-cell", { text: r.name || "" }),
        h("td.num", { text: isNum(r.qty) ? r.qty.toLocaleString("zh-CN") : String(r.qty ?? "") }),
        h("td.num", { text: isNum(r.price) ? r.price.toLocaleString("zh-CN") : String(r.price ?? "") }),
        h("td", null, [h("span.tag." + (r.paper ? "paper" : "real"), { text: r.paper ? "模拟" : "真实" })]),
        h("td", null, isOpening(r) ? h("span.tag.opening.only", { text: "期初库存" })
          : h("span", { text: SOURCE_LABEL[r.source] || r.source || "" })),
        h("td", { text: r.note || "", title: r.note || null }),
      ]);
      if (S.selectedTrade === r.id) tr.classList.add("selected");
      if (animate && !S.knownTrades.has(r.id)) tr.classList.add("row-in");
      tr.addEventListener("click", () => {
        S.selectedTrade = r.id;
        $$("#trades tr").forEach((x) => x.classList.toggle("selected", x === tr));
      });
      tb.appendChild(tr);
    }
    for (const r of S.trades) S.knownTrades.add(r.id);
  }

  // ---------------------------------------------------------------- 复盘输出：轻量着色
  const RE_SIGNED = /(?<![\w.:/-])([+\-−]\d[\d,]*(?:\.\d+)?%?)/g;
  function renderOutput(el, text) {
    clear(el);
    if (!text) return;
    const frag = document.createDocumentFragment();
    for (const line of String(text).replace(/\r\n?/g, "\n").split("\n")) {
      const d = document.createElement("div");
      d.className = "out-line";
      const t = line.trim();
      if (/^(━━|==|##|【|▶)/.test(t) || (/[：:]$/.test(t) && t.length <= 30)) d.classList.add("head");
      else if (/(警告|注意|提示|暂停|无法|不足|过期|WARNING)/.test(t)) d.classList.add("warn");
      else if (/(失败|出错|错误|ERROR|返回码)/.test(t)) d.classList.add("bad");
      let last = 0;
      for (const m of line.matchAll(RE_SIGNED)) {
        if (m.index > last) d.appendChild(document.createTextNode(line.slice(last, m.index)));
        d.appendChild(h("span." + (m[1][0] === "+" ? "pos" : "neg"), { text: m[1] }));
        last = m.index + m[1].length;
      }
      if (last < line.length) d.appendChild(document.createTextNode(line.slice(last)));
      frag.appendChild(d);
    }
    el.appendChild(frag);
    el.scrollTop = el.scrollHeight;
  }

  // ---------------------------------------------------------------- 右栏收起 / 展开
  function sideCollapsed() { return S.narrow ? !S.sideOpenNarrow : !!S.prefs.side_collapsed; }
  function layoutSide() {
    const body = $("#body");
    if (!body) return;
    const collapsed = sideCollapsed();
    body.classList.toggle("narrow", S.narrow);
    body.classList.toggle("side-collapsed", collapsed);
    body.classList.toggle("side-open", S.narrow && !collapsed);
    const b = $("#side-btn");
    b.innerHTML = window.DFQ_ICONS[collapsed ? "sideOpen" : "sideClose"];
    b.title = (collapsed ? "展开右栏" : "收起右栏") + "（Ctrl+B）";
    b.setAttribute("aria-label", b.title);
    b.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  async function toggleSide() {
    if (S.narrow) { S.sideOpenNarrow = !S.sideOpenNarrow; layoutSide(); return; }
    await setPref("side_collapsed", !S.prefs.side_collapsed, { quiet: true });
  }
  function onResize() {
    const narrow = window.innerWidth < 1100;
    if (narrow !== S.narrow) { S.narrow = narrow; S.sideOpenNarrow = false; layoutSide(); }
    placeTabIndicator(false);
  }

  // ---------------------------------------------------------------- 命令面板（Ctrl+K）
  function commands() {
    const list = [];
    const pages = [["home", "首页", "home"], ["plan", "最新计划", "plan"], ["ledger", "记账", "ledger"], ["review", "复盘", "review"],
      ["intel", "资讯录入", "intel"], ["settings", "设置", "settings"]];
    pages.forEach(([id, label, ic], i) => list.push({ group: "跳转", label: "打开 " + label, icon: ic, hint: `Ctrl+${i + 1}`,
      run: () => showPage(id) }));
    if (window.DFQSettings) {
      for (const g of window.DFQSettings.groups()) {
        list.push({ group: "设置", label: "设置 › " + g.title, icon: g.icon, keys: g.hint || "",
          run: () => { showPage("settings"); window.DFQSettings.show(g.id); } });
      }
    }
    if (has("backup_now")) {
      list.push({ group: "数据", label: "立即备份数据库", icon: "database", keys: "备份 backup",
        run: async () => { const r = await invoke("backup_now"); if (r) toast("已备份数据库" + (r.name ? "：" + r.name : ""), "ok"); } });
    }
    if (has("check_update") && window.DFQSettings) {
      list.push({ group: "数据", label: "检查更新", icon: "update", keys: "版本 更新 update", run: () => window.DFQSettings.checkUpdate() });
    }
    for (const t of [...(S.tasks.home || []), ...(S.tasks.review || [])]) {
      for (const a of t.actions || []) {
        list.push({ group: "任务", label: `${t.title}：${a.label}`, icon: window.DFQ_ICONS[t.icon] ? t.icon : "play",
          hint: t.cost && S.state && !S.state.mock ? "计费" : "", keys: (t.chips || []).join(" "),
          run: () => run(a.action, ["run_rules", "run_llm"].includes(a.action) ? { force: $("#force").checked } : {}) });
      }
    }
    const dark = root.dataset.theme === "dark";
    list.push({ group: "外观", label: dark ? "切换到浅色主题" : "切换到深色主题", icon: dark ? "sun" : "moon",
      run: () => setPref("theme", dark ? "light" : "dark") });
    list.push({ group: "外观", label: "主题跟随系统", icon: "palette", run: () => setPref("theme", "") });
    list.push({ group: "外观", label: sideCollapsed() ? "展开右栏" : "收起右栏", icon: "sideOpen", hint: "Ctrl+B", run: toggleSide });
    if (S.state) {
      list.push({ group: "数据来源", label: S.state.mock ? "切换到真实行情" : "切换到模拟数据", icon: "database", run: () => switchSource(!S.state.mock) });
    }
    list.push({ group: "其他", label: "导出日志", icon: "export", run: exportLogs });
    list.push({ group: "其他", label: "清空日志", icon: "eraser", run: clearLogs });
    list.push({ group: "其他", label: "打开数据文件夹", icon: "folder", run: () => openPath("data") });
    list.push({ group: "其他", label: "打开文档文件夹", icon: "book", run: () => openPath("docs") });
    list.push({ group: "其他", label: "检查配置", icon: "check", run: checkConfig });
    list.push({ group: "其他", label: "刷新当前页数据", icon: "refresh", hint: "F5", run: () => refreshCurrent() });
    list.push({ group: "其他", label: "搜索日志", icon: "search", hint: "Ctrl+L", run: focusLogSearch });
    return list;
  }
  function openPalette() {
    if (U.modalOpen()) return;
    const input = h("input", { type: "text", placeholder: "输入要做的事，例如“运行”“深色”“备份”", "aria-label": "搜索命令", autocomplete: "off",
      spellcheck: "false" });
    const listEl = h("div.palette-list", { role: "listbox" });
    const panel = h("div.modal.palette", null, [
      h("div.palette-input", null, [icon("search"), input, h("kbd", { text: "Esc" })]),
      listEl,
      h("div.palette-foot", null, [h("span", null, [h("kbd", { text: "↑↓" }), "选择"]), h("span", null, [h("kbd", { text: "Enter" }), "执行"]),
        h("span", null, [h("kbd", { text: "Esc" }), "关闭"])]),
    ]);
    const all = commands();
    let shown = [], active = 0;
    const render = () => {
      const q = input.value.trim().toLowerCase();
      shown = all.filter((c) => !q || (c.label + " " + c.group + " " + (c.keys || "")).toLowerCase().includes(q));
      active = Math.min(active, Math.max(0, shown.length - 1));
      clear(listEl);
      if (!shown.length) { listEl.appendChild(h("div.palette-empty", { text: "没有匹配的命令" })); return; }
      let g = null;
      shown.forEach((c, i) => {
        if (c.group !== g) { g = c.group; listEl.appendChild(h("div.palette-group", { text: g })); }
        const b = h("button.palette-item" + (i === active ? ".active" : ""), { type: "button", role: "option",
          "aria-selected": i === active ? "true" : "false" }, [h("span.p-ico", null, icon(c.icon || "chevron")), h("span", { text: c.label }),
          c.hint ? h("span.p-hint", { text: c.hint }) : null]);
        b.addEventListener("mousemove", () => { if (active !== i) { active = i; mark(); } });
        b.addEventListener("click", () => exec(i));
        listEl.appendChild(b);
      });
    };
    const mark = () => {
      $$(".palette-item", listEl).forEach((b, i) => {
        b.classList.toggle("active", i === active);
        b.setAttribute("aria-selected", i === active ? "true" : "false");
        if (i === active) b.scrollIntoView({ block: "nearest" });
      });
    };
    const exec = (i) => { const c = shown[i]; if (!c) return; ctx.close(null); setTimeout(() => c.run(), 60); };
    const ctx = U.openLayer({
      panel, maskClass: "palette-mask", label: "命令面板",
      onKey(e) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          if (shown.length) { active = (active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length; mark(); }
          return true;
        }
        if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); exec(active); return true; }
        if (e.key === "Tab") { e.preventDefault(); return true; }
        return false;
      },
    });
    input.addEventListener("input", () => { active = 0; render(); });
    render();
    setTimeout(() => input.focus(), 20);
  }

  // ---------------------------------------------------------------- 通用小动作
  async function exportLogs() {
    const r = await invoke("export_logs");
    if (r && r.ok) toast("日志已导出，已在资源管理器中选中", "ok");
  }
  async function clearLogs() {
    if (has("clear_log")) { try { await api.clear_log(); } catch (e) { /* 忽略 */ } }
    window.DFQLog.clear();
    S.seq = 0;
    toast("日志已清空", "info", 1600);
  }
  async function openPath(what) {
    const r = await invoke("open_path", what);
    return !!r;
  }
  async function checkConfig() {
    if (!has("check_config")) return;
    let r;
    try { r = await api.check_config(); } catch (e) { toast("检查失败：" + ((e && e.message) || e), "error"); return; }
    if (r.error) await modal({ title: "配置有问题", text: r.error, okText: "知道了", single: true, danger: true });
    else await modal({ title: "配置正确", text: "配置检查通过。\n\n" + ((r.notes || []).length ? r.notes.map((n) => "· " + n).join("\n") : "没有需要注意的地方。"),
      okText: "好的", single: true, icon: "ok" });
  }
  async function switchSource(mock) {
    if (!has("set_mock")) return false;
    let r;
    try { r = await api.set_mock(mock); } catch (e) { toast("切换失败：" + ((e && e.message) || e), "error"); return false; }
    if (r && r.error) { toast(r.error, "error"); return false; }
    toast(mock ? "已切换到模拟数据" : "已切换到真实行情：运行会按次计费，执行前会再确认", mock ? "ok" : "warn");
    await refreshState();
    S.planLoaded = false;
    refreshPage();
    return true;
  }
  function refreshCurrent() {
    refreshState();
    if (S.page === "plan") refreshPlan(true);
    else refreshPage();
    if (S.page !== "plan") toast("已刷新", "info", 1200);
  }
  function focusLogSearch() {
    if (sideCollapsed()) { if (S.narrow) { S.sideOpenNarrow = true; layoutSide(); } else setPref("side_collapsed", false, { quiet: true }); }
    setTimeout(() => window.DFQLog.focusSearch(), 50);
  }

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    // 顶部标签：点击 + 方向键
    $$("#tabs .tab").forEach((t) => t.addEventListener("click", () => showPage(t.dataset.page)));
    $("#tabs").addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const i = PAGES.indexOf(S.page);
      const n = PAGES[(i + (e.key === "ArrowLeft" ? -1 : 1) + PAGES.length) % PAGES.length];
      e.preventDefault();
      showPage(n, { focus: true });
    });
    // 通用：data-action 按钮（含任务清单里的按钮）
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-action]");
      if (!b || b.disabled) return;
      const action = b.dataset.action;
      const params = {};
      if (["run_rules", "run_llm"].includes(action)) params.force = $("#force").checked;
      run(action, params);
    });
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-open]");
      if (b) openPath(b.dataset.open);
    });
    // 数据来源
    sourceSeg = seg($("#source"), { onChange: (v) => switchSource(v === "mock") });
    // 计划
    planSeg = seg($("#plan-view-seg"), { value: S.planMode, onChange: (v) => setPlanMode(v) });
    setPlanMode(S.planMode);
    $("#plan-refresh").addEventListener("click", () => refreshPlan(true));
    // 记账
    seg($("#pos-book"), {
      value: S.posBook,
      onChange: (v) => { S.posBook = v; $("#positions").dataset.loaded = ""; refreshPositions(); },
    });
    $("#pos-refresh").addEventListener("click", () => refreshPositions());
    const sideSeg = seg($("#side"), { value: "buy", onChange: (v) => $("#net-field").classList.toggle("hidden", v !== "sell") });
    seg($("#record-kind"), {
      value: "trade",
      onChange: (v) => {
        $("#pane-trade").classList.toggle("hidden", v !== "trade");
        $("#pane-opening").classList.toggle("hidden", v !== "opening");
      },
    });
    $("#paper").addEventListener("change", () => { S.paperTouched = true; });
    $("#inv-paper").addEventListener("change", () => { S.paperTouched = true; });
    $("#trade-btn").addEventListener("click", async () => {
      const side = sideSeg.value;
      const params = { side, item: $("#item").value, qty: $("#qty").value, price: $("#price").value, paper: $("#paper").checked };
      const net = $("#net").value.trim();
      if (side === "sell" && net) params.net = net;
      if (await run("trade", params)) { $("#qty").value = ""; $("#price").value = ""; $("#net").value = ""; }
    });
    $("#inv-btn").addEventListener("click", async () => {
      const params = { item: $("#inv-item").value, qty: $("#inv-qty").value, cost: $("#inv-cost").value, paper: $("#inv-paper").checked };
      if (await run("inventory_add", params)) { $("#inv-qty").value = ""; $("#inv-cost").value = ""; }
    });
    $("#balance-btn").addEventListener("click", async () => {
      if (await run("balance_set", { amount: $("#balance").value })) $("#balance").value = "";
    });
    $("#trades-refresh").addEventListener("click", () => { refreshTrades(); toast("已刷新", "info", 1200); });
    seg($("#trade-filter"), { value: "all", onChange: (v) => { S.tradeFilter = v; renderTrades(); } });
    $("#undo-btn").addEventListener("click", () => {
      if (S.selectedTrade === null) { toast("请先在表格里点选要撤销的那笔成交", "info"); return; }
      run("trade_undo", { id: S.selectedTrade });
    });
    // 复盘
    const rid = () => ({ id: $("#reflect-id").value });
    $("#reflect-show").addEventListener("click", () => run("reflect_show", rid()));
    $("#reflect-accept").addEventListener("click", () => run("reflect_accept", rid()));
    $("#reflect-reject").addEventListener("click", () => run("reflect_reject", rid()));
    $("#review-copy").addEventListener("click", () => {
      const t = S.results.review || "";
      if (!t.trim()) toast("还没有结果可以复制", "info"); else copyText(t, "结果");
    });
    // 资讯
    const count = () => { $("#note-count").textContent = `${$("#note-text").value.length.toLocaleString("zh-CN")} 字`; };
    $("#note-text").addEventListener("input", count);
    $("#note-btn").addEventListener("click", async () => {
      const ok = await call("note_add", $("#src").value, $("#src-name").value, $("#src-title").value, $("#note-text").value);
      if (ok) { $("#note-text").value = ""; $("#src-title").value = ""; count(); toast("已保存入库", "ok"); }
    });
    // 右栏
    $("#continue-btn").addEventListener("click", () => { if (has("continue_")) api.continue_(); });
    $("#export-btn").addEventListener("click", exportLogs);
    $("#clear-btn").addEventListener("click", clearLogs);
    window.DFQLog.init({ box: $("#log"), countEl: $("#log-count"), filterEl: $("#log-filter"), searchEl: $("#log-search"),
      autoBtn: $("#log-auto"), copyBtn: $("#log-copy") });
    $("#side-btn").addEventListener("click", toggleSide);
    $("#rail-open").innerHTML = window.DFQ_ICONS.sideOpen;
    $("#rail-open").addEventListener("click", toggleSide);
    $(".pages").addEventListener("mousedown", () => { if (S.narrow && S.sideOpenNarrow) { S.sideOpenNarrow = false; layoutSide(); } });
    // 顶栏
    $("#theme-btn").addEventListener("click", () => setPref("theme", root.dataset.theme === "dark" ? "light" : "dark", { quiet: true }));
    $("#cmd-btn").addEventListener("click", openPalette);
    // 快捷键
    document.addEventListener("keydown", onShortcut);
    window.addEventListener("resize", debounce(onResize, 60));
    // 新版后端才有的动作：旧版时禁用并说明
    if (!v2()) {
      $$("[data-v2]").forEach((b) => { b.disabled = true; b.dataset.locked = "1"; b.title = "当前后端版本还没有这个功能，请更新 DFQ"; });
      $("#inv-btn").disabled = true;
      $("#inv-btn").dataset.locked = "1";
      $("#inv-btn").title = "当前后端版本还没有期初库存功能，请更新 DFQ";
    }
  }
  function onShortcut(e) {
    if (U.modalOpen()) return;
    const k = e.key;
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && !e.shiftKey && !e.altKey && /^[1-6]$/.test(k)) { e.preventDefault(); showPage(PAGES[Number(k) - 1]); return; }
    if (ctrl && (k === "k" || k === "K")) { e.preventDefault(); openPalette(); return; }
    if (ctrl && k === ",") { e.preventDefault(); showPage("settings"); return; }
    if (ctrl && (k === "b" || k === "B")) { e.preventDefault(); toggleSide(); return; }
    if (ctrl && (k === "l" || k === "L")) { e.preventDefault(); focusLogSearch(); return; }
    if (k === "F5" || (ctrl && (k === "r" || k === "R"))) { e.preventDefault(); refreshCurrent(); return; }   // 不让浏览器组件整页重载
    if (k === "Escape") {
      if (S.narrow && S.sideOpenNarrow) { S.sideOpenNarrow = false; layoutSide(); return; }
      const a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) a.blur();
    }
  }

  // ---------------------------------------------------------------- 关闭窗口：托盘 / 退出
  // 后端在用户点 × 时调用 window.dfqAskClose()（见 dfq/webui/app.py）
  let closeAsking = false;
  window.dfqAskClose = async function () {
    if (closeAsking || !api) return;                          // 已经在问了
    closeAsking = true;
    let q = { busy: false };
    try { q = await api.quit_check(); } catch (e) { /* 忽略 */ }
    const text = q.busy
      ? `“${q.label}”还在运行。退出程序会中断它（记账写到一半可能不完整），最小化到托盘则会继续运行。`
      : "要把程序最小化到托盘继续在后台运行，还是直接退出？";
    const choice = (id, ic, title, sub) => h("button.choice", { type: "button", id }, [h("span.choice-ico", null, icon(ic)),
      h("span.choice-title", { text: title }), h("span.choice-sub", { text: sub })]);
    const tray = choice("close-tray", "download" in window.DFQ_ICONS ? "download" : "arrowDown", "最小化到托盘", "窗口隐藏，程序继续运行；双击托盘图标可以重新打开");
    const quit = choice("close-quit", "close", "退出程序", "完全关闭 DFQ");
    const remember = h("input", { type: "checkbox", id: "close-remember" });
    const cancel = h("button.btn", { type: "button", id: "close-cancel", text: "取消" });
    const panel = h("div.modal", { "aria-labelledby": "close-title" }, [
      h("div.modal-head", null, [h("div.m-ico" + (q.busy ? ".warn" : ""), null, icon(q.busy ? "warn" : "info")),
        h("h3", { id: "close-title", text: "关闭 DFQ" })]),
      h("div.modal-text", { id: "close-text", text }),
      h("div.close-choices", null, [tray, quit]),
      h("label.check.remember", null, [remember, h("span", { text: "记住我的选择，下次不再提醒（可以在“设置 › 常规”里修改）" })]),
      h("div.modal-actions", null, cancel),
    ]);
    const ctx = U.openLayer({ panel, label: "关闭 DFQ", onClose: () => { closeAsking = false; } });
    const done = async (action) => {
      const rem = remember.checked;
      ctx.close(action);
      if (!action) return;                                    // 取消：什么都不做，窗口留着
      try { await api.close_choice(action, rem); } catch (e) { /* 窗口可能已经关了 */ }
      if (rem) { S.prefs.close_action = action; if (window.DFQSettings) window.DFQSettings.onPrefs(S.prefs); }
    };
    tray.addEventListener("click", () => done("tray"));
    quit.addEventListener("click", () => done("quit"));
    cancel.addEventListener("click", () => done(null));
    setTimeout(() => tray.focus(), 30);
  };

  // ---------------------------------------------------------------- 启动
  // 整个窗口都拦住拖放：否则把文件拖到别处，浏览器组件会直接打开这个文件（导入区自己处理 drop）
  ["dragover", "drop"].forEach((ev) => window.addEventListener(ev, (e) => e.preventDefault()));

  function bootVisuals() {
    U.fillIcons();
    $("#brand-logo").innerHTML = window.DFQ_LOGO;
    try { const m = localStorage.getItem("dfq-plan-view"); if (m === "text" || m === "cards") S.planMode = m; } catch (e) { /* 忽略 */ }
    try { S.prefs.side_collapsed = localStorage.getItem("dfq-side") === "1"; } catch (e) { /* 忽略 */ }
    S.narrow = window.innerWidth < 1100;
    updateThemeButton();
    layoutSide();
    buildMetrics();
    placeTabIndicator(false);
  }

  async function start() {
    if (api) return;
    api = window.pywebview.api;
    bind();
    if (window.DFQSettings) {
      window.DFQSettings.init({ api, has, invoke, call, run, setPref, prefs: () => S.prefs, state: () => S.state, refreshState,
        showPage, kickPoll, setMoligodMode, moligodSeg, openUrl, openPath, checkConfig, exportLogs, badge, v2 });
    }
    await loadPrefs();
    watchSystem();
    syncWindowTheme(S.prefs.theme || "");                      // 首屏按缓存渲染；这里让窗口底色与实际主题一致
    try {
      const t = await api.tasks();
      S.tasks = t || S.tasks;
      renderTasks($("#home-tasks"), S.tasks.home);
      renderTasks($("#review-tasks"), S.tasks.review);
      stagger($("#home-tasks"));
    } catch (e) { toast("读取任务清单失败：" + ((e && e.message) || e), "error"); }
    await refreshState();
    refreshDashboard();
    refreshTrades();
    poll();
    // 网址参数（预览工具用）：?page=settings&group=appearance
    const qs = new URLSearchParams(location.search);
    if (qs.get("page")) showPage(qs.get("page"));
    if (qs.get("group") && window.DFQSettings) window.DFQSettings.show(qs.get("group"));
  }

  bootVisuals();
  window.DFQApp = { showPage, openPalette, toast, modal, setPref };
  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start);
})();
