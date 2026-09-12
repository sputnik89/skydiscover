use vstd::prelude::*;
use db::Database;

#[path = "spec/mod.rs"]
mod db;
#[path = "impl/implementation.rs"]
mod candidate;

verus! {

fn skysynth_new() -> (db: candidate::VerifiedDb)
    ensures db@ == Map::<u64, u64>::empty(),
{
    candidate::VerifiedDb::new()
}

fn skysynth_exercise() {
    let mut db = skysynth_new();
    let missing = <candidate::VerifiedDb as Database<u64, u64>>::get(&db, &9);
    assert(missing.is_none());
    <candidate::VerifiedDb as Database<u64, u64>>::put(&mut db, 9, 90);
    <candidate::VerifiedDb as Database<u64, u64>>::put(&mut db, 3, 30);
    <candidate::VerifiedDb as Database<u64, u64>>::put(&mut db, 9, 99);
    let found = <candidate::VerifiedDb as Database<u64, u64>>::get(&db, &9);
    assert(found == Some(&99u64));
    let range = <candidate::VerifiedDb as Database<u64, u64>>::scan(&db, &3, &9);
    assert(range@.contains((3u64, 30u64)));
    assert(range@.contains((9u64, 99u64)));
    let empty = <candidate::VerifiedDb as Database<u64, u64>>::scan(&db, &4, &8);
    assert(empty@.len() == 0);
    let ordered = <candidate::VerifiedDb as Database<u64, u64>>::sort(&db);
    assert(ordered@.contains((3u64, 30u64)));
    assert(ordered@.contains((9u64, 99u64)));
}

}

fn main() { skysynth_exercise(); }
