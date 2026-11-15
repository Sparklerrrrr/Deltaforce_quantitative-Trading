"""打开 DFQ 窗口：pywebview + Windows 自带的 Edge WebView2。

页面是本地文件（dfq/webui/static），不加载任何网络资源；页面里的按钮通过
window.pywebview.api.* 调用 api.Api 的方法。

关闭窗口（点右上角 ×）时：
  - 设置是“每次询问”：页面里弹出提示，选“最小化到托盘”或“退出程序”，可勾选“记住我的选择”；
  - 设置是“最小化到托盘”：直接收进托盘；
  - 设置是“退出程序”：直接退出（如果还有操作在运行，仍会先提示）。
单实例：程序已经在运行（哪怕收在托盘里）时再打开一次，只会把原来的窗口叫出来。
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path

from .. import __version__, gui_core, layout
from . import prefs
from .api import Api, make_bridge
from .tray import Tray


def static_dir() -> Path:
    """静态文件位置：源码运行时在包里；打包成 exe 后在解压目录的同一相对位置。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "dfq" / "webui" / "static"
    return Path(__file__).resolve().parent / "static"


def icon_path() -> Path | None:
    """程序图标 dfq.ico：源码运行时在项目根目录；打包后被放在 _internal 根部（见 build_exe.bat）。"""
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "dfq.ico"
    else:
        p = Path(__file__).resolve().parents[2] / "dfq.ico"
    return p if p.is_file() else None


# ---------------------------------------------------------------- 单实例（按程序文件夹区分）
def _instance_address(base: Path) -> tuple[str, bytes]:
    h = hashlib.sha256(str(Path(base).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\dfq-{h}", h.encode()


def _wake_existing(base: Path) -> bool:
    """已经有一个在运行：让它把窗口叫出来，返回 True。"""
    if sys.platform != "win32":
        return False
    addr, key = _instance_address(base)
    try:
        with Client(addr, authkey=key) as conn:
            conn.send("show")
        return True
    except (OSError, EOFError, ValueError):
        return False


def _listen_for_wake(base: Path, on_show) -> None:
    if sys.platform != "win32":
        return
    addr, key = _instance_address(base)
    try:
        listener = Listener(addr, authkey=key)
    except OSError:
        return

    def loop():
        while True:
            try:
                with listener.accept() as conn:
                    if conn.recv() == "show":
                        on_show()
            except Exception:
                continue
    threading.Thread(target=loop, daemon=True).start()


# 浏览器组件（WebView2）的启动参数。实测（2026-11-15）：把网络服务并进主进程能少开一个进程、省约 36 MB；
# 其余常见的“省内存”参数实测没有效果，没有加。只通过本进程的环境变量传给 DFQ 自己启动的浏览器组件，
# 不改任何系统设置。用户自己设置了这个环境变量时以用户的为准。
BROWSER_ARGS = ("--disable-features=ElasticOverscroll --enable-features=NetworkServiceInProcess2 "
                "--disable-background-networking --disable-component-update --no-pings")
THEME_BG = {"light": "#F4F6F5", "dark": "#15191A"}   # 与 app.css 的 --bg 一致：缩放窗口时露出的底色不闪


def _on_ui_thread(window, fn) -> None:
    """在 WinForms 界面线程里执行 fn（pywebview 的 window.native 是 WinForms 窗体）。失败就算了，不影响使用。"""
    form = getattr(window, "native", None)
    if form is None:
        return
    try:
        from System import Action
        form.Invoke(Action(fn))
    except Exception:
        pass


def set_browser_memory(window, low: bool) -> None:
    """收进托盘时让浏览器组件“真正进入后台”，打开时恢复：
      1) 把浏览器控件也设为不可见：只隐藏外层窗口时浏览器并不知道（实测 document.hidden 仍是 false），
         会照常渲染、页面也照常轮询；控件不可见后浏览器会暂停渲染、页面停止轮询；
      2) 内存目标设为 Low（WebView2 MemoryUsageTargetLevel），让浏览器主动回收内存。"""
    def do():
        from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
        ctl = window.native.webview
        if not low:
            ctl.Visible = True
        core = ctl.CoreWebView2
        if core is not None:
            core.MemoryUsageTargetLevel = Level.Low if low else Level.Normal
        if low:
            ctl.Visible = False
    _on_ui_thread(window, do)


def set_window_background(window, theme: str) -> None:
    color = THEME_BG.get(theme)
    if not color:
        return

    def do():
        from System.Drawing import ColorTranslator
        window.native.BackColor = ColorTranslator.FromHtml(color)
    _on_ui_thread(window, do)


APP_MUTEX = "DFQ_SubdanTradingAssistant_Running"    # 与 installer/DFQ.iss 的 AppMutex 一致


def _hold_app_mutex():
    """创建一个具名互斥体并在程序运行期间持有：安装 / 升级 / 卸载程序发现它存在时会提示先退出 DFQ，
    避免文件被替换到一半。拿不到也不影响运行。"""
    try:
        from System.Threading import Mutex
        return Mutex(False, APP_MUTEX)
    except Exception:
        return None


def check_database(base: Path) -> str | None:
    """打开（不存在则初始化）数据库并做快速完整性检查。损坏时只报告，不做任何修改，避免二次破坏交易记录。"""
    try:
        from ..db import DB
        db = DB(Path(base) / "data" / "dfq.db")
        try:
            row = db.one("PRAGMA quick_check")
            ok = row is not None and str(tuple(row)[0]).lower() == "ok"
        finally:
            db.close()
        return None if ok else ("警告：数据库 data/dfq.db 完整性检查没有通过。请先复制一份 data 文件夹备份，"
                                "不要删除它；可以导出日志反馈问题。")
    except Exception as e:
        return f"警告：无法打开数据库 data/dfq.db（{type(e).__name__}）。请先备份 data 文件夹，不要删除它。"


def main(config: str | Path | None = None) -> int:
    try:
        import webview
    except ImportError:
        print("缺少界面组件 pywebview。请先运行：.venv\\Scripts\\pip install -r requirements.txt")
        return 1
    base = gui_core.default_base_dir()
    cfg_path = Path(config) if config else base / "config.yaml"
    if not cfg_path.is_absolute():
        cfg_path = base / cfg_path
    base = cfg_path.parent
    if _wake_existing(base):
        return 0
    try:
        os.chdir(base)                               # 让相对路径（data/、manual_notes/）都落在这里
    except OSError:
        pass
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", BROWSER_ARGS)
    created = layout.bootstrap(base)                 # 首次启动：建目录、从模板生成 config.yaml / .env（已有的不动）
    db_problem = check_database(base)
    app_mutex = _hold_app_mutex()                    # noqa: F841  安装 / 卸载程序靠它判断 DFQ 是否还在运行
    api = Api(cfg_path)
    if created:
        api._emit("首次启动，已创建：" + "、".join(created) + "\n")
    if db_problem:
        api._emit(db_problem + "\n")
    title = f"DFQ 子弹交易决策助手 {__version__}"
    theme = prefs.load(base).get("theme") or "light"
    # 用 file:// 直接打开本地页面，不启动 pywebview 自带的本地网页服务：
    # 那个服务在“非无痕模式”下固定用 42001 端口，两个不同文件夹里的 DFQ 同时运行时，
    # 后开的那个会连到先开的那个的服务、显示它的旧页面（2026-11-15 实测踩到）。也省掉一个每 0.5 秒醒一次的线程。
    window = webview.create_window(
        title, url=(static_dir() / "index.html").resolve().as_uri(), js_api=make_bridge(api),
        width=1280, height=820, min_size=(1000, 640), background_color=THEME_BG.get(theme, THEME_BG["light"]),
        text_select=True)
    state = {"allow_close": False}

    def show():
        window.show()                                # 先显示窗口，再让浏览器控件可见（顺序反了浏览器收不到“可见”通知）
        window.restore()
        set_browser_memory(window, False)

    def quit_app():
        state["allow_close"] = True
        window.destroy()

    ico = icon_path()
    tray = Tray(title, on_show=show, on_quit=quit_app, icon_path=ico)

    def to_tray():
        if tray.available:
            set_browser_memory(window, True)         # 先让浏览器控件进入后台（窗口还可见时它才收得到通知），再隐藏窗口
            window.hide()
            tray.hidden()
        else:
            window.minimize()                        # 没有托盘组件：退回最小化到任务栏

    def handle_choice(action: str):
        if action == "tray":
            to_tray()
        elif action == "quit":
            quit_app()
    api._close_handler = handle_choice               # 下划线开头：不暴露给页面
    api._theme_handler = lambda t: set_window_background(window, t)
    api._window = window                             # 导出配置时弹“选择文件夹”要用

    def ask_in_page():
        try:
            window.evaluate_js("window.dfqAskClose && window.dfqAskClose()")
        except Exception:
            pass

    def on_closing():
        # 在界面线程里执行：必须马上返回。False = 取消这次关闭。
        if state["allow_close"]:
            return True
        action = prefs.load(base)["close_action"]
        if action == "tray":
            threading.Thread(target=to_tray, daemon=True).start()
            return False
        if action == "quit" and not api.busy:
            return True
        threading.Thread(target=ask_in_page, daemon=True).start()   # 询问（或“退出”但还有操作在运行）
        return False
    window.events.closing += on_closing

    def on_started():
        tray.start()
        _listen_for_wake(base, show)
    # 浏览器组件（WebView2）的数据默认会写到 C 盘的临时目录 / AppData；这里固定放进程序自己的 data\webview，
    # 保证程序产生的所有文件都在安装目录里。不用无痕模式，界面的深色/浅色等偏好才能记住。
    storage = base / "data" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(on_started, gui="edgechromium", debug=bool(os.environ.get("DFQ_UI_DEBUG")),
                      private_mode=False, storage_path=str(storage), icon=str(ico) if ico else None)
    finally:
        tray.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
