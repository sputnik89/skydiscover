"""Persistent outer candidate attempts and inner DSA budgets. Only the lead writes this budget record.

python -m skydiscover.synthesize.spec.loop init RUN --iterations 10 --cycles 100
python -m skydiscover.synthesize.spec.loop begin RUN
python -m skydiscover.synthesize.spec.loop cycle RUN
python -m skydiscover.synthesize.spec.loop fail RUN --reason 'unclosed obligation'
python -m skydiscover.synthesize.spec.loop status RUN

Reserve a cycle BEFORE dispatching DSA. A crashed worker still consumes its reservation.
ISA never resets counters. Scored checkpoints complete attempts, including regressions.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .paths import Run, run_cli


@contextmanager
def locked_state(run_dir: Path) -> Iterator[dict]:
    path = Run(run_dir).loop_state
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".loop.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        # Recover the narrow crash window between publishing a checkpoint and completing its
        # budget record row. The checkpoint is the durable scored event, never a guessed counter.
        if state:
            from . import checkpoint as artifacts

            out = artifacts.published_output(run_dir)
            for cp in sorted((out / "checkpoints").glob("checkpoint_*")) if out else []:
                score = artifacts.read_score(cp)
                number = score.get("attempt")
                if (
                    type(number) is int
                    and 1 <= number <= len(state["attempts"])
                    and score.get("score")
                ):
                    attempt = state["attempts"][number - 1]
                    if attempt["status"] == "constructing" and state["active"] == number:
                        attempt.update(status="scored", checkpoint=str(cp.resolve()))
                        state["active"] = None
        yield state
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".loop-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, indent=2)
                stream.write("\n")
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def initialize(run_dir: Path, iterations: int, cycles: int, wall_secs: int = 0) -> dict:
    if iterations < 1 or cycles < 1 or wall_secs < 0:
        raise ValueError("iterations and cycles must be positive; wall-secs cannot be negative")
    limits = {"iterations": iterations, "cycles": cycles, "wall_secs": wall_secs}
    with locked_state(run_dir) as state:
        if state:
            if state["limits"] != limits:
                raise ValueError("A resumed run cannot reset its limits; use the recorded budget")
        else:
            state.update(limits=limits, started=time.time(), cycles=0, attempts=[], active=None)
        return state


def _require(state: dict) -> None:
    if not state:
        raise ValueError("Initialize the loop budget before constructing a candidate")


def _scored(state: dict) -> int:
    return sum(a["status"] == "scored" for a in state["attempts"])


def _time_check(state: dict) -> None:
    seconds = state["limits"]["wall_secs"]
    if seconds and time.time() - state["started"] >= seconds:
        raise ValueError("Construction time budget exhausted; preserve progress and best candidate")


def active(state: dict) -> dict:
    _require(state)
    if state["active"] is None:
        raise ValueError("No active candidate attempt; begin one first")
    return state["attempts"][state["active"] - 1]


def begin(run_dir: Path) -> int:
    with locked_state(run_dir) as state:
        _require(state)
        _time_check(state)
        if state["active"] is not None:
            raise ValueError("Resume or fail the active candidate before starting another")
        if _scored(state) >= state["limits"]["iterations"]:
            raise ValueError("Scored iteration budget exhausted")
        if state["cycles"] >= state["limits"]["cycles"]:
            raise ValueError("DSA cycle budget exhausted")
        number = len(state["attempts"]) + 1
        state["attempts"].append({"id": number, "status": "constructing", "cycles": []})
        state["active"] = number
        return number


def cycle(run_dir: Path) -> dict:
    with locked_state(run_dir) as state:
        attempt = active(state)
        _time_check(state)
        if state["cycles"] >= state["limits"]["cycles"]:
            raise ValueError("DSA cycle budget exhausted")
        state["cycles"] += 1
        attempt["cycles"].append(state["cycles"])
        return {"attempt": attempt["id"], "cycle": state["cycles"]}


def fail(run_dir: Path, reason: str) -> None:
    if not reason.strip():
        raise ValueError("Record the remaining obligation or failure reason")
    with locked_state(run_dir) as state:
        attempt = active(state)
        attempt.update(status="failed", reason=reason)
        state["active"] = None


def pending(run_dir: Path) -> int:
    """An already-started attempt may finish after its construction budget is spent."""
    with locked_state(run_dir) as state:
        attempt = active(state)
        if _scored(state) >= state["limits"]["iterations"]:
            raise ValueError("Scored iteration budget exhausted")
        if not attempt["cycles"]:
            raise ValueError("A proof candidate requires at least one reserved DSA cycle")
        return attempt["id"]


def complete(run_dir: Path, checkpoint: Path, attempt_id: int) -> None:
    with locked_state(run_dir) as state:
        _require(state)
        attempt = state["attempts"][attempt_id - 1]
        if attempt.get("checkpoint") == str(checkpoint.resolve()):
            return
        if state["active"] != attempt_id or attempt["status"] != "constructing":
            raise ValueError("Checkpoint does not belong to the active candidate")
        attempt.update(status="scored", checkpoint=str(checkpoint.resolve()))
        state["active"] = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["init", "begin", "cycle", "fail", "status"])
    ap.add_argument("run", type=Path)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--cycles", type=int, default=100)
    ap.add_argument("--wall-secs", type=int, default=0)
    ap.add_argument("--reason", default="")
    args = ap.parse_args(argv)
    if args.action == "init":
        result = initialize(args.run, args.iterations, args.cycles, args.wall_secs)
    elif args.action == "begin":
        result = {"attempt": begin(args.run)}
    elif args.action == "cycle":
        result = cycle(args.run)
    elif args.action == "fail":
        fail(args.run, args.reason)
        result = {"status": "failed", "reason": args.reason}
    else:
        with locked_state(args.run) as state:
            _require(state)
            result = dict(state, scored_iterations=_scored(state))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "loop"))
