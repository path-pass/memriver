//! One-rule rule sets that isolate each point where the Python and the `regex` dialects
//! part, with the Python verdict and the verdict the Rust policy must give. Written by
//! hand in `compat/record_policy_fixtures.py`, which fills in what Python answers.

use memriver_core::content_policy::Policy;
use memriver_core::content_policy::rules_loader::{RewriteError, load_rules, rewrite};
use serde_json::Value;

fn rows() -> Vec<Value> {
    include_str!("fixtures/policy_dialect.jsonl")
        .lines()
        .map(|line| serde_json::from_str(line).expect("a fixture line"))
        .collect()
}

fn verdict(row: &Value) -> Option<String> {
    let policy = Policy::from_sources(&[row["rules"].as_str().unwrap()]).expect("the rule loads");
    policy
        .check(row["text"].as_str().unwrap(), 8000)
        .map(|rejection| rejection.rule_id)
}

/// The pattern of a one-rule rule file as the fixture writes it.
fn pattern(row: &Value) -> &str {
    let rules = row["rules"].as_str().unwrap();
    let from = rules.find("'''").unwrap() + 3;
    &rules[from..rules.rfind("'''").unwrap()]
}

#[test]
fn every_row_gives_its_expected_rust_verdict() {
    let (mut verdicts, mut refused) = (0, 0);
    for row in rows() {
        if let Some(error) = row["rust_error"].as_str() {
            let found = rewrite(pattern(&row), None).expect_err("the pattern is refused");
            assert_eq!(format!("{found:?}"), error, "{}", row["rules"]);
            // and so the rule file does not load
            let loaded = load_rules(&[row["rules"].as_str().unwrap()]);
            assert!(loaded.unwrap_err().starts_with("rule r: "));
            refused += 1;
        } else {
            assert_eq!(verdict(&row).as_deref(), row["rust"].as_str(), "{row}");
            verdicts += 1;
        }
    }
    assert_eq!((verdicts, refused), (57, 8));
    assert_eq!(
        rewrite(r"A$\b", None),
        Err(RewriteError::DollarNotInTailPosition)
    );
}

#[test]
fn kelvin_and_long_s_match_the_narrowed_word_class_only_under_i() {
    let one = |pattern: &str| {
        Policy::from_sources(&[&format!("[[rules]]\nid = \"r\"\nregex = '''{pattern}'''\n")])
            .unwrap()
    };
    let hit = |pattern: &str, text: &str| one(pattern).check(text, 8000).is_some();
    for special in ["\u{212a}", "\u{17f}"] {
        assert!(hit(r"(?i)x\w", &format!("x{special}")));
        assert!(hit(r"(?i)x[\w-]", &format!("x{special}")));
        assert!(!hit(r"x\w", &format!("x{special}")));
        assert!(!hit(r"(?i)x(?-i)\w", &format!("x{special}")));
        // a digit class folds to nothing outside ASCII, with or without i
        assert!(!hit(r"(?i)x\d", &format!("x{special}")));
    }
    // nothing else outside ASCII, under i or not; the ASCII letters under both
    for other in [
        "\u{e9}", "\u{ff15}", "\u{130}", "\u{131}", "\u{3b1}", "\u{4e2d}",
    ] {
        assert!(!hit(r"(?i)x\w", &format!("x{other}")), "{other:?}");
        assert!(!hit(r"x\w", &format!("x{other}")), "{other:?}");
    }
    for ascii in ["K", "k", "s", "S", "_", "7"] {
        assert!(hit(r"(?i)x\w", &format!("x{ascii}")) && hit(r"x\w", &format!("x{ascii}")));
    }
    // the fixture states the same four rows with what Python answers
    let stated = rows()
        .iter()
        .filter(|row| {
            row["text"]
                .as_str()
                .is_some_and(|text| text.ends_with('\u{212a}') || text.ends_with('\u{17f}'))
                && pattern(row).ends_with(r"x\w")
        })
        .map(|row| {
            (
                pattern(row).starts_with("(?i)"),
                row["python"].is_string(),
                row["rust"].is_string(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        stated,
        [
            (true, true, true),
            (true, true, true),
            (false, true, false),
            (false, true, false)
        ]
    );
}

#[test]
fn the_listed_unicode_differences_are_exactly_a_b_and_c() {
    let mut kinds = std::collections::BTreeMap::new();
    for row in rows().iter().filter(|row| row["text"].is_string()) {
        let text = row["text"].as_str().unwrap();
        let differs = row["python"] != row["rust"];
        match row["difference"].as_str() {
            Some(kind) => {
                assert!(["a", "b", "c"].contains(&kind), "{row}");
                assert!(differs, "a row tagged as a difference must differ: {row}");
                // no difference is ever on ASCII text
                assert!(!text.is_ascii(), "{row}");
                *kinds.entry(kind.to_string()).or_insert(0) += 1;
            }
            None => assert!(!differs, "an untagged row must agree with Python: {row}"),
        }
        // what the fixture says Rust answers is what it answers
        assert_eq!(verdict(row).as_deref(), row["rust"].as_str(), "{row}");
    }
    assert_eq!(kinds.keys().collect::<Vec<_>>(), ["a", "b", "c"]);
    assert!(kinds.values().all(|count| *count >= 2), "{kinds:?}");
}
