<p align="center">
  <img src="peak_alphaloop/static/favicon.png" width="72" alt="">
</p>

<h1 align="center">Peak Alphaloop</h1>

<p align="center">
  A small, free video tool: remove black, white or green-screen backgrounds to real
  transparency, make seamless mirror loops, and do basic multi-clip edits.<br>
  Windows · macOS · Linux · MIT licensed
</p>

<p align="center">
  <a href="https://github.com/peakprojections/peak-alphaloop/releases/latest"><b>⬇ Download the latest release</b></a>
</p>

---

## Why this exists

At [Peak Projections](https://peakprojections.com) our players can layer videos with
transparent backgrounds on top of each other. Getting those videos was the hard part:
very little software exports video with an alpha channel intact, and the one
"intelligent" background remover we tried didn't do well on our footage. What we
actually needed was narrow and specific: take a clip shot on black, white or a green
screen, make that background transparent, maybe loop it seamlessly, and save it in a
format that keeps the alpha.

So we built that tool, together with **[Claude](https://www.anthropic.com/claude)**,
Anthropic's AI model. We described the problem; Claude worked out the approach,
wrote the code and tests, and iterated with us as we added features. It turned out
useful enough that we'd rather share it than keep it to ourselves. There is nothing
Peak-specific in it, so if you have a use for it, it's yours.

## What it does

- **Remove a background to transparency**
  - **Black or white** backgrounds, with a *soft* luma key for glows, fire, smoke, light
    and particles, or a *hard* key for solid objects and logos.
  - **Any color** (green/blue screens, a grey wall…): click **Pick from video** and click the
    background. Shadows and uneven lighting on the screen are handled, and green or blue
    spill is pulled out of hair and skin edges.
  - Edge colors are "unblended" so nothing keeps a dark, light or green halo when it is
    layered over something else.
- **Mirror loop**: plays forward then backward (without repeating the turnaround
  frames) so the end flows straight back into the start, giving a seamless loop.
- **Basic editing**: add several clips, then trim, split, reverse, duplicate, remove and
  drag them into order on a simple timeline, with undo/redo. Clips of different sizes and
  frame rates are fitted to the first clip's size and frame rate.
- **Export** as **WebM** (VP9 with alpha, small), **ProRes 4444** (.mov with alpha, large,
  best for editing), or, with background removal turned off, **MP4** (H.264) and
  **ProRes 422 HQ**. Audio can be kept or dropped.
- Everything runs **locally**. Your videos never leave your computer.

## Download

Get the file for your system from the
[latest release](https://github.com/peakprojections/peak-alphaloop/releases/latest):

| System | File | Notes |
| --- | --- | --- |
| Windows 10/11 (64-bit) | `PeakAlphaloop-<version>-windows-x64-setup.exe` | Installs for your user only; no admin rights needed. |
| macOS (Apple silicon) | `PeakAlphaloop-<version>-macos-arm64.zip` | Unzip and drag **Peak Alphaloop** to Applications. |
| Linux (x86-64) | `PeakAlphaloop-<version>-linux-x64.tar.xz` | Extract, then run `PeakAlphaloop` (or `install-desktop-entry.sh` to add it to your menu). |

FFmpeg is included; there is nothing else to install.

**First launch warnings.** The downloads are not code-signed with a paid certificate:

- **Windows** may show *"Windows protected your PC"*. Click **More info → Run anyway**.
- **macOS** may say the app *"can't be opened"* or is from an unidentified developer.
  Right-click (or Control-click) the app and choose **Open**, then **Open** again. On
  recent macOS versions you may instead need **System Settings → Privacy & Security →
  Open Anyway**.

Every release lists SHA-256 checksums in `SHA256SUMS.txt` if you want to verify a download.

**How the app runs.** Peak Alphaloop opens in your web browser, but it is not a website:
a small program on your computer does the work and only accepts connections from the
browser tab it opened. Close the tab and the app shuts itself down after a couple of
minutes, or click **Quit** in the top bar.

**Updates.** The app checks GitHub for a newer release when you open it (one anonymous
request to `api.github.com`). On Windows it can download, verify and install the update
for you; on macOS and Linux it links you to the download. Start it with
`--no-update-check` (or set `ALPHALOOP_NO_UPDATE_CHECK=1`) to turn this off.

## Using it

1. **Add clips**: drop videos on the window or use **＋ Add clips**. Several clips play
   back to back.
2. **Edit** on the timeline strip:
   - Click a clip to select it and move the playhead; drag clips to reorder them.
   - **Split** (`S`), **Reverse** (`R`), **Duplicate** (`D`), **Remove** (`Del`),
     **Trim start here / Trim end here**, or type exact source in/out times.
   - Step frames with `←` `→` (`Shift` for one second), and undo/redo with `Ctrl/⌘+Z` and
     `Ctrl/⌘+Shift+Z`.
3. **Remove background** (on by default; switch it off to just edit, loop or convert):
   pick **Black**, **White** or **Color**, choose **Soft** or **Hard**, and fine-tune
   **Threshold** and **Softness**. Check the result with the **Result / Alpha matte /
   Original** views over checker, red, green, white or black backgrounds.
4. **Export**: choose a format, optionally **Mirror loop** and **Include audio**, then
   **Export video**. Download the file, or play WebM/MP4 results right in the preview.

### Tips

- For glows and smoke on black, use **Soft**. For logos and people, use **Hard**.
- If dark details inside a subject on black (or light details on white) disappear, turn
  on **Protect interior**. It keeps any region not connected to the frame edge.
- Mirror loops suit motion that looks natural backwards: smoke, particles, light, water,
  slow camera drifts. Walking, falling objects or text will visibly rewind.
- Despill slightly shifts saturated colors close to the key (yellows and teals on a
  green screen). Untick **Remove color cast** if your subject really contains them.

## Command line

Install with Python 3.11+ and FFmpeg on your `PATH` (`pip install .` from this
repository), then:

```bash
alphaloop                                        # open the app
alphaloop-cli smoke.mp4                          # black -> transparent WebM
alphaloop-cli logo.mp4 -b white -p hard -f prores
alphaloop-cli talent.mp4 -b "#2aa34a" -p soft    # green screen, picked color
alphaloop-cli embers.mp4 --mirror-loop           # seamless loop with alpha
alphaloop-cli waves.mp4 -n -m                    # no keying, just loop -> waves-loop.mp4
alphaloop-cli a.mp4 b.mp4 --join -n -a -o out.mp4
alphaloop-cli clip.mp4 --start 2 --end 5.5 --reverse -n
alphaloop-cli clips/*.mp4 -o keyed/              # batch into a folder
alphaloop-cli --help
```

## How it works

Each pixel is treated as `observed = alpha × foreground + (1 − alpha) × background`.
Alpha comes from the pixel's distance to the background, shaped by threshold and
softness, and the foreground color is then solved back out ("unblended").

- **Black/white:** distance is the pixel's brightest channel (on black) or darkest
  channel (on white), so the whole key is two 256×256 lookup tables.
- **Color:** distance is measured in YCbCr with brightness given half weight, so a
  shadowed or hot-spotted screen still reads as screen. Alpha comes from a 16M-entry
  table indexed by the packed RGB value, and unblending uses a 256×256 table per
  channel. Spill suppression limits the key's dominant channel to the average of the
  other two.

The timeline decodes each segment with FFmpeg onto one canvas (scaling and letterboxing
in the key color so the bars turn transparent too), reverses segments through a
compressed on-disk spool, keys frames on a thread pool, and pipes them into a single
encoder. Mirror loops replay the keyed frames from the same spool, and the audio track
is assembled to match the video frame for frame.

**Decoding WebM alpha elsewhere:** FFmpeg's built-in VP9 decoder silently ignores the
alpha channel. Anything that reads these WebM files through FFmpeg must force the
libvpx decoder before the input: `ffmpeg -c:v libvpx-vp9 -i overlay.webm …`.
Browsers and ProRes 4444 need no special handling.

### Known limits

- Parts of a subject that match the background also become transparent unless
  **Protect interior** is on, and that only helps for regions fully enclosed by the subject.
- One key setting for the whole timeline; no garbage mattes or per-clip keys yet.
- No transitions, titles or effects. It's deliberately a small tool.
- macOS downloads are for Apple silicon. Intel Macs can run it from source.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v       # needs ffmpeg/ffprobe on PATH
python packaging/build_standalone.py          # standalone build for this OS
```

Releases are built by GitHub Actions: pushing a tag `vX.Y.Z` that matches
`peak_alphaloop/__init__.py` builds all three platforms, runs a self-test of each
packaged app (real keying, looping and audio exports with the bundled FFmpeg),
and publishes the release with `SHA256SUMS.txt`.

## Credits and license

Made by [Peak Projections](https://peakprojections.com), built with
[Claude](https://www.anthropic.com/claude) by Anthropic.

Peak Alphaloop is released under the [MIT License](LICENSE). The standalone downloads
bundle [FFmpeg](https://ffmpeg.org), which is licensed under the GPL; see
[THIRD_PARTY_NOTICES.md](packaging/THIRD_PARTY_NOTICES.md) for details and source links.
