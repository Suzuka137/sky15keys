"""录音质量体检：花 20 秒跑模型之前，先告诉用户这段音频"能不能用、哪里糟"。

为什么要有它：`sky15/out/agent_trans/report.md` §4 实测（6 首）——
混响 RT60 ≥ 1.5 s 会把 139 号打到 **6.4%**（干净时 96%），而"拿手机对着音箱录"是常态；
8 kHz 采样不是"少认几个音"，而是**炸出两倍虚音**（2387 音 vs 真值 1061）。
与其让他跑完看到一坨错谱，不如先给人话。

指标的取舍（`sky15/out/audioq/probe_*.wav` 上标定的，见 `_audioq_probe.py`）：

| 指标 | 能测出什么 | 实测分离度 |
|---|---|---|
| 原始采样率 | 8 kHz 录音 | 直接判 |
| `hi` = >5.5 kHz 能量占比 | 手机免提/通话频响 | 干净 0.0048 / 手机 0.0001 |
| `flat` = 谱平坦度中位 | 底噪、嘶声 | 干净 0.0001 / 加噪 0.067 |
| `flux` = 谱通量峰均比 | 混响（把起音糊掉） | 干净 14.8 / RT1.2 11.1 / RT2 10.5 |
| 平台采样占比 | 削顶 | 干净 0 / 硬削顶 4% |
| 首帧静音 | 引导静音 | 站点回放头上 3~6 秒 |

**没有做的**：RT60 数值估计。拟合出来的是 2.38 / 8.62 / 6.3（真值 0.6/1.2/2.0），
不单调、不可信，所以只报"混响偏重"这种定性判断，不报秒数。

    from sky15 import audioq
    q = audioq.analyze("a.wav")      # dict：score / level / warnings / 各指标
    print(audioq.describe(q))        # 一行中文
"""

from __future__ import annotations

import numpy as np

SR = 16000
HOP = 160  # 10 ms


def _db(x) -> float:
    return float(20.0 * np.log10(max(float(x), 1e-10)))


def _load(path_or_y, sr=None):
    """返回 (16k 单声道, 原始采样率)。原始采样率用来判 8 kHz 录音。"""
    if isinstance(path_or_y, np.ndarray):
        y = np.asarray(path_or_y, dtype=np.float32)
        if y.ndim > 1:
            y = y.mean(axis=1)
        orig = int(sr or SR)
        if orig != SR:
            import librosa

            y = librosa.resample(y, orig_sr=orig, target_sr=SR)
        return y.astype(np.float32), orig

    import librosa

    y, orig = librosa.load(str(path_or_y), sr=None, mono=True)
    y = np.asarray(y, dtype=np.float32)
    if orig != SR:
        y = librosa.resample(y, orig_sr=orig, target_sr=SR)
    return y.astype(np.float32), int(orig)


def analyze(path_or_y, sr=None) -> dict:
    y, orig_sr = _load(path_or_y, sr)
    if y.size < SR // 2:
        return {"ok": False, "score": 0, "level": "bad",
                "duration": round(y.size / SR, 2),
                "warnings": ["音频太短（不到 0.5 秒）"]}

    import librosa

    dur = y.size / float(SR)
    peak = float(np.max(np.abs(y)))
    peak_db = _db(peak)

    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    rdb = 20.0 * np.log10(np.maximum(rms, 1e-10))
    rel = rdb - float(np.max(rdb))

    loud = np.where(rel > -40.0)[0]
    if loud.size:
        lead = float(loud[0] * HOP / SR)
        tail = float((rdb.size - 1 - loud[-1]) * HOP / SR)
    else:
        lead = tail = 0.0

    snr = float(np.max(rdb) - np.percentile(rdb, 5))

    S = np.abs(librosa.stft(y, n_fft=1024, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=1024)
    tot = float(S.sum()) + 1e-9
    hi = float(S[freqs > 5500].sum()) / tot
    flat = float(np.median(librosa.feature.spectral_flatness(S=S)[0]))
    flux = np.sum(np.maximum(np.diff(S, axis=1), 0), axis=0)
    flux_ratio = float(np.max(flux) / (np.mean(flux) + 1e-9)) if flux.size else 0.0

    # 削顶：贴着最大值（±0.5%）的采样占比。软削顶也是平台，所以判平台不判越界
    plateau = float(np.mean(np.abs(y) > 0.995 * peak)) if peak > 1e-6 else 0.0

    q = {
        "ok": True,
        "duration": round(dur, 2),
        "sr": orig_sr,
        "peak_db": round(peak_db, 1),
        "snr": round(snr, 1),
        "lead_s": round(lead, 2),
        "tail_s": round(tail, 2),
        "hi": round(hi, 4),
        "flat": round(flat, 4),
        "flux": round(flux_ratio, 1),
        "plateau": round(plateau, 4),
    }

    warns: list[str] = []
    severe: list[str] = []
    score = 100.0

    if orig_sr < 12000:
        score -= 45
        severe.append("sr")
        warns.append(f"采样率只有 {orig_sr} Hz —— 模型会炸出两倍虚音（实测 2387 音 vs 1061），换原始文件再传")
    elif hi < 8e-4:
        score -= 35
        if hi < 3e-4:
            severe.append("band")
        warns.append("高频几乎没有（像手机免提/通话录音），琴键音色认不准")
    if flat > 0.02:
        score -= 18
        warns.append(f"底噪很明显（谱平坦度 {flat:.3f}）")
    if plateau > 0.01:
        score -= 12
        warns.append(f"有削顶（{plateau * 100:.1f}% 采样点压在满刻度上）")
    if flux_ratio < 12.0 and flat <= 0.02 and hi >= 8e-4:
        score -= 18
        warns.append("起音被糊掉了（混响偏重或失真），识别会明显变差，建议耳机直录或换干声")
    if peak_db < -20:
        score -= 8
        warns.append("音量偏小，转谱前会自动归一，影响不大")
    if dur < 8:
        score -= 10
        warns.append("太短，不到 8 秒")
    if dur > 480:
        score -= 5
        warns.append("超过 8 分钟，会切成很多段，建议只截一段")

    q["score"] = int(max(0, round(score)))
    q["warnings"] = warns
    if severe:
        q["level"] = "bad"
    else:
        q["level"] = "good" if q["score"] >= 90 else ("ok" if q["score"] >= 65 else "bad")
    # 引导静音不算毛病：转谱层已自动裁掉，只是告知
    q["note"] = (f"开头 {lead:.1f} 秒静音已自动裁掉" if lead > 1.5 else "")
    return q


def describe(q: dict) -> str:
    """一行中文，命令行用。"""
    if not q.get("ok"):
        return "音频太短，读不出内容"
    bits = [f"质量 {q['score']}/100"]
    bits.extend(q.get("warnings") or ["没发现问题"])
    if q.get("note"):
        bits.append(q["note"])
    return " · ".join(bits)


def _main(argv=None):
    import argparse
    import json

    ap = argparse.ArgumentParser(description="录音质量体检（跑模型之前先看一眼）")
    ap.add_argument("files", nargs="+", help="音频文件")
    ap.add_argument("--json", action="store_true", help="输出原始指标")
    a = ap.parse_args(argv)
    for f in a.files:
        q = analyze(f)
        if a.json:
            print(json.dumps({"file": f, **q}, ensure_ascii=False))
        else:
            print(f"{f}\n  {describe(q)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
