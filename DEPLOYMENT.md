# Auditor MVP — Deployment Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Fly.io Machine                    │
│                                                     │
│  Docker Image (baked in)                            │
│  ├── auditor/          application code             │
│  ├── run.py / api.py   entrypoint                   │
│  └── config.yaml       platform config              │
│                                                     │
│  Persistent Volume  →  /app/flows                   │
│  ├── *.fingerprints.yaml   machine-maintained       │
│  └── strategy_stats.yaml   machine-maintained       │
│                                                     │
│  Runtime (per API call)                             │
│  ├── yaml_contents     flow YAML text               │
│  ├── yaml_filenames    logical filenames            │
│  └── data_content      test_data.yaml text          │
└─────────────────────────────────────────────────────┘
```

**Key design decisions:**
- Flow YAMLs and test data are **never stored on the server** — sent in every API call
- Credentials never touch the Docker image
- Fingerprints and strategy stats persist on the volume across runs and restarts
- `max_workers=1` — only one Playwright run at a time per machine

---

## Local Docker (Development)

### Build and run

```bash
# Build image
docker build -t auditor-mvp .

# Run (maps flows/ and evidence/ to local directories)
docker run -d \
  --name auditor-mvp \
  -p 8080:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-api03-... \
  -v $(pwd)/flows:/app/flows \
  -v $(pwd)/evidence:/app/evidence \
  auditor-mvp

# Tail logs
docker logs -f auditor-mvp

# Stop
docker stop auditor-mvp && docker rm auditor-mvp
```

### Rebuild after code changes

```bash
docker stop auditor-mvp && docker rm auditor-mvp
docker build -t auditor-mvp . && docker run -d \
  --name auditor-mvp -p 8080:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-api03-... \
  -v $(pwd)/flows:/app/flows \
  -v $(pwd)/evidence:/app/evidence \
  auditor-mvp
```

---

## Fly.io Deployment

### Prerequisites

```bash
# Install flyctl (macOS)
brew install flyctl

# Authenticate
fly auth login
```

### First-time setup

```bash
cd /path/to/auditor-mvp

# 1. Create the app (do NOT deploy yet)
fly launch --no-deploy
# Prompts:
#   App name:  auditor-mvp         (or any unique name)
#   Region:    iad                 (US East — closest to Ivalua US servers)
#   Postgres:  No
#   Redis:     No
#   fly.toml already exists? → keep existing (say No to overwrite)

# 2. Create the persistent volume (fingerprints + strategy stats)
fly volumes create auditor_flows --region iad --size 1
#   --size 1  = 1GB (fingerprints are tiny YAML files, this is plenty)

# 3. Set the Anthropic API key as a secret
fly secrets set ANTHROPIC_API_KEY=sk-ant-api03-...

# 4. Deploy
fly deploy
#   First deploy: 4-6 min (pulls 2.5GB Playwright base image)
#   Subsequent:   1-2 min (base image cached, only app code changes)
```

### Redeployment (after code changes)

```bash
fly deploy
# Only app code layers rebuild — fast
```

### fly.toml reference

```toml
app            = "auditor-mvp"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[[mounts]]
  source      = "auditor_flows"    # persistent volume name
  destination = "/app/flows"       # mounted path inside container
  initial_size = "1gb"

[http_service]
  internal_port        = 8000
  force_https          = true
  auto_stop_machines   = "stop"   # powers off after ~5 min idle
  auto_start_machines  = true     # wakes on request (~10-15s cold start)
  min_machines_running = 0        # true scale-to-zero

  [http_service.concurrency]
    type       = "requests"
    hard_limit = 50
    soft_limit = 25

[[vm]]
  memory   = "2gb"    # minimum for Chromium; increase to 4gb if OOM
  cpu_kind = "shared"
  cpus     = 2
```

### Secrets

| Secret | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for LLM calls |

```bash
# Set / update a secret
fly secrets set ANTHROPIC_API_KEY=sk-ant-api03-...

# List secrets (shows names only, not values)
fly secrets list

# Remove a secret
fly secrets unset ANTHROPIC_API_KEY
```

---

## API Usage

Base URL: `https://auditor-mvp.fly.dev` (Fly.io) or `http://localhost:8080` (local)

### POST /run — Start a run

Both flow YAML and test data are sent as **content in the request body** — nothing is stored on the server beforehand.

```bash
curl -X POST https://auditor-mvp.fly.dev/run \
  -H "Content-Type: application/json" \
  -d '{
    "yaml_filenames": ["pocr_creation.yaml"],
    "yaml_contents":  ["<full YAML text here>"],
    "data_content":   "app_username: user@example.com\napp_password: secret\nbase_budget: '\''74770'\''\nchange_reason: Automation Testing\n"
  }'

# Response
{ "run_id": "run_a1b2c3d4" }
```

**Request fields:**

| Field | Type | Description |
|---|---|---|
| `yaml_filenames` | `list[str]` | Logical filenames e.g. `["pocr_creation.yaml"]` — used to match fingerprints |
| `yaml_contents` | `list[str]` | Full YAML text for each flow file (same order as filenames) |
| `data_content` | `str` | Full content of test_data.yaml (credentials + test values) |

> `yaml_filenames` and `yaml_contents` must have the same length.

### GET /run/{run_id}/status — Poll status

```bash
curl https://auditor-mvp.fly.dev/run/run_a1b2c3d4/status
```

```json
{
  "run_id":  "run_a1b2c3d4",
  "status":  "done",          // queued | running | done | failed
  "result":  "passed",        // passed | failed | partial  (null until done)
  "summary": {
    "total": 10, "verified": 10, "failed": 0, "blocked": 0, "unverifiable": 0
  },
  "error": null
}
```

**Polling pattern:**

```python
import time, requests

BASE = "https://auditor-mvp.fly.dev"

# Start run
resp = requests.post(f"{BASE}/run", json={
    "yaml_filenames": ["pocr_creation.yaml"],
    "yaml_contents":  [open("flows/pocr_creation.yaml").read()],
    "data_content":   open("flows/test_data.yaml").read(),
})
run_id = resp.json()["run_id"]

# Poll until done
while True:
    status = requests.get(f"{BASE}/run/{run_id}/status").json()
    if status["status"] in ("done", "failed"):
        break
    time.sleep(10)

print(status["result"])   # passed | failed | partial
```

### GET /run/{run_id}/report — Full report

```bash
curl https://auditor-mvp.fly.dev/run/run_a1b2c3d4/report
```

Returns 202 if still running, 500 if failed, 200 with full report JSON when done.

---

## Operations

### Logs

```bash
fly logs                    # live tail
fly logs --no-tail          # last N lines then exit
```

### SSH into container

```bash
fly ssh console             # interactive shell
```

### Inspect persistent volume

```bash
# List volumes
fly volumes list

# Check fingerprints on the volume
fly ssh console
ls /app/flows/

# View strategy stats
cat /app/flows/strategy_stats.yaml
```

### Scale / resize machine

```bash
# Upgrade to 4GB RAM if runs are OOMing
fly scale memory 4096

# Check current machine config
fly status
```

### Destroy and recreate (nuclear reset)

```bash
# Delete the app entirely
fly apps destroy auditor-mvp

# Delete the volume (separate — volumes outlive apps by default)
fly volumes destroy <volume-id>
```

---

## Data Flow (Runtime YAML Mode)

```
Caller
  │
  ├── POST /run { yaml_contents, yaml_filenames, data_content }
  │
  ▼
api.py
  ├── writes YAML   → evidence/run_xyz/staging/pocr_creation.yaml  (ephemeral)
  └── writes data   → evidence/run_xyz/staging/test_data.yaml       (ephemeral)
  │
  ▼
run.py
  ├── reads YAML    from staging (parsed, then discarded after run)
  ├── reads data    from staging (used during run, then discarded)
  ├── fingerprints  → flows/pocr_creation.fingerprints.yaml  ✅ PERSISTENT
  └── strategy_stats→ flows/strategy_stats.yaml              ✅ PERSISTENT
  │
  ▼
report.json  →  returned via GET /run/{run_id}/report
```

---

## Cost Estimate (Fly.io)

| Scenario | Monthly cost |
|---|---|
| 5 runs/day × 2 min each (scale-to-zero) | ~$1–2 |
| 20 runs/day × 3 min each | ~$5–8 |
| Always-on (min_machines_running=1) | ~$30–35 |

Machine: `shared-cpu-2x`, 2GB RAM, US East region.
LLM API costs (Anthropic) are separate and depend on step count / token usage.
