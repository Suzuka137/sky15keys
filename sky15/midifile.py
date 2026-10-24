# -*- coding: utf-8 -*-
"""MIDI 读写（依赖 mido）。

时间轴上有个坑，值得写明白：
MIDI 的 delta tick 是「每条轨道各自从 0 累加」的，但 tempo map 是**整份文件共用**的。
标准做法（Type-1，任何 DAW 导出的默认格式、网上绝大多数 MIDI）把 set_tempo 全放在
第 0 轨，音符放在别的轨。所以必须先把所有轨扫一遍拼出全局 tempo map，再拿它去换算
每条轨的 tick —— 只按「本轨有没有 set_tempo」判断，Type-1 就会被当成默认 120 BPM 算，
100 BPM 的曲子时长只有实际的 5/6，变速曲能差到 1/3。
"""
import bisect

import mido

DEFAULT_US = 500000       # MIDI 规范规定的「没写 set_tempo 时」速度：120 BPM


def _tempo_segments(mid):
    """→ (segs, keys)。segs = [(abs_tick, us_per_beat, 该 tick 处的秒数)]，全文件共用。"""
    raw = [(0, DEFAULT_US)]
    for tr in mid.tracks:
        tick = 0
        for msg in tr:
            tick += msg.time
            if msg.type == "set_tempo":
                raw.append((tick, msg.tempo))
    raw.sort(key=lambda x: x[0])

    segs = []
    for tk, us in raw:
        if segs and segs[-1][0] == tk:
            segs[-1] = (tk, us, segs[-1][2])      # 同一刻度上后来的覆盖前面的
        else:
            segs.append((tk, us, 0.0))

    ppq = mid.ticks_per_beat
    sec = 0.0
    for i in range(1, len(segs)):
        pt, pus, _ = segs[i - 1]
        sec += (segs[i][0] - pt) * pus / 1e6 / ppq
        segs[i] = (segs[i][0], segs[i][1], sec)

    return segs, [s[0] for s in segs]


def _sec_of(segs, keys, tick, ppq):
    """tick → 秒。按全局 tempo map 分段线性换算。"""
    i = max(0, bisect.bisect_right(keys, tick) - 1)
    tk, us, sec = segs[i]
    return sec + (tick - tk) * us / 1e6 / ppq


def read(path):
    """MIDI → [{'start':秒,'dur':秒,'pitch':,'vel':}] + 元信息"""
    mid = mido.MidiFile(path)
    ppq = mid.ticks_per_beat
    segs, keys = _tempo_segments(mid)

    events = []
    for track in mid.tracks:
        tick = 0
        pending = {}
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                pending.setdefault(msg.note, []).append((tick, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                lst = pending.get(msg.note)
                if lst:
                    st_tick, vel = lst.pop(0)
                    st = _sec_of(segs, keys, st_tick, ppq)
                    events.append({"start": st,
                                   "dur": max(0.02, _sec_of(segs, keys, tick, ppq) - st),
                                   "pitch": msg.note, "vel": vel})
        # 本轨跑完了还没收到 note_off 的音：按「响到本轨最后」补上。
        # 以前这里是个空循环（`for note, lst in []: pass`），没闭合的音直接消失，
        # 用户看到的就是「MIDI 里明明有，转出来少了几个音」。
        end = _sec_of(segs, keys, tick, ppq)
        for note, lst in pending.items():
            for st_tick, vel in lst:
                st = _sec_of(segs, keys, st_tick, ppq)
                events.append({"start": st, "dur": max(0.02, end - st),
                               "pitch": note, "vel": vel})

    events.sort(key=lambda e: e["start"])

    bpm = 120
    if any(m.type == "set_tempo" for tr in mid.tracks for m in tr):
        us_list = [s[1] for s in segs]
        us = max(set(us_list), key=us_list.count)      # 变速曲取占时间最长的那个速度
        bpm = int(round(60000000 / us))

    ts = "4/4"
    for track in mid.tracks:                           # 只认第一个拍号，别被后面的轨覆盖
        hit = None
        for msg in track:
            if msg.type == "time_signature":
                hit = f"{msg.numerator}/{msg.denominator}"
                break
        if hit:
            ts = hit
            break

    return {"events": events, "tempo": bpm, "time_sig": ts,
            "length": mid.length, "ppq": ppq}
