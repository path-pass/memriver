//! The port of an optional check of a write's new text, asked before the write
//! transaction. Core holds no implementation and no policy about who is asked.

/// What a classifier answers for one text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// The text may be stored.
    Allowed,
    /// The text may not be stored. `category` is a short ASCII label the refusal
    /// names, for example "instruction".
    Blocked { category: String },
    /// The classifier could not decide: the write is refused, never let through.
    /// `detail` is a fixed reason such as "timeout", never words from the checked text
    /// or from a model's answer.
    Unavailable { detail: String },
}

pub trait ContentClassifier {
    /// `changed_by` names the caller, so an implementation can apply its own switches
    /// per source.
    fn classify(&self, text: &str, changed_by: &str) -> Verdict;
}
