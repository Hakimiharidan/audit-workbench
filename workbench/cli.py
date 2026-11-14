"""Command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from workbench import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description="Audit Analytics Workbench")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    vm = sub.add_parser("validate-mapping", help="check a mapping file without loading data")
    vm.add_argument("mapping", type=Path)
    vm.add_argument("--packs", type=Path, default=None, help="packs folder (default: bundled packs)")

    run = sub.add_parser("run", help="run the integrity gate and test packs")
    run.add_argument("mapping", type=Path)
    run.add_argument("--config", type=Path, required=True, help="config.yaml with materiality")
    run.add_argument("--out", type=Path, required=True, help="output folder")
    run.add_argument("--engagement", default="", help="engagement name for the Cover sheet")
    run.add_argument("--force", action="store_true", help="continue even if the integrity gate fails (recorded)")
    run.add_argument("--packs", type=Path, default=None, help="packs folder (default: bundled packs)")
    rv = sub.add_parser("review", help="open the local review screen for a run's output folder (needs .[review])")
    rv.add_argument("out", type=Path, help="the --out folder of a previous run")
    rv.add_argument("--port", type=int, default=8501)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate-mapping":
        return _validate(args)
    if args.command == "run":
        return _run(args)
    if args.command == "review":
        return _review(args)
    parser.print_help()
    return 1


def _validate(args) -> int:
    from workbench.mapping import validate_mapping
    from workbench.packs import DEFAULT_PACKS_ROOT

    issues = validate_mapping(args.mapping, args.packs or DEFAULT_PACKS_ROOT)
    for issue in issues:
        print(issue)
    if issues:
        print(f"{len(issues)} problem(s) found in {args.mapping}")
        return 1
    print(f"OK: {args.mapping} is valid")
    return 0


def _run(args) -> int:
    from workbench.pipeline import run_workbench

    outcome = run_workbench(
        args.mapping, args.config, args.out,
        engagement=args.engagement, force=args.force, packs_root=args.packs,
    )
    for line in outcome.messages:
        print(line)
    return 0 if outcome.status == "ok" else 1


OFFLINE_FLAGS = [
    "--browser.gatherUsageStats=false",
    "--server.address=localhost",
    "--server.headless=true",
    "--client.toolbarMode=minimal",
    "--global.developmentMode=false",
]


def _streamlit_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("streamlit") is not None


def review_command(out: Path, port: int = 8501) -> tuple[list[str], dict]:
    """The exact command and environment used to launch the review screen, offline settings forced."""
    import os
    import sys

    app = Path(__file__).with_name("review_app.py")
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), *OFFLINE_FLAGS, f"--server.port={port}"]
    env = dict(os.environ)
    env["WORKBENCH_REVIEW_DIR"] = str(Path(out).resolve())
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["STREAMLIT_SERVER_ADDRESS"] = "localhost"
    return cmd, env


def _review(args) -> int:
    import subprocess

    from workbench.review import ReviewError, current_path

    try:
        current_path(args.out)
    except ReviewError as exc:
        print(exc)
        return 1
    if not _streamlit_available():
        print("The review screen is an optional extra. Install it with: pip install .[review]")
        return 1
    cmd, env = review_command(args.out, args.port)
    print(f"Review screen: http://localhost:{args.port}  (Ctrl+C to stop; runs only on this computer)")
    return subprocess.call(cmd, env=env)
