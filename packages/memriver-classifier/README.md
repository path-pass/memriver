# memriver-classifier

An optional check of new memory text before memriver writes it: a model decides whether
storing the text would plant instructions in a future agent's context. Install it with
`memriver[classifier]` and configure it with a `[classifier]` table in memriver's
`settings.toml`; without both, memriver calls no classifier at all. Backends: a clean
headless `claude` or `codex` run, or TypeSafe's hosted `jev` model. See the memriver
README, section "Content classifier".
