# -*- coding: utf-8 -*-
"""项目自己的临时目录：**不放 C 盘**。

系统 temp 在 C 盘（`%LOCALAPPDATA%\\Temp`），识谱/转谱一次会扔好几 MB 进去，
跑一晚上能攒出几百 MB 的历史残留（2026-10-24 实测 547 个目录 / 351 MB，全是
`tempfile.mkdtemp()` 留下的，没人清）。这里统一收到 `sky15/out/tmp/`，并在每次
新建时顺手清掉 3 天前的，永远不往 C 盘长。

用法：
    from sky15.tmp import tmpdir
    d = tmpdir("omr")        # → out/tmp/omr_ab12cd34/
"""
import os
import shutil
import tempfile
import time
import pathlib

OUT = pathlib.Path(__file__).resolve().parent / "out"
BASE = pathlib.Path(os.environ.get("SKY15_TMP") or (OUT / "tmp"))
KEEP_SECONDS = 3 * 24 * 3600


def _prune(base):
    now = time.time()
    try:
        for d in base.iterdir():
            if not d.is_dir():
                continue
            try:
                if now - d.stat().st_mtime > KEEP_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def tmpdir(prefix="sky15_"):
    """在 E 盘的项目目录里建临时目录；顺带清理过期残留。"""
    BASE.mkdir(parents=True, exist_ok=True)
    if not getattr(tmpdir, "_pruned", False):
        _prune(BASE)
        tmpdir._pruned = True
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix if prefix.endswith("_") else prefix + "_",
                                         dir=str(BASE)))


def tmpfile(prefix="sky15_", name="a.tmp"):
    d = tmpdir(prefix)
    return d / name
