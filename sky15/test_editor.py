"""编辑器的往返/边界测试：python -m pytest sky15/test_editor.py -q"""
import os
import pathlib

from . import editor, smc

HERE = pathlib.Path(__file__).resolve().parent

#: 语料目录（发布副本不带语料，找不到就跳过往返用例）
CORPUS_ENV = "SKY15_CORPUS"
DEFAULT_CORPUS = HERE.parent.parent / "corpus" / "scores"


def test_sort_chars_high_first():
    assert editor.sort_chars("D1") == "1D"          # 站点写法：高音在前
    assert editor.sort_chars("1D") == "1D"
    assert editor.sort_chars("8DR") == "8RD"        # 两个八度点的 8 最高
    assert editor.sort_chars("DD") == "D"           # 去重
    assert editor.sort_chars("DRMFSLX") == "XLS"    # 只留最高的三个
    assert len(editor.sort_chars("1234567")) == 3   # 最多 3 个
    assert editor.sort_chars("") == ""
    assert editor.sort_chars("00>") == ""           # 非音符字符全丢掉


def test_full_group_pad():
    assert editor.full_group("1") == "100"
    assert editor.full_group("") == "000"
    assert editor.full_group("D1") == "1D0"


def test_bar_len_from_markers():
    gs = ["000"] * 7 + ["000>"] + ["000"] * 8
    assert editor.bar_len(gs) == 8
    gs3 = ["000"] * 5 + ["000>"]                      # 3/4 → 6 格
    assert editor.bar_len(gs3) == 6
    assert editor.bar_len(["000"] * 3) == 8           # 没有标记 → 回落 8


def test_insert_delete_shift():
    gs = ["LFD", "000", "000>", "DRM", "000", "000", "000", "000>"]
    out = editor.insert_slots(gs, 1, 1)
    assert len(out) == 9
    assert out[1] == "000" and out[3] == "000>"       # 原来的第 1 格被推后一格
    back = editor.delete_slots(out, 1, 1)
    assert back == gs
    # 删到空也要留一格
    assert editor.delete_slots(["LFD"], 0, 5) == ["000"]
    # 越界插入等价于追加到末尾
    assert len(editor.insert_slots(["000"], 99, 2)) == 3


def test_rebuild_markers():
    gs = ["000"] * 20
    out = editor.rebuild(gs, 8)
    assert [i for i, g in enumerate(out) if g.endswith(">")] == [7, 15]
    assert all(len(g) == 4 for g in out if g.endswith(">"))
    assert all(len(g) == 3 for g in out if not g.endswith(">"))


def test_set_chars_keeps_marker():
    gs = ["000"] * 7 + ["000>"]
    out = editor.set_chars(gs, 7, "D1")
    assert out[7] == "1D0>"                            # 高音在前、补 3 位、标记保留
    assert editor.set_chars(gs, 99, "D") == gs         # 越界不动
    assert editor.set_chars(["000"], 0, "D1") == ["1D0"]   # 本来没标记就不加


def test_roundtrip_text():
    """语料里每一首：拆开再拼回去，格子内容和小节线位置必须一模一样。

    语料目录可用 SKY15_CORPUS 指定（默认 ./corpus/scores）；
    发布副本不带语料，找不到就跳过，不算失败。
    """
    root = pathlib.Path(os.environ.get(CORPUS_ENV) or DEFAULT_CORPUS)
    files = sorted(root.glob("*.txt"))
    if not files:
        print(f"  · 跳过往返用例：没找到语料 {root}（可用 {CORPUS_ENV} 指定）")
        return
    bad = []
    for f in files[:40]:
        text = f.read_text(encoding="utf-8")
        meta, slots, spb = editor.from_text(text)
        again = editor.to_text(meta, slots, spb)
        m2, s2, spb2 = editor.from_text(again)
        if s2 != slots or spb2 != spb:
            bad.append(f.name)
    assert not bad, f"往返不一致: {bad[:5]}"


def test_to_text_tempo_int():
    text = editor.to_text({"title": "t", "tempo": 196.914, "time_sig": "3/4",
                           "key": "E", "has8vb": True, "difficulty": "3"},
                          ["D00"] * 6, 6)
    meta = smc.parse_meta(text)
    assert meta["tempo"] == 197
    assert meta["key_raw"] == "E(8vb)"
    assert meta["time_sig"] == "3/4"
    assert len(smc.parse_groups(text)) == 6


def test_stats():
    s = editor.stats(["1D0", "000", "LFD"], 8)
    assert s["slots"] == 3 and s["notes"] == 5 and s["filled"] == 2
    assert s["bars"] == 1
