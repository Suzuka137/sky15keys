# -*- coding: utf-8 -*-
"""格子粗细：8 分格不够用时翻倍到 16 分格。

光遇 15 键谱一个格子 = 一个八分音符（8 格 = 一小节，见 smc.py）。可有些曲子
（2/4 拍的快速跑句、16 分音符连奏）最短音值只有八分的一半，硬塞进 8 分格会
把两个甚至三个音压进同一格，只能留一个，旋律就断了。

解决：把格子翻倍（速度 ×2，格长变一半），8 格仍然是"一小节"，只是这一小节
变成 2/4 的一小节。文件格式不用改，站点照常播——它只认"一格多长"。

判定用**最常见的短音值**，不用最短音值：装饰音、倚音会给出 75 ms 这种孤点，
拿它当依据会把本来干净的 4/4 谱也翻倍（雨爱就是这样，翻倍后反而变差）。
"""
from collections import Counter

MIN_SHARE = 0.40        # 这个音值至少要占到 40% 的相邻间隔，才算"真有这么细的音"
                        # 0.15 太松：雨爱只有 25%（几个经过音），翻倍后虽然指标涨，可
                        # 那一版用户已经听过并认可了，不该悄悄变样。一梦繁唐是 74%。
FINE_RATIO = 0.80       # 比格长的 0.8 倍还短，就认定 8 分格装不下


def _modal_short(onsets, slot):
    """返回 (最常见的短音值, 它的占比)。只看比格长更短的间隔。"""
    ons = sorted({round(t, 4) for t in onsets})
    gaps = [round(b - a, 4) for a, b in zip(ons, ons[1:]) if b - a > 0.02]
    short = [g for g in gaps if g < slot * FINE_RATIO]
    if not short:
        return None, 0.0
    # 按 5 ms 归桶再取众数，避免浮点抖动把同类间隔拆成两堆
    bucket = Counter(int(round(g * 200)) / 200 for g in short)
    val, n = bucket.most_common(1)[0]
    return val, n / len(gaps)


def _loss(events, slot):
    """格子这么粗的时候，有多少音根本放不下。

    两种丢法分开看：
      · 同一时刻 >3 个音 —— 乐器只有 3 个键，这是硬限制，跟格子粗细无关；
      · 同一格里挤了**不同时刻**的起音 —— 一格是一个瞬间，后面的起音全没了，
        这才是"格子太粗"造成的损失。
    这里只数后一种：每格只留人数最多的那个时刻（最多 3 个音），别的都算丢。
    """
    per = {}
    for e in events:
        idx = int(round(e["start"] / slot))
        per.setdefault(idx, {}).setdefault(round(e["start"], 3), []).append(e["pitch"])
    kept = 0
    for onsets in per.values():
        main = max(onsets.values(), key=len)
        kept += min(3, len(main))
    return 1.0 - kept / max(1, len(events))


def choose_factor(events, tempo, max_factor=2, min_gain=0.20):
    """该用几倍密度的格子。

    比较八分格和十六分格各自会丢多少音，只有当「翻倍能救回 ≥20 个百分点的音」时才翻。
    为什么不是「丢得多了就翻」：丢音有两个来源，一个是一格里挤了不同时刻的起音
    （格子太粗，能救），一个是同一时刻超过 3 个音（乐器只有 3 个键，救不了）。
    实测《雨爱》八分格丢 31%，但十六分格还剩 20%——那 20% 是和弦太厚，只能救 11~15 点，
    而用户听过八分格那版并认可了，不该悄悄变样；《一梦繁唐》八分格丢 38%、十六分格
    只剩 4%，救回 33 点，那种才是真该翻。门槛定 20 点，是为了让雨爱离边界有 5 点余量
    （两次 OMR 分别测出 11.0 / 15.2），不至于同一份谱跑两遍结论不同。

    返回 (factor, info)。
    """
    onsets = [e["start"] for e in events]
    if len(onsets) < 8:
        return 1, {"why": "音太少，不判", "loss8": 0.0, "loss16": 0.0}

    slot = 60.0 / float(tempo) / 2.0
    l1 = _loss(events, slot)
    l2 = _loss(events, slot / 2.0)
    info = {"loss8": round(l1, 3), "loss16": round(l2, 3),
            "gain": round(l1 - l2, 3), "slot": round(slot, 4)}

    if max_factor >= 2 and (l1 - l2) >= min_gain:
        factor = 2
        info["why"] = (
            f"八分格会把 {l1:.0%} 的音挤掉（同一格里挤了不同时刻的起音），"
            f"十六分格只丢 {l2:.0%}，能救回 {(l1 - l2):.0%}"
        )
    else:
        factor = 1
        if l1 - l2 < min_gain:
            info["why"] = (
                f"八分格丢 {l1:.0%}，翻倍只救回 {(l1 - l2):.0%}（剩下的是和弦超过三个键），不划算"
            )
        else:
            info["why"] = f"八分格丢 {l1:.0%}，翻倍只降到 {l2:.0%}，没必要"

    factor = max(1, min(int(max_factor), int(factor)))
    info["factor"] = factor
    return factor, info


def apply_factor(tempo, factor, time_sig):
    """把速度和拍号换算成翻倍格子之后的值。"""
    if factor <= 1:
        return tempo, time_sig
    # 8 格 = 2 拍，正好是一小节 2/4
    return tempo * factor, "2/4"
