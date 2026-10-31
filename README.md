# Peak Video Editor

A small Peak Projections tool that turns a **black or white video background into a
transparent alpha channel** and saves it in a format the Peak viewer can layer over
other surfaces:

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

1. **Background to remove**: Black or White.
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
4. Check it using **Result / Alpha matte / Original** and the backdrop buttons
   (checker, red, green, white, black). Scrub with the timeline.
5. Pick **WebM** or **ProRes 4444** and click **Export video**. When it finishes,
   **Download** saves the file. For WebM, **Play result in preview** plays the
   exported file over the backdrop.

## Command line

```bash
peak-key intro.mp4                                  # soft key on black -> intro-alpha.webm
peak-key logo.mp4 -b white -p hard -f prores        # -> logo-alpha.mov
peak-key smoke.mp4 --threshold 0.06 --crf 24        # tweak the key and WebM quality
peak-key clips/*.mp4 -o keyed/                      # batch into a folder
peak-key --help
```

## How the key works

Each pixel is treated as `observed = alpha × foreground + (1 − alpha) × background`,
with the background pure black or white. Alpha comes from the pixel's distance to
the background (its brightest channel on black, its darkest on white), shaped by
threshold and softness. The foreground color is then solved back out ("unblended").
Because both depend only on a pixel's max/min channel and its own channel value,
the key is precomputed into 256×256 lookup tables, which keeps it fast.

Known limits:

- Parts of a subject that match the background (black clothing on black) also
  become transparent unless *Protect interior* is on, and that only helps for
  regions fully enclosed by the subject.
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
