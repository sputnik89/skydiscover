---
domain: verified database
checked_by: proof
evaluation: scored
---

# Implement and verify an in-memory database

Build a usable Rust database and prove its operations satisfy the immutable Verus
`Database` trait in `evaluator/mod.rs`. This spec lives in the skydiscover repository at
`skydiscover/synthesize/examples/verus-db/evaluator/mod.rs`.

## Contract

Keys are `String` and values are `i32`. The database's `View` is a mathematical
`Map<Seq<char>, i32>`, each key viewed as its characters. Keys are ordered
lexicographically by character code (`key_lt` and `key_le` in the spec):

- `get` returns precisely the current value, or `None` for an absent key.
- `put` inserts or overwrites one key and preserves all other mappings.
- `scan(lo, hi)` returns every and only entry in the inclusive range, in strictly
  increasing key order. Its precondition is `key_le(lo, hi)`.
- `sort` returns every entry exactly once, in strictly increasing key order.

For a concrete runnable target, implement `Database` for a public `VerifiedDb` type.
Provide `pub fn new() -> (db: Self)` on that type, with an empty-map postcondition.
This empty constructor is an additional task requirement, separate from the
original trait. `evaluator/tests/database_test.rs` checks it is callable without
preconditions and invokes the original trait via fully qualified calls.

This task covers a single-process, in-memory database. Persistence, transactions,
SQL, and concurrency are not specified by this contract. Optimize verified throughput
under the frozen workload in evaluator/proof.json and interface/workload.json.

## What to read and write

Read `evaluator/mod.rs` and the tests in `evaluator/tests/`.
The trusted controller runs `prepare.py` before candidate work: it stages the immutable
interface and tests, generates and freezes the workload, pins the toolchain, and
initializes the shared scored-loop budget. Do not initialize or re-pin the contract
from a candidate worker.

Write `implementation.rs` in `<run>/synthesis/impl/` as a complete Rust/Verus module:

```rust
use vstd::prelude::*;
use crate::db::Database;

verus! {
    // Define VerifiedDb, View, new(), and the Database implementation here.
}
```

The database test imports the spec as `db` and your file as `candidate`.
The checker copies the spec, tests, and candidate Rust sources unchanged into a
clean build directory and verifies `database_test.rs`. The test supplies `main`.
Define the concrete type, its `View`, constructor, trait implementation, and helper
proofs together; leave the representation choice to the synthesis loop.

Write ordinary Rust/Verus source. Additional `.rs` modules beneath the candidate
directory are supported and retain their relative paths in the build. Verus
handles syntax and type checking. Like the Rocq example, the checker rejects known
proof shortcuts by name, including in comments, and runs with `--no-cheating`.
Do not introduce assumptions or skip verification of implementation bodies.

## Done test

Run `python3 <scripts>/run_tests.py --run <run>`, where `<scripts>` is the installed
SkySynth workflow's scripts directory. The suite receives `SKYDISCOVER_IMPL` and
`SKYDISCOVER_INTERFACE` and exits 0 only if:

1. The immutable spec matches its pinned hash.
2. All candidate Rust sources pass the proof-shortcut check.
3. Verus verifies the complete crate with `--no-cheating`, normal
   lifetime checks, and a successful nonempty JSON verification summary.
4. The same invocation compiles an executable, and the fixed example terminates.

The database test checks empty construction, missing lookup, insertion, overwrite,
inclusive scan, empty scan, and ordered enumeration. Its calls force a real
implementation of the original trait. These finite examples supplement the
universal trait proofs; they do not replace them.

Use the formal DSA/ISA loop and record attempts in `synthesis/proof-log.md`.
Tool failures, missing Verus/Z3, timeouts, and incomplete verification are failures,
never successful proofs. See `README.md` for toolchain setup and standalone replay.


## Scored evaluation

Use the shared proof-and-score loop. Reserve a DSA cycle before each construction
attempt, then run full `proof evaluate` and `checkpoint snapshot`. Each accepted
checkpoint counts once, including regressions. Three timing trials form one score.
Failed proofs receive no score and consume construction budget. ISA does not reset
budgets. Restore the best eligible candidate and independently reverify for delivery.

Default score: maximize median throughput_ops_per_sec across three 30-second runs,
each freshly preloaded with 1,000,000 shuffled keys. Use the single-machine-kvstore
scrambled Zipf generator (theta 0.99, seed 211, 2,000,000 run keys), independent
50:50 read/write choices, and one thread. Each key id becomes a zero-padded decimal
`String` (7 characters for 1,000,000 keys); values are `i32`. The frozen workload
configuration is authoritative for a particular run, including explicit small
validation configurations. Every read is checked against a shadow value array.
Only get/put are timed; all four operations and the constructor must verify.

A second trace, generated the same way from seed 223, is frozen as the held-out draw.
Measure it with `proof evaluate --draw held-out` before recording a new best; it never
becomes a checkpoint score. `specification/cards/workload.json` describes both draws.

Implementation workers may modify only synthesis/impl. The spec, tests, benchmark,
traces, toolchain identity, and budget ledger are controlled externally. Source
injection, conditional implementations, unsafe code, and custom macro definitions
are rejected in the scored build. Use ordinary candidate-local Rust modules.
