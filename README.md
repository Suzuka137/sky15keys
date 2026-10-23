# sky15 — 音频 / 五线谱 → 光遇 15 键乐谱

把一段钢琴音频或一份 MusicXML 五线谱，转成《Sky 光遇》那种 15 键乐器的乐谱文本
（8 个格 = 一小节，最多 3 个音同时按）。

> ## ⚠️ 五线谱识别（拍照 / PDF / 截图）是半成品，不好用
>
> **先说清楚：只有音频转谱是能用的。** 拿图片去识谱这一块我没做好，请别指望它。
>
> 具体差在哪（实测，不是感觉）：
>
> - 识谱这一步靠开源的 [Audiveris](https://github.com/Audiveris/audiveris)，它自己就要装
>   一个 300+ MB 的 Java 程序；多数图片要先放大 2 倍它才认得准，一页要 15 秒。
> - 认出来的音里，同一时刻经常挤着 4~9 个音（正常钢琴谱最多也就 5 个），而 15 键
>   **每格最多按 3 个音**，为了塞进去必须砍音：1× 识别要砍掉 **20.4%** 的音，
>   放大 2× 也要砍 **9.3%**。被砍掉的就是你听到的"漏音 / 走味"。
> - 源谱的音真正落到 15 键上只有 **约 78%**（2× 时 89%）；准确率倒是 100%
>   （宁可少写也不写错音）。
> - 拍号/调号识别也会错（有份 2/4 的谱被读成 3/4），反复、D.C.、装饰音这些符号
>   处理得不完整。
>
> **结论**：想要好结果，请走 MusicXML（MuseScore、Sibelius、Finale、Dorico 都能导出，
> 网上买的正版电子谱通常是 .mxl）。**只有图片的话，这个功能能给你一份"能弹但明显不如
> 人工"的草稿，不要期待成品。** 这条限制我不打算假装它不存在。
>
> 音频转谱那边是另一回事：干净钢琴录音实测 **95%**，正常手机录的钢琴也在这个量级。

只依赖 Python 与公开模型，不含任何第三方曲谱数据。界面上那 48 个音色是
从 [Salamander Grand Piano](https://archive.org/details/SalamanderGrandPianoV3)（CC-BY 3.0）
实时合成的，随仓库分发合法，和游戏里的原声音无关。

> **English (short):** sky15 turns piano audio or MusicXML into sheet text for 15-key
> instruments like Sky: Children of the Light (8 slots per bar, ≤3 simultaneous notes per
> slot). It transcribes with Transkun 2.0, estimates tempo/key itself, arranges
> melody + bass under the 3-note constraint, and can render back to audio, PNG and PDF.
> On our own 80-song set it reproduces ~95% of the notes of the reference arrangement.
> The Transkun weights ship inside its pip package, so installation needs no extra download;
> no third-party scores or game audio are bundled.
>
> **Status: audio → sheet works. Sheet-image OMR (photo/PDF → score) is a half-finished
> prototype and is NOT recommended** — it needs a 300 MB external Java program, loses
> 9–20% of the source notes just to fit the 3-notes-per-slot limit, and misreads time/key
> signatures sometimes. Use MusicXML if you can export it.
>
> See `CONTRIBUTING.md` if you want to help.

## 安装

**Windows 一键**：双击 `install.bat`（自动建虚拟环境、装依赖、体检）。

手动装：

```bash
pip install -r requirements.txt
pip install --no-deps transkun        # 见下，不能直接 pip install transkun
python -m sky15.cli doctor            # 体检：告诉你缺什么、会用哪个后端
```

> **为什么不直接 `pip install transkun`**：它的依赖树里有 `ncls`，那个包在 PyPI 上
> 只有源码没有轮子，Windows 上要装 MSVC 才能编译，而转谱推理根本用不到它。
> 所以 Windows 用 `--no-deps` 单独装；macOS/Linux 有编译器，`requirements.txt`
> 里已经带上了 `transkun`，会自动装好。

转谱模型（Transkun 2.0）的权重**已经打在 pip 包里**（53.8 MB），不用另外下载。
国内网络慢：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

想用显卡加速（可选，比 CPU 快 3~6 倍，但要下 2 GB）：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

两个**可选**的外部依赖，缺了只影响对应输入：

| 缺什么 | 影响 | 装法 |
|---|---|---|
| ffmpeg | 音频/视频输入不能用（MusicXML、MIDI、SMC 不受影响）| `winget install Gyan.FFmpeg` |
| Audiveris | 五线谱**图片/PDF** 不能识别（MusicXML 不受影响）| 官网下载解压即用，设 `SKY15_AUDIVERIS` 指向 `Audiveris.exe` |

## 用法

```bash
python -m sky15 audio 我的录音.mp3 -o 我的曲子.smc     # 音频 → 乐谱（推荐）
python -m sky15 midi  某曲.mid   -o 某曲.smc           # MIDI → 乐谱
python -m sky15 smc   参考.smc   --tempo 120           # 已有乐谱换编配
python -m sky15 omr   乐谱截图.jpg -o 出.smc            # 图片识谱 → 乐谱（半成品，见上）
```

网页界面：`python -m sky15.web.server`（或双击 `web/start-ui.bat`），浏览器开 `http://127.0.0.1:8777/`。

## 乐谱格式

`曲名:[X] 速度:[120] 节拍:[4/4] 调性:[E(8vb)] 难度:[3] 曲谱:[...]`

曲谱体是空格分隔的 4 字符组：三个音位 + 一个标记位。**每 8 组一个小节**，第 8 组标记位
是 `>`；没有小节线的组只有 3 个字符。同组内高音写前面。字符表 `DRMFSLX12345678`，
对应一个根音往上 0,2,4,5,7,9,11 / 12,14,16,17,19,21,23 / 24 个半音。

## 它做得怎么样

| 输入 | 结果 |
|---|---|
| 干净钢琴音频（48 首自建基准） | **F1 95.1**，速度与调性全自动判定 |
| 128 kbps 单声道 + 底噪 + 削顶 | **97.7**（比干净版只掉 0.5 点） |
| 混响 rt≤1.0 s | 92.5；rt1.5 s 断崖到 77.2 —— 混响过重会明显掉分 |
| 五线谱 **MusicXML** | 源音召回约 78%，**准确率 100%**（宁可少写，不写错音） |
| 五线谱 **图片/PDF** | ⚠️ 半成品：先砍 9.3~20.4% 的音才塞得进 3 音格，拍号调号也会读错 |
| 速度估错 | 会整首崩（差 1.2% 就几乎全错），界面因此给了候选速度一键重排 |

（以上基准用的是自建数据集，未随仓库发布。）

## 目录

```
sky15/            核心：layout 音高模型、smc 读写、beats 定速、transcribe 转录、
                  arrange 编配、render 出图、omr 识谱、warptrack 速度起伏
sky15/web/        网页界面（stdio 起服务，无框架依赖）
sky15/tools/      杂项：MIDI 渲染、占位音色生成
```

## 已知限制

- 录屏、混响重、声音太小的录音，转出来会明显变差（`sky15.audioq` 会先给体检分）
- 同一格里超过 3 个音只能留 3 个，这是乐器本身的限制，不是算法问题
- 变速演奏（rubato）能跟上 ±1%，更大幅度的起伏仍会掉
- 音色随仓库分发的是 **Salamander Grand Piano V3**（CC-BY 3.0，Alexander Holm），
  见 `NOTICE.md`；`1.mp3` = C3（MIDI 48）… `48.mp3` = B6（MIDI 95）。
  不想用这套采样：删掉 `sky15/web/samples/*.mp3`，再
  `python -m sky15.tools.make_samples sky15/web/samples` 生成一套合成音色（无外部素材）。
  换成别的钢琴音源也可以：`python -m sky15.tools.make_samples --from-dir 你的采样目录`
  （文件名同样要是 `1.mp3`…`48.mp3`）。

## 许可

MIT，见 LICENSE。第三方模型（Transkun、piano_transcription_inference）各自遵循其
原许可；本仓库不包含任何游戏音频、商业曲谱或他人站点的资源。
