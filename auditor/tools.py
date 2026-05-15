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
    # Label/text of the most recently interacted element — used by read_page
    # to focus the aria snapshot and by take_screenshot to highlight on screen.
    _last_interacted_label: str = field(default="", init=False, repr=False)

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

    def _highlight_last_element(self) -> str | None:
        """
        Inject a bright outline on the last interacted element via JS.
        Returns the XPath used (so the caller can unhighlight later), or None.
        """
        xpath = next(
            (s["value"] for s in self._last_selectors if s.get("type") in ("xpath", "xpath_structural")),
            None,
        )
        if not xpath:
            return None
        try:
            self._page.evaluate(f"""() => {{
                const el = document.evaluate(
                    {json.dumps(xpath)}, document, null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!el) return;
                el._prevOutline      = el.style.outline;
                el._prevOutlineOff   = el.style.outlineOffset;
                el._prevBg           = el.style.backgroundColor;
                el.style.outline        = '3px solid #FF3333';
                el.style.outlineOffset  = '3px';
                el.style.backgroundColor = 'rgba(255,51,51,0.08)';
            }}""")
            return xpath
        except Exception:
            return None

    def _unhighlight_element(self, xpath: str) -> None:
        """Remove the highlight injected by _highlight_last_element."""
        try:
            self._page.evaluate(f"""() => {{
                const el = document.evaluate(
                    {json.dumps(xpath)}, document, null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!el) return;
                el.style.outline         = el._prevOutline    || '';
                el.style.outlineOffset   = el._prevOutlineOff || '';
                el.style.backgroundColor = el._prevBg         || '';
            }}""")
        except Exception:
            pass

    def _focused_snapshot(self, full_snapshot: str, ancestor_levels: int = 3) -> str:
        """
        Return the subtree of the aria snapshot that contains the last interacted
        element, by walking UP the indentation tree a fixed number of levels and
        then returning ALL children of that ancestor.

        This gives a structurally complete section (e.g. the entire tabpanel or
        form group) rather than an arbitrary line-count window that can slice
        through the middle of a parent element and leave orphaned children.

        ancestor_levels=3 means: go 3 indentation steps above the anchor line.
        For a typical Ivalua form field inside a tabpanel the hierarchy is:
            tabpanel (level 0)
              heading  (level 1)
              text: label (level 2)
              combobox (level 2)  ← anchor
        Walking 3 levels up from the combobox reaches tabpanel, whose subtree
        includes every field, heading, and radio group in the PMQ section.
        """
        label = self._last_interacted_label
        if not label:
            return full_snapshot

        lines = full_snapshot.splitlines()

        # --- Find the LAST line that mentions this element's label or text ---
        # Using last rather than first: form fields appear in <main> which comes
        # after navigation elements that may share the same label text
        # (e.g. "Procurement Method" tab vs "Procurement Method" combobox).
        target = None
        for i, line in enumerate(lines):
            if label.lower()[:30] in line.lower():
                target = i   # keep scanning — last match wins

        if target is None:
            return full_snapshot

        # --- Walk UP the indentation tree ancestor_levels steps ---
        def indent_of(line: str) -> int:
            return len(line) - len(line.lstrip())

        anchor_indent = indent_of(lines[target])
        current_indent = anchor_indent
        ancestor_line = target

        steps_taken = 0
        for j in range(target - 1, -1, -1):
            if not lines[j].strip():
                continue                        # skip blank lines
            ind = indent_of(lines[j])
            if ind < current_indent:
                current_indent = ind
                ancestor_line = j
                steps_taken += 1
                if steps_taken >= ancestor_levels:
                    break
            if current_indent == 0:
                break                           # already at root

        # --- Collect entire subtree of the ancestor ---
        # The subtree ends at the next line whose indentation is ≤ ancestor's
        ancestor_indent = indent_of(lines[ancestor_line])
        end = len(lines)
        for j in range(ancestor_line + 1, len(lines)):
            if not lines[j].strip():
                continue
            if indent_of(lines[j]) <= ancestor_indent:
                end = j
                break

        window = lines[ancestor_line:end]
        prefix = f"[... {ancestor_line} lines above ...]\n" if ancestor_line > 0 else ""
        suffix = f"\n[... {len(lines) - end} lines below ...]" if end < len(lines) else ""
        return prefix + "\n".join(window) + suffix

    def _extract_selectors(self, locator) -> list[dict]:
        """Extract multiple selectors from a Playwright Locator, in priority order.

        Selector priority (tried in this order during replay):
          1. xpath (id-based)   — //*[@id="..."]  — stable, fast
          2. xpath (structural) — /html/body/...  — fallback if id changes
          3. aria_label         — [aria-label="..."] — semantic, UI-version-resilient
          4. text               — exact visible text — last resort

        Both XPath variants are always recorded when the element has an id so
        that a Ivalua form version bump (which can change generated ids) still
        has a structural fallback rather than falling all the way through to ReAct.
        """
        try:
            # Fast existence check — locator.count() is immediate, no wait.
            # Skips the expensive evaluate() (30s default timeout) when element is absent.
            if locator.count() == 0:
                return []
            data = locator.evaluate("""el => {
                function getIdXPath(node) {
                    if (node.id) return '//*[@id="' + node.id + '"]';
                    return '';
                }
                function getStructuralXPath(node) {
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
                    xpath_id:         getIdXPath(el),
                    xpath_structural: getStructuralXPath(el),
                    aria_label:       el.getAttribute('aria-label') || '',
                    text:             (el.textContent || '').trim().slice(0, 100)
                };
            }""")
            result = []
            # 1. ID-based XPath — highest confidence
            if data.get("xpath_id"):
                result.append({"type": "xpath", "value": data["xpath_id"]})
            # 2. Structural XPath — fallback when id changes (e.g. Ivalua form version bump)
            #    Only add if it differs from the id xpath (always true when xpath_id exists)
            if data.get("xpath_structural") and data.get("xpath_structural") != data.get("xpath_id"):
                result.append({"type": "xpath_structural", "value": data["xpath_structural"]})
            # 3. aria-label — semantic, survives DOM restructuring
            if data.get("aria_label"):
                result.append({"type": "aria_label", "value": data["aria_label"]})
            # 4. Visible text — last resort (broad match, use with caution)
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
            page  = self._page
            url   = page.url
            title = page.title()
            snapshot = page.aria_snapshot()

            # ── Priority 1: visible listbox (autocomplete dropdown open) ──────
            # Return only the dropdown — the LLM just needs to pick an item.
            try:
                lb = page.locator('[role="listbox"]:visible').first
                if lb.count() > 0:
                    lb_snap = lb.aria_snapshot(timeout=2000)
                    if lb_snap and lb_snap.strip():
                        return (
                            f"url: {url}\ntitle: {title}\n\n"
                            f"[active dropdown — pick an item]\n{lb_snap}"
                        )
            except Exception:
                pass

            # ── Priority 2: visible dialog/modal ─────────────────────────────
            # Return only the modal content + any iframe content inside it.
            try:
                dlg = page.locator('[role="dialog"]:visible').first
                if dlg.count() > 0:
                    dlg_snap = dlg.aria_snapshot(timeout=2000)
                    if dlg_snap and dlg_snap.strip():
                        return (
                            f"url: {url}\ntitle: {title}\n\n"
                            f"[modal open]\n{dlg_snap}"
                        )
            except Exception:
                pass

            # ── Priority 3: iframe content (Ivalua browse modals) ────────────
            if "- iframe" in snapshot:
                frame_parts: list[str] = []
                for frame in page.frames[1:]:
                    if not frame.url or frame.url == "about:blank":
                        continue
                    try:
                        try:
                            frame_snap = frame.locator("body").aria_snapshot(timeout=3000)
                        except Exception:
                            frame_snap = ""
                        if not frame_snap or not frame_snap.strip():
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

            # ── Priority 4: focused view around last interacted element ───────
            # If a click or fill just happened, centre the snapshot on that element.
            if self._last_interacted_label:
                focused = self._focused_snapshot(snapshot, ancestor_levels=3)
                trimmed = _trim_table_rows(focused, max_rows=5)
                return (
                    f"url: {url}\ntitle: {title}\n\n"
                    f"[focused — last action: {self._last_interacted_label!r}]\n{trimmed}"
                )

            # ── Default: full page with table trimming ────────────────────────
            trimmed = _trim_table_rows(snapshot, max_rows=5)
            return f"url: {url}\ntitle: {title}\n\n{trimmed}"

        except Exception as e:
            return f"error reading page: {e}"

    def _platform_click_priority(self, desc: str) -> str | None:
        """
        Extension hook — override in platform subclasses to inject high-priority
        click strategies that must run before generic ones (e.g. custom listbox widgets).
        Return a result string if handled, None to fall through to generic strategies.
        """
        return None

    def _platform_fill_strategies(self, label: str, slug: str, value: str) -> str | None:
        """
        Extension hook — override in platform subclasses to inject platform-specific
        fill strategies that run before the generic get_by_label / placeholder / aria
        strategies. Return a result string if handled, None to fall through.
        """
        return None

    def _click_autocomplete_item(self, value: str) -> str | None:
        """
        Extension hook — override in platform subclasses to atomically click an
        autocomplete dropdown item after fill_field has typed the search value.
        Return the clicked item text on success, None to fall through to a fixed wait.
        Called by fill_by_xpath for combobox/autocomplete inputs.
        """
        return None

    def click(self, element_description: str) -> str:
        page = self._page
        self._last_selectors = []
        # Strip mandatory-field asterisk markers that bleed into aria labels
        desc = element_description.rstrip(" *").strip()

        # Priority 0: platform-specific strategies (listbox widgets, custom dropdowns, etc.)
        result = self._platform_click_priority(desc)
        if result is not None:
            return result

        # Strip a leading role prefix the LLM sometimes adds, e.g. 'tab "Foo"' → 'Foo'
        import re as _re
        clean = _re.sub(r'^(tab|button|link|option)\s+["\']?(.*?)["\']?$', r'\2', desc).strip()
        # Escape double quotes for CSS attribute selectors
        css_desc = desc.replace('"', '\\"')
        css_clean = clean.replace('"', '\\"')

        # Strategies paired with a boolean: True = this is a tab/navigation click.
        # Tab clicks should NOT anchor the focused snapshot — the revealed tabpanel
        # content appears elsewhere in the DOM, far below the tab element itself.
        #
        # NOTE: role-based strategies (button, link) run BEFORE text-based ones.
        # Text-based locators (get_by_text) match inner spans/labels first, which
        # are not directly clickable in frameworks like Ivalua where buttons wrap
        # a <span data-iv-role="label"> child.  Role-based lookup resolves to the
        # actual interactive element and passes actionability checks.
        normal_strategies: list[tuple[Any, bool]] = [
            (lambda: page.get_by_role("option", name=clean).first, False),
            (lambda: page.get_by_role("tab", name=clean).first,    True),   # tab
            (lambda: page.get_by_role("tab", name=desc).first,     True),   # tab
            (lambda: page.get_by_role("button", name=clean).first, False),  # before text — avoids inner-span match
            (lambda: page.get_by_role("link", name=clean).first,   False),  # before text
            (lambda: page.get_by_text(clean, exact=True).first,    False),
            (lambda: page.get_by_text(desc, exact=True).first,     False),
            (lambda: page.get_by_text(clean, exact=False).first,   False),
            (lambda: page.locator(f'[aria-label*="{css_clean}"]').first, False),
            (lambda: page.locator(f'[title*="{css_clean}"]').first,      False),
        ]
        for i, (strategy, is_tab) in enumerate(normal_strategies):
            try:
                loc = strategy()
                selectors = self._extract_selectors(loc)
                if selectors:
                    sel_str = " | ".join(f"{s['type']}={s['value']!r}" for s in selectors)
                    print(f"           [click strategy {i+1}] element: {sel_str}")
                self._last_selectors = selectors
                loc.click(timeout=1500)
                # Tab clicks: wait a moment for the tabpanel to render
                if is_tab:
                    page.wait_for_timeout(600)
                    return f"clicked '{element_description}' (tab)"
                return f"clicked '{element_description}'"
            except Exception as e:
                print(f"           [click strategy {i+1}] failed: {e}")
                self._last_selectors = []
                continue

        # Force click — bypasses visibility check for CSS hover dropdowns
        force_strategies = [
            lambda: page.get_by_role("button", name=clean).first,  # force-click button (e.g. partially obscured)
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
                self._last_selectors = self._extract_selectors(target)
                print(f"           [click radio] checked radio '{clean}' (idx {idx if target else 0})")
                return f"clicked '{element_description}' (radio)"
        except Exception as e:
            print(f"           [click radio] failed: {e}")

        # JavaScript radio fallback — finds input[type=radio] by adjacent label text,
        # prefers unchecked radios, sets checked and dispatches change event to trigger
        # Ivalua conditional logic (e.g. showing conditional fields).
        # Returns the radio's id so we can extract selectors for screenshot highlighting.
        try:
            js_desc = desc.replace("\\", "\\\\").replace('"', '\\"')
            radio_id = page.evaluate(f"""() => {{
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
                        return radio.id || null;
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
                        return radio.id || null;
                    }}
                }}
                return null;
            }}""")
            if radio_id is not None:
                page.wait_for_timeout(800)
                try:
                    loc = page.locator(f"#{radio_id}").first
                    self._last_selectors = self._extract_selectors(loc)
                except Exception:
                    pass
                return f"clicked '{element_description}' (radio-js)"
        except Exception as e:
            print(f"           [click radio-js] failed: {e}")

        # JavaScript click — works on CSS-hidden elements (e.g. hover dropdowns).
        # Returns {found, id, xpath} so we can extract selectors for fingerprinting.
        # Computes XPath as fallback when element has no id attribute.
        # Visibility guard: use getBoundingClientRect (not offsetParent) — offsetParent
        # is null for position:fixed/absolute elements in headless Chrome even when
        # the element is visually present (e.g. Ivalua action-bar buttons, table links
        # inside overflow:hidden containers). getBoundingClientRect correctly reports
        # non-zero dimensions for all visible elements regardless of positioning.
        # Exact text match is tried first; contains-match is the fallback.
        try:
            js_desc = desc.replace("\\", "\\\\").replace('"', '\\"')
            js_result = page.evaluate(f"""() => {{
                function getXPath(el) {{
                    if (el.id) return '//*[@id="' + el.id + '"]';
                    const parts = [];
                    let cur = el;
                    while (cur && cur.nodeType === 1) {{
                        let idx = 1, sib = cur.previousSibling;
                        while (sib) {{
                            if (sib.nodeType === 1 && sib.tagName === cur.tagName) idx++;
                            sib = sib.previousSibling;
                        }}
                        parts.unshift(cur.tagName.toLowerCase() + '[' + idx + ']');
                        cur = cur.parentElement;
                    }}
                    return '/' + parts.join('/');
                }}
                function isVisible(el) {{
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }}
                const all = Array.from(document.querySelectorAll('a, button, [role="menuitem"]'))
                    .filter(isVisible);
                // Exact match first; fall back to contains so partial labels still work
                const el = all.find(e => e.textContent.trim() === "{js_desc}")
                         || all.find(e => e.textContent.trim().includes("{js_desc}"));
                if (el) {{
                    el.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
                    el.click();
                    return {{found: true, id: el.id || '', xpath: getXPath(el)}};
                }}
                return {{found: false, id: '', xpath: ''}};
            }}""")
            if js_result and js_result.get("found"):
                el_xpath = js_result.get("xpath", "")
                # Always use the JS-computed XPath directly — _extract_selectors()
                # can return [] when the page is mid-navigation (e.g. Save button
                # that submits the form) because the element is gone by the time
                # the locator evaluates. el_xpath is already computed in the same
                # synchronous JS call as the click, so it's always accurate.
                if el_xpath:
                    self._last_selectors = [{"type": "xpath", "value": el_xpath,
                                             "successes": 1, "failures": 0}]
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
                # click() instead of focus() — auto-scrolls into view and fires
                # the mouse event chain needed to activate autocomplete widgets
                # in headless Chrome (focus() alone is insufficient when the
                # element is below the fold in a small headless viewport).
                loc.click(timeout=2000)
                self._page.wait_for_timeout(150)
                self._page.keyboard.press("Control+a")
                self._page.wait_for_timeout(50)
                loc.press_sequentially(value, delay=80)
                # Try atomic click (platform hook).
                # For autocomplete comboboxes the value is only properly SET when
                # the dropdown item is clicked — typing alone leaves the field in
                # an incomplete/uncommitted state.  Return False if the item was
                # not found so that the caller can fall back (e.g. ReAct loop).
                clicked = self._click_autocomplete_item(value)
                if not clicked:
                    # _click_autocomplete_item may return None for base-class stubs
                    # (no platform hook).  In that case a fixed wait is the best
                    # we can do — treat as success so non-Ivalua fills still work.
                    if hasattr(self, '_platform_fill_strategies'):
                        # Platform-aware session: item MUST be clicked for selection
                        return False
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

        # Platform-specific strategies run first (autocomplete widgets, custom date pickers, etc.)
        result = self._platform_fill_strategies(label, slug, value)
        if result is not None:
            return result

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
