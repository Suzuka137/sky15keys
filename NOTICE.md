# 第三方素材与许可
本项目自身代码：MIT（见 `LICENSE`）。

## 音色（sky15/web/samples/1.mp3 … 48.mp3）

- 来源：**Salamander Grand Piano V3**，作者 Alexander Holm
- 许可：**Creative Commons Attribution 3.0（CC-BY 3.0）**
- 主页：https://archive.org/details/SalamanderGrandPianoV3
- 本项目对原素材做了以下改动：按 SFZ 的音高映射把每三个半音一个的采样变调成
  48 个半音（最多 ±1.5 个半音），合单声道、截取前 5 秒、按响度归一、转 128 kbps mp3。

重新生成（需要原 SFZ 与 ogg 目录）：

```
python -m sky15.tools.build_samples <SalamanderGrandPianoV3.sfz> sky15/web/samples --vel 80
```

不想用这套采样时，删掉 `sky15/web/samples/*.mp3` 再跑一次合成器即可生成替代音色：

```
python -m sky15.tools.make_samples sky15/web/samples
```

## 运行时依赖（不随本仓库分发）

| 组件 | 许可 | 说明 |
|---|---|---|
| Transkun 2.0 | MIT | 默认转录后端；权重 53.8 MB 打在该 pip 包里 |
| piano-transcription-inference | MIT | 可选后端；权重由该包首次运行时从 Zenodo 下载，本仓库不重分发 |
| PyTorch | BSD-3-Clause | |
| librosa | ISC | |
| soundfile | BSD-3-Clause | |
| numpy | BSD-3-Clause | |
| mido | MIT | |
| music21 | BSD-3-Clause | 用于调性分析 |
| homr | Apache-2.0 | **可选**，图片识谱用；以 pip 包安装，权重由 `homr --init` 下载 |
| Audiveris | AGPL-3.0 | 旧识谱后端，已不在默认流程里；本仓库不分发也不链接它 |
| ffmpeg | LGPL-2.1+ / GPL（看构建） | 外部命令行工具 |

## 关于游戏与格式

`sky15/layout.py` 里的 15 键音高排列，是《Sky 光遇》这个乐器本身的键位规则，属于
游戏机制（事实性数据），按观察重新实现；仓库内**不包含任何游戏音频、贴图或客户端资源**。

`sky15/smc.py` 处理的 SMC 是社区多个乐谱播放器通用的一种纯文本交换格式。
本仓库不含任何他人曲谱数据；基准测试用的乐曲只用于本地评测，未随仓库发布。
