# -*- coding: utf-8 -*-
"""给用户一句实话：这份谱值不值得照着弹。

先说清楚**没有**做的事：试过用 11 个信号线性回归预测 F1，48 首留一法
MAE 2.0~2.6 点、相关系数只有 0.36~0.70 —— 而"直接猜平均值"的 MAE 是 2.4 点，
也就是说回归等于没学到东西（F1 分布太窄，85~99，大部分方差不是这些信号能解释的）。
所以这里**不预测分数**，只做两件可靠的事：

1. 抽出可解释的诊断信号（网格命中率、空小节比例、密度、折八度比例…）；
2. 用规则给出"可疑"标记 —— 只报**我们确知会出事**的情形。

`_eval_quality.py` 会打印这套规则在 48 首上的命中情况（哪些该报的没报、哪些误报）。
"""
from __future__ import annotations

import math
import statistics

FEATURES = (
    "hit_rate", "doubled", "log_dur", "notes_per_sec", "dens_pred",
    "empty_bar", "one_note", "fold_share", "snap_err", "ioi_cv", "chord_share",
)

# 正常范围来自 48 首语料（见 out/agent_quality/report.txt 的分布）
DENS_MIN, DENS_MAX = 0.35, 1.40
HIT_MIN = 0.60
EMPTY_BAR_MAX = 0.08
FOLD_MAX = 0.15


def features(events, text=None, tempo=None, phase=0.0, doubled=0, hit_rate=None, root_key=None):
    """从转谱事件 + 排好的谱里抽诊断信号。

    `root_key` 应传排谱时实际用的调（`arrange.pick_key(events)`），
    折八度和吸附误差都相对它算，才和最终谱面一致。
    """
    from sky15 import beats, layout, smc

    on = sorted(float(e["start"]) for e in events)
    n = len(on)
    if n == 0:
        return None
    dur = max(float(e["start"]) + float(e.get("dur", 0)) for e in events) or 1.0
    ioi = [b - a for a, b in zip(on, on[1:]) if b > a]
    ioi_cv = (statistics.pstdev(ioi) / statistics.mean(ioi)) if len(ioi) > 3 and statistics.mean(ioi) else 0.0

    if tempo is None:
        tempo = beats.select_grid(events)[0]
    if hit_rate is None:
        hit_rate = beats.grid_quality([e["start"] for e in events], float(tempo), phase)
    if root_key is None:
        from sky15 import arrange
        root_key = arrange.pick_key(events) or "C"

    root = layout.KEY_MIDI[root_key]
    fold, errs = 0, []
    for p in (int(e["pitch"]) for e in events):
        rel = p - root
        if rel < 0 or rel > layout.SPAN:
            fold += 1
        errs.append(abs(layout.snap(p, root_key)[1]))

    dens = empty = one = chord = 0.0
    bars = 0
    if text:
        gs = smc.parse_groups(text)
        tot = max(1, len(gs))
        counts = [sum(1 for c in g[:3] if c not in "0<> ") for g in gs]
        dens = sum(counts) / tot
        one = sum(1 for k in counts if k == 1) / tot
        chord = sum(1 for k in counts if k >= 3) / tot
        barmax = [max(counts[i:i + 8] or [0]) for i in range(0, len(counts), 8)]
        bars = len(barmax)
        empty = sum(1 for m in barmax if m == 0) / max(1, bars)

    return {
        "hit_rate": float(hit_rate or 0.0),
        "doubled": float(bool(doubled)),
        "log_dur": math.log10(max(1.0, dur)),
        "notes_per_sec": n / dur,
        "dens_pred": dens,
        "empty_bar": empty,
        "one_note": one,
        "fold_share": fold / n,
        "snap_err": statistics.mean(errs) if errs else 0.0,
        "ioi_cv": ioi_cv,
        "chord_share": chord,
        "bars": bars,
        "notes": n,
        "dur": dur,
    }


def assess(feat):
    """返回 {"grade": 好/一般/可疑, "warnings": [...], "notes": [...], "advice": [...]}。

    只覆盖我们确知会出事的粗粒度情形（对齐崩了、空小节、密度离谱、音域折太多）。
    实测说明（48 首，见 `_eval_quality.py`）：这套规则**抓不到 86 分和 95 分的区别**
    —— 那 6 首最差的曲子网格命中 100%、密度正常、空小节 0~2%，从信号上就是"好"。
    诊断信号能发现的是"整体错了"，不是"细处不完美"。别把它当分数用。
    """
    if not feat:
        return {"grade": "可疑", "warnings": ["没有识别出任何音符"],
                "notes": [], "advice": ["换更清楚的录音或谱面图片"]}
    warns, notes, advice = [], [], []

    if feat["doubled"]:
        notes.append("演奏速度是标注速度的一半或两倍，已自动纠正")
        advice.append("如果整首听起来快一倍或慢一倍，请手填 BPM 重算")
    if feat["hit_rate"] < HIT_MIN:
        warns.append(f"节拍网格命中率只有 {feat['hit_rate']*100:.0f}%")
        advice.append("音频速度和自动估计的差得远，谱面会整体错位 —— 请把谱子标注的 BPM 手填进来重算")
    if feat["empty_bar"] > EMPTY_BAR_MAX:
        warns.append(f"有 {feat['empty_bar']*100:.0f}% 的小节是空的")
        advice.append("可能是漏音，或速度估偏导致音落到别的格子")
    if feat["dens_pred"] > DENS_MAX:
        warns.append(f"每格 {feat['dens_pred']:.2f} 个音，比常见编配密得多")
        advice.append("可能把泛音/余响当成了音，可以试试「少弹一点」或换更清晰的音源")
    if 0 < feat["dens_pred"] < DENS_MIN:
        warns.append(f"每格只有 {feat['dens_pred']:.2f} 个音，比常见编配稀")
        advice.append("可能漏音，试试「多弹一点」或提高录音音量")
    if feat["fold_share"] > FOLD_MAX:
        warns.append(f"{feat['fold_share']*100:.0f}% 的音超出 15 键音域，被折八度塞进来")
        advice.append("原曲音域太宽，超出部分只能折八度，听感会跳")

    grade = "可疑" if len(warns) >= 2 else ("一般" if warns else "好")
    return {"grade": grade, "warnings": warns, "notes": notes, "advice": advice}
