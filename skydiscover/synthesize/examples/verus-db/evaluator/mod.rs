use vstd::prelude::*;

verus! {

pub open spec fn key_lt(a: Seq<char>, b: Seq<char>) -> bool
    decreases a.len(),
{
    if b.len() == 0 {
        false
    } else if a.len() == 0 {
        true
    } else if a[0] != b[0] {
        (a[0] as u32) < (b[0] as u32)
    } else {
        key_lt(a.drop_first(), b.drop_first())
    }
}

pub open spec fn key_le(a: Seq<char>, b: Seq<char>) -> bool {
    a == b || key_lt(a, b)
}

pub trait Database: View<V = Map<Seq<char>, i32>> {
    fn get(&self, key: &String) -> (result: Option<&i32>)
        ensures
            match result {
                Some(value) => self@.dom().contains(key@) && self@[key@] == *value,
                None => !self@.dom().contains(key@),
            };

    fn put(&mut self, key: String, value: i32)
        ensures final(self)@ == old(self)@.insert(key@, value);

    fn scan(&self, lo: &String, hi: &String) -> (list: Vec<(String, i32)>)
        requires key_le(lo@, hi@),
        ensures
            forall|i: int, j: int| #![trigger list@[i], list@[j]]
                0 <= i < j < list@.len() ==> key_lt(list@[i].0@, list@[j].0@),
            forall|i: int| #![trigger list@[i]]
                0 <= i < list@.len() ==> key_le(lo@, list@[i].0@) && key_le(list@[i].0@, hi@),
            forall|i: int| #![trigger list@[i]]
                0 <= i < list@.len() ==> self@.dom().contains(list@[i].0@) && self@[list@[i].0@] == list@[i].1,
            forall|k: Seq<char>| #![trigger self@.dom().contains(k)]
                self@.dom().contains(k) && key_le(lo@, k) && key_le(k, hi@)
                    ==> exists|i: int| #![trigger list@[i]] 0 <= i < list@.len() && list@[i].0@ == k,
        ;

    fn sort(&self) -> (list: Vec<(String, i32)>)
        ensures
            forall|i: int, j: int| #![trigger list@[i], list@[j]]
                0 <= i < j < list@.len() ==> key_lt(list@[i].0@, list@[j].0@),
            forall|i: int| #![trigger list@[i]]
                0 <= i < list@.len() ==> self@.dom().contains(list@[i].0@) && self@[list@[i].0@] == list@[i].1,
            forall|k: Seq<char>| #![trigger self@.dom().contains(k)]
                self@.dom().contains(k)
                    ==> exists|i: int| #![trigger list@[i]] 0 <= i < list@.len() && list@[i].0@ == k,
        ;
}
}