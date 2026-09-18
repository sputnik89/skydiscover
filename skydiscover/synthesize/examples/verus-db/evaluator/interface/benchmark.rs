use crate::candidate::VerifiedDb;
use crate::db::Database;
use std::{env, fs, hint::black_box, sync::{Barrier, OnceLock, RwLock}, thread, time::Instant};

fn mix(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    x ^ (x >> 31)
}

fn keys(path: &str) -> Vec<u64> {
    let bytes = fs::read(path).expect("read trace");
    assert_eq!(bytes.len() % 8, 0);
    bytes.chunks_exact(8).map(|b| u64::from_le_bytes(b.try_into().unwrap())).collect()
}

// A trace id as a zero-padded decimal key: every key has the same length, so string order
// matches numeric order.
fn key_text(id: u64, width: usize) -> String {
    format!("{id:0width$}")
}

// Values are the low 32 bits of the SplitMix64 hash.
fn value_of(x: u64) -> i32 {
    mix(x) as i32
}

fn smoke() {
    let key = |text: &str| String::from(text);
    let mut store = VerifiedDb::new();
    assert_eq!(<VerifiedDb as Database>::get(&store, &key("9")), None);
    <VerifiedDb as Database>::put(&mut store, key("9"), 90);
    <VerifiedDb as Database>::put(&mut store, key("3"), 30);
    <VerifiedDb as Database>::put(&mut store, key("9"), 99);
    assert_eq!(<VerifiedDb as Database>::get(&store, &key("9")), Some(&99));
    let range = <VerifiedDb as Database>::scan(&store, &key("3"), &key("9"));
    assert_eq!(range, vec![(key("3"), 30), (key("9"), 99)]);
    assert!(<VerifiedDb as Database>::scan(&store, &key("4"), &key("8")).is_empty());
    assert_eq!(<VerifiedDb as Database>::sort(&store), vec![(key("3"), 30), (key("9"), 99)]);
}

// The adapter is trusted harness code, not a proof of a concurrent candidate API.
// One shared database; GET/SCAN/SORT take read guards, PUT takes a write guard.
pub fn run() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(args.len(), 8, "benchmark LOAD RUN SECONDS OPERATION_SEED THREADS OPERATOR SCAN_WIDTH");
    let seconds: f64 = args[3].parse().unwrap();
    let seed: u64 = args[4].parse().unwrap();
    let threads: usize = args[5].parse().unwrap();
    let operator = args[6].as_str();
    let scan_width: usize = args[7].parse().unwrap();
    assert!(seconds.is_finite() && seconds > 0.0 && threads > 0 && scan_width > 0);
    assert!(["get", "put", "scan", "sort"].contains(&operator));
    smoke();
    let load = keys(&args[1]);
    let run = keys(&args[2]);
    assert!(!load.is_empty() && !run.is_empty());
    let n = load.len();
    let mut seen = vec![false; n];
    for &key in &load {
        assert!((key as usize) < n && !seen[key as usize]);
        seen[key as usize] = true;
    }
    assert!(run.iter().all(|&key| (key as usize) < n));
    let width = n.to_string().len();
    let names: Vec<String> = (0..=n as u64).map(|id| key_text(id, width)).collect();
    let mut expected = vec![0i32; n];
    let mut store = VerifiedDb::new();
    let load_start = Instant::now();
    for &key in &load {
        let value = value_of(key);
        <VerifiedDb as Database>::put(&mut store, names[key as usize].clone(), value);
        expected[key as usize] = value;
    }
    let load_seconds = load_start.elapsed().as_secs_f64();
    let shared = RwLock::new((store, expected));
    let ready = Barrier::new(threads + 1);
    let go = Barrier::new(threads + 1);
    let start = OnceLock::<Instant>::new();
    let results = thread::scope(|scope| {
        let mut handles = Vec::new();
        for worker in 0..threads {
            let (shared, ready, go, start, run, names) =
                (&shared, &ready, &go, &start, &run, &names);
            handles.push(scope.spawn(move || {
                let mut operations = 0u64;
                let mut checksum = 0i64;
                ready.wait();
                go.wait();
                let start = *start.get().unwrap();
                // Check time per call: a full enumeration can be much slower than GET.
                while start.elapsed().as_secs_f64() < seconds {
                    let index = (worker * (run.len() / threads) + operations as usize) % run.len();
                    let key = black_box(run[index]) as usize;
                    if operator == "put" {
                        let value = black_box(value_of(seed ^ operations.wrapping_mul(threads as u64)
                            .wrapping_add(worker as u64) ^ key as u64));
                        let mut guard = shared.write().unwrap();
                        let (store, expected) = &mut *guard;
                        <VerifiedDb as Database>::put(store, names[key].clone(), value);
                        expected[key] = value;
                    } else {
                        let guard = shared.read().unwrap();
                        let (store, expected) = &*guard;
                        if operator == "get" {
                            let value = *black_box(<VerifiedDb as Database>::get(store, &names[key]))
                                .expect("preloaded key missing");
                            assert_eq!(value, expected[key]);
                            checksum = checksum.wrapping_add(value as i64);
                        } else {
                            let (lo, hi) = if operator == "scan" {
                                (key, key.saturating_add(scan_width - 1).min(n - 1))
                            } else { (0, n - 1) };
                            let list = if operator == "scan" {
                                <VerifiedDb as Database>::scan(store, &names[lo], &names[hi])
                            } else { <VerifiedDb as Database>::sort(store) };
                            assert_eq!(list.len(), hi - lo + 1, "missing/extra result");
                            for (offset, (name, value)) in list.iter().enumerate() {
                                let id = lo + offset;
                                assert_eq!(name, &names[id], "incorrect result order/key");
                                assert_eq!(*value, expected[id], "incorrect result value");
                                checksum = checksum.wrapping_add(*value as i64);
                            }
                            black_box(list);
                        }
                    }
                    operations += 1;
                }
                (operations, start.elapsed().as_secs_f64(), black_box(checksum))
            }));
        }
        ready.wait();
        start.set(Instant::now()).unwrap();
        go.wait();
        handles.into_iter().map(|h| h.join().expect("benchmark worker failed"))
            .collect::<Vec<_>>()
    });
    let operations: u64 = results.iter().map(|r| r.0).sum();
    let elapsed = results.iter().map(|r| r.1).fold(0.0, f64::max);
    assert!(operations > 0);
    let guard = shared.read().unwrap();
    let (store, expected) = &*guard;
    // A deterministic post-trial sample catches lost/incorrect writes, outside timing.
    for i in 0..n.min(1024) {
        let key = i * n / n.min(1024);
        assert_eq!(<VerifiedDb as Database>::get(store, &names[key]), Some(&expected[key]));
    }
    assert_eq!(<VerifiedDb as Database>::get(store, &names[n]), None);
    println!("{{\"operator\":\"{operator}\",\"threads\":{threads},\"operations\":{operations},\"seconds\":{elapsed:.9},\"ops_per_second\":{:.6},\"load_seconds\":{load_seconds:.9},\"runtime_checks_passed\":true,\"workers\":[{}]}}",
        operations as f64 / elapsed,
        results.iter().map(|(ops, seconds, checksum)| format!("{{\"operations\":{ops},\"seconds\":{seconds:.9},\"checksum\":{checksum}}}")).collect::<Vec<_>>().join(","));
}
