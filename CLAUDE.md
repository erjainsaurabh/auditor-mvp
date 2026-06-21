# Auditor MVP — Claude Context

Full framework specification is in `AUDITOR_AGENT_FRAMEWORK.md`. This file
gives the session-start context needed to work on the code without re-reading
the entire spec each time.

## Task tracking — mandatory

All work is tracked in `TODO.md`. At the start of every session:
1. Read `TODO.md` to understand what is done, what is current, and what is pending.
2. Before starting any implementation, confirm the task matches what is listed.
3. When a task is completed, update `TODO.md` immediately — mark it `✓ done`
   and move it to the Completed section.
4. When a new task is identified (design decision, bug, gap), add it to `TODO.md`
   before or immediately after the conversation that surfaces it.
5. Never let `TODO.md` go stale — it is the single source of truth for project state.

---

## What this project is

An AI-powered, spec-driven exploratory testing agent. The agent acts as an
**auditor with a checklist**: it takes a set of claims about a web application,
verifies each one against the live UI using a ReAct loop (Reason → Act →
Observe), and produces an evidence-backed pass/fail report.

Target use case: SaaS customizations (Salesforce, ServiceNow, etc.) where
there is no code access and specs live in Word/PDF documents. MVP uses
hand-written YAML claims.

---

## Stack

| Layer | Technology |
|---|---|
| Browser automation | Playwright (Python, sync API) — version 1.59+ |
| LLM abstraction | LiteLLM — provider is a config value, never a code dependency |
| Default reasoning model | `claude-sonnet-4-6` |
| Default fast model | `claude-haiku-4-5-20251001` |
| Schema + validation | Pydantic v2 |
| Dependency graph | networkx |
| Config | config.yaml + .env (API keys) |
| Console output | rich |

---

## Project structure

```
auditor-mvp/
├── AUDITOR_AGENT_FRAMEWORK.md   # full framework spec
├── config.yaml                  # base_url, LLM model strings, agent limits
├── claims.yaml                  # what to verify — hand-written (MVP/V1.2), LLM-generated (V1.4+)
├── fingerprints.yaml            # how to verify it — machine-maintained, created at runtime
├── run.py                       # entrypoint: python run.py claims.yaml
│
├── auditor/
│   ├── loader.py        # Pydantic schemas (Claim, ClaimType, ClaimStatus) + load_claims()
│   ├── graph.py         # build_graph(), execution_order(), cascade_failure(), mark_blocked()
│   ├── tools.py         # BrowserSession class + _trim_table_rows() snapshot trimmer
│   ├── llm_client.py    # LLMClient: reason() for ReAct loop, extract() for simple tasks
│   ├── agent.py         # run_claim() — the ReAct loop driving one claim end to end
│   ├── evidence.py      # EvidenceCollector: action log + screenshots + evidence.json
│   └── report.py        # write_report() → report.json, print_summary() → rich console
│
├── evidence/            # created at runtime, gitignored
└── report.json          # output, gitignored
```

### Three-file ownership model

```
claims.yaml          → what to verify       (spec-level, stable, human/LLM authored)
fingerprints.yaml    → how to verify it     (DOM-level, machine-maintained, evolves with UI)
config.yaml          → where to run it      (environment: base_url, credentials)
```

`claims.yaml` and `fingerprints.yaml` are environment-agnostic — DOM structure
is determined by the codebase, not the environment. Only `config.yaml` changes
between dev / staging / prod.

---

## How to run

```bash
# one-time setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env    # add ANTHROPIC_API_KEY

# run
python run.py claims.yaml
```

---

## Data flow

```
claims.yaml
  → loader.py       parse + validate → list[Claim]
  → graph.py        build DAG → topological execution order
  → run.py          for each claim in order:
      → agent.py    ReAct loop → ClaimStatus
          ↕ llm_client.py   LiteLLM calls (reason model)
          ↕ tools.py        Playwright browser actions
          ↕ evidence.py     log actions + screenshots
  → report.py       write report.json + print summary
```

---

## ReAct loop (agent.py)

One call to `run_claim()` handles one claim:

1. Build initial message: claim description + expected outcome + navigation hint
2. Call `llm.reason(messages)` — LLM responds with a tool call
3. Dispatch tool call via `_dispatch()` → Playwright action or evidence action
4. Append tool result to messages; prune stale `read_page` results (keep only latest)
5. Repeat until `verify_claim` tool is called or `max_actions` reached
6. `verify_claim` → sets `claim.status`, calls `evidence.set_verdict()`
7. Max actions exceeded → `claim.status = blocked`

---

## LLM client rules (llm_client.py)

- `agent.py` never imports `litellm`, `anthropic`, or any provider SDK directly
- All LLM calls go through `LLMClient.reason()` or `LLMClient.extract()`
- Provider and model strings live **only** in `config.yaml`
- Switching provider = change two lines in `config.yaml`, zero code changes
- System prompt uses `cache_control: ephemeral` — paid once per session
- Rate limit errors are retried with backoff (3 attempts, 60s intervals)
- Token usage (in/out) is logged per step for cost visibility

```yaml
# config.yaml — swap these two lines to change provider/model
llm:
  reasoning_model: "claude-sonnet-4-6"
  fast_model:      "claude-haiku-4-5-20251001"
```

---

## Graph + cascade rules (graph.py)

- Claims with `depends_on` form a DAG (networkx DiGraph)
- Execution follows topological sort — dependencies always run first
- If a claim is `failed` or `blocked`, all descendants are immediately marked `blocked`
- Blocked claims are skipped in `run.py` — not passed to the agent

---

## Tools exposed to the LLM (tools.py + llm_client.py)

| Tool | What it does |
|---|---|
| `navigate` | Go to a path or full URL. Paths append to base_url (SPA pattern) |
| `read_page` | Returns `aria_snapshot` output trimmed to 5 table rows max |
| `click` | Click by visible text — 6 strategies + force + JS fallback |
| `hover` | Hover to reveal CSS dropdowns — 6 strategies + JS fallback |
| `fill_field` | Fill a form field by label — 5 strategies |
| `clear_field` | Clear a form field by label |
| `submit_form` | Press Enter to submit |
| `get_field_options` | Get picklist/select options |
| `take_screenshot` | Save screenshot as evidence |
| `verify_claim` | Record verdict: verified / failed / unverifiable |

**`read_page` implementation**: calls `page.aria_snapshot()` (Playwright 1.44+),
prepends `url` and `title`, then runs `_trim_table_rows()` to cap table data rows
at 5 (header row always kept). All other elements — buttons, links, headings,
fields — are passed through untrimmed to avoid hiding relevant UI elements.

**URL construction**: `navigate()` appends relative paths to `base_url` using
string concatenation (`base_url + "/" + path`), NOT `urljoin`. This preserves
the full SPA base path (e.g. `page.aspx/en/ctr/...`).

---

## Evidence structure

```
evidence/
  {run_id}/
    {claim_id}/
      evidence.json       # action sequence, screenshots list, verdict
      {claim_id}_*.png    # screenshots taken during verification
```

---

## Claim schema (loader.py)

```yaml
- id: claim_001
  description: "Customer Tier field exists on Account Create"
  type: existence          # existence | value | behavioral | transition |
                           # persistence | permission | constraint | cross_module
  navigation: "/accounts/new"
  expected: "field with label 'Customer Tier' is visible"
  depends_on: []           # claim IDs that must pass first
  setup:                   # optional pre-actions before verification
    - fill_field: {label: "Account Name", value: "Test"}
  action: submit_form      # optional explicit action (e.g. submit before observing)
```

---

## Execution Fingerprint Layer

On the first successful ReAct run for a claim, the agent records a fingerprint:
the action sequence, the resolved DOM selectors, and the assertions that confirmed
the verdict. On every subsequent run, the fingerprint is replayed deterministically
before the ReAct loop is attempted.

### Three execution tiers

```
Tier 1 — Primary selector (highest confidence, direct Playwright call, no LLM)
    ↓ fails
Tier 2 — Alternative selectors (tried in confidence order, no LLM)
    ↓ all fail
Tier 3 — Full ReAct loop → on success, update fingerprint with new primary
```

### Fingerprint structure

```yaml
claim_001:
  recorded_at: "2026-05-07T..."
  run_id: "run_b66f0053"
  description: "Contract menu item is visible"   # metadata only — not executed
  actions:
    - tool: navigate
      args: {target: "/"}
    - tool: click
      description: "Contracts"                   # original intent — metadata only
      selectors:
        - type: xpath
          value: "//nav//button[normalize-space()='Contracts']"
          successes: 12
          failures: 0
          confidence: 1.0
        - type: aria
          value: "button[name='Contracts']"
          successes: 11
          failures: 1
          confidence: 0.92
    - tool: read_page
      assertions:
        - aria_contains: {role: link, name: "Browse Financial Contract Change Requests"}
          confidence: 1.0
```

### Confidence matrix

`confidence = successes / (successes + failures)` per selector.

- Selector works → `successes += 1`
- Selector fails, fallback works → `failures += 1` on failed, `successes += 1` on fallback
- All selectors fail → ReAct runs, new selector added as primary, old ones demoted

A selector whose confidence drops below 0.7 is flagged as drift — the UI may
have changed even if fallbacks are still catching it.

### Selector extraction

Happens in `tools.py` at action success time — zero extra LLM cost:
- XPath: computed from the resolved element's DOM position
- Aria: `aria-label` + `role` attribute if present
- CSS: tag + stable structural attributes if available

The original natural language description (`"Contracts"`) is kept as metadata
in the fingerprint but is not a separate execution tier. It documents intent;
the stored selectors do the actual work.

---

## Current build phase: V1.4

```
MVP  ✓  Manual YAML claims, flat claim list, single ReAct loop per claim
V1.1 ✓  Simple feature flows — ordered steps, shared context within a step
V1.2 ✓  Session state + data handoff between steps
V1.3 ✓  Execution fingerprint layer — selector recording, three-tier replay, confidence matrix, drift detection
V1.4 ←  LLM claim extraction from pasted spec text
V1.5    Word/PDF parsing feeding into LLM extraction
V1.6    SaaS DOM simplification (Salesforce Lightning, ServiceNow)
V2.0    Full framework — two-level graph, HTML report, visual diffing, PostgreSQL
```

### V1.3 scope (completed)
- Three-tier replay: Tier 1 (primary selector, no LLM) → Tier 2 (alt selectors by confidence) → Tier 3 (full ReAct)
- Fingerprint recorded after first successful ReAct run — exact DOM selectors (xpath, aria, css) + assertions
- `step_hash` (SHA-1 of description + expected + navigation + data_keys + hints) — fingerprint auto-invalidated when YAML changes
- Confidence matrix per selector: `successes / (successes + failures)`; drift flagged when confidence < 0.7
- Dynamic-ID filtering at recording time — 5+ digit sequences skipped as assertions
- `hints` included in the step hash — changing hints invalidates the fingerprint and forces a fresh ReAct run

### Still excluded (V1.4+)
- LLM claim extraction from spec text
- Word/PDF spec parsing
- PostgreSQL
- HTML report
- Multi-role / multi-session testing
- Visual diffing / image hashing

---

## Key design decisions — never violate these

1. **LLM provider is config, not code** — `agent.py` never imports a provider SDK
2. **DOM is always simplified before LLM** — `read_page` returns aria snapshot, never raw HTML
3. **Claim graph is the scope boundary** — agent only verifies what's in the graph
4. **Evidence is mandatory** — every verdict must have screenshots + action log
5. **Cascade before reporting** — failed claim blocks all descendants before report runs
6. **Sync Playwright only** — no async; the ReAct loop is inherently sequential
7. **Unverifiable is a first-class status** — ambiguous claims are flagged, never guessed
8. **Credentials never go to the LLM** — login is a deterministic pre-step in run.py using .env
