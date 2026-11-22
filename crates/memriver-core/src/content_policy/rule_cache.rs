//! Which compiled rules are kept between checks.
//!
//! A compiled rule costs memory, and most rules are never needed: a rule is compiled
//! only when a text passes its keyword prefilter. The few rules without keywords run on
//! every text and stay compiled once used. The rest go through a bounded cache, or
//! through none at all.

/// What happens to a compiled rule that has keywords, after it was used.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CachePolicy {
    /// Dropped at once: compile, use, drop.
    None,
    /// At most this many are kept; the one used longest ago makes room.
    Lru(usize),
    /// At most this many are kept; the one used least often makes room, of several the
    /// one used longest ago.
    Lfu(usize),
}

struct Entry<T> {
    rule: usize,
    value: T,
    uses: u64,
    last_used: u64,
}

pub struct Cache<T> {
    policy: CachePolicy,
    /// Rules that run on every text: never dropped.
    kept: Vec<(usize, T)>,
    bounded: Vec<Entry<T>>,
    clock: u64,
}

impl<T> Cache<T> {
    pub fn new(policy: CachePolicy) -> Cache<T> {
        Cache {
            policy,
            kept: Vec::new(),
            bounded: Vec::new(),
            clock: 0,
        }
    }

    /// How many compiled rules are held right now.
    pub fn len(&self) -> usize {
        self.kept.len() + self.bounded.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn holds(&self, rule: usize) -> bool {
        self.kept.iter().any(|(held, _)| *held == rule)
            || self.bounded.iter().any(|entry| entry.rule == rule)
    }

    /// Runs `run` on rule number `rule`, compiled by `compile` unless it is held.
    /// `always` says the rule runs on every text, so it is kept for good.
    pub fn with<R>(
        &mut self,
        rule: usize,
        always: bool,
        compile: impl FnOnce() -> T,
        run: impl FnOnce(&T) -> R,
    ) -> R {
        self.clock += 1;
        if always {
            let at = match self.kept.iter().position(|(held, _)| *held == rule) {
                Some(at) => at,
                None => {
                    self.kept.push((rule, compile()));
                    self.kept.len() - 1
                }
            };
            return run(&self.kept[at].1);
        }
        let size = match self.policy {
            CachePolicy::Lru(size) | CachePolicy::Lfu(size) => size,
            CachePolicy::None => 0,
        };
        if let Some(entry) = self.bounded.iter_mut().find(|entry| entry.rule == rule) {
            entry.uses += 1;
            entry.last_used = self.clock;
            return run(&entry.value);
        }
        if size == 0 {
            return run(&compile());
        }
        if self.bounded.len() >= size {
            // dropped before the next one is compiled, so the two never stand together
            let lfu = matches!(self.policy, CachePolicy::Lfu(_));
            let rank = |entry: &Entry<T>| (if lfu { entry.uses } else { 0 }, entry.last_used);
            let out = (0..self.bounded.len())
                .min_by_key(|&at| rank(&self.bounded[at]))
                .expect("a full cache has an entry");
            self.bounded.swap_remove(out);
        }
        self.bounded.push(Entry {
            rule,
            value: compile(),
            uses: 1,
            last_used: self.clock,
        });
        run(&self.bounded.last().expect("just pushed").value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    /// A cache of numbers, and how many times something was compiled.
    fn used(cache: &mut Cache<usize>, compiled: &Cell<usize>, rule: usize, always: bool) {
        let made = cache.with(
            rule,
            always,
            || {
                compiled.set(compiled.get() + 1);
                rule * 10
            },
            |value| *value,
        );
        assert_eq!(made, rule * 10);
    }

    fn held(cache: &Cache<usize>, rules: std::ops::Range<usize>) -> Vec<usize> {
        rules.filter(|rule| cache.holds(*rule)).collect()
    }

    #[test]
    fn keywordless_rules_stay_compiled() {
        for policy in [CachePolicy::None, CachePolicy::Lru(1), CachePolicy::Lfu(1)] {
            let (mut cache, compiled) = (Cache::new(policy), Cell::new(0));
            for _ in 0..3 {
                for rule in 0..5 {
                    used(&mut cache, &compiled, rule, true);
                }
                used(&mut cache, &compiled, 7, false);
                used(&mut cache, &compiled, 8, false);
            }
            assert_eq!(held(&cache, 0..5), [0, 1, 2, 3, 4], "{policy:?}");
            // the five were compiled once each; the two others, which take turns in a
            // cache of one or in none, every time
            assert_eq!(compiled.get(), 5 + 6, "{policy:?}");
        }
    }

    #[test]
    fn a_bounded_cache_never_exceeds_its_size() {
        for (policy, size) in [
            (CachePolicy::Lru(4), 4),
            (CachePolicy::Lfu(4), 4),
            (CachePolicy::Lru(1), 1),
            (CachePolicy::Lfu(0), 0),
            (CachePolicy::None, 0),
        ] {
            let (mut cache, compiled) = (Cache::new(policy), Cell::new(0));
            for round in 0..200 {
                used(&mut cache, &compiled, round * 7 % 23, false);
                assert!(cache.len() <= size, "{policy:?}");
            }
            assert_eq!(cache.len(), size, "{policy:?}");
        }
    }

    #[test]
    fn lfu_drops_the_least_used_ties_by_recency() {
        let (mut cache, compiled) = (Cache::new(CachePolicy::Lfu(3)), Cell::new(0));
        for rule in [1, 1, 1, 2, 2, 3] {
            used(&mut cache, &compiled, rule, false);
        }
        // 3 was used once: it makes room, although it was used last
        used(&mut cache, &compiled, 4, false);
        assert_eq!(held(&cache, 0..9), [1, 2, 4]);
        // 4 and 5 would tie at one use: the one used longer ago goes
        used(&mut cache, &compiled, 2, false);
        used(&mut cache, &compiled, 5, false);
        assert_eq!(held(&cache, 0..9), [1, 2, 5]);
        used(&mut cache, &compiled, 6, false);
        assert_eq!(held(&cache, 0..9), [1, 2, 6]);
        // what was dropped is compiled again, and starts counting again
        used(&mut cache, &compiled, 3, false);
        assert_eq!(held(&cache, 0..9), [1, 2, 3]);
        assert_eq!(compiled.get(), 7);
    }

    #[test]
    fn lru_drops_the_least_recent() {
        let (mut cache, compiled) = (Cache::new(CachePolicy::Lru(3)), Cell::new(0));
        for rule in [1, 1, 1, 2, 3, 1] {
            used(&mut cache, &compiled, rule, false);
        }
        // 2 was used longest ago, however often 1 was used
        used(&mut cache, &compiled, 4, false);
        assert_eq!(held(&cache, 0..9), [1, 3, 4]);
        used(&mut cache, &compiled, 3, false);
        used(&mut cache, &compiled, 5, false);
        assert_eq!(held(&cache, 0..9), [3, 4, 5]);
        assert_eq!(compiled.get(), 5);
    }

    #[test]
    fn policy_none_compiles_and_drops() {
        let (mut cache, compiled) = (Cache::new(CachePolicy::None), Cell::new(0));
        for _ in 0..4 {
            used(&mut cache, &compiled, 9, false);
            assert!(cache.is_empty());
        }
        assert_eq!(compiled.get(), 4);
        // a bounded cache compiles the same rule once
        let (mut cache, compiled) = (Cache::new(CachePolicy::Lru(2)), Cell::new(0));
        for _ in 0..4 {
            used(&mut cache, &compiled, 9, false);
        }
        assert_eq!((cache.len(), compiled.get()), (1, 1));
    }

    #[test]
    fn nothing_compiles_before_a_keyword_hit() {
        use crate::content_policy::Policy;
        for (policy, kept) in [
            (CachePolicy::None, 0),
            (CachePolicy::Lru(4), 1),
            (CachePolicy::Lfu(4), 1),
        ] {
            let shipped = Policy::embedded().with_cache(policy);
            assert_eq!(shipped.compiled_len(), 0);
            // plain prose: only the five rules without keywords, which stay
            assert_eq!(shipped.check("prefers tabs over spaces", 8000), None);
            assert_eq!(shipped.compiled_len(), 5, "{policy:?}");
            assert_eq!(shipped.check("we met at the cafe", 8000), None);
            assert_eq!(shipped.compiled_len(), 5, "{policy:?}");
            // a keyword brings in its rule and no other
            let found = shipped.check("the token ghp_ is described in the handbook", 8000);
            assert_eq!(found, None);
            let held = shipped.compiled_len();
            assert!(
                if kept == 0 {
                    held == 5
                } else {
                    (6..=9).contains(&held)
                },
                "{policy:?}: {held}"
            );
            // and many keywords never exceed the bound
            let many = "ghp_ gho_ glpat- xoxb sk_live akia AIza npm_ pypi- shpat_ hooks.slack";
            shipped.check(many, 8000);
            assert!(shipped.compiled_len() <= 5 + 4 * kept, "{policy:?}");
        }
    }
}
