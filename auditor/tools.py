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
    _last_selectors: list[dict] = field(default_factory=list, init=False, repr=False)

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

    # --- internal helpers ---

    def _extract_selectors(self, locator) -> list[dict]:
        """Extract XPath, aria-label, and text from a Playwright Locator."""
        try:
            # Fast existence check — locator.count() is immediate, no wait.
            # Skips the expensive evaluate() (30s default timeout) when element is absent.
            if locator.count() == 0:
                return []
            data = locator.evaluate("""el => {
                function getXPath(node) {
                    if (node.id) return '//*[@id="' + node.id + '"]';
                    const parts = [];
                    let cur = node;
                    while (cur && cur.nodeType === 1) {
                        let idx = 1;
                        let sib = cur.previousElementSibling;
                        while (sib) { if (sib.tagName === cur.tagName) idx++; sib = sib.previousElementSibling; }
                        parts.unshift(cur.tagName.toLowerCase() + (idx > 1 ? '[' + idx + ']' : ''));
                        cur = cur.parentElement;
                    }
                    return '/' + parts.join('/');
                }
                return {
                    xpath: getXPath(el),
                    aria_label: el.getAttribute('aria-label') || '',
                    text: (el.textContent || '').trim().slice(0, 100)
                };
            }""")
            result = []
            if data.get("xpath"):
                result.append({"type": "xpath", "value": data["xpath"]})
            if data.get("aria_label"):
                result.append({"type": "aria_label", "value": data["aria_label"]})
            if data.get("text"):
                result.append({"type": "text", "value": data["text"]})
            return result
        except Exception:
            return []

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
            snapshot = self._page.aria_snapshot()
            url = self._page.url
            title = self._page.title()

            # When a modal/popup iframe is open, read its content too
            if "- iframe" in snapshot:
                frame_parts: list[str] = []
                for frame in self._page.frames[1:]:  # skip main frame
                    if not frame.url or frame.url == "about:blank":
                        continue
                    try:
                        # aria_snapshot() is on Page not Frame — use locator on body
                        try:
                            frame_snap = frame.locator("body").aria_snapshot(timeout=3000)
                        except Exception:
                            frame_snap = ""
                        if not frame_snap or not frame_snap.strip():
                            # fall back to visible text
                            frame_snap = frame.locator("body").inner_text(timeout=2000)
                        if frame_snap and frame_snap.strip():
                            label_hint = frame.name or frame.url.split("/")[-1]
                            frame_parts.append(
                                f"[iframe: {label_hint}]\n{frame_snap.strip()[:1200]}"
                            )
                    except Exception as e:
                        frame_parts.append(f"[iframe: url={frame.url} — unreadable: {e}]")
                if frame_parts:
                    snapshot = snapshot + "\n\n" + "\n\n".join(frame_parts)

            trimmed = _trim_table_rows(snapshot, max_rows=5)
            return f"url: {url}\ntitle: {title}\n\n{trimmed}"
        except Exception as e:
            return f"error reading page: {e}"

    def click(self, element_description: str) -> str:
        page = self._page
        self._last_selectors = []
        # Strip mandatory-field asterisk markers that bleed into aria labels
        desc = element_description.rstrip(" *").strip()

        # Priority 0: Ivalua listbox — check FIRST before any generic strategy.
        # When an autocomplete dropdown is open, get_by_text() can click the wrong
        # element before reaching this fallback. Run it first to avoid false positives.
        try:
            found = page.evaluate(f"""() => {{
                const containers = document.querySelectorAll(
                    'ul[role="listbox"], .iv-menu-container ul, .scrolling.menu.visible'
                );
                for (const lb of containers) {{
                    if (!lb.offsetParent) continue;  // skip hidden containers
                    const items = Array.from(lb.querySelectorAll('li, [role="option"], a, span'));
                    const item = items.find(el => el.textContent.trim().includes("{desc}"));
                    if (item) {{ item.click(); return true; }}
                }}
                return false;
            }}""")
            if found:
                page.wait_for_timeout(500)
                return f"clicked '{element_description}' (listbox)"
        except Exception:
            pass

        # Strip a leading role prefix the LLM sometimes adds, e.g. 'tab "Foo"' → 'Foo'
        import re as _re
        clean = _re.sub(r'^(tab|button|link|option)\s+["\']?(.*?)["\']?$', r'\2', desc).strip()
        # Escape double quotes for CSS attribute selectors
        css_desc = desc.replace('"', '\\"')
        css_clean = clean.replace('"', '\\"')

        normal_strategies = [
            lambda: page.get_by_role("option", name=clean).first,
            lambda: page.get_by_role("tab", name=clean).first,
            lambda: page.get_by_role("tab", name=desc).first,
            lambda: page.get_by_text(clean, exact=True).first,
            lambda: page.get_by_text(desc, exact=True).first,
            lambda: page.get_by_text(clean, exact=False).first,
            lambda: page.get_by_role("button", name=clean).first,
            lambda: page.get_by_role("link", name=clean).first,
            lambda: page.locator(f'[aria-label*="{css_clean}"]').first,
            lambda: page.locator(f'[title*="{css_clean}"]').first,
        ]
        for i, strategy in enumerate(normal_strategies):
            try:
                loc = strategy()
                selectors = self._extract_selectors(loc)
                if selectors:
                    sel_str = " | ".join(f"{s['type']}={s['value']!r}" for s in selectors)
                    print(f"           [click strategy {i+1}] element: {sel_str}")
                self._last_selectors = selectors
                loc.click(timeout=1500)
                return f"clicked '{element_description}'"
            except Exception as e:
                print(f"           [click strategy {i+1}] failed: {e}")
                self._last_selectors = []
                continue

        # Force click — bypasses visibility check for CSS hover dropdowns
        force_strategies = [
            lambda: page.get_by_text(desc, exact=False).first,
            lambda: page.locator(f'a:has-text("{desc}")').first,
        ]
        for strategy in force_strategies:
            try:
                loc = strategy()
                self._last_selectors = self._extract_selectors(loc)
                loc.click(timeout=1500, force=True)
                return f"clicked '{element_description}' (force)"
            except Exception:
                self._last_selectors = []
                continue

        # Radio button strategy — must use check() not click() to fire change events
        # that trigger conditional-field logic in frameworks like Ivalua.
        # When multiple radios share the same name (e.g. two "Yes" groups on one page),
        # prefer the first UNCHECKED one — the already-checked one is a no-op.
        try:
            radio_locs = page.get_by_role("radio", name=clean)
            count = radio_locs.count()
            if count > 0:
                target = None
                for idx in range(count):
                    loc = radio_locs.nth(idx)
                    try:
                        if not loc.is_checked(timeout=500):
                            target = loc
                            break
                    except Exception:
                        pass
                if target is None:
                    target = radio_locs.first
                target.check(timeout=2000)
                page.wait_for_timeout(800)
                print(f"           [click radio] checked radio '{clean}' (idx {idx if target else 0})")
                return f"clicked '{element_description}' (radio)"
        except Exception as e:
            print(f"           [click radio] failed: {e}")

        # JavaScript radio fallback — finds input[type=radio] by adjacent label text,
        # prefers unchecked radios, sets checked and dispatches change event to trigger
        # Ivalua conditional logic (e.g. showing conditional fields).
        try:
            js_desc = desc.replace("\\", "\\\\").replace('"', '\\"')
            found = page.evaluate(f"""() => {{
                const text = "{js_desc}";
                // First pass: prefer unchecked radios with matching label
                for (const radio of document.querySelectorAll('input[type="radio"]')) {{
                    if (radio.checked) continue;
                    const lbl = document.querySelector('label[for="' + radio.id + '"]')
                        || radio.nextElementSibling;
                    if (lbl && lbl.textContent.trim() === text) {{
                        radio.checked = true;
                        radio.dispatchEvent(new Event('change', {{bubbles: true}}));
                        radio.dispatchEvent(new Event('input', {{bubbles: true}}));
                        radio.click();
                        return true;
                    }}
                }}
                // Second pass: fall back to any matching radio (already checked ones)
                for (const radio of document.querySelectorAll('input[type="radio"]')) {{
                    const lbl = document.querySelector('label[for="' + radio.id + '"]')
                        || radio.nextElementSibling;
                    if (lbl && lbl.textContent.trim() === text) {{
                        radio.checked = true;
                        radio.dispatchEvent(new Event('change', {{bubbles: true}}));
                        radio.dispatchEvent(new Event('input', {{bubbles: true}}));
                        radio.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            if found:
                page.wait_for_timeout(800)
                return f"clicked '{element_description}' (radio-js)"
        except Exception as e:
            print(f"           [click radio-js] failed: {e}")

        # JavaScript click — works on CSS-hidden elements (e.g. hover dropdowns)
        try:
            found = page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll('a, button, [role="menuitem"]'));
                const el = all.find(e => e.textContent.trim().includes("{desc}"));
                if (el) {{ el.click(); return true; }}
                return false;
            }}""")
            if found:
                return f"clicked '{element_description}' (js)"
        except Exception:
            pass

        # Iframe fallback — try clicking inside visible popup/modal iframes
        for frame in self._page.frames[1:]:
            if not frame.url or frame.url == "about:blank":
                continue

            # Priority JS walk-up strategy for Ivalua browse modals.
            # get_by_text() hits non-interactive <td> cells; walk up to the nearest
            # <a>/<button>/onclick ancestor which carries the selection handler.
            try:
                js_desc = desc.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
                found = frame.evaluate(f"""() => {{
                    const text = "{js_desc}";
                    const candidates = Array.from(document.querySelectorAll(
                        'td, li, span, div, a, button'
                    ));
                    for (const el of candidates) {{
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t === text || t === text + '\\u00a0') {{
                            let target = el;
                            for (let i = 0; i < 6 && target && target !== document.body; i++) {{
                                if (target.onclick || target.tagName === 'A' ||
                                        target.tagName === 'BUTTON') {{
                                    target.click();
                                    return target.tagName + ':' + (target.textContent || '')
                                        .trim().substring(0, 40);
                                }}
                                target = target.parentElement;
                            }}
                            el.click();
                            return 'direct:' + el.tagName + ':' + t.substring(0, 40);
                        }}
                    }}
                    return null;
                }}""")
                if found:
                    self._page.wait_for_timeout(1500)
                    print(f"           [click iframe JS walk-up] {found}")
                    return f"clicked '{element_description}' (iframe-js)"
            except Exception as e:
                print(f"           [click iframe JS walk-up] failed: {e}")

            iframe_strategies = [
                lambda f=frame: f.get_by_text(desc, exact=True).first,
                lambda f=frame: f.get_by_text(desc, exact=False).first,
                lambda f=frame: f.get_by_role("button", name=desc).first,
                lambda f=frame: f.locator(f'[aria-label*="{desc}"]').first,
                lambda f=frame: f.get_by_role("link", name=desc).first,
            ]
            for i, strategy in enumerate(iframe_strategies):
                try:
                    loc = strategy()
                    if loc.count() == 0:
                        continue
                    selectors = self._extract_selectors(loc)
                    if selectors:
                        sel_str = " | ".join(f"{s['type']}={s['value']!r}" for s in selectors)
                        print(f"           [click iframe strategy {i+1}] element: {sel_str}")
                    loc.click(timeout=2000)
                    return f"clicked '{element_description}' (iframe)"
                except Exception as e:
                    print(f"           [click iframe strategy {i+1}] failed: {e}")
                    continue

        return f"error: could not find element '{element_description}'"

    def fill_by_xpath(self, xpath: str, value: str) -> bool:
        """Fill a field by XPath. Uses press_sequentially for combobox/autocomplete inputs."""
        try:
            loc = self._page.locator(f"xpath={xpath}").first
            role = loc.get_attribute("role", timeout=1000) or ""
            aria_auto = loc.get_attribute("aria-autocomplete", timeout=1000) or ""
            if role == "combobox" or aria_auto:
                # Ivalua autocomplete — clear then type character by character to trigger XHR
                loc.click(timeout=2000)
                self._page.wait_for_timeout(200)
                self._page.keyboard.press("Control+a")
                self._page.keyboard.press("Backspace")
                self._page.wait_for_timeout(100)
                loc.press_sequentially(value, delay=80)
                self._page.wait_for_timeout(3000)
            else:
                loc.fill(value, timeout=3000)
            return True
        except Exception:
            return False

    def fill_field(self, field_label: str, value: str, timeout: int = 1500) -> str:
        page = self._page
        self._last_selectors = []
        label = field_label.rstrip(" *").strip()
        slug = label.lower().replace(" ", "")

        # Strategy 0 (first): Ivalua iv-autocompletion-selector.
        # Must run before generic strategies because:
        # 1. The label is a <span data-iv-role="label">, not <label for=...>, so get_by_label
        #    either fails or resolves to a wrong/hidden element.
        # 2. Ivalua's autocomplete listens for per-keystroke keyup events ("Type at least 3
        #    characters") — fill() dispatches a single input event and doesn't trigger XHR.
        # 3. Generic strategies' control.click() can accidentally open the "See All" modal.
        try:
            result = page.evaluate("""(lbl) => {
                const labelEls = Array.from(document.querySelectorAll(
                    '[data-iv-role="label"], label, th, .field-label'
                ));
                const match = labelEls.find(el => {
                    const txt = el.textContent.trim().replace(/\\s*\\*\\s*$/, '').trim();
                    return txt === lbl || txt.startsWith(lbl);
                });
                if (!match) return {found: false, reason: 'label not found'};
                const wrapper = match.closest('[data-iv-role="controlWrapper"]') ||
                                match.parentElement;
                if (!wrapper) return {found: false, reason: 'no wrapper'};
                const inp = wrapper.querySelector(
                    'input[role="combobox"], input.search, input[aria-autocomplete]'
                );
                if (!inp) return {found: false, reason: 'no search input'};
                return {found: true, id: inp.id, name: inp.name};
            }""", label)
            if result and result.get("found"):
                inp_id = result.get("id")
                inp_name = result.get("name")
                print(f"           [fill_field ivalua] input id={inp_id!r} name={inp_name!r}")
                inp_loc = page.locator(f"#{inp_id}").first if inp_id else page.locator(f'input[name="{inp_name}"]').first
                inp_loc.click(timeout=2000)
                page.wait_for_timeout(200)
                # Clear any existing text before typing (Ctrl+A then Backspace)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(100)
                inp_loc.press_sequentially(value, delay=80)
                page.wait_for_timeout(3000)
                self._last_selectors = self._extract_selectors(inp_loc)
                return f"filled '{label}' with '{value}' (ivalua-autocomplete)"
            else:
                print(f"           [fill_field ivalua] not found: {result}")
        except Exception as e:
            print(f"           [fill_field ivalua] failed: {e}")

        # Strategy 0a: combobox by aria-label — handles Ivalua fields where the combobox
        # element itself has role=combobox + aria-label (not wrapped in iv-autocompletion).
        # click() focuses it, press_sequentially() triggers per-keystroke XHR like Agency/Division.
        try:
            loc = page.get_by_role("combobox", name=label).first
            if loc.count() > 0:
                print(f"           [fill_field combobox-aria] found combobox '{label}'")
                # Clear any existing selection — Ivalua shows a "Delete the value." button
                # when a value is already selected. Clicking it clears the chip/token before
                # we type, preventing "EmergencyEmergency"-style doubling.
                try:
                    del_btn = loc.locator("xpath=following::button[contains(@title,'Delete') or contains(text(),'Delete')]").first
                    if del_btn.count() == 0:
                        # Try JS: find a delete button near this combobox
                        page.evaluate(f"""() => {{
                            const cb = document.querySelector('[aria-label="{label}"]') ||
                                       document.querySelector('[name="{label}"]');
                            if (!cb) return;
                            const container = cb.closest('[data-iv-role="controlWrapper"]') || cb.parentElement;
                            if (!container) return;
                            const btn = container.querySelector('button[title*="Delete"], button[aria-label*="Delete"]');
                            if (btn) btn.click();
                        }}""")
                        page.wait_for_timeout(300)
                    else:
                        del_btn.click(timeout=1000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass
                loc.click(timeout=2000)
                page.wait_for_timeout(200)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(100)
                loc.press_sequentially(value, delay=80)
                page.wait_for_timeout(3000)
                self._last_selectors = self._extract_selectors(loc)
                return f"filled '{label}' with '{value}' (combobox-aria)"
        except Exception as e:
            print(f"           [fill_field combobox-aria] failed: {e}")

        # Strategy 0b: Ivalua date range end field — find the first input[type=text]
        # that appears after a heading whose text starts with "to:" using a DOM TreeWalker.
        # Sibling-walk fails when the input is nested; TreeWalker traverses the full subtree.
        if slug in ("to", "enddate", "todate", "contractperiodend"):
            try:
                result = page.evaluate("""() => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_ELEMENT
                    );
                    let node;
                    let foundToHeading = false;
                    while ((node = walker.nextNode())) {
                        if (!foundToHeading) {
                            const tag = node.tagName || '';
                            if (/^H[1-6]$/.test(tag)) {
                                const txt = node.textContent.trim();
                                if (txt.startsWith('to:') || txt === 'to') {
                                    foundToHeading = true;
                                }
                            }
                        } else {
                            if (node.tagName === 'INPUT' && node.type === 'text') {
                                return {found: true, id: node.id, name: node.name};
                            }
                        }
                    }
                    return {found: false, reason: 'end date input not found after to: heading'};
                }""")
                if result and result.get("found"):
                    inp_id = result.get("id")
                    inp_name = result.get("name")
                    loc = page.locator(f"#{inp_id}").first if inp_id else page.locator(f'input[name="{inp_name}"]').first
                    print(f"           [fill_field date-end] id={inp_id!r}")
                    loc.fill(value, timeout=2000)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                    self._last_selectors = self._extract_selectors(loc)
                    return f"filled 'to' date with '{value}' (ivalua-date-end)"
                else:
                    print(f"           [fill_field date-end] not found: {result}")
            except Exception as e:
                print(f"           [fill_field date-end] failed: {e}")

        # Generic strategies 1-5
        strategies = [
            lambda: page.get_by_label(label, exact=True).first,
            lambda: page.get_by_label(label, exact=False).first,
            lambda: page.get_by_placeholder(label).first,
            lambda: page.locator(f'[aria-label*="{label}"]').first,
            lambda: page.locator(f'input[name*="{slug}"], input[id*="{slug}"]').first,
        ]
        for i, strategy in enumerate(strategies):
            try:
                loc = strategy()
                selectors = self._extract_selectors(loc)
                if selectors:
                    sel_str = " | ".join(f"{s['type']}={s['value']!r}" for s in selectors)
                    print(f"           [fill_field strategy {i+1}] element: {sel_str}")
                self._last_selectors = selectors
                loc.fill(value, timeout=timeout)
                page.keyboard.press("Escape")  # dismiss calendar pickers / dropdowns
                page.wait_for_timeout(500)
                return f"filled '{label}' with '{value}' (strategy {i+1})"
            except Exception as e:
                print(f"           [fill_field strategy {i+1}] failed: {e}")
                self._last_selectors = []
                continue

        # Iframe fallback — try filling inside visible popup/modal iframes
        for frame in self._page.frames[1:]:
            if not frame.url or frame.url == "about:blank":
                continue
            iframe_strategies = [
                lambda f=frame: f.get_by_label(label, exact=True).first,
                lambda f=frame: f.get_by_label(label, exact=False).first,
                lambda f=frame: f.get_by_placeholder(label).first,
                lambda f=frame: f.locator(f'[aria-label*="{label}"]').first,
                lambda f=frame: f.locator(f'input[name*="{slug}"], input[id*="{slug}"]').first,
            ]
            for i, strategy in enumerate(iframe_strategies):
                try:
                    loc = strategy()
                    if loc.count() == 0:
                        continue
                    selectors = self._extract_selectors(loc)
                    if selectors:
                        sel_str = " | ".join(f"{s['type']}={s['value']!r}" for s in selectors)
                        print(f"           [fill_field iframe strategy {i+1}] element: {sel_str}")
                    self._last_selectors = selectors
                    loc.fill(value, timeout=2000)
                    page.wait_for_timeout(500)
                    return f"filled '{label}' with '{value}' (iframe strategy {i+1})"
                except Exception as e:
                    print(f"           [fill_field iframe strategy {i+1}] failed: {e}")
                    self._last_selectors = []
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
                strategy().clear(timeout=1500)
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

    def press_key(self, key: str) -> str:
        try:
            self._page.keyboard.press(key)
            self._page.wait_for_timeout(300)
            return f"pressed '{key}'"
        except Exception as e:
            return f"error pressing key '{key}': {e}"

    def get_field_options(self, field_label: str) -> str:
        label = field_label.rstrip(" *").strip()
        try:
            select = self._page.get_by_label(label).first
            options = select.evaluate("el => Array.from(el.options).map(o => o.text)")
            return f"options for '{field_label}': {options}"
        except Exception as e:
            return f"error getting options for '{field_label}': {e}"

    def select_option(self, field_label: str, option_value: str) -> str:
        """Select a value in a <select> or ARIA combobox by label."""
        label = field_label.rstrip(" *").strip()
        slug = label.lower().replace(" ", "")
        page = self._page
        self._last_selectors = []

        # Strategy 1: native <select> via select_option()
        select_locators = [
            lambda: page.get_by_label(label, exact=True).first,
            lambda: page.get_by_label(label, exact=False).first,
            lambda: page.locator(f'select[name*="{slug}"], select[id*="{slug}"]').first,
        ]
        for loc_fn in select_locators:
            try:
                loc = loc_fn()
                if loc.count() == 0:
                    continue
                loc.select_option(option_value, timeout=3000)
                self._last_selectors = self._extract_selectors(loc)
                return f"selected '{option_value}' in '{field_label}'"
            except Exception:
                continue

        # Strategy 2: ARIA combobox — click to open, then click the option
        combobox_locators = [
            lambda: page.get_by_role("combobox", name=label).first,
            lambda: page.locator(f'[aria-label*="{label}"]').first,
        ]
        for loc_fn in combobox_locators:
            try:
                loc = loc_fn()
                if loc.count() == 0:
                    continue
                loc.click(timeout=3000)
                page.wait_for_timeout(300)
                # Click the matching option in the opened dropdown
                page.get_by_role("option", name=option_value).first.click(timeout=3000)
                self._last_selectors = self._extract_selectors(loc)
                return f"selected '{option_value}' in '{field_label}' (combobox)"
            except Exception:
                continue

        # Strategy 3: fill to filter then pick first matching option
        fill_locators = [
            lambda: page.get_by_label(label, exact=False).first,
        ]
        for loc_fn in fill_locators:
            try:
                loc = loc_fn()
                if loc.count() == 0:
                    continue
                loc.fill(option_value, timeout=3000)
                page.wait_for_timeout(300)
                page.get_by_role("option", name=option_value).first.click(timeout=3000)
                self._last_selectors = self._extract_selectors(loc)
                return f"selected '{option_value}' in '{field_label}' (fill+pick)"
            except Exception:
                continue

        return f"error: could not select '{option_value}' in '{field_label}'"

    def hover(self, element_description: str) -> str:
        page = self._page
        self._last_selectors = []
        desc = element_description.rstrip(" *").strip()
        strategies = [
            lambda: page.get_by_text(desc, exact=True).first,
            lambda: page.get_by_text(desc, exact=False).first,
            lambda: page.get_by_role("button", name=desc).first,
            lambda: page.get_by_role("link", name=desc).first,
            lambda: page.locator(f'[aria-label*="{desc}"]').first,
            lambda: page.locator(f'[title*="{desc}"]').first,
        ]
        for strategy in strategies:
            try:
                loc = strategy()
                self._last_selectors = self._extract_selectors(loc)
                loc.hover(timeout=1500)
                page.wait_for_timeout(200)
                return f"hovered over '{element_description}' — dropdown or submenu may now be visible, call read_page to see options"
            except Exception:
                self._last_selectors = []
                continue

        # JS mouseover fallback
        try:
            found = page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll('a, button, li, [role="menuitem"]'));
                const el = all.find(e => e.textContent.trim().includes("{desc}"));
                if (el) {{
                    el.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                    el.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                    return true;
                }}
                return false;
            }}""")
            if found:
                page.wait_for_timeout(200)
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
