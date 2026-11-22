//! The content policy: refuses text that is empty, too large, or looks like a secret.

use std::sync::Mutex;

use regex::Regex;

use super::rule_cache::{Cache, CachePolicy};
use super::rules_loader::{Rule, load_rules};
use crate::models::helpers::py_strip;
use crate::settings::POLICY_RULE_CACHE;

// memriver's own floor rules first, so one of them wins over a vendored rule of the
// same id
const MEMRIVER_RULES: &str = include_str!(
    "../../../../packages/memriver-core/src/memriver_core/content_policy/rules/memriver.toml"
);
const GITLEAKS_RULES: &str = include_str!(
    "../../../../packages/memriver-core/src/memriver_core/content_policy/rules/gitleaks.toml"
);

/// Why a text may not be stored. The matched text is deliberately absent from the
/// message: it travels into logs and agent transcripts, where a secret must not go.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejection {
    pub rule_id: String,
    pub message: String,
}

fn secret(rule_id: &str) -> Rejection {
    Rejection {
        rule_id: rule_id.to_string(),
        message: format!(
            "content rejected: looks like a secret ({rule_id}). \
             Store a pointer (e.g. 'token is in 1Password item X') instead."
        ),
    }
}

/// C0 and C1 control characters and the Unicode line and paragraph separators: what
/// `single_line` turns into spaces. Stripping removes only white space, so a body of
/// nothing but these would read as non-empty and show as a blank index line.
fn control(c: char) -> bool {
    matches!(c, '\0'..='\u{1f}' | '\u{7f}'..='\u{9f}' | '\u{2028}' | '\u{2029}')
}

/// Shannon entropy in bits per character, the same float the Python implementation
/// computes: the comparison with a threshold depends on the last bit. So the terms are
/// taken in the order the characters first occur, and added the way Python's `sum`
/// adds floats -- with Neumaier's compensation, which carries the rounding error of
/// every addition along and puts it back at the end.
pub fn shannon_entropy(text: &str) -> f64 {
    let mut counts: Vec<(char, usize)> = Vec::new();
    let mut total = 0usize;
    for c in text.chars() {
        total += 1;
        match counts.iter_mut().find(|(seen, _)| *seen == c) {
            Some((_, count)) => *count += 1,
            None => counts.push((c, 1)),
        }
    }
    if total == 0 {
        return 0.0;
    }
    let (mut sum, mut lost) = (0.0_f64, 0.0_f64);
    for (_, count) in counts {
        let share = count as f64 / total as f64;
        let term = share * share.log2();
        let next = sum + term;
        lost += if sum.abs() >= term.abs() {
            (sum - next) + term
        } else {
            (term - next) + sum
        };
        sum = next;
    }
    if lost != 0.0 && lost.is_finite() {
        sum += lost;
    }
    -sum
}

/// What a rule measures in each of its matches in `text`: the declared group, else the
/// first, else the whole match -- the credential itself, not the words a pattern has to
/// anchor on, which would lower its entropy.
fn candidates<'t>(
    regex: &'t Regex,
    group: usize,
    text: &'t str,
) -> impl Iterator<Item = &'t str> + 't {
    regex.captures_iter(text).map(move |found| {
        [group, 1]
            .into_iter()
            .filter(|number| *number > 0)
            .find_map(|number| found.get(number))
            .or(found.get(0))
            .map_or("", |part| part.as_str())
    })
}

pub struct Policy {
    rules: Vec<Rule>,
    cache: Mutex<Cache<Regex>>,
}

impl Policy {
    /// The shipped rules. Nothing is compiled until a text needs a rule.
    pub fn embedded() -> Policy {
        Policy::from_sources(&[MEMRIVER_RULES, GITLEAKS_RULES])
            .expect("the shipped rule files load")
    }

    /// The rules of these rule files, read in order.
    ///
    /// Success means every pattern could be rewritten, not that it compiles: a pattern
    /// the `regex` crate refuses (a lookbehind, a backreference, `a{2,1}`) loads here
    /// and panics in the first `check` that needs its rule. The shipped rules all
    /// compile, which a test holds. Whoever loads rule files that are not the shipped
    /// ones while running must call `uncompilable` first and refuse a set for which it
    /// names a rule.
    pub fn from_sources(sources: &[&str]) -> Result<Policy, String> {
        Ok(Policy {
            rules: load_rules(sources)?,
            cache: Mutex::new(Cache::new(POLICY_RULE_CACHE)),
        })
    }

    /// The same rules with another way of keeping compiled ones; nothing compiled is
    /// carried over.
    pub fn with_cache(self, policy: CachePolicy) -> Policy {
        Policy {
            rules: self.rules,
            cache: Mutex::new(Cache::new(policy)),
        }
    }

    pub fn rules(&self) -> &[Rule] {
        &self.rules
    }

    /// How many compiled rules are held right now.
    pub fn compiled_len(&self) -> usize {
        self.cache().len()
    }

    fn cache(&self) -> std::sync::MutexGuard<'_, Cache<Regex>> {
        // a check that panicked left nothing half-done in the cache
        self.cache
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn with_rule<R>(&self, at: usize, run: impl FnOnce(&Regex) -> R) -> R {
        let rule = &self.rules[at];
        let compile = || {
            Regex::new(&rule.pattern)
                .unwrap_or_else(|_| panic!("rule {} does not compile", rule.id))
        };
        self.cache()
            .with(at, rule.keywords.is_empty(), compile, run)
    }

    /// Compiles every rule once, holding none: the ids of those that do not compile.
    pub fn uncompilable(&self) -> Vec<&str> {
        self.rules
            .iter()
            .filter(|rule| Regex::new(&rule.pattern).is_err())
            .map(|rule| rule.id.as_str())
            .collect()
    }

    /// Why `text` may not be stored, or None. `max_chars` is the caller's budget, in
    /// characters.
    pub fn check(&self, text: &str, max_chars: usize) -> Option<Rejection> {
        let visible: String = text.chars().filter(|c| !control(*c)).collect();
        if py_strip(&visible).is_empty() {
            return Some(Rejection {
                rule_id: "empty".to_string(),
                message: "content is empty; nothing to store".to_string(),
            });
        }
        let length = text.chars().count();
        if length > max_chars {
            return Some(Rejection {
                rule_id: "too-large".to_string(),
                message: format!(
                    "content too large ({length} > {max_chars} chars); \
                     store a summary or pointer instead"
                ),
            });
        }
        self.scan(text).map(secret)
    }

    /// The first rule `text` matches, in rule order.
    fn scan(&self, text: &str) -> Option<&str> {
        let lowered = text.to_lowercase();
        for (at, rule) in self.rules.iter().enumerate() {
            // a rule that declares keywords cannot match a text that holds none of
            // them, and skipping it is what keeps it from being compiled at all
            let passes = |keyword: &String| lowered.contains(keyword.as_str());
            if !rule.keywords.is_empty() && !rule.keywords.iter().any(passes) {
                continue;
            }
            let hit = self.with_rule(at, |regex| match rule.entropy {
                None => regex.is_match(text),
                // a rule can have several candidates in one text, and a first one of
                // low entropy must not hide a later one
                Some(threshold) => candidates(regex, rule.secret_group, text)
                    .any(|candidate| shannon_entropy(candidate) >= threshold),
            });
            if hit {
                return Some(&rule.id);
            }
        }
        None
    }

    /// What the rule `rule_id` would measure for entropy in `text`, match by match.
    #[doc(hidden)]
    pub fn measured(&self, rule_id: &str, text: &str) -> Vec<String> {
        let Some(at) = self.rules.iter().position(|rule| rule.id == rule_id) else {
            return Vec::new();
        };
        self.with_rule(at, |regex| {
            candidates(regex, self.rules[at].secret_group, text)
                .map(str::to_string)
                .collect()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn verdict(policy: &Policy, text: &str, max_chars: usize) -> Option<String> {
        policy
            .check(text, max_chars)
            .map(|rejection| rejection.rule_id)
    }

    #[test]
    fn empty_after_controls_and_strip() {
        let policy = Policy::embedded();
        for empty in [
            "",
            "   ",
            "\n\t \n",
            "\u{1}\u{2}",
            "\u{1b}\u{1b}",
            "\u{1c}\u{1f}",
            "\u{a0}\u{3000}",
            "\u{2028}\u{2029}\u{85}",
            "\u{7f} \u{9f}",
        ] {
            let rejection = policy.check(empty, 8000).expect("refused");
            assert_eq!(
                (rejection.rule_id.as_str(), rejection.message.as_str()),
                ("empty", "content is empty; nothing to store"),
                "{empty:?}"
            );
        }
        // empty comes first, whatever the budget
        assert_eq!(verdict(&policy, " ", 0).as_deref(), Some("empty"));
        // a control before real text does not make it empty, nor does a character
        // that is neither a control nor white space
        assert_eq!(verdict(&policy, "\u{1}note", 8000), None);
        assert_eq!(verdict(&policy, "\u{200b}", 8000), None);
        // nothing was compiled to find any of this out, but for the rules that always run
        assert_eq!(policy.compiled_len(), 5);
    }

    #[test]
    fn too_large_counts_scalars() {
        let policy = Policy::embedded();
        let wide = "\u{1f600}".repeat(8000);
        assert_eq!(verdict(&policy, &wide, 8000), None);
        let rejection = policy
            .check(&format!("{wide}\u{1f600}"), 8000)
            .expect("refused");
        assert_eq!(rejection.rule_id, "too-large");
        assert_eq!(
            rejection.message,
            "content too large (8001 > 8000 chars); store a summary or pointer instead"
        );
        assert_eq!(verdict(&policy, "abcdefg", 7), None);
        assert_eq!(
            verdict(&policy, "abcdefgh", 7).as_deref(),
            Some("too-large")
        );
        assert_eq!(verdict(&policy, "\u{9879}\u{76ee}\u{4e00}", 3), None);
        assert_eq!(
            verdict(&policy, "\u{9879}\u{76ee}\u{4e00}", 2).as_deref(),
            Some("too-large")
        );
        // the size is judged before any rule: a secret over the budget is too large
        let secret = "password = hunter2hunter2";
        assert_eq!(verdict(&policy, secret, 10).as_deref(), Some("too-large"));
        assert_eq!(
            verdict(&policy, secret, usize::MAX).as_deref(),
            Some("memriver-credential-assignment")
        );
    }

    #[test]
    fn a_secret_is_refused_by_the_first_rule_in_order_without_its_text() {
        let policy = Policy::embedded();
        let rejection = policy
            .check("deploy with password = hunter2hunter2", 8000)
            .expect("refused");
        assert_eq!(rejection.rule_id, "memriver-credential-assignment");
        assert_eq!(
            rejection.message,
            "content rejected: looks like a secret (memriver-credential-assignment). \
             Store a pointer (e.g. 'token is in 1Password item X') instead."
        );
        assert!(!rejection.message.contains("hunter2"));
        for stored in [
            "the password is in 1Password",
            "we rotate the API key monthly; see the runbook",
            "prefers tabs over spaces",
        ] {
            assert_eq!(verdict(&policy, stored, 8000), None, "{stored}");
        }
    }

    #[test]
    fn fullwidth_space_between_password_and_value_is_caught() {
        let policy = Policy::embedded();
        for text in [
            "password\u{3000}:\u{3000}hunter2hunter2",
            "the password\u{3000}is\u{3000}correcthorsebattery1",
            "token \u{a0}=\u{a0}abcdef",
            "password\u{1f}=\u{1c}hunter2hunter2",
        ] {
            assert_eq!(
                verdict(&policy, text, 8000).as_deref(),
                Some("memriver-credential-assignment"),
                "{text:?}"
            );
        }
    }

    #[test]
    fn fullwidth_digit_inside_a_token_is_not_caught() {
        // a word or digit class is ASCII here: the Python implementation, where it is
        // Unicode, refuses both of these
        let rules =
            "[[rules]]\nid = \"token\"\nregex = 'tok_\\w{8}\\d{4}'\nkeywords = [\"tok_\"]\n";
        let policy = Policy::from_sources(&[rules]).unwrap();
        assert_eq!(
            verdict(&policy, "tok_abcdefgh1234", 8000).as_deref(),
            Some("token")
        );
        assert_eq!(verdict(&policy, "tok_abcdefgh12\u{ff13}4", 8000), None);
        assert_eq!(verdict(&policy, "tok_abcd\u{4e2d}fgh1234", 8000), None);
    }

    #[test]
    fn entropy_sum_order_matches_python() {
        // the floats the Python implementation computes, to the last bit
        for (text, bits) in [
            ("aabbbcccc", 0x3ff8_7ce6_4998_7492_u64),
            ("Zx9!Zx9!kQ", 0x4004_2ce8_a313_3d0b),
            ("abcabcabcd0123", 0x4006_4ebf_1d05_3e27),
            ("sk-\u{e9}\u{e9}a1b2c3", 0x400a_388d_64c0_8bce),
            (
                "1234567890abcdefghijklmnopqrstuvwxyzABC",
                0x4015_2440_7ab0_e073,
            ),
            ("aab", 0x3fed_62ad_f1ea_257c),
        ] {
            assert_eq!(shannon_entropy(text).to_bits(), bits, "{text}");
        }
        assert_eq!(shannon_entropy("").to_bits(), 0.0_f64.to_bits());
        assert_eq!(shannon_entropy("aaaa"), 0.0);
        assert_eq!(
            (shannon_entropy("aabb"), shannon_entropy("abcd")),
            (1.0, 2.0)
        );
    }

    #[test]
    fn entropy_is_measured_on_the_declared_group_then_the_first_then_the_whole_match() {
        let rule = |pattern: &str, more: &str| {
            Policy::from_sources(&[&format!(
                "[[rules]]\nid = \"r\"\nregex = '{pattern}'\nentropy = 1.0\n{more}"
            )])
            .unwrap()
        };
        let hit = |policy: &Policy, text: &str| policy.check(text, 8000).is_some();
        // 'ZQ-aaaaaaaa' as a whole measures about 1.28; its group 'aaaaaaaa' measures 0
        let whole = rule("ZQ-\\w+", "");
        assert!(hit(&whole, "see ZQ-aaaaaaaa here"));
        let first = rule("ZQ-(\\w+)", "");
        assert!(!hit(&first, "see ZQ-aaaaaaaa here"));
        assert!(hit(&first, "see ZQ-abcdefgh here"));
        let declared = rule("ZQ-(\\w+)-(\\w+)", "secretGroup = 2\n");
        assert!(!hit(&declared, "ZQ-abcdefgh-aaaaaaaa"));
        assert!(hit(&declared, "ZQ-aaaaaaaa-abcdefgh"));
        // a group that took no part falls back to the first, then to the whole match
        let absent = rule("ZQ-(\\w+)(?:-(x))?", "secretGroup = 2\n");
        assert!(hit(&absent, "ZQ-abcdefgh") && !hit(&absent, "ZQ-aaaaaaaa"));
        // every candidate is measured, not only the first
        assert!(hit(&first, "ZQ-aaaaaaaa then ZQ-abcdefgh"));
        assert!(!hit(&first, "ZQ-aaaaaaaa then ZQ-bbbbbbbb"));
        // exactly at the threshold counts
        assert!(hit(&rule("ZQ-(\\w+)", ""), "ZQ-ab"));
        assert_eq!(first.measured("r", "ZQ-aa ZQ-bc"), ["aa", "bc"]);
    }

    #[test]
    fn the_keyword_prefilter_folds_case_and_decides_the_rule_that_is_named() {
        let rules = "[[rules]]\nid = \"second\"\nregex = 'ZQ9[0-9]{4}'\nkeywords = [\"ZQ9\"]\n\n[[rules]]\nid = \"never\"\nregex = '[0-9]{4}'\nkeywords = [\"absent\"]\n\n[[rules]]\nid = \"third\"\nregex = '(?i)zq9[0-9]{4}'\n";
        let policy = Policy::from_sources(&[rules])
            .unwrap()
            .with_cache(CachePolicy::Lru(8));
        // the keyword is found whatever its case in the text; the pattern still decides
        assert_eq!(verdict(&policy, "ZQ91234", 8000).as_deref(), Some("second"));
        assert_eq!(verdict(&policy, "zq91234", 8000).as_deref(), Some("third"));
        assert_eq!(verdict(&policy, "nothing 1234", 8000), None);
        // the rule whose keyword never came was never compiled
        assert_eq!(policy.compiled_len(), 2);
    }
}
