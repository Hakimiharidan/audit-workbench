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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate-mapping":
        return _validate(args)
    if args.command == "run":
        return _run(args)
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
