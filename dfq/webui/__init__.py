"""DFQ 桌面界面（网页技术）。

结构参考 MaaEnd + MXU 的做法：界面只负责显示和交互，业务全部走现有的 dfq 命令。

  api.py      后端：界面能调用的全部操作。纯 Python、不依赖窗口，可以直接单元测试。
              会花钱 / 改账本的操作，确认在这里强制执行（不是靠界面自觉弹窗）。
  tasks.py    首页任务清单（声明式：名称、说明、标签、是否花钱、执行哪条命令）。
  app.py      打开窗口：pywebview + Windows 自带的 Edge WebView2。
  static/     index.html / app.css / app.js，不依赖任何网络资源。
"""
