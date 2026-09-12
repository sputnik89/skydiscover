use vstd::prelude::*;
use vstd::std_specs::cmp::PartialOrdIs;

verus! {

pub trait Database<K: Ord, V>: View<V = Map<K, V>> {
    fn get(&self, key: &K) -> (result: Option<&V>)
        ensures
            match result {
                Some(value) => self@.dom().contains(*key) && self@[*key] == *value,
                None => !self@.dom().contains(*key),
            };

    fn put(&mut self, key: K, value: V)
        ensures final(self)@ == old(self)@.insert(key, value);

    fn scan(&self, lo: &K, hi: &K) -> (list: Vec<(K, V)>)
        requires lo.is_le(hi),
        ensures
            forall|i: int, j: int| #![trigger list@[i], list@[j]] 0 <= i < j < list@.len() ==> list@[i] != list@[j],
            forall|i: int| #![trigger list@[i]] 0 <= i < list@.len() ==> lo.is_le(&list@[i].0) && list@[i].0.is_le(hi),
            forall|k: K| #![trigger self@.dom().contains(k)] self@.dom().contains(k) && lo.is_le(&k) && k.is_le(hi) ==> list@.contains((k, self@[k])),
            forall|i: int| #![trigger list@[i]] 0 <= i < list@.len() ==> self@.dom().contains(list@[i].0) && self@[list@[i].0] == list@[i].1
        ;
    
    fn sort(&self) -> (list: Vec<(K, V)>)
        ensures
            forall|i: int| #![trigger list@[i]] 0 <= i < list@.len() ==> self@.dom().contains(list@[i].0) && self@[list@[i].0] == list@[i].1,
            forall|k: K| #![trigger self@.dom().contains(k)] self@.dom().contains(k) ==> list@.contains((k, self@[k])),
            forall|i: int, j: int| #![trigger list@[i], list@[j]] 0 <= i < j < list@.len() ==> list@[i].0.is_lt(&list@[j].0),
        ;
    
}

}
