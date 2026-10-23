# 光遇键谱 · sky15keys — 钢琴录音 / 五线谱 → 光遇 15 键乐谱

**中文 | [English](README.en.md)**

> **搜关键词：光遇键谱 / sky15keys**（仓库名 `sky15keys`，以前叫 `sky15`）
> 抖音、B站这类平台不让放链接，记住这四个字就能找到。

把一段钢琴录音（手机录的、游戏里放的、视频里扒的）丢进去，转成《Sky 光遇》那种
15 键乐器的乐谱文本——**8 个格 = 一小节，每格最多 3 个音同时按**，和游戏里的键位一一对应。
手上只有五线谱也行：图片、PDF、MusicXML 都能进。

速度、调性、左右手全自动判定，不用你填任何参数。转完能直接听、能导出 MIDI、
能存成谱面长图 / 分页图 / PDF，也能在网页上的 15 键面板里按键弹。

自己也有一套引擎（原版音频合成器），所以这条路对得上：**自建 80 首基准，干净录音 F1 95.1，
全库均值 92.0，速度自动判对 76/80，调性 80/80。**

- 输入：`.mp3` `.m4a` `.wav` `.flac` 视频文件、`.mid`、`.musicxml`/`.mxl`、五线谱图片/PDF、已有的 `.smc`
- 输出：`.smc` 乐谱、MIDI、钢琴声 MP3、PNG 长图 / 分页图、PDF
- 后端：Transkun 2.0（MIT，权重随 pip 包走，装完即用；没有显卡也能跑，CPU 一首约 1 分钟）
- 界面：Python 标准库起服务，没有框架依赖，双击 `sky15/web/start-ui.bat` 就能用

> **三条输入都能用**：钢琴录音 / 视频、MusicXML、五线谱图片或 PDF。
> 图片这条路是本地自动识谱（开源 [homr](https://github.com/liebharc/homr)，CPU 跑，一页 5~9 秒），
> 识出来的谱再**先弹成钢琴音频过一遍转谱模型**，识谱漏掉的音会在这一步被听回来。
> 图片**只认五线谱**：简谱、光遇 15 键格子谱、照片里的别的东西，一律不转——传上去会直接告诉你原因。
>
> 顺带提一句：**看图这条路还算半成品**，准不准很吃图片。扫描的、正对着拍的还行；
> 手机随手拍、拍歪了、有阴影的，会漏音，偶尔连拍号都读错。要稳就走音频或 MusicXML，
> 那两条路是反复标定过的。

只依赖 Python 与公开模型，不含任何第三方曲谱数据。界面上那 48 个音色是
从 [Salamander Grand Piano](https://archive.org/details/SalamanderGrandPianoV3)（CC-BY 3.0）
合成的，随仓库分发合法，和游戏里的原声音无关。

> **English (short):** sky15keys turns a piano recording or MusicXML into sheet text for
> 15-key instruments like Sky: Children of the Light (8 slots per bar, ≤3 simultaneous
> notes per slot). Tempo, key and the two hands are detected automatically; output can be
> played back, exported to MIDI/PNG/PDF, or played on an on-screen 15-key pad.
> On our own 80-song set: F1 95.1 on clean audio, 92.0 over the whole set, tempo 76/80,
> key 80/80. Backend is Transkun 2.0 (MIT, weights bundled in its pip package, CPU-only works).
> Note: recordings, MusicXML and staff-notation images/PDFs all work; images are read with
> local OMR (homr) and replayed as piano audio before transcription.
> The image path is still a work in progress: it is fussy about the picture — clean scans are
> fine, casual phone photos with skew or shadow can drop notes or misread the time signature.
> For dependable results use audio or MusicXML.
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
| homr | 五线谱**图片/PDF** 不能识别（其它输入不受影响）| `pip install "homr[cpu]"`，再跑一次 `homr --init`；或用 `SKY15_HOMR` 指向 `homr.exe` |

## 用法

```bash
python -m sky15 audio 我的录音.mp3 -o 我的曲子.smc     # 音频 → 乐谱（推荐）
python -m sky15 midi  某曲.mid   -o 某曲.smc           # MIDI → 乐谱
python -m sky15 smc   参考.smc   --tempo 120           # 已有乐谱换编配
python -m sky15 omr   五线谱.jpg  -o 出.smc            # 五线谱图片/PDF → 乐谱（只认五线谱）
```

网页界面：`python -m sky15.web.server`（或双击 `sky15/web/start-ui.bat`），浏览器开 `http://127.0.0.1:8777/`。

### 界面上有什么

- 拖文件进去就转，多个文件排队，能看到当前阶段（转谱 / 测速 / 编配 / 出谱）
- 转完直接放：5 个八度共 48 个琴键音，和游戏里一样，按住不放是长音
- 15 键面板，键盘 `Y U I O P / H J K L ; / N M , . /` 对应三行，点方块也能弹
- 谱面按 4 小节一行排，橙色线每 4 行分段；能导出 PNG 长图 / 分页图 / PDF / MIDI / `.smc`
- **能下载钢琴声 MP3**：五线谱来的弹原谱的每一个音（不压缩成 15 键），识谱漏音的地方自动连奏补洞；
  音频来的弹屏幕上这份 15 键谱
- 顺手能给成品改名（标题和谱面里的 `曲名:[...]` 一起改）
- 参数随时改随时重排：速度（可点候选值）、和弦（自动 / 双音）、每格音数、左右手是否分开、音区
- **和弦双音**：只给「一个高音 + 一个低音」的格子补第三音，补的音只用低音区那七个，
  主旋律不动；补完会撞上 `12 23 34 45 67 17 27 16 26` 的格子自动跳过
- 音区三档（`--octave`）：`off` 不动（听到什么写什么）／`auto` 自动（整首偏低时唱名抬一层、
  标 `(8vb)`，发声不变，这是现成 15 键谱里最常见的写法）／`mel` 只抬旋律（旋律进高音行、
  发声真高 12 半音）。想跟现成的谱子长得像就选 `auto`
- 调性不让你手选：全库 80/80 自动判对。**想换调听就把 15 键面板的「弹奏调性」换掉**，
  那只改发声、不改谱子（游戏里按的是相对位置，跟调性无关）
- 手填的速度是权威的：改任何参数都不会顺手把速度改掉（这条踩过坑，写进代码注释了）
- 录音体检分（`audioq`）：混响太重、采样率太低、音头糊了会提前告警
- 音频质量差或速度估错时给建议，不是闷着出错

## 反过来：乐谱 → 钢琴音频

有时候你要的不是 15 键谱，而是**先听听这谱子到底是什么样**。`pianorender` 用真正的钢琴采样
（Salamander Grand Piano V3，CC-BY，每个音 16 个力度层）把谱子直接弹出来：

```bash
python -m sky15.pianorender 谱子.musicxml          # → 谱子.wav
python -m sky15.pianorender 谱子.jpg --mp3         # 五线谱图片（会先识谱，慢）
python -m sky15.pianorender 曲子.smc -o 输出目录    # 15 键谱也能弹
```

支持 MusicXML / MIDI / `.smc` / 图片·PDF；渲染时按音高挑采样层，松键有 0.3 秒释放，
整首按峰值归一到 -1 dBFS。仓库里那 48 个 mp3 不够用时，下
[Salamander Grand Piano V3](https://archive.org/details/SalamanderGrandPianoV3) 并用
`SKY15_SALAMANDER` 指向解压目录，音质会好一个档次。

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
| 五线谱 **图片 / PDF** | homr 识谱，一页 5~9 秒；识完默认先弹成钢琴音频再过一遍转谱模型，漏音在那一层被听回来 |
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

- **图片只认五线谱**：简谱、光遇 15 键格子谱、照片里的其它东西不转（会直接报「这张图不是五线谱」）。
  五线谱照片请正对镜头、别斜别压阴影；复杂的反复记号、D.C.、装饰音会被简化。
- 识谱读出来的音会比原谱少一些，所以图片这条路默认「先弹成钢琴音频再转谱」——
  让模型按听感补回漏掉的音；想要原谱的绝对准确性，导 MusicXML 最稳
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
