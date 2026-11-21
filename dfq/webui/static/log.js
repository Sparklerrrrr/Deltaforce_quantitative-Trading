// 运行日志视图：按“行”渲染（一行一个 div），批量用 DocumentFragment + requestAnimationFrame 追加，最多保留 4000 行。
// 后端发来的文本可能在一行中间被切开（分块到达）：没结束的半行先缓存，等换行符到了再渲染。
// 只追加、不重写：日志再长也不会越来越卡。着色：时间戳淡色、级别、[节点名]、▶ 开始行、■ 结束行。
"use strict";

(function () {
  const { $, h, toast, copyText, debounce } = window.DFQUI;
  const MAX_LINES = 4000;
  // "21:30:01 WARNING [collect] 文字" —— 见 dfq/log.py 的 ConsoleFormatter
  const RE_LINE = /^(\d{2}:\d{2}:\d{2})(\s+)(DEBUG|INFO|WARNING|ERROR|CRITICAL)?(\s*)(\[[^\]\n]{1,40}\])?(.*)$/;

  const L = {
    lines: [],          // [{text, level, el}]
    partial: "",        // 还没等到换行符的半行
    queue: [],          // 等待渲染的整行
    raf: 0,
    filter: "all",      // all | warn | error
    query: "",
    auto: true,
    box: null,
    countEl: null,
  };

  function classify(text) {
    const m = RE_LINE.exec(text);
    const lv = m && m[3];
    if (lv === "ERROR" || lv === "CRITICAL" || /^出错了|Traceback \(most recent/.test(text) || /^■ 结束（返回码/.test(text)) return "error";
    if (lv === "WARNING") return "warn";
    return "info";
  }

  // 把一行渲染成带着色的片段；有搜索词时高亮命中
  function paint(el, text, level) {
    while (el.firstChild) el.removeChild(el.firstChild);
    el.className = "ll";
    if (text.startsWith("▶")) el.classList.add("start");
    if (/^■ 结束（成功）/.test(text)) el.classList.add("end-ok");
    else if (/^■ 结束/.test(text)) el.classList.add("end-bad");
    if (level === "error") el.classList.add("l-error");
    else if (level === "warn") el.classList.add("l-warn");
    const parts = [];
    const m = RE_LINE.exec(text);
    if (m) {
      parts.push(["ts", m[1]], ["", m[2]]);
      if (m[3]) parts.push([m[3] === "WARNING" ? "lv-warn" : m[3] === "ERROR" || m[3] === "CRITICAL" ? "lv-error" : "lv", m[3]]);
      if (m[4]) parts.push(["", m[4]]);
      if (m[5]) parts.push(["node", m[5]]);
      parts.push(["", m[6]]);
    } else parts.push(["", text]);
    const q = L.query.toLowerCase();
    for (const [cls, t] of parts) {
      if (!t) continue;
      const span = cls ? h("span." + cls) : null;
      const target = span || el;
      if (q) highlight(target, t, q); else target.appendChild(document.createTextNode(t));
      if (span) el.appendChild(span);
    }
    if (!text.trim()) { el.classList.add("blank"); if (!text) el.appendChild(document.createTextNode(" ")); }   // 空行也占一行高
  }
  function highlight(target, t, q) {
    const low = t.toLowerCase();
    let i = 0;
    for (;;) {
      const j = low.indexOf(q, i);
      if (j < 0) { if (i < t.length) target.appendChild(document.createTextNode(t.slice(i))); break; }
      if (j > i) target.appendChild(document.createTextNode(t.slice(i, j)));
      target.appendChild(h("mark", { text: t.slice(j, j + q.length) }));
      i = j + q.length;
    }
  }
  function visible(line) {
    if (L.filter === "warn" && line.level === "info") return false;
    if (L.filter === "error" && line.level !== "error") return false;
    if (L.query && !line.text.toLowerCase().includes(L.query.toLowerCase())) return false;
    return true;
  }

  function nearBottom() { const b = L.box; return b.scrollHeight - b.scrollTop - b.clientHeight < 40; }
  // 半行（例如“完成后点继续”这类不带换行的提示）先临时显示在最后，整行到达后被正式的行替换
  let pendingEl = null;
  function showPartial() {
    if (!L.partial) { if (pendingEl) { pendingEl.remove(); pendingEl = null; } return; }
    if (!pendingEl) pendingEl = document.createElement("div");
    paint(pendingEl, L.partial, classify(L.partial));
    L.box.appendChild(pendingEl);
  }
  function flush() {
    L.raf = 0;
    if (!L.queue.length) { showPartial(); return; }
    if (pendingEl) { pendingEl.remove(); pendingEl = null; }
    const stick = L.auto || nearBottom();
    const frag = document.createDocumentFragment();
    const animate = L.queue.length < 60 && window.DFQUI.animOn();
    for (const text of L.queue) {
      const level = classify(text);
      const el = document.createElement("div");
      const line = { text, level, el };
      paint(el, text, level);
      if (animate) el.classList.add("new");
      if (!visible(line)) el.classList.add("hide");
      L.lines.push(line);
      frag.appendChild(el);
    }
    L.queue = [];
    L.box.appendChild(frag);
    // 超过上限：从头部删除
    if (L.lines.length > MAX_LINES) {
      const drop = L.lines.splice(0, L.lines.length - MAX_LINES);
      for (const d of drop) d.el.remove();
    }
    updateCount();
    showPartial();
    if (L.auto && stick) L.box.scrollTop = L.box.scrollHeight;
  }

  function append(text) {
    if (!text) return;
    const all = L.partial + text.replace(/\r\n?/g, "\n");
    const parts = all.split("\n");
    L.partial = parts.pop();                                      // 最后一段没有换行符：等下一块
    for (const p of parts) L.queue.push(p);
    // 开头的空行（每个动作前的 "\n▶"）不单独占位
    if (!L.lines.length) while (L.queue.length && !L.queue[0].trim()) L.queue.shift();
    if (!L.raf) L.raf = requestAnimationFrame(flush);
  }
  function clearView() {
    L.lines = [];
    L.partial = "";
    L.queue = [];
    pendingEl = null;
    while (L.box.firstChild) L.box.removeChild(L.box.firstChild);
    updateCount();
  }
  function refilter() {
    const filtering = L.filter !== "all" || !!L.query;
    L.box.classList.toggle("filtered", filtering);
    for (const line of L.lines) {
      paint(line.el, line.text, line.level);
      line.el.classList.toggle("hide", !visible(line));
    }
    updateCount();
    if (L.auto) L.box.scrollTop = L.box.scrollHeight;
  }
  function updateCount() {
    if (!L.countEl) return;
    const total = L.lines.length;
    if (L.filter !== "all" || L.query) {
      const shown = L.lines.filter(visible).length;
      L.countEl.textContent = `${shown} / ${total} 行`;
    } else L.countEl.textContent = total ? `${total} 行` : "";
  }
  function allText() { return L.lines.map((l) => l.text).join("\n") + (L.partial ? "\n" + L.partial : ""); }

  function init({ box, countEl, filterEl, searchEl, autoBtn, copyBtn }) {
    L.box = box;
    L.countEl = countEl;
    L.box.classList.add("filtered");
    window.DFQUI.seg(filterEl, {
      value: "all",
      onChange(v) { L.filter = v; refilter(); },
    });
    searchEl.addEventListener("input", debounce(() => { L.query = searchEl.value.trim(); refilter(); }, 120));
    searchEl.addEventListener("keydown", (e) => { if (e.key === "Escape" && searchEl.value) { searchEl.value = ""; L.query = ""; refilter(); } });
    autoBtn.setAttribute("aria-pressed", "true");
    autoBtn.addEventListener("click", () => {
      L.auto = !L.auto;
      autoBtn.setAttribute("aria-pressed", L.auto ? "true" : "false");
      toast(L.auto ? "已开启自动滚动" : "已关闭自动滚动：新日志不会把视图拉到底部", "info", 2200);
      if (L.auto) L.box.scrollTop = L.box.scrollHeight;
    });
    copyBtn.addEventListener("click", () => {
      const t = allText();
      if (!t.trim()) { toast("日志是空的，没有可复制的内容", "info"); return; }
      copyText(t, `全部日志（${L.lines.length} 行）`);
    });
  }

  window.DFQLog = { init, append, clear: clearView, focusSearch: () => { const s = $("#log-search"); if (s) { s.focus(); s.select(); } } };
})();
