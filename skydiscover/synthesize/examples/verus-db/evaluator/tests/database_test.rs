use vstd::prelude::*;
use db::Database;

#[path = "spec/mod.rs"]
mod db;
#[path = "impl/implementation.rs"]
mod candidate;

verus! {

fn skysynth_new() -> (db: candidate::VerifiedDb)
    ensures db@ == Map::<Seq<char>, i32>::empty(),
{
    candidate::VerifiedDb::new()
}

// Build one-character keys with push, so each key's view is exact without string-literal axioms.
fn skysynth_key(c: char) -> (key: String)
    ensures key@ == seq![c],
{
    let mut key = String::new();
    key.push(c);
    assert(key@ =~= seq![c]);
    key
}

fn skysynth_exercise() {
    let c = skysynth_key('c');
    let d = skysynth_key('d');
    let h = skysynth_key('h');
    let i = skysynth_key('i');
    let mut db = skysynth_new();
    let missing = <candidate::VerifiedDb as Database>::get(&db, &i);
    assert(missing.is_none());
    <candidate::VerifiedDb as Database>::put(&mut db, i.clone(), 90);
    <candidate::VerifiedDb as Database>::put(&mut db, c.clone(), 30);
    <candidate::VerifiedDb as Database>::put(&mut db, i.clone(), 99);
    let found = <candidate::VerifiedDb as Database>::get(&db, &i);
    assert(found == Some(&99i32));
    // Distinct keys: overwriting i must not disturb c.
    assert(c@ != i@) by { assert(c@[0] != i@[0]); }
    // Instantiate the map facts needed by the quantified completeness clauses.
    assert(db@.dom().contains(c@));
    assert(db@.dom().contains(i@));
    assert(db@[c@] == 30i32);
    assert(db@[i@] == 99i32);
    let range = <candidate::VerifiedDb as Database>::scan(&db, &c, &i);
    proof {
        let p = choose|p: int| #![trigger range@[p]] 0 <= p < range@.len() && range@[p].0@ == c@;
        let q = choose|q: int| #![trigger range@[q]] 0 <= q < range@.len() && range@[q].0@ == i@;
        assert(range@[p].1 == 30i32);
        assert(range@[q].1 == 99i32);
    }
    let empty = <candidate::VerifiedDb as Database>::scan(&db, &d, &h);
    proof {
        // Any returned first element would violate soundness or the range bounds.
        if empty@.len() > 0 {
            let key = empty@[0].0@;
            assert(db@.dom().contains(key));
            assert(key == c@ || key == i@);
            assert(db::key_le(d@, key) && db::key_le(key, h@));
            assert(false);
        }
    }
    assert(empty@.len() == 0);
    let ordered = <candidate::VerifiedDb as Database>::sort(&db);
    proof {
        let p = choose|p: int| #![trigger ordered@[p]] 0 <= p < ordered@.len() && ordered@[p].0@ == c@;
        let q = choose|q: int| #![trigger ordered@[q]] 0 <= q < ordered@.len() && ordered@[q].0@ == i@;
        assert(ordered@[p].1 == 30i32);
        assert(ordered@[q].1 == 99i32);
    }
}

}

fn main() { skysynth_exercise(); }
