# -*- coding: utf-8 -*-
"""闭环验证：已知答案的曲子 → 渲染音频 → 转谱 → 重排 → 与原谱对比。

跑的是一整条链路，能量出"模型 + 重排"合起来到底丢了多少。
"""
import os
import sys, pathlib, statistics, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sky15 import arrange, layout, smc

ROOT = pathlib.Path(os.environ.get("SKY15_DATA", ""))


def load_original(sid):
    text = (ROOT / "exports" / "smc" / f"{sid}.smc").read_text(encoding="utf-8")
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    slot = 60.0 / meta["tempo"] / 2.0
    keys = []                                  # [(slotIndex, char)]
    for i, g in enumerate(groups):
        for c in g[:3]:
            if c in "0<> ":
                continue
            keys.append((i, c))
    return meta, groups, keys, slot


def compare(orig_keys, new_text, slot):
    meta2 = smc.parse_meta(new_text)
    groups2 = smc.parse_groups(new_text)
    new_keys = []
    for i, g in enumerate(groups2):
        for c in g[:3]:
            if c in "0<> ":
                continue
            new_keys.append((i, c))

    A, B = set(orig_keys), set(new_keys)
    inter = len(A & B)
    prec = inter / len(B) * 100 if B else 0
    rec = inter / len(A) * 100 if A else 0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
    return {"原音数": len(A), "新音数": len(B), "命中": inter,
            "准确率": round(prec, 1), "召回率": round(rec, 1), "F1": round(f1, 1),
            "新调": meta2["key"]}


def main(sid="2", wav=None):
    if not os.environ.get("SKY15_DATA"):
        print("跳过：这个测试需要自建语料，设置 SKY15_DATA 指向目录后再跑")
        return 0

    wav = wav or f"sky15/out/{sid}.wav"
    print(f"=== 曲目 {sid} 闭环验证")
    meta, groups, orig_keys, slot = load_original(sid)
    print(f"原谱：{meta['title']}  {meta['tempo']}BPM  调 {meta['key']}{'(8vb)' if meta['has8vb'] else ''}  "
          f"{len(orig_keys)} 个音")

    from sky15 import transcribe
    t0 = time.time()
    ev, _bpm, backend = transcribe.transcribe(wav)
    print(f"转谱：后端 {backend}，{len(ev)} 个音符，{time.time()-t0:.1f}s")
    if ev:
        print(f"      音高 {min(e['pitch'] for e in ev)} ~ {max(e['pitch'] for e in ev)}，"
              f"中位时值 {statistics.median(e['dur'] for e in ev):.3f}s")

    out = arrange.arrange(ev, tempo=meta["tempo"], time_sig=meta["time_sig"],
                          key=meta["key"], title=meta["title"])
    res = compare(orig_keys, out, slot)
    print("\n对比（按调性强制对齐后）：")
    for k, v in res.items():
        print(f"  {k:6} {v}")

    p = pathlib.Path(f"sky15/out/{sid}_ai.smc")
    p.write_text(out, encoding="utf-8")
    print(f"\n已写出 {p}")

    # 以前这里到这就结束了：不返回、不断言、__main__ 也不 sys.exit，
    # 于是 accept.py 里那条「闭环自测」永远 PASS —— 模型退化了也照样绿。
    # 实测 F1 99.7（雨爱，transkun），红线定 90。
    MIN_F1 = 90.0
    f1 = float(res["F1"])
    assert f1 >= MIN_F1, (
        f"闭环 F1 只有 {f1}（红线 {MIN_F1}）—— 转谱或重排退化了。"
        f"原音 {res['原音数']} / 新音 {res['新音数']} / 命中 {res['命中']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["2"])))
