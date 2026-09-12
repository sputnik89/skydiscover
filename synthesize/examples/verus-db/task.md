---
domain: verified database
checked_by: proof
---

# Implement and verify an in-memory database

Build a usable Rust database and prove its operations satisfy the immutable Verus
`Database<K, V>` trait in `evaluator/mod.rs`. This spec comes from
`~/jitskit/specs/db/mod.rs`, with its implementation marker removed. The trait is
unchanged, and the example has no runtime dependency on jitskit.

## Contract

The database's `View` is a mathematical `Map<K, V>`:

- `get` returns precisely the current value, or `None` for an absent key.
- `put` inserts or overwrites one key and preserves all other mappings.
- `scan(lo, hi)` returns every and only entry in the inclusive range, without
  duplicate pairs. Its precondition is `lo <= hi`; scan order is unspecified.
- `sort` returns every entry exactly once, in strictly increasing key order.

For a concrete runnable target, implement `Database<u64, u64>` for a public
`VerifiedDb` type. A generic implementation is welcome if this instantiation works.
Provide `pub fn new() -> (db: Self)` on that type, with an empty-map postcondition.
This empty constructor is an additional task requirement, separate from the
original trait. `evaluator/tests/database_test.rs` checks it is callable without
preconditions and invokes the original trait via fully qualified calls.

This task covers a single-process, in-memory database. Persistence, transactions,
SQL, concurrency, and performance scores are not specified by this contract.

## What to read and write

Read `evaluator/mod.rs` and the tests in `evaluator/tests/`.
Copy the immutable `mod.rs` into `<run>/synthesis/evaluator/interface/` and
the contents of `evaluator/tests/` into `<run>/synthesis/tests/`.

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
