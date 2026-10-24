# -*- coding: utf-8 -*-
"""节拍网格拟合：从音符起点反推"格子长度"和相位。

为什么不用 librosa 的 beat_track 就完事：
  · 它会给半速/倍速两种答案，选错整首就废
  · 它输出的是拍点，而 SMC 的格子是 **8 分音符 = 半拍**，还要再除 2
  · 真实歌曲开头常有弱起/散板，相位错半格全盘皆输

踩过的两个坑（都真实发生过）：
  ① 用"命中率"当目标 → 搜索一路飙到最快速度，因为格子越密越容易命中。
  ② 加了机会校正又会滑到"半速"，因为更粗的格子照样能命中一半的音。
所以改成两步：
  ① **IOI 直方图**找最小稳定间隔（tatum）—— 相邻音符的时间差里最小的那个稳定众数，
     就是 8 分音符的长度，这是确定量，不靠猜。
  ② 相位用圆统计（把 onset 对格子取模后求集中度），一步到位。
最后再用局部搜索微调格子长度。
"""
import math

POP_MIN, POP_MAX = 62.0, 200.0

# 速度的合法区间。低于 10 已经不是音乐（0.6 BPM），高于 1000 也不是。
_TEMPO_LO, _TEMPO_HI = 10.0, 1000.0


def _t(tempo, default=100.0):
    """速度兜底：非数 / 0 / 负数 → default，其余夹进 10~1000 BPM。

    这个模块里 `slot = 60/tempo/2` 这种式子出现十几次，上游只要传一个 0 就是
    ZeroDivisionError，传负数会算出**负的格长** —— 往下切窗、算索引全乱，
    最后表现成"能跑但谱子整体错位"，比直接崩更难查。
    速度最终来自用户输入（网页的「速度」框、谱面头部、命令行 --tempo），
    所以统一在入口兜一次。
    """
    try:
        v = float(tempo)
    except (TypeError, ValueError):
        return default
    if v != v or v <= 0:            # NaN / 0 / 负
        return default
    return min(max(v, _TEMPO_LO), _TEMPO_HI)


def _tatums(onsets, min_gap=0.03):
    """相邻 onset 的时间差（去掉几乎同时的音）"""
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size < 4:
        return np.array([])
    keep = [on[0]]
    for t in on[1:]:
        if t - keep[-1] >= min_gap:
            keep.append(t)
    on = np.array(keep)
    return np.diff(on)


def _phase_concentration(onsets, slot, bins=240):
    """把 onset 对 slot 取模，返回 (最佳相位, 集中度 0~1)"""
    import numpy as np
    on = np.asarray(onsets, dtype=float)
    ph = np.mod(on, slot) / slot                      # 0~1
    hist, edges = np.histogram(ph, bins=bins, range=(0, 1))
    # 圆滑一下，避免被单个 bin 的抖动带偏
    k = np.array([1, 2, 3, 4, 3, 2, 1], dtype=float)
    hist = np.convolve(hist, k / k.sum(), mode="same")
    i = int(np.argmax(hist))
    conc = hist[i] / max(1e-9, hist.mean())           # 峰值/均值，越大越集中
    center = (edges[i] + edges[i + 1]) / 2 * slot
    return center, conc


def _resultant(onsets, slot):
    """圆集中度 R = |mean(exp(2πi·t/slot))| ∈ [0,1]

    全体音都落在格点上时 R→1；格子不对时相位铺开，R 掉下来。
    好处：不需要设容差，也不会因为"格子越密越容易命中"而偏袒快速度。
    """
    import numpy as np
    on = np.asarray(onsets, dtype=float)
    z = np.exp(2j * np.pi * np.mod(on, slot) / slot)
    return float(abs(z.mean())), float(np.angle(z.mean()))     # (R, 圆均值相位)


def _ioi_fit(onsets, slot, tol_frac=0.12):
    """所有相邻音的时间差，有多大比例落在 slot 的整数倍附近。

    这条性质最干净：真格子的任何整数倍都成立，所以能一票筛掉"半格/三格"的假答案；
    反过来，格子取真值的一半时，音程会落在半整数倍上，这一项会明显掉下来。
    """
    import numpy as np
    d = _tatums(onsets)
    if d.size < 4:
        return 0.0
    q = d / slot
    err = np.abs(q - np.round(q)) * slot
    return float((err < tol_frac * slot).sum()) / d.size


def _best_phase(onsets, slot, bins=96, tol_frac=0.22):
    """在 [0, slot) 里找让最多 onset 落在格点附近的相位（不需要圆统计）"""
    import numpy as np
    on = np.asarray(onsets, dtype=float)
    tol = tol_frac * slot
    best_ph, best_hit = 0.0, -1
    for k in range(bins):
        ph = slot * k / bins
        q = (on - ph) / slot
        err = np.abs(q - np.round(q)) * slot
        hit = int((err < tol).sum())
        if hit > best_hit:
            best_ph, best_hit = ph, hit
    return best_ph


def fit_grid(onsets, tempo_hint=None, verbose=False):
    """onsets: [秒] → (tempo_bpm, offset_sec, R)"""
    import numpy as np

    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size < 8:
        return (tempo_hint or 120.0), 0.0, 0.0

    # ---- ① 选格子长度：最大的"不撞格"间距 ----
    # 先把同时发声的和弦合成一个时间点（同一 onset）。
    # 真格子恰好让所有 onset 各占一格；格子再大一点，相邻两个音就会并进同一格（撞格）。
    # 所以"最大的不撞格间距"就是 tatum —— 这条比 IOI 直方图稳得多，
    # 也不会像"最短格子"那样滑到半速/倍速上去。
    uniq = []
    for t in on:
        if not uniq or t - uniq[-1] >= 0.02:
            uniq.append(float(t))
    u = np.asarray(uniq)

    def collisions(slot):
        ph0 = _best_phase(u, slot, bins=48, tol_frac=0.25)
        idx = np.round((u - ph0) / slot).astype(int)
        return len(idx) - len(set(idx.tolist()))

    slots = np.exp(np.linspace(np.log(0.11), np.log(0.70), 480))
    table = [(float(s), _resultant(u, float(s))[0]) for s in slots]

    # 实测下来最稳的一条：**从慢到快扫，取第一个"高集中度且完全不撞格"的**。
    #   · 太慢（真格子的整数倍）→ 音会并进同一格，撞格数 > 0，剔除
    #   · 太快（真格子的一半）→ 集中度掉下来，被 0.80 的门槛挡住
    # 放宽门槛或者改成"取最慢"都会掉 20~30 个点，这两条红线别动。
    slot = None
    for s, r in table[::-1]:                    # slot 从大到小 = 速度从慢到快
        if r < 0.80:
            continue
        if collisions(s) == 0:
            slot = s
            break
    if slot is None:
        slot = max(table, key=lambda t: t[1])[0]

    # ---- ② 倍速歧义：若有 hint 就贴它 ----
    bpm = 30.0 / slot
    if tempo_hint:
        # 负数会让下面的 log 直接抛 math domain error（0 已被 if 挡掉）。
        # 上游速度来自用户输入，清零写成 -1 这种事真的会发生。
        tempo_hint = _t(tempo_hint)
        cands = [bpm * m for m in (0.5, 1.0, 2.0)]
        bpm = min(cands, key=lambda b: abs(math.log(max(b, 1e-6) / tempo_hint)))
    else:
        while bpm > 245:
            bpm /= 2
        while bpm < 55:
            bpm *= 2
    slot = 30.0 / _t(bpm)

    # ---- ③ 细扫精修：两级，先在 ±4% 粗收敛，再在 ±0.3% 细收敛 ----
    # 这一步是关键。粗扫的 slot 哪怕差 2%，三分钟也能漂掉 20 格；
    # 而"最近整数格"的取整会跟着漂，最小二乘会被带歪。
    # 圆集中度 R 对格子长度是连续的、不需要指派整数格，所以不会被带歪。
    def refine(center, half_frac, steps):
        best_s, best_r = center, _resultant(on, center)[0]
        for k in range(-steps, steps + 1):
            s = center * (1 + half_frac * k / steps)
            if s <= 0.05:
                continue
            r, _a = _resultant(on, s)
            if r > best_r:
                best_s, best_r = s, r
        return best_s

    slot = refine(slot, 0.04, 400)
    slot = refine(slot, 0.003, 300)

    # ---- ④ 相位 + 最小二乘收尾（带护栏）----
    # 最小二乘会把 idx 指派到"最近的整数格"，一旦 slot 偏了半格，
    # 它会收敛到 2× 或 0.5× 上去（实测把 120 BPM 拉到 240）。
    # 所以精修结果必须留在初值的 ±12% 内，否则回退。
    anchor = slot
    ph = _best_phase(on, slot)
    for _ in range(4):
        idx = np.round((on - ph) / slot)
        A = np.vstack([np.ones_like(idx), idx]).T
        sol, *_ = np.linalg.lstsq(A, on, rcond=None)
        new_ph, new_slot = float(sol[0]), float(sol[1])
        if new_slot <= 0 or abs(math.log(new_slot / anchor)) > 0.12:
            break                               # 跑偏了，保留细扫的结果
        done = abs(new_slot - slot) < 1e-9 and abs(new_ph - ph) < 1e-8
        ph, slot = new_ph, new_slot
        if done:
            break
    bpm = 30.0 / slot

    # ---- ④ 相位取最靠近 0 的代表元 ----
    # 不能锚定"第一个音"：转谱常常漏掉曲首那个音（t=0 的起音不好检测），
    # 一锚就整首错开一格，F1 从 95% 掉到 30%。所以取模到 [-slot/2, slot/2)。
    ph = (ph + slot / 2) % slot - slot / 2

    R, _ang = _resultant(on, slot)

    if verbose:
        print(f"    slot {slot:.4f}s → {bpm:.2f} BPM  R={R:.3f}  phase={ph:+.3f}  "
              f"首音 {on[0]:.3f}")
        near = [(round(30.0 / s, 1), round(f, 2)) for s, f in table[::40]]
        print(f"    扫描表(速度,音程拟合率): {near}")
    return bpm, ph, R


def fix_tempo_octave(onsets, tempo, min_ratio=0.72, max_double=2, gate=0.05):
    """用「最小音符间隔」判断速度是不是估成了倍速，必要时翻倍。

    为什么有效：转录出来的 onset 密度是有实际下限的 —— 音与音之间不可能比
    真实的十六分音符还密。所以把相邻 onset 间隔的 10 分位数和第 10 分位
    跟格子长度比一下：

        速度对   → p10 / slot ≈ 1.0（音符铺满八分格）
        估成一半 → p10 / slot ≈ 0.5（音符比格子密一倍）

    实测 6 首：对的都是 0.95~0.99，估成一半的两首是 0.46 / 0.49 —— 分得很开，
    阈值取 0.72 有一大截余量。

    只做「翻倍」不做「减半」：减半会把本来正确的曲子拖慢一倍，风险远大于收益。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size < 16:
        return tempo, 0
    ioi = np.diff(on)
    ioi = ioi[ioi > 0.01]
    if ioi.size < 8:
        return tempo, 0
    p10 = float(np.percentile(ioi, 10))

    times = 0

    def _gq(t):
        """这个速度下网格有多整齐（容差 0.2 格的命中率，相位取最优）。"""
        t = _t(t)
        try:
            slot_t = 30.0 / t
            ph = refine_phase(on.tolist(), t, float(on[0] % slot_t))
            return float(grid_quality(on.tolist(), t, ph))
        except Exception:
            return 0.0

    for _ in range(max_double):
        slot = 30.0 / _t(tempo)
        if p10 < min_ratio * slot:
            tempo *= 2.0
            times += 1
        else:
            break
    return tempo, times


def _with_ok(why, onsets, tempo, phase):
    """在 why 里补一份 `ok` 与算准的 `base_hit`。

    `ok=False` 的意思是「速度没测准，别信这个数」——以前只有一句 reason 文本，
    任务状态照样 done，脚本/批处理侧拿不到任何失败信号，谱却整首错格。
    """
    out = dict(why)
    # 命中率必须按**最终的** (tempo, phase) 重算：`core` 里那个 `base_hit`
    # 是用"翻倍前"的相位算的，速度一换相位就偏三成，那个数会低到 0.1 ——
    # 拿它判 `ok` 会把"倍速纠正做对了"的曲子判成不可信（实测 14/41/5 号）。
    hit = None
    try:
        hit = float(grid_quality(onsets, tempo, phase))
    except Exception:
        hit = None
    if hit is None:
        hit = out.get("base_hit")
    if not isinstance(hit, (int, float)):
        try:
            hit = grid_quality(onsets, tempo, phase)
        except Exception:
            hit = 0.0
    try:
        hit = float(hit)
    except (TypeError, ValueError):
        hit = 0.0
    out["base_hit"] = round(hit, 3)
    # 宽命中率（容差 0.2 格）对随机输入也很给面子：随机 40 个音实测 0.625。
    # 所以再加一道紧容差（0.08 格）—— 真节奏 1.000，随机 0.35，噪声分得开。
    # 等间隔输入两边都是 1.000，那是信息本来就够不到（等间隔没有拍信息），
    # 该走 alts 让用户点，不算"测错"，所以这里判 True。
    tight = 1.0
    if len(onsets) <= 5000:
        try:
            tight = float(_tight_hit(list(onsets), tempo))
        except Exception:
            tight = 1.0
    out["tight_hit"] = round(tight, 3)
    # 少于 8 个音本来就走"太短，用默认"；命中率不到 0.3 说明格子跟音符对不上。
    out["ok"] = bool(len(onsets) >= 8 and hit > 0.3 and tight > 0.5)
    # 最后一道：最终速度跟初测差太多就标不可信。
    #
    # 为什么不拦（只在事后标记）：我用「翻倍必须让网格更整齐」当闸门试过，
    # 48 首里直接打坏一首（41 号 210→315）—— 因为 `grid_quality` 是容差 0.2 格
    # 的宽判据，格子砍半时所有音照样落得进去，命中率反而更高（41 号实测
    # 105 BPM 0.990 / 210 BPM 0.692）。紧容差 `_tight_hit` 也分不开：
    # 该翻的 41 号只涨 0.008，不该翻的 16 号却涨 0.036，两者重叠。
    # 原来的 `p10/slot < 0.72` 在 48 首上是全对的，所以判据不动；
    # 只把"改得离谱"这件事暴露出来，让界面提示用户点候选确认。
    t_fit = out.get("t_fit")
    if isinstance(t_fit, (int, float)) and float(t_fit) > 0:
        ratio = float(tempo) / float(t_fit)
        out["ratio"] = round(ratio, 2)
        if ratio >= 2.6 or ratio <= 1.0 / 2.6:
            out["ok"] = False
            _r = str(out.get("reason") or "").strip()
            if "可能判错" not in _r:
                out["reason"] = (_r or "自动测速") + (
                    " · 速度跟初测差了 %.1f 倍，可能判错，请点候选确认" % ratio)
    return out


def select_grid(events, tempo_hint=None, tempo_min=40, tempo_max=260):
    """外层：先按老办法定网格，再用语料先验纠倍速（用户自己填了速度就不动）。

    不纠倍速的后果是实测出来的：48 首只有 32 首速度对，平均 F1 66.6%
    （速度给对时 94.9%）。纠完 47/48。
    """
    tempo, phase, why = _select_grid_core(events, tempo_hint, tempo_min, tempo_max)
    onsets = sorted(e["start"] for e in events if e.get("dur", 1) > 0)
    if tempo_hint:                     # 用户/调用方给了速度：一个字都不改
        # 注意：以前这里只做了 snap_phase，可 `_select_grid_core` 内部会顺手
        # 搜索/纠倍速，实测 48 首里有 3 首把**正确的**提示改坏
        # （150→93.8、174→87 `_hintcheck.py`）。表现就是"我改调性，速度也跟着变"。
        hint = _t(tempo_hint)
        # 入口必须挡一下。`_t` 只夹到 10~1000 BPM，而这里的约定是
        # tempo_min~tempo_max（40~260）；不挡的话传 1000 就真按 1000 出谱，
        # 一个格 0.03 秒，整首挤成一小段，状态还是 done。
        clamped = min(max(float(hint), float(tempo_min)), float(tempo_max))
        note = ""
        if abs(clamped - float(hint)) > 1e-6:
            note = "（你填的 %g 超出 %g~%g，按 %g 算）" % (
                float(hint), float(tempo_min), float(tempo_max), clamped)
            hint = clamped
        ph = refine_phase(onsets, hint, phase)
        return hint, snap_phase(onsets, hint, ph), {
            "reason": "按你填的速度" + note, "head": "手填速度",
            "base_hit": round(grid_quality(onsets, hint, ph), 3),
            "ok": True,        # 用户自己定的速度，不拿命中率去质疑它
        }
    new_t, new_ph, info = pick_tempo_scale(onsets, tempo, phase)
    if abs(new_t - tempo) > 0.5:
        ph = refine_phase(onsets, new_t, new_ph)
        why = dict(why)
        why.update(info)
        why["tempo_before_scale"] = round(tempo, 1)
        return new_t, snap_phase(onsets, new_t, ph), _with_ok(why, onsets, new_t, ph)
    # 速度没变也要把候选带出去：40 号(127 vs 254)、315 号(124 vs 166)就是
    # 先验判不出、只能让人点一下的两首，界面靠 why["alts"] 出候选按钮。
    if info.get("alts"):
        why = dict(why)
        why["alts"] = info["alts"]
    return tempo, snap_phase(onsets, tempo, phase), _with_ok(why, onsets, tempo, phase)


def _select_grid_core(events, tempo_hint=None, tempo_min=40, tempo_max=260):
    """在候选 (tempo, phase) 里挑「小节首列最不空」的那一组。

    为什么需要它：音频转谱常犯两种错 —— 倍速（2× / 0.5×）和相位差半拍。
    这两种错在成品上的表现非常一致：**每小节第一个音总是空的**，
    小节线落在半拍上，整首听起来像被人往后拖了一下。

    只按 onset 命中率挑会选错（倍速时命中率一样高），
    所以这里直接用「小节首列有音的比例」当主判据：
    正常曲子绝大多数小节都从强拍起音。

    返回 (tempo, offset, 依据)
    """
    onsets = sorted(e["start"] for e in events if e.get("dur", 1) > 0)
    t0, o0, r0 = fit_grid(onsets, tempo_hint=tempo_hint)
    t_fit = t0                     # 留一份初测值：最后要拿它判"改得离不离谱"
    if len(onsets) < 8:
        return t0, o0, {"reason": "太短，用默认", "base_hit": round(r0, 3),
                        "t_fit": round(t_fit, 2)}

    # 先修倍速：关键在于**这一步必须先做**。
    # 速度估成一半时，下面的"小节头对齐"会拿错的格子长度去对齐，
    # 结果反而更差（实测某首 F1 从 86 掉到 13）。
    t0, doubled = fix_tempo_octave(onsets, t0)
    r0 = grid_quality(onsets, t0, o0)

    def score(tempo, phase):
        if tempo <= 0:
            return -1, 0
        slot = 30.0 / _t(tempo)
        slots = set()
        for t in onsets:
            tt = t - phase
            if tt < -slot:
                continue
            slots.add(int(round(tt / slot)))
        if not slots:
            return -1, 0
        bars = max(slots) // 8 + 1
        filled = sum(1 for b in range(bars) if b * 8 in slots)
        return filled / bars, len(slots) / max(1, bars * 8)

    def best_phase(tempo, around):
        slot = 30.0 / _t(tempo)
        best = (-1, 0, around)
        for k in range(-4, 5):
            ph = around + k * slot / 4.0
            head, fill = score(tempo, ph)
            val = head + fill * 0.15
            if val > best[0]:
                best = (val, head, ph)
        return best[1], best[2]

    # 第一步：**只调相位，不动速度**。相位差半拍是常见错，改它没有副作用。
    head0, ph0 = best_phase(t0, o0)
    if head0 >= 0.65:
        ph0 = refine_phase(onsets, t0, ph0)
        return t0, ph0, {"reason": "仅调相位", "head": round(head0, 3),
                         "doubled": doubled,
                         "base_tempo": round(t0, 1), "base_hit": round(r0, 3),
                         "t_fit": round(t_fit, 2)}

    # 第二步：小节头还是大面积空着 → 才怀疑倍速。
    # 而且要求**明显变好**才换：换倍速会改变播放速度，宁可留错也不能无谓地换。
    best = (head0, t0, ph0, 1.0)
    for factor in (2.0, 0.5, 1.5, 2.0 / 3.0):
        tempo = t0 * factor
        if not (tempo_min <= tempo <= tempo_max):
            continue
        head, ph = best_phase(tempo, o0)
        if head >= 0.75 and head > head0 + 0.20 and head > best[0]:
            best = (head, tempo, ph, factor)

    head, tempo, phase, factor = best
    phase = refine_phase(onsets, tempo, phase)
    return tempo, phase, {"reason": "小节头对齐" if factor != 1.0 else "仅调相位",
                          "head": round(head, 3), "factor": factor,
                          "doubled": doubled,
                          "base_tempo": round(t0, 1), "base_hit": round(r0, 3),
                          "t_fit": round(t_fit, 2)}


def refine_phase(onsets, tempo, phase, span=0.5):
    """把相位精调到「落在格点 ±20% 内」最多的位置。

    为什么需要：`score()` 判断小节头时用的是四舍五入（容差半个格子），
    所以它挑出来的相位可能是偏 1/3 格的。测速翻倍之后尤其明显 ——
    相位是旧格子长度下算的，换速度后能偏三成，命中率会显示 0%，
    音也会被摆到格子边缘上。

    `span` 只能到 ±半格：`select_grid` 末尾的 `snap_phase` 会把相位的
    **整数格部分**折掉（那是"整首往后挪几小节"，不是对齐误差），
    所以超过半格的搜索没有意义。2026-10-23 试过放宽到 ±2 格，
    48 首基准 94.8 一动不动，与 snap_phase 的折叠一致。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.asarray(onsets, dtype=float)
    if on.size == 0 or tempo <= 0:
        return phase
    slot = 30.0 / _t(tempo)
    steps = max(20, int(40 * span))
    best_q, best_ph, best_d = -1.0, phase, 1e9
    for k in range(-steps, steps + 1):
        d = k * slot / 40.0
        q = grid_quality(on, tempo, phase + d)
        if q > best_q + 1e-9 or (abs(q - best_q) <= 1e-9 and abs(d) < best_d):
            best_q, best_ph, best_d = q, phase + d, abs(d)
    return best_ph


def estimate_drift(onsets, tempo, offset=0.0, win_slots=16, max_win=80):
    """【负结果，勿用】想靠分窗相位漂移反推真实速度，实测失败。

    实测：给 158 BPM（真值 160）时，每个 24 格窗口的最佳相位在 -95~+80 ms 之间
    纯乱跳，且命中率都是 100% —— 因为音符太稀（每 24 格只有 10~29 个音），
    任何相位都能把稀疏的点凑上，漂移根本没有可辨识度。
    结论：速度误差不能靠"事后拟合"补，只能靠用户给准速度。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size < 24 or tempo <= 0:
        return tempo, offset

    slot = 30.0 / _t(tempo)
    ks = np.round((on - offset) / slot).astype(int)
    total = int(ks.max()) + 1
    if total < win_slots:
        return tempo, offset

    shifts, idx = [], []
    for w in range(0, total - win_slots + 1, win_slots):
        sel = on[(ks >= w) & (ks < w + win_slots)]
        if sel.size < 8:
            continue
        base = offset + w * slot
        cand = np.arange(-0.5, 0.5, 0.01) * slot
        # 每个候选相位算命中率
        frac = (sel[None, :] - base - cand[:, None]) / slot
        err = np.abs(frac - np.round(frac))
        hit = (err < 0.20).mean(axis=1)
        b = int(np.argmax(hit))
        if hit[b] >= 0.45:
            shifts.append(cand[b])
            idx.append(w)

    if len(idx) < 3:
        return tempo, offset

    idx = np.asarray(idx, dtype=float)
    shifts = np.asarray(shifts, dtype=float)
    # 稳健：去掉两端各 10% 的离群
    A = np.vstack([np.ones_like(idx), idx]).T
    coef, *_ = np.linalg.lstsq(A, shifts, rcond=None)
    drift = float(coef[1])                      # 每格偏移多少秒
    if not np.isfinite(drift) or abs(drift / slot) > 0.15:
        return tempo, offset
    new_slot = slot + drift
    if new_slot <= 0:
        return tempo, offset
    return 30.0 / max(float(new_slot), 1e-6), offset - float(coef[0])


def refit_grid(onsets, tempo, offset=0.0, iters=4, tol=0.40, max_drift=0.20):
    """【负结果，勿用】用实际 onset 反拟合网格，实测无效。

    实测（歌 2，真值 160）：输入 158 → 出来 157.99，F1 14.0→11.8；
    155 → 155.01；168 → 168.01。全部纹丝不动。
    原因：挑「离格点近的音」做最小二乘，挑出来的样本本身就偏向当前网格，
    拟合必然把自己拟回原样。这是自我强化，不是估计。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size < 16 or tempo <= 0:
        return tempo, offset, 0.0

    slot = 30.0 / _t(tempo)
    a, b = float(offset), float(slot)

    # 先用分窗漂移把速度拉近，再用最小二乘微调
    t2, a2 = estimate_drift(on, tempo, offset)
    if abs(t2 - tempo) / _t(tempo) > 1e-4:
        tempo = t2
        slot = 30.0 / _t(tempo)
        a, b = float(a2), float(slot)

    for _ in range(iters):
        k = np.round((on - a) / b)
        resid = np.abs(on - (a + k * b))
        m = resid < tol * b
        if m.sum() < 12:
            break
        A = np.vstack([np.ones(int(m.sum())), k[m]]).T
        try:
            (a_new, b_new), *_ = np.linalg.lstsq(A, on[m], rcond=None)
        except Exception:
            break
        if not np.isfinite(a_new) or not np.isfinite(b_new) or b_new <= 0:
            break
        # 不许跑飞：斜率最多动 max_drift
        if abs(b_new / slot - 1.0) > max_drift:
            break
        a, b = float(a_new), float(b_new)

    tempo_out = 30.0 / max(float(b), 1e-6)
    # 注意：不要「小抖动就回退到输入值」。1~2% 的偏差正是要修的量，
    # 一格 0.2 秒、整首几百格，1% 就能让后半段全错位。
    tempo_out = round(float(tempo_out), 2)
    hit = grid_quality(on, tempo_out, a)
    return tempo_out, a, hit


def grid_quality(onsets, tempo, offset=0.0, tol_frac=0.20):
    """网格质量：有多大比例的音落在格点 ±20% 之内。

    比圆集中度 R 好读：R 会被"音符在格上但时值抖动"拉低，
    而命中率直接回答"这速度对不对"。实测 R=0.59 的案例命中率仍有 0.93。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.asarray(onsets, dtype=float)
    if on.size == 0:
        return 0.0
    slot = 30.0 / _t(tempo)
    q = (on - offset) / slot
    err = np.abs(q - np.round(q)) * slot
    return float((err < tol_frac * slot).sum()) / on.size


# ---------------------------------------------------------------- 倍速纠错
# 实测（一批现成 15 键谱，速度只靠自动估）：只有 32 首估对，错的 16 首里 14 首是
# **整数倍关系**（10 首 2 倍速、4 首半速）。一首 2 倍速等于整首报废，
# 平均 F1 从"速度给对"的 94.9% 掉到 66.6%。
#
# 为什么不能只靠"命中率"判对错：真值格子是 8 分音符，速度翻倍后每个音仍落在整格上
# （位置翻倍而已），命中率一样高。所以必须引入**音乐上的先验**：
#   语料统计：音符/小节 中位 6.49、格子填充率 中位 0.48、速度多集中在 120~180
# 在 est 的整数/简单分数倍里挑"音符/小节和填充率最像正常曲子"的那个 → 实测 32/48 修到 47/48。
#
# 注意：命中率门槛要**软**。转谱本身会多出音符（最差那首 697 音 vs 真值 488），
# 这些杂音落在奇数格上，减半后全成半整数、命中率掉到 0.5 ——
# 用硬门槛 0.90 会把"正确答案"直接淘汰，只剩错的（第一版就这么翻车的）。

TEMPO_SCALE_CAND = (1 / 4.0, 1 / 3.0, 2 / 5.0, 1 / 2.0, 3 / 5.0, 2 / 3.0, 3 / 4.0,
                    4 / 5.0, 1.0, 5 / 4.0, 4 / 3.0, 3 / 2.0, 5 / 3.0, 8 / 5.0,
                    2.0, 5 / 2.0, 3.0, 4.0)

# 权重由 `python sky15\_prior_f1.py` 按 **F1** 在**两套**数据上定（只看"速度对不对"
# 会选错：fill 0.8→1.0 让站点音色 93.14→94.96，但合成音 94.76→92.97，是等价交换）。
# `tight`（紧容差命中率，容差 0.08 格）由 `python sky15\_tight_score.py` 定：
# 0.5~0.7 是安全带，站点音色 93.14→95.06、合成音 94.75 不动（严格帕累托改进），
# 取 0.6。它救的是 315 号那种"×3/4 错位"（真值网格 0.981 vs ×4/3 只有 0.334），
# 对 40 号这种"×1 还是 ×2"本质上分不开（0.981 vs 0.970），所以那首仍靠 `alts` 让用户点。
TEMPO_PRIOR = {"center": 135.0, "hit": 2.0, "npb": 0.7, "fill": 0.8,
               "tight": 0.6, "bar_ref": 6.49, "fill_ref": 0.48}


def _scale_phase(onsets, tempo, guess, span=0.5, step=0.05):
    """在 guess 附近找让「落格」最多的相位（span 以新格子为单位）。

    两种用法，别混：
      · **打分**（span 给宽，1.6）：回答"这个速度下有没有某个相位能让音都落格"，
        速度对不对就该这么量。
      · **落谱**（span 给窄，0.5）：返回的相位要交给画谱，必须是格内的那点误差。
        搜太宽会返回"整格平移"的答案，音符全往后挪几格、小节线跟着错位，
        F1 从 85 直接掉到 0（实测歌 41：−1.21 格 → F1 0.0）。
    """
    tempo = _t(tempo)
    import numpy as np
    slot = 30.0 / _t(tempo)
    best_h, best_p = -1.0, guess
    for o in np.arange(-span, span + 1e-9, step) * slot:
        ph = guess + o
        x = (onsets - ph) / slot
        h = float((np.abs(x - np.round(x)) < 0.20).mean())
        if h > best_h:
            best_h, best_p = h, ph
    return best_p, best_h


def _scale_stats(onsets, tempo, phase):
    """返回 (音符/小节, 填充率)"""
    tempo = _t(tempo)
    import numpy as np
    slot = 30.0 / _t(tempo)
    y = (onsets - phase) / slot
    lo, hi = int(math.floor(y.min())), int(math.ceil(y.max()))
    total = max(1, hi - lo + 1)
    npb = onsets.size / (total / 8.0)
    fill = np.unique(np.round(y).astype(int) - lo).size / total
    return npb, fill


def _tight_hit(onsets, tempo, tol=0.08, span=1.6, step=0.02):
    """紧容差（0.08 格）下的最好命中率。

    和 `_scale_phase` 的宽容差（0.20 格）不是一回事：容差 0.2 时，速度错成 ×1.5
    的音符仍能被"最近的格"收下（实测 0.836），紧到 0.08 就只剩 0.836→0.836 的
    假象消失，变成真正区分"这速度对不对"的量。真值网格 0.981，×1.5 0.836、
    ×4/3 0.334、×0.75 0.633（`python sky15/_tight_hit.py`，48 首）。
    """
    tempo = _t(tempo)
    import numpy as np
    onsets = np.asarray(onsets, dtype=float).reshape(-1)
    if onsets.size == 0:
        return 0.0          # 空输入：以前 np.mean 空切片会吐 nan 并打 RuntimeWarning
    slot = 30.0 / _t(tempo)
    best = 0.0
    for o in np.arange(-span, span + 1e-9, step) * slot:
        x = (onsets - o) / slot
        h = float((np.abs(x - np.round(x)) < tol).mean())
        if h > best:
            best = h
    return best


def pick_tempo_scale(onsets, tempo, phase=0.0):
    """在 est 的整数/简单分数倍里挑最像正常曲子的那个速度。

    返回 (tempo, phase, info)。info 里有候选明细，方便排查。
    """
    tempo = _t(tempo)
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    p = TEMPO_PRIOR
    if on.size < 24 or tempo <= 0:
        return tempo, phase, {"reason": "太短"}

    best = (1e9, tempo, phase)
    rows = []
    for f in TEMPO_SCALE_CAND:
        t = tempo * f
        if not (45.0 <= t <= 380.0):
            continue
        # 打分用「这个速度下最好能对到多齐」（自由搜相位）——
        # 速度对不对，看的是"有没有某个相位能让音都落格"。
        # 但**返回**的相位要 snap 到 0 附近：谱从 0 秒开始，整格平移会把小节线挪走。
        ph_s, hit = _scale_phase(on, t, phase, span=1.6, step=0.06)
        npb, fill = _scale_stats(on, t, ph_s)
        s = p["hit"] * max(0.0, 0.97 - hit)
        s += p["npb"] * abs(math.log(max(npb, 1e-6) / p["bar_ref"]))
        s += p["fill"] * abs(fill - p["fill_ref"])
        s += p.get("tight", 0.0) * (1.0 - _tight_hit(on, t))
        s += abs(math.log(t / p["center"])) * 0.9
        rows.append((s, t, ph_s, hit, npb, fill))
        if s < best[0]:
            best = (s, t, ph_s)
    if not rows:
        return tempo, phase, {"reason": "无候选"}
    s, t, ph = best
    # 备选速度：按分差给前几个，UI 让用户一键换（48 首里 46 首一次就对，
    # 剩两首 40 号(127 vs 254)、315 号(124 vs 166=3/4 倍) 是这个先验也判不出的，
    # 与其猜，不如把候选摊开让人点一下）。
    rows.sort(key=lambda r: r[0])
    alts = []
    for sc, tt, _ph, ht, npb, fl in rows:
        if abs(tt - t) < 0.6:
            continue
        alts.append({"tempo": int(round(tt)), "factor": round(tt / _t(tempo), 3),
                     "npb": round(float(npb), 2), "fill": round(float(fl), 2),
                     "hit": round(float(ht), 3), "score": round(float(sc), 3)})
        if len(alts) >= 3:
            break
    return t, ph, {"reason": "倍速纠正" if abs(t - tempo) > 0.5 else "速度未变",
                   "factor": round(t / _t(tempo), 3), "from": round(tempo, 1),
                   "score": round(s, 3), "cand": len(rows), "alts": alts}


def snap_phase(onsets, tempo, phase):
    """把相位收进 [-半格, +半格]。

    相位只有**模一个格子**的部分是真正的对齐误差；整数格部分等于"整首往后挪几小节"。
    我们无从得知原曲第 1 小节该从哪一刻起（音频开头本来就有静音），
    所以按"谱从 0 秒开始"处理 —— 只保留格内的那点误差。
    不收的后果是实测的：歌 41 相位 −1.21 格 → F1 85.2 掉到 0.0，歌 7 −1.19 格 → 6.2。
    """
    tempo = _t(tempo)
    slot = 30.0 / _t(tempo)
    return phase - round(phase / slot) * slot


# ---------------------------------------------------------------- 逐段自适应对齐
# 为什么做 —— `_rubato.py`（结果表在 out/agent_warp/）把转谱事件的时间轴人为做
# 线性/正弦伸缩、真值不动，10 首缓存曲目平均 F1：
#
#     漂移      线性渐变    正弦起伏
#     ±0.0%      92.09      92.09
#     ±1.0%      21.46      13.72
#     ±2.0%      15.08      11.37
#     ±3.0%      13.16      10.70
#
# 一个全局 tempo + 一个全局相位撑不住任何速度起伏：±1% 的线性伸缩在三分钟里
# 就能让后半段漂掉五格以上。真人演奏 1% 的起伏属于基本操作，所以要补一个
# 「时间 → 格子坐标」的**单调**分段线性映射。
#
# 怎么估（两段式）：
#   ① 名义坐标 u(t) = (t - phase)/slot（全局 tempo/phase 给的）；
#   ② 在 u 上开重叠窗（默认 24 格 / 步 8 格），窗内求 onset 的**格内残差**
#      （圆均值相位，"这一小段该整体挪多少格"），并记集中度当权重；
#   ③ 残差只有模 1 的意义，按"离上一个窗最近"做相位展开 → 连续修正曲线 c(u)。
#      真正的速度起伏会让 c 长到好几格（±1% × 900 格 ≈ 9 格），只允许 ±半格
#      是折不过来的，展开这一步不能省；
#   ④ 中值滤波 + 集中度加权滑动平均，限幅 ±max_dev，逐段斜率夹到 ≤0.6 保证单调；
#   ⑤ 落格用 round(u - c(u))。
#
# 三条踩过的坑（别重犯）：
#   · 音符稀疏时残差没有可辨识度（`estimate_drift` 的注释里记过：给错 1.2% 速度时
#     24 格窗里只有 10~29 个音，任何相位都能把点凑上）。所以每窗要求
#     ≥ min_onsets 个**不同时刻**的 onset，不足的窗不参与、交给插值。
#   · 同刻和弦必须去重，否则密和弦把权重全压在少数位置上。
#   · 窗不能太窄：8 格窗在 4/4 里只有两小节，命中率曲线上一堆等价峰，
#     残差会随窗乱跳（`_rubato.py` 第一版一格一窗就翻了车）。

WARP_DEFAULTS = dict(win=24.0, step=8.0, min_onsets=8, max_dev=64.0,
                     smooth=3, passes=2, rounds=2,
                     tols=(0.5, 0.42, 0.35, 0.35),
                     min_conc=0.0, gap_sec=0.03,
                     min_dev=0.35, max_slope=0.6, slope_span=0.05,
                     align_shift=True)


class WarpMap:
    """时间(秒) → 格子坐标(浮点) 的单调分段线性映射。

    `knots_t` / `knots_u` 是折线节点。两端按端段斜率线性外推 —— 曲首弱起、
    曲尾渐慢时那段还在漂，clamp 会把两头掐死。
    `identity=True` 表示没估出可靠结果，调用方应当原样走全局网格。
    """

    __slots__ = ("knots_t", "knots_u", "identity", "info", "_lo_slope", "_hi_slope",
                 "slot")

    def __init__(self, knots_t=(), knots_u=(), info=None, slot=0.0):
        self.knots_t = [float(x) for x in knots_t]
        self.knots_u = [float(x) for x in knots_u]
        self.identity = len(self.knots_t) < 2 or len(self.knots_u) != len(self.knots_t)
        self.info = dict(info or {})
        self.slot = float(slot or 0.0)
        if self.identity:
            self._lo_slope = self._hi_slope = 1.0
        else:
            dt0 = max(1e-9, self.knots_t[1] - self.knots_t[0])
            dt1 = max(1e-9, self.knots_t[-1] - self.knots_t[-2])
            self._lo_slope = (self.knots_u[1] - self.knots_u[0]) / dt0
            self._hi_slope = (self.knots_u[-1] - self.knots_u[-2]) / dt1

    def at(self, t):
        """标量或数组都吃，返回格子坐标（浮点，0 = 网格相位处）。"""
        import numpy as np
        arr = np.asarray(t, dtype=float)
        if self.identity:
            # 没估出修正：退回名义网格 u = t/slot（与 arrange 的全局网格一致）
            if self.slot > 0:
                y = arr / self.slot
                return float(y) if arr.ndim == 0 else y
            return float(arr) if arr.ndim == 0 else arr
        kt = np.asarray(self.knots_t, dtype=float)
        ku = np.asarray(self.knots_u, dtype=float)
        y = np.interp(arr, kt, ku)
        y = np.where(arr < kt[0], ku[0] + (arr - kt[0]) * self._lo_slope, y)
        y = np.where(arr > kt[-1], ku[-1] + (arr - kt[-1]) * self._hi_slope, y)
        return float(y) if arr.ndim == 0 else y

    __call__ = at

    def __bool__(self):
        return not self.identity

    def __len__(self):
        return len(self.knots_t)

    def __repr__(self):
        if self.identity:
            return "WarpMap(identity)"
        return (f"WarpMap({len(self.knots_t)} knots "
                f"{self.knots_t[0]:.2f}~{self.knots_t[-1]:.2f}s "
                f"dev {self.info.get('dev_min', 0):+.2f}~{self.info.get('dev_max', 0):+.2f} 格)")


def _dedupe_onsets(onsets, gap_sec=0.03):
    """同刻和弦只留一个时间点（否则密和弦把窗内权重全压在少数位置）。"""
    import numpy as np
    on = np.sort(np.asarray(onsets, dtype=float))
    if on.size == 0:
        return on
    keep = [float(on[0])]
    for t in on[1:]:
        if t - keep[-1] >= gap_sec:
            keep.append(float(t))
    return np.asarray(keep)


def _warp_centers(u, win=24.0, step=8.0):
    """窗中心（格子坐标）。整首比一个窗还短就只给一个中心。"""
    import numpy as np
    lo, hi = float(u[0]), float(u[-1])
    if hi - lo < win:
        return np.asarray([(lo + hi) / 2.0])
    c0, c1 = lo + win / 2.0, hi - win / 2.0
    n = int(math.floor((c1 - c0) / max(1e-6, step))) + 1
    centers = c0 + step * np.arange(max(1, n))
    centers = centers[centers <= c1 + 1e-9]
    if centers.size < 2:
        centers = np.asarray([(lo + hi) / 2.0])
    return centers


def _window_resid(u, r, centers, win=24.0, min_onsets=8, tol=0.25):
    """窗内残差：按 `u` 选窗，对 `r` 的**格内偏离**求稳健圆均值。

    返回 (resid, conc)：resid ∈ (-0.5, 0.5]，单位是格；conc 是加权集中度
    （0~1，越接近 1 说明这一窗的音越整齐地落在同一相位上，当权重用）。
    窗内 onset 不够 min_onsets 个 → resid=nan、conc=0，不参与拟合。

    要分开传 `u`（选窗，固定不动）和 `r`（求相位，每轮迭代已扣掉当前修正）：
    第一轮窗内的音还散在 ±半格里，圆均值被压扁；r 被拉直后窗内才是一个尖峰。

    **稳健那一步不能省**：真实转谱里有一堆不落在八分格上的音（十六分、
    装饰音、三连音、误检），它们会把圆均值往半格上拽。先求全量圆均值，
    再把偏离超过 `tol` 格的踢掉、用 cos² 核加权重算 —— 一脚踢掉以后
    1% 漂移的真实曲子从 F1 47 抬到 80 以上，这是这套东西能不能用的分水岭。
    """
    import numpy as np
    half = float(win) / 2.0
    resid, conc = [], []
    for c in centers:
        sel = (u >= c - half) & (u <= c + half)
        if int(sel.sum()) < int(min_onsets):
            resid.append(float("nan"))
            conc.append(0.0)
            continue
        d = r[sel]
        d = d - np.round(d)                       # 格内偏离 ∈ (-0.5, 0.5]
        z0 = np.exp(2j * np.pi * d).mean()
        m0 = float(np.angle(z0)) / (2.0 * np.pi)
        d2 = d - m0
        d2 = d2 - np.round(d2)
        keep = np.abs(d2) <= float(tol)
        if int(keep.sum()) < int(min_onsets):
            resid.append(float("nan"))
            conc.append(0.0)
            continue
        w = np.cos(0.5 * np.pi * d2[keep] / float(tol)) ** 2
        z = (w * np.exp(2j * np.pi * d2[keep])).sum() / max(1e-9, w.sum())
        resid.append(m0 + float(np.angle(z)) / (2.0 * np.pi))
        conc.append(float(abs(z)))
    return np.asarray(resid), np.asarray(conc)


def _unwrap(resid, w=3):
    """c ≡ resid (mod 1)：用**斜率积分**展开，不用逐点"最近的整数代表元"。

    残差是 mod 1 的锯齿：逐点取离前一点最近的代表元，只要某个窗被噪声推过
    ±0.5 边界一次，后面整段就平移一格（实测一首曲丢 2 格 → 全部音换格，
    F1 95 → 5）。改成先解"相邻窗之间走了多少"——真漂移每 8 格只走 0.05~0.2 格，
    永远远小于 0.5——平滑后再累积，整数部分由累积自然给出，抗噪得多。
    """
    import numpy as np
    r = np.asarray(resid, dtype=float)
    if r.size == 1:
        return r.copy()
    d = np.diff(r)
    d = d - np.round(d - float(np.median(d)))
    if r.size >= 4:
        d = _smooth_seq(d, w=max(1, int(w)), passes=2)
    c = np.empty_like(r)
    c[0] = r[0]
    c[1:] = float(r[0]) + np.cumsum(d)
    return c


def _robust_rest(rest, w=5, lim=0.30):
    """把「扣掉全局斜坡之后剩下的残余」拉直：±1 格的翻面用中值干掉。

    残余是 mod 1 的：真值 0.49 和 -0.51 是同一个东西，噪声一推就在边界上翻面。
    直接 `_unwrap` 会把它当成"又跳了一格"，于是 c 开始 ±1 随机游走 ——
    实测就是这样一首曲子整段平移两格、F1 从 95 掉到 5。
    这里换个思路：中值滤波先把翻面的样本认出来（=中值附近 ±1 的那批），
    夹回 ±lim，再中值一次压毛刺。翻面不连续、真曲率连续，中值天然分得开。
    """
    import numpy as np
    r = np.asarray(rest, dtype=float)
    if r.size < 3:
        return r.copy()
    h = max(1, int(w) // 2)
    med = r.copy()
    for i in range(r.size):
        a, b = max(0, i - h), min(r.size, i + h + 1)
        med[i] = float(np.median(r[a:b]))
    d = r - med
    d = d - np.round(d)
    out = med + np.clip(d, -float(lim), float(lim))
    res = out.copy()
    for i in range(out.size):
        a, b = max(0, i - h), min(out.size, i + h + 1)
        res[i] = float(np.median(out[a:b]))
    return res


def _fix_steps(c, max_step=0.55, rounds=8):
    """修 `_unwrap` 剩下的整格踏空：真漂移是连续的，跳 >0.55 格必是算错了一整格。

    做法：找第一处 |Δc - 中位Δ| 超过阈值的点，把它之后的整段减掉那个整数，
    重复到没有为止。漂移本身的 Δc 每窗（8 格）也就 0.05~0.2 格，
    所以 0.55 的门槛分得干净；一次修完立刻重算 Δ，修一处不会连累别处。
    """
    import numpy as np
    c = np.asarray(c, dtype=float).copy()
    if c.size < 3:
        return c
    for _ in range(int(rounds)):
        d = np.diff(c)
        md = float(np.median(d))
        bad = np.where(np.abs(d - md) > float(max_step))[0]
        if bad.size == 0:
            break
        k = int(bad[0])
        c[k + 1:] -= int(round(float(d[k] - md)))
    return c


def _slope_fit(u, resid, conc, span=0.05, n=801):
    """全局斜率：resid_k ≈ a + m*u_k (mod 1)，在 m 网格上找 |Σ w·e^{2πi(resid-mu)}| 的峰。

    为什么需要它：逐窗残差只有 mod 1 的分辨率，而"当前 c 偏了整数格"和
    "残差本来就是 0.98"长得一模一样。逐窗 `_unwrap`（取离前一窗最近的代表元）
    在残差噪声 ±0.4 格的时候会一步错一格 —— 错一格就是整首后半段平移一个格，
    实测 F1 从 95 掉到 5。先把这个"整体斜率"从数据里一次性解出来，
    剩下要 unwrap 的只有曲率（< 0.3 格），逐窗就不会再错。

    span 默认 ±5%：够吃掉"漂移把全局速度估偏 1%"这类误差；再大的（倍速选错）
    得靠 `scan_tempo` 那一层，斜率解不动。
    返回 (m, a)：a 已折到 (-0.5, 0.5]。
    """
    import numpy as np
    u = np.asarray(u, dtype=float)
    resid = np.asarray(resid, dtype=float)
    conc = np.asarray(conc, dtype=float)
    ok = np.isfinite(resid)
    if int(ok.sum()) < 3:
        return 0.0, 0.0
    u, resid, conc = u[ok], resid[ok], conc[ok]
    w = conc / max(1e-9, float(conc.sum()))
    z = w * np.exp(2j * np.pi * resid)
    ms = np.linspace(-float(span), float(span), int(n))
    # Z(m) = Σ z_k e^{-2πi m u_k}；写成矩阵乘，150 窗 × 801 个 m 一步到位
    Z = np.exp(-2j * np.pi * np.outer(ms, u)) @ z
    k = int(np.argmax(np.abs(Z)))
    a = float(np.angle(Z[k])) / (2.0 * np.pi)
    a = a - round(a)
    return float(ms[k]), a


def _smooth_seq(c, w=3, weights=None, passes=2):
    """一维稳健平滑：中值滤波（抑单窗跳格）+ 加权滑动平均。"""
    import numpy as np
    c = np.asarray(c, dtype=float)
    if c.size < 3 or w < 2:
        return c.copy()
    h = max(1, int(w) // 2)
    out = c.copy()
    for _ in range(max(1, int(passes))):
        med = out.copy()
        for i in range(out.size):
            a, b = max(0, i - h), min(out.size, i + h + 1)
            med[i] = float(np.median(out[a:b]))
        out = med
        sm = out.copy()
        for i in range(out.size):
            a, b = max(0, i - h), min(out.size, i + h + 1)
            seg = out[a:b]
            ww = np.ones(b - a) if weights is None else np.asarray(weights[a:b], dtype=float)
            if ww.sum() <= 1e-9:
                ww = np.ones(b - a)
            sm[i] = float((seg * ww).sum() / ww.sum())
        out = sm
    return out


def fit_warp(onsets, tempo, phase=0.0, **kw):
    """估一条「时间 → 格子坐标」的单调分段线性映射（局部 warp）。

    onsets : [秒]，转谱事件的起点（同刻和弦会自己去重）
    tempo  : 全局速度（BPM），决定名义格子长度 slot = 30/tempo
    phase  : 全局相位（秒），与 `select_grid` 返回的 offset 同一个约定；
             映射在 phase + 第一个窗中心处**锚定在 k=0 附近**，所以不会把整首
             谱往后挪几格（`snap_phase` 的约定：只有格内那点误差有意义）。

    返回 `WarpMap`；`identity=True` 时表示"没有可靠修正，按全局网格走"。
    参数见 `WARP_DEFAULTS`：
      win       窗宽（格），24 格 = 3 小节（4/4）
      step      窗步（格），8 格 = 1 小节；重叠保证相位展开有冗余
      min_onsets 一个窗至少多少个不同时刻的 onset 才参与
      max_dev   修正量限幅（格）—— 默认 64：±1% 的时间伸缩在 3 分钟的曲子上
                就有 ±10 格，±5% 能到 ±50 格，限到 8 格会把后段砍成硬平台
      smooth    中值/均值滤波窗（以节点计），passes 平滑次数
      min_dev   拟合出的最大修正量小于它就当没漂（避免干净数据被噪声搅动）
      max_slope 修正曲线的斜率上限；u' = u - c(u)，0.6 保证单调且留余量
      align_shift 最后整体平移 ≤半格，把窗内密度不均衡带来的系统偏买回来
    """
    tempo = _t(tempo)
    import numpy as np
    p = dict(WARP_DEFAULTS)
    p.update({k: v for k, v in kw.items() if v is not None})
    tempo = float(tempo or 0.0)
    if tempo <= 0 or len(onsets) == 0:
        return WarpMap(info={"why": "无速度或无比"})
    slot = 30.0 / _t(tempo)
    on = _dedupe_onsets(onsets, p["gap_sec"])
    if on.size < 3 * int(p["min_onsets"]):
        return WarpMap(info={"why": f"音太少({on.size})"})

    u = (on - float(phase)) / slot
    u = u[u >= -0.5]
    if u.size < 3 * int(p["min_onsets"]):
        return WarpMap(info={"why": "相位之后音太少"})

    win, step = float(p["win"]), float(p["step"])
    min_on = int(p["min_onsets"])
    if u.size < max(min_on, win * 0.5):
        return WarpMap(info={"why": "窗不够"})

    # ---- 迭代：先粗求窗内相位 → 把 u 拉直 → 再求 → 收敛 ----
    # 第一轮窗内的音还散在 ±半格里（漂移越狠越散），圆均值被压扁；
    # 扣掉当前修正后窗内变成一个尖峰，圆均值才无偏。
    #
    # **容差必须由松到紧**：一上来就用 0.25 格去踢野点，会在"当前 c 还差
    # 0.3 格"的时候把真信号一起踢掉 —— 剩下的全是贴边界的那批音，
    # 残差被系统性低估，c 收敛到七成就卡死（实测 1% 漂移下 slope 只跟到 80%，
    # 攒到曲尾差 2 格，F1 5.5）。所以 0.5 → 0.42 → … → 0.16 逐级收紧。
    centers = _warp_centers(u, win, step)
    tols = list(p.get("tols") or (0.5, 0.42, 0.35, 0.35))
    c, conc = None, None
    slope = 0.0
    for _round in range(max(1, int(p.get("rounds", 2)))):
        for tl in tols:
            r = u if c is None else u - np.interp(u, centers, c, left=c[0], right=c[-1])
            resid, conc = _window_resid(u, r, centers, win, min_on, tol=tl)
            good = np.isfinite(resid) & (conc >= float(p["min_conc"]))
            if int(good.sum()) < 3:
                return WarpMap(info={"why": "有效窗不足"})
            cu, rs, cc = centers[good], resid[good], conc[good]
            add = _fix_steps(_unwrap(rs))
            if c is None:
                c = add
            else:
                # 上一轮的 c 在这批新中心处重新取值，再叠本轮的残差
                c = np.interp(cu, centers, c, left=c[0], right=c[-1]) + add
            centers, conc = cu, cc

    # ---- 平滑 + 限幅 + 单调 ----
    # 低阶多项式拟合是主力平滑：残差逐窗的噪声（±0.05~0.1 格）经累积会变成
    # 缓变的斜率误差（实测曲尾偏 ±1 格 → 全曲错位）。时间伸缩本来就慢，
    # 用 2 次多项式贴合"线性起→线性落的累积量"，比中值+均值滤波干净。
    poly = int(p.get("poly", 0) or 0)
    if poly >= 1 and c.size > poly + 1:
        try:
            pf = np.polyfit(np.asarray(centers, float), np.asarray(c, float), poly,
                            w=np.asarray(conc, float) + 1e-6)
            c = np.polyval(pf, np.asarray(centers, float))
        except Exception:
            pass
    c = _smooth_seq(c, w=int(p["smooth"]), weights=conc, passes=int(p["passes"]))
    # 限幅：±1% 的时间伸缩在 3 分钟的曲子上就是 ±10 格，±5% 能到 ±50 格。
    # 别设小了 —— 之前 8 格的上限直接把整首后段砍成硬平台。
    c = np.clip(c, -float(p["max_dev"]), float(p["max_dev"]))
    # u' 必须还在前进：允许 c 的斜率到 max_slope，剩下的夹掉
    lim = float(p["max_slope"])
    for k in range(1, c.size):
        du = float(centers[k] - centers[k - 1])
        c[k] = min(max(c[k], c[k - 1] - lim * du), c[k - 1] + lim * du)

    # ---- 全局半格微调：整体平移一点点让落点更贴格 ----
    # 每窗的相位是无偏的，但窗内密度不均会留个零点几格的系统偏（实测 0.1~0.2 格），
    # 整体平移一个常数把它买回来；只在 ±0.5 格里挑，不会改变小节归属。
    if bool(p.get("align_shift", True)):
        fr = u - np.interp(u, centers, c, left=c[0], right=c[-1])
        best, best_v = 0.0, -1.0
        for d in np.linspace(-0.5, 0.5, 201):
            x = fr - d
            v = float(np.exp(-0.5 * ((x - np.round(x)) / 0.12) ** 2).sum())
            if v > best_v:
                best_v, best = v, float(d)
        c = c - best

    # ---- 联调 (斜率, 相位)：把逐窗噪声累积成的缓变斜率误差吃掉 ----
    # 逐窗残差的噪声（±0.05 格）积到最后会变成 ±1 格的斜率误差 —— 实测曲尾
    # 平均落后 1.2 格，F1 直接从 95 掉到 11（整整一格的错位）。
    # 这里在去弯后的坐标 u' 上再搜一次 a + b·u'：b 用 1% 的细扫，
    # a 对每个 b 直接取圆均值（不用搜），目标是让落点尽量贴格。
    if bool(p.get("polish", True)) and c.size >= 3:
        up = u - np.interp(u, centers, c, left=c[0], right=c[-1])
        upc = np.interp(centers, u, up)
        best = None
        for b in np.linspace(-0.012, 0.012, 49):
            y = up * (1.0 - float(b))
            a = float(np.angle(np.exp(2j * np.pi * y).mean())) / (2 * np.pi)
            x = y - a
            v = float(np.exp(-0.5 * ((x - np.round(x)) / 0.12) ** 2).sum())
            if best is None or v > best[0]:
                best = (v, a, float(b))
        if best is not None:
            _v, a, b = best
            c = c + a + b * upc

    dev_max = float(np.max(np.abs(c))) if c.size else 0.0
    if dev_max < float(p["min_dev"]):
        return WarpMap(info={"why": f"修正量过小({dev_max:.3f}格)", "dev_max": dev_max})

    # ---- 端点补两个节点（用最近窗的修正量平推），再交给 np.interp ----
    t_knots = list(float(phase) + centers * slot)
    u_knots = list(centers - c)
    t_lo = float(phase) + float(u[0]) * slot
    t_hi = float(phase) + float(u[-1]) * slot
    if t_lo < t_knots[0] - 1e-9:
        t_knots.insert(0, t_lo)
        u_knots.insert(0, float(u[0]) - float(c[0]))
    if t_hi > t_knots[-1] + 1e-9:
        t_knots.append(t_hi)
        u_knots.append(float(u[-1]) - float(c[-1]))

    info = {"why": "分段对齐", "windows": int(c.size), "win": p["win"],
            "dev_min": round(float(np.min(c)), 3),
            "dev_max": round(float(np.max(c)), 3),
            "dev_max_abs": round(dev_max, 3),
            "conc_med": round(float(np.median(conc)), 3),
            "span_s": round(t_knots[-1] - t_knots[0], 2)}
    return WarpMap(t_knots, u_knots, info, slot=slot)


def warp_for(events, tempo, phase=0.0, refit=2, **kw):
    """`fit_warp` 的事件表封装 + "去弯后重估全局网格"的定点迭代。

    **坐标约定（调用方必读）**：返回的 `WarpMap` 的 y 轴单位是"**格子**"——
    拟合时那个 tempo 的 8 分音符为 1 格（`slot = 60/tempo/2` 秒），并且**已经减掉
    phase**：`warp(t) = (去弯后的格子坐标)`，直接 `round()` 就是格号。喂给
    `arrange(..., warp=w)` 时：
      · `offset` 不必再传（warp 已含相位；传了也不影响，warp 分支不读它）；
      · `tempo`/`tempo_exact` 应当与拟合时一致。若不一致（例如 `warp_for` 用浮点
        tempo 拟合、`arrange` 传 `int(round(tempo))`），`arrange._warp_fn` 会自动
        按 `WarpMap.slot / slot_dur` 换算到本次的格子单位，不会整体错位。
    也就是说：**warp_for 的输出只能配"它自己那套 tempo"**，跨 tempo 复用请让
    arrange 自己换算（`WarpMap.slot` 就是为此存的），别手工改 y 值。

    单次 `fit_warp` 是在**给定的全局 (tempo, phase)** 下解残差，而漂移会把这两个
    量本身一起带偏（实测 1% 漂移下 91 → 113.9、166 → 123.1，整首倍速选错；
    还有若干首相位偏半格），于是残差里混进一个恒定的斜率/相位偏，怎么平滑都
    追不回来。做法是洗完再洗：按当前 warp 把时间轴"去弯"，在去弯后的干净时间轴上
    重估 (tempo, phase)，再拟合一次，最后把每步串起来在**原始 onsets** 上取样成
    (t, 格子坐标) 折线。定点一旦收敛，输出的格子编号就和"没漂移时那套网格"一致 ——
    这正是 F1 比对的东西。
    """
    tempo = _t(tempo)
    import numpy as np

    onsets = [e["start"] for e in events if e.get("dur", 1) > 0]
    if len(onsets) == 0:
        return WarpMap(info={"why": "没有 onset"})
    slot = 30.0 / _t(tempo)
    ts = np.sort(np.asarray(onsets, dtype=float))
    x = ts.copy()
    tem, ph = float(tempo), float(phase)
    n_ok = 0
    for _ in range(max(0, int(refit)) + 1):
        w = fit_warp(x, tem, ph, **kw)
        if not w:
            break
        n_ok += 1
        x = ph + np.asarray(w(x), float) * slot
        # 去弯后的时间轴上重估网格。**放开倍速**：漂移会把 `select_grid` 的倍速
        # 选错（实测 91 → 113.9、166 → 123.1），只在小范围细调是救不回来的；
        # 去弯之后的轴线本来就该回到干净状态，直接整段重估。
        try:
            fake = [{"start": float(v), "dur": 0.05} for v in x]
            t2, p2, _r = select_grid(fake)
            if t2:
                tem, ph = float(t2), float(p2)
                slot = 30.0 / _t(tem)
        except Exception:
            pass
    if n_ok == 0:
        return WarpMap(info={"why": "分段对齐失败"})
    vals = (x - ph) / slot
    # 单调保护（折线不能回头）
    for k in range(1, vals.size):
        if vals[k] < vals[k - 1]:
            vals[k] = vals[k - 1]
    info = {"why": "分段对齐(refit)", "refit": int(n_ok),
            "tempo": round(float(tem), 2), "phase": round(float(ph), 4),
            "knots": int(ts.size), "span_s": round(float(ts[-1] - ts[0]), 2)}
    return WarpMap(list(ts), list(vals), info, slot=slot)


def grid_from_audio(path, tempo_hint=None, verbose=False):
    """直接从音频估：librosa 找 onset，再拟合网格。"""
    import librosa
    y, sr = librosa.load(path, sr=22050, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr,
                                        hop_length=512, backtrack=True)
    times = librosa.frames_to_time(frames, sr=sr, hop_length=512)
    if len(times) < 8:
        tempo, _b = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=512)
        tempo = float(tempo if not hasattr(tempo, "__len__") else tempo[0])
        return tempo, 0.0, 0.0
    return fit_grid(list(times), tempo_hint=tempo_hint, verbose=verbose)


if __name__ == "__main__":
    import sys
    print(grid_from_audio(sys.argv[1], verbose=True))






