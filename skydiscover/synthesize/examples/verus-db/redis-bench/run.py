"""Standalone Redis throughput baseline for the verus-db workload.

Measures each operator (get, put, scan, sort) with THREADS concurrent clients, each on its own
connection, against a running Redis. It is independent of the synthesis loop: no run directory,
frozen contract, evaluator or leaderboard is read or written. Traces come from the same scrambled
Zipf generator and seed the loop uses, so the numbers line up with a candidate's.

    python3 synthesize/examples/verus-db/redis-bench/run.py --threads 4 --output outputs/redis-bench/4t

Each trial preloads a fresh namespace, runs one operator for --seconds, and deletes the namespace.
Writes <output>/result.json with the per-operator median ops/s and every raw trial.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
GENERATOR = HERE.parents[1] / "single-machine-kvstore/evaluator/generate.py"
OPERATORS = ("get", "put", "scan", "sort")


def redis_cli(args, *command):
    return subprocess.run(["redis-cli", "-h", args.host, "-p", str(args.port), *command],
                          check=True, capture_output=True, text=True).stdout


def server(args):
    info = dict(line.split(":", 1) for line in redis_cli(args, "INFO").splitlines()
                if ":" in line and not line.startswith("#"))
    raw = redis_cli(args, "CONFIG", "GET", "io-threads", "save", "appendonly", "maxmemory",
                    "maxmemory-policy", "busy-reply-threshold").splitlines()
    return {"redis_version": info["redis_version"], "run_id": info["run_id"],
            "connected_clients": int(info["connected_clients"]),
            "config": dict(zip(raw[::2], raw[1::2]))}


def traces(args, output):
    """One draw of the loop's workload: preload keys and the Zipf run trace."""
    outdir = output / "traces"
    subprocess.run([sys.executable, str(GENERATOR), "zipf", "--theta", str(args.theta),
                    "--load-count", str(args.load_count), "--run-count", str(args.run_count),
                    "--seed", str(args.seed), "--outdir", str(outdir)],
                   check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    meta = json.loads(next(outdir.glob("*.meta.json")).read_text())
    return str(outdir / Path(meta["load_file"]).name), str(outdir / Path(meta["run_file"]).name)


def build(output):
    binary = output / "redis-client"
    command = ["rustc", "--edition=2021", "-C", "opt-level=3", "-C", "debuginfo=0",
               str(HERE / "client.rs"), "-o", str(binary)]
    subprocess.run(command, check=True)
    shutil.copy2(HERE / "client.rs", output / "client.rs")
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True, help="new directory for results")
    parser.add_argument("--threads", type=int, default=4, help="concurrent clients (default 4)")
    parser.add_argument("--operators", default=",".join(OPERATORS),
                        help="comma-separated subset of get,put,scan,sort")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--load-count", type=int, default=1_000_000)
    parser.add_argument("--run-count", type=int, default=2_000_000)
    parser.add_argument("--seed", type=int, default=211,
                        help="trace seed; the loop's scored draw is 211, held-out 223")
    parser.add_argument("--theta", type=float, default=0.99)
    parser.add_argument("--scan-width", type=int, default=16)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()
    operators = args.operators.split(",")
    if not operators or any(op not in OPERATORS for op in operators):
        parser.error("--operators must name get, put, scan or sort")
    if args.threads < 1 or args.repeats < 1 or args.seconds <= 0:
        parser.error("--threads, --repeats and --seconds must be positive")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = server(args)
    if before["connected_clients"] != 1:
        print(f"warning: {before['connected_clients'] - 1} other client(s) connected to Redis",
              file=sys.stderr)
    load_file, run_file = traces(args, output)
    binary = build(output)

    trials = {op: [] for op in operators}
    for op in operators:
        for repeat in range(1, args.repeats + 1):
            namespace = f"skybench:{uuid.uuid4().hex}"
            env = {**os.environ, "SKY_REDIS_ADDRESS": f"{args.host}:{args.port}",
                   "SKY_REDIS_NAMESPACE": namespace}
            command = [str(binary), load_file, run_file, str(args.seconds), str(args.seed + 2),
                       str(args.threads), op, str(args.scan_width)]
            print(f"{op} trial {repeat}/{args.repeats}: preload, then {args.seconds:g}s "
                  f"with {args.threads} clients", flush=True)
            try:
                proc = subprocess.run(command, env=env, capture_output=True, text=True,
                                      timeout=args.seconds + 600)
                (output / f"{op}-{repeat}.stderr").write_text(proc.stderr)
                if proc.returncode:
                    sys.exit(f"{op} trial {repeat} failed; see {output / f'{op}-{repeat}.stderr'}")
                trial = json.loads(proc.stdout)
                (output / f"{op}-{repeat}.json").write_text(json.dumps(trial, indent=2) + "\n")
                trials[op].append(trial)
                print(f"  {trial['ops_per_second']:.1f} ops/s", flush=True)
            finally:
                # UNLINK frees the namespace in the background; never FLUSHDB.
                redis_cli(args, "UNLINK", f"{namespace}:values", f"{namespace}:index")

    after = server(args)
    if after["run_id"] != before["run_id"] or after["config"] != before["config"]:
        sys.exit("Redis restarted or changed configuration during the measurement")
    result = {
        "name": f"Redis {before['redis_version']}, {args.threads} independent clients",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "server": before,
        "host": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "workload": {"threads": args.threads, "seconds": args.seconds, "repeats": args.repeats,
                     "load_count": args.load_count, "run_count": args.run_count,
                     "seed": args.seed, "operation_seed": args.seed + 2, "theta": args.theta,
                     "scan_width": args.scan_width},
        "model": "hash of values + zero-score sorted-set index; PUT and SCAN are Lua scripts, "
                 "SORT is ZRANGE BYLEX + pipelined HMGET; one connection per client, no "
                 "client-side lock, no pipelining in timed calls",
        "metrics": {f"{op}_ops_per_sec": statistics.median(t["ops_per_second"] for t in rows)
                    for op, rows in trials.items()},
        "trials": trials,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    for name, value in result["metrics"].items():
        print(f"{name}: {value:.1f}")
    print(f"Saved: {output / 'result.json'}")


if __name__ == "__main__":
    main()
