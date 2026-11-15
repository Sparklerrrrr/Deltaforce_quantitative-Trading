// 在页面渲染之前先定好主题，深色模式下打开时不会先闪一下浅色
  (function () {
    var t = null;
    try { t = localStorage.getItem("dfq-theme"); } catch (e) { /* 忽略 */ }
    if (t !== "dark" && t !== "light") t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.dataset.theme = t;
  })();
