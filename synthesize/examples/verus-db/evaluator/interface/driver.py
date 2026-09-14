"""Frozen Verus DB build and measurement driver. Never skips verification."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_checker():
    path = HERE.parent.parent / "tests/proof.py"
    if not path.is_file():
        path = HERE.parent / "tests/proof.py"
    spec = importlib.util.spec_from_file_location("database_checker", path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker, path.with_name("database_test.rs")


def toolchain():
    settings = json.loads((HERE / "toolchain.json").read_text())
    for path, expected in settings["files"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Pinned toolchain file changed: {path}")
    return settings


def environment(settings):
    env = os.environ.copy()
    for name in ("VERUS_EXTRA_ARGS", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(name, None)
    env["VERUS"] = settings["verus"]
    env["VERUS_Z3_PATH"] = settings["z3"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def build(impl, destination):
    checker, exercise = load_checker()
    settings = toolchain()
    if hashlib.sha256((HERE / "mod.rs").read_bytes()).hexdigest() != checker.HASHES["mod.rs"]:
        raise ValueError("Original Database specification changed")
    with tempfile.TemporaryDirectory(prefix="verus-db-build-") as temporary:
        work = Path(temporary)
        (work / "spec").mkdir()
        shutil.copy2(HERE / "mod.rs", work / "spec/mod.rs")
        for path in Path(impl).rglob("*.rs"):
            text = path.read_text()
            checker.check_source(text)
            # Prevent alternate unchecked implementations and imports outside the candidate.
            if re.search(
                r"\b(cfg|cfg_attr|include|include_str|include_bytes|env|option_env|unsafe|macro_rules)\b|#\s*\[\s*path\s*=",
                text,
            ):
                raise ValueError(
                    f"Conditional code, source injection, unsafe code, or custom macros: {path.name}"
                )
            target = work / "impl" / path.relative_to(impl)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        original = exercise.read_text()
        main = "fn main() { skysynth_exercise(); }"
        if original.count(main) != 1:
            raise ValueError("Unexpected fixed exercise entry point")
        # Retain every original theorem and assertion in the measured crate.
        (work / "database.rs").write_text(
            original.replace(
                main, "mod benchmark;\nfn main() { skysynth_exercise(); benchmark::run(); }"
            )
        )
        shutil.copy2(HERE / "benchmark.rs", work / "benchmark.rs")
        command = [
            settings["verus"],
            "database.rs",
            "--crate-type",
            "bin",
            "--crate-name",
            "skysynth_database",
            "--no-cheating",
            "--output-json",
            "--expand-errors",
            "--rlimit",
            "10",
            "--num-threads",
            "1",
            "--compile",
            "-C",
            "opt-level=3",
            "-C",
            "debuginfo=0",
            "--remap-path-prefix",
            str(work) + "=/verus-db",
            "-o",
            str(Path(destination) / "program"),
        ]
        result = subprocess.run(
            command,
            cwd=work,
            env=environment(settings),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode or not checker.successful_summary(result.stdout):
            raise ValueError(
                "Full measured-crate verification failed:\n" + result.stdout + result.stderr
            )
        # Build directory must reproduce byte-for-byte; do not store timings or random paths there.
        print("Measured crate fully verified and compiled")


def benchmark(executable, draw="scored"):
    config = json.loads((HERE / "workload.json").read_text())
    for name, digest in config["trace_sha256"].items():
        if hashlib.sha256((HERE / "traces" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Frozen trace changed")
    if draw not in config["draws"]:
        raise ValueError(f"Unknown workload draw: {draw}")
    trace = config["draws"][draw]
    results = []
    for _ in range(config["repeats"]):
        command = [
            executable,
            str(HERE / "traces" / trace["load_file"]),
            str(HERE / "traces" / trace["run_file"]),
            str(config["seconds"]),
            str(trace["operation_seed"]),
        ]
        run = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=config["seconds"] + 120
        )
        result = json.loads(run.stdout)
        if not result["runtime_checks_passed"] or result["seconds"] < config["seconds"]:
            raise ValueError("Benchmark validation failed")
        results.append(result)
    print(
        json.dumps(
            {
                "metrics": {
                    "throughput_ops_per_sec": statistics.median(
                        r["ops_per_second"] for r in results
                    )
                },
                "draw": draw,
                "trials": results,
                "workload": config,
            }
        )
    )


def main():
    action = sys.argv[1]
    if action == "version":
        print(json.dumps(toolchain(), sort_keys=True))
    elif action == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]))
    elif action == "benchmark":
        benchmark(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "scored")
    else:
        raise ValueError("Expected version, build, or benchmark")


if __name__ == "__main__":
    main()
