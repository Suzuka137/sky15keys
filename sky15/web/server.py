# -*- coding: utf-8 -*-
"""sky15 网页界面服务端（纯标准库，不装任何 web 框架）。

启动：
    python -m sky15.web.server --port 8799
    python sky15\\web\\server.py 8777          # 老写法，仍然认
    python -m sky15.web.server --port 8777 --open     # 起完自动开浏览器

接口一览（旧的三个字段一律不动，只加不删）：
    POST /api/convert?name=&tempo=&key=&backend=&chords=&harmony=&max_notes=
    GET  /api/job/<id>                 任务快照（阶段、进度、结果、建议）
    GET  /api/download/<id>            下载 .smc
    GET  /api/image/<id>?fmt=png|pdf|preview&theme=&dpi=     ← 参数语义冻结，勿改
    GET  /api/export/<id>?mode=long|pages|pdf|smc|midi|page|piano&theme=&dpi=&page=
    GET  /api/info/<id>                页数等统计（导出面板用）
    POST /api/rearrange/<id>           用缓存的转谱结果就地重排（不重新转谱）
    POST /api/cancel/<id>              请求取消（安全点退出，不硬杀线程）
    GET  /api/history                  历史（服务端持久化）
    POST /api/history/<id>/rename      {"title": "..."}
    POST /api/history/<id>/delete
    POST /api/example                  加载内置示例
    GET  /api/ping                     健康检查

设计取舍：
* 单 worker 线程串行跑任务。转谱吃 GPU/CPU，并发只会互相拖慢，
  前端排队体验反而更好；队列里能看到自己排第几。
* 取消是「打标记 + 安全点检查」，不 kill 线程：模型推理中途硬杀会留下
  半截文件，还可能卡住 CUDA 上下文。
* 成品 .smc 落 `out/web_jobs/`，历史落 `out/web_history.json`，重启不丢。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import zipfile
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingTCPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent                                   # ...\sky15
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from sky15 import layout, smc                        # 轻量模块，直接导入
from sky15.web import midiout, scoreio

VERSION = "2.0"


def log(msg):
    """一句话日志。

    以前这些分支里直接写 `log(...)` 但全文件根本没有定义 log —— 平时走不到，
    一走到就是 `name 'log' is not defined`，把真正的失败原因盖掉了
    （PhotoScore 那次就是这样：引擎那边只说了一句日志未定义）。
    """
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


OUT = ROOT / "out"
JOBS_DIR = OUT / "web_jobs"
CACHE_DIR = OUT / "web_cache"
# 排版版本：改了 render.py 的观感（行数/字号/页眉）就 +1，让磁盘缓存失效。
_REN_VER = "v8"

# 缓存文件的尾标记：写完必然带上，没带上就是写了一半
_TAIL = {"png": b"IEND", "zip": b"PK\x05\x06", "pdf": b"%%EOF"}


def _cache_ok(p, ext: str) -> bool:
    """判断缓存文件是否完整。

    以前只看 size > 0：另一个线程正好在 write_bytes 写到一半时来读，就会拿到
    半个 PNG / ZIP / PDF 直接发给浏览器。这里用文件尾魔数当完整标记，
    写入侧配 tmp + os.replace（见 _render_bytes 末尾），读者要么看到旧文件、
    要么看到完整文件。
    """
    try:
        size = p.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    tail = _TAIL.get(ext)
    if not tail:
        return True
    try:
        with p.open("rb") as f:
            f.seek(max(0, size - 4096))
            return tail in f.read()
    except OSError:
        return False
HIST_FILE = OUT / "web_history.json"
# 上传的原始文件不放 %TEMP%（那是 C 盘，几十 MB 的视频一存就好几百兆）。
# 统一放到 out/web_uploads，跟着项目一起在 E 盘。
WORK = Path(os.environ.get("SKY15_WORK") or (OUT / "web_uploads"))
for _d in (OUT, JOBS_DIR, CACHE_DIR, WORK):
    _d.mkdir(parents=True, exist_ok=True)

SHEET_EXT = {".musicxml", ".xml", ".mxl"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".pdf", ".webp"}
SMC_EXT = {".smc", ".txt", ".text", ".smt"}
SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,9}$")   # `.musicxml` 是 9 个字符
# jid 会直接拼进文件名（JOBS_DIR / f"{jid}.smc"）。必须挡住 `..`、`/`、`\`：
# Windows 上反斜杠也是路径分隔符，`a\..\..\x.smc` 能逃出 out\web_jobs，
# 读写删掉 jobs 目录外的任何 .smc。正常 jid 是 12 位 hex，历史里另有一个调试用的 "dbg"。
_JID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _jid_bad(jid) -> bool:
    return not (isinstance(jid, str) and _JID_RE.match(jid))


def _looks_like_smc(raw: bytes) -> bool:
    """没后缀或后缀不对时，靠内容判断是不是 15 键谱文本。

    15 键谱必须有 `曲谱:[...]` 这一段；顺便挡掉二进制文件——前 4KB 里
    NUL 字节一多就不是文本。
    """
    head = raw[:4096]
    if b"\x00" in head:
        return False
    for enc in ("utf-8-sig", "gbk"):
        try:
            t = head.decode(enc, errors="ignore")
        except Exception:
            continue
        if "曲谱" in t:
            return True
    return False

# 阶段表：kind -> [(key, 中文标签)]
STAGES = {
    "audio": [("probe", "抽音轨"), ("transcribe", "转谱"), ("tempo", "测速"),
              ("arrange", "重排"), ("render", "出谱")],
    "score": [("read", "读五线谱"), ("tempo", "定速"), ("arrange", "重排"),
              ("render", "出谱")],
    "smc": [("read", "读谱面"), ("render", "出谱")],
}

# ---------------------------------------------------------------- 模块懒加载
_MODS: dict = {}


def mod(name):
    """按需导入 sky15 内部模块。

    转谱要拉 torch/librosa，几十秒起步；服务一起来就 import 会让「打开页面」
    等半天。render 额外支持热重载（改字体/布局不用重启服务）。
    """
    m = _MODS.get(name)
    if m is not None:
        return m
    import importlib
    m = importlib.import_module("sky15." + name)
    if name == "render":
        m = _reload_if_changed(m)
    _MODS[name] = m
    return m


def _reload_if_changed(m):
    try:
        src = getattr(m, "__file__", None)
        if not src:
            return m
        stamp = os.path.getmtime(src)
        if getattr(m, "_web_stamp", None) == stamp:
            return m
        import importlib
        m = importlib.reload(m)
        m._web_stamp = stamp
    except Exception:
        pass
    return m


# ---------------------------------------------------------------- 全局状态
JOBS: dict = {}
PENDING: list = []
JOBS_LOCK = threading.RLock()
JOBQ: "queue.Queue[str]" = queue.Queue()
HISTORY: list = []
HIST_LOCK = threading.RLock()
_RENDER_INDEX: dict = {}
_RENDER_LOCK = threading.Lock()
_WORKER_STARTED = False


class HumanError(Exception):
    """带人话建议的错误。"""

    def __init__(self, message, advice=None, code=""):
        super().__init__(message)
        self.advice = list(advice or [])
        self.code = code or ""


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------- 小工具
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _int_or_none(v):
    try:
        if v in (None, "", "auto", "自动"):
            return None
        n = int(float(v))
        return n if n > 0 else None
    except Exception:
        return None


def _bool(v, default=True):
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes", "是", "开"):
        return True
    if s in ("0", "false", "off", "no", "否", "关"):
        return False
    return default


def _chord_mode(v):
    """调参里的「和弦」开关：`auto` 原样 / `duo` 双音。

    双音 = 只给「一个高音 + 一个低音」的格子补一个低音区的和弦音，
    具体怎么挑在 `arrange._chord_density` 里（含禁配表）。
    """
    s = str(v or "").strip().lower()
    if s in ("duo", "2", "double", "双音", "两音", "加和弦"):
        return "duo"
    return "auto"


def _pretty_title(name: str) -> str:
    """文件名 → 曲名：去扩展名、去下划线/连字符堆、去掉常见的机器后缀。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b(final|new|demo|test|out|mix|master|final\d*)\b", "", stem,
                  flags=re.I)
    # 去掉"01 - 曲名"这类轨道号，但**纯数字文件名要留住**（"40.wav" 曾经被削成"未命名"）
    m = re.match(r"^\d{1,3}[\s._-]+(.+)$", stem)
    if m and m.group(1).strip(" ._-"):
        stem = m.group(1)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" ._-")
    return stem or "未命名"


def _safe_filename(s: str, fallback="sky15") -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (s or "").strip())
    s = s.strip(" ._")
    return (s or fallback)[:80]


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _mime_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return {
        ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".pdf": "application/pdf",
        ".smc": "text/plain; charset=utf-8", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------- 任务快照
def _set(job, **kw):
    with JOBS_LOCK:
        job.update(kw)


def _public(job) -> dict:
    with JOBS_LOCK:
        d = dict(job)
    t0 = d.pop("_t0", None)
    aq = d.get("_audioq")
    for k in [k for k in list(d) if k.startswith("_")]:
        d.pop(k, None)
    if aq:
        # 录音体检结果在转谱阶段就能拿到，先给前端，别等任务结束
        d["quality"] = aq
    if t0 and d.get("status") in ("queued", "running"):
        d["elapsed"] = round(time.time() - t0, 1)
    d.setdefault("elapsed", 0.0)
    return d


def _stage(job, key: str, frac: float = 0.0, note: str = None):
    stages = job.get("stages") or []
    idx, label = 0, job.get("stage") or ""
    for i, s in enumerate(stages):
        if s["key"] == key:
            idx, label = i, s["label"]
            break
    n = max(1, len(stages))
    prog = (idx + _clamp(frac, 0.0, 1.0)) / n
    kw = {"stage": label, "stage_key": key, "stage_index": idx,
          "progress": round(min(0.99, prog), 3)}
    if note:
        kw["stage_note"] = str(note)[:80]
    if job.get("status") == "queued":
        kw["status"] = "running"
    _set(job, **kw)


def _ck(job):
    """安全点：取消标记在这里生效。"""
    if job.get("_cancel"):
        raise _Cancelled()


def _jobs_gc(keep: int = 200):
    """回收 JOBS 里已经跑完的任务。

    以前 JOBS 只在删历史时才 pop 一条，而 _get_job 每次打开历史记录都会 setdefault
    一个完整 job（带 result.smc 全文和 _cache 事件表）回内存 —— 长会话下只增不减。
    这里只回收「已完成/出错/取消」且不在排队里的，正在跑的绝不碰。
    """
    with JOBS_LOCK:
        if len(JOBS) <= keep:
            return
        pend = set(PENDING)
        idle = [(j.get("created_at") or 0, jid) for jid, j in JOBS.items()
                if jid not in pend and j.get("status") in ("done", "error", "cancelled")]
        idle.sort()
        for _, jid in idle[:len(JOBS) - keep]:
            JOBS.pop(jid, None)


def new_job(kind: str, *, name: str, source: str, params: dict) -> dict:
    jid = secrets.token_hex(6)
    stages = [{"key": k, "label": lb} for k, lb in STAGES[kind]]
    job = {
        "id": jid, "kind": kind, "name": name, "source": source,
        "status": "queued", "stage": "排队中", "stage_key": "queue",
        "stage_index": 0, "stage_total": len(stages), "stages": stages,
        "progress": 0.0, "queue_pos": 0, "params": dict(params or {}),
        "created_at": time.time(), "elapsed": 0.0, "advice": [], "error": "",
        # 旧前端兼容字段（完成时填真值）
        "tempo": None, "confidence": "", "hit": None, "grid": "", "notes": 0,
        "doubled": 0,
        "_t0": time.time(), "_cancel": False, "_cache": None, "_payload": {},
    }
    with JOBS_LOCK:
        JOBS[jid] = job
        PENDING.append(jid)
        for i, qid in enumerate(PENDING):
            if qid in JOBS:
                JOBS[qid]["queue_pos"] = i + 1
    _jobs_gc()
    return job


def _enqueue(job):
    JOBQ.put(job["id"])


# ---------------------------------------------------------------- 结果组装
def _advice_for(res: dict) -> list:
    """根据结果里的信号，给能直接照做的话。"""
    out = []
    hit = res.get("hit")
    conf = res.get("confidence")
    doubled = int(res.get("doubled") or 0)
    notes = int(res.get("notes") or 0)
    kind = res.get("kind")
    params = res.get("params") or {}

    if kind == "audio" and conf in ("中", "低") and params.get("tempo") in (None, ""):
        if isinstance(hit, (int, float)) and hit < 70:
            out.append({
                "level": "warn",
                "text": f"自动测速只有 {int(hit)}% 的把握。这首歌速度八成有起伏"
                        f"（自由节奏、渐快渐慢），机测最容易在这里翻车。"
                        f"照着谱面或原曲标出的 BPM 填进「速度」，点重算就对了。",
                "action": "tempo",
            })
    if doubled:
        if (res.get("audioq") or {}).get("level") in ("ok", "bad"):
            out.append({
                "level": "warn",
                "text": f"音头被糊掉了，自动测速跟着跑偏（这次把网格放大了 {doubled} 倍）。"
                        f"谱面或原曲标着速度的话，直接填进「速度」再点重算，比让机器猜稳。",
                "action": "tempo",
            })
        else:
            out.append({
                "level": "warn",
                "text": f"自动测速把网格放大了 {doubled} 倍才对上音头，说明实际速度可能只有一半："
                        f"把速度填成 {max(40, int((res.get('tempo') or 120) / 2))} 试试。",
                "action": "tempo",
            })
    if kind == "audio" and notes and notes < 24:
        out.append({
            "level": "warn",
            "text": f"只识别到 {notes} 个音，像是没听清。确认录音里主要就是钢琴或单音乐器，"
                    f"音量别太小；手机外放录的、环境吵的，识别率会掉很多。",
            "action": "file",
        })
    if res.get("backend") == "librosa_pyin":
        out.append({
            "level": "info",
            "text": "这次用的是「快速旋律」后端，只抓单音——和弦会丢。想要完整伴奏，换回自动/钢琴模型再转一次。",
            "action": "backend",
        })
    if kind == "smc":
        out.append({"level": "info",
                    "text": "这是直接打开的现成谱面，没经过转谱。改速度、调性、和弦都能直接重算。",
                    "action": ""})
    cover = res.get("cover")
    aq = res.get("audioq") or {}
    if kind == "audio" and aq.get("ok"):
        if aq.get("level") == "bad":
            out.append({
                "level": "warn",
                "text": f"这段录音本身有问题（质量 {aq['score']}/100）："
                        + "；".join(aq.get("warnings") or [])
                        + "。上面这些错，很大一部分是录音带来的，换一份干净的源文件会好很多。",
                "action": "file",
            })
        elif aq.get("level") == "ok" and aq.get("warnings"):
            out.append({
                "level": "info",
                "text": f"录音体检 {aq['score']}/100：" + "；".join(aq["warnings"]) + "。",
                "action": "",
            })
        if aq.get("note"):
            out.append({"level": "info", "text": aq["note"] + "，不用管。", "action": ""})
    return out


def _bar_quarters(time_sig):
    """一小节等于几个四分音符。2/4→2，3/4→3，4/4→4，6/8→3，12/8→6。"""
    try:
        num, den = str(time_sig or "4/4").split("/")
        num, den = int(num), int(den)
        if num <= 0 or den <= 0:
            return 4.0
        return 4.0 * num / den
    except Exception:
        return 4.0


def _grid_tempo(tempo_q, time_sig):
    """谱面「每分钟四分音符数」→ 15 键格子速度。

    15 键谱的硬约定是**一小节永远 8 格**（格子 = 八分音符的时值），
    而谱面一小节有 `_bar_quarters` 个四分音符。要让 8 格正好铺满一小节：

        格子时长 = 小节秒数 / 8 =(bar_q × 60/tempo_q) / 8
        格子速度 = 60/(2×格子时长) = 4 × tempo_q / bar_q

    4/4 时等于原速度（所以以前从没露过这个问题）；2/4 时会翻倍 ——
    用户 2026-10-23 上传一张 2/4 谱（谱面写 100 BPM），说 199 才是对的，
    就是这个换算。以前直接把 100 当格子速度，一小节被拉成两小节那么长。
    """
    bq = _bar_quarters(time_sig)
    if bq <= 0:
        return float(tempo_q)
    return 4.0 * float(tempo_q) / bq


def _tempo_note(parsed, tempo, time_sig=None):
    """结果芯片上那句"速度是怎么定的"——给用户看，不要贴整张速度表。

    谱子里没写速度时 MusicXML 没速度可依，`parse_full` 会退回 100 BPM，
    这时明说"没标速度、按默认走"，用户才知道该不该手填。

    注意：这里说的都是**谱面上四分音符的速度**，不是成谱速度。成谱速度要按
    拍号换算（`_grid_tempo`），所以调用方还会用 `_grid_tail` 补一句结论，
    否则 4/4 的谱会只剩一个 "100 BPM"，看起来像没生效。
    """
    tm = list(getattr(parsed, "tempo_map", []) or [])
    found = bool(getattr(parsed, "tempo_found", True))
    if len(tm) > 1:
        return f"变速 {len(tm)} 段（{tempo} BPM 起）"
    if found:
        return f"谱面标 {tempo} BPM"
    return f"谱面没标速度，按默认 {tempo} BPM 读谱（不对就手填）"


def _grid_tail(tempo, grid, time_sig):
    """把"谱面速度 → 成谱速度"这一步写在芯片上，免得用户以为没换算。"""
    g = int(round(grid))
    bq = _bar_quarters(time_sig)
    if abs(grid - tempo) < 0.5:
        return f" · {time_sig} 一小节 8 格 → 成谱 {g} BPM"
    return (f" · {time_sig} 一小节 {bq:g} 拍要铺 8 格 → 成谱 {g} BPM"
            f"（谱面 {tempo} 是四分音符算的）")


def _build_result(jid: str, text: str, *, kind: str, backend: str = "",
                  params: dict | None = None, extra: dict | None = None) -> dict:
    s = scoreio.summary(text)
    res = {
        "title": s["title"], "key": s["key"], "key_raw": s["key_raw"],
        "tempo": s["tempo"], "time_sig": s["time_sig"],
        "notes": s["notes"], "seconds": s["seconds"], "backend": backend or "",
        "smc": text, "bars": s["bars"], "groups": s["groups"],
        "difficulty": s["difficulty"], "has8vb": s["has8vb"],
        "confidence": "手动", "hit": 100, "grid": "", "doubled": 0,
        "kind": kind, "job_id": jid, "params": dict(params or {}),
        "warnings": [], "advice": [],
    }
    res.update(extra or {})
    res["download"] = f"/api/download/{jid}"
    res["preview"] = f"/api/image/{jid}?fmt=preview&theme=dark"
    res["files"] = {
        "smc": f"/api/export/{jid}?mode=smc",
        "midi": f"/api/export/{jid}?mode=midi",
        "png_long": f"/api/export/{jid}?mode=long",
        "png_pages": f"/api/export/{jid}?mode=pages",
        "pdf": f"/api/export/{jid}?mode=pdf",
        "preview": f"/api/image/{jid}?fmt=preview&theme=dark",
    }
    res["advice"] = _advice_for(res)
    return res


def _tempo_alts(why, tempo) -> list:
    """测速先验也判不出的那两首（40 号 127 vs 254、315 号 124 vs 166=3/4 倍），
    与其硬猜，不如把候选速度摊给用户点一下。界面上一键重排（/api/rearrange）。
    """
    out = []
    for a in (why or {}).get("alts") or []:
        t = int(a.get("tempo") or 0)
        if not (40 <= t <= 320) or abs(t - int(tempo or 0)) < 1:
            continue
        out.append({"tempo": t, "factor": a.get("factor"),
                    "npb": a.get("npb"), "fill": a.get("fill")})
    return out[:3]


def _finish(job, text: str, *, backend: str = "", extra: dict | None = None):
    """落盘 + 组装结果 + 预热预览 + 写历史。"""
    jid = job["id"]
    _stage(job, "render", 0.2)
    _ck(job)
    path = JOBS_DIR / f"{jid}.smc"
    path.write_text(text, encoding="utf-8")
    res = _build_result(jid, text, kind=job["kind"], backend=backend,
                        params=job["params"], extra=extra)
    res["smc_path"] = str(path)
    try:                                   # 预览图先渲一张，用户切过去不用等
        _render_to(text, "preview", "dark", 110)
    except Exception:
        res["warnings"].append("预览图没渲出来，导出时再试一次。")
    _stage(job, "render", 1.0)
    _set(job, status="done", stage="完成", progress=1.0, result=res, advice=res["advice"])
    # 老前端直接读 job.tempo / job.hit / job.grid，这里给它们留一份副本
    _set(job, tempo=res.get("tempo"), confidence=res.get("confidence"),
         hit=res.get("hit"), grid=res.get("grid"), notes=res.get("notes"),
         doubled=res.get("doubled"), error="")
    _history_upsert(_hist_entry(job, res))
    return res


def _hist_entry(job, res) -> dict:
    now = time.time()
    return {
        "id": job["id"], "title": res.get("title") or "未命名",
        "notes": res.get("notes") or 0,
        "at": time.strftime("%H:%M:%S", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "ts": now, "kind": job["kind"],
        "tempo": res.get("tempo"), "key": res.get("key"),
        "time_sig": res.get("time_sig"), "seconds": res.get("seconds"),
        "bars": res.get("bars"), "backend": res.get("backend") or "",
        "source": job.get("source") or "", "name": job.get("name") or "",
        "params": res.get("params") or {},
        "confidence": res.get("confidence"), "hit": res.get("hit"),
        "available": True,
    }


# ---------------------------------------------------------------- 历史持久化
def _load_state():
    global HISTORY
    items = []
    try:
        if HIST_FILE.exists():
            data = json.loads(HIST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("items") or data.get("history") or []
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict) and x.get("id")]
    except Exception:
        traceback.print_exc()
        items = []
    for it in items:
        it.setdefault("notes", 0)
        it.setdefault("at", "")
        it.setdefault("title", "未命名")
        it["available"] = (JOBS_DIR / f"{it['id']}.smc").exists() or \
                          (WORK / f"{it['id']}.smc").exists()
    with HIST_LOCK:
        HISTORY = items[:200]


def _save_history():
    with HIST_LOCK:
        data = json.dumps({"items": HISTORY}, ensure_ascii=False, indent=1)
    try:
        tmp = HIST_FILE.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, HIST_FILE)
    except Exception:
        traceback.print_exc()


def _history_upsert(entry: dict):
    with HIST_LOCK:
        for i, it in enumerate(HISTORY):
            if it.get("id") == entry["id"]:
                merged = dict(it)
                merged.update(entry)
                HISTORY[i] = merged
                del HISTORY[200:]
                break
        else:
            HISTORY.insert(0, entry)
            del HISTORY[200:]
    _save_history()


# ---------------------------------------------------------------- 渲染 / 导出
def _cache_path(jid, text, mode, theme, dpi, page, ext):
    h = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    return CACHE_DIR / f"{jid}_{h}_{mode}_{theme}_{int(dpi)}_{int(page)}.{ext}"


PIANO_DIR = OUT / "piano"
_PIANO_LOCK = threading.Lock()


def _piano_audio(text: str, jid: str = "", src_events=None,
                 legato: bool = False) -> tuple[bytes, str]:
    """谱面 → 钢琴 mp3（Salamander 真采样）。带磁盘缓存，改一个音就重算。

    src_events 不为空时直接弹这些事件（原谱的每一个音，不经过 15 键压缩）；
    为空就退回把 15 键谱文本还原成音。返回 (音频字节, 用的哪一层)。
    """
    from sky15 import pianorender as PR

    if src_events:
        ev = [{"pitch": int(e["pitch"]),
               "start": float(e["start"]),
               "dur": float(e.get("dur") or 0.1),
               "velocity": int(e.get("velocity") or e.get("vel") or 80)} for e in src_events]
        tag, label = "src", "原谱"
    else:
        ev, _kind = PR.events_from_smc_text(text)
        tag, label = "keys", "15 键谱"

    if legato:
        ev = _fill_gaps(ev)
        tag += "L"

    h = hashlib.sha1((tag + "|" + (text or "")).encode("utf-8")).hexdigest()[:12]
    jid = jid or h
    PIANO_DIR.mkdir(parents=True, exist_ok=True)
    mp3 = PIANO_DIR / f"{jid}_{tag}_{h}.mp3"
    if mp3.exists() and mp3.stat().st_size > 2048:
        return mp3.read_bytes(), label

    if not ev:
        raise HumanError("这份谱子是空的，没有音可以弹")
    # 渲染是整首一次性铺进内存的（44.1k 双声道 float32 ≈ 0.34 MB/秒），
    # 20 分钟以上会吃掉 400 MB 以上，先拦住给个人话，而不是让它 OOM。
    last = max(float(e["start"]) + float(e.get("dur") or 0) for e in ev)
    if last > 1200:
        raise HumanError(
            f"这首有 {last/60:.0f} 分钟，网页里一次性渲染太吃内存。"
            f"先用命令行：python -m sky15.pianorender 谱子.smc --mp3")
    lib = PR.Library()
    a = PR.render(ev, lib, verbose=False)
    wav = PIANO_DIR / f"{jid}_{tag}_{h}.wav"
    with _PIANO_LOCK:
        try:
            PR.write_wav(a, wav)
            PR.to_mp3(wav, mp3)
            data = mp3.read_bytes()
        finally:
            try:
                wav.unlink()
            except OSError:
                pass
    _piano_gc()
    return data, label


def _fill_gaps(ev, min_gap: float = 0.12, max_hold: float = 2.0):
    """把音延长到下一个音起头，补掉谱面上的空洞。

    为什么需要：导出来的 MusicXML 常常缺音，漏掉的音就变成空拍，
    直接弹出来就是「断断续续」。这里是**听感修补**，不改变谱面本身，
    只在渲染音频时让上一个音继续响着，听起来像连奏。
    """
    ev = sorted(ev, key=lambda e: e["start"])
    for i, e in enumerate(ev):
        nxt = ev[i + 1]["start"] if i + 1 < len(ev) else None
        if nxt is None:
            continue
        if nxt - e["start"] > e["dur"] + min_gap:
            e["dur"] = min(max_hold, nxt - e["start"])
    return ev


def _coverage(ev) -> float:
    """有音覆盖的时间比例（10ms 网格）。"""
    if not ev:
        return 0.0
    last = max(float(e["start"]) + float(e.get("dur") or 0) for e in ev)
    if last <= 0:
        return 0.0
    n = int(last / 0.01) + 2
    grid = bytearray(n)
    for e in ev:
        a = max(0, int(float(e["start"]) / 0.01))
        b = min(n, int((float(e["start"]) + float(e.get("dur") or 0)) / 0.01))
        for i in range(a, b):
            grid[i] = 1
    return sum(grid) / n


def _piano_gc(keep: int = 40):
    try:
        files = sorted(PIANO_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for p in files[keep:]:
            p.unlink()
    except OSError:
        pass


def _render_to(text: str, mode: str, theme: str = "dark", dpi: int = 150,
               page: int = 1, jid: str = "", title: str = "",
               bars: int = 0, rows: int = 0, crop: bool = False) -> bytes:
    """统一出口。mode: preview / long / page / pagefit / pages / pdf。带磁盘缓存。
    bars>0 时按指定「一行几小节」排版（手机竖屏用 2，字能大一倍）。
    rows>0 时按指定「一页几行」分页（默认 4）。
    crop=True 时每页按本页实际内容裁掉下方空白（改谱子页面用）。"""
    theme = theme if theme in ("dark", "light") else "dark"
    dpi = int(_clamp(int(dpi or 150), 72, 400))
    bars = int(_clamp(int(bars or 0), 0, 8))
    rows = int(_clamp(int(rows or 0), 0, 12))
    jid = jid or hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    ck_mode = mode if not bars or bars == 4 else f"{mode}b{bars}"
    # rows 只在"和默认不同"时才进缓存键；默认是一页 8 行（主界面），改谱子页面固定传 4
    if rows and rows != 8:
        ck_mode += f"r{rows}"
    if crop:
        ck_mode += "c"
    # 排版版本号：改名/改行数/改字号这类"代码变了但参数没变"的情况，
    # 缓存键必须跟着变，否则用户看到的还是旧图（2026-10-25 一页 8 行改 4 行踩过）。
    ck_mode += "_" + _REN_VER
    ropt = {}
    if bars:
        ropt["bars_per_row"] = bars
    if rows:
        ropt["rows_per_page"] = rows
    if crop:
        ropt["crop"] = True

    ext = {"preview": "png", "long": "png", "page": "png", "pagefit": "png",
           "pages": "zip", "pdf": "pdf"}.get(mode, "png")
    cpath = _cache_path(jid, text, ck_mode, theme, dpi, page, ext)
    key = str(cpath)
    with _RENDER_LOCK:
        hit = _RENDER_INDEX.get(key)
    if hit and _cache_ok(cpath, ext):
        return cpath.read_bytes()
    if _cache_ok(cpath, ext):
        return cpath.read_bytes()

    R = mod("render")
    Buf = io.BytesIO
    if mode == "preview":
        img = R.render_first_page(text, dpi=110, theme=theme, **ropt)
        img.thumbnail((760, 1400))
        buf = Buf()
        img.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
    elif mode == "long":
        pages = R.render_pages(text, dpi=dpi, theme=theme, paginate=False, **ropt)
        buf = Buf()
        pages[0].save(buf, "PNG", optimize=True)
        data = buf.getvalue()
    elif mode == "page":
        pages = R.render_pages(text, dpi=dpi, theme=theme, **ropt)
        idx = _clamp(int(page or 1) - 1, 0, len(pages) - 1)
        buf = Buf()
        pages[idx].save(buf, "PNG", optimize=True)
        data = buf.getvalue()
    elif mode == "pagefit":
        # 第一页按内容裁边：手机上单页谱面不留整页空白，字更大
        img = R.render_first_page(text, dpi=dpi, theme=theme, **ropt)
        buf = Buf()
        img.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
    elif mode == "pages":
        pages = R.render_pages(text, dpi=dpi, theme=theme, **ropt)
        buf = Buf()
        base = _safe_filename(title or "sky15")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, img in enumerate(pages, 1):
                one = Buf()
                img.save(one, "PNG", optimize=True)
                z.writestr(f"{base}_第{i:02d}页.png", one.getvalue())
        data = buf.getvalue()
    elif mode == "pdf":
        pages = R.render_pages(text, dpi=dpi, theme=theme, **ropt)
        buf = Buf()
        pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:],
                      resolution=dpi, title=title or "sky15")
        data = buf.getvalue()
    else:
        raise HumanError(f"不认识的导出格式：{mode}")
    try:
        # 原子落盘：先写 .part 再 os.replace，避免并发读者拿到半个文件
        cpath.parent.mkdir(parents=True, exist_ok=True)
        part = cpath.with_name(cpath.name + ".part")
        part.write_bytes(data)
        os.replace(part, cpath)
        with _RENDER_LOCK:
            _RENDER_INDEX[key] = cpath
            _n = len(_RENDER_INDEX)
        # 以前 _cache_gc 只在启动时跑一次，长会话里 web_cache 只增不减
        if _n % 20 == 0:
            _cache_gc()
    except Exception:
        pass
    return data


def _page_count(text: str, dpi: int = 150, bars: int = 0, rows: int = 0) -> int:
    R = mod("render")
    opt = {"dpi": dpi}
    if bars:
        opt["bars_per_row"] = int(bars)
    if rows:
        opt["rows_per_page"] = int(rows)
    try:
        plan = R._plan(text, opt)
        return max(1, len(plan["page_rows"]))
    except Exception:
        try:
            return max(1, len(R.render_pages(text, **opt)))
        except Exception:
            return 1


def _cache_gc(max_files=240):
    try:
        files = sorted(CACHE_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
        for f in files[:-max_files] if len(files) > max_files else []:
            f.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 重排
def do_rearrange(job, p: dict) -> dict:
    """用缓存的事件表就地重排，不重新转谱。"""
    A, B = mod("arrange"), mod("beats")
    cache = job.get("_cache") or {}
    prev = job.get("result") or {}
    kind = job.get("kind") or "audio"

    time_sig = cache.get("time_sig") or prev.get("time_sig") or "4/4"
    tempo_arg = _int_or_none(p.get("tempo"))
    key = (p.get("key") or "").strip()
    if key.lower() in ("auto", "自动", "原调", ""):
        key = None
    max_notes = _clamp(_int_or_none(p.get("max_notes")) or 3, 1, 3)
    harmony = _bool(p.get("harmony"), True)
    chords = (p.get("chords") or "auto").strip().lower()
    octv = (p.get("octave") or "off").strip().lower()
    # 音区三档（界面）/ 四档（命令行）：
    #   off   —— 不动：完全按源音频音高写，可能一片不带点
    #   auto  —— 常见写法：整首偏低时唱名抬一层并打 (8vb)，发声高度不变
    #   mel   —— 只抬旋律：每格最高的音抬一层，低音不动，发声真的高 12 半音
    #   force —— 命令行 --octave high：抬得下就整首抬（同样打 8vb，声音不变）
    octave_fit = ("force" if octv in ("high", "up", "+12", "高八度", "1")
                  else "mel" if octv in ("mel", "melody", "只抬旋律", "2")
                  else "off" if octv in ("off", "none", "0")
                  else "auto")
    hs = "auto"                       # 左右手自动分，开关交给 chord_mode
    chord_mode = _chord_mode(chords)
    title = (p.get("title") or prev.get("title") or "未命名").strip() or "未命名"

    conf, grid, doubled, hit = "手动", "", 0, 1.0
    warp = None
    if cache.get("timebase") == "beats":
        tempo = tempo_arg or _int_or_none(cache.get("tempo")) or prev.get("tempo") or 120
        events = scoreio.to_seconds(cache["events"], tempo)
        phase = 0.0
        conf, grid = "手动", "按谱面速度"
        # 谱面自己带变速时，重排也得跟着谱面走（跟 `_run_score` 同一套）。
        tmap = list(cache.get("tempo_map") or [])
        if len({round(float(b), 3) for _q, b in tmap}) > 1:
            sem = mod("score_semantics")
            warp = (lambda tm: (lambda t: sem.seconds_to_slots(t, tm)))(tmap)
            grid += " · 跟随谱面变速"
    else:
        events = cache.get("events") or []
        if not events:
            raise HumanError("这份结果没有可用的音符事件，得重新转一次。",
                             advice=["把原文件重新拖进来转一次，再改参数。"])
        if tempo_arg:
            tempo, phase, why = B.select_grid(events, tempo_hint=tempo_arg)
            hit = _grid_hit(B, events, tempo, phase)
            conf = "手动" if hit is not None else "手动·速度待核"
            grid = why.get("head") or why.get("reason") or ""
            doubled = int(why.get("doubled") or 0)
        else:
            auto = cache.get("auto")
            if not auto:
                tempo, phase, why = B.select_grid(events)
                h = _grid_hit(B, events, tempo, phase, why.get("base_hit"))
                auto = {"tempo": int(tempo), "phase": float(phase), "hit": h,
                        "grid": why.get("head") or why.get("reason") or "",
                        "doubled": int(why.get("doubled") or 0),
                        "conf": _conf_of(h)}
                cache["auto"] = auto
            tempo, phase = auto["tempo"], auto["phase"]
            hit, conf = auto["hit"], auto["conf"]
            grid, doubled = auto["grid"], auto["doubled"]

    tempo = int(_clamp(int(tempo or 120), 20, 400))
    text = A.arrange(events, tempo=tempo, time_sig=time_sig, key=key, title=title,
                     offset=phase, warp=warp, harmony=harmony, max_notes=max_notes,
                     hand_split=hs, chord_mode=chord_mode, octave_fit=octave_fit)
    params = {"tempo": tempo, "key": key or "", "harmony": harmony,
              "max_notes": max_notes, "chords": chords, "octave": octv,
              "backend": prev.get("backend") or ""}
    res = _build_result(job["id"], text, kind=kind,
                        backend=prev.get("backend") or "", params=params,
                        extra={"confidence": conf,
                               "hit": (None if hit is None else int(round(hit * 100))),
                               "grid": grid, "doubled": doubled, "rearranged": True,
                               "auto_tempo": (cache.get("auto") or {}).get("tempo")})
    path = JOBS_DIR / f"{job['id']}.smc"
    path.write_text(text, encoding="utf-8")
    res["smc_path"] = str(path)
    with JOBS_LOCK:
        job["result"] = res
        job["params"] = params
        job["_cache"] = cache
        job["advice"] = res["advice"]
        job.update(tempo=res.get("tempo"), confidence=res.get("confidence"),
                   hit=res.get("hit"), grid=res.get("grid"),
                   notes=res.get("notes"), doubled=res.get("doubled"))
    _history_upsert(_hist_entry(job, res))
    return res


# ---------------------------------------------------------------- 三种 worker
def _grid_hit(B, events, tempo, phase, cached=None):
    """算网格命中率（0~1）。算不出来返回 None（=未知），绝不返回 1.0。

    以前这里是 `except Exception: hit = 1.0` —— 方向反了：算不出来本该是
    "不知道"，却变成"命中率 100%、置信度高"，用户看到的就是一句「置信度：高」。
    另一个坑是 `why.get("base_hit") or ...`：base_hit 恰好是 0.0 时被 `or` 当假，
    白算一遍，还可能把确定的 0 换成别的值。所以这里用 isinstance 判断，
    0.0 照样采纳；真是 None 才回退去算。
    """
    if isinstance(cached, (int, float)):
        return float(cached)
    try:
        return float(B.grid_quality([e["start"] for e in events], tempo, phase))
    except Exception:
        return None


def _conf_of(hit):
    """命中率 → 置信度文案。None 就是「未知」，别硬凑成「高」。"""
    if hit is None:
        return "未知"
    return "高" if hit >= 0.85 else "中" if hit >= 0.70 else "低"


def _arrange_from_events(job, events, opts, *, backend_name, bpm_guess=None,
                         fixed_tempo=None, fixed_ts=None, fixed_note=None):
    """转谱事件（秒）→ 测速 → 重排 → 出谱。

    音频任务、以及「五线谱先弹成钢琴音频再转谱」这条路，共用这一段：
    后者的重点就是让转谱模型替我们把钢琴谱里 7~9 个同时音压到 4 个以内，
    15 键每格只放 3 个音，不压就只能硬砍。

    fixed_tempo / fixed_ts：谱面自己写着速度和拍号时直接用它，不再从音频里猜。
    为什么必须这样——音频是我们自己照着谱子弹出来的，格子本来就知道；
    让测速器重新猜反而会猜成 2 倍（实测一梦繁唐 100 BPM 2/4 被猜成 199 BPM 4/4，
    12 小节变 22 小节）。
    """
    B, A = mod("beats"), mod("arrange")
    _stage(job, "tempo", 0.35)
    hint = _int_or_none(opts.get("tempo"))
    ts = fixed_ts or "4/4"
    if fixed_tempo:
        tempo, phase, why = float(fixed_tempo), 0.0, {}
        hit, conf, doubled = 1.0, "谱面", 0
        grid = fixed_note or f"按谱面速度 {int(round(tempo))} BPM · {ts}"
        # 把"谱面速度"当成这首歌的自动值存进 cache：用户在调参里点「恢复自动测速」
        # 时会走 do_rearrange 的 auto 分支，如果不存这一条，就会拿音频重新猜一遍
        # （实测 4/4 谱的 100 被猜成 199，用户看到的就是"点一下速度就变了"）。
        job["_cache"] = {"timebase": "sec", "events": events, "time_sig": ts,
                         "from_sheet": True,
                         "auto": {"tempo": int(round(tempo)), "phase": 0.0,
                                  "hit": 1.0, "conf": "谱面", "grid": grid,
                                  "doubled": 0}}
        _stage(job, "tempo", 1.0)
        _ck(job)
        job["params"] = dict(opts, tempo=int(round(tempo)), key=(opts.get("key") or ""),
                             backend=backend_name, confidence=conf, hit=100, grid=grid)
        _stage(job, "arrange", 0.3)
        _ck(job)
        text = A.arrange(events, tempo=int(round(tempo)), time_sig=ts,
                         key=(opts.get("key") or "").strip() or None,
                         title=_pretty_title(job.get("name") or ""), offset=0.0,
                         harmony=_bool(opts.get("harmony"), True),
                         max_notes=_clamp(_int_or_none(opts.get("max_notes")) or 3, 1, 3),
                         hand_split="auto", chord_mode=_chord_mode(opts.get("chords")),
                         octave_fit=(lambda o: "force" if o in ("high", "up", "+12", "高八度", "1")
                                     else "mel" if o in ("mel", "melody", "只抬旋律", "2")
                                     else "off" if o in ("off", "none", "0")
                                     else "auto")((opts.get("octave") or "").lower()))
        _stage(job, "arrange", 1.0)
        if job.get("_via_audio") and not job["_via_audio"].get("fallback"):
            grid = f"{grid} · 先弹钢琴音频再转谱"
        _finish(job, text, backend=backend_name,
                extra={"confidence": conf, "hit": 100, "grid": grid,
                       "doubled": int(why.get("doubled") or 0),
                       "audioq": job.get("_audioq"), "rubato": {"applied": False},
                       "tempo_alts": [], "bpm_guess": _int_or_none(bpm_guess),
                       "via_audio": bool(job.get("_via_audio"))})
        return

    if hint:
        tempo, phase, why = B.select_grid(events, tempo_hint=hint)
        hit = _grid_hit(B, events, tempo, phase)
        conf = "手动" if hit is not None else "手动·速度待核"
        doubled = int(why.get("doubled") or 0)
        # 速度可能是文件名字里读回来的（我们导出的钢琴声），那句话要如实显示
        grid = job.pop("_tempo_note", None) or "手填速度"
        job["_cache"] = {"timebase": "sec", "events": events, "time_sig": "4/4"}
    else:
        tempo, phase, why = B.select_grid(events)
        hit = _grid_hit(B, events, tempo, phase, why.get("base_hit"))
        conf = _conf_of(hit)
        doubled = int(why.get("doubled") or 0)
        # 测速没把握时别让界面只显示一句「低」：速度错了整首都会错格，
        # 这是"给错谱"里最贵的一种，必须说清"可能不准、请手填"。
        if not why.get("ok", True):
            _why_reason = str(why.get("reason") or "").strip()
            if "速度" not in _why_reason:
                why = dict(why, reason=(_why_reason or "自动测速") + "（速度可能不准）")
        # `why["head"]` 是数字（小节头命中率 0~1），`reason` 才是给人看的那句话。
        _head = why.get("head")
        _reason = str(why.get("reason") or "").strip()
        grid = (f"{_reason} · 命中 {_head * 100:.0f}%" if isinstance(_head, (int, float))
                else _reason)
        job["_cache"] = {"timebase": "sec", "events": events, "time_sig": "4/4",
                         "auto": {"tempo": int(tempo), "phase": float(phase),
                                  "hit": hit if hit is not None else 0.0,
                                  "conf": conf, "grid": grid,
                                  "doubled": doubled}}
    _stage(job, "tempo", 1.0)
    _ck(job)

    key = (opts.get("key") or "").strip() or None
    if key and key not in layout.KEY_MIDI:
        key = None
    job["params"] = dict(opts, tempo=int(tempo), key=key or "",
                         backend=backend_name, confidence=conf,
                         hit=int(round(hit * 100)), grid=grid)
    _stage(job, "arrange", 0.35)
    _ck(job)
    # 速度起伏（rubato）：恒速网格对得上就完全不动，对不上才换分段网格。
    # 干净音频实测 gq>=0.895（闸门 0.65 永不触发，结果与不加这条逐位相同），
    # 真演奏漂 ±1~2% 时 gq 掉到 0.43~0.61 → 启用后 ±1% 从 21 分救到 75~96。
    rubato_mode = (opts.get("rubato") or "auto").lower()
    warp, winfo = None, {}
    try:
        warp, winfo = mod("warptrack").autowarp(events, tempo=float(tempo),
                                               mode=rubato_mode)
    except Exception as exc:                       # 拍点跟踪坏了不能影响出谱
        log(f"autowarp 失败，按恒速出谱：{exc}")
    kw = {}
    if warp is not None:
        kw["tempo_exact"] = float(winfo.get("tempo") or tempo)
    job["rubato"] = {"applied": warp is not None, "mode": rubato_mode,
                     "gq": round(float(winfo.get("gq") or 0.0), 3),
                     "why": str(winfo.get("why") or "")}
    text = A.arrange(events, tempo=int(tempo), time_sig="4/4", key=key,
                     title=_pretty_title(job.get("name") or ""), offset=phase,
                     harmony=_bool(opts.get("harmony"), True),
                     max_notes=_clamp(_int_or_none(opts.get("max_notes")) or 3, 1, 3),
                     hand_split="auto", chord_mode=_chord_mode(opts.get("chords")),
                     octave_fit=(lambda o: "force" if o in ("high", "up", "+12", "高八度", "1")
                                 else "mel" if o in ("mel", "melody", "只抬旋律", "2")
                                 else "off" if o in ("off", "none", "0")
                                 else "auto")((opts.get("octave") or "").lower()),
                     warp=warp, **kw)
    _stage(job, "arrange", 1.0)
    via = job.get("_via_audio") or {}
    if via and not via.get("fallback"):
        grid = f"{grid} · 先弹钢琴音频再转谱"
    _finish(job, text, backend=backend_name,
            extra={"confidence": conf, "hit": int(round(hit * 100)),
                   "grid": grid, "doubled": doubled,
                   "audioq": job.get("_audioq"),
                   "rubato": job.get("rubato"),
                   "tempo_alts": _tempo_alts(why, tempo),
                   "bpm_guess": _int_or_none(bpm_guess),
                   "via_audio": bool(job.get("_via_audio"))})


def _sheet_to_audio_events(job, secs):
    """谱面事件（秒）→ 用 Salamander 弹成钢琴音频 → 转谱，返回转谱事件。

    为什么绕这一圈（用户 2026-10-23 试听后点名要这条）：
    五线谱里钢琴一按就是 6~9 个同时音，15 键一格最多 3 个，直排必然硬砍；
    先弹成钢琴音频再过一遍转谱模型，模型会按「听感上真正突出的音」压到
    3~4 个，而且顺带把力度、踏板、连奏都带进来，出谱更像人在编配。
    """
    PR, T = mod("pianorender"), mod("transcribe")
    tmp = OUT / "via_audio" / (job.get("id") or "x")
    tmp.mkdir(parents=True, exist_ok=True)
    _stage(job, "read", 0.6, note="先弹成钢琴音频")
    buf = PR.render(secs, PR.lib(), normalize=True, verbose=False)
    wav = tmp / "sheet.wav"
    PR.write_wav(buf, wav)
    _stage(job, "read", 0.8, note="钢琴音频→再转谱")
    _ck(job)
    ev, bpm, name = T.transcribe(str(wav))
    # 转谱会自动裁掉开头静音（TRIM_SILENCE），事件时间就从"裁完之后"算起。
    # 谱子开头可能是休止符，不补回这段偏移，整首会往前挪几格。
    shift = float(getattr(T, "LAST_TRIM", {}).get("offset") or 0.0)
    if shift > 1e-6 and ev:
        for e in ev:
            e["start"] = float(e["start"]) + shift
            if e.get("end") is not None:
                e["end"] = float(e["end"]) + shift
    job["_via_audio"] = {"wav": str(wav), "backend": name,
                         "sec": round(len(buf) / PR.SR, 2), "notes": len(ev or []),
                         "trim": round(shift, 3)}
    if not ev:
        raise HumanError("先把谱弹成钢琴音频，但转谱这一步一个音都没听出来。",
                         advice=["换成直接出谱试试（调参里关掉「先弹成音频再转谱」）。"])
    return ev, name


_BPM_IN_NAME = re.compile(r"(\d{2,3})\s*BPM", re.I)


def _tempo_from_name(name):
    """从文件名里读回我们自己导出的那个速度。

    导出的「钢琴声」就是按谱面速度弹出来的，文件名形如
    `雨爱_钢琴声（原谱·100BPM）.mp3`——用户拖回来时直接采信它，
    比重新从音频里猜稳得多（猜成一半/两倍会让整首错位）。
    只有名字里带「钢琴声」才认，避免误判用户自己的文件名。
    """
    s = str(name or "")
    if "钢琴声" not in s:
        return None
    m = _BPM_IN_NAME.search(s)
    if not m:
        return None
    try:
        v = int(m.group(1))
    except Exception:
        return None
    return v if 20 <= v <= 400 else None


def _run_audio(job):
    T = mod("transcribe")
    path = job["_payload"]["path"]
    opts = job["params"]
    wav, tmpdir = path, None
    _stage(job, "probe", 0.05)
    _ck(job)
    ext = os.path.splitext(path)[1].lower()
    # 录音体检：在花 20 秒跑模型之前先看一眼，结果随任务返回给前端显示。
    # 不限扩展名 —— 上传落盘时可能没有真正后缀（临时文件名是 .bin），这里只管内容。
    try:
        job["_audioq"] = mod("audioq").analyze(path)
    except Exception:
        job["_audioq"] = None
    if ext not in (".wav", ".flac"):
        try:
            wav = T.to_wav(path)
        except Exception as e:
            raise HumanError("抽取音频轨道失败，多半是没装 ffmpeg 或者这个文件格式不常见。",
                             advice=["装一个 ffmpeg（winget install Gyan.FFmpeg）后重开界面，",
                                     "或者自己先转成 .wav 再拖进来。"]) from e
        tmpdir = os.path.dirname(wav)
    _stage(job, "probe", 1.0)
    _ck(job)

    _stage(job, "transcribe", 0.02)
    backend = (opts.get("backend") or "").strip() or None
    try:
        events, _bpm_guess, backend_name = T.transcribe(wav, backend=backend)
    except RuntimeError as e:
        msg = str(e)
        if "ffmpeg" in msg.lower():
            raise HumanError("转谱需要 ffmpeg 来读音频，本机 PATH 里找不到。",
                             advice=["winget install Gyan.FFmpeg 之后重开界面。",
                                     "已装的话，确认 ffmpeg.exe 在 PATH 里。"]) from e
        raise
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if not events:
        raise HumanError("一个音符都没听出来。",
                         advice=["确认文件里确实有声音（不是静音、不是纯人声说话）。",
                                 "手机外放翻录的、环境吵的，识别率会掉得很厉害；",
                                 "尽量用录音笔/内录/清一点的手机贴近录。"],
                         code="no_notes")
    _stage(job, "transcribe", 1.0)
    _ck(job)
    # 文件名里带着我们自己导出的速度（`…_钢琴声（原谱·100BPM）.mp3`）时直接采信：
    # 那段音频就是按这个速度弹的，重新猜只会猜错（用户 2026-10-23 就是因为这个
    # 发现“五线谱直接转”和“转成 mp3 再拖回来”结果不一样）。
    if not _int_or_none(opts.get("tempo")):
        t_name = _tempo_from_name(job.get("name") or "")
        if t_name:
            opts = dict(opts, tempo=t_name)
            job["params"] = opts
            job["_tempo_note"] = f"速度取自文件名 {t_name} BPM"
    return _arrange_from_events(job, events, opts,
                                backend_name=backend_name, bpm_guess=_bpm_guess)


def _run_score(job):
    MX, A = mod("musicxml"), mod("arrange")
    path = job["_payload"]["path"]
    opts = job["params"]
    _stage(job, "read", 0.1)
    _ck(job)
    try:
        parsed = MX.parse_full(path)
        events, tempo_file, time_sig, key_file = (
            parsed.events, parsed.tempo, parsed.time_sig, parsed.key)
    except Exception as e:
        raise HumanError("这份 MusicXML 没读通，可能是压缩包坏了或者版本不标准。",
                         advice=["用打谱软件另存为 MusicXML（未压缩的 .musicxml）再试。",
                                 ".mxl 是 zip，先解压出里面的 .xml 也可以。"]) from e
    if not events:
        raise HumanError("谱面里没解析到音符。",
                         advice=["确认谱子里确实写着音符，而不是只有标题和文字。"],
                         code="no_notes")
    _stage(job, "read", 1.0)
    _ck(job)

    _stage(job, "tempo", 0.5)
    # 手填速度 = 用户要的**格子速度**（跟站点那个"速度"字段一致）；
    # 没填就用谱子自带的（可能中途变速，走 tempo_map，
    # 反复/一二结尾/D.C./D.S. 已经在 parse_full 里展开成演奏顺序）
    time_sig = time_sig or "4/4"
    want = _int_or_none(opts.get("tempo"))
    warp = None
    if want:
        tempo_grid = float(_clamp(want, 20, 400))
        # 谱面按"每分钟四分音符"算秒，格子速度要换算回去
        tempo = tempo_grid * _bar_quarters(time_sig) / 4.0
        secs = MX.to_seconds(events, tempo)
        grid_note = f"手动 {int(tempo_grid)} BPM"
    else:
        tempo = int(_clamp(_int_or_none(tempo_file) or 120, 20, 400))
        tempo_grid = _grid_tempo(tempo, time_sig)
        secs = parsed.to_seconds()
        grid_note = _tempo_note(parsed, tempo)
        grid_note += _grid_tail(tempo, tempo_grid, time_sig)
        # 谱面自己写了变速（渐快/渐慢/换段）时，秒已经按分段速度积分过，
        # 而格子只有一个固定 tempo —— 直接喂进去，变速段的音会被抖到格子外。
        # 这里把「秒 → 拍」反推回来除以 0.5 当 warp，格子就跟着谱面的速度走。
        # 单一速度的谱子 warp 与固定格子逐位等价，所以只在真的分了段时才挂。
        tmap = list(getattr(parsed, "tempo_map", None) or [])
        if len({round(float(b), 3) for _p, b in tmap}) > 1:
            try:
                sem = mod("score_semantics")
                warp = (lambda tm: (lambda t: sem.seconds_to_slots(t, tm)))(tmap)
                grid_note += " · 跟随谱面变速"
            except Exception as exc:
                log(f"变速映射建不起来，按固定速度出谱：{exc}")
    tempo = int(round(tempo_grid))
    # 十六分格（速度 ×2）默认关：2026-10-23 用户听过 A/B（9009 十六分格 / 9010 八分格）
    # 之后说「算了你改回来吧，我之后不用五线谱转了，不太行」——密谱并没有换来更好听，
    # 只是谱子长一倍、速度数字翻倍，反而更碎。判定代码留在 sky15/grid.py 里没删，
    # 想再试就把 max_factor 改回 2（还能用 --grid16 之类的手动开关）。
    AUTO_DOUBLE_GRID = False
    if AUTO_DOUBLE_GRID and not want and warp is None:
        try:
            gmod = mod("grid")
            factor, ginfo = gmod.choose_factor(secs, tempo)
            if factor > 1:
                tempo, time_sig = gmod.apply_factor(tempo, factor, time_sig)
                grid_note += f" · 十六分格（速度 ×{factor}）"
                print(f"格子翻倍：{ginfo.get('why')}", flush=True)
        except Exception as exc:
            print(f"格子判定跳过：{exc}", flush=True)
    _stage(job, "tempo", 1.0)
    _ck(job)

    key = (opts.get("key") or "").strip() or None
    if key and key not in layout.KEY_MIDI:
        key = None
    _stage(job, "arrange", 0.1)
    _ck(job)
    # 「钢琴声」要的是原谱，不是压进 15 键之后的那份（后者每格最多 3 音，
    # 雨爱那张图实测丢 25% 的音）。这里把秒级事件留着给它用。
    job["_src_seconds"] = secs

    # 默认走「先弹成钢琴音频，再当音频转谱」（用户 2026-10-23 试听后点名要的）。
    # 关掉的办法：参数里 via_audio=off，或者犯错了随时改回来。
    #
    # 2026-10-23 试过「上传的本来就是 xml 时直读、不过音频」：同一份 PhotoScore
    # xml 直读 211 音 / 走音频 212 音，差 1 个音等于没差，但用户明确希望所有输入
    # 走同一条路（他怀疑差异来自流程），所以这里保持统一，不再按后缀分流。
    if _bool(opts.get("via_audio"), True):
        try:
            ev2, _bk = _sheet_to_audio_events(job, secs)
            return _arrange_from_events(job, ev2, opts, backend_name=_bk,
                                        fixed_tempo=tempo, fixed_ts=time_sig, fixed_note=grid_note)
        except Exception as exc:                   # 音色/转谱坏了不能连谱都不出
            log(f"谱面→钢琴音频这条路失败，回退直排：{exc}")
            job["_via_audio"] = {"fallback": f"{type(exc).__name__}: {exc}"[:160]}

    text = A.arrange(secs, tempo=tempo, time_sig=time_sig, key=key,
                     title=_pretty_title(job.get("name") or ""), offset=0.0,
                     warp=warp,
                     harmony=_bool(opts.get("harmony"), True),
                     max_notes=_clamp(_int_or_none(opts.get("max_notes")) or 3, 1, 3),
                     hand_split="auto", chord_mode=_chord_mode(opts.get("chords")))
    job["_cache"] = {"timebase": "beats", "events": events, "tempo": tempo,
                     "time_sig": time_sig,
                     "tempo_map": list(getattr(parsed, "tempo_map", None) or [])}
    job["params"] = dict(opts, tempo=tempo, key=key or "", backend="musicxml",
                         confidence="手动", hit=100, grid=grid_note)
    _stage(job, "arrange", 1.0)
    _finish(job, text, backend="musicxml",
            extra={"confidence": "手动", "hit": 100,
                   "grid": grid_note, "doubled": 0,
                   "source_tempo": _int_or_none(tempo_file),
                   "score_warnings": len(getattr(parsed, "warnings", []) or [])})


def _run_smc(job):
    text = job["_payload"]["text"]
    _stage(job, "read", 0.4)
    _ck(job)
    meta = smc.parse_meta(text)
    events, _m = scoreio.smc_to_events(text)
    job["_cache"] = {"timebase": "beats", "events": events,
                     "tempo": _int_or_none(meta.get("tempo")) or 120,
                     "time_sig": meta.get("time_sig") or "4/4"}
    job["params"] = {"tempo": _int_or_none(meta.get("tempo")) or 120,
                     "key": meta.get("key") or "", "harmony": True,
                     "max_notes": 3, "chords": "auto", "backend": ""}
    _stage(job, "read", 1.0)
    _finish(job, text, backend="",
            extra={"confidence": "手动", "hit": 100, "grid": "导入的现成谱面",
                   "doubled": 0})


_DISPATCH = {"audio": _run_audio, "score": _run_score, "smc": _run_smc}


def _humanize(exc) -> tuple:
    if isinstance(exc, HumanError):
        return str(exc), exc.advice
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "ffmpeg" in low:
        return ("转谱需要 ffmpeg，本机没找到。",
                ["winget install Gyan.FFmpeg，然后重开界面。"])
    if "no module named" in low or "importerror" in low:
        return (f"缺少运行这个功能需要的组件：{msg}",
                ["按 README 的安装那段把依赖装上，再重开界面。"])
    if "out of memory" in low or "cuda" in low:
        return ("显存/内存不够了。",
                ["关掉别的占内存的程序；或者把「转谱后端」切成「快速旋律」再试。"])
    if isinstance(exc, FileNotFoundError):
        return ("文件不见了或者路径读不到。",
                ["重新拖一次原文件；上传一半就被关掉的情况也会这样。"])
    if isinstance(exc, PermissionError):
        return ("文件被别的程序占着，写不进去。",
                ["关掉正在编辑这个文件的软件再试。"])
    return (f"出错了：{msg}",
            ["换个文件再试一次；一直这样的话，把 out\\web.log 最后一段发出来。"])


def _worker():
    while True:
        jid = JOBQ.get()
        job = JOBS.get(jid)
        if job is None:
            continue
        with JOBS_LOCK:
            if jid in PENDING:
                PENDING.remove(jid)
            for i, qid in enumerate(PENDING):
                if qid in JOBS:
                    JOBS[qid]["queue_pos"] = i + 1
            job["queue_pos"] = 0
        if job.get("_cancel"):
            _set(job, status="cancelled", stage="已取消", progress=0.0)
            continue
        _set(job, status="running", stage="开始")
        try:
            _DISPATCH[job["kind"]](job)
        except _Cancelled:
            _set(job, status="cancelled", stage="已取消", result=None,
                 advice=[{"level": "info", "text": "已取消。原文件没删，随时可以重新上传。"}])
        except Exception as exc:
            traceback.print_exc()
            msg, advice = _humanize(exc)
            _set(job, status="error", stage="出错", error=msg,
                 advice=[{"level": "error", "text": a} for a in advice] or
                        [{"level": "error", "text": msg}],
                 result=None)
        finally:
            with JOBS_LOCK:
                job["elapsed"] = round(time.time() - job.get("_t0", time.time()), 1)


def start_worker():
    global _WORKER_STARTED
    with JOBS_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    threading.Thread(target=_worker, name="sky15-worker", daemon=True).start()


# ---------------------------------------------------------------- HTTP
class App(SimpleHTTPRequestHandler):
    server_version = "sky15web/" + VERSION

    def __init__(self, *a, **kw):
        kw.setdefault("directory", str(WEB))
        super().__init__(*a, **kw)

    # 静音访问日志（刷屏没意义），出错还是打
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    # ---------- 输出helper
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _bytes(self, data, mime, filename=None, inline=True, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "sky15"
            disp = "inline" if inline else "attachment"
            self.send_header("Content-Disposition",
                             f"{disp}; filename=\"{ascii_name}\"; "
                             f"filename*=UTF-8''{_quote(filename)}")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _err(self, code, message, advice=None, **extra):
        body = {"error": message, "ok": False}
        if advice:
            body["advice"] = advice
        body.update(extra)
        self._json(body, code)

    def _body(self, limit=512 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > limit:
            raise HumanError(f"文件太大了（{n // 1024 // 1024} MB），超过 {limit // 1024 // 1024} MB 不收。",
                             advice=["先剪一小段再转，或者自己压一下音频。"])
        chunks, left = [], n
        while left > 0:
            b = self.rfile.read(min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)

    @staticmethod
    def _q1(q, key, default=""):
        v = q.get(key)
        return (v[0] if isinstance(v, list) and v else v) or default

    # ---------- 路由
    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            p = u.path
            q = urllib.parse.parse_qs(u.query)
            if p.startswith("/api/"):
                return self._get_api(p, q)
            # 静态根就是 web\ 目录本身，server.py / midiout.py / scoreio.py 都躺在旁边。
            # 以前 GET /server.py 能直接把源码下走（含 SAFE_EXT、_REN_VER、路径布局），
            # 跟任何路径类问题配合都更好利用。这里只放行前端真需要的静态资源。
            # 注意：`/` 本身是首页，SimpleHTTPRequestHandler 会自己映射到 index.html，
            # 所以这里只拦「以 / 结尾但不是根」的目录请求，别把首页拦掉。
            pu = urllib.parse.unquote(p)                             # 防 %2e%2e / %2f 绕过
            segs = pu.replace("\\", "/").split("/")                  # Windows 上 \ 也是分隔符
            last = segs[-1].lower()
            if (".." in segs or pu.startswith("/.") or (p != "/" and p.endswith("/"))
                    or last.endswith((".py", ".pyc", ".bat", ".log", ".json", ".pth"))):
                return self._err(404, "没有这个文件")
            return super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                msg, advice = _humanize(e)
                self._err(500, msg, advice)
            except Exception:
                pass

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            return self._post_api(u.path, urllib.parse.parse_qs(u.query))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except HumanError as e:
            # HumanError.code 是人话错误码（字符串），不是 HTTP 状态码——不能再往 code= 里塞，
            # 否则会撞上 _err 的第一个位置参数，直接 TypeError（调参重排曾因此 500）
            extra = {"hcode": e.code} if e.code else {}
            self._err(400, str(e), e.advice, **extra)
        except Exception as e:
            traceback.print_exc()
            msg, advice = _humanize(e)
            try:
                self._err(500, msg, advice)
            except Exception:
                pass

    def do_DELETE(self):
        try:
            p = urllib.parse.urlparse(self.path).path
            if p.startswith("/api/history/"):
                jid = p.strip("/").split("/")[-1]
                if _jid_bad(jid):
                    return self._err(404, "没有这个记录。")
                return self._history_delete(jid)
            self._err(404, "没有这个接口")
        except Exception:
            traceback.print_exc()

    # ---------- GET
    def _get_api(self, p, q):
        parts = [x for x in p.strip("/").split("/") if x]
        if p == "/api/ping":
            return self._json({"ok": True, "version": VERSION,
                               "jobs": len(JOBS), "pending": len(PENDING),
                               "history": len(HISTORY)})
        if p == "/api/history":
            with HIST_LOCK:
                items = [dict(x) for x in HISTORY]
            for it in items:
                it["available"] = (JOBS_DIR / f"{it['id']}.smc").exists() or \
                                  (WORK / f"{it['id']}.smc").exists()
            return self._json({"items": items, "history": items, "count": len(items)})
        if len(parts) < 3:
            return self._err(404, "没有这个接口")
        kind, jid = parts[1], parts[2]
        if _jid_bad(jid):
            return self._err(404, "没有这个任务，可能已经被清掉了。")
        if kind == "job":
            job = _get_job(jid)
            if job is None:
                return self._err(404, "找不到这个任务，可能已经被清掉了。",
                                 ["重新上传一次就行；历史里的记录还能点开看。"])
            return self._json(_public(job))
        if kind == "edit":
            return self._edit_get(jid, q)
        if kind in ("download", "image", "export", "info"):
            job = _get_job(jid)
            text = _smc_text(jid)
            if text is None:
                return self._err(404, "这个任务的谱面文件不在了。",
                                 ["重新上传一次；或者从历史里删掉这条记录。"])
            if kind == "download":
                title = (job or {}).get("result", {}).get("title") or _title_of(text)
                fname = _safe_filename(title) + ".smc"
                return self._bytes(text.encode("utf-8"),
                                   "text/plain; charset=utf-8", fname,
                                   inline=False)
            if kind == "info":
                s = scoreio.summary(text)
                dpi = int(_clamp(int(self._q1(q, "dpi", 150) or 150), 72, 400))
                bars = int(_clamp(int(_int_or_none(self._q1(q, "bars")) or 0), 0, 8))
                rows = int(_clamp(int(_int_or_none(self._q1(q, "rows")) or 0), 0, 12))
                s.update({"pages": _page_count(text, dpi, bars, rows), "dpi": dpi,
                          "bars_per_row": bars, "rows_per_page": rows or 4,
                          "title_file": _safe_filename(s["title"])})
                return self._json(s)
            if kind == "image":
                return self._serve_image(jid, text, q)
            return self._serve_export(jid, text, q, job)
        return self._err(404, "没有这个接口")

    def _serve_image(self, jid, text, q):
        """老接口，参数语义冻结：fmt=png 长图 / pdf / preview。"""
        fmt = (self._q1(q, "fmt", "png") or "png").lower()
        theme = self._q1(q, "theme", "dark")
        dpi = int(_clamp(int(self._q1(q, "dpi", 150) or 150), 72, 400))
        title = _title_of(text)
        if fmt == "pdf":
            data = _render_to(text, "pdf", theme, dpi, 1, jid, title)
            return self._bytes(data, "application/pdf",
                               _safe_filename(title) + ".pdf", inline=True)
        if fmt == "preview":
            data = _render_to(text, "preview", theme, 110, 1, jid, title)
            return self._bytes(data, "image/png", None, inline=True)
        data = _render_to(text, "long", theme, dpi, 1, jid, title)
        return self._bytes(data, "image/png", _safe_filename(title) + ".png",
                           inline=True)

    def _serve_export(self, jid, text, q, job=None):
        mode = (self._q1(q, "mode", "long") or "long").lower()
        theme = self._q1(q, "theme", "dark")
        dpi = int(_clamp(int(self._q1(q, "dpi", 150) or 150), 72, 400))
        page = int(_int_or_none(self._q1(q, "page")) or 1)
        bars = int(_clamp(int(_int_or_none(self._q1(q, "bars")) or 0), 0, 8))
        rows = int(_clamp(int(_int_or_none(self._q1(q, "rows")) or 0), 0, 12))
        crop = str(self._q1(q, "crop") or "") in ("1", "true", "yes", "on")
        title = _title_of(text)
        base = _safe_filename(title)
        if mode == "smc":
            return self._bytes(text.encode("utf-8"), "text/plain; charset=utf-8",
                               base + ".smc", inline=False)
        if mode == "midi":
            meta = smc.parse_meta(text)
            groups = smc.parse_groups(text)
            notes = scoreio.groups_to_notes(groups, meta)
            data = midiout.build(notes, title=meta.get("title") or title,
                                 tempo=int(meta.get("tempo") or 120),
                                 time_sig=meta.get("time_sig") or "4/4")
            return self._bytes(data, "audio/midi", base + ".mid", inline=False)
        if mode == "piano":
            # 谱面是从 MusicXML 来的，就弹原谱的每一个音（不经过 15 键压缩）；
            # 从音频来的或已经是 15 键谱，就弹屏幕上这份。
            src = (job or {}).get("_src_seconds")
            want = (self._q1(q, "src", "") or "").lower()
            if want == "keys":
                src = None
            # MusicXML 常缺音，谱面上一半是空拍，直接弹就是断断续续。
            # 这类曲子默认按连奏补洞（只影响音频，不动谱面），?legato=0 可关。
            lg = (self._q1(q, "legato", "") or "").lower()
            kind = (job or {}).get("kind") or ""
            legato = (lg not in ("0", "off", "no"))
            data, label = _piano_audio(text, jid, src, legato=legato)
            if legato:
                label += "·连奏"
            # 文件名里带上速度：这段音频是我们自己按这个速度弹出来的，
            # 用户把它再拖回来时就不用重新猜速度了（猜错会整首错位，用户 2026-10-23 踩过）。
            try:
                bpm_tag = int(round(float(smc.parse_meta(text).get("tempo") or 0)))
            except Exception:
                bpm_tag = 0
            if bpm_tag:
                label += f"·{bpm_tag}BPM"
            # 响应头只能是 latin-1，中文 label 放文件名里，头里给 ascii 代号
            code = ("score" if "原谱" in label else "keys") + ("+legato" if legato else "")
            return self._bytes(data, "audio/mpeg",
                               base + f"_钢琴声（{label}）.mp3", inline=False,
                               extra={"X-Piano-Source": code})
        if mode == "info":
            return self._json({"pages": _page_count(text, dpi, bars, rows), "bars": bars})
        if mode == "preview":
            data = _render_to(text, "preview", theme, 110, 1, jid, title)
            return self._bytes(data, "image/png", None, inline=True)
        if mode == "pdf":
            data = _render_to(text, "pdf", theme, dpi, 1, jid, title, bars, rows)
            return self._bytes(data, "application/pdf", base + ".pdf", inline=False)
        if mode == "pages":
            data = _render_to(text, "pages", theme, dpi, 1, jid, title, bars, rows)
            return self._bytes(data, "application/zip", base + "_分页.zip", inline=False)
        if mode == "page":
            data = _render_to(text, "page", theme, dpi, page, jid, title, bars, rows, crop)
            return self._bytes(data, "image/png",
                               f"{base}_第{page}页.png", inline=True,
                               extra={"X-Page": page})
        if mode == "pagefit":
            data = _render_to(text, "pagefit", theme, dpi, 1, jid, title, bars, rows)
            return self._bytes(data, "image/png", base + "_第一页.png",
                               inline=True, extra={"X-Page": 1})
        data = _render_to(text, "long", theme, dpi, 1, jid, title, bars, rows)
        return self._bytes(data, "image/png", base + "_长图.png", inline=False)

    # ---------- POST
    def _post_api(self, p, q):
        parts = [x for x in p.strip("/").split("/") if x]
        if len(parts) >= 3 and _jid_bad(parts[2]):
            return self._err(404, "没有这个接口")
        if p == "/api/convert":
            return self._convert(q)
        if p == "/api/example":
            return self._example()
        if len(parts) >= 3 and parts[1] == "rearrange":
            return self._rearrange(parts[2])
        if len(parts) >= 3 and parts[1] == "cancel":
            return self._cancel(parts[2])
        if len(parts) >= 3 and parts[1] == "edit":
            return self._edit_save(parts[2])
        if len(parts) >= 4 and parts[1] == "history":
            jid, act = parts[2], parts[3]
            if act == "rename":
                return self._history_rename(jid)
            if act == "delete":
                return self._history_delete(jid)
        return self._err(404, "没有这个接口")

    def _convert(self, q):
        name = (self._q1(q, "name") or "上传文件").strip()
        ext = os.path.splitext(name)[1].lower()
        if not SAFE_EXT.match(ext):
            ext = ".bin"
        raw = self._body()
        if not raw:
            raise HumanError("没收到文件内容（0 字节）。",
                             advice=["重新拖一次；有些浏览器在文件被别处打开时会传空。"])
        if ext in SHEET_EXT:
            kind = "score"
        elif ext in IMAGE_EXT:
            # 图片/PDF 识谱在 1.0 里整块删掉了。
            # 保留这个分支只为把话说清楚 —— 别让它掉进下面的 ffmpeg 分支，
            # 那会报「音频解码失败」，用户会以为文件坏了。
            raise HumanError("五线谱图片/PDF 的识别功能已经删了。",
                             advice=["要转谱就传音频（mp3 / wav / flac / m4a）。",
                                     "手里已经是打谱软件导出的谱，就直接拖 .musicxml / .mxl 进来。",
                                     "手里只有图片的话，用 MuseScore 之类把音高等记一遍导成 MusicXML，"
                                     "或者干脆对着图片把音敲进 15 键面板。"])
        elif ext in SMC_EXT:
            kind = "smc"
        elif raw[:5].lstrip().lower().startswith(b"<?xml") or raw[:2] == b"PK":
            # 后缀不认识（.bin / 没后缀 / 导出工具自己起的名），但内容明显是
            # MusicXML（明文 XML）或 .mxl（zip）—— 别丢给 ffmpeg 去抽音轨
            kind = "score"
        elif _looks_like_smc(raw):
            # 15 键谱文本：后缀可能是 .txt / .smt / 什么都没有，但里面一定有 `曲谱:[`
            kind = "smc"
        else:
            kind = "audio"

        params = {
            "tempo": _int_or_none(self._q1(q, "tempo")) or "",
            "key": (self._q1(q, "key") or "").strip(),
            "backend": (self._q1(q, "backend") or "").strip(),
            "chords": (self._q1(q, "chords") or "auto").strip(),
            "harmony": _bool(self._q1(q, "harmony"), True),
            "max_notes": _clamp(_int_or_none(self._q1(q, "max_notes")) or 3, 1, 3),
            # 界面上那个"速度起伏"选择（app.js:416 会带 ?rubato=）。以前这里没这个键，
            # 下游 :1057 的 opts.get("rubato") 永远是 "auto"，选 on/off 都没反应。
            "rubato": (self._q1(q, "rubato") or "auto").strip().lower(),
        }
        smc_text = None
        if kind == "smc":
            try:
                smc_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                smc_text = raw.decode("gbk", errors="replace")
            if "曲谱" not in smc_text:
                raise HumanError("这个谱面文件里没有「曲谱:」这一段，不是 15 键谱。",
                                 advice=["用记事本打开能看到 曲名:[...] 速度:[...] 曲谱:[...] 才对。",
                                         "五线谱图片不再支持识别，请先在 MuseScore 里导出 MusicXML。"])
        job = new_job(kind, name=name, source=name, params=params)
        jid = job["id"]
        try:
            if kind == "smc":
                job["_payload"] = {"text": smc_text}
            else:
                dest = WORK / f"{jid}{ext}"
                dest.write_bytes(raw)
                job["_payload"] = {"path": str(dest)}
        except Exception as exc:
            # 盘满 / 杀软锁住 / 没权限。new_job 已经把 jid 塞进 PENDING 了，
            # 这里不收干净的话它永远是 queued、pending 恒 >0，后面的任务全被堵住。
            with JOBS_LOCK:
                if jid in PENDING:
                    PENDING.remove(jid)
            _set(job, status="error", stage="出错", result=None,
                 error=f"文件没能存下来：{type(exc).__name__}: {exc}",
                 advice=[{"level": "error",
                          "text": "磁盘满了或文件被杀软锁住了。清点空间、换个位置再传一次。"}])
            raise HumanError("文件没能存到工作目录，这个任务已经取消。",
                             advice=[f"{type(exc).__name__}: {exc}"])
        _enqueue(job)
        return self._json({"job": jid, "id": jid, "kind": kind, "name": name,
                           "queue_pos": job.get("queue_pos", 1), "ok": True})

    def _example(self):
        demo = WEB / "demo.smc"
        if not demo.exists():
            raise HumanError("内置示例文件不见了。", advice=["从 out\\ 里拖一个 .smc 进来同样能体验。"])
        text = demo.read_text(encoding="utf-8")
        job = new_job("smc", name="示例：小星星", source="内置示例", params={})
        job["_payload"] = {"text": text}
        _enqueue(job)
        return self._json({"job": job["id"], "id": job["id"], "kind": "smc", "ok": True})

    def _rearrange(self, jid):
        job = _get_job(jid)
        if job is None:
            return self._err(404, "找不到这个任务。", ["重新上传一次。"])
        if job.get("status") in ("queued", "running"):
            return self._err(409, "这个任务还在跑，等它出结果再调参数。")
        cache = job.get("_cache")
        if not cache:
            return self._err(409, "这份结果是重启后从历史里恢复的，转谱的中间数据已经不在内存里了，改参数重排不了。",
                             ["把原文件重新拖进来转一次（可用历史里的参数），之后就能随便调。"],
                             hcode="no_cache")
        raw = self._body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # 手动改过的谱不能被「改调性/速度」悄悄冲掉——那份功夫是用户手一格一格点的。
        if job.get("edited") and not payload.get("force"):
            return self._err(409, "这首谱你手动改过，重排会把改过的格子盖回自动结果。",
                             ["想保留改动就别重排；确实要重排，再点一次就会出现「覆盖」确认。"],
                             hcode="edited")
        try:
            res = do_rearrange(job, payload)
        except HumanError as e:
            return self._err(400, str(e), e.advice)
        job["edited"] = False
        return self._json({"ok": True, "job": jid, "result": res})

    def _cancel(self, jid):
        job = JOBS.get(jid)
        if job is None:
            # 任务已经不在内存里了（跑完/出错后被回收、或者服务重启过）——
            # 前端点「×」只是想关掉这张卡片，不能因为查不到就当错误弹提示。
            return self._json({"ok": True, "status": "gone", "job": None,
                               "note": "这个任务已经结束了，卡片可以直接关掉。"})
        st = job.get("status")
        if st in ("done", "error", "cancelled"):
            return self._json({"ok": True, "status": st, "job": _public(job)})
        _set(job, _cancel=True, stage="正在停下…",
             stage_hint="正在跑模型，这一小段算完就退出（不会把已经算出来的一半丢掉）")
        if st == "queued":
            _set(job, status="cancelled", stage="已取消", progress=0.0, result=None)
            with JOBS_LOCK:
                if jid in PENDING:
                    PENDING.remove(jid)
                for i, qid in enumerate(PENDING):
                    if qid in JOBS:
                        JOBS[qid]["queue_pos"] = i + 1
        return self._json({"ok": True, "status": job.get("status"), "job": _public(job)})

    # ------------------------------------------------------------ 谱面编辑器
    def _edit_get(self, jid, q):
        """给网页编辑器用的整份谱：格子 + 那一页每个格子的像素坐标。

        格子用**纯音字符**传（`1D`，不带 `>`、不用补 0），小节线由前端按 spb 画 ——
        这样插入/删除格子之后，小节线不用跟着搬家，重新铺就行。
        """
        text = _smc_text(jid)
        if text is None:
            return self._err(404, "这个任务的谱面文件不在了。",
                             ["重新上传一次；或者从历史里删掉这条记录。"])
        E, R = mod("editor"), mod("render")
        meta, slots, spb = E.from_text(text)
        try:
            page = int(self._q1(q, "page", 1) or 1)
        except (TypeError, ValueError):
            page = 1
        dpi = int(_clamp(int(self._q1(q, "dpi", 110) or 110), 72, 200))
        bars = int(_clamp(int(_int_or_none(self._q1(q, "bars")) or 0), 0, 8))
        rows = int(_clamp(int(_int_or_none(self._q1(q, "rows")) or 0), 0, 12))
        canon = E.to_text(meta, slots, spb)
        ropt = {"dpi": dpi}
        if bars:
            ropt["bars_per_row"] = bars
        if rows:
            ropt["rows_per_page"] = rows
        geo = R.page_geometry(canon, page=page, **ropt)
        return self._json({
            "ok": True, "id": jid, "page": geo.get("page"),
            "meta": {k: meta.get(k) for k in
                     ("title", "tempo", "time_sig", "key", "key_raw",
                      "has8vb", "difficulty")},
            "spb": spb, "slots": slots, "stats": E.stats(slots, spb),
            "rows": rows or 4,
            "geometry": geo,
        })

    def _edit_save(self, jid):
        """保存编辑结果：格子 → SMC 文本，覆盖原文件并同步历史和结果。"""
        raw = self._body(8 * 1024 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._err(400, "提交的内容不是合法 JSON。")
        slots = payload.get("slots")
        if not isinstance(slots, list) or not slots:
            return self._err(400, "没有收到任何格子。",
                             ["刷新页面重新打开这首谱子再改。"])
        text = _smc_text(jid)
        if text is None:
            return self._err(404, "这个任务的谱面文件不在了。",
                             ["重新上传一次再改。"])
        E = mod("editor")
        meta0, _old, spb0 = E.from_text(text)
        meta = dict(meta0)
        pm = payload.get("meta") or {}

        def _clean_meta(v):
            """meta 会原样拼进 SMC 头部。以前前端发什么就写什么，
            一个 difficulty = "1] 曲谱:[D R M F" 就能伪造出整行字段，
            而 smc._field 只认第一个匹配，解析立刻错乱。"""
            s = re.sub(r"[\[\]\r\n\t]+", " ", str(v)).strip()
            return s[:40]

        for k in ("title", "tempo", "time_sig", "key", "difficulty"):
            v = pm.get(k)
            if v in (None, ""):
                continue
            s = _clean_meta(v)
            if not s:
                continue
            if k == "tempo":
                m = re.search(r"-?\d+(?:\.\d+)?", s)
                if not m:
                    continue
                n = int(round(float(m.group(0))))
                if not (20 <= n <= 400):
                    continue
                s = str(n)
            elif k == "time_sig":
                if not re.fullmatch(r"\d{1,2}\s*/\s*\d{1,2}", s):
                    continue
                s = s.replace(" ", "")
            elif k == "difficulty":
                # 难度只该是「3 星」这种短标签。以前前端发什么写什么，
                # `1] 曲谱:[D R M F` 能在一行里拼出看着像第二个字段的东西。
                # 这里只认数字 + 可选「星」，其余一律丢弃。
                m = re.search(r"\d+(?:\.\d+)?", s)
                if not m:
                    continue
                s = m.group(0) + ("星" if "星" in s else "")
            meta[k] = s
        if "has8vb" in (payload.get("meta") or {}):
            meta["has8vb"] = bool(payload["meta"]["has8vb"])
        try:
            spb = int(payload.get("spb") or spb0 or 8)
        except (TypeError, ValueError):
            spb = spb0 or 8
        spb = int(_clamp(spb, 2, 16))
        clean = [E.full_group(E.strip_group(s)) for s in slots[:20000] if isinstance(s, str)]
        new_text = E.to_text(meta, clean, spb)

        path = JOBS_DIR / f"{jid}.smc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")

        job = JOBS.get(jid)
        if job is not None:
            kind = job.get("kind") or "smc"
            old = job.get("result") or {}
            res = _build_result(jid, new_text, kind=kind,
                                backend=old.get("backend") or "",
                                params=old.get("params") or {},
                                extra={"confidence": "手动编辑",
                                       "grid": "手动编辑",
                                       "hit": old.get("hit") or 100})
            job["result"] = res
            job["advice"] = res.get("advice") or []
            # 手动改过：之后任何「改调性/速度」的重排都要先问一句
            job["edited"] = True
        # 历史列表里的标题/音数也要跟着变，否则列表和打开的对不上
        with HIST_LOCK:
            for it in HISTORY:
                if it.get("id") == jid:
                    it["title"] = meta.get("title") or it.get("title")
                    it["notes"] = sum(1 for s in clean for c in s
                                      if c in E.ORDER)
                    break
        _save_history()
        return self._json({"ok": True, "id": jid, "spb": spb,
                           "stats": E.stats(clean, spb),
                           "text_len": len(new_text),
                           "download": f"/api/download/{jid}"})

    def _history_rename(self, jid):
        raw = self._body(64 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        title = (payload.get("title") or "").strip()
        if not title:
            return self._err(400, "新名字是空的。")
        # SMC 头部是 `曲名:[X]`，名字里带 ] 或换行会把整个文件头搞坏
        title = re.sub(r"[\[\]\r\n\t]+", " ", title).strip()[:40]
        if not title:
            return self._err(400, "这个名字里全是不能用的符号。")
        found = False
        with HIST_LOCK:
            for it in HISTORY:
                if it.get("id") == jid:
                    it["title"] = title
                    found = True
                    break
        job = JOBS.get(jid)
        if job and isinstance(job.get("result"), dict):
            job["result"]["title"] = title
        if not found and not (job and job.get("result")):
            return self._err(404, "历史里没有这条记录。")
        if found:
            _save_history()
        # 标题改了，谱面正文里的曲名也一起换掉，导出的图才一致
        text = _smc_text(jid)
        if text:
            # 用 lambda 而不是直接拼替换串：标题里若有 `\1`、`\g<0>` 之类，
            # 会被 re 当成反向引用——轻则嵌套出名、重则抛 re.error 直接 500。
            new_text = re.sub(r"曲名:\s*\[[^\]]*\]", lambda m: "曲名:[" + title + "]",
                              text, count=1)
            if new_text != text:
                (JOBS_DIR / f"{jid}.smc").write_text(new_text, encoding="utf-8")
                if job and isinstance(job.get("result"), dict):
                    job["result"]["smc"] = new_text
        return self._json({"ok": True, "id": jid, "title": title})

    def _history_delete(self, jid):
        with HIST_LOCK:
            before = len(HISTORY)
            HISTORY[:] = [x for x in HISTORY if x.get("id") != jid]
            removed = before != len(HISTORY)
        with JOBS_LOCK:
            JOBS.pop(jid, None)
        for d in (JOBS_DIR, WORK):
            for pat in (f"{jid}.smc", f"{jid}.*"):
                try:
                    for p in d.glob(pat):
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
        _save_history()
        if not removed:
            return self._err(404, "历史里没有这条记录。")
        return self._json({"ok": True, "id": jid})


# ---------------------------------------------------------------- 任务读取
def _smc_text(jid: str):
    for d in (JOBS_DIR, WORK):
        p = d / f"{jid}.smc"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                pass
    return None


def _title_of(text: str) -> str:
    try:
        return smc.parse_meta(text).get("title") or "未命名"
    except Exception:
        return "未命名"


def _result_from_text(jid: str, text: str, it: dict) -> dict:
    res = _build_result(jid, text, kind=(it or {}).get("kind") or "smc",
                        backend=(it or {}).get("backend") or "",
                        params=(it or {}).get("params") or {},
                        extra={"confidence": (it or {}).get("confidence") or "手动",
                               "hit": (it or {}).get("hit") or 100,
                               "grid": "历史记录", "doubled": 0,
                               "restored": True})
    return res


def _get_job(jid: str):
    job = JOBS.get(jid)
    if job is not None:
        return job
    with HIST_LOCK:
        it = next((dict(x) for x in HISTORY if x.get("id") == jid), None)
    if not it:
        return None
    text = _smc_text(jid)
    if text is None:
        return None
    res = _result_from_text(jid, text, it)
    stages = [{"key": k, "label": lb} for k, lb in STAGES.get(it.get("kind") or "smc",
                                                             STAGES["smc"])]
    job = {"id": jid, "kind": it.get("kind") or "smc",
           "name": it.get("name") or it.get("source") or it.get("title"),
           "source": it.get("source") or "", "status": "done", "stage": "完成",
           "stage_key": "done", "stage_index": len(stages), "stage_total": len(stages),
           "stages": stages, "progress": 1.0, "queue_pos": 0,
           "params": it.get("params") or {}, "elapsed": it.get("elapsed") or 0,
           "result": res, "advice": res.get("advice") or [], "restored": True,
           "created_at": it.get("ts") or time.time(),
           "_t0": time.time(), "_cancel": False, "_cache": None, "_payload": {}}
    with JOBS_LOCK:
        JOBS.setdefault(jid, job)
    _jobs_gc()
    return JOBS.get(jid)


# ---------------------------------------------------------------- 启动
def _ensure_stdio():
    """pythonw 起的进程没有控制台，print 会炸；把输出接到 web.log。"""
    if sys.stdout is None or sys.stderr is None:
        try:
            f = open(WEB / "web.log", "a", encoding="utf-8", buffering=1)
        except Exception:
            return
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def _port_busy(host: str, port: int) -> bool:
    try:
        with socket.create_connection((("127.0.0.1" if host in ("0.0.0.0", "") else host),
                                       port), timeout=0.4):
            return True
    except OSError:
        return False


class Server(ThreadingTCPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 会把「端口已被占用」变成静默双绑定，别开
    allow_reuse_address = os.name != "nt"


def serve(port: int = 8777, host: str = "127.0.0.1", open_browser: bool = False):
    _ensure_stdio()
    _load_state()
    _cache_gc()
    start_worker()
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}/"
    if _port_busy(host, port):
        print(f"端口 {port} 上已经有一个 sky15 界面在跑了：{url}", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return 1
    httpd = Server((host, port), App)
    print(f"sky15 界面已启动：{url}    （Ctrl+C 停止）", flush=True)
    print(f"  任务队列：单线程串行，避免转谱互相抢显存", flush=True)
    print(f"  成品目录：{JOBS_DIR}", flush=True)
    if open_browser:
        threading.Timer(0.9, lambda: __import__("webbrowser").open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，收工。", flush=True)
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="sky15.web.server", description="sky15 网页界面")
    ap.add_argument("port_pos", nargs="?", type=int, default=None, help="端口（老写法）")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 8777")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；手机要连就填 0.0.0.0")
    ap.add_argument("--open", action="store_true", help="启动后自动开浏览器")
    ap.add_argument("--no-browser", action="store_true", help="不要开浏览器")
    a = ap.parse_args(argv)
    port = a.port or a.port_pos or 8777
    return serve(port=port, host=a.host,
                 open_browser=(a.open and not a.no_browser))


if __name__ == "__main__":
    raise SystemExit(main())
