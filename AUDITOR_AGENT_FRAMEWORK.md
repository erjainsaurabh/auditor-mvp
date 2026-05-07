# Auditor Agent Framework
## AI-Powered Spec-Driven Exploratory Testing

> **Core Mental Model:** The agent is an auditor, not an explorer.
> An auditor has a checklist (the spec), explores the system to verify each item,
> gathers evidence for every finding, and reports what's compliant, failed, or unverifiable.
> It is goal-directed, evidence-based, and scoped — not random exploration.

---

## Problem Context

### The Target Use Case

Testing customizations built on top of SaaS platforms (Salesforce, ServiceNow, SAP, Workday) where:

- No code access, no repo access
- No git diff to detect what changed
- Spec exists only as Word documents / design documents
- Two independent moving targets: the base SaaS platform (quarterly releases) + customer customizations
- Test suite diverges from application reality over time with no automated signal

### Why Traditional Approaches Fail

| Problem | Traditional testing | This framework |
|---|---|---|
| No code access | Blocked — needs code to trace impact | Works from UI alone |
| Word doc spec | Ignored — tests written manually | Parsed into structured claims and flows |
| Two moving targets | Both break tests silently | DOM snapshotting detects changes |
| Test suite divergence | Discovered reactively (tests fail) | Proactive claim-driven verification |
| Page-only validation | Flows break silently across pages | Flow graph connects pages into sequences |
| High maintenance volume | Engineers fix broken locators | Agent adapts, humans review findings |

### Key Distinction: Self-Healing vs. This Framework

Self-healing (Mabl/Testim) solves **locator drift** — the element moved but nothing functionally changed.

This framework solves **test suite divergence** — the application behavior changed and the test suite no longer reflects reality. Self-healing cannot fix this. Only re-verification against the spec can.

---

## Verification Hierarchy

The framework operates at four levels. Every level is necessary — page-level claims alone cannot verify that a business process works end to end.

```
Level 1 — End-to-End Flows       (full business process: Lead → Closed Won)
  └── Level 2 — Module Flows     (feature area: Account management lifecycle)
        └── Level 3 — Feature Flows  (single feature: Create Account with Tier)
              └── Level 4 — Page Claims  (atomic checks: Tier field is required)
```

### Why Each Level Matters

| Level | What it catches | What it cannot catch |
|---|---|---|
| Page Claims | Field missing, wrong options, wrong validation | Whether saving actually persists data |
| Feature Flow | Data persisted, redirect works, detail page correct | Whether two features interact correctly |
| Module Flow | Cross-feature interactions within a domain | Whether cross-module handoffs work |
| End-to-End Flow | Full business process integrity | Nothing — this is the final arbiter |

A test suite that only verifies page-level claims will miss entire categories of real bugs.

---

## Framework Architecture: 7 Layers

```
┌──────────────────────────────────────────────────────┐
│  Layer 0: Flow Intelligence                          │
│  Flows → Steps → Session State → Data Handoff        │
├──────────────────────────────────────────────────────┤
│  Layer 1: Spec Layer                                 │
│  Word doc / PDF → claims + flows                     │
├──────────────────────────────────────────────────────┤
│  Layer 2: Two-Level Graph                            │
│  Flow Graph (steps) + Claim Graph (per step)         │
├──────────────────────────────────────────────────────┤
│  Layer 3: Application Model                          │
│  Pages, states, navigation + session context         │
├──────────────────────────────────────────────────────┤
│  Layer 4: ReAct Loop                                 │
│  Core agent execution: Reason → Act → Observe        │
├──────────────────────────────────────────────────────┤
│  Layer 5: Evidence Collector                         │
│  Screenshots, DOM snapshots, flow traces             │
├──────────────────────────────────────────────────────┤
│  Layer 6: Report Layer                               │
│  Verified / Failed / Blocked per claim + per flow    │
└──────────────────────────────────────────────────────┘
```

---

## Layer 0: Flow Intelligence

### Purpose
Connect pages into sequences. Carry data between steps. Verify that business
processes work end to end — not just that individual pages look correct.

### Three Flow Levels

#### Feature Flow (single feature, one user, linear)
```
"Create an Account with Customer Tier = Gold"

Step 1: Navigate to Account Create
Step 2: Fill all fields, set Tier = Gold
Step 3: Save
Step 4: Verify redirect to Account Detail        ← output of step 3 consumed here
Step 5: Verify Tier shows as Gold on detail page
Step 6: Verify Priority badge appears (Gold-tier behavior)
```

#### Module Flow (cross-feature within one domain)
```
"Account management lifecycle"

Flow A: Create Account → captures account_id
Flow B: Edit Account Tier using account_id → captures updated state
Flow C: Deactivate Account → verify absent from active list
Flow D: Reactivate Account → verify reappears
```
Each flow builds on data produced by the previous. Order is mandatory.

#### End-to-End Flow (cross-module business process)
```
"Lead to Closed Won"

Step 1: Create Lead                         → produces lead_id
Step 2: Qualify Lead                        → uses lead_id
Step 3: Convert Lead → Account + Opportunity → produces account_id, opportunity_id
Step 4: Add Products to Opportunity         → uses opportunity_id
Step 5: Request Discount Approval           → uses opportunity_id
Step 6: Manager approves                    → uses approval_id
Step 7: Mark Closed Won                     → uses opportunity_id
Step 8: Verify Invoice triggered            → uses account_id
```

### Flow Schema

```json
{
  "flow_id": "flow_lead_to_closed_won",
  "level": "end_to_end",
  "description": "Full sales cycle from Lead creation to Closed Won",
  "spec_source": "Sales_Process_v4.docx",
  "steps": [
    {
      "step_id": "step_001",
      "goal": "Create a new Lead",
      "input": {},
      "claims": ["claim_lead_001", "claim_lead_002", "claim_lead_003"],
      "output_capture": {
        "lead_id": "capture_from_url_segment(1)"
      }
    },
    {
      "step_id": "step_002",
      "goal": "Qualify and convert Lead to Account and Opportunity",
      "input": {"lead_id": "step_001.output.lead_id"},
      "claims": ["claim_convert_001", "claim_convert_002"],
      "output_capture": {
        "account_id":     "capture_from_url_segment(1)",
        "opportunity_id": "capture_from_related_list('Opportunities', 0)"
      }
    }
  ],
  "status": "not_started",
  "session_data": {}
}
```

### Session State (Data Handoff Between Steps)

This is the most critical concept in flow intelligence. Data created in step N
must be available to step N+1. Without this, flows cannot be connected.

```json
{
  "flow_id": "flow_lead_to_closed_won",
  "run_id": "run_007",
  "current_step": "step_003",
  "session_data": {
    "lead_id":        "00Q5g000004XyZEAU",
    "account_id":     "0015g000003QwAbAAK",
    "opportunity_id": "0065g000003RxBcAAK",
    "logged_in_as":   "sales.rep@org.com",
    "test_data_created": ["lead_id", "account_id", "opportunity_id"]
  }
}
```

Rules for session state:
- Each step declares what it **needs** (input) and what it **produces** (output_capture)
- The agent captures outputs automatically from URLs, page content, or DOM
- If a step fails to capture a required output, all downstream steps are blocked
- Session data is persisted so a failed flow can be resumed from the failed step

### Data Capture Strategies

The agent needs to know how to extract IDs and context from the UI after each action:

| Strategy | When to use | Example |
|---|---|---|
| `capture_from_url` | After save, ID in URL | `/accounts/0015g...` → `account_id` |
| `capture_from_page_title` | Record name shown in header | `"Gold Corp — Account"` |
| `capture_from_field` | ID shown in a field on page | Auto-number field value |
| `capture_from_related_list` | Child record created via parent | First row of Opportunities list |
| `capture_from_toast` | Confirmation message contains ID | `"Record 00Q5g... created"` |

The capture strategy per step is either extracted from the spec by the LLM
or inferred by the agent at runtime based on what it observes.

### Test Data Lifecycle

Flows create real records in the application. The framework must manage this:

```
BEFORE FLOW:   provision test data if needed (seed accounts, users, etc.)
DURING FLOW:   capture IDs of everything created → store in session_data
AFTER FLOW:    clean up all records in test_data_created (in reverse order)
ON FAILURE:    still attempt cleanup → log anything that could not be cleaned
```

Skipping cleanup leads to test data accumulation and eventual interference between runs.

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Flow schema | Pydantic | Typed flow + step definitions |
| Session state | Python dataclass + PostgreSQL | Persist across steps, survives failures |
| Data capture | Playwright + regex + LLM | Extract IDs from URL, DOM, toast messages |
| Test data cleanup | Playwright + API calls (if available) | Reverse-order deletion |
| Flow orchestration | Python (custom) | Drive steps in order, handle step failures |

---

## Layer 1: Spec Parsing → Flows + Claims

### Purpose
Convert unstructured Word/PDF documents into two outputs:
1. **Flows** — ordered sequences of steps describing business processes
2. **Claims** — atomic, typed, verifiable facts about each step

Both are extracted from the same spec document.

### What the LLM Extracts

The spec parser runs two passes over the document:

**Pass 1 — Flow extraction:**
Identify processes described in the spec. Each process becomes a flow with ordered steps.

```
Spec section: "Sales Process"
→ flow: Lead to Closed Won (end_to_end)
  → step: Create Lead
  → step: Qualify Lead
  → step: Convert Lead
  ...
```

**Pass 2 — Claim extraction per step:**
For each step, extract atomic verifiable claims.

```
Step: "Create Lead"
→ claim: Lead Create form is accessible from Leads tab
→ claim: First Name is required
→ claim: Last Name is required
→ claim: Company is required
→ claim: After save, redirect to Lead Detail page
→ claim: Lead owner defaults to logged-in user
```

### Claim Types

| Type | Example | Verification strategy |
|---|---|---|
| **Existence** | "Customer Tier field exists on Account Create" | Navigate → find element |
| **Value** | "Options are Gold, Silver, Bronze" | Inspect element options |
| **Behavioral** | "Customer Tier is required" | Submit without field → expect error |
| **Transition** | "After save, redirect to Account Detail" | Submit → check URL/page |
| **Persistence** | "Saved Tier value appears on detail page" | Create → navigate back → read field |
| **Permission** | "Only Admins see Tier field" | Login as non-admin → field absent |
| **Constraint** | "Account Name must be unique" | Create duplicate → expect error |
| **Cross-module** | "Converting Lead creates an Opportunity" | Convert → check related list |

Note: **Persistence** and **Cross-module** are new claim types that only exist
in a flow context — they cannot be verified by checking a single page.

### Claim Schema

```json
{
  "id": "claim_003",
  "type": "behavioral",
  "description": "Customer Tier is required on Account creation",
  "flow_id": "flow_account_create",
  "step_id": "step_002",
  "location": {
    "page": "Account Create",
    "section": "Custom Fields"
  },
  "verification_strategy": "submit_without_field",
  "expected_outcome": "error message visible, form not submitted",
  "prerequisites": ["claim_001", "claim_002"],
  "requires_session_data": [],
  "produces_session_data": [],
  "status": "not_started",
  "evidence": null,
  "verifiable": true,
  "unverifiable_reason": null
}
```

### Ambiguity Resolution

The agent must classify every claim as verifiable or not. Unverifiable claims
go directly to human review — the agent never guesses.

```
Verifiable:   "Customer Tier is required"
              → submit without it, observe validation error

Unverifiable: "The field should be prominent"
              → subjective, requires human judgment
              → mark UNVERIFIABLE, flag in report, do not attempt
```

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Document parsing | `python-docx`, `pdfplumber` | Read raw text from Word/PDF |
| Flow extraction | LLM (Claude) — Pass 1 | Identify processes and steps |
| Claim extraction | LLM (Claude) — Pass 2 | Extract atomic claims per step |
| Claim classification | LLM or fine-tuned classifier | Type labeling |
| Entity extraction | spaCy NER or LLM | Field names, page names, values |
| Schema validation | Pydantic | Enforce flow + claim schemas |

---

## Layer 2: Two-Level Graph

### Purpose
Model dependencies at two levels:
- **Flow Graph** — flows depend on flows, steps depend on steps
- **Claim Graph** — claims within a step depend on each other

Both use the same cascade logic: a failure at any node blocks all downstream nodes.

### Flow Graph (top level)

```
flow_account_management
  └── step_001: Create Account
        └── step_002: Edit Account Tier      [depends on step_001]
              └── step_003: Deactivate Account  [depends on step_002]
                    └── step_004: Reactivate    [depends on step_003]

flow_lead_to_closed_won
  └── step_001: Create Lead
        └── step_002: Qualify Lead           [depends on step_001]
              └── step_003: Convert Lead     [depends on step_002]
                    └── step_004: Add Products  [depends on step_003]
```

### Claim Graph (per step, nested inside flow graph)

```
step_002: Edit Account Tier
  └── claim_edit_001: Edit button present on Account Detail
        └── claim_edit_002: Tier field editable in edit mode
              └── claim_edit_003: Saving new Tier value persists
                    └── claim_edit_004: Detail page reflects updated Tier
```

### Combined Failure Cascade

```
step_001 FAILED (Account Create fails)
→ session_data.account_id never captured
→ step_002, step_003, step_004 all BLOCKED (no account_id to work with)
→ all claims within step_002, step_003, step_004 also BLOCKED

claim_edit_001 FAILED (Edit button not found)
→ claim_edit_002, claim_edit_003, claim_edit_004 BLOCKED
→ report: 1 root failure, 3 blocked — fix the edit button, re-run
```

### Node States (same at both levels)

```
not_started → in_progress → verified
                          → failed
                          → blocked (prerequisite failed)
                          → unverifiable (ambiguous spec)
```

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Graph structure | `networkx` (Python) | Two DAGs: flow graph + claim graph |
| Dependency detection | LLM reasoning | Infers step and claim dependencies |
| Topological sort | `networkx.topological_sort` | Correct execution order at both levels |
| Persistence | PostgreSQL + SQLAlchemy | Store full graph state across runs |

---

## Layer 3: Application Model

### Purpose
Build a dynamic understanding of pages, states, and navigation paths without
code access. Also tracks session context so the agent knows what data it has
available when executing each step.

### Page Node Schema

```json
{
  "url": "/accounts/new",
  "title": "New Account",
  "page_type": "create_form",
  "discovered_at": "run_001",
  "reachable_via": ["click 'New' on /accounts"],
  "known_fields": [
    {"label": "Account Name", "type": "text", "required": true},
    {"label": "Customer Tier", "type": "picklist", "required": true,
     "options": ["Gold", "Silver", "Bronze"]}
  ],
  "known_actions": ["Save", "Cancel"],
  "snapshot_hash": "a3f9bc..."
}
```

### Agent State (page-level, changes every action)

```json
{
  "current_page": "/accounts/new",
  "logged_in_as": "admin@test.com",
  "filled_fields": {"Account Name": "Test Co"},
  "unfilled_fields": ["Customer Tier", "Industry"],
  "open_modals": [],
  "pending_errors": []
}
```

### Session Context (flow-level, persists across steps)

```json
{
  "flow_id": "flow_lead_to_closed_won",
  "current_step_id": "step_003",
  "session_data": {
    "lead_id":        "00Q5g000004XyZEAU",
    "account_id":     "0015g000003QwAbAAK",
    "opportunity_id": null
  },
  "steps_completed": ["step_001", "step_002"],
  "steps_remaining": ["step_003", "step_004", "step_005"]
}
```

The agent always has access to both. Agent state tells it where it is now.
Session context tells it what data it has collected so far in this flow.

### Change Detection Between Runs

```
Run 5 snapshot: known_fields = [Account Name, Industry]
Run 6 snapshot: known_fields = [Account Name, Industry, Customer Tier]

→ NEW FIELD DETECTED: Customer Tier
→ AFFECTED STEPS: any step whose claims reference Account Create form
→ AFFECTED FLOWS: any flow containing those steps
→ ACTION: re-run affected flows from the affected step forward
```

### SaaS DOM Simplification

Raw SaaS DOM (Salesforce Lightning, ServiceNow) is too noisy for LLM reasoning.
Always extract a semantic representation before passing to the LLM:

```
Raw DOM: 200 lines of nested divs with hash-suffixed classes
         ↓
Semantic: {
  page_type: "create_form",
  fields: [{label: "Customer Tier", type: "picklist", options: [...]}],
  errors: [],
  buttons: ["Save", "Cancel"]
}
```

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Browser automation | `Playwright` (Python) | Navigate, interact, capture |
| DOM extraction | `BeautifulSoup` | Parse raw HTML |
| DOM simplification | LLM | Convert noisy DOM → semantic summary |
| Page classification | LLM | "This is a create form, not a list" |
| Visual hashing | `imagehash` + `Pillow` | Detect visual changes between runs |
| Agent state | Python dataclass | In-memory, per action |
| Session context | Python dataclass + PostgreSQL | Persisted, per flow |

---

## Layer 4: ReAct Loop (Core Agent Engine)

### Purpose
The primary execution engine. Operates at two levels:
- **Flow level** — orchestrates steps in order, manages session state
- **Claim level** — verifies individual claims within each step

### Four Agent Modes

```
COMPREHENSION MODE      (once, at session start)
  Parse flow graph + claim graph
  Determine execution order at both levels
  Initialize session context

FLOW NAVIGATION MODE    (between steps)
  Goal: advance from current step to next step
  Actions: use session_data to navigate to the right record/page
  Key challenge: constructing correct URLs using captured IDs

VERIFICATION MODE       (per claim within a step)
  Goal: verify one specific claim
  Actions: targeted interactions → observe → record verdict

DATA CAPTURE MODE       (at end of each step)
  Goal: extract and store outputs declared in step.output_capture
  Actions: read URL, read DOM, read toast messages
  Failure here blocks all downstream steps
```

### ReAct Loop — Flow Level

```
FOR each flow in flow_graph (topological order):

  initialize session_context for this flow

  FOR each step in flow.steps (topological order):

    REASON:
      What does this step need from session_data?
      Are all required inputs available?
      How do I navigate to the right page for this step?

    NAVIGATE:
      Use session_data to reach the correct page
      (e.g., /accounts/{session_data.account_id}/edit)

    EXECUTE CLAIMS:
      Run claim-level ReAct loop for all claims in this step

    CAPTURE OUTPUTS:
      Extract step.output_capture values from current page
      Store in session_context.session_data

    IF capture fails:
      Mark step FAILED
      Mark all downstream steps BLOCKED
      BREAK

  AFTER all steps:
    Trigger test data cleanup (reverse order)
    Record flow-level verdict
```

### ReAct Loop — Claim Level

```
WHILE unchecked claims remain for current step:

  REASON:
    What is the next claim? (topological order within step)
    What state do I need to be in?
    What minimal actions will verify this claim?
    What should I observe?

  ACT:
    Execute one browser action

  OBSERVE:
    Read DOM semantic summary
    Check URL and page title
    Look for errors, confirmations, redirects
    Take screenshot

  DECIDE:
    Does observation match expected outcome?
      YES   → mark VERIFIED, attach evidence
      NO    → mark FAILED, attach evidence, cascade to dependents
      UNSURE → retry (max 3 times) → mark BLOCKED

  UPDATE:
    Update application model
    Update claim graph
    Log action to evidence store
```

### Agent Tools (Browser Actions Exposed to LLM)

```python
tools = [
    # Navigation
    {
        "name": "navigate",
        "description": "Go to a URL — supports {session_data.key} interpolation",
        "parameters": {"target": "string"}
    },
    {
        "name": "read_page",
        "description": "Get semantic summary of current page",
        "parameters": {}
    },

    # Interaction
    {
        "name": "click",
        "description": "Click an element described in natural language",
        "parameters": {"element_description": "string"}
    },
    {
        "name": "fill_field",
        "description": "Type a value into a named field",
        "parameters": {"field_label": "string", "value": "string"}
    },
    {
        "name": "clear_field",
        "description": "Clear a field's current value",
        "parameters": {"field_label": "string"}
    },
    {
        "name": "submit_form",
        "description": "Submit the current form",
        "parameters": {}
    },
    {
        "name": "get_field_options",
        "description": "Get all available options in a dropdown or picklist",
        "parameters": {"field_label": "string"}
    },

    # Session data
    {
        "name": "capture_from_url",
        "description": "Extract a value from the current URL and store in session_data",
        "parameters": {"key": "string", "url_segment_index": "integer"}
    },
    {
        "name": "capture_from_page",
        "description": "Extract a value from page content and store in session_data",
        "parameters": {"key": "string", "element_description": "string"}
    },
    {
        "name": "read_session_data",
        "description": "Read current session context — what IDs have been captured",
        "parameters": {}
    },

    # Evidence and verdict
    {
        "name": "take_screenshot",
        "description": "Capture the current page as evidence",
        "parameters": {"label": "string"}
    },
    {
        "name": "verify_claim",
        "description": "Record a verdict for the current claim",
        "parameters": {
            "claim_id": "string",
            "verdict": "verified | failed | blocked",
            "confidence": "high | medium | low",
            "reasoning": "string"
        }
    },
    {
        "name": "record_step_output",
        "description": "Mark current step complete and store its outputs",
        "parameters": {
            "step_id": "string",
            "verdict": "verified | failed",
            "captured_data": "object"
        }
    }
]
```

### Confidence Levels

| Confidence | Meaning | Action |
|---|---|---|
| **High** | Agent clearly observed pass or fail | Accept verdict automatically |
| **Medium** | Uncertain due to timing or dynamic content | Accept but flag for spot-check |
| **Low** | Agent guessed or couldn't reach right state | Always route to human review |

### Scope Control

```
Max actions per claim:      20
Max retries per claim:      3
Max steps per flow session: configured per flow
Timeout per flow:           configured per flow
Scope boundary:             only execute flows and claims in the graph
```

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| LLM reasoning | LiteLLM → reasoning model | Provider + model set in config.yaml only |
| DOM extraction | LiteLLM → fast model | Cheaper model for simple extraction tasks |
| Tool execution | Playwright | Browser actions |
| Flow orchestration | Python (custom) | Step sequencing, session state |
| URL interpolation | Python `str.format_map` | `{session_data.account_id}` in nav targets |
| Retry logic | Rule-based | Max retries, then BLOCKED |

---

## Layer 5: Evidence Collector

### Purpose
Every finding — pass or fail — must have attached evidence at both the claim level
and the flow level. A report without evidence is just an opinion.

### Claim-Level Evidence

```json
{
  "claim_id": "claim_003",
  "step_id": "step_002",
  "flow_id": "flow_account_create",
  "run_id": "run_007",
  "action_sequence": [
    "navigate('/accounts/new')",
    "fill_field('Account Name', 'Test Account')",
    "submit_form()"
  ],
  "screenshot_before": "evidence/run_007/flow_account_create/step_002/claim_003_before.png",
  "screenshot_after":  "evidence/run_007/flow_account_create/step_002/claim_003_after.png",
  "dom_snapshot":      "evidence/run_007/flow_account_create/step_002/claim_003_dom.json",
  "observed":          "Form submitted successfully, no error shown",
  "expected":          "Validation error visible, form not submitted",
  "verdict":           "failed",
  "confidence":        "high",
  "timestamp":         "2026-05-06T14:32:01Z"
}
```

### Flow-Level Trace

Each flow also has an end-to-end trace — a sequential record of every step,
what data was captured, and where the flow succeeded or broke:

```json
{
  "flow_id": "flow_lead_to_closed_won",
  "run_id": "run_007",
  "overall_verdict": "failed",
  "failed_at_step": "step_003",
  "steps": [
    {
      "step_id": "step_001",
      "verdict": "verified",
      "session_data_captured": {"lead_id": "00Q5g000004XyZEAU"},
      "duration_seconds": 12
    },
    {
      "step_id": "step_002",
      "verdict": "verified",
      "session_data_captured": {},
      "duration_seconds": 8
    },
    {
      "step_id": "step_003",
      "verdict": "failed",
      "failed_claim": "claim_convert_002",
      "session_data_captured": {},
      "duration_seconds": 15,
      "screenshot": "evidence/run_007/flow_lead_to_closed_won/step_003_failure.png"
    },
    {
      "step_id": "step_004",
      "verdict": "blocked",
      "blocked_by": "step_003"
    }
  ]
}
```

### Visual Diffing (Two Levels)

**Level 1 — Perceptual hash (no ML, fast):**
```python
from PIL import Image
import imagehash

h1 = imagehash.phash(Image.open("run_006_page.png"))
h2 = imagehash.phash(Image.open("run_007_page.png"))
diff = h1 - h2  # Hamming distance
# diff > 10 → significant visual change, flag for review
```

**Level 2 — CNN classification (ML, build later):**
```
Input:  before + after screenshot pair
Output: "regression" | "intentional_change"
Model:  fine-tuned ResNet or ViT
Data:   labeled before/after pairs from real runs
```

Start with Level 1. Add Level 2 when labeled data exists (50+ examples minimum).

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Screenshots | Playwright `page.screenshot()` | Before/after each key action |
| DOM snapshots | Playwright `page.content()` | Frozen DOM at verdict time |
| Action logging | Python `logging` | Full action sequence per claim |
| Flow trace | PostgreSQL JSON | Step-by-step flow execution record |
| Visual diff | `imagehash`, `Pillow` | Perceptual hash, not pixel diff |
| Visual classification | PyTorch + ResNet | Optional, Phase 3 |
| Storage | PostgreSQL (blobs) or S3 | Organised by run / flow / step / claim |

---

## Layer 6: Report Layer

### Purpose
Communicate findings at two levels: flow-level summary for business stakeholders,
claim-level detail for engineers. Four buckets at each level.

### Flow-Level Report (stakeholder view)

```
FLOW SUMMARY — run_007 — 2026-05-06
  Spec: Sales_Process_v4.docx

  END-TO-END FLOWS        Total: 2
    ✓ flow_quote_to_order      All 6 steps verified
    ✗ flow_lead_to_closed_won  Failed at step 3 of 8
      Root cause: Lead conversion does not create Opportunity
      Blocked: steps 4, 5, 6, 7, 8

  MODULE FLOWS            Total: 3
    ✓ flow_account_lifecycle   All 4 steps verified
    ✓ flow_contact_management  All 3 steps verified
    ✗ flow_opportunity_stages  Failed at step 2 of 5

  FEATURE FLOWS           Total: 8
    6 verified, 1 failed, 1 blocked
```

### Claim-Level Report (engineer view)

```
FAILED CLAIMS (action required)

  flow_lead_to_closed_won / step_003 / claim_convert_002
  ✗ Opportunity not created on Lead conversion
    Observed: Opportunities related list is empty after conversion
    Expected: One Opportunity record created
    Evidence: evidence/run_007/.../claim_convert_002_after.png

BLOCKED CLAIMS

  ⊘ flow_lead_to_closed_won / step_004 through step_008
    All blocked by: step_003 failure
    Action: fix Lead conversion, re-run from step_003

VERIFIED CLAIMS (18)
  ✓ claim_lead_001: Lead Create page accessible from Leads tab
  ✓ claim_lead_002: Last Name is required
  ... (16 more)

UNVERIFIABLE CLAIMS (human review needed)
  ? "The conversion experience should feel seamless"
  ? "Approval notifications should arrive promptly"
```

### Trend Tracking (Across Runs)

```
          Flow pass rate    Claim pass rate
Run 1:    50%               67%
Run 2:    75%  ↑            78%  ↑   ← Lead conversion fixed
Run 3:    75%               89%  ↑   ← validation fixes
Run 4:    100% ↑            96%  ↑   ← all flows passing
```

### Technical Stack

| Component | Technology | Notes |
|---|---|---|
| Data aggregation | Python + `pandas` | Summarize at flow + claim level |
| Report rendering | `Jinja2` | Separate stakeholder + engineer templates |
| Trend tracking | PostgreSQL time series | Pass rate over runs at both levels |
| LLM summary | Claude (optional) | Natural language narrative of findings |

---

## Technology Stack Summary

```
Layer               Primary Tech                  ML/AI Component
─────────────────   ──────────────────────────    ────────────────────────────────
Flow Intelligence   Pydantic (flow schema)         No ML
                    PostgreSQL (session state)     No ML
                    Playwright (data capture)      No ML
                    Python (flow orchestration)    No ML

Spec Parsing        python-docx, pdfplumber        LLM — flow + claim extraction
                    spaCy                          NER — entity extraction
                    Pydantic                       Schema validation (not ML)

Two-Level Graph     networkx (flow + claim DAGs)   LLM — dependency detection
                    PostgreSQL, SQLAlchemy          No ML

Application Model   Playwright                     LLM — DOM interpretation
                    BeautifulSoup                  No ML
                    imagehash, Pillow              No ML (perceptual hash)

ReAct Loop          LiteLLM (provider abstraction)  Swap model/provider via config only
                    Claude Sonnet (reasoning)       Default reasoning model
                    Claude Haiku (fast tasks)       Default extraction model
                    Playwright (as tools)           No ML
                    Python (flow orchestration)     No ML

Evidence            Playwright                     No ML
                    OpenCV, Pillow                 No ML (image processing)
                    PyTorch + ResNet               CNN — visual diff (Phase 3)
                    PostgreSQL / S3                No ML

Report              Jinja2, pandas                 LLM summary (optional)
```

---

## LLM Provider Strategy

### Why an Abstraction Layer

Hardcoding the Anthropic SDK into agent logic means every model or provider change
touches core code. The agent should never know which LLM it is talking to — that
is a deployment decision, not a code decision.

```
agent.py  →  LLMClient (your thin wrapper)  →  LiteLLM  →  any provider
```

Provider and model are config values. Nothing else changes.

### LiteLLM as the Abstraction Layer

LiteLLM provides a single unified interface over 100+ LLM providers. Tool use,
streaming, and response format differences between providers are handled internally.

```python
# llm_client.py — the only file that knows about providers

from litellm import completion

class LLMClient:
    def __init__(self, config):
        self.reasoning_model = config["llm"]["reasoning_model"]
        self.fast_model      = config["llm"]["fast_model"]
        self.system_prompt   = self._build_cached_system_prompt()

    def reason(self, messages, tools):
        # Used for: ReAct loop, verdict reasoning, flow planning
        return completion(
            model=self.reasoning_model,
            messages=messages,
            tools=tools,
            system=self.system_prompt   # cached prompt — paid once per session
        )

    def extract(self, prompt):
        # Used for: DOM simplification, page classification, entity extraction
        return completion(
            model=self.fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
```

### config.yaml Controls Everything

```yaml
llm:
  # Change these two lines to switch provider or model — nothing else changes

  reasoning_model: "claude-sonnet-4-6"             # direct Anthropic (MVP default)
  fast_model:      "claude-haiku-4-5-20251001"     # direct Anthropic (MVP default)

  # Future options — uncomment to switch, no code changes needed:
  # reasoning_model: "bedrock/claude-sonnet-4-6"   # Claude via AWS Bedrock
  # reasoning_model: "bedrock/llama3-70b"          # Llama via AWS Bedrock
  # reasoning_model: "azure/gpt-4o"               # Azure OpenAI
  # fast_model:      "ollama/llama3"               # local, zero cost (dev only)

  max_tokens: 4096
  cache_system_prompt: true
  max_budget_usd: 10.00       # hard spend limit per run
  budget_duration: "1d"       # reset daily
```

### Model Routing — Right Model for the Right Task

Not every operation needs the same intelligence. Routing saves 40-60% of cost.

| Task | Model tier | Why |
|---|---|---|
| ReAct reasoning + verdict | Reasoning model (Sonnet) | Needs judgment and multi-step thinking |
| Flow planning + step navigation | Reasoning model (Sonnet) | Needs understanding of context |
| DOM semantic simplification | Fast model (Haiku) | Simple extraction, no reasoning needed |
| Page classification | Fast model (Haiku) | Pattern matching, not reasoning |
| Claim dependency detection | Reasoning model (Sonnet) | Needs to understand relationships |
| Report summarisation | Fast model (Haiku) | Simple text generation |

### Prompt Caching

System prompt and tool definitions are identical on every LLM call within a session.
Caching them means they are paid for once, not on every iteration.

```python
# System prompt marked for caching — Anthropic charges 10% for cache hits
# vs 100% for uncached. Saves ~40% on input token cost across a full run.
system_prompt = [{
    "type": "text",
    "text": "You are a QA auditor verifying...",
    "cache_control": {"type": "ephemeral"}
}]
```

**Important:** Prompt caching works fully with direct Anthropic. When using
AWS Bedrock, verify current caching support before relying on it for cost estimates.

### Cost Control Levers (in priority order)

| Lever | Typical saving | When to apply |
|---|---|---|
| Prompt caching | 30-40% | Always — enable from day one |
| Model routing (Haiku for simple tasks) | 20-30% | Phase 1 onwards |
| DOM compression before LLM | 50-80% token reduction | Always |
| Claim batching by page | 30-40% fewer calls | Phase 2 onwards |
| Hard budget per run | Cost cap | Always |
| Skip blocked claims early | Variable | Automatic via claim graph |

### Provider Extensibility Roadmap

```
MVP (Phase 1):
  Direct Anthropic — simplest, best caching support, lowest cost

Phase 2:
  Add AWS Bedrock option — enterprise customers, data residency
  Same code, change config.yaml model strings to bedrock/...

Phase 3:
  Multi-model routing — Bedrock for enterprise, Anthropic direct for dev
  Parallel flow execution — one LiteLLM client per concurrent flow agent
  Azure OpenAI — if customer mandates Microsoft stack
  Local Ollama — zero-cost dev/CI environment
```

### Why Not LangChain for This

LangChain provides similar model abstraction but adds problems that outweigh
the benefit for this specific use case:

- Its agent abstractions (AgentExecutor) are opinionated and fight custom ReAct loops
- Prompt caching requires workarounds rather than being first-class
- Heavy dependency tree (47+ transitive deps vs LiteLLM's 12)
- History of breaking changes across versions

LangChain is excellent for RAG pipelines and document retrieval. It is not the
right tool for a browser-based ReAct agent where loop control is critical.
If RAG is needed in a future phase (e.g., spec retrieval), LangChain can be
added for that specific purpose without replacing LiteLLM in the agent loop.

---

## MVP Approach

### Principle

Build the smallest thing that proves the agent can verify a claim against a
real UI. Everything else is an extension on top of a working core.

```
Input:   claims.yaml   (human written — no LLM extraction yet)
Process: LiteLLM + Playwright ReAct loop
Output:  report.json   + screenshots per claim
```

### What the MVP Cuts

| Full framework component | MVP decision | Reason to defer |
|---|---|---|
| Word doc / LLM claim extraction | Manual YAML instead | Fragile to build before loop works |
| Flow intelligence + session state | Single page claims only | Validate core loop first |
| Two-level graph | Flat claim list with depends_on | Flows come after page claims work |
| SaaS DOM simplification | Generic simplification only | Platform-specific is Phase 2 |
| Visual diff / CNN | Screenshots as evidence only | Sufficient for MVP reporting |
| Test data cleanup | Manual cleanup | Automate once flows are added |
| PostgreSQL | SQLite | No multi-user, no scale yet |
| HTML report | Console output + report.json | Readable without a template engine |
| Multi-role testing | Single user session | One role is enough to prove the loop |

### MVP File Structure

```
auditor-mvp/
├── config.yaml           # base URL, credentials, LLM model config
├── claims.yaml           # input: human-written claims
├── run.py                # entrypoint: load → run → report
├── loader.py             # parse + validate claims.yaml (Pydantic)
├── graph.py              # flat dependency sort + cascade logic (networkx)
├── agent.py              # ReAct loop — LiteLLM + Playwright tools
├── tools.py              # browser action implementations (Playwright)
├── llm_client.py         # thin LiteLLM wrapper — only file that knows providers
├── evidence.py           # screenshot saving + action log per claim
├── report.py             # write report.json + print console summary
├── evidence/             # screenshots created at runtime (gitignored)
└── report.json           # output (gitignored)
```

~500 lines of Python total. No database. No flow schema. No Word parsing.

### MVP Claim Format (YAML)

```yaml
# claims.yaml
config:
  base_url: "https://yourapp.com"
  login_url: "https://yourapp.com/login"

claims:
  - id: claim_001
    description: Account Create page is reachable
    type: existence
    navigation: "/accounts/new"
    expected: "page title contains 'New Account'"

  - id: claim_002
    description: Customer Tier field exists on Account Create
    type: existence
    navigation: "/accounts/new"
    expected: "field with label 'Customer Tier' is visible"
    depends_on: [claim_001]

  - id: claim_003
    description: Customer Tier is required
    type: behavioral
    navigation: "/accounts/new"
    setup:
      - fill_field: {label: "Account Name", value: "Test Account"}
    action: submit_form
    expected: "validation error visible for Customer Tier"
    depends_on: [claim_001, claim_002]

  - id: claim_004
    description: Customer Tier options are exactly Gold Silver Bronze
    type: value
    navigation: "/accounts/new"
    expected: "Customer Tier picklist has exactly options: Gold, Silver, Bronze"
    depends_on: [claim_002]
```

### MVP Build Order

```
Week 1 — Browser automation foundation
  Day 1-2:  tools.py     Playwright wrapper: read_page, click, fill_field,
                         submit_form, get_field_options, take_screenshot
  Day 3:    loader.py    Parse + Pydantic-validate claims.yaml
  Day 4:    graph.py     Topological sort + cascade (networkx, flat list)
  Day 5:    evidence.py  Save screenshots, log action sequence per claim

Week 2 — Agent working end to end
  Day 1:    llm_client.py  LiteLLM wrapper, config.yaml wired in
  Day 2-3:  agent.py       ReAct loop: LiteLLM reasons, calls tools, gives verdict
  Day 4:    report.py      Write report.json, print console summary
  Day 5:    run.py         Wire everything together, test against real target
```

After week 2 you have a working agent verifying real claims against a real UI.

### MVP Extension Roadmap

Each version is additive. Nothing from the previous version is rewritten.

```
MVP     Manual YAML claims, single page, flat claim list, console report
  │
  ▼
V1.1    Simple feature flows
        Ordered steps in YAML, agent runs them sequentially
        No session state yet — each step navigates independently

  │
  ▼
V1.2    Session state + data handoff
        capture_from_url / capture_from_page tools added
        {session_data.account_id} interpolation in navigation targets
        Failed capture blocks downstream steps automatically

  │
  ▼
V1.3    LLM claim extraction
        Paste spec text → LLM generates claims.yaml
        Manual writing replaced, everything else stays identical

  │
  ▼
V1.4    Word doc + PDF parsing
        python-docx / pdfplumber feeds into LLM extraction
        Two-pass: flows first, then claims per step

  │
  ▼
V1.5    SaaS DOM simplification
        Platform-specific semantic extractors (Salesforce Lightning, ServiceNow)
        Replaces generic DOM extraction in tools.py

  │
  ▼
V2.0    Full framework
        Two-level flow + claim graph, HTML report, visual diffing,
        multi-role sessions, test data cleanup, PostgreSQL, parallel flows
```

---

## Build Phases

### Phase 1 — MVP (~2 weeks, no ML, pure engineering)

Goal: working agent that verifies YAML-defined claims against a real UI.

- [ ] `config.yaml` — base URL, credentials, LLM model strings
- [ ] `llm_client.py` — LiteLLM wrapper (reasoning model + fast model, config-driven)
- [ ] `tools.py` — Playwright wrapper: read_page, click, fill_field, submit_form, get_field_options, take_screenshot
- [ ] `loader.py` — parse + Pydantic-validate claims.yaml
- [ ] `graph.py` — flat dependency sort + cascade (networkx)
- [ ] `agent.py` — ReAct loop: LiteLLM reasons, calls tools, gives verdict
- [ ] `evidence.py` — save screenshots, log action sequence per claim
- [ ] `report.py` — write report.json + console summary
- [ ] `run.py` — entrypoint wiring everything together
- [ ] Test against a real target application

Deliverable: `python run.py claims.yaml` → pass/fail report with screenshots.

### Phase 2 — Flow Intelligence (V1.1 → V1.2, no ML)

Goal: connect pages into sequences, carry data between steps.

- [ ] Flow schema + step schema (Pydantic)
- [ ] Flow graph with networkx (two-level DAG — flow graph + claim graph per step)
- [ ] Session state dataclass + SQLite persistence (upgrade to PostgreSQL when needed)
- [ ] Data capture tools: `capture_from_url`, `capture_from_page`, `capture_from_toast`
- [ ] URL interpolation: `{session_data.account_id}` in navigation targets
- [ ] Flow orchestration in `agent.py` — step sequencing, session data handoff
- [ ] Test data cleanup (reverse-order deletion after each flow)
- [ ] Flow trace in evidence (step-by-step execution record)
- [ ] Report extended to flow level (stakeholder view + engineer view)

### Phase 3 — LLM Intelligence (V1.3 → V1.5, LLM-powered, no custom training)

Goal: replace manual YAML writing with LLM extraction from real spec documents.

- [ ] Spec parser Pass 1: LLM extracts flows + steps from plain text spec
- [ ] Spec parser Pass 2: LLM extracts claims per step
- [ ] `python-docx` + `pdfplumber` integration for Word/PDF input
- [ ] DOM interpretation: LLM converts SaaS DOM to semantic summary
- [ ] Dependency detection: LLM infers step + claim dependencies from spec
- [ ] Data capture strategy inference: LLM decides how to capture step outputs
- [ ] Confidence scoring per verdict
- [ ] Platform-specific DOM simplification: Salesforce Lightning, ServiceNow

### Phase 4 — Learning (custom ML, requires labeled data from real runs)

Goal: improve accuracy using data accumulated across runs.

- [ ] Weight learning for locator scoring (logistic regression, scikit-learn)
- [ ] Visual diff classification (CNN, PyTorch) — needs 50+ labeled before/after pairs
- [ ] Fine-tuned claim extractor — needs labeled spec examples
- [ ] Per-platform DOM model — Salesforce, ServiceNow specific
- [ ] Capture strategy model — learns which capture method works per SaaS platform

> Phase 1 proves the concept.
> Phase 1 + 2 handles real feature and E2E flows.
> Phase 1 + 2 + 3 handles real spec documents without manual claim writing.
> Phase 4 is optimisation — do not start until you have 50+ labeled examples from real runs.

---

## Key Design Decisions

### 1. Four-level verification hierarchy is non-negotiable
Page claims alone cannot verify business processes. A system that only checks
pages will miss entire categories of real bugs — data not persisted, flows that
break at handoff between modules, cross-module state corruption.

### 2. Session state is the spine of flow intelligence
Without captured IDs flowing from step to step, flows cannot be connected.
Session state must be persisted (not just in-memory) so failed flows can be
resumed without re-running completed steps.

### 3. Two-level cascade before reporting
Resolve failures at the flow graph level first, then the claim graph level.
A step failure blocking downstream steps is distinct from a claim failure
blocking downstream claims. Both must cascade correctly before generating the report.

### 4. Claim graph is the scope boundary
The agent only verifies what is in the claim graph. It does not explore freely.
This prevents scope explosion and makes runs deterministic and repeatable.

### 5. DOM simplification before LLM reasoning
Never pass raw SaaS DOM to the LLM. Always extract a semantic summary first.
Raw DOM is too large and too noisy. Semantic summaries are small and precise.

### 6. Evidence is mandatory at both levels
A claim cannot be marked verified or failed without attached evidence.
A flow must have a complete step trace. Low-confidence verdicts always go
to human review regardless of outcome.

### 7. Test data cleanup is part of the flow, not an afterthought
Every record created during a flow must be tracked and cleaned up after.
Skipping cleanup causes test data accumulation and inter-run interference,
which corrupts future test results.

### 8. Unverifiable claims are a first-class output
Ambiguous spec language is a finding, not an error. The report tells spec
authors exactly which claims need to be rewritten to be testable.

### 9. LLM provider is a config value, never a code dependency
`agent.py` must never import `anthropic`, `boto3`, or any provider SDK directly.
All LLM calls go through `llm_client.py` which wraps LiteLLM.
Provider and model strings live only in `config.yaml`.
Switching from Anthropic to AWS Bedrock, Azure OpenAI, or a local Ollama model
must require changing `config.yaml` only — zero code changes.

### 10. Right-size the model for the task
Reasoning tasks (ReAct loop, verdict, flow planning) use the reasoning model.
Extraction tasks (DOM simplification, page classification) use the fast model.
This routing is enforced in `llm_client.py` via `reason()` vs `extract()` methods.
Components must call the correct method — they do not choose the model directly.

---

## Open Problems (for future iterations)

- **Multi-role flows**: some flows require switching user mid-flow (e.g., sales rep creates, manager approves). Requires managing multiple authenticated sessions simultaneously.
- **SaaS platform specificity**: Salesforce Lightning, ServiceNow, SAP Fiori each need their own DOM interpretation layer and data capture strategies.
- **Spec versioning**: when the Word doc changes between runs, which flows and claims changed? Diffing natural language specs is hard.
- **Parallel flows**: some E2E processes have parallel branches (two approvers must both approve). The current sequential step model does not handle this.
- **Regression classification**: when a previously verified flow starts failing, determine whether the regression is from a platform update or a customization change.
- **Script generation**: once the agent has verified a flow end to end, can it emit a reusable Playwright script for that flow? Bridge to traditional test automation.
- **Seeding complex prerequisites**: some flows require data that cannot be created through the UI (e.g., legacy records, bulk imports). Needs an API-based or DB-level seeding strategy.
