"""alphaloop-cli: Peak Alphaloop from the command line.

Remove a background to alpha, trim, reverse, join clips and make mirror loops
without opening the app.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .keying import PRESETS, preset
from .media import (FORMATS, MediaError, Segment, default_output_path, export_timeline, probe,
                    read_alpha_range)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alphaloop-cli",
        description="Remove a black, white or green-screen background to transparency, trim, "
                    "reverse, join clips and make seamless mirror loops. Saves WebM (VP9), "
                    "ProRes, or MP4.",
        epilog="Examples:\n"
               "  alphaloop-cli smoke.mp4                         # black -> transparent WebM\n"
               "  alphaloop-cli talent.mp4 -b '#2aa34a' -p hard   # green screen\n"
               "  alphaloop-cli waves.mp4 -n -m                   # just a seamless loop (MP4)\n"
               "  alphaloop-cli a.mp4 b.mp4 --join -n -o out.mp4  # join two clips\n"
               "  alphaloop-cli clip.mp4 --start 2 --end 5.5 --reverse -n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, nargs="+", help="Source video file(s).")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output file (single input) or folder (several inputs). "
                             "Defaults to <name>-alpha.webm/.mov next to the source.")
    parser.add_argument("-b", "--background", default="black", metavar="black|white|#RRGGBB",
                        help="Background to remove: black, white, or a hex color such as "
                             "#00b140 for a green screen (default black).")
    parser.add_argument("-p", "--preset", choices=sorted(PRESETS), default="soft",
                        help="soft = glows/smoke/light (luma key); hard = solid objects.")
    parser.add_argument("-f", "--format", choices=sorted(FORMATS), default=None,
                        help="webm = small VP9 file (default); prores = large ProRes master; "
                             "mp4 = H.264, only with --no-key (default with --no-key).")
    parser.add_argument("-n", "--no-key", action="store_true",
                        help="Keep the background (no transparency); e.g. just make a mirror loop.")
    parser.add_argument("--threshold", type=float, help="0-1. Override the preset threshold.")
    parser.add_argument("--softness", type=float, help="0-1. Override the preset softness.")
    parser.add_argument("--protect-interior", dest="protect_interior",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Only key background regions touching the frame edge.")
    parser.add_argument("--unblend", action=argparse.BooleanOptionalAction, default=None,
                        help="Recover edge colors to remove dark/light fringes (default on).")
    parser.add_argument("--despill", action=argparse.BooleanOptionalAction, default=None,
                        help="Color keys only: remove the key color's cast from the subject (default on).")
    parser.add_argument("-m", "--mirror-loop", action="store_true",
                        help="Play forward then backward so the export loops seamlessly "
                             "(about double the length).")
    parser.add_argument("--start", type=float, default=0.0, metavar="SECONDS",
                        help="Trim: start each input at this time.")
    parser.add_argument("--end", type=float, default=None, metavar="SECONDS",
                        help="Trim: stop each input at this time.")
    parser.add_argument("--reverse", action="store_true", help="Play the clip(s) backwards.")
    parser.add_argument("-j", "--join", action="store_true",
                        help="Join all inputs, in order, into one output (size and frame rate of "
                             "the first). Without it, each input is exported separately.")
    parser.add_argument("-a", "--audio", action="store_true",
                        help="Include audio (dropped by default).")
    parser.add_argument("--crf", type=int, default=30, help="WebM quality, 15 (best) to 50. Default 30.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.format is None:
        args.format = "mp4" if args.no_key else "webm"
    if args.format == "mp4" and not args.no_key:
        print("alphaloop-cli: MP4 cannot hold transparency; use --no-key or choose webm/prores.",
              file=sys.stderr)
        return 2
    if args.end is not None and args.end <= args.start:
        print("alphaloop-cli: --end must be after --start.", file=sys.stderr)
        return 2
    background = args.background.strip().lower()
    try:
        if background in ("black", "white"):
            settings = preset(args.preset, background)  # type: ignore[arg-type]
        else:
            settings = preset(args.preset, "color", background)
    except ValueError:
        print("alphaloop-cli: --background must be black, white or a #RRGGBB color.", file=sys.stderr)
        return 2
    overrides = {key: getattr(args, key) for key in
                 ("threshold", "softness", "protect_interior", "unblend", "despill")
                 if getattr(args, key) is not None}
    settings = replace(settings, **overrides).validated()

    many_outputs = len(args.input) > 1 and not args.join
    if many_outputs and args.output is not None and args.output.suffix:
        print("alphaloop-cli: with several inputs, --output must be a folder (or use --join).",
              file=sys.stderr)
        return 2

    def segment(path: Path) -> Segment:
        return Segment(path, args.start, args.end, args.reverse)

    jobs = ([(args.input, args.input[0])] if args.join else [([path], path) for path in args.input])
    failures = 0
    for sources, name_from in jobs:
        destination = _destination(name_from, args.output, args.format, many_outputs,
                                   not args.no_key, args.mirror_loop)
        try:
            info = probe(sources[0])
            label = " + ".join(path.name for path in sources)
            print(f"{label}: {info.width}x{info.height} @ {float(info.frame_rate):.3g} fps -> "
                  f"{destination}")
            export_timeline([segment(path) for path in sources], destination, settings,
                            args.format, webm_crf=args.crf, progress=_progress_printer(),
                            mirror_loop=args.mirror_loop, remove_background=not args.no_key,
                            include_audio=args.audio)
            if args.no_key:
                print("\n  done.")
            else:
                low, high = read_alpha_range(destination)
                print(f"\n  done. alpha range in first frame: {low}-{high}")
        except MediaError as error:
            failures += 1
            print(f"\nalphaloop-cli: {name_from.name}: {error}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nalphaloop-cli: cancelled.", file=sys.stderr)
            return 130
    return 1 if failures else 0


def _destination(source: Path, output: Path | None, export_format: str, many: bool,
                 remove_background: bool = True, mirror_loop: bool = False) -> Path:
    default = default_output_path(source, export_format,  # type: ignore[arg-type]
                                  remove_background, mirror_loop)
    if output is None:
        return default
    if many or output.is_dir() or not output.suffix:
        return output / default.name
    return output


def _progress_printer():
    last = [-1]

    def report(done: int, total: int) -> None:
        percent = int(done * 100 / max(total, 1))
        if percent != last[0]:
            last[0] = percent
            print(f"\r  {percent:3d}%  ({done}/{total} frames)", end="", flush=True)

    return report


if __name__ == "__main__":
    raise SystemExit(main())
