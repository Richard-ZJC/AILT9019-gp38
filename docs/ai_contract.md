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
| `search_used` | `CLARIFY/disambiguate` carries `true`; `OK` carries the model's own flag | |
| `source_hint` | rule id or null | validated against rules.json |
| `raw_model` | `none / openai / anthropic` | |
| `mode` | `exact / fuzzy / ai / clarify / clarify_answer / best_guess / refuse / service_unavailable` | |
| `degraded` | schema repaired the model output | |

## Rules that keep it fixed

1. **Clarify payload**: built ONLY by `fallback.build` (packaging) or `fallback.build_products` (brand disambiguation); schema.py delegates; pipeline never re-assembles.
2. **Envelope**: built ONLY by `pipeline._base_body`; branches `update()` their own keys on top.
3. **Model output is never trusted**: `schema.validate` repairs or downgrades; a malformed answer can never become a category.
4. **Refusal structure is fixed, copy is layered**: `refusal.py` stays the single source of the four fixed copies (`out_of_scope`, `not_packaging`, `hazard`, `service_unavailable`). For model-driven REFUSE only, the model may write its own `message` (cleaned + capped by schema) which replaces the fixed copy; a missing/invalid message falls back to `refusal.py`. Local refusals (hazard net, service down) never use model text.
5. **Options**: packaging clarify uses only ids from `features.json`; anything else is replaced with the fixed list.

## Brand disambiguation (web search)

- Bare brand name (`可口可乐`, `Nestle`) -> model status `BRAND` -> `Identifier.disambiguate` runs on the **search router** (OpenAI client + `enable_search: true`, Qwen/DashScope web search; never the main identify call) -> `CLARIFY` with `clarify.kind = "disambiguate"`, options = product names, hints = packaging phrases, `source = "ai-search"`.
- A chip click re-identifies the product name (frontend calls `/identify`), it is not an `/answer`.
- Loop guard: `session.context.disambiguated` stores the **query text**, and only the identical query degrades to the ordinary packaging clarify. A different word in the same session is not a loop.
- Search failure does **not** degrade to the packaging question: the card keeps a single honest option, `"<query> (no packaging)"`. Asking "which packaging do you see?" would assume packaging the user may not have.
- **Question budget must conclude, not refuse**: after two clarify rounds a third question is forbidden. The pipeline instead calls `Identifier.commit`, a final model call that is told it may not ask again and must pick the most likely packaging (`OK`, `mode = "best_guess"`, confidence <= 0.6, with a "check before you sort" note). If the model still fails, the leading clarify option is resolved deterministically; only if that also fails does it return a neutral `best_effort` banner. There is no "I still cannot tell - check it yourself" refusal.
- Candidate hygiene (deterministic, in `pipeline._with_unpacked_option`): duplicates are dropped; an `"X (no packaging)"` entry is removed when the same `X` is also listed as packaged, because one product cannot be both.
- **No packaging assumption**: the search prompt must include an unpackaged candidate (`packaging: "none"`, e.g. "Whole lemon (no packaging)") FIRST whenever the query plausibly names a bare item. Its chip sends the deterministic gate text `no packaging: <item>` -> `REFUSE`/`not_packaging` with a templated message (`refusal.not_packaging_item`), no model call. Hazards still win over it.

## Deterministic guards around CLARIFY (do not rely on the model)

The model is free to be wrong; these rules make two specific wrong answers unreachable:

- **Bulk / category words never refuse as "unpackaged"**: `pipeline._is_bulk_material` covers `tea, coffee, milk, water, juice, rice, flour, sugar, salt, oil, soup, cereal, ...` (plus `茶/咖啡/牛奶/果汁`). If the model answers `REFUSE/not_packaging` for one of them, the query is sent to product search instead - these substances ship in dozens of packagings. A whole fruit (`banana`, `apple`) is not in the list and keeps its refusal.
- **A bare noun never gets the packaging quiz**: `pipeline._is_bare_noun` = at most two words, no digits, no container word (`bottle/cup/can/box/carton/jar/bag/pack/...`). When the model answers `CLARIFY` for such a query, the packaging question is replaced by product search, because "which packaging do you see on the product?" has no answer when the user typed a flavour and may not be holding anything. `"a bottle"` names a container, so its packaging question stands.

