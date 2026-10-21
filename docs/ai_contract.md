# AI Response Contract

One envelope, five statuses. Any branch that returns something else is a bug.

## Envelope (12 keys, built only by `Pipeline._base_body`)

| Key | Meaning | Notes |
|---|---|---|
| `status` | `OK / CLARIFY / REFUSE / SERVICE_UNAVAILABLE` | exactly one of four; the model's fifth status `BRAND` never reaches the wire as itself |
| `session_id` | server-side session handle | |
| `confidence` | 0.0-1.0, 3 decimals | |
| `product` | `{name, brand}` | may be empty |
| `components` | verdicts, only in `OK` | |
| `clarify` | question payload, only in `CLARIFY` | |
| `message` | user-facing copy | `REFUSE`: model-written (schema-cleans, 280-char cap), fixed `refusal.py` copy is the fallback; all other statuses: fixed copy only |
| `reason` | refusal detail, only in `REFUSE` | `model: not_packaging` etc.; diagnostics, the frontend shows `message` |
| `search_used` | reserved for AI search | |
| `source_hint` | rule id or null | validated against rules.json |
| `raw_model` | `none / openai / anthropic` | |
| `mode` | `exact / fuzzy / ai / clarify / clarify_answer / refuse / service_unavailable` | |
| `degraded` | schema repaired the model output | |

## Rules that keep it fixed

1. **Clarify payload**: built ONLY by `fallback.build` (packaging) or `fallback.build_products` (brand disambiguation); schema.py delegates; pipeline never re-assembles.
2. **Envelope**: built ONLY by `pipeline._base_body`; branches `update()` their own keys on top.
3. **Model output is never trusted**: `schema.validate` repairs or downgrades; a malformed answer can never become a category.
4. **Refusal structure is fixed, copy is layered**: `refusal.py` stays the single source of the five fixed copies (`out_of_scope`, `not_packaging`, `hazard`, `too_many_questions`, `service_unavailable`). For model-driven REFUSE only, the model may write its own `message` (cleaned + capped by schema) which replaces the fixed copy; a missing/invalid message falls back to `refusal.py`. Local refusals (hazard net, clarify budget, service down) never use model text.
5. **Options**: packaging clarify uses only ids from `features.json`; anything else is replaced with the fixed list.

## Brand disambiguation (web search)

- Bare brand name (`可口可乐`, `Nestle`) -> model status `BRAND` -> `Identifier.disambiguate` runs on the **search router** (OpenAI client + `enable_search: true`, Qwen/DashScope web search; never the main identify call) -> `CLARIFY` with `clarify.kind = "disambiguate"`, options = product names, hints = packaging phrases, `source = "ai-search"`.
- A chip click re-identifies the product name (frontend calls `/identify`), it is not an `/answer`.
- Loop guard: after one disambiguation round (`session.context.disambiguated`), a second `BRAND` degrades to the ordinary packaging clarify.
- Search failure or `< 2` candidates degrades to the ordinary packaging clarify - never a guess.
- **No packaging assumption**: the search prompt must include an unpackaged candidate (`packaging: "none"`, e.g. "Whole lemon (no packaging)") FIRST whenever the query plausibly names a bare item. Its chip sends the deterministic gate text `no packaging: <item>` -> `REFUSE`/`not_packaging` with a templated message (`refusal.not_packaging_item`), no model call. Hazards still win over it.
