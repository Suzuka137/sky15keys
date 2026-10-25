"""sky15 —— 钢琴录音 / 五线谱 → 光遇 15 键乐谱。

模块分工：
    layout      15 键音高模型（字符 ↔ MIDI 的吸附与换算）
    smc          SMC 文本格式的读写
    transcribe  音频 → 音符事件（Transkun 2.0 / 字节钢琴模型 / librosa）
    beats       节拍网格：测速、相位、rubato 跟随
    arrange     音符事件 → 15 键分组（旋律 / 低音 / 和弦密度）
    chords      用 music21 做调性与和弦分析（可选依赖）
    melody      用 CREPE 提旋律线（可选依赖）
    musicxml    MusicXML ↔ 音符事件（含反复、连音、多声部）
    omr         五线谱图片 → MusicXML（homr 引擎）
    grid        格子的疏密（八分 / 十六分）
    render      SMC → PNG / PDF 谱面
    siteplay    按 48 个钢琴采样把 SMC 回放成 wav（评测用）
    pianorender 按原谱（不经 15 键压缩）渲染钢琴 mp3
    audioq      录音体检（信噪比 / 高频 / 混响 / 削顶）
    warptrack   逐段变速跟踪
    web/        本地网页界面（stdlib，无框架）
"""

__version__ = "0.3.0"
__all__ = ["layout", "smc", "transcribe", "beats", "arrange", "musicxml",
           "omr", "render", "pianorender", "audioq"]
