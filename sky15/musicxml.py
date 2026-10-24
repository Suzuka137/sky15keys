# -*- coding: utf-8 -*-
"""MusicXML → 音符事件 → 15 键谱。

为什么支持它：**五线谱的通用交换格式就是 MusicXML**。
不管是 MuseScore / Sibelius / Finale 导出的，还是 Audiveris / oemer 这类 OMR
识别出来的，最后都落成它。所以支持了 MusicXML，等于支持了所有"五线谱来源"，
只剩"图片→MusicXML"那一步需要外部工具。

同时支持 `.mxl`（压缩包版，MuseScore 默认导这个）。

## 两段式：先"读小节"，再"排演奏"

原来的实现是"源文件第 n 个小节 → 时间轴第 n 个位置"，**有反复的谱子直接错**。
现在拆成两段（`score_semantics.py` 提供第二段的纯逻辑）：

  1. `_collect_part()`：把每个 `<part>` 读成**小节列表**，每小节记
     音符(小节内相对 onset) / 长度 / 反复边线 / 结尾标记 / 变速点 / 导航标记。
     这一步完全不知道"演奏顺序"，只反映**谱面写了什么**。
  2. `score_semantics.performance_order()`：反复 + D.C./D.S./Coda 展开成
     "按谱弹一遍"的小节下标序列；再按这个序列累加小节起点。
  3. `score_semantics.build_tempo_map()`：中途变速 → `[(起始拍, BPM), ...]`。

## 已经踩过并搬进来的坑

  · `divisions` 是每四分音符的细分，所有时值都要除它；**它会中途变**，
    所以内部一律换算成"拍"（float），不再拿 divisions 当货币
  · **小节线是硬边界**：`t` 在每小节开头复位到 `小节起点`，下一小节起点 =
    本小节起点 + 拍号长度。Audiveris 常在小节末尾留一个"回到小节头"的
    `<backup>` 给下一个声部用，若照单全收，下一小节就会从上一小节的开头开始，
    整首曲子被压扁、一堆音堆在同一时刻 —— 这是真实钢琴谱丢音的头号原因
  · `<backup>` 回退不越过小节起点（OMR 的取整误差会让 backup 多退几个 tick，
    不夹住就出负时间）
  · 和弦用 `<chord/>` 标记 —— 它**不推进时间**；起点按 (谱表, 声部) 各记各的
    `last_onset`，否则第 2 声部的首个和弦音会用到第 1 声部的位置。
    跨谱表的和弦（`<chord/>` 但 `<staff>` 变了）先按同 voice 回退，再退回当前游标
  · 多声部用 `<backup>` / `<forward>` 回退和前进时间
  · `<tie>` 与 `<tied>` 是两个不同的东西：前者管播放，后者管显示；只写 `<tied>`
    的谱子也要能合并。合并键是 (谱表, 声部, 音高)，同音高的普通音不得打断连音链，
    A→A→A 这种多段链要一路累加；断链（只有 stop 没有 start）就当普通音，不许丢。
    **连音只在相邻的小节次之间合并** —— 反复把同一个音弹两遍时不许把两遍连起来
  · 休止符 `<rest/>` 推进时间但不发声
  · 装饰音 `<grace/>` 不占时值、不出声（但会记进 `warnings` 计数）
  · 时值超过小节线的音在小节线处截断（跨小节靠 tie，不靠一个音硬拖）
  · 速度：开头那个当全局速度，中途的全部记进**速度分段表**；
    `<metronome>` 的八分/附点单位会折算成四分音符 BPM（见 score_semantics）
  · 移调乐器 `<transpose chromatic="…" octave-change="…">`：`<pitch>` 是**写谱音高**，
    实际发声 = 写谱 + chromatic + 12×octave-change
  · `<clef-octave-change>`（8vb / 8va 谱号）：发声再 ±12×change，按谱号 number 分谱表
  · `<key><mode>minor</mode>`：小调主音 = 关系大调主音 + 9，必须用 mode 判
  · 弱起小节：不补空也不挪 —— 首小节内容明显不足时按实际长度算
  · 多个 `<part>` 各自从 0 开始，小节长度按拍号对齐，混到一起时间轴才是齐的
"""
import re
import xml.etree.ElementTree as ET
import zipfile, pathlib
from dataclasses import dataclass, field

from . import score_semantics as _sem

STEP_SEMIS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NAME_TO_KEY = {"C": "C", "G": "G", "D": "D", "A": "A", "E": "E", "B": "B",
               "F": "F", "BB": "Bb", "EB": "Eb", "AB": "Ab", "DB": "Db",
               "GB": "Gb", "CB": "B", "FB": "E"}

# <type> → 几拍（四分音符 = 1 拍）。只在缺 <duration> 时兜底。
TYPE_BEATS = {"maxima": 32.0, "long": 16.0, "breve": 8.0, "whole": 4.0,
              "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25,
              "32nd": 0.125, "64th": 0.0625, "128th": 0.03125}

# <words> 里的文字兜底用的正则（顺序无所谓，`_read_words` 里自己按语义分派）
_WORDS_PATTERNS = (
    (re.compile(r"\bD\.?\s*C\.?", re.I), "dacapo"),
    (re.compile(r"\bD\.?\s*S\.?", re.I), "dalsegno"),
    (re.compile(r"\bto\s+coda\b|\bal\s+coda\b", re.I), "tocoda"),
    (re.compile(r"\bfine\b", re.I), "fine"),
    (re.compile(r"\bsegno\b", re.I), "segno"),
    (re.compile(r"\bcoda\b", re.I), "coda"),
)


def _tag(e):
    return e.tag.split("}")[-1] if isinstance(e.tag, str) else ""


def _load(path):
    """支持 .xml / .musicxml / .mxl（后者是 zip）"""
    p = pathlib.Path(path)
    if p.suffix.lower() == ".mxl":
        with zipfile.ZipFile(p) as z:
            root = None
            # META-INF/container.xml 指向真正的根文件
            try:
                c = z.read("META-INF/container.xml").decode("utf-8", "replace")
                m = re.search(r'full-path="([^"]+)"', c)
                if m:
                    root = m.group(1)
            except KeyError:
                pass
            if root is None:
                cands = [n for n in z.namelist()
                         if n.lower().endswith((".xml", ".musicxml"))
                         and not n.startswith("META-INF")]
                root = cands[0] if cands else None
            if root is None:
                raise ValueError("这个 .mxl 里找不到 MusicXML 主体")
            data = z.read(root)
    else:
        data = p.read_bytes()
    return ET.fromstring(data)


# ---------------------------------------------------------------- 全局信息


def _num_str(x):
    """5.0 → "5"（拍号只认整数写法，别把 5.0 抖成 "5.0"）。"""
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:g}"


def _read_time(el):
    """<time> → ("4/4", 4.0)；长度单位是拍。复合拍号 3+2/8 会加和。

    **加和之后必须写成合法拍号**（`3+2/8` → `5/8`）。原来把 XML 里的原文
    `"3+2/8"` 原样外传，下游全是按 `int(n)/int(d)` 拆的：
    `web/server.py:_bar_quarters` 的 except 会静默回落 4/4（真值 2.5 拍 →
    一小节多算 1.5 拍，整首速度和时值全错），`smc.py` / `web/midiout.py`
    同样吃不下。长度 `num*4/bt` 原来就是加和后的值，只有字符串没跟上。
    """
    beats = btype = None
    for c in el:
        ct = _tag(c)
        if ct == "beats":
            beats = (c.text or "").strip()
        elif ct == "beat-type":
            btype = (c.text or "").strip()
    if not beats or not btype:
        return None
    try:
        bt = float(btype)
        num = sum(float(x) for x in beats.replace(" ", "").split("+") if x)
    except ValueError:
        return None
    if bt <= 0 or num <= 0:
        return None
    return f"{_num_str(num)}/{_num_str(bt)}", num * 4.0 / bt


def _fifths_to_key(f):
    """兼容旧接口：只有 fifths 时按大调判（新代码走 score_semantics）。"""
    return _sem.fifths_to_key(f, "major")[0]


# ---------------------------------------------------------------- 单个 <note>


def _read_note(note):
    """把一个 <note> 拆成普通 dict（时值单位：拍）。"""
    it = {"staff": "", "voice": "", "pitch": None, "dur": 0.0, "vel": 90,
          "rest": False, "chord": False, "grace": False,
          "tie_start": False, "tie_stop": False}
    dur_raw = None
    ntype = None
    dots = 0
    step = octv = None
    alt = 0
    ties = []            # <tie>
    noted = []           # <notations><tied>
    sound_tempo = None   # <note><sound tempo="…"/>

    for c in note:
        ct = _tag(c)
        if ct == "chord":
            it["chord"] = True
        elif ct == "grace":
            it["grace"] = True
        elif ct == "rest":
            it["rest"] = True
        elif ct == "unpitched":
            it["rest"] = True          # 打击乐：没有音高，当休止处理
        elif ct == "pitch":
            for g in c:
                gt = _tag(g)
                if gt == "step":
                    step = (g.text or "").strip().upper()
                elif gt == "alter":
                    try:
                        alt = int(float(g.text))
                    except (TypeError, ValueError):
                        alt = 0
                elif gt == "octave":
                    try:
                        octv = int(g.text)
                    except (TypeError, ValueError):
                        octv = None
        elif ct == "duration":
            try:
                dur_raw = int(float(c.text))
            except (TypeError, ValueError):
                dur_raw = None
        elif ct == "voice":
            it["voice"] = (c.text or "").strip()
        elif ct == "staff":
            it["staff"] = (c.text or "").strip()
        elif ct == "type":
            ntype = (c.text or "").strip().lower()
        elif ct == "dot":
            dots += 1
        elif ct == "tie":
            ties.append(c.get("type") or "")
        elif ct == "notations":
            for g in c:
                if _tag(g) == "tied":
                    noted.append(g.get("type") or "")
        elif ct == "velocity":
            try:
                it["vel"] = int(c.text)
            except (TypeError, ValueError):
                pass
        elif ct == "sound":
            sound_tempo = c.get("tempo")

    if step is not None and octv is not None and step in STEP_SEMIS:
        it["pitch"] = 12 * (octv + 1) + STEP_SEMIS[step] + alt

    tlist = ties or noted
    it["tie_start"] = "start" in tlist
    it["tie_stop"] = "stop" in tlist
    it["dur_raw"] = dur_raw
    it["type"] = ntype
    it["dots"] = dots
    it["sound_tempo"] = sound_tempo
    return it


def _item_beats(it, divisions):
    """这个音符占几拍。优先 <duration>，缺了就按 <type> + 附点兜底。"""
    if it["dur_raw"] is not None:
        return it["dur_raw"] / float(divisions)
    base = TYPE_BEATS.get(it["type"])
    if base is None:
        return 0.0
    return base * (2.0 - 0.5 ** it["dots"])


def _find_duration(el):
    """<backup>/<forward> 里的 <duration>（divisions 单位）。"""
    for c in el:
        if _tag(c) == "duration":
            try:
                return int(float(c.text))
            except (TypeError, ValueError):
                return 0
    return 0


def _find_offset(el, divisions):
    """<direction><offset> 是相对当前位置的位移（divisions 单位）。"""
    for c in el:
        if _tag(c) == "offset":
            try:
                v = float(c.text)
            except (TypeError, ValueError):
                return 0.0
            return v / float(divisions or 1)
    return 0.0


# ---------------------------------------------------------------- 小节级收集


def _new_measure(index, number):
    return {"index": index, "number": number, "items": [], "length": 0.0,
            "implicit": False, "tempos": [],
            "repeat_forward": False, "repeat_backward": None,
            "ending_start": None, "ending_stop": False,
            "segno": False, "coda": False, "dacapo": False,
            "dalsegno": False, "tocoda": False, "fine": False,
            "time_sig": None, "fifths": 0, "mode": "major"}


def _read_sound(el, m):
    """<sound> 上的 tempo / 导航属性。

    tempo 与 dacapo/dalsegno/tocoda/fine/segno/coda **可以同时出现**
    （`<sound tempo="72" dacapo="yes"/>` 是 MuseScore/OMR 的常见写法）。
    原来 tempo 一有值就 `return float(tempo)`，后面的导航标记一个都不解析 →
    D.C./D.S./coda/fine 全丢，谱子按源顺序直排（实测 fx_dc 类谱子只出 2 小节）。
    """
    bpm = None
    tempo = el.get("tempo")
    if tempo:
        try:
            bpm = float(tempo)
        except ValueError:
            bpm = None
    if (el.get("dacapo") or "").lower() in ("yes", "true", "1"):
        m["dacapo"] = True
    if el.get("dalsegno"):
        m["dalsegno"] = True
    if el.get("tocoda"):
        m["tocoda"] = True
    if (el.get("fine") or "").lower() in ("yes", "true", "1"):
        m["fine"] = True
    if el.get("segno"):
        m["segno"] = True
    if el.get("coda"):
        m["coda"] = True
    return bpm


def _read_words(text, m):
    """`<words>` 文字兜底：OMR / 老软件只画一个 "D.C. al Fine" 而不写 <sound>。

    注意区分三种 "Coda"：
      · "To Coda" / "al Coda" → **跳到** coda（tocoda）
      · 单独的 "Coda"         → **coda 落点**
    混在一起会把 tocoda 的落点也标成 coda，D.S. 之后一跳就回到自己身上，
    变成死循环式重复（实测 fx_ds_coda 会从 7 小节膨胀成 9 小节）。
    """
    t = text or ""
    if re.search(r"\bD\.?\s*C\.?", t, re.I):
        m["dacapo"] = True
    if re.search(r"\bD\.?\s*S\.?", t, re.I):
        m["dalsegno"] = True
    if re.search(r"\bto\s+coda\b|\bal\s+coda\b", t, re.I):
        m["tocoda"] = True
    elif re.search(r"\bcoda\b", t, re.I):
        m["coda"] = True
    if re.search(r"\bfine\b", t, re.I):
        m["fine"] = True
    if re.search(r"\bsegno\b", t, re.I):
        m["segno"] = True


def _collect_part(part, default_beats=None):
    """一个 <part> → 小节列表（+ 调号/移调信息）。不处理演奏顺序。"""
    divisions = 1
    measure_beats = default_beats
    time_sig = None
    transpose_ch = 0
    transpose_oct = 0
    clef_oct = {}                 # 谱表号(str) → 八度偏移
    fifths, mode = 0, "major"
    first = True
    grace_dropped = 0
    bad_divisions = 0             # <divisions> 0 / 非数字出现几次（下面会进 warnings）
    ts_seen = []                  # [(小节号, 拍号)]：中途变拍号要留痕
    cur_ts = None                 # 当前生效的拍号（变过之后的小节继承它）
    measures = []
    idx = 0

    for measure in part:
        if _tag(measure) != "measure":
            continue
        m = _new_measure(idx, measure.get("number") or str(idx + 1))
        m["implicit"] = (measure.get("implicit") or "").lower() == "yes"
        rel = 0.0
        last_onset = {}           # (staff, voice) → 本声部上一音起点（小节内）
        last_by_voice = {}        # voice → 起点（跨谱表和弦兜底）
        note_sounds = []          # (rel, tempo) 来自 <note><sound>

        for el in measure:
            tag = _tag(el)

            if tag == "attributes":
                for c in el:
                    ct = _tag(c)
                    if ct == "divisions":
                        try:
                            div = int(float(c.text))
                        except (TypeError, ValueError):
                            div = None
                        if div is None:
                            bad_divisions += 1
                        elif div < 1:
                            # 和 omr._first_div 统一：clamp 到 1，并且说出来。
                            # 0 会让 `duration / divisions` 要么炸要么整体错 4 倍
                            bad_divisions += 1
                            divisions = 1
                        else:
                            divisions = div
                    elif ct == "time":
                        got = _read_time(c)
                        if got:
                            measure_beats = got[1]
                            if time_sig is None:
                                time_sig = got[0]
                            # 中途变拍号：写进**本小节**（原来只记第一个，后面
                            # 全按第一个拍号算，小节长度/速度都错）
                            cur_ts = got[0]
                            m["time_sig"] = got[0]
                            if not ts_seen or ts_seen[-1][1] != got[0]:
                                ts_seen.append((idx + 1, got[0]))
                    elif ct == "key":
                        for g in c:
                            gt = _tag(g)
                            if gt == "fifths":
                                try:
                                    fifths = int(float(g.text))
                                except (TypeError, ValueError):
                                    pass
                            elif gt == "mode":
                                mode = (g.text or "major").strip().lower()
                    elif ct == "transpose":
                        ch = oc = 0
                        for g in c:
                            gt = _tag(g)
                            if gt == "chromatic":
                                try:
                                    ch = int(float(g.text))
                                except (TypeError, ValueError):
                                    ch = 0
                            elif gt == "octave-change":
                                try:
                                    oc = int(float(g.text))
                                except (TypeError, ValueError):
                                    oc = 0
                        transpose_ch, transpose_oct = ch, oc
                    elif ct == "clef":
                        num = (c.get("number") or "1").strip()
                        for g in c:
                            if _tag(g) == "clef-octave-change":
                                try:
                                    clef_oct[num] = int(float(g.text))
                                except (TypeError, ValueError):
                                    pass
                continue

            if tag == "barline":
                loc = (el.get("location") or "right").lower()
                for c in el:
                    ct = _tag(c)
                    if ct == "repeat":
                        d = (c.get("direction") or "").lower()
                        if d == "forward":
                            m["repeat_forward"] = True
                        elif d == "backward":
                            try:
                                times = int(c.get("times") or 2)
                            except (TypeError, ValueError):
                                times = 2
                            m["repeat_backward"] = max(2, times)
                    elif ct == "ending":
                        etype = (c.get("type") or "").lower()
                        if etype == "start":
                            m["ending_start"] = c.get("number") or "1"
                        elif etype in ("stop", "discontinue"):
                            # 这个标记落在哪一侧都可能，统一记在"小节右端"
                            m["ending_stop"] = True
                if loc == "left":
                    pass
                continue

            if tag == "sound":
                bpm = _read_sound(el, m)
                if bpm:
                    m["tempos"].append((rel, bpm))
                continue

            if tag == "direction":
                off = _find_offset(el, divisions)
                pos = rel + off
                for c in el:
                    ct = _tag(c)
                    if ct == "sound":
                        bpm = _read_sound(c, m)
                        if bpm:
                            m["tempos"].append((pos, bpm))
                    elif ct == "direction-type":
                        for g in c:
                            gt = _tag(g)
                            if gt == "metronome":
                                unit, per, dots = None, None, 0
                                for h in g:
                                    ht = _tag(h)
                                    if ht == "beat-unit":
                                        unit = (h.text or "").strip()
                                    elif ht == "per-minute":
                                        try:
                                            per = float(h.text)
                                        except (TypeError, ValueError):
                                            per = None
                                    elif ht == "beat-unit-dot":
                                        dots += 1
                                bpm = _sem.metronome_bpm(unit, per, dots)
                                if bpm:
                                    m["tempos"].append((pos, bpm))
                            elif gt == "segno":
                                m["segno"] = True
                            elif gt == "coda":
                                m["coda"] = True
                            elif gt == "words":
                                _read_words(g.text, m)
                continue

            if tag == "backup":
                # 不越过小节起点：OMR 的 backup 经常多退一点点
                rel = max(0.0, rel - _find_duration(el) / float(divisions))
                last_onset.clear()
                last_by_voice.clear()
                continue

            if tag == "forward":
                rel += _find_duration(el) / float(divisions)
                last_onset.clear()
                last_by_voice.clear()
                continue

            if tag != "note":
                continue

            it = _read_note(el)
            if it["sound_tempo"]:
                try:
                    m["tempos"].append((rel, float(it["sound_tempo"])))
                except ValueError:
                    pass
            if it["grace"]:
                grace_dropped += 1
                continue                 # 装饰音不占时值，也不出声
            it["dur"] = _item_beats(it, divisions)
            it.pop("dur_raw", None)
            it.pop("sound_tempo", None)

            # 移调 + 谱号八度 → 实际发声音高
            if it["pitch"] is not None:
                staff_no = it["staff"] or "1"
                shift = transpose_ch + 12 * (transpose_oct +
                                             clef_oct.get(staff_no, 0))
                if shift:
                    it["pitch"] += shift

            vkey = (it["staff"], it["voice"])
            if it["chord"]:
                # 同一谱表同一声音 → 同 voice 别的谱表（跨谱表和弦）→ 当前游标
                if vkey in last_onset:
                    onset = last_onset[vkey]
                elif it["voice"] in last_by_voice:
                    onset = last_by_voice[it["voice"]]
                else:
                    onset = rel
            else:
                onset = rel
                last_onset[vkey] = rel
                last_by_voice[it["voice"]] = rel
                rel += it["dur"]
            it["onset"] = onset
            m["items"].append(it)

        # ---- 本小节有多长 ----
        content_end = 0.0
        for it in m["items"]:
            content_end = max(content_end, it["onset"] + it["dur"])
        if measure_beats is None:
            length = max(content_end, 1.0)      # 全谱没写拍号：只能按内容走
        else:
            length = measure_beats
            # 弱起小节：不补空，也不挪 —— 首小节内容明显不足就按实际长度算。
            # 中途小节短了是 OMR 漏音，仍按拍号算（否则后面所有小节线都往里缩）
            if content_end > 0 and content_end < length - 1e-6 and \
                    (m["implicit"] or (first and content_end <= length * 0.75)):
                length = content_end
        m["length"] = length
        # 本小节没写 <time> 就继承前面生效的那个（voice 2 之类常常只在第一行写）
        m["time_sig"] = m["time_sig"] or cur_ts or time_sig
        m["fifths"] = fifths
        m["mode"] = mode

        measures.append(m)
        first = False
        idx += 1

    return {"measures": measures, "time_sig": time_sig,
            "fifths": fifths, "mode": mode,
            "transpose": (transpose_ch, transpose_oct),
            "grace_dropped": grace_dropped,
            "bad_divisions": bad_divisions, "ts_seen": ts_seen,
            "saw_transpose": bool(transpose_ch or transpose_oct)}


# ---------------------------------------------------------------- 出声


def _emit(measures, order, events, track=None):
    """按演奏顺序把小节里的音铺到全局时间轴上（含连音合并）。"""
    starts, _ = _sem.measure_starts(order, measures)
    pending = {}                 # (staff, voice, pitch) → (事件, 演奏位置)
    dropped = 0

    for pos, (mi, measure_start) in enumerate(zip(order, starts)):
        m = measures[mi]
        measure_end = measure_start + m["length"]

        for it in m["items"]:
            if it["rest"] or it["pitch"] is None:
                continue                 # 休止：占时间，不发声
            onset = measure_start + it["onset"]
            dur = it["dur"]
            # 起在小节内的音不许拖过小节线（跨小节靠 tie）；OMR 已经把音
            # 排到小节线外的（forward 溢出的尾巴）就别再压成 0，留原样
            if onset < measure_end <= onset + dur:
                dur = measure_end - onset
            dur = max(0.02, dur)          # 0 时值的音给个最小值，别变哑音

            key = (it["staff"], it["voice"], it["pitch"])
            got = pending.get(key)
            if it["tie_stop"] and got is not None:
                prev, prev_pos = got
                # 只在"本小节 / 紧邻的下一小节次"之间接。反复把同一段弹两遍时
                # 两个同音高相隔整段，不许被连成一个跨段的长音（时值累加会爆）
                if pos - prev_pos <= 1:
                    prev["dur"] += dur          # A→A→A 一路累加（旧行为）
                    if it["tie_start"]:
                        pending[key] = (prev, pos)
                    else:
                        pending.pop(key, None)
                    continue
                dropped += 1
                pending.pop(key, None)

            # 断链（只有 stop 没 start）也照常发声，别丢
            e = {"start": onset, "dur": dur, "pitch": it["pitch"],
                 "vel": it["vel"]}
            if track is not None:
                e["voice"] = it["voice"]
                e["staff"] = it["staff"]
                e["measure"] = m["number"]
            events.append(e)
            if it["tie_start"]:
                pending[key] = (e, pos)
    return dropped


# ---------------------------------------------------------------- 入口


@dataclass
class ParsedScore:
    """`parse_full()` 的结果。`parse()` 只取前四项，保持旧签名。"""
    events: list = field(default_factory=list)
    tempo: int = 100
    time_sig: str = "4/4"
    key: str = "C"
    mode: str = "major"
    key_spelled: str = "C"
    tempo_map: list = field(default_factory=lambda: [(0.0, 100.0)])
    total_beats: float = 0.0
    parts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # 谱子里到底写没写速度。OMR 产物一份都没有 → 全是这里兜的 100，
    # 界面上要能照实说"这个速度是兜底的"，不能让人以为是谱子写的。
    tempo_found: bool = False
    # 一个可发声的音都没有（OMR 认不出音符时的空壳谱）。`warnings` 里也有说明，
    # 这里给个能直接判的布尔，省得调用方去翻字符串。
    empty: bool = False

    def to_seconds(self, tempo_map=None):
        """拍 → 秒。给 `tempo_map` 就用分段速度，不给就用全局单一速度。"""
        tm = tempo_map if tempo_map is not None else self.tempo_map
        return _sem.beats_to_seconds(self.events, tm)

    def describe_tempo(self):
        return _sem.describe_tempo_map(self.tempo_map, self.total_beats)


def _dominant_key(measures, fallback=(0, "major")):
    """挑"覆盖音符最多"的调号。单一调号的谱子必然等于开头那个。

    谱子中途转调时，拿第一个调号去折音域/吸附会让后半段整段错位；
    按音符数加权取多数调，才是"这首曲子听起来是什么调"。
    """
    tally = {}
    order = []
    for m in measures:
        k = (m.get("fifths", 0), m.get("mode", "major"))
        if k not in tally:
            tally[k] = 0
            order.append(k)
        tally[k] += sum(1 for it in m["items"]
                        if it["pitch"] is not None and not it["rest"])
    if not tally:
        return fallback, False
    best = max(order, key=lambda k: (tally[k], -order.index(k)))
    modulated = sum(1 for k in order if tally[k] > 0) > 1
    if tally[best] == 0:                      # 全曲没音（空谱）
        return order[0], False
    return best, modulated


def parse_full(path, expand=True):
    """完整解析：反复展开 + 速度分段表 + 调号/移调。

    `expand=False` 时退回"源顺序直排"（对账用：看谱面本身有几个音）。
    """
    root = _load(path)

    part_els = [p for p in root.iter() if _tag(p) == "part"]
    # 全局拍号先扫一遍：某个 part 的第 1 小节没写 <time> 时要能接上别的 part 的
    default_beats = None
    default_ts = None
    for pe in part_els:
        for measure in pe:
            if _tag(measure) != "measure":
                continue
            for el in measure:
                if _tag(el) == "attributes":
                    for c in el:
                        if _tag(c) == "time":
                            got = _read_time(c)
                            if got:
                                default_beats, default_ts = got[1], got[0]
                                break
            break
        if default_beats is not None:
            break

    events = []
    parts = []
    warnings = []
    global_ts = default_ts
    ref = None                    # 参考 part（判全局调性）
    total_beats = 0.0

    for pi, pe in enumerate(part_els):
        data = _collect_part(pe, default_beats)
        measures = data["measures"]
        order = (_sem.performance_order(measures) if expand
                 else list(range(len(measures))))
        _, part_beats = _sem.measure_starts(order, measures)

        dropped = _emit(measures, order, events, track=None)
        tmap = _sem.build_tempo_map(order, measures, 100.0)
        parts.append({"index": pi, "measures": measures, "order": order,
                      "beats": part_beats, "tempo_map": tmap,
                      "transpose": data["transpose"],
                      "fifths": data["fifths"], "mode": data["mode"],
                      "extended": len(order) != len(measures)})
        total_beats = max(total_beats, part_beats)
        if global_ts is None and data["time_sig"]:
            global_ts = data["time_sig"]
        if data["grace_dropped"]:
            warnings.append(f"part{pi+1}: 丢掉 {data['grace_dropped']} 个装饰音"
                            "（不占时值）")
        if data.get("bad_divisions"):
            warnings.append(
                f"part{pi+1}: {data['bad_divisions']} 处 <divisions> 是 0 或非数字，"
                "已按 1 解释 —— 这种情况下的时值可能整体错几倍，"
                "谱面本身有问题（导出它的软件/OMR 没写对单位）")
        ts_seen = data.get("ts_seen") or []
        if len(ts_seen) > 1:
            warnings.append(
                f"part{pi+1}: 中途变拍号（共 {len(ts_seen)} 个）—— "
                + " → ".join(f"第{n}小节 {ts}" for n, ts in ts_seen[:6])
                + ("…" if len(ts_seen) > 6 else "")
                + "，各小节按各自拍号算长度，全局 time_sig 只代表开头")
        if dropped:
            warnings.append(f"part{pi+1}: {dropped} 个连音的 stop 找不到可接的"
                            "起点（当普通音发了）")
        if len(order) != len(measures):
            warnings.append(f"part{pi+1}: 反复展开 {len(measures)} → {len(order)} 小节")
        # 参考 part：优先"不移调"的那个（钢琴/人声），全移调乐器才退回第一个
        if ref is None or (ref["transpose"] != (0, 0) and
                           data["transpose"] == (0, 0)):
            ref = {"part": pi, "measures": measures,
                   "fifths": data["fifths"], "mode": data["mode"],
                   "transpose": data["transpose"]}

    # 全局速度 = 演奏顺序里第一个变速点（不是源文件里第一个）
    tmap = _sem.build_tempo_map(
        max(parts, key=lambda p: p["beats"])["order"] if parts else [],
        max(parts, key=lambda p: p["beats"])["measures"] if parts else [],
        100.0) if parts else [(0.0, 100.0)]
    tempo = int(round(tmap[0][1]))

    # 全局调性：参考 part 的**主调**（按音符数加权）+ 它的移调量 → 实际发声调
    if ref is not None:
        (fifths, mode), modulated = _dominant_key(ref["measures"],
                                                 (ref["fifths"], ref["mode"]))
        key_name, spelled = _sem.fifths_to_key(fifths, mode)
        shift = ref["transpose"][0] + 12 * ref["transpose"][1]
        if shift:
            written = _sem.PC_NAMES[_sem.fifths_to_pc(fifths, mode)]
            key_name = _sem.transpose_key_name(key_name, shift)
            warnings.append(f"移调乐器：写谱 {spelled}（主音 {written}），"
                            f"实际发声 {shift:+d} 半音 → {key_name}")
        if modulated:
            warnings.append(f"中途转调：主调取音符最多的那个（{spelled}）")
    else:
        key_name, spelled, mode = "C", "C", "major"

    # 各 part 的反复展开不一样长 → 时间轴对不上，得让人知道
    if len(parts) > 1 and len({round(p["beats"], 4) for p in parts}) > 1:
        warnings.append("多个 part 展开后总长不一致："
                        + ", ".join(f"P{p['index']+1}={p['beats']:.1f}"
                                    for p in parts))

    # 同音高同起点的重复事件（OMR 会把同一个声部识别两遍）在这里不合并 ——
    # arrange 每个 8 分格会按音高去重，重复音到不了谱面；但 parse() 的数字
    # 要能如实反映"XML 里到底有多少个音"，合并了就没法对账。
    events.sort(key=lambda e: (e["start"], e["pitch"]))
    # 谱子里到底有没有写速度？OMR 产物一份都没有，全是这里兜的 100 —— 
    # 使用者有权知道"这个速度是我们瞎猜的"，否则会以为是谱子写的。
    tempo_found = bool(parts) and any(
        bool(m.get("tempos"))
        for p in parts for m in p["measures"])
    # 一个音都没有（只解析到属性/休止）：绝不能让调用方当"识别成功"。
    # OMR 认不出音符时最典型的产物就是这种空壳谱。
    if not events:
        warnings.append("这份谱里没有任何可发声的音符（只解析到属性/休止/装饰音）"
                        "—— 别当成识别成功的结果用")
    return ParsedScore(events=events, tempo=tempo,
                       time_sig=global_ts or "4/4", key=key_name,
                       mode=mode, key_spelled=spelled, tempo_map=tmap,
                       total_beats=total_beats, parts=parts,
                       warnings=warnings, tempo_found=tempo_found,
                       empty=not events)


def parse(path, expand=True):
    """返回 (events, tempo, time_sig, key)，events 与其它后端同构。

    `start` / `dur` 单位是**四分音符 = 1 拍**（不是秒）。
    需要速度分段表 / 调式 / 演奏顺序时用 `parse_full()`。
    """
    got = parse_full(path, expand=expand)
    return got.events, got.tempo, got.time_sig, got.key


def _parse_part(part, events, default_beats=None):
    """兼容旧调用（`_report.py` 这类诊断脚本直接用）。"""
    data = _collect_part(part, default_beats)
    order = _sem.performance_order(data["measures"])
    _emit(data["measures"], order, events)
    return events


def to_seconds(events, tempo, tempo_map=None):
    """把 parse() 出来的「拍」换算成「秒」。

    **这一步不能省**：`arrange()` 的格子是按秒算的（slot = 60/tempo/2），
    而其它输入源（transcribe / midifile）给的都是秒。
    直接把拍喂进去，小节线不会落在强拍上，音也会被网格抖散。
    实测《雨爱》60 拍应按 120 组出谱，直接喂会出 208 组。

    `tempo_map=[(起始拍, BPM), ...]` 给定时按分段速度积分，用于中途变速的谱子；
    不给就退回单一 `tempo`（旧行为，signature 向后兼容）。
    """
    if tempo_map:
        return _sem.beats_to_seconds(events, tempo_map)
    k = 60.0 / float(tempo or 100)
    return [dict(e, start=e["start"] * k, dur=e["dur"] * k) for e in events]


def to_smc(path, key=None, tempo=None, time_sig=None, title="", **kw):
    from . import arrange
    got = parse_full(path)
    use_tempo = tempo or got.tempo
    return arrange.arrange(got.to_seconds(), tempo=use_tempo,
                           time_sig=time_sig or got.time_sig,
                           key=key or got.key, title=title, **kw)
