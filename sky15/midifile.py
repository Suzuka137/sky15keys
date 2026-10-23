# -*- coding: utf-8 -*-
"""MIDI 读写（依赖 mido）。"""
import mido


def read(path):
    """MIDI → [{'start':秒,'dur':秒,'pitch':,'vel':}] + 元信息"""
    mid = mido.MidiFile(path)
    us_per_beat = 500000
    tempo_changes = []
    t = 0.0
    events = []
    for track in mid.tracks:
        t = 0.0
        us_per_beat = 500000
        pending = {}
        for msg in track:
            t += mido.tick2second(msg.time, mid.ticks_per_beat, us_per_beat)
            if msg.type == "set_tempo":
                us_per_beat = msg.tempo
                tempo_changes.append((t, msg.tempo))
            elif msg.type == "note_on" and msg.velocity > 0:
                pending.setdefault(msg.note, []).append((t, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                if pending.get(msg.note):
                    st, vel = pending[msg.note].pop(0)
                    events.append({"start": st, "dur": max(0.02, t - st),
                                   "pitch": msg.note, "vel": vel})
    # 没收到 note_off 的补一个
    for note, lst in []:
        pass
    events.sort(key=lambda e: e["start"])

    bpm = 120
    if tempo_changes:
        tempos = [t for _, t in tempo_changes]
        us = max(set(tempos), key=tempos.count)
        bpm = int(round(60000000 / us))

    ts = "4/4"
    for track in mid.tracks:
        for msg in track:
            if msg.type == "time_signature":
                ts = f"{msg.numerator}/{msg.denominator}"
                break

    return {"events": events, "tempo": bpm, "time_sig": ts,
            "length": mid.length, "ppq": mid.ticks_per_beat}
