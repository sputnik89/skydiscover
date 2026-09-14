use vstd::prelude::*;
use crate::db::{Database, key_le, key_lt};

verus! {

// Candidate R1 for String keys: entries kept in insertion order, no structural invariant.
// The ghost view folds the sequence from the right so the last occurrence of each key wins.
// `put` appends, `get` scans from the right, `sort` walks the log from the right and inserts
// each key's last occurrence into a sorted vector, and `scan` filters the sorted result.
// Keys compare lexicographically by character code, exactly as `key_lt` in the spec.
pub struct VerifiedDb {
    pub entries: Vec<(String, i32)>,
}

pub open spec fn seq_to_map(s: Seq<(String, i32)>) -> Map<Seq<char>, i32>
    decreases s.len(),
{
    if s.len() == 0 {
        Map::<Seq<char>, i32>::empty()
    } else {
        seq_to_map(s.drop_last()).insert(s.last().0@, s.last().1)
    }
}

impl View for VerifiedDb {
    type V = Map<Seq<char>, i32>;

    open spec fn view(&self) -> Map<Seq<char>, i32> {
        seq_to_map(self.entries@)
    }
}

pub open spec fn has_key(s: Seq<(String, i32)>, k: Seq<char>) -> bool {
    exists|p: int| #![trigger s[p]] 0 <= p < s.len() && s[p].0@ == k
}

pub open spec fn strictly_sorted(s: Seq<(String, i32)>) -> bool {
    forall|p: int, q: int| #![trigger s[p], s[q]] 0 <= p < q < s.len() ==> key_lt(s[p].0@, s[q].0@)
}

// The key order is irreflexive.
pub proof fn lemma_lt_irreflexive(a: Seq<char>)
    ensures !key_lt(a, a),
    decreases a.len(),
{
    if a.len() > 0 {
        lemma_lt_irreflexive(a.drop_first());
    }
}

// The key order is transitive.
pub proof fn lemma_lt_transitive(a: Seq<char>, b: Seq<char>, c: Seq<char>)
    requires
        key_lt(a, b),
        key_lt(b, c),
    ensures key_lt(a, c),
    decreases a.len(),
{
    if a.len() > 0 && b.len() > 0 && c.len() > 0 && a[0] == b[0] && b[0] == c[0] {
        lemma_lt_transitive(a.drop_first(), b.drop_first(), c.drop_first());
    }
}

// Base lemma: the empty sequence maps to the empty map.
pub proof fn lemma_map_empty()
    ensures seq_to_map(Seq::<(String, i32)>::empty()) == Map::<Seq<char>, i32>::empty(),
{
    assert(seq_to_map(Seq::<(String, i32)>::empty()) == Map::<Seq<char>, i32>::empty());
}

// Lookup hit: position i holds k and no later position holds k.
pub proof fn lemma_hit(s: Seq<(String, i32)>, k: Seq<char>, i: int)
    requires
        0 <= i < s.len(),
        s[i].0@ == k,
        forall|j: int| i < j < s.len() ==> s[j].0@ != k,
    ensures
        seq_to_map(s).dom().contains(k),
        seq_to_map(s)[k] == s[i].1,
    decreases s.len() - i,
{
    if i == s.len() - 1 {
        assert(s.last() == s[i]);
        assert(seq_to_map(s) == seq_to_map(s.drop_last()).insert(s.last().0@, s.last().1));
    } else {
        assert(forall|j: int| i < j < s.drop_last().len() ==> s.drop_last()[j].0@ != k);
        assert(s.drop_last()[i] == s[i]);
        lemma_hit(s.drop_last(), k, i);
        assert(s.last().0@ != k);
        assert(seq_to_map(s) == seq_to_map(s.drop_last()).insert(s.last().0@, s.last().1));
    }
}

// Lookup miss: k appears nowhere, so the fold never inserts k.
pub proof fn lemma_miss(s: Seq<(String, i32)>, k: Seq<char>)
    requires forall|j: int| 0 <= j < s.len() ==> s[j].0@ != k,
    ensures !seq_to_map(s).dom().contains(k),
    decreases s.len(),
{
    if s.len() == 0 {
        assert(seq_to_map(s) == Map::<Seq<char>, i32>::empty());
    } else {
        assert(forall|j: int| 0 <= j < s.drop_last().len() ==> s.drop_last()[j].0@ != k);
        lemma_miss(s.drop_last(), k);
        assert(s.last().0@ != k);
        assert(seq_to_map(s) == seq_to_map(s.drop_last()).insert(s.last().0@, s.last().1));
    }
}

// Push commutes with map insertion by unfolding the fold once.
pub proof fn lemma_push(s: Seq<(String, i32)>, p: (String, i32))
    ensures seq_to_map(s.push(p)) == seq_to_map(s).insert(p.0@, p.1),
{
    assert(s.push(p).drop_last() == s);
    assert(s.push(p).last() == p);
    assert(seq_to_map(s.push(p)) == seq_to_map(s.push(p).drop_last()).insert(
        s.push(p).last().0@,
        s.push(p).last().1,
    ));
}

// Every mapped key has a last occurrence in the sequence.
pub proof fn lemma_last_occ(s: Seq<(String, i32)>, k: Seq<char>)
    requires seq_to_map(s).dom().contains(k),
    ensures
        exists|i: int| 0 <= i < s.len() && s[i].0@ == k && (forall|j: int| i < j < s.len() ==> s[j].0@ != k),
    decreases s.len(),
{
    if s.len() == 0 {
        assert(seq_to_map(s) == Map::<Seq<char>, i32>::empty());
        assert(false);
    } else if s.last().0@ == k {
        let w = s.len() - 1;
        assert(s[w] == s.last());
        assert(0 <= w < s.len() && s[w].0@ == k && (forall|j: int| w < j < s.len() ==> s[j].0@ != k));
    } else {
        assert(seq_to_map(s) == seq_to_map(s.drop_last()).insert(s.last().0@, s.last().1));
        assert(seq_to_map(s.drop_last()).dom().contains(k));
        lemma_last_occ(s.drop_last(), k);
        let ghost w: int = choose|i: int|
            0 <= i < s.drop_last().len() && s.drop_last()[i].0@ == k && (forall|j: int|
                i < j < s.drop_last().len() ==> s.drop_last()[j].0@ != k);
        assert(forall|j: int| 0 <= j < s.drop_last().len() ==> s[j] == s.drop_last()[j]);
        assert(s[w] == s.drop_last()[w]);
        assert(s[s.len() - 1] == s.last());
        assert(forall|j: int| w < j < s.len() ==> s[j].0@ != k);
        assert(exists|i: int| 0 <= i < s.len() && s[i].0@ == k && (forall|j: int| i < j < s.len() ==> s[j].0@ != k));
    }
}

// Three-way comparison agreeing with the spec order: negative, zero, or positive.
fn key_compare(a: &String, b: &String) -> (r: i8)
    ensures
        (r < 0) == key_lt(a@, b@),
        (r == 0) == (a@ == b@),
        (r > 0) == key_lt(b@, a@),
{
    let sa = a.as_str();
    let sb = b.as_str();
    let la = sa.unicode_len();
    let lb = sb.unicode_len();
    proof {
        assert(a@.skip(0) =~= a@);
        assert(b@.skip(0) =~= b@);
        assert(a@.subrange(0, 0) =~= b@.subrange(0, 0));
    }
    let mut i: usize = 0;
    while i < la && i < lb
        invariant
            sa@ == a@,
            sb@ == b@,
            la == a@.len(),
            lb == b@.len(),
            i <= la,
            i <= lb,
            a@.subrange(0, i as int) == b@.subrange(0, i as int),
            key_lt(a@, b@) == key_lt(a@.skip(i as int), b@.skip(i as int)),
            key_lt(b@, a@) == key_lt(b@.skip(i as int), a@.skip(i as int)),
        decreases la - i,
    {
        let ca = sa.get_char(i);
        let cb = sb.get_char(i);
        proof {
            assert(a@.skip(i as int)[0] == ca);
            assert(b@.skip(i as int)[0] == cb);
        }
        if ca != cb {
            if (ca as u32) < (cb as u32) {
                return -1;
            } else {
                return 1;
            }
        }
        proof {
            assert(a@.skip(i as int).drop_first() =~= a@.skip(i as int + 1));
            assert(b@.skip(i as int).drop_first() =~= b@.skip(i as int + 1));
            assert forall|t: int| #![trigger a@.subrange(0, i as int + 1)[t]]
                0 <= t < i + 1 implies a@.subrange(0, i as int + 1)[t] == b@.subrange(0, i as int + 1)[t] by {
                if t < i {
                    assert(a@.subrange(0, i as int)[t] == b@.subrange(0, i as int)[t]);
                }
            }
            assert(a@.subrange(0, i as int + 1) =~= b@.subrange(0, i as int + 1));
        }
        i += 1;
    }
    if la == lb {
        proof {
            assert(a@ =~= a@.subrange(0, i as int));
            assert(b@ =~= b@.subrange(0, i as int));
        }
        0
    } else if la < lb {
        proof {
            assert(a@.skip(i as int).len() == 0);
            assert(b@.skip(i as int).len() > 0);
        }
        -1
    } else {
        proof {
            assert(a@.skip(i as int).len() > 0);
            assert(b@.skip(i as int).len() == 0);
        }
        1
    }
}

// First position whose key is not below `key`, and whether that position holds `key`.
fn locate(list: &Vec<(String, i32)>, key: &String) -> (res: (usize, bool))
    ensures
        res.0 <= list@.len(),
        forall|q: int| #![trigger list@[q]] 0 <= q < res.0 ==> key_lt(list@[q].0@, key@),
        res.1 ==> res.0 < list@.len() && list@[res.0 as int].0@ == key@,
        !res.1 ==> (res.0 == list@.len() || key_lt(key@, list@[res.0 as int].0@)),
{
    let mut p: usize = 0;
    while p < list.len()
        invariant
            p <= list@.len(),
            forall|q: int| #![trigger list@[q]] 0 <= q < p ==> key_lt(list@[q].0@, key@),
        decreases list.len() - p,
    {
        let r = key_compare(&list[p].0, key);
        if r == 0 {
            return (p, true);
        }
        if r > 0 {
            return (p, false);
        }
        p += 1;
    }
    (p, false)
}

impl VerifiedDb {
    pub fn new() -> (db: Self)
        ensures db@ == Map::<Seq<char>, i32>::empty(),
    {
        let db = VerifiedDb { entries: Vec::new() };
        assert(db.entries@ == Seq::<(String, i32)>::empty());
        proof {
            lemma_map_empty();
        }
        db
    }
}

impl Database for VerifiedDb {
    fn get(&self, key: &String) -> (result: Option<&i32>) {
        let mut i = self.entries.len();
        while i > 0
            invariant
                i <= self.entries.len(),
                forall|j: int| i <= j < self.entries.len() ==> self.entries@[j].0@ != key@,
            decreases i,
        {
            i -= 1;
            if key_compare(&self.entries[i].0, key) == 0 {
                proof {
                    lemma_hit(self.entries@, key@, i as int);
                }
                return Some(&self.entries[i].1);
            }
        }
        proof {
            lemma_miss(self.entries@, key@);
        }
        None
    }

    fn put(&mut self, key: String, value: i32) {
        let ghost pair = (key, value);
        self.entries.push((key, value));
        proof {
            lemma_push(old(self).entries@, pair);
        }
        assert(self.entries@ == old(self).entries@.push(pair));
        assert(final(self)@ == old(self)@.insert(pair.0@, value));
    }

    fn sort(&self) -> (list: Vec<(String, i32)>) {
        let mut out: Vec<(String, i32)> = Vec::new();
        let mut i = self.entries.len();
        while i > 0
            invariant
                i <= self.entries.len(),
                strictly_sorted(out@),
                forall|p: int| #![trigger out@[p]] 0 <= p < out@.len() ==>
                    self@.dom().contains(out@[p].0@) && self@[out@[p].0@] == out@[p].1,
                forall|j: int| #![trigger self.entries@[j]] i <= j < self.entries@.len() ==>
                    has_key(out@, self.entries@[j].0@),
            decreases i,
        {
            i -= 1;
            let (p, present) = locate(&out, &self.entries[i].0);
            let ghost k = self.entries@[i as int].0@;
            if present {
                assert(out@[p as int].0@ == k);
                assert(has_key(out@, self.entries@[i as int].0@));
            } else {
                proof {
                    // k is absent from the sorted output ...
                    assert forall|q: int| #![trigger out@[q]] 0 <= q < out@.len() implies out@[q].0@ != k by {
                        lemma_lt_irreflexive(k);
                        if q > p {
                            lemma_lt_transitive(k, out@[p as int].0@, out@[q].0@);
                        }
                    }
                    // ... so no later log entry holds k, and position i is its last occurrence.
                    assert forall|j: int| i < j < self.entries@.len() implies self.entries@[j].0@ != k by {
                        if self.entries@[j].0@ == k {
                            assert(has_key(out@, self.entries@[j].0@));
                            let q = choose|q: int| #![trigger out@[q]] 0 <= q < out@.len() && out@[q].0@ == self.entries@[j].0@;
                            assert(out@[q].0@ != k);
                        }
                    }
                    lemma_hit(self.entries@, k, i as int);
                }
                let item = (self.entries[i].0.clone(), self.entries[i].1);
                let ghost before = out@;
                out.insert(p, item);
                proof {
                    before.insert_ensures(p as int, item);
                    assert(out@ == before.insert(p as int, item));
                    // Everything at or after the insertion point is above k.
                    assert forall|q: int| #![trigger before[q]] p <= q < before.len() implies key_lt(k, before[q].0@) by {
                        if q > p {
                            lemma_lt_transitive(k, before[p as int].0@, before[q].0@);
                        }
                    }
                    assert forall|a: int, b: int| #![trigger out@[a], out@[b]] 0 <= a < b < out@.len() implies key_lt(out@[a].0@, out@[b].0@) by {
                        if b < p {
                            assert(out@[a] == before[a] && out@[b] == before[b]);
                        } else if b == p {
                            assert(out@[a] == before[a]);
                        } else if a < p {
                            assert(out@[a] == before[a] && out@[b] == before[b - 1]);
                            lemma_lt_transitive(before[a].0@, k, before[b - 1].0@);
                        } else if a == p {
                            assert(out@[b] == before[b - 1]);
                        } else {
                            assert(out@[a] == before[a - 1] && out@[b] == before[b - 1]);
                        }
                    }
                    assert forall|q: int| #![trigger out@[q]] 0 <= q < out@.len() implies
                        self@.dom().contains(out@[q].0@) && self@[out@[q].0@] == out@[q].1 by {
                        if q < p {
                            assert(out@[q] == before[q]);
                        } else if q > p {
                            assert(out@[q] == before[q - 1]);
                        }
                    }
                    assert forall|j: int| #![trigger self.entries@[j]] i <= j < self.entries@.len() implies
                        has_key(out@, self.entries@[j].0@) by {
                        if j == i {
                            assert(out@[p as int].0@ == self.entries@[j].0@);
                        } else {
                            assert(has_key(before, self.entries@[j].0@));
                            let q = choose|q: int| #![trigger before[q]] 0 <= q < before.len() && before[q].0@ == self.entries@[j].0@;
                            if q < p {
                                assert(out@[q] == before[q]);
                            } else {
                                assert(out@[q + 1] == before[q]);
                            }
                        }
                    }
                }
            }
        }
        proof {
            assert forall|key: Seq<char>| #![trigger self@.dom().contains(key)] self@.dom().contains(key) implies
                exists|p: int| #![trigger out@[p]] 0 <= p < out@.len() && out@[p].0@ == key by {
                lemma_last_occ(self.entries@, key);
                let j = choose|j: int| 0 <= j < self.entries@.len() && self.entries@[j].0@ == key && (forall|t: int|
                    j < t < self.entries@.len() ==> self.entries@[t].0@ != key);
                assert(has_key(out@, self.entries@[j].0@));
            }
        }
        out
    }

    fn scan(&self, lo: &String, hi: &String) -> (list: Vec<(String, i32)>) {
        let all = self.sort();
        let mut out: Vec<(String, i32)> = Vec::new();
        let mut i: usize = 0;
        while i < all.len()
            invariant
                i <= all@.len(),
                forall|p: int, q: int| #![trigger all@[p], all@[q]] 0 <= p < q < all@.len() ==> key_lt(all@[p].0@, all@[q].0@),
                forall|p: int| #![trigger all@[p]] 0 <= p < all@.len() ==>
                    self@.dom().contains(all@[p].0@) && self@[all@[p].0@] == all@[p].1,
                strictly_sorted(out@),
                forall|p: int| #![trigger out@[p]] 0 <= p < out@.len() ==>
                    self@.dom().contains(out@[p].0@) && self@[out@[p].0@] == out@[p].1
                        && key_le(lo@, out@[p].0@) && key_le(out@[p].0@, hi@),
                forall|p: int, j: int| #![trigger out@[p], all@[j]] 0 <= p < out@.len() && i <= j < all@.len() ==>
                    key_lt(out@[p].0@, all@[j].0@),
                forall|j: int| #![trigger all@[j]] 0 <= j < i && key_le(lo@, all@[j].0@) && key_le(all@[j].0@, hi@) ==>
                    has_key(out@, all@[j].0@),
            decreases all.len() - i,
        {
            let low = key_compare(lo, &all[i].0);
            let high = key_compare(&all[i].0, hi);
            if low <= 0 && high <= 0 {
                let item = (all[i].0.clone(), all[i].1);
                let ghost before = out@;
                out.push(item);
                proof {
                    assert forall|p: int| #![trigger before[p]] 0 <= p < before.len() implies out@[p] == before[p] by {}
                    assert(out@[before.len() as int] == item);
                    assert forall|a: int, b: int| #![trigger out@[a], out@[b]] 0 <= a < b < out@.len() implies key_lt(out@[a].0@, out@[b].0@) by {
                        if b == before.len() {
                            assert(out@[a] == before[a]);
                            assert(key_lt(before[a].0@, all@[i as int].0@));
                        } else {
                            assert(out@[a] == before[a] && out@[b] == before[b]);
                        }
                    }
                    assert forall|p: int, j: int| #![trigger out@[p], all@[j]] 0 <= p < out@.len() && i + 1 <= j < all@.len() implies
                        key_lt(out@[p].0@, all@[j].0@) by {
                        if p == before.len() {
                            assert(key_lt(all@[i as int].0@, all@[j].0@));
                        } else {
                            assert(out@[p] == before[p]);
                            assert(key_lt(before[p].0@, all@[j].0@));
                        }
                    }
                    assert forall|j: int| #![trigger all@[j]] 0 <= j < i + 1 && key_le(lo@, all@[j].0@) && key_le(all@[j].0@, hi@) implies
                        has_key(out@, all@[j].0@) by {
                        if j == i {
                            assert(out@[before.len() as int].0@ == all@[j].0@);
                        } else {
                            assert(has_key(before, all@[j].0@));
                            let q = choose|q: int| #![trigger before[q]] 0 <= q < before.len() && before[q].0@ == all@[j].0@;
                            assert(out@[q] == before[q]);
                        }
                    }
                }
            }
            i += 1;
        }
        proof {
            assert forall|key: Seq<char>| #![trigger self@.dom().contains(key)]
                self@.dom().contains(key) && key_le(lo@, key) && key_le(key, hi@) implies
                exists|p: int| #![trigger out@[p]] 0 <= p < out@.len() && out@[p].0@ == key by {
                let j = choose|j: int| #![trigger all@[j]] 0 <= j < all@.len() && all@[j].0@ == key;
                assert(has_key(out@, all@[j].0@));
            }
        }
        out
    }
}

}
