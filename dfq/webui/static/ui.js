// DFQ 界面通用组件：DOM 小工具、轻提示、弹窗、分段选择器、开关、滑动指示、数字滚动、高亮闪烁、骨架屏、复制。
// 规则：后端返回的任何文字都用 textContent / DOM API 写入，绝不拼进 innerHTML（计划理由、资讯、日志都可能含不可信文字）；
// innerHTML 只用于 icons.js 里写死的 SVG。
"use strict";

(function () {
  const ICONS = window.DFQ_ICONS || {};
  const root = document.documentElement;

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // h("div.card.grow", {title: "x", onclick: fn, dataset: {a: 1}}, [子元素或文字...])
  function h(tag, props, children) {
    const [name, ...classes] = tag.split(".");
    const el = document.createElement(name || "div");
    if (classes.length) el.className = classes.join(" ");
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "text") el.textContent = String(v);
        else if (k === "class") el.className += (el.className ? " " : "") + v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (k === "icon") el.appendChild(icon(v));
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, String(v));
      }
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    if (children === undefined || children === null || children === false) return el;
    if (!Array.isArray(children)) children = [children];
    for (const c of children) {
      if (c === undefined || c === null || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  // 图标：只用写死的 SVG
  function icon(name, cls) {
    const s = document.createElement("span");
    s.className = cls || "ico";
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = ICONS[name] || "";
    return s;
  }
  function fillIcons(scope = document) {
    $$("[data-icon]", scope).forEach((el) => {
      if (!el.firstChild) el.innerHTML = ICONS[el.dataset.icon] || "";
    });
  }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); return el; }

  // ---------------------------------------------------------------- 格式化
  const isNum = (v) => typeof v === "number" && isFinite(v);
  function fmtInt(v) { return isNum(v) ? Math.round(v).toLocaleString("zh-CN") : "—"; }
  function fmtNum(v, digits = 0) {
    return isNum(v) ? v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
  }
  function fmtMoney(v) {
    if (!isNum(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + " 亿";
    if (a >= 1e4) return (v / 1e4).toFixed(a >= 1e6 ? 0 : 1) + " 万";
    return Math.round(v).toLocaleString("zh-CN");
  }
  function fmtSigned(v, f = fmtMoney) { return isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + f(Math.abs(v)) : "—"; }
  function fmtPct(v, digits = 1, signed = false) {
    if (!isNum(v)) return "—";
    const s = (Math.abs(v) * 100).toFixed(digits) + "%";
    return signed ? (v > 0 ? "+" : v < 0 ? "−" : "") + s : (v < 0 ? "−" : "") + s;
  }
  function fmtBytes(b) {
    if (!isNum(b)) return "—";
    if (b < 1024) return b + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { b /= 1024; i++; } while (b >= 1024 && i < u.length - 1);
    return b.toFixed(b >= 100 ? 0 : 1) + " " + u[i];
  }
  // "2026-11-17 23:45:00" → "11-17 23:45"
  function shortTime(s) {
    if (!s) return "—";
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(s));
    return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(s);
  }
  function signClass(v) { return isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : ""; }

  // ---------------------------------------------------------------- 动效偏好
  const motion = () => root.dataset.motion || "full";
  const animOn = () => motion() === "full";

  // ---------------------------------------------------------------- 轻提示：右下滑入，图标 + 倒计时细线，悬停暂停，最多 4 条
  const TOAST_ICON = { ok: "ok", error: "error", warn: "warn", info: "info", "": "info" };
  function toast(text, kind = "ok", ms) {
    const box = $("#toasts");
    if (!box) return;
    if (kind === "") kind = "ok";
    const life = ms || (kind === "error" ? 6000 : 3200);
    const bar = h("i.t-bar", { style: { animationDuration: life + "ms" } });
    const x = h("button.t-x", { type: "button", "aria-label": "关闭提示", title: "关闭" }, icon("close"));
    const el = h("div.toast." + kind, { role: kind === "error" ? "alert" : "status" },
      [icon(TOAST_ICON[kind] || "info", "t-ico"), h("div.t-text", { text }), x, bar]);
    box.appendChild(el);
    while (box.children.length > 4) box.removeChild(box.firstElementChild);
    // 计时：悬停时暂停（与细线的 animation-play-state 同步）
    let left = life, started = Date.now(), timer = null, gone = false;
    const dismiss = () => {
      if (gone) return;
      gone = true;
      clearTimeout(timer);
      if (!animOn()) { el.remove(); return; }
      el.classList.add("leaving");
      el.addEventListener("animationend", () => el.remove(), { once: true });
      setTimeout(() => el.remove(), 400);                     // 动画被关掉时的保底
    };
    const run = () => { started = Date.now(); timer = setTimeout(dismiss, left); };
    el.addEventListener("mouseenter", () => { clearTimeout(timer); left -= Date.now() - started; });
    el.addEventListener("mouseleave", () => { if (!gone) run(); });
    x.addEventListener("click", dismiss);
    run();
  }

  // ---------------------------------------------------------------- 弹窗
  // modal({title, text, input, okText, cancelText, danger, single, icon, body, wide}) → Promise
  //   输入型返回输入的文字（取消返回 null）；其他返回 true / null。
  //   打开时焦点进入；Tab 在弹窗内循环；Enter 确认、Esc 取消；关闭后焦点回到触发按钮。输入框用 password 类型，关闭时清空。
  const openModals = [];
  function focusables(el) {
    return $$("button, input, select, textarea, [tabindex]:not([tabindex='-1']), a[href]", el)
      .filter((x) => !x.disabled && x.offsetParent !== null);
  }
  function trapKeys(e, panel) {
    if (e.key !== "Tab") return;
    const f = focusables(panel);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!panel.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }
  // 低层：打开一个浮层；返回 {mask, panel, close(result)}。onKey 可拦截按键。
  function openLayer({ panel, maskClass = "", onClose, onKey, closeOnMask = true, label }) {
    const mask = h("div.modal-mask" + (maskClass ? "." + maskClass : ""));
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    if (label) panel.setAttribute("aria-label", label);
    mask.appendChild(panel);
    const opener = document.activeElement;
    const layer = $("#layer") || document.body;
    layer.appendChild(mask);
    let closed = false;
    const ctx = {
      mask, panel,
      close(result) {
        if (closed) return;
        closed = true;
        const i = openModals.indexOf(ctx);
        if (i >= 0) openModals.splice(i, 1);
        if (onClose) onClose(result);
        if (animOn()) {
          mask.classList.add("closing");
          setTimeout(() => mask.remove(), 170);
        } else mask.remove();
        if (opener && typeof opener.focus === "function" && document.contains(opener)) {
          try { opener.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
        }
      },
      key(e) {
        if (onKey && onKey(e) === true) return;
        if (e.key === "Escape") { e.preventDefault(); ctx.close(null); return; }
        trapKeys(e, panel);
      },
    };
    if (closeOnMask) mask.addEventListener("mousedown", (e) => { if (e.target === mask) ctx.close(null); });
    openModals.push(ctx);
    return ctx;
  }
  document.addEventListener("keydown", (e) => {
    const top = openModals[openModals.length - 1];
    if (top) { top.key(e); e.stopPropagation(); }
  }, true);
  const modalOpen = () => openModals.length > 0;

  function modal(opts) {
    const { title = "", text = "", input = false, okText = "确定", cancelText = "取消", danger = false,
      single = false, body = null, wide = false } = opts || {};
    const ico = opts && opts.icon !== undefined ? opts.icon : (danger ? "warn" : input ? "key" : "info");
    return new Promise((resolve) => {
      const inp = input ? h("input.modal-input", { type: "password", autocomplete: "off", spellcheck: "false",
        "aria-label": title }) : null;
      const ok = h("button.btn" + (danger ? ".danger.solid" : ".primary"), { type: "button", text: okText });
      const cancel = single ? null : h("button.btn", { type: "button", text: cancelText });
      const titleId = "m-title-" + Math.random().toString(36).slice(2);
      const panel = h("div.modal" + (wide ? ".wide" : ""), { "aria-labelledby": titleId }, [
        h("div.modal-head", null, [
          ico ? h("div.m-ico" + (danger ? ".danger" : ""), null, icon(ico)) : null,
          h("h3", { id: titleId, text: title }),
        ]),
        text ? h("div.modal-text", { text }) : null,
        body,
        inp,
        h("div.modal-actions", null, [cancel, ok]),
      ]);
      let value = null;
      const ctx = openLayer({
        panel, label: title,
        onClose(r) {
          if (inp) inp.value = "";                              // 密钥不在页面上多留一刻
          resolve(r);
        },
        onKey(e) {
          if (e.key === "Enter" && !e.isComposing && !(e.target && e.target.tagName === "TEXTAREA")
              && !(e.target && e.target.tagName === "BUTTON" && e.target !== ok)) {
            e.preventDefault();
            confirm();
            return true;
          }
          return false;
        },
      });
      function confirm() {
        value = input ? inp.value : true;
        ctx.close(value);
      }
      ok.addEventListener("click", confirm);
      if (cancel) cancel.addEventListener("click", () => ctx.close(null));
      setTimeout(() => (inp || ok).focus(), 30);
    });
  }

  // ---------------------------------------------------------------- 分段选择器（滑块会滑动）
  // seg(container, {value, onChange(value, prev) → 可返回 Promise<false> 表示失败要回滚, items: [[value, label], ...]})
  const segRegistry = new WeakMap();
  const ro = typeof ResizeObserver === "function"
    ? new ResizeObserver((entries) => { for (const en of entries) { const s = segRegistry.get(en.target); if (s) s.place(false); } })
    : null;
  function seg(el, opts = {}) {
    if (opts.items) {
      clear(el);
      for (const [v, label] of opts.items) el.appendChild(h("button.seg", { type: "button", "data-value": v, text: label }));
    }
    el.setAttribute("role", "radiogroup");
    let thumb = $(".seg-thumb", el);
    if (!thumb) { thumb = h("span.seg-thumb.no-anim", { "aria-hidden": "true" }); el.insertBefore(thumb, el.firstChild); }
    const buttons = () => $$(".seg", el);
    let value = opts.value !== undefined ? String(opts.value) : null;
    const api = {
      el,
      get value() { return value; },
      set(v, animate = true) {
        value = v === null || v === undefined ? null : String(v);
        for (const b of buttons()) {
          const on = b.dataset.value === value;
          b.setAttribute("role", "radio");
          b.setAttribute("aria-checked", on ? "true" : "false");
          b.tabIndex = on || (value === null && b === buttons()[0]) ? 0 : -1;
        }
        api.place(animate);
      },
      place(animate) {
        const b = buttons().find((x) => x.dataset.value === value);
        if (!b || !el.offsetWidth) { thumb.style.opacity = b ? "" : "0"; return; }
        thumb.classList.toggle("no-anim", !animate);
        thumb.style.opacity = "1";
        thumb.style.width = b.offsetWidth + "px";
        thumb.style.transform = `translateX(${b.offsetLeft}px)`;
        if (!animate) requestAnimationFrame(() => thumb.classList.remove("no-anim"));
      },
      disable(on) { buttons().forEach((b) => { b.disabled = !!on; }); },
    };
    el.addEventListener("click", async (e) => {
      const b = e.target.closest(".seg");
      if (!b || b.disabled || !el.contains(b)) return;
      const v = b.dataset.value, prev = value;
      if (v === prev && !opts.allowSame) return;
      api.set(v, true);
      if (opts.onChange) {
        const r = await opts.onChange(v, prev);
        if (r === false) api.set(prev, true);                       // 失败：滑回原处
      }
    });
    el.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
      const bs = buttons().filter((b) => !b.disabled);
      const i = bs.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      const n = bs[(i + (e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1) + bs.length) % bs.length];
      n.focus();
      n.click();
    });
    segRegistry.set(el, api);
    if (ro) ro.observe(el);
    api.set(value, false);
    return api;
  }
  function segOf(el) { return segRegistry.get(el); }

  // ---------------------------------------------------------------- 开关
  function toggle(opts = {}) {
    const el = h("button.switch", { type: "button", role: "switch", "aria-checked": opts.value ? "true" : "false",
      "aria-label": opts.label || "", title: opts.label || "" });
    const api = {
      el,
      get value() { return el.getAttribute("aria-checked") === "true"; },
      set(v) { el.setAttribute("aria-checked", v ? "true" : "false"); },
    };
    el.addEventListener("click", async () => {
      if (el.disabled) return;
      const v = !api.value;
      api.set(v);
      if (opts.onChange) {
        const r = await opts.onChange(v);
        if (r === false) api.set(!v);
      }
    });
    return api;
  }

  // ---------------------------------------------------------------- 滑动指示（顶部标签、设置导航）
  // horizontal：移动 + 改宽度；vertical：只移动
  function placeIndicator(ind, target, { vertical = false, animate = true } = {}) {
    if (!ind || !target) return;
    ind.classList.toggle("no-anim", !animate);
    if (vertical) ind.style.transform = `translateY(${target.offsetTop}px)`;
    else { ind.style.width = target.offsetWidth - 24 + "px"; ind.style.transform = `translateX(${target.offsetLeft + 12}px)`; }
    if (!animate) requestAnimationFrame(() => requestAnimationFrame(() => ind.classList.remove("no-anim")));
  }

  // ---------------------------------------------------------------- 数字滚动与高亮闪烁
  function flash(el) {
    if (!el || motion() === "off") return;
    el.classList.remove("flash");
    void el.offsetWidth;                                         // 重新触发动画
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 650);
  }
  // countUp(el, 新值, 格式化函数)：400ms 数字滚动；第一次赋值不滚动、不闪烁；值变了闪一下
  function countUp(el, to, format = fmtInt) {
    if (!el) return;
    const from = el._dfqValue;
    el._dfqValue = to;
    if (!isNum(to)) { el.textContent = format(to); return; }
    if (!isNum(from) || from === to || !animOn()) {
      el.textContent = format(to);
      if (isNum(from) && from !== to) flash(el);
      return;
    }
    const t0 = performance.now(), dur = 400;
    cancelAnimationFrame(el._dfqRaf);
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      el.textContent = format(k >= 1 ? to : from + (to - from) * e);
      if (k < 1) el._dfqRaf = requestAnimationFrame(step);
    };
    el._dfqRaf = requestAnimationFrame(step);
    flash(el);
  }
  // 文本变了才写，并闪一下
  function setText(el, text, flashOnChange = true) {
    if (!el) return;
    text = text === null || text === undefined ? "" : String(text);
    if (el.textContent === text) return;
    const had = el.dataset.set === "1";
    el.textContent = text;
    el.dataset.set = "1";
    if (had && flashOnChange) flash(el);
  }

  // ---------------------------------------------------------------- 骨架屏
  function skeleton(kind) {
    if (kind === "metrics") {
      return [0, 1, 2, 3].map(() => h("div.card.metric", null, [
        h("div.skel.h12.w45"), h("div.skel.h22.w60"), h("div.skel.h8.w80"),
      ]));
    }
    if (kind === "table") {
      return h("div.skel-stack", null, [h("div.skel.h22"), h("div.skel.h22.w80"), h("div.skel.h22"), h("div.skel.h22.w60")]);
    }
    return h("div.skel-stack", null, [
      h("div.card", null, h("div.skel-stack", null, [h("div.skel.h22.w60"), h("div.skel.h12.w45"), h("div.skel.h40")])),
      h("div.card", null, h("div.skel-stack", null, [h("div.skel.h12.w30"), h("div.skel.h22"), h("div.skel.h22"), h("div.skel.h22.w80")])),
      h("div.card", null, h("div.skel-stack", null, [h("div.skel.h12.w30"), h("div.skel.h120")])),
    ]);
  }
  function emptyState(iconName, title, sub, action) {
    return h("div.empty", null, [
      h("div.e-ico", null, icon(iconName)), h("div.e-title", { text: title }), sub ? h("div.e-sub", { text: sub }) : null, action || null,
    ]);
  }

  // ---------------------------------------------------------------- 复制
  async function copyText(text, what) {
    text = String(text);
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); ok = true; }
    } catch (e) { ok = false; }
    if (!ok) {                                                     // 剪贴板接口不可用时的老办法
      const ta = h("textarea", { style: { position: "fixed", left: "-9999px", top: "0", opacity: "0" } });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      ta.remove();
    }
    if (ok) toast(what ? `已复制${what}` : `已复制：${text.length > 40 ? text.slice(0, 40) + "…" : text}`, "ok", 2000);
    else toast("复制失败：请手动选中文字后按 Ctrl+C", "error");
    return ok;
  }

  // 给一组元素写入入场顺序（卡片依次入场）
  function stagger(container) {
    if (!container) return;
    let i = 0;
    for (const c of container.children) {
      if (c.classList.contains("hidden")) continue;
      c.classList.add("stagger");
      c.style.setProperty("--i", String(i++));
    }
  }

  // 防抖
  function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  window.DFQUI = {
    $, $$, h, append, icon, fillIcons, clear,
    isNum, fmtInt, fmtNum, fmtMoney, fmtSigned, fmtPct, fmtBytes, shortTime, signClass,
    motion, animOn, toast, modal, openLayer, modalOpen, seg, segOf, toggle, placeIndicator,
    flash, countUp, setText, skeleton, emptyState, copyText, stagger, debounce,
  };
})();
