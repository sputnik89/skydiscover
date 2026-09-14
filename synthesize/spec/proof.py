"""Trusted formal verification and scored evaluation.

The lead freezes inputs once, retains the printed contract digest outside worker control,
and supplies SKYDISCOVER_PROOF_TRUST and SKYDISCOVER_PROOF_CONTRACT to the evaluator.
Workers must run through `worker` (or equivalent external isolation). The generic coding
agent adapters do not themselves isolate filesystem access. Never derive the expected
digest from a candidate-editable manifest at verification time.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .paths import Run, run_cli


def digest(path: Path) -> str:
    from .checkpoint import _tree_digest

    return _tree_digest(path)


def config(run: Run) -> dict:
    if not run.proof_config.is_file():
        if run.requires_evaluation():
            raise ValueError("Scored proof runs require evaluator/proof.json")
        return {"timeout": 600}
    value = json.loads(run.proof_config.read_text())
    if not isinstance(value, dict) or type(value.get("timeout", 600)) is not int:
        raise ValueError("proof.json must be an object with an integer timeout")
    if value.get("timeout", 600) <= 0:
        raise ValueError("Proof command timeout must be positive")
    if run.requires_evaluation():
        for key in ("build", "benchmark", "toolchain", "held_out_benchmark"):
            command = value.get(key)
            if key == "held_out_benchmark" and command is None:
                continue  # optional: a frozen held-out draw measured before best selection
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(arg, str) and arg for arg in command)
            ):
                raise ValueError(f"proof.json requires a nonempty argv list: {key}")
        for key in ("objective", "executable", "workload", "trust_boundary"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"proof.json requires {key}")
        executable = Path(value["executable"])
        if executable.is_absolute() or ".." in executable.parts:
            raise ValueError("executable must be relative to the build directory")
        if value.get("direction") not in ("min", "max") or not isinstance(
            value.get("config"), dict
        ):
            raise ValueError("proof.json requires direction min|max and a config object")
    return value


def freeze(run_dir: Path, trust: Path, *, isolation: str = "sandbox") -> str:
    """Lead-only initialization. Never overwrite an existing trusted bundle."""
    run = Run(run_dir)
    if not run.is_proof_run():
        raise ValueError("Only formal runs have a pinned proof contract")
    config(run)
    if isolation not in ("sandbox", "external"):
        raise ValueError("isolation must be sandbox or external")
    if not run.test_script.is_file() or not run.interface.is_dir():
        raise ValueError("Provide the formal interface and complete test.sh before freezing")
    trust = trust.resolve()
    if trust.is_relative_to(run.path.resolve()) or run.path.resolve().is_relative_to(trust):
        raise ValueError("The trusted bundle must be outside the run directory")
    if trust.exists():
        raise ValueError("Trusted bundle already exists; never re-pin a contract on resume")
    trust.parent.mkdir(parents=True, exist_ok=True)
    from .checkpoint import _copy_ignore

    with tempfile.TemporaryDirectory(dir=trust.parent, prefix=".freeze-") as tmp:
        stage = Path(tmp) / "contract"
        stage.mkdir()
        for source, name in (
            (run.task, "task.md"),
            (run.tests, "tests"),
            (run.evaluator, "evaluator"),
        ):
            digest(source)  # reject symlinks, including external checker dependencies
            if source.is_dir():
                shutil.copytree(source, stage / name, ignore=_copy_ignore)
            else:
                shutil.copy2(source, stage / name)
        cfg = config(run)
        toolchain = (
            execute(cfg["toolchain"], stage, dict(os.environ), cfg.get("timeout", 600))
            if run.requires_evaluation()
            else None
        )
        (stage / "boundary.json").write_text(
            json.dumps({"isolation": isolation, "toolchain": toolchain}, sort_keys=True)
        )
        contract = digest(stage)
        os.replace(stage, trust)
    return contract


def trusted(run: Run) -> tuple[Path, str]:
    raw = os.environ.get("SKYDISCOVER_PROOF_TRUST", "")
    expected = os.environ.get("SKYDISCOVER_PROOF_CONTRACT", "")
    if not raw or not expected:
        raise ValueError(
            "Missing external proof anchor: set SKYDISCOVER_PROOF_TRUST and SKYDISCOVER_PROOF_CONTRACT"
        )
    root = Path(raw).resolve()
    if not root.is_dir() or digest(root) != expected:
        raise ValueError("Trusted proof contract changed or is missing")
    if root.is_relative_to(run.path.resolve()):
        raise ValueError("The proof anchor cannot be stored inside the run")
    for source, name in ((run.task, "task.md"), (run.evaluator, "evaluator")):
        if digest(source) != digest(root / name):
            raise ValueError(f"Pinned formal input changed: {name}")
    # New integration tests are allowed; the original checker and every supplied test are immutable.
    for source in (root / "tests").rglob("*"):
        if source.is_file():
            local = run.tests / source.relative_to(root / "tests")
            if (
                not local.is_file()
                or local.is_symlink()
                or local.read_bytes() != source.read_bytes()
            ):
                raise ValueError(f"Pinned proof checker changed: {local}")
    return root, expected


def execute(argv: list[str], cwd: Path, env: dict, timeout: int) -> str:
    """Timeout the process group, including compiler/benchmark children."""
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError(f"Command timed out after {timeout}s: {argv[0]}") from None
        if process.returncode:
            raise ValueError(
                f"Command failed ({process.returncode}): {argv[0]}\n{stdout[-4000:]}{stderr[-4000:]}"
            )
        return stdout.strip()


def confined(command: list[str], writable: list[Path], *, network: bool = False) -> list[str]:
    """Constrain the whole process tree, not a prompt or reversible chmod."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = "(version 1)(allow default)(deny file-write*)"
        if not network:
            profile += "(deny network*)"
        profile += "".join(
            f"(allow file-write* (subpath {json.dumps(str(p.resolve()))}))" for p in writable
        )
        return ["sandbox-exec", "-p", profile, *command]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        argv = ["bwrap", "--die-with-parent", "--unshare-all", "--ro-bind", "/", "/"]
        if network:
            argv.append("--share-net")
        for path in writable:
            argv += ["--bind", str(path.resolve()), str(path.resolve())]
        return [*argv, "--proc", "/proc", "--dev", "/dev", "--", *command]
    raise ValueError(
        "No enforced sandbox available; configure external isolation under a separately controlled evaluator"
    )


def _argv(
    command: list[str], impl: Path, interface: Path, build: Path, executable: Path
) -> list[str]:
    values = {
        "impl": str(impl),
        "interface": str(interface),
        "build": str(build),
        "executable": str(executable),
    }
    return [arg.format_map(values) for arg in command]


def verify(run_dir: Path, *, impl: Path | None = None, build_candidate: bool = True) -> dict:
    """Run the frozen full checker on a clean copy; partial checks never create this evidence."""
    run = Run(run_dir)
    root, contract = trusted(run)
    cfg = config(run)
    source = impl if impl is not None else run.impl
    if not source.exists():
        raise ValueError("Proof candidate is missing")
    before = digest(source)
    timeout = cfg.get("timeout", 600)
    from .checkpoint import _copy_ignore, _replace_dir

    evidence = {"contract": contract, "implementation": before, "complete": True}
    boundary = json.loads((root / "boundary.json").read_text())
    with tempfile.TemporaryDirectory(prefix="skysynth-proof-") as tmp:
        scratch = Path(tmp)
        candidate = scratch / "impl"
        if source.is_dir():
            shutil.copytree(source, candidate, ignore=_copy_ignore)
        else:
            candidate.mkdir()
            shutil.copy2(source, candidate / source.name)
        shutil.copytree(root / "tests", scratch / "tests", ignore=_copy_ignore)
        shutil.copytree(root / "evaluator", scratch / "evaluator", ignore=_copy_ignore)
        interface = scratch / "evaluator/interface"
        build = scratch / "build"
        build.mkdir()
        temporary = scratch / "tmp"
        temporary.mkdir()
        env = dict(
            os.environ, SKYDISCOVER_IMPL=str(candidate), SKYDISCOVER_INTERFACE=str(interface)
        )
        # The trusted checker must use explicit paths, not mutable files in the original run.
        env.pop("SKYDISCOVER_RUN", None)
        env["TMPDIR"] = str(temporary)

        def checked(command: list[str], cwd: Path) -> str:
            argv = (
                confined(command, [build, temporary])
                if boundary["isolation"] == "sandbox"
                else command
            )
            return execute(argv, cwd, env, timeout)

        checked(["bash", "test.sh"], scratch / "tests")
        # Extra integration tests supplement, never replace, the original proof checker.
        extras = sorted(
            p.name
            for p in run.tests.iterdir()
            if p.is_file() and not (root / "tests" / p.name).exists()
        )
        if extras:
            shutil.copytree(run.tests, scratch / "tests", dirs_exist_ok=True, ignore=_copy_ignore)
            checked(["bash", "test.sh", *extras], scratch / "tests")
        if digest(candidate) != before:
            raise ValueError(
                "The proof check changed candidate sources; write build products to TMPDIR"
            )
        if run.requires_evaluation() and build_candidate:
            executable = build / cfg["executable"]
            toolchain = checked(cfg["toolchain"], scratch)
            if toolchain != boundary["toolchain"]:
                raise ValueError("Pinned proof/build toolchain changed")
            checked(_argv(cfg["build"], candidate, interface, build, executable), scratch)
            if digest(candidate) != before:
                raise ValueError("The build changed the verified candidate sources")
            if not executable.is_file() or executable.is_symlink():
                raise ValueError("The trusted build did not produce the declared executable")
            evidence.update(
                executable=digest(build),
                toolchain=toolchain,
                trust_boundary=cfg["trust_boundary"],
                isolation=boundary["isolation"],
            )
            destination = run.synthesis / "verified-build"
            # Stage on the destination filesystem so atomic replacement also works across mounts.
            with tempfile.TemporaryDirectory(dir=run.synthesis, prefix=".verified-") as staging:
                staged = Path(staging) / "build"
                shutil.copytree(build, staged)
                _replace_dir(staged, destination)
    trusted(run)
    if before != digest(source):
        raise ValueError("Candidate changed during full verification")
    return evidence


def evaluate(run_dir: Path, draw: str = "scored") -> dict:
    """Independent evaluator: verify, build, benchmark, and append an input-bound measurement.

    `draw="held-out"` runs proof.json's `held_out_benchmark` on the same verified build and records
    the row as `draw: held-out`, which scoring skips: it never becomes a checkpoint score."""
    from . import checkpoint, loop

    run = Run(run_dir)
    if not run.is_proof_run() or not run.requires_evaluation():
        raise ValueError("evaluate requires a scored proof task")
    if draw not in ("scored", "held-out"):
        raise ValueError("draw must be scored or held-out")
    attempt = loop.pending(run_dir)
    cfg = config(run)
    command_field = "benchmark" if draw == "scored" else "held_out_benchmark"
    if command_field not in cfg:
        raise ValueError("proof.json declares no held_out_benchmark")
    evidence = verify(run_dir)
    inputs = checkpoint.input_digests(run_dir)
    build = run.synthesis / "verified-build"
    executable = build / cfg["executable"]
    env = dict(
        os.environ,
        SKYDISCOVER_IMPL=str(run.impl.resolve()),
        SKYDISCOVER_INTERFACE=str(run.interface.resolve()),
    )
    root, _ = trusted(run)
    boundary = json.loads((root / "boundary.json").read_text())
    with tempfile.TemporaryDirectory(prefix="skysynth-benchmark-") as tmp:
        env["TMPDIR"] = tmp
        command = _argv(
            cfg[command_field],
            run.impl.resolve(),
            run.interface.resolve(),
            build.resolve(),
            executable.resolve(),
        )
        if boundary["isolation"] == "sandbox":
            command = confined(command, [Path(tmp)])
        output = execute(command, run.evaluator, env, cfg.get("timeout", 600))
    result = json.loads(output)
    metrics = result.get("metrics", {})
    value = metrics.get(cfg["objective"])
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Benchmark must return JSON with a finite objective in metrics")
    trusted(run)
    if inputs != checkpoint.input_digests(run_dir) or evidence["executable"] != digest(build):
        raise ValueError("Inputs or executable changed while benchmarking")
    entry = run.entry_impl()
    if entry is None:
        raise ValueError("No candidate entry")
    row = {
        "role": "candidate",
        "draw": draw,
        "impl": entry.relative_to(run.path).as_posix(),
        "metrics": metrics,
        "objective": cfg["objective"],
        "direction": cfg["direction"],
        "config": cfg["config"],
        "input_digests": inputs,
        "proof": evidence,
        "attempt": attempt,
    }
    run.bench.mkdir(parents=True, exist_ok=True)
    rows = checkpoint._read_json(run.leaderboard, [])
    if not isinstance(rows, list):
        raise ValueError("Leaderboard must be a list")
    rows.append(row)
    checkpoint._write_json(run.leaderboard, rows)
    suffix = "" if draw == "scored" else f".{draw}"
    (run.bench / f"attempt-{attempt}{suffix}.json").write_text(output + "\n")
    return row


def worker(run_dir: Path, command: list[str], timeout: int) -> str:
    """Launch a complete candidate worker with writes confined to impl and private scratch.

    No unsafe fallback: native adapters must launch the whole worker here, not just one
    shell tool call. Tool servers outside this process need their own equivalent sandbox.
    """
    run = Run(run_dir)
    trusted(run)
    if not command or timeout <= 0:
        raise ValueError("A worker command and positive timeout are required")
    run.impl.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skysynth-worker-") as tmp:
        scratch = Path(tmp).resolve()
        writable = run.impl.resolve()
        # Deny paths with symlinks before granting the writable subtree.
        digest(run.impl)
        env = dict(os.environ, TMPDIR=str(scratch), SKYDISCOVER_IMPL=str(writable))
        # Network is needed for model API calls; external tools must not expose writable host paths.
        argv = confined(command, [writable, scratch], network=True)
        return execute(argv, writable, env, timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("freeze", "verify", "evaluate", "worker"):
        parser = sub.add_parser(name)
        parser.add_argument("run", type=Path)
        if name == "freeze":
            parser.add_argument("--trust-root", type=Path, required=True)
            parser.add_argument("--isolation", choices=["sandbox", "external"], default="sandbox")
        if name == "evaluate":
            parser.add_argument("--draw", choices=["scored", "held-out"], default="scored")
        if name == "worker":
            parser.add_argument("--timeout", type=int, default=600)
            parser.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    if args.action == "freeze":
        print(
            json.dumps(
                {
                    "SKYDISCOVER_PROOF_TRUST": str(args.trust_root.resolve()),
                    "SKYDISCOVER_PROOF_CONTRACT": freeze(
                        args.run, args.trust_root, isolation=args.isolation
                    ),
                },
                indent=2,
            )
        )
    elif args.action == "verify":
        print(json.dumps(verify(args.run), indent=2))
    elif args.action == "evaluate":
        print(json.dumps(evaluate(args.run, args.draw), indent=2))
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        print(worker(args.run, command, args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "proof"))
