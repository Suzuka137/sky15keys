# -*- coding: utf-8 -*-
"""把 MIDI 渲染成音频（加法合成的简易钢琴音色），用于闭环验证。

用途：我们已经知道某首 MIDI 的正确 15 键谱（站上原版），
把它渲染成音频再走一遍 "音频 → 转谱 → 重排"，就能量出端到端到底丢了多少。
"""
import sys, pathlib
import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sky15 import midifile

SR = 16000
HARM = [(1, 1.0), (2, 0.45), (3, 0.22), (4, 0.12), (5, 0.06)]


def render(midi_path, out_path, gain=0.9):
    d = midifile.read(midi_path)
    ev = d["events"]
    dur = max(e["start"] + e["dur"] for e in ev) + 1.0
    n = int(dur * SR)
    buf = np.zeros(n, dtype=np.float32)

    for e in ev:
        f0 = 440.0 * 2 ** ((e["pitch"] - 69) / 12)
        s = int(e["start"] * SR)
        ln = int((e["dur"] + 0.35) * SR)          # 留一点余响
        if s >= n:
            continue
        ln = min(ln, n - s)
        t = np.arange(ln, dtype=np.float32) / SR
        env = np.exp(-t / max(0.12, e["dur"] * 0.6)) * (e["vel"] / 127.0)
        w = np.zeros(ln, dtype=np.float32)
        for k, amp in HARM:
            if f0 * k < SR / 2 - 200:
                w += amp * np.sin(2 * np.pi * f0 * k * t)
        buf[s:s + ln] += w * env

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * gain
    sf.write(out_path, buf, SR, subtype="PCM_16")
    print(f"{midi_path} → {out_path}  {len(ev)} 音 / {dur:.1f}s")
    return out_path


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
