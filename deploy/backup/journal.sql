-- Only UUIDs, integer IDs, enums, timestamps and integrity fields leave here.
-- One repeatable-read snapshot binds every head to exactly its record suffix.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object(
    'version', 1, 'checked_at', CURRENT_TIMESTAMP,
    'heads', COALESCE((SELECT json_agg(h ORDER BY h.account_recovery_id)
        FROM app.privacy_journal_heads h), '[]'::json),
    'records', COALESCE((SELECT json_agg(r ORDER BY r.account_recovery_id, r.sequence)
        FROM (SELECT 1 AS version, sequence, decision_id, account_recovery_id,
            scope, subject_recovery_id, operation, processor_id, decided_at,
            previous_digest, digest FROM app.privacy_journal) r), '[]'::json)
);
COMMIT;
