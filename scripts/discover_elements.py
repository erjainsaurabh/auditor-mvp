#!/usr/bin/env python3
"""
Ivalua element discovery script.

Navigates to a page (after login) and prints all visible Ivalua-semantic
elements with their attributes — use this output to write new entries in
flowprobe/platforms/ivalua_elements.yaml.

Usage:
    python scripts/discover_elements.py --url /en/ord/order_browse/contract_budget
    python scripts/discover_elements.py --url /en/ord/order_browse/contract_budget --test-selector '[name*="cmdSearchBtn"]'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from flowprobe.platforms.ivalua import IvaluaBrowserSession


def discover(url: str, test_selector: str | None) -> None:
    config = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    app = config["app"]

    with IvaluaBrowserSession(
        base_url=app["base_url"],
        headless=False,          # visible browser — easier to inspect
        slow_mo_ms=300,
    ) as session:
        page = session._page

        # --- login ---
        username = app.get("username", "")
        password = app.get("password", "")
        if not username or not password:
            print("ERROR: set username and password in config.yaml under app:")
            sys.exit(1)

        print(f"Navigating to base URL and logging in...")
        session.navigate("/")
        page.wait_for_load_state("networkidle", timeout=15000)

        # Fill login form
        for sel, val in [
            ('input[type="email"], input[name*="user"], #Ecom_User_ID', username),
            ('input[type="password"], #Ecom_Password', password),
        ]:
            try:
                page.locator(sel).first.fill(val, timeout=5000)
            except Exception:
                pass
        try:
            page.locator('button[type="submit"], input[type="submit"], [id*="loginButton"]').first.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"Login click failed: {e}")

        # --- navigate to target ---
        print(f"\nNavigating to {url} ...")
        session.navigate(url)
        page.wait_for_load_state("networkidle", timeout=15000)
        print(f"Page: {page.title()}\n")

        # --- test a specific selector if provided ---
        if test_selector:
            found = page.evaluate(f"!!document.querySelector({repr(test_selector)})")
            print(f"Selector test: {test_selector!r} → {'FOUND ✓' if found else 'NOT FOUND ✗'}\n")

        # --- dump all visible Ivalua-semantic elements ---
        elements = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll(
                '[data-iv-role], [data-iv-control-type-name], ' +
                'button[type="submit"], button[name], ' +
                '[class*="iv-filter"], [class*="iv-button"], ' +
                '[class*="iv-menu"], [class*="iv-chip"]'
            )).filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            return els.map(el => ({
                tag:      el.tagName,
                id:       el.id || '',
                name:     el.getAttribute('name') || '',
                role:     el.getAttribute('data-iv-role') || '',
                ctrlType: el.getAttribute('data-iv-control-type-name') || '',
                type:     el.getAttribute('type') || '',
                ariaLbl:  el.getAttribute('aria-label') || '',
                text:     el.textContent.trim().slice(0, 60),
                ivCls:    Array.from(el.classList).filter(c => c.startsWith('iv-')).join(' '),
                selector: el.id ? '#' + el.id :
                          el.getAttribute('name') ? '[name="' + el.getAttribute('name') + '"]' :
                          el.tagName.toLowerCase() + (el.className ? '.' + el.className.trim().split(/\s+/)[0] : '')
            }));
        }""")

        print(f"Found {len(elements)} visible Ivalua-semantic elements:\n")
        print(f"{'TAG':<10} {'ID/NAME':<45} {'data-iv-role':<20} {'TEXT'}")
        print("-" * 110)
        for el in elements:
            ident = el['id'] or el['name'] or el['ivCls'] or el['ariaLbl'] or '—'
            print(f"{el['tag']:<10} {ident:<45} {el['role'] or el['ctrlType']:<20} {el['text'][:40]!r}")

        print("\n--- Suggested YAML entries ---\n")
        # Group by data-iv-role to suggest registry entries
        seen_roles: dict[str, list] = {}
        for el in elements:
            role = el['role'] or el['ctrlType']
            if role:
                seen_roles.setdefault(role, []).append(el)

        for role, els in seen_roles.items():
            print(f"# {role}:")
            for el in els[:3]:
                if el['name']:
                    print(f"  - '[name=\"{el['name']}\"]'")
                elif el['id']:
                    print(f"  - '#{el['id']}'")
                elif el['ivCls']:
                    print(f"  - '.{el['ivCls'].split()[0]}'")

        input("\nPress Enter to close browser...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Ivalua DOM elements on a page")
    parser.add_argument("--url", required=True, help="Page path to inspect (e.g. /en/ord/order_browse/contract_budget)")
    parser.add_argument("--test-selector", help="CSS selector to test against the page")
    args = parser.parse_args()
    discover(args.url, args.test_selector)


if __name__ == "__main__":
    main()
