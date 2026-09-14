#!/usr/bin/env python3
"""Measure the current String-keyed Verus candidate using the KV-store example's key generator.

Requires numpy and a Verus installation. Artifacts include the exact source snapshot,
verification/build logs, trace metadata, commands, hashes, and per-repeat results.
An incomplete proof requires explicit --allow-unverified and is never called a score.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--impl", type=Path, default=root / ".skydiscover/verus-db/synthesis/impl")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verus", default=os.environ.get("VERUS", "verus"))
    parser.add_argument("--z3", default=os.environ.get("VERUS_Z3_PATH"))
    parser.add_argument("--load-count", type=int, default=1_000_000)
    parser.add_argument("--run-count", type=int, default=2_000_000)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=211)
    parser.add_argument("--allow-unverified", action="store_true")
    args = parser.parse_args()
    if min(args.load_count, args.run_count, args.repeats) < 1 or args.seconds <= 0:
        parser.error("counts and seconds must be positive")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build = out / "snapshot"
    build.mkdir()  # Refuse to overwrite a previous measured snapshot.
    source = Path(__file__).with_name("benchmark.rs")
    example = source.parent.parent
    shutil.copytree(args.impl.resolve(), build / "impl")
    (build / "spec").mkdir()
    shutil.copyfile(example / "evaluator/mod.rs", build / "spec/mod.rs")
    shutil.copytree(example / "evaluator/tests", build / "tests")
    shutil.copyfile(source, build / "benchmark.rs")
    binary = shutil.which(args.verus)
    if binary is None:
        parser.error("Verus not found")
    verus = str(Path(binary).resolve())
    env = os.environ.copy()
    for name in ("VERUS_EXTRA_ARGS", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(name, None)
    env.update(
        VERUS=verus, SKYDISCOVER_IMPL=str(build / "impl"), SKYDISCOVER_INTERFACE=str(build / "spec")
    )
    env["VERUS_Z3_PATH"] = args.z3 or str(Path(verus).with_name("z3"))
    commands = []

    def logged(command, name, cwd=root, timeout=180):
        commands.append({"argv": command, "cwd": str(cwd)})
        result = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout
        )
        (out / name).write_text(result.stdout + result.stderr)
        return result

    proof = logged([sys.executable, str(build / "tests/proof.py")], "proof.log")
    verified = proof.returncode == 0
    print(f"Full proof passed: {verified}", flush=True)
    if not verified and not args.allow_unverified:
        raise SystemExit(
            f"Proof failed; see {out / 'proof.log'}. For a diagnostic only, use --allow-unverified with a fresh --out."
        )
    flags = [] if verified else ["--no-verify"]
    compile_command = [
        verus,
        "benchmark.rs",
        "--crate-type",
        "bin",
        "--crate-name",
        "verus_db_bench",
        "--no-cheating",
        "--output-json",
        "--rlimit",
        "10",
        "--num-threads",
        "1",
        *flags,
        "--compile",
        "-C",
        "opt-level=3",
        "-C",
        "debuginfo=0",
        "-o",
        "benchmark",
    ]
    compiled = logged(compile_command, "build.log", build)
    if compiled.returncode:
        raise SystemExit(f"Build failed; see {out / 'build.log'}")
    generator = root / "synthesize/examples/single-machine-kvstore/evaluator/generate.py"
    generated = logged(
        [
            sys.executable,
            str(generator),
            "zipf",
            "--theta",
            "0.99",
            "--load-count",
            str(args.load_count),
            "--run-count",
            str(args.run_count),
            "--seed",
            str(args.seed),
            "--outdir",
            str(out / "traces"),
        ],
        "generator.log",
    )
    generated.check_returncode()
    metadata = list((out / "traces").glob("*.meta.json"))
    if len(metadata) != 1:
        raise SystemExit("Expected exactly one trace metadata file; use a clean output directory")
    trace = json.loads(metadata[0].read_text())
    results = []
    for repeat in range(1, args.repeats + 1):
        print(
            f"Repeat {repeat}/{args.repeats}: {args.seconds:g} seconds, fresh database", flush=True
        )
        run = logged(
            [
                str(build / "benchmark"),
                trace["load_file"],
                trace["run_file"],
                str(args.seconds),
                str(args.seed + 2),
            ],
            f"repeat-{repeat}.log",
            build,
            max(180, args.seconds + 120),
        )
        run.check_returncode()
        result = json.loads(run.stdout)
        results.append(result)
        print(
            f"  {result['ops_per_second']:.2f} ops/s; reads={result['reads']}, writes={result['writes']}",
            flush=True,
        )
    hashes = {
        str(p.relative_to(build)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(build.rglob("*"))
        if p.is_file() and p.name != "benchmark"
    }
    trace_hashes = {
        Path(trace[key]).name: hashlib.sha256(Path(trace[key]).read_bytes()).hexdigest()
        for key in ("load_file", "run_file")
    }
    summary = {
        "kind": "ad_hoc_throughput_diagnostic",
        "full_proof_passed": verified,
        "verification_skipped_for_benchmark_build": not verified,
        "platform": platform.platform(),
        "verus_version": logged([verus, "--version"], "verus-version.log").stdout.strip(),
        "z3_version": logged([env["VERUS_Z3_PATH"], "--version"], "z3-version.log").stdout.strip(),
        "threads": 1,
        "key_encoding": "fixed-width zero-padded decimal String",
        "value_type": "i32",
        "target_read_fraction": 0.5,
        "operation_seed": args.seed + 2,
        "timed_read_validation": "every read checked against a shadow value array; writes update it",
        "excluded_from_timing": [
            "trace generation/loading",
            "operation choices",
            "database preload",
            "smoke checks",
            "final sampled checks",
        ],
        "trace": trace,
        "source_sha256": hashes,
        "trace_sha256": trace_hashes,
        "median_ops_per_second": statistics.median(r["ops_per_second"] for r in results),
        "results": results,
        "commands": commands,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"Median: {summary['median_ops_per_second']:.2f} ops/s. Results: {out / 'summary.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
