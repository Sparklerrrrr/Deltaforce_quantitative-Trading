"""测试公共设施。

铁律：测试绝不允许消耗真实接口次数、绝不联网。
这里把 urllib 的出口整个封死，并清空所有 DFQ_* 环境变量；
需要“网络”的测试一律注入假的 transport。
"""
import copy
import logging
import os
import urllib.request

import pytest

from dfq import timeutil
from dfq.config import DEFAULTS, Config
from dfq.db import DB


@pytest.fixture(autouse=True)
def _no_network_no_secrets(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("测试试图访问真实网络！请注入假的 transport")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    for k in list(os.environ):
        if k.startswith("DFQ_"):
            monkeypatch.delenv(k, raising=False)
    timeutil.set_now("2026-11-13 21:30:00")
    yield
    timeutil.set_now(None)
    lg = logging.getLogger("dfq")              # 摘掉测试里挂上的日志文件，避免写到已删除的临时目录
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)
    lg.propagate = True


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(DEFAULTS)
    data["general"]["data_dir"] = str(tmp_path / "data")
    return Config(data, tmp_path / "config.yaml")


@pytest.fixture
def db(cfg):
    d = DB(cfg.data_dir / "dfq.db")
    yield d
    d.close()
