# FlowProbe — Task Tracker

Status key: `✓ done` · `← current` · `○ pending`

---

## Next Up

- `←` **V1.4 — LLM claim extraction** *(current)*
  - Paste spec text → LLM generates `claims.yaml`
  - Replaces manual YAML authoring; everything downstream stays identical

- `○` **V1.5 — Word/PDF parsing**
  - `python-docx` + `pdfplumber` feeds into V1.4 extraction
  - Two-pass: flows first, then claims per step

- `○` **V1.6 — SaaS DOM simplification**
  - Platform-specific semantic extractors for Salesforce Lightning, ServiceNow
  - Replaces generic `aria_snapshot` approach in `tools.py`

- `○` **V2.0 — Full framework**
  - HTML report (Jinja2)
  - Visual diffing (perceptual hash → CNN)
  - PostgreSQL for session state and trend tracking
  - Multi-role sessions (switching users mid-flow)
  - Test data cleanup (reverse-order deletion after each flow)
  - Parallel flow execution

---

## Code audit findings (2026-06-24)

Findings from external code audit — performance, reliability, extensibility, scalability.

### Priority 0 — fix before next real run

- `○` **F1 · No browser-level timeout** — `BrowserSession.start()` never calls `set_default_timeout` / `set_default_navigation_timeout`. A hung SPA navigation blocks the entire process forever. Fix: add `self._page.set_default_timeout(30_000)` and `set_default_navigation_timeout(60_000)` in `tools.py:start()`.

- `○` **F2 · Shared mutable `session_data` dict** — `run_test_condition` mutates the caller's dict in place. A bad capture in one TC silently poisons all subsequent TCs. Fix: `session_data = dict(session_data or {})` at the top of `run_test_condition`.

- `○` **F3 · Hard-coded `"20"` in LLM working-memory context** — `react_agent.py:676` injects `step N/20` regardless of `max_actions` config. LLM misjudges remaining budget. Fix: thread `max_actions` into the closure and use `f"{_working_memory['step']}/{max_actions}"`.

### Priority 1 — fix this sprint

- `○` **F4 · FingerprintStore saves on every `record()` call** — writes the full YAML after every action; 50 claims = 50 full rewrites. Also the write is not atomic (no temp-file + rename), so a crash mid-write leaves a corrupt `fingerprints.yaml`. Fix: remove `save()` from inside `record()`; rely on the `fp_store.save()` already called in `run.py` at teardown. Make the write atomic with a `.tmp` → `os.replace()` pattern.

- `○` **F5 · LLM retry only catches `RateLimitError`** — transient `APIConnectionError`, 529 overload, SSL timeout, and `Timeout` all crash the run mid-execution, losing all evidence collected so far. Fix: extend retry block to also catch `litellm.APIConnectionError`, `litellm.ServiceUnavailableError`, `litellm.Timeout`.

### Priority 2 — schedule next cycle

- `○` **F6 · `import re` inside hot ReAct loop** — three inline `import re` / `import re as _re` inside the per-action loop body (`react_agent.py:527, 616, 707`). Already imported at module level (line 14). Fix: delete the inline imports.

- `○` **F7 · `session._page.title()` bypasses BrowserSession abstraction** — `react_agent.py:226` calls a private Playwright attribute directly, breaking subclasses and mocks. Fix: add `current_title() -> str` to `BrowserSession` and call that.

- `○` **F8 · `assert` used as runtime guard in `tools.py`** — `assert self._page is not None` is silently removed under `python -O`. Fix: replace with `if self._page is None: raise RuntimeError("BrowserSession not started")`.

- `○` **F9 · Seq log queue silently drops messages at capacity** — `_SeqHttpHandler` queue (maxsize=1000) swallows `queue.Full` silently. For an audit tool, losing structured evidence logs is a correctness issue. Fix: emit a `WARNING` to stderr when drops occur, or expose a drop counter.

- `○` **F10 · `_DYNAMIC_ID_RE` misses UUIDs and token strings** — regex `r"\d{5,}"` filters auto-increment IDs but UUID-style strings (`a3f2e1b9-...`) and token patterns (`tok_abc123`) slip through and corrupt fingerprint assertions. Fix: extend regex to also match UUIDs and common token patterns.

### Priority 3 — backlog / V2 considerations

- `○` **F11 · Module-level `_replayer` singleton** — `react_agent.py:56` creates `FingerprintReplayer()` at import time; implicit shared state across runs in future API-server mode. Fix: instantiate inside `run_test_condition` or inject via parameter.

- `○` **F12 · `report.py` uses `getattr()` guards masking schema regressions** — silent empty fields instead of loud `AttributeError` when model fields are removed. Fix: access Pydantic fields directly.

- `○` **F13 · No claim-level parallelism** — independent claims (no `depends_on`) run serially. Claims in the same topological generation could run in parallel browser contexts. V2 design: `ThreadPoolExecutor` per generation, one `BrowserSession` per worker. Note: `BrowserSession` is not thread-safe — each worker must get its own instance.

- `○` **F14 · `max_budget_usd` config never enforced** — token usage is logged but never checked against the budget cap. A runaway loop can burn far more than the stated limit. Fix: accumulate token counts across calls, estimate cost with a model pricing table, raise `BudgetExceededError` before the next LLM call.

- `○` **F15 · Evidence storage backend tightly coupled** — S3 upload is embedded in `EvidenceCollector` rather than behind a protocol. Adding Azure Blob or GCS later requires forking the class. Fix: define a `StorageBackend` protocol (`upload(local_path, remote_key) -> str`) and inject it.

---

## Design gaps

- `✓` **Consolidate flow storage into `flows/` directory**
  - Currently the API writes submitted YAMLs to `evidence/{run_id}/staging/` — a new
    temp directory per run, accumulating forever on EBS with no reuse benefit
  - Target layout — everything related to a flow lives together:
    ```
    flows/
      extranet_plan.yaml                  ← flow definition
      extranet_plan_data.yaml             ← test data
      extranet_plan.fingerprints.yaml     ← generated alongside, not in a separate dir
    evidence/                             ← run output only (no staging clutter)
      run_abc/
        report.json
        *.png
    ```
  - API changes needed:
    - `POST /flows` (or on `/run`) — save `yaml_contents` to `flows/{filename}` and
      `data_content` to `flows/{stem}_data.yaml` by name, not by run_id
    - `run.py` / `FingerprintStore` — point fingerprint path to `flows/` instead of
      `fingerprints_dir` so it sits next to its source YAML
    - Remove staging logic from `api.py`; file-path mode and content mode both resolve
      to the same `flows/` location
  - Benefit: fingerprints persist and are found correctly on EBS across deploys;
    no orphaned staging directories; `flows/` is the single source of truth for inputs

---

## Pending (tooling improvements)

- `○` **Fix click/hover disambiguation** — add optional `role` parameter to `click()` and
  `hover()` in `tools.py`; return richer confirmation (role + context) so the LLM knows
  what it actually clicked. Update tool definition in `llm_client.py` to expose `role`.
  Fixes the case where two elements share similar text (e.g., "Financials" tab vs
  "Financial Change Request" nav link) and `.first` picks the wrong one silently.

- `○` **Add missing UI actions to `tools.py`**
  - `select_option` — choose a value in `<select>` / custom picklist (currently `get_field_options` reads but cannot select)
  - `check` / `uncheck` — checkboxes and toggle switches
  - `double_click` — some SaaS UIs open records on double-click
  - `press_key` — Escape (close modals), Tab (next field), keyboard shortcuts
  - `scroll` — reveal lazy-loaded content or off-screen elements
  - `wait_for_element` — explicit wait when networkidle isn't sufficient
  - `drag_and_drop` — kanban boards, reordering
  - `iframe_context` — switch into embedded iframes

---

## Pattern inventory — machine-learned action priors

- `○` **Tool-usage stats per step type**
  - Track which tools the ReAct loop actually calls per `StepType` (existence, value, behavioral, etc.)
  - After many runs: `behavioral → [navigate, click, fill_field, read_page]` with frequencies
  - Feeds into LLM context for steps without a fingerprint: "for behavioral steps, typically use these tools"

- `○` **Platform aria-pattern → tool mapping**
  - On each successful ReAct run, record: aria pattern observed → tool called → succeeded
  - Example: Ivalua `combobox[name~="..."]` → `select_option`; `grid` with rows → `click` first row link
  - Stored per platform in `pattern_inventory.yaml`

- `○` **Pattern-assisted fingerprint generation (core feature)**
  - For steps with no fingerprint, query the inventory: step type + platform + keywords from description
  - Generate a candidate action plan (ordered tool sequence) and inject as machine-generated hints
  - LLM executes the plan → succeeds faster → fingerprint recorded on first run
  - Sits between human hints and fingerprint replay in the execution lifecycle:
    ```
    hints (human, static) → pattern inventory (machine, dynamic) → fingerprint (exact replay)
    ```
  - Design needed: inventory schema, query logic (keyword match vs embedding similarity), confidence threshold for when to inject vs stay silent

- `○` **Pattern inventory — verb normalisation**
  - Currently each verb is stored as-is (`select`, `choose`, `pick`, `set`, `enter` all create separate keys)
  - Once real runs accumulate, review which verbs cluster naturally and build a normalisation map
  - Examples expected: `choose/pick → select`, `enter/type/input → fill`, `open/expand → click`
  - Do NOT implement until patterns from real runs are available — normalisation map must be data-driven, not guessed

---

## Multi-application support

- `○` **Multi-app config — holistic end-to-end testing across applications**
  - Today `config.yaml` is a single flat config (one `base_url`, one `platform`, one credential set)
  - Goal: allow flows targeting different apps (e.g. Ivalua + Salesforce + ServiceNow) to run in one session without separate config files or separate invocations
  - Design options to evaluate:
    - `apps:` block in `config.yaml` — named app entries each with their own `base_url`, `platform`, `auth`, `strategy_stats_file`
    - Flow YAML declares `app: ivalua` at the flow level; `run.py` switches `BrowserSession` and `StrategyStats` between flows
    - Cross-app flows: a test condition can navigate across app boundaries (e.g. create record in App A, verify downstream effect in App B)
  - Requires: per-app `BrowserSession` lifecycle (separate pages or separate browser contexts), per-app login pre-step, per-app `StrategyStats` and fingerprint scoping
  - Enables: holistic regression suites that span procurement → ERP → CRM without manual stitching

---

## Spec gaps to address before V2.0

These are in `AUDITOR_AGENT_FRAMEWORK.md` but not yet implemented:

- `capture_from_toast` and `capture_from_field` output capture strategies
- Flow-level evidence trace (step-by-step execution record, not just per-claim)
- Trend tracking across runs (pass rate over time per claim / flow)

---

## Completed

- `✓` **MVP** — flat YAML claims, single ReAct loop per claim, evidence + report
- `✓` **V1.1** — flows → test conditions → steps hierarchy, shared LLM context within a test condition, message pruning
- `✓` **V1.2** — session state capture (`current_url`, `page_title`, `url_segment:N`), data handoff between steps
- `✓` **V1.3** — Execution fingerprint layer: selector recording, three-tier replay, confidence matrix, drift detection, dynamic-ID filtering at recording time
- `✓` **Bug: hints excluded from step_hash** — `hints` added to `step_definition_hash()` signature in `fingerprint.py`; both call sites in `react_agent.py` updated; changing hints now correctly invalidates the fingerprint and forces a ReAct re-run
- `✓` **Test data decoupling** — `{{key}}` placeholders in claims.yaml resolved from `test_data.yaml`; `--data` flag + auto-detection in `run.py`
- `✓` **Claims spec cleanup** — all `expected` fields rewritten to BA-readable natural language outcomes; tool syntax, nav directives, LLM guardrails moved to system prompt
- `✓` **System prompt hardening** — SCOPE RULE, BROWSER STATE RULE, BEHAVIORAL CLAIM RULE added; interaction rules generalized (no hardcoded values)
- `✓` **Bug: click focused-snapshot wrong anchor** — `click()` now resets `_last_interacted_label = ""`; ivalua-listbox clicks preserve the label from the preceding `fill_field` so conditional questions stay visible
- `✓` **Bug: focused-snapshot reset at wrong boundary** — label reset moved from per-claim to per-step so fill context from one claim carries into the next within the same step
- `✓` **Bug: JS click hid wrong button** — added `e.offsetParent !== null` visibility filter + exact-before-contains matching in JS fallback
- `✓` **Bug: dynamic REQ IDs stored as assertions** — `is_dynamic_assertion()` filter added to `extract_assertions()` in `fingerprint.py`; 5+ digit sequences skipped at recording time
- `✓` **Cold-run validation** — 32/32 verified from scratch (no fingerprints) with clean expected fields and updated system prompt
- `✓` **Architectural rename** — YAML and code renamed: `steps` → `test_conditions`, `claims` → `steps`, IDs updated throughout (`loader.py`, `agent.py`, `graph.py`, `fingerprint.py`, `report.py`, `run.py`)
- `✓` **Warm-run validation** — 32/32 verified with fingerprints; ⚡ hit: 29, miss: 3, none: 0
- `✓` **BA/QA schema separation** — `navigation`, `setup`, `action`, `input`, `output_capture` moved under `execution:` block; BA layer (`goal`, `description`, `type`, `expected`, `depends_on`, `data`) now clean of developer concerns
- `✓` **Element registry decoupling** — CSS selector strategies extracted to `ivalua_elements.yaml`; `_discover_element()` tries strategies in order, caches winner, prints candidates on miss; Python code is selector-free
- `✓` **Prompt externalization** — system prompt → `auditor/prompts/system_prompt.md`, tool definitions → `auditor/prompts/tool_definitions.yaml`, platform guidance → `auditor/platforms/ivalua_guidance.md`; zero Python changes to tune prompts
- `✓` **Bug: _click_result_row_link intercepting button labels** — added `_INTERACTIVE_LABELS` blocklist in `_platform_click_priority`; "Submit", "Cancel", "Save", "OK" etc. now fall through to `get_by_role("button")` strategies
- `✓` **Bug: _focused_snapshot hiding popup content** — added safety check: if `ancestor_line > len(window)` (hiding more than showing), return full snapshot; fixes POCR popup Submit/Cancel buttons disappearing into `[... N lines above ...]`
