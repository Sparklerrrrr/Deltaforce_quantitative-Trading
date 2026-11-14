"""PyInstaller 打包入口：生成的 DFQ.exe 双击后打开图形界面。"""
import sys

from dfq.gui import main

if __name__ == "__main__":
    sys.exit(main())
