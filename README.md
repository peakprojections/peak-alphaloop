# Peak Video Editor

A small Peak Projections tool that turns a **black, white, or colored (green screen)
video background into a transparent alpha channel**, optionally makes it a seamless
**mirror loop**, and saves it in a format the Peak viewer can layer over other surfaces:

| Format | Container | Typical size | Use it when |
| --- | --- | --- | --- |
| **WebM** (VP9 + alpha) | `.webm` | small, often 20–60× smaller than ProRes | Default. Uploading and caching on the mini PCs. |
| **ProRes 4444** | `.mov` | large, roughly 30–40 MB per second at 1080p | Maximum fidelity, or as a master to re-edit later. |

It is not a video editor in the timeline sense. You open a clip, pick the background
color and key style, check the result over a test backdrop, and export.

## Requirements

- Python 3.11+
- FFmpeg (`ffmpeg` and `ffprobe` on your `PATH`, built with `libvpx`; standard
  builds from ffmpeg.org, gyan.dev, Homebrew and apt all include it). To use a
  specific binary, set `PEAK_FFMPEG` / `PEAK_FFPROBE` to its full path.

## Install

```bash
python -m pip install .
```

## The app

```bash
peak-video-editor                 # opens the app in your browser
peak-video-editor clip.mp4        # opens with a clip already loaded
```

The app runs a small local server on `127.0.0.1`; nothing leaves your machine.
Drop a video onto the preview (or use **Open video**), then:

1. **Background to remove**: Black, White, or **Color**. For Color, click
   **Pick from video** and click the background in the preview (it samples a small
   patch, so noise doesn't matter). You can also type a hex value or use the color
   swatch. Pick a typical spot on the screen, not its brightest or darkest corner.
   **Remove color cast** (on by default) pulls green/blue bounce light out of hair
   and skin edges.
2. **Key style**:
   - **Soft** is a luma key for glows, fire, smoke, light leaks, sparks and particles.
     Brightness becomes opacity, so falloff stays smooth.
   - **Hard** is a crisp cutout for solid objects, logos and people. It turns on
     *Protect interior* by default.
3. **Fine tune**:
   - **Threshold** treats everything this close to the background color as fully
     transparent. Raise it if compression noise or a slightly grey "black" shows.
   - **Softness** is how gradually pixels fade from transparent to opaque.
   - **Remove edge fringe** recovers the true color of semi-transparent pixels so
     glows don't carry a dark (or light) halo over other content.
   - **Protect interior** only removes background-colored areas connected to the
     frame edge, so a black logo detail inside a white object stays solid.

   For color keys, Soft and Hard load their own starting values. Threshold and
   softness then measure distance from the picked color, weighted toward hue so
   shadows and uneven lighting on the screen are still removed.
4. Check it using **Result / Alpha matte / Original** and the backdrop buttons
   (checker, red, green, white, black). Scrub with the timeline.
5. Optionally tick **Mirror loop** (see below).
6. Pick **WebM** or **ProRes 4444** and click **Export video**. When it finishes,
   **Download** saves the file. For WebM, **Play result in preview** plays the
   exported file over the backdrop.

## Mirror loop

**Mirror loop** plays the clip forward and then in reverse, so the last frame
flows straight back into the first and the video loops with no jump. Frames are
`0, 1, … N-1, N-2, … 1`; the two turnaround frames are not repeated, which would
show as a brief stutter. A 10 s clip becomes just under 20 s. Works with every
background mode and both formats. Reversed frames are buffered compressed in
your temp folder during export, not in memory.

Mirroring suits motion that looks natural backwards: smoke, particles, light,
water, slow camera drifts. Motion with an obvious direction (walking, text
appearing, falling objects) will visibly rewind.

## Command line

```bash
peak-key intro.mp4                                  # soft key on black -> intro-alpha.webm
peak-key logo.mp4 -b white -p hard -f prores        # -> logo-alpha.mov
peak-key smoke.mp4 --threshold 0.06 --crf 24        # tweak the key and WebM quality
peak-key talent.mp4 -b "#2aa34a" -p soft            # green screen, picked color
peak-key embers.mp4 --mirror-loop                   # forward + reverse seamless loop
peak-key clips/*.mp4 -o keyed/                      # batch into a folder
peak-key --help
```

## How the key works

Each pixel is treated as `observed = alpha × foreground + (1 − alpha) × background`.
Alpha comes from the pixel's distance to the background, shaped by threshold and
softness, and the foreground color is then solved back out ("unblended").

- **Black/white:** distance is the pixel's brightest channel (on black) or darkest
  channel (on white), so the whole key is two 256×256 lookup tables.
- **Color:** distance is measured in YCbCr with brightness given half weight, so a
  shadowed or hot-spotted screen still reads as screen. Alpha comes from a 16M-entry
  table indexed by the packed RGB value (built in about 70 ms when settings change),
  and unblending uses a 256×256 table per channel. Spill suppression limits the key's
  dominant channel to the average of the other two (the classic "average" despill).

Known limits:

- Parts of a subject that match the background (black clothing on black, a green
  shirt on a green screen) also become transparent unless *Protect interior* is on,
  and that only helps for regions fully enclosed by the subject.
- Despill shifts saturated colors close to the key (yellows and teals on a green
  screen). Turn off *Remove color cast* if the subject really contains them.
- One key color per clip; there is no garbage matte or per-region keying yet.
- Exports are video only; audio is dropped (the viewer renders without audio).
- Variable-frame-rate sources are exported at their average frame rate.

## Decoding WebM alpha (important for consumers)

FFmpeg's built-in VP9 decoder **silently ignores** the alpha channel. Anything that
reads these WebM files through FFmpeg must force the libvpx decoder before the
input, e.g. `ffmpeg -c:v libvpx-vp9 -i overlay.webm ...`. ProRes 4444 needs no
special handling. `tests/test_media.py` documents both behaviours.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```
