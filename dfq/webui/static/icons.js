// DFQ 图标表：线条风格的内联 SVG（不依赖网络、不加载任何文件）。
// 注意：这里故意不写 xmlns 属性——用 innerHTML 插进 HTML 文档时浏览器会自动放进 SVG 命名空间，
// 而且测试要求静态文件里不出现任何网址字样。图标只由代码写死，从不拼接后端返回的文字。
"use strict";

(function () {
  const P = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" `
    + `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${d}</svg>`;

  const ICONS = {
    // 任务清单（dfq/webui/tasks.py 里的 icon 名）
    play: P('<path d="M8 5.5v13a.8.8 0 0 0 1.2.7l10.2-6.5a.8.8 0 0 0 0-1.4L9.2 4.8A.8.8 0 0 0 8 5.5z"/>'),
    rules: P('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>'),
    spark: P('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>'
      + '<path d="M19 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>'),
    gauge: P('<path d="M4 15a8 8 0 1 1 16 0"/><path d="M12 15l4-5"/><circle cx="12" cy="15" r="1.2"/>'),
    probe: P('<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>'),
    mail: P('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.5 7.5l8.5 6 8.5-6"/>'),
    chart: P('<path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 15l4-4 3 3 5-6"/>'),
    refresh: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>'),
    check: P('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
    // 顶部标签
    home: P('<path d="M4 10.5L12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z"/>'),
    plan: P('<rect x="5" y="3.5" width="14" height="17" rx="2"/><path d="M9 3.5V5h6V3.5"/><path d="M8.5 10h7M8.5 13.5h7M8.5 17h4"/>'),
    ledger: P('<path d="M5 4.5h11a3 3 0 0 1 3 3V19.5H8a3 3 0 0 1-3-3z"/><path d="M5 16.5a3 3 0 0 1 3-3h11"/><path d="M9 8h6"/>'),
    review: P('<path d="M4 19h16"/><rect x="5.5" y="11" width="3" height="5.5" rx=".8"/>'
      + '<rect x="10.5" y="7" width="3" height="9.5" rx=".8"/><rect x="15.5" y="4" width="3" height="12.5" rx=".8"/>'),
    intel: P('<path d="M5 4h10l4 4v12H5z"/><path d="M15 4v4h4"/><path d="M8.5 12h7M8.5 15.5h7"/>'),
    settings: P('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 '
      + '1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 '
      + '.3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 '
      + '1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 '
      + '1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>'),
    // 通用
    sun: P('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
      + 'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    moon: P('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
    search: P('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>'),
    sideOpen: P('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/><path d="M8 10l2 2-2 2"/>'),
    sideClose: P('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/><path d="M10 10l-2 2 2 2"/>'),
    export: P('<path d="M12 3v12"/><path d="M7 8l5-5 5 5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>'),
    import: P('<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>'),
    eraser: P('<path d="M16 4l4 4-10 10H6l-2-2z"/><path d="M13 7l4 4"/><path d="M10 20h10"/>'),
    copy: P('<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>'),
    close: P('<path d="M6 6l12 12M18 6L6 18"/>'),
    info: P('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.5h.01"/>'),
    ok: P('<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.8 2.8L16.5 9.5"/>'),
    warn: P('<path d="M12 3.5L2.8 19.5h18.4z"/><path d="M12 10v4.5"/><path d="M12 17.2h.01"/>'),
    error: P('<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>'),
    chevron: P('<path d="M9 6l6 6-6 6"/>'),
    chevronDown: P('<path d="M6 9l6 6 6-6"/>'),
    external: P('<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>'),
    folder: P('<path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'),
    palette: P('<path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.2 0 1.8-.8 1.6-1.8-.3-1.3.6-2.2 1.9-2.2h1.6A3.4 3.4 0 0 0 20.5 13 '
      + '8.5 8.5 0 0 0 12 3.5z"/><circle cx="7.8" cy="11" r="1"/><circle cx="10.5" cy="7.5" r="1"/><circle cx="15" cy="8" r="1"/>'),
    sliders: P('<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'),
    chip: P('<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 3v3.5M14.5 3v3.5M9.5 17.5V21M14.5 17.5V21'
      + 'M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21"/><rect x="9.5" y="9.5" width="5" height="5" rx=".8"/>'),
    shield: P('<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M9 12l2.2 2.2L15.5 10"/>'),
    key: P('<circle cx="8" cy="15" r="4"/><path d="M11 12l8.5-8.5"/><path d="M16.5 6.5l2.5 2.5M14 9l2 2"/>'),
    database: P('<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6"/>'
      + '<path d="M4.5 12v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-6"/>'),
    clock: P('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    about: P('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.5h.01"/>'),
    bell: P('<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>'),
    link: P('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
    trash: P('<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5l1-12.5"/>'),
    restore: P('<path d="M4 12a8 8 0 1 0 2.3-5.7"/><path d="M4 4v4.5h4.5"/><path d="M12 8v4l2.5 1.5"/>'),
    plus: P('<path d="M12 5v14M5 12h14"/>'),
    terminal: P('<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5M12.5 15h4.5"/>'),
    filter: P('<path d="M4 5h16l-6 7.5V19l-4-2v-4.5z"/>'),
    arrowDown: P('<path d="M12 4v15"/><path d="M6 13l6 6 6-6"/>'),
    keyboard: P('<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 14h9"/>'),
    update: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/><path d="M12 8v4"/>'),
    github: P('<path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 '
      + '4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12 12 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5'
      + 'c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>'),
    bug: P('<rect x="7.5" y="7" width="9" height="12" rx="4.5"/><path d="M12 11v8M9 4.5l1.5 2.5M15 4.5L13.5 7'
      + 'M4 13h3.5M16.5 13H20M5 8.5l2.8 1.5M19 8.5l-2.8 1.5M5 18l2.8-1.5M19 18l-2.8-1.5"/>'),
    book: P('<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 1 6.5 18H20v3H6.5"/>'),
    wallet: P('<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18v3"/><rect x="4" y="8" width="16" height="11" rx="2.5"/><circle cx="16" cy="13.5" r="1.2"/>'),
    box: P('<path d="M3.5 7.5L12 3l8.5 4.5v9L12 21l-8.5-4.5z"/><path d="M3.5 7.5L12 12l8.5-4.5M12 12v9"/>'),
    pulse: P('<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>'),
    eye: P('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>'),
    globe: P('<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z"/>'),
    sparkles: P('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>'),
    task: P('<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12.3l2.8 2.8L16.5 9.5"/>'),
  };

  // DFQ 标志：圆角方块 + 三根上升的柱子（关于页、品牌区共用；颜色跟随主题色）
  const LOGO = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false">'
    + '<rect x="2" y="2" width="44" height="44" rx="13" fill="var(--accent)"/>'
    + '<rect x="2" y="2" width="44" height="44" rx="13" fill="url(#dfq-logo-g)" opacity=".35"/>'
    + '<defs><linearGradient id="dfq-logo-g" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>'
    + '<rect x="12" y="25" width="6" height="11" rx="2" fill="var(--on-accent)"/>'
    + '<rect x="21" y="18" width="6" height="18" rx="2" fill="var(--on-accent)"/>'
    + '<rect x="30" y="11" width="6" height="25" rx="2" fill="var(--on-accent)"/>'
    + '</svg>';

  window.DFQ_ICONS = ICONS;
  window.DFQ_LOGO = LOGO;
})();
