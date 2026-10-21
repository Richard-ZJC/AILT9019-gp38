# AI Response Contract

One envelope, four statuses. Any branch that returns something else is a bug.

## Envelope (12 keys, built only by `Pipeline._base_body`)

| Key | Meaning | Notes |
|---|---|---|
| `status` | `OK / CLARIFY / REFUSE / SERVICE_UNAVAILABLE` | exactly one of four |
| `session_id` | server-side session handle | |
| `confidence` | 0.0-1.0, 3 decimals | |
| `product` | `{name, brand}` | may be empty |
| `components` | verdicts, only in `OK` | |
| `clarify` | question payload, only in `CLARIFY` | |
| `message` | refusal copy / empty | copy lives in `refusal.py` only |
| `search_used` | reserved for AI search | |
| `source_hint` | rule id or null | validated against rules.json |
| `raw_model` | `none / openai / anthropic` | |
| `mode` | `exact / fuzzy / ai / clarify / clarify_answer / refuse / service_unavailable` | |
| `degraded` | schema repaired the model output | |

## Rules that keep it fixed

1. **Clarify payload**: built ONLY by `fallback.build` (schema.py delegates; pipeline never re-assembles).
2. **Envelope**: built ONLY by `pipeline._base_body`; branches `update()` their own keys on top.
3. **Model output is never trusted**: `schema.validate` repairs or downgrades; a malformed answer can never become a category.
4. **Refusal copy**: lives in `refusal.py`, one string per reason, never duplicated elsewhere.
5. **Options**: only ids from `features.json`; anything else is replaced with the fixed list.
