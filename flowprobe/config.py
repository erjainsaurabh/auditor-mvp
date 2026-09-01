from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM ───────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    reasoning_model: str = ""
    fast_model: str = ""
    max_tokens: int = 4096
    cache_system_prompt: bool = True
    max_budget_usd: float = 10.00
    budget_duration: str = "1d"
    # LLM request/response logging — three orthogonal axes:
    #   what:       log_llm_prompts / log_llm_responses  (on/off)
    #   level:      log_llm_level = "debug" | "info"      (where it shows)
    #   truncation: log_message_max_chars / log_response_max_chars  (-1 = full, 0 = header only)
    # For full payload visibility on the console during debugging, set
    # log_llm_level=info and the *_max_chars to -1. Payloads contain DOM snapshots
    # and credential-shaped content, so keep level=debug for normal runs.
    log_llm_prompts: bool = True
    log_llm_responses: bool = True
    log_llm_level: str = "debug"
    log_message_max_chars: int = 0
    log_response_max_chars: int = 0

    # ── App / Browser ─────────────────────────────────────────────────────────
    base_url: str = ""
    login_url: str = ""
    platform: str = ""
    flowprobe_headless: bool = False
    slow_mo_ms: int = 0
    auth_username_field: str = ""
    auth_password_field: str = ""

    # ── Agent ─────────────────────────────────────────────────────────────────
    max_actions_per_claim: int = 0
    max_retries_per_claim: int = 0

    # ── Evidence / Fingerprints ───────────────────────────────────────────────
    evidence_output_dir: str = ""
    fingerprints_dir: str = ""
    screenshot_on_every_action: bool = False
    screenshot_on_verdict: bool = True

    # ── Logging ───────────────────────────────────────────────────────────────
    seq_url: str = ""
    log_file: str = ""

    # ── Object storage ────────────────────────────────────────────────────────
    storage_bucket: str = ""
    storage_endpoint_url: str = ""
    storage_presign_expiry_seconds: int = 604800
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
