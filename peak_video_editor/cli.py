"""peak-key: key a black or white background to alpha from the command line."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .keying import PRESETS, preset
from .media import FORMATS, MediaError, default_output_path, export, probe, read_alpha_range


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peak-key",
        description="Turn a black or white video background into a transparent alpha channel "
                    "and save it as WebM (VP9) or ProRes 4444.",
    )
    parser.add_argument("input", type=Path, nargs="+", help="Source video file(s).")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output file (single input) or folder (several inputs). "
                             "Defaults to <name>-alpha.webm/.mov next to the source.")
    parser.add_argument("-b", "--background", choices=["black", "white"], default="black")
    parser.add_argument("-p", "--preset", choices=sorted(PRESETS), default="soft",
                        help="soft = glows/smoke/light (luma key); hard = solid objects.")
    parser.add_argument("-f", "--format", choices=sorted(FORMATS), default="webm",
                        help="webm = small VP9 file; prores = large ProRes 4444 master.")
    parser.add_argument("--threshold", type=float, help="0-1. Override the preset threshold.")
    parser.add_argument("--softness", type=float, help="0-1. Override the preset softness.")
    parser.add_argument("--protect-interior", dest="protect_interior",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Only key background regions touching the frame edge.")
    parser.add_argument("--unblend", action=argparse.BooleanOptionalAction, default=None,
                        help="Recover edge colors to remove dark/light fringes (default on).")
    parser.add_argument("--crf", type=int, default=30, help="WebM quality, 15 (best) to 50. Default 30.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = preset(args.preset, args.background)
    overrides = {key: getattr(args, key) for key in ("threshold", "softness", "protect_interior", "unblend")
                 if getattr(args, key) is not None}
    settings = replace(settings, **overrides).validated()

    if len(args.input) > 1 and args.output is not None and args.output.suffix:
        print("peak-key: with several inputs, --output must be a folder.", file=sys.stderr)
        return 2

    failures = 0
    for source in args.input:
        destination = _destination(source, args.output, args.format, len(args.input) > 1)
        try:
            info = probe(source)
            print(f"{source.name}: {info.width}x{info.height} @ {float(info.frame_rate):.3g} fps, "
                  f"{info.frame_count} frames -> {destination}")
            export(source, destination, settings, args.format, webm_crf=args.crf,
                   progress=_progress_printer())
            low, high = read_alpha_range(destination)
            print(f"\n  done. alpha range in first frame: {low}-{high}")
        except MediaError as error:
            failures += 1
            print(f"\npeak-key: {source.name}: {error}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\npeak-key: cancelled.", file=sys.stderr)
            return 130
    return 1 if failures else 0


def _destination(source: Path, output: Path | None, export_format: str, many: bool) -> Path:
    if output is None:
        return default_output_path(source, export_format)  # type: ignore[arg-type]
    if many or output.is_dir() or not output.suffix:
        return output / default_output_path(source, export_format).name  # type: ignore[arg-type]
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
