from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, Page, Playwright, sync_playwright


@dataclass
class BrowserSession:
    base_url: str
    headless: bool = True
    slow_mo_ms: int = 0
    _playwright: Playwright | None = field(default=None, init=False, repr=False)
    _browser: Browser | None = field(default=None, init=False, repr=False)
    _page: Page | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
        )
        self._page = self._browser.new_page()

    def stop(self) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    @property
    def page(self) -> Page:
        assert self._page is not None, "BrowserSession not started"
        return self._page

    def current_url(self) -> str:
        try:
            return self._page.url
        except Exception:
            return ""

    # --- tools exposed to the LLM ---

    def navigate(self, target: str) -> str:
        if not target or target == "/":
            url = self.base_url
        elif target.startswith("http"):
            url = target
        else:
            # SPA pattern: paths append to base URL (e.g. page.aspx/en/ctr/...)
            url = self.base_url.rstrip("/") + "/" + target.lstrip("/")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"navigated to {url}"
        except Exception as e:
            return f"error navigating to {url}: {e}"

    def read_page(self) -> str:
        try:
            self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            snapshot = self._page.aria_snapshot()
            url = self._page.url
            title = self._page.title()
            trimmed = _trim_table_rows(snapshot, max_rows=5)
            return f"url: {url}\ntitle: {title}\n\n{trimmed}"
        except Exception as e:
            return f"error reading page: {e}"

    def click(self, element_description: str) -> str:
        page = self._page
        normal_strategies = [
            lambda: page.get_by_text(element_description, exact=True).first,
            lambda: page.get_by_text(element_description, exact=False).first,
            lambda: page.get_by_role("button", name=element_description).first,
            lambda: page.get_by_role("link", name=element_description).first,
            lambda: page.locator(f'[aria-label*="{element_description}"]').first,
            lambda: page.locator(f'[title*="{element_description}"]').first,
        ]
        for strategy in normal_strategies:
            try:
                strategy().click(timeout=5000)
                return f"clicked '{element_description}'"
            except Exception:
                continue

        # Force click — bypasses visibility check for CSS hover dropdowns
        force_strategies = [
            lambda: page.get_by_text(element_description, exact=False).first,
            lambda: page.locator(f'a:has-text("{element_description}")').first,
        ]
        for strategy in force_strategies:
            try:
                strategy().click(timeout=5000, force=True)
                return f"clicked '{element_description}' (force)"
            except Exception:
                continue

        # JavaScript click — works on CSS-hidden elements (e.g. hover dropdowns)
        try:
            found = page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll('a, button, [role="menuitem"]'));
                const el = all.find(e => e.textContent.trim().includes("{element_description}"));
                if (el) {{ el.click(); return true; }}
                return false;
            }}""")
            if found:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                return f"clicked '{element_description}' (js)"
        except Exception:
            pass

        return f"error: could not find element '{element_description}'"

    def fill_field(self, field_label: str, value: str) -> str:
        page = self._page
        slug = field_label.lower().replace(" ", "")
        strategies = [
            lambda: page.get_by_label(field_label, exact=True).first,
            lambda: page.get_by_label(field_label, exact=False).first,
            lambda: page.get_by_placeholder(field_label).first,
            lambda: page.locator(f'[aria-label*="{field_label}"]').first,
            lambda: page.locator(f'input[name*="{slug}"], input[id*="{slug}"]').first,
        ]
        for strategy in strategies:
            try:
                strategy().fill(value, timeout=5000)
                return f"filled '{field_label}' with '{value}'"
            except Exception:
                continue
        return f"error: could not find field '{field_label}'"

    def clear_field(self, field_label: str) -> str:
        page = self._page
        slug = field_label.lower().replace(" ", "")
        strategies = [
            lambda: page.get_by_label(field_label, exact=True).first,
            lambda: page.get_by_label(field_label, exact=False).first,
            lambda: page.locator(f'input[name*="{slug}"], input[id*="{slug}"]').first,
        ]
        for strategy in strategies:
            try:
                strategy().clear(timeout=5000)
                return f"cleared '{field_label}'"
            except Exception:
                continue
        return f"error: could not find field '{field_label}'"

    def submit_form(self) -> str:
        try:
            self._page.keyboard.press("Enter")
            return "form submitted"
        except Exception as e:
            return f"error submitting form: {e}"

    def get_field_options(self, field_label: str) -> str:
        try:
            select = self._page.get_by_label(field_label)
            options = select.evaluate("el => Array.from(el.options).map(o => o.text)")
            return f"options for '{field_label}': {options}"
        except Exception as e:
            return f"error getting options for '{field_label}': {e}"

    def hover(self, element_description: str) -> str:
        page = self._page
        strategies = [
            lambda: page.get_by_text(element_description, exact=True).first,
            lambda: page.get_by_text(element_description, exact=False).first,
            lambda: page.get_by_role("button", name=element_description).first,
            lambda: page.get_by_role("link", name=element_description).first,
            lambda: page.locator(f'[aria-label*="{element_description}"]').first,
            lambda: page.locator(f'[title*="{element_description}"]').first,
        ]
        for strategy in strategies:
            try:
                strategy().hover(timeout=5000)
                page.wait_for_timeout(600)
                return f"hovered over '{element_description}' — dropdown or submenu may now be visible, call read_page to see options"
            except Exception:
                continue

        # JS mouseover fallback
        try:
            found = page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll('a, button, li, [role="menuitem"]'));
                const el = all.find(e => e.textContent.trim().includes("{element_description}"));
                if (el) {{
                    el.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                    el.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                    return true;
                }}
                return false;
            }}""")
            if found:
                page.wait_for_timeout(600)
                return f"hovered over '{element_description}' (js) — dropdown may now be visible"
        except Exception:
            pass

        return f"error: could not find element '{element_description}' to hover"

    def take_screenshot(self, label: str, output_dir: Path) -> str:
        path = output_dir / f"{label}.png"
        try:
            self._page.screenshot(path=str(path))
            return str(path)
        except Exception as e:
            return f"error taking screenshot: {e}"


def _trim_table_rows(snapshot: str, max_rows: int = 5) -> str:
    """Cap data rows inside each table block, leave everything else untouched."""
    lines = snapshot.splitlines()
    out: list[str] = []
    table_indent: int | None = None  # indentation level of the current table
    row_count = 0
    skipped = 0

    for line in lines:
        stripped = stripped_line = line.lstrip()
        indent = len(line) - len(stripped)

        # Detect entering a table block
        if stripped.startswith("- table") or stripped.startswith("- grid"):
            table_indent = indent
            row_count = 0
            skipped = 0
            out.append(line)
            continue

        # Detect leaving the table block (indentation returns to table level or less)
        if table_indent is not None and indent <= table_indent and stripped and not stripped.startswith("- row") and not stripped.startswith("- rowgroup") and not stripped.startswith("- columnheader") and not stripped.startswith("- cell"):
            if skipped:
                out.append(f"{' ' * (table_indent + 2)}- ... ({skipped} more rows)")
            table_indent = None
            row_count = 0
            skipped = 0

        # Inside a table — count and cap data rows
        if table_indent is not None and stripped.startswith("- row"):
            # first row is the header row — always keep it
            if row_count == 0:
                row_count += 1
                out.append(line)
            elif row_count <= max_rows:
                row_count += 1
                out.append(line)
            else:
                skipped += 1
            continue

        out.append(line)

    if skipped:
        out.append(f"{' ' * (table_indent + 2)}- ... ({skipped} more rows)")

    return "\n".join(out)
