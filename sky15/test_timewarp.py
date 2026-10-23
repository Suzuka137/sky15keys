# -*- coding: utf-8 -*-
"""分段速度（变速谱）的格子映射：`score_semantics` 的反函数 + `arrange` 挂 warp。

背景：谱子里写了中途变速时，`to_seconds()` 出来的秒是按分段速度积分过的，
可出谱的格子只有一个固定 tempo。不挂 warp，变速段的音会被抖到格子外
（实测 60 拍的一段能撑出 22 小节，正确是 15~16 小节）。

跑法：`python sky15\test_timewarp.py`
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sky15 import arrange, score_semantics as ss, smc  # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAIL.append(name)


def main():
    tm = ss.normalize_tempo_map([(0.0, 100.0), (20.0, 60.0)])

    # 1. 秒 ↔ 拍 往返
    ok = True
    for b in (0.0, 3.5, 20.0, 27.0, 60.0):
        if abs(ss.beat_at_seconds(ss.seconds_at(b, tm), tm) - b) > 1e-9:
            ok = False
    ck("秒↔拍 往返一致", ok)

    # 2. 变速段里一格仍是半拍：60 BPM 处半拍 = 0.5 秒
    s20 = ss.seconds_at(20.0, tm)
    ck("60 BPM 段一格 0.500s", abs((ss.seconds_at(20.5, tm) - s20) - 0.5) < 1e-9,
       f"{ss.seconds_at(20.5, tm) - s20:.4f}")

    # 3. seconds_to_slots：拍 20.5 必须正好是第 41 格
    ck("拍 20.5 = 第 41 格", abs(ss.seconds_to_slots(s20 + 0.5, tm) - 41.0) < 1e-9,
       f"{ss.seconds_to_slots(s20 + 0.5, tm):.4f}")

    # 4. arrange 挂 warp：64 个连续八分音符必须落成 64 个连续格，不多出空格
    ev_beats = [{"start": i * 0.5, "dur": 0.5, "pitch": 60 + (i % 5) * 2, "vel": 80}
                for i in range(64)]
    secs = ss.beats_to_seconds(ev_beats, tm)
    warp = lambda t: ss.seconds_to_slots(t, tm)          # noqa: E731
    text = arrange.arrange(secs, tempo=100, time_sig="4/4", key="C", title="t", warp=warp)
    pg = smc.parse_groups(text)
    filled = [i for i, g in enumerate(pg) if any(c not in "0<> " for c in g[:3])]
    gaps = {b - a for a, b in zip(filled, filled[1:])}
    ck("挂 warp 后组数=64", len(pg) == 64, f"{len(pg)}")
    ck("挂 warp 后无空格", gaps == {1}, f"间隔 {sorted(gaps)}")

    # 5. 不挂 warp 必须更差（否则这条测试没有意义）
    text2 = arrange.arrange(secs, tempo=100, time_sig="4/4", key="C", title="t")
    pg2 = smc.parse_groups(text2)
    ck("不挂 warp 会多出格子", len(pg2) > len(pg) + 10, f"{len(pg2)} vs {len(pg)}")

    print(f"\n共 {5} 项，失败 {len(FAIL)} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
