# Verus database: verified throughput optimization

This example uses the shared scored proof loop: construct code/proofs, verify the
full original Database contract, benchmark the verified executable, checkpoint,
and use feedback for the next candidate. No shared loop changes are needed.

The immutable trait is in `evaluator/mod.rs` (SHA-256
`b22f651ce5002f6f5fb733ccde9cc0b433ae2f5555d33571f5ddc5f7652b0d92`).
Candidates implement `Database` (`String` keys, `i32` values, viewed as
`Map<Seq<char>, i32>`) as `VerifiedDb`, with a public empty constructor. The
contract covers get, put, inclusive scan, and sorted enumeration; keys are ordered
lexicographically by character code (`key_lt`), and scan results are strictly
ascending. The concrete exercise uses one-character keys, with explicit proof
steps for map membership, distinct keys, and the empty range.

## Prepare and run N iterations

Requires Python with NumPy, Verus with its matching Rust toolchain available through
rustup, and Z3. Native isolation requires macOS sandbox-exec or Linux bwrap.
VERUS and VERUS_Z3_PATH are honored; otherwise the setup resolves Verus from PATH
and looks for Z3 beside its resolved executable. Setup pins the actual executable,
Verus libraries, solver, Rust compiler/libraries, and Python interpreter hashes.

From the repository root:

```sh
export VERUS=/absolute/path/to/verus
export VERUS_Z3_PATH=/absolute/path/to/z3
python3 synthesize/examples/verus-db/prepare.py \
  --run .skydiscover/verus-db-throughput \
  --trust-root outputs/verus-db-throughput-controller/contract \
  --seed-from .skydiscover/verus-db/synthesis/impl \
  --iterations 10 --cycles 100
```

Use fresh run and trust paths. This preserves the old proof-only run and copies its
candidate as an unverified seed. To start without a seed, omit --seed-from. The
supplied `candidates/baseline.rs` is a fully verified append-only implementation for
integration validation; it keeps an insertion-order log (the last write to a key
wins), sorts by inserting each key's last write into a sorted vector, and scans by
filtering that sorted result. Its scan/sort algorithm is quadratic;
it is a starting point for optimization, not an efficient production database.

Or prepare and run the whole loop from one bash script, which owns the loop control
itself (budget checks between lead sessions, a per-session watchdog, finalization, and
`run finish --export-to`) and calls only the framework's commands and the trace generator:

```sh
bash skydiscover/synthesize/examples/verus-db/run_loop.sh 10 \
  --run .skydiscover/verus-db-throughput \
  --trust-root outputs/verus-db-throughput-contract \
  --verus /absolute/path/to/verus --z3 /absolute/path/to/z3
```

It prepares a fresh run on first launch (same staged files, pinned toolchain, two frozen
draws, and workload card as `prepare.py`) and resumes from `<trust-root>.controller.json`
afterwards; rerun it with the same N and budgets. `--dry-run` and `--prepare-only` stop
before any agent session. Needs `jq`, `rustup`, and a Python with NumPy and skydiscover.

Launch or resume the trusted lead using the external controller record:

```sh
bash synthesize/examples/verus-db/run_claude.sh \
  --run .skydiscover/verus-db-throughput \
  --trust-root outputs/verus-db-throughput-controller/contract \
  --iterations 10
```

`run_fcc.sh` accepts the same arguments and uses fcc-claude; its server must already
be configured and running. Both wrappers also support preparing a fresh run with
--seed-from, --prepare-only, --dry-run, --model, and --agent-timeout. They invoke the
existing `scripts/run_iterations.py`, without changing agent installation or
permissions. Use the same run, trust root, and budgets on resume. Older interactive
launcher flags are replaced by this explicit scored-run interface.

The controller record is adjacent to the trust bundle, outside the run. The trusted
lead retains its anchor, runs candidate processes through `proof worker`, and owns
budget/evaluation records. Workers can write only the candidate directory and their
private scratch space. The trusted lead must not expose a broader filesystem tool
server to workers. See [the framework trust boundary](../../PROOF_EVALUATION.md).

N counts verified, measured checkpoints, including regressions. Each checkpoint's
three timing trials produce one score. Failed proofs consume construction cycles
and receive no score. Defaults are 10*N DSA cycles and no construction wall-time
limit; --cycles and --wall-secs set explicit limits. Proof failures may prevent the
run from reaching N scores. Restoring the best candidate preserves unfinished work.

## Frozen score contract

- Report separate `get_ops_per_sec`, `put_ops_per_sec`, `scan_ops_per_sec`, and
  `sort_ops_per_sec`, each the median of three fresh 30-second homogeneous trials.
- `--optimize-for get|put|scan|sort` selects the winner metric (default `get`).
  There is no throughput aggregate across operator types. All metrics and raw
  trials are retained in benchmark output and leaderboard rows; the existing
  checkpoint summary shows the selected objective.
- `--threads N` sets client concurrency (default 8). All workers share ONE
  `VerifiedDb` through a trusted `std::sync::RwLock`; GET/SCAN/SORT can overlap,
  PUT is serialized. Candidates must be `Send + Sync`. The original proved trait
  is unchanged; the adapter is not a formal concurrency or linearizability proof.
- 1,000,000 shuffled loaded keys, 2,000,000 scrambled Zipf trace keys (theta 0.99,
  scored seed 211, held-out seed 223). Each worker traverses the trace with a
  deterministic starting offset and wraps independently.
- Keys are fixed-width decimal Strings; values are i32 SplitMix64-derived values.
- `--scan-width N` caps an inclusive scan at N consecutive loaded keys (default 16),
  clipped at the final key. SORT returns all keys. Count calls, not returned rows.
- Each trial uses a fresh database and synchronized worker start. Preload and
  post-trial checks are outside timing. Locking, clock checks, key cloning, result
  allocation/destruction, read/result validation, and write shadow updates are timed.
  Throughput is total completed calls divided by wall time through the last worker,
  including operations that finish after the requested duration.
- All GET results and every SCAN/SORT row are validated; PUT shadow updates share
  the write guard and final values are sampled after workers join. A worker panic
  fails the trial. The shared lock enforces the original API's borrowing rules.
- Native Rust optimization level 3; volatile storage, no memory cap. Full SORT
  trials can consume substantial memory because multiple results coexist.
- A fresh run/trust bundle is required for these new settings. Existing frozen
  runs are not migrated.

For example, add `--threads 8 --optimize-for put --scan-width 16` to either
`prepare.py` or `run_loop.sh`. Those settings cannot change on resume.

Setup copies the generator into the evaluator, generates the scored and held-out
traces before workers start, records checksums and relative metadata paths, writes
the run's workload card from `spec/workload.json`, and freezes everything with the
task and toolchain. Workload overrides (--load-count, --run-count, --seconds,
--repeats, --seed, --held-out-seed) are explicit new-contract settings, intended also
for small validation runs. Existing contracts cannot be reconfigured on resume.

The checker verifies the candidate against the original trait and concrete exercise.
The build driver then fully verifies the *measured crate*, including every original
exercise assertion, and compiles it with pinned flags. It never uses --no-verify or
partial verification. Conditional implementations, source injection, unsafe code,
and custom macros are rejected. The benchmark driver, runtime checks, clock, and
Verus/Rust/Z3/vstd compilation chain are trusted and outside the theorem.
Checkpointing independently re-verifies and rebuilds, requiring identical build
bytes. Accepted scores, per-trial details, proof identities and measured binaries
are retained by the shared loop. The older `benchmark/` command remains a separate,
explicitly unverified diagnostic when --allow-unverified is requested; it is not
called by scored evaluation.

## Validation and standalone proof checking

```sh
python3 synthesize/examples/verus-db/checker_tests.py
python3 -m pytest tests/spec/test_verus_db_integration.py
python3 synthesize/examples/verus-db/replay.py --output outputs/verus-db-replay-new
```

Replay uses two supplied fixture candidates with small traces and short timing,
exercising actual Verus verification, scoring, reproducible checkpoint builds,
budget exhaustion, recovery, best restoration and final export under native
sandboxing. It validates integration, not autonomous synthesis. --full-workload
uses the default measurement workload. Always use a fresh replay output path.
To exercise additional native negative tests, set VERUS_DB_NATIVE_TESTS=1 when
running the pytest file, outside any sandbox that disallows creating child sandboxes.

Standalone proof-only checker (no throughput score):

```sh
SKYDISCOVER_IMPL=/absolute/path/to/candidate \
  bash synthesize/examples/verus-db/evaluator/tests/test.sh
```

The checker supports candidate-local .rs modules and checks every module for proof
shortcuts. Verifier errors, missing tools, timeouts, empty/partial verification
summaries, and failed runtime checks are failures. The trait's pinned hash may not
be changed to accommodate a candidate.

## Redis baseline (standalone)

`redis-bench/` measures Redis on the same workload, independently of the loop: it reads
no run, contract, evaluator or leaderboard, and scores never depend on it.

```sh
python3 synthesize/examples/verus-db/redis-bench/run.py --threads 4 --output outputs/redis-bench/4t
```

It regenerates the traces with the same Zipf generator and seed, then runs each operator
with `--threads` clients, each on its own connection with no client-side lock. Redis
must already be listening on `--host`/`--port`. Results go to `<output>/result.json`.
