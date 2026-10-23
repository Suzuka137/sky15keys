# -*- coding: utf-8 -*-
"""SMC 谱 → 标准 MIDI 文件（纯 stdlib，不引第三方包）。

为什么要自己写：`sky15/midifile.py` 只实现了「读」，而界面要能导出 `.mid`
丢进 DAW 或手机播放器里跟着对拍。SMF 规格本身很简单，一百多行就够，
不必为此给用户的机器多装一个依赖。

时值口径和谱面一致：**一组 = 一个八分音符**，所以每格固定 240 tick
（tpq=480 时）。同格多个音就是和弦，起点相同。
"""
from __future__ import annotations

import math

TPQ = 480                      # 每四分音符 tick 数
SLOT_TICKS = TPQ // 2          # 一个 8 分格 = 240 tick


def _vlq(n: int) -> bytes:
    """MIDI 的可变长整数（delta time 用）。"""
    n = max(0, int(n))
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(payload)) + payload


def _time_sig(numerator: int, denominator: int) -> bytes:
    dd = 0
    while (1 << dd) < max(1, denominator):
        dd += 1
    return _meta(0x58, bytes([numerator & 0xFF, dd, 24, 8]))


def _track(events) -> bytes:
    """events: [(tick, order, raw_bytes)]；同 tick 上先关音再开音。"""
    events.sort(key=lambda e: (e[0], e[1]))
    body = bytearray()
    last = 0
    for tick, _order, raw in events:
        body += _vlq(tick - last)
        body += raw
        last = tick
    body += _vlq(0) + b"\xff\x2f\x00"
    return bytes(body)


def build(notes, *, title: str = "", tempo: int = 120,
          time_sig: str = "4/4", channel: int = 0, velocity: int = 80) -> bytes:
    """notes: 每格一个 MIDI 音高列表，长度 = 谱面格数。

    返回完整的 `.mid` 字节串（format 0，单轨）。
    """
    tempo = int(tempo or 120)
    try:
        num, den = (int(x) for x in str(time_sig).split("/"))
    except Exception:
        num, den = 4, 4

    ev = []
    # 轨道名 / 速度 / 拍号都排在 0 tick，顺序无所谓（同 tick 按 order 排）
    if title:
        ev.append((0, 0, _meta(0x03, title.encode("utf-8"))))
    us_per_quarter = max(1, int(round(60_000_000 / max(1, tempo))))
    ev.append((0, 1, _meta(0x51, us_per_quarter.to_bytes(3, "big"))))
    ev.append((0, 2, _time_sig(num, den)))
    ev.append((0, 3, _meta(0x59, b"\x00\x00")))          # C 大调，娱乐性信息

    for i, pitches in enumerate(notes):
        tick = i * SLOT_TICKS
        big = layout_shift(tick)
        for p in pitches or []:
            p = int(p)
            if not (0 <= p <= 127):
                continue
            ev.append((big, 4, bytes([0x90 | channel, p, velocity & 0x7F])))
            ev.append((big + SLOT_TICKS - 1, 1, bytes([0x80 | channel, p, 0])))

    header = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") \
        + (1).to_bytes(2, "big") + TPQ.to_bytes(2, "big")
    chunk = _track(ev)
    return header + b"MTrk" + len(chunk).to_bytes(4, "big") + chunk


def layout_shift(tick: int) -> int:
    """预留的钩子：以后要整体挪相位就改这里（目前恒等）。"""
    return tick


def midi_seconds(notes, tempo: int) -> float:
    """估算时长（秒），给界面上显示用。"""
    return len(notes) * 60.0 / max(1, int(tempo or 120)) / 2.0


def bars(notes, per_bar: int = 8) -> int:
    return max(1, int(math.ceil(len(notes) / float(max(1, per_bar)))))
