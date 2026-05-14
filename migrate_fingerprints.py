"""One-time migration: replace literal test-data values in fingerprint files
with {{key}} placeholder references.

Usage:
    python migrate_fingerprints.py                         # uses test_data.yaml
    python migrate_fingerprints.py --data other_data.yaml  # explicit data file

For every fill_field action in every fingerprint, if the stored value is an
exact case-insensitive match for a test_data.yaml entry, it is replaced with
{{key}}.  This makes fingerprints resilient to test_data.yaml changes.

Partial / abbreviation matches (e.g. "NYC" for "Department of Homeless Services")
are left unchanged — they cannot be reliably mapped to a single key.

Run from the project root.  Safe to run multiple times (idempotent).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def _load_test_data(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _templatize_fingerprint_file(fp_path: Path, test_data: dict[str, str]) -> int:
    """Update fp_path in-place. Returns count of substitutions made."""
    if not fp_path.exists():
        return 0

    raw = yaml.safe_load(fp_path.read_text()) or {}
    steps: dict = raw.get("steps") or {}
    substitutions = 0

    # Build reverse map: value_lower → key  (longer values take precedence)
    value_to_key: dict[str, str] = {}
    for key, val in test_data.items():
        if key.startswith("_") or not val:
            continue
        vl = val.strip().lower()
        # Keep the entry with the longest value when multiple keys share a value
        if vl not in value_to_key or len(val) > len(test_data.get(value_to_key[vl], "")):
            value_to_key[vl] = key

    for step_id, step_data in steps.items():
        actions = step_data.get("actions") or []
        for action in actions:
            tool = action.get("tool")
            args = action.get("args") or {}

            if tool == "fill_field":
                val = args.get("value", "")
                if "{{" in val:
                    continue
                matched_key = value_to_key.get(val.strip().lower())
                if matched_key:
                    old = val
                    args["value"] = f"{{{{{matched_key}}}}}"
                    print(f"  {fp_path.name}  {step_id}  fill_field '{args.get('field_label', '?')}': "
                          f"{old!r} → {{{{{matched_key}}}}}")
                    substitutions += 1

            elif tool == "select_option":
                val = args.get("option_value", "")
                if "{{" in val:
                    continue
                matched_key = value_to_key.get(val.strip().lower())
                if matched_key:
                    old = val
                    args["option_value"] = f"{{{{{matched_key}}}}}"
                    print(f"  {fp_path.name}  {step_id}  select_option '{args.get('field_label', '?')}': "
                          f"{old!r} → {{{{{matched_key}}}}}")
                    substitutions += 1

    if substitutions:
        fp_path.write_text(
            yaml.dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )

    return substitutions


def main() -> None:
    args = sys.argv[1:]
    data_file = Path("test_data.yaml")
    if "--data" in args:
        idx = args.index("--data")
        data_file = Path(args[idx + 1])

    test_data = _load_test_data(data_file)
    if not test_data:
        print(f"No test data found at {data_file} — nothing to do.")
        return

    # Exclude credential keys — we never want passwords in fingerprints
    import re
    _sensitive = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)
    test_data = {k: v for k, v in test_data.items() if not _sensitive.search(k)}

    print(f"Loaded {len(test_data)} test-data keys from {data_file}")
    print(f"Keys: {list(test_data.keys())}\n")

    root = Path(__file__).parent
    fp_files = list(root.glob("*.fingerprints.yaml")) + list(root.glob("flows/*.fingerprints.yaml"))

    total = 0
    for fp_path in sorted(fp_files):
        count = _templatize_fingerprint_file(fp_path, test_data)
        total += count
        if count == 0:
            print(f"  {fp_path.name}  — no changes")

    print(f"\nDone. {total} substitution(s) across {len(fp_files)} file(s).")


if __name__ == "__main__":
    main()
