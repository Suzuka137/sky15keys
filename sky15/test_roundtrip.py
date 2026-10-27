# -*- coding: utf-8 -*-
"""回环自测：把站上真实的一批 SMC 还原成音符，再让重排器重新出谱，逐组比对。

这一关过了，说明"重排 + 出谱"这一段是准的 —— 换成真实音频只是把音符来源换掉。
"""
import sys, os, pathlib, collections

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sky15 import layout, smc, arrange

CORPUS_ENV = "SKY15_SMC_CORPUS"
DEFAULT_CORPUS = pathlib.Path(__file__).resolve().parent.parent / "corpus" / "smc"


def smc_to_events(text):
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    slot = 60.0 / meta["tempo"] / 2.0
    ev = []
    for i, g in enumerate(groups):
        for c in g[:3]:
            if c == "0":
                continue
            ev.append({"start": i * slot, "dur": slot,
                       "pitch": layout.char_pitch(c, meta["key"]), "vel": 90})
    return meta, groups, ev


def main(root=None):
    if not os.environ.get("SKY15_TRUTH"):
        print("跳过：这个测试需要自建语料，设置 SKY15_TRUTH 指向目录后再跑")
        return 0
    root = root or os.environ["SKY15_TRUTH"]

    # 语料默认和工程同级（corpus/smc）。别写死本机绝对路径：
    # 换台机器或换个盘符就找不到，而找不到时以前是静默 return 0 ——
    # accept 那边靠 "81" 这个词就能通过，等于这道闸门从没验过。
    root = root or os.environ.get(CORPUS_ENV) or str(DEFAULT_CORPUS)
    src = pathlib.Path(root)
    files = sorted(src.glob("*.smc"))
    if not files:
        print("没找到 smc 文件:", root)
        print("（可用 SKY15_SMC_CORPUS 指到放了 .smc 的目录）")
        return 2
    exact = 0
    note_match = 0
    worst = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        meta, groups, ev = smc_to_events(text)

        out = arrange.arrange(
            ev, tempo=meta["tempo"], time_sig=meta["time_sig"],
            key=meta["key"], title=meta["title"],
            has8vb=meta["has8vb"], difficulty=meta["difficulty"])

        new_groups = smc.parse_groups(out)

        # 去掉尾部空组再比（原谱末尾常有长段静音，重排器会裁掉）
        def trim(gs):
            gs = list(gs)
            while gs and gs[-1][:3] == "000":
                gs.pop()
            return gs

        a_raw, b_raw = trim(groups), trim(new_groups)
        mk = lambda g: g[3] if len(g) > 3 else " "
        # `0` 只是占位，位置随作者手写习惯（全库 56% 补在末尾、21% 补在前面、中间从不出现）
        # 所以语义比对只看"这一格按了哪几个键" + 小节线位置（`>`/`<` 都算小节线）
        a = [tuple(sorted(c for c in g[:3] if c != "0")) for g in a_raw]
        b = [tuple(sorted(c for c in g[:3] if c != "0")) for g in b_raw]
        mark_a = [1 if mk(g) in "><" else 0 for g in a_raw]
        mark_b = [1 if mk(g) in "><" else 0 for g in b_raw]

        if a == b and mark_a == mark_b and list(a_raw) == list(b_raw):
            exact += 1
        elif a == b and mark_a == mark_b:
            note_match += 1                       # 音符与小节线全对，只是 0 的补位/`<`用法不同
        else:
            same = sum(1 for x, y in zip(a, b) if x == y)
            ratio = same / max(len(a), len(b), 1)
            worst.append((f.name, round(ratio * 100, 2), len(a), len(b)))

    print(f"共 {len(files)} 首")
    print(f"  逐组完全一致 : {exact}")
    print(f"  仅小节线/尾部差异 : {note_match}")
    print(f"  音符不一致 : {len(worst)}")
    for w in sorted(worst, key=lambda x: x[1])[:10]:
        print(f"    {w[0]:10} 一致率 {w[1]}%  原 {w[2]} 组 / 新 {w[3]} 组")
    # 以前这里没有返回值，退出码永远是 0 —— 音符真错了也是绿的。
    return 0 if not worst else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
