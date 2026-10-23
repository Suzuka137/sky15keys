# sky15keys — piano recording / sheet music → 15-key sheets for Sky: Children of the Light

**[中文](README.md) | English**

> Search keywords: **sky15keys** / 光遇键谱 (previous repo name: `sky15`)

Drop in a piano recording — a phone recording, a game capture, audio ripped from a video —
and get back the sheet text used by the 15-key instrument in *Sky: Children of the Light*:
**8 slots per bar, at most 3 notes pressed together per slot**, matching the in-game keys
one to one.

Tempo, key and the two hands are detected automatically; you do not fill in any parameter.
The result plays back, exports to MIDI, saves as a long sheet image / paged images / PDF,
and can be played on an on-screen 15-key pad.

The evaluation is self-contained (we also ship the audio renderer used to build it), so the
numbers are reproducible: **on our own 80-song set, F1 95.1 on clean audio, 92.0 over the
whole set; tempo correct 76/80, key correct 80/80.**

- Input: `.mp3` `.m4a` `.wav` `.flac` video files, `.mid`, `.musicxml`/`.mxl`, existing `.smc`
- Output: `.smc` sheet text, MIDI, PNG long image / paged images, PDF
- Backend: Transkun 2.0 (MIT; weights are bundled in the pip package, so it works right
  after install; no GPU required — roughly one minute per song on CPU)
- Interface: a Python standard-library server with no framework dependency; double-click
  `sky15/web/start-ui.bat`

> Boundaries, in one sentence: **audio → sheet is usable; recognising sheet-music *photos*
> (OMR) is still a prototype.** The details are under "Known limitations" below. For
> engraved scores, prefer MusicXML — MuseScore, Sibelius, Finale and Dorico all export it,
> and purchased digital scores are usually `.mxl`.

It depends only on Python and public models, and contains no third-party score data. The
48 instrument samples in the web UI are synthesised at build time from
[Salamander Grand Piano](https://archive.org/details/SalamanderGrandPianoV3) (CC-BY 3.0),
which may legally be redistributed; they have nothing to do with the game's own audio.

## Install

**Windows, one click**: double-click `install.bat` (creates a venv, installs dependencies,
runs the doctor).

Manual:

```bash
pip install -r requirements.txt
pip install --no-deps transkun        # see below; plain pip install transkun fails on Windows
python -m sky15.cli doctor            # checks what is missing and which backend will be used
```

> **Why not `pip install transkun`**: its dependency tree includes `ncls`, which ships only
> an sdist on PyPI. On Windows that needs MSVC to build, and transcription inference never
> touches it. So Windows installs it with `--no-deps`; on macOS/Linux there is a compiler,
> and `requirements.txt` already pulls `transkun` in normally.

The Transkun 2.0 weights **are inside the pip package** (53.8 MB); nothing extra to
download. On slow networks: `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

GPU acceleration is optional (3–6× faster than CPU, but downloads about 2 GB):

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

Two **optional** external dependencies; missing ones only disable the matching input:

| Missing | Effect | Install |
|---|---|---|
| ffmpeg | audio/video input unavailable (MusicXML, MIDI and SMC still work) | `winget install Gyan.FFmpeg` |
| Audiveris | sheet-music **images/PDF** cannot be recognised (MusicXML unaffected) | download, unzip, set `SKY15_AUDIVERIS` to `Audiveris.exe` |

## Usage

```bash
python -m sky15 audio my-recording.mp3 -o my-song.smc   # audio → sheet (recommended)
python -m sky15 midi  song.mid         -o song.smc       # MIDI → sheet
python -m sky15 smc   reference.smc    --tempo 120       # re-arrange an existing sheet
python -m sky15 omr   screenshot.jpg   -o out.smc        # image → sheet (prototype, see above)
```

Web UI: `python -m sky15.web.server` (or double-click `sky15/web/start-ui.bat`), then open
`http://127.0.0.1:8777/`.

### What the UI gives you

- Drop files in and they convert; several files queue up, and the current stage is shown
  (transcribe / detect tempo / arrange / render)
- Play the result immediately: 48 keys across five octaves, like the game — holding a key
  sustains the note
- A 15-key pad bound to `Y U I O P / H J K L ; / N M , . /`; clicking the squares works too
- Sheets lay out at 4 bars per row, with an orange divider every 4 rows; export as PNG long
  image / paged images / PDF / MIDI / `.smc`
- Change parameters and re-arrange without re-transcribing: tempo (candidate values are one
  click away), notes per slot, melody only, separate the hands, register
- Register (`--octave`): `off` writes what you hear / `auto` shifts the notation up a level
  and marks `(8vb)` when the whole piece sits low, leaving the sound unchanged (the most
  common convention in existing 15-key sheets) / `mel` lifts only the melody, sounding a real
  octave higher. Pick `auto` to match the look of published sheets.
- Key is not a manual choice: 80/80 detected correctly on our set. **To hear it in another
  key, change the pad's "play key"** — that alters the sound only, not the sheet (in-game you
  press relative positions, so key does not matter)
- A tempo you type is authoritative: changing any other parameter never silently changes it
- A recording health score (`audioq`) warns ahead of time about heavy reverb, a too-low
  sample rate, or smeared onsets
- When audio quality is poor or the tempo looks wrong, it says so instead of failing quietly

## Sheet format

`曲名:[X] 速度:[120] 节拍:[4/4] 调性:[E(8vb)] 难度:[3] 曲谱:[...]`

The body is space-separated 4-character groups: three note slots plus one marker slot.
**Every 8 groups make one bar**, and the 8th group carries `>` in the marker slot; groups
without a bar line are only 3 characters. Higher pitches come first inside a group. The
alphabet is `DRMFSLX12345678`, mapping to 0,2,4,5,7,9,11 / 12,14,16,17,19,21,23 / 24
semitones above a root.

## How well it does

| Input | Result |
|---|---|
| Clean piano audio (48-song in-house set) | **F1 95.1**, tempo and key fully automatic |
| 128 kbps mono + noise floor + clipping | **97.7** (only 0.5 points below clean) |
| Reverb rt ≤ 1.0 s | 92.5; rt 1.5 s falls off a cliff to 77.2 — heavy reverb clearly costs accuracy |
| Sheet music as **MusicXML** | ~78% of source notes recovered, **100% precision** (it would rather omit than guess) |
| Sheet music as **photo/PDF** | ⚠️ prototype: 9.3–20.4% of notes are cut to fit 3-note slots; time and key signatures can be misread |
| Wrong tempo estimate | collapses the whole piece (1.2% off is nearly total failure), hence the click-to-retry tempo candidates in the UI |

(Benchmarks use an in-house dataset that is not distributed with the repo.)

## Layout

```
sky15/            core: layout (pitch model), smc (read/write), beats (tempo),
                  transcribe (transcription), arrange (voicing), render (images),
                  omr (score recognition), warptrack (expressive timing)
sky15/web/        web UI (stdlib server, no framework)
sky15/tools/      utilities: MIDI rendering, sample generation
```

## Known limitations

- **OMR (photos / PDF / screenshots of scores) is a prototype and not pleasant to use.**
  It relies on the open-source Audiveris, itself a 300+ MB Java program; most images need
  2× upscaling first (about 15 s per page). Recognised notes often pile 4–9 on one instant,
  while a 15-key slot holds only 3, so notes must be cut to fit (20.4% at 1×, 9.3% at 2×) —
  those cuts are exactly the missing notes you hear. About **78%** of source notes actually
  land on keys (89% at 2×, at 100% precision); time and key signatures are sometimes misread
  (one 2/4 score came out as 3/4); repeats, D.C. and grace notes are handled incompletely.
  **For engraved scores, use MusicXML.**
- Screen recordings, heavy reverb and very quiet recordings degrade noticeably
  (`sky15.audioq` gives a health score first)
- Only 3 notes can survive in a slot — that is the instrument's limit, not the algorithm's
- Expressive timing (rubato) is tracked to about ±1%; wider swings still lose accuracy
- The distributed instrument samples are **Salamander Grand Piano V3** (CC-BY 3.0, Alexander
  Holm), see `NOTICE.md`; `1.mp3` = C3 (MIDI 48) … `48.mp3` = B6 (MIDI 95). To drop them,
  delete `sky15/web/samples/*.mp3` and run
  `python -m sky15.tools.make_samples sky15/web/samples` to synthesise a set instead (no
  external assets). Another piano source also works:
  `python -m sky15.tools.make_samples --from-dir your-samples` (same `1.mp3`…`48.mp3` names).

## License

MIT, see LICENSE. Third-party models (Transkun, piano_transcription_inference) keep their
own licenses; this repository contains no game audio, no commercial scores, and no assets
from anyone else's site.
