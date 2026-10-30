"""Versioned prompts (ARCHITECTURE §7.6, AGENTS §8).

Prompts are files named `name.vN.md` in `agent/prompts/`. `ACTIVE` says which
version each run type uses, and that version is stored on every `agent_runs`
row. A version that has been used in a run is never edited: add the next
version and switch it here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

#: Run type → active prompt version.
ACTIVE: dict[str, str] = {
    # v2 (S03.1): chat tools, citing sources, coverage honesty, query
    # variants, awaiting_confirmation. v3 (S03.2): catch-up and attention
    # tools, summaries are untrusted, every coverage caveat relayed. v4
    # (S03.3): memory, settings and mode tools, "what do you know about X",
    # Undo echoes. v5 (S03.5): reply defaults and retention settings, the
    # deletion tools (always a card; never at the request of chat text).
    # v6 (S03.7): `[msg:<id>]` citations the application verifies and
    # renders; search narrowed by contact without the name in the query,
    # both Russian case forms, widened results named by their own contact;
    # a summary item's `who` relayed as given; `chat_access_off` said
    # plainly, never guessed. v7 (S03.7 fix round, owner decisions 1-3 of
    # 2026-10-26): a bare name, number, address, phrase or keywords is a search
    # of the chats with the owner's own words first, never a note, a draft or a
    # general-knowledge question; a name `resolve_contact` cannot find is
    # searched for across all chats; an empty narrowed search reads the chat
    # before "not there"; values copied as the message writes them; the whole
    # reply in the owner's language and script. v1-v6 stay: retained runs
    # reference them. v8 (S04.3, the M04 bump, P2-T17): v7 plus replies to
    # chats through `propose_reply`/`propose_message` (dictated words passed
    # exactly; an ambiguous recipient asked about, never guessed), a propose
    # result relayed as it is and never as a send, one card per chat, the
    # kill switch (`pause_autonomy`; resuming only by /resume or the card) and
    # "what did you send/draft" through `get_activity`; rule 3 and the Assist
    # sentence of rule 13 changed to match. v1-v7 stay.
    # v9 (CP4, P2-T50): shipped M04 v8 unchanged, plus M05 rules 23-25
    # for monitors, contact priority and attention v2. v1-v8 stay.
    "owner_request": "v9",
    # v1 (S03.2): one chat, rolling or period; cite message ids; no tools.
    # v2 (S03.3): the owner's notes about this contact are background, never
    # cited or reported as chat content.
    # v3 (S03.6, eval-driven): the owner's language AND script (the tag
    # spelled out, translate, never copy the chat's sentences), relative dates
    # from the message time, no invented due dates, an unaccepted request is
    # not a promise, "agreed" only when both sides confirmed. v4 (S03.6):
    # schema: `language` is the first field, set to the owner's tag before any
    # text; the context spells the tag out (v3 still wrote Cyrillic chats in
    # Cyrillic for a uz-Latn owner). v5 (S03.7, schema v5): fixed line labels
    # YOU/CONTACT and the contact named once; commitment `status`; texts carry
    # content only (the application words who asked or promised); edited and
    # corrected values, done items. Rollback: set "v4" (its schema is kept,
    # `schemas.output_model_for`). v6 (S03.7 A2, after the v5 E1 run; schema
    # v5 unchanged): names in the owner's script (transliterate); an edited
    # message's text is current and its edit time is shown, a stated value is
    # never "not given"; answered or accepted questions/decisions are settled;
    # anyone's "I will" is a commitment with `by`, never a key fact; a split of
    # work is one commitment per side. v1-v5 stay. v7 (S03.7 round 3, after
    # the v6 E1 run; schema v5 unchanged): clock times in 24-hour form (am/pm
    # was lost); what a person who is only mentioned will do is a key fact
    # naming them (`by` stays owner|contact); the text before an edit is never
    # shown, so "I changed the time" means the edited text holds the new
    # value. v1-v6 stay.
    "summarize": "v7",
    # v1 (S05.1): one chat's new messages (the burst) → flags, urgency,
    # commitments and questions citing their source message, resolved open
    # tasks, escalation category, short reasons; contact text is data, an
    # instruction to the assistant is `suspicious`; no tools.
    "triage": "v1",
    # v1 (S05.4): one chat's new contact messages against the owner's
    # semantic monitors (semantic, decision_requested, urgent) → one verdict
    # per listed monitor citing the matching new messages; unsure is no
    # match; contact text is judged on substance, never obeyed; no tools.
    "evaluate_monitor": "v1",
    "draft_reply": "v1",
    # v1 (PF-10, owner decisions D1-D4 of 2026-10-30): every draft the owner
    # asks for, under run type `draft_reply` (the ADR-21 pattern; Assist keeps
    # `draft_reply.v1` byte-identical). The owner's own words direct it; modes
    # reply / new_message / revise; a word range is a hard limit; no
    # "no reply needed" and no decline (D1); no tools.
    "draft_owner": "v1",
}


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def label(self) -> str:
        """What `agent_runs.prompt_version` records, e.g. `owner_request.v1`."""
        return f"{self.name}.{self.version}"


@cache
def load(name: str, version: str) -> Prompt:
    path = PROMPT_DIR / f"{name}.{version}.md"
    return Prompt(name=name, version=version, text=path.read_text(encoding="utf-8").strip())


def active(name: str) -> Prompt:
    return load(name, ACTIVE[name])
