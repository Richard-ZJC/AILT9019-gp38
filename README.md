# AILT9019-gp38 — Campus Recycling Helper

A brand-aware packaging sorting assistant: user enters a brand or product name, the app identifies its packaging components, matches them against a fixed set of published campus recycling rules, and returns the category with a visible source. A category can only come from a rule; when the packaging is ambiguous it asks, and after two questions it commits to its best guess labelled as a guess instead of refusing.

## Status

Core pipeline implemented (T0–T8); optional modules are interface stubs. See [plan.md](./plan.md) §8 for the live progress table.

| Layer | State |
|---|---|
| Text input → components → rule → category + source | done |
| Zero-cost exact keyword path (no API call) | done |
| Dual-channel AI (OpenAI → Anthropic, 10 s failover) | done, needs a key |
| Strict JSON template + validation + degradation | done |
| Clarify with fixed options, 2-round server-side limit | done |
| Refusal (out of scope / hazardous / both channels down) | done |
| Web UI, single file, English | done |
| Image upload, fuzzy match, SSE streaming, local memory | interfaces only (🟦) |

## Stack

Python 3.10+ standard library only, plus a single-file HTML frontend. No build step, no third-party packages.

```bash
python server.py            # real providers from config.json / env
python server.py --mock     # deterministic offline provider, no key needed
# then open http://localhost:8000
```

macOS shortcut: double-click `start.command` (or `./start.command`) — it starts the server if not running and opens the browser.

## API key

Copy the template and fill in the campus-provided key:

```bash
cp config.example.json config.json     # config.json is gitignored
```

```json
{ "providers": ["openai", "anthropic"],
  "openai":    { "api_key": "sk-...", "model": "gpt-4o-mini" },
  "anthropic": { "api_key": "sk-ant-...", "model": "claude-3-5-haiku-latest" },
  "timeout_seconds": 10 }
```

Environment variables win over the file: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`.

Without any key the server still starts: the keyword layer keeps answering, everything else returns `SERVICE_UNAVAILABLE` (never a guess).

### Bailian (course-provided key)

The course key is an OpenAI-compatible/Alibaba Cloud Bailian gateway, not the public OpenAI API. Use `qwen3.8-flash` and two gateway-specific switches — they live in `extra_body` so the code stays provider-neutral:

```json
{
  "providers": ["openai", "anthropic"],
  "timeout_seconds": 15,
  "openai": {
    "api_key": "<group key>",
    "model": "qwen3.8-flash",
    "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "extra_body": { "enable_thinking": false }
  },
  "anthropic": {
    "api_key": "<group key>",
    "model": "qwen3.8-flash",
    "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
    "extra_body": { "thinking": { "type": "disabled" } }
  }
}
```

Why those switches: with reasoning left on, one request took **12–67 s** for the same prompt; with thinking disabled it drops to **2–5 s**. This is a JSON-extraction task, so the chain of thought buys nothing and costs the failover budget.

The key is shared by the group and stays in `config.json` (gitignored). Never commit it, and do not paste it into an issue or a screenshot.

### Timeout

`timeout_seconds` is a **hard deadline per provider**, and it covers the whole non-streaming generation rather than time-to-first-token. Measured on the Bailian flash model: 3–7 s typical, so 15 s leaves headroom; worst case is 2 × 15 s before `SERVICE_UNAVAILABLE`.

## Deploy (hosted public URL)

The server is standard-library only, so it deploys to any Python web host with no build step. A `Procfile` and `render.yaml` are included.

### Render (recommended, free)

1. Sign up at render.com → **New Web Service** → connect this GitHub repo (`Richard-ZJC/AILT9019-gp38`).
2. Render auto-detects the `Procfile`; `render.yaml` pre-sets the start command and `/health` check.
3. **Environment** tab — add these (read by `app/config.py`, never committed):
   - `OPENAI_API_KEY` — your Bailian / OpenAI key
   - `OPENAI_BASE_URL` — Bailian compatible endpoint (`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`)
   - `OPENAI_MODEL` — `qwen3.8-flash`
   - `OPENAI_EXTRA_BODY` — `{"enable_thinking": false}` (keeps latency at 2–5 s; without it Bailian thinks and takes 12–67 s)
   - (optional) `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` / `ANTHROPIC_EXTRA_BODY` for the fallback channel
4. Deploy → you get a public `https://ailt9019-gp38.onrender.com`. Free plan sleeps after inactivity (~30–60 s cold start).

> The URL is public — anyone with the link can call the AI endpoint and spend your key's quota. Share it only within the group, or ask for a shared-password gate to be added.

## Endpoints

| Route | Body | Answer |
|---|---|---|
| `POST /identify` | `{"text": "...", "session_id": "optional"}` | full result JSON |
| `POST /answer` | `{"session_id": "...", "choice": "bottle"}` | full result JSON |
| `POST /identify/stream` | — | `501 {"status":"UNAVAILABLE"}` (reserved for T13) |
| `GET /health` | — | `{"ok": true}` |

Result shape (abridged):

```json
{ "status": "OK",
  "session_id": "…",
  "confidence": 0.93,
  "product": { "name": "Vitasoy lemon tea", "brand": "Vitasoy" },
  "components": [
    { "part": "carton", "material": "tetra pak", "status": "MATCHED",
      "category": "Recyclable - Tetra Pak", "rule_id": "beverage-carton",
      "explanation": "Empty and rinse the carton, flatten it, then place it in the Tetra Pak slot…",
      "source": { "name": "HKU re Campaign — Recycling 101 Photo Guide", "url": "https://wastereduction.hku.hk/photo-guide" },
      "effective_date": "2026-10-21" } ],
  "mode": "exact | fuzzy | ai | clarify | clarify_answer | best_guess | refuse | service_unavailable" }
```

`status` is one of `OK`, `CLARIFY`, `REFUSE`, `SERVICE_UNAVAILABLE`. `mode` tells you which path produced the answer (`exact` = no model call at all).

## Supported items

16 rules, all traceable to the HKU *re* Campaign pages
([Recycling 101 photo guide](https://wastereduction.hku.hk/photo-guide),
[re Kiosk accepted items](https://www.wastereduction.hku.hk/re-kiosk)):

| Rule id | Item | Category |
|---|---|---|
| `paper-general` | paper, newspaper, cardboard box, paper bag | Recyclable - Paper |
| `pizza-box` | clean pizza box | Recyclable - Paper |
| `paper-cup-sleeve` | take-away cup sleeve | Recyclable - Paper |
| `baking-paper` | baking / parchment paper | General Waste |
| `beverage-carton` | Tetra Pak, juice box, clean take-away coffee cup | Recyclable - Tetra Pak |
| `metal-can` | aluminium can, tin can, metal box | Recyclable - Metal |
| `glass-bottle` | glass bottle or jar | Recyclable - Glass |
| `plastic-bottle` | PET / water / drink bottle | Recyclable - Plastic |
| `plastic-cup-lid` | clean cup lid, bottle cap | Recyclable - Plastic |
| `noodle-cup-bowl` | instant noodle cup / bowl | Recyclable - Plastic |
| `noodle-cup-lid` | instant noodle container lid | General Waste |
| `styrofoam-box` | cleaned styrofoam box or tray | Recyclable - Plastic |
| `contact-lens-tray` | contact lens packaging tray | Recyclable - Plastic |
| `plastic-packaging` | other clean plastic packaging | Recyclable - Plastic |
| `rechargeable-battery` | rechargeable battery, power bank | Special Handling |
| `out-of-scope` | chemicals, e-waste, furniture, medical waste… | Refuse |

Notes:

- `effective_date` is the date the rule was last verified against the linked page — replace it with the official policy date once the Estates Office document numbers are confirmed (see plan.md T1).
- Glass and rechargeable batteries are collected at the re Kiosk (and, for glass, only at the Smart Recycling Bins at Central Podium Levels and Pine Court).

## Verify

```bash
python tests/run_tests.py                     # 10 brand + 5 low-confidence + 5 unsupported, mock provider
python tests/run_tests.py --live              # same cases against the real key
python tests/run_tests.py --live --no-fast-path   # disables the keyword layer: every case goes through the model
python tests/test_ai_plugin.py                # 9 routing / schema / degradation checks, fake providers
python tests/smoke_test.py                    # boots the server, hits every route
python tests/live_ai_test.py                  # one model call per query, prints the routing trace
```

Pass line: every group ≥ 80 %, all plugin checks and all smoke checks green.

Last measured (Bailian `qwen3.8-flash`, 2026-10-21): mock 20/20 · live with keyword layer 19/20 · live AI-only 19–20/20. The one unstable case is `l03 "some snack wrapper"`, where the model sometimes calls it plastic packaging instead of asking — a genuine judgement call, not a pipeline bug.

## Layout

```
server.py            entry point (http.server, no framework)
app/                 pipeline, session, matcher, adjudicator, ai_plugin/, refusal, fallback, explain
data/rules.json      16 rules: id / keywords / parts / materials / category / source / effective_date
data/features.json   the only legal source of clarifying options
web/index.html       single-page English UI
tests/               cases + harnesses (see above)
```

## Design rules we do not break

- The AI identifies, it never decides a category. `app/match.py` is the only place a category is produced, and it only copies from `rules.json`.
- If no rule matches, the answer is `REFUSE` with a fixed sentence — a category is never invented.
- The 2-round clarifying limit is counted on the server, so a modified frontend cannot bypass it. The limit does not end in a refusal: the third round calls the model once more with "decide, do not ask again", answers with `mode: "best_guess"` (confidence ≤ 0.6, an explicit "check before you sort" note), and falls back to the leading option if the model fails. "I still cannot tell, check it yourself" is not a valid outcome.
- Every answered component carries a source URL and the rule id it came from.
