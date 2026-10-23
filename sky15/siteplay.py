# -*- coding: utf-8 -*-
"""按「站点播放器的方式」把 SMC 重放成音频，再走我们的识别，看差在哪。

为什么单独做这个：
  之前 48 首基准用的是简单合成音（加法合成、时值短、无重叠），
  转录几乎不会错。但站点播放器是用 48 个钢琴采样直接放：
  每个音**放满 5 秒**、增益 0.9、压限器兜底 —— 密集段落大量叠加，
  这才是用户录屏得到的音源，也是错误率的真实来源。

用法：
    python -m sky15.siteplay 1.smc out.wav
"""
import os
import sys, pathlib
import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sky15 import smc, layout

SAMPLE_DIR = pathlib.Path(os.environ.get(
    "SKY15_SAMPLES", str(pathlib.Path(__file__).resolve().parent / "web" / "samples")))
SR = 22050
_root_cache = {}


def _load_sample(midi):
    if midi in _root_cache:
        return _root_cache[midi]
    n = midi - 47                      # 1.mp3 = C3 = MIDI 48
    p = SAMPLE_DIR / f"{n}.mp3"
    if not (1 <= n <= 48) or not p.exists():
        _root_cache[midi] = None
        return None
    import librosa
    y, _sr = librosa.load(str(p), sr=SR, mono=True)
    _root_cache[midi] = y
    return y


def render(text, out_wav, gain=0.9, tail=1.0):
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    tempo = meta["tempo"]
    slot = 60.0 / tempo / 2.0

    pathlib.Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    total = int((len(groups) * slot + 5.5 + tail) * SR)
    buf = np.zeros(total, dtype=np.float32)

    for i, g in enumerate(groups):
        t = i * slot
        for ch in g[:3]:
            if ch in "0<> ":
                continue
            midi = layout.char_pitch(ch, meta["key"])
            if not midi:
                continue
            y = _load_sample(midi)
            if y is None:
                continue
            k = int(t * SR)
            n = min(len(y), total - k)
            if n > 0:
                buf[k:k + n] += y[:n] * gain

    # 站点播放器前面挂了一个压限器（-10dB / ratio 10），这里用软限幅近似
    peak = float(np.max(np.abs(buf))) or 1.0
    if peak > 0.9:
        buf = np.tanh(buf / peak * 1.2) * 0.9
    sf.write(out_wav, buf, SR)
    return pathlib.Path(out_wav)

def render_notes(notes, out_wav, gain=0.9, tail=1.0, sr=SR):
    """把任意 (起始秒, MIDI) 列表用游戏那 48 个采样重放出来。

    用途：五线谱/MusicXML 也能"先转成我们最熟悉的那种音色"，再交给转谱模型。
    超出 C3..B6 的音按八度折回范围内（游戏乐器本来就只有这些音）。
    """
    notes = [(float(t), int(p)) for t, p in notes]
    if not notes:
        raise ValueError("没有音")
    span = max(t for t, _ in notes) + 5.0 + tail
    total = int(span * sr)
    buf = np.zeros(total, dtype=np.float32)
    for t, midi in notes:
        while midi < 48:
            midi += 12
        while midi > 95:
            midi -= 12
        y = _load_sample(midi)
        if y is None:
            continue
        k = int(t * sr)
        n = min(len(y), total - k)
        if n > 0:
            buf[k:k + n] += y[:n] * gain
    peak = float(np.max(np.abs(buf))) or 1.0
    if peak > 0.9:
        buf = np.tanh(buf / peak * 1.2) * 0.9
    sf.write(out_wav, buf, sr)
    return pathlib.Path(out_wav)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    text = pathlib.Path(src).read_text(encoding="utf-8")
    p = render(text, dst)
    print(f"{p}  {p.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main()
