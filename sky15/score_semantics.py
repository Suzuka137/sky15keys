# -*- coding: utf-8 -*-
"""乐谱语义：演奏顺序（反复/D.C./D.S./Coda）、速度分段、调号与移调。

`musicxml.py` 负责"怎么把一个 `<note>` 变成一个事件"（时值、连音、和弦、
backup/forward）；本模块负责**"这一遍到底弹哪些小节、按什么速度"** —— 全是
纯函数，不碰 XML，好在测试里直接构造数据验证。

为什么要单独一层：`musicxml.py` 原来的做法是"源文件第 n 个小节就排在时间轴的
第 n 个位置"。这个假设在**有反复的谱子上是错的** —— 演奏顺序是源顺序的一个
**重排+重复**，不是恒等映射。把这个映射算错，后面所有音的时间轴全错，
而且错得很隐蔽（音高看着很对，节奏整段偏移）。

三层结构：

  1. `expand_repeats()`  小节级的反复展开 → 演奏顺序（源小节下标列表）
  2. `apply_navigation()` 在 1 的结果上再套 D.C./D.S./Fine/To Coda
  3. `build_tempo_map()` 把 (小节, 小节内偏移) 的变速换算成 (绝对拍, BPM)

## 支持的记法

反复（小节级）：
  · `<barline location="left"><repeat direction="forward"/>`  → `|:`
  · `<barline location="right"><repeat direction="backward"/>` → `:|`
    （`times="3"` 表示共弹 3 遍，缺省 2）
  · 没有对应 `|:` 的 `:|` 回到开头（`start=0`），这是最常见的写法
  · 反复嵌套：内层先展开完，外层再重复 —— 栈式处理
  · 第一/第二结尾：`<ending number="1" type="start"/>` … `<ending type="stop"/>`
    第 n 遍只弹 number 里包含 n 的那段；`number="1,2"` 支持

导航：
  · D.C.  `<sound dacapo="yes"/>`      → 回到开头
  · D.S.  `<sound dalsegno="1"/>`      → 回到 segno
  · Segno `<sound segno="1"/>` 或 `<direction-type><segno/>`
  · To Coda `<sound tocoda="coda"/>` 或 `<direction-type><coda/>` 之前的
    `<sound tocoda="...">`；Coda 目标用 `<sound coda="coda"/>` 或
    `<direction-type><coda/>`
  · Fine  `<sound fine="yes"/>` → **只有在 D.C./D.S. 之后的第二遍才停**
    （第一遍琴手会直接弹过去，这是通行约定）

速度：
  · `<sound tempo="120"/>`（四分音符/分钟，MusicXML 规定就是这个口径）
  · `<direction><direction-type><metronome><beat-unit>eighth</beat-unit>
    <per-minute>120</per-minute></metronome>` → 折算成四分音符 BPM
    （八分=120 等于四分=60；附点要靠 `<beat-unit-dot/>`）
  · `<direction><offset>` 会移动标记位置

## 明确还没支持（报告里也写了）
  · D.C./D.S. **之后的第二遍不再走反复**（通行约定；想走要把两层合成一层）
  · 装饰音的演奏时值（`<grace/>` 直接丢掉，不占时间轴，也不出声）
  · 多谱表之间的交叉声部（`<chord/>` 跨谱表已按"同 voice 回退"处理，但不是完整实现）
  · `<sound>` 上的 `dynamics` / `pan` 等播放参数
"""
from __future__ import annotations

# MusicXML <sound tempo> 的口径：四分音符 / 分钟
_TYPE_BEATS = {"maxima": 32.0, "long": 16.0, "breve": 8.0, "whole": 4.0,
               "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25,
               "32nd": 0.125, "64th": 0.0625, "128th": 0.03125}


# ---------------------------------------------------------------- 调号


PC_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# 五线谱上真正写出来的名字（用于对账；sky15 的调名表只认 12 个音级）
SPELL_MAJOR = {-7: "Cb", -6: "Gb", -5: "Db", -4: "Ab", -3: "Eb", -2: "Bb",
               -1: "F", 0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B",
               6: "F#", 7: "C#"}
SPELL_MINOR = {-7: "Abm", -6: "Ebm", -5: "Bbm", -4: "Fm", -3: "Cm",
               -2: "Gm", -1: "Dm", 0: "Am", 1: "Em", 2: "Bm", 3: "F#m",
               4: "C#m", 5: "G#m", 6: "D#m", 7: "A#m"}


def fifths_to_pc(fifths, mode="major"):
    """<fifths> + <mode> → 主音音级（0..11）。

    五度圈：每 +1 个升号 = +7 个半音。小调的主音 = 关系大调主音 + 9。
    **小调必须用 mode 判**，否则 A 小调（fifths=0）会被当成 C 调 ——
    这正好是 sky15 以前的行为，也是"调对了音却怪"的一个来源。
    """
    try:
        f = int(fifths)
    except (TypeError, ValueError):
        f = 0
    f = max(-7, min(7, f))
    major_pc = (7 * f) % 12
    m = (mode or "major").strip().lower()
    if m in ("minor", "min"):
        return (major_pc + 9) % 12
    return major_pc


def fifths_to_key(fifths, mode="major"):
    """→ (sky15 调名, 谱面写法)。sky15 调名一定在 `layout.KEYS` 里。

    `layout.KEYS` 只有 12 个音级名（C/Db/D/…/B），`arrange.pick_key` 直接拿它
    当 `KEY_MIDI` 的键查表。所以这里必须归一化到那张表 —— 否则像 `<fifths>-7`
    （Cb 大调）会返回 "Cb"，`arrange` 里 `layout.KEY_MIDI["Cb"]` 直接 KeyError。
    """
    pc = fifths_to_pc(fifths, mode)
    m = (mode or "major").strip().lower()
    try:
        f = max(-7, min(7, int(fifths)))
    except (TypeError, ValueError):
        f = 0
    spelled = (SPELL_MINOR if m.startswith("min") else SPELL_MAJOR).get(f)
    if spelled is None:
        spelled = PC_NAMES[pc]
    return PC_NAMES[pc], spelled


def transpose_key_name(pc_name, semitones):
    """调名按半音数移调（用于移调乐器：写谱调 + 移调量 = 实际发声调）。"""
    if pc_name not in PC_NAMES or not semitones:
        return pc_name
    return PC_NAMES[(PC_NAMES.index(pc_name) + int(semitones)) % 12]


# ---------------------------------------------------------------- 反复


def _ending_numbers(num):
    """`number` 属性 → 整数集合。支持 "1" / "1,2" / "1 2" / "1-2"。"""
    if num is None:
        return set()
    out = set()
    for part in str(num).replace(".", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            out.update(range(min(lo, hi), max(lo, hi) + 1))
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def pair_endings(measures):
    """把 ending 起止配成区间，并把 `ending` / `ending_span_end` 铺到区间里每个小节。"""
    open_at = None
    for i, m in enumerate(measures):
        if m.get("ending_start"):
            open_at = (i, m["ending_start"])
        if m.get("ending_stop") and open_at is not None:
            start, num = open_at
            for j in range(start, i + 1):
                measures[j]["ending"] = num
                measures[j]["ending_span_end"] = i
            open_at = None
    # 只有 start 没有 stop（OMR 常见）：这一小节就是第一结尾，不往后铺
    if open_at is not None:
        start, num = open_at
        measures[start]["ending"] = num
        measures[start]["ending_span_end"] = start
    return measures


LAST_WARNINGS = []          # 最近一次 performance_order() 的说明，上层并进 ParsedScore.warnings


def expand_repeats(measures, max_steps=None):
    """→ 演奏顺序（源小节下标列表，可重复）。

    `measures` 每项至少要能读到这四个键（缺省当没有）：
        repeat_forward : bool       小节**左**边线是 `|:`
        repeat_backward: int|None   小节**右**边线是 `:|`，值 = 共弹几遍
        ending         : str|None   本节属于第几个结尾（`pair_endings` 铺好的）
        ending_span_end: int|None   本结尾的最后一小节下标
    """
    n = len(measures)
    if n == 0:
        return []
    pair_endings(measures)
    limit = int(max_steps) if max_steps else 20 * n + 200

    order = []
    stack = []                 # 未闭合的 `|:` 位置
    passes = {}                # 反复起点 -> 已完成的遍数
    i = 0
    guard = 0
    while 0 <= i < n:
        guard += 1
        if guard > limit:             # 死循环保护（恶意/损坏的谱子）
            # 以前这里默默 break，用户只看到"曲子短了一截"，不知道是标记配对不上。
            LAST_WARNINGS.append(
                f"反复展开走到第 {guard} 步被截断（上限 {limit} 步）——"
                f"反复/结尾标记可能配对不上，演奏顺序比实际短。")
            break
        m = measures[i]

        if m.get("repeat_forward"):
            if not stack or stack[-1] != i:
                stack.append(i)
                passes.setdefault(i, 0)

        # 结尾门：按"所属反复当前第几遍"决定这一段弹不弹
        if m.get("ending"):
            start = stack[-1] if stack else 0
            cur = passes.get(start, 0) + 1
            if cur not in _ending_numbers(m["ending"]):
                nxt = m.get("ending_span_end")
                i = (nxt if nxt is not None else i) + 1
                continue

        order.append(i)

        rb = m.get("repeat_backward")
        if rb:
            start = stack[-1] if stack else 0
            passes[start] = passes.get(start, 0) + 1
            if passes[start] < int(rb):
                i = start
                continue
            passes.pop(start, None)
            if stack:
                stack.pop()
        i += 1
    return order


def apply_navigation(order, measures, max_steps=None):
    """在反复展开结果上再套 D.C. / D.S. / Fine / To Coda。

    只跳一次：`visited` 之后不再响应 D.C./D.S.；Fine 也只在 `visited`
    之后才生效（通行约定：第一遍弹到 Fine 不停）。
    """
    if not order:
        return order
    has_jump = any(measures[i].get("dacapo") or measures[i].get("dalsegno")
                   for i in order)
    if not has_jump:
        return order

    segno_pos = coda_pos = None
    for k, idx in enumerate(order):
        m = measures[idx]
        if m.get("segno") and segno_pos is None:
            segno_pos = k
        if m.get("coda") and coda_pos is None:
            coda_pos = k

    out = []
    i = 0
    visited = False           # 已经跳过 D.C./D.S. 一次
    coda_taken = False
    guard = 0
    limit = int(max_steps) if max_steps else 4 * len(order) + 200
    while 0 <= i < len(order):
        guard += 1
        if guard > limit:
            LAST_WARNINGS.append(
                f"演奏顺序套 D.C./D.S. 走到第 {guard} 步被截断（上限 {limit} 步）——"
                f"Fine/Coda/segno 位置可能有问题，结尾会比实际短。")
            break
        m = measures[order[i]]
        if m.get("fine") and visited:
            out.append(order[i])      # Fine 那一小节弹完收
            break
        out.append(order[i])
        if m.get("tocoda") and visited and not coda_taken and coda_pos is not None:
            coda_taken = True
            i = coda_pos
            continue
        if not visited:
            if m.get("dacapo"):
                visited = True
                i = 0
                continue
            if m.get("dalsegno"):
                visited = True
                i = segno_pos if segno_pos is not None else 0
                continue
        i += 1
    return out


def performance_order(measures, max_steps=None):
    """反复 + 导航 的最终演奏顺序。

    被死循环保护截断时，原因会进 `LAST_WARNINGS`（上层并进 `ParsedScore.warnings`），
    不再静默少一段。
    """
    del LAST_WARNINGS[:]
    return apply_navigation(expand_repeats(measures, max_steps=max_steps),
                            measures, max_steps=max_steps)


def measure_starts(order, measures):
    """演奏顺序 → (每个位置的小节起点拍, 总长拍)。"""
    starts = []
    t = 0.0
    for idx in order:
        starts.append(t)
        t += max(0.0, float(measures[idx].get("length") or 0.0))
    return starts, t


# ---------------------------------------------------------------- 速度


def metronome_bpm(beat_unit, per_minute, dots=0):
    """`<metronome>` → 四分音符 BPM。八分=120 → 60；附点四分=60 → 90。"""
    base = _TYPE_BEATS.get((beat_unit or "quarter").strip().lower())
    if base is None or not per_minute:
        return None
    beats = base * (2.0 - 0.5 ** int(dots or 0))
    try:
        return float(per_minute) * beats
    except (TypeError, ValueError):
        return None


def build_tempo_map(order, measures, default_bpm=100.0):
    """→ [(起始拍, BPM), ...]，按拍升序，第一项一定在 0。

    `measures[i]["tempos"]` 是 [(小节内偏移拍, BPM), ...]。
    同一个小节在演奏顺序里出现两次，它的变速就记两次 —— 这正是演奏的真相。
    """
    marks = []
    t = 0.0
    for idx in order:
        m = measures[idx]
        for rel, bpm in (m.get("tempos") or ()):
            if bpm and bpm > 0:
                marks.append((t + float(rel), float(bpm)))
        t += max(0.0, float(m.get("length") or 0.0))
    marks.sort(key=lambda x: x[0])

    dedup = []
    for pos, bpm in marks:
        if dedup and abs(dedup[-1][0] - pos) < 1e-9:
            dedup[-1] = (pos, bpm)          # 同位置以最后写的为准
        elif dedup and abs(dedup[-1][1] - bpm) < 1e-9:
            continue                        # 速度没变：不切段
        else:
            dedup.append((pos, bpm))
    if not dedup or dedup[0][0] > 1e-9:
        dedup.insert(0, (0.0, float(default_bpm or 100.0)))
    return dedup


def normalize_tempo_map(tempo_map, default_bpm=100.0):
    """把外部传进来的速度表收拾干净；空表退回单速度。"""
    if not tempo_map:
        return [(0.0, float(default_bpm or 100.0))]
    out = []
    for item in tempo_map:
        try:
            pos, bpm = item
            pos, bpm = float(pos), float(bpm)
        except (TypeError, ValueError):
            continue
        if bpm <= 0:
            continue
        out.append((pos, bpm))
    if not out:
        return [(0.0, float(default_bpm or 100.0))]
    out.sort(key=lambda x: x[0])
    if out[0][0] > 1e-9:
        out.insert(0, (0.0, out[0][1]))
    return out


def seconds_at(beat, tempo_map):
    """拍 → 秒（按分段速度积分）。tempo_map 必须已 normalize。"""
    beat = float(beat)
    if beat <= tempo_map[0][0]:
        return 0.0
    s = 0.0
    for k, (pos, bpm) in enumerate(tempo_map):
        nxt = tempo_map[k + 1][0] if k + 1 < len(tempo_map) else None
        if nxt is None or beat <= nxt:
            return s + (beat - pos) * 60.0 / bpm
        s += (nxt - pos) * 60.0 / bpm
    return s


def beats_to_seconds(events, tempo_map):
    """事件表（拍）→ 秒。跨变速点的音按时长积分，不是一刀切。"""
    tm = normalize_tempo_map(tempo_map)
    out = []
    for e in events:
        s0 = seconds_at(e["start"], tm)
        s1 = seconds_at(e["start"] + e["dur"], tm)
        out.append(dict(e, start=s0, dur=max(0.0, s1 - s0)))
    return out


def beat_to_seconds_positions(starts, tempo_map):
    tm = normalize_tempo_map(tempo_map)
    return [seconds_at(b, tm) for b in starts]


def tempo_at(beat, tempo_map):
    tm = normalize_tempo_map(tempo_map)
    bpm = tm[0][1]
    for pos, v in tm:
        if pos <= beat + 1e-9:
            bpm = v
        else:
            break
    return bpm


def beat_at_seconds(sec, tempo_map):
    """秒 → 拍（`seconds_at` 的逆函数，分段线性）。

    五线谱里有中途变速（渐快/rall/换段）时，`to_seconds()` 出来的秒已经按
    分段速度积分过；出谱的格子却只有一个固定 tempo。要把两者对上，就得把
    "秒"反推回"拍"，再除以 0.5（一格 = 一个八分音符 = 半拍）。
    """
    tm = normalize_tempo_map(tempo_map)
    sec = float(sec)
    if sec <= 0.0:
        return float(tm[0][0])
    s = 0.0
    for k, (pos, bpm) in enumerate(tm):
        seg_len = (60.0 / bpm)
        nxt = tm[k + 1][0] if k + 1 < len(tm) else None
        seg_sec = (nxt - pos) * seg_len if nxt is not None else None
        if seg_sec is None or sec <= s + seg_sec:
            return pos + (sec - s) / seg_len
        s += seg_sec
    return pos + (sec - s) / seg_len


def seconds_to_slots(sec, tempo_map):
    """秒 → 格子坐标（浮点，一格 = 一个八分音符）。直接喂给 `arrange(warp=...)`。"""
    return beat_at_seconds(sec, tempo_map) / 0.5


def describe_tempo_map(tempo_map, total_beats=None):
    """一张给人看的速度分段表（报告 / 调试用）。"""
    tm = normalize_tempo_map(tempo_map)
    lines = []
    for k, (pos, bpm) in enumerate(tm):
        seg_beats = (tm[k + 1][0] - pos) if k + 1 < len(tm) else (
            (total_beats - pos) if total_beats else None)
        s0 = seconds_at(pos, tm)
        seg = f"{seg_beats:.2f}拍/{seg_beats * 60.0 / bpm:.2f}秒" if seg_beats else ""
        lines.append(f"{pos:8.2f}拍 ({s0:7.2f}s)  {bpm:6.2f} BPM  {seg}")
    return lines
