from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .pipeline import BuildFailed, build_release, check_workspace, verify_release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracepress",
        description="Validate and package a traceable, offline-first sample publication.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Run quality gates without writing output.")
    check.add_argument("--workspace", type=Path, default=Path("sample"), help="Workspace containing input/ (default: sample).")

    build = subparsers.add_parser("build", help="Run gates and create deterministic release artifacts.")
    build.add_argument("--workspace", type=Path, default=Path("sample"), help="Workspace containing input/ (default: sample).")
    build.add_argument("--output", type=Path, default=Path("sample/output"), help="Generated artifact directory (default: sample/output).")

    verify = subparsers.add_parser("verify", help="Verify generated files against CHECKSUMS.sha256.")
    verify.add_argument("--output", type=Path, default=Path("sample/output"), help="Generated artifact directory (default: sample/output).")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        report = check_workspace(args.workspace)
        _print(report)
        return 0 if report["summary"]["status"] == "pass" else 1
    if args.command == "build":
        try:
            manifest = build_release(args.workspace, args.output)
        except BuildFailed as exc:
            _print(exc.report)
            print(str(exc), file=sys.stderr)
            return 1
        _print(
            {
                "status": "pass",
                "release_id": manifest["release"]["release_id"],
                "artifacts": [item["path"] for item in manifest["artifacts"]]
                + ["release-manifest.json", "CHECKSUMS.sha256"],
            }
        )
        return 0
    report = verify_release(args.output)
    _print(report)
    return 0 if report["summary"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
