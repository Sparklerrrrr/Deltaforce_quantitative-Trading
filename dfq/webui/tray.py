"""系统托盘图标（pystray）：最小化到托盘后，从这里重新打开或退出。

托盘图标在自己的线程里运行（run_detached），菜单回调里直接调用 pywebview 的 show / destroy，
这两个方法本身是线程安全的。缺少 pystray / Pillow 时 available=False，程序退回“最小化到任务栏”。
"""
from __future__ import annotations

import threading
from typing import Callable

ACCENT = (120, 180, 157, 255)                    # 与界面主色一致


def _icon_image(ico_path=None):
    """托盘图标：优先用程序图标 dfq.ico；没有时画一个主色圆角方块 + “D”。"""
    from PIL import Image, ImageDraw, ImageFont
    if ico_path:
        try:
            img = Image.open(ico_path)
            img.size = (64, 64) if (64, 64) in img.info.get("sizes", ()) else img.size
            return img.convert("RGBA")
        except Exception:
            pass
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=ACCENT)
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            font = ImageFont.truetype(name, 40)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), "D", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "D", font=font, fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, title: str, on_show: Callable[[], None], on_quit: Callable[[], None], icon_path=None):
        self._on_show, self._on_quit = on_show, on_quit
        self._icon_path = icon_path
        self._title = title
        self._icon = None
        self._notified = False
        self._lock = threading.Lock()
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            self.available = True
        except ImportError:
            self.available = False

    def start(self) -> None:
        if not self.available or self._icon is not None:
            return
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("打开 DFQ", lambda icon, item: self._on_show(), default=True),  # 双击图标也是它
            pystray.MenuItem("退出", lambda icon, item: self._on_quit()),
        )
        self._icon = pystray.Icon("DFQ", _icon_image(self._icon_path), self._title, menu)
        self._icon.run_detached()

    def hidden(self) -> None:
        """窗口刚被收进托盘：第一次提示一下用户程序还在后台。"""
        with self._lock:
            if self._notified or self._icon is None:
                return
            self._notified = True
        try:
            self._icon.notify("DFQ 仍在后台运行。双击托盘里的图标可以重新打开，右键可以退出。", "已最小化到托盘")
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def notify(self, text: str, title: str = "DFQ") -> None:
        """窗口收在托盘里时弹一条系统通知（例如后台任务结束）。失败就算了。"""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(text, title)
        except Exception:
            pass
