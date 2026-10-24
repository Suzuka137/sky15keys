# -*- coding: utf-8 -*-
"""命令行入口。

    py -m sky15 audio 某首歌.mp3 -o 输出.smc      # 音频/视频 → 15 键谱
    py -m sky15 midi  已转谱.mid  -o 输出.smc      # 已有 MIDI → 15 键谱
    py -m sky15 smc   原谱.smc   -o 输出.smc      # 重排既有 SMC（换调/简化）
    py -m sky15 test                              # 回环自测
    py -m sky15 doctor                            # 体检：依赖/显卡/格式
"""
import argparse, os, sys

from . import arrange, layout, midifile, smc


def _write(text, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已写出 {out}  ({len(text)} 字符)")


def cmd_audio(a):
    from . import transcribe, beats
    print("转谱中…")
    ev, _bpm, backend = transcribe.transcribe(a.input, backend=a.backend)
    print(f"  后端 {backend}，收到 {len(ev)} 个音符")

    tempo, offset, warp, t_exact = a.tempo, 0.0, None, None
    if tempo is None:
        est, off, why = beats.select_grid(ev, tempo_hint=a.tempo_hint)
        tempo, offset = max(30, int(round(est))), off
        hit = beats.grid_quality([e["start"] for e in ev], tempo, off)
        conf = "高" if hit >= 0.85 else "中" if hit >= 0.70 else "低"
        print(f"  自动测速 {est:.1f} BPM → 取 {tempo}，相位 {off:+.3f}s，"
              f"命中率 {hit*100:.0f}%（置信度 {conf}，{why.get('reason','')}）")
        if hit < 0.70:
            print("  ⚠ 速度多半估错了：请用 --tempo 明确指定。"
                  "实测速度差 2% 就能让三分钟的曲子漂 20 格，整首错位。")
    else:
        print(f"  使用指定速度 {tempo} BPM")

    # 速度起伏（rubato）：默认 auto —— 恒速网格对得上就不动，对不上才换分段网格
    from . import warptrack
    warp, winfo = warptrack.autowarp(ev, tempo=float(tempo),
                                     mode=getattr(a, "rubato", "auto"))
    if warp is not None:
        t_exact = float(winfo.get("tempo") or tempo)
        print(f"  跟随速度起伏：周期 {winfo.get('p_med', 0)*1000:.0f} ms/"
              f"{winfo.get('beats', 0)} 个拍点，网格置信度 {winfo.get('gq', 0):.2f}"
              f"（闸门 <{warptrack.GQ_USE:.2f}）")

    text = arrange.arrange(ev, tempo=tempo, time_sig=a.time_sig, octave_fit=getattr(a, "octave", "auto"),
                           key=a.key, title=a.title or os.path.splitext(
                               os.path.basename(a.input))[0],
                           max_notes=a.max_notes, offset=offset, warp=warp,
                           **({"tempo_exact": t_exact} if t_exact else {}))
    _write(text, a.output)

    if getattr(a, "publish", False):
        from . import publish
        publish.publish(a.output, title=a.title or None)


def cmd_midi(a):
    d = midifile.read(a.input)
    ev = d["events"]
    print(f"MIDI: {len(ev)} 个音符，{d['tempo']} BPM，{d['time_sig']}")
    text = arrange.arrange(ev, tempo=a.tempo or d["tempo"], time_sig=a.time_sig or d["time_sig"], octave_fit=getattr(a, "octave", "auto"),
                           key=a.key, title=a.title or os.path.splitext(os.path.basename(a.input))[0],
                           max_notes=a.max_notes)
    _write(text, a.output)


_KEY_ENH = {"C#": "Db", "D#": "Eb", "E#": "F", "F#": "Gb",
            "G#": "Ab", "A#": "Bb", "B#": "C"}


def _norm_key(k, default="C"):
    """谱面里的调名归一化成 layout.KEY_MIDI 认识的写法。

    KEY_MIDI 只有降号那 12 个键名，所以曲子写成 `F#`/`G#`/`c` 的时候
    cmd_smc 会直接 KeyError 崩栈 —— 而同一份文件走网页端不会崩。
    """
    from . import layout
    s = str(k or "").strip()
    for a_, b_ in (("♯", "#"), ("♭", "b"), ("＃", "#"), (" ", ""), ("调", "")):
        s = s.replace(a_, b_)
    if not s:
        return default
    s = s[0].upper() + s[1:]
    if s in layout.KEY_MIDI:
        return s
    if s in _KEY_ENH:
        return _KEY_ENH[s]
    # `C大调` / `A minor` 这类：只认开头的音名，认不出就回默认
    head = s[0].upper()
    return head if head in layout.KEY_MIDI else default


def cmd_smc(a):
    try:
        text = open(a.input, encoding="utf-8-sig").read()
    except UnicodeDecodeError:
        # Windows 上老谱子多半是 GBK。服务端上传路径本来就有这层兜底（utf-8-sig → gbk），
        # CLI 这条以前直接崩栈。
        text = open(a.input, encoding="gbk", errors="replace").read()
    meta = smc.parse_meta(text)
    groups = smc.parse_groups(text)
    # 格长必须按「最终要用的速度」算。以前固定用谱面里的速度换算成秒，
    # 再交给 arrange(tempo=--tempo)：时间轴按旧速度、量化按新速度，
    # 整首错格。现在按目标速度定格长，音符仍然落在同一格序号上。
    use_tempo = a.tempo or meta["tempo"]
    key = _norm_key(a.key or meta["key"])
    slot = 60.0 / use_tempo / 2.0
    ev = []
    for i, g in enumerate(groups):
        for c in g[:3]:
            if c in "0<> ":
                continue
            ev.append({"start": i * slot, "dur": slot,
                       "pitch": layout.char_pitch(c, key), "vel": 90})
    out = arrange.arrange(ev, tempo=use_tempo, time_sig=meta["time_sig"], octave_fit=getattr(a, "octave", "auto"),
                          key=key, title=a.title or meta["title"],
                          has8vb=meta["has8vb"], max_notes=a.max_notes)
    _write(out, a.output)


def cmd_render(a):
    """SMC → 图片 / PDF"""
    from . import render
    made = render.render_file(a.input, a.output, dpi=a.dpi, theme=a.theme,
                              paginate=not a.long)
    print(f"共 {len(made)} 个文件：")
    for f in made:
        print(f"  {f}  ({os.path.getsize(f)/1024:.0f} KB)")
    if getattr(a, "publish", False):
        from . import publish
        publish.publish(a.input, title=a.title or None)


def cmd_musicxml(a):
    """MusicXML / .mxl → 15 键谱（五线谱的通用交换格式）"""
    from . import musicxml
    got = musicxml.parse_full(a.input)
    ev, tempo, ts, key = got.events, got.tempo, got.time_sig, got.key
    print(f"MusicXML：{len(ev)} 个音，{tempo} BPM，{ts}，调 {key}")
    if got.tempo_map and len(got.tempo_map) > 1:
        print(f"  变速：{got.describe_tempo()}")
    if not ev:
        print("没解析出音符 —— 确认文件是 score-partwise 格式的 MusicXML")
        return 1
    use_tempo = a.tempo or tempo
    # 没手工指定 tempo 时按谱面的速度分段表积秒（中途变速才对得上）
    secs = musicxml.to_seconds(ev, use_tempo) if a.tempo else got.to_seconds()
    out = arrange.arrange(secs, tempo=use_tempo,
                          time_sig=a.time_sig or ts,
                          key=a.key or key, title=a.title or
                          os.path.splitext(os.path.basename(a.input))[0],
                          max_notes=a.max_notes)
    _write(out, a.output)
    if getattr(a, "publish", False):
        from . import publish
        publish.publish(a.output, title=a.title or None)


def cmd_omr(a):
    """五线谱图片/PDF → OMR → MusicXML → 15 键谱"""
    from . import omr
    if not omr.available():
        print("找不到 Audiveris。装法：")
        print("  1) 下 https://github.com/audiveris/audiveris/releases 里")
        print("     Audiveris-x.y.z-windowsConsole-x86_64.msi（自带 Java）")
        print("  2) 或设环境变量 SKY15_AUDIVERIS 指向可执行文件")
        return
    print(f"用 {omr.find_audiveris()}")
    print("识别中（图片越大越慢，几分钟正常）…")
    mx = omr.to_musicxml(a.input, outdir=a.workdir, timeout=a.timeout)
    print(f"  MusicXML：{mx}")
    from . import musicxml
    got = musicxml.parse_full(mx)
    ev, tempo, ts, key = got.events, got.tempo, got.time_sig, got.key
    print(f"  音符 {len(ev)}，{tempo} BPM，{ts}，调 {key}")
    if got.tempo_map and len(got.tempo_map) > 1:
        print(f"  变速：{got.describe_tempo()}")
    use_tempo = a.tempo or tempo
    secs = musicxml.to_seconds(ev, use_tempo) if a.tempo else got.to_seconds()
    out = arrange.arrange(secs, tempo=use_tempo,
                          time_sig=a.time_sig or ts,
                          key=a.key or key, title=a.title or
                          os.path.splitext(os.path.basename(a.input))[0],
                          max_notes=a.max_notes)
    _write(out, a.output)


def cmd_batch(a):
    """整个文件夹批量转：音频/视频都收，一个个出谱，可顺手存进本地曲库"""
    import pathlib
    from . import transcribe, beats, publish

    exts = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".mp4", ".mov",
            ".mkv", ".webm", ".avi", ".ts"}
    src = pathlib.Path(a.input)
    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in exts)
    if not files:
        print(f"{src} 里没有找到音频/视频")
        return 1
    outdir = pathlib.Path(a.output)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"共 {len(files)} 个文件 → {outdir}")
    done = 0
    for i, f in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {f.name}")
        try:
            ev, _bpm, backend = transcribe.transcribe(str(f), backend=a.backend)
            tempo, offset = a.tempo, 0.0
            if tempo is None:
                est, off, _why = beats.select_grid(ev, tempo_hint=a.tempo_hint)
                tempo, offset = max(30, int(round(est))), off
                _hit = _why.get("base_hit") if isinstance(_why, dict) else None
                print(f"  测速 {est:.1f} BPM"
                      + (f"（网格契合 {_hit:.2f}）" if isinstance(_hit, (int, float)) else ""))
            text = arrange.arrange(ev, tempo=tempo, time_sig=a.time_sig or "4/4", octave_fit=getattr(a, "octave", "auto"),
                                   key=a.key, title=f.stem, max_notes=a.max_notes,
                                   offset=offset)
            dst = outdir / (f.stem + ".smc")
            dst.write_text(text, encoding="utf-8")
            print(f"  ✓ {dst.name}  {len(ev)} 音")
            if a.publish:
                publish.publish(str(dst), title=f.stem)
            done += 1
        except Exception as e:
            print(f"  ✗ 失败：{type(e).__name__}: {e}")
    print(f"\n完成 {done}/{len(files)}")
    return 0 if done == len(files) else 1


def cmd_test(a):
    from . import test_roundtrip
    test_roundtrip.main()


def cmd_doctor(a):
    """装完先跑这个：一句话告诉你能不能用、缺什么。"""
    import shutil
    import pathlib
    ok = []
    warn = []
    bad = []

    print(f"Python  {sys.version.split()[0]}  ({sys.platform})")
    if sys.version_info < (3, 9):
        bad.append("Python 版本太老，需要 3.9 以上（推荐 3.10~3.13）")

    # ---- 外部程序
    ff = shutil.which("ffmpeg")
    print("ffmpeg  ", ff or "缺失")
    if not ff:
        warn.append("没有 ffmpeg：音频/视频输入会失败（MusicXML、MIDI、SMC 输入不受影响）。"
                    "Windows 装法：winget install Gyan.FFmpeg")

    # ---- 后端
    backend = None
    try:
        from . import transcribe
        backend = transcribe.auto_backend()
    except Exception as e:                                    # noqa: BLE001
        bad.append(f"sky15 自身模块导入失败：{type(e).__name__} {e}")

    print("\n转谱后端（自动挑：Transkun > 字节 > basic_pitch > librosa）")
    has_tk = False
    try:
        import transkun  # noqa: F401
        from transkun import transcribe as _tk
        w = pathlib.Path(_tk.__file__).parent / "pretrained" / "2.0.pt"
        has_tk = True
        size = f"{w.stat().st_size / 1024 / 1024:.1f} MB" if w.exists() else "缺失"
        print(f"  transkun                       已装，权重 {size}")
    except Exception:                                         # noqa: BLE001
        print("  transkun                       未安装（推荐：pip install transkun）")

    try:
        import piano_transcription_inference as pti       # noqa: F401
        ck = pathlib.Path.home() / "piano_transcription_inference_data"
        # 该目录可能有多个 .pth（如 gitcode.pth 仅 0.5 MB）；只认 >150 MB 的真权重，
        # 判据与 transcribe.auto_backend 保持一致，否则会把占位文件报成「权重 0 MB」。
        def _size(p):
            # glob 与 stat 之间文件可能被删/正在被下载，裸 stat 会 FileNotFoundError
            # 被外层宽 except 吞掉，最后误报成「未安装」
            try:
                return p.stat().st_size
            except OSError:
                return 0

        have = sorted((ck.glob("*.pth") if ck.exists() else []), key=_size, reverse=True)
        real = [p for p in have if _size(p) > 150 * 1024 * 1024]
        if real:
            print(f"  piano_transcription_inference  已装，权重 {_size(real[0]) / 1024 / 1024:.0f} MB")
        else:
            print("  piano_transcription_inference  已装，但权重没下（首次使用会下 165 MB）")
    except Exception:                                         # noqa: BLE001
        print("  piano_transcription_inference  未安装（可选，精度与 transkun 相当但慢一倍）")
    try:
        import basic_pitch  # noqa: F401
        print("  basic_pitch                    已装")
    except Exception:                                         # noqa: BLE001
        print("  basic_pitch                    未安装（可选，轻量兜底）")

    print(f"  → 实际会用：{backend or '未知'}")
    if not has_tk and backend != "transkun":
        warn.append("没装 transkun：精度会明显下降。pip install transkun 再把音频转一遍")

    # ---- 其余依赖
    print("\n依赖")
    for m in ("torch", "librosa", "soundfile", "mido", "music21", "PIL", "numpy", "soxr"):
        try:
            mod = __import__(m)
            v = getattr(mod, "__version__", None)
            if not isinstance(v, str):
                try:
                    v = mod.version.version      # mido 这种把版本放在子模块里
                except Exception:                # noqa: BLE001
                    v = "ok"
            print(f"  {m:12} {v}")
        except Exception:                                     # noqa: BLE001
            print(f"  {m:12} 未安装")
            if m in ("torch", "librosa", "soundfile", "numpy"):
                bad.append(f"缺 {m}：pip install -r requirements.txt")
            else:
                warn.append(f"缺 {m}（部分功能不可用）")

    try:
        import torch
        cuda = torch.cuda.is_available()
        print("\nCUDA    ", cuda, torch.cuda.get_device_name(0) if cuda else "（CPU 也能跑，慢 3~6 倍）")
    except Exception:                                         # noqa: BLE001
        pass

    # ---- 识谱
    try:
        from . import omr
        av = omr.find_audiveris()
        print("Audiveris", av or "未找到（只有「五线谱图片」输入需要它）")
        if not av:
            warn.append("没有 Audiveris：传图片/PDF 会失败；MusicXML 输入不受影响。"
                        "下载：https://github.com/Audiveris/audiveris/releases")
    except Exception:                                         # noqa: BLE001
        pass

    # ---- 音色
    try:
        from . import siteplay
        n = len(list(siteplay.SAMPLE_DIR.glob("*.mp3"))) if siteplay.SAMPLE_DIR.exists() else 0
        print(f"音色    {n} 个（{siteplay.SAMPLE_DIR}）")
        if n < 48:
            warn.append("音色文件不全：播放和「渲染成音频」会缺音。"
                        "仓库里带 48 个；也可 python -m sky15.tools.make_samples 现场合成")
    except Exception:                                         # noqa: BLE001
        pass

    # ---- 结论
    print("\n" + "=" * 52)
    if bad:
        print("还不能用，先解决这些：")
        for s in bad:
            print("  ✗", s)
    else:
        print("可以用。")
    if warn:
        print("提醒（不影响基本使用）：")
        for s in warn:
            print("  !", s)
    if not bad:
        print("\n试一下：python -m sky15.cli audio 你的录音.mp3 -o out/demo.smc")
    return 0 if not bad else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="sky15", description="音频 → 光遇 15 键乐谱")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, help_ in (("audio", cmd_audio, "音频/视频 → SMC"),
                            ("midi", cmd_midi, "MIDI → SMC"),
                            ("smc", cmd_smc, "SMC → SMC（重排）")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("input")
        sp.add_argument("-o", "--output", required=True)
        sp.add_argument("--key", default=None, help="指定调性，如 C / Ab；默认自动挑")
        sp.add_argument("--tempo", type=int, default=None)
        sp.add_argument("--tempo-hint", dest="tempo_hint", type=float, default=None,
                        help="速度提示（自动测速时消歧用）")
        sp.add_argument("--time-sig", dest="time_sig", default=None,
                        help="拍号，如 4/4；不给就沿用谱面/MIDI 里的")
        sp.add_argument("--title", default="")
        sp.add_argument("--max-notes", dest="max_notes", type=int, default=3)
        sp.add_argument("--octave", default="auto",
                        choices=["auto", "off", "high", "mel"],
                        help="音区写法：auto=常见写法（唱名写高、用 (8vb) 落回"
                             "原来的声音，默认）；off=完全按源音高写；"
                             "high=再抬一层（声音也跟着高）；mel=只抬旋律")
        if name == "audio":
            sp.add_argument("--backend", default=None,
                            choices=[None, "piano", "basic", "librosa"])
            sp.add_argument("--rubato", default="auto",
                            choices=["auto", "on", "off"],
                            help="速度起伏：auto=网格对不上才跟随（默认），"
                                 "on=强制跟随，off=恒速")
            sp.add_argument("--publish", action="store_true",
                            help="生成后直接存进本地曲库（默认 my_scores/）")
        sp.set_defaults(func=fn)

    sub.add_parser("test", help="回环自测").set_defaults(func=cmd_test)
    sub.add_parser("doctor", help="依赖体检").set_defaults(func=cmd_doctor)

    bp = sub.add_parser("batch", help="整个文件夹批量转")
    bp.add_argument("input", help="放音频/视频的文件夹")
    bp.add_argument("-o", "--output", required=True, help="输出目录")
    bp.add_argument("--key", default=None)
    bp.add_argument("--tempo", type=int, default=None)
    bp.add_argument("--tempo-hint", dest="tempo_hint", type=float, default=None)
    bp.add_argument("--time-sig", dest="time_sig", default="4/4")
    bp.add_argument("--max-notes", dest="max_notes", type=int, default=3)
    bp.add_argument("--backend", default=None, choices=[None, "piano", "basic", "librosa"])
    bp.add_argument("--publish", action="store_true", help="同时存进本地曲库")
    bp.set_defaults(func=cmd_batch)

    rp = sub.add_parser("render", help="SMC 谱 → 图片 / PDF")
    rp.add_argument("input", help=".smc 文件")
    rp.add_argument("-o", "--output", required=True, help=".png 或 .pdf")
    rp.add_argument("--dpi", type=int, default=150, help="150=普通屏 300=打印")
    rp.add_argument("--theme", default="dark", choices=["dark", "light"])
    rp.add_argument("--long", action="store_true", help="输出整首一张长图")
    rp.add_argument("--title", default="")
    rp.add_argument("--publish", action="store_true")
    rp.set_defaults(func=cmd_render)

    mx = sub.add_parser("musicxml", help="MusicXML / .mxl → SMC（五线谱）")
    mx.add_argument("input")
    mx.add_argument("-o", "--output", required=True)
    mx.add_argument("--key", default=None)
    mx.add_argument("--tempo", type=int, default=None)
    mx.add_argument("--time-sig", dest="time_sig", default=None)
    mx.add_argument("--title", default="")
    mx.add_argument("--max-notes", dest="max_notes", type=int, default=3)
    mx.add_argument("--publish", action="store_true")
    mx.set_defaults(func=cmd_musicxml)

    om = sub.add_parser("omr", help="五线谱图片/PDF → SMC（要 Audiveris）")
    om.add_argument("input")
    om.add_argument("-o", "--output", required=True)
    om.add_argument("--workdir", default=None, help="中间产物目录")
    om.add_argument("--timeout", type=int, default=600)
    om.add_argument("--key", default=None)
    om.add_argument("--tempo", type=int, default=None)
    om.add_argument("--time-sig", dest="time_sig", default=None)
    om.add_argument("--title", default="")
    om.add_argument("--max-notes", dest="max_notes", type=int, default=3)
    om.set_defaults(func=cmd_omr)

    a = p.parse_args(argv)
    # 以前这里没有 return，`__main__.py` 的 sys.exit(main()) 拿到 None = 退出码恒 0，
    # accept.py 里靠 returncode 判失败的闸门等于失效。
    return a.func(a) or 0


if __name__ == "__main__":
    main()
