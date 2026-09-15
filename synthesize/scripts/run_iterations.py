#!/usr/bin/env python3
"""Run N total scored proof iterations on a prepared SkySynth run.

    python3 synthesize/scripts/run_iterations.py 10 --run .skydiscover/my-run
    python3 synthesize/scripts/run_iterations.py 10 --run .skydiscover/my-run --agent fcc-claude
    python3 synthesize/scripts/run_iterations.py 10 --run .skydiscover/my-run --agent codex

With --agent codex, each lead session is `codex exec` in the run directory. The project holding
the run must be wired by `skydiscover init --agent codex` and trusted by Codex; the script checks
both before launching. --codex-sandbox off bypasses Codex's own sandbox, which on macOS cannot
host the nested sandbox-exec that proof evaluation uses under `sandbox` isolation.

Prerequisites: task.md with checked_by: proof and evaluation: scored, the formal interface,
tests/test.sh, evaluator/proof.json, and the externally retained SKYDISCOVER_PROOF_TRUST
and SKYDISCOVER_PROOF_CONTRACT values from `spec.proof freeze`. See PROOF_EVALUATION.md.
This script starts the trusted lead; that lead must isolate candidate workers as documented.
It does not bypass the agent's permission checks or install/change agent configuration.
One additional lead invocation prepares final audit/delivery after N scores; it does not
count as another scored iteration. Agent output and prompts are saved under synthesis/launcher/.

N is the total run budget, not N additional iterations on resume. New runs default to 10*N
DSA cycles and no construction wall-time limit. Existing runs retain recorded limits.
The script exits unsuccessfully if construction is exhausted before N scores, the agent
fails, or an invocation makes no counted progress. Partial work and logs are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Both the source checkout and installed package ship this script with the synthesis kit.
KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT.parent.parent))

from skydiscover.synthesize.spec import loop, proof  # noqa: E402
from skydiscover.synthesize.spec.paths import Run  # noqa: E402


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return number


def status(run: Run) -> dict:
    with loop.locked_state(run.path) as state:
        return dict(state, scored=sum(a["status"] == "scored" for a in state["attempts"]))


def invoke(command: list[str], prompt: str, run: Run, timeout: int, label: str) -> Path:
    logs = run.synthesis / "launcher"
    logs.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{label}-", suffix=".log", dir=logs)
    log = Path(name)
    log.with_suffix(".prompt.md").write_text(prompt + "\n")
    env = dict(os.environ, SKYDISCOVER_RUN=str(run.path))
    # Ensure helpers remain importable when Claude runs a shell from the run directory.
    roots = [str(KIT.parent.parent), str(KIT.parent)]
    env["PYTHONPATH"] = os.pathsep.join(
        roots + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    print(f"{label}: agent output -> {log}", flush=True)
    with (
        os.fdopen(fd, "w") as stream,
        subprocess.Popen(
            command,
            cwd=run.path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        ) as process,
    ):
        try:
            process.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(
                f"Agent interrupted or exceeded {timeout}s; progress kept. See {log}"
            ) from None
        if process.returncode:
            raise RuntimeError(f"Agent exited {process.returncode}; progress kept. See {log}")
    marker = run.synthesis / ".worker-active.json"
    if marker.exists():
        raise RuntimeError(
            "Lead exited while a candidate worker was active; progress kept. "
            f"The worker marker remains at {marker}; see {log}"
        )
    return log


def iteration_prompt(run: Run, state: dict) -> str:
    limits = state["limits"]
    return f"""Use the SkySynth workflow at {KIT / 'workflow/SKILL.md'}.
Continue the prepared scored proof run at {run.path}; do not create a new run or change task.md.
Read its plan, proof strategy/log, decision log, and synthesis/loop.json before acting.
The trusted contract is already frozen. Retain the inherited external anchor values.
Complete exactly ONE further scored iteration, then return to this controller.
Every DSA/ISA worker must be run synchronously through `spec.proof worker`; do not background it,
delegate it and end this session, or return until its report has been collected. The controller
will reject a session that exits while a worker is still active.
Current scores: {state['scored']}/{limits['iterations']}; DSA cycles: {state['cycles']}/{limits['cycles']}.
Do not reset or edit the budget record. Use loop begin only if there is no active attempt;
resume an active attempt otherwise. Reserve loop cycle before each DSA substep. Use isolated
DSA workers and ISA on stalls, preserving the pinned theorem, checker, and benchmark.
The evaluator must run proof evaluate, then checkpoint snapshot (including regressions),
followed by the required audit decision and critic feedback. Keep the best candidate selected.
If construction fails, record loop fail with the remaining obligation. On budget exhaustion,
save progress and return; never fabricate a score. Do not start another scored iteration or
perform final delivery in this invocation. Report which checkpoint was completed or why none was.
"""


#: What `skydiscover init --agent codex` writes; Codex must find all of it from the run directory.
CODEX_WIRING = (
    ".codex/agents/dsa.toml",
    ".codex/agents/isa.toml",
    ".codex/hooks.json",
    ".agents/skills/skysynth/SKILL.md",
)


def codex_command(args: argparse.Namespace) -> list[str]:
    """`codex exec` with the prompt on stdin; `invoke` starts it in the run directory."""
    command = ["codex", "exec", "--skip-git-repo-check", "--color", "never"]
    if args.codex_sandbox == "off":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command += ["--sandbox", "workspace-write"]
    if args.codex_bypass_hook_trust:
        command.append("--dangerously-bypass-hook-trust")
    if args.model:
        command += ["--model", args.model]
    return command + ["-"]


def codex_trusts(project: Path) -> bool:
    """True if the Codex user config marks this project trusted."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config = home / "config.toml"
    if not config.is_file():
        return False
    header = f"[projects.{json.dumps(str(project))}]"
    section = None
    for line in config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == header and line.replace(" ", "") == 'trust_level="trusted"':
            return True
    return False


def codex_ready(run: Run, sandbox: str) -> None:
    """Refuse to launch Codex where a session would spend budget without the roles or a checker.

    Codex silently falls back to generic agents when it cannot see the generated roles or does
    not trust the project, and on macOS its sandbox forbids the nested sandbox-exec that proof
    evaluation runs under `sandbox` isolation.
    """
    project = next(
        (
            root
            for root in (run.path, *run.path.parents)
            if all((root / name).is_file() for name in CODEX_WIRING)
        ),
        None,
    )
    if project is None:
        raise ValueError(
            f"Codex cannot see the SkySynth roles from {run.path}; "
            "run `skydiscover init --agent codex` in the project that holds the run"
        )
    if not codex_trusts(project):
        raise ValueError(
            f"Codex does not trust {project}, so it would ignore .codex/; approve it in an "
            f'interactive Codex session or set trust_level = "trusted" under '
            f'[projects."{project}"] in ~/.codex/config.toml'
        )
    root, _ = proof.trusted(run)
    isolation = json.loads((root / "boundary.json").read_text())["isolation"]
    if sys.platform == "darwin" and sandbox == "workspace-write" and isolation == "sandbox":
        raise ValueError(
            "On macOS, Codex's workspace-write sandbox forbids the nested sandbox-exec that "
            "proof evaluate needs; pass --codex-sandbox off (candidate code still runs inside "
            "the proof runner's sandbox) or freeze the contract with --isolation external"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("iterations", type=positive, help="N total scored iterations")
    parser.add_argument(
        "--run", type=Path, required=True, help="existing prepared scored proof run"
    )
    parser.add_argument("--agent", choices=["claude", "fcc-claude", "codex"], default="claude")
    parser.add_argument("--model", help="optional model name passed to the agent")
    parser.add_argument(
        "--codex-sandbox",
        choices=["workspace-write", "off"],
        default="workspace-write",
        help="codex only: Codex's shell sandbox; off bypasses it (required on macOS with sandbox isolation)",
    )
    parser.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "auto", "bypassPermissions", "dontAsk"],
        help="claude only: permission mode for the unattended lead (default: the agent's own settings)",
    )
    parser.add_argument(
        "--codex-bypass-hook-trust",
        action="store_true",
        help="codex only: run the project's hooks without Codex's persisted hook trust",
    )
    parser.add_argument("--cycles", type=positive, help="total DSA cycles (new run default: 10*N)")
    parser.add_argument("--wall-secs", type=nonnegative, help="construction time limit (0: none)")
    parser.add_argument(
        "--agent-timeout", type=positive, default=3600, help="seconds per lead invocation"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print configuration and prompt without changing files or launching",
    )
    args = parser.parse_args(argv)
    run = Run(args.run.resolve())
    try:
        if not run.task.is_file() or not run.is_proof_run() or not run.requires_evaluation():
            raise ValueError(
                "--run must have a task.md with checked_by: proof and evaluation: scored"
            )
        proof.config(run)
        if not run.test_script.is_file() or not run.interface.is_dir():
            raise ValueError(
                "Prepare synthesis/tests/test.sh and synthesis/evaluator/interface first"
            )
        old = json.loads(run.loop_state.read_text()) if run.loop_state.exists() else {}
        previous = old.get("limits", {})
        limits = {
            "iterations": args.iterations,
            "cycles": (
                args.cycles
                if args.cycles is not None
                else previous.get("cycles", 10 * args.iterations)
            ),
            "wall_secs": (
                args.wall_secs if args.wall_secs is not None else previous.get("wall_secs", 0)
            ),
        }
        if old and previous != limits:
            raise ValueError(f"Existing budget is {previous}; resume with the same N and limits")
        if args.agent == "codex":
            command = codex_command(args)
        else:
            # The session starts in the run directory; it must reach SKILL.md and scripts/.
            command = [
                args.agent,
                "-p",
                "--output-format",
                "text",
                "--plugin-dir",
                str(KIT / "workflow"),
                "--add-dir",
                str(KIT),
            ]
            if args.permission_mode:
                command += ["--permission-mode", args.permission_mode]
            if args.model:
                command += ["--model", args.model]
        if args.dry_run:
            state = dict(old or {"cycles": 0, "attempts": []}, limits=limits)
            state["scored"] = sum(a["status"] == "scored" for a in state["attempts"])
            print(f"Run: {run.path}\nBudget: {json.dumps(limits)}\nLaunch: {shlex.join(command)}")
            if state["scored"] < args.iterations:
                print(iteration_prompt(run, state))
            else:
                print(
                    "The scored budget is already complete; only final audit/verification/export remains."
                )
            print(
                "Dry run: no files changed. Actual execution requires the external proof anchor and an authenticated agent."
            )
            return 0
        if not shutil.which(args.agent):
            raise ValueError(f"Missing agent executable: {args.agent}")
        proof.trusted(run)
        if args.agent == "codex":
            codex_ready(run, args.codex_sandbox)
        initial = loop.initialize(
            run.path, limits["iterations"], limits["cycles"], limits["wall_secs"]
        )
        started = initial["started"]
        while True:
            before = status(run)
            if before["limits"] != limits or before["started"] != started:
                raise ValueError("The agent changed the persisted budget; refusing to continue")
            if before["cycles"] > limits["cycles"]:
                raise ValueError("The DSA cycle limit was exceeded")
            print(
                f"Progress: {before['scored']}/{args.iterations} scored, {before['cycles']}/{limits['cycles']} DSA cycles",
                flush=True,
            )
            if before["scored"] >= args.iterations:
                if before["scored"] != args.iterations:
                    raise ValueError("The scored iteration limit was exceeded")
                break
            exhausted = before["cycles"] >= limits["cycles"] or (
                limits["wall_secs"] and time.time() - started >= limits["wall_secs"]
            )
            if exhausted and before["active"] is None:
                raise ValueError(
                    f"Construction budget exhausted at {before['scored']}/{args.iterations} scored iterations; progress and best candidate kept"
                )
            log = invoke(
                command,
                iteration_prompt(run, before),
                run,
                args.agent_timeout,
                f"iteration-{before['scored'] + 1}",
            )
            proof.trusted(run)
            after = status(run)
            if after["scored"] < before["scored"] or after["cycles"] < before["cycles"]:
                raise ValueError("The agent reset a progress counter")
            if after["scored"] == before["scored"] and after["cycles"] == before["cycles"]:
                raise ValueError(
                    f"Agent made no counted progress; stopping instead of retrying indefinitely. See {log}"
                )
        # Finalization consumes no scored iteration. The helper independently verifies publication.
        invoke(
            command,
            f"""Follow {KIT / 'workflow/SKILL.md'} for final delivery of {run.path}.
The run has reached {args.iterations} scored iterations. Do not begin any new attempt or DSA cycle.
Preserve unfinished work, restore the selected best with checkpoint restore-best if needed,
perform the final audit and stamp it, and write report.md with counts, stop reason, and obligations.
Keep the run directory for replay. Return after preparing final delivery; this controller will
independently verify and export it. Do not change the frozen contract or budget.
""",
            run,
            args.agent_timeout,
            "finalize",
        )
        final = status(run)
        if (
            final["limits"] != limits
            or final["started"] != started
            or final["scored"] != args.iterations
            or final["cycles"] != before["cycles"]
        ):
            raise ValueError("Finalization changed the iteration/cycle budget or count")
        from skydiscover.synthesize.spec.run import export_deliverable

        for line in export_deliverable(run.path, run.path.parent):
            print(line)
        print(
            f"Completed {args.iterations}/{args.iterations} scored iterations. Run and logs kept at {run.path}"
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"run_iterations: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
