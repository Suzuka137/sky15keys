# -*- coding: utf-8 -*-
"""旋律线提取：音源分离 + 单音高追踪 → 每格的旋律音高（右手主音）。

为什么需要它
------------
转谱后端（piano_transcription）是**复音**识别：它把左手伴奏、内声部、
踏板泛音全部读成音符。`arrange._pick_melody` 只能靠
「上半区里时值×力度最长、并列取高音」猜旋律 —— 这是启发式，抓不到真正的
主奏声部。一首 3 分钟的曲子转出来 700 个音、真值只有 490 个时，猜错的概率
就压不住了（41 号曲 F1 85~89%）。

这个模块提供两件武器：
  1. `separate`   —— demucs 音源分离，把"主奏"和"伴奏"拆成两条轨
  2. `crepe_f0`   —— torchcrepe 单音高追踪：复音混音里给出**最突出的那个 f0**

然后把逐帧 f0 折叠到 SMC 的 8 分格子上，得到「第 i 格应该是哪个 MIDI 音」，
交给 `arrange` 当旋律提示（`melody=` 参数）。

重要前提（实测结论，见 out/agent_melody/report.md）
------------------------------------------------
got 两个工具都不是为"钢琴独奏的主奏提取"训练的：
  · crepe 是**单音高**模型，复音输入它只报一个 f0，通常是能量最大/最干净的那条线
  · demucs 是 4 轨（drums/bass/other/vocals）流行乐模型，钢琴曲的"主奏 vs 伴奏"
    并不在它的训练目标里 —— 钢琴通常整体落在 `other` 里
所以 melody 提示只当**证据**用，不是真理：`arrange` 只在提示音高附近
（默认 ±0.6 半音）真的存在转谱音符时才采纳，否则回退原逻辑。
"""
import json
import math
import os
import pathlib

import numpy as np

CREPE_SR = 16000
HINT_DIR = pathlib.Path(__file__).resolve().parent / "out" / "agent_melody" / "hints"


# ---------------------------------------------------------------- 音频工具
def load_mono(path, sr=CREPE_SR):
    """任意音频 → (np.float32 mono, sr)。用 librosa，顺带处理 mp3。"""
    import librosa
    y, _sr = librosa.load(str(path), sr=sr, mono=True)
    return np.asarray(y, dtype=np.float32), sr


def highpass(wav, cut_hz=200.0, order=4):
    """一阶/多阶高通：把低音区压下去，逼 crepe 往高音声部看。

    动机：crepe 在复音上跟着能量最大的 f0 走，而钢琴低音的能量天然占优
    （低频 + 基频强）。高通之后主奏的相对权重上升，实测能提高旋律命中
    （见 report.md 的 mel_hp 变体）。
    """
    from scipy.signal import butter, sosfilt
    y, sr = load_mono(wav)
    sos = butter(order, cut_hz / (sr / 2.0), btype="highpass", output="sos")
    return sosfilt(sos, y).astype(np.float32), sr


# ---------------------------------------------------------------- 音源分离
def separate(wav, out_dir, model="htdemucs", device=None, verbose=False,
             two_stems=None):
    """demucs 分离 → {stem 名: 输出 wav 路径}。

    默认 4 轨（drums/bass/other/vocals）。钢琴曲请重点看 `other` 和 `vocals`：
    真实现场是钢琴整首落在 `other`，`vocals` 近乎静音或只剩一点中频。

    two_stems 非空时走 `--two-stems` 语义（demucs 只在 CLI 支持，这里手动合并）：
    给的 stem 保留，其余求和当成第二条轨，命名 `no_<stem>`。
    """
    import torch
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    import torchaudio

    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))

    t0 = __import__("time").time()
    m = get_model(model).to(dev).eval()
    load_s = __import__("time").time() - t0

    y, sr = load_mono(wav, sr=44100)          # demucs 训练在 44.1k
    wav_t = torch.from_numpy(y)[None]          # (1, n) = (通道, 采样)
    if sr != m.samplerate:
        wav_t = torchaudio.functional.resample(wav_t, sr, m.samplerate)
        sr = m.samplerate
    # demucs 要 (batch, channels, samples)；htdemucs 的 audio_channels = 2，
    # 单声道必须复制成伪立体声，否则第一层 conv 直接报 "expected 2 channels"
    if wav_t.shape[0] != m.audio_channels:
        wav_t = wav_t.repeat(m.audio_channels, 1)

    t1 = __import__("time").time()
    with torch.no_grad():
        ref = wav_t.mean(0)
        est = apply_model(m, wav_t[None].to(dev), device=dev,
                          progress=verbose, split=True, overlap=0.25)[0]
        est = est * (ref.std() / est.std(dim=(1, 2), keepdim=True).clamp_min(1e-8))
    sep_s = __import__("time").time() - t1

    stems = {}
    names = list(m.sources)

    def _save(path, ten):
        # torchaudio 2.11 的 save 依赖 torchcodec（环境里没有），改用 soundfile
        import soundfile as sf
        sf.write(str(path), ten.detach().cpu().numpy().reshape(-1), sr)

    for i, name in enumerate(names):
        p = out_dir / f"{name}.wav"
        _save(p, est[i].mean(0, keepdim=True))
        stems[name] = str(p)

    if two_stems and two_stems in stems:
        # 其余各轨求和 = 伴奏；这样就有了一对真正的"主奏 / 伴奏"
        idx = [i for i, n in enumerate(names) if n != two_stems]
        bg = est[idx].sum(0).mean(0, keepdim=True)
        p = out_dir / f"no_{two_stems}.wav"
        _save(p, bg)
        stems[f"no_{two_stems}"] = str(p)

    stems["_sec"] = {"model_load": round(load_s, 2), "separate": round(sep_s, 2),
                     "audio_sec": round(len(y) / float(sr), 1)}
    return stems


# ---------------------------------------------------------------- 单音高追踪
def crepe_f0(wav, hop=0.01, fmin=32.7, fmax=2093.0, model="full",
             device=None, batch_size=2048, verbose=False, decoder="viterbi",
             auto_fallback=True):
    """torchcrepe 单音高追踪 → (times[秒], freq[Hz, nan=无声], confidence[0~1])。

    fmin/fmax 默认 C1~C7：钢琴 88 键的常用段。给的音域越窄 crepe 越稳，
    所以调用方可以按曲子中位音高自己收紧。

    decoder: "viterbi"（默认，轨迹平滑，复音下也稳）/ "argmax"（逐帧，边界更容易崩）。

    坑（实测）：torchcrepe 0.0.24 的 viterbi 解码在某些**和弦极密**的曲子上会
    整条塌到频段上界（f0≈fmax、periodicity≈0.000），例如 2 号《雨爱》全曲
    conf 中位 0.000 / f0 钉在 2000Hz；同一段音频换 argmax 就正常（conf 0.42）。
    所以这里做自动降级：全曲置信度中位 < 0.02 时重跑 argmax。
    """
    import torch
    import torchcrepe

    y, sr = load_mono(wav, sr=CREPE_SR)
    audio = torch.from_numpy(y)[None]
    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    hop_len = max(1, int(round(hop * sr)))
    # torchcrepe 0.0.24 的 predict 只返回 (pitch, periodicity)，时间轴要自己算
    fmax = min(float(fmax), float(torchcrepe.MAX_FMAX))

    def _predict(dec):
        dec_fn = {"viterbi": torchcrepe.decode.viterbi,
                  "argmax": torchcrepe.decode.argmax}[dec]
        t0 = __import__("time").time()
        with torch.no_grad():
            out = torchcrepe.predict(
                audio, sr, hop_length=hop_len, fmin=float(fmin), fmax=fmax,
                model=model, batch_size=batch_size, device=dev,
                decoder=dec_fn, return_periodicity=True)
        f = np.asarray(out[0].cpu() if hasattr(out[0], "cpu") else out[0],
                       dtype=float).reshape(-1)
        c = np.asarray(out[1].cpu() if hasattr(out[1], "cpu") else out[1],
                       dtype=float).reshape(-1)
        c = np.nan_to_num(c, nan=0.0)
        f = np.where(c > 0, f, np.nan)
        return f, c, __import__("time").time() - t0

    freq, conf, dt = _predict(decoder)
    used = decoder
    if auto_fallback and decoder == "viterbi" and float(np.median(conf)) < 0.02:
        f2, c2, dt2 = _predict("argmax")
        if float(np.median(c2)) > float(np.median(conf)) + 0.02:
            freq, conf, used = f2, c2, "argmax"
            dt += dt2

    if verbose:
        print(f"    crepe {len(y)/sr:.1f}s 音频 → {len(freq)} 帧，{dt:.2f}s on {dev}，"
              f"decoder={used}，conf 中位 {np.median(conf):.3f}")
    times = np.arange(len(freq), dtype=float) * hop_len / float(sr)
    return times, freq, conf


def f0_to_midi(freq):
    f = np.asarray(freq, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        m = 69.0 + 12.0 * np.log2(np.maximum(f, 1e-9) / 440.0)
    return np.where(np.isfinite(f) & (f > 0), m, np.nan)


def smooth_midi(midi, conf, hop=0.01, med_s=0.06, bridge_s=0.09,
                octave_fix=True, min_conf=0.25):
    """逐帧 MIDI 平滑：置信度门 → 短空隙补 → 中值滤波 → 八度跳变修。

    为什么每一步都不能省：
      · 置信度门：crepe 在噪声/踏板余响上会给随机 f0，低置信度帧全是垃圾
      · 补空隙：音符衰减到听不见时 f0 会断，断点会让"格子→音高"映射漏格
      · 中值滤波：抹掉 ±1 半音的抖动（揉弦/拍频）
      · 八度修：crepe 的典型错误是整数倍 f0（唱名不变、八度错），
        单独一帧差 12 半音就拉回来
    """
    m = np.array(midi, dtype=float)
    m[conf < min_conf] = np.nan

    # 补短空隙
    n = len(m); i = 0
    gap_max = int(round(bridge_s / hop))
    while i < n:
        if np.isnan(m[i]):
            j = i
            while j < n and np.isnan(m[j]):
                j += 1
            if 0 < i and j < n and (j - i) <= gap_max:
                m[i:j] = np.linspace(m[i - 1], m[j], j - i + 2)[1:-1]
            i = j
        else:
            i += 1

    # 中值滤波（nan 感知）
    w = max(1, int(round(med_s / hop)) | 1)
    if w > 1 and np.isfinite(m).any():
        pad = w // 2
        ext = np.concatenate([np.full(pad, np.nan), m, np.full(pad, np.nan)])
        out = np.full_like(m, np.nan)
        for k in range(n):
            seg = ext[k:k + w]
            seg = seg[~np.isnan(seg)]
            if seg.size:
                out[k] = np.median(seg)
        m = out

    if octave_fix and np.isfinite(m).any():
        ref = np.nanmedian(m)
        for k in range(n):
            if not np.isfinite(m[k]):
                continue
            d = m[k] - ref
            if abs(d) >= 11.0:
                shift = -12.0 * round(d / 12.0)
                m[k] += shift
            if abs(m[k] - ref) <= 24.0:
                ref = 0.9 * ref + 0.1 * m[k]
    return m


# ---------------------------------------------------------------- 格子映射
def melody_slots(f0, times, conf, tempo, phase=0.0, nslots=None,
                 hop=0.01, min_conf=0.25, voiced_min=0.30, tol_cents=60.0,
                 smooth=True):
    """把逐帧 f0 折到 SMC 的 8 分格子上。

    返回 np.float64[nslots]：第 i 格的旋律 MIDI（nan = 这一格没有可信旋律）。

    参数
    ----
    tempo   : BPM（格子 = 60/tempo/2 秒）
    phase   : 网格相位（秒），arrange 的 offset
    voiced_min : 一格内至少这么多比例的有声帧才给提示，
                 太低会让伴奏的零星泛音冒充旋律
    tol_cents  : 格子内音高的允许散布；超过就说明这一格里有多个音高在打架
                 （复音/颤音），取加权中位数而不是众数更稳

    为什么用"置信度加权中位数"而不是"最高音"：
      格子里如果同时有旋律和伴奏，f0 会在两者之间跳；中位数落在更持久的
      那个音高上，而最高音会被一两个毛刺帧带飞。
    """
    if smooth:
        midi = smooth_midi(f0_to_midi(f0), conf, hop=hop, min_conf=min_conf)
    else:
        midi = f0_to_midi(f0)

    slot_dur = 60.0 / float(tempo) / 2.0
    if nslots is None:
        nslots = int(math.ceil((float(times[-1]) - phase) / slot_dur)) if len(times) else 0
    out = np.full(int(nslots), np.nan)

    if len(times) == 0:
        return out
    idx = np.floor((np.asarray(times, dtype=float) - phase) / slot_dur).astype(int)
    ok = (idx >= 0) & (idx < nslots)

    order = np.argsort(idx[ok], kind="stable")
    idx_ok = idx[ok][order]
    mid_ok = midi[ok][order]
    conf_ok = conf[ok][order]

    # 按格切段
    bounds = np.searchsorted(idx_ok, np.arange(nslots + 1))
    for s in range(nslots):
        a, b = bounds[s], bounds[s + 1]
        if b <= a:
            continue
        seg_m = mid_ok[a:b]
        seg_c = conf_ok[a:b]
        good = np.isfinite(seg_m) & (seg_c >= min_conf)
        if good.sum() < max(2, voiced_min * (b - a)):
            continue
        mm, cc = seg_m[good], seg_c[good]
        if float(np.percentile(mm, 90) - np.percentile(mm, 10)) > tol_cents / 100.0:
            # 格内有多个音高打架 → 用加权中位数挑"占上风"的那条线
            pass
        order2 = np.argsort(mm)
        cw = np.cumsum(cc[order2])
        half = cw[-1] / 2.0
        k = int(np.searchsorted(cw, half))
        out[s] = float(mm[order2][min(k, len(mm) - 1)])
    return out


# ---------------------------------------------------------------- 提示仓
def fingerprint(events, tempo, phase=0.0):
    """事件表 + 网格 → 稳定短指纹（用来给提示结果做文件名）。"""
    if not events:
        return "empty"
    t0 = float(min(e["start"] for e in events))
    t1 = float(max(e["start"] + e.get("dur", 0) for e in events))
    ps = [e["pitch"] for e in events]
    return (f"n{len(events)}_t{t0:.2f}-{t1:.2f}_p{min(ps)}-{max(ps)}"
            f"_b{tempo:g}_o{phase * 1000:.0f}")


_MEM = {}


def register(events, tempo, phase, hints, save=True):
    key = fingerprint(events, tempo, phase)
    arr = [None if (h is None or (isinstance(h, float) and math.isnan(h)))
           else round(float(h), 2) for h in hints]
    _MEM[key] = arr
    if save:
        HINT_DIR.mkdir(parents=True, exist_ok=True)
        (HINT_DIR / f"{key}.json").write_text(json.dumps({
            "key": key, "tempo": tempo, "phase": phase,
            "nslots": len(arr), "hints": arr,
        }), encoding="utf-8")
    return key


def hints_for(events, tempo, phase=0.0):
    """arrange 的变体用这个拿提示；没有就返回 None（自动回退原逻辑）。"""
    key = fingerprint(events, tempo, phase)
    if key in _MEM:
        return _MEM[key]
    p = HINT_DIR / f"{key}.json"
    if p.exists():
        arr = json.loads(p.read_text(encoding="utf-8"))["hints"]
        _MEM[key] = arr
        return arr
    return None
