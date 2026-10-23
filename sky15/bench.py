# -*- coding: utf-8 -*-
"""基准测试台：把"音频 → 谱"的每一环单独量出来，好逐项优化。

思路
----
转谱结果会被缓存成 JSON，之后改重排策略就不用再跑模型了 —— 一轮对比只要几秒。

用法
----
    python -m sky15.bench prepare 2 37 321        # 渲染音频 + 转谱，落缓存
    python -m sky15.bench eval                    # 对缓存里的所有曲子跑全部策略
    python -m sky15.bench eval --variants base    # 只跑某个策略
"""
import os
import json, pathlib, sys, time

ROOT = pathlib.Path(os.environ.get("SKY15_DATA", ""))
OUT = pathlib.Path(__file__).resolve().parent / "out"
CACHE = OUT / "trans"
CACHE.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------- 真值
def load_truth(sid):
    """真值 = 站上原谱的音符集合。

    注意：站点的 parseCustomFormat 在最后一步会**裁掉首尾的空白组**，
    导出的 MIDI 也一样（所以音频里没有开头那段静音）。
    这里必须跟着裁，否则真值和音频会整体错位 —— 曾经就因为一处 28 组的前导空白
    把某首的 F1 从 95% 拉到 20%。
    """
    from sky15 import smc, layout
    text = (ROOT / "exports" / "smc" / f"{sid}.smc").read_text(encoding="utf-8")
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)

    # 与站点一致：首尾空白组裁掉
    while groups and groups[0][:3] == "000":
        groups.pop(0)
    while groups and groups[-1][:3] == "000":
        groups.pop()

    keys = []
    for i, g in enumerate(groups):
        for c in g[:3]:
            if c in "0<> ":
                continue
            keys.append((i, c))
    return meta, groups, keys


def score(truth_keys, text):
    """(slot,char) 集合的 P/R/F1"""
    from sky15 import smc
    groups = smc.parse_groups(text)
    pred = []
    for i, g in enumerate(groups):
        for c in g[:3]:
            if c in "0<> ":
                continue
            pred.append((i, c))
    A, B = set(truth_keys), set(pred)
    inter = len(A & B)
    p = inter / len(B) * 100 if B else 0.0
    r = inter / len(A) * 100 if A else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


# ---------------------------------------------------------------- 预处理
def render_and_transcribe(sid, force=False):
    from sky15 import transcribe
    from sky15.tools.render_midi import render

    cache = CACHE / f"{sid}.json"
    if cache.exists() and not force:
        print(f"  {sid}: 已缓存")
        return json.loads(cache.read_text(encoding="utf-8"))

    wav = OUT / f"{sid}.wav"
    if not wav.exists():
        render(str(ROOT / "exports" / "midi" / f"{sid}.mid"), str(wav))
    t0 = time.time()
    ev, _bpm, backend = transcribe.transcribe(str(wav))
    dt = time.time() - t0
    meta, _g, _k = load_truth(sid)
    data = {"sid": sid, "backend": backend, "sec": round(dt, 1),
            "events": ev,
            "truth": {"tempo": meta["tempo"], "time_sig": meta["time_sig"],
                      "key": meta["key"], "has8vb": meta["has8vb"],
                      "title": meta["title"]}}
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"  {sid}: {len(ev)} 音 / {dt:.1f}s / {backend}")
    return data


def load_caches():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CACHE.glob("*.json"))]


# ---------------------------------------------------------------- 评测
def eval_variants(variant_names=None, verbose=True):
    from sky15 import arrange

    VARIANTS = arrange.VARIANTS
    if variant_names:
        VARIANTS = {k: v for k, v in VARIANTS.items() if k in variant_names}
    caches = load_caches()
    if not caches:
        print("没有缓存，先跑 prepare")
        return {}

    agg = {name: {"p": 0.0, "r": 0.0, "f": 0.0, "n": 0} for name in VARIANTS}
    detail = {name: [] for name in VARIANTS}

    for c in caches:
        sid = c["sid"]
        meta, _groups, truth_keys = load_truth(sid)
        for name, fn in VARIANTS.items():
            out = fn(c["events"], meta)
            p, r, f = score(truth_keys, out)
            agg[name]["p"] += p
            agg[name]["r"] += r
            agg[name]["f"] += f
            agg[name]["n"] += 1
            detail[name].append((sid, f))

    print(f"\n{'策略':22} {'准确率':>8} {'召回率':>8} {'F1':>8}")
    print("-" * 52)
    rows = []
    for name, a in agg.items():
        n = a["n"] or 1
        rows.append((a["f"] / n, name, a["p"] / n, a["r"] / n))
    for f, name, p, r in sorted(rows, reverse=True):
        print(f"{name:22} {p:7.1f}% {r:7.1f}% {f:7.1f}%")
    if verbose:
        best = sorted(rows, reverse=True)[0][1]
        print(f"\n明细（{best}）：")
        for sid, f in sorted(detail[best], key=lambda x: x[1])[:8]:
            print(f"  {sid:>5}  F1 {f:5.1f}%")
    return {name: agg[name]["f"] / (agg[name]["n"] or 1) for name in agg}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if cmd == "prepare":
        sids = sys.argv[2:] or ["2"]
        for s in sids:
            render_and_transcribe(s)
    else:
        names = None
        if "--variants" in sys.argv:
            i = sys.argv.index("--variants")
            names = sys.argv[i + 1].split(",")
        eval_variants(names)
