//! Keyword search: a query becomes terms, an entry's texts give it a rank.
//!
//! One rule for both stores: the query and every searched text are folded the same
//! way (NFKC, then case-folded), an entry matches when any term occurs in any of its
//! texts, and matches sort by rank (larger first), recency within equal ranks.
//! Folded text is never stored.

use caseless::Caseless;
use unicode_normalization::UnicodeNormalization;

use super::helpers::py_isspace;
use super::memory::Memory;

pub const MAX_TERMS: usize = 16;

/// How texts answer terms, compared as a tuple, larger first: the whole query (the
/// terms joined by one space) occurs in one text; the distinct terms found in any
/// text, 0 being no match; the terms found in the description.
pub type Rank = (bool, usize, usize);

/// The form texts are compared in: NFKC, then full case folding.
pub fn fold(text: &str) -> String {
    caseless::default_case_fold_str(&text.nfkc().collect::<String>())
}

/// The query's distinct terms in order, at most `MAX_TERMS`; none matches nothing.
/// Split on whitespace and on the comma, the semicolon and the ideographic comma (the
/// full-width comma and semicolon are listed although NFKC already turns them into
/// the ASCII ones). NUL is dropped first, so a NUL-only query has no terms.
///
/// The query is folded as it is read, and reading stops at the last term that counts:
/// what follows it cannot change the answer, however long it is.
pub fn search_terms(query: &str) -> Vec<String> {
    let separator =
        |c| py_isspace(c) || matches!(c, ',' | ';' | '\u{3001}' | '\u{ff0c}' | '\u{ff1b}');
    // the same characters as `fold` gives for the query without its NULs
    let folded = query
        .chars()
        .filter(|c| *c != '\0')
        .nfkc()
        .default_case_fold();
    let mut terms: Vec<String> = Vec::new();
    let mut piece = String::new();
    // the end of the query ends a term as a separator does
    for c in folded.map(Some).chain([None]) {
        match c {
            Some(c) if !separator(c) => piece.push(c),
            _ if piece.is_empty() || terms.contains(&piece) => piece.clear(),
            _ => {
                terms.push(std::mem::take(&mut piece));
                if terms.len() == MAX_TERMS {
                    break;
                }
            }
        }
    }
    terms
}

fn key(terms: &[String], phrase: &str, folded: &[String], cue: &str) -> Rank {
    let holds = |term: &str| folded.iter().any(|text| text.contains(term));
    (
        !terms.is_empty() && holds(phrase),
        terms.iter().filter(|term| holds(term)).count(),
        terms
            .iter()
            .filter(|term| cue.contains(term.as_str()))
            .count(),
    )
}

/// How `texts` answer `terms`. `description` (a memory's, one of `texts`) counts the
/// terms it holds on its own.
pub fn rank_key(terms: &[String], texts: &[&str], description: Option<&str>) -> Rank {
    let folded: Vec<String> = texts.iter().map(|text| fold(text)).collect();
    key(
        terms,
        &terms.join(" "),
        &folded,
        &description.map(fold).unwrap_or_default(),
    )
}

/// A memory's rank for `terms`, `phrase` being the terms joined by one space; None
/// when no term occurs in its description or body.
pub fn rank(terms: &[String], phrase: &str, description: &str, body: &str) -> Option<Rank> {
    let folded = [fold(description), fold(body)];
    let found = key(terms, phrase, &folded, &folded[0]);
    (found.1 > 0).then_some(found)
}

/// The memories `terms` match, best first; the given order breaks ties, so pass them
/// newest first.
pub fn rank_memories(terms: &[String], memories: Vec<Memory>) -> Vec<Memory> {
    let phrase = terms.join(" ");
    let mut ranked: Vec<(Rank, Memory)> = memories
        .into_iter()
        .filter_map(|memory| {
            rank(terms, &phrase, &memory.description, &memory.body).map(|found| (found, memory))
        })
        .collect();
    // stable: equal ranks keep the given order
    ranked.sort_by_key(|(found, _)| std::cmp::Reverse(*found));
    ranked.into_iter().map(|(_, memory)| memory).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::memory::{Memory, MemoryType, Trust};

    fn terms(words: &[&str]) -> Vec<String> {
        words.iter().map(|word| word.to_string()).collect()
    }

    fn memory(body: &str, description: &str) -> Memory {
        Memory::new(
            body,
            MemoryType::Project,
            "zzzzzzzzzz",
            "test",
            "agent",
            Trust::Agent,
            true,
            description,
        )
        .unwrap()
    }

    #[test]
    fn whitespace_and_the_five_separators_split_terms() {
        for query in [
            "uv pip",
            "  uv\tpip\n",
            "uv\u{3000}pip",
            "uv\u{a0}pip",
            "uv,pip",
            "uv\u{ff0c}pip",
            "uv\u{3001}pip",
            "uv;pip",
            "uv\u{ff1b}pip",
            "uv , ;\u{3001}pip",
            "uv\u{1f}pip",
        ] {
            assert_eq!(search_terms(query), ["uv", "pip"], "{query:?}");
        }
    }

    #[test]
    fn duplicates_keep_their_first_occurrence() {
        assert_eq!(search_terms("uv pip UV Pip ruff uv"), ["uv", "pip", "ruff"]);
    }

    #[test]
    fn at_most_sixteen_terms_the_rest_ignored() {
        let words: Vec<String> = (0..20).map(|n| format!("w{n}")).collect();
        assert_eq!(MAX_TERMS, 16);
        assert_eq!(search_terms(&words.join(" ")), words[..16]);
        // the cap counts distinct terms: repeats do not use it up
        let repeated = format!("{} {}", ["w0"; 20].join(" "), words.join(" "));
        assert_eq!(search_terms(&repeated), words[..16]);
    }

    #[test]
    fn a_long_tail_of_terms_is_not_read_past_the_sixteenth() {
        let words: Vec<String> = (0..50_000).map(|n| format!("w{n}")).collect();
        let query = words.join(" ");
        let started = std::time::Instant::now();
        let found = search_terms(&query);
        let took = started.elapsed();
        assert_eq!(found, words[..16]);
        // a generous bound: the work must not grow with the terms that are never used
        assert!(took < std::time::Duration::from_millis(50), "{took:?}");
        // a term that ends the query, and one cut off by nothing, are still whole
        assert_eq!(search_terms(&words[..16].join(",")), words[..16]);
        assert_eq!(
            search_terms(&format!("{}x", words[..16].join(";")))
                .last()
                .unwrap(),
            "w15x"
        );
        // folding still comes first: the sixteenth term is found in its folded form, and a
        // repeat that only folding reveals does not count
        let folded = format!(
            "W0 \u{ff37}0 {} \u{ff37}16 w17",
            words[1..15].join("\u{3000}")
        );
        assert_eq!(
            search_terms(&folded),
            [&words[..15], &["w16".to_string()]].concat()
        );
    }

    #[test]
    fn a_query_with_no_term_has_no_terms() {
        for query in [
            "",
            "   ",
            "\u{3000}",
            ",;\u{3001}\u{ff0c}\u{ff1b}",
            "\0",
            " \0 ",
        ] {
            assert!(search_terms(query).is_empty(), "{query:?}");
        }
    }

    #[test]
    fn nul_is_dropped_not_a_separator() {
        assert_eq!(search_terms("u\0v"), ["uv"]);
    }

    #[test]
    fn no_quoting_or_operators() {
        assert_eq!(
            search_terms("\"uv pip\" -ruff OR x"),
            ["\"uv", "pip\"", "-ruff", "or", "x"]
        );
    }

    #[test]
    fn full_width_letters_fold_to_ascii() {
        assert_eq!(
            search_terms("\u{ff35}\u{ff36} \u{ff30}\u{ff29}\u{ff30}"),
            ["uv", "pip"]
        );
        assert_eq!(
            rank_key(&terms(&["uv"]), &["\u{ff35}\u{ff36} manages python"], None).1,
            1
        );
    }

    #[test]
    fn case_folding_goes_beyond_lower() {
        assert_eq!(search_terms("STRASSE"), ["strasse"]);
        assert_eq!(search_terms("stra\u{df}e"), ["strasse"]);
        assert_eq!(
            rank_key(&search_terms("STRASSE"), &["Die Stra\u{df}e"], None).1,
            1
        );
        assert_eq!(
            rank_key(
                &search_terms("\u{c4}rger"),
                &["\u{c4}RGER mit Umlauten"],
                None
            )
            .1,
            1
        );
        assert_eq!(
            fold("\u{130}stanbul \u{1c5} \u{fb01} \u{1e9e} \u{3c2}"),
            "i\u{307}stanbul d\u{17e} fi ss \u{3c3}"
        );
    }

    #[test]
    fn cjk_two_character_terms_match_inside_a_sentence() {
        let found = search_terms("executor \u{5c42} classifier \u{5e76}\u{56de} memriver");
        assert_eq!(
            found,
            [
                "executor",
                "\u{5c42}",
                "classifier",
                "\u{5e76}\u{56de}",
                "memriver"
            ]
        );
        let rank = rank_key(
            &found,
            &["\u{628a} classifier \u{5e76}\u{56de}\u{5230} memriver \u{91cc}"],
            None,
        );
        assert_eq!(rank, (false, 3, 0));
    }

    #[test]
    fn no_term_found_is_rank_zero() {
        assert_eq!(
            rank_key(&terms(&["uv", "pip"]), &["ruff only"], None),
            (false, 0, 0)
        );
        assert_eq!(rank_key(&[], &["anything"], None), (false, 0, 0));
        assert_eq!(rank(&terms(&["uv", "pip"]), "uv pip", "ruff", "only"), None);
        assert_eq!(rank(&[], "", "anything", "at all"), None);
    }

    #[test]
    fn the_whole_query_is_the_terms_joined_by_one_space() {
        let found = search_terms("uv,  PIP");
        assert!(rank_key(&found, &["use uv pip install"], None).0);
        // both terms, not the phrase
        assert_eq!(rank_key(&found, &["use uv, pip"], None), (false, 2, 0));
        assert_eq!(
            rank(&found, "uv pip", "cue", "use UV Pip install"),
            Some((true, 2, 0))
        );
    }

    #[test]
    fn terms_are_counted_once_across_every_text() {
        assert_eq!(
            rank_key(&terms(&["uv", "pip"]), &["uv", "uv pip", "pip"], None),
            (true, 2, 0)
        );
    }

    #[test]
    fn the_description_counts_its_own_terms() {
        let both = terms(&["uv", "pip"]);
        assert_eq!(
            rank_key(&both, &["uv cue", "pip body"], Some("uv cue")),
            (false, 2, 1)
        );
        assert_eq!(rank_key(&both, &["uv cue", "pip body"], None).2, 0);
        assert_eq!(
            rank(&both, "uv pip", "uv cue", "pip body"),
            Some((false, 2, 1))
        );
    }

    #[test]
    fn rank_memories_drops_non_matches_and_orders_by_the_four_rules() {
        let found = search_terms("uv pip");
        let newest_one_term = memory("uses uv", "");
        let two_terms_body = memory("uv, and pip", "");
        let two_terms_cue = memory("and pip", "uv");
        let whole_older = memory("run uv pip install", "");
        let whole_newer = memory("uv pip everywhere", "");
        let unrelated = memory("ruff", "");
        // given newest first, as the stores pass them
        let given = vec![
            newest_one_term.clone(),
            unrelated,
            whole_newer.clone(),
            two_terms_body.clone(),
            two_terms_cue.clone(),
            whole_older.clone(),
        ];
        assert_eq!(
            rank_memories(&found, given),
            [
                whole_newer, // rule 1, then recency (rule 4)
                whole_older,
                two_terms_cue, // rule 2, then rule 3 beats recency
                two_terms_body,
                newest_one_term
            ]
        );
    }

    #[test]
    fn more_terms_found_beats_more_terms_in_the_description() {
        let found = search_terms("uv pip");
        // neither holds the whole query; one has both terms and none in its description,
        // the other has one term, in its description
        let both_in_body = memory("uv, pip", "");
        let one_in_cue = memory("x", "uv");
        assert_eq!(rank(&found, "uv pip", "", "uv, pip"), Some((false, 2, 0)));
        assert_eq!(rank(&found, "uv pip", "uv", "x"), Some((false, 1, 1)));
        for given in [
            vec![one_in_cue.clone(), both_in_body.clone()],
            vec![both_in_body.clone(), one_in_cue.clone()],
        ] {
            assert_eq!(
                rank_memories(&found, given),
                [both_in_body.clone(), one_in_cue.clone()]
            );
        }
    }

    #[test]
    fn rank_memories_with_one_term_keeps_the_given_order_but_puts_cue_hits_first() {
        let (older_body, cue_hit, newer_body) =
            (memory("uv a", ""), memory("x", "uv"), memory("uv b", ""));
        assert_eq!(
            rank_memories(
                &terms(&["uv"]),
                vec![newer_body.clone(), cue_hit.clone(), older_body.clone()]
            ),
            [cue_hit, newer_body, older_body]
        );
    }
}
