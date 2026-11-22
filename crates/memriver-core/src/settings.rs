//! Every default and limit of this crate, and how settings are loaded.

use std::collections::HashMap;

/// A process environment, handed down from the binary's entry point.
pub type Env = HashMap<String, String>;
