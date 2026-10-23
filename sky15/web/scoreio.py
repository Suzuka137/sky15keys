# -*- coding: utf-8 -*-
"""SMC ↔ 事件表的落地实现（界面专用，不改动 sky15 核心模块）。

两个用途：
1. **直接打开一份 .smc**（拖进来 / 点「示例」）—— 不用转谱，秒开；
2. 把谱面还原成事件表，让「改速度 / 改调性 / 改和弦」能就地重排，
   走的是和音频完全相同的 `arrange()` 路径，所以结果口径一致。

时间基是**拍**（四分音符=1），和 `musicxml.parse()` 同构：重排换速度时
用 `to_seconds()` 重新换算即可，不需要知道原始音频。
"""
from __future__ import annotations

from sky15 import layout, smc

SLOT_BEATS = 0.5        # 一个 8 分格 = 半拍


def base_midi(meta) -> int:
    """谱面标头 → 15 键第一格（字符 D）的实际 MIDI 音高。"""
    key = (meta.get("key") or "C").strip()
    if key not in layout.KEY_MIDI:
        key = "C"
    return layout.KEY_MIDI[key] - (12 if meta.get("has8vb") else 0)


def smc_to_events(text: str):
    """SMC → (events 拍为单位, meta)。events 与 transcribe/musicxml 同构。"""
    meta = smc.parse_meta(text)
    base = base_midi(meta)
    events = []
    for i, g in enumerate(smc.parse_groups(text)):
        for ch in g[:3]:
            if ch in "0<> " or ch not in layout.OFFSET_OF:
                continue
            events.append({"start": i * SLOT_BEATS, "dur": SLOT_BEATS,
                           "pitch": base + layout.OFFSET_OF[ch], "vel": 90})
    return events, meta


def to_seconds(events, tempo) -> list:
    """拍 → 秒（和 musicxml.to_seconds 同一个换算）。"""
    k = 60.0 / float(tempo or 120)
    return [dict(e, start=e["start"] * k, dur=e["dur"] * k) for e in events]


def groups_to_notes(groups, meta):
    """曲谱组 → 每格的 MIDI 音高列表（MIDI 导出 / 试听共用一套换算）。"""
    base = base_midi(meta)
    out = []
    for g in groups:
        row = []
        for ch in g[:3]:
            if ch in "0<> " or ch not in layout.OFFSET_OF:
                continue
            p = base + layout.OFFSET_OF[ch]
            if 0 <= p <= 127:
                row.append(p)
        out.append(row)
    return out


def count_notes(text_or_groups) -> int:
    if isinstance(text_or_groups, str):
        groups = smc.parse_groups(text_or_groups)
    else:
        groups = text_or_groups
    return sum(1 for g in groups for ch in g[:3] if ch not in "0<> ")


def summary(text: str) -> dict:
    """从 SMC 文本算出界面要显示的那几个数。"""
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    tempo = max(1, int(meta.get("tempo") or 120))
    return {
        "title": meta.get("title") or "未命名",
        "tempo": tempo,
        "key": meta.get("key") or "C",
        "key_raw": meta.get("key_raw") or meta.get("key") or "C",
        "time_sig": meta.get("time_sig") or "4/4",
        "has8vb": bool(meta.get("has8vb")),
        "difficulty": meta.get("difficulty") or "",
        "notes": count_notes(groups),
        "groups": len(groups),
        "bars": (len(groups) + 7) // 8 if groups else 0,
        "seconds": round(len(groups) * 60.0 / tempo / 2.0, 1),
    }
