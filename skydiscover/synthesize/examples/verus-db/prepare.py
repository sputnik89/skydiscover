"""Prepare a fresh scored database run and an external controller anchor."""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[1]
sys.path.insert(
    0, str(KIT.parent if (KIT.parent / "pyproject.toml").is_file() else KIT.parent.parent)
)

from skydiscover.synthesize.spec import loop, proof
from skydiscover.synthesize.spec.paths import Run


def tools_config(verus=None, z3=None):
    binary = shutil.which(verus or os.environ.get("VERUS", "verus"))
    if binary is None:
        raise ValueError("Verus not found; set VERUS or --verus")
    binary = Path(binary).resolve()
    solver_name = str(z3 or os.environ.get("VERUS_Z3_PATH") or binary.with_name("z3"))
    solver = Path(shutil.which(solver_name) or solver_name).resolve()
    files = [
        p
        for p in binary.parent.iterdir()
        if p.is_file()
        and (
            p.name in {"verus", "rust_verify", "vstd.vir"} or p.suffix in {".rlib", ".dylib", ".so"}
        )
    ]
    files.append(solver)
    version = subprocess.check_output([str(binary), "--version", "--output-json"], text=True)
    compiler = Path(
        subprocess.check_output(
            ["rustup", "which", "--toolchain", json.loads(version)["verus"]["toolchain"], "rustc"],
            text=True,
        ).strip()
    ).resolve()
    files.extend([compiler, Path(sys.executable).resolve()])
    files.extend(
        p
        for p in (compiler.parent.parent / "lib").rglob("*")
        if p.is_file() and p.suffix in {".rlib", ".dylib", ".so"}
    )
    solver_version = subprocess.check_output([str(solver), "--version"], text=True)
    return {
        "verus": str(binary),
        "z3": str(solver),
        "verus_version": json.loads(version),
        "z3_version": solver_version.strip(),
        "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files))},
    }


def controller_path(trust):
    return trust.parent / (trust.name + ".controller.json")


def prepare(
    run_dir,
    trust,
    *,
    seed_from=None,
    iterations=10,
    cycles=None,
    wall_secs=0,
    load_count=1_000_000,
    run_count=2_000_000,
    seconds=30,
    repeats=3,
    seed=211,
    held_out_seed=223,
    verus=None,
    z3=None,
):
    run_dir, trust = Path(run_dir).resolve(), Path(trust).resolve()
    if run_dir.exists() or trust.exists() or controller_path(trust).exists():
        raise ValueError(
            "Use fresh run/trust paths; existing proof-only runs must be seeded into a new scored run"
        )
    if (
        not math.isfinite(seconds)
        or min(
            iterations,
            cycles if cycles is not None else iterations * 10,
            load_count,
            run_count,
            repeats,
            seconds,
        )
        <= 0
    ):
        raise ValueError("Budgets and workload sizes must be positive")
    if load_count < 2 or wall_secs < 0:
        raise ValueError("Zipf requires at least two keys and wall_secs must be nonnegative")
    if held_out_seed == seed:
        raise ValueError("The held-out draw needs a seed different from the scored seed")
    if trust.is_relative_to(run_dir) or run_dir.is_relative_to(trust):
        raise ValueError("Trust bundle must be outside the run")
    settings = tools_config(verus, z3)
    run = Run(run_dir).create()
    shutil.copy2(HERE / "task.md", run.task)
    shutil.copytree(
        HERE / "evaluator/interface", run.interface, ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        HERE / "evaluator/tests", run.tests, ignore=shutil.ignore_patterns("__pycache__")
    )
    (run.interface / "toolchain.json").write_text(json.dumps(settings, indent=2) + "\n")
    generator = KIT / "examples/single-machine-kvstore/evaluator"
    frozen_generator = run.interface / "trace-generator"
    frozen_generator.mkdir()
    shutil.copy2(generator / "generate.py", frozen_generator / "generate.py")
    shutil.copytree(
        generator / "generators",
        frozen_generator / "generators",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # Two frozen draws from the same generator: the scored trace every checkpoint is measured on,
    # and a held-out trace (another seed) measured only before a new best is recorded.
    traces = run.interface / "traces"
    draws = {}
    for draw, draw_seed in (("scored", seed), ("held-out", held_out_seed)):
        outdir = traces / draw
        command = [
            sys.executable,
            str(frozen_generator / "generate.py"),
            "zipf",
            "--theta",
            "0.99",
            "--load-count",
            str(load_count),
            "--run-count",
            str(run_count),
            "--seed",
            str(draw_seed),
            "--outdir",
            str(outdir),
        ]
        subprocess.run(command, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        metadata = next(outdir.glob("*.meta.json"))
        trace = json.loads(metadata.read_text())
        # Generator metadata uses absolute paths by default; frozen/exported artifacts must relocate.
        for key in ("load_file", "run_file"):
            trace[key] = Path(trace[key]).name
        metadata.write_text(json.dumps(trace, indent=2) + "\n")
        draws[draw] = {
            "seed": draw_seed,
            "operation_seed": draw_seed + 2,
            "load_file": f"{draw}/{trace['load_file']}",
            "run_file": f"{draw}/{trace['run_file']}",
        }
    cfg = json.loads((HERE / "evaluator/proof.json").read_text())
    cfg["config"].update(
        load_count=load_count,
        run_count=run_count,
        seconds=seconds,
        repeats=repeats,
        seed=seed,
        held_out_seed=held_out_seed,
    )
    cfg["workload"] = (
        f"{load_count} shuffled keys as fixed-width decimal strings; {run_count} scrambled Zipf(theta=0.99) trace, "
        f"seed {seed} (held-out draw: seed {held_out_seed}); 50:50 get/put with i32 values; "
        f"median of {repeats} fresh {seconds}s trials"
    )
    cfg["timeout"] = max(600, int(repeats * (seconds + 120)) + 60)
    # Use the setup interpreter for every trusted Python command, independent of PATH on resume.
    for field in ("toolchain", "build", "benchmark", "held_out_benchmark"):
        cfg[field][0] = str(Path(sys.executable).resolve())
    workload = dict(
        cfg["config"],
        draws=draws,
        trace_sha256={
            draws[draw][key]: hashlib.sha256((traces / draws[draw][key]).read_bytes()).hexdigest()
            for draw in draws
            for key in ("load_file", "run_file")
        },
    )
    (run.interface / "workload.json").write_text(json.dumps(workload, indent=2) + "\n")
    run.proof_config.write_text(json.dumps(cfg, indent=2) + "\n")
    # The workload card the planner and evaluator read, with the configuration this run froze.
    card = json.loads((HERE / "spec/workload.json").read_text())
    card["scored_configuration"] = dict(cfg["config"])
    run.workload_card.parent.mkdir(parents=True, exist_ok=True)
    run.workload_card.write_text(json.dumps(card, indent=2) + "\n")
    run.impl.mkdir(exist_ok=True)
    if seed_from:
        shutil.copytree(
            Path(seed_from).resolve(),
            run.impl,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    anchor = proof.freeze(run.path, trust)
    loop.initialize(
        run.path, iterations, cycles if cycles is not None else 10 * iterations, wall_secs
    )
    controller = {
        "run": str(run.path),
        "trust": str(trust),
        "contract": anchor,
        "iterations": iterations,
        "cycles": cycles if cycles is not None else 10 * iterations,
        "wall_secs": wall_secs,
    }
    controller_path(trust).write_text(json.dumps(controller, indent=2) + "\n")
    run.plan.write_text(
        "# Scored database run\nPreserve the Database contract. Resolve all proof obligations, then measure throughput and checkpoint. Improve the representation using measured feedback.\n"
    )
    return controller


def arguments(parser):
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--trust-root", type=Path, required=True)
    parser.add_argument("--seed-from", type=Path)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--cycles", type=int)
    parser.add_argument("--wall-secs", type=int, default=0)
    parser.add_argument("--load-count", type=int, default=1_000_000)
    parser.add_argument("--run-count", type=int, default=2_000_000)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=211)
    parser.add_argument("--held-out-seed", type=int, default=223)
    parser.add_argument("--verus")
    parser.add_argument("--z3")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    args = vars(parser.parse_args())
    args["run_dir"] = args.pop("run")
    args["trust"] = args.pop("trust_root")
    print(json.dumps(prepare(**args), indent=2))


if __name__ == "__main__":
    main()
