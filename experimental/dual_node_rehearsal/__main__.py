"""Offline dual-node rehearsal. Never authorizes real booking."""
import argparse
import json
from pathlib import Path
import time

from .contracts import NODE_IDS, RehearsalError, envelope, intent_identity, safe_read_json, write_json


def _output_ready(value: str | None) -> None:
    if value is None:
        return
    path = Path(value)
    if (not path.is_absolute() or path.exists() or path.is_symlink()
            or not path.parent.is_dir() or any(p.is_symlink() for p in path.parents)):
        raise RehearsalError("output_unavailable")


def _selection(args: argparse.Namespace) -> tuple[dict, dict]:
    from .selection import propose
    intent = safe_read_json(args.intent)
    reports = [safe_read_json(args.mac_report), safe_read_json(args.ubuntu_report)]
    proposal = propose(reports, intent, eligible_nodes=args.eligible,
                       incumbent=args.incumbent, now=time.time())
    return intent, proposal


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RehearsalError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize a NEW private rehearsal ledger")
    init.add_argument("--state", required=True)
    evaluate = commands.add_parser("evaluate", help="compare imported reports without a ledger mutation")
    freeze = commands.add_parser("freeze", help="freeze one simulation owner in the local ledger")
    for command in (evaluate, freeze):
        command.add_argument("--intent", required=True)
        command.add_argument("--mac-report", required=True)
        command.add_argument("--ubuntu-report", required=True)
        command.add_argument("--eligible", nargs="+", required=True, choices=NODE_IDS,
                             help="explicit simulated eligibility; does not verify a school account")
        command.add_argument("--incumbent", required=True, choices=NODE_IDS)
    freeze.add_argument("--state", required=True)
    consume = commands.add_parser("consume", help="consume an assignment once; sends no request")
    finish = commands.add_parser("finish", help="record a terminal simulation outcome")
    for command in (consume, finish):
        command.add_argument("--state", required=True)
        command.add_argument("--assignment", required=True)
        command.add_argument("--node", required=True, choices=NODE_IDS)
    finish.add_argument("--status", required=True, choices=("revoked", "uncertain", "completed"))
    inspect = commands.add_parser("inspect", help="read a retained rehearsal assignment")
    inspect.add_argument("--state", required=True)
    inspect.add_argument("--task-id", required=True)
    demo = commands.add_parser("demo", help="local child-process simulation with synthetic reports and virtual time")
    demo.add_argument("--directory", required=True, help="new absolute evidence directory")
    for command in (init, evaluate, freeze, consume, finish, inspect, demo):
        command.add_argument("--output", help="new absolute private JSON file; never overwrite")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        _output_ready(args.output)
        if args.command == "demo":
            from .demo import run_demo
            result = run_demo(Path(args.directory))
        elif args.command == "evaluate":
            _, result = _selection(args)
        else:
            from .ledger import Authority
            if args.command == "init":
                with Authority.initialize(Path(args.state)):
                    result = envelope(status="initialized", reason="rehearsal_ledger_created")
            elif args.command == "freeze":
                intent, proposal = _selection(args)
                with Authority(Path(args.state)) as authority:
                    existing = authority.inspect(intent_identity(intent)[0])
                    result = (proposal if proposal.get("status") == "blocked" and existing is None
                              else authority.assign(intent, proposal, now=time.time()))
            else:
                with Authority(Path(args.state)) as authority:
                    if args.command == "inspect":
                        result = authority.inspect(args.task_id) or envelope(status="not_found", reason="unknown_task")
                    else:
                        assignment = safe_read_json(args.assignment)
                        if args.command == "consume":
                            result = authority.consume(assignment, node=args.node, now=time.time())
                        else:
                            result = authority.finish(assignment, node=args.node, status=args.status, now=time.time())
        if args.output:
            write_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 3 if result.get("status") == "blocked" else 0
    except (RehearsalError, OSError, ValueError) as exc:
        reason = exc.code if isinstance(exc, RehearsalError) else "rehearsal_failed"
        print(json.dumps(envelope(status="error", reason=reason), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
