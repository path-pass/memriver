//! Loads the rule files and turns each pattern into the text the `regex` crate compiles.
//!
//! The rules are written in RE2 syntax, which the crate reads natively. What it reads
//! differently from the Python implementation is rewritten here, by one scanner that
//! tracks escapes, bracket expressions and groups, so that on text made only of ASCII
//! characters the two give the same verdict:
//!
//! 1. `\w` and `\d` become the ASCII classes `[[:word:]]` and `[[:digit:]]`. Where the
//!    `i` flag is on, the crate's case folding lets the word class also match U+212A
//!    (Kelvin sign) and U+017F (long s), as Python does; nothing else outside ASCII.
//! 2. `\s` is Python's: Unicode white space plus U+001C..U+001F.
//! 3. `$` is Python's -- the end of the text, or just before one final line feed -- and
//!    is accepted only where nothing can follow it (`dollar_is_in_tail_position`).
//!
//! Anything this cannot translate with certainty is an error, never a guess.

use std::fmt;

use toml::de::{DeTable, DeValue};

/// One rule, ready to compile.
#[derive(Debug, Clone, PartialEq)]
pub struct Rule {
    pub id: String,
    /// The pattern as the `regex` crate compiles it: rewritten, lookaheads expanded.
    pub pattern: String,
    /// The entropy a candidate must reach, where the rule's threshold is honoured.
    pub entropy: Option<f64>,
    pub secret_group: usize,
    /// Lower-cased. A text that contains none of them cannot match the rule.
    pub keywords: Vec<String>,
}

/// Why a pattern is not rewritten.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RewriteError {
    /// The `m` flag is turned on: `$` is then the end of a line in both engines and
    /// must not be rewritten.
    MultilineFlag,
    /// Something can follow a `$`, so consuming the final line feed would change what
    /// matches.
    DollarNotInTailPosition,
    /// A `$` stands inside the group whose text is measured for entropy.
    DollarInsideMeasuredGroup,
    /// A class the two engines do not read alike: a POSIX name the Python
    /// implementation does not know, `\p`, or a bracket expression that nests or uses
    /// the crate's set operators.
    UnknownClass,
}

impl fmt::Display for RewriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::MultilineFlag => "the pattern turns the m flag on",
            Self::DollarNotInTailPosition => "a $ is not at the end of the pattern",
            Self::DollarInsideMeasuredGroup => "a $ is inside the group measured for entropy",
            Self::UnknownClass => "a character class is not one both engines read alike",
        })
    }
}

impl std::error::Error for RewriteError {}

/// The POSIX class names the Python implementation translates; any other drops the rule
/// there, so it is refused here.
const POSIX_CLASSES: [&str; 8] = [
    "alnum", "alpha", "digit", "lower", "upper", "xdigit", "space", "word",
];

const FLAG_LETTERS: &str = "imsRUux-";

/// Whether `chars[at]` is a quantifier's first character.
fn quantifier(chars: &[char], at: usize) -> bool {
    matches!(chars.get(at), Some('*' | '+' | '?' | '{'))
}

/// From `at`, inside a bracket expression just after its `[`: the index after its `]`.
fn after_class(chars: &[char], mut at: usize) -> usize {
    // a leading '^' or ']' does not close the class it opens
    if chars.get(at) == Some(&'^') {
        at += 1;
    }
    if chars.get(at) == Some(&']') {
        at += 1;
    }
    while let Some(&c) = chars.get(at) {
        match c {
            '\\' => at += 2,
            '[' if chars.get(at + 1) == Some(&':') => {
                let close = (at + 2..chars.len().saturating_sub(1))
                    .find(|&k| chars[k] == ':' && chars[k + 1] == ']');
                at = close.map_or(at + 1, |k| k + 2);
            }
            ']' => return at + 1,
            _ => at += 1,
        }
    }
    at
}

/// Whether nothing can follow the `$` before index `at` (the index after it): the next
/// token is a `|` (the rest of that alternation is skipped to its closing parenthesis),
/// a `)` not followed by a quantifier, or the end -- repeatedly, to the end.
fn tail_position(chars: &[char], mut at: usize) -> bool {
    loop {
        match chars.get(at) {
            None => return true,
            Some('|') => {
                // the other alternatives are siblings of the `$`, not what follows it
                let mut depth = 0usize;
                at += 1;
                loop {
                    match chars.get(at) {
                        None => return true,
                        Some('\\') => at += 2,
                        Some('[') => at = after_class(chars, at + 1),
                        Some('(') => {
                            depth += 1;
                            at += 1;
                        }
                        Some(')') if depth == 0 => break,
                        Some(')') => {
                            depth -= 1;
                            at += 1;
                        }
                        Some(_) => at += 1,
                    }
                }
            }
            Some(')') => {
                at += 1;
                if quantifier(chars, at) {
                    return false;
                }
            }
            // a character, a class, a group, or a zero-width assertion such as `\b`
            Some(_) => return false,
        }
    }
}

/// Whether the `$` at byte offset `at` of `pattern` is in tail position: nothing of the
/// pattern can match after it.
pub fn dollar_is_in_tail_position(pattern: &str, at: usize) -> bool {
    let chars: Vec<char> = pattern.chars().collect();
    let index = pattern[..at].chars().count();
    chars.get(index) == Some(&'$') && tail_position(&chars, index + 1)
}

/// `pattern` as the `regex` crate must compile it to match as the Python
/// implementation does on ASCII text. `measured_group` is the `secretGroup` of a rule
/// whose entropy threshold is honoured (0 when it declares none), None for any other
/// rule.
pub fn rewrite(pattern: &str, measured_group: Option<usize>) -> Result<String, RewriteError> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut out = String::with_capacity(pattern.len() + 32);
    // the capture number of every open group, 0 for one that captures nothing
    let mut open: Vec<usize> = Vec::new();
    let mut captures = 0usize;
    let mut dollars = 0usize;
    let mut in_class = false;
    let mut at = 0usize;
    while let Some(&c) = chars.get(at) {
        if c == '\\' {
            let Some(&escaped) = chars.get(at + 1) else {
                out.push(c);
                break;
            };
            match (escaped, in_class) {
                ('w', false) => out.push_str("[[:word:]]"),
                ('w', true) => out.push_str("[:word:]"),
                ('d', false) => out.push_str("[[:digit:]]"),
                ('d', true) => out.push_str("[:digit:]"),
                ('W', _) => out.push_str("[^[:word:]]"),
                ('D', _) => out.push_str("[^[:digit:]]"),
                ('s', false) => out.push_str(r"[\s\x1c-\x1f]"),
                ('s', true) => out.push_str(r"\s\x1c-\x1f"),
                // inside a bracket expression this is a nested class
                ('S', _) => out.push_str(r"[^\s\x1c-\x1f]"),
                ('p' | 'P', _) => return Err(RewriteError::UnknownClass),
                _ => {
                    out.push(c);
                    out.push(escaped);
                }
            }
            at += 2;
            continue;
        }
        if in_class {
            match c {
                '[' if chars.get(at + 1) == Some(&':') => {
                    let close = (at + 2..chars.len().saturating_sub(1))
                        .find(|&k| chars[k] == ':' && chars[k + 1] == ']')
                        .ok_or(RewriteError::UnknownClass)?;
                    let name: String = chars[at + 2..close].iter().collect();
                    if !POSIX_CLASSES.contains(&name.as_str()) {
                        return Err(RewriteError::UnknownClass);
                    }
                    out.extend(&chars[at..close + 2]);
                    at = close + 2;
                    continue;
                }
                // a member in RE2, the start of a nested class to the crate
                '[' => return Err(RewriteError::UnknownClass),
                // set operators to the crate, two members in RE2
                '&' | '-' | '~' if chars.get(at + 1) == Some(&c) => {
                    return Err(RewriteError::UnknownClass);
                }
                ']' => in_class = false,
                _ => {}
            }
            out.push(c);
            at += 1;
            continue;
        }
        match c {
            '[' => {
                in_class = true;
                out.push(c);
                at += 1;
                // a leading '^' or ']' does not close the class it opens
                for first in ['^', ']'] {
                    if chars.get(at) == Some(&first) {
                        out.push(first);
                        at += 1;
                    }
                }
                continue;
            }
            '(' if chars.get(at + 1) == Some(&'?') => {
                let named = match (chars.get(at + 2), chars.get(at + 3)) {
                    (Some('P'), Some('<')) => true,
                    (Some('<'), Some(next)) => !matches!(next, '=' | '!'),
                    _ => false,
                };
                let flags_end = (at + 2..chars.len())
                    .find(|&k| !FLAG_LETTERS.contains(chars[k]))
                    .unwrap_or(chars.len());
                let flags: String = chars[at + 2..flags_end].iter().collect();
                let turned_on = flags.split('-').next().unwrap_or("");
                match chars.get(flags_end) {
                    _ if named => {
                        captures += 1;
                        open.push(captures);
                    }
                    Some(')') | Some(':') if turned_on.contains('m') => {
                        return Err(RewriteError::MultilineFlag);
                    }
                    // flags for the rest of the enclosing group: no group is opened
                    Some(')') => {
                        out.extend(&chars[at..=flags_end]);
                        at = flags_end + 1;
                        continue;
                    }
                    _ => open.push(0),
                }
            }
            '(' => {
                captures += 1;
                open.push(captures);
            }
            ')' => {
                open.pop();
            }
            '$' => {
                if !tail_position(&chars, at + 1) {
                    return Err(RewriteError::DollarNotInTailPosition);
                }
                // the group measured is the declared one, else the first
                let measured = |group: usize| group > 0 && open.contains(&group);
                if measured_group.is_some_and(|group| measured(group) || measured(1)) {
                    return Err(RewriteError::DollarInsideMeasuredGroup);
                }
                dollars += 1;
                // consumes the final line feed, which is why nothing may follow
                out.push_str(r"(?:\n?\z)");
                at += 1;
                continue;
            }
            _ => {}
        }
        out.push(c);
        at += 1;
    }
    // without the group, the whole match is measured, the consumed line feed with it
    let whole = |group: usize| group.max(1) > captures;
    if dollars > 0 && measured_group.is_some_and(whole) {
        return Err(RewriteError::DollarInsideMeasuredGroup);
    }
    Ok(out)
}

/// What every value of a credential rule may be: a quoted string, or a run without
/// white space.
const VALUE: &str = r#"(?:"[^"]{6,}"|'[^']{6,}'|\S{6,})"#;
/// The separators of the credential rule, the two after `is` guarded by lookaheads.
const SEPARATORS: &str = r#"(?:\s*[:=]\s*|\s+is\s+(?=["'])|\s+is\s+(?=\S{12})(?=\S{16}|\S*[0-9]))"#;

/// The pattern of the one rule that uses lookaheads, which the `regex` crate has none
/// of, with each lookahead replaced by the values it admits:
///
/// - `\s+is\s+(?=["'])` then a value: a quoted string, or a quote and five more;
/// - `\s+is\s+(?=\S{12})(?=\S{16}|\S*[0-9])` then a value: sixteen characters without
///   white space, or a run of at least twelve that holds a digit.
///
/// Whether the rule matches is all that is ever asked of it, and that is unchanged. The
/// rule file stays the only source: a pattern that does not end in exactly this shape
/// is an error.
pub fn expand_credential_lookaheads(pattern: &str) -> Result<String, String> {
    let head = pattern
        .strip_suffix(VALUE)
        .and_then(|rest| rest.strip_suffix(SEPARATORS))
        .filter(|head| !head.contains("(?=") && !head.contains("(?!") && !head.contains("(?<"))
        .ok_or("a pattern with lookaheads is not the credential rule's shape")?;
    let quoted = r#"(?:"[^"]{6,}"|'[^']{6,}'|["']\S{5,})"#;
    // a run of at least twelve that holds a digit, by the place of its first digit
    let mut with_digit: Vec<String> = (0..12)
        .map(|first| format!(r"[^\s0-9]{{{first}}}[0-9]\S{{{}}}", 11 - first))
        .collect();
    with_digit.push(r"[^\s0-9]{12,15}[0-9]".to_string());
    Ok(format!(
        r"{head}(?:\s*[:=]\s*{VALUE}|\s+is\s+{quoted}|\s+is\s+(?:\S{{16}}|{}))",
        with_digit.join("|")
    ))
}

fn number(value: &DeValue<'_>) -> Option<f64> {
    let text = match value {
        DeValue::Float(float) => float.as_str(),
        DeValue::Integer(integer) if integer.radix() == 10 => integer.as_str(),
        _ => return None,
    };
    text.replace('_', "").parse().ok()
}

/// Every rule of `sources` with its pattern as written, and the ids a `[policy]` table
/// lists, if a source has one.
fn read(sources: &[&str]) -> Result<(Vec<Rule>, Option<Vec<String>>), String> {
    // every rule as written; what counts of it is decided once all sources are read
    let mut raw: Vec<Rule> = Vec::new();
    let mut honoured: Option<Vec<String>> = None;
    for source in sources {
        let config = DeTable::parse(source)
            .map_err(|_| "a rule file is not TOML".to_string())?
            .into_inner();
        let top = |name: &str| {
            config
                .iter()
                .find(|(key, _)| key.get_ref() == name)
                .map(|(_, value)| value.get_ref())
        };
        if let Some(policy) = top("policy") {
            let listed = policy
                .get("honor_entropy_only_for")
                .and_then(|ids| ids.get_ref().as_array())
                .map(|ids| {
                    ids.iter()
                        .filter_map(|id| id.get_ref().as_str().map(str::to_string))
                        .collect()
                });
            honoured = Some(listed.unwrap_or_default());
        }
        let Some(rules) = top("rules").and_then(DeValue::as_array) else {
            continue;
        };
        for rule in rules.iter() {
            let rule = rule.get_ref();
            let text = |name: &str| rule.get(name).and_then(|value| value.get_ref().as_str());
            let id = text("id").ok_or("a rule has no id")?;
            let Some(pattern) = text("regex").filter(|pattern| !pattern.is_empty()) else {
                continue;
            };
            if raw.iter().any(|seen| seen.id == id) {
                continue;
            }
            let entropy = match rule.get("entropy") {
                Some(value) => Some(
                    number(value.get_ref()).ok_or(format!("rule {id}: entropy is no number"))?,
                ),
                None => None,
            };
            let secret_group = match rule.get("secretGroup") {
                Some(value) => number(value.get_ref())
                    .filter(|group| group.fract() == 0.0 && (0.0..1e6).contains(group))
                    .ok_or(format!("rule {id}: secretGroup is no group number"))?
                    as usize,
                None => 0,
            };
            let keywords = rule
                .get("keywords")
                .and_then(|keywords| keywords.get_ref().as_array())
                .map(|keywords| {
                    keywords
                        .iter()
                        .filter_map(|keyword| keyword.get_ref().as_str().map(str::to_lowercase))
                        .collect()
                })
                .unwrap_or_default();
            raw.push(Rule {
                id: id.to_string(),
                pattern: pattern.to_string(),
                entropy,
                secret_group,
                keywords,
            });
        }
    }
    Ok((raw, honoured))
}

/// The rules of `sources`, in order; of two rules with one id the first counts, so the
/// floor rules, loaded first, are never displaced. A rule without a pattern gates files,
/// not content, and is left out. A `[policy]` table names in `honor_entropy_only_for`
/// the rules whose entropy threshold counts; without one in any source, every
/// threshold does.
pub fn load_rules(sources: &[&str]) -> Result<Vec<Rule>, String> {
    let (raw, honoured) = read(sources)?;
    raw.into_iter()
        .map(|rule| {
            let counts = honoured.as_ref().is_none_or(|ids| ids.contains(&rule.id));
            let entropy = rule.entropy.filter(|_| counts);
            let named = |err: &dyn fmt::Display| format!("rule {}: {err}", rule.id);
            let expanded = if rule.pattern.contains("(?=") || rule.pattern.contains("(?!") {
                expand_credential_lookaheads(&rule.pattern).map_err(|err| named(&err))?
            } else {
                rule.pattern.clone()
            };
            let pattern = rewrite(&expanded, entropy.map(|_| rule.secret_group))
                .map_err(|err| named(&err))?;
            Ok(Rule {
                pattern,
                entropy,
                ..rule
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use regex::Regex;

    const MEMRIVER: &str = include_str!(
        "../../../../packages/memriver-core/src/memriver_core/content_policy/rules/memriver.toml"
    );
    const GITLEAKS: &str = include_str!(
        "../../../../packages/memriver-core/src/memriver_core/content_policy/rules/gitleaks.toml"
    );

    fn shipped() -> Vec<Rule> {
        load_rules(&[MEMRIVER, GITLEAKS]).unwrap()
    }

    /// The shipped rules with their patterns as the rule files write them.
    fn written() -> Vec<Rule> {
        read(&[MEMRIVER, GITLEAKS]).unwrap().0
    }

    /// The byte offsets of every `$` of `pattern` that is neither escaped nor inside a
    /// bracket expression.
    fn dollars(pattern: &str) -> Vec<usize> {
        let chars: Vec<(usize, char)> = pattern.char_indices().collect();
        let only: Vec<char> = chars.iter().map(|(_, c)| *c).collect();
        let (mut found, mut at) = (Vec::new(), 0);
        while let Some(&(offset, c)) = chars.get(at) {
            match c {
                '\\' => at += 2,
                '[' => at = after_class(&only, at + 1),
                '$' => {
                    found.push(offset);
                    at += 1;
                }
                _ => at += 1,
            }
        }
        found
    }

    fn one(pattern: &str) -> String {
        format!("[[rules]]\nid = \"r\"\nregex = '''{pattern}'''\n")
    }

    fn hits(pattern: &str, text: &str) -> bool {
        Regex::new(&rewrite(pattern, None).unwrap())
            .unwrap()
            .is_match(text)
    }

    #[test]
    fn loads_226_rules_memriver_first() {
        let rules = shipped();
        assert_eq!(rules.len(), 226);
        let first: Vec<&str> = rules[..5].iter().map(|rule| rule.id.as_str()).collect();
        assert_eq!(
            first,
            [
                "memriver-github-fine-grained-pat",
                "memriver-private-key-header",
                "memriver-slack-token",
                "memriver-openai-api-key",
                "memriver-credential-assignment"
            ]
        );
        assert!(
            rules[5..]
                .iter()
                .all(|rule| !rule.id.starts_with("memriver-"))
        );
        // only memriver's own five run on every text
        let without: Vec<&str> = rules
            .iter()
            .filter(|rule| rule.keywords.is_empty())
            .map(|rule| rule.id.as_str())
            .collect();
        assert_eq!(without, first);
        let mut ids: Vec<&str> = rules.iter().map(|rule| rule.id.as_str()).collect();
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), 226);
        // keywords are lower-cased, whatever the file writes
        assert!(
            rules
                .iter()
                .flat_map(|rule| &rule.keywords)
                .all(|keyword| *keyword == keyword.to_lowercase())
        );
        assert_eq!(
            rules
                .iter()
                .find(|rule| rule.id == "sonar-api-token")
                .unwrap()
                .secret_group,
            2
        );
    }

    #[test]
    fn all_226_rewrite_and_compile() {
        let rules = shipped();
        let failed: Vec<&str> = rules
            .iter()
            .filter(|rule| Regex::new(&rule.pattern).is_err())
            .map(|rule| rule.id.as_str())
            .collect();
        assert_eq!(failed, Vec::<&str>::new());
        // nothing of the three rewrites is left as written
        for rule in &rules {
            for left in ["\\w", "\\d", "(?="] {
                assert!(
                    !rule.pattern.replace("\\\\", "").contains(left),
                    "{}: {left}",
                    rule.id
                );
            }
            assert!(dollars(&rule.pattern).is_empty(), "{}", rule.id);
        }
    }

    #[test]
    fn all_155_dollars_of_the_shipped_rules_are_in_tail_position() {
        let (mut total, mut with_dollar, mut before_parentheses) = (0, 0, Vec::new());
        for rule in written() {
            let found = dollars(&rule.pattern);
            with_dollar += usize::from(!found.is_empty());
            for at in found {
                total += 1;
                assert!(dollar_is_in_tail_position(&rule.pattern, at), "{}", rule.id);
                // most are followed by closing parentheses and nothing else
                if !rule.pattern[at + 1..].chars().all(|c| c == ')') {
                    before_parentheses.push(rule.id.clone());
                }
            }
        }
        assert_eq!((total, with_dollar), (155, 154));
        // the others are the last item of an alternative
        before_parentheses.dedup();
        assert_eq!(
            before_parentheses,
            [
                "atlassian-api-token",
                "azure-ad-client-secret",
                "perplexity-api-key"
            ]
        );
        // an offset that is no `$` is no tail position
        assert!(!dollar_is_in_tail_position("ab", 1));
    }

    #[test]
    fn word_and_digit_are_ascii_inside_and_outside_brackets() {
        assert_eq!(
            rewrite(r"\w[\w-]\d[^\d]\W\D[x\W]", None).unwrap(),
            "[[:word:]][[:word:]-][[:digit:]][^[:digit:]][^[:word:]][^[:digit:]][x[^[:word:]]]"
        );
        // an escaped backslash before the letter is not the class
        assert_eq!(rewrite(r"\\w\\d", None).unwrap(), r"\\w\\d");
        for (pattern, text, hit) in [
            (r"^\w+$", "aZ09_", true),
            (r"^\w$", "\u{e9}", false),
            (r"^\w$", "\u{ff15}", false),
            (r"^\d+$", "0123456789", true),
            (r"^\d$", "\u{ff15}", false),
            (r"^[\w.]+$", "a.b", true),
            (r"^[\w.]$", "\u{e9}", false),
            (r"^[^\d]$", "\u{ff15}", true),
            (r"^\W$", "\u{e9}", true),
            (r"^\W$", "a", false),
            (r"^[x\W]$", "-", true),
            (r"^[x\W]$", "y", false),
        ] {
            assert_eq!(hits(pattern, text), hit, "{pattern} on {text:?}");
        }
    }

    #[test]
    fn whitespace_includes_u001c_to_u001f_inside_and_outside_brackets() {
        assert_eq!(
            rewrite(r"\s[\s:][^\s]\S", None).unwrap(),
            r"[\s\x1c-\x1f][\s\x1c-\x1f:][^\s\x1c-\x1f][^\s\x1c-\x1f]"
        );
        for space in [
            "\t", "\n", "\u{b}", "\u{c}", "\r", " ", "\u{1c}", "\u{1d}", "\u{1e}", "\u{1f}",
            "\u{85}", "\u{a0}", "\u{3000}",
        ] {
            assert!(hits(r"^k\sv$", &format!("k{space}v")), "{space:?}");
            assert!(hits(r"^k[\s:]v$", &format!("k{space}v")), "{space:?}");
            assert!(!hits(r"^k[^\s]v$", &format!("k{space}v")), "{space:?}");
            assert!(!hits(r"^k\Sv$", &format!("k{space}v")), "{space:?}");
        }
        for other in ["\0", "\u{8}", "\u{1b}", "\u{7f}", "x", "\u{200b}"] {
            assert!(!hits(r"^k\sv$", &format!("k{other}v")), "{other:?}");
            assert!(hits(r"^k\Sv$", &format!("k{other}v")), "{other:?}");
        }
    }

    #[test]
    fn negated_whitespace_as_a_nested_class_for_freemius_and_private_key() {
        assert_eq!(rewrite(r"[\S]{29}", None).unwrap(), r"[[^\s\x1c-\x1f]]{29}");
        assert_eq!(
            rewrite(r"[\s\S-]{64,}?", None).unwrap(),
            r"[\s\x1c-\x1f[^\s\x1c-\x1f]-]{64,}?"
        );
        assert!(hits(r"^[\S]{3}$", "a-9") && !hits(r"^[\S]{3}$", "a\u{1c}9"));
        assert!(hits(r"^[\s\S-]{3}$", "a\u{1c}-") && hits(r"^[^\S]$", "\u{1f}"));
        // the two shipped rules that write it this way
        let written = written();
        let shipped = shipped();
        for (id, as_written) in [
            ("freemius-secret-key", r"[\S]{29}"),
            ("private-key", r"[\s\S-]{64,}?"),
        ] {
            let at = written.iter().position(|rule| rule.id == id).unwrap();
            assert!(written[at].pattern.contains(as_written), "{id}");
            assert!(shipped[at].pattern.contains(r"[^\s\x1c-\x1f]"), "{id}");
            assert!(Regex::new(&shipped[at].pattern).is_ok(), "{id}");
        }
    }

    #[test]
    fn dollar_skips_escaped_and_bracketed() {
        for pattern in [
            r"a\$b",
            r"a[$]b",
            r"a[^$x]b",
            r"a[]$]b",
            r"a\\\$b",
            r"[[:alnum:]$]x",
        ] {
            assert_eq!(rewrite(pattern, None).unwrap(), pattern);
        }
        // an escaped backslash does not escape the `$` after it
        assert_eq!(rewrite(r"a\\$", None).unwrap(), r"a\\(?:\n?\z)");
        assert_eq!(rewrite("a$", None).unwrap(), r"a(?:\n?\z)");
        assert_eq!(rewrite("(?:a|b$)", None).unwrap(), r"(?:a|b(?:\n?\z))");
    }

    #[test]
    fn dollar_followed_by_a_word_boundary_is_a_loader_error() {
        for pattern in [
            r"A$\b",
            r"A$\B",
            r"(?:A$)\b",
            r"(?:A$|B)\b",
            r"A$\z",
            r"A$^",
        ] {
            assert_eq!(
                rewrite(pattern, None),
                Err(RewriteError::DollarNotInTailPosition),
                "{pattern}"
            );
        }
        assert_eq!(
            load_rules(&[&one(r"A$\b")]).unwrap_err(),
            "rule r: a $ is not at the end of the pattern"
        );
    }

    #[test]
    fn dollar_followed_by_any_token_is_a_loader_error() {
        for pattern in [
            "A$B",
            "A$.",
            "A$[x]",
            "A$(?:x)",
            "(A$)x",
            "A$$",
            "(?:A$|B)C",
            "((A$)|B)(C)",
            r"A$\n",
            "A$(?i)",
        ] {
            assert_eq!(
                rewrite(pattern, None),
                Err(RewriteError::DollarNotInTailPosition),
                "{pattern}"
            );
        }
    }

    #[test]
    fn dollar_as_last_item_of_an_alternative_is_accepted() {
        for pattern in [
            "A$",
            "(A$)",
            "((A$))",
            "(?:A|B$|C)",
            "(?:A$|B)",
            "(?:x(?:A$|B(?:c|d))|C)",
            "(?:A$)|B",
            "(?:A$)|(?:B|C$)",
            r#"(?:A$|[\x60'"\s;|)(])"#,
            r"(?:A$|\b)",
            r"(?:A$|x\)y)",
        ] {
            assert!(rewrite(pattern, None).is_ok(), "{pattern}");
        }
        // the three shipped shapes
        let written = written();
        for id in [
            "atlassian-api-token",
            "azure-ad-client-secret",
            "perplexity-api-key",
        ] {
            let rule = written.iter().find(|rule| rule.id == id).unwrap();
            assert!(
                !dollars(&rule.pattern).is_empty() && rewrite(&rule.pattern, None).is_ok(),
                "{id}"
            );
        }
    }

    #[test]
    fn dollar_inside_a_quantified_group_is_a_loader_error() {
        for pattern in [
            "(?:A$)+",
            "(A$)?",
            "(A$){2}",
            "(?:A$)*",
            "(?:(?:A$))+",
            "(?:A$|B)+",
            "(?:A$)+?",
        ] {
            assert_eq!(
                rewrite(pattern, None),
                Err(RewriteError::DollarNotInTailPosition),
                "{pattern}"
            );
        }
    }

    #[test]
    fn a_pattern_with_the_m_flag_is_a_loader_error() {
        for pattern in [
            "(?m)A$",
            "(?im)A",
            "(?m:A)",
            "A(?mi)B",
            "(?si-x:(?m)A)",
            "(?m-i)A",
        ] {
            assert_eq!(
                rewrite(pattern, None),
                Err(RewriteError::MultilineFlag),
                "{pattern}"
            );
        }
        // off, or no flag at all
        for pattern in [
            "(?i-m)A", "(?-m)A", "(?i)m", "(?:m)", "(?P<m>A)", "(?<m>A)", r"\(?m\)",
        ] {
            assert!(rewrite(pattern, None).is_ok(), "{pattern}");
        }
        assert!(
            written()
                .iter()
                .all(|rule| rewrite(&rule.pattern, None) != Err(RewriteError::MultilineFlag))
        );
    }

    #[test]
    fn dollar_inside_the_measured_group_is_a_loader_error() {
        let inside = Err(RewriteError::DollarInsideMeasuredGroup);
        // the declared group, else the first, else the whole match
        for (pattern, group) in [
            (r"k=(\w+$)", 0),
            (r"k=(\w+$)", 1),
            (r"(a)(b$)", 2),
            (r"(a$)|(b)", 2),
            (r"k=((?:a|b$))", 0),
            (r"k=(?P<v>\w+$)", 1),
            (r"k=\w+$", 0),
            (r"k=(?:\w+)$", 0),
            (r"(a)$", 2),
        ] {
            assert_eq!(
                rewrite(pattern, Some(group)),
                inside,
                "{pattern} group {group}"
            );
            // a rule whose entropy does not count measures nothing
            assert!(rewrite(pattern, None).is_ok(), "{pattern}");
        }
        for (pattern, group) in [
            (r"k=(\w+)$", 0),
            (r"k=(\w+)(?:;|$)", 1),
            (r"(a)(b)$", 2),
            (r"(a)(b$)", 1),
        ] {
            assert!(
                rewrite(pattern, Some(group)).is_ok(),
                "{pattern} group {group}"
            );
        }
        // the one shipped rule that is measured keeps its `$` outside its group
        let written = written();
        let generic = written
            .iter()
            .find(|rule| rule.id == "generic-api-key")
            .unwrap();
        assert_eq!(dollars(&generic.pattern).len(), 1);
        assert!(rewrite(&generic.pattern, Some(generic.secret_group)).is_ok());
    }

    #[test]
    fn credential_pattern_is_derived_and_fails_loudly_when_the_toml_shape_changes() {
        let written = written();
        let rule = written
            .iter()
            .find(|rule| rule.id == "memriver-credential-assignment")
            .unwrap();
        // the only shipped rule with a lookahead
        let with: Vec<&str> = written
            .iter()
            .filter(|rule| rule.pattern.contains("(?"))
            .filter(|rule| {
                rule.pattern.contains("(?=")
                    || rule.pattern.contains("(?!")
                    || rule.pattern.contains("(?<=")
            })
            .map(|rule| rule.id.as_str())
            .collect();
        assert_eq!(with, ["memriver-credential-assignment"]);
        let derived = expand_credential_lookaheads(&rule.pattern).unwrap();
        assert!(!derived.contains("(?=") && derived.starts_with("(?i)(api[ _-]?key|secret|token|"));
        let compiled = Regex::new(&rewrite(&derived, None).unwrap()).unwrap();
        for stored in [
            "the password is in 1Password",
            "token is rotated monthly",
            "password=",
            "password=***",
            "the password is confidential",
            "the passphrase is automatically",
            "see the API key rotation notes",
            "password is abcdefghijklmno",
            "password is abcdefghij1",
            "password is \"abc\"",
            "password is 'ab'",
        ] {
            assert!(!compiled.is_match(stored), "{stored}");
        }
        for refused in [
            "password = hunter2hunter2",
            "the password is correcthorsebattery1",
            "password is \"correct horse battery staple\"",
            "password is 'correct horse'",
            "API_KEY: abc123!xyz",
            "the passphrase is abcdefghijklmnop",
            "password is abcdefghijk1",
            "password is 1abcdefghijk",
            "password is abcdefghijklmno1",
            "token is \"abcde",
            // a quote and five more: as long as the unquoted floor of six
            "password is \"short\"",
            "SECRET_ACCESS_KEY=abcdef",
            "credentials:\u{1f}abcdef",
        ] {
            assert!(compiled.is_match(refused), "{refused}");
        }
        // any other shape is refused: the rule file stays the only source
        for changed in [
            rule.pattern.replace(r"\S{12}", r"\S{10}"),
            rule.pattern.replace(r"\S{6,})", r"\S{8,})"),
            rule.pattern.replace("(?i)(api", "(?i)(?=x)(api"),
            format!("{}x", rule.pattern),
            r"a(?=b)".to_string(),
            r"a(?!b)".to_string(),
        ] {
            let refused = expand_credential_lookaheads(&changed);
            assert_eq!(
                refused.unwrap_err(),
                "a pattern with lookaheads is not the credential rule's shape",
                "{changed}"
            );
            assert!(
                load_rules(&[&one(&changed)])
                    .unwrap_err()
                    .starts_with("rule r: ")
            );
        }
    }

    #[test]
    fn entropy_is_honoured_only_for_listed_rules() {
        let honoured: Vec<(&str, Option<f64>)> = Vec::new();
        let shipped = shipped();
        let mut measured = honoured;
        measured.extend(
            shipped
                .iter()
                .filter(|rule| rule.entropy.is_some())
                .map(|rule| (rule.id.as_str(), rule.entropy)),
        );
        assert_eq!(measured, [("generic-api-key", Some(3.5))]);
        // many more declare one in the file
        assert!(
            written()
                .iter()
                .filter(|rule| rule.entropy.is_some())
                .count()
                > 100
        );

        let rule = "[[rules]]\nid = \"synthetic-key\"\nregex = 'ZQ9\\w+'\nentropy = 3\n";
        let entropy = |sources: &[&str]| load_rules(sources).unwrap()[0].entropy;
        let listed = "[policy]\nhonor_entropy_only_for = [\"synthetic-key\"]\n";
        let other = "[policy]\nhonor_entropy_only_for = [\"other-rule\"]\n";
        assert_eq!(entropy(&[&format!("{listed}{rule}")]), Some(3.0));
        assert_eq!(entropy(&[&format!("{other}{rule}")]), None);
        // no table anywhere: every threshold counts
        assert_eq!(entropy(&[rule]), Some(3.0));
        // a table in any source counts for all, and the last one read decides
        assert_eq!(entropy(&[rule, other]), None);
        assert_eq!(entropy(&[other, rule, listed]), Some(3.0));
        assert_eq!(entropy(&[rule, "[policy]\n"]), None);
    }

    #[test]
    fn of_two_rules_with_one_id_the_first_counts_and_a_rule_without_a_pattern_is_left_out() {
        let first = "[[rules]]\nid = \"dup\"\nregex = \"aaa\"\nkeywords = [\"KeY\"]\n";
        let second = "[[rules]]\nid = \"dup\"\nregex = \"bbb\"\n\n[[rules]]\nid = \"path-only\"\npath = '\\.pem$'\n\n[[rules]]\nid = \"empty\"\nregex = \"\"\n\n[[rules]]\nid = \"good\"\nregex = \"ZQ9[0-9]{4}\"\nsecretGroup = 1\n";
        let rules = load_rules(&[first, second]).unwrap();
        let read: Vec<(&str, &str)> = rules
            .iter()
            .map(|rule| (rule.id.as_str(), rule.pattern.as_str()))
            .collect();
        assert_eq!(read, [("dup", "aaa"), ("good", "ZQ9[0-9]{4}")]);
        assert_eq!(
            (rules[0].keywords.as_slice(), rules[1].secret_group),
            (["key".to_string()].as_slice(), 1)
        );
        // a file without rules is no error; a file that is no TOML, or a rule without an id, is
        assert_eq!(load_rules(&["title = \"none\"\n"]).unwrap(), []);
        assert_eq!(
            load_rules(&["[[rules"]).unwrap_err(),
            "a rule file is not TOML"
        );
        assert_eq!(
            load_rules(&["[[rules]]\nregex = \"a\"\n"]).unwrap_err(),
            "a rule has no id"
        );
    }

    #[test]
    fn a_class_the_engines_read_differently_is_a_loader_error() {
        for pattern in [
            r"[[:blank:]]{3}",
            r"[x[:nonsense:]]",
            r"[[:alnum:]x[:punct:]]",
            r"\pL",
            r"\p{Greek}",
            r"[\PL]",
            "[a[b]c]",
            "[a&&b]",
            "[a--b]",
            "[a~~b]",
            "[[:alnum",
        ] {
            assert_eq!(
                rewrite(pattern, None),
                Err(RewriteError::UnknownClass),
                "{pattern}"
            );
        }
        for pattern in [
            "[[:alnum:]]",
            "[x[:digit:][:upper:]-]",
            "[a&b]",
            "[a-b-]",
            r"[\[x]",
            "a&&b",
            "a--b",
        ] {
            assert!(rewrite(pattern, None).is_ok(), "{pattern}");
        }
    }
}
