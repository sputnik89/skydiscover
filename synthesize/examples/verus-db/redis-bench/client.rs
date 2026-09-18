//! Standalone Redis throughput client: one operator, THREADS concurrent clients, each with its own
//! connection and no client-side lock (Redis serializes commands itself). std only.
//!
//!   client LOAD RUN SECONDS OPERATION_SEED THREADS OPERATOR SCAN_WIDTH
//!   env SKY_REDIS_ADDRESS=127.0.0.1:6379 SKY_REDIS_NAMESPACE=skybench:<unique>
//!
//! Keys, values and trace walking follow the verus-db workload (zero-padded decimal keys, i32
//! SplitMix64 values, per-worker trace offsets), so the numbers are comparable to a candidate's.
//! Data model: a hash of values plus a zero-score sorted set as the lexicographic index.
use std::{env, fs, hint::black_box, io::{BufRead, BufReader, Read, Write}, net::TcpStream,
    sync::{Barrier, OnceLock}, thread, time::Instant};

// PUT keeps the value and the index in step atomically; SCAN reads a short range in one round trip.
const PUT: &str = "redis.call('HSET',KEYS[1],ARGV[1],ARGV[2]); redis.call('ZADD',KEYS[2],0,ARGV[1]); return 1";
const SCAN: &str = "local ks=redis.call('ZRANGE',KEYS[2],ARGV[1],ARGV[2],'BYLEX'); local out={}; for _,k in ipairs(ks) do out[#out+1]=k; out[#out+1]=redis.call('HGET',KEYS[1],k); end; return out";
// SORT is not a script: one over every key would block Redis past busy-reply-threshold. Plain
// ZRANGE then pipelined HMGET chunks; a sort trial has no concurrent writers.
const SORT_CHUNK: usize = 4096;
const LOAD_BATCH: usize = 1024;

enum Reply { Bytes(Vec<u8>), Integer(i64), Array(Vec<Reply>), Nil }
impl Reply {
    fn bytes(self) -> Vec<u8> {
        match self { Self::Bytes(b) => b, _ => panic!("expected bulk reply") }
    }
    fn integer(self) -> i64 {
        match self { Self::Integer(n) => n, _ => panic!("expected integer reply") }
    }
    fn array(self) -> Vec<Reply> {
        match self { Self::Array(a) => a, _ => panic!("expected array reply") }
    }
}

struct Connection { reader: BufReader<TcpStream>, writer: TcpStream, buffer: Vec<u8> }
impl Connection {
    fn new(address: &str) -> Self {
        let writer = TcpStream::connect(address).expect("Redis connect");
        writer.set_nodelay(true).unwrap();
        Self { reader: BufReader::with_capacity(1 << 16, writer.try_clone().unwrap()), writer,
               buffer: Vec::new() }
    }
    fn queue(&mut self, args: &[&[u8]]) {
        write!(self.buffer, "*{}\r\n", args.len()).unwrap();
        for arg in args {
            write!(self.buffer, "${}\r\n", arg.len()).unwrap();
            self.buffer.extend_from_slice(arg);
            self.buffer.extend_from_slice(b"\r\n");
        }
    }
    fn send(&mut self) {
        self.writer.write_all(&self.buffer).expect("Redis write");
        self.buffer.clear();
    }
    fn reply(&mut self) -> Reply {
        let mut line = String::new();
        self.reader.read_line(&mut line).expect("Redis reply header");
        assert!(line.ends_with("\r\n"), "truncated Redis reply");
        let body = &line[1..line.len() - 2];
        match line.as_bytes()[0] {
            b'+' => Reply::Bytes(body.as_bytes().to_vec()),
            b'-' => panic!("Redis error: {body}"),
            b':' => Reply::Integer(body.parse().unwrap()),
            b'$' => {
                let n: i64 = body.parse().unwrap();
                if n < 0 { return Reply::Nil; }
                let mut bytes = vec![0; n as usize + 2];
                self.reader.read_exact(&mut bytes).expect("Redis bulk body");
                assert_eq!(&bytes[n as usize..], b"\r\n");
                bytes.truncate(n as usize);
                Reply::Bytes(bytes)
            }
            b'*' => {
                let n: i64 = body.parse().unwrap();
                if n < 0 { return Reply::Nil; }
                Reply::Array((0..n).map(|_| self.reply()).collect())
            }
            _ => panic!("unsupported RESP2 reply"),
        }
    }
    fn call(&mut self, args: &[&[u8]]) -> Reply {
        self.queue(args);
        self.send();
        self.reply()
    }
}

fn keys(path: &str) -> Vec<u64> {
    let bytes = fs::read(path).expect("read trace");
    assert_eq!(bytes.len() % 8, 0);
    bytes.chunks_exact(8).map(|b| u64::from_le_bytes(b.try_into().unwrap())).collect()
}

fn mix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9e37_79b9_7f4a_7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

fn value_of(x: u64) -> i32 {
    mix(x) as i32
}

fn int(bytes: Vec<u8>) -> i32 {
    i32::from_le_bytes(bytes.try_into().expect("i32 value"))
}

struct Names { values: Vec<u8>, index: Vec<u8>, put: Vec<u8>, scan: Vec<u8> }

fn main() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(args.len(), 8, "client LOAD RUN SECONDS OPERATION_SEED THREADS OPERATOR SCAN_WIDTH");
    let seconds: f64 = args[3].parse().unwrap();
    let seed: u64 = args[4].parse().unwrap();
    let threads: usize = args[5].parse().unwrap();
    let operator = args[6].as_str();
    let scan_width: usize = args[7].parse().unwrap();
    assert!(seconds.is_finite() && seconds > 0.0 && threads > 0 && scan_width > 0);
    assert!(["get", "put", "scan", "sort"].contains(&operator));
    let address = env::var("SKY_REDIS_ADDRESS").expect("SKY_REDIS_ADDRESS");
    let namespace = env::var("SKY_REDIS_NAMESPACE").expect("SKY_REDIS_NAMESPACE");
    assert!(namespace.starts_with("skybench:"));

    let load = keys(&args[1]);
    let run = keys(&args[2]);
    let n = load.len();
    assert!(n > 0 && !run.is_empty() && run.iter().all(|&key| (key as usize) < n));
    let width = n.to_string().len();
    let names: Vec<String> = (0..=n as u64).map(|id| format!("{id:0width$}")).collect();

    // Preload, pipelined, outside timing.
    let mut control = Connection::new(&address);
    let db = Names {
        values: format!("{namespace}:values").into_bytes(),
        index: format!("{namespace}:index").into_bytes(),
        put: control.call(&[b"SCRIPT", b"LOAD", PUT.as_bytes()]).bytes(),
        scan: control.call(&[b"SCRIPT", b"LOAD", SCAN.as_bytes()]).bytes(),
    };
    assert_eq!(control.call(&[b"EXISTS", &db.values, &db.index]).integer(), 0,
               "refusing to use existing Redis keys");
    let load_start = Instant::now();
    for batch in load.chunks(LOAD_BATCH) {
        for &key in batch {
            let value = value_of(key).to_le_bytes();
            control.queue(&[b"EVALSHA", &db.put, b"2", &db.values, &db.index,
                            names[key as usize].as_bytes(), &value]);
        }
        control.send();
        for _ in batch { assert_eq!(control.reply().integer(), 1); }
    }
    let load_seconds = load_start.elapsed().as_secs_f64();

    let ready = Barrier::new(threads + 1);
    let go = Barrier::new(threads + 1);
    let start = OnceLock::<Instant>::new();
    let results = thread::scope(|scope| {
        let mut handles = Vec::new();
        for worker in 0..threads {
            let (db, ready, go, start, run, names, address) =
                (&db, &ready, &go, &start, &run, &names, &address);
            handles.push(scope.spawn(move || {
                let mut c = Connection::new(address);
                assert_eq!(c.call(&[b"PING"]).bytes(), b"PONG");
                let mut operations = 0u64;
                let mut checksum = 0i64;
                ready.wait();
                go.wait();
                let start = *start.get().unwrap();
                while start.elapsed().as_secs_f64() < seconds {
                    let index = (worker * (run.len() / threads) + operations as usize) % run.len();
                    let key = black_box(run[index]) as usize;
                    match operator {
                        "get" => {
                            let value = int(c.call(&[b"HGET", &db.values, names[key].as_bytes()]).bytes());
                            assert_eq!(value, value_of(key as u64));
                            checksum = checksum.wrapping_add(value as i64);
                        }
                        "put" => {
                            let value = value_of(seed ^ operations.wrapping_mul(threads as u64)
                                .wrapping_add(worker as u64) ^ key as u64).to_le_bytes();
                            assert_eq!(c.call(&[b"EVALSHA", &db.put, b"2", &db.values, &db.index,
                                               names[key].as_bytes(), &value]).integer(), 1);
                        }
                        "scan" => {
                            let hi = key.saturating_add(scan_width - 1).min(n - 1);
                            let lo_arg = format!("[{}", names[key]);
                            let hi_arg = format!("[{}", names[hi]);
                            let rows = c.call(&[b"EVALSHA", &db.scan, b"2", &db.values, &db.index,
                                                lo_arg.as_bytes(), hi_arg.as_bytes()]).array();
                            assert_eq!(rows.len(), 2 * (hi - key + 1), "missing/extra result");
                            let mut rows = rows.into_iter();
                            for id in key..=hi {
                                assert_eq!(rows.next().unwrap().bytes(), names[id].as_bytes());
                                let value = int(rows.next().unwrap().bytes());
                                assert_eq!(value, value_of(id as u64));
                                checksum = checksum.wrapping_add(value as i64);
                            }
                        }
                        _ => {
                            let ordered = c.call(&[b"ZRANGE", &db.index, b"-", b"+", b"BYLEX"]).array();
                            assert_eq!(ordered.len(), n, "missing/extra result");
                            let ordered: Vec<Vec<u8>> = ordered.into_iter().map(Reply::bytes).collect();
                            for chunk in ordered.chunks(SORT_CHUNK) {
                                let mut args: Vec<&[u8]> = vec![b"HMGET", &db.values];
                                args.extend(chunk.iter().map(Vec::as_slice));
                                c.queue(&args);
                            }
                            c.send();
                            let mut id = 0;
                            for chunk in ordered.chunks(SORT_CHUNK) {
                                let values = c.reply().array();
                                assert_eq!(values.len(), chunk.len());
                                for (name, value) in chunk.iter().zip(values) {
                                    assert_eq!(name.as_slice(), names[id].as_bytes(), "incorrect order");
                                    let value = int(value.bytes());
                                    assert_eq!(value, value_of(id as u64));
                                    checksum = checksum.wrapping_add(value as i64);
                                    id += 1;
                                }
                            }
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
        handles.into_iter().map(|h| h.join().expect("benchmark worker failed")).collect::<Vec<_>>()
    });
    let operations: u64 = results.iter().map(|r| r.0).sum();
    let elapsed = results.iter().map(|r| r.1).fold(0.0, f64::max);
    assert!(operations > 0);
    // Every key still present with a well-formed value, outside timing.
    for i in 0..n.min(1024) {
        let key = i * n / n.min(1024);
        let value = control.call(&[b"HGET", &db.values, names[key].as_bytes()]).bytes();
        if operator != "put" { assert_eq!(int(value), value_of(key as u64)); } else { int(value); }
    }
    assert_eq!(control.call(&[b"ZCARD", &db.index]).integer(), n as i64);
    println!("{{\"operator\":\"{operator}\",\"threads\":{threads},\"operations\":{operations},\"seconds\":{elapsed:.9},\"ops_per_second\":{:.6},\"load_seconds\":{load_seconds:.9},\"workers\":[{}]}}",
        operations as f64 / elapsed,
        results.iter().map(|(ops, seconds, checksum)| format!("{{\"operations\":{ops},\"seconds\":{seconds:.9},\"checksum\":{checksum}}}")).collect::<Vec<_>>().join(","));
}
