"""五线谱 / MIDI / SMC → 钢琴音频。

用真正的钢琴采样（Salamander Grand Piano V3，CC-BY，641 个 ogg、16 个力度层）
把谱子直接弹成 WAV / MP3，不是合成器糊出来的正弦波。

用法：
    python -m sky15.pianorender 谱子.musicxml
    python -m sky15.pianorender 曲子.smc --mp3
    python -m sky15.pianorender 曲子.mid -o out --gain 0.8

支持的输入：
    .musicxml / .xml / .mxl   MusicXML（MuseScore 导出）
    .mid / .midi              MIDI
    .smc / .txt               15 键谱（本站格式）

默认输出：与输入同名的 .wav，放在输入旁边。
"""
from __future__ import annotations

import math
import os
import pathlib
import subprocess
import sys
import tempfile
import wave

import numpy as np

SR = 44100                     # 输出采样率
PEAK = 0.89                    # 归一化目标峰值（-1 dBFS）
RELEASE = 0.30                 # 松键释放时间（秒）
TAIL_PAD = 1.2                 # 结尾留的自然衰减
CACHE_BYTES = 96 * 1024 * 1024  # 解码缓存上限

SALAMANDER = os.environ.get("SKY15_SALAMANDER", "")
FALLBACK = os.environ.get(
    "SKY15_PIANO_SAMPLES",
    str(pathlib.Path(__file__).with_name("web") / "samples"),
)


# ─────────────────────────── 采样库 ───────────────────────────

class Library:
    """Salamander SFZ 的运行时视图：按 (音高, 力度) 找采样。

    找不到 Salamander 时自动退回 `web/samples` 里那 48 个单层采样
    （C3..B6，一个八度一档），音质差些但能直接用。
    """

    def __init__(self, root: str | pathlib.Path | None = None):
        root = pathlib.Path(root or SALAMANDER)
        sfz = root / "SalamanderGrandPianoV3.sfz"
        self.note_regions = []      # (lokey, hikey, lovel, hivel, path, center, gain_db)
        self._cache: dict[str, np.ndarray] = {}
        self._lru: list[str] = []
        self._bytes = 0
        self.decoded = 0
        self.basic = False
        if sfz.exists():
            self.root = root
            self._parse(sfz)
        else:
            fb = pathlib.Path(FALLBACK)
            samples = sorted(fb.glob("*.mp3")) if fb.is_dir() else []
            if not samples:
                raise FileNotFoundError(
                    f"找不到钢琴采样：{sfz}\n"
                    "两个办法：\n"
                    "  1) 下 Salamander Grand Piano V3（OggVorbis 版，CC-BY，"
                    "archive.org/details/SalamanderGrandPianoV3），解压后用环境变量 "
                    "SKY15_SALAMANDER 指向那个目录；\n"
                    "  2) 把任意 48 个钢琴单音（C3..B6，文件名 1..48.mp3）放进 "
                    f"{fb}。"
                )
            self.root = fb
            self.basic = True
            for i in range(48):
                p = fb / f"{i + 1}.mp3"
                if p.exists():
                    midi = 48 + i
                    self.note_regions.append((midi, midi, 1, 127, p, midi, 0.0))
        if not self.note_regions:
            raise RuntimeError(f"{self.root} 里没解析出任何采样")

    def _parse(self, sfz: pathlib.Path) -> None:
        cur: dict = {}
        for raw in sfz.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("<group>"):
                cur = {}
                if "trigger=release" in line.replace(" ", ""):
                    cur["release"] = True
                continue
            if not line.startswith("<region>"):
                continue
            d = dict(cur)
            for tok in line[len("<region>"):].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    d[k] = v
            if d.get("release"):
                continue
            smp = d.get("sample")
            if not smp:
                continue
            p = self.root / smp.replace("\\", "/")
            if not p.exists():                      # sfz 里写 ogg\xxx，实际也有平铺的
                p = self.root / pathlib.Path(smp).name
            if not p.exists():
                continue
            lokey = int(d.get("lokey", 0)); hikey = int(d.get("hikey", 127))
            lovel = int(d.get("lovel", 1)); hivel = int(d.get("hivel", 127))
            # SFZ 规范：缺 pitch_keycenter 时默认 60（C4）。这份 sfz 里 C4 那几行
            # 恰好没写，早先按 lokey（59）兜底会把每个 C4 弹成升 C —— 差一个半音。
            center = int(d.get("pitch_keycenter", 60))
            vol = float(d.get("volume", 0) or 0)
            self.note_regions.append((lokey, hikey, lovel, hivel, p, center, vol))
        if not self.note_regions:
            raise RuntimeError(f"{sfz} 里没解析出任何采样区域")

    def pick(self, pitch: int, vel: int):
        """返回最合适的 (path, center, gain_db)。越界音高夹到最近的键。"""
        pitch = max(0, min(127, int(pitch)))
        vel = max(1, min(127, int(vel)))
        same_key = None
        best = None          # (力度差, 路径, center, vol)
        nearest = None       # (键距, 路径, center, vol) —— 没有覆盖到这个键时兜底
        for lokey, hikey, lovel, hivel, p, center, vol in self.note_regions:
            if lokey <= pitch <= hikey:
                if lovel <= vel <= hivel:
                    return p, center, vol
                d = min(abs(lovel - vel), abs(hivel - vel))
                if best is None or d < best[0]:
                    best = (d, p, center, vol)
                continue
            dist = min(abs(pitch - lokey), abs(pitch - hikey))
            if nearest is None or dist < nearest[0]:
                nearest = (dist, p, center, vol)
        if best is not None:
            return best[1], best[2], best[3]
        if nearest is not None and nearest[0] <= 24:   # 最多上下两个八度内借音
            span = nearest[2] + (pitch - nearest[2])     # 保持 center，由调用方移调
            return nearest[1], nearest[2], nearest[3]
        return None, pitch, 0.0

    # ── 解码（libsndfile 读不了 vorbis，走 ffmpeg）──
    def load(self, path: pathlib.Path, need_s: float) -> np.ndarray:
        """按文件名缓存，长度固定够用就行 —— 同一采样按不同长度缓存会反复解码。"""
        key = path.name
        hit = self._cache.get(key)
        if hit is not None:
            if hit.shape[0] >= int(need_s * SR):
                return hit
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
            capture_output=True, check=True,
        ).stdout
        a = np.frombuffer(raw, dtype="<f4").reshape(-1, 2).astype(np.float32)
        n = min(len(a), max(int(need_s * SR), int(10.0 * SR)))
        a = np.ascontiguousarray(a[:n])
        self.decoded += 1
        if key in self._cache:
            self._bytes -= self._cache.pop(key).nbytes
            self._lru.remove(key)
        self._cache[key] = a
        self._lru.append(key)
        self._bytes += a.nbytes
        while self._bytes > CACHE_BYTES and len(self._lru) > 1:
            old = self._lru.pop(0)
            self._bytes -= self._cache.pop(old).nbytes
        return a


# ─────────────────────────── 事件来源 ───────────────────────────

def events_from_smc_text(text: str) -> tuple[list[dict], str]:
    """15 键谱文本 → 事件列表。网页那边谱子在内存里，没存成文件，走这个。"""
    from . import layout, smc

    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    key = str(meta.get("key") or "C")
    root_name = key.split("(")[0].strip() or "C"
    if root_name not in layout.KEYS:
        root_name = "C"
    # 注意：meta["key"] 在 smc.parse_meta 里已经被 re.sub(r"\(.*\)", "", ...) 剥掉了括号，
    # 拿它再判 "(8vb)" 永远 False，8vb 谱会整体高八度。以 meta["has8vb"] 为准。
    has8vb = bool(meta.get("has8vb")) or "(8vb)" in key.lower()
    tempo = float(meta.get("tempo") or 100)
    if not math.isfinite(tempo) or tempo <= 0:      # 脏 tempo：0 会 ZeroDivisionError，负数算出负格长
        tempo = 100.0
    slot = 60.0 / tempo / 2
    out = []
    for i, g in enumerate(groups):
        for c in g:
            if c not in layout.CHARS:
                continue
            p = layout.char_pitch(c, root_name)   # 第二个参数要调名，不是 MIDI 号
            if has8vb:
                p -= 12
            out.append({"pitch": p, "start": i * slot, "dur": slot * 0.98,
                        "velocity": 88})
    return out, f"15 键谱（{tempo} BPM，{key}）"


def events_from(path: str | pathlib.Path) -> tuple[list[dict], str]:
    """把各种输入统一成 [{'pitch','start','dur','velocity'}]，单位秒。"""
    from . import layout, midifile, musicxml, smc   # noqa: F401  (供下面的分支用)

    path = pathlib.Path(path)
    ext = path.suffix.lower()
    if ext in (".musicxml", ".xml", ".mxl"):
        got = musicxml.parse_full(str(path))
        ev = got.to_seconds()                 # 自带分段速度表，中途变速也跟得上
        n_tempo = len({round(b, 2) for _, b in (got.tempo_map or [])})
        tempo_txt = f"{got.tempo} BPM" + (f"，中间变速 {n_tempo} 档" if n_tempo > 1 else "")
        if not getattr(got, "tempo_found", True):
            tempo_txt += "（谱子没写速度，按 100 兜底）"
        return ev, f"MusicXML（{tempo_txt}，{got.time_sig}，{got.key}）"

    if ext in (".mid", ".midi"):
        got = midifile.read(str(path))
        # midifile.read 返回的是 dict（{"events": [...], "tempo": ...}），不是 tuple/list。
        # 写成 got[0] if isinstance(got, tuple) else got 会把整个 dict 当事件表，
        # 于是 for e in ev 遍历的是键名字符串，e.get(...) 直接 AttributeError。
        ev = got["events"] if isinstance(got, dict) else (got[0] if isinstance(got, tuple) else got)
        for e in ev:                          # MIDI 用 vel，统一成 velocity
            e["velocity"] = e.get("velocity") or e.get("vel") or 80
        return ev, "MIDI"

    if ext in (".smc", ".txt"):
        return events_from_smc_text(path.read_text(encoding="utf-8", errors="replace"))

    raise RuntimeError(f"不认识的文件类型：{ext}")


# ─────────────────────────── 合成 ───────────────────────────

def _pitch_shift(a: np.ndarray, ratio: float) -> np.ndarray:
    if abs(ratio - 1.0) < 1e-4:
        return a
    n = int(round(len(a) / ratio))
    if n <= 1:
        return a[:1]
    x_old = np.arange(len(a), dtype=np.float64)
    x_new = np.linspace(0, len(a) - 1, n)
    return np.stack([np.interp(x_new, x_old, a[:, c]) for c in range(a.shape[1])],
                    axis=1).astype(np.float32)


def render(events: list[dict], lib: Library, gain: float = 1.0,
           normalize: bool = True, verbose: bool = True) -> np.ndarray:
    if not events:
        raise RuntimeError("谱子是空的，没有可以弹的音")
    last = max(e["start"] + e["dur"] for e in events)
    total = max(int((last + TAIL_PAD) * SR), 1)      # 全部音都在 0 之前时别给 np.zeros 负长度
    buf = np.zeros((total, 2), dtype=np.float32)

    n_missing = 0
    for e in events:
        pitch = int(round(e["pitch"]))
        vel = int(e.get("velocity") or 80)
        start = float(e["start"]); dur = float(e["dur"])
        path, center, vol_db = lib.pick(pitch, vel)
        if path is None:
            n_missing += 1
            continue
        need = min(dur + RELEASE + 0.6, 14.0)
        seg = lib.load(path, need)
        seg = _pitch_shift(seg, 2.0 ** ((pitch - center) / 12.0))
        # 力度增益：采样层已经分好了强弱，再叠一点连续变化
        v = (vel / 127.0) ** 0.6
        g = (10.0 ** (vol_db / 20.0)) * (0.35 + 0.65 * v) * gain
        # 松键释放
        n_end = int(dur * SR)
        seg = seg.copy()
        if n_end < len(seg):
            fade = min(int(RELEASE * SR), len(seg) - n_end)
            if fade > 1:
                seg[n_end:n_end + fade] *= np.linspace(1.0, 0.0, fade,
                                                       dtype=np.float32)[:, None]
            seg[n_end + fade:] = 0.0
        i0 = int(start * SR)
        if i0 < 0:
            # 负 start（arrange(offset<0) / 极小 tempo / 外部 MIDI 的负时间戳）：
            # numpy 的负下标会从缓冲区**尾部**倒着数，整个音被叠到曲尾且不报错。
            # 正确做法是把 0 之前那一截切掉，而不是整体平移。
            seg = seg[-i0:]
            i0 = 0
        n = min(len(seg), total - i0)
        if n > 0:
            buf[i0:i0 + n] += seg[:n] * g

    peak = float(np.max(np.abs(buf))) if total else 0.0
    if normalize and peak > 1e-6:
        buf *= PEAK / peak
    if verbose:
        print(f"  音符 {len(events)} 个" + (f"（{n_missing} 个超出钢琴音域已跳过）" if n_missing else ""))
        print(f"  时长 {total / SR:.1f} 秒，原始峰值 {20 * math.log10(peak or 1e-9):.1f} dBFS，"
              f"解码采样 {lib.decoded} 个")
    return buf


def write_wav(a: np.ndarray, out: pathlib.Path) -> pathlib.Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(a, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2").tobytes()
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)
    return out


_LIB: "Library | None" = None


def lib() -> "Library":
    """进程内共用一个采样库。

    Library 内部按文件名做 LRU 解码缓存，每次新建都会把 48 个采样重新
    ffmpeg 解一遍（约 1.5 秒）。服务里一段音频要连续弹好几次，所以共享。
    """
    global _LIB
    if _LIB is None:
        _LIB = Library()
    return _LIB


def to_mp3(wav: pathlib.Path, mp3: pathlib.Path, kbps: int = 192) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                    "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(mp3)],
                   check=True)


def convert(src: str | pathlib.Path, out: str | pathlib.Path | None = None,
            want_mp3: bool = False, gain: float = 1.0,
            normalize: bool = True, verbose: bool = True) -> pathlib.Path:
    src = pathlib.Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    if verbose:
        print(f"读谱：{src.name}")
    events, kind = events_from(src)
    if verbose:
        print(f"  来源：{kind}")
    library = lib()          # 复用模块级 _LIB：每次 new 一个会把全部采样重新解码一遍
    buf = render(events, library, gain=gain, normalize=normalize, verbose=verbose)

    if out is None:
        dst = src.with_suffix(".mp3" if want_mp3 else ".wav")
    else:
        dst = pathlib.Path(out)
        if dst.is_dir() or dst.suffix == "":
            dst = dst / (src.stem + (".mp3" if want_mp3 else ".wav"))

    if want_mp3:
        # 中转 wav 必须落在系统临时目录、且绝不叫 <stem>.wav：以前写在输出目录、
        # 用同名 stem，finally 里的 unlink 会把用户自己那份 .wav 删掉（失败路径也删）。
        with tempfile.TemporaryDirectory(prefix="sky15_mp3_") as tmpdir:
            tmp = pathlib.Path(tmpdir) / "render.wav"
            write_wav(buf, tmp)
            to_mp3(tmp, dst)
    else:
        write_wav(buf, dst)
    if verbose:
        print(f"写出：{dst}  ({dst.stat().st_size / 1048576:.1f} MB)")
    return dst


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m sky15.pianorender",
        description="五线谱 / MIDI / 15 键谱 → 钢琴音频（Salamander 真采样）")
    ap.add_argument("src", help="谱子文件（musicxml/mxl/mid/smc）")
    ap.add_argument("-o", "--out", default=None, help="输出文件或目录，默认同名 .wav")
    ap.add_argument("--mp3", action="store_true", help="直接输出 MP3")
    ap.add_argument("--gain", type=float, default=1.0, help="整体音量倍数")
    ap.add_argument("--no-normalize", action="store_true", help="不要自动归一化")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    out = a.out
    # `-o x.mp3` 也该出 MP3；以前只认 --mp3，于是把 WAV 字节写进了 .mp3 文件里。
    want_mp3 = bool(a.mp3) or (out is not None
                              and pathlib.Path(out).suffix.lower() == ".mp3")
    try:
        convert(a.src, out, want_mp3=want_mp3, gain=a.gain,
                normalize=not a.no_normalize, verbose=not a.quiet)
    except Exception as exc:                                   # noqa: BLE001
        print(f"出错：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
