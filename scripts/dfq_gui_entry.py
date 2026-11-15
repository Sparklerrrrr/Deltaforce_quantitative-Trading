"""PyInstaller 打包入口（DFQ.exe）。

  双击 / 不带参数   → 打开图形界面
  带参数            → 当命令行用，例如计划任务里的  DFQ.exe run  /  DFQ.exe mail-poll
                      （打包的是无控制台窗口的程序，所以输出追加写到 data\\logs\\task-日期.log）
"""
import sys
from datetime import datetime
from pathlib import Path


def _cli(argv: list[str]) -> int:
    from dfq import cli, gui_core, layout
    base = gui_core.default_base_dir()
    layout.bootstrap(base, create_config=False)       # 命令行（计划任务）只补目录，不生成配置文件
    if sys.stdout is None or sys.stderr is None:      # 无控制台：输出写进程序自己的日志文件夹
        log_dir = base / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / f"task-{datetime.now():%Y%m%d}.log", "a", encoding="utf-8")
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  DFQ.exe {' '.join(argv)} =====\n")
        sys.stdout = sys.stderr = fh
    if "--config" not in argv:
        argv = [argv[0], "--config", str(base / "config.yaml"), *argv[1:]]
    return cli.main(argv)


def main() -> int:
    if len(sys.argv) > 1:
        return _cli(sys.argv[1:])
    from dfq.webui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
