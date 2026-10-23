# -*- coding: utf-8 -*-
"""用 music21 生成"真值" fixture（MusicXML）—— 测试用的独立来源。

为什么不用手搓 XML 当全部真值：手搓的 XML 只能说"我按我的理解写了"，是**自证**。
music21 是一个独立实现的记谱库，它导出的 MusicXML 是第三方产物，拿它当输入
才能证明"通用 MusicXML 长这样时，我们的解析对不对"。

    python -m sky15.tests.make_fixtures      # 重新生成（一般不用，fixture 已入库）

生成物（sky15/tests/fixtures/）：
  fx_repeat.musicxml      |: … :| + 中途变速（music21 导出，含 <repeat>）
  fx_tempo.musicxml       三段速度：♩=60 → 附点二分=40 → 八分=240
  fx_keys.musicxml        E 大调 → 中途 A 小调（<mode>minor</mode>）
  fx_pickup.musicxml      弱起小节（music21 会标 implicit="yes"）
  fx_two_parts.musicxml   双谱表双声部（钢琴式）
  fx_volta.musicxml       第一/第二结尾（手写，music21 不能可靠导出 <ending>）
  fx_dc_fine.musicxml     D.C. al Fine（手写，同上）
  fx_ds_coda.musicxml     D.S. al Coda（手写，同上）
  fx_transpose.musicxml   降 B 单簧管 <transpose>（手写）
  fx_8vb.musicxml         低八度谱号 <clef-octave-change>（手写）
"""
import pathlib

HERE = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- music21 部分


def _mk(measures, ts="4/4", keys=("E",), tempo_marks=None, title="fx"):
    """measures: [[(pitch, quarterLength), ...], ...]"""
    from music21 import stream, note, meter, tempo, key
    s = stream.Score()
    p = stream.Part()
    p.insert(0, meter.TimeSignature(ts))
    for i, k in enumerate(keys):
        p.insert(0, key.Key(k))
    for i, pitches in enumerate(measures):
        m = stream.Measure(number=i + 1)
        for j, item in enumerate(pitches):
            if isinstance(item, tuple) and item[0] == "rest":
                m.append(note.Rest(quarterLength=item[1]))
            else:
                pitch, ql = item
                m.append(note.Note(pitch, quarterLength=ql))
        p.append(m)
    if tempo_marks:
        for mi, num, unit in tempo_marks:
            mm = tempo.MetronomeMark(number=num)
            if unit:
                mm.referent = None      # 直接给四分音符口径
            p.measure(mi + 1).insert(0, mm)
    s.insert(0, p)
    s.metadata = None
    return s


def gen_repeat():
    """4 小节：第 1-2 小节反复，第 3 小节变速，第 4 小节收尾。

    按谱弹一遍： 小节 1 2 1 2 3 4   → 共 6 个
    第 3 小节（源 index 2）在演奏顺序里排第 5 个位置，起点 = 4×4 = 16 拍。
    """
    from music21 import bar
    s = _mk([[("E4", 1)] * 4, [("F#4", 1)] * 4,
             [("G#4", 1)] * 4, [("A4", 1)] * 4],
            tempo_marks=[(0, 60, False), (2, 120, False)])
    p = s.parts[0]
    p.measure(1).leftBarline = bar.Repeat(direction="start")
    p.measure(2).rightBarline = bar.Repeat(direction="end")
    return s


def gen_tempo():
    """♩=60 起，第 3 小节附点四分=40（→ 60 四分/分），第 5 小节八分=240（→120）。

    故意用不同的 beat-unit：解析器必须把 <metronome> 折算成四分音符 BPM，
    不能拿 <per-minute> 直接当速度。
    """
    from music21 import tempo
    s = _mk([[("G#4", 4)]] * 6, tempo_marks=[(0, 60, False)])
    p = s.parts[0]
    mm = tempo.MetronomeMark(number=40, referent=1.5)     # 附点四分
    p.measure(3).insert(0, mm)
    mm2 = tempo.MetronomeMark(number=240, referent=0.5)   # 八分
    p.measure(5).insert(0, mm2)
    return s


def gen_keys():
    """E 大调 2 小节 → A 小调 2 小节。"""
    from music21 import key
    s = _mk([[("E4", 4)], [("B4", 4)], [("A4", 4)], [("E4", 4)]], keys=("E",))
    s.parts[0].measure(3).insert(0, key.Key("a"))
    return s


def gen_pickup():
    """弱起：首小节只有 1 拍（music21 会写 implicit="yes"）。"""
    from music21 import meter
    s = _mk([[("E4", 1)], [("F#4", 4)], [("G#4", 4)]])
    s.parts[0].measure(1).padAsAnacrusis()      # 标成弱起
    return s


def gen_two_parts():
    """双谱表：上声部旋律 + 下声部低音，两小节。"""
    from music21 import stream, note, meter, clef, key
    s = stream.Score()
    for name, pitches, c in (("RH", ["E5", "D5", "C5", "B4"], "G"),
                             ("LH", ["E3", "B2", "C3", "G2"], "F")):
        p = stream.Part()
        p.partName = name
        p.insert(0, meter.TimeSignature("4/4"))
        p.insert(0, key.Key("C"))
        p.insert(0, clef.TrebleClef() if c == "G" else clef.BassClef())
        m1 = stream.Measure(number=1)
        m2 = stream.Measure(number=2)
        for j, pt in enumerate(pitches):
            (m1 if j < 2 else m2).append(note.Note(pt, quarterLength=2))
        p.append(m1)
        p.append(m2)
        s.insert(0, p)
    s.metadata = None
    return s


# ---------------------------------------------------------------- 手写部分（music21 导出不可靠）

_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
         '<score-partwise version="3.1">\n'
         '  <part-list><score-part id="P1"><part-name>P1</part-name></score-part>'
         '</part-list>\n  <part id="P1">\n')
_TAIL = "  </part>\n</score-partwise>\n"


def _n(step, octv, dur, typ="quarter", alter=0, extra=""):
    alt = f"<alter>{alter}</alter>" if alter else ""
    return (f"<note><pitch><step>{step}</step>{alt}<octave>{octv}</octave>"
            f"</pitch><duration>{dur}</duration><type>{typ}</type>{extra}</note>")


FX_VOLTA = _HEAD + """    <measure number="1">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <barline location="left"><repeat direction="forward"/></barline>
      <sound tempo="90"/>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="3">
      <barline location="left"><ending number="1" type="start"/></barline>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
      <barline location="right"><ending number="1" type="stop"/>
        <repeat direction="backward"/></barline>
    </measure>
    <measure number="4">
      <barline location="left"><ending number="2" type="start"/></barline>
      <note><pitch><step>F</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
      <barline location="right"><ending number="2" type="stop"/></barline>
    </measure>
    <measure number="5">
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
""" + _TAIL

# D.C. al Fine：第 1 小节挂 Fine，第 3 小节挂 D.C.（弹完第 3 小节回开头，
# 一直到 Fine 那小节收）。实际演奏：1 2 3 1
FX_DC_FINE = _HEAD + """    <measure number="1">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <sound tempo="100" fine="yes"/>
      <direction><direction-type><words>Fine</words></direction-type></direction>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="3">
      <direction><direction-type><words>D.C. al Fine</words></direction-type>
        <sound dacapo="yes"/></direction>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="4">
      <note><pitch><step>F</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
""" + _TAIL

# D.S. al Coda：第 2 小节 Segno，第 4 小节 D.S. + To Coda，第 5 小节是 Coda。
# 实际演奏：1 2 3 4 2 3 (→To Coda) 5
FX_DS_CODA = _HEAD + """    <measure number="1">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <direction><direction-type><segno/></direction-type></direction>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="3">
      <direction><direction-type><words>To Coda</words></direction-type>
        <sound tocoda="coda"/></direction>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="4">
      <direction><direction-type><words>D.S. al Coda</words></direction-type>
        <sound dalsegno="segno1"/></direction>
      <note><pitch><step>F</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="5">
      <direction><direction-type><coda/></direction-type></direction>
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
""" + _TAIL

# 降 B 单簧管：写谱 C4 → 实际发声 Bb3（-2 半音）。
# 写谱调号 0（C 大调）→ 实际发声 Bb 大调。
FX_TRANSPOSE = _HEAD + """    <measure number="1">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <attributes><transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
""" + _TAIL

# 8vb 谱号：写谱 C5 标在低八度谱号里 → 实际发声 C4
FX_8VB = _HEAD + """    <measure number="1">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <attributes><clef number="1"><sign>G</sign><line>2</line>
        <clef-octave-change>-1</clef-octave-change></clef></attributes>
      <note><staff>1</staff><pitch><step>C</step><octave>5</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <note><staff>1</staff><pitch><step>E</step><octave>5</octave></pitch><duration>16</duration><type>whole</type></note>
    </measure>
""" + _TAIL


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    made = []
    for name, fn in (("fx_repeat", gen_repeat), ("fx_tempo", gen_tempo),
                     ("fx_keys", gen_keys), ("fx_pickup", gen_pickup),
                     ("fx_two_parts", gen_two_parts)):
        s = fn()
        fp = HERE / f"{name}.musicxml"
        s.write("musicxml", fp=str(fp))
        made.append((name, fp.stat().st_size))
    for name, text in (("fx_volta", FX_VOLTA), ("fx_dc_fine", FX_DC_FINE),
                       ("fx_ds_coda", FX_DS_CODA),
                       ("fx_transpose", FX_TRANSPOSE), ("fx_8vb", FX_8VB)):
        fp = HERE / f"{name}.musicxml"
        fp.write_text(text, encoding="utf-8")
        made.append((name, fp.stat().st_size))
    for name, size in made:
        print(f"  {name:16s} {size:7d} B")


if __name__ == "__main__":
    main()
