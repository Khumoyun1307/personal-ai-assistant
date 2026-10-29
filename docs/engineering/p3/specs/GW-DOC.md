# GW-DOC — `send_document`, control chat only

| | |
|---|---|
| **Unit** | GW-DOC (S07.4 delivery seam for `/export`) |
| **Team / slot** | Josh / J2 |
| **Size** | S (≈ 0.5 eng-day) |
| **Depends on** | F0-S (CP1). Builds on `main`'s gateway as fixed by P2 #133 (escape-once render path and the multi-chunk `partial_delivery` outcome); this unit does not touch that path |
| **Branch → base** | `p3/gw-doc` → `int/p3` |
| **Peer reviewer** | E2 (Codex), dispatched by the TL |
| **QA slot and lane** | QJ1 or QJ2, before merge |
| **Tier** | 1 (TL scope check). M7-EXP depends on it |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §4.6, §15; ARCHITECTURE §5.2, §6.8; AGENTS §5, §7, §9; TF §4.2 (allow-list: `sendDocument`, control chat only), §5 (captions ≤ 1024), §6 (uploads ≤ 50 MB, Verified); decisions OD-20, P3-T45 |

## 1. Goal

The gateway can send a file to the owner's control chat, and only there: one throttled, classified `sendDocument` per call, refused locally (no Telegram call) when the file or caption breaks a Telegram limit, with the filename and caption never logged and an uncertain outcome reported as uncertain, never as sent. `ControlChannel` gains the same method so the domain (`privacy/export.py`, M7-EXP) can use it without importing Telegram code.

## 2. Scope

**In:**
1. `telegram/gateway.py`: `TelegramSender.send_document`, `AiogramSender.send_document`, `OutboundGateway.send_document`, the local limits and their error codes.
2. `core/channel.py`: `ControlChannel.send_document` (named touch point).
3. `telegram/control/agent_channel.py`: `GatewayControlChannel.send_document` (the port's only implementation; contract gap G11), and, **only if** M6-PAUSE finds that `Delivery.error` does not carry the forbidden code, the one-line change in `GatewayControlChannel.send` that does (R100; §3.4).
4. Fakes: `tests/fakes/sender.py` (`FakeTelegramSender.send_document`), `tests/fakes/channel.py` (`FakeChannel.send_document`).
5. Confinement: an AST test and a `pyproject.toml` banned-API entry for file uploads.
6. RG-20 extension (a document's outcome is truthful), ARCHITECTURE §6.8.

**Out:**
- `/export`, the parts and their content (M7-EXP).
- A business-chat document (none exists, none is allowed; AGENTS §7, TF §4.2), editing or deleting a document (`deleteMessage` is not on the control allow-list; OD-20 has no delete button), thumbnails, captions with markup.
- The GW-ESC verification (P2 #133 landed on `main`; F0-C/QA confirm it).

## 3. Design

### 3.1 Limits and codes (`telegram/gateway.py`)

```python
#: TF §6 (Verified): uploads are limited to 50 MB. The decimal reading is the stricter one.
MAX_DOCUMENT_BYTES = 50 * 1000 * 1000
#: TF §5: a caption is at most 1024 characters (counted as UTF-16 code units, as Telegram counts).
MAX_CAPTION_CHARS = 1024
#: Generated filenames only (`[A-Za-z0-9._-]`, 1-64 chars): a name can never carry content.
_DOCUMENT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: An upload may take longer than a text send; it stays below the `llm` queue's
#: 45 s shutdown grace (jobs/app.py), so an upload in flight at SIGTERM ends
#: before the worker stops, as REQUEST_TIMEOUT_SECONDS does for the outbound queue.
DOCUMENT_REQUEST_TIMEOUT_SECONDS = 40
#: `error_code` of a document refused before Telegram was asked.
DOCUMENT_TOO_LARGE = "document_too_large"
DOCUMENT_EMPTY = "document_empty"
CAPTION_TOO_LONG = "caption_too_long"
FILENAME_INVALID = "filename_invalid"
```

### 3.2 Sender (`TelegramSender`, `AiogramSender`)

```python
# TelegramSender (Protocol) gains:
async def send_document(
    self, *, chat_id: int, filename: str, data: bytes, caption: str | None = None
) -> dict[str, Any]: ...
```

`AiogramSender.send_document` calls `self._bot.send_document(chat_id=chat_id, document=BufferedInputFile(data, filename=filename), caption=caption, parse_mode=None, request_timeout=DOCUMENT_REQUEST_TIMEOUT_SECONDS)` and returns `{"message_id": message.message_id}`. No `business_connection_id` argument exists on this method at all. `parse_mode=None`: the caption is plain text, so no escaping question arises (escape-once applies only to HTML renderers). The engineer checks the exact aiogram 3 signature of `Bot.send_document` and `BufferedInputFile` in the installed version before writing it (AGENTS §8 spirit: verify, do not guess).

### 3.3 Gateway (`OutboundGateway.send_document`)

```python
async def send_document(
    self, target: ControlChat, *, filename: str, data: bytes, caption: str | None = None
) -> SendResult: ...
```

1. `not isinstance(target, ControlChat)` → raise `BusinessSendNotAvailableError` (as `send_text` does for a business target). The type already says `ControlChat`; the runtime check guards untyped callers.
2. Local refusals, in this order, each returning `SendResult(ErrorClass.BAD_REQUEST, error_code=<code>)` without a Telegram call and logging `control_document_refused` with `account_id` and `error_code` only:
   - `len(data) == 0` → `DOCUMENT_EMPTY`;
   - `len(data) > MAX_DOCUMENT_BYTES` → `DOCUMENT_TOO_LARGE`;
   - `caption is not None and utf16_units(caption) > MAX_CAPTION_CHARS` → `CAPTION_TOO_LONG` (a local helper `len(caption.encode("utf-16-le")) // 2`);
   - `not _DOCUMENT_FILENAME.fullmatch(filename)` → `FILENAME_INVALID`.
3. `await self._throttle(target.chat_id)` (the same per-chat and global spacing as text sends).
4. `self._sender.send_document(chat_id=target.chat_id, filename=filename, data=data, caption=caption)`; any exception → `self._classify_failure(error, account_id=target.account_id)` (unchanged classifier: a 4xx Telegram answered is definite, including 403 `forbidden` and 413 `bad_request`; a timeout, reset or 5xx is `transient` and therefore `delivery_uncertain`).
5. Success → log `control_document_sent` with `account_id`, `message_id` and `size_bytes`; return `SendResult(ErrorClass.OK, message_id=…)`.

Never logged: the filename, the caption, the bytes, any URL. Never retried here: a caller that gets an uncertain result must not resend (invariant 6; M7-EXP reports it). A `rate_limited` result keeps its `retry_after` on the `SendResult`; the gateway does not wait and resend (the caller decides; M7-EXP stops the export). A `forbidden` result is returned to the caller like any other; the automatic pause on a control-chat `forbidden` (ARCHITECTURE §6.8) is not in the gateway (no session): M6-PAUSE owns `actions/autonomy.py::pause_after_control_forbidden`, and the control-send call sites call it after commit (notification delivery, M7-EXP; R46).

What the gateway cannot check: that `chat_id` is the owner's control chat. `ControlChat` only guarantees that no `business_connection_id` is attached; the caller must pass `account.control_chat_id` (M7-EXP reads it under the account lock and also checks it equals the owner's Telegram user id, its private chat with the bot).

The return type is `SendResult`, like every gateway method; P3-interfaces §4.6 writes `Delivery` (contract gap G11). The channel adapter converts.

### 3.4 Port and adapter (`core/channel.py`, `telegram/control/agent_channel.py`)

```python
# core/channel.py — ControlChannel gains
async def send_document(
    self, *, account_id: int, chat_id: int, filename: str, data: bytes, caption: str | None = None
) -> Delivery: ...
```

`GatewayControlChannel.send_document` calls the gateway with `ControlChat(account_id, chat_id)` and returns `Delivery(ok=result.ok, message_id=result.message_id, error=None if result.ok else result.error_class.value, uncertain=result.delivery_uncertain)`, the same mapping as its `send`.

**The forbidden code in `Delivery.error` (R100).** M6-PAUSE's `pause_after_control_forbidden` call sites (notification delivery, M7-EXP) recognise a blocked bot by `Delivery.error == "forbidden"` (`core/delivery.py::ErrorClass.FORBIDDEN.value`). At planning `GatewayControlChannel.send` already maps `error=result.error_class.value`, so a 403 gives `"forbidden"` and nothing needs to change. M6-PAUSE checks this first (its §3.9). **If** `Delivery.error` lacks the forbidden code on `int/p3`, this unit adds the one-line change that makes `send` and `send_document` set `error` to the error class value (never the Bot API description text, which can name the chat), and Josh lead sequences this PR before M6-PAUSE's notification call site. Either way the unit adds the pinning test named in §5 (it is cheap and fixes the contract M6-PAUSE relies on).

Adding a method to the Protocol requires every implementation to have it; the only ones are `GatewayControlChannel` and `FakeChannel` (checked on `main` and `int/m05`).

### 3.5 Fakes

- `FakeTelegramSender.send_document` awaits `on_call`, appends `{"chat_id", "filename", "size": len(data), "caption"}` to a new `documents` list (never the bytes, so a test cannot accidentally print content), raises `fail_with` when set, else returns a fresh `message_id`.
- `FakeChannel.send_document` first awaits an optional `on_document: Callable[[], Awaitable[None]] | None` hook (so M7-EXP's tests can change the database "while a part is being sent"), then appends to `documents` (`account_id`, `chat_id`, `filename`, `size`, `caption`) and `events` ("document"); it honours `deliver` and `uncertain` like `send`, plus a `document_script: list[Delivery]` consumed in order so M7-EXP can script a failed or uncertain part among good ones. It also keeps every document's bytes in `document_bytes: list[bytes]`, parallel to `documents` (tests use synthetic data only), so a test can parse each part's JSON.

### 3.6 Confinement

- `tests/unit/test_document_send_confinement.py` (the `test_business_send_confinement.py` pattern; AST over every module under `src/`):
  - `test_only_the_gateway_uploads_files`: the names `BufferedInputFile`, `FSInputFile`, `URLInputFile` and the module `aiogram.types.input_file` are imported or referenced in `src/` only in `telegram/gateway.py`. This is the guard for the modules ruff exempts from TID251 (`telegram/ingress/*`, `telegram/connections.py`, `bootstrap.py`).
  - `test_send_document_is_called_only_by_its_allowed_callers`: calls `.send_document(` appear only in `telegram/gateway.py` (the sender call), `telegram/control/agent_channel.py` (the gateway call) and `privacy/export.py` (M7-EXP's port call; allowed before the file exists). The test also asserts that the gateway and adapter calls are found, so it is never vacuous.
- `pyproject.toml`: add banned-API entries `aiogram.types.BufferedInputFile`, `aiogram.types.FSInputFile`, `aiogram.types.URLInputFile` and `aiogram.types.input_file` with the message "Only telegram/gateway.py uploads files (AGENTS §5)." `gateway.py` already has the `TID251` per-file ignore, so no new ignore is needed. Tests are not exempt from TID251: the one unit test that checks the upload type imports `BufferedInputFile` with a line-level `# noqa: TID251` (no `pyproject.toml` ignore for tests). Contract gap G11: P3-interfaces §4.6 and §15.1 ask for "the TID251 per-file ignore entry"; these banned-API entries are what confines uploads.

### 3.7 Edge cases (each has a test in §5)

0 bytes; exactly 50,000,000 bytes (accepted) and one byte more (refused); a 1024-unit caption (accepted) and 1025 (refused), including a caption with characters outside the Basic Multilingual Plane; a filename with a space, a slash, a non-ASCII letter or 65 characters; a business target passed by an untyped caller; 403, 413, 429 with `retry_after`, a 5xx and a timeout from the sender; two documents to one chat within a second (throttled).

## 4. Files

**Owned (P3-interfaces §15):**
- `telegram/gateway.py`: the `send_document` methods and constants only (GW-ESC's sanitizer path is not touched).
- Named touch points: `core/channel.py` (`ControlChannel.send_document`), `telegram/control/agent_channel.py` (`GatewayControlChannel.send_document`; and the one-line `error` mapping in `send` only when R100's check requires it), `tests/fakes/sender.py`, `tests/fakes/channel.py`, `pyproject.toml` (the banned-API entries only).
- Test files: `tests/unit/test_gateway_documents.py`, `tests/unit/test_document_send_confinement.py`, `tests/unit/test_log_hygiene_gw_doc.py`; `tests/unit/test_agent_channel.py` (extended by one test).

**ARCHITECTURE:** §6.8 (§7).

**Never edit without an ICR** (§15.2): the rest of `telegram/gateway.py` (render path, business methods, throttle internals), `telegram/ingress/*`, `telegram/render.py`, `core/*` beyond the one method, `config.py`.

## 5. Tests (red first)

`tests/unit/test_gateway_documents.py` (`OutboundGateway` over `FakeTelegramSender`, `FakeClock`, an injected sleep that records waits; the existing `tests/unit/test_gateway_and_render.py` helpers):
- `test_send_document_goes_to_the_control_chat_without_a_connection_id`: one sender call, `chat_id` = the control chat; the sender method has no connection argument.
- `test_send_document_refuses_a_business_target`.
- `test_send_document_refuses_an_empty_file_without_calling_telegram`.
- `test_send_document_accepts_50_mb_and_refuses_one_byte_more_without_calling_telegram`.
- `test_send_document_refuses_a_caption_over_1024_units_without_calling_telegram` (with a non-BMP character).
- `test_send_document_refuses_an_unsafe_filename_without_calling_telegram` (parametrised).
- `test_send_document_is_throttled_like_text_sends`.
- `test_send_document_classifies_failures` (parametrised with real aiogram exceptions, as `FakeTelegramSender` scripts them: 403 → `forbidden`; 413 → `bad_request`; 429 → `rate_limited` with `retry_after`; 500 → `transient`; network timeout → `transient`), each `ok = false`.
- `test_rg20_an_uncertain_document_is_reported_uncertain_never_sent` (RG-20 extension): a timeout → `SendResult.delivery_uncertain` true; through `GatewayControlChannel` → `Delivery(ok=False, uncertain=True)`; a 4xx → `uncertain=False`.
- `test_the_channel_adapter_maps_a_sent_document_to_a_delivery`.
- `test_the_channel_adapter_reports_a_forbidden_send_as_error_forbidden` (R100; `tests/unit/test_agent_channel.py`, extended: a 403 from the sender through `send` and through `send_document` gives `Delivery(ok=False, error="forbidden", uncertain=False)`; the error text never contains the Bot API description).
- `test_aiogram_sender_uploads_bytes_as_a_plain_captioned_file` (a recording stand-in for `Bot`): `document` is a `BufferedInputFile` with the given name, `parse_mode=None`, `request_timeout=40`, no `business_connection_id`, and nothing is written to disk (the bytes stay in memory).
- `test_a_rate_limited_document_is_not_resent_by_the_gateway`: a 429 → one sender call, `rate_limited` with `retry_after`, no second call.

`tests/unit/test_document_send_confinement.py`: the two tests of §3.6.

`tests/unit/test_log_hygiene_gw_doc.py`: `test_document_sends_never_log_the_filename_or_caption` — a caption and a valid filename carrying canary tokens; sent, refused and failed sends log neither.

Run locally: these three files plus `tests/unit/test_gateway_and_render.py`, `tests/unit/test_business_send_confinement.py`, `tests/unit/test_no_aiogram_shortcuts.py`, `tests/unit/test_agent_channel.py`; ruff (including the new banned-API entries on all of `src/`), format, mypy on `src/assistant/{telegram,core}`; `lint-imports`.

## 6. Acceptance criteria

1. `OutboundGateway.send_document` accepts only a `ControlChat`, sends one throttled `sendDocument`, and refuses locally (no Telegram call) an empty or over-50 MB file, an over-long caption and an unsafe filename.
2. Every failure is classified as for text sends; an uncertain upload is reported uncertain, never as sent, and never retried by the gateway.
3. Filenames and captions never reach logs.
4. Only `telegram/gateway.py` builds an upload; `send_document` is called only by the gateway, its channel adapter and (later) `privacy/export.py`.
5. `ControlChannel`, `GatewayControlChannel` and both fakes have the method; mypy is clean; a forbidden send is reported as `Delivery.error == "forbidden"` (R100, pinned by a test).
6. Every named test in §5 passes in CI; RG-20 extended.
7. ARCHITECTURE §6.8 updated; the TF §13 row text is in the PR description for the TL.

## 7. Docs updated in the same PR

- ARCHITECTURE **§6.8**: replace the planned `send_document(target, file: ExportFile, caption)` line with the as-built signature; add the limits, the local refusal codes, plain captions, the 40 s upload timeout and why, the logging rule, and the confinement test.
- PR description: the TF §13 docs-check row for the TL ("`sendDocument`: multipart upload of an `InputFile`, ≤ 50 MB for bots on the cloud Bot API; caption 0–1024 characters after entity parsing; docs-checked, not live-verified for 45 MB parts"; sources https://core.telegram.org/bots/api#senddocument and https://core.telegram.org/bots/api#sending-files). The TL adds it (P3-interfaces §15.1: TF is the TL's).

## 8. Owner decisions

- **OD-20:** command only, JSON parts ≤ 45 MB, no delete button. A delete button would need `deleteMessage` on the control allow-list (TF §4.2, ARCHITECTURE §6.8 change; a 48 h limit applies) and a new gateway method; a larger part size cannot exceed 50 MB (TF §6).

## 9. Risks and notes

- **Contract gap G11:** P3-interfaces §4.6 gives the gateway a `Delivery` return, omits `telegram/control/agent_channel.py` from the touch points, and asks for a per-file ignore that already exists. This spec follows the gateway's own conventions; the fixer aligns the contract.
- **Upload time:** a 45 MB part needs about 9 Mbit/s to finish within 40 s. A timeout is reported uncertain and never resent; if the trial shows timeouts, lower `EXPORT_MAX_PART_MB` (a setting) rather than raising the timeout above the queue's grace.
- **Memory:** the method takes `bytes`; the caller (M7-EXP) holds at most one part in memory at a time.
- **`forbidden` and the auto-pause (R46):** a `forbidden` document result triggers the pause through the caller's after-commit call of M6-PAUSE's `pause_after_control_forbidden` (M7-EXP does it); nothing in this unit changes for it.
- **Wrong recipient:** the gateway trusts the caller's `chat_id` for a `ControlChat`, as it does for `send_text`. The export's own guard (M7-EXP §3.4) is what keeps a whole data export from reaching any chat but the owner's.
