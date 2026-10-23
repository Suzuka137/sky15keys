# 第三方素材与许可

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
