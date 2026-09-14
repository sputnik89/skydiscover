---
domain: verified-arithmetic
checked_by: proof
evaluation: scored
---

Implement parity for every u64 input, proving that the result equals n % 2 == 0.
Write only body.rs in the candidate directory. The trusted wrapper fixes the signature,
postcondition, build, and benchmark. Its small allowed language excludes declarations,
attributes, macros, imports, and proof escape hatches. Use the repeated-subtraction seed
and optimize it using performance feedback without changing the contract.

Measure latency_ns for the pinned batch of 2,000 inputs. The objective is minimization.
Budget: two scored iterations and six DSA cycles total. This is a small integration
example, not a claim about realistic database workloads or optimized production builds.
