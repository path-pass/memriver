# memriver-dream

The harness-neutral maintenance run behind `memriver dream`: a content-policy scan
of every stored memory version, session summaries, the project and global layers
of consolidation, and TTL retirement after a model review. It changes memories
only through `memriver-core`'s `MemoryService.apply`, keeps its own records in
`<root>/dream/dream.db` and its reports in `<root>/dream/reports/`, and talks to a
model only through an `Executor` the `memriver` package supplies; not a
standalone tool.
