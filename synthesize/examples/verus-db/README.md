# Verus-verified database

Generate an in-memory Rust database with machine-checked lookup, insert/overwrite,
inclusive range scan, and sorted enumeration. Requires Python 3, Bash, and Verus
with its matching Rust toolchain and Z3. There is no benchmark in this proof task.

## Run it

With `fcc-server` already running and configured in another terminal:

```bash
bash synthesize/examples/verus-db/run_fcc.sh
```

The launcher discovers Verus and Z3 (including the local `~/verus/verus` and
`~/verdex/.venv/bin/z3` paths), installs the Claude skill, agent roles, and hooks
using SkySynth's installer, then opens `fcc-claude` with the task prompt. It uses
normal interactive permission prompts. The synthesis loop runs inside that
session; the launcher does not automatically restart it after exit.

```bash
# Check prerequisites and inspect the command without changing project settings:
bash synthesize/examples/verus-db/run_fcc.sh --dry-run

# Resume a particular run from its saved files:
bash synthesize/examples/verus-db/run_fcc.sh --resume-run .skydiscover/<slug>
```

Set `VERUS` and `VERUS_Z3_PATH` to override discovery. Additional Claude options
can be passed after `--`. FCC performs its own server-connectivity check at launch.

For manual setup:

Install Verus following the [official setup guide](https://verus-lang.github.io/verus/guide/getting_started.html).
The checker was exercised with Verus `0.2026.09.06.8dea4a2`. It uses the unchanged
spec's `final(self)` syntax; older releases may not support it.

Make `verus` available on PATH, or export its absolute executable path before
launching the coding agent. Set `VERUS_Z3_PATH` if your installation needs it:

```bash
export VERUS=/absolute/path/to/verus
export VERUS_Z3_PATH=/absolute/path/to/z3
```

Wire SkySynth using `uv run skydiscover init`, restart the coding agent, and invoke
from this directory's repository root:

```text
/skysynth build and prove the database specified in synthesize/examples/verus-db/task.md
```

The result is exported to `outputs/synthesize/<slug>_<timestamp>/best/`.
Replay a candidate (substitute its actual directory):

```bash
SKYDISCOVER_IMPL=/absolute/path/to/candidate \
  bash synthesize/examples/verus-db/evaluator/tests/test.sh
```

The candidate directory must contain `implementation.rs`. The standalone suite
defaults to the adjacent `evaluator/` for its interface; the SkySynth harness sets
`SKYDISCOVER_INTERFACE` to the run's copied interface. Both forms support
`bash test.sh proof.py` for a named test. Verification and execution have fixed
timeouts of 180 and 10 seconds respectively.

## What's here

| Path | Purpose |
|---|---|
| `task.md` | Formal task, candidate interface, and acceptance conditions |
| `run_fcc.sh` | Prerequisite checks, Claude setup, and FCC launch/resume |
| `evaluator/mod.rs` | Database spec from `~/jitskit/specs/db/mod.rs`, with the implementation marker removed |
| `evaluator/tests/database_test.rs` | Constructor checks and concrete database tests; the Verus entry point |
| `evaluator/tests/test.sh` | SkySynth suite entry point |
| `evaluator/tests/proof.py` | Integrity checks, clean build, verification, compilation, and execution |
| `checker_tests.py` | Regression tests for the checker (no reference implementation) |

During setup, copy `mod.rs` to the run's
`synthesis/evaluator/interface/` and the tests to `synthesis/tests/`.
The checker copies the spec, database test, and candidate `.rs` files unchanged
into a fresh temporary directory, preserving candidate submodule paths. The test
imports the spec and implementation as separate modules and is the Verus crate
entry point. No reference database
implementation is shipped.

The original trait is generic; this task requires the `u64` key/value instance and
an empty constructor so the result is runnable. The constructor is an additional
task requirement, not part of the original trait. Scan order remains unspecified.
Storage is volatile, and no transactional or performance guarantee is claimed.

The verifier and `vstd` are trusted dependencies. As in the Rocq example, a simple
source scan rejects named proof shortcuts, including in comments. Verus checks
ordinary Rust/Verus source with `--no-cheating`; there is no custom source grammar.
The checker is not an OS sandbox. See the
[Verus guidance on proof shortcuts](https://verus-lang.github.io/verus/guide/llmforverusproof.html).
Changing the trusted task requires intentionally updating the hashes in `proof.py`.

Run checker regressions with `python3 synthesize/examples/verus-db/checker_tests.py`.
