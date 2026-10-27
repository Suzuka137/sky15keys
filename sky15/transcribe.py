# -*- coding: utf-8 -*-
"""音频 → 音符事件。两条后端可选，按装了什么自动挑。

后端 A：piano_transcription_inference（字节跳动的高精度钢琴转谱，MIT，权重自动下）
        —— 认钢琴、纯音乐最准，是本项目的首选
后端 B：basic_pitch（Spotify，轻量，CPU 也能跑）
        —— 装不上 A 时的退路，和声薄一点

视频文件先经 ffmpeg 抽音轨。
"""
from .tmp import tmpdir, tmpfile
import os, shutil, subprocess, tempfile

# ---------------------------------------------------------------- 转谱超参
# 48 首基准（beats 自估速 + arrange(key=None) + bench.score）上扫出来的：
#   (0.40, 0.30, 0.50) → 95.38%，旧默认 (0.30, 0.30, 0.35) → 95.33%。
# 逐项证据见 out/agent_trans/report.md；复现（只吃 out/probs 缓存，几分钟）：
#     python -m sky15._trans_eval --grid final --jobs 2 --raw
#
# onset  0.40（不是 0.35）：干净音频上 0.35 高 0.03（95.41），但**真实录音会塌**：
#        站点播放音频加混响，139 号在 0.35 下 8.5%、0.40 下 94.7%（混响尾巴把
#        reg_onset 抬过 0.35，炸出一堆虚 onset → 相位整个错）；手机频响 5 首
#        82.8 → 83.6。0.39 是翻转点，0.45 起是悬崖（91.9）。宁可要这 0.03。
# frame  0.50：95.26 → 95.41。机理不是"少产音符"（音符数完全不变，1070.5），
#        而是把时值截短 → arrange 的 drop_short_rel=0.20 相对剪音顺手剪掉一批
#        "虚音"。0.45~0.55 是平台，0.60 回落，0.30 以下明显差。
# offset 不动：0.20~0.50 在整张表上只差 0.01~0.02，纯噪声（降质音频上同理）。
# pedal  不动：pedal 事件不参与音符生成（output_dict_to_detected_notes 不读 pedal），
#        实测 0.10 与 0.50 的 F1 完全一致（96.02% vs 96.02%）。
# 段长/hop 不动：seg 5/10/20/30s 的 F1 在 ±0.06 内；hop=seg（无重叠）反而 −0.8
#        （召回掉），hop=seg/4 只 +0.08，不值得两倍算力。包内 hop 硬编码 = seg//2。
ONSET_TH = 0.40
OFFSET_TH = 0.30
FRAME_TH = 0.50
PEDAL_TH = 0.20
SEGMENT_SECONDS = 10.0        # 推理分段长度；hop 固定 = segment/2（包内硬编码，实测够用）
TRIM_SILENCE = True           # 转谱前裁掉首尾静音（防真实录音的引导静音把相位打飞）
LAST_TRIM = {"offset": 0.0}   # 上一次转谱裁掉的头部秒数：原始录音时间 = 事件时间 + offset

# 旧口径（95.33% 那一版）：transcribe(wav, **OLD_PARAMS)
_OLD = dict(onset=0.30, offset=0.30, frame=0.35, segment=10.0, trim=False)


def _which_ffmpeg():
    return shutil.which("ffmpeg")


def to_wav(path, sr=16000, mono=True):
    """任意音频/视频 → 16k 单声道 wav（transcription 模型的输入口径）"""
    ff = _which_ffmpeg()
    if not ff:
        raise RuntimeError("需要 ffmpeg：把它放进 PATH（winget install Gyan.FFmpeg）")
    out = str(tmpfile("sky15", "a.wav"))
    cmd = [ff, "-y", "-loglevel", "error", "-i", path,
           "-ac", "1" if mono else "2", "-ar", str(sr), out]
    subprocess.run(cmd, check=True)
    return out


def _events_from_mido_midi(mid):
    """把转谱结果落的 MIDI 读成事件表（复用 midifile）"""
    from . import midifile
    return midifile.read(mid)["events"]


def transcribe(audio_path, backend=None, device=None, **kw):
    """返回 (events, bpm_guess, 后端名)

    kw 可覆盖超参：onset=, offset=, frame=, pedal=, segment=, trim=
      transcribe(wav, frame=0.35)      # 用旧的 frame 阈值（对比用）
      transcribe(wav, **transcribe.OLD_PARAMS)   # 整套退回旧口径
      transcribe(wav, trim=False)      # 不裁首尾静音（录音本身已经贴 0 时用）
    """
    # LAST_TRIM 记的是「上一次转谱裁掉了多少秒头部」。它只有 piano / transkun
    # 两个后端会写，basic / librosa 不写 —— 于是从前者切到后者时会沿用上一首的
    # 偏移去平移事件，整首谱错位。每一首开始前先归零。
    LAST_TRIM["offset"] = 0.0
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)
    if kw.pop("old_params", False):
        kw = {**_OLD, **kw}

    # 视频/音频统一先转 wav
    ext = os.path.splitext(audio_path)[1].lower()
    wav = audio_path
    if ext not in (".wav", ".flac"):
        wav = to_wav(audio_path)

    backend = backend or auto_backend()
    if backend == "piano":
        ev, bpm = _piano(wav, device, **kw)
        name = "piano_transcription"
    elif backend == "transkun":
        ev, bpm = _transkun(wav, device, **kw)
        name = "transkun"
    elif backend == "basic":
        ev, bpm = _basic(wav)
        name = "basic_pitch"
    elif backend == "librosa":
        ev, bpm = _librosa(wav)
        name = "librosa_pyin"
    else:
        raise RuntimeError("没有可用的转谱后端，见 install 说明")
    return clean_events(ev), bpm, name


# 钢琴模型的合法音域：A0(21) ~ C8(108)。范围外的都是模型的"休止/无效"占位符，
# 不是音。实测（2026-10-24，用户那条 274 秒的视频）Transkun 会吐 pitch = −64、
# vel = 127、时值 57 秒这种事件，一首里 4 个。它们不发音，但会：
#   1) 混进 beats 的 onset 列表（`dur > 0` 就收），把格子的相位带偏 ——
#      实测同一份音频相位从 0.026 秒变成 0.059 秒（一个格 0.1875 秒，偏 18%），
#      表现就是"开头前几个音位置对不上"；
#   2) 让音符数虚高，用户看到的"多少音"里混进根本不存在的音。
# 所以在唯一的出口处统一过一遍，各后端都受益。
PITCH_LO, PITCH_HI = 21, 108
LAST_DROPPED: dict = {"n": 0, "sample": []}


def clean_events(events):
    """丢掉音域外的假事件和零时值事件，顺手记下丢了几个备查。"""
    keep, drop = [], []
    for e in events or []:
        try:
            p = int(e.get("pitch", 0))
            d = float(e.get("dur", 1) or 0)
        except (TypeError, ValueError):
            continue
        (keep if (PITCH_LO <= p <= PITCH_HI and d > 0) else drop).append(e)
    LAST_DROPPED["n"] = len(drop)
    LAST_DROPPED["sample"] = drop[:4]
    return keep


OLD_PARAMS = dict(onset=_OLD["onset"], offset=_OLD["offset"],
                  frame=_OLD["frame"], segment=_OLD["segment"], trim=_OLD["trim"])


def _librosa(wav):
    """纯旋律退路：不用任何模型，靠 pyin 基频跟踪。

    只能出**单音**（同一时刻一个音高），和声全部丢失。
    适合：清唱、哼唱、单音乐器、想快速验证流程。
    """
    import librosa, numpy as np

    y, sr = librosa.load(wav, sr=22050, mono=True)
    f0, voiced, _vprob = librosa.pyin(
        y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
        sr=sr, frame_length=2048, hop_length=512, fill_na=np.nan)
    times = librosa.times_like(f0, sr=sr, hop_length=512)
    hop_dt = float(times[1] - times[0]) if len(times) > 1 else 0.023

    events, cur = [], None
    for t, hz, v in zip(times, f0, voiced):
        midi = int(round(librosa.hz_to_midi(float(hz)))) if (v and hz and hz > 0) else None
        if midi is None:
            if cur:
                events.append(cur)
                cur = None
            continue
        if cur and cur["pitch"] == midi:
            cur["dur"] += hop_dt
        else:
            if cur:
                events.append(cur)
            cur = {"start": float(t), "dur": hop_dt, "pitch": midi, "vel": 90}
    if cur:
        events.append(cur)

    # 去掉闪断造成的碎音
    events = [e for e in events if e["dur"] >= 0.06]
    return events, None


def auto_backend():
    """默认后端：有 Transkun 就用它。

    依据（48 首，站点音色，同口径 `_bench_site.py`）：
    Transkun **91.67%** vs 字节 **85.99%**（+5.69，胜负 30/18），速度快一倍
    （9s/首 vs 19s/首），权重同样是 MIT。反过来在"加法合成音"上字节 95 对 TK 76，
    但那种音频只存在于我们的旧基准里，用户手里没有 —— 所以按真实场景选。

    想退回字节：`transcribe(wav, backend="piano")`。
    """
    try:
        import transkun  # noqa
        import moduleconf  # noqa

        return "transkun"
    except Exception:
        pass
    try:
        import piano_transcription_inference  # noqa
        import os
        ck = os.path.expanduser("~/piano_transcription_inference_data/"
                                "note_F1=0.9677_pedal_F1=0.9186.pth")
        if os.path.exists(ck) and os.path.getsize(ck) > 150 * 1024 * 1024:
            return "piano"
    except Exception:
        pass
    try:
        import basic_pitch  # noqa
        return "basic"
    except Exception:
        pass
    try:
        import librosa  # noqa
        return "librosa"
    except Exception:
        pass
    raise RuntimeError(
        "没有可用的转谱后端。装其中一个：\n"
        "  pip install transkun                       # 推荐，真实音色最准（MIT）\n"
        "  pip install piano-transcription-inference   # 退路\n"
        "  pip install basic-pitch                     # 轻量退路\n"
        "  pip install librosa                         # 纯旋律退路（无需模型）"
    )


def trim_silence(y, sr, thresh_db=-45.0, pad=0.05, min_head=0.20, floor_margin=8.0,
                 return_offset=False):
    """裁掉首尾静音（20ms 帧 RMS）。

    阈值取两者里更高的那个：相对峰值的 `thresh_db`（默认 −45dB），
    以及**噪声底 + `floor_margin`**（底噪用帧 RMS 的 **2% 分位**估计）。
    只看绝对阈值在真实录音上会失效 —— 底噪一旦高于 −45dB（手机录、18dB SNR、
    mild/heavy 降质），整首都判成"有声"，一秒也裁不掉，引导静音的相位错还在。
    实测：站点音频 + 18dB 噪声，纯峰值阈值裁不动、F1 70.2；加噪声底判据后修好。

    **为什么是 2% 不是 10%**：10% 分位在「前奏轻、高潮响」的曲子上会被前奏顶高，
    整段前奏会被当成静音裁掉（2026-10-25 用户报视频前 30 秒的音全没了，实测那段
    10% 分位 −32.2dB、峰值 −11.2dB → 阈值 −24.2dB → 裁掉 28.47 秒；换 2% 分位后
    阈值 −40dB → 只裁开头 4.56 秒的真静音）。

    为什么必须有：真实录音/站点回放音频经常带 3~6 秒引导静音，
    而 `beats.select_grid` 的相位是相对**录音起点**算的 —— 头上多 6 秒，
    整首会错 20~30 个组，单首 F1 从 96 掉到 10。裁掉静音即修复。
    对本来就贴着 0 开始的音频（`out/*.wav`，实测 67 个文件首音全在 0.000s）是空操作；
    `min_head` 再兜一层：头部静音不足 0.2s 就不裁，避免几十毫秒的差异改变相位估计。
    """
    import numpy as np

    y = np.asarray(y, dtype=np.float32).ravel()

    def _ret(z, off):
        return (z, off) if return_offset else z

    if y.size == 0:
        return _ret(y, 0.0)
    fl = max(1, int(0.02 * sr))
    n = y.size // fl
    if n < 2:
        return _ret(y, 0.0)
    rms = np.sqrt(np.mean(y[: n * fl].reshape(n, fl) ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    peak_db = float(db.max())
    # 噪声底用 2% 分位，不是 10% —— 10% 分位在「前奏很轻、高潮很响」的曲子会被前奏
    # 顶得很高，于是整段前奏被判成静音裁掉（2026-10-25 用户报：视频前 30 秒的音全没了。
    # 那段实测 10% 分位 -32.2dB、峰值 -11.2dB，阈值算出 -24.2dB，前奏全在阈值以下，
    # 裁掉 28.47 秒；换 2% 分位（-48dB）后阈值 -40dB，只裁掉开头 4.56 秒的真静音）。
    floor_db = float(np.percentile(db, 2))
    # 兜底：底噪不可能比峰值低 40dB 以上，再低就是数字静音了（别让 2% 分位被空段拉穿）
    floor_db = max(floor_db, peak_db - 40.0)
    thr_db = max(peak_db + thresh_db, floor_db + floor_margin)
    thr_db = min(thr_db, peak_db - 6.0)      # 兜底：别把整首都判成静音
    idx = np.nonzero(db > thr_db)[0]
    if idx.size == 0:
        return _ret(y, 0.0)
    lead = idx[0] * fl / float(sr)
    a = max(0, int(idx[0] * fl - pad * sr)) if lead > min_head else 0
    b = min(y.size, int((idx[-1] + 1) * fl + pad * sr))
    return _ret(y[a:b], a / float(sr))


def _piano(wav, device=None, onset=None, offset=None, frame=None, pedal=None,
           segment=None, progress=False, trim=True):
    import librosa
    from piano_transcription_inference import PianoTranscription, sample_rate
    import tempfile, os

    audio, _ = librosa.load(wav, sr=sample_rate, mono=True)
    t0 = 0.0
    if trim:
        audio, t0 = trim_silence(audio, sample_rate, return_offset=True)
    LAST_TRIM["offset"] = float(t0)
    dev = device or auto_device()
    seg = float(segment if segment is not None else SEGMENT_SECONDS)
    tr = PianoTranscription(device=dev, checkpoint_path=None,
                            segment_samples=int(round(seg * sample_rate)))
    tr.onset_threshold = ONSET_TH if onset is None else float(onset)
    tr.offset_threshold = OFFSET_TH if offset is None else float(offset)
    tr.frame_threshold = FRAME_TH if frame is None else float(frame)
    tr.pedal_offset_threshold = PEDAL_TH if pedal is None else float(pedal)
    mid = str(tmpfile("sky15", "t.mid"))
    tr.transcribe(audio, mid)
    return _events_from_mido_midi(mid), None


def _transkun(wav, device=None, trim=True, **kw):
    """Transkun 2.0（MIT，Yujia Yan）—— 实测在**真实音色**上更强。

    为什么默认不用它：48 首基准的音频是 `tools/render_midi` 的加法合成音，
    在这个"不像钢琴"的输入上 Transkun 只有 76%，字节 95%；但把音频换成
    站点播放器（游戏自己的 48 个采样）之后结论反过来：
    **Transkun 91.67% vs 字节 85.99%（48 首，+5.69，胜负 30/18）**。
    用户手上是录屏/视频/真钢琴，所以真实场景应该走这条。

    它要 44.1 kHz（`pretrained/2.0.conf` 里 fs: 44100, f_max: 8000），
    所以这里单独按 44.1k 读盘，不走 16k 的那条路。
    """
    # Transkun 没有 onset / offset / frame / pedal / segment 这套阈值（那是字节后端的
    # 超参）。以前这里是 **_ignored 全吞：用户传 onset=0.35 也一声不吭，
    # 会以为自己调过了，然后拿两套不可比的数去比对精度。
    _unused = [k for k in ("onset", "offset", "frame", "pedal", "segment")
               if kw.get(k) is not None]
    if _unused:
        print(f"  · Transkun 后端不吃 {', '.join(_unused)}（那是字节后端的阈值），已忽略")
    import numpy as np
    import torch

    model = _load_transkun(device or auto_device())
    # 不用包里那个 readAudio：它走 pydub，而 pydub 在 Python 3.13 上要
    # audioop（3.13 已从标准库删除），会逼用户再装一个 pyaudioop。
    # 这里直接自己读盘，行为一致：单声道 float32、重采样到模型的 44.1 kHz。
    audio = _read_mono(wav, model.fs)
    t0 = 0.0
    if trim:
        audio, t0 = trim_silence(audio, model.fs, return_offset=True)
    LAST_TRIM["offset"] = float(t0)
    # 这个模型要 (样本数, 通道) 的列向量，内部自己 transpose 成 (1, n)
    x = torch.from_numpy(np.asarray(audio, dtype=np.float32).reshape(-1, 1)).to(
        next(model.parameters()).device)
    with torch.no_grad():
        notes = model.transcribe(x, stepInSecond=None, segmentSizeInSecond=None,
                                 discardSecondHalf=False)
    ev = [{"start": float(n.start), "dur": max(1e-3, float(n.end - n.start)),
           "end": float(n.end), "pitch": int(n.pitch),
           "vel": int(getattr(n, "velocity", None) or 64)} for n in notes]
    return ev, None


_TK_CACHE: dict = {}


def _read_mono(path, sr):
    """读成单声道 float32 并重采样到 sr。

    Transkun 需要 44.1 kHz；我们自己读能绕开 pydub（Python 3.13 上它依赖
    已被移除的 audioop，会让用户多装一个包）。
    """
    import librosa

    y, fs = librosa.load(str(path), sr=None, mono=True)
    if fs != sr:
        import soxr

        y = soxr.resample(y, fs, sr)
    return y.astype("float32", copy=False)


def _load_transkun(device="cuda"):
    """载一次，重复用（载入要 1~2 秒，网页连着转好几首时不能每次都载）。"""
    key = str(device)
    if key in _TK_CACHE:
        return _TK_CACHE[key]
    import importlib.resources as ir

    import moduleconf
    import torch
    from transkun import ModelTransformer

    root = ir.files("transkun") / "pretrained"
    wp = str(root / "2.0.pt")
    cp = str(root / "2.0.conf")
    conf = moduleconf.parseFromFile(cp)["Model"].config
    ck = torch.load(wp, map_location=device)
    model = ModelTransformer.TransKun(conf=conf).to(device)
    miss, _unexp = model.load_state_dict(
        ck.get("best_state_dict") or ck.get("state_dict"), strict=False)
    # strict=False 是为了容忍包里键名的少量差异。但要是整份权重都没对上，
    # 模型会带着随机初始化照跑 —— 出谱是垃圾，却一个字都不报。
    _total = max(1, len(model.state_dict()))
    if miss and len(miss) > 0.3 * _total:
        raise RuntimeError(
            f"transkun 权重没对上（缺 {len(miss)} / {_total} 个键），"
            f"不拿随机权重硬跑；重装 transkun，或检查 {wp}")
    model.eval()
    _TK_CACHE[key] = model
    return model


def _basic(wav):
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH
    import tempfile, os

    _model_out, midi_data, _notes = predict(wav, ICASSP_2022_MODEL_PATH)
    mid = str(tmpfile("sky15", "t.mid"))
    midi_data.write(mid)
    return _events_from_mido_midi(mid), None


def auto_device():
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"
