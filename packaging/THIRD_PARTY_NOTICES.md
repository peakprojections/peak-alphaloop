# Third-party software in Peak Alphaloop downloads

Peak Alphaloop itself is released under the MIT License (see `LICENSE.txt`).
The standalone downloads also contain the following software.

## FFmpeg (GPL)

The `ffmpeg` and `ffprobe` programs shipped with Peak Alphaloop are unmodified
static builds of [FFmpeg](https://ffmpeg.org), configured with `--enable-gpl`
(they include libx264 and other GPL components), and are therefore licensed
under the GNU General Public License, version 3 or later. The full license text
is in `GPL-3.0.txt`, and the exact version and build configuration of the
bundled binaries are recorded in `FFmpeg-build.txt`.

Peak Alphaloop runs FFmpeg as a separate program; it does not link to it.

Corresponding source code:

- FFmpeg source releases: https://ffmpeg.org/download.html#releases and
  https://git.ffmpeg.org/ffmpeg.git (the commit/version is in `FFmpeg-build.txt`)
- Windows and Linux builds: https://github.com/BtbN/FFmpeg-Builds (build
  scripts and the exact sources of every bundled library)
- macOS builds: https://ffmpeg.martin-riedl.de (build scripts:
  https://git.martin-riedl.de/ffmpeg/build-script)

If any of these links stop working, email jesse@peakprojections.com and we will
provide the complete corresponding source code for the FFmpeg binaries in any
version of Peak Alphaloop we distributed, for at least three years after that
release.

Codec note: H.264 and ProRes are covered by patents and trademarks held by
third parties. Peak Alphaloop is free software provided as-is; you are
responsible for any licensing that applies to how you use the files you create.

## Python and libraries

The standalone builds embed the Python runtime (PSF License) and these
packages, each under its own permissive license: NumPy (BSD-3-Clause),
SciPy (BSD-3-Clause), Pillow (MIT-CMU), certifi (MPL-2.0), and the PyInstaller
bootloader (GPL-2.0 with an exception that permits distributing applications
under any license).
