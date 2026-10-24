"""谱面编辑器的后端逻辑：把"格子列表"和 SMC 文本互相转换。

为什么要有这一层：网页编辑器改的是**格子**（一列 = 一个八分音符），
但存盘要的是 SMC 文本。中间有三件容易错的事，全收敛在这里：

1. **同格内高音在前**（`1D` 不是 `D1`）—— 站点和我们的渲染器都按"从高往低竖排"画，
   顺序错了虽然还能弹，但和站上的谱子不一样。
2. **小节线在第 8 格**（`i % spb == 7` 有 `>`），不是按拍号算出来的。
   官方 整库里 13,008 个标记零例外。spb 按**标记间距**从原谱自己推断，
   推断不出来才回落到 8 —— 编辑器插入/删除格子后要重新按同一间距铺标记。
   标记有 `>` 和 `<` 两种，都算小节线，重铺时不能把 `<` 改掉。
3. **没小节线的组只有 3 位**（`D10`），有标记才 4 位（`000>`）——
   多写一个空格就会让站点解析出一堆空拍。

对外 API（都是纯函数，不碰磁盘）：
    bar_len(groups)          → 每小节几格（从现有标记间距推断）
    split_mark(g)            → (3 位音字符, 标记 `>`/`<`/``)
    strip_group(g)           → 去掉标记后的 3 位音字符
    sort_chars(chars)        → 按音高从高到低排序、去重、最多 3 个
    set_chars(groups, i, cs) → 改写第 i 格
    insert_slots(groups, at, n) / delete_slots(groups, at, n)
    rebuild(groups, spb, marks=None) → 重新铺小节线，得到干净的组列表
    to_text(meta, groups, spb) → SMC 文本（meta["_marks"] 存在时按原标记回写）
    from_text(text)          → (meta, slots, spb)
"""
import math
import re
from collections import Counter

from . import smc

BASE = "DRMFSLX"
HIGH = "1234567"
ORDER = {c: i for i, c in enumerate(BASE + HIGH + "8")}   # 0..14，越大越高
DEFAULT_SPB = 8
MARKS = "><"


def split_mark(g):
    """`000>` → ('', '>')，`1D0<` → ('1D', '<')，`LFD` → ('LFD', '')。

    `>` 和 `<` 都是小节线（见 smc.py 格式说明），只是种类不同；
    把 `<` 当成普通字符丢掉，等于把原谱的段落结构吃掉。
    """
    s = g or ""
    mark = s[3] if len(s) >= 4 and s[3] in MARKS else ""
    return s[:3], mark


def bar_len(groups):
    """每小节几格：看现有小节线（`>` 和 `<` 都算）的**间距**，推不出就 8。

    以前是"第一个 `>` 的下标 + 1"：官方谱 `4.txt` 前 496 格只有 `<` 没有 `>`，
    于是推出 spb=496，256 个小节线重铺完只剩 4 个（`stats()["bars"]` 250 → 5）。
    相邻标记的间距才是小节长度本身；偶尔漏一个小节线也不影响众数。
    """
    pos = [i for i, g in enumerate(groups) if split_mark(g)[1]]
    if not pos:
        return DEFAULT_SPB
    if len(pos) == 1:                 # 只有一个标记：只能按它的位置推
        return max(1, pos[0] + 1)
    diffs = [b - a for a, b in zip(pos, pos[1:])]
    gap, n = Counter(diffs).most_common(1)[0]
    if n * 2 >= len(diffs):           # 至少一半间距一致 → 那就是一小节
        return max(1, gap)
    g = 0                             # 间距花（缺标记/多标记）→ 取最大公约数
    for d in diffs:
        g = math.gcd(g, d)
    return g or DEFAULT_SPB


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
        mark = split_mark(out[i])[1]        # `>` / `<` 都保留，不能只认 `>`
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


def _marks_reusable(groups, marks, spb):
    """原标记能不能照搬：长度对得上，且都落在同一个小节相位上。"""
    if not isinstance(marks, (list, tuple)) or len(marks) != len(groups):
        return False
    pos = [i for i, m in enumerate(marks) if m in (">", "<")]
    if not pos:
        return True       # 原谱本来就没有小节线 → 别给它凭空补
    first = pos[0] % spb
    return all(i % spb == first for i in pos)


def rebuild(groups, spb=None, marks=None):
    """重新铺小节线：每 spb 格一个 `>`，其余 3 位。

    编辑器里插一格会让后面所有格子移位，原有的标记就全错位了，所以每次改动
    都要重铺一遍。但 `marks`（与 groups 等长的标记序列，见 from_text）长度对得上、
    且每个标记都落在同一个小节相位上时**按原样回写**：往返不改动任何标记，
    `<` 不会被改写，弱起/前奏也不会被对齐到第 8 格。
    相位或长度对不上（插删过格子）才按 spb 均匀重铺。
    """
    spb = max(1, int(spb or DEFAULT_SPB))
    keep = _marks_reusable(groups, marks, spb)
    out = []
    for i, g in enumerate(groups):
        body = full_group(strip_group(g))
        if keep:
            mark = marks[i] if marks[i] in (">", "<") else ""
        else:
            mark = split_mark(g)[1] or (">" if i % spb == spb - 1 else "")
        out.append(body + mark)
    return out


def to_text(meta, groups, spb=None):
    """格子列表 → SMC 文本。meta 里缺的字段用安全默认值。"""
    meta = dict(meta or {})
    spb = max(1, int(spb or bar_len(groups) or DEFAULT_SPB))
    gs = rebuild(groups, spb, meta.get("_marks"))
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
    标记不往外传（前端按 spb 自己画小节线，插入/删除后也才对），
    但要留在 `meta["_marks"]` 里：`to_text` 靠它把原标记（含 `<`）原样写回去。
    """
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    if not groups:
        groups = ["000"]
    spb = bar_len(groups)
    slots = [full_group(strip_group(g)) for g in groups]
    meta["_marks"] = [split_mark(g)[1] for g in groups]
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
