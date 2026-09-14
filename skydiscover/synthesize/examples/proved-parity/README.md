# Scored formal parity example

This example proves parity for every u64 input and benchmarks two implementations against
the same frozen specification: repeated subtraction, then constant-work modulo. Verus checks
the universal postcondition, loop invariant, and termination. The candidate supplies only a
restricted function body; it cannot change the signature or postcondition.

Install Verus with its matching Z3. Set `VERUS` and `VERUS_Z3_PATH` if they are not discoverable
on PATH. From the repository root, use a fresh output directory:

```bash
python3 skydiscover/synthesize/examples/proved-parity/replay.py --output /tmp/parity-replay
```

The default requires macOS sandbox-exec or Linux bwrap. If running inside a separately managed
equivalent isolation boundary, declare it with `--isolation external`; this is an explicit
operator responsibility, not an automatic fallback. Native coding-agent adapters alone do not
isolate workers. See `../../PROOF_EVALUATION.md` for the trust and execution requirements.

The replay supplies both candidates to validate integration. It exercises two outer scored
iterations, four inner cycle reservations, feedback, restoration, final verification, and export.
It does not simulate an actual agent conversation or claim autonomous synthesis. To synthesize
a candidate using agents, point `/skysynth` at task.md and follow the shared proof workflow.

The benchmark uses a fixed batch and Rust opt-level 0 to expose the algorithmic difference in
this small fixture. It is a real elapsed-time measurement, not a representative production
performance claim. The proof covers the pure function. Verus/Rust compilation, the operating
system, and the fixed benchmark driver are trusted; their scope is recorded with the score.
