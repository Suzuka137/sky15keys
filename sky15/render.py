# -*- coding: utf-8 -*-
"""SMC 谱 → 图片（简谱样式） / PDF。

15 键谱最常见的用法不是拿代码看，是**存到手机里照着弹**、或者打印出来。

排版用 15 键谱常见的固定网格：
  · **一行 = 4 小节 = 32 格**（一小节 8 个八分音符）。
  · **一小节固定 8 列，空拍照样占位** —— 列宽 = content_w / 32，全页统一，
    所以小节线天然等距、每行右边界天然对齐，不需要 justify。
  · 尾行不足 4 小节也按 4 小节排：空小节画成带小节线的空白小节，
    整行 5 条竖线齐全。
  · 行首标 `[小节号]`（行号 × 4 + 1，1 起），贴在行首小节线正上方。
  · 头部：标题居中；左上 `调性:` / `版本:`，右上 `作者/艺术家:` / `改编:`。
  · 每 4 行之间一条橙色横线分段（段间多留一点空，线才不会贴住音符）。
  · 同一格最多 3 个音，竖排、高音在上；八度点贴着数字画。
  · 深色底白字，观感和站点一致；也能切浅色。

用法：
    python -m sky15 render song.smc -o song.png
    python -m sky15 render song.smc -o song.pdf --theme light --dpi 200
"""
import pathlib, re

from PIL import Image, ImageDraw, ImageFont

from . import smc

DARK = {"paper": (18, 18, 21), "ink": (238, 238, 244),
        "dim": (122, 124, 138), "line": (196, 198, 210),
        "sep": (232, 114, 44)}
LIGHT = {"paper": (255, 255, 255), "ink": (28, 30, 40),
         "dim": (140, 144, 158), "line": (36, 38, 48),
         "sep": (216, 92, 30)}

DEFAULTS = {
    "dpi": 150,
    "margin": 62,
    "theme": "dark",
    "note_size": 32,        # 音符字号（列宽 = content_w/32 ≈ 35px，留出气口）
    "chord_pitch": 42,      # 和弦里相邻两音的垂直距离
    "bars_per_row": 4,      # 一行 4 小节
    "slots_per_bar": 8,     # 一小节 8 列，空拍也占位
    "rows_per_page": 8,     # 每页最多几行
    "rows_per_section": 4,  # 每几行画一条橙色分段线
}

FONT_REG = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
FONT_BOLD = [r"C:\Windows\Fonts\msyhbd.ttc",
             r"C:\Windows\Fonts\simhei.ttf"] + FONT_REG


def _font(size, bold=False):
    for p in (FONT_BOLD if bold else FONT_REG):
        if pathlib.Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def char_to_glyph(ch):
    """SMC 字符 → (音级 1-7, 八度偏移)。

    15 键跨 root..root+24，正好两个八度：
      D R M F S L X  = 第一八度 1-7  → **不带点**（这是本位，绝大多数旋律都在这）
      1 2 3 4 5 6 7  = 第二八度 1-7  → 上方一个点
      8              = root+24（再高一个八度）→ 上方两个点

    之前把 D R M F S L X 当成"低八度"画下方点，结果整首每个音都带点，
    和简谱的样子完全不符 —— 那一层是错的。
    """
    base = "DRMFSLX"
    if ch in base:
        return base.index(ch) + 1, 0
    if ch == "8":
        return 1, 2
    if ch in "1234567":
        return int(ch), 1
    return None


def _extra_meta(text):
    """谱面四角信息（SMC 里没有就留空，渲染时用占位符）。"""
    def field(*names):
        for name in names:
            m = re.search(rf"{name}:\[([^\]]*)\]", text)
            if m and m.group(1).strip():
                return m.group(1).strip()
        return ""

    return {
        "version": field("版本") or "Orig",
        "author": field("作者", "艺术家", "曲作者"),
        "arranger": field("改编", "扒谱", "制谱"),
    }


def _lanes(n):
    """n 个音竖排时各占哪一条"轨"。

    站点是**从第一行往下排**，不留白：一个音就在第一行（`.note-line:nth-child(1)`），
    两个音占第 1、2 行，三个音占满 1、2、3 行。
    这里必须跟站点一致 —— 之前单个音走中线（第 2 行），成品和站上对不上。
    """
    return {1: [0], 2: [0, 1], 3: [0, 1, 2]}.get(n, [0, 1, 2])


# ---------------------------------------------------------------- 分小节
def split_bars(groups, per_bar=8):
    """按小节切开，**保留格号**（空拍列不压缩，位置就是它在小节里的第几格）。"""
    bars = []
    for i in range(0, len(groups), per_bar):
        chunk = groups[i:i + per_bar]
        cols = []
        for k, g in enumerate(chunk):
            glyphs = []
            for c in g[:3]:
                if c in "0<> ":
                    continue
                gl = char_to_glyph(c)
                if gl:
                    glyphs.append(gl)
            if glyphs:
                glyphs.sort(key=lambda t: -(t[1] * 7 + t[0]))
                cols.append((k, glyphs))        # k = 这一格在小节里的列号
        bars.append({"start": i, "cols": cols})
    return bars


def _rows(bars, per_row):
    """小节按 4 个一行分组；尾行不足的补空小节（画成带线的空白小节）。"""
    rows = []
    for i in range(0, len(bars), per_row):
        chunk = bars[i:i + per_row]
        chunk = chunk + [None] * (per_row - len(chunk))
        rows.append(chunk)
    # 末尾整行没音的行不占页（只砍结尾空行，中间空行照留）
    while rows and not any(b and b["cols"] for b in rows[-1]):
        rows.pop()
    return rows


def _plan(text, opt):
    """整张谱的几何计划：行 → 小节 → 固定列 → 每页哪几行。渲染和预览共用。"""
    o = dict(DEFAULTS)
    o.update({k: v for k, v in opt.items() if v is not None})
    o.update(DARK if o["theme"] == "dark" else LIGHT)

    scale = o["dpi"] / 150.0

    def px(v):
        return max(1, int(round(v * scale)))

    page_w, page_h = px(1240), px(1754)
    margin = px(o["margin"])
    content_w = page_w - 2 * margin

    per_row = max(1, int(o["bars_per_row"]))
    per_bar = max(1, int(o["slots_per_bar"]))
    columns = per_row * per_bar                 # 一行总列数：4×8 = 32
    cell = content_w / float(columns)           # 列宽：全页统一 → 小节线等距
    measure_w = content_w / float(per_row)

    meta = smc.parse_meta(text)
    meta.update(_extra_meta(text))
    groups = smc.parse_groups(text)
    bars = split_bars(groups, per_bar)
    rows = _rows(bars, per_row)

    head_h = px(224)
    foot_h = px(58)
    sys_h = px(o["chord_pitch"]) * 2 + px(86)
    sec = max(1, int(o["rows_per_section"]))
    sec_extra = px(24)                          # 段间多留的空（给橙色线呼吸）
    per_page = min(max(1, int(o["rows_per_page"])),
                   max(1, int((page_h - head_h - foot_h - margin) // sys_h)))

    def layout(bottom):
        """竖向游标排页：行高固定，段尾多留一点；放不下就翻页。"""
        pages, cur, y = [], [], head_h
        for ri in range(len(rows)):
            if cur and (len(cur) >= per_page or y + sys_h > bottom):
                pages.append(cur)
                cur, y = [], head_h
            cur.append((ri, y))
            y += sys_h
            if (ri + 1) % sec == 0:
                y += sec_extra
        if cur:
            pages.append(cur)
        return pages, y

    if o.get("paginate", True):
        page_rows, _ = layout(page_h - foot_h - margin)
    else:
        # 长图：不分页。per_page 必须放开，否则 layout 仍会按行数切开，
        # 结果只画出第一页（之前 47 小节的长图只出了 992px 高，就是这个原因）。
        per_page = max(1, len(rows))
        page_rows, content_bottom = layout(10 ** 9)
        page_h = content_bottom + px(64)
    if not page_rows:
        page_rows = [[]]

    # 小节线竖向范围：按全谱实际用到的"轨"来定（有和弦就长）
    lane_oct = {}
    for b in bars:
        for (_k, gl) in b["cols"]:
            ls = _lanes(len(gl))
            for gi in range(min(len(gl), 3)):
                lane_oct[ls[gi]] = max(lane_oct.get(ls[gi], 0), gl[gi][1])
    top_lane = min(lane_oct) if lane_oct else 1
    bot_lane = max(lane_oct) if lane_oct else 1

    return {"o": o, "px": px, "meta": meta, "groups": groups, "bars": bars,
            "rows": rows, "page_rows": page_rows,
            "page_w": page_w, "page_h": page_h,
            "head_h": head_h, "foot_h": foot_h, "sys_h": sys_h,
            "margin": margin, "content_w": content_w,
            "cell": cell, "measure_w": measure_w,
            "per_row": per_row, "per_bar": per_bar,
            "sec": sec, "sec_extra": sec_extra,
            "top_lane": top_lane, "bot_lane": bot_lane,
            "top_oct": lane_oct.get(top_lane, 0)}


def render_pages(text, **opt):
    p = _plan(text, opt)
    o, meta, groups = p["o"], p["meta"], p["groups"]
    rows, page_rows = p["rows"], p["page_rows"]
    px = p["px"]
    page_w, page_h = p["page_w"], p["page_h"]
    margin = p["margin"]
    cell, measure_w, per_row = p["cell"], p["measure_w"], p["per_row"]
    sys_h, head_h = p["sys_h"], p["head_h"]

    f_note = _font(px(o["note_size"]))
    f_title = _font(px(48), bold=True)
    f_meta = _font(px(22))
    f_label = _font(px(20))
    f_foot = _font(px(18))

    pitch = px(o["chord_pitch"])

    # 用真实包围盒算音符上下沿，小节线才能把音（和八度点）整个框住
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    gb = probe.textbbox((0, 0), "0", font=f_note)
    asc, desc = gb[1], gb[3]              # 字形墨迹的上/下沿（相对绘制原点）

    note_dy = px(12)                      # 第一条轨距小节线顶端的距离
    # 小节线固定框住 3 条轨（每一拍永远渲染 3 行，空行也占高度），
    # 这样每一行长得一样，行与行之间上下也对得齐。
    bar_top = note_dy + asc
    if p["top_oct"] > 0:                  # 八度点画在音的上方，要给它们留位置
        bar_top -= px(9) + px(3.4) + (p["top_oct"] - 1) * px(11)
    bar_bot = note_dy + 2 * pitch + desc
    bar_top -= px(5)
    bar_bot += px(5)

    line_w = px(2)
    end_w = px(3)
    sep_w = px(3)
    dot_r = max(2, int(round(px(o["note_size"]) * 0.085)))

    total_pages = len(page_rows)
    pages = []
    for page_no, plist in enumerate(page_rows):
        img = Image.new("RGB", (page_w, page_h), o["paper"])
        d = ImageDraw.Draw(img)

        # ---------- 页眉：标题居中，四角两行信息 ----------
        title = meta.get("title") or "未命名"
        if not title.startswith("《"):
            title = f"《{title}》"
        if page_no == 0:
            tw = d.textlength(title, font=f_title)
            d.text(((page_w - tw) / 2, px(44)), title, font=f_title, fill=o["ink"])
        else:
            t2 = f"{title}（续）"
            tw = d.textlength(t2, font=f_meta)
            d.text(((page_w - tw) / 2, px(78)), t2, font=f_meta, fill=o["dim"])

        key_txt = meta.get("key_raw") or meta.get("key", "C")
        left_lines = [f"调性: {key_txt}", f"版本: {meta.get('version') or 'Orig'}"]
        right_lines = [f"作者/艺术家: {meta.get('author') or '—'}",
                       f"改编: {meta.get('arranger') or '—'}"]
        for i, txt in enumerate(left_lines):
            d.text((margin, px(118) + i * px(34)), txt, font=f_meta, fill=o["dim"])
        for i, txt in enumerate(right_lines):
            d.text((page_w - margin - d.textlength(txt, font=f_meta),
                    px(118) + i * px(34)), txt, font=f_meta, fill=o["dim"])

        # ---------- 谱面（固定网格：每行 4 小节 × 每小节 8 列） ----------
        xe = margin + p["content_w"]
        for k, (ri, y_top) in enumerate(plist):
            row = rows[ri]
            bar_a = y_top + bar_top
            bar_b = y_top + bar_bot

            # 行首小节号：贴在行首小节线正上方
            d.text((margin, y_top - px(28)),
                   f"[{ri * per_row + 1}]", font=f_label, fill=o["dim"])

            for m in range(per_row):
                bx = margin + m * measure_w
                # 每个小节都以一条竖线开头（空格小节也一样，整行 5 条线齐全）
                d.line([(bx, bar_a), (bx, bar_b)], fill=o["line"], width=line_w)

                bar = row[m]
                if not bar:
                    continue
                for (slot_k, glyphs) in bar["cols"]:
                    # 列号决定 x：空拍不压缩，音符落在格子正中
                    cx = bx + (slot_k + 0.5) * cell
                    lanes = _lanes(len(glyphs))
                    for gi, (deg, octv) in enumerate(glyphs[:3]):
                        cy = y_top + note_dy + lanes[gi] * pitch
                        txt = str(deg)
                        tw = d.textlength(txt, font=f_note)
                        d.text((cx - tw / 2, cy), txt, font=f_note, fill=o["ink"])

                        # 用文字包围盒定位点，避免和相邻行撞
                        bb = d.textbbox((cx - tw / 2, cy), txt, font=f_note)
                        if octv > 0:
                            for q in range(octv):
                                dy = bb[1] - px(9) - q * px(11)
                                d.ellipse([cx - dot_r, dy - dot_r,
                                           cx + dot_r, dy + dot_r], fill=o["ink"])
                        elif octv < 0:
                            for q in range(-octv):
                                dy = bb[3] + px(9) + q * px(11)
                                d.ellipse([cx - dot_r, dy - dot_r,
                                           cx + dot_r, dy + dot_r], fill=o["ink"])

            # 行尾收线：固定落在 page_w - margin（第 4 条小节线之后）
            d.line([(xe, bar_a), (xe, bar_b)], fill=o["line"], width=end_w)

            # 橙色分段线：每 4 行一条，画在"上一行小节底部"和"下一行小节号"中间
            if ((ri + 1) % p["sec"] == 0 and ri + 1 < len(rows)
                    and k + 1 < len(plist)):
                nxt = plist[k + 1][1]
                sy = (bar_b + nxt - px(30)) / 2
                d.line([(margin, sy), (xe, sy)], fill=o["sep"], width=sep_w)

        # ---------- 页脚：左信息 / 中小字说明 / 右页码 ----------
        fy = page_h - margin + px(4)
        diff = str(meta.get("difficulty", "") or "")
        left = (f"{meta.get('tempo', 120)} BPM · {meta.get('time_sig', '4/4')} · "
                f"{len(groups)} 格 · {len(p['bars'])} 小节")
        if diff:
            left += f" · 难度 {diff}"
        d.text((margin, fy), left, font=f_foot, fill=o["dim"])

        note = "仅供个人学习研究使用，请勿商用"
        d.text(((page_w - d.textlength(note, font=f_foot)) / 2, fy), note,
               font=f_foot, fill=o["dim"])

        pn = f"{page_no + 1} / {total_pages}"
        d.text((page_w - margin - d.textlength(pn, font=f_foot), fy), pn,
               font=f_foot, fill=o["dim"])

        pages.append(img)

    return pages


def render_file(smc_path, out_path, **opt):
    text = pathlib.Path(smc_path).read_text(encoding="utf-8")
    pages = render_pages(text, **opt)
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    dpi = opt.get("dpi") or DEFAULTS["dpi"]

    if out.suffix.lower() == ".pdf":
        pages[0].save(out, save_all=True, append_images=pages[1:], resolution=dpi)
        return [out]
    if len(pages) == 1:
        pages[0].save(out)
        return [out]

    made = []
    for i, p in enumerate(pages, 1):
        f = out.with_name(f"{out.stem}_{i}{out.suffix}")
        p.save(f)
        made.append(f)
    return made


def render_long_png(text, out_path, **opt):
    """整首一张长图（手机上顺着往下划就能看）"""
    opt["paginate"] = False
    pages = render_pages(text, **opt)
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, optimize=True)
    return out


def render_first_page(text, **opt):
    """只画第一页，并按内容高度裁掉下方空白（给界面做预览）"""
    opt.pop("paginate", None)
    page = render_pages(text, **opt)[0]
    p = _plan(text, opt)
    px = p["px"]
    plist = p["page_rows"][0]
    if plist:
        bottom = plist[-1][1] + p["sys_h"] + px(70)
    else:
        bottom = p["head_h"] + px(120)
    return page.crop((0, 0, page.width, min(page.height, int(bottom))))


if __name__ == "__main__":
    import sys
    print(render_file(sys.argv[1], sys.argv[2]))
