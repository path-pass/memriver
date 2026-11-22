//! What may be stored: the secret scanner over the rule files, and the port of an
//! optional classifier.

pub mod classifier;
pub mod rule_cache;
pub mod rules_loader;
pub mod secret_scanner;

pub use classifier::{ContentClassifier, Verdict};
pub use rule_cache::CachePolicy;
pub use secret_scanner::{Policy, Rejection};
