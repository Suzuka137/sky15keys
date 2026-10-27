# sky15keys — piano recording / MIDI / scores → 15-key sheets for Sky: Children of the Light

**[中文](README.md) | English**

> Search keywords: **sky15keys** / 光遇键谱 (previous repo name: `sky15`)

Drop in a piano recording — a phone recording, a game capture, audio ripped from a video —
and get back the sheet text used by the 15-key instrument in *Sky: Children of the Light*:
**8 slots per bar, at most 3 notes pressed together per slot**, matching the in-game keys
one to one. MIDI and MusicXML go straight in as well.

Tempo, key and the two hands are detected automatically; you do not fill in any parameter.
The result plays back, exports to MIDI, saves as a long sheet image / paged images / PDF,
and can be played on an on-screen 15-key pad.

The evaluation is self-contained (we also ship the audio renderer used to build it), so the
numbers are reproducible: **on a self-built benchmark, F1 95.1 on clean audio, 92.0 over the
whole set, tempo 95% correct, key 100% correct.**

- Input: `.mp3` `.m4a` `.wav` `.flac` video files, `.mid`, `.musicxml`/`.mxl`, existing `.smc`
- Output: `.smc` sheet text, MIDI, piano-audio MP3, PNG long image / paged images, PDF
- Backend: Transkun 2.0 (MIT; weights are bundled in the pip package, so it works right
  after install; no GPU required — roughly one minute per song on CPU)
- Interface: a Python standard-library server with no framework dependency; double-click
  `sky15/web/start-ui.bat`

It depends only on Python and public models, and contains no third-party score data. The
48 instrument samples in the web UI are synthesised at build time from
[Salamander Grand Piano](https://archive.org/details/SalamanderGrandPianoV3) (CC-BY 3.0),
which may legally be redistributed; they have nothing to do with the game's own audio.

## What's new in 1.0

- The player was fixed: after the last note the playhead **stays just past it** instead of
  snapping back to the top of the page, and the final note is allowed to ring out.
- No more permanent silence after the browser closes the audio context, and waking from
  sleep no longer dumps a wall of stacked notes.
- Leftover notes are cut on pause/seek; double-clicking pause no longer doubles a note.
- The sheet editor's 15-key pad lights up and sounds again after reopening the editor.
- Self-check: `python -m sky15.accept` — 30 checks on the fast path, 33 in full.
- **Staff-notation image/PDF recognition was removed in 1.0.** It was never reliable enough
  to justify its weight. For score input, export **MusicXML** from MuseScore / Overture /
  Sibelius instead — that path recovers far more notes than OCR ever did.

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

The only external dependency is ffmpeg; without it audio/video input is unavailable
(MusicXML, MIDI and SMC still work). Install it with `winget install Gyan.FFmpeg`.

## Usage

```bash
python -m sky15 audio my-recording.mp3 -o my-song.smc   # audio → sheet (recommended)
python -m sky15 midi  song.mid         -o song.smc       # MIDI → sheet
python -m sky15 smc   reference.smc    --tempo 120       # re-arrange an existing sheet
python -m sky15 musicxml score.musicxml -o out.smc       # MusicXML → sheet
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
- **Piano-audio MP3 export**: renders the sheet you see, playhead parking after the last note
  and the final note left to ring out
- Rename the result — both the title and `曲名:[...]` inside the sheet change
- Change parameters and re-arrange without re-transcribing: tempo (candidate values are one
  click away), chords (auto / duo), notes per slot, separate the hands, register
- **Duo chords**: a slot that already holds one high note plus one low note gets a third note,
  picked only from the seven low-register tones, so the melody never moves; slots where every
  candidate would form `12 23 34 45 67 17 27 16 26` are left alone
- Register (`--octave`): `off` writes what you hear / `auto` shifts the notation up a level
  and marks `(8vb)` when the whole piece sits low, leaving the sound unchanged (the most
  common convention in existing 15-key sheets) / `mel` lifts only the melody, sounding a real
  octave higher. Pick `auto` to match the look of published sheets.
- Key is detected automatically (a geometry score, with a music21 tie-break when the margin is
  thin). **To hear it in another key, change the pad's "play key"** — that alters the sound
  only, not the sheet (in-game you press relative positions, so key does not matter)
- A tempo you type is authoritative: changing any other parameter never silently changes it
- A recording health score (`audioq`) warns ahead of time about heavy reverb, a too-low
  sample rate, or smeared onsets
- When audio quality is poor or the tempo looks wrong, it says so instead of failing quietly
- **Sheets can be edited in the browser**: open "改谱子", click a slot to change notes, move
  with `←→↑↓`, type with the number keys, `E` seeks the playhead to the selected column,
  space plays/pauses; inserted or deleted rows re-pad everything after them and the edit
  autosaves (max 3 notes per slot, highest first)

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
| Clean piano audio | **F1 95.1**, tempo and key fully automatic |
| 128 kbps mono + noise floor + clipping | **97.7** (only 0.5 points below clean) |
| Reverb rt ≤ 1.0 s | 92.5; rt 1.5 s falls off a cliff to 77.2 — heavy reverb clearly costs accuracy |
| Sheet music as **MusicXML** | ~78% of source notes recovered, **100% precision** (it would rather omit than guess) |
| Wrong tempo estimate | collapses the whole piece (1.2% off is nearly total failure), hence the click-to-retry tempo candidates in the UI |

(Benchmarks use a self-built dataset that is not distributed with the repo.)

## Layout

```
sky15/            core: layout (pitch model), smc (read/write), beats (tempo),
                  transcribe (transcription), arrange (voicing), render (images),
                  musicxml (score parsing), warptrack (expressive timing)
sky15/web/        web UI (stdlib server, no framework)
sky15/tools/      utilities: MIDI rendering, sample generation
```

## Known limitations

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
