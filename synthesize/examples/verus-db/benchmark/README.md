Run a throughput diagnostic on the generated `Database` implementation (`String`
keys, `i32` values) named `VerifiedDb`:

```sh
VERUS=/path/to/verus VERUS_Z3_PATH=/path/to/z3 \
python3 synthesize/examples/verus-db/benchmark/run.py \
  --out outputs/benchmarks/verus-db-new-run
```

Python needs NumPy for the existing single-machine-kvstore trace generator. The
default candidate is `.skydiscover/verus-db/synthesis/impl`; override with `--impl`.
Use a fresh output directory for each invocation. If the full proof fails, the
runner stops. To measure that candidate as an **unverified diagnostic**, explicitly
pass `--allow-unverified`; this compiles with `--no-verify` after recording the
failed full proof. Such a result is not an accepted SkySynth evaluation score.

The defaults are three 30-second trials, each with a fresh database containing
1,000,000 shuffled keys. The runner directly invokes single-machine-kvstore's
generator for 2,000,000 scrambled Zipf keys, theta 0.99, seed 211. A separate
deterministic operation sequence selects reads with probability 0.5. The trace
wraps if exhausted. Override `--load-count`, `--run-count`, `--seconds`, `--repeats`,
or `--seed` as needed.

This measures one thread, without persistence or a memory limit. Keys are the
trace ids as zero-padded decimal `String`s and values are `i32`, exactly as in the
scored benchmark. It matches the other example's key distribution and read/write
ratio; it does not reproduce its C++ harness or the task's 16-thread, 4 KB,
larger-than-memory configuration. Operations call the candidate's `get` and `put`
directly. The Rust executable uses optimization level 3. The harness also reports
the current log implementation's entry count; that field would need adapting for a
different representation. It measures the scored draw only, never the held-out one.

Every timed read is checked against a shadow array, and each write updates that
array; both costs are included. Setup, preloading, small API smoke checks, and
final sampled checks are excluded. No additional warmup is performed. Elapsed
time is checked every 256 operations and actual elapsed time is used in the
throughput calculation. Five-second intervals expose changes during a trial.
Compilation and trials run sequentially.

The output includes an unchanged implementation/specification snapshot, source
and trace hashes, verification and compiler logs, tool versions, commands,
individual results, and `summary.json` with the median throughput. Existing
candidate and specification files are never modified.
