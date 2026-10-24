# -*- coding: utf-8 -*-
"""五线谱图片 / PDF → MusicXML（调用外部 OMR 引擎 Audiveris）。

这一步必须借外部工具：把图片里的音符读出来是计算机视觉问题，
成熟的开源方案只有 Audiveris（Java）。本模块负责"调它、等它、找产物"，
外加**把 OMR 的输入条件调到它擅长的区间**：

  1. 多页 PDF 逐页识别，再把每页的 MusicXML 合并成一份（小节连续编号，
     `<divisions>` 不一致时按 lcm 重缩放，否则后半首的时值会整体错位）
  2. 手机拍照的三类病：页面倾斜（投影法测角 + 反旋）、透视（找页面四边形做
     四点透视校正）、噪点（中值）
  3. 放大倍数不再靠"×2"，而是按**五线间距**（interline）定目标 —— 实测
     Audiveris 在 interline 15~25 最稳，同时用 16M 像素上限兜住（超了它直接
     被 OOM 打死）
  4. 失败时按日志特征给可操作建议，而不是甩一坨 Java 栈

安装 Audiveris：
    Windows  https://github.com/audiveris/audiveris/releases  选 windowsConsole 的 msi
    Linux    sudo apt install audiveris
    macOS    brew install --cask audiveris
找不到可执行文件时用环境变量指定：

    set SKY15_AUDIVERIS=D:\\Audiveris\\Audiveris.exe
"""
from .tmp import tmpdir, tmpfile
import os, pathlib, shutil, subprocess, sys, tempfile, math, re, time

# Audiveris 的硬上限：总像素超过约 16M 时会 Exit forced（内存/图幅），不是"慢一点"
MAX_PIXELS = 16_000_000
# 目标五线间距（px）。实测 1439x2249 原图 interline≈9 → 识别漏音严重；
# 2× 后 interline≈18 → 好很多；再往上收益递减但要吃像素预算
TARGET_INTERLINE = 18
BITMAP_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
PDF_EXT = (".pdf",)

#: 环境变量：指向 homr.exe（可选引擎）
HOMR_ENV = "SKY15_HOMR"
#: homr 的常见安装位置（找不到才需要设 SKY15_HOMR）
HOMR_CANDIDATES = (
    r"E:\ai\workspace\.engines\homr-venv\Scripts\homr.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\sky15\homr\Scripts\homr.exe"),
    os.path.expandvars(r"%USERPROFILE%\sky15\homr\Scripts\homr.exe"),
)


# ---------------------------------------------------------------- 引擎发现
def find_audiveris():
    """按顺序找：环境变量 → PATH → 常见安装位置"""
    env = os.environ.get("SKY15_AUDIVERIS")
    if env and pathlib.Path(env).exists():
        return env

    for name in ("audiveris", "Audiveris", "Audiveris.exe"):
        p = shutil.which(name)
        if p:
            return p

    common = [
        r"C:\Program Files\Audiveris\Audiveris.exe",
        r"C:\Program Files\Audiveris\audiveris.bat",
        r"C:\Program Files (x86)\Audiveris\Audiveris.exe",
        "/usr/bin/audiveris",
        "/usr/local/bin/audiveris",
        "/Applications/Audiveris.app/Contents/MacOS/Audiveris",
    ]
    for p in common:
        if pathlib.Path(p).exists():
            return p
    return None


def available():
    return find_audiveris() is not None


def _require_exe():
    exe = find_audiveris()
    if not exe:
        raise RuntimeError(
            "没找到 Audiveris（五线谱识别的外部引擎）。\n"
            "  下载：https://github.com/audiveris/audiveris/releases\n"
            "  选 windowsConsole 那个 msi，装完即可（它自带 Java 运行时）\n"
            "  或者设环境变量 SKY15_AUDIVERIS 指向可执行文件"
        )
    return exe


# ---------------------------------------------------------------- 图像度量
def _np():
    import numpy
    return numpy


def _cv2():
    import cv2
    return cv2


def load_gray(path):
    """读成 uint8 灰度 ndarray。

    Windows 上 cv2.imread 读不了中文路径（"雨爱_五线谱.jpg" 直接返回 None），
    所以走 fromfile+imdecode；再退回 PIL（能多认几种手机格式）。
    """
    np = _np()
    try:
        cv2 = _cv2()
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size:
            arr = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
            if arr is not None:
                return arr
    except Exception:
        pass
    from PIL import Image
    return np.array(Image.open(str(path)).convert("L"))


def _save_gray(arr, path):
    from PIL import Image
    Image.fromarray(arr).save(str(path), optimize=True)
    return pathlib.Path(path)


def _ink_threshold(gray):
    """墨迹阈值：Otsu，但夹在 [60, 200]。

    死用 128 会瞎：手机拍照 + JPEG 压缩后，1px 宽的谱线灰到 150~180，
    `< 128` 一条都取不到，`estimate_interline` 直接返回 None，
    于是"看不清谱线 → 不知道要不要放大"（实测拍照件 interline=None，
    放大后仍然 None，识别直接失败）。
    """
    cv2 = _cv2()
    t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(min(200, max(60, float(t))))


def _interline_autocorr(row, lo=6, hi=40):
    """行投影自相关的第一个显著峰 → interline（备用手段，见 estimate_interline）。"""
    np = _np()
    if row.size < 4 * lo:
        return None
    r = row - float(row.mean())
    if not float(np.abs(r).max()):
        return None
    ac = np.correlate(r, r, "full")[len(r) - 1:]
    ac = ac / ac[0]
    best = None
    for lag in range(lo, min(hi, ac.size - 1) + 1):
        if ac[lag] < 0.35:
            continue
        if ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]:
            best = (float(ac[lag]), lag)   # 第一个本地极大（取最小的周期，避免 2×/3× 谐波）
            break
    if best is None:
        lag = lo + int(np.argmax(ac[lo:min(hi, ac.size - 1) + 1]))
        if ac[lag] < 0.2:
            return None
        best = (float(ac[lag]), lag)
    return round(float(best[1]), 2)


def estimate_interline(gray):
    """估五线间距（px）：五线谱的谱线横贯整页，一行的墨水量远超其它行。

    只取"最小的那一簇间距"，不取全体中位数 —— 一页里同时存在
    行内间距（≈interline）和谱表间距（≈2~3×interline），当噪声多到
    某簇数量翻转时，中位数会突然从 12.5 跳到 48（实测过）。

    返回 None 表示"看不出谱线"（图太糊、太斜/横放、或根本不是谱）。
    """
    np = _np()
    h, w = gray.shape
    if h < 40 or w < 40:
        return None
    ink = (gray < _ink_threshold(gray))
    row = ink.sum(1) / float(w)
    cand = np.where(row > 0.45)[0]
    if cand.size < 10:
        # 备用：谱线没横贯整行（糊的 / 只占半边）时，用行投影的**自相关周期**
        # 找 interline。歪 1.5° 的谱线每行墨量只有 0.39，过不了 0.45 的线，
        # 但整页行投影的周期仍然是 7.5px。
        # 前提：至少**有一行**横贯 45%（说明谱线基本是水平的）。整页歪着拍
        # 时行投影是一条单调衰减曲线，自相关只会给出 JPEG 块效应的假周期
        # （实测歪 1.5° 的拍照件会报 4.0px）。
        if float(row.max()) < 0.45:
            return None
        return _interline_autocorr(row)
    # 把连续行并成"谱线组"，取组中心
    centers, cur = [], [cand[0]]
    for v in cand[1:]:
        if v - cur[-1] <= 2:
            cur.append(v)
        else:
            centers.append(sum(cur) / len(cur))
            cur = [v]
    centers.append(sum(cur) / len(cur))
    if len(centers) < 6:
        return None
    gaps = np.diff(np.array(centers))
    gaps = gaps[(gaps >= 3.0) & (gaps <= 60.0)]
    if gaps.size < 4:
        return None
    # 最密的一簇（容差 ±2px），同数量时取更小的 → 行内间距
    best_c, best_n = None, -1
    for g in gaps:
        near = gaps[np.abs(gaps - g) <= 2.0]
        if near.size > best_n or (near.size == best_n and g < best_c):
            best_c, best_n = float(near.mean()), near.size
    return round(best_c, 2) if best_c else None


def noise_level(gray):
    """每百万像素里有多少个 ≤6px 的孤立墨点（手机压缩/纸张噪点）。

    干净扫描件接近 0；这个数大于几百才值得去噪 —— 盲目中值滤波会把
    1px 的谱线一起抹掉（实测谱线中心检测数从 35 掉到 26）。
    """
    cv2 = _cv2()
    np = _np()
    binimg = (gray < 128).astype("uint8")
    n, lab, stats, _ = cv2.connectedComponentsWithStats(binimg, 8)
    if n <= 1:
        return 0.0
    areas = stats[1:, cv2.CC_STAT_AREA]
    tiny = int((areas <= 6).sum())
    mp = gray.shape[0] * gray.shape[1] / 1e6
    return round(tiny / max(mp, 1e-6), 1)


def estimate_ink(gray):
    """墨水占比，用来判断"是不是一张谱"和人眼可读性。"""
    np = _np()
    return float((gray < 128).mean())


def _rotate(gray, angle):
    """按角度（度，正=逆时针）旋转灰度图，空白处填白。

    插值必须用 **LANCZOS4**（或 CUBIC），不能 LINEAR：实测同一张 1.5° 歪的
    手机拍照件，LINEAR 会把细线（小节线、符干）糊掉，Audiveris 认不出小节线
    就把 15 小节读成每小节 6~7 拍（总量 102 拍 / 应为 60 拍，调也从 E 变成 A）；
    换 LANCZOS4 后 15×4.0 拍、调 E、调内比例 0.975，和干净扫描件持平。
    """
    cv2 = _cv2()
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2.0 - w / 2.0
    M[1, 2] += nh / 2.0 - h / 2.0
    return cv2.warpAffine(gray, M, (nw, nh),
                          flags=cv2.INTER_LANCZOS4,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def _profile_score(gray):
    """行投影的锐度：谱线越水平，墨水量在少数行上越集中 → 方差越大。"""
    np = _np()
    row = (gray < _ink_threshold(gray)).sum(1).astype(np.float32)
    if row.size < 8:
        return 0.0
    return float(row.var())


def detect_rotation(gray, ratio=2.0):
    """整页被横放了没有：音乐行是竖着走的 → 列投影比行投影锐得多。

    返回 0 或 90（顺时针转 90 度才能正过来）。手机拍横了、扫描时选了横版都会这样，
    Audiveris 对旋转过的图是零容忍（interline 都量不出来，直接当空页）。
    """
    np = _np()
    h, w = gray.shape
    scale = 900.0 / max(h, w)
    if scale < 1:
        small = _cv2().resize(gray, (int(w * scale), int(h * scale)),
                              interpolation=_cv2().INTER_AREA)
    else:
        small = gray
    rs = _profile_score(small)
    cs = _profile_score(np.ascontiguousarray(small.T))
    if cs > ratio * rs and rs > 0:
        return 90
    return 0


def estimate_skew(gray, limit=8.0, coarse=0.5, fine=0.05):
    """投影法测倾斜。**返回值是"校正角"**：直接 `_rotate(gray, 返回值)` 就把谱线摆平。

    先在缩到宽 700 的小图上粗扫 ±limit，再在最优点附近细扫。
    """
    cv2 = _cv2()
    np = _np()
    h, w = gray.shape
    scale = 700.0 / max(1, w)
    small = cv2.resize(gray, (int(w * scale), max(8, int(h * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1 else gray
    best_a, best_s = 0.0, _profile_score(small)
    a = -limit
    while a <= limit + 1e-9:
        s = _profile_score(_rotate(small, a))
        if s > best_s:
            best_a, best_s = a, s
        a += coarse
    a = best_a - coarse
    while a <= best_a + coarse + 1e-9:
        s = _profile_score(_rotate(small, a))
        if s > best_s:
            best_a, best_s = a, s
        a += fine
    return float(best_a)


def _quad_edges(best):
    """四个角 → [(角度, 长度), ...]，角度是边的方向（0~180 度）。"""
    np = _np()
    out = []
    for i in range(4):
        p0, p1 = best[i], best[(i + 1) % 4]
        ang = abs(math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))) % 180.0
        out.append((ang, float(np.linalg.norm(p1 - p0))))
    return out


def _is_perspective(edges, min_skew=0.5, min_diff=0.4):
    """这个四边形**需要**校正吗？（已经不是摆平的矩形 → 才值得 warp）

    · 四条边都必须"接近水平/垂直"（纸不会歪 40 度，那是别的东西）
    · 至少一条边偏离轴线 ≥ min_skew 度
    · 对边(0vs2, 1vs3)角度不平行 ≥ min_diff 度
    阈值取得很松：反正后面还有"不许裁掉墨迹"的保险，这里只负责挡掉
    已经完全摆平的扫描件（那种 warp 纯属白白重采样一次）。
    """
    devs = []
    for ang, _ in edges:
        devs.append(min(abs(ang), abs(ang - 90), abs(ang - 180)))
    if max(devs) > 35.0:
        return False
    if max(devs) < min_skew:
        return False
    d02 = abs(edges[0][0] - edges[2][0])
    d13 = abs(edges[1][0] - edges[3][0])
    d02 = min(d02, abs(d02 - 180.0))
    d13 = min(d13, abs(d13 - 180.0))
    return max(d02, d13) >= min_diff


def find_page_quad(gray, min_contrast=12.0):
    """找"纸"的四个角（手机拍照才有：纸比周围桌面亮，边界是一条四边形）。

    判据是**对比度**而不是"有没有四边形"：
      · 画面外圈(6% 边带) vs 中心(50%) 的亮度差 ≥ `min_contrast` 才认为
        "拍到了桌面上的纸"；扫描件/整页截图两者一样亮 → 直接返回 None，
        绝不去裁它一圈（历史坑：拿 OTSU 亮区当前景，纸和桌面都亮的时候
        整幅图变成一个 0.997 面积的前景块，标定出个假四边形把谱裁掉）
      · 四边形还得"需要校正"（至少一条边偏离 0/90 轴 ≥0.5°）
    返回 4x2 float32（原图坐标）或 None。
    """
    cv2 = _cv2()
    np = _np()
    h, w = gray.shape
    if min(h, w) < 120:
        return None
    band = max(4, int(0.06 * min(h, w)))
    ring = float(np.mean(np.concatenate([
        gray[:band, :].ravel(), gray[-band:, :].ravel(),
        gray[:, :band].ravel(), gray[:, -band:].ravel()])))
    cy0, cy1 = int(h * 0.25), int(h * 0.75)
    cx0, cx1 = int(w * 0.25), int(w * 0.75)
    center = float(np.mean(gray[cy0:cy1, cx0:cx1]))
    if center - ring < min_contrast:
        return None                      # 没有可辨识的纸边（扫描件/满幅截图）
    thr = int(max(1, min(254, (ring + center) / 2.0)))
    work = gray
    small_scale = 1200.0 / max(h, w)
    k = 1.0
    if small_scale < 1:
        work = cv2.resize(gray, (int(w * small_scale), int(h * small_scale)),
                          interpolation=cv2.INTER_AREA)
        k = 1.0 / small_scale
    sh, sw = work.shape
    mask = (work >= thr).astype("uint8") * 255
    ck = max(3, int(min(sh, sw) / 30) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (ck, ck)))
    cnts = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    if not cnts:
        return None
    best, best_area = None, 0.0
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:6]:
        area = cv2.contourArea(c)
        if area < 0.35 * sh * sw:
            continue                     # 纸必须占画面一大块，否则是噪点/页眉
        peri = cv2.arcLength(c, True)
        ap = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(ap) == 4 and cv2.isContourConvex(ap) and area > best_area:
            best, best_area = ap.reshape(4, 2).astype("float32"), area
    if best is None:
        return None
    if not _is_perspective(_quad_edges(best)):
        return None                      # 已经摆平了，不用动
    # 最后一道保险：**不许把墨迹裁掉**。候选四边形之外还有谱子，说明标定歪了
    ink = (work < max(60, int(float(work.mean()) * 0.75))).astype("uint8")
    if ink.sum() > 0:
        inside = np.zeros_like(ink)
        cv2.fillConvexPoly(inside, best.astype("int32"), 1)
        keep = float((ink & inside).sum()) / float(ink.sum())
        if keep < 0.995:
            return None                  # 会裁掉 0.5% 以上的墨：宁可不校正
    return best * k


def warp_page(gray, quad):
    """四点透视校正 → 摆正的矩形（长宽取四边的平均长度）。"""
    cv2 = _cv2()
    np = _np()
    pts = quad.reshape(4, 2).astype("float32")
    s = pts.sum(1)
    d = np.diff(pts, axis=1).ravel()
    rect = np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")
    (tl, tr, br, bl) = rect
    wA = np.linalg.norm(br - bl)
    wB = np.linalg.norm(tr - tl)
    hA = np.linalg.norm(tr - br)
    hB = np.linalg.norm(tl - bl)
    W, H = int(max(wA, wB)), int(max(hA, hB))
    if W < 50 or H < 50:
        return gray
    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype="float32")
    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(gray, M, (W, H), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def _denoise(gray):
    """只做中值（3x3），**不做开运算**：2x2 开运算会把 1~2px 的谱线整条抹掉，
    谱线少了 Audiveris 就找不到谱表（实测 interline 从 24.75 跳到 48）。

    再加一道自检：抹掉 >7% 的墨就说明伤到谱线了，宁可不做去噪。
    """
    cv2 = _cv2()
    out = cv2.medianBlur(gray, 3)
    before = float((gray < 128).mean())
    after = float((out < 128).mean())
    if before > 0 and after < before * 0.93:
        return gray
    return out


def _resize_gray(gray, factor):
    from PIL import Image
    im = Image.fromarray(gray)
    return _np().array(im.resize((int(im.width * factor), int(im.height * factor)),
                                 Image.LANCZOS))


def plan_scale(width, height, interline=None, upscale=None,
               target_interline=TARGET_INTERLINE, max_pixels=MAX_PIXELS):
    """算放大倍数：按五线间距定目标，再用像素预算封顶。

    返回 (factor, note)。`upscale` 给了就强制（仍受像素上限约束）。
    """
    if upscale:
        factor = float(upscale)
    elif interline and interline > 0:
        factor = target_interline / interline
        factor = 1.0 if factor < 1.25 else (2.0 if factor < 2.6 else
                                            (3.0 if factor < 3.6 else 4.0))
    else:
        # 没测出五线间距：退回"短边 <2000 就 ×2"的老经验
        factor = 2.0 if min(width, height) < 2000 else 1.0
    note = ""
    want = float(factor)
    px = (width * factor) * (height * factor)
    while factor > 1.0 and px > max_pixels:
        factor -= 1.0
        px = (width * factor) * (height * factor)
    if factor <= 1.0:
        factor = 1.0
    if want > factor:
        # 别静默降档：有人以为自己在跑 2×，其实被 16M 像素上限压回 1×
        note = (f"要的 ×{want:g} 会到 {width*want*height*want/1e6:.1f}M 像素，"
                f"超过 {max_pixels/1e6:.0f}M 上限 → 实际按 ×{factor:g} 跑")
    if px > max_pixels:
        note = f"已是 1× 仍 {px/1e6:.1f}M 像素 > {max_pixels/1e6:.0f}M 上限"
    return factor, note


def preprocess_image(src, outdir, *, rotate="auto", deskew="auto", warp="auto",
                     denoise="auto", scale=None, upscale=None,
                     target_interline=TARGET_INTERLINE,
                     max_pixels=MAX_PIXELS):
    """一张谱图 → 送去识别的那张图。返回 dict（含 path 和做了什么）。

    `rotate` / `deskew` / `warp` / `denoise` 默认 "auto"：只在**真的测出病**
    时才动手，干净扫描件走原路径（重采样本身会损细节）。
    """
    src = pathlib.Path(src)
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    gray = load_gray(src)
    info = {
        "src": str(src),
        "size_before": [int(gray.shape[1]), int(gray.shape[0])],
        "interline_before": estimate_interline(gray),
        "ink": round(estimate_ink(gray), 4),
        "actions": [],
    }
    work = gray

    # 0) 整页横放（手机拍横了 / 扫描选错方向）
    rot = 0
    if rotate == "auto" or rotate is True:
        rot = detect_rotation(work)
    elif rotate:
        rot = int(rotate) % 360
    if rot == 90:
        work = _cv2().rotate(work, _cv2().ROTATE_90_CLOCKWISE)
        info["actions"].append(f"转正 90° {gray.shape[1]}x{gray.shape[0]} → "
                               f"{work.shape[1]}x{work.shape[0]}")
    elif rot == 180:
        work = _cv2().rotate(work, _cv2().ROTATE_180)
        info["actions"].append("转正 180°")
    elif rot == 270:
        work = _cv2().rotate(work, _cv2().ROTATE_90_COUNTERCLOCKWISE)
        info["actions"].append("转正 270°")
    info["rotated"] = rot

    # 1) 透视：手机拍照的纸条不会正对镜头
    quad = find_page_quad(work) if (warp == "auto" or warp is True) else None
    if quad is not None:
        before = work.shape[:2]
        work = warp_page(work, quad)
        info["actions"].append(f"透视校正 {before[1]}x{before[0]} → "
                               f"{work.shape[1]}x{work.shape[0]}")
    info["warped"] = quad is not None

    # 2) 倾斜：投影法找让谱线最水平的角（estimate_skew 返回的就是校正角）
    #    **必须排在量 interline 前面**：歪 1.5° 的谱线在行投影上散成 40 行，
    #    每行墨量都不到宽度的 45%，`estimate_interline` 直接返回 None
    #    （实测拍照件：先纠斜才有 69 行过线，不纠斜 0 行）。
    #    投影方差在低分辨率下依然稳：1× 的糊照片都能估到 0.01° 以内。
    ang = 0.0
    if deskew == "auto" or deskew is True:
        ang = estimate_skew(work)
        if abs(ang) >= 0.4:
            work = _rotate(work, ang)
            info["actions"].append(f"纠斜 {ang:+.2f}°")
        else:
            ang = 0.0
    elif isinstance(deskew, (int, float)) and deskew:
        ang = float(deskew)
        work = _rotate(work, ang)
        info["actions"].append(f"纠斜（指定）{ang:+.2f}°")
    info["skew"] = round(float(ang), 3)

    # 3) 放大：量出 interline 才能算倍数（CLEF: 目标 15~20px）
    il = estimate_interline(work)
    info["interline_before_scale"] = il
    factor, note = plan_scale(work.shape[1], work.shape[0], il,
                              upscale=upscale if scale is None else scale,
                              target_interline=target_interline,
                              max_pixels=max_pixels)
    if factor > 1.0:
        work = _resize_gray(work, factor)
        info["actions"].append(f"放大 ×{factor:g}")
    info["scale"] = round(float(factor), 3)
    if note:
        info["note"] = note

    # 4) 噪点：**放大之后**才量（小图上的孤立噪点密度会被高估 10 倍：
    #    同一张拍照件 1× 是 706 个/Mpx，2× 只有 61 个/Mpx），
    #    而且盲目中值去噪会把 1~2px 的谱线抹掉，宁可不做。
    nz = noise_level(work)
    info["noise"] = nz
    if denoise is True or (denoise == "auto" and nz >= 200):
        work = _denoise(work)
        info["actions"].append(f"中值去噪（孤立噪点 {nz}/Mpx）")

    info["interline_after"] = estimate_interline(work)
    info["size_after"] = [int(work.shape[1]), int(work.shape[0])]
    info["megapixels"] = round(work.shape[0] * work.shape[1] / 1e6, 2)
    if note:
        info["note"] = note

    if work is gray:
        info["path"] = str(src)
        info["actions"] = info["actions"] or ["无（原图直接识别）"]
        return info
    tag = src.stem + ("_pp" if info["actions"] else "")
    dst = outdir / (tag + ".png")
    _save_gray(work, dst)
    info["path"] = str(dst)
    return info


def analyze_image(path):
    """只量不识别：给对照表用的指标。"""
    gray = load_gray(path)
    w, h = gray.shape[1], gray.shape[0]
    return {
        "path": str(path),
        "w": w, "h": h,
        "megapixels": round(w * h / 1e6, 2),
        "interline": estimate_interline(gray),
        "skew": round(estimate_skew(gray), 3),
        "ink": round(estimate_ink(gray), 4),
    }


# ---------------------------------------------------------------- PDF
def pdf_page_count(path):
    try:
        import pypdfium2 as pdfium
    except Exception:
        return 0
    try:
        doc = pdfium.PdfDocument(str(path))
        return len(doc)
    except Exception:
        return 0


def pdf_to_images(path, outdir, dpi=300, first=None, last=None):
    """PDF → 每页一张 PNG。用 pypdfium2（pip 自带二进制，不依赖 poppler）。"""
    try:
        import pypdfium2 as pdfium
    except Exception as e:                       # pragma: no cover
        raise RuntimeError(
            "读 PDF 需要 pypdfium2（已随 requirements 安装）：pip install pypdfium2\n"
            f"  原始错误：{e}")
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(path))
    n = len(doc)
    lo = 0 if first is None else max(0, first - 1)
    hi = n if last is None else min(n, last)
    scale = float(dpi) / 72.0
    out = []
    for i in range(lo, hi):
        page = doc[i]
        bmp = page.render(scale=scale)
        im = bmp.to_pil().convert("L")
        p = outdir / f"page_{i+1:03d}.png"
        im.save(p, optimize=True)
        out.append(p)
    return out


# ---------------------------------------------------------------- MusicXML 合并
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _first_div(el, default=1):
    """第一处 `<divisions>`，**clamp 到 ≥1**。

    `<divisions>0</divisions>` 原来会一路带到下面的 lcm 再 `g / float(d)` →
    裸 ZeroDivisionError；而 `musicxml.py` 那边早就是 `max(1, …)`（按 1 解释）。
    两条路必须一种行为：都按 1 解释，并且由 merge 的 warn 说清楚 ——
    否则一个炸、一个静默把时值拉伸 4 倍。
    """
    for d in el.iter():
        if _local(d.tag) == "divisions":
            try:
                v = int(float(d.text))
            except (TypeError, ValueError):
                return default
            return v if v >= 1 else 1
    return default


def _bad_div_pages(paths, roots):
    """哪些页的 `<divisions>` 是 0 / 非数字（要进 warnings，不能静默 clamp）。"""
    out = []
    for p, r in zip(paths, roots):
        for d in r.iter():
            if _local(d.tag) != "divisions":
                continue
            try:
                if int(float(d.text)) < 1:
                    out.append(pathlib.Path(p).name)
            except (TypeError, ValueError):
                out.append(pathlib.Path(p).name)
            break
    return out


def _rescale_durations(root, factor):
    if factor == 1:
        return 0
    n = 0
    for d in root.iter():
        if _local(d.tag) == "duration" and d.text:
            try:
                d.text = str(int(round(float(d.text) * factor)))
                n += 1
            except ValueError:
                pass
    return n


def _write_divisions(root, value):
    """把一份谱里所有 `<divisions>` 改写成 value；一个都没有就补一个。

    **只重缩放 `<duration>` 而不改 `<divisions>` 是本模块最贵的错**：合并后
    第 2 页仍按自己原来的单位解释已经被放大的 duration，整页内容成倍拉长。
    实测 p001(div=4)+p002(div=8) 合并，产物里 `divisions` 是 `['4','8']`，
    第 1 页每小节 content_end 从 4.0 变 8.0、时值 1.0/3.0 变成 2.0/6.0，
    而 warnings 是空的。
    """
    import xml.etree.ElementTree as ET
    n = 0
    for d in root.iter():
        if _local(d.tag) == "divisions":
            d.text = str(int(value))
            n += 1
    if n:
        return n
    # 这一页压根没写 <divisions>（按 1 解释过）：重缩放之后必须补一个，
    # 否则读回来还是按 1 算，时值整体放大 value 倍
    for part in root:
        if _local(part.tag) != "part":
            continue
        ns = part.tag.split("}")[0] + "}" if part.tag.startswith("{") else ""
        for measure in part:
            if _local(measure.tag) != "measure":
                continue
            attrs = [c for c in measure if _local(c.tag) == "attributes"]
            if attrs:
                el = ET.Element(ns + "divisions")
                el.text = str(int(value))
                attrs[0].insert(0, el)      # schema 要求 divisions 是 attributes 首子元素
            else:
                a = ET.Element(ns + "attributes")
                ET.SubElement(a, ns + "divisions").text = str(int(value))
                measure.insert(0, a)
            break
        break
    return 1


def _load_root(path):
    """MusicXML / .mxl → `<score-partwise>` 根元素（.mxl 是 ZIP，不能直接 parse）。"""
    import xml.etree.ElementTree as ET
    from . import musicxml
    return musicxml._load(str(path))


# 最近一次合并的实情（divisions 原来各是多少、统一成多少、有哪些问题）。
# 多页合并是个静默出错的重灾区，留给调用方/排查用，不影响任何返回值。
LAST_MERGE = {"paths": [], "divisions": [], "unified": None,
              "warnings": [], "out": None}


def merge_musicxml(paths, out_path=None, warn=None):
    """把逐页识别出来的 MusicXML 合成一份（页序 = 传入顺序）。

    三件必须做的事：
      · `<divisions>` 各页可能不同（Audiveris / homr 按页自己算），不统一会让
        后一页的每个时值按前一页的单位解释 → 整页错位。用 lcm 统一，**并且把
        每页的 `<divisions>` 也改写成统一值**（只缩放 duration 不改 divisions
        等于没修：读回来还是按旧单位算）。
      · 小节编号重排（每页都从 1 开始）。
      · 只保留第一页的 `<part-list>`；页数不同时按 part 序号对齐，缺的补空。

    `warn` 收到的是给人看的中文说明（divisions 不一致 / 有页 divisions 无效 /
    lcm 过大），同时记在 `LAST_MERGE["warnings"]` 里。
    """
    import xml.etree.ElementTree as ET
    paths = [pathlib.Path(p) for p in paths if pathlib.Path(p).exists()]
    if not paths:
        raise RuntimeError("没有可合并的 MusicXML")
    if len(paths) == 1:
        return paths[0]

    roots, divs = [], []
    for p in paths:
        # `.mxl` 是 ZIP 容器（PK 头），直接 ET.parse 会报
        # "not well-formed (invalid token): line 1, column 2"
        roots.append(_load_root(p))
        divs.append(_first_div(roots[-1]))
    msgs = []

    def say(msg):
        msgs.append(msg)
        if warn:
            warn(msg)

    bad = _bad_div_pages(paths, roots)
    if bad:
        say(f"这些页的 <divisions> 是 0 或非数字（{', '.join(bad)}）：按 1 解释，"
            "时值可能整体偏大 —— 谱面本身有问题")
    g = 1
    for d in divs:
        d = d if d >= 1 else 1
        g = int(g * d / math.gcd(g, d)) if g else d
    if g > 10 ** 6:                     # lcm 炸了（如 10080 与 480 混）：退回最大值
        say(f"各页 divisions {divs} 的最小公倍数过大（{g}），改用最大值重缩放")
        g = max(divs)
    if len(set(divs)) > 1:
        say(f"各页 divisions 不一致 {divs} → 统一成 {g}（duration 已按比例重缩放）")
    for r, d in zip(roots, divs):
        d = d if d >= 1 else 1
        _rescale_durations(r, g / float(d))
        _write_divisions(r, g)          # ← 光缩放 duration 不改 divisions 就是错谱

    ns = ""
    tag = roots[0].tag
    if tag.startswith("{"):
        ns = tag.split("}")[0] + "}"

    def q(t):
        return ns + t

    parts = []
    nparts = max(len([c for c in r if _local(c.tag) == "part"]) for r in roots)
    per_page = []
    for r in roots:
        ps = [c for c in r if _local(c.tag) == "part"]
        per_page.append(ps)
    for pi in range(nparts):
        out_part = ET.Element(q("part"))
        pid = None
        num = 0
        for ps in per_page:
            if pi >= len(ps):
                continue
            sub = ps[pi]
            if pid is None:
                pid = sub.get("id") or f"P{pi+1}"
            for m in sub:
                if _local(m.tag) != "measure":
                    continue
                num += 1
                m.set("number", str(num))
                out_part.append(m)
        out_part.set("id", pid or f"P{pi+1}")
        parts.append(out_part)

    root = ET.Element(q("score-partwise"), {"version": roots[0].get("version", "3.1")})
    pl = [c for c in roots[0] if _local(c.tag) == "part-list"]
    if pl:
        root.append(pl[0])
    for p in parts:
        root.append(p)
    ET.indent(root, space="  ") if hasattr(ET, "indent") else None
    out_path = pathlib.Path(out_path) if out_path else \
        tmpdir("sky15_merge") / "merged.musicxml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    LAST_MERGE.update(paths=[str(p) for p in paths], divisions=list(divs),
                      unified=int(g), warnings=list(msgs), out=str(out_path))
    return out_path


# ---------------------------------------------------------------- 跑 Audiveris
_ADVICE = (
    (re.compile(r"OutOfMemoryError|GC overhead|heap space", re.I),
     "JVM 堆不够：调大 -Xmx（extra_env={'JAVA_TOOL_OPTIONS':'-Xmx4g'}），"
     "或降低放大倍数/把长图切成两半"),
    # 实测遇到过：892x1394 的糊照片（interline≈7.5px）报 "Error in export"
    # + "Exit forced"，其实跟像素上限、内存都没关系 —— 是谱线太细认不出。
    (re.compile(r"Error in export|Cannot export", re.I),
     "识别环节中途失败（常见于**图太小/太糊**：谱线只有 1~2px 时会认不出音符）："
     "先 `preprocess_image` 把 interline 放大到 15~20px 再跑；"
     "确认它报的 interline 不是 None"),
    (re.compile(r"Exit forced", re.I),
     "Audiveris 被强杀：多数是图幅超 16M 像素或内存不足。"
     "把 upscale 调小，或先用 preprocess_image(max_pixels=12_000_000)"),
    (re.compile(r"No sheet|no sheet|Empty sheet|not a sheet", re.I),
     "没找到谱表：图里可能没有五线谱，或者倾斜/透视太严重。"
     "先 preprocess_image 看 actions 里有没有纠斜/透视校正"),
    (re.compile(r"excess|too long", re.I),
     "声部时值溢出（Audiveris 自己把音塞到小节外）：这类谱子识别出来会出现"
     "同一时刻堆太多音，建议提高分辨率重跑，或手工修 MusicXML"),
    (re.compile(r"Too many|max.*limit|LIMIT", re.I),
     "触到 Audiveris 的数量上限：分页/裁掉空白边距后重试"),
    (re.compile(r"SIGSEGV|hs_err|A fatal error has been detected", re.I),
     "JVM 崩了：换 -Xmx 后重试；仍然崩就把该页单独切出来跑"),
)


def diagnose(log_text, returncode=None):
    """按日志特征给"下一步该干什么"。返回 [] 表示没有命中已知症状。"""
    out = []
    for pat, advice in _ADVICE:
        if pat.search(log_text or ""):
            out.append(advice)
    if not out and returncode not in (None, 0):
        out.append(f"返回码 {returncode}，日志里没有已知特征：把日志尾部贴出来，"
                   f"或先用 preprocess_image 把图摆正再试")
    return out


def _run_audiveris(src, outdir, timeout=600, extra_env=None, keep_log=True):
    """跑一次 Audiveris，返回 (mxls, log_text, returncode)。"""
    exe = _require_exe()
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-batch", "-export", "-output", str(outdir), str(src)]
    env = dict(os.environ)
    env.setdefault("JAVA_TOOL_OPTIONS", "-Xmx2g")
    if extra_env:
        env.update(extra_env)
    t0 = time.time()
    try:
        # `text=True` 不写 encoding 就用系统默认编码（Windows 上是 cp936/GBK），
        # 而 Audiveris 是 Java 程序、输出 UTF-8。中文谱名/中文日志一旦出现在
        # stdout 里就会解成乱码，运气不好直接 UnicodeDecodeError。
        # 另一处（homr，:1160 附近）一直是显式 utf-8 的，这里对齐。
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Audiveris 超时（>{timeout}s）。\n"
            "  1) 图片可能太大：降低 upscale / max_pixels\n"
            "  2) 单页谱子正常只要 3~10 秒，超时多半是卡在某一页：分页重跑\n"
            "  3) 也可以调大 timeout")
    dt = time.time() - t0
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if keep_log:
        try:
            ts = time.strftime("%Y%m%dT%H%M")
            (outdir / f"{pathlib.Path(src).stem}-{ts}.log").write_text(
                log, encoding="utf-8")
        except OSError:
            pass
    cands = []
    for pat in ("*.mxl", "*.musicxml", "*.xml"):
        cands += [p for p in outdir.rglob(pat)]
    mxl = [p for p in cands if "META-INF" not in p.parts]
    mxl.sort(key=lambda p: p.stat().st_size, reverse=True)
    return mxl, log, proc.returncode, dt


# ---------------------------------------------------------------- 对外主入口
def to_musicxml(image_path, outdir=None, timeout=600, extra_env=None,
                upscale=None, preprocess=None, pages=None, dpi=300,
                max_pages=None, target_interline=TARGET_INTERLINE,
                max_pixels=MAX_PIXELS, on_step=None):
    """图片/PDF → MusicXML 路径。失败抛 RuntimeError（带日志和下一步建议）。

    新增（都向后兼容，老调用 `to_musicxml(p, outdir=..., timeout=...)` 不变）：
      `upscale`     固定放大倍数；None = 按五线间距自动定（见 plan_scale）
      `preprocess`  True/False / None=auto（只在测出倾斜或透视时才动）
      `pages`       PDF 分页策略：None=多页就逐页；"book"=整个 PDF 交给 Audiveris
      `dpi`          PDF 栅格化分辨率（默认 300）
      `max_pages`   只处理前 N 页（调试用）
      `on_step`     回调 (msg, info)，方便 CLI/Web 显示进度
    """
    src = pathlib.Path(image_path)
    if not src.exists():
        raise FileNotFoundError(src)
    outdir = pathlib.Path(outdir or tmpdir("sky15_omr"))
    outdir.mkdir(parents=True, exist_ok=True)
    step = on_step or (lambda *a, **k: None)

    def _pp(one, sub):
        pre = preprocess
        if pre is False:
            return {"path": str(one), "actions": []}
        return preprocess_image(
            one, outdir / sub, deskew=("auto" if pre is None else pre),
            warp=("auto" if pre is None else pre),
            denoise=("auto" if pre is None else pre),
            upscale=upscale, target_interline=target_interline,
            max_pixels=max_pixels)

    # --- 输入分页 ---
    if src.suffix.lower() in PDF_EXT and pages != "book":
        n = pdf_page_count(src)
        if n > 1:
            imgs = pdf_to_images(src, outdir / "pdf", dpi=dpi,
                                 last=max_pages)
            step(f"PDF {n} 页 → 逐页识别", {"pages": len(imgs)})
            mxls = []
            for i, img in enumerate(imgs, 1):
                info = _pp(img, f"p{i:03d}")
                step(f"第 {i}/{len(imgs)} 页", info)
                got, log, rc, dt = _run_audiveris(
                    info["path"], outdir / f"p{i:03d}", timeout, extra_env)
                if not got:
                    adv = diagnose(log, rc) or ["把这一页单独导出来看是不是空白页"]
                    raise RuntimeError(
                        f"第 {i} 页没产出 MusicXML（返回码 {rc}，{dt:.1f}s）。\n"
                        "下一步：\n" + "\n".join(f"  · {a}" for a in adv) +
                        f"\n  日志尾部：\n{log[-800:].strip()}")
                mxls.append(got[0])
                step(f"第 {i} 页完成", {"mxl": str(got[0]), "sec": round(dt, 1)})
            if len(mxls) == 1:
                return mxls[0]
            merged = merge_musicxml(mxls, outdir / (src.stem + "_merged.musicxml"))
            step("逐页产物已合并", {"mxl": str(merged), "pages": len(mxls)})
            return merged
        if n == 1 and pages != "book":
            imgs = pdf_to_images(src, outdir / "pdf", dpi=dpi, last=1)
            src = imgs[0]

    # --- 单张图（或整个 PDF 交给 Audiveris）---
    if src.suffix.lower() in BITMAP_EXT:
        info = _pp(src, "pp")
        step("预处理", info)
        work = info["path"]
    else:
        work = str(src)

    got, log, rc, dt = _run_audiveris(work, outdir, timeout, extra_env)
    step("识别完成", {"sec": round(dt, 1), "files": len(got)})
    if not got:
        adv = diagnose(log, rc)
        raise RuntimeError(
            "Audiveris 跑完了但没产出 MusicXML。\n"
            f"  返回码 {rc}\n"
            + ("下一步：\n" + "\n".join(f"  · {a}" for a in adv) + "\n" if adv else "")
            + f"  输出尾部：\n{log[-800:].strip()}")
    if len(got) > 1:
        # 多页 PDF 交给 Audiveris 时会每页一个 MXL，按文件名里的页码排序合并
        def _pageno(p):
            m = re.search(r"#(\d+)", p.name) or re.search(r"_(\d+)\.", p.name)
            return int(m.group(1)) if m else 0
        got.sort(key=lambda p: (_pageno(p), -p.stat().st_size))
        if len(got) > 1 and all(_pageno(p) for p in got):
            merged = merge_musicxml(got, outdir / (src.stem + "_merged.musicxml"))
            step("多页产物已合并", {"mxl": str(merged), "pages": len(got)})
            return merged
    return got[0]


def find_homr():
    """找 homr 可执行文件。找不到返回 None。

    homr (github.com/liebharc/homr) 是 2024-2025 年的新 OMR：分割网络用
    oemer 的 UNet，节奏/拍号/小节切分换成 TrOMR transformer。实测同一张
    钢琴谱，它能把拍号读对（Audiveris 根本没识别出拍号符号）、小节数对、
    覆盖率 100%，纯 CPU 6~7 秒。
    """
    env = os.environ.get(HOMR_ENV)
    if env and pathlib.Path(env).exists():
        return pathlib.Path(env)
    for p in HOMR_CANDIDATES:
        if pathlib.Path(p).exists():
            return pathlib.Path(p)
    got = shutil.which("homr")
    return pathlib.Path(got) if got else None


def homr_available() -> bool:
    return find_homr() is not None


def _homr_run_root() -> pathlib.Path:
    """homr 的运行目录 —— 交给 `tmp.py` 统一管。

    **必须在工作区里、不能用系统 %TEMP%**：rapidocr 会把 OCR 用的中间图
    `page_tesseract_input.png` 写在自己的 cwd 下再去读，在 Windows 的
    %TEMP% 里这一步会失败（实测三种 cwd 全报 LoadImageError，同一张图
    放到工作区就正常）。

    原来硬编码 `sky15/out/.homr_run/`，绕开了 `tmp.py` 的清理规则：目录
    只增不删、也不在 `out/tmp/` 里，看 `out` 目录时像个野目录。现在走
    `tmpdir()`（→ `sky15/out/tmp/sky15_homr_run_xxxx/`，三天前的自动清）。
    """
    try:
        return tmpdir("sky15_homr_run")
    except OSError:                    # 工作区不可写（只读盘/权限）：退回系统 temp
        root = pathlib.Path(tempfile.gettempdir()) / "sky15_homr_run"
        root.mkdir(parents=True, exist_ok=True)
        return root


# homr --debug 会把「它认出来的音符」画回原图（绿/蓝框）。最近一次的结果记在这，
# 给前端做「看它到底认到了哪」用：漏音的地方一眼就能看见，比只看输出谱强。
LAST_DEBUG = {"notes": None, "all": []}

# 最近一次 homr 跑了几个 PDF 页、有哪些话要说（多页/合并/丢页都记在这）
LAST_HOMR = {"pages": None, "warnings": [], "out": None}


def to_musicxml_homr(image_path, outdir=None, timeout=420, on_step=None,
                     keep_debug=False, warn=None):
    """图片 → MusicXML（用 homr）。

    homr 只会把 xml 写在图片旁边，所以先把图复制成纯 ascii 名字（它用
    cv2.imread 读图，中文路径会失败），跑完再搬回来。

    PDF 多页：**每一页都跑**，跑完 `merge_musicxml` 合成一份（原来
    `last=1` 只认第 1 页，第 2 页起静默丢失 —— 而 `to_musicxml_best`
    默认就是 homr，等于多页 PDF 一律只剩第一页）。页数、合并提示都进
    `warn` / `LAST_HOMR["warnings"]`。

    keep_debug=True 时额外要一张「识别结果叠在原图上」的图（--debug 产出的
    `*_notes.png`），路径写进 `LAST_DEBUG["notes"]`，并且拷到 outdir 里。
    """
    exe = find_homr()
    if not exe:
        raise RuntimeError(
            "没找到 homr。装法：\n"
            "  python -m venv %LOCALAPPDATA%\\sky15\\homr\n"
            "  %LOCALAPPDATA%\\sky15\\homr\\Scripts\\python.exe -m pip install \"homr[cpu]\"\n"
            "  %LOCALAPPDATA%\\sky15\\homr\\Scripts\\homr.exe --init   # 下 157 MB 模型\n"
            "然后用环境变量 SKY15_HOMR 指向 homr.exe。")
    src = pathlib.Path(image_path)
    if not src.exists():
        raise FileNotFoundError(src)
    outdir = pathlib.Path(outdir or tmpdir("sky15_homr"))
    outdir.mkdir(parents=True, exist_ok=True)
    step = on_step or (lambda *a, **k: None)
    msgs = []
    LAST_HOMR.update(pages=None, warnings=[], out=None)

    def say(msg):
        msgs.append(msg)
        if warn:
            warn(msg)

    td = _homr_run_root() / f"r{int(time.time() * 1000) % 10 ** 9}"
    td.mkdir(parents=True, exist_ok=True)
    try:
        # --- 分页：PDF 每一页都要认，不许只认第 1 页 ---
        # 原来这里是 `pdf_to_images(..., last=1)` + 只跑一次 homr：多页 PDF
        # 第 2 页起**静默消失**（`to_musicxml_best` 默认 engines=("homr",)，
        # 等于整个识谱链路默认丢掉后面的页）。现在逐页跑，跑完合并。
        if src.suffix.lower() in PDF_EXT:
            imgs = pdf_to_images(src, td / "pdf", dpi=400)
        else:
            imgs = [src]
        pages = max(1, len(imgs))
        if pages > 1:
            say(f"这份 PDF 有 {pages} 页：逐页识别后合并成一份"
                f"（每页超时上限 {timeout}s）")
            step(f"PDF {pages} 页 → 逐页 homr", {"pages": pages})

        # homr 自带的 --debug 会把每个音标成 `note12` 压在谱面上，密到看不清。
        # 走 tools/homr_overlay.py（同一个 venv 的 python 跑）：它先把画法换成
        # 「只画音符头的小圈」，再调 homr，出的叠图干净、能直接给用户看。
        overlay_py = pathlib.Path(__file__).resolve().parent / "tools" / "homr_overlay.py"
        venv_py = pathlib.Path(str(exe)).parent / "python.exe"
        use_overlay = keep_debug and overlay_py.exists() and venv_py.exists()
        LAST_DEBUG["notes"] = None
        LAST_DEBUG["all"] = []

        made, secs = [], 0.0
        for i, img in enumerate(imgs, 1):
            sub = td / f"p{i:03d}"
            sub.mkdir(parents=True, exist_ok=True)
            work = sub / "page.png"
            if src.suffix.lower() in PDF_EXT:
                shutil.copyfile(img, work)
            elif src.suffix.lower() != ".png":
                from PIL import Image
                Image.open(src).convert("RGB").save(work)
            else:
                shutil.copyfile(src, work)
            where = "homr 识别中" if pages == 1 else f"homr 识别中（第 {i}/{pages} 页）"
            step(where, {"page": i, "pages": pages})
            t0 = time.time()
            if use_overlay:
                cmd = [str(venv_py), str(overlay_py), str(work)]
            else:
                cmd = [str(exe), str(work)] + (["--debug"] if keep_debug else [])
            proc = subprocess.run(cmd, capture_output=True,
                                  text=True, timeout=timeout, cwd=str(sub),
                                  encoding="utf-8", errors="replace")
            dt = time.time() - t0
            secs += dt
            if keep_debug:
                # `write_all_bounding_boxes_alternating_colors("notes", ...)` 落成
                # `page_debug_<n>_notes.png`，是全部认出来的音符（含符干位置）。
                found = sorted(sub.glob("*notes*.png"))
                if found:
                    suffix = "_homr_notes.png" if i == 1 else f"_homr_notes_p{i}.png"
                    dst_dbg = outdir / (src.stem + suffix)
                    try:
                        shutil.copyfile(found[-1], dst_dbg)
                        if i == 1:
                            LAST_DEBUG["notes"] = str(dst_dbg)
                    except OSError:
                        pass
                LAST_DEBUG["all"] += [str(p) for p in sorted(sub.glob("*_debug_*.png"))]
            got = sorted(sub.glob("*.musicxml"))
            if not got:
                raise RuntimeError(
                    f"第 {i}/{pages} 页 homr 跑完了但没出 MusicXML"
                    f"（返回码 {proc.returncode}，{dt:.1f}s）\n"
                    f"{(proc.stderr or proc.stdout or '')[-700:].strip()}")
            made.append(got[0])
            if pages > 1:
                step(f"第 {i}/{pages} 页完成（{dt:.1f}s）", {"sec": round(dt, 1)})
        if not made:
            raise RuntimeError("homr 没有产出任何 MusicXML")

        dst = outdir / (src.stem + "_homr.musicxml")
        if len(made) == 1:
            shutil.copyfile(made[0], dst)
        else:
            for i, one in enumerate(made, 1):
                shutil.copyfile(one, outdir / f"{src.stem}_homr_p{i}.musicxml")
            dst = pathlib.Path(merge_musicxml(made, dst, warn=warn))
            step(f"{len(made)} 页已合并", {"mxl": str(dst), "pages": len(made)})
        LAST_HOMR.update(pages=pages, warnings=list(msgs), out=str(dst))
        step(f"homr 完成（{secs:.1f}s）", {"sec": round(secs, 1), "pages": pages})
        return dst
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _cover_of_events(ev, span=None) -> float:
    """有音时间占比（10ms 网格）。识谱好不好的第一判据。

    分母取 **`span`（谱面总时长）** 和"最后一个音的结束"里大的那个。
    原来只用后者：一段只有 3 个音、铺在第一小节的碎谱也能拿 cover≈1.0
    （审计 A 实测 16 小节的谱只认出 3 个音，cover 0.989、总分 0.995，
    赢过真正读全的那份）。
    """
    if not ev:
        return 0.0
    last = max(float(e["start"]) + float(e.get("dur") or 0) for e in ev)
    total = max(last, float(span or 0.0))
    if total <= 0:
        return 0.0
    n = int(total / 0.01) + 2
    grid = bytearray(n)
    for e in ev:
        a = max(0, int(float(e["start"]) / 0.01))
        b = min(n, int((float(e["start"]) + float(e.get("dur") or 0)) / 0.01))
        for i in range(a, b):
            grid[i] = 1
    return sum(grid) / float(n)


def _bar_fill(measures) -> float:
    """小节填充率：每小节"内容终点"离小节线有多远（1.0 = 都正好落在线上）。

    OMR 最常见的两种错法都在这里露头：漏音（内容短一截）和串行
    （内容冲过小节线）。只看覆盖率看不出这两种。
    """
    dev, n = 0.0, 0
    for m in measures or []:
        length = float(m.get("length") or 0.0)
        if length <= 0:
            continue
        end = max((float(it["onset"]) + float(it["dur"]) for it in m["items"]),
                  default=0.0)
        if end <= 0:
            continue
        dev += min(1.0, abs(end - length) / length)
        n += 1
    return 1.0 - dev / n if n else 0.0


def _rate_ok(rate) -> float:
    """起音速率的合理性（1.0 = 正常）。1.5~8 起音/秒是正常钢琴谱：
    低于 1.5 基本是"被拉伸"（小节线读错，整首拉长），高于 8 是虚音/重影。
    """
    if rate <= 0:
        return 0.0
    if rate < 1.5:
        return rate / 1.5
    if rate <= 8.0:
        return 1.0
    return 8.0 / rate


def score_musicxml(path) -> dict:
    """给一份 MusicXML 打分，用来在同一张图的多次识别里挑最好的一份。

    四个判据（都有实测依据），加权几何平均到 0~1：
      cover      有音时间占比（分母 = 谱面总时长）。覆盖率一塌，听感就是断断续续。
      bar        小节填充率。漏音/串行都会让它掉。
      dens       音密度（每拍几个音，≥0.5 算正常）。
      rate_ok    起音速率是否在 1.5~8/s。把小节线读错会把整首拉长（实测同一张
                 图 4/4 读出 67.8s、2/4 读出 28.5s），拉长后起音速率明显偏低。

    **为什么不用"覆盖率 + 一点加分"：** 那样一来"每小节一个全音符"的错谱
    覆盖率反而更高（0.9933 赢过对谱 0.9740），因为它把所有时间都占满了。
    改成乘积以后，覆盖率再高，起音速率/密度不合理就整体打折。
    """
    from . import musicxml, score_semantics as _sem
    got = musicxml.parse_full(str(path))
    ev = got.to_seconds()
    total_beats = float(got.total_beats or 0.0)
    span = (_sem.seconds_at(total_beats, _sem.normalize_tempo_map(got.tempo_map))
            if total_beats > 0 else 0.0)
    cov = _cover_of_events(ev, span)
    last = max((float(e["start"]) + float(e.get("dur") or 0) for e in ev),
               default=0.0)
    ons = len({round(float(e["start"]), 3) for e in ev})
    rate = ons / last if last > 0 else 0.0
    bar = _bar_fill(got.parts[0]["measures"] if got.parts else [])
    dens = min(1.0, len(ev) / max(total_beats, 1e-6) / 0.5)
    rok = _rate_ok(rate)
    score = (cov ** 0.5) * (bar ** 0.15) * (dens ** 0.15) * (rok ** 0.2)
    return {"notes": len(ev), "cover": round(cov, 4),
            "onset_rate": round(rate, 2), "dur": round(last, 1),
            "bar_fill": round(bar, 4), "density": round(dens, 4),
            "rate_ok": round(rok, 3), "total_beats": round(total_beats, 2),
            "empty": bool(getattr(got, "empty", not ev)),
            "time_sig": got.time_sig, "key": got.key, "tempo": got.tempo,
            "score": round(score, 5)}


def _too_weak(info, min_notes=20, min_cover=0.5):
    """识别结果太寒酸时给一句中文说明（None = 过闸）。

    只有覆盖率没有绝对下限 = "3 个音、覆盖 5%" 也算成功（审计 A 实测）。
    `min_notes` / `min_cover` 传 0 可以关掉对应那一条。自定义 scorer 返回的
    dict 里没有 notes/cover 时跳过那一条，不动老调用方。
    """
    bad = []
    if min_notes and "notes" in info and int(info.get("notes") or 0) < min_notes:
        bad.append(f"只认出 {int(info.get('notes') or 0)} 个音（下限 {min_notes}）")
    if min_cover and "cover" in info and float(info.get("cover") or 0.0) < min_cover:
        bad.append(f"有音时间只占 {100 * float(info.get('cover') or 0.0):.1f}%"
                   f"（下限 {100 * min_cover:.0f}%）")
    if not bad:
        return None
    return "识别结果太不完整：" + "，".join(bad)


def to_musicxml_best(image_path, outdir=None, scales=(1.0, 1.5, 2.0, None),
                     scorer=None, on_step=None, engines=("homr",),
                     keep_debug=False, min_notes=20, min_cover=0.5, **kw):
    """图片 → MusicXML，多个引擎/多个放大倍数都跑一遍，取最好的那份。

    默认只用 homr（2026-10-23 用户定的：只留 homr，Audiveris 从识别流程里撤掉）。
    实测同一张 1439×1797 的钢琴谱：
      homr      原尺寸  249 音、2/4 拍号读对、覆盖 99.9%、26.7 秒、6~8 秒跑完
      Audiveris 原尺寸 317 音、覆盖 99.5%，但拍号一个都没认出来，整首拉长到 67.8 秒
      Audiveris 放大 2× 只剩 231 音、覆盖 71.6%
    Audiveris 留着只为兼容老脚本：engines=("audiveris",) 能显式点名。

    参数
      engines  要试的引擎，默认 ("homr",)——没装就报错，不悄悄降级
      scales   Audiveris 的放大倍数（默认引擎下用不到）
      scorer   回调 (mxl_path) -> dict，含 `score` 键，越大越好
      min_notes / min_cover
               绝对下限（默认 20 音 / 覆盖率 50%）。低于就算这次识别失败，
               抛 RuntimeError 说清差在哪。传 0 关掉对应那一条——审计 A 实测
               "3 个音、覆盖 5%" 原来会被当成成功交给用户。
    返回
      (最好的 mxl 路径, 明细 dict)
    """
    src = pathlib.Path(image_path)
    if not src.exists():
        raise FileNotFoundError(src)
    outdir = pathlib.Path(outdir or tmpdir("sky15_omr"))
    outdir.mkdir(parents=True, exist_ok=True)
    step = on_step or (lambda *a, **k: None)

    def _score(p):
        if scorer is not None:
            return scorer(p)
        return score_musicxml(p)

    if engines is None:
        engines = ("homr",)
    engines = tuple(engines)
    if "homr" in engines and not homr_available():
        raise RuntimeError(
            "没找到 homr（现在只用它识谱）。装法：\n"
            "  python -m venv %LOCALAPPDATA%\\sky15\\homr\n"
            "  %LOCALAPPDATA%\\sky15\\homr\\Scripts\\python.exe -m pip install \"homr[cpu]\"\n"
            "  %LOCALAPPDATA%\\sky15\\homr\\Scripts\\homr.exe --init\n"
            "然后用环境变量 SKY15_HOMR 指向 homr.exe。")

    tries, best = [], None

    def _try(tag, engine, fn):
        nonlocal best
        try:
            step(f"识别（{tag}）", {"engine": engine})
            mx = fn()
            info = _score(mx)
            info.update(engine=engine, tag=tag, mxl=str(mx))
            tries.append(info)
            step(f"{tag}：{info.get('notes')} 音 {info.get('time_sig')}，覆盖 "
                 f"{100 * info.get('cover', 0):.1f}%", info)
            if best is None or info["score"] > best["score"]:
                best = info
        except Exception as exc:                    # 某一档失败不该整体失败
            tries.append({"tag": tag, "engine": engine,
                          "error": f"{type(exc).__name__}: {exc}"})
            step(f"{tag} 失败：{type(exc).__name__}", {})

    if "homr" in engines:
        _try("homr", "homr",
             lambda: to_musicxml_homr(src, outdir / "s_homr", on_step=step,
                                      keep_debug=keep_debug))
        # 多页 PDF 的页数/合并提示挂在候选上（best 和 tries 里是同一个 dict，
        # 改一处两边都看得到）。审计 A 致命 4：只识第 1 页、静默丢页。
        for t in tries:
            if t.get("engine") == "homr" and "error" not in t:
                t["pages"] = LAST_HOMR.get("pages")
                if LAST_HOMR["warnings"]:
                    t["warnings"] = list(LAST_HOMR["warnings"])
        if best is not None and best.get("engine") == "homr" and LAST_DEBUG["notes"]:
            best["debug_notes"] = LAST_DEBUG["notes"]

    # homr 读得很干净就直接交卷，不再花 30~40 秒把 Audiveris 跑四遍。
    # 判据：覆盖率 ≥99.5% 且起音速率在 1.5~8/s（速率过低是被拉伸，过高是虚音），
    # 且过了绝对下限（不然"3 个音、覆盖 5%"这种残谱也会被当成够干净）。
    early = (best is not None and best.get("engine") == "homr"
             and best.get("cover", 0) >= 0.995
             and 1.5 <= best.get("onset_rate", 0) <= 8.0
             and _too_weak(best, min_notes, min_cover) is None)
    if early:
        step("homr 读得够干净，跳过其他引擎", {})
    elif "audiveris" in engines:
        for sc in scales:
            tag = "audiveris-auto" if sc is None else f"audiveris-x{sc:g}"
            _try(tag, "audiveris",
                 (lambda sc=sc, tag=tag: to_musicxml(
                     src, outdir=outdir / f"s_{tag}", upscale=sc, **kw)))

    if best is None:
        raise RuntimeError("所有识别都失败了：\n" + "\n".join(
            f"  · {t.get('tag')}: {t.get('error', '')[:200]}" for t in tries))
    # 绝对下限：光看覆盖率，"每小节一个全音符"的错谱反而能赢（审计 A 实测
    # 0.9933 > 0.9740），"3 个音、覆盖 5%"也算成功。这里兜住下限，宁可按
    # 失败报出来，也不给用户一份"看着成功、其实是残谱"的结果。
    weak = _too_weak(best, min_notes, min_cover)
    if weak:
        # 注意别让这条信息被上下文吃掉：web 那边只把「最后一行」甩给用户，
        # 所以结论句放最后，原因/下一步放前面。
        raise RuntimeError(
            f"识谱结果太不完整（引擎：{best.get('engine')}）。\n"
            "  可能原因：图太小/太糊、不是五线谱、裁掉了谱面。\n"
            "  下一步：preprocess_image 把 interline 放大到 15~20px 再跑；"
            "确认是整页谱而不是局部截图。\n"
            "  确认这就是一份短谱的话，显式放宽：min_notes=0, min_cover=0。\n"
            + weak)
    best["tries"] = [{k: v for k, v in t.items() if k != "mxl"} for t in tries]
    step(f"选用 {best['engine']}（覆盖 {100 * best['cover']:.1f}%，"
         f"{best['notes']} 音）", best)
    return pathlib.Path(best["mxl"]), best


def to_smc(image_path, **kw):
    """一步到位：图片 → MusicXML → SMC"""
    from . import musicxml, arrange
    mx = to_musicxml(image_path, outdir=kw.pop("outdir", None),
                     timeout=kw.pop("timeout", 600),
                     upscale=kw.pop("upscale", None),
                     preprocess=kw.pop("preprocess", None),
                     pages=kw.pop("pages", None),
                     dpi=kw.pop("dpi", 300))
    ev, tempo, ts, key = musicxml.parse(mx)
    title = kw.pop("title", "") or pathlib.Path(image_path).stem
    return arrange.arrange(ev, tempo=kw.pop("tempo", None) or tempo,
                           time_sig=kw.pop("time_sig", None) or ts,
                           key=kw.pop("key", None) or key, title=title, **kw), mx
