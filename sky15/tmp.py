# -*- coding: utf-8 -*-
"""项目自己的临时目录：**不放 C 盘**。

系统 temp 在 C 盘（`%LOCALAPPDATA%\\Temp`），转谱/出图一次会扔好几 MB 进去，
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
DEFAULT_BASE = OUT / "tmp"
KEEP_SECONDS = 3 * 24 * 3600

# 允许当临时根用的目录名（小写比较，含子串即可）
_TMPISH = ("tmp", "temp", "cache", "scratch")


def _resolve_base(raw):
    """把 `SKY15_TMP` 解析成一个**敢往里 rmtree 的**目录。

    为什么要校验：`_prune()` 会 `shutil.rmtree` 掉 3 天前的每一个子目录，
    而它每次 `tmpdir()` 都会跑一次。所以环境变量其实是个直通危险的口子 ——
    `set SKY15_TMP=E:\\ai\\workspace` 就会把工作区里 3 天前建的目录全删掉
    （实测风险项由审计 D 报出）。这里改成白名单式判断：像临时目录才认，
    否则打印一行说明并退回 `out/tmp`。
    """
    if not raw:
        return DEFAULT_BASE
    try:
        p = pathlib.Path(raw).expanduser()
    except (OSError, ValueError):
        return DEFAULT_BASE
    if not p.is_absolute():
        print(f"[sky15.tmp] SKY15_TMP={raw!r} 不是绝对路径，改用 {DEFAULT_BASE}")
        return DEFAULT_BASE
    try:
        p = p.resolve()
    except OSError:
        return DEFAULT_BASE

    home = pathlib.Path.home()
    cwd = pathlib.Path.cwd()

    def _covers(anc, child):
        """anc 是不是 child 本身或它的上级（= 往 anc 里 rmtree 会伤到 child）。"""
        try:
            return anc == child or child.is_relative_to(anc)
        except (OSError, ValueError):
            return False

    if len(p.parts) <= 1:                       # E:\ 这种盘符根
        print(f"[sky15.tmp] SKY15_TMP 指到盘符根了（{p}），改用 {DEFAULT_BASE}")
        return DEFAULT_BASE
    if _covers(p, home):
        print(f"[sky15.tmp] SKY15_TMP 是用户主目录或它的上级（{p}），改用 {DEFAULT_BASE}")
        return DEFAULT_BASE
    for other, name in ((cwd, "当前目录"),
                        (pathlib.Path(__file__).resolve().parent, "sky15 源码目录")):
        if _covers(p, other):
            print(f"[sky15.tmp] SKY15_TMP 是{name}或它的上级（{p}），改用 {DEFAULT_BASE}")
            return DEFAULT_BASE
    # 已经存在而且不是空的：名字得像临时目录，才敢往里清东西
    try:
        if p.is_dir() and any(p.iterdir()):
            if not any(k in p.name.lower() for k in _TMPISH):
                print(f"[sky15.tmp] SKY15_TMP={p} 已存在且非空、名字也不像临时目录，"
                      f"为了避免误删，改用 {DEFAULT_BASE}")
                return DEFAULT_BASE
    except OSError:
        pass
    return p


BASE = _resolve_base(os.environ.get("SKY15_TMP"))


def _prune(base):
    now = time.time()
    try:
        for d in base.iterdir():
            if not d.is_dir():
                continue
            # 双保险：只删自己建的目录（前世是 mkdtemp 生成的 `<prefix>_xxxxxxxx`）
            if not d.name.lower().startswith("sky15"):
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
