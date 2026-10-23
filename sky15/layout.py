# -*- coding: utf-8 -*-
"""光遇 15 键的乐理模型。

键盘布局（3 行 × 5 列）与音符字符一一对应：

    Y U I O P      D R M F S     低八度   +0 +2 +4 +5 +7
    H J K L ;      L X 1 2 3     中八度   +9 +11 +12 +14 +16
    N M , . /      4 5 6 7 8     +17 +19 +21 +23 +24

即：以某调为根音的大调音阶，铺满两个八度，再补一个高八度的根音，共 15 个音。
（这套映射已与站内 buildNoteMap 逐字符核对过。）
"""

CHARS = "DRMFSLX12345678"
SEMIS = [0, 2, 4, 5, 7, 9, 11]          # 大调音阶音程
KEYS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
KEY_MIDI = {"C": 60, "Db": 61, "D": 62, "Eb": 63, "E": 64, "F": 65,
            "Gb": 66, "G": 67, "Ab": 68, "A": 69, "Bb": 70, "B": 71}

# 15 键每个字符相对根音的半音数
OFFSETS = [CHARS[i] for i in range(15)]
OFFSET_OF = {}
for _i, _c in enumerate(CHARS):
    if _i < 7:
        OFFSET_OF[_c] = SEMIS[_i]                 # 低八度  0..11
    elif _i < 14:
        OFFSET_OF[_c] = 12 + SEMIS[_i - 7]        # 中八度 12..23
    else:
        OFFSET_OF[_c] = 24                        # 最高  24

SPAN = 24                                     # 可用音域：根音 ~ 根音+24


def span(root_key):
    """该调下 15 键覆盖的 MIDI 音高区间（闭区间）。"""
    r = KEY_MIDI[root_key]
    return r, r + SPAN


def snap(pitch, root_key):
    """把任意 MIDI 音高塞进 15 键：返回 (字符, 误差半音)。

    规则：
      1) 先按八度折回可用音域（超出就 ±12 平移，尽量少动）
      2) 再吸附到最近的调内音；距离相同取低音（下行更自然）
    """
    r = KEY_MIDI[root_key]
    rel = pitch - r

    # 1) 折回音域
    while rel < 0:
        rel += 12
    while rel > SPAN:
        rel -= 12

    # 2) 吸附到调内音
    best_c, best_err = None, None
    for c, off in OFFSET_OF.items():
        err = abs(rel - off)
        if best_err is None or err < best_err:
            best_c, best_err = c, err
    return best_c, (rel - OFFSET_OF[best_c])


def char_pitch(c, root_key):
    """字符 → 实际音高。"""
    return KEY_MIDI[root_key] + OFFSET_OF[c]


def in_range(pitch, root_key):
    r = KEY_MIDI[root_key]
    return r <= pitch <= r + SPAN
