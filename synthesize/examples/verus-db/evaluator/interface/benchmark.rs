use crate::candidate::VerifiedDb;
use crate::db::Database;
use std::{env, fs, hint::black_box, time::Instant};

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
    let mut range = <VerifiedDb as Database>::scan(&store, &key("3"), &key("9"));
    range.sort_unstable();
    assert_eq!(range, vec![(key("3"), 30), (key("9"), 99)]);
    assert!(<VerifiedDb as Database>::scan(&store, &key("4"), &key("8")).is_empty());
    assert_eq!(<VerifiedDb as Database>::sort(&store), vec![(key("3"), 30), (key("9"), 99)]);
}

pub fn run() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(args.len(), 5, "benchmark LOAD RUN SECONDS OPERATION_SEED");
    let seconds: f64 = args[3].parse().unwrap();
    assert!(seconds.is_finite() && seconds > 0.0);
    let seed: u64 = args[4].parse().unwrap();
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
    drop(seen);
    // The width fits every loaded id and the absent id n, so all keys share one length.
    let width = n.to_string().len();
    // Key text for every id is prepared before timing; each timed put clones its owned key.
    let names: Vec<String> = (0..=n as u64).map(|id| key_text(id, width)).collect();
    // Independent, deterministic Bernoulli(0.5) operation choices, prepared before timing.
    let reads: Vec<bool> = (0..run.len()).map(|i| mix((i as u64).wrapping_add(seed)) & 1 == 0).collect();
    let mut expected = vec![0i32; n];
    let mut store = VerifiedDb::new();
    let load_start = Instant::now();
    for &key in &load {
        let value = value_of(key);
        <VerifiedDb as Database>::put(&mut store, names[key as usize].clone(), value);
        expected[key as usize] = value;
    }
    let load_seconds = load_start.elapsed().as_secs_f64();
    let mut operations = 0usize;
    let mut read_count = 0usize;
    let mut checksum = 0i64;
    let mut intervals = Vec::new();
    let mut last_time = 0.0;
    let mut last_ops = 0usize;
    let start = Instant::now();
    let elapsed;
    loop {
        for _ in 0..256 {
            let index = operations % run.len();
            let key = black_box(run[index]) as usize;
            if reads[index] {
                let value = *black_box(<VerifiedDb as Database>::get(&store, &names[key])).expect("preloaded key missing");
                assert_eq!(value, expected[key], "incorrect read at operation {operations}");
                checksum = checksum.wrapping_add(value as i64);
                read_count += 1;
            } else {
                let value = black_box(value_of((operations as u64) ^ key as u64));
                <VerifiedDb as Database>::put(&mut store, names[key].clone(), value);
                expected[key] = value;
            }
            operations += 1;
        }
        let now = start.elapsed().as_secs_f64();
        if now - last_time >= 5.0 || now >= seconds {
            intervals.push((now, operations - last_ops, now - last_time));
            last_time = now;
            last_ops = operations;
        }
        if now >= seconds {
            elapsed = now;
            break;
        }
    }
    black_box(checksum);
    // Check final values for a deterministic sample after the timed interval.
    for i in 0..1024 {
        let key = run[(i * 7919) % run.len()] as usize;
        assert_eq!(<VerifiedDb as Database>::get(&store, &names[key]), Some(&expected[key]));
    }
    assert_eq!(<VerifiedDb as Database>::get(&store, &names[n]), None);
    println!("{{\"operations\":{operations},\"reads\":{read_count},\"writes\":{},\"seconds\":{elapsed:.9},\"ops_per_second\":{:.6},\"mops_per_second\":{:.9},\"load_seconds\":{load_seconds:.9},\"key_width\":{width},\"checksum\":{checksum},\"runtime_checks_passed\":true,\"intervals\":[{}]}}",
        operations - read_count, operations as f64 / elapsed, operations as f64 / elapsed / 1e6,
        intervals.iter().map(|(end, ops, duration)| format!("{{\"end_seconds\":{end:.6},\"operations\":{ops},\"seconds\":{duration:.6},\"ops_per_second\":{:.6}}}", *ops as f64 / duration)).collect::<Vec<_>>().join(","));
}
