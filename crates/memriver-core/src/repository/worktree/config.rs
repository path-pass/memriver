//! A git configuration file, read far enough to see that it holds nothing unusual.
//!
//! Not git's parser: whatever this one is not sure about -- a value continued on the next
//! line, a variable written on the line of its section header, an escape it does not know
//! -- makes the whole file unparsed, and the caller asks git instead.

/// One variable, as written under a section header. Names are in lower case; a variable
/// without `=` has no value, which git reads as true.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Variable {
    pub section: String,
    pub subsection: Option<String>,
    pub name: String,
    pub value: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GitConfig {
    /// Every section header's name in lower case, an empty section included.
    pub sections: Vec<String>,
    pub variables: Vec<Variable>,
}

impl GitConfig {
    pub fn has_section(&self, section: &str) -> bool {
        self.sections.iter().any(|name| name == section)
    }

    /// Every value given to `section.name` (no subsection), in file order.
    pub fn values<'a>(
        &'a self,
        section: &'a str,
        name: &'a str,
    ) -> impl Iterator<Item = Option<&'a str>> {
        self.variables
            .iter()
            .filter(move |variable| {
                variable.section == section
                    && variable.subsection.is_none()
                    && variable.name == name
            })
            .map(|variable| variable.value.as_deref())
    }
}

/// A value as git reads a boolean: no value is true, an empty one false; `true`, `yes`,
/// `on` and `false`, `no`, `off` in any case; a whole number, true unless it is zero.
/// None for anything else.
pub fn boolean(value: Option<&str>) -> Option<bool> {
    let Some(value) = value else {
        return Some(true);
    };
    match value.to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" => Some(true),
        "false" | "no" | "off" | "" => Some(false),
        number => number.parse::<i32>().ok().map(|number| number != 0),
    }
}

fn is_blank(c: char) -> bool {
    c == ' ' || c == '\t'
}

/// What follows something complete on a line: nothing, or a comment.
fn ends(rest: &str) -> bool {
    let rest = rest.trim_start_matches(is_blank);
    rest.is_empty() || rest.starts_with(['#', ';'])
}

/// `[name]` or `[name "subsection"]`, alone on its line: the section and its subsection.
fn header(line: &str) -> Option<(String, Option<String>)> {
    let inner = line.strip_prefix('[')?;
    let end = inner.find(|c: char| !(c.is_ascii_alphanumeric() || c == '-' || c == '.'))?;
    let (name, rest) = inner.split_at(end);
    if name.is_empty() {
        return None;
    }
    if let Some(rest) = rest.strip_prefix(']') {
        // the older spelling `[section.subsection]`, in which case does not count
        let name = name.to_ascii_lowercase();
        return ends(rest).then(|| match name.split_once('.') {
            Some((section, subsection)) => (section.to_string(), Some(subsection.to_string())),
            None => (name, None),
        });
    }
    let mut rest = rest.trim_start_matches(is_blank).strip_prefix('"')?.chars();
    let mut subsection = String::new();
    loop {
        match rest.next()? {
            '"' => break,
            '\\' => subsection.push(rest.next()?),
            c => subsection.push(c),
        }
    }
    let rest = rest.as_str().strip_prefix(']')?;
    (!name.contains('.') && ends(rest)).then(|| (name.to_ascii_lowercase(), Some(subsection)))
}

/// The text after `=`: blanks at both ends dropped, quotes removed, the five escapes read.
fn value(rest: &str) -> Option<String> {
    let mut value = String::new();
    let mut pending = 0;
    let mut quoted = false;
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        if !quoted {
            if is_blank(c) {
                pending += usize::from(!value.is_empty());
                continue;
            }
            if c == '#' || c == ';' {
                break;
            }
        }
        value.extend(std::iter::repeat_n(' ', std::mem::take(&mut pending)));
        match c {
            '"' => quoted = !quoted,
            // a backslash at the end of the line continues the value on the next one:
            // not read here
            '\\' => value.push(match chars.next()? {
                'n' => '\n',
                't' => '\t',
                'b' => '\u{8}',
                c @ ('"' | '\\') => c,
                _ => return None,
            }),
            c if c.is_control() => return None,
            c => value.push(c),
        }
    }
    (!quoted).then_some(value)
}

/// The file's sections and variables; None for a file this parser does not fully read.
pub fn parse(text: &str) -> Option<GitConfig> {
    let mut config = GitConfig::default();
    let mut current: Option<(String, Option<String>)> = None;
    for line in text.split('\n') {
        let line = line.strip_suffix('\r').unwrap_or(line);
        let line = line.trim_start_matches(is_blank);
        if ends(line) {
            continue;
        }
        if line.starts_with('[') {
            let (section, subsection) = header(line)?;
            config.sections.push(section.clone());
            current = Some((section, subsection));
            continue;
        }
        let (section, subsection) = current.clone()?;
        if !line.starts_with(|c: char| c.is_ascii_alphabetic()) {
            return None;
        }
        let end = line
            .find(|c: char| !(c.is_ascii_alphanumeric() || c == '-'))
            .unwrap_or(line.len());
        let (name, rest) = line.split_at(end);
        let rest = rest.trim_start_matches(is_blank);
        // after the name: the end of the line or `=`; git takes nothing else, a comment
        // included
        let value = match rest.strip_prefix('=') {
            Some(rest) => Some(value(rest)?),
            None if rest.is_empty() => None,
            None => return None,
        };
        config.variables.push(Variable {
            section,
            subsection,
            name: name.to_ascii_lowercase(),
            value,
        });
    }
    Some(config)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn values(text: &str, section: &str, name: &str) -> Vec<Option<String>> {
        parse(text)
            .unwrap()
            .values(section, name)
            .map(|value| value.map(str::to_string))
            .collect()
    }

    fn one(text: &str) -> Option<String> {
        let mut found = values(text, "core", "x");
        assert_eq!(found.len(), 1, "{text:?}");
        found.remove(0)
    }

    #[test]
    fn section_and_key_names_are_read_in_any_case() {
        let text = "[CoRe]\n\tBARE = true\n[core]\nbare=false\n";
        assert_eq!(
            values(text, "core", "bare"),
            [Some("true".to_string()), Some("false".to_string())]
        );
        let config = parse(text).unwrap();
        assert_eq!(config.sections, ["core", "core"]);
        assert!(config.has_section("core") && !config.has_section("CoRe"));
    }

    #[test]
    fn key_and_value_with_and_without_spaces() {
        for text in [
            "[core]\nx=v\n",
            "[core]\nx = v\n",
            "[core]\n  x\t=\t v  \n",
            "[core]\r\nx = v\r\n",
            "[core]\nx = v",
            "[core] # the core\nx = v ; why\n",
            "\n\n; a comment\n# another\n[core]\n\n  x = v\n",
        ] {
            assert_eq!(one(text), Some("v".to_string()), "{text:?}");
        }
        assert_eq!(one("[core]\nx = a  b \n"), Some("a  b".to_string()));
        assert_eq!(one("[core]\nx =\n"), Some(String::new()));
        assert_eq!(one("[core]\nx = # nothing\n"), Some(String::new()));
        assert_eq!(
            one("[core]\nx = /a path/with spaces\n"),
            Some("/a path/with spaces".to_string())
        );
    }

    #[test]
    fn a_bare_variable_has_no_value() {
        assert_eq!(one("[core]\nx\n"), None);
        assert_eq!(one("[core]\nx"), None);
        assert_eq!(one("[core]\nx \t \r\n"), None);
        // after the name git takes the end of the line or `=`, and nothing else
        for text in [
            "[core]\nx   ; said so\n",
            "[core]\nx # why\n",
            "[core]\nx;\n",
        ] {
            assert_eq!(parse(text), None, "{text:?}");
        }
        assert_eq!(boolean(one("[core]\nx\n").as_deref()), Some(true));
    }

    #[test]
    fn quoted_values_keep_what_they_hold() {
        assert_eq!(one("[core]\nx = \" a b \"\n"), Some(" a b ".to_string()));
        assert_eq!(
            one("[core]\nx = \"a # b\" ; c\n"),
            Some("a # b".to_string())
        );
        assert_eq!(one("[core]\nx = a\" b\"c\n"), Some("a bc".to_string()));
        assert_eq!(one("[core]\nx = a \"b\"\n"), Some("a b".to_string()));
        assert_eq!(
            one("[core]\nx = \"q\\\"s\\\\\" \\t\\n\\b\n"),
            Some("q\"s\\ \t\n\u{8}".to_string())
        );
    }

    #[test]
    fn subsections_are_kept_apart_from_the_section() {
        let text = "[remote \"Origin \\\"x\\\"\"]\nurl = u\n[branch.Main]\nmerge = m\n[core \"sub\"]\nbare = true\n";
        let config = parse(text).unwrap();
        assert_eq!(config.sections, ["remote", "branch", "core"]);
        assert_eq!(
            config.variables[0].subsection.as_deref(),
            Some("Origin \"x\"")
        );
        assert_eq!(config.variables[1].subsection.as_deref(), Some("main"));
        // `core.sub.bare` is not `core.bare`
        assert_eq!(config.values("core", "bare").count(), 0);
    }

    #[test]
    fn an_empty_section_is_still_a_section() {
        let config = parse("[core]\nx = 1\n[extensions]\n").unwrap();
        assert!(config.has_section("extensions"));
        assert!(parse("").unwrap().sections.is_empty());
    }

    #[test]
    fn the_boolean_spellings_git_accepts() {
        for spelling in ["true", "TRUE", "yes", "Yes", "on", "ON", "1", "2", "-1"] {
            assert_eq!(boolean(Some(spelling)), Some(true), "{spelling}");
        }
        for spelling in ["false", "False", "no", "NO", "off", "Off", "0", ""] {
            assert_eq!(boolean(Some(spelling)), Some(false), "{spelling}");
        }
        assert_eq!(boolean(None), Some(true));
        for spelling in ["maybe", "tru", "1x", " true", "y"] {
            assert_eq!(boolean(Some(spelling)), None, "{spelling}");
        }
    }

    #[test]
    fn what_the_parser_is_not_sure_about_is_not_parsed() {
        for text in [
            // a value continued on the next line
            "[core]\nx = a\\\n  b\n",
            "[core]\nx = \"a\\\nb\"\n",
            // a variable on the line of its header
            "[core] x = v\n",
            // quotes that do not close, an escape git does not know
            "[core]\nx = \"open\n",
            "[core]\nx = a\\qb\n",
            // a variable before any section, names that are not names
            "x = v\n[core]\n",
            "[core]\n1x = v\n",
            "[core]\nx y = v\n",
            "[core]\nx.y = v\n",
            "[]\nx = v\n",
            "[core\nx = v\n",
            "[co re]\nx = v\n",
            "[core \"sub]\nx = v\n",
            "[core \"sub\"\nx = v\n",
            "[core.x \"sub\"]\nx = v\n",
            "[core] trailing\n",
            // a byte order mark, a control character
            "\u{feff}[core]\nx = v\n",
            "[core]\nx = a\u{1}b\n",
            "[core]\nx = a\rb\n",
        ] {
            assert_eq!(parse(text), None, "{text:?}");
        }
    }
}
