"""Launch the existing N-iteration controller using an externally retained anchor."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from prepare import KIT, arguments, controller_path, prepare

from skydiscover.synthesize.spec import proof
from skydiscover.synthesize.spec.paths import Run

PERMISSION_MODES = ["acceptEdits", "auto", "bypassPermissions", "dontAsk"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    # Workload overrides apply only when explicitly supplied, including on resume.
    parser.set_defaults(
        load_count=None, run_count=None, seconds=None, repeats=None, seed=None, held_out_seed=None
    )
    parser.add_argument("--agent", choices=["claude", "fcc-claude", "codex"], default="claude")
    parser.add_argument("--model")
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument(
        "--codex-sandbox", choices=["workspace-write", "off"], default="workspace-write"
    )
    parser.add_argument("--codex-bypass-hook-trust", action="store_true")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run = Run(args.run.resolve())
    trust = args.trust_root.resolve()
    if args.dry_run:
        print(
            f"Run: {run.path}\nExternal trust: {trust}\nAgent: {args.agent}\nIterations: {args.iterations}"
        )
        print(
            "Would prepare a fresh scored run or validate its original controller anchor, then invoke run_iterations.py. No files changed or agents launched."
        )
        return
    if not run.path.exists():
        options = {
            key: value
            for key, value in vars(args).items()
            if value is not None
            and key
            not in {
                "run",
                "trust_root",
                "agent",
                "model",
                "agent_timeout",
                "codex_sandbox",
                "codex_bypass_hook_trust",
                "permission_mode",
                "prepare_only",
                "dry_run",
            }
        }
        controller = prepare(run.path, trust, **options)
    else:
        if args.seed_from:
            raise ValueError("--seed-from is for new runs only")
        controller = json.loads(controller_path(trust).read_text())
    if controller["run"] != str(run.path) or controller["trust"] != str(trust):
        raise ValueError("Controller anchor belongs to another run")
    if (
        controller["iterations"] != args.iterations
        or (args.cycles is not None and controller["cycles"] != args.cycles)
        or controller["wall_secs"] != args.wall_secs
    ):
        raise ValueError("Resume must retain the original iteration/cycle/time budgets")
    os.environ.update(
        SKYDISCOVER_PROOF_TRUST=str(trust), SKYDISCOVER_PROOF_CONTRACT=controller["contract"]
    )
    proof.trusted(run)
    config = proof.config(run)["config"]
    for name in ("load_count", "run_count", "seconds", "repeats", "seed", "held_out_seed"):
        value = getattr(args, name)
        if value is not None and value != config[name]:
            raise ValueError(f"Cannot change frozen workload on resume: {name}")
    settings = json.loads((run.interface / "toolchain.json").read_text())
    for name, value in (("verus", args.verus), ("z3", args.z3)):
        if (
            value is not None
            and str(Path(shutil.which(value) or value).resolve()) != settings[name]
        ):
            raise ValueError(f"Cannot change frozen toolchain on resume: {name}")
    if args.prepare_only:
        print(f"Prepared: {run.path}\nController: {controller_path(trust)}")
        return
    command = [
        sys.executable,
        str(KIT / "scripts/run_iterations.py"),
        str(args.iterations),
        "--run",
        str(run.path),
        "--agent",
        args.agent,
        "--agent-timeout",
        str(args.agent_timeout),
    ]
    if args.model:
        command += ["--model", args.model]
    if args.permission_mode and args.agent != "codex":
        command += ["--permission-mode", args.permission_mode]
    if args.agent == "codex":
        command += ["--codex-sandbox", args.codex_sandbox]
        if args.codex_bypass_hook_trust:
            command.append("--codex-bypass-hook-trust")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
