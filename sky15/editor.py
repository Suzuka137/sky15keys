"""谱面编辑器的后端逻辑：把"格子列表"和 SMC 文本互相转换。

为什么要有这一层：网页编辑器改的是**格子**（一列 = 一个八分音符），
但存盘要的是 SMC 文本。中间有三件容易错的事，全收敛在这里：

1. **同格内高音在前**（`1D` 不是 `D1`）—— 站点和我们的渲染器都按"从高往低竖排"画，
   顺序错了虽然还能弹，但和站上的谱子不一样。
2. **小节线在第 8 格**（`i % spb == 7` 有 `>`），不是按拍号算出来的。
   官方 整库里 13,008 个标记零例外。这里 spb 优先从原谱自己推断，
   推断不出来才回落到 8 —— 编辑器插入/删除格子后要重新按同一间距铺标记。
3. **没小节线的组只有 3 位**（`D10`），有标记才 4 位（`000>`）——
   多写一个空格就会让站点解析出一堆空拍。

对外 API（都是纯函数，不碰磁盘）：
    bar_len(groups)          → 每小节几格（从现有标记推断）
    strip_group(g)           → 去掉标记后的 3 位音字符
    sort_chars(chars)        → 按音高从高到低排序、去重、最多 3 个
    set_chars(groups, i, cs) → 改写第 i 格
    insert_slots(groups, at, n) / delete_slots(groups, at, n)
    rebuild(groups, spb)     → 重新铺小节线，得到干净的组列表
    to_text(meta, groups, spb) → SMC 文本
    from_text(text)          → (meta, slots, spb)
"""
import re

from . import smc

BASE = "DRMFSLX"
HIGH = "1234567"
ORDER = {c: i for i, c in enumerate(BASE + HIGH + "8")}   # 0..14，越大越高
DEFAULT_SPB = 8


def bar_len(groups):
    """每小节几格：看第一个带 `>` 的组在第几位。找不到就 8。"""
    for i, g in enumerate(groups):
        if len(g) >= 4 and ">" in g:
            return i + 1
    return DEFAULT_SPB


def strip_group(g):
    """`000>` → `""`，`1D0` → `1D`，`LFD ` → `LFD`。"""
    return "".join(c for c in (g or "")[:3] if c in ORDER)


def sort_chars(chars):
    """同格内高音在前，去重，最多 3 个。"""
    seen, out = set(), []
    for c in chars or "":
        if c in ORDER and c not in seen:
            seen.add(c)
            out.append(c)
    out.sort(key=lambda c: -ORDER[c])
    return "".join(out[:3])


def full_group(chars):
    """补到 3 位（没音的位置用 0）：`1` → `100`。

    站点写法是**三位定宽**，位数不足会被当成上一组的一部分。
    """
    s = sort_chars(chars)
    return (s + "000")[:3]


def set_chars(groups, i, chars):
    """把第 i 格的音改成 chars（不改变标记）。越界返回原列表。"""
    out = list(groups)
    if 0 <= i < len(out):
        mark = ">" if len(out[i]) >= 4 and ">" in out[i] else ""
        out[i] = full_group(chars) + mark
    return out


def insert_slots(groups, at, n=1):
    """在第 at 格**前面**插入 n 个空拍，后面的整体往后推。"""
    at = max(0, min(int(at), len(groups)))
    n = max(1, min(int(n), 64))
    out = list(groups)
    for _ in range(n):
        out.insert(at, "000")
    return out


def delete_slots(groups, at, n=1):
    """从第 at 格开始删 n 格（至少留 1 格，全删了就没谱了）。"""
    n = max(1, min(int(n), 64))
    at = max(0, min(int(at), len(groups)))
    out = list(groups)
    del out[at:at + n]
    if not out:
        out = ["000"]
    return out


def rebuild(groups, spb=None):
    """重新铺小节线：每 spb 格一个 `>`，其余 3 位。

    编辑器里插一格会让后面所有格子移位，原有的 `>` 就全错位了，
    所以每次改动都要重铺一遍。
    """
    spb = max(1, int(spb or DEFAULT_SPB))
    out = []
    for i, g in enumerate(groups):
        body = full_group(strip_group(g))
        out.append(body + (">" if i % spb == spb - 1 else ""))
    return out


def to_text(meta, groups, spb=None):
    """格子列表 → SMC 文本。meta 里缺的字段用安全默认值。"""
    meta = dict(meta or {})
    spb = max(1, int(spb or bar_len(groups) or DEFAULT_SPB))
    gs = rebuild(groups, spb)
    title = re.sub(r"[\[\]\r\n\t]+", " ", str(meta.get("title") or "未命名")).strip()[:40]
    return smc.emit(
        title or "未命名",
        meta.get("tempo") or 120,
        meta.get("time_sig") or "4/4",
        meta.get("key") or "C",
        bool(meta.get("has8vb")),
        meta.get("difficulty") or "",
        gs,
    )


def from_text(text):
    """SMC 文本 → (meta, slots, spb)。

    slots 是**去掉标记、补足 3 位**的音字符列表，长度 = 格数；
    标记不往外传，前端按 spb 自己画小节线（插入/删除后也才对）。
    """
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    if not groups:
        groups = ["000"]
    spb = bar_len(groups)
    slots = [full_group(strip_group(g)) for g in groups]
    return meta, slots, spb


def stats(slots, spb=None):
    """编辑面板上的小字统计。"""
    spb = max(1, int(spb or DEFAULT_SPB))
    notes = sum(1 for s in slots for c in s if c in ORDER)
    filled = sum(1 for s in slots if any(c in ORDER for c in s))
    thirds = sum(1 for s in slots if len([c for c in s if c in ORDER]) >= 3)
    return {"slots": len(slots), "notes": notes, "filled": filled,
            "bars": (len(slots) + spb - 1) // spb,
            "three": thirds}
