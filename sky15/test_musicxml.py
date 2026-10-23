# -*- coding: utf-8 -*-
"""MusicXML 解析的回归测试。

除了基础的拍号/调性/和弦，重点是**丢音**相关的坑（都见过真实 OMR 谱翻车）：
  1. 小节末尾给下个声部用的 `<backup>` 把下一小节的起点拽回小节头 → 整首被压扁
  2. `<backup>` 退过头 → 负时间
  3. 连音链 A→A→A 只合并了最后两段
  4. 同音高的普通音（另一个声部）把连音链打断
  5. 断链的 tie stop（只有 stop 没 start）被整颗丢掉
  6. `<chord/>` 用了别的声部的 last_onset（backup/forward 之后尤其明显）
  7. `<divisions>` 中途变化后，前面累计的位置被按新 divisions 重算
  8. 多 part 各有各的时间轴，小节必须对齐
  9. 弱起小节（implicit）不能留出一大段空白
 10. 越过小节线的音要截在小节线上
 11. 只写 `<notations><tied>` 不写 `<tie>` 的谱子也要能连
"""
import sys, os, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sky15 import musicxml, arrange, smc

HERE = pathlib.Path(__file__).parent / "tests"
SAMPLE = HERE / "sample.musicxml"


# ---------------------------------------------------------------- 拼 XML 的工具
# divisions 默认 4：四分音符 = 4，一拍 = 4 个 divisions

def N(p="C4", dur=4, voice=1, chord=False, rest=False, grace=False,
      tie=None, tied=None, staff=None):
    """一个 <note>。p 写成 C4 / F#4 / Bb3；tie/tied 可写 'start' / 'stop' / 'stop+start'。"""
    out = ["<note>"]
    if chord:
        out.append("<chord/>")
    if grace:
        out.append("<grace/>")
    if rest:
        out.append("<rest/>")
    elif p:
        step, mid, octv = p[0], p[1:-1], p[-1]
        inner = f"<step>{step}</step>"
        if mid == "#":
            inner += "<alter>1</alter>"
        elif mid == "b":
            inner += "<alter>-1</alter>"
        inner += f"<octave>{octv}</octave>"
        out.append(f"<pitch>{inner}</pitch>")
    if dur is not None and not grace:
        out.append(f"<duration>{dur}</duration>")
    if voice is not None:
        out.append(f"<voice>{voice}</voice>")
    if staff is not None:
        out.append(f"<staff>{staff}</staff>")
    for t in (tie or "").split("+") if tie else []:
        out.append(f'<tie type="{t}"/>')
    for t in (tied or "").split("+") if tied else []:
        out.append(f'<notations><tied type="{t}"/></notations>')
    out.append("</note>")
    return "".join(out)


def B(dur):
    return f"<backup><duration>{dur}</duration></backup>"


def F(dur):
    return f"<forward><duration>{dur}</duration></forward>"


def ATTR(divisions=4, beats="4", btype="4"):
    return (f"<attributes><divisions>{divisions}</divisions><key><fifths>0</fifths></key>"
            f"<time><beats>{beats}</beats><beat-type>{btype}</beat-type></time></attributes>")


def M(content, implicit=False):
    """一个小节：内容 + 是否弱起（implicit=yes）"""
    return (content, implicit)


def _doc(part_measures):
    """part_measures: [[小节, ...], ...]，一个子列表 = 一个 <part>；
    小节可以直接给字符串，也可以给 M(内容, implicit=True)"""
    parts, plist = [], []
    for i, ms in enumerate(part_measures):
        plist.append(f'<score-part id="P{i+1}"><part-name>P{i+1}</part-name></score-part>')
        body = []
        for j, m in enumerate(ms):
            content, implicit = m if isinstance(m, tuple) else (m, False)
            body.append(f'<measure number="{j+1}"'
                        + (' implicit="yes"' if implicit else "") +
                        ">" + content + "</measure>")
        parts.append(f'<part id="P{i+1}">' + "".join(body) + "</part>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<score-partwise version="3.1"><part-list>' + "".join(plist) +
            '</part-list>' + "".join(parts) + '</score-partwise>')


def parse_doc(*part_measures):
    text = _doc(list(part_measures))
    fd, p = tempfile.mkstemp(suffix=".musicxml")
    os.close(fd)
    pathlib.Path(p).write_text(text, encoding="utf-8")
    try:
        return musicxml.parse(p)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


def starts_of(ev, pitch=None):
    return sorted(round(e["start"], 4) for e in ev if pitch is None or e["pitch"] == pitch)


# ---------------------------------------------------------------- 原有基础用例


def t_parse_basic():
    ev, tempo, ts, key = musicxml.parse(SAMPLE)
    print(f"  音符 {len(ev)}  tempo {tempo}  ts {ts}  key {key}")

    assert tempo == 120, tempo
    assert ts == "4/4", ts
    assert key == "C", key

    # 时间轴（拍）：
    #   0.0 C4 八分 | 0.5 E4 + G4 和弦 | 1.0 休止 | 1.5 D4 附点四分 | 3.0 E4 四分
    #   第 2 小节从 4.0 开始
    by_start = {}
    for e in ev:
        by_start.setdefault(round(e["start"], 3), []).append(e["pitch"])

    assert by_start.get(0.0) == [60], by_start.get(0.0)
    assert sorted(by_start[0.5]) == [64, 67], by_start.get(0.5)
    assert round(by_start[1.5][0]) == 62, by_start.get(1.5)
    assert round(by_start[3.0][0]) == 64, by_start.get(3.0)

    # 附点四分 = 1.5 拍
    d4 = [e for e in ev if e["pitch"] == 62][0]
    assert abs(d4["dur"] - 1.5) < 1e-6, d4["dur"]


def t_tie_merges():
    """连音必须合并成一个长音：F4 四分 + 二分 = 3 拍"""
    ev, *_ = musicxml.parse(SAMPLE)
    fs = [e for e in ev if e["pitch"] == 65 and e["start"] < 6]
    assert fs, "没找到 F4"
    longest = max(fs, key=lambda e: e["dur"])
    assert abs(longest["dur"] - 3.0) < 1e-6, f"连音没合并：{longest['dur']}"


def t_accidental():
    """F#4 = 66"""
    ev, *_ = musicxml.parse(SAMPLE)
    assert any(e["pitch"] == 66 for e in ev), "变音记号丢了"


def t_grace_ignored():
    """装饰音不占时值：第 2 小节的 A4 不该出现在时间轴上"""
    ev, *_ = musicxml.parse(SAMPLE)
    assert not any(e["pitch"] == 69 for e in ev), "装饰音被当成实体音符了"


def t_voice_backup():
    """backup 之后 voice 2 的 C3 应回到第 2 小节开头（4.0 拍）"""
    ev, *_ = musicxml.parse(SAMPLE)
    c3 = [e for e in ev if e["pitch"] == 48]
    assert c3, "第二声部丢了"
    assert abs(c3[0]["start"] - 4.0) < 1e-6, c3[0]["start"]


def t_to_smc():
    out = musicxml.to_smc(SAMPLE, title="sample")
    meta = smc.parse_meta(out)
    assert meta["tempo"] == 120 and meta["key"] == "C"
    groups = smc.parse_groups(out)
    assert len(groups) >= 8, len(groups)
    print(f"  SMC: {len(groups)} 组  {out[:90]}...")


# ---------------------------------------------------------------- 丢音相关

def t_measure_boundary():
    """小节末尾「回到小节头」的 backup 不能把下一小节拽回来。

    真实 OMR 谱每个声部结束都会来一发 backup，照单全收的话第 3 小节会从
    4.0 拍开始（压在第 2 小节身上），越往后偏得越多。
    """
    ev, *_ = parse_doc([ATTR(4) +
                        N("C4", 4) + N("D4", 4) + N("E4", 4) + N("F4", 4),
                        N("G4", 4) + N("A4", 4) + N("B4", 4) + N("C5", 4) + B(16),
                        N("D5", 4) + N("E5", 4) + N("F5", 4) + N("G5", 4)])
    assert starts_of(ev) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], starts_of(ev)


def t_backup_underflow():
    """backup 退过头不许出现负时间，后面的音要落在小节头上。"""
    ev, *_ = parse_doc([ATTR(4) + N("C4", 4) + B(12) + N("D4", 4)])
    assert min(e["start"] for e in ev) >= 0, [e["start"] for e in ev]
    d4 = [e for e in ev if e["pitch"] == 62]
    assert d4 and abs(d4[0]["start"]) < 1e-6, [e["start"] for e in ev]


def t_tie_chain():
    """A→A→A：三段连音合成一个音，时值累加（1 + 2 + 1 = 4 拍）。"""
    ev, *_ = parse_doc([ATTR(4) + N("F4", 4, tie="start") +
                        N("F4", 8, tie="stop+start") +
                        N("F4", 4, tie="stop")])
    fs = [e for e in ev if e["pitch"] == 65]
    assert len(fs) == 1, f"连音链断了：{[(e['start'], e['dur']) for e in fs]}"
    assert abs(fs[0]["start"]) < 1e-6 and abs(fs[0]["dur"] - 4.0) < 1e-6, fs[0]


def t_tie_other_voice_same_pitch():
    """另一个声部弹同一个音高（非连音）不能把连音链打掉。"""
    ev, *_ = parse_doc([ATTR(4) + N("F4", 4, voice=1, tie="start") +
                        B(4) + F(4) + N("F4", 4, voice=2) +
                        N("F4", 4, voice=1, tie="stop")])
    fs = [e for e in ev if e["pitch"] == 65]
    assert len(fs) == 2, f"应该只剩 2 个 F4，实际 {len(fs)}：{[(e['start'], e['dur']) for e in fs]}"
    long = max(fs, key=lambda e: e["dur"])
    assert abs(long["start"]) < 1e-6 and abs(long["dur"] - 2.0) < 1e-6, long
    short = min(fs, key=lambda e: e["dur"])
    assert abs(short["start"] - 1.0) < 1e-6, short


def t_tie_broken_chain():
    """只有 tie stop 没有 start（OMR 常见）→ 当普通音发出来，不许丢。"""
    ev, *_ = parse_doc([ATTR(4) + N("A4", 4, tie="stop")])
    a4 = [e for e in ev if e["pitch"] == 69]
    assert len(a4) == 1 and abs(a4[0]["start"]) < 1e-6, "断链的连音被吞了"


def t_tie_notations_only():
    """只写 <notations><tied>、不写 <tie> 的谱子也要合并。"""
    ev, *_ = parse_doc([ATTR(4) + N("C4", 4, tied="start") +
                        N("C4", 4, tied="stop")])
    cs = [e for e in ev if e["pitch"] == 60]
    assert len(cs) == 1 and abs(cs[0]["dur"] - 2.0) < 1e-6, cs


def t_chord_after_forward():
    """backup/forward 之后的和弦音要用当前声部的位置，不是上一个声部的。"""
    ev, *_ = parse_doc([ATTR(4) + N("C4", 4, voice=1) +
                        B(4) + F(8) + N("E4", 4, voice=2, chord=True)])
    e4 = [e for e in ev if e["pitch"] == 64]
    assert e4 and abs(e4[0]["start"] - 2.0) < 1e-6, \
        f"和弦音挂到了别人的位置上：{[e['start'] for e in e4]}"


def t_chord_does_not_advance():
    """<chord/> 不推进时间：三个音同一起点，下一个音在 1 拍后。"""
    ev, *_ = parse_doc([ATTR(4) + N("C4", 4) + N("E4", 4, chord=True) +
                        N("G4", 4, chord=True) + N("B4", 4)])
    assert starts_of(ev) == [0, 0, 0, 1], starts_of(ev)


def t_divisions_change():
    """<divisions> 中途从 4 变 8：前面小节的位置不能按新值重算。"""
    ev, *_ = parse_doc([
        ATTR(4) + "".join(N(p, 4) for p in ("C4", "D4", "E4", "F4")),
        ATTR(8) + "".join(N(p, 8) for p in ("G4", "A4", "B4", "C5")),
        "".join(N("D5", 8)),
    ])
    assert starts_of(ev) == [0, 1, 2, 3, 4, 5, 6, 7, 8], starts_of(ev)
    assert all(abs(e["dur"] - 1.0) < 1e-6 for e in ev), [e["dur"] for e in ev]


def t_multipart_aligned():
    """两个 part 各自从 0 起，小节线必须对齐（不能因为 backup 各走各的）。"""
    p1 = [ATTR(4) + N("C5", 4) + N("D5", 4) + N("E5", 4) + N("F5", 4),
          N("G5", 16)]
    p2 = [ATTR(4) + N("C3", 16) + B(16),
          N("G3", 16)]
    ev, *_ = parse_doc(p1, p2)
    assert len(ev) == 7, [(e["pitch"], e["start"]) for e in ev]
    assert starts_of(ev, 79) == [4.0], starts_of(ev, 79)      # G5 在第 2 小节
    assert starts_of(ev, 55) == [4.0], starts_of(ev, 55)      # G3 也在第 2 小节
    assert starts_of(ev, 48) == [0.0], starts_of(ev, 48)      # C3 从 0 起


def t_pickup_measure():
    """弱起小节：implicit=yes 时下一小节紧跟内容，不按整小节留白。"""
    ev, *_ = parse_doc([M(ATTR(4) + N("G4", 4), implicit=True),   # 只有一拍
                        N("C5", 16)])
    assert starts_of(ev) == [0, 1], starts_of(ev)
    assert abs([e for e in ev if e["pitch"] == 72][0]["start"] - 1.0) < 1e-6


def t_barline_clamp():
    """跨小节线的长音在小节线处截断（4 拍小节里的 6 拍音）。"""
    ev, *_ = parse_doc([ATTR(4) + N("C4", 24)])
    c4 = [e for e in ev if e["pitch"] == 60]
    assert c4 and abs(c4[0]["dur"] - 4.0) < 1e-6, c4


def t_rest_and_grace_timing():
    """休止推进时间、装饰音不推进：0 休止(2) 0.5 音 … 装饰音后仍是 1.0。"""
    ev, *_ = parse_doc([ATTR(4) + N(rest=True, dur=2) + N("C4", 2) +
                        N("D4", grace=True, dur=None) + N("E4", 4)])
    assert starts_of(ev) == [0.5, 1.0], starts_of(ev)


def t_malformed_note():
    """没有 <duration> 也没有 <type> 的音：留着（不丢），时值给最小值。"""
    ev, *_ = parse_doc([ATTR(4) + N(dur=None) + N(rest=True, dur=4) + N("C4", 4)])
    assert len(ev) == 2, [(e["pitch"], e["start"]) for e in ev]
    assert starts_of(ev) == [0, 1], starts_of(ev)
    assert all(e["dur"] > 0 for e in ev), [e["dur"] for e in ev]


def t_empty_part():
    """空 part / 空小节不许炸。"""
    ev, tempo, ts, key = parse_doc([ATTR(4)], [""])
    assert ev == [] and ts == "4/4" and key == "C"


def t_no_time_signature():
    """谱子里没写拍号：只能按内容长度切小节，别每小节补一段静音。"""
    a = "<attributes><divisions>4</divisions></attributes>"
    ev, *_ = parse_doc([a + N("C4", 8), N("D4", 8)])
    assert starts_of(ev) == [0, 2], starts_of(ev)


# ---------------------------------------------------------------- 谱面语义（新）
# fixture 由 tests\make_fixtures.py 用 music21 生成 + 手写补足 music21 不出的记号：
#   fx_repeat  fx_volta  fx_dc_fine  fx_ds_coda  fx_tempo  fx_pickup
#   fx_transpose  fx_8vb  fx_keys  fx_two_parts
FIX = HERE / "fixtures"
REAL = pathlib.Path(__file__).parent / "out"


def _fx(name):
    return musicxml.parse_full(FIX / (name + ".musicxml"))


def t_repeat_expanded():
    """|: ... :| —— 演奏顺序 [0,1,0,1,2,3]，音高序列必须真的过两遍。"""
    got = _fx("fx_repeat")
    order = got.parts[0]["order"]
    assert order == [0, 1, 0, 1, 2, 3], order
    assert abs(got.total_beats - 24) < 1e-6, got.total_beats
    # 每小节 4 个同音高的四分音符：bar0 E4 / bar1 F#4 / bar2 G#4 / bar3 A4
    seq = [e["pitch"] for e in got.events]
    m0, m1 = [64] * 4, [66] * 4
    assert seq[:8] == m0 + m1, seq[:8]
    assert seq[8:16] == m0 + m1, seq[8:16]       # 第二遍：bar0 bar1
    assert seq[16:] == [68] * 4 + [69] * 4, seq[16:]
    assert starts_of(got.events)[:4] == [0.0, 1.0, 2.0, 3.0], starts_of(got.events)[:4]
    assert starts_of(got.events)[8:12] == [8.0, 9.0, 10.0, 11.0], starts_of(got.events)[8:12]


def t_volta_expanded():
    """第一/第二结尾：第一遍走结尾 1，第二遍跳过它走结尾 2。"""
    got = _fx("fx_volta")
    order = got.parts[0]["order"]
    assert order == [0, 1, 2, 0, 1, 3, 4], order
    assert abs(got.total_beats - 28) < 1e-6, got.total_beats
    from sky15 import score_semantics as sem
    starts = sem.measure_starts(order, got.parts[0]["measures"])[0]
    assert starts == [0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0], starts
    # 结尾 1 里的小节只出现一次（位置 2），结尾 2 只出现一次（位置 5）
    assert order.count(2) == 1 and order.count(3) == 1, order


def t_dc_fine():
    """D.C. al Fine：回到开头，弹到 Fine 停 —— 不是弹完整首。"""
    got = _fx("fx_dc_fine")
    assert got.parts[0]["order"] == [0, 1, 2, 0], got.parts[0]["order"]
    assert abs(got.total_beats - 16) < 1e-6, got.total_beats


def t_ds_coda():
    """D.S. al Coda：回到 Segno，到 To Coda 跳 Coda。"""
    got = _fx("fx_ds_coda")
    assert got.parts[0]["order"] == [0, 1, 2, 3, 1, 2, 4], got.parts[0]["order"]
    assert abs(got.total_beats - 28) < 1e-6, got.total_beats


def t_tempo_map():
    """速度分段表：♩=60 → (附点四分=40 ⇒ ♩=60) → (八分=240 ⇒ ♩=120)。

    只记录**变化**：第 3 小节那个 40 折算出来还是 60，不能切成新段。
    """
    got = _fx("fx_tempo")
    tm = got.tempo_map
    assert tm == [(0.0, 60.0), (16.0, 120.0)], tm
    assert got.tempo == 60, got.tempo
    # 前 16 拍 @60 = 16 秒；后 8 拍 @120 = 4 秒 —— 全曲 24 拍，共 20 秒
    assert abs(got.total_beats - 24) < 1e-6, got.total_beats
    secs = got.to_seconds()
    end = max(e["start"] + e["dur"] for e in secs)
    assert abs(end - 20.0) < 1e-6, end
    # 分段积秒不是"总拍数 ÷ 单一速度"（24 拍全按 120 只有 12 秒，全按 60 是 24 秒）
    assert abs(24 * 60.0 / 120 - 12.0) < 1e-9
    # 第 5 小节（120 段）第一个音：起始 16.0 秒，全音符 4 拍 = 2.0 秒
    mid = [e for e in secs if abs(e["start"] - 16.0) < 1e-6]
    assert mid and abs(mid[0]["dur"] - 2.0) < 1e-6, mid
    from sky15 import score_semantics as sem
    assert abs(sem.seconds_at(16.0, tm) - 16.0) < 1e-9
    assert abs(sem.seconds_at(24.0, tm) - 20.0) < 1e-9


def t_to_seconds_compat():
    """老的两参数调用方式必须继续按单一速度算（cli.py / web\\server.py 在用）。"""
    ev = [{"pitch": 60, "start": 0.0, "dur": 4.0}]
    out = musicxml.to_seconds(ev, 120)
    assert abs(out[0]["start"]) < 1e-9 and abs(out[0]["dur"] - 2.0) < 1e-9, out
    out2 = musicxml.to_seconds(ev, 120, tempo_map=[(0.0, 60.0)])
    assert abs(out2[0]["dur"] - 4.0) < 1e-9, out2


def t_pickup_no_pad():
    """弱起：首小节只有 1 拍，后面照常从第 1 拍接上，不补空也不整体挪。"""
    got = _fx("fx_pickup")
    assert abs(got.parts[0]["measures"][0]["length"] - 1.0) < 1e-6, \
        got.parts[0]["measures"][0]["length"]
    assert starts_of(got.events) == [0.0, 1.0, 5.0], starts_of(got.events)
    assert abs(got.total_beats - 9) < 1e-6, got.total_beats


def t_quote_off_coda():
    """"To Coda / al Coda" 是**跳转**不是落点，落点只有单独的 <coda/>。

    混用会让 D.S. 之后第一跳就落回自己（实测 7 小节膨胀成 9 小节）。
    """
    from sky15.musicxml import _read_words
    m = {}
    _read_words("To Coda", m)
    assert m.get("tocoda") and not m.get("coda"), m
    m = {}
    _read_words("D.S. al Coda", m)
    assert m.get("dalsegno") and m.get("tocoda") and not m.get("coda"), m
    m = {}
    _read_words("D.C. al Fine", m)
    assert m.get("dacapo") and m.get("fine") and not m.get("tocoda"), m


def t_transpose():
    """降 B 单簧管：写谱 C4/G4 → 实际发声 Bb3/F4；调性 C → Bb。"""
    got = _fx("fx_transpose")
    assert [(e["pitch"], e["start"]) for e in got.events] == [(58, 0.0), (65, 4.0)], \
        [(e["pitch"], e["start"]) for e in got.events]
    assert got.key == "Bb", got.key
    assert got.key_spelled == "C", got.key_spelled
    assert any("移调" in w for w in got.warnings), got.warnings


def t_8vb():
    """<clef-octave-change>-1</clef-octave-change>：写 C5/E5 实际发 C4/E4。"""
    got = _fx("fx_8vb")
    assert [(e["pitch"], e["start"]) for e in got.events] == [(60, 0.0), (64, 4.0)], \
        [(e["pitch"], e["start"]) for e in got.events]


def t_minor_mode():
    """同名的 0 个升降号：大调是 C，小调是 Am —— 不能都报 C。"""
    body = ('<attributes><divisions>4</divisions><key><fifths>0</fifths>'
            '<mode>minor</mode></key><time><beats>4</beats><beat-type>4</beat-type>'
            '</time></attributes>' + N("A4", 16))
    got = musicxml.parse_full(_write_tmp(_doc([[body]])))
    assert got.mode == "minor", got.mode
    assert got.key == "A", got.key
    assert got.key_spelled == "Am", got.key_spelled


def t_key_extremes():
    """7 个降号 = Cb 大调 = 实际 B 大调；7 个升号 = C# 大调 = 实际 Db。"""
    from sky15 import score_semantics as sem
    assert sem.fifths_to_key(-7)[0] == "B", sem.fifths_to_key(-7)
    assert sem.fifths_to_key(-7)[1] == "Cb", sem.fifths_to_key(-7)
    assert sem.fifths_to_key(7)[0] == "Db", sem.fifths_to_key(7)
    assert sem.fifths_to_key(7)[1] == "C#", sem.fifths_to_key(7)
    assert sem.fifths_to_key(3, "minor")[1] == "F#m", sem.fifths_to_key(3, "minor")
    # 返回的调名必须能直接被 arrange 用（layout.KEYS 里没有 Cb / C#）
    from sky15 import layout
    for f in range(-7, 8):
        for mode in ("major", "minor"):
            name = sem.fifths_to_key(f, mode)[0]
            assert name in layout.KEY_MIDI, (f, mode, name)


def t_two_parts_two_voices():
    """两个 part（左右手）+ 双向 voice：起点必须对齐，不能被 backup 拽歪。"""
    got = _fx("fx_two_parts")
    assert len(got.parts) == 2, len(got.parts)
    assert abs(got.parts[0]["measures"][1]["length"] - 4.0) < 1e-6
    sim = {}
    for e in got.events:
        sim[round(e["start"], 4)] = sim.get(round(e["start"], 4), 0) + 1
    assert max(sim.values()) == 2, sim
    assert sorted(sim) == [0.0, 2.0, 4.0, 6.0], sorted(sim)


def t_real_omr_samples():
    """真实 OMR 产物：数字是钉死的，改解析器要能立刻看出回归。

    雨爱：15 小节 4/4，E 大调，299 个事件，98.3% 音在调内（OMR 只错在别处）。
    Dichterliebe：2/4，A 大调，带 implicit 弱起（0.25 拍）。
    """
    y = REAL / "omr2" / "雨爱_五线谱.mxl"
    if not y.exists():
        print("  (跳过：没有 out/omr2/雨爱_五线谱.mxl)")
        return
    got = musicxml.parse_full(y)
    assert len(got.events) == 299, len(got.events)
    assert got.key == "E", got.key
    assert got.time_sig == "4/4", got.time_sig
    assert len(got.parts[0]["order"]) == 15, len(got.parts[0]["order"])
    from sky15.out.agent_score.probe import in_key_ratio
    r = in_key_ratio(got.events, got.key)
    assert r >= 0.97, r
    d = REAL / "omr" / "Dichterliebe01-1.mxl"
    if d.exists():
        g2 = musicxml.parse_full(d)
        assert len(g2.events) == 162, len(g2.events)
        assert g2.time_sig == "2/4", g2.time_sig
        assert abs(g2.parts[0]["measures"][0]["length"] - 0.25) < 1e-6
        assert abs(g2.parts[0]["measures"][1]["length"] - 2.0) < 1e-6


def _write_tmp(text):
    fd, p = tempfile.mkstemp(suffix=".musicxml")
    os.close(fd)
    pathlib.Path(p).write_text(text, encoding="utf-8")
    return p


def test_all():
    """给 pytest 用：没装 pytest 时走 main()，装了也不会漏跑。"""
    for name, fn in _TESTS:
        fn()


_TESTS = [("基础解析", t_parse_basic), ("连音合并", t_tie_merges),
             ("变音记号", t_accidental), ("装饰音忽略", t_grace_ignored),
             ("多声部 backup", t_voice_backup), ("出 SMC", t_to_smc),
             ("小节硬边界", t_measure_boundary), ("backup 退过头", t_backup_underflow),
             ("连音链 A→A→A", t_tie_chain),
             ("同音高别声部不打链", t_tie_other_voice_same_pitch),
             ("断链 tie 不丢", t_tie_broken_chain),
             ("只写 tied 也连", t_tie_notations_only),
             ("和弦用本声部位置", t_chord_after_forward),
             ("和弦不推进时间", t_chord_does_not_advance),
             ("divisions 中途变", t_divisions_change),
             ("多 part 对齐", t_multipart_aligned),
             ("弱起小节", t_pickup_measure),
             ("越界音截断", t_barline_clamp),
             ("休止/装饰音时值", t_rest_and_grace_timing),
             ("残缺输入不炸", t_malformed_note),
             ("空 part 不炸", t_empty_part),
             ("没写拍号", t_no_time_signature),
             # ---- 谱面语义 ----
             ("反复 |: :|", t_repeat_expanded),
             ("第一/第二结尾", t_volta_expanded),
             ("D.C. al Fine", t_dc_fine),
             ("D.S. al Coda", t_ds_coda),
             ("速度分段表", t_tempo_map),
             ("to_seconds 老签名", t_to_seconds_compat),
             ("弱起不补空", t_pickup_no_pad),
             ("ToCoda 不是落点", t_quote_off_coda),
             ("移调乐器", t_transpose),
             ("8vb 谱号", t_8vb),
             ("小调 mode", t_minor_mode),
             ("调号极值", t_key_extremes),
             ("双 part 双 voice", t_two_parts_two_voices),
             ("真实 OMR 样本", t_real_omr_samples)]


def main():
    tests = _TESTS
    ok = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL  {name}  -> {e}")
        except Exception as e:
            print(f"  ERR   {name}  -> {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(tests)} 通过")
    return ok == len(tests)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
