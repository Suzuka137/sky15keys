# -*- coding: utf-8 -*-
"""调性识别 + 逐小节和弦标注（music21 后端）。

为什么要它
----------
`arrange._bass_chord` 配左手是**不看调性的局部启发式**：拿这一格最低的音当
根音，再找五度、三度。钢琴谱左手是琶音，"这一格最低的音"常常是经过音、
七音的转位，不是真正的和弦根音 —— 于是左手配出来的和弦跟右手旋律打架。

这里用 music21 先把整首曲子的**调**判出来（Krumhansl-Schmuckler 调性轮廓），
再按小节把同时发声的音交给 `stream.chordify()` 合成"每小节的实响和声"，
然后给每小节标一个和弦（根音 pitch class + 性质 + 置信度）。`arrange` 的
`lh_m21` 变体据此配左手：**根音 → 五度 → 三度**，置信度不够就退回老逻辑。

两个对外函数
------------
    analyze_key(events, tempo=None, time_sig="4/4") -> "E"
        返回光遇 15 键能用的调名（`layout.KEYS` 里的一个，永远是大调名）。
        music21 判成小调时换同名关系大调（Am → C，C#m → E）：15 键布局本身
        就是"某调的大调音阶铺两个八度"，没有小调键位，小调只能借关系大调。

    chords_per_bar(events, tempo, bars) -> [dict, ...]
        每小节一个 dict：
            bar        小节序号（0 起）
            root_pc    根音 pitch class（0-11，C=0）；空小节为 None
            root       根音名（"E"/"F#"…）
            quality    "maj"/"min"/"dim"/"aug"/"maj7"/"min7"/"7"/"" ...
            name       music21 给的和弦记法（"E", "C-", "G7"…）
            confidence 0~1，该小节发声时间里落在和弦音上的比例
            weights    该小节的 pitch class 时长权重（调试用）

时间单位约定
------------
`events` 与 `arrange` 一致：`start`/`dur` 单位是**秒**，`pitch` 是 MIDI。
内部按 `tempo` 换成 quarterLength（拍）。"小节"沿用本站规则：**固定 8 个
8 分格 = 4 拍**（与 `arrange` 的 spb=8 一致，跟真实拍号无关）。
"""
import collections

__all__ = ["analyze_key", "analyze_key_detail", "chords_per_bar", "available"]

# 15 键音名 → pitch class
KEY_PC = {"C": 0, "Db": 1, "D": 2, "Eb": 3, "E": 4, "F": 5,
          "Gb": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}
PC_KEY = {v: k for k, v in KEY_PC.items()}

# 音级 → 弦名称偏好（跟 layout.KEYS 一致，用降号拼写）
PC_NAME_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
PC_NAME_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# 这些调的五线谱是按升号写的，和弦名也用升号（E 调里的 vi 是 C#m，不是 Dbm）
SHARP_KEYS = {"G", "D", "A", "E", "B", "F#", "C#"}
_SHARP_PCS = {KEY_PC[k] for k in SHARP_KEYS if k in KEY_PC}

# 性质 → 和弦名后缀（自己拼，比 music21 的 "-"/"--" 好读）
SUFFIX = {"maj": "", "min": "m", "dim": "dim", "aug": "aug",
          "sus4": "sus4", "7": "7", "maj7": "maj7", "min7": "m7"}

SPB = 8                # 一小节 8 个 8 分格（本站固定规则）
BAR_QUARTERS = SPB * 0.5  # = 4 拍

# 调性兜底：低音线取样窗口（秒）与"允许翻盘"的相关系数差距
BASS_WIN = 0.5
KEY_GAP_MAX = 0.30

# 候选和弦（相对根音的半音集合）。三音和弦优先，七和弦只在高覆盖时选。
CANDIDATES = [
    ("maj", (0, 4, 7), 0.0),
    ("min", (0, 3, 7), 0.0),
    ("dim", (0, 3, 6), -0.10),
    ("aug", (0, 4, 8), -0.10),
    ("sus4", (0, 5, 7), -0.18),
    ("7", (0, 4, 7, 10), -0.06),
    ("maj7", (0, 4, 7, 11), -0.10),
    ("min7", (0, 3, 7, 10), -0.10),
]


def _m21():
    """延迟导入 music21（没装时 arrange 的老路径仍要能跑）。"""
    import music21
    return music21


def available():
    try:
        _m21()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 调性


def _to_stream(events, tempo=None, ts="4/4"):
    """events（秒）→ music21 Stream。tempo=None 时按 60 BPM 处理（音级分布对
    时间单位不敏感，判调只需相对比例）。"""
    m21 = _m21()
    qsec = 60.0 / float(tempo or 60)
    s = m21.stream.Stream()
    try:
        s.append(m21.meter.TimeSignature(ts))       # 让分析器知道节拍重心
    except Exception:
        pass
    for e in events:
        p = int(round(e["pitch"]))
        if not (0 <= p <= 127):
            continue
        n = m21.note.Note(midi=p)
        n.quarterLength = max(0.05, float(e.get("dur", 0.5)) / qsec)
        s.insert(float(e["start"]) / qsec, n)
    return s


def _bass_line_weights(events, win=BASS_WIN):
    """低音线音级权重：每 `win` 秒取"此刻最低音的音级"，按窗口时长累加。

    为什么需要它：Krumhansl-Schmuckler 在"主音 vs 下属音"上系统性犯迷糊
    （A 大调的歌被判成 D 大调，Bb 大调被判成 Eb 大调 —— 两者的音阶集合
    只差一个音，轮廓相关性只差 0.05~0.19）。而**低音声部**是最硬的证据：
    主音在三和弦低音上的累计厚度远大于下属音。所以只在"和首选差不太多"
    的候选里，用低音厚度做一次翻盘。
    """
    if not events:
        return {}
    t0 = min(e["start"] for e in events)
    t1 = max(e["start"] + e.get("dur", 0.1) for e in events)
    w = {}
    t = t0
    while t < t1:
        live = [e for e in events
                if e["start"] <= t < e["start"] + e.get("dur", 0.1)]
        if live:
            pc = min(live, key=lambda e: e["pitch"])["pitch"] % 12
            w[pc] = w.get(pc, 0.0) + win
        t += win
    return w


def _key_candidates(stream, limit=3):
    """music21 给的前几个调性候选（含相关系数），第一个就是它自己的首选。"""
    try:
        k = stream.analyze("key")
    except Exception:
        return []
    out = []
    for kk in [k] + list(getattr(k, "alternateInterpretations", None) or [])[:limit - 1]:
        try:
            pc = int(kk.tonic.pitchClass) % 12
        except Exception:
            continue
        mode = kk.mode
        out.append({"pc": pc, "mode": mode,
                    "corr": float(getattr(kk, "correlationCoefficient", 0.0) or 0.0),
                    "mapped_pc": (pc + 3) % 12 if mode == "minor" else pc,
                    "name": f"{kk.tonic.name.replace('-', 'b')} {mode}"})
    return out[:limit]


def analyze_key_detail(events, tempo=None, ts="4/4", time_sig=None,
                       bass_tiebreak=True):
    """music21 判调（+ 低音厚度兜底），返回明细 dict。

    {'tonic': 'E', 'mode': 'major', 'confidence': 0.87, 'name': 'E major',
     'mapped': 'E', 'relative': False, 'rule': 'music21' | 'bass-tiebreak',
     'raw': 'D major', 'candidates': [...]}

    `mapped` 就是 15 键能用的调名（小调 → 关系大调）。
    """
    empty = {"tonic": None, "mode": None, "confidence": 0.0, "name": "",
             "mapped": None, "mapped_pc": None, "relative": False,
             "tonic_pc": None, "rule": "empty", "raw": "", "candidates": []}
    s = _to_stream(events, tempo, time_sig or ts)
    try:
        if not s.recurse().notes:
            return empty
    except Exception:
        return empty
    cs = _key_candidates(s, 3)
    if not cs:
        return empty

    best = cs[0]
    rule = "music21"
    if bass_tiebreak and len(cs) > 1:
        bw = _bass_line_weights(events)
        pick = best
        for c in cs:
            if best["corr"] - c["corr"] > KEY_GAP_MAX:
                continue                      # 差太多，低音证据翻不动
            if bw.get(c["mapped_pc"], 0.0) > bw.get(pick["mapped_pc"], 0.0) + 1e-9:
                pick = c
        if pick is not best:
            best, rule = pick, "bass-tiebreak"

    return {"tonic": best["name"].rsplit(" ", 1)[0], "mode": best["mode"],
            "confidence": best["corr"], "name": best["name"],
            "mapped": PC_KEY[best["mapped_pc"]], "mapped_pc": best["mapped_pc"],
            "relative": best["mode"] == "minor", "tonic_pc": best["pc"],
            "rule": rule, "raw": cs[0]["name"],
            "candidates": [{"name": c["name"], "corr": round(c["corr"], 3)}
                           for c in cs]}


def analyze_key(events, tempo=None, ts="4/4", time_sig=None, bass_tiebreak=True):
    """→ `layout.KEYS` 里的一个调名（大调名，永远非空）。"""
    d = analyze_key_detail(events, tempo, ts, time_sig)
    return d["mapped"] or "C"


# ---------------------------------------------------------------- 逐小节和弦


def _stats(events, tempo, bars, time_sig="4/4"):
    """每小节每个 pitch class 的发声时长权重（拍）。"""
    qsec = 60.0 / float(tempo or 100)
    total_q = (bars or 0) * BAR_QUARTERS
    if not bars:
        total_q = max([float(e["start"]) / qsec + float(e.get("dur", 0.1)) / qsec
                       for e in events] or [0.0])
        bars = max(1, int(round(total_q / BAR_QUARTERS)))
        total_q = bars * BAR_QUARTERS

    out = [collections.defaultdict(float) for _ in range(bars)]
    for e in events:
        on = float(e["start"]) / qsec
        du = max(1e-3, float(e.get("dur", 0.1)) / qsec)
        end = on + du
        b0 = max(0, int(on // BAR_QUARTERS))
        b1 = min(bars - 1, int((end - 1e-9) // BAR_QUARTERS))
        pc = int(round(e["pitch"])) % 12
        for b in range(b0, b1 + 1):
            lo = max(on, b * BAR_QUARTERS)
            hi = min(end, (b + 1) * BAR_QUARTERS)
            if hi > lo:
                out[b][pc] += hi - lo
    return out


def _prefer_sharp(key):
    return bool(key) and (key in SHARP_KEYS or "#" in key)


def _pc_name(pc, prefer_sharp=False):
    table = PC_NAME_SHARP if prefer_sharp else PC_NAME_FLAT
    return table[int(pc) % 12]


def _m21_chord_info(root_pc, quality, prefer_sharp=None):
    """让 music21 自己说说这是个什么和弦（交叉验证 + 报告里给人看）。

    返回 (figure, commonName)：figure 是 `harmony.chordSymbolFigureFromChord`
    的记法（"Am"/"E"/"G7"），commonName 是 `chord.Chord.commonName` 的英文
    描述（"minor triad"）。取不到就是空串 —— 名字我们自己也会拼。

    拼写要试两遍：`Pitch(midi=63)` 出来是 E♭，于是 B 大三和弦会被写成
    B-E♭-F#，music21 只能说"enharmonic equivalent"；换成升号拼写
    B-D#-F# 才能认成 B major。所以先按调号偏好拼，认不出再换另一套。
    """
    ivs = dict((q, iv) for q, iv, _p in CANDIDATES).get(quality, (0, 4, 7))
    root_pc = int(root_pc) % 12
    sharp_pref = (root_pc in _SHARP_PCS) if prefer_sharp is None else bool(prefer_sharp)
    best = ("", "")
    for sharp in ((True, False) if sharp_pref else (False, True)):
        try:
            m21 = _m21()
            from music21 import harmony
            table = PC_NAME_SHARP if sharp else PC_NAME_FLAT
            # 用 48 + root + 音程（不能对 12 取模，否则跨八度的和弦音会掉到根音
            # 下面，music21 会把原位和弦当转位报成 "Am/C"）
            names = [table[(root_pc + i) % 12] + str(4 + (root_pc + i) // 12)
                     for i in ivs]
            c = m21.chord.Chord(names)
            try:
                # music21 10.x 的 kwarg 是 includeChordType（旧版叫 includeChordTypeSuffix）
                try:
                    fig = harmony.chordSymbolFigureFromChord(c, includeChordType=True)
                except TypeError:
                    fig = harmony.chordSymbolFigureFromChord(c)
                figure = fig[0] if isinstance(fig, tuple) else (fig or "")
            except Exception:
                figure = ""
            common = ""
            try:
                common = c.commonName or ""
            except Exception:
                pass
            if figure and "Cannot" not in figure and "enharmonic" not in common:
                return figure, common
            if not best[0] and (figure or common):
                best = (figure, common)
        except Exception:
            continue
    return best


def _name_chord(root_pc, quality, prefer_sharp=False):
    """和弦名：自己拼 root+后缀（好读），music21 的记法另存字段。"""
    return _pc_name(root_pc, prefer_sharp) + SUFFIX.get(quality, "")


def _best_chord(weights, bass_pc=None, prefer_sharp=False):
    """在候选三/七和弦里挑覆盖最好的一个。"""
    tot = sum(weights.values())
    if tot <= 0:
        return None
    best = None
    for quality, ivs, penalty in CANDIDATES:
        for root in range(12):
            chord_pcs = {(root + i) % 12 for i in ivs}
            cover = sum(weights.get(p, 0.0) for p in chord_pcs) / tot
            score = cover + penalty
            if bass_pc is not None and bass_pc == root:
                score += 0.06           # 低音＝根音更可信（原位）
            elif bass_pc is not None and bass_pc in chord_pcs:
                score += 0.02
            if best is None or score > best[0]:
                best = (score, root, quality, ivs, cover)
    score, root, quality, ivs, cover = best
    conf = max(0.0, min(1.0, cover))
    figure, common = _m21_chord_info(root, quality, prefer_sharp)
    return {"root_pc": root, "quality": quality, "confidence": conf,
            "name": _name_chord(root, quality, prefer_sharp),
            "m21_figure": figure, "m21_common": common,
            "third_semis": (ivs[1] if len(ivs) > 1 else 4),
            "fifth_semis": (ivs[2] if len(ivs) > 2 else 7),
            "pcs": sorted({(root + i) % 12 for i in ivs})}


def chords_per_bar(events, tempo, bars=None, time_sig="4/4", bass_of=None,
                   key=None):
    """每小节一个和弦标注。

    events : [{'start':秒,'dur':秒,'pitch':MIDI}]（与 arrange 同构）
    tempo  : BPM（把秒换成拍用）
    bars   : 小节数；None 时按最后一个音的结束时间推
    bass_of: 可选 [(bar, bass_pc)] 或 callable(bar)->pc，低音线索（原位判断）
    key    : 调名（`layout.KEYS` 风格）；只用来决定和弦名用升号还是降号，
             None 时自己判一次
    返回   : [dict]，见模块 docstring
    """
    stats = _stats(events, tempo, bars, time_sig)
    prefer_sharp = _prefer_sharp(key or analyze_key(events, tempo, time_sig))
    out = []
    for b, w in enumerate(stats):
        bass_pc = None
        if bass_of is not None:
            try:
                bass_pc = bass_of(b) if callable(bass_of) else dict(bass_of).get(b)
            except Exception:
                bass_pc = None
        got = _best_chord(w, bass_pc, prefer_sharp)
        if got is None:
            out.append({"bar": b, "root_pc": None, "root": None, "quality": "",
                        "name": "", "confidence": 0.0, "weights": dict(w)})
            continue
        got.update({"bar": b, "root": _pc_name(got["root_pc"], prefer_sharp),
                    "weights": dict(w)})
        out.append(got)
    return out


def chord_at(chords, bar):
    """安全取某小节的和弦（越界返回 None）。"""
    if not chords:
        return None
    if 0 <= bar < len(chords):
        return chords[bar]
    return None
