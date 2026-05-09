# Auditor MVP — Task Tracker

Status key: `✓ done` · `← current` · `○ pending`

---

## In Progress / Next Up

- `←` **Revisit four agent modes** — assess whether Comprehension, Flow Navigation,
  Verification, and Data Capture should become explicit code boundaries or stay
  implicit. Determine if any mode needs design changes before V1.3 fingerprint
  work begins. Outcome shapes where the fingerprint engine plugs in.

---

## Pending

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

- `○` **V1.3 — Execution fingerprint layer**
  - `fingerprints.yaml` schema and read/write logic
  - Selector extraction in `tools.py` at action-success time (XPath, aria, CSS)
  - Three-tier replay in `agent.py`: primary selector → alternatives → ReAct fallback
  - Confidence matrix (`successes`, `failures`, `confidence`) updated after every run
  - Drift detection: warn when selector confidence drops below 0.7
  - Fingerprint written only on successful ReAct verdict; never on failed/blocked

- `○` **V1.4 — LLM claim extraction**
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

## Spec gaps to address before V2.0

These are in `AUDITOR_AGENT_FRAMEWORK.md` but not yet implemented:

- `capture_from_toast` and `capture_from_field` output capture strategies
- Flow-level evidence trace (step-by-step execution record, not just per-claim)
- Trend tracking across runs (pass rate over time per claim / flow)

---

## Completed

- `✓` **MVP** — flat YAML claims, single ReAct loop per claim, evidence + report
- `✓` **V1.1** — flows → steps → claims hierarchy, shared LLM context within a step, message pruning
- `✓` **V1.2** — session state capture (`current_url`, `page_title`, `url_segment:N`), data handoff between steps
