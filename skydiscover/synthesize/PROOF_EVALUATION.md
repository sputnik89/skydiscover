# Proof candidates in the evaluation loop

Correctness and optimization are independent. Put `checked_by: proof` and
`evaluation: scored` in task front matter to optimize a formally verified candidate.
Omitting `evaluation` on a proof task preserves verification-only behavior; an explicit
`evaluation: proof-only` means the same thing. Invalid modes and incomplete scored
configuration fail instead of silently disabling evaluation.

The shared cycle is planner → candidate construction → full correctness → benchmark →
checkpoint → audit → critic. Proof construction contains multiple DSA code/proof cycles,
with ISA redesigns after three failed cycles on one obligation. The planner owns the outer
optimization brief; the lead records ISA's inner strategy separately in
`synthesis/proof-strategy.md`.

## Contract setup

Install the complete checker as `synthesis/tests/test.sh`, together with every supplied test,
and the immutable interface/target theorems as `synthesis/evaluator/interface/`. Keep trusted
checker dependencies under tests or evaluator. The checker must use `SKYDISCOVER_IMPL` and
`SKYDISCOVER_INTERFACE`, check the target theorems and allowed assumptions, reject escape
hatches, and check required non-vacuity examples. This is the trusted task-specific checker;
the framework cannot infer the intended theorem from arbitrary prover source.

For scored tasks, add `synthesis/evaluator/proof.json`:

```json
{
  "objective": "latency_ns",
  "direction": "min",
  "config": {"requests": 1000},
  "workload": "1000 declared requests",
  "timeout": 600,
  "toolchain": ["verus", "--version"],
  "build": ["python3", "{interface}/build.py", "{impl}", "{build}"],
  "executable": "program",
  "benchmark": ["{executable}", "1000"],
  "trust_boundary": "Verified function compiled by pinned Verus/Rust; benchmark driver is outside the proof"
}
```

Commands are argv arrays, never shell strings. Available placeholders are `{impl}`, `{interface}`,
`{build}`, and `{executable}`. The benchmark prints one JSON object containing `metrics` with a
finite numeric objective. Build output stays in the supplied build directory and must reproduce
identical bytes on re-verification. Pin paths, compiler flags, dependencies, and toolchain as needed
for reproducibility. If a command needs literal braces, escape them as `{{` and `}}`.

Optionally add `"held_out_benchmark"`: an argv array with the same placeholders that measures a
frozen held-out workload draw (another seed or slice the candidate workers never see) on the same
verified build. Run it with `spec.proof evaluate RUN --draw held-out` before recording
`--became-best`. Its row carries `draw: held-out` and never becomes a checkpoint score.

The lead initializes the trust bundle **before any candidate worker runs**:

```bash
python3 -m skydiscover.synthesize.spec.proof freeze RUN --trust-root TRUST_DIR
```

`TRUST_DIR` must be outside the run and outside candidate write access. Keep the printed
`SKYDISCOVER_PROOF_TRUST` and `SKYDISCOVER_PROOF_CONTRACT` values in the trusted controller's
environment for evaluator and delivery invocations. The expected digest is not read from an
editable manifest. Freeze refuses to overwrite a bundle. Resume retains the original values;
a user-authorized specification change requires a new run and contract identity.

The bundle includes task, evaluator, original tests, isolation declaration, and scored toolchain
version. Verification compares against the externally retained digest, rejects local changes,
and executes the frozen checker against clean source copies. New integration tests can be added
without replacing any original test; the original test.sh must support named additions.

## Enforced execution boundary

The default checker/build/benchmark execution uses macOS `sandbox-exec` or Linux `bwrap`.
Each process tree gets read access and writes only to its private scratch directory. Candidate
workers must also be launched as complete processes through:

```bash
python3 -m skydiscover.synthesize.spec.proof worker RUN -- WORKER_COMMAND
```

Workers can write only to `synthesis/impl/` and private temporary storage. They return logs and
strategies to the lead; they cannot write the budget ledger, outer plan, or trusted contract.
Worker network access supports model API calls; checker/build/benchmark network access is denied.
Configure worker caches/session storage under its temporary directory. Do not expose an external
tool server with broader host write access: sandboxing a client cannot restrict such a server.

Claude, Codex, Cursor, and pi's native role adapters alone do **not** provide this write boundary.
If these launch mechanisms cannot isolate the whole worker, run it in a separately provisioned
sandbox/container with equivalent mounts and a separately controlled evaluator. Declare that
setup at initialization with `--isolation external`; this explicitly records reliance on the
operator's isolation, rather than claiming local enforcement. No automatic unsafe fallback exists.
An unrestricted process running as the lead can change its environment and impersonate the lead;
integrity hashing is not protection against that authority. Keep trusted initialization and
evaluation outside worker control. Read-only chmod by the same file owner is insufficient.

## Budgets and execution

```bash
python3 -m skydiscover.synthesize.spec.loop init RUN --iterations 10 --cycles 100 --wall-secs 3600
python3 -m skydiscover.synthesize.spec.loop begin RUN
python3 -m skydiscover.synthesize.spec.loop cycle RUN
# Dispatch one bounded DSA cycle; repeat reservations as needed. ISA does not reset them.
python3 -m skydiscover.synthesize.spec.proof evaluate RUN
python3 -m skydiscover.synthesize.spec.checkpoint snapshot RUN --became-best
python3 -m skydiscover.synthesize.spec.loop status RUN
```

One reserved DSA cycle counts even if it fails or the worker crashes. One verified, benchmarked
checkpoint counts as a scored iteration even if it regresses. Failed candidate attempts use
`loop fail RUN --reason "remaining obligation"` and receive no fabricated score. Resume cannot
reset the limits. Checkpoint completion repairs the ledger after a crash between snapshot and
ledger update. Only the trusted lead writes these records; serialize evaluator/snapshot work.

The time limit stops new construction, not an in-flight bounded verification/benchmark. Every
worker has its own timeout; cycle limits alone cannot bound arbitrary retries inside a worker.
The default worker timeout is 600 seconds; commands inside proof evaluation use proof.json's
positive timeout. All owned child processes are terminated on timeout.

`proof evaluate` checks the full proof, builds the executable, and measures it. `checkpoint snapshot`
independently repeats verification/build and rejects a changed source, proof, toolchain, executable,
contract, or measurement configuration. Checkpoints retain proof evidence and the measured binary
under `.verification/build`, alongside source, benchmark inputs, and score. A separately written
fast executable is not an acceptable replacement for the verified build. Specify and audit the
compiler/extraction and runtime assumptions in `trust_boundary`.

After the budget, preserve unfinished progress and select the best eligible candidate:

```bash
python3 -m skydiscover.synthesize.spec.checkpoint restore-best RUN
# Audit the restored candidate, record report/stop reason and remaining obligations.
python3 -m skydiscover.synthesize.spec.checkpoint stamp-audit RUN
python3 -m skydiscover.synthesize.spec.run finish RUN --export-to .
```

Restoration moves unfinished code/proof/build into `synthesis/recovery/` before replacing it.
Re-audit after restoration. If no verified candidate exists, retain progress and report incomplete.
Proof-only runs use `proof verify RUN`, quality review, audit, and finish without a score; their
budget/proof progress remains available for replay. Full verification remains mandatory.

The delivery hook treats a structured DSA/ISA `SubagentStop` as an inner return, unless its title
explicitly declares delivery. Final delivery and `run finish` independently verify the result.
Adapters without structured role events should keep inner construction out of delivery tasks.

## Runnable validation example

`examples/proved-parity/` supplies a pinned Verus theorem, two verified implementations, a
deterministic build, and a real latency benchmark. Its replay exercises two outer iterations with
multiple inner cycle reservations and performance feedback, then revalidates and exports the best.
The replay uses supplied candidates to test integration; it is not evidence of autonomous DSA/ISA
search. See its README for the exact command and trust boundary.
