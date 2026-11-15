// DFQ 界面逻辑。所有操作都调用后端 window.pywebview.api.*（dfq/webui/api.py）。
// 会花钱 / 改账本的操作：后端第一次返回 {confirm}，这里弹窗，用户点“确定”后带 confirmed=true 再调一次。
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// ---------------------------------------------------------------- 图标（线条风格，内联 SVG，不依赖网络）
const P = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const ICONS = {
  play: P('<path d="M7 5v14l11-7z"/>'),
  rules: P('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>'),
  spark: P('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17l.7 1.8L21.5 19.5l-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7z"/>'),
  gauge: P('<path d="M4 15a8 8 0 1 1 16 0"/><path d="M12 15l4-5"/><circle cx="12" cy="15" r="1.2"/>'),
  probe: P('<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>'),
  mail: P('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>'),
  chart: P('<path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 15l4-4 3 3 5-6"/>'),
  refresh: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>'),
  check: P('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  sun: P('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: P('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
  export: P('<path d="M12 3v12"/><path d="M7 8l5-5 5 5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>'),
  import: P('<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>'),
  eraser: P('<path d="M16 4l4 4-10 10H6l-2-2z"/><path d="M13 7l4 4"/><path d="M10 20h10"/>'),
};

// ---------------------------------------------------------------- 状态
const S = { seq: 0, rseq: 0, busy: false, state: null, selectedTrade: null, wasBusy: false };
let api = null;

// ---------------------------------------------------------------- 工具
function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3200);
}

function modal({ title, text, input = false, okText = "确定" }) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    const inp = $("#modal-input");
    inp.value = "";
    inp.classList.toggle("hidden", !input);
    $("#modal-ok").textContent = okText;
    $("#modal").classList.remove("hidden");
    (input ? inp : $("#modal-ok")).focus();
    const done = (ok) => {
      $("#modal").classList.add("hidden");
      const v = inp.value;
      inp.value = "";                                 // 密钥不在页面上多留一刻
      $("#modal-ok").onclick = $("#modal-cancel").onclick = null;
      document.onkeydown = null;
      resolve(ok ? (input ? v : true) : null);
    };
    $("#modal-ok").onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
    document.onkeydown = (e) => { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); };
  });
}

// 调用后端；处理“需要确认”和错误提示。返回 true 表示已开始执行。
async function call(method, ...args) {
  let r = await api[method](...args);
  if (r && r.confirm) {
    const ok = await modal({ title: r.confirm.title, text: r.confirm.text });
    if (!ok) return false;
    r = await api[method](...withConfirmed(method, args));
  }
  if (r && r.error) { toast(r.error, "error"); return false; }
  kickPoll();                                                  // 动作已开始：马上切到快速轮询
  return true;
}
function withConfirmed(method, args) {
  // run(action, params, confirmed) / clear_secret(name, confirmed) / install_tasks(confirmed)
  if (method === "run") return [args[0], args[1] || {}, true];
  if (method === "install_tasks") return [true];
  if (method === "reset_config") return [true];
  return [...args, true];
}
const run = (action, params = {}) => call("run", action, params);

// ---------------------------------------------------------------- 渲染
function renderTasks(el, list) {
  el.innerHTML = "";
  for (const t of list) {
    const row = document.createElement("div");
    row.className = "task";
    const icon = document.createElement("div");
    icon.className = "task-icon";
    icon.innerHTML = ICONS[t.icon] || ICONS.check;
    const title = document.createElement("div");
    title.className = "task-title";
    title.textContent = t.title;
    const chips = document.createElement("div");
    chips.className = "task-chips";
    for (const c of t.chips) {
      const s = document.createElement("span");
      s.className = "chip";
      s.textContent = c;
      chips.appendChild(s);
    }
    if (t.cost) {
      const b = document.createElement("span");
      b.className = "badge-cost";
      b.textContent = "真实数据时计费";
      chips.appendChild(b);
    }
    const acts = document.createElement("div");
    acts.className = "task-actions";
    for (const a of t.actions) {
      const btn = document.createElement("button");
      btn.className = "btn" + (a.style === "primary" ? " primary" : "");
      btn.textContent = a.label;
      btn.dataset.action = a.action;
      acts.appendChild(btn);
    }
    row.append(icon, title, chips, acts);
    el.appendChild(row);
  }
}

function renderState(st) {
  S.state = st;
  $("#version").textContent = st.version;
  // 数据来源
  $$("#source .seg").forEach((b) => b.classList.toggle("active", (b.dataset.value === "mock") === st.mock));
  $("#source-desc").textContent = st.mock
    ? "模拟数据：不联网、不花接口次数，数据单独存放在 data/mock。"
    : "真实行情：运行会请求小涛查接口，按次计费；执行前都会弹窗确认。";
  // 模式
  const mode = $("#mode");
  if (st.config_error) { mode.textContent = "配置有误"; mode.className = "v danger-text"; }
  else { mode.textContent = st.paper ? "模拟（paper_mode）" : "实盘记账"; mode.className = "v " + (st.paper ? "ok" : "warn"); }
  $("#usage").textContent = st.usage ? `${st.usage.today} / ${st.usage.limit} 次` : "—";
  const f = st.features || {};
  $("#features").innerHTML = "";
  for (const [k, label] of [["llm", "大模型"], ["news", "新闻"], ["mail", "邮件"], ["moligod", "moligod"]]) {
    const p = document.createElement("span");
    p.className = "pill" + (f[k] ? " on" : "");
    p.textContent = label;
    $("#features").appendChild(p);
  }
  // 品种下拉
  const dl = $("#items");
  dl.innerHTML = "";
  for (const n of st.items || []) { const o = document.createElement("option"); o.value = n; dl.appendChild(o); }
  if (st.paper !== null && !S.paperTouched) $("#paper").checked = st.paper;
  // 设置页
  $("#config-path").textContent = st.config_path + (st.config_exists ? "" : "（还不存在，点“生成 config.yaml”）");
  $("#secret-where").textContent = `保存在 ${st.secret_where}；只能填写，不能查看`;
  renderSecrets(st.secrets, st.secret_writable);
}

function renderSecrets(list, writable) {
  const box = $("#secrets");
  box.innerHTML = "";
  for (const s of list) {
    const row = document.createElement("div");
    row.className = "secret";
    row.innerHTML = `<span class="desc"></span><span class="name"></span><span class="stat"></span>
      <span class="actions"><button class="btn">填写 / 更换</button><button class="btn danger">清除</button></span>`;
    $(".desc", row).textContent = s.desc;
    $(".name", row).textContent = s.name;
    const stat = $(".stat", row);
    stat.textContent = s.set ? "已设置" : "未设置";
    stat.className = "stat " + (s.set ? "ok" : "muted");
    const [fill, clear] = $$("button", row);
    fill.disabled = clear.disabled = !writable;
    fill.onclick = async () => {
      const v = await modal({ title: s.desc, text: `${s.name}\n\n粘贴后点保存。内容不会显示，也不会写进日志；\n保存到程序文件夹的 .env（已加入 .gitignore，不会被提交）。`,
        input: true, okText: "保存" });
      if (v === null) return;
      const r = await api.set_secret(s.name, v);
      if (r.error) toast(r.error, "error"); else { toast(`已保存 ${s.name}`); refreshState(); }
    };
    clear.onclick = async () => { if (await call("clear_secret", s.name)) { toast(`已清除 ${s.name}`); refreshState(); } };
    box.appendChild(row);
  }
}

function renderTrades(rows) {
  const tb = $("#trades");
  tb.innerHTML = "";
  $("#trades-empty").classList.toggle("hidden", rows.length > 0);
  for (const r of rows) {
    const tr = document.createElement("tr");
    const cells = [
      ["", r.id], ["", r.ts], [r.side === "buy" ? "buy" : "sell", r.side === "buy" ? "买" : "卖"], ["", r.name],
      ["num", r.qty.toLocaleString()], ["num", r.price.toLocaleString()], ["", null], ["", r.source], ["", r.note],
    ];
    for (const [cls, v] of cells) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      if (v === null) {
        const tag = document.createElement("span");
        tag.className = "tag " + (r.paper ? "paper" : "real");
        tag.textContent = r.paper ? "模拟" : "真实";
        td.appendChild(tag);
      } else td.textContent = v;
      tr.appendChild(td);
    }
    if (S.selectedTrade === r.id) tr.classList.add("selected");
    tr.onclick = () => { S.selectedTrade = r.id; $$("#trades tr").forEach((x) => x.classList.remove("selected")); tr.classList.add("selected"); };
    tb.appendChild(tr);
  }
}

function setBusy(busy, label, waiting) {
  S.busy = busy;
  $$("[data-action], #trade-btn, #balance-btn, #undo-btn, #note-btn, #reflect-show, #reflect-accept, #reflect-reject, #install-tasks, #source .seg")
    .forEach((b) => { b.disabled = busy; });
  const st = $("#status");
  st.classList.toggle("busy", busy);
  st.className = "v " + (busy ? "busy warn" : "ok");
  $("span", st).textContent = busy ? `运行中：${label} …` : "就绪";
  $("#continue-btn").classList.toggle("hidden", !waiting);
}

// ---------------------------------------------------------------- 日志轮询
// 日志只追加新的文本节点（不重写整段文字，日志再长也不会越来越卡）；超过上限时从最前面删掉旧的
const LOG_MAX_CHARS = 300000;
let logChars = 0;
function appendLog(text) {
  if (!text) return;
  const log = $("#log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.appendChild(document.createTextNode(text));
  logChars += text.length;
  while (logChars > LOG_MAX_CHARS && log.firstChild && log.firstChild !== log.lastChild) {
    logChars -= log.firstChild.nodeValue.length;
    log.removeChild(log.firstChild);
  }
  if (atBottom) log.scrollTop = log.scrollHeight;
}
function clearLogView() { $("#log").textContent = ""; logChars = 0; }

// 轮询后端：运行中 0.15 秒一次；空闲 1.5 秒一次；窗口被隐藏（收进托盘）时完全不问
const POLL_BUSY = 150, POLL_IDLE = 1500;
let pollTimer = null, polling = false;
function schedulePoll(ms) {
  clearTimeout(pollTimer);
  if (document.hidden) { pollTimer = null; return; }        // 看不见就不问，回到前台时由 visibilitychange 重新开始
  pollTimer = setTimeout(poll, ms);
}
function kickPoll() { schedulePoll(0); }                      // 刚点了按钮：马上问一次，不用等
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const r = await api.poll(S.seq, S.rseq);
    if (r.seq < S.seq) clearLogView();                        // 后端清空过
    S.seq = r.seq;
    appendLog(r.text);
    if (r.results) {
      S.rseq = r.results_seq;
      for (const [k, v] of Object.entries(r.results)) { const el = $("#out-" + k); if (el) el.textContent = v; }
    }
    setBusy(r.busy, r.busy_label, r.waiting_continue);
    if (S.wasBusy && !r.busy) { refreshState(); refreshTrades(); }
    S.wasBusy = r.busy;
  } catch (e) { /* 窗口关闭时会失败，忽略 */ }
  polling = false;
  schedulePoll(S.busy ? POLL_BUSY : POLL_IDLE);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) kickPoll(); });

async function refreshState() { renderState(await api.state()); }
async function refreshTrades() { renderTrades(await api.trades()); }

// ---------------------------------------------------------------- 事件
function bind() {
  // 顶部标签
  $$("#tabs .tab").forEach((t) => t.onclick = () => {
    $$("#tabs .tab").forEach((x) => x.classList.toggle("active", x === t));
    $$(".page").forEach((p) => p.classList.toggle("active", p.dataset.page === t.dataset.page));
    if (t.dataset.page === "ledger") refreshTrades();
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
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-open]");
    if (!b) return;
    const r = await api.open_path(b.dataset.open);
    if (r.error) toast(r.error, "error");
  });
  // 数据来源
  $$("#source .seg").forEach((b) => b.onclick = async () => {
    const r = await api.set_mock(b.dataset.value === "mock");
    if (r.error) toast(r.error, "error"); else refreshState();
  });
  // 记账
  $$("#side .seg").forEach((b) => b.onclick = () => $$("#side .seg").forEach((x) => x.classList.toggle("active", x === b)));
  $("#paper").onchange = () => { S.paperTouched = true; };
  $("#trade-btn").onclick = async () => {
    const side = $("#side .seg.active").dataset.value;
    const ok = await run("trade", { side, item: $("#item").value, qty: $("#qty").value, price: $("#price").value, paper: $("#paper").checked });
    if (ok) { $("#qty").value = ""; $("#price").value = ""; }
  };
  $("#balance-btn").onclick = async () => { if (await run("balance_set", { amount: $("#balance").value })) $("#balance").value = ""; };
  $("#trades-refresh").onclick = refreshTrades;
  $("#undo-btn").onclick = () => {
    if (S.selectedTrade === null) { toast("请先在表格里点选要撤销的那笔成交"); return; }
    run("trade_undo", { id: S.selectedTrade });
  };
  // 复盘
  const rid = () => ({ id: $("#reflect-id").value });
  $("#reflect-show").onclick = () => run("reflect_show", rid());
  $("#reflect-accept").onclick = () => run("reflect_accept", rid());
  $("#reflect-reject").onclick = () => run("reflect_reject", rid());
  // 资讯
  $("#note-btn").onclick = async () => {
    const ok = await call("note_add", $("#src").value, $("#src-name").value, $("#src-title").value, $("#note-text").value);
    if (ok) { $("#note-text").value = ""; $("#src-title").value = ""; }
  };
  // 设置
  $("#check-config").onclick = async () => {
    const r = await api.check_config();
    if (r.error) await modal({ title: "配置有问题", text: r.error, okText: "知道了" });
    else await modal({ title: "配置正确", text: "配置检查通过。\n\n" + (r.notes.length ? r.notes.map((n) => "· " + n).join("\n") : "没有需要注意的地方。"), okText: "好的" });
  };
  $("#install-tasks").onclick = () => call("install_tasks");
  $("#reset-config").onclick = async () => { if (await call("reset_config")) { toast("已恢复默认配置，旧文件已备份"); refreshState(); } };
  bindConfigIO();
  bindClosePref();
  $("#continue-btn").onclick = () => api.continue_();
  // 日志
  $("#export-btn").innerHTML = ICONS.export;
  $("#clear-btn").innerHTML = ICONS.eraser;
  $("#export-btn").onclick = async () => { const r = await api.export_logs(); if (r.ok) toast("日志已导出，已在资源管理器中选中"); };
  $("#clear-btn").onclick = async () => { await api.clear_log(); clearLogView(); S.seq = 0; };
  // 主题
  // 首屏的主题已经由 index.html <head> 里的小脚本在渲染前设好（不会先闪一下浅色），这里只负责按钮和后续切换
  const applyTheme = (t, save) => {
    document.documentElement.dataset.theme = t;
    $("#theme-btn").innerHTML = t === "dark" ? ICONS.sun : ICONS.moon;
    if (save) {
      try { localStorage.setItem("dfq-theme", t); } catch (e) { /* 忽略 */ }
    }
    api.set_theme(t);                                          // 窗口底色跟着变：拖动缩放时边缘不闪
  };
  applyTheme(document.documentElement.dataset.theme || "light", false);
  $("#theme-btn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  $$("[data-icon]").forEach((el) => { el.innerHTML = ICONS[el.dataset.icon] || ""; });
}

// ---------------------------------------------------------------- 配置导入 / 导出
function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

async function importFile(file) {
  if (!file) return;
  if (file.size > 1000000) { toast("文件太大，不像是配置文件", "error"); return; }
  const b64 = await readAsBase64(file);
  let r = await api.import_config(file.name, b64, false);
  if (r.confirm) {
    if (!(await modal({ title: r.confirm.title, text: r.confirm.text, okText: "导入" }))) return;
    r = await api.import_config(file.name, b64, true);
  }
  if (r.error) { await modal({ title: "没有导入", text: r.error, okText: "知道了" }); return; }
  toast("配置已导入");
  refreshState();
}

function bindConfigIO() {
  const dz = $("#dropzone"), input = $("#import-file");
  // 整个窗口都拦住拖放：否则把文件拖到别处，浏览器组件会直接打开这个文件
  ["dragover", "drop"].forEach((ev) => window.addEventListener(ev, (e) => e.preventDefault()));
  dz.addEventListener("dragenter", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("over");
    importFile(e.dataTransfer.files[0]);
  });
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") input.click(); });
  input.onchange = () => { importFile(input.files[0]); input.value = ""; };
  $("#export-config").onclick = async () => {
    const withKeys = $("#export-secrets").checked;
    let r = await api.export_config(withKeys, false);
    if (r.confirm) {
      if (!(await modal({ title: r.confirm.title, text: r.confirm.text, okText: "仍然导出" }))) return;
      r = await api.export_config(withKeys, true);
    }
    if (r.error) toast(r.error, "error");
    else if (r.path) toast("已导出到：\n" + r.path);
  };
}

// ---------------------------------------------------------------- 关闭窗口：托盘 / 退出
// 后端在用户点 × 时调用 window.dfqAskClose()（见 dfq/webui/app.py）
window.dfqAskClose = async function () {
  const mask = $("#close-modal");
  if (!mask.classList.contains("hidden")) return;           // 已经在问了
  const q = await api.quit_check();
  $("#close-text").textContent = q.busy
    ? `“${q.label}”还在运行。退出程序会中断它（记账写到一半可能不完整），最小化到托盘则会继续运行。`
    : "要把程序最小化到托盘继续在后台运行，还是直接退出？";
  $("#close-remember").checked = false;
  mask.classList.remove("hidden");
  $("#close-tray").focus();
  const done = async (action) => {
    mask.classList.add("hidden");
    document.removeEventListener("keydown", onKey);
    if (!action) return;                                     // 取消：什么都不做，窗口留着
    const remember = $("#close-remember").checked;
    await api.close_choice(action, remember);
    if (remember) loadClosePref();
  };
  const onKey = (e) => { if (e.key === "Escape") done(null); };
  document.addEventListener("keydown", onKey);
  $("#close-tray").onclick = () => done("tray");
  $("#close-quit").onclick = () => done("quit");
  $("#close-cancel").onclick = () => done(null);
  mask.onclick = (e) => { if (e.target === mask) done(null); };
};

async function loadClosePref() {
  const p = await api.ui_prefs();
  $$("#close-action .seg").forEach((b) => b.classList.toggle("active", b.dataset.value === p.close_action));
}

function bindClosePref() {
  $$("#close-action .seg").forEach((b) => b.onclick = async () => {
    const r = await api.set_close_action(b.dataset.value);
    if (r.error) toast(r.error, "error"); else { loadClosePref(); toast("已保存"); }
  });
  loadClosePref();
}

async function start() {
  api = window.pywebview.api;
  const t = await api.tasks();
  renderTasks($("#home-tasks"), t.home);
  renderTasks($("#review-tasks"), t.review);
  bind();
  await refreshState();
  await refreshTrades();
  poll();
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start);
