Versioned prompt templates, named `name.vN.md` (AGENTS §8).

`manifest.json` inventories every label, its SHA-256 over the exact file bytes,
active status, structured-output schema family (empty for the tool-calling owner
run), historical schema notes, and repository-relative links to existing JSON
eval records. A link records provenance, not a passing gate: trials, aborted runs
and failed evaluations are included. An empty list means no linked record exists.
Historical schema notes matter: a current class name does not recreate an older
schema revision. Archiving preserves prompt assets; it does not implement replay.

Active files live here; inactive files live in `archive/`. Use
`prompt_registry.load(name, version)` for model-facing text, which keeps the
existing UTF-8 decoding and whitespace stripping, or `load_bytes(name, version)`
for the exact bytes. Both locations resolve through the manifest and hashes are
checked before decoding. Stored `agent_runs.prompt_version` labels never change.

A version that has been used is immutable. To change a prompt, allocate the next
unused version, add its file and manifest entry, and run the affected eval gate
with the owner's cost approval. When activating it, update `ACTIVE` in
`prompt_registry.py`, set the two manifest active flags, and move the previous
active file to `archive/` without rewriting it. Never regenerate an old hash to
accept changed text. The unit tests check the full inventory, hashes, registry,
schema pairing and static references in code/tests/evals. Keep historical entries
and files: static checks cannot establish that an operator database has no retained
references. No prompt was changed or activated by the initial I31 archival.

For an operator to report references on a chosen database, run this read-only SQL
in an authorized `psql` session, setting `account_id` to the account being audited:

```sql
\set account_id 1
SELECT prompt_version, count(*) AS retained_runs
FROM app.agent_runs
WHERE account_id = :'account_id'::bigint
  AND prompt_version IS NOT NULL
GROUP BY prompt_version
ORDER BY prompt_version;
```

It returns only version labels and counts, never run inputs, outputs, or message
content. Repeat for each authorized account before considering any removal;
absence from one account or the static reference scan is not permission to delete
an asset. No operator database is queried by the tests.
