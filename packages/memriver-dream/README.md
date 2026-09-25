# memriver-dream

The harness-neutral maintenance run behind `memriver dream`: a safety re-scan
of stored memories, session summaries, memory consolidation and extraction to
global, and TTL retirement after a model review. It talks to the store only
through `memriver-core`'s `MaintenanceService` and to a model only through an
`Executor` the `memriver` package supplies; not a standalone tool.
