Versioned prompt templates, named `name.vN.md` (AGENTS §8).

A version that has been used in a run is never edited: add `name.v(N+1).md` and
switch the active version in the registry. Old files stay while retained
`agent_runs` rows reference them.
