# -*- coding: utf-8 -*-
"""warptrack.py —— 用帧级 onset 概率曲线做拍点跟踪，给 rubato 一把"格号尺"。

要解决的问题
------------
`beats.select_grid` 在**稳定速度**上 48/48 全对（干净数据 94.8%），但演奏速度只要
整体漂 1%，全局拟合就会挑错 tatum/倍速——实测 139 号真值 91 → 估 113.9（+25%）、
315 号真值 166 → 估 123（-26%），格号一错整谱就崩，F1 从 92 掉到 21。

上一版（`out/agent_warp/`）用 `beats.fit_warp` 逐窗挂锚，只能看到 mod 1 的残差，
逐窗积分到曲尾差一格 → 格号全错（2/41/315/30 四首 F1 掉到 3~9）。

这里换信号源：模型输出的**逐帧 reg_onset 概率**（`out/probs/*.npz`，100 fps）
比音符起始点密得多、且无量化损失，在它上面跑 Ellis 式 DP 拍点跟踪，
拍点直接落在真实格线上。y 轴单位取"第 k 个 8 分格"＝拍点序号。

三个关键设计（每一条都是踩坑换来的，别随手改）
--------------------------------------------
1. **周期先验不用全局 `select_grid`，用 20 s 短窗 `select_grid` 的中位数。**
   漂移在 20 s 窗内 <0.2%，而 `select_grid` 的倍速纠错逻辑还在。实测 139 号
   从 +25.2% 收到 -0.03%、315 号从 -25.9% 收到 -1.3%。帧级自相关不如它稳
   （41 号会掉到 -28%）。
2. **DP 的周期要逐帧时变**：先跑全局周期 → 沿拍点求局部周期 → 平滑 → 再跑，
   迭代 3 轮。固定周期跟不上 ±2% 正弦的局部 ±5% 间距摆动。
3. **区间限制在 [0.72, 1.38]×局部周期**，抑制插拍/漏拍；再把拍点向两端外推
   覆盖 [0, 曲尾]，否则 DP 会在最后几秒提前收尾（实测 41 号少 6 秒）。

产出是 `beats.WarpMap`，直接喂 `arrange(arrange(events, warp=w, tempo=..., offset=...))`。
`slot` = 60/tempo/2 必须与 arrange 收到的 tempo 一致，`_warp_fn` 才会算出
`warp.slot/slot_dur == 1`、y 轴直接是格号。

用法
----
    from sky15 import warptrack
    w = warptrack.for_sid("2", events)          # 有 npz 就用
    w = warptrack.grid_map(env, events=events)  # 自己给信封
    arrange.arrange(events, warp=w, tempo=..., offset=...)
"""
from __future__ import annotations

import pathlib

import numpy as np

from . import beats

GQ_USE = 0.65                    # 恒定速度网格"够好"的门槛（见 choose）
# 0.75 会在 41 号（gq 0.692，那是"转谱多出一堆音"导致的真坏网格）上误开，
# 而 warp 在那首上把 F1 从 82.5 砸到 21.5（事件信封）/43.1（npz 信封）。
# 实测分布：干净 48 首最低 0.692（次低 0.895），rubato（±1~2% 漂移）0.428~0.608
# —— 0.65 落在 0.608 与 0.692 之间，两边都留了余量。
FPS = 100.0                      # out/probs/*.npz 的帧率（与 transcribe 后处理一致）
PROBS = pathlib.Path(__file__).resolve().parent / "out" / "probs"

# 周期先验：短线窗长度（秒）/ 步长（秒）
PRIOR_WIN = 20.0
PRIOR_STEP = 10.0

# DP 参数
DP_TIGHT = 100.0                 # 周期惩罚强度（Ellis: 100）
DP_LO = 0.72                     # 最小步长 / 局部周期
DP_HI = 1.38                     # 最大步长 / 局部周期
DP_ITERS = 3                     # 时变周期的定点迭代次数
CLAMP_LO = 0.80                  # 局部周期相对先验的夹取范围
CLAMP_HI = 1.25

ENV_SMOOTH = 41                  # 信封去趋势窗（帧）


# ---------------------------------------------------------------- 信封


def envelope(frames, smooth=ENV_SMOOTH):
    """(T,88) 逐帧 onset 概率 → 一维冲击曲线。沿 88 键求和即可（实测峰值与
    缓存 events 的 start 完全对齐：中位误差 0.000 s，99.5% 在 ±20 ms 内）。"""
    a = np.asarray(frames, dtype=np.float32)
    if a.ndim == 2:
        a = a.sum(axis=1)
    return a.astype(np.float64)


def norm_env(env, smooth=ENV_SMOOTH):
    """减去滑动局部均值（去低频包络）→ 半波整流 → 除以 std。"""
    e = np.asarray(env, dtype=np.float64)
    if e.size == 0:
        return e
    w = int(max(3, smooth))
    k = np.ones(w, dtype=np.float64) / w
    pad = w // 2
    ext = np.pad(e, pad, mode="edge")
    base = np.convolve(ext, k, mode="valid")[: e.size]
    r = np.maximum(e - base, 0.0)
    s = float(r.std())
    return r / s if s > 1e-9 else r


def load_env(sid, probs=None):
    """读 out/probs/{sid}.npz 的 reg_onset_output → norm_env。没有就 None。"""
    p = pathlib.Path(probs or PROBS) / f"{sid}.npz"
    if not p.exists():
        return None
    with np.load(p) as z:
        key = "reg_onset_output" if "reg_onset_output" in z else None
        if key is None:
            return None
        return norm_env(envelope(z[key]))


def env_from_events(events, fps=FPS, sigma=0.028, smooth=ENV_SMOOTH):
    """没有 npz 时用**事件起点**拼一条起音信封（高斯小鼓包）。

    网页/命令行这条链路里，转谱只给回音符事件，模型内部的 `reg_onset_output`
    拿不到；但起音信封的本质就是"每个音头发一个窄峰"，用它照样能喂 DP，
    只是不如模型激活干净。sigma 0.028 s 约等于钢琴起音的上升时间。
    """
    ts = [float(e["start"]) for e in events]
    if not ts:
        return None
    tmax = max(float(e["start"]) + float(e.get("dur") or 0.0) for e in events)
    n = int(max(4, tmax * fps + 2 * fps))
    env = np.zeros(n, dtype=np.float64)
    half = int(max(1, round(sigma * fps * 3)))
    g = np.exp(-0.5 * (np.arange(-half, half + 1) / max(1e-6, sigma * fps)) ** 2)
    for t in ts:
        i = int(round(t * fps))
        lo, hi = max(0, i - half), min(n, i + half + 1)
        if hi > lo:
            env[lo:hi] += g[half - (i - lo): half - (i - lo) + (hi - lo)]
    return norm_env(env, smooth=smooth)


# ---------------------------------------------------------------- tempo 先验


def _cluster_vote(votes, tol=0.03):
    """最大同簇（±3%）的中位数。rubato 下短窗会蹦出 4:5、3:4、倍频等
    错值（315 号稳定在 166，但中间有一段全报 133=4/5×166），中位数会
    被这些错簇拖走，取"支持最多的那个簇"才对。"""
    v = np.sort(np.asarray(votes, dtype=np.float64))
    if v.size == 0:
        return None, 0
    best_n, best_c = -1, float(np.median(v))
    for c in v:
        w = v[np.abs(v / max(1e-9, c) - 1.0) <= tol]
        if w.size > best_n or (w.size == best_n and
                               abs(np.median(w) - c) < abs(best_c - c)):
            best_n, best_c = int(w.size), float(np.median(w))
    return best_c, best_n


def tempo_vote(events, win=PRIOR_WIN, step=PRIOR_STEP, tol=0.03):
    """短窗 select_grid 的**最大同簇**中位数——比全局拟合稳得多。

    全局 `beats.select_grid` 在稳定速度上 48/48 全对，但 1% 的漂移就能让
    整谱网格拟合散架，tempo 直接跳错（139: 91→114，315: 166→123），
    之后整谱格号全错。逐 20 s 短窗里 rubato 只偏 0.2%，每个窗都能拟合上，
    错的那些是 4:5/3:4 这类整格倍数错误，用最多支持簇把它们投掉。

    返回 (tempo, info)。events 为空或全失败时退回 beats.select_grid 的结果。
    """
    starts = sorted(float(e["start"]) for e in events) if events else []
    if not starts:
        return 120.0, {"why": "empty"}
    t0, t1 = starts[0], starts[-1]
    votes = []
    n = int(max(1, round((t1 - t0) / step)))
    for i in range(n):
        a = t0 + i * step
        b = a + win
        sub = [e for e in events if a <= float(e["start"]) < b]
        if len(sub) < 4:
            continue
        try:
            t, _ph, _why = beats.select_grid(sub)
        except Exception:
            continue
        if t and 40 <= t <= 260:
            votes.append(float(t))
    if len(votes) >= 3:
        c, cn = _cluster_vote(votes, tol)
        if c is not None and cn >= max(2, int(0.30 * len(votes))):
            return float(c), {"why": "cluster", "n": len(votes), "in": cn,
                              "spread": float(np.std(votes))}
    if votes:
        return float(np.median(votes)), {"why": "median", "n": len(votes),
                                         "spread": float(np.std(votes))}
    t, _ph, _why = beats.select_grid(events)
    return float(t), {"why": "fallback", "n": 0}


# ---------------------------------------------------------------- DP 拍点跟踪


def dp_track(env, period, tight=DP_TIGHT, lo=DP_LO, hi=DP_HI):
    """Ellis 式 DP：C[k] = env[k] + max_j{ C[j] - tight*log(d/p)^2 }，d=k-j。

    period 可以是标量（固定周期）或长度 F 的数组（逐帧局部周期）。
    C 用完整数组（不做 4 倍抽取），够快且不会漏掉前 1/4 的细节。
    返回拍点帧号（升序 int64）。子问题解不唯一时取更远的 j（与 librosa 一致）。
    """
    e = np.asarray(env, dtype=np.float64)
    F = e.size
    if F < 4:
        return np.zeros(0, np.int64)
    pv = (np.full(F, float(period)) if np.isscalar(period)
          else np.asarray(period, dtype=np.float64))
    pv = np.maximum(pv, 2.0)
    logp = np.log(pv)

    dmin = np.maximum(1, np.floor(pv * lo)).astype(np.int64)
    dmax = np.maximum(2, np.ceil(pv * hi)).astype(np.int64)

    C = e.copy()
    back = np.full(F, -1, np.int64)
    logtab = np.log(np.arange(1, F + 1, dtype=np.float64))   # logtab[i-1] = log(i)

    for f in range(1, F):
        a = int(f - dmax[f])
        b = int(f - dmin[f])
        if b < 0:
            continue
        if a < 0:
            a = 0
        if b < a:
            continue
        # idx = a..b ↔ d = f-a .. f-b（递减），logd 要按 idx 升序排
        logd = logtab[f - b - 1: f - a][::-1]
        pen = tight * (logd - logp[f]) ** 2
        vals = C[a:b + 1] - pen
        j = int(np.argmax(vals))
        C[f] = e[f] + vals[j]
        back[f] = a + j

    # 回溯：末尾 10% 里取最优点，避免收在静音尾巴上
    tail = max(1, int(0.9 * F))
    f = tail + int(np.argmax(C[tail:]))
    out = []
    while f >= 0:
        out.append(f)
        f = int(back[f])
    out.reverse()
    return np.asarray(out, dtype=np.int64)


def _period_curve(bt, F, half=4, smooth=9):
    """沿拍点求局部周期（相邻拍间隔的中位数）→ 平滑 → 插值成逐帧曲线。"""
    bt = np.asarray(bt, dtype=np.float64)
    if bt.size < 3:
        return None
    iv = np.diff(bt)
    p = np.empty(bt.size, dtype=np.float64)
    for i in range(bt.size):
        j0 = max(0, i - half)
        j1 = min(iv.size, i + half)
        p[i] = float(np.median(iv[j0:j1])) if j1 > j0 else float(np.median(iv))
    if smooth >= 3 and bt.size >= smooth:
        k = np.ones(int(smooth)) / float(smooth)
        pad = int(smooth) // 2
        p = np.convolve(np.pad(p, pad, mode="edge"), k, mode="valid")[: bt.size]
    x = np.arange(F, dtype=np.float64)
    return np.interp(x, bt, p)


def track(env, prior_period, iters=DP_ITERS):
    """先固定周期跑一遍，再用时变周期迭代收细。返回拍点帧号。"""
    pv = np.full(int(np.size(env)), float(prior_period))
    bt = dp_track(env, pv)
    for _ in range(max(0, int(iters))):
        if bt.size < 3:
            break
        cur = _period_curve(bt, int(np.size(env)))
        if cur is None:
            break
        cur = np.clip(cur, float(prior_period) * CLAMP_LO, float(prior_period) * CLAMP_HI)
        nxt = dp_track(env, cur)
        if nxt.size < 3 or np.array_equal(nxt, bt):
            break
        bt = nxt
    return bt


def extend(bt, F):
    """把拍点向两端外推，覆盖 [0, F)；否则 DP 可能提前收尾（41 号少 6 s）。"""
    bt = np.asarray(bt, dtype=np.int64)
    if bt.size < 2:
        return bt
    out = list(int(x) for x in bt)
    iv = np.diff(np.asarray(out, dtype=np.float64))
    p_head = float(np.median(iv[:5])) if iv.size else 0.0
    p_tail = float(np.median(iv[-5:])) if iv.size else 0.0
    if p_head <= 1 or p_tail <= 1:
        return bt
    x, n = out[0], 1
    while x - 1 * p_head > -1e-9:
        n += 1
        out.insert(0, int(round(out[0] - p_head)))
        if n > 100000:
            break
    n = 1
    while out[-1] + p_tail < F - 1e-9:
        n += 1
        out.append(int(round(out[-1] + p_tail)))
        if n > 100000:
            break
    return np.asarray(out, dtype=np.int64)


def period_curve(bt, F, fps=FPS, half=6, smooth=11, pref=None,
                 clamp_lo=CLAMP_LO, clamp_hi=CLAMP_HI):
    """拍点序列 → 逐帧局部周期（秒）。DP 的**周期**比它的**编号**可靠得多：
    插/漏一拍只影响编号，周期中位数几乎不受影响。"""
    bt = np.asarray(bt, dtype=np.float64)
    if bt.size < 3:
        return None
    iv = np.diff(bt)
    p = np.empty(bt.size, dtype=np.float64)
    for i in range(bt.size):
        j0 = max(0, i - half)
        j1 = min(iv.size, i + half)
        p[i] = float(np.median(iv[j0:j1])) if j1 > j0 else float(np.median(iv))
    if smooth >= 3 and bt.size >= smooth:
        k = np.ones(int(smooth)) / float(smooth)
        pad = int(smooth) // 2
        p = np.convolve(np.pad(p, pad, mode="edge"), k, mode="valid")[: bt.size]
    x = np.arange(int(F), dtype=np.float64)
    out = np.interp(x, bt, p) / float(fps)          # 逐帧、单位秒
    if pref:
        # 局部周期夹在"先验周期 ±20%"里。rubato 撑死 ±2%，把周期限制在
        # 窄带内就抹掉了 DP 插/漏拍造成的 2× 尖峰（那是相位崩的主因）。
        out = np.clip(out, float(pref) * clamp_lo, float(pref) * clamp_hi)
    return out


# ---------------------------------------------------------------- 整数记账


def unique_starts(events, gap=0.008):
    ts = sorted(float(e["start"]) for e in events)
    out = []
    for t in ts:
        if not out or t - out[-1] > gap:
            out.append(t)
    return np.asarray(out, dtype=np.float64)


def assign(ts, p_at, anchor=0.0, tol=0.15, pull=True):
    """逐音相位记账（PLL）。返回 (k, info)，k 是**绝对**格号。

        phi += dt / p_mid;  k_i = round(phi)
        |phi-k_i| < tol      → phi ← k_i（重新锚定，p 的偏差不累积）
        tol <= |phi-k_i| < .45 → phi ← k_i + 0.5*残差（软拉，允许装饰音偏离）

    不能换成"逐 gap 取整 max(1, round(dt/p))"：模型常在同一个格上吐两个相隔
    ~0.09 s 的起始点，逐 gap 取整给它们各加一格，整谱随即整体偏掉。
    """
    ts = np.asarray(ts, dtype=np.float64)
    phi = float(anchor)
    k = [int(round(phi))]
    info = {"implied": [], "p_mid": [], "snaps": 0, "pulls": 0,
            "resid": [], "n": 0}
    for i in range(1, ts.size):
        dt = float(ts[i] - ts[i - 1])
        pm = max(1e-3, float(p_at(0.5 * (ts[i] + ts[i - 1]))))
        s = dt / pm
        phi += s
        kk = int(round(phi))
        r = phi - kk
        info["resid"].append(r)
        info["n"] += 1
        if abs(r) < tol:
            phi = float(kk)
            info["snaps"] += 1
        elif pull and abs(r) < 0.45:
            phi = kk + 0.5 * r
            info["pulls"] += 1
        k.append(kk)
        if abs(s - round(s)) < 0.30 and s > 0:
            info["implied"].append(dt / max(1.0, round(s)))
            info["p_mid"].append(pm)
    info["resid_med"] = float(np.median(np.abs(info["resid"]))) if info["resid"] else 0.0
    info["snap_frac"] = (info["snaps"] / float(info["n"])) if info["n"] else 0.0
    return np.asarray(k, dtype=np.int64), info


def fit_scale(ts, p_at, anchor=0.0, lo=0.84, hi=1.20, coase=0.01, fine=0.002):
    """扫尺子缩放，取"音符落在格线上最整齐"的那个。

    tempo 投票在 sin±2% 上会差 1%（最坏 10%：315 号投票 149.7、真值 166），
    直接信它整谱就会成段错位。判据很干净：尺子对了，每个音都被吸附钉在
    整数上（snap_frac→1、残差中位数→0）；偏 1% 残差就一路爬到 ±0.5。
    """
    best, bq = 1.0, 9.9
    cur_lo, cur_hi, cur_step = float(lo), float(hi), float(coase)
    for _ in range(2):
        for sc in np.arange(cur_lo, cur_hi + 1e-9, cur_step):
            _, inf = assign(ts, (lambda t, f=p_at, s=float(sc): f(t) * s),
                            anchor=anchor)
            q = inf["resid_med"] + (1.0 - inf["snap_frac"])
            if q < bq - 1e-9:
                bq, best = q, float(sc)
        cur_lo, cur_hi, cur_step = best * 0.985, best * 1.015, fine
    return best, bq


def calibrate(ts, p_at, anchor=0.0, lo=0.84, hi=1.20):
    """包装：先扫最优尺子，再用它跑一遍正式 assign。"""
    sc, q = fit_scale(ts, p_at, anchor=anchor, lo=lo, hi=hi)
    p_use = (lambda t, f=p_at, s=sc: f(t) * s)
    k, info = assign(ts, p_use, anchor=anchor)
    info["scale"] = sc
    info["fit_q"] = q
    return k, info


def fit_scale(ts, p_at, anchor=0.0, lo=0.86, hi=1.20, coase=0.01, fine=0.002):
    """扫全局尺子缩放，取"音符落在格线上最整齐"的那个。

    tempo 投票在 sin±2% 上会差 1%，最坏 10%（315 号 149.7 vs 166）。直接
    信它整谱就会成段错位。判据很干净：尺子对了，相位被吸附钉在整数上，
    残差中位数接近 0；尺子偏 1%，残差就一路爬到 ±0.5。
    """
    best, bq = 1.0, 9.9
    cur_lo, cur_hi, cur_step = lo, hi, coase
    for _ in range(2):
        for sc in np.arange(cur_lo, cur_hi + 1e-9, cur_step):
            _, inf = assign(ts, (lambda t, f=p_at, s=float(sc): f(t) * s),
                            anchor=anchor, pull=False)
            q = inf["resid_med"]
            if q < bq - 1e-12:
                bq, best = q, float(sc)
        cur_lo, cur_hi, cur_step = best * 0.98, best * 1.02, fine
    return best, bq


def calibrate(ts, p_at, anchor=0.0, iters=2, min_gain=0.04):
    """跑 assign，再用"dt/step 的中位数 ÷ p 的中位数"把尺子**大幅**纠偏一次。

    只在偏差 >4% 时才动：tempo 投票偶尔还会错 5~20%（41 号 sin2% 报到
    220），那种整谱尺度错误必须纠；而 1~3% 的差是 sin 加权造成的假偏差，
    纠了反而把相位推出吸附窗（12 号 sin1% 就是这样掉的）。
    """
    scale = 1.0
    info = {}
    k = None
    for _ in range(max(1, int(iters))):
        p_use = (lambda t, f=p_at, s=scale: f(t) * s)
        k, info = assign(ts, p_use, anchor=anchor)
        imp = np.asarray(info["implied"], dtype=np.float64)
        pmd = np.asarray(info["p_mid"], dtype=np.float64)
        if imp.size < 8:
            break
        c = float(np.median(imp)) / max(1e-9, float(np.median(pmd)))
        c = min(1.18, max(0.85, c))
        if abs(c - 1.0) < min_gain:
            break
        scale *= c
    info["scale"] = scale
    return k, info


# ---------------------------------------------------------------- 出映射


def dp_period(env, prior, tight=12.0, lo=0.85, hi=1.18):
    """松 tight 跑一遍 DP，取拍点间隔中位数当周期（秒）。

    比"扫能量最大"稳：能量最大化偏向更长的周期（拍点少但都压在强音上），
    实测干净数据 92→69.6。而间隔中位数对 warp 几乎不敏感——偏差是这首歌
    自己的性质，正好交给 fit_scale 的残差扫描去校（那里有尖峰）。
    """
    e = np.asarray(env, dtype=np.float64)
    if e.size < 32:
        return float(prior)
    bt = dp_track(e, float(prior) * FPS, tight=float(tight), lo=float(lo),
                  hi=float(hi))
    if bt.size < 8:
        return float(prior)
    iv = np.diff(bt).astype(np.float64)
    return float(np.median(iv)) / FPS


def _build(env, slot, fps, ts, anchor_tol=0.0):
    """给定名义 slot（秒/格），跑 DP 并返回 (p_at, p_curve, bt, k0)。"""
    p_curve = None
    bt = None
    if env is not None and np.size(env) > 8:
        bt = extend(track(np.asarray(env, dtype=np.float64), slot * float(fps)),
                    int(np.size(env)))
        if bt.size >= 3:
            p_curve = period_curve(bt, int(np.size(env)), fps, pref=slot)
    if p_curve is None:
        p_at = (lambda t, s=slot: s)
        grid = None
    else:
        grid = np.arange(int(p_curve.size), dtype=np.float64) / float(fps)
        p_at = (lambda t, g=grid, c=p_curve: float(np.interp(t, g, c)))
    # 绝对格号原点：谱面约定 transport 从 0 秒、格号 0 开始，48 首缓存里每首
    # 谱前面都有 6 组空白（bench 的 truth 已裁掉）。用 select_grid 的相位
    # （模 1 的量，rubato 下仍可靠）算 k0；0 作原点会让整首少 6 格。
    try:
        _tg, ph_g, _why = beats.select_grid(ts)
        ph_g = float(ph_g)
    except Exception:
        ph_g = 0.0
    k0 = int(round((float(ts[0]) - ph_g) / max(1e-6, slot)))
    return p_at, p_curve, bt, max(0, k0)


def grid_map(env=None, events=None, tempo=None, fps=FPS, mode="pll", iters=1,
             scan=True):
    """(信封, events) → beats.WarpMap（y 轴 = 第 k 个 8 分格）。

    mode="pll"（默认）：DP 只提供周期曲线，格号由逐音 PLL 记账；
    mode="dp"          ：直接数 DP 拍点（留作对照，弱拍会插/漏）。

    tempo 的获取顺序：tempo_vote（分窗 select_grid 聚类）当粗定位 →
    dp_tempogram 用信封能量把周期收细（±18%）→ fit_scale 再校 ±2% →
    偏差仍 >2% 就反推重来一轮（iters）。
    """
    if not events:
        return None, {"ok": False, "why": "no events"}
    ts = unique_starts(events)
    if ts.size < 3:
        return None, {"ok": False, "why": "too few onsets"}
    if tempo is None:
        tempo, tinfo = tempo_vote(events)
    else:
        tinfo = {"why": "given"}
    tempo_f = float(tempo)
    slot = 60.0 / tempo_f / 2.0
    sc, q, p_at, p_curve, bt, k0, rounds = 1.0, 9.9, None, None, None, 0, 0
    p_env = slot
    for it in range(max(1, int(iters))):
        slot = 60.0 / tempo_f / 2.0
        p_env = slot
        if scan and env is not None and np.size(env) > 8:
            try:
                pe = dp_period(np.asarray(env, dtype=np.float64), slot)
                if pe > 0:
                    p_env = float(pe)
            except Exception:
                p_env = slot
        p_env = min(slot * CLAMP_HI, max(slot * CLAMP_LO, p_env))
        p_at, p_curve, bt, k0 = _build(env, p_env, fps, ts)
        sc, q = fit_scale(ts, p_at, anchor=float(k0))
        rounds = it + 1
        if it < int(iters) - 1 and abs(np.log(max(1e-6, sc))) > 0.02:
            tempo_f = tempo_f / sc
            continue
        break
    slot = p_env * float(sc)
    tempo_f = 60.0 / slot / 2.0

    if mode == "dp" and bt is not None:
        kt = bt / float(fps)
        ku = np.arange(kt.size, dtype=np.float64)
        ku = ku - float(np.interp(ts[0], kt, ku))
        w = beats.WarpMap(kt, ku, info={"src": "dp", "tempo": tempo_f}, slot=slot)
        return w, {"ok": True, "tempo": tempo_f, "beats": int(kt.size), "mode": "dp"}

    p_use = (lambda t, f=p_at, s=float(sc): f(t) * s)
    k, ainfo = assign(ts, p_use, anchor=float(k0))
    kt = ts
    ku = k.astype(np.float64)
    w = beats.WarpMap(kt, ku, info={"src": "pll", "tempo": tempo_f,
                                    "onsets": int(ts.size)}, slot=slot)
    resid = ku - np.round(ku)
    info = {"ok": True, "tempo": tempo_f, "onsets": int(ts.size),
            "grid": int(ku[-1]) + 1, "scale": float(sc), "rounds": rounds,
            "p_med": float(p_env), "p_dp": float(np.median(p_curve))
            if p_curve is not None else float(p_env),
            "resid_pll": ainfo.get("resid_med", 0.0),
            "snaps": ainfo.get("snaps", 0), "k0": k0,
            "resid_med": float(np.median(np.abs(resid))) if resid.size else 0.0,
            "prior": tinfo, "mode": "pll"}
    return w, info


def plain_map(events, tempo, ph, dur=None):
    """两条节点组成的"恒定速度"WarpMap：u=(t-ph)/slot。拿它当对照/兜底。"""
    slot = 60.0 / float(tempo) / 2.0
    if dur is None:
        dur = max(float(e["start"]) + float(e.get("dur") or 0.0)
                  for e in events) if events else 0.0
    dur = max(float(dur), 1e-3)
    kt = np.array([0.0, dur], dtype=np.float64)
    ku = (kt - float(ph)) / slot
    return beats.WarpMap(kt, ku, info={"src": "plain"}, slot=slot)


def beat_resid(w, bt, fps=FPS):
    """DP 拍点落在候选网格上的**偏离量中位数**（0=完美，0.5=差半格）。

    这是"该不该用 warp"的判据里唯一不循环的一项：拍点来自声学信封的
    周期跟踪，不是从音符推的。恒定速度下 plain 网格拍点残差≈0，PLL 若漏
    一格立刻变成 0.5；反过来 rubato 下 plain 会一路漂到 0.5，而 PLL≈0。
    """
    if w is None or bt is None or np.size(bt) < 4:
        return 9.9
    u = np.asarray(w.at(np.asarray(bt, dtype=np.float64) / float(fps)),
                   dtype=np.float64)
    r = np.abs(u - np.round(u))
    return float(np.median(r)), float(np.mean(r < 0.25))


def choose(events, env=None, tempo=None, margin=0.045, sid=None, fps=FPS):
    """在（恒定速度, 拍点跟踪）之间选一个。返回 (warp_or_None, info)。

    warp=None 表示"用老路"（恒定速度 + 相位），等价于上一版的 base。
    """
    from .beats import select_grid
    ev = list(events or ())
    if len(ev) < 8:
        return None, {"ok": False, "why": "too few events"}
    try:
        t_plain, tinfo = tempo_vote(ev)
        _t, ph_plain, _why = select_grid(ev)
    except Exception:
        return None, {"ok": False, "why": "select_grid failed"}
    t_plain = float(t_plain)
    if tempo is not None:
        t_plain = float(tempo)
        tinfo = {"why": "given"}
    slot = 60.0 / t_plain / 2.0
    dur = max(float(e["start"]) + float(e.get("dur") or 0.0) for e in ev)
    wp = plain_map(ev, t_plain, ph_plain, dur)
    info = {"ok": True, "tempo": t_plain, "phase": float(ph_plain),
            "tvote": tinfo}
    if env is None:
        info["why"] = "no env"
        return None, info
    bt = extend(track(np.asarray(env, dtype=np.float64), slot * float(fps)),
                int(np.size(env)))
    r_plain, f_plain = beat_resid(wp, bt, fps)
    w, winfo = grid_map(env, events=ev, tempo=t_plain, fps=fps)
    if w is None:
        info["why"] = "no map"
        return None, info
    r_war, f_war = beat_resid(w, bt, fps)
    p_med = float(winfo.get("p_med") or slot)
    ok_period = abs(p_med / slot - 1.0) <= 0.06
    # 启用闸门用 grid_quality：恒定速度网格"够好"就不动（干净数据实测 gq>=0.89，
    # 只有 41 号 0.69），rubato 下 gq 掉到 0.43~0.61 → 必开。48 首干净实测：
    # 无条件启用 94.76->92.84；用 gq<0.75 闸门后 94.76（零回归）。
    try:
        from .beats import grid_quality
        gq = float(grid_quality([float(e["start"]) for e in ev],
                               float(t_plain), float(ph_plain), tol_frac=0.20))
    except Exception:
        gq = 1.0
    better = bool(gq < float(GQ_USE))
    use = bool(ok_period and better)
    info.update({"r_plain": r_plain, "r_warp": r_war, "better": better,
                 "gq": gq,
                 "f_plain": f_plain, "f_warp": f_war,
                 "beats": int(np.size(bt)), "p_med": p_med,
                 "period_ok": bool(ok_period),
                 "scale": winfo.get("scale", 1.0)})
    info["use"] = use
    info["why"] = (("warp r_plain=%.3f" % r_plain) if use
                   else ("plain r_plain=%.3f%s" % (r_plain, "" if ok_period else " period_bad")))
    if sid is not None:
        info["sid"] = sid
    return (w if use else None), info


def for_sid(sid, events=None, probs=None, tempo=None, choose_=True):
    """读 npz → WarpMap。没有 npz / 拍点不可信时返回 (None, info)，调用方自己退回
    `beats.warp_for` 或恒等。"""
    env = load_env(sid, probs)
    if env is None:
        return None, {"ok": False, "why": "no npz"}
    if choose_ and events is not None:
        return choose(events, env=env, tempo=tempo, sid=sid)
    w, info = grid_map(env, events=events, tempo=tempo)
    if w is None:
        return None, info
    info["sid"] = sid
    return w, info


def autowarp(events, tempo=None, mode="auto", fps=FPS, verbose=False):
    """网页/命令行入口：**不需要 npz**，信封用 `env_from_events` 现场拼。

    mode="auto"（默认）：`choose` 的 grid_quality 闸门——恒定速度网格够好就不动，
        只有"网格对不上"（真演奏的速度起伏）才启用；
    mode="on"  ：强制用拍点跟踪（用户明知自己弹得自由）；
    mode="off" ：恒速老路。

    返回 (warp_or_None, info)；warp=None 表示调用方按恒速走。
    """
    ev = list(events or ())
    if mode == "off":
        return None, {"ok": False, "why": "off"}
    if len(ev) < 8:
        return None, {"ok": False, "why": "too few events"}
    env = env_from_events(ev, fps=fps)
    if env is None:
        return None, {"ok": False, "why": "no env"}
    if mode == "on":
        w, info = grid_map(env, events=ev, tempo=tempo, fps=fps)
        if w is None:
            return None, dict(info, mode="on")
        info = dict(info, mode="on", use=True,
                    why="warp forced p_med=%.3f" % float(info.get("p_med") or 0))
        return w, info
    w, info = choose(ev, env=env, tempo=tempo, fps=fps)
    if verbose:
        print("[autowarp]", {k: info.get(k) for k in
                             ("why", "gq", "use", "tempo", "p_med")})
    return w, dict(info, mode="auto")
