# -*- coding: utf-8 -*-
"""把 SFZ 音色库（如 Salamander Grand Piano, CC-BY 3.0）裁成 48 个单音 mp3。

网页播放器只要 48 个文件：`1.mp3` = C3（MIDI 48）…… `48.mp3` = B6（MIDI 95）。
SFZ 里通常只有每三个半音一个采样（A / C / D#），靠 pitch_keycenter 变调；
这里按 SFZ 的映射变调，最多 ±1.5 个半音，听不出来。

    python -m sky15.tools.build_samples <sfz 文件> [输出目录] [--vel 80] [--dur 5]

Salamander Grand Piano 的 SFZ 一共 641 个 region（88 键 × 16 力度层）。
"""
import pathlib
import re
import subprocess
import sys


def parse_sfz(path):
    """返回 [(lokey, hikey, lovel, hivel, keycenter, sample_path)]。"""
    path = pathlib.Path(path)
    base = path.parent
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s.startswith("<region>"):
            continue
        kv = dict(re.findall(r"(\w+)=([^\s]+)", s))

        def num(k, d=None):
            try:
                return int(kv[k])
            except (KeyError, ValueError):
                return d

        smp = kv.get("sample", "").replace("\\", "/")
        if not smp:
            continue
        out.append((
            num("lokey", 0), num("hikey", 127),
            num("lovel", 0), num("hivel", 127),
            num("pitch_keycenter", num("key", 60)),
            base / smp,
        ))
    return out


def pick_region(regions, midi, vel):
    for lo, hi, lv, hv, kc, smp in regions:
        if lo <= midi <= hi and lv <= vel <= hv:
            return kc, smp
    return None, None


def build(sfz, outdir, vel=80, dur=5.0, sr=44100, bitrate="128k"):
    import numpy as np
    import soxr
    import soundfile as sf

    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    regions = parse_sfz(sfz)
    if not regions:
        raise SystemExit(f"SFZ 里没解析到 region: {sfz}")

    made, miss = 0, []
    levels = []
    for i in range(48):
        midi = 48 + i
        kc, smp = pick_region(regions, midi, vel)
        if smp is None or not pathlib.Path(smp).exists():
            miss.append(midi)
            continue
        y, fs = sf.read(str(smp), always_2d=True, dtype="float32")
        y = y.mean(axis=1)                       # 合单声道
        ratio = 2.0 ** ((midi - kc) / 12.0)
        if abs(ratio - 1.0) > 1e-6:
            y = soxr.resample(y, fs, int(round(fs * ratio)), quality="VHQ")
            fs = int(round(fs * ratio))
        if fs != sr:
            y = soxr.resample(y, fs, sr, quality="VHQ")

        n = int(sr * dur)
        if len(y) < n:
            y = np.pad(y, (0, n - len(y)))
        else:
            y = y[:n].copy()
        fade = int(sr * 0.06)
        y[-fade:] *= np.linspace(1.0, 0.0, fade) ** 1.2

        # 音量：按前 300 ms 的 RMS 拉到同一条线上，别让某个音突然炸出来
        head = y[:int(sr * 0.3)]
        rms = float(np.sqrt(np.mean(head ** 2))) or 1e-6
        y *= min(3.0, 0.10 / rms)
        peak = float(np.max(np.abs(y))) or 1.0
        if peak > 0.85:
            y *= 0.85 / peak
        levels.append(float(np.sqrt(np.mean(y[:int(sr * 0.3)] ** 2))))

        wav = outdir / f"_{i + 1}.wav"
        sf.write(str(wav), y.astype("float32"), sr)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                            "-codec:a", "libmp3lame", "-b:a", bitrate,
                            str(outdir / f"{i + 1}.mp3")], capture_output=True)
        if r.returncode == 0:
            wav.unlink()
        made += 1

    return made, miss, levels


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    vel = 80
    if "--vel" in argv:
        i = argv.index("--vel")
        vel = int(argv[i + 1])
        del argv[i:i + 2]
    dur = 5.0
    if "--dur" in argv:
        i = argv.index("--dur")
        dur = float(argv[i + 1])
        del argv[i:i + 2]
    sfz = argv[0]
    outdir = argv[1] if len(argv) > 1 else str(
        pathlib.Path(__file__).resolve().parent.parent / "web" / "samples")

    made, miss, levels = build(sfz, outdir, vel=vel, dur=dur)
    lo, hi = (min(levels), max(levels)) if levels else (0, 0)
    print(f"生成 {made} 个 mp3 → {outdir}")
    if miss:
        print(f"缺 {len(miss)} 个音: {miss}")
    if levels:
        print(f"各音响度范围 {lo:.3f}~{hi:.3f}（越接近越均匀）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
