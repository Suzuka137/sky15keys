# -*- coding: utf-8 -*-
"""生成 48 个占位音色（MIDI 48..95）。

这不是任何游戏或站点的采样——是四根泛音 + 衰减包络现算的，只为让网页播放器
在你手上没有音色库时也能出声。想换成真钢琴采样，把同名文件（1.mp3=C3 … 48.mp3=B6）
塞进 sky15/web/samples/ 即可，代码只认文件名。

    python -m sky15.tools.make_samples [输出目录]
"""
import subprocess, sys, pathlib

def make(outdir, sr=44100, dur=4.0):
    import numpy as np
    import soundfile as sf
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(sr * dur)) / sr
    done = 0
    for i in range(48):
        midi = 48 + i
        f0 = 440.0 * 2 ** ((midi - 69) / 12.0)
        y = np.zeros_like(t)
        for k, amp in ((1, 1.0), (2, 0.42), (3, 0.20), (4, 0.10), (5, 0.05)):
            fk = f0 * k * (1 + 0.0004 * k * k)
            if fk > sr / 2:
                continue
            y += amp * np.sin(2 * np.pi * fk * t + 0.1 * k)
        env = np.exp(-t * (2.2 + midi / 60.0)) * (1 - np.exp(-t * 240))
        y = (0.32 * y * env).astype("float32")
        wav = outdir / f"_{i + 1}.wav"
        sf.write(str(wav), y, sr)
        mp3 = outdir / f"{i + 1}.mp3"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                            "-codec:a", "libmp3lame", "-b:a", "128k", str(mp3)],
                           capture_output=True)
        if r.returncode == 0:
            wav.unlink()
            done += 1
        else:
            wav.rename(outdir / f"{i + 1}.wav")
    return done

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(
        pathlib.Path(__file__).resolve().parent.parent / "web" / "samples")
    n = make(out)
    print(f"生成 {n} 个 mp3 → {out}")
