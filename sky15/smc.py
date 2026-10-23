# -*- coding: utf-8 -*-
"""SMC 文本格式的读写。

格式：
    曲名:[X] 速度:[160] 节拍:[4/4] 调性:[E(8vb)] 难度:[3] 曲谱:[...]

    曲谱部分按空格切成 4 字符一组：
        [第1音][第2音][第3音][小节标记]
    · 一次最多 3 个音同时按（和弦上限）
    · 第 4 位 `>` 或 `<` = 小节线；空格 = 无标记
    · 一组 = 一个 8 分音符
"""
import re

CHARS = "DRMFSLX12345678"


def _field(text, name, default=""):
    m = re.search(rf"{name}:\[([^\]]*)\]", text)
    return m.group(1).strip() if m else default


def parse_meta(text):
    key_raw = _field(text, "调性", "Ab") or "Ab"
    return {
        "title": _field(text, "曲名"),
        "tempo": int(_field(text, "速度", "120").split()[0] or 120),
        "time_sig": _field(text, "节拍", "4/4") or "4/4",
        "key_raw": key_raw,
        "key": re.sub(r"\(.*\)", "", key_raw).strip() or "Ab",
        "has8vb": "(8vb)" in key_raw.lower(),
        "difficulty": _field(text, "难度"),
    }


def parse_groups(text):
    """曲谱串 → 4 字符组列表。"""
    score = _field(text, "曲谱")
    out = []
    for tok in [t for t in re.split(r"\s+", score.strip()) if t]:
        out.extend(tok[i:i + 4] for i in range(0, len(tok), 4))
    return out


def emit(title, tempo, time_sig, key, has8vb, difficulty, groups):
    """把 4 字符组列表拼回 SMC 文本（与站点导出器同格式）。"""
    key_raw = f"{key}(8vb)" if has8vb else key
    body = " ".join(groups)
    return (f"曲名:[{title}] 速度:[{tempo}] 节拍:[{time_sig}] "
            f"调性:[{key_raw}] 难度:[{difficulty}] 曲谱:[{body}]")


def slots_per_bar(time_sig):
    """一小节有几个 8 分音符。"""
    try:
        num, den = (int(x) for x in time_sig.split("/"))
    except Exception:
        num, den = 4, 4
    # 一小节 = num 个 (1/den) 音符；一个 8 分组 = 1/8
    return max(1, int(round(num * (8 / den))))


def make_group(chars, mark=""):
    """三个音字符 + 标记。

    站上的写法：**没有小节线时只有 3 位**（如 `D10`），
    有小节线才是 4 位（如 `000>`）。所以空标记不能补空格。
    """
    s = "".join(chars[:3])
    s = s + "0" * (3 - len(s))
    return s + mark if mark in "><" else s


def is_empty(group):
    return group[:3] == "000"
