# -*- coding: utf-8 -*-
"""omr.py 的单元测试（不需要 Audiveris，纯图像 + MusicXML 合并）。

跑法：`cd sky15; python test_omr.py`

覆盖：
  1. 五线间距估计（含"谱表间距 >> 行内间距"时不跳档）
  2. 倾斜角估计 + 反旋能真的把谱线摆平
  3. 整页横放（90°）检测
  4. 手机拍照透视：合成一张透视变形的谱，校正后 interline 必须回来
  5. 干净扫描件**不该**被判成需要透视校正（否则会莫名其妙裁剪）
  6. 放大倍数：按 interline 定目标 + 16M 像素上限
  7. 多页 MusicXML 合并：小节连续、divisions 不同要重缩放
  8. 失败建议：日志特征 → 可操作建议
"""
import os
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np
import cv2

from sky15 import omr

HERE = pathlib.Path(__file__).parent
IN = pathlib.Path(os.environ.get("SKY15_TEST_IMG", ""))
JPG = IN / "雨爱_五线谱.jpg"
DICH = IN / "Dichterliebe01-1.png"
ROT = IN / "SchbAvMaSample_rotated-1.png"


def _staff_image(width=1200, height=1600, interline=14, n_systems=8,
                 angle=0.0, noise=0):
    """合成一张"五线谱"：每组 5 条线 + 一些符头（没有符头 Audiveris 也不认，
    但对测角度/间距足够）。返回 uint8 灰度图。"""
    img = np.full((height, width), 255, np.uint8)
    sys_h = interline * 4
    gap = interline * 3
    y = interline * 3
    for s in range(n_systems):
        for k in range(5):
            yy = int(y + k * interline)
            if yy < height:
                cv2.line(img, (int(width * 0.08), yy),
                         (int(width * 0.92), yy), 0, max(1, interline // 6))
        # 一点符头，免得整张图只有横线
        for j in range(10):
            cx = int(width * 0.12 + j * (width * 0.075))
            cy = int(y + (j % 5) * interline)
            cv2.circle(img, (cx, cy), max(2, interline // 3), 0, -1)
        y += sys_h + gap
    if noise:
        rng = np.random.default_rng(7)
        m = rng.random((height, width)) < (noise / 1e5)
        img[m] = 0
    if angle:
        img = omr._rotate(img, angle)
    return img


def t_interline_basic():
    g = _staff_image(interline=14)
    got = omr.estimate_interline(g)
    assert got is not None and abs(got - 14) <= 1.5, got


def t_interline_does_not_jump():
    """行内间距 vs 谱表间距：谱表间距出现得更多时也不能返回那个。"""
    img = np.full((2000, 1200), 255, np.uint8)
    # 只画"每组两条相距 40px 的线"，组内间距 10px —— 造一个 40 更多的局面
    ys = []
    y = 100
    for _ in range(12):
        ys += [(y, y + 10)]
        y += 40
    for a, b in ys:
        cv2.line(img, (100, a), (1100, a), 0, 2)
        cv2.line(img, (100, b), (1100, b), 0, 2)
    got = omr.estimate_interline(img)
    assert got is not None and abs(got - 10) <= 2.5, got


def t_skew_and_deskew():
    g = _staff_image(interline=14)
    for truth in (2.0, -3.5):
        r = omr._rotate(g, truth)
        est = omr.estimate_skew(r)
        # estimate_skew 返回的是**校正角**：按它转一次就摆平
        assert abs(est + truth) <= 0.6, (truth, est)
        fixed = omr._rotate(r, est)
        got = omr.estimate_interline(fixed)
        assert got is not None and abs(got - 14) <= 1.5, (truth, got)


def t_rotation_90():
    g = _staff_image(interline=14)
    assert omr.detect_rotation(g) == 0
    rot = cv2.rotate(g, cv2.ROTATE_90_CLOCKWISE)
    assert omr.detect_rotation(rot) == 90


def t_perspective_fix():
    """合成"手机拍照"：把谱子贴进一张纸，再做四点透视。校正后 interline 回来。"""
    g = _staff_image(interline=14, width=900, height=1200)
    # 白底 + 缩小贴到中间，模拟纸在画面里、四周有桌面
    canvas = np.full((1400, 1100), 210, np.uint8)
    small = cv2.resize(g, (700, 950))
    canvas[180:180 + 950, 200:200 + 700] = small
    h, w = canvas.shape
    src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    # 四角往里收 + 不对称 = 透视
    dst = np.float32([[90, 40], [w - 30, 95], [w - 70, h - 55], [40, h - 25]])
    M = cv2.getPerspectiveTransform(src, dst)
    photo = cv2.warpPerspective(canvas, M, (w, h), borderValue=215)

    quad = omr.find_page_quad(photo)
    assert quad is not None, "合成透视图标定不到纸边"
    fixed = omr.warp_page(photo, quad)
    il = omr.estimate_interline(fixed)
    assert il is not None, "校正后仍量不出五线间距"
    assert abs(il - 14) <= 4.0, il


def t_clean_scan_no_warp():
    """干净扫描件不该被判成"要透视校正"（否则会裁掉边上的谱号）。"""
    g = _staff_image(interline=14, width=1400, height=1800)
    assert omr.find_page_quad(g) is None
    if JPG.exists():
        real = omr.load_gray(JPG)
        assert omr.find_page_quad(real) is None, "真实干净扫描件被误判要透视校正"


def t_pickup_scale_plan():
    # 短边小 → 放大到 interline 目标
    f, _ = omr.plan_scale(1439, 2249, interline=12.14)
    assert f == 2.0, f
    # 已经够大 → 不放大
    f, _ = omr.plan_scale(2549, 3299, interline=18.75)
    assert f == 1.0, f
    # 长图 + 强制 4× 会超 16M → 必须压回预算内
    f, note = omr.plan_scale(1080, 5824, interline=6, upscale=4)
    assert (1080 * f) * (5824 * f) <= omr.MAX_PIXELS, (f, note)
    # 强制 1× 但本身已超预算：给出提示而不是硬跑
    f, note = omr.plan_scale(4000, 5000, interline=20, upscale=1)
    assert f == 1.0 and note, (f, note)


def t_denoise_preserves_staff():
    """中值去噪不能把谱线抹掉（2x2 开运算就会）。"""
    g = _staff_image(interline=6)          # 线细
    before = omr.estimate_interline(g)
    after = omr.estimate_interline(omr._denoise(g))
    assert before is not None and after is not None, (before, after)
    assert abs(after - before) <= 1.0, (before, after)


def t_merge_two_pages():
    p1 = """<?xml version="1.0"?><score-partwise version="3.1">
<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
<part id="P1">
<measure number="1"><attributes><divisions>4</divisions>
<time><beats>4</beats><beat-type>4</beat-type></time></attributes>
<note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration></note></measure>
<measure number="2"><note><pitch><step>D</step><octave>4</octave></pitch><duration>16</duration></note></measure>
</part></score-partwise>"""
    p2 = """<?xml version="1.0"?><score-partwise version="3.1">
<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
<part id="P1">
<measure number="1"><attributes><divisions>8</divisions>
<time><beats>4</beats><beat-type>4</beat-type></time></attributes>
<note><pitch><step>E</step><octave>4</octave></pitch><duration>32</duration></note></measure>
<measure number="2"><note><pitch><step>F</step><octave>4</octave></pitch><duration>32</duration></note></measure>
</part></score-partwise>"""
    d = pathlib.Path(tempfile.mkdtemp(prefix="sky15_omrt_"))
    a, b = d / "p1.musicxml", d / "p2.musicxml"
    a.write_text(p1, encoding="utf-8")
    b.write_text(p2, encoding="utf-8")
    out = omr.merge_musicxml([a, b], d / "merged.musicxml")

    from sky15 import musicxml
    got = musicxml.parse_full(out)
    ms = got.parts[0]["measures"]
    assert len(ms) == 4, [len(ms), [m["number"] for m in ms]]
    assert [m["number"] for m in ms] == ["1", "2", "3", "4"], [m["number"] for m in ms]
    # divisions 4 与 8 统一后，每小节都必须是 4 拍
    for i, m in enumerate(ms):
        assert abs(m["length"] - 4.0) < 1e-6, (i, m["length"])
    assert [e["pitch"] for e in got.events] == [60, 62, 64, 65], \
        [e["pitch"] for e in got.events]
    assert [e["start"] for e in got.events] == [0.0, 4.0, 8.0, 12.0], \
        [e["start"] for e in got.events]


def t_merge_single_and_empty():
    d = pathlib.Path(tempfile.mkdtemp(prefix="sky15_omrt_"))
    a = d / "only.musicxml"
    a.write_text('<?xml version="1.0"?><score-partwise version="3.1"><part-list>'
                 '<score-part id="P1"><part-name>x</part-name></score-part>'
                 '</part-list><part id="P1"><measure number="1"><note><rest/>'
                 '<duration>4</duration></note></measure></part></score-partwise>',
                 encoding="utf-8")
    assert omr.merge_musicxml([a]) == a        # 只有一页：原样返回，不重写
    try:
        omr.merge_musicxml([])
        raise AssertionError("空列表应该抛 RuntimeError")
    except RuntimeError:
        pass


def t_diagnose():
    assert any("堆" in a or "Xmx" in a for a in
               omr.diagnose("java.lang.OutOfMemoryError: Java heap space"))
    assert any("16M" in a for a in omr.diagnose("Exit forced. Failure"))
    assert any("谱表" in a for a in omr.diagnose("No sheet found in image"))
    assert any("溢出" in a for a in omr.diagnose("Measure{#9} Voice{3} excess:3/16 too long"))
    assert omr.diagnose("everything is fine", 0) == []
    assert omr.diagnose("weird", 3)             # 未知失败也得给一句话


def _TESTS():
    return [("五线间距", t_interline_basic),
            ("间距不跳档", t_interline_does_not_jump),
            ("倾斜检测+纠正", t_skew_and_deskew),
            ("整页横放 90°", t_rotation_90),
            ("透视校正", t_perspective_fix),
            ("干净件不误裁", t_clean_scan_no_warp),
            ("放大倍数规划", t_pickup_scale_plan),
            ("去噪不伤谱线", t_denoise_preserves_staff),
            ("两页合并", t_merge_two_pages),
            ("单页/空合并", t_merge_single_and_empty),
            ("失败建议", t_diagnose)]


def test_all():
    for _, fn in _TESTS():
        fn()


def main():
    tests = _TESTS()
    ok = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL  {name}  -> {e}")
        except Exception as e:
            print(f"  ERR   {name}  -> {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(tests)} 通过")
    return ok == len(tests)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
