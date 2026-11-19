"""Run offline fixtures, export/import review sheets, and regenerate summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from .adapters import CLIAdapterStub, ScriptedAdapter, ScriptedTurn
from .config import BenchmarkConfig, load_config, read_data
from .grading import apply_sheets, export_sheets
from .results import load_results, write_results
from .runner import run_benchmark
from .summary import write_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print the typed configuration JSON Schema")
    run = commands.add_parser("run", help="Run a local fixture with the scripted adapter")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True, help="New scratch directory")
    run.add_argument("--adapter", choices=("scripted", "claude-code", "codex"), default="scripted")
    run.add_argument("--script", type=Path, help="Optional YAML/JSON list of scripted replies")
    export = commands.add_parser("export-review", help="Write per-session review sheets")
    export.add_argument("--results", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True, help="New directory of CSV sheets")
    export.add_argument("--unblind", action="store_true")
    review = commands.add_parser("review", help="Import reviewed sheets into a new result set")
    review.add_argument("--results", type=Path, required=True)
    review.add_argument(
        "--sheets", type=Path, nargs="+", required=True, help="CSV files or directories"
    )
    review.add_argument("--reviewer", default="manual")
    review.add_argument(
        "--output", type=Path, required=True, help="New directory; raw results are preserved"
    )
    summary = commands.add_parser("summarize")
    summary.add_argument("--results", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True, help="New directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            print(json.dumps(BenchmarkConfig.model_json_schema(), indent=2))
        elif args.command == "run":
            config = load_config(args.config)
            turns = (
                ()
                if args.script is None
                else TypeAdapter(tuple[ScriptedTurn, ...]).validate_python(read_data(args.script))
            )
            adapter = (
                ScriptedAdapter(turns)
                if args.adapter == "scripted"
                else CLIAdapterStub(args.adapter)
            )
            run_benchmark(
                config, output=args.output, adapter=adapter, config_directory=args.config.parent
            )
            print(f"Wrote {args.output / 'results.jsonl'} (scripted dry run; no model calls)")
        elif args.command == "export-review":
            args.output.mkdir(parents=True)
            export_sheets(load_results(args.results), args.output, blind=not args.unblind)
        else:
            records = load_results(args.results)
            if args.command == "review":
                records = apply_sheets(records, args.sheets, reviewer=args.reviewer)
            args.output.mkdir(parents=True)
            if args.command == "review":
                write_results(args.output / "results.jsonl", records)
            write_outputs(records, args.output)
    except (ValueError, OSError, NotImplementedError, RuntimeError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
