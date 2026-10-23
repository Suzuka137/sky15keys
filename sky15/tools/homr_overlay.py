"""用 homr 识谱，并额外出一张「它认到了哪些音」的干净叠图。

homr 自带 `--debug` 会把每个音标成 `note12` 这种文字，挤满整页，人根本看不懂。
这个脚本在调 homr 之前把画法换掉：只画音符头的小圈，全部用绿色，不写文字、
不画符干/符尾。产出的 `*_notes.png` 就是「绿点叠在原图上」，一眼能看出哪里漏了。

用法（必须用 homr 所在 venv 的 python 跑）：
    python homr_overlay.py <图片> [--outdir DIR]

它会：
  1. 在图片旁边生成 `<名字>.musicxml`（homr 的原有行为，不接管）
  2. 生成 `<名字>_debug_<n>_notes.png`——纯绿圈叠图
退出码 0 表示成功。
"""
import os
import sys


def _patch():
    import cv2

    from homr import debug as hdbg
    from homr.model import Note

    def draw_only_ring(self, img, color=(0, 200, 0)):
        cx, cy = int(self.box.center[0]), int(self.box.center[1])
        cv2.circle(img, (cx, cy), 11, color, 2, cv2.LINE_AA)

    Note.draw_onto_image = draw_only_ring
    # teaser 用轮换色，统一成一种绿
    hdbg.Debug.colors = [(0, 200, 0)]


def main(argv):
    if len(argv) < 2:
        print("usage: homr_overlay.py <image>", file=sys.stderr)
        return 2
    _patch()
    import homr.main as hmain

    img = argv[1]
    sys.argv = ["homr", img, "--debug"]
    hmain.main()
    base = os.path.splitext(img)[0]
    got = [f for f in os.listdir(os.path.dirname(base) or ".")
           if f.startswith(os.path.basename(base) + "_debug_") and "notes" in f]
    for f in got:
        print("overlay:", os.path.join(os.path.dirname(base) or ".", f))
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
