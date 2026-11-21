// DFQ 设置页：左侧分组导航（滑动指示）+ 右侧内容 + 跨分组搜索。
// 每一行“改了立即保存”：成功轻提示“已保存”，失败回滚控件并提示原因。
// 用到的后端接口（都先判断有没有，旧版后端没有时显示占位说明，不让整页报错）：
//   外观 / 常规 / 性能：ui_prefs、set_pref（旧版只有 set_theme、set_close_action）、about（浏览器组件版本）
//   交易与风控、服务：settings、set_config、set_moligod_mode、run("fee_check")、state().secrets、set_secret、clear_secret、run("mail_test")
//   数据与备份：storage、clean、backup_now、backups、restore_backup、export_config、import_config、reset_config、check_config、open_path
//   计划任务：tasks_status、install_tasks、remove_tasks；关于：about、check_update、open_url、export_logs
// 后端返回的文字一律用 textContent 写入（ui.js 的 h()），不拼进 innerHTML。
"use strict";

(function () {
  const U = window.DFQUI;
  const { $, $$, h, icon, clear, toast, modal, seg, toggle, isNum, fmtBytes, fmtPct, shortTime, debounce, placeIndicator } = U;
  const T = window.DFQTheme;
  const root = document.documentElement;

  const GROUPS = [
    { id: "appearance", title: "外观", icon: "palette", hint: "深浅色、主题色、缩放、密度和动效。改动立即生效" },
    { id: "general", title: "常规", icon: "sliders", hint: "关闭窗口、任务通知、窗口位置" },
    { id: "performance", title: "性能", icon: "chip", hint: "硬件加速与内存占用" },
    { id: "trade", title: "交易与风控", icon: "shield", hint: "写入 config.yaml；改动前会按启动时同样的规则整体检查一遍" },
    { id: "services", title: "服务与密钥", icon: "key", hint: "密钥只能填写、不能查看；其余选项写入 config.yaml" },
    { id: "data", title: "数据与备份", icon: "database", hint: "存储占用、数据库备份、配置文件" },
    { id: "tasks", title: "计划任务", icon: "clock", hint: "Windows 计划任务：每晚运行、定时读取邮件回复" },
    { id: "about", title: "关于", icon: "about", hint: "" },
  ];
  const SPEC_ICON = { trade: "ledger", fee: "wallet", risk: "shield", xtc: "pulse", llm: "spark", news: "globe", mail: "mail" };
  const ACCENTS = [["薄荷", "#78b49d"], ["松石", "#3fb6a8"], ["天青", "#4c9be8"], ["靛蓝", "#6b7cf6"], ["葡萄", "#9a6cf0"],
    ["玫红", "#e0608f"], ["珊瑚", "#ef7a5a"], ["琥珀", "#e0a23a"], ["青柠", "#8fbf3f"], ["石墨", "#6f7b85"]];
  // 本页面自己就能保存的外观项（后端没有 set_pref 时存在本机）；其他偏好需要新版后端
  const LOCAL_OK = new Set(["theme", "close_action", "accent", "density", "motion", "glow", "side_collapsed"]);
  const STORE_COLORS = ["var(--accent)", "var(--info)", "var(--warn)", "var(--ok)", "#9a6cf0", "#e0608f", "#ef7a5a", "#6f7b85",
    "#3fb6a8", "var(--faint)"];
  const TASK_NAMES = { "dfq-nightly": ["每晚运行", "生成计划并发邮件"], "dfq-mail-poll": ["读取邮件回复", "回复计划邮件即可记账"],
    "dfq-watch": ["异常波动检查", "每小时看一眼价格（需要在配置里开启）"] };
  const SHORTCUTS = [["Ctrl + K", "命令面板：搜索并执行任何操作"], ["Ctrl + 1 ～ 6", "切换到对应页面"], ["Ctrl + ,", "打开设置"],
    ["Ctrl + B", "收起 / 展开右侧栏"], ["Ctrl + L", "搜索日志"], ["F5", "刷新当前页数据"], ["Esc", "关闭弹窗 / 面板"]];
  const DISCLAIMER_FALLBACK = "DFQ 只做决策辅助：它不会操作游戏、不会替你下单，所有买卖都由你自己在游戏里完成。"
    + "价格数据来自第三方平台，可能有延迟或错误；计划里的价格线和数量只是参考，不构成任何收益承诺。请量力而行。";

  let C = null;                 // app.js 传进来的上下文（api、invoke、setPref……）
  const S = {
    current: "appearance", query: "", built: false,
    settings: null, storage: null, backups: null, tasksStatus: null, about: null,
    loaded: {}, loading: {},
  };
  const prefCtl = {};           // 偏好键 → {set(value)}：其他地方改了偏好时同步控件
  const sections = {};          // 分组 id → {sec, body}

  // ---------------------------------------------------------------- 小工具
  function errText(e) { return (e && e.message) || String(e); }
  async function fetchApi(method, ...args) {
    if (!C.has(method)) return { missing: true };
    try {
      const r = await C.api[method](...args);
      return r === null || r === undefined ? {} : r;
    } catch (e) { return { error: `读取失败（${method}）：${errText(e)}` }; }
  }
  function card(title, iconName, hint, extra) {
    return h("div.card.set-card", null, h("div.card-head", null, [
      h("h3", null, [icon(iconName, "h-ico"), title]), hint ? h("span.hint", { text: hint }) : null,
      extra ? h("div.spacer") : null, extra || null,
    ]));
  }
  // 一个设置行：左边标题 + 说明，右边控件；search 里的文字参与搜索
  function row({ title, desc, ctl, block = false, search = "", badges = null, keyName = null }) {
    const titleEl = h("div.set-title", null, [h("span", { text: title }), ...(badges || [])]);
    const el = h("div.set-row" + (block ? ".block" : ""), null, [
      h("div.set-text", null, [titleEl, desc ? h("div.set-desc", { text: desc }) : null,
        keyName ? h("div.set-key", { text: keyName }) : null]),
      ctl ? h("div.set-ctl", null, ctl) : null,
    ]);
    el.dataset.search = [title, desc, search, keyName].filter(Boolean).join(" ").toLowerCase();
    el._titleEl = titleEl;
    return el;
  }
  function hl(el) {
    if (!el) return;
    el.classList.remove("hl");
    void el.offsetWidth;
    el.classList.add("hl");
  }
  function placeholder(text, action) {
    return h("div.placeholder", null, [icon("info"), h("div", null, [h("span", { text }),
      action ? h("div", { style: { marginTop: "10px" } }, action) : null])]);
  }
  function btn(label, iconName, onClick, cls = "") {
    const b = h("button.btn" + cls, { type: "button" }, [iconName ? icon(iconName, "b-ico") : null, h("span", { text: label })]);
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }
  function fmtTime(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (isNum(v)) {
      const d = new Date(v < 1e12 ? v * 1000 : v);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    }
    return String(v).replace("T", " ").slice(0, 16);
  }
  function newBackend() { return C.has("set_pref"); }
  // 带单位的输入框：单位写在框内右侧，按单位字数留出空间，数字不会和单位重叠
  function unitInput(inp, unit) {
    inp.style.paddingRight = 16 + unit.length * 13 + "px";
    return h("span.unit-input", null, [inp, h("span.unit", { text: unit })]);
  }

  // ---------------------------------------------------------------- 偏好控件（立即保存，失败回滚）
  async function savePref(key, value) {
    const r = await C.setPref(key, value);
    return !!r;
  }
  function prefUnavailable(key) { return !newBackend() && !LOCAL_OK.has(key); }
  function prefSeg(key, items, { toValue = (v) => v, fromValue = (v) => String(v), small = false } = {}) {
    const el = h("div.segmented" + (small ? ".small" : ""), { "aria-label": key });
    const s = seg(el, { items, value: fromValue(C.prefs()[key]), onChange: async (v) => savePref(key, toValue(v)) });
    prefCtl[key] = { set: (v) => { const f = fromValue(v); if (s.value !== f) s.set(f); } };
    if (prefUnavailable(key)) s.disable(true);
    return el;
  }
  function prefToggle(key, label, after) {
    const t = toggle({ value: !!C.prefs()[key], label, onChange: async (v) => {
      const ok = await savePref(key, v);
      if (ok && after) after(v);
      return ok;
    } });
    prefCtl[key] = { set: (v) => t.set(!!v) };
    if (prefUnavailable(key)) t.el.disabled = true;
    return t.el;
  }
  function needBackendBadge(key) {
    return prefUnavailable(key) ? [C.badge("需要新版后端", "", null)] : null;
  }

  // ---------------------------------------------------------------- 1. 外观
  function buildAppearance(body) {
    // 实时预览：一张迷你卡片 + 主按钮 + 开关 + 分段选择器，改颜色马上能看到效果
    const pvSeg = h("div.segmented.small", { "aria-hidden": "true" }, [
      h("button.seg", { type: "button", "data-value": "a", text: "卡片", tabindex: "-1" }),
      h("button.seg", { type: "button", "data-value": "b", text: "原文", tabindex: "-1" })]);
    const pvSwitch = toggle({ value: true, label: "预览开关" });
    pvSwitch.el.tabIndex = -1;
    const preview = h("div.preview", { "aria-label": "实时预览" }, [
      h("div.pv-card", null, [
        h("div.pv-tabs", null, [h("span.on", { text: "首页" }), h("span", { text: "最新计划" }), h("span", { text: "记账" })]),
        h("div.pv-row", null, [h("div.task-icon", null, icon("spark")), h("div", { style: { flex: "1", minWidth: "0" } },
          [h("div.pv-title", { text: "运行（规则 + 大模型）" }), h("div.pv-sub", { text: "每日流程 · 生成计划" })]),
        h("button.btn.primary.sm", { type: "button", tabindex: "-1" }, [icon("play", "b-ico"), h("span", { text: "运行" })])]),
      ]),
      h("div.pv-side", null, [pvSeg, h("div.pv-row", null, [pvSwitch.el, h("span.pv-sub", { text: "氛围背景" })]),
        h("div.pv-row", null, [C.badge("有效至 11-17 23:45", "accent"), C.badge("模拟", "")])]),
    ]);
    seg(pvSeg, { value: "a" });
    const pvCard = h("div.card.set-card", { style: { padding: "16px" } }, preview);

    const c1 = card("主题", "palette", "");
    c1.appendChild(row({ title: "主题模式", desc: "跟随系统时，Windows 切换深浅色后这里自动跟着变", search: "深色 浅色 暗色 dark light",
      ctl: prefSeg("theme", [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]],
        { toValue: (v) => (v === "system" ? "" : v), fromValue: (v) => (v === "light" || v === "dark" ? v : "system") }) }));
    c1.appendChild(accentRow());

    const c2 = card("显示", "eye", "");
    c2.appendChild(row({ title: "界面缩放", desc: "放大或缩小整个界面（文字、按钮一起变）", search: "缩放 字体 大小 zoom", badges: needBackendBadge("scale"),
      ctl: prefSeg("scale", [["90", "90%"], ["100", "100%"], ["110", "110%"], ["125", "125%"]], { toValue: (v) => Number(v) }) }));
    c2.appendChild(row({ title: "界面密度", desc: "紧凑：卡片和表格行更矮，一屏能看到更多内容", search: "紧凑 舒适 密度",
      ctl: prefSeg("density", [["comfortable", "舒适"], ["compact", "紧凑"]]) }));
    c2.appendChild(row({ title: "动效", desc: "减弱：去掉位移和缩放，只保留很短的淡入淡出；关闭：完全没有动画", search: "动画 动效 motion",
      ctl: prefSeg("motion", [["auto", "跟随系统"], ["full", "完整"], ["reduced", "减弱"], ["off", "关闭"]]) }));
    c2.appendChild(row({ title: "氛围背景", desc: "页面背景上两处很淡的主题色光晕。关闭硬件加速或动效时不显示", search: "光晕 背景 glow",
      ctl: prefToggle("glow", "氛围背景") }));
    body.append(pvCard, c1, c2);
  }
  function accentRow() {
    const sw = h("div.swatches", { role: "radiogroup", "aria-label": "预设主题色" });
    const buttons = ACCENTS.map(([name, hex]) => {
      const b = h("button.swatch", { type: "button", role: "radio", title: `${name} ${hex}`, "aria-label": name, "aria-checked": "false",
        "data-hex": hex, style: { "--sw": hex } });
      b.style.setProperty("--sw", hex);
      b.addEventListener("click", () => saveAccent(hex));
      sw.appendChild(b);
      return b;
    });
    const picker = h("input", { type: "color", "aria-label": "自定义主题色", title: "自定义颜色" });
    const hex = h("input.hex", { type: "text", maxlength: "7", "aria-label": "主题色十六进制", spellcheck: "false", autocomplete: "off",
      placeholder: "#78b49d" });
    const reset = h("button.btn.sm.ghost", { type: "button", title: "恢复默认的薄荷绿" }, [icon("restore", "b-ico"), h("span", { text: "恢复默认" })]);
    let saved = C.prefs().accent || T.DEFAULT_ACCENT;
    const sync = (v) => {
      v = (T.validHex(v) ? v : T.DEFAULT_ACCENT).toLowerCase();
      saved = v;
      picker.value = v;
      if (document.activeElement !== hex) hex.value = v;
      for (const b of buttons) b.setAttribute("aria-checked", b.dataset.hex === v ? "true" : "false");
      custom.classList.toggle("on", !ACCENTS.some(([, x]) => x === v));
    };
    async function saveAccent(v) {
      v = String(v || "").trim().toLowerCase();
      if (!v.startsWith("#")) v = "#" + v;
      if (!T.validHex(v)) { toast("颜色格式不对：请写成 # 加 6 位十六进制，例如 #78b49d", "error"); sync(saved); T.applyAccent(saved); return; }
      if (v === saved) { T.applyAccent(v); return; }
      const ok = await savePref("accent", v);
      if (!ok) { sync(saved); T.applyAccent(saved); }
    }
    picker.addEventListener("input", () => { T.applyAccent(picker.value); hex.value = picker.value; });   // 拖动时实时预览
    picker.addEventListener("change", () => saveAccent(picker.value));                                  // 松手后保存
    hex.addEventListener("input", () => { const v = hex.value.trim(); if (T.validHex(v)) T.applyAccent(v); });
    hex.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); hex.blur(); }
      else if (e.key === "Escape") { hex.value = saved; T.applyAccent(saved); }
    });
    hex.addEventListener("blur", () => { if (hex.value.trim().toLowerCase() !== saved) saveAccent(hex.value); });
    reset.addEventListener("click", () => saveAccent(T.DEFAULT_ACCENT));
    const custom = h("div.custom-color", null, [h("span.cc-label", { text: "自定义" }), picker, hex]);
    prefCtl.accent = { set: sync };
    sync(saved);
    const r = row({ title: "主题色", desc: "按钮、标签指示条、高亮都用这个颜色。拖动自定义颜色时实时预览，松手后保存", block: true,
      search: "颜色 主题色 accent 配色 " + ACCENTS.map((a) => a[0]).join(" "),
      ctl: [sw, h("div.accent-tools", null, [custom, reset])] });
    return r;
  }

  // ---------------------------------------------------------------- 2. 常规
  function buildGeneral(body) {
    const c = card("窗口", "sliders", "");
    c.appendChild(row({ title: "关闭窗口时", desc: "点右上角 × 时怎么处理。关闭提示里勾选“记住我的选择”后，也可以在这里改回来",
      search: "托盘 退出 关闭 最小化", ctl: prefSeg("close_action", [["ask", "每次询问"], ["tray", "最小化到托盘"], ["quit", "退出程序"]]) }));
    c.appendChild(row({ title: "任务结束时通知", desc: "窗口收在托盘里时，任务跑完弹一条系统通知", search: "通知 提醒 notify",
      badges: needBackendBadge("notify_done"), ctl: prefToggle("notify_done", "任务结束时通知") }));
    c.appendChild(row({ title: "记住窗口大小和位置", desc: "下次打开时恢复到上次关闭时的样子", search: "窗口 位置 大小",
      badges: needBackendBadge("remember_window"), ctl: prefToggle("remember_window", "记住窗口大小和位置") }));
    c.appendChild(row({ title: "右侧栏默认收起", desc: "收起后只留一条窄边，随时可以按 Ctrl+B 展开。窗口宽度小于 1100 时总是先收起",
      search: "右栏 侧栏 日志 收起", ctl: prefToggle("side_collapsed", "右侧栏默认收起") }));
    body.appendChild(c);
  }

  // ---------------------------------------------------------------- 3. 性能
  let gpuRow = null, wv2Value = null;
  function buildPerformance(body) {
    const c = card("渲染与内存", "chip", "");
    gpuRow = row({ title: "GPU 硬件加速", desc: "用显卡渲染界面，更流畅；界面花屏或闪烁时可以关掉。改动后需要重启 DFQ 生效",
      search: "显卡 gpu 硬件加速 花屏 闪烁", badges: needBackendBadge("gpu"), ctl: prefToggle("gpu", "GPU 硬件加速", () => updateGpuBadge()) });
    c.appendChild(gpuRow);
    c.appendChild(row({ title: "收进托盘时释放内存", desc: "窗口隐藏后让浏览器组件尽量少占内存；重新打开时会稍微慢一点",
      search: "内存 托盘 节省", badges: needBackendBadge("tray_saver"), ctl: prefToggle("tray_saver", "收进托盘时释放内存") }));
    wv2Value = h("span.set-readonly.mono", { text: "读取中…" });
    c.appendChild(row({ title: "浏览器组件版本", desc: "界面由系统自带的 Microsoft Edge WebView2 显示", search: "webview2 edge 浏览器 版本",
      ctl: wv2Value }));
    body.appendChild(c);
  }
  function updateGpuBadge() {
    if (!gpuRow) return;
    const want = C.prefs().gpu === false ? "off" : "on";
    const old = $(".restart-badge", gpuRow);
    const need = want !== (root.dataset.gpu || "on");            // root.dataset.gpu 是本次启动时的实际状态
    if (need && !old) gpuRow._titleEl.appendChild(h("span.badge.warn.restart-badge", null, [icon("refresh"), h("span", { text: "重启后生效" })]));
    if (!need && old) old.remove();
  }

  // ---------------------------------------------------------------- 4/5. 配置项（settings / set_config，按 spec 自动生成）
  function specValueText(s, v) {
    if (v === null || v === undefined) return "";
    if (s.type === "percent") return isNum(v) ? String(+(v * 100).toFixed(4)) : String(v);
    return String(v);
  }
  function parseSpec(s, raw) {
    raw = String(raw).trim();
    if (s.type === "text") {
      if (/[\r\n]/.test(raw)) return { error: "不能换行" };
      if (raw.length > 300) return { error: "太长了（最多 300 个字）" };
      return { value: raw };
    }
    const n = Number(raw.replace(/[,，\s]/g, "").replace(/%$/, ""));
    if (raw === "" || !isFinite(n)) return { error: `“${s.label}”要填数字` };
    if (s.type === "int" && !Number.isInteger(n)) return { error: `“${s.label}”要填整数` };
    const v = s.type === "percent" ? +(n / 100).toFixed(6) : n;
    const show = (x) => (s.type === "percent" ? +(x * 100).toFixed(4) + "%" : String(x));
    if (isNum(s.min) && v < s.min) return { error: `“${s.label}”不能小于 ${show(s.min)}` };
    if (isNum(s.max) && v > s.max) return { error: `“${s.label}”不能大于 ${show(s.max)}` };
    return { value: v };
  }
  function specRow(s, value, overridden) {
    let rowEl = null;
    let setVal = () => {};
    const save = async (v) => {
      rowEl.classList.add("saving");
      const r = await C.invoke("set_config", s.key, v);
      rowEl.classList.remove("saving");
      if (!r) return false;
      const vals = r.values || {};
      if (S.settings) {
        S.settings.values = { ...(S.settings.values || {}), ...vals };
        if (!(s.key in vals)) S.settings.values[s.key] = v;
      }
      setVal(s.key in vals ? vals[s.key] : v);
      toast(r.backup ? "已保存（今天第一次修改，旧的 config.yaml 已备份）" : "已保存", "ok", 2000);
      hl(rowEl);
      if (/^(general\.paper_mode|llm\.enabled|mail\.enabled|intel\.news\.enabled|xtc\.daily_limit)$/.test(s.key)) C.refreshState();
      return true;
    };
    let ctl;
    if (s.type === "bool") {
      const t = toggle({ value: !!value, label: s.label, onChange: (v) => save(v) });
      setVal = (v) => t.set(!!v);
      ctl = t.el;
    } else if (s.type === "choice" && Array.isArray(s.choices)) {
      const items = s.choices.map(([v, l]) => [String(v), String(l)]);
      if (items.length <= 4) {
        const el = h("div.segmented.small");
        const sg = seg(el, { items, value: value === null || value === undefined ? null : String(value),
          onChange: (v) => save(s.choices.find(([x]) => String(x) === v)[0]) });
        setVal = (v) => sg.set(String(v));
        ctl = el;
      } else {
        const sel = h("select", { "aria-label": s.label }, items.map(([v, l]) => h("option", { value: v, text: l })));
        sel.value = String(value);
        let cur = sel.value;
        sel.addEventListener("change", async () => {
          const ok = await save(s.choices.find(([x]) => String(x) === sel.value)[0]);
          if (!ok) sel.value = cur; else cur = sel.value;
        });
        setVal = (v) => { sel.value = String(v); cur = sel.value; };
        ctl = sel;
      }
    } else {
      const numeric = s.type !== "text";
      const inp = h("input" + (numeric ? ".num-input" : ".text"), { type: "text", inputmode: numeric ? "decimal" : null, autocomplete: "off",
        spellcheck: "false", "aria-label": s.label, placeholder: s.placeholder || (numeric && isNum(s.min) ? "" : "") });
      let committed = specValueText(s, value);
      inp.value = committed;
      setVal = (v) => { committed = specValueText(s, v); if (document.activeElement !== inp) inp.value = committed; };
      const commit = async () => {
        const raw = inp.value.trim();
        if (raw === committed) return;
        const p = parseSpec(s, raw);
        if (p.error) { toast(p.error, "error"); inp.value = committed; return; }
        const ok = await save(p.value);
        if (!ok) inp.value = committed; else inp.value = committed;
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
        else if (e.key === "Escape") inp.value = committed;
      });
      inp.addEventListener("blur", commit);
      if (s.type === "percent" || /（%）$/.test(s.label)) ctl = unitInput(inp, "%");
      else ctl = inp;
    }
    const badges = overridden ? [h("span.badge.info.sm", { title: "data/overrides.yaml 里的反思建议优先于 config.yaml，这里改了暂时不会生效",
      text: "已被反思建议覆盖" })] : null;
    const range = s.type === "percent" && isNum(s.min) && isNum(s.max) ? `范围 ${fmtPct(s.min, 1)}～${fmtPct(s.max, 1)}`
      : (s.type === "int" || s.type === "float") && isNum(s.min) && isNum(s.max) ? `范围 ${s.min}～${s.max}` : "";
    const desc = [s.hint, range].filter(Boolean).join("。");
    rowEl = row({ title: s.label, desc, ctl, badges, keyName: s.key });
    return rowEl;
  }
  function specCards(groupIds) {
    const st = S.settings;
    const out = [];
    for (const gid of groupIds) {
      const g = (st.groups || []).find((x) => x.id === gid);
      const items = (st.spec || []).filter((x) => x.group === gid);
      if (!g || !items.length) continue;
      const c = card(g.title, SPEC_ICON[gid] || "sliders", g.hint || "");
      c.dataset.gid = gid;
      for (const s of items) c.appendChild(specRow(s, (st.values || {})[s.key], (st.from_overrides || []).includes(s.key)));
      out.push(c);
    }
    return out;
  }
  // 配置不能在界面里改时的说明（旧版后端 / 配置不合法 / 读取失败）
  function configProblem(st) {
    if (!st || st.missing) {
      return h("div.card.set-card", { style: { padding: "16px 20px" } }, placeholder(
        "当前后端版本还不能在界面里直接修改这些配置。可以用记事本打开 config.yaml 修改，改完后点“检查配置”。更新 DFQ 后这里会显示可以直接修改的表单。",
        h("div.row-actions", null, [btn("用记事本打开", "external", () => C.openPath("config")), btn("检查配置", "check", () => C.checkConfig())])));
    }
    const c = h("div.card.set-card", { style: { padding: "16px 20px" } });
    c.appendChild(h("div.banner.danger", null, [icon("error"), h("div", null, [h("b", { text: "配置文件有问题，暂时不能在这里修改" }),
      h("div", { text: st.error || "读取配置失败", style: { whiteSpace: "pre-wrap" } })])]));
    c.appendChild(h("div.row-actions", null, [btn("用记事本打开", "external", () => C.openPath("config")),
      btn("恢复默认配置…", "restore", resetConfig), btn("重新读取", "refresh", () => loadGroup(S.current, true))]));
    return c;
  }
  const specBox = {};           // 分组 id → 放 spec 卡片的容器
  function renderSpec(gid) {
    const box = specBox[gid];
    if (!box) return;
    clear(box);
    const st = S.settings;
    const ok = st && !st.missing && st.ok !== false && !st.error;
    if (gid === "trade") {
      // 费率依据放进“费率”那张卡片里（和卖出费率挨着）；配置读不出来时单独成卡
      const feeCard = ok ? null : card("费率依据", "wallet", "回本价、止损亏损都按卖出费率计算");
      if (feeCard) feeCard.appendChild(feeBox);
      if (!ok) { box.append(configProblem(st), feeCard); return; }
      box.append(...specCards(["trade", "fee", "risk"]));
      const fc = box.querySelector('[data-gid="fee"]');
      if (fc) fc.appendChild(feeBox); else box.appendChild(h("div.card.set-card", null, feeBox));
      return;
    }
    if (!ok) { box.appendChild(configProblem(st)); return; }
    box.append(...specCards(["xtc", "llm", "news", "mail"]));
    if (gid === "services") {
      // 邮件分组末尾：发送测试邮件
      const mailCard = [...box.children].pop();
      if (mailCard) {
        mailCard.appendChild(row({ title: "发送测试邮件", desc: "按上面的设置给“你的邮箱”发一封测试信，确认能收到", search: "测试 邮件 发送",
          ctl: btn("发送测试邮件", "mail", () => C.run("mail_test"), ".busy-lock") }));
      }
    }
  }

  // ---------------------------------------------------------------- 4. 交易与风控
  let feeBox = null;
  function buildTrade(body) {
    // moligod：手动 / 自动
    const mg = card("moligod 价格提醒", "bell", "");
    const openBtn = btn("打开 moligod 网页", "external", () => C.openUrl("moligod"), ".sm");
    mg.appendChild(row({ title: "提醒设置方式", desc: "手动：程序在计划里列出每个品种的提醒价位，你到 moligod 网页上手动设置。自动设置功能目前已停用",
      search: "moligod 提醒 自动 手动", ctl: [C.moligodSeg().el, openBtn] }));
    // 费率依据（renderSpec 会把它放进“费率”卡片）
    feeBox = h("div.fee-box");
    renderFee();
    const box = h("div.set-stack");
    specBox.trade = box;
    body.append(mg, box);
  }
  function renderFee() {
    if (!feeBox) return;
    clear(feeBox);
    const st = C.state() || {};
    const f = st.fee;
    let status, desc;
    if (!f) { status = C.badge("未知", ""); desc = "当前后端版本还不显示费率依据。"; }
    else if (f.calibrated && f.calibrated.n >= 3) {
      status = C.badge("按实际成交校准", "ok", "ok");
      desc = `${f.calibrated.n} 笔卖出填了实际到手金额，反推的费率中位数约 ${fmtPct(f.calibrated.median, 1)}`
        + `（范围 ${fmtPct(f.calibrated.min, 1)}～${fmtPct(f.calibrated.max, 1)}）。`;
    } else if (f.evidence) {
      status = C.badge("已用平台数据核对", "ok", "ok");
      desc = `${String(f.evidence.checked_at || "").slice(0, 10)} 核对了 ${f.evidence.n || "?"} 种子弹，手续费 / 出售价中位数 ${fmtPct(f.evidence.median, 2)}。`
        + "平台数据来自第三方，不是游戏官方公告。";
    } else {
      status = C.badge("未核对", "warn", "warn");
      desc = "现在的费率是默认值，不是你账号上实测的数。可以核对一次，或者卖出记账时填“实际到手总额”，满 3 笔后程序自动校准。";
    }
    const rate = f && isNum(f.rate) ? fmtPct(f.rate, 1) : "—";
    const checkBtn = btn("核对费率（花 1 次接口）", "check", () => C.run("fee_check"), ".busy-lock");
    if (!C.v2()) { checkBtn.disabled = true; checkBtn.dataset.locked = "1"; checkBtn.title = "当前后端版本还没有这个功能，请更新 DFQ"; }
    feeBox.appendChild(row({ title: "当前卖出费率 " + rate, desc, badges: [status], search: "费率 手续费 核对 校准 fee", ctl: checkBtn }));
  }

  // ---------------------------------------------------------------- 5. 服务与密钥
  let secretBox = null, secretWhere = null;
  function buildServices(body) {
    const c = card("密钥", "key", "");
    secretWhere = c.querySelector(".card-head");
    const hint = h("span.hint", { text: "" });
    secretWhere.appendChild(hint);
    secretWhere = hint;
    secretBox = h("div.secret-list");
    c.appendChild(secretBox);
    const box = h("div.set-stack");
    specBox.services = box;
    body.append(c, box);
    renderSecrets();
  }
  function renderSecrets() {
    if (!secretBox) return;
    const st = C.state();
    clear(secretBox);
    if (!st) { secretBox.appendChild(h("div.skel-stack", null, [h("div.skel.h22"), h("div.skel.h22.w80")])); return; }
    secretWhere.textContent = st.secret_where ? `保存在 ${st.secret_where}；只能填写，不能查看` : "只能填写，不能查看";
    const writable = st.secret_writable !== false;
    for (const s of st.secrets || []) {
      const fill = btn(s.set ? "更换" : "填写", "key", null, ".sm" + (s.set ? "" : ".primary"));
      const del = btn("清除", "trash", null, ".sm.danger");
      fill.disabled = !writable;
      del.disabled = !writable || !s.set;
      fill.addEventListener("click", async () => {
        const v = await modal({ title: s.desc, text: `${s.name}\n\n粘贴后点保存。内容不会显示，也不会写进日志。`, input: true, okText: "保存", icon: "key" });
        if (v === null) return;
        if (!v.trim()) { toast("没有填写内容，什么都没改", "info"); return; }
        const r = await C.invoke("set_secret", s.name, v);
        if (r) { toast(`已保存 ${s.name}`, "ok"); C.refreshState(); }
      });
      del.addEventListener("click", async () => {
        const r = await C.invoke("clear_secret", s.name);
        if (r) { toast(`已清除 ${s.name}`, "ok"); C.refreshState(); }
      });
      const el = h("div.secret.set-row", null, [
        h("span.desc", { text: s.desc }), h("span.name", { text: s.name, title: s.name }),
        h("span", null, s.set ? C.badge("已设置", "ok", "ok") : C.badge("未设置", "")),
        h("span.actions", null, [fill, del]),
      ]);
      el.dataset.search = `密钥 key token ${s.desc} ${s.name}`.toLowerCase();
      secretBox.appendChild(el);
    }
    if (!(st.secrets || []).length) secretBox.appendChild(h("div.hint", { text: "没有需要填写的密钥。" }));
    if (!writable) secretBox.appendChild(h("div.banner", { style: { marginTop: "10px" } }, [icon("warn"),
      h("span", { text: "现在不能保存密钥（保存位置不可写）。请检查程序文件夹的权限。" })]));
    applySearch();
  }

  // ---------------------------------------------------------------- 6. 数据与备份
  let storeBox = null, backupBox = null, cfgPath = null;
  function buildData(body) {
    // 存储占用
    const sc = card("存储占用", "database", "", btn("刷新", "refresh", () => loadGroup("data", true), ".sm.ghost"));
    storeBox = h("div");
    sc.appendChild(storeBox);
    // 数据库备份
    const backupNow = btn("立即备份", "database", doBackup, ".sm.primary.busy-lock");
    const bc = card("数据库备份", "restore", "", backupNow);
    backupBox = h("div");
    bc.appendChild(backupBox);
    bc.appendChild(row({ title: "启动时自动备份", desc: "打开 DFQ 时，距离上次自动备份超过 20 小时就备份一次", search: "自动备份 启动",
      badges: needBackendBadge("backup_on_start"), ctl: prefToggle("backup_on_start", "启动时自动备份") }));
    bc.appendChild(row({ title: "自动备份保留份数", desc: "超过后删除最旧的自动备份（手动备份不受影响）", search: "保留 份数 备份",
      badges: needBackendBadge("backup_keep"), ctl: keepInput() }));
    // 日志保留
    const lc = card("日志", "terminal", "");
    lc.appendChild(row({ title: "日志保留天数", desc: "启动时删除更早的日志文件", search: "日志 保留 清理", badges: needBackendBadge("log_keep_days"),
      ctl: prefSeg("log_keep_days", [["0", "不清理"], ["7", "7 天"], ["30", "30 天"], ["90", "90 天"], ["365", "一年"]],
        { toValue: (v) => Number(v), small: true }) }));
    // 配置导入 / 导出（原有功能）
    const io = card("配置导入 / 导出", "import", "换电脑、重装后用它快速恢复设置");
    const file = h("input", { type: "file", accept: ".zip,.yaml,.yml,.env", hidden: true });
    const dz = h("label.dropzone", { tabindex: "0" }, [file, h("span.dz-icon", null, icon("import")),
      h("span.dz-title", { text: "把配置文件拖到这里，或点击选择" }),
      h("span.dz-sub", { text: "支持本程序导出的 .zip、config.yaml、.env。导入前会先检查，旧文件自动备份到 data\\backups" })]);
    dz.addEventListener("dragenter", (e) => { e.preventDefault(); dz.classList.add("over"); });
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("over"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("over");
      importFile(e.dataTransfer && e.dataTransfer.files ? e.dataTransfer.files[0] : null);
    });
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
    file.addEventListener("change", () => { importFile(file.files[0]); file.value = ""; });
    const withKeys = h("input", { type: "checkbox" });
    const exp = btn("导出配置…", "export", () => exportConfig(withKeys.checked), ".primary");
    const ioRow = h("div.set-row.block", null, h("div.io-grid", null, [dz, h("div.io-export", null, [
      h("div.io-title", { text: "导出当前配置" }), h("p.hint", { text: "导出为一个 .zip 文件，点按钮后选择保存到哪个文件夹。" }),
      h("label.check", null, [withKeys, h("span", { text: "同时导出密钥（.env，明文）" })]), exp])]));
    ioRow.dataset.search = "导入 导出 配置 备份 zip 换电脑";
    io.appendChild(ioRow);
    // 配置文件（原有按钮）
    const cf = card("配置文件", "settings", "比例一律写小数：5% 写 0.05；改完保存后点“检查配置”");
    cfgPath = h("div.path", { text: "—" });
    const cfRow = h("div.set-row.block", null, [cfgPath, h("div.row-actions", null, [
      h("button.btn", { type: "button", "data-action": "init" }, [icon("plus", "b-ico"), h("span", { text: "生成 config.yaml" })]),
      h("button.btn", { type: "button", "data-open": "config" }, [icon("external", "b-ico"), h("span", { text: "用记事本打开" })]),
      btn("检查配置", "check", () => C.checkConfig()),
      btn("恢复默认配置…", "restore", resetConfig),
      h("button.btn", { type: "button", "data-open": "data" }, [icon("folder", "b-ico"), h("span", { text: "打开数据文件夹" })]),
    ])]);
    cfRow.dataset.search = "配置文件 config.yaml 记事本 检查 恢复默认 数据文件夹";
    cf.appendChild(cfRow);
    body.append(sc, bc, lc, io, cf);
    renderPath();
  }
  function keepInput() {
    const inp = h("input.num-input", { type: "text", inputmode: "numeric", "aria-label": "自动备份保留份数", autocomplete: "off" });
    let cur = String(C.prefs().backup_keep ?? 7);
    inp.value = cur;
    prefCtl.backup_keep = { set: (v) => { cur = String(v ?? 7); if (document.activeElement !== inp) inp.value = cur; } };
    const commit = async () => {
      const raw = inp.value.trim();
      if (raw === cur) return;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 1 || n > 60) { toast("保留份数要填 1～60 的整数", "error"); inp.value = cur; return; }
      if (!(await savePref("backup_keep", n))) inp.value = cur;
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); inp.blur(); } else if (e.key === "Escape") inp.value = cur;
    });
    inp.addEventListener("blur", commit);
    if (prefUnavailable("backup_keep")) inp.disabled = true;
    return unitInput(inp, "份");
  }
  function renderPath() {
    const st = C.state();
    if (!cfgPath || !st) return;
    cfgPath.textContent = (st.config_path || "") + (st.config_exists === false ? "（还不存在，点“生成 config.yaml”）" : "");
  }
  function renderStorage() {
    if (!storeBox) return;
    clear(storeBox);
    const r = S.storage;
    if (!r) { storeBox.appendChild(h("div.skel-stack", null, [h("div.skel.h12"), h("div.skel.h22"), h("div.skel.h22.w80")])); return; }
    if (r.missing) { storeBox.appendChild(placeholder("当前后端版本还不能统计存储占用。可以点“打开数据文件夹”自己查看。")); return; }
    if (r.error) { storeBox.appendChild(h("div.banner.danger", null, [icon("error"), h("span", { text: r.error })])); return; }
    const items = (r.items || []).filter((x) => x);
    const total = isNum(r.total) ? r.total : items.reduce((a, x) => a + (x.bytes || 0), 0);
    storeBox.appendChild(h("div.store-total", null, [h("b", { text: fmtBytes(total) }), h("span.hint", { text: "共占用" }),
      r.data_dir ? h("span.hint.mono-path", { text: r.data_dir, title: r.data_dir }) : null]));
    const bar = h("div.stack-bar", { role: "img", "aria-label": "各项占用比例" });
    const list = h("div");
    items.forEach((x, i) => {
      const color = STORE_COLORS[i % STORE_COLORS.length];
      const k = total > 0 ? (x.bytes || 0) / total : 0;
      if (k > 0.002) {
        const seg = h("i", { title: `${x.label}：${fmtBytes(x.bytes)}` });
        seg.style.setProperty("--c", color);
        seg.style.flex = String(Math.max(k, 0.006));
        bar.appendChild(seg);
      }
      const sw = h("span.sw");
      sw.style.setProperty("--c", color);
      let action = h("span");
      if (x.cleanable) {
        action = btn("清理", "trash", () => doClean(x), ".sm.ghost.busy-lock");
        if (!x.bytes) { action.disabled = true; action.dataset.locked = "1"; }
      }
      const el = h("div.store-row", null, [sw, h("span", { text: x.label || x.id }),
        h("span.files", { text: isNum(x.files) ? `${x.files} 个` : "" }),
        h("span.size", { text: fmtBytes(x.bytes) }), action]);
      list.appendChild(el);
    });
    if (!bar.children.length) bar.appendChild(h("i", { style: { flex: "1", background: "transparent" } }));
    storeBox.append(bar, list);
    const r2 = h("div.set-row.block.flat");
    r2.dataset.search = "存储 占用 空间 清理 缓存 磁盘";
    storeBox.appendChild(r2);
  }
  function renderBackups() {
    if (!backupBox) return;
    clear(backupBox);
    const r = S.backups;
    if (!r) { backupBox.appendChild(h("div.skel-stack", null, [h("div.skel.h22"), h("div.skel.h22.w80")])); return; }
    if (r.missing) { backupBox.appendChild(placeholder("当前后端版本还没有数据库备份功能，请更新 DFQ。")); return; }
    if (r.error) { backupBox.appendChild(h("div.banner.danger", null, [icon("error"), h("span", { text: r.error })])); return; }
    const items = r.items || [];
    if (!items.length) {
      backupBox.appendChild(h("div.empty-line", null, [icon("database"), h("span", { text: "还没有备份。点右上角“立即备份”做第一份。" })]));
    } else {
      const list = h("div.backup-list");
      for (const b of items.slice(0, 30)) {
        const restore = btn("恢复…", "restore", () => doRestore(b), ".sm.ghost.busy-lock");
        const el = h("div.list-row", null, [
          h("span.b-time.tnum", { text: fmtTime(b.mtime) }),
          b.auto ? C.badge("自动", "") : C.badge("手动", "accent"),
          h("span.grow.mono", { text: b.name, title: b.name }),
          h("span.tnum.muted-num", { text: fmtBytes(b.bytes) }), restore,
        ]);
        list.appendChild(el);
      }
      if (items.length > 30) list.appendChild(h("div.hint", { text: `另有 ${items.length - 30} 份更早的备份没有列出`, style: { padding: "8px 0" } }));
      backupBox.appendChild(list);
    }
    const r2 = h("div.set-row.block.flat");
    r2.dataset.search = "备份 恢复 数据库 backup restore";
    backupBox.appendChild(r2);
  }
  async function doBackup() {
    const r = await C.invoke("backup_now");
    if (!r) return;
    toast(`已备份数据库（${fmtBytes(r.bytes)}）`, "ok");
    loadGroup("data", true);
  }
  async function doRestore(b) {
    const r = await C.invoke("restore_backup", b.name);
    if (!r) return;
    await modal({ title: "已恢复", text: `数据库已恢复到 ${fmtTime(b.mtime)} 的备份。` + (r.safety ? `\n\n恢复前的数据库另存为：${r.safety}` : ""),
      okText: "知道了", single: true, icon: "ok" });
    C.refreshState();
    loadGroup("data", true);
  }
  async function doClean(x) {
    const r = await C.invoke("clean", x.id);
    if (!r) return;
    toast(r.freed ? `已清理「${x.label}」，释放 ${fmtBytes(r.freed)}` : `「${x.label}」没有可清理的文件`, "ok");
    loadGroup("data", true);
  }
  async function resetConfig() {
    const r = await C.invoke("reset_config");
    if (!r) return;
    toast("已恢复默认配置，旧文件已备份", "ok");
    await C.refreshState();
    S.loaded.trade = S.loaded.services = false;
    loadGroup(S.current, true);
  }
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
    if (!C.has("import_config")) { toast("当前后端版本不能导入配置", "warn"); return; }
    if (file.size > 1000000) { toast("文件太大，不像是配置文件", "error"); return; }
    let b64;
    try { b64 = await readAsBase64(file); } catch (e) { toast("读取文件失败：" + errText(e), "error"); return; }
    let r;
    try {
      r = await C.api.import_config(file.name, b64, false);
      if (r && r.confirm) {
        if (!(await modal({ title: r.confirm.title, text: r.confirm.text, okText: "导入", icon: "import" }))) return;
        r = await C.api.import_config(file.name, b64, true);
      }
    } catch (e) { toast("导入失败：" + errText(e), "error"); return; }
    if (r && r.error) { await modal({ title: "没有导入", text: r.error, okText: "知道了", single: true, danger: true }); return; }
    toast("配置已导入", "ok");
    await C.refreshState();
    S.loaded.trade = S.loaded.services = false;
  }
  async function exportConfig(withKeys) {
    const r = await C.invoke("export_config", withKeys);
    if (r && r.path) toast("已导出到：\n" + r.path, "ok");
  }

  // ---------------------------------------------------------------- 7. 计划任务
  let tasksBox = null;
  function buildTasks(body) {
    const c = card("任务状态", "clock", "", btn("刷新", "refresh", () => loadGroup("tasks", true), ".sm.ghost"));
    tasksBox = h("div");
    c.appendChild(tasksBox);
    const at = h("input.num-input", { type: "text", inputmode: "numeric", placeholder: "21:30", "aria-label": "每晚运行时间", maxlength: "5" });
    const mins = h("input.num-input", { type: "text", inputmode: "numeric", placeholder: "30", "aria-label": "读取邮件间隔" });
    try { at.value = localStorage.getItem("dfq-task-at") || "21:30"; mins.value = localStorage.getItem("dfq-task-min") || "30"; }
    catch (e) { at.value = "21:30"; mins.value = "30"; }
    const c2 = card("创建 / 更新", "task", "计划任务与本程序共用同一份数据；创建后即使不开 DFQ 也会按时运行");
    c2.appendChild(row({ title: "每晚运行时间", desc: "24 小时制，例如 21:30", search: "时间 每晚 运行", ctl: at }));
    c2.appendChild(row({ title: "读取邮件间隔", desc: "每隔多少分钟读取一次邮件回复（5～720）", search: "邮件 间隔 分钟",
      ctl: unitInput(mins, "分钟") }));
    const install = btn("创建 / 更新计划任务", "check", async () => {
      const t = at.value.trim(), m = mins.value.trim();
      if (!C.has("tasks_status")) { C.call("install_tasks"); return; }              // 旧版后端：只能用默认时间
      if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(t)) { toast("运行时间要写成 时:分，例如 21:30", "error"); return; }
      const n = Number(m);
      if (!Number.isInteger(n) || n < 5 || n > 720) { toast("邮件间隔要填 5～720 的整数（分钟）", "error"); return; }
      try { localStorage.setItem("dfq-task-at", t); localStorage.setItem("dfq-task-min", String(n)); } catch (e) { /* 忽略 */ }
      if (await C.call("install_tasks", false, t, n)) setTimeout(() => loadGroup("tasks", true), 4000);
    }, ".primary.busy-lock");
    const remove = btn("删除计划任务…", "trash", async () => {
      if (await C.call("remove_tasks")) setTimeout(() => loadGroup("tasks", true), 4000);
    }, ".danger.busy-lock");
    if (!C.has("remove_tasks")) { remove.disabled = true; remove.dataset.locked = "1"; remove.title = "当前后端版本还没有这个功能，请更新 DFQ"; }
    const actRow = h("div.set-row", null, h("div.row-actions", null, [install, remove]));
    actRow.dataset.search = "创建 删除 计划任务 install";
    c2.appendChild(actRow);
    body.append(c, c2);
  }
  function renderTasks() {
    if (!tasksBox) return;
    clear(tasksBox);
    const r = S.tasksStatus;
    if (!r) { tasksBox.appendChild(h("div.skel-stack", null, [h("div.skel.h22"), h("div.skel.h22"), h("div.skel.h22.w80")])); return; }
    if (r.missing) { tasksBox.appendChild(placeholder("当前后端版本还不能读取计划任务的状态。可以在 Windows 的“任务计划程序”里查看 dfq- 开头的任务。")); return; }
    if (r.error) { tasksBox.appendChild(h("div.banner.danger", null, [icon("error"), h("span", { text: r.error })])); return; }
    if (r.supported === false) { tasksBox.appendChild(placeholder("计划任务只能在 Windows 上使用。")); return; }
    for (const t of r.tasks || []) {
      const [label, sub] = TASK_NAMES[t.name] || [t.name, ""];
      const badges = [];
      if (!t.exists) badges.push(C.badge("未创建", ""));
      else {
        badges.push(C.badge(t.state === "Disabled" ? "已禁用" : t.state === "Running" ? "运行中" : "已创建", t.state === "Disabled" ? "warn" : "ok"));
        if (t.points_here === false) badges.push(C.badge("指向别的 DFQ 文件夹", "warn", "warn"));
      }
      const last = t.last_run ? [fmtTime(t.last_run), t.last_result === 0 ? "成功" : isNum(t.last_result) ? `返回码 ${t.last_result}` : ""]
        .filter(Boolean).join(" · ") : "还没运行过";
      const el = h("div.task-row", null, [
        h("div.task-icon.sm", null, icon(t.name === "dfq-mail-poll" ? "mail" : t.name === "dfq-watch" ? "pulse" : "clock")),
        h("div.tr-main", null, [h("div.set-title", null, [h("span", { text: label }), ...badges]),
          h("div.set-desc", { text: sub + (t.name ? `（${t.name}）` : "") })]),
        h("div.tr-times", null, [h("div", null, [h("span.k", { text: "下次 " }), h("span.tnum", { text: t.exists ? fmtTime(t.next_run) : "—" })]),
          h("div", null, [h("span.k", { text: "上次 " }),
            h("span.tnum" + (isNum(t.last_result) && t.last_result !== 0 ? ".danger-text" : ""), { text: t.exists ? last : "—" })])]),
      ]);
      el.dataset.search = `计划任务 ${label} ${t.name}`.toLowerCase();
      el.classList.add("set-row");
      tasksBox.appendChild(el);
    }
    applySearch();
  }

  // ---------------------------------------------------------------- 8. 关于
  let aboutBox = null;
  function buildAbout(body) {
    aboutBox = h("div.set-stack");
    const keys = card("快捷键", "keyboard", "");
    const tb = h("table.shortcut-table", null, h("tbody", null, SHORTCUTS.map(([k, d]) => h("tr", null, [
      h("td", null, k.split(" + ").flatMap((x, i) => (i ? [" + ", h("kbd", { text: x })] : [h("kbd", { text: x })]))), h("td", { text: d })]))));
    const kr = h("div.set-row.block", null, tb);
    kr.dataset.search = "快捷键 键盘 shortcut ctrl";
    keys.appendChild(kr);
    body.append(aboutBox, keys);
    renderAbout();
  }
  function renderAbout() {
    if (!aboutBox) return;
    clear(aboutBox);
    const a = S.about && !S.about.missing && !S.about.error ? S.about : null;
    const st = C.state() || {};
    const version = (a && a.version) || st.version || "";
    const logo = h("span.about-logo");
    logo.innerHTML = window.DFQ_LOGO;                         // 写死的 SVG
    const hero = h("div.card.set-card.about-card", null, [
      h("div.about-hero", null, [logo, h("div", null, [h("div.about-name", { text: "DFQ 子弹交易决策助手" }),
        h("div.about-meta", null, [version ? C.badge("v" + version, "accent") : null, a ? C.badge(a.frozen ? "安装版" : "源码运行", "") : null,
          a && a.license ? C.badge(a.license + " 许可", "") : null])])]),
    ]);
    const kv = h("div.kv-grid");
    const pathLink = (text, what) => {
      const b = h("button.path-link", { type: "button", title: "在资源管理器中打开" }, [h("span", { text }), icon("external")]);
      b.addEventListener("click", () => C.openPath(what));
      return b;
    };
    const add = (k, v) => kv.append(h("span.k", { text: k }), v instanceof Node ? v : h("span.v.mono", { text: v || "—" }));
    if (a) {
      add("Python", a.python || "—");
      add("浏览器组件", a.webview2 || "未检测到");
      add("程序文件夹", a.base ? pathLink(a.base, "folder") : "—");
      add("数据文件夹", a.data_dir ? pathLink(a.data_dir, "data") : "—");
    } else if (S.about && S.about.missing) {
      add("配置文件", st.config_path || "—");
    } else {
      add("Python", "读取中…");
    }
    hero.appendChild(kv);
    const upd = btn("检查更新", "update", checkUpdate, ".primary");
    const repo = btn("项目主页", "github", () => C.openUrl("repo"));
    const issues = btn("反馈问题", "bug", () => C.openUrl("issues"));
    if (!C.has("check_update")) { upd.disabled = true; upd.title = "当前后端版本还没有这个功能"; }
    if (!C.has("open_url")) { repo.disabled = issues.disabled = true; }
    const acts = h("div.row-actions", { style: { marginTop: "16px" } }, [upd, repo, issues,
      btn("打开文档文件夹", "book", () => C.openPath("docs")), btn("导出日志", "export", () => C.exportLogs())]);
    hero.appendChild(acts);
    const hr = h("div.set-row.block.flat");
    hr.dataset.search = "关于 版本 更新 主页 反馈 python webview2 文件夹 about";
    hero.appendChild(hr);
    const disc = card("免责声明", "info", "");
    const dr = h("div.set-row.block", null, h("div.disclaimer", { text: (a && a.disclaimer) || DISCLAIMER_FALLBACK }));
    dr.dataset.search = "免责声明 风险";
    disc.appendChild(dr);
    aboutBox.append(hero, disc);
    if (wv2Value) wv2Value.textContent = a ? (a.webview2 || "未检测到") : (S.about && S.about.missing ? "需要新版后端" : "读取中…");
  }
  async function checkUpdate() {
    const r = await C.invoke("check_update");
    if (!r) return;
    const cur = r.current || (S.about && S.about.version) || "";
    if (!r.latest) {
      await modal({ title: "检查更新", text: `当前版本 ${cur}。\n\n还没有查到发布版本。`, okText: "知道了", single: true, icon: "update" });
      return;
    }
    const notes = r.notes ? h("div.update-box", { text: r.notes }) : null;
    const go = await modal({
      title: r.newer ? "有新版本" : "已是最新版本",
      text: `当前版本 ${cur}，最新版本 ${r.latest}。` + (r.newer ? "\n\nDFQ 不会自动下载或安装，请到发布页手动下载。" : ""),
      body: notes, okText: r.newer ? "打开发布页" : "知道了", cancelText: "关闭", single: !r.newer, icon: r.newer ? "update" : "ok", wide: !!notes,
    });
    if (go && r.newer) C.openUrl("releases");
  }

  // ---------------------------------------------------------------- 加载数据（按分组懒加载）
  async function loadGroup(id, force = false) {
    if (!C || S.loading[id]) return;
    if (S.loaded[id] && !force) return;
    S.loading[id] = true;
    try {
      if (id === "trade" || id === "services") {
        const r = await fetchApi("settings");
        S.settings = r;
        S.loaded.trade = S.loaded.services = true;
        renderSpec("trade");
        renderSpec("services");
        renderFee();
      } else if (id === "data") {
        const [st, bk] = await Promise.all([fetchApi("storage"), fetchApi("backups")]);
        S.storage = st;
        S.backups = bk;
        S.loaded.data = true;
        renderStorage();
        renderBackups();
      } else if (id === "tasks") {
        if (!S.tasksStatus) renderTasks();
        S.tasksStatus = await fetchApi("tasks_status");
        S.loaded.tasks = true;
        renderTasks();
      } else if (id === "about" || id === "performance") {
        S.about = await fetchApi("about");
        S.loaded.about = S.loaded.performance = true;
        renderAbout();
      } else S.loaded[id] = true;
    } finally {
      S.loading[id] = false;
    }
    applySearch();
    lockBusy();
  }
  // 运行中禁用会冲突的按钮（.busy-lock 也在 app.js 的 setBusy 里统一处理，这里给刚生成的按钮补上）
  function lockBusy() {
    const busy = document.getElementById("app").classList.contains("is-busy");
    $$("#set-body .busy-lock").forEach((b) => { b.disabled = busy || b.dataset.locked === "1"; });
  }

  // ---------------------------------------------------------------- 导航、搜索
  function buildNav() {
    const nav = $("#set-nav");
    for (const g of GROUPS) {
      const b = h("button.set-nav-item", { type: "button", role: "tab", "data-group": g.id, "aria-selected": "false",
        "aria-controls": "set-" + g.id },
        [icon(g.icon), h("span", { text: g.title })]);
      b.addEventListener("click", () => show(g.id));
      nav.appendChild(b);
    }
    nav.setAttribute("role", "tablist");
    nav.setAttribute("aria-orientation", "vertical");
    nav.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      if (!e.target.classList.contains("set-nav-item")) return;
      e.preventDefault();
      const i = GROUPS.findIndex((g) => g.id === S.current);
      const n = GROUPS[(i + (e.key === "ArrowUp" ? -1 : 1) + GROUPS.length) % GROUPS.length];
      show(n.id);
      $(`.set-nav-item[data-group="${n.id}"]`).focus();
    });
  }
  function buildBody() {
    const body = $("#set-body");
    const builders = { appearance: buildAppearance, general: buildGeneral, performance: buildPerformance, trade: buildTrade,
      services: buildServices, data: buildData, tasks: buildTasks, about: buildAbout };
    for (const g of GROUPS) {
      const inner = h("div.set-group-body");
      const sec = h("section.set-group.hidden", { id: "set-" + g.id, "data-group": g.id, role: "tabpanel", "aria-label": g.title }, [
        h("div.set-group-head", null, [h("h2", { text: g.title }), g.hint ? h("span.hint", { text: g.hint }) : null]), inner]);
      sections[g.id] = { sec, body: inner };
      body.appendChild(sec);
      try { builders[g.id](inner); } catch (e) {
        inner.appendChild(h("div.banner.danger", null, [icon("error"), h("span", { text: "这一组显示出错：" + errText(e) })]));
      }
    }
    body.appendChild(h("div.set-empty.hidden", { id: "set-empty" }, U.emptyState("search", "没有找到相关设置", "换个关键字试试，比如“深色”“备份”“费率”。")));
    renderSpec("trade");
    renderSpec("services");
    renderStorage();
    renderBackups();
    renderTasks();
  }
  function show(id, { scroll = true } = {}) {
    if (!sections[id]) id = "appearance";
    const changed = id !== S.current;
    S.current = id;
    try { localStorage.setItem("dfq-set-group", id); } catch (e) { /* 忽略 */ }
    if (S.query) { const s = $("#set-search"); s.value = ""; S.query = ""; }
    for (const g of GROUPS) {
      const on = g.id === id;
      sections[g.id].sec.classList.toggle("hidden", !on);
      const b = $(`.set-nav-item[data-group="${g.id}"]`);
      if (b) { b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; }
    }
    $("#set-nav").classList.remove("searching");
    applySearch();
    placeNav(changed);
    const sec = sections[id].sec;
    if (changed && U.animOn()) {
      sec.classList.remove("set-enter");
      void sec.offsetWidth;
      sec.classList.add("set-enter");
    }
    if (scroll && changed) $("#set-body").scrollTop = 0;
    loadGroup(id);
    if (id === "performance") loadGroup("about");
    if (id === "about") loadGroup("performance");
  }
  function placeNav(animate) {
    const b = $(`.set-nav-item[data-group="${S.current}"]`);
    const ind = $("#set-nav-ind");
    if (!b || !ind || !b.offsetHeight) return;
    placeIndicator(ind, b, { vertical: true, animate });
  }
  function applySearch() {
    const q = S.query.toLowerCase();
    const searching = !!q;
    $("#set-nav") && $("#set-nav").classList.toggle("searching", searching);
    let any = false;
    for (const g of GROUPS) {
      const { sec } = sections[g.id] || {};
      if (!sec) continue;
      const rows = $$(".set-row", sec);
      if (!searching) {
        rows.forEach((r) => r.classList.remove("s-hide"));
        $$(".set-card", sec).forEach((c) => c.classList.remove("s-hide"));
        sec.classList.toggle("hidden", g.id !== S.current);
        continue;
      }
      const titleHit = (g.title + " " + g.hint).toLowerCase().includes(q);
      let groupHit = false;
      for (const r of rows) {
        const hit = titleHit || (r.dataset.search || r.textContent.toLowerCase()).includes(q);
        r.classList.toggle("s-hide", !hit);
        if (hit) groupHit = true;
      }
      for (const c of $$(".set-card", sec)) {
        const cardRows = $$(".set-row", c);
        const headHit = (($(".card-head", c) || {}).textContent || "").toLowerCase().includes(q);
        if (headHit) cardRows.forEach((r) => r.classList.remove("s-hide"));
        const vis = headHit || titleHit || cardRows.some((r) => !r.classList.contains("s-hide"));
        c.classList.toggle("s-hide", !vis);
        if (vis) groupHit = true;
      }
      sec.classList.toggle("hidden", !groupHit);
      if (groupHit) any = true;
      const nb = $(`.set-nav-item[data-group="${g.id}"]`);
      if (nb) nb.classList.toggle("s-miss", !groupHit);           // 搜索时：没有命中的分组在导航里变淡
    }
    if (!searching) $$(".set-nav-item.s-miss").forEach((b) => b.classList.remove("s-miss"));
    const empty = $("#set-empty");
    if (empty) empty.classList.toggle("hidden", !searching || any);
  }
  const onSearch = debounce(() => {
    const v = $("#set-search").value.trim();
    S.query = v;
    if (v) GROUPS.forEach((g) => loadGroup(g.id === "performance" ? "about" : g.id));   // 跨分组搜索：把还没加载的分组也读一遍
    applySearch();
    if (v) $("#set-body").scrollTop = 0;
  }, 120);

  // ---------------------------------------------------------------- 对外
  function init(ctx) {
    if (S.built) return;
    C = ctx;
    S.built = true;
    buildNav();
    buildBody();
    U.fillIcons($("#page-settings"));
    const search = $("#set-search");
    search.addEventListener("input", onSearch);
    search.addEventListener("keydown", (e) => { if (e.key === "Escape" && search.value) { e.stopPropagation(); search.value = ""; onSearch(); } });
    let first = "appearance";
    try { const g = localStorage.getItem("dfq-set-group"); if (g && sections[g]) first = g; } catch (e) { /* 忽略 */ }
    S.current = null;
    show(first, { scroll: false });
    // 设置页显示出来之后（display 从 none 变为可见）才能量出导航项的位置
    if (typeof ResizeObserver === "function") new ResizeObserver(() => placeNav(false)).observe($("#set-nav"));
    new MutationObserver(lockBusy).observe($("#app"), { attributes: true, attributeFilter: ["class"] });
  }
  function refresh() {
    if (!C) return;
    for (const k of Object.keys(S.loaded)) S.loaded[k] = false;
    loadGroup(S.current, true);
    if (S.current === "performance") loadGroup("about", true);
    renderFee();
    placeNav(false);
  }
  function onPrefs(p) {
    if (!C) return;
    for (const [k, ctl] of Object.entries(prefCtl)) if (k in p) ctl.set(p[k]);
    updateGpuBadge();
  }
  function onState() {
    if (!C) return;
    renderSecrets();
    renderFee();
    renderPath();
    if (!S.about || S.about.missing) renderAbout();
  }
  window.DFQSettings = {
    init, refresh, show: (id) => show(id), onPrefs, onState, checkUpdate: () => checkUpdate(),
    groups: () => GROUPS.map((g) => ({ id: g.id, title: g.title, icon: g.icon, hint: g.hint })),
  };
})();
