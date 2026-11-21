"""PyInstaller 打包入口（DFQ.exe）。

  双击 / 不带参数   → 打开图形界面
  带参数            → 当命令行用，例如计划任务里的  DFQ.exe run  /  DFQ.exe mail-poll
                      （打包的是无控制台窗口的程序，所以输出追加写到 data\\logs\\task-日期.log）

数据隔离：命令行模式下，配置、.env 和数据一律以 DFQ.exe 所在的文件夹为准，
不看“当前目录”（计划任务的当前目录可能是 C:\\Windows\\System32，也可能是别的程序文件夹）。
"""
import os
import sys
from datetime import datetime

_GLOBAL_WITH_VALUE = ("--now",)                          # dfq/cli.py 里除 --config 外带值的全局参数


def with_config(argv: list[str], config: str) -> list[str]:
    """用户没指定 --config 时，把安装文件夹里的 config.yaml 塞进参数。

    必须放在子命令名之后：argparse 的子命令解析器带着同名参数的默认值（config.yaml），
    放在子命令前面会被这个默认值盖掉，结果又回到“当前目录的 config.yaml”。
    """
    if any(a == "--config" or a.startswith("--config=") for a in argv):
        return list(argv)
    skip = False
    for i, a in enumerate(argv):
        if skip:                                         # 上一个是带值的全局参数（--now 时间），这个是它的值
            skip = False
            continue
        if a in _GLOBAL_WITH_VALUE:
            skip = True
            continue
        if not a.startswith("-"):                        # 第一个不是参数、也不是参数值的，就是子命令名
            return [*argv[:i + 1], "--config", config, *argv[i + 1:]]
    return [*argv, "--config", config]                   # 只有开关、没有子命令（例如 --help）


def _cli(argv: list[str]) -> int:
    from dfq import cli, gui_core, layout
    base = gui_core.default_base_dir()
    os.chdir(base)                                    # 相对路径（data/、manual_notes/）都落在安装文件夹，不落在当前目录
    layout.bootstrap(base, create_config=False)       # 命令行（计划任务）只补目录，不生成配置文件
    if sys.stdout is None or sys.stderr is None:      # 无控制台：输出写进程序自己的日志文件夹
        log_dir = base / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / f"task-{datetime.now():%Y%m%d}.log", "a", encoding="utf-8")
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  DFQ.exe {' '.join(argv)} =====\n")
        sys.stdout = sys.stderr = fh
    return cli.main(with_config(argv, str(base / "config.yaml")))


def main() -> int:
    if len(sys.argv) > 1:
        return _cli(sys.argv[1:])
    from dfq.webui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
