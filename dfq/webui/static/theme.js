// 在页面渲染之前先定好外观（主题、主题色、密度、动效、氛围背景），打开时不会先闪一下浅色或默认色。
// 偏好以后端 prefs（data/ui-settings.json）为准；这里读的 localStorage 只是“首屏缓存”，
// 页面拿到后端的 prefs 之后会再校正一次并回写缓存（见 app.js 的 applyPrefs）。
"use strict";

(function () {
  var root = document.documentElement;
  var DEFAULT_ACCENT = "#78b49d";

  function cached(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }   // 存储被禁用时照样能打开
  }
  function media(q) {
    try { return window.matchMedia(q).matches; } catch (e) { return false; }
  }

  // "" / "system" = 跟随系统
  function resolveTheme(pref) {
    if (pref === "dark" || pref === "light") return pref;
    return media("(prefers-color-scheme: dark)") ? "dark" : "light";
  }
  // "auto" = 跟随系统的“减少动画”设置
  function resolveMotion(pref) {
    if (pref === "full" || pref === "reduced" || pref === "off") return pref;
    return media("(prefers-reduced-motion: reduce)") ? "reduced" : "full";
  }
  function validHex(v) {
    return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v);
  }
  // 主色按钮上的文字颜色：主题色很亮（黄、青柠一类）时用深色字，否则白字。
  // 阈值偏向白字：默认的薄荷绿保持现在用户认可的“绿底白字”观感。
  function onAccent(hex) {
    if (!validHex(hex)) return "#ffffff";
    var n = parseInt(hex.slice(1), 16);
    var ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (c) {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    var lum = 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
    return lum > 0.4 ? "#10201a" : "#ffffff";
  }
  function applyAccent(hex) {
    if (!validHex(hex)) hex = DEFAULT_ACCENT;
    hex = hex.toLowerCase();
    root.style.setProperty("--accent", hex);
    root.style.setProperty("--on-accent", onAccent(hex));
    return hex;
  }

  var theme = cached("dfq-theme");
  root.dataset.themePref = theme === "light" || theme === "dark" ? theme : "";
  root.dataset.theme = resolveTheme(theme);
  applyAccent(cached("dfq-accent"));
  var density = cached("dfq-density");
  root.dataset.density = density === "compact" ? "compact" : "comfortable";
  var motion = cached("dfq-motion");
  root.dataset.motionPref = motion || "auto";
  root.dataset.motion = resolveMotion(motion);
  root.dataset.glow = cached("dfq-glow") === "off" ? "off" : "on";
  root.dataset.gpu = cached("dfq-gpu") === "off" ? "off" : "on";

  window.DFQTheme = {
    DEFAULT_ACCENT: DEFAULT_ACCENT,
    resolveTheme: resolveTheme,
    resolveMotion: resolveMotion,
    validHex: validHex,
    onAccent: onAccent,
    applyAccent: applyAccent,
  };
})();
