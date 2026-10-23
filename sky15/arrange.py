# -*- coding: utf-8 -*-
"""重排器：任意音符事件 → 光遇 15 键 SMC。

这是整套东西的核心。转谱模型只负责把音频变成音符，能不能弹得出来全看这一步。

四件事：
  1. 选调    —— 找一个调，让最多的音落进 15 键音域
  2. 折音域  —— 超出的音按八度折回来
  3. 减声部  —— 每 8 分音符最多 3 个音（15 键的物理上限）
  4. 量化    —— 起点吸附到 8 分音符网格，加小节线

第 3 步对**钢琴谱**是另一套打法（`_pick_notes` 里的分左右手分支）：
钢琴谱一格常挤 5~16 个音（右手和弦 + 左手琶音），单纯按"时值最长的音"挑会
挑到内声部，听起来就是"音错了"。现在先按音高断层分左右手，
右手只留旋律，左手按和声功能取根音 + 五度/三度，一格凑成
「旋律 + 和弦音 + 低音」的三音形态。用 `hand_split="auto"` 只在密集曲子上启用，
单声部为主的曲子（48 首基准里的绝大多数）走的还是原来的路径，F1 不受影响。

每个环节都有几种可选做法，`VARIANTS` 里登记成策略名，好用基准测试台 A/B。
"""
import math

from . import layout, smc

# ---------------------------------------------------------------- 参数工具


def _slot_of(t, slot_dur):
    return int(round(t / slot_dur))


def _warp_fn(warp, slot_dur=None):
    """把几种 warp 写法统一成「秒 → 格子坐标(浮点)」。

    可接受：
      · `beats.WarpMap`（推荐，单调分段线性 + 两端线性外推）
      · (t_breaks, slot_breaks) 折线 —— 与 WarpMap 同一套插值规则
      · 任意 callable(t)->float

    **单位对齐**（外部调用方最容易踩的坎）：warp 的格子坐标是"以拟合时那个
    tempo 的 8 分音符为 1"，`WarpMap.slot` 记着拟合时的 `slot_dur`（秒/格）。
    如果 `arrange(tempo=...)` 传的 tempo 与之不同（例如 `warp_for` 用了浮点
    tempo 而这里传了 `int(round())`），直接相减就会整体错位。所以这里按
    `warp.slot / slot_dur` 把坐标换算到本次 arrange 的格子单位上。
    `slot_dur=None`（内部不传）时按原样透出，方便单独测。
    """
    if warp is None:
        return None
    if hasattr(warp, "at"):
        # WarpMap.identity=True（没估出可靠修正）→ 直接走老路径，浮点比较一个都别变
        if getattr(warp, "identity", False):
            return None
        w_slot = float(getattr(warp, "slot", 0.0) or 0.0)
        if slot_dur and w_slot > 0 and abs(w_slot - float(slot_dur)) > 1e-9:
            ratio = w_slot / float(slot_dur)      # 秒/格(warp) ÷ 秒/格(arrange)
            return lambda t: float(warp.at(t)) * ratio
        return warp.at
    if isinstance(warp, (tuple, list)) and len(warp) == 2:
        import numpy as np
        kt = np.asarray(warp[0], dtype=float)
        ku = np.asarray(warp[1], dtype=float)
        if kt.size < 2 or kt.size != ku.size:
            return None
        lo_s = (ku[1] - ku[0]) / max(1e-9, kt[1] - kt[0])
        hi_s = (ku[-1] - ku[-2]) / max(1e-9, kt[-1] - kt[-2])

        def fn(t):
            y = float(np.interp(t, kt, ku))
            if t < kt[0]:
                y = ku[0] + (t - kt[0]) * lo_s
            elif t > kt[-1]:
                y = ku[-1] + (t - kt[-1]) * hi_s
            return y
        return fn
    if callable(warp):
        return warp
    return None


def pick_key(events, forced=None, mode="fit"):
    """挑一个最省事的调。

    fit : 落域内的音最多 + 吸附误差最小（默认）
    inr : 只看"多少音正好落在调内"，不看误差
    low : 偏向低音区（歌曲线通常在中低音区，弹起来舒服）
    """
    if forced:
        return forced
    return pick_key_detail(events, mode=mode)[0]


def pick_key_detail(events, mode="fit"):
    """和 `pick_key` 一样，但把「第一名比第二名领先多少」也带出来。

    领先幅度是会判调的关键：领先很大时 pick_key 基本不会错（实测 141/158/289/6
    领先 117~318 分，全对）；领先很小时它就是在两个只差一个升降号的近关系调里
    猜硬币（41 号 Bb/Eb 只差 3 分、84 号 D/A 差 21 分），这种场合才该参考
    music21。归一化用「每音的领先分」（margin / 事件数），阈值 0.05：
    实测要听 external 的两首是 0.006 / 0.029，不该听的是 0.096~0.29。
    """
    best, best_score = None, None
    second = None
    for k in layout.KEYS:
        inr = 0
        err = 0.0
        for e in events:
            c, d = layout.snap(e["pitch"], k)
            if abs(d) == 0:
                inr += 1
            err += abs(d)
        if mode == "inr":
            score = inr
        elif mode == "low":
            import statistics
            med = statistics.median(e["pitch"] for e in events)
            score = inr * 2 - err - abs((med - layout.KEY_MIDI[k]) - 12) * 0.5
        else:
            score = inr * 2 - err
        if best_score is None or score > best_score:
            best, second, best_score = k, best_score, score
        elif second is None or score > second:
            second = score
    n = max(1, len(events))
    margin = (best_score - (second if second is not None else best_score - 1e9)) / n
    return best, margin


# ---------------------------------------------------------------- 声部取舍


def _dedupe(notes):
    """同音高去重，留时值×力度最大的。"""
    byp = {}
    for n in notes:
        p = n["pitch"]
        if p not in byp or n["dur"] * n["vel"] > byp[p]["dur"] * byp[p]["vel"]:
            byp[p] = n
    return byp


def _wt(n):
    return n["dur"] * n["vel"]


def _split_hands(cand, gap_min=7):
    """按音高断层把一只手里的一堆音分成 (左手低音组, 右手高音组)。

    钢琴谱的典型形态是「左手低音区、右手高音区」，中间常常是空的：
    C3 以下一堆、C4 以上一堆。所以取**最大的那个相邻音高间隔**当分割点，
    比固定阈值（比如 C4）稳 —— 谱子整体高八度、低八度都不会失效。

    分不开（音数不足、或最大间隔小于 gap_min）时返回 (None, None)，
    交给原来的单声部逻辑。

    gap_min 试过 5/7/9（变体 piano_gap5 / piano_gap9）：48 首基准 94.7 / 94.8 /
    94.8，钢琴化压力输入三者一样，真谱《雨爱》也是 7 最好。取 7（五度）。
    """
    if len(cand) < 4:
        return None, None
    bi, bg = 0, 0
    for i in range(len(cand) - 1):
        g = cand[i + 1]["pitch"] - cand[i]["pitch"]
        if g > bg:
            bi, bg = i, g
    if bg < gap_min:
        return None, None
    return cand[:bi + 1], cand[bi + 1:]


def _bass_chord(low, limit):
    """左手取音：按**和声功能**挑，不按时值。

    1) 最低音当根音
    2) 找与根音成 7 个半音的（五度；也可能是低八度的四度，听感一样）
    3) 没有五度就找 3/4 个半音的（三度，定大小调色彩）
    4) 都没有就取根音上方最近的一个
    同一个音级（pitch class）只留一个 —— 两个八度叠同一个音在 15 键上是白占名额。

    为什么按时值挑不行：钢琴谱左手是十六分琶音，时值全都短，
    bass_mode="strong"（低音要够长）会把根音整个筛掉，格子只剩旋律一个音。
    """
    if not low:
        return []
    root = low[0]
    keep = [root]
    seen = {root["pitch"] % 12}
    rest = [n for n in low[1:] if n["pitch"] % 12 not in seen]

    for ivs in ((7,), (3, 4)):
        if len(keep) >= limit:
            break
        for n in rest:
            if n["pitch"] % 12 in seen:
                continue
            if (n["pitch"] - root["pitch"]) % 12 in ivs:
                keep.append(n)
                seen.add(n["pitch"] % 12)
                break
    if len(keep) < limit:
        for n in rest:
            if n["pitch"] % 12 in seen:
                continue
            keep.append(n)
            break
    return keep[:limit]


def _bass_chord_m21(low, limit, chord, allow_synth=False):
    """左手取音（music21 版）：按**这一小节检测出来的和弦**配，不按最低音猜根音。

    `_bass_chord` 的根音是"本格最低音"，钢琴谱左手的琶音里那个音经常是经过音/
    七音/转位，配出来的和弦和右手旋律打架。这里改成：

      1) 和弦根音（pitch class）
      2) 五度
      3) 三度（性质由检测结果给：min=3，maj=4，dim/aug 照抄）
      都只在**这一格里真实存在的音**里找；找不到根音且 allow_synth 时，
      在低音区合成一个根音（真正的编配就是这么做的），否则返回空由调用方回退。

    同一个音级只留一个（15 键上两个八度叠同一个音是白占名额），取最低的实例
    —— 左手保持在下方，别爬到旋律上去。
    """
    if not low:
        return []
    root_pc = chord.get("root_pc")
    if root_pc is None:
        return _bass_chord(low, limit)
    pcs = [root_pc % 12,
           (root_pc + int(chord.get("fifth_semis", 7))) % 12,
           (root_pc + int(chord.get("third_semis", 4))) % 12]
    keep, seen = [], set()
    for pc in pcs:
        if len(keep) >= limit:
            break
        if pc in seen:
            continue
        cands = [n for n in low if n["pitch"] % 12 == pc]
        if cands:
            keep.append(min(cands, key=lambda n: n["pitch"]))
            seen.add(pc)
    if allow_synth and len(keep) < limit and (root_pc % 12) not in seen:
        base = keep[0]["pitch"] if keep else low[0]["pitch"]
        p = base + ((root_pc - base) % 12)
        if p - low[0]["pitch"] > 12:      # 别把合成音放到左手组的上面去
            p -= 12
        keep.append({"pitch": p, "dur": low[0].get("dur", 0.25),
                     "vel": low[0].get("vel", 90), "synth": True})
        seen.add(root_pc % 12)
    if not keep:
        return _bass_chord(low, limit)
    return keep[:limit]


def _pick_melody(pool, mel_mode, prev_mel):
    """从一组候选里挑旋律。pool 已按音高升序。

    top    取最高音
    smooth 取离上一格旋律最近的（高音区里找）
    long   在高音区里取时值×力度最长的
    并列时一律**取高音**：钢琴谱一个和弦里时值全都一样，
    老写法 `max(...)` 遇到并列会返回第一个（也就是上半区里最低的那个内声部音），
    这就是钢琴谱「音错了」最直接的原因。

    实测（只改这一处并列规则，其它参数不动）：
      48 首基准 F1       94.9% → 94.9%（不掉）
      钢琴化压力输入 F1  42.4% → 46.6%
      真谱《雨爱》旋律命中 88.2% → 95.8%
    """
    if not pool:
        return None
    if mel_mode == "smooth" and prev_mel is not None:
        upper = [n for n in pool if n["pitch"] >= pool[len(pool) // 2]["pitch"]] or pool
        return min(upper, key=lambda n: (abs(n["pitch"] - prev_mel), -n["pitch"]))
    if mel_mode == "long":
        upper = [n for n in pool if n["pitch"] >= pool[len(pool) // 2]["pitch"]] or pool
        return max(upper, key=lambda n: (_wt(n), n["pitch"]))
    return pool[-1]


# 负结果记录（2026-10-22）：试过在 _pick_notes 末尾去掉八度重复
# （同 pitch class 只留第一个）。站点播放音频的误差分类里「差八度」
# 确实占多出来音的 30~47%，看着像最大的一块可修项，但实测两个基准都掉：
#   站点播放闭环 6 首 93.4 → 93.0，48 首基准 94.9 → 94.8。
# 原因：这些谱子里八度叠置是**编配本身的写法**（旋律加高八度、根音加低八度），
# 不是识别错误。别再去掉它。


def _pick_notes(notes, limit=3, bass_mode="lowest", mel_mode="top", prev_mel=None,
                harmony=False, hand_split=True, hand_gap=7, hand_fill="third",
                slot=0, lh_beat=0, mel_hint=None, mel_tol=0.6, mel_hi_only=False,
                chord=None, chord_conf_min=0.0, lh_synth=False):
    """一个格子里挑至多 limit 个音。

    光遇 15 键谱的常规形态不是"只有旋律"，而是
    **顶上一个旋律音 + 下面两个和弦音（根音 + 五度/三度）**。
    所以对钢琴谱这种密集输入，取音顺序是：

      1) 先按音高断层分左右手（`_split_hands`）
      2) 右手只留旋律一个，内声部全丢（`_pick_melody`）
      3) 左手按和声功能取 根音 + 五度/三度（`_bass_chord`）
      4) 左手只够一个音时，从右手和弦里补一个真实的音（`hand_fill`）
      5) 总数不超 limit

    分不开手（音数 ≤ limit、或最大间隔太小）时退回单声部逻辑：
    旋律 + 最低音 + 按时值补中间声部。

    旋律：默认取最高音；
          'smooth' 时优先选离上一格旋律最近的音；
          'long'   在较高的一半里选时值×力度最长的（旋律通常是长音）。
          若事件带 `voice` 标记（简谱双声部会给），**voice 0 优先当旋律**。
    低音：默认取最低音；'none' 不要；'strong' 只保留时值够长的
          （分左右手的路径下 root 不再受这条限制 —— 钢琴谱的左手根音常常是
          十六分音符，按时值筛会把它全部筛掉，而它恰恰是和声的锚）。
    harmony=True 时，与旋律构成不协和音程的陪衬音会被丢掉（转谱碎音的常见形态）。

    这一整套只对"格子被挤满"的输入生效，所以对 48 首基准（98% 的格子 ≤3 音）
    几乎无影响 —— 见 `hand_split="auto"` 的门槛说明。
    """
    if not notes:
        return []
    byp = _dedupe(notes)
    cand = sorted(byp.values(), key=lambda n: n["pitch"])
    if len(cand) <= limit:
        return cand

    # 有明确声部标记时，旋律直接从 voice 0 里挑 —— 这比任何启发式都准
    v0 = [n for n in cand if n.get("voice", 0) == 0]
    vpool = v0 if v0 else cand
    dissonant = {1, 2, 6, 10, 11}

    # ---------------------------------------------------------- 外部旋律提示
    # mel_hint 来自 melody.py（demucs + torchcrepe 的单音高追踪），
    # 是「第 slot 格最突出的那个 f0」。只在提示音高附近 mel_tol 半音内
    # **真的存在一个转谱音符**时才采纳 —— 提示说这里有音、转谱却没读到音，
    # 那就是提示自己的幻觉（crepe 在踏板余响上会给伪 f0），不能凭空造音。
    hinted = None
    if mel_hint is not None and mel_hint == mel_hint:      # 排除 nan
        near = [n for n in vpool if abs(n["pitch"] - mel_hint) <= mel_tol]
        if near:
            hinted = min(near, key=lambda n: (abs(n["pitch"] - mel_hint), -n["pitch"]))

    # ---------------------------------------------------------- 分左右手
    if hand_split and bass_mode != "none":
        low, high = _split_hands(vpool, hand_gap)
        if high:
            use = hinted
            if use is not None and mel_hi_only and use["pitch"] < high[0]["pitch"]:
                use = None
            melody = use if use is not None else _pick_melody(high, mel_mode, prev_mel)
            keep = [melody]
            lh = [n for n in low if n is not melody]
            if lh_beat and slot % lh_beat:
                # 左手琶音只留拍点上的音：非拍点的十六分音符在 8 分格里是糊的
                lh = []
            if harmony:
                clean = [n for n in lh
                         if abs(n["pitch"] - melody["pitch"]) not in dissonant]
                if clean:
                    lh = clean
            if lh:
                # 有音乐 21 给的「本小节和弦」且置信度够 → 按真实和弦配左手
                # （根音 → 五度 → 三度）；否则回退老的「最低音当根音」
                if chord is not None and chord.get("root_pc") is not None \
                        and chord.get("confidence", 0.0) >= chord_conf_min:
                    keep.extend(_bass_chord_m21(lh, limit - len(keep), chord,
                                                allow_synth=lh_synth))
                else:
                    keep.extend(_bass_chord(lh, limit - len(keep)))
            if hand_fill != "none" and len(keep) < limit:
                # 左手只出一个根音时还差一个音。从右手和弦里补**真实的**音，
                # 不凭空造三度：
                #   'third' 补贴着旋律的内声部（默认）：旋律 + 三度/七度 + 低音，
                #           三音格比例和协和度都是这几个方案里最好的
                #   'bass'  补右手和弦自己的低音（和弦根音）——「取顶 + 根音」，
                #           骨架更清楚，但跟左手根音功能重复，格子听着空
                used = {n["pitch"] for n in keep}
                rest = [n for n in high if n is not melody and n["pitch"] not in used]
                if harmony:
                    clean = [n for n in rest
                             if abs(n["pitch"] - melody["pitch"]) % 12 not in dissonant]
                    if clean:
                        rest = clean
                if rest:
                    keep.append(min(rest, key=lambda n: n["pitch"])
                                if hand_fill == "bass"
                                else max(rest, key=lambda n: n["pitch"]))
            return keep

    # ---------------------------------------------------------- 单声部退回
    melody = hinted if hinted is not None else _pick_melody(vpool, mel_mode, prev_mel)

    keep = [melody]
    rest = [n for n in cand if n is not melody]

    if harmony:
        # 与旋律差 1/2/6/10/11 半音的多半是转谱噪声，先剔除
        clean = [n for n in rest if abs(n["pitch"] - melody["pitch"]) not in dissonant]
        if clean:
            rest = clean

    # 低音
    if bass_mode != "none" and rest and len(keep) < limit:
        low = rest[0]
        strong = _wt(low) >= _wt(melody) * 0.5
        if bass_mode == "lowest" or (bass_mode == "strong" and strong):
            if low["pitch"] != melody["pitch"]:
                keep.append(low)
                rest = rest[1:]

    # 中间声部按时值×力度
    rest.sort(key=_wt, reverse=True)
    for n in rest:
        if len(keep) >= limit:
            break
        keep.append(n)
    return keep


def _dejitter(events, slot_dur, rel=0.5):
    """琶音经过音去抖。

    钢琴谱的左手是快速琶音：同一个音高会在前后两个十六分位置上各出现一次，
    量化到八分格就变成"同一格/邻格反复点名"，听起来是糊的。
    规则：时值短于 rel×格长、且相邻格里也有同一个音高的音 → 当经过音丢掉。

    **负结果（实测没用，默认关）**：rel=0.5 时
      48 首基准 F1       94.7%（不分手的单声部路径不受影响，差异在被挤满的格子上）
      钢琴化压力输入 F1  45.7% → 45.8%（+0.1，就是噪声）
      真谱《雨爱》旋律命中 96.6% → 95.0%（反而掉了 1.6）
    原因：钢琴谱里相邻格的同音高常常是**真的重复音**（同一个和弦在不同八分上重弹），
    砍掉以后旋律线断口变多。留着这个函数只是给以后换数据集时还能再 A/B。
    """
    grid = {}
    for e in events:
        grid.setdefault(_slot_of(e["start"], slot_dur), []).append(e)
    near_p = {s: {e["pitch"] for e in v} for s, v in grid.items()}
    out = []
    for s, v in grid.items():
        near = near_p.get(s - 1, set()) | near_p.get(s + 1, set())
        for e in v:
            if e.get("dur", 0) <= slot_dur * rel and e["pitch"] in near:
                continue
            out.append(e)
    return out


# ---------------------------------------------------------------- 主流程


def _pitch_order():
    """15 个字符按音高从低到高排列，索引 0..14（`_fold_melody` 用）。"""
    return sorted(layout.OFFSET_OF, key=lambda c: layout.OFFSET_OF[c])


def _fold_melody(chars, prev_idx, max_gap, order=None):
    """把这一格的旋律音按八度折回来，让它离上一格近一点。

    **结论：试过，默认关（`mel_fold=0.0`），因为降 F1。** 记在这里免得重走：
    `_leap_stats.py` 量出官方 48 首旋律相邻音的音级距离均值 2.36、超 5 级 9.9%；
    我们排出来均值 3.66、超 5 级 22.3%、超 10 级 1.8%（官方 0.3%）—— 听感上确实更"跳"。
    但把旋律按八度折回来对齐上一格，F1 反而掉：fold6 91.3、fold5 89.0、fold4 83.8、
    fold3 74.8（base 94.76）。说明官方的"平顺"不是靠挪八度，而是他听到的旋律本身
    就跟我们不一样；我们的跳来自转谱的八度误判（误差里 30~47% 是差八度），
    挪八度只是把错的音挪到另一个错的八度。要治得治转谱，不是治排谱。

    折叠本身是**同音名换八度**（索引 ±7，永远还在调内），所以逻辑没写错，是方向不对。
    """
    if not max_gap or not chars or prev_idx is None:
        return chars
    order = order or _pitch_order()
    idx = order.index(chars[0])
    if abs(idx - prev_idx) <= max_gap:
        return chars
    cands = [k for k in (idx - 7, idx + 7) if 0 <= k < len(order)]
    if not cands:
        return chars
    best = min(cands, key=lambda k: (abs(k - prev_idx), k))
    if abs(best - prev_idx) >= abs(idx - prev_idx):
        return chars
    newc = order[best]
    if newc in chars[1:]:
        return chars
    chars = [newc] + chars[1:]
    return chars


def arrange(events, tempo=120, time_sig="4/4", key=None, title="",
            difficulty=None, has8vb=None, max_notes=3,
            key_mode="fit", mel_mode="long", bass_mode="strong", harmony=True,
            hand_split="auto", hand_gap=7, hand_fill="third",
            lh_beat=0, dejitter=0.0,
            drop_short=0.0, drop_short_rel=0.12, snap_limit=2, min_vel=0, vel_pct=0.0,
            quant="round", offset=0.0, warp=None, melody=None, mel_tol=0.6,
            mel_hi_only=False,
            mel_fold=0.0,
            lh_m21=False, lh_conf_min=0.55, lh_synth=False, tempo_exact=None):
    """events: [{'start':秒, 'dur':秒, 'pitch':MIDI, 'vel':0-127}] → SMC 文本

    默认参数是 15 首实测跑出来的最优组合（见 bench.py）：
      mel_mode="long"    旋律取"上半区里时值最长的音"，比"最高音"稳
      harmony=True       丢掉与旋律构成不协和音程的陪衬（转谱碎音的常见形态）
      bass_mode="strong" 低音要够长才留，避免抓到路过的装饰音
      hand_split="auto"  密集格子先分左右手：右手留旋律，左手按和声取根音+五度
                         （"auto"：整首曲子的格子有 10% 以上被挤满才启用，
                          这样单声部为主的曲子仍然走老的取音路径）
      drop_short=0.08    短于 80ms 的音是转谱毛刺
      snap_limit=2       偏离调内超过 2 个半音的直接丢，不硬凑

    drop_short : 丢掉短于该秒数的音
    snap_limit : 吸附误差超过该半音数就直接丢掉（None=从不丢）
    vel_pct    : 丢掉力度最低的这一比例（0~1）
    quant      : round=就近吸附 / floor=向前对齐
    offset     : 网格相位（秒）—— 真实音频里第一拍不在 0 上时必填
    warp       : 逐段对齐映射（`beats.fit_warp` / `beats.warp_for` 的返回值），
                 也可以直接给 callable(t)->格子坐标 或 (t_breaks, slot_breaks)。
                 **默认 None = 老行为**（全局单一相位）。开了以后落格改用
                 `round(warp(t))`，并按时间顺序做单调夹取，保证后面的音不会
                 跑到前面的格子去。`offset` 仍然有效：warp 是"绝对时间 → 格子
                 坐标"，它的名义坐标按同一个 (tempo, offset) 拟合。
    hand_gap   : 分左右手要求的最小音高断层（半音）。7=五度；
                 太小会把和弦内部的三度误当断层，太大则分不开手。
    hand_fill  : 左手只出一个根音时补什么：
                 "third"=贴着旋律的内声部（默认，三音格比例和协和度最好）/
                 "bass"=右手和弦低音（更"空"，但每个音都是骨架音）/
                 "none"=不补，留 2 个音
    lh_beat    : >0 时左手只在每 lh_beat 格留音（8 分格下 2 = 每拍一次）。
                 **负结果，默认 0**：lh_beat=2 时钢琴化 F1 45.7%→44.6%，
                 真谱 3 音率 41%→40%、平均音数 2.03→1.93，左手根音被砍掉后
                 格子变成"旋律 + 一个远低音"，反而更空。lh_beat=4 更差（42.2%）。
    dejitter   : >0 时开启琶音经过音去抖，数值 = "短音"判定的格长比例（试过 0.5）。
                 **负结果，默认 0**，见 `_dejitter` 的注释。
    melody     : 外部旋律提示 —— 长度 ≥ 总格数、每项为 MIDI 或 None/nan。
                 来自 `melody.melody_slots()`（demucs 分离 + torchcrepe 单音高追踪）。
                 默认 None = 沿用老逻辑，完全不影响现有变体。
    mel_tol    : 采纳提示的容差（半音）。提示音高 mel_tol 之内没有转谱音符 → 回退。
    mel_hi_only: True 时只接受落在"右手高音组"里的提示，防止 crepe 把低音声部
                 当主旋律时报上来（实测这一条反而更差，见 report.md）。
    lh_m21     : True 时用 `chords.chords_per_bar()`（music21）逐小节判和弦，
                 左手按 和弦根音 → 五度 → 三度 的顺序取真实音符（`_bass_chord_m21`）。
                 **默认 False**：默认路径一字不改，想用就选 `lh_m21` 变体。
    lh_conf_min: 和弦置信度低于此值时该小节回退老的 `_bass_chord`（默认 0.55）。
    lh_synth   : True 时允许在低音区**合成**一个根音（本格真的没有根音时）。
                 编配里这是常规做法，但会引入输入里不存在的音，默认关。
    """
    if events:
        events = [e for e in events
                  if e.get("dur", 0) >= drop_short and e.get("vel", 90) >= min_vel]
    if events and drop_short_rel > 0:
        # 相对门槛：真实录音带混响时，转谱给出的时值会整体变短
        # （实测中位数从 0.58s 掉到 0.15s），固定 80ms 门槛会误杀四分之一的真实音符。
        # 所以门槛跟着"这首曲子的中位时值"走。
        #
        # 0.12 而不是 0.20（2026-10-23 联合标定，`_drop_joint.py`）：
        #   站点音色 + Transkun：0.20 → 91.68%，0.12 → **93.14%**（41 胜 1 负）
        #   合成音 + 字节：    0.20 → 94.84%，0.12 → 94.76%（只掉 0.08）
        # 两个数据集一起看 0.12 最高（93.95 均值）。原因是 Transkun 给的时值更长、
        # 更真，短音符里虚音占比更高，剪得狠一点反而赚。
        import statistics
        med = statistics.median(e.get("dur", 0) for e in events)
        cut = drop_short_rel * med
        if cut > drop_short:
            events = [e for e in events if e.get("dur", 0) >= cut]
    if events and vel_pct > 0:
        vs = sorted(e.get("vel", 90) for e in events)
        cut = vs[min(len(vs) - 1, int(len(vs) * vel_pct))]
        events = [e for e in events if e.get("vel", 90) > cut]
    if not events:
        return smc.emit(title or "未命名", tempo, time_sig, key or "Ab",
                        False, "1", [])

    root, key_margin = (pick_key_detail(events, mode=key_mode) if key is None
                        else (key, 1.0))
    # 判调兜底：pick_key 是"落域音最多 + 吸附误差最小"，在两个近关系调之间打平时猜错。
    # 只在**它自己没把握**（归一化领先 < 0.05）时才听 music21：
    #   · 41 号 Bb/Eb 只差 3 分（0.006）→ 听 music21，改对，F1 3.31 → 99.8
    #   · 84 号 D/A 差 21 分（0.029）→ 听 music21，改对
    #   · 141/158/289/6 领先 0.096~0.29 → 不听，music21 在这四首上全错
    # 全库 80 首实测：只信 pick_key 77/80，一律信 music21 75/80，这个开关 80/80。
    if key is None and key_margin < 0.05:
        try:
            from . import chords as _chords
            if _chords.available():
                root = _chords.analyze_key(events, tempo, time_sig) or root
        except Exception:
            pass
    slot_dur = 60.0 / float(tempo_exact or tempo) / 2.0      # 一个 8 分音符

    grid = {}
    if dejitter > 0:
        events = _dejitter(events, slot_dur, dejitter)
    warp_fn = _warp_fn(warp, slot_dur)
    if warp_fn is None:
        for e in events:
            t = e["start"] - offset
            s = max(0, (int(t / slot_dur) if quant == "floor"
                        else _slot_of(t, slot_dur)))
            grid.setdefault(s, []).append(e)
    else:
        # 逐段对齐：先按时间排序，再对整数格做单调夹取 ——
        # warp 只保证"格子坐标"单调，四舍五入后仍可能出现后音落到前格 （
        # 比如相邻两音挤在 .49/.51 两个坐标上），夹一下最省事。
        placed = []
        for e in events:
            u = float(warp_fn(e["start"]))
            s = int(math.floor(u + 1e-9)) if quant == "floor" else int(round(u))
            placed.append((e["start"], max(0, s), e))
        placed.sort(key=lambda x: x[0])
        prev = 0
        for _t, s, e in placed:
            s = max(prev, s)
            prev = s
            grid.setdefault(s, []).append(e)

    # 分左右手是给「钢琴谱」用的取音法，单声部为主的曲子用不着，硬套反而掉分。
    # 先量这首到底密不密：去重后音数超过 max_notes 的格子占比。
    #   48 首基准（单声部转谱）   3.4%  → 不触发（F1 与改动前一致：94.9%）
    #   雨爱那张钢琴谱           23%    → 触发
    #   钢琴化压力输入(合成)      100%   → 触发
    # 阈值 10%；另外至少要有 4 个这样的格子，免得短曲被偶发的和弦带偏。
    # 强行 hand_split=True 时 48 首基准会掉到 94.7%（-0.2），所以必须留这道门。
    if hand_split == "auto":
        dense = sum(1 for v in grid.values()
                    if len({e["pitch"] for e in v}) > max_notes)
        hand_split = len(grid) > 0 and dense >= 4 and dense / len(grid) >= 0.10

    if not grid:
        return smc.emit(title or "未命名", tempo, time_sig, root, False, "1", [])

    total = max(grid) + 1
    # 小节线固定每 8 组一条 —— 全库 13,008 个标记无一例外都在 i%8==7，
    # 与拍号无关（3/4 的曲子也一样），这是站点解析器的规则，不是乐理拍号。
    spb = 8

    # ------------------------------------------------ 逐小节和弦（music21）
    # 老的左手是"本格最低音当根音"，钢琴谱琶音里的经过音/七音/转位会把
    # 根音带偏；这里先按小节把和声算出来，左手再按和弦音取（根音→五度→三度）。
    # 只在 lh_m21=True 时才算，其它变体的行为一字不改；music21 缺失或算炸了就
    # 静默退回老逻辑，绝不因为一个可选依赖把整条链打断。
    bar_chords = None
    if lh_m21:
        try:
            from . import chords as _chords
            _bars = (total + spb - 1) // spb
            bar_chords = {c["bar"]: c for c in
                          _chords.chords_per_bar(events, tempo, _bars,
                                                 time_sig=time_sig)}
        except Exception as exc:                      # noqa: BLE001
            import sys
            print(f"[arrange] lh_m21 关闭：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            bar_chords = None

    groups = []
    used = 0
    prev_mel = None
    prev_top = None          # 上一格旋律音的音级序号（`_fold_melody` 用）
    order = _pitch_order() if mel_fold else None
    for i in range(total):
        hint = None
        if melody is not None and i < len(melody):
            hint = melody[i]
            if hint is not None:
                try:
                    hint = float(hint)
                except (TypeError, ValueError):
                    hint = None
        picked = _pick_notes(grid.get(i, []), max_notes,
                             bass_mode=bass_mode, mel_mode=mel_mode,
                             prev_mel=prev_mel, harmony=harmony,
                             hand_split=hand_split, hand_gap=hand_gap,
                             hand_fill=hand_fill, slot=i, lh_beat=lh_beat,
                             mel_hint=hint, mel_tol=mel_tol,
                             mel_hi_only=mel_hi_only,
                             chord=(bar_chords.get(i // spb)
                                    if bar_chords is not None else None),
                             chord_conf_min=lh_conf_min, lh_synth=lh_synth)
        if picked:
            prev_mel = max(n["pitch"] for n in picked)
        # 站上的写法：同一格里**高音在前、低音在后**（如 `1D` 而非 `D1`）
        picked.sort(key=lambda n: n["pitch"], reverse=True)
        chars = []
        for n in picked:
            c, err = layout.snap(n["pitch"], root)
            if snap_limit is not None and abs(err) > snap_limit:
                continue
            if c not in chars:                     # 同一格不能按同一个键两次
                chars.append(c)
        if chars:
            # 旋律八度折叠：只改八度、不改音名，让旋律别上下乱蹦（见 _fold_melody）
            if mel_fold:
                before = chars[0]
                chars = _fold_melody(chars, prev_top, mel_fold, order)
                if chars[0] != before:
                    chars.sort(key=lambda c: layout.OFFSET_OF[c], reverse=True)
                prev_top = order.index(chars[0])
            used += len(chars)
        mark = ">" if (i % spb == spb - 1) else ""
        groups.append(smc.make_group(chars, mark))

    # 尾巴补到整小节
    while len(groups) % spb:
        groups.append(smc.make_group([], ">" if len(groups) % spb == spb - 1 else ""))

    if has8vb is None:
        import statistics
        med = statistics.median(e["pitch"] for e in events)
        has8vb = med < layout.KEY_MIDI[root]

    if difficulty is None:
        secs = max(1e-6, total * slot_dur)
        density = used / secs
        difficulty = "1" if density < 1.2 else "2" if density < 2.0 \
            else "3" if density < 3.0 else "4" if density < 4.2 else "5"

    return smc.emit(title or "未命名", tempo, time_sig, root, bool(has8vb),
                    difficulty, groups)


# ---------------------------------------------------------------- 策略登记

def _mk(**opts):
    def fn(events, meta):
        return arrange(events,
                       tempo=meta["tempo"], time_sig=meta["time_sig"],
                       key=meta.get("key"), title=meta.get("title", ""),
                       has8vb=meta.get("has8vb"), **opts)
    return fn


def _mk_mel(**opts):
    """用 `melody.hints_for` 拿外部旋律提示的变体（demucs 分离 + torchcrepe）。

    提示不存在时 `hints_for` 返回 None，arrange 内部逐格回退到老逻辑 ——
    所以这个变体在"没跑过 melody prepare"时等价于 base，不会崩，也不会悄悄改结果。
    提示的键是 (事件表指纹, tempo, phase)，所以它天然跟着测试台给的速度走。
    """
    def fn(events, meta):
        from . import melody
        tempo = meta["tempo"]
        phase = meta.get("offset", 0.0) or 0.0
        hint = melody.hints_for(events, tempo, phase)
        return arrange(events,
                       tempo=tempo, time_sig=meta["time_sig"],
                       key=meta.get("key"), title=meta.get("title", ""),
                       has8vb=meta.get("has8vb"), offset=phase,
                       melody=hint, **opts)
    return fn


def _mk_auto(**opts):
    """不给正确速度：完全靠 onsets 自己定网格（模拟真实歌曲的处境）。

    走的是与网页/CLI 完全相同的 `beats.select_grid`（含倍速纠错），
    所以这个变体的分数才是用户实际拿到的分数。`base` 是「速度已给」的实验室上限。

    `warp=True` 时再叠一层逐段对齐（`beats.fit_warp`），用来量 rubato 的收益。
    """
    want_warp = bool(opts.pop("warp", False))
    want_warp_ev = bool(opts.pop("warp_ev", False))
    warp_kw = dict(opts.pop("warp_kw", None) or {})

    def fn(events, meta):
        from . import beats
        bpm, off, _sc = beats.select_grid(events)
        kw = dict(opts)
        kw.setdefault("key", meta.get("key"))
        kw.setdefault("tempo_exact", float(bpm))   # 网格用小数速度，谱头仍写整数
        w = beats.warp_for(events, float(bpm), off, **warp_kw) if want_warp else None
        if want_warp_ev:
            from . import warptrack
            w2, _info = warptrack.autowarp(events, tempo=float(bpm), mode="auto")
            if w2 is not None:                      # 闸门放行才换，否则保持恒速
                w = w2
                kw["tempo_exact"] = float(_info.get("tempo") or bpm)
        return arrange(events,
                       tempo=max(30, int(round(bpm))), time_sig=meta["time_sig"],
                       title=meta.get("title", ""),
                       has8vb=meta.get("has8vb"), offset=off, warp=w, **kw)
    return fn


def _mk_warp(**opts):
    """`base` + 逐段对齐：速度/相位都给对（实验室上限），量 warp 会不会帮倒忙。

    干净数据上残差本来就应该接近 0，`fit_warp` 的 `min_dev` 门槛该把它挡成 identity；
    这条变体就是用来验证那道门的（48 首基准 `base` 94.8，开了 warp 不该掉）。
    """
    warp_kw = dict(opts.pop("warp_kw", None) or {})

    def fn(events, meta):
        from . import beats
        tempo = meta["tempo"]
        w = beats.warp_for(events, float(tempo), 0.0, **warp_kw)
        return arrange(events,
                       tempo=tempo, time_sig=meta["time_sig"],
                       key=meta.get("key"), title=meta.get("title", ""),
                       has8vb=meta.get("has8vb"), offset=0.0, warp=w, **opts)
    return fn


def _mk_warp_ev(**opts):
    """`base` + 帧级拍点跟踪（`warptrack.autowarp`，事件信封，不用 npz）。

    和 `_mk_warp` 的区别：`_mk_warp` 走的是老 `beats.warp_for`（逐段最小二乘），
    这条走 warptrack 的 DP 周期曲线 + 逐音相位积分，并且带 grid_quality 闸门。
    """
    mode = opts.pop("warp_mode", "auto")

    def fn(events, meta):
        from . import warptrack
        tempo = meta["tempo"]
        w, info = warptrack.autowarp(events, tempo=float(tempo), mode=mode)
        kw = {}
        if w is not None:
            # warp 的 y 轴是"第 k 个 8 分格"，格长必须由 w.slot 反推，
            # 否则 `_warp_fn` 会按 60/tempo/2 与 w.slot 的比值再缩放一次 → 整谱错位
            kw["tempo_exact"] = float(info.get("tempo") or tempo)
        return arrange(events,
                       tempo=tempo, time_sig=meta["time_sig"],
                       key=meta.get("key"), title=meta.get("title", ""),
                       has8vb=meta.get("has8vb"), offset=0.0, warp=w, **kw, **opts)
    return fn


VARIANTS = {
    "base":            _mk(),
    # ---- 旋律八度折叠：量过，降 F1，默认关（见 `_fold_melody` 的注释）----
    "fold3":           _mk(mel_fold=3.0),
    "fold4":           _mk(mel_fold=4.0),
    "fold5":           _mk(mel_fold=5.0),
    "fold6":           _mk(mel_fold=6.0),
    "base_auto":       _mk_auto(),          # ← 速度自己估
    "base_full":       _mk_auto(key=None),  # ← 速度+调性都自己判（对外该看这个）
    "mel_smooth":      _mk(mel_mode="smooth"),
    "drop_bass":       _mk(bass_mode="none"),
    "drop_mid":        _mk(bass_mode="none", mel_mode="smooth"),
    "filter_short":    _mk(drop_short=0.08),
    "snap1":           _mk(snap_limit=1),
    "snap2":           _mk(snap_limit=2),
    "combo":           _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2),
    "combo_strong":    _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           bass_mode="strong"),
    # ---- 第二轮：旋律/和声/量化 ----
    "mel_long":        _mk(mel_mode="long", drop_short=0.08, snap_limit=2),
    "harmony":         _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           harmony=True),
    "harmony_strong":  _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong"),
    "vel_filter":      _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           vel_pct=0.15),
    "quant_floor":     _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           quant="floor"),
    "key_inr":         _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           key_mode="inr"),
    "key_low":         _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           key_mode="low"),
    "all_in":          _mk(mel_mode="smooth", drop_short=0.08, snap_limit=2,
                           harmony=True, vel_pct=0.10),
    # ---- 第三轮：把历次最好的零件拼起来 ----
    "b1_long_harm_strong": _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                               harmony=True, bass_mode="strong"),
    "b2_long_strong":      _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                               bass_mode="strong"),
    "b3_long_harm":        _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                               harmony=True),
    "b4_long_harm_strong_s1": _mk(mel_mode="long", drop_short=0.08, snap_limit=1,
                                  harmony=True, bass_mode="strong"),
    "b5_long_harm_strong_ns": _mk(mel_mode="long", drop_short=0.08,
                                  harmony=True, bass_mode="strong"),
    "b6_long_harm_strong_f12": _mk(mel_mode="long", drop_short=0.12, snap_limit=2,
                                   harmony=True, bass_mode="strong"),
    # ---- 第四轮：速度未知（真实歌曲处境）----
    "auto_grid":       _mk_auto(mel_mode="long", drop_short=0.08, snap_limit=2,
                                harmony=True, bass_mode="strong"),
    "auto_plain":      _mk_auto(mel_mode="long", drop_short=0.08, snap_limit=2),
    # ---- 第五轮：钢琴谱（密集和弦 + 双手）----
    # 全部用同一套参数（mel long / drop_short .08 / snap 2 / harmony / strong），
    # 只换取音方案。三列口径：48 首基准 F1 ｜ 钢琴化压力输入 F1 ｜ 真谱《雨爱》旋律命中
    #   旧逻辑            95.0% ｜ 42.4% ｜ 88.2%
    #   并列取高音        95.0% ｜ 46.6% ｜ 95.8%   ← _pick_melody 的并列规则
    #   分手+不补         94.8% ｜ 57.3% ｜ 96.6%
    #   分手+补右手根音    94.8% ｜ 46.8% ｜ 96.6%
    #   分手+补内声部      94.7% ｜ 45.7% ｜ 96.6%   ← 现在的默认（三音格/协和度最好）
    #   ＋左手只留拍点     94.7% ｜ 44.6% ｜ 96.6%   ← 负结果
    #   ＋邻格去抖        94.7% ｜ 45.8% ｜ 95.0%   ← 负结果
    # nosplit = 只修并列取高音、不分手的路径，用来量"分左右手"的净收益
    "piano_nosplit":   _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong", hand_split=False),
    "piano_hands":     _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7),
    "piano_gap5":      _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=5),
    "piano_gap9":      _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=9),
    "piano_all_low":   _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="lowest",
                           hand_split=True, hand_gap=7),
    "piano_plain":     _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           hand_split=True, hand_gap=7),
    "piano_nofill":    _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, hand_fill="none"),
    "piano_bass":      _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, hand_fill="bass"),
    "piano_third":     _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, hand_fill="third"),
    # auto = 跟默认一样（只在密集曲子上分手），用来和 forced 对比
    "piano_auto":      _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong"),
    "piano_lh2":       _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, lh_beat=2),
    "piano_lh4":       _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, lh_beat=4),
    "piano_dejit":     _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, dejitter=0.5),
    # ---- 第六轮：外部旋律提示（melody.py: demucs + torchcrepe）----
    # 提示内容由 out/agent_melody/hints/*.json 决定，取不到就等价于 base。
    # 三列口径：48 首基准 F1 ｜ 6 首站点闭环 F1 ｜ 41/315 旋律命中
    #   见 out/agent_melody/report.md
    "mel_crepe":       _mk_mel(mel_mode="long", drop_short=0.08, snap_limit=2,
                               harmony=True, bass_mode="strong"),
    "mel_crepe_hi":    _mk_mel(mel_mode="long", drop_short=0.08, snap_limit=2,
                               harmony=True, bass_mode="strong", mel_hi_only=True),
    "mel_crepe_hands": _mk_mel(mel_mode="long", drop_short=0.08, snap_limit=2,
                               harmony=True, bass_mode="strong",
                               hand_split=True, hand_gap=7),
    # ---- 第七轮：music21 逐小节和弦配左手（chords.py）----
    # 左手不再"本格最低音当根音"，改按本小节检测到的和弦取音：
    #   根音 → 五度 → 三度，同音级去重；置信度 < lh_conf_min 时回退老 `_bass_chord`
    # 三列口径：48 首基准 F1 ｜ 钢琴化压力输入 F1 ｜ 真谱《雨爱》旋律命中
    #   见 out/agent_music21/report.md
    "lh_m21":          _mk(lh_m21=True),
    "lh_m21_synth":    _mk(lh_m21=True, lh_synth=True),
    # 与 piano_third / piano_auto 同一套参数，只把左手换成和弦配 —— 用来量净收益
    "piano_m21":       _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, lh_m21=True),
    "piano_m21_auto":  _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong", lh_m21=True),
    "piano_m21_synth": _mk(mel_mode="long", drop_short=0.08, snap_limit=2,
                           harmony=True, bass_mode="strong",
                           hand_split=True, hand_gap=7, lh_m21=True,
                           lh_synth=True),
    # ---- 第八轮：逐段自适应量化（beats.fit_warp）----
    # 默认全是**关**（arrange 的 warp=None 一字不改）。这两条只是 A/B 用：
    #   base_warp  速度/相位都给对 → 干净数据上 warp 该自动退成 identity
    #   auto_warp  速度自己估 + 逐段对齐 → 真实录音（有 rubato）该看这条
    # 数字见 out/agent_warp/report.md
    "base_warp":       _mk_warp(),
    "auto_warp":       _mk_auto(warp=True),
    "auto_warp_grid":  _mk_auto(warp=True, mel_mode="long", drop_short=0.08,
                                snap_limit=2, harmony=True, bass_mode="strong"),
    # ---- 第九轮：帧级拍点跟踪（warptrack.autowarp，不需 npz）----
    # 网页/命令行走的就是这条：速度/相位照旧估，再用 grid_quality 闸门决定要不要
    # 换成分段变化的网格。干净数据上闸门该一直关着（gq>=0.89），所以 `base_warp_ev`
    # 应该和 `base` 逐位相同；rubato 下 gq 掉到 0.43~0.61，才真正启用。
    "base_warp_ev":    _mk_warp_ev(),
    "auto_warp_ev":    _mk_auto(warp_ev=True),
}

