# Evaluating concurrent candidates in the verus-db loop

Notes on what the verus-db evaluator would need before an iteration can produce a
genuinely concurrent database and be measured and verified as one. Written against the
working tree of 2026-09-18 (four operators, `--threads`, shared `RwLock` harness).

The short version: the evaluator has exactly one mode, sequential. A concurrent
implementation in iteration n+1 has no interface it can be called through, no contract it
can be proven against, and no harness path that measures it without the global lock. Today
it would be forced through the sequential trait and the harness's `RwLock`, so its
concurrency would be invisible, or at best unproven.

## 1. The interface forbids concurrent calls

The pinned trait in `evaluator/interface/mod.rs` (its hash is checked by
`evaluator/tests/proof.py`, so candidates cannot change it):

```rust
pub trait Database {
    fn get(&self, key: &String) -> Option<&i32>;
    fn put(&mut self, key: String, value: i32);
    fn scan(&self, lo: &String, hi: &String) -> Vec<(String, i32)>;
    fn sort(&self) -> Vec<(String, i32)>;
}
```

The harness only ever calls these four methods, and it is frozen, so a candidate cannot
make it call anything else.

### 1a. `put(&mut self)` forces the harness to serialize every write

`&mut self` means the caller has exclusive access: no other thread holds any reference to
the store. The compiler enforces it, and `unsafe` is rejected by `driver.py`, so there is
no way around it.

The harness shares one store between `--threads` workers. The only way to obtain exclusive
access from shared ownership is a lock:

```rust
let shared = RwLock::new((store, expected));
// in each worker thread:
let mut guard = shared.write().unwrap();   // waits until no other thread holds the lock
<VerifiedDb as Database>::put(&mut guard.0, key, value);
```

A candidate that is concurrent inside gains nothing from it:

```rust
struct VerifiedDb { shards: [Mutex<BTreeMap<String, i32>>; 64] }

impl Database for VerifiedDb {
    fn put(&mut self, key: String, value: i32) {
        self.shards[shard_of(&key)].lock().insert(key, value);   // meant to run in parallel
    }
}
```

By the time `put` runs, the harness already holds the global write lock, so only one `put`
is in flight across all workers:

```
thread 1: [wait global write lock][put -> shard 7]
thread 2:                               [wait ......][put -> shard 40]
thread 3:                                                  [wait ....][put -> shard 3]
```

Shards 7, 40 and 3 could have been written simultaneously. The trait serialized them first.
This is the same effect the standalone Redis baseline showed: with the harness lock, Redis
reached ~10.7k put/s; without it, ~34k put/s, about the same as its `get` rate.

The fix is a method that needs only shared access:

```rust
fn put(&self, key: String, value: i32);   // many threads may call it at once
```

### 1b. `get` returning `Option<&i32>` pins the store

`Option<&i32>` is a reference into the store's memory, not a copy. It is sound today only
because the harness holds a read guard while using it, and no writer can obtain `&mut`
until every read guard is released.

Once `put` takes `&self`, a writer may run while a reader holds that reference. If the
write moves data (a `Vec` growing, a node splitting), the reference dangles. Rust
therefore makes the method unwritable for a concurrent store:

```rust
fn get(&self, key: &String) -> Option<&i32> {
    let shard = self.shards[shard_of(key)].lock();  // a writer may be changing it
    shard.get(key)                                  // ERROR: reference into `shard`,
}                                                   // but the lock is released on return
```

The only correct signature copies the value out:

```rust
fn get(&self, key: &String) -> Option<i32>;
```

`scan` and `sort` already return owned `Vec<(String, i32)>`, so they are unaffected.

### 1c. `&self` still allows modification

`&mut T` means *exclusive*, `&T` means *shared*. Neither means "read-only". Data behind a
shared reference can be modified when it sits inside a type built for simultaneous access
(a lock or an atomic). This is interior mutability, and the harness already depends on it:
`RwLock::write(&self)` takes a shared reference and hands back `&mut` to the value inside.

A concurrent store does the same at a finer grain:

```rust
impl ShardedDb {
    fn put(&self, key: String, value: i32) {              // shared access only
        let i = shard_of(&key, self.shards.len());
        self.shards[i].lock().unwrap().insert(key, value);  // Mutex::lock(&self) -> &mut map
    }
    fn get(&self, key: &String) -> Option<i32> {
        let i = shard_of(key, self.shards.len());
        self.shards[i].lock().unwrap().get(key).copied()
    }
}
```

| | `put(&mut self)` (today) | `put(&self)` (concurrent) |
|---|---|---|
| Who makes writes safe | the caller, via exclusive access (harness global lock) | the store, via its own locks or atomics |
| Writers to different shards | wait for each other | run in parallel |
| Can the store still be modified? | yes | yes |

In Verus the candidate would use `vstd`'s verified primitives rather than
`std::sync::Mutex`: `vstd::rwlock::RwLock`, `PAtomic`, or `PCell` with tracked permissions.

## 2. The spec has to change from before/after to atomic effect

### 2a. What the current spec says

```rust
pub trait Database: View<V = Map<Seq<char>, i32>> {
    fn put(&mut self, key: String, value: i32)
        ensures final(self)@ == old(self)@.insert(key@, value);
    fn get(&self, key: &String) -> (result: Option<&i32>)
        ensures match result {
            Some(v) => self@.dom().contains(key@) && self@[key@] == *v,
            None => !self@.dom().contains(key@),
        };
}
```

It rests on two things: `self@` is a spec function computing an abstract `Map` from the
store's fields, and `old(self)`/`final(self)` name the states before and after the call,
which is meaningful only because `&mut self` excludes everyone else during the call.

### 2b. What breaks with `&self`

- **No before/after.** With `&self`, Verus treats `self` as unchanged, so there is no
  `old(self)`/`final(self)` to relate. That is accurate: the reference did not change, the
  data behind the locks did.
- **`self@` can no longer read the contents.** The data lives in cells; a spec function
  cannot look inside one. Only the holder of the lock or permission can. The abstract map
  must become separately tracked *ghost state* that the implementation keeps in step with
  the real data.
- **The postcondition would be false anyway.** If thread A runs `put(k, 1)` while thread B
  runs `put(k, 2)`, then after A returns the map may hold `k -> 2`. Nothing about the state
  after a call can be promised to the caller.

### 2c. What replaces it

**Linearizability**: every operation behaves as if it took effect at a single instant
between its call and its return, and the outcomes agree with some sequential order of
those instants.

The sequential spec is reused rather than discarded. "Insert into the map" and "look up in
the map" still define each atomic step. What changes is where it is stated: not as pre/post
on `self`, but as the effect applied at the linearization point.

Sketch (the exact `vstd` API depends on the pinned Verus version):

```rust
// The store owns the authoritative ghost map M. The implementation proves that at exactly
// one moment inside each call it updates M, and that the returned result agrees with M at
// that same moment.
fn put(&self, key: String, value: i32)
    // atomically: M  ~>  M.insert(key@, value)
fn get(&self, key: &String) -> (r: Option<i32>)
    // atomically: M  ~>  M,  and  r == if M.contains(key@) { Some(M[key@]) } else { None }
```

The proof inside `put` then runs: take the shard lock, open the invariant linking the real
shard to its slice of the ghost map, update real data and ghost map together, re-establish
the invariant, release. The usual Verus tools are lock invariants (`vstd::rwlock`), atomic
invariants and ghost resources (`vstd::atomic_ghost`, authoritative/fragment tokens), and
`tokenized_state_machine!` (VerusSync) for harder structures.

### 2d. Choices the contract must make

| spec style | what it proves | fits this workload? |
|---|---|---|
| lock invariant only | memory safety; structure stays well-formed | too weak: says nothing about what `get` returns |
| per-key ownership tokens | caller owns `k -> v`; `put` updates it, `get` returns it | no: `put` trials have many threads writing the same hot keys, so no single owner exists |
| linearizability (atomic update) | every operation atomic against one abstract map | yes, this is the right target |

`scan`/`sort` need a separate decision, because atomic per-key operations do not determine
a range read:

- **Atomic snapshot**: the whole range is read at one instant. Strongest, and costlier
  (lock every shard in range, or keep versions).
- **Weakly consistent**: every returned row was that key's value at some point during the
  call; keys present for the whole call appear, keys absent for the whole call do not.

The benchmark cannot distinguish them, since trials never mix writes with range reads. The
contract must still choose, because the choice decides which designs are legal.

**Who meets the preconditions.** Ghost arguments are erased at compile time, so the
unverified harness simply calls `put`/`get`. Today a verified `exercise` in
`evaluator/tests/database_test.rs` shows the trait is usable as specified. A concurrent
contract needs an equivalent verified example that spawns threads and calls the store with
the real ghost setup, so the spec is proven usable rather than vacuous. The rest stays in
the trust boundary, as the lock adapter is today.

## 3. Everything else that is missing

### 3a. A second harness mode

`benchmark.rs` always wraps the store in `RwLock<(store, expected)>`. It needs a lock-free
path that calls the concurrent trait directly, and `driver.py` needs to know which trait a
candidate implements so it builds the matching entry point. Both modes must share trace
walking, thread count, timing and validation cost so the numbers stay comparable.

### 3b. Validation without the lock

Today `put` updates the `expected` shadow array under the same write guard, which works
only because writes are serialized. Without the lock:

- `get`/`scan`/`sort` trials have no writers, so results can be checked against the
  preload values (as the standalone Redis client does).
- For `put`, the final value of each key must be the last value *some* worker wrote to that
  key. That holds under linearizability. Each worker records its own last write per key,
  and the check runs after the workers stop.

### 3c. Checker rules that keep concurrency verified

`driver.py` already rejects `unsafe`, so a candidate cannot write `unsafe impl Sync`.
But `proof.py` does not require candidate code to sit inside `verus!{}`, and plain Rust
outside that block is compiled without being verified. A candidate could wrap
`std::sync::Mutex` or raw atomics outside `verus!` and still pass as "verified". This needs
ruling out, with concurrency permitted only through verified `vstd` primitives. (Inferred
from what the checker inspects; not tested directly.)

### 3d. Scoring across both modes

The leaderboard compares `get_ops_per_sec` across iterations, so a sequential iteration n
and a concurrent iteration n+1 compare fine numerically. Each row should record which
contract the candidate proved, so best-selection and `restore_best` know which guarantee
they are keeping. `proof.json`'s trust boundary currently states "No formal
concurrency/linearizability or persistence claim", and `task.md` tells agents that internal
concurrency is unspecified and that one instance is wrapped in a trusted lock; both must
describe the concurrent route. All of this changes the frozen evaluator, so it requires a
freshly prepared run.

## 4. Suggested order

1. **Mechanical** (1a, 1b, 3a, 3b): the concurrent trait, the lock-free harness path, and
   lock-free validation. Testable with plain-Rust stores, the way
   `tests/spec/test_verus_db_concurrency.py` tests the current harness.
2. **The real work** (2): pick the spec strength and the `scan`/`sort` semantics, then
   write the concurrent trait's contract. Land 3c with it.
3. **Bookkeeping** (3d): record the proven contract per leaderboard row; update `task.md`
   and the trust boundary.

Proving linearizability is far harder than proving the sequential spec. Candidates going
concurrent will spend many cycles on proofs, so the sequential trait should remain
available for the loop to keep improving on that path.

## Related

- `redis-bench/` measures Redis with independent clients and no client-side lock, which is
  what the concurrent harness path should look like for candidates.
- Known unrelated breakage in the working tree: `replay.py` still reads
  `metrics["throughput_ops_per_sec"]`, which the four-operator evaluator no longer reports.
