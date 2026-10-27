"""The phase has one switch choosing who requests proactive drafts."""

from itertools import product

from assistant.actions.drafts import assist_trigger


def test_the_assist_trigger_switch_table() -> None:
    for assist, triage, trigger in product((False, True), repeat=3):
        flags = frozenset(
            name
            for name, enabled in (
                ("assist.drafts", assist),
                ("triage.run", triage),
                ("triage.assist_trigger", trigger),
            )
            if enabled
        )
        expected = "off" if not assist else "triage" if triage and trigger else "ingestion"
        assert assist_trigger(flags) == expected
