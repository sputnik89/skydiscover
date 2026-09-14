# Integrating proof-driven synthesis with the scored evaluation loop

Date: 2026-09-13
Status: implemented on 2026-09-13 following the user's implementation request.

## Objective

Use one outer optimization loop for test-driven and proof-driven candidates. In proof mode, candidate construction contains multiple DSA implementation/proof cycles, with ISA redesigns on stalls. A fully verified candidate then enters the existing benchmark, checkpoint, audit, and performance-feedback stages.

Keep proof-only tasks supported when they have no benchmark objective. Do not invent performance objectives for those tasks.

This plan records the agreed design and the implementation completed after the user authorized changes.

## Current repository behavior

These findings come from the local checkout, not the blog:

- `skydiscover/synthesize/workflow/SKILL.md` routes `checked_by: proof` to a separate formal procedure. Its formal section explicitly replaces measurement steps and says a proof has no benchmark.
- `skydiscover/synthesize/spec/paths.py`, `Run.is_proof_run()`, identifies proof mode from task front matter.
- `skydiscover/synthesize/spec/checkpoint.py`, `score_from_run()`, returns an empty score for proof runs before reading the leaderboard.
- `skydiscover/synthesize/spec/run.py` publishes proof candidates using `require_evaluation=False` through `require_evaluation=not run.is_proof_run()`.
- `skydiscover/synthesize/workflow/agents/2-synthesis-loop/critic.md` explicitly tells the critic that proof runs have neither a leaderboard nor a profile.
- `skydiscover/synthesize/workflow/scripts/run_tests.py` already supports a common correctness gate: it invokes the task's `test.sh`, which can run a proof checker.
- The distributed-store task requires its proof check to compare the supplied specification byte-for-byte, check the target theorem and assumptions, and build a non-vacuity example. Confirm the actual checker implementation before generalizing this protection.
- `main.py` locates the installable kit under `skydiscover/synthesize`.
- There are also files under top-level `synthesize/`. The relevant workflow, checkpoint, and run files matched the packaged copies when inspected. Determine the repository's source/synchronization convention before editing; keep shipped behavior and documentation consistent.

The current proof path shares correctness and delivery infrastructure, but does not enter the scored performance loop.

## Proposed control flow

```text
Initialize specification, correctness mode, objective, and budgets
    |
Planner selects an improvement direction
    |
Construct candidate
    |-- tests: coding agent implements the change
    `-- proof: DSA advances implementation and proof in bounded cycles
                  `-- ISA proposes a redesign on a proof stall
    |
Full correctness gate on the completed candidate
    |-- test suite
    `-- complete formal verification plus required integration tests
    |
If a benchmark objective exists:
    Evaluator benchmarks the verified candidate
        -> scored checkpoint
        -> audit decision / audit
        -> critic attributes performance and directs the next improvement
        -> repeat within budget
Otherwise:
    proof-only completion and quality review
    |
Final verification, current audit, report, and delivery
```

The exact ordering of audit, best selection, and publication must preserve the existing audit-stamp requirements. Scoring is not permission to publish an unaudited artifact.

## Separate correctness mode from optimization objective

- Keep `checked_by: proof` as the choice of correctness mechanism.
- Independently represent whether a run has a declared benchmark objective, command, workload, configuration, and comparison direction.
- Choose the smallest explicit configuration extension consistent with existing task/leaderboard conventions during implementation. No new field names were agreed in the discussion.
- A scored proof task requires complete verification and a matching score before delivery.
- A proof-only task requires complete verification and applicable quality/audit checks, but no score.
- An absent or broken benchmark for a declared scored task must be an error, not an automatic fallback to proof-only mode.
- Existing test-driven scoring behavior should remain intact.

## Agent roles and ownership

- The planner uses workload, environment, measurements, and critic feedback to choose an optimization direction. For proof candidates, it must not hand DSA a finished implementation, simulation relation, or crux lemma merely to mechanize.
- DSA derives code and proof together, advancing one bounded substep per cycle. Incremental type-checking demonstrates progress; it is not a substitute for complete verification.
- ISA responds to proof stalls using the failure log. Preserve the existing trigger of three failed cycles on the same obligation, unless later requirements change it.
- Define plan ownership explicitly: the planner owns the outer optimization brief; an ISA redesign is recorded as the inner construction strategy and handed back to the lead. Avoid conflicting planner/ISA rewrites of the same brief. Final storage layout is an implementation choice.
- The evaluator independently runs full correctness checks and then the benchmark against the completed candidate.
- The critic reads both performance evidence and proof history, and retains the existing quality-contract veto.
- The auditor reviews the trusted checking boundary, candidate behavior, and applicable integration assumptions. Agent-authored tests must not weaken the pinned formal contract.

## Iteration accounting and termination

Use distinct counters:

- **DSA cycle:** one incremental implementation/proof attempt, successful or failed.
- **Scored iteration:** one completed, fully verified candidate evaluated by the benchmark and checkpointed, including regressions and ties.
- **Failed candidate attempt:** candidate construction ends without full verification; record the failure without fabricating a score or a scored checkpoint.

Persist counters and limits so resume does not reset them. Link inner proof-log entries and failed attempts to their candidate attempt, and link successful attempts to scored checkpoints.

Use a scored-iteration limit plus an independent construction limit, such as total DSA cycles or elapsed time. ISA redesigns must not reset the total construction budget. A cycle can contain tactic retries, so a DSA-cycle cap alone is not a strict runtime cap; bound retries or enforce timeouts where a hard cap is required.

When a limit is reached, preserve the best eligible candidate and unfinished proof progress, report all counters and remaining obligations, and stop starting new work. Define whether in-flight work may finish within its own timeout. Never claim success for an incomplete proof or silently publish an unscored candidate from a scored run. Successful proof completion alone does not end a scored optimization run before its configured stop condition.

## Protect the given formal specification

Instructions alone are insufficient. Use both isolation to prevent edits and independent checks to detect changes.

1. Freeze the supplied specification, target theorem declarations, allowed assumptions, trusted proof-check command, and relevant configuration before candidate agents run.
2. Keep the trusted original or integrity manifest outside candidate agents' writable scope. A digest stored beside editable files is not an enforcement boundary.
3. Give DSA/ISA write access to their candidate implementation/proof area and controlled progress records. Expose the trusted formal inputs and checker read-only in the supported execution environment.
4. Verify integrity against the trusted original before accepting verification, scoring, or delivery. Fail closed on missing or mismatched inputs.
5. Check exact target theorems, permitted assumptions, prohibited escape hatches, and required non-vacuity examples. DSA/ISA may revise representations, invariants, and supporting lemmas, but cannot weaken the formal contract.
6. Define which component owns trusted-input initialization and any sanctioned changes. A spec change requires explicit user authorization and a new contract identity; invalidate affected verification and comparison records.

Read-only file permissions alone may be reversible by an agent running as the file owner. Inspect actual execution capabilities and use a sandbox, separate authority, or equivalent enforced boundary where available. Do not describe prompt instructions or ordinary file permissions as hard isolation. If an adapter cannot enforce isolation, document that limitation and keep verification anchored outside the candidate's control.

## Bind verification to the measured artifact

Extend or reuse checkpoint provenance to identify:

- the implementation and proof sources;
- the pinned formal contract and checker configuration;
- the complete verification result for those exact inputs;
- the build/extraction configuration and executable digest;
- the benchmark workload, configuration, measurement, and score.

For extracted or compiled systems, record the relationship between the proved implementation and the executed artifact, including relevant trusted toolchain assumptions. Do not substitute a separately written executable for the proved candidate. If an external runtime or wrapper is outside the proof, state that boundary and test its integration.

Changes to candidate sources, formal inputs, or relevant build/checker inputs invalidate verification. Changes to measured inputs invalidate the score. Preserve the existing input-digest matching and final revalidation behavior.

Partial DSA recovery snapshots belong to proof progress, not the scored checkpoint history. Restore a coherent code-and-proof state when reverting an unsuccessful optimization.

## Expected implementation areas

Paths below refer to the packaged copy; resolve synchronization with top-level `synthesize/` before editing.

| Area | Planned change |
| --- | --- |
| `workflow/SKILL.md` | Define the shared outer loop, proof construction stage, separate budgets, and proof-only completion. Remove blanket no-benchmark rules for proof mode. |
| `workflow/agents/2-synthesis-loop/{planner,dsa,isa,evaluator,critic,auditor}.md` | Align roles, completion criteria, ownership, protected inputs, and performance feedback. |
| `spec/paths.py` and task configuration | Separate correctness mode from whether scored evaluation is required. |
| `spec/checkpoint.py` | Remove the blanket proof scoring bypass; bind verification and executable provenance to the scored candidate. Preserve an explicit proof-only path. |
| `spec/run.py` | Require measurements based on the run's objective, preserve final correctness/audit checks, and report proof-only runs without misleading missing-score warnings. |
| Verification scripts and execution adapters | Establish trusted-input integrity checks and enforce agent write boundaries where supported. Inspect delivery hooks for premature completion at an inner DSA step. |
| Formal examples | Preserve proof-only examples; add an explicit scored formal example with a real executable, workload, benchmark, and build mapping. |
| Documentation and tests | Explain the two independent modes, nested counts, failure handling, and actual enforcement guarantees. |

Do not merely delete the early return in scoring: without workflow sequencing, a real benchmark, complete-verification gating, and trusted-input protection, that would not implement the intended behavior.

## Validation before completion

Use focused tests for the new behavior and existing relevant checks:

1. A scored proof candidate passes complete verification, receives a matching benchmark score, creates a checkpoint, and feeds the next optimization step.
2. A failed or partial proof cannot be accepted as an eligible scored candidate or delivered as complete.
3. Editing a supplied spec, theorem, allowed-assumption configuration, or trusted checker is prevented or independently detected and rejected. Include attempts to replace the integrity manifest.
4. Changing code, proof, executable, or relevant build inputs after verification/measurement invalidates the corresponding evidence.
5. A scored proof run with a missing or stale score cannot publish; a genuine proof-only run still completes without a benchmark.
6. DSA failures and ISA redesigns consume persistent construction budgets; resume preserves counters; regressions still count as scored iterations.
7. Exhaustion without a verified candidate reports incomplete work. Exhaustion with an eligible best candidate preserves and revalidates that candidate.
8. Existing test-driven scoring, best selection, audit stamps, and delivery checks still pass.
9. Run a small end-to-end scored formal example to demonstrate more than one verified candidate and performance feedback between them. Use the same pinned spec throughout.

## Implementation record

Implemented in both `synthesize/` and `skydiscover/synthesize/`:

- Independent `checked_by: proof` and `evaluation: scored|proof-only` configuration, with backward-compatible proof-only defaults.
- Shared score/checkpoint/export handling with complete verification, pinned contract checks, reproducible executable/toolchain evidence, and score-required delivery for scored tasks.
- `spec.proof freeze|worker|verify|evaluate` for protected inputs, bounded worker/checker/build/benchmark process trees, full verification, and measurement.
- `spec.loop init|begin|cycle|fail|status` for persistent budgets, candidate attempts, failure records, and crash recovery of scored counts.
- `spec.checkpoint restore-best` preserves unfinished code/proof/build before restoring the selected candidate for re-audit and delivery.
- Updated planner/DSA/ISA/evaluator/auditor/critic instructions, artifact ownership, adapters, delivery hook, and operator documentation.
- A runnable Verus parity example with two real verified/scored candidates under one immutable theorem; four inner cycle reservations, feedback, best restoration, and final export.
- Regression coverage for failed proofs, specification/checker/anchor changes, correctness-mode downgrade, source substitution, stale proof/executable evidence, budget exhaustion/resume, proof-only export, and inner-versus-final delivery.

Operational boundaries:

- The complete candidate worker must use the supplied sandbox launcher or equivalent external isolation. Native role adapters and remote tool servers do not automatically gain a filesystem boundary.
- Default local enforcement uses macOS sandbox-exec or Linux bwrap. Unsupported isolation fails closed. Explicit external mode records reliance on operator-provided isolation and a separately controlled evaluator; it is not a claim of local enforcement.
- The trusted lead retains the original contract digest and owns budget/progress records. Arbitrary unrestricted processes with the lead's permissions are outside the worker threat model.
- The task-specific trusted checker defines theorem/assumption/non-vacuity checks. The generic framework does not infer a theorem from arbitrary source.
- The replay supplies candidate fixtures to test integration; it does not claim an autonomous DSA/ISA search.

Validation:

- Final non-slow/non-integration regression suite: 798 passed, 1 skipped (nested sandbox unavailable), 1 deselected. Focused synthesis/hook suite: 318 passed, 1 skipped. The skipped native sandbox check separately passed outside the enclosing sandbox.
- Native sandbox test run outside the enclosing development sandbox: worker could edit its candidate but could not modify either trusted or local formal inputs.
- Final Verus replay at `/private/tmp/skysynth-parity-final`: both universal u64 parity proofs passed, both candidates were benchmarked and checkpointed, and the selected candidate passed final export verification. The fixture recorded 20,564,500 ns and 16,000 ns for the pinned batch; these are fixture measurements, not production performance claims.
- Black/isort checks passed for the repository's configured trees. Canonical package type checking (`mypy -p skydiscover --exclude 'synthesize/examples/'`) passed for 155 source files. Package-based discovery avoids this checkout's duplicate top-level/nested package names.
- `git diff --check` passed; the modified/new synthesis kit files match between both copies.

