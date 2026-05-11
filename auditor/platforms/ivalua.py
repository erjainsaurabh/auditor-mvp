"""
Ivalua platform adapter.

Extends BrowserSession with Ivalua-specific DOM interaction strategies.
All knowledge of Ivalua's widget patterns (data-iv-role, iv-menu-container,
chip/token deletion, "to:" date heading) lives here — not in the core tools.
"""
from __future__ import annotations

from auditor.tools import BrowserSession


class IvaluaBrowserSession(BrowserSession):
    """BrowserSession configured for Ivalua SaaS procurement platform."""

    # ------------------------------------------------------------------
    # Click: priority hook
    # ------------------------------------------------------------------

    def _platform_click_priority(self, desc: str) -> str | None:
        """
        Ivalua-specific: when an autocomplete or static dropdown is open, clicking
        via get_by_text() can hit the wrong element (e.g. a hidden conditional-field
        label that contains the same word). Check visible listbox containers first —
        including Ivalua's .iv-menu-container and Semantic UI .scrolling.menu.

        Tries exact match first, then contains-match as fallback.
        """
        page = self._page
        js_desc = desc.replace("\\", "\\\\").replace('"', '\\"')
        try:
            result = page.evaluate(f"""() => {{
                const containers = document.querySelectorAll(
                    'ul[role="listbox"], .iv-menu-container ul, .scrolling.menu.visible'
                );
                for (const lb of containers) {{
                    if (!lb.offsetParent) continue;
                    const items = Array.from(lb.querySelectorAll('li, [role="option"], a, span'));
                    // Exact match first
                    let item = items.find(el => el.textContent.trim() === "{js_desc}");
                    // Fall back to contains match
                    if (!item) item = items.find(el => el.textContent.trim().includes("{js_desc}"));
                    if (item) {{
                        item.click();
                        return {{text: item.textContent.trim().substring(0, 60), id: item.id || ''}};
                    }}
                }}
                return null;
            }}""")
            if result:
                page.wait_for_timeout(800)
                print(f"           [ivalua-listbox] clicked item: {result['text']!r}")
                # Extract selectors for screenshot highlighting
                el_id = result.get("id", "")
                if el_id:
                    try:
                        loc = page.locator(f"#{el_id}").first
                        self._last_selectors = self._extract_selectors(loc)
                    except Exception:
                        pass
                return f"clicked '{desc}' (ivalua-listbox)"
        except Exception as e:
            print(f"           [ivalua-listbox] failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Fill: platform strategies hook
    # ------------------------------------------------------------------

    def _dismiss_modal_if_open(self) -> None:
        """
        Close any Ivalua browse modal (iframe overlay) that may be blocking
        the form. This can happen when a fingerprint replay partially runs
        and opens the 'See All' browse iframe before failing — leaving the
        modal open and intercepting all pointer events on the underlying form.

        Tries Close/Cancel buttons inside the iframe first, then Escape.
        """
        page = self._page
        for frame in page.frames[1:]:
            if not frame.url or frame.url == "about:blank":
                continue
            try:
                for btn_name in ("Close", "close", "Cancel"):
                    try:
                        btn = frame.get_by_role("button", name=btn_name).first
                        if btn.count() > 0 and btn.is_visible(timeout=500):
                            btn.click(timeout=1000)
                            page.wait_for_timeout(600)
                            print(f"           [dismiss-modal] closed modal via '{btn_name}' button")
                            return
                    except Exception:
                        pass
            except Exception:
                pass
        # Fallback: Escape dismisses most Ivalua overlays
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

    def _platform_fill_strategies(self, label: str, slug: str, value: str) -> str | None:
        """
        Three Ivalua-specific fill strategies tried in order before generic ones.
        Dismisses any open modal first — stale modals can block form interactions
        when a fingerprint replay opens a browse iframe and then fails.
        Returns a result string if any strategy succeeds, None to fall through.
        """
        self._dismiss_modal_if_open()
        return (
            self._fill_ivalua_autocomplete(label, value)
            or self._fill_combobox_aria(label, value)
            or self._fill_date_end(slug, value)
        )

    def _fill_ivalua_autocomplete(self, label: str, value: str) -> str | None:
        """
        Strategy: Ivalua iv-autocompletion widget.

        Why this must run before generic strategies:
        - The label is a <span data-iv-role="label">, not <label for=...>, so
          get_by_label() either fails or resolves to a wrong/hidden element.
        - Ivalua's autocomplete fires per-keystroke XHR ("Type at least 3 chars");
          fill() dispatches a single input event and doesn't trigger the search.
        - Generic click() on the control can accidentally open the "See All" modal.
        """
        page = self._page
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
                print(f"           [fill ivalua-autocomplete] id={inp_id!r} name={inp_name!r}")
                loc = (
                    page.locator(f"#{inp_id}").first
                    if inp_id
                    else page.locator(f'input[name="{inp_name}"]').first
                )
                # focus() activates the input without triggering Ivalua's click
                # handlers (which can open/close the dropdown and corrupt XHR state).
                # Ctrl+A selects any stale content; press_sequentially replaces the
                # selection character-by-character to fire per-keystroke XHR searches.
                loc.focus(timeout=2000)
                page.wait_for_timeout(150)
                page.keyboard.press("Control+a")
                page.wait_for_timeout(50)
                loc.press_sequentially(value, delay=80)
                page.wait_for_timeout(3000)
                self._last_selectors = self._extract_selectors(loc)
                return f"filled '{label}' with '{value}' (ivalua-autocomplete)"
            else:
                print(f"           [fill ivalua-autocomplete] not found: {result}")
        except Exception as e:
            print(f"           [fill ivalua-autocomplete] failed: {e}")
        return None

    def _fill_combobox_aria(self, label: str, value: str) -> str | None:
        """
        Strategy: combobox with aria-label — handles both Ivalua widget types:

        1. Static dropdown (e.g. Procurement Method): click opens the full option
           list without typing. Detected by items appearing immediately after click.
           Return early so the LLM can call read_page and click the option.

        2. XHR autocomplete (e.g. Agency, Division): click opens an empty list;
           typing triggers a server search. Clear any existing chip/token first to
           prevent "EmergencyEmergency"-style doubling, then press_sequentially.
        """
        page = self._page
        try:
            loc = page.get_by_role("combobox", name=label).first
            if loc.count() == 0:
                return None

            print(f"           [fill combobox-aria] found combobox '{label}'")

            # --- Phase 1: click to open, check if static dropdown ---
            loc.click(timeout=2000)
            page.wait_for_timeout(1200)   # allow dropdown animation / data load

            items_visible = page.evaluate("""() => {
                const containers = document.querySelectorAll(
                    'ul[role="listbox"], .iv-menu-container ul, .scrolling.menu.visible'
                );
                for (const c of containers) {
                    if (c.offsetParent) {
                        return c.querySelectorAll('li, [role="option"]').length;
                    }
                }
                return 0;
            }""")

            if items_visible > 0:
                # Static dropdown: options already visible — do NOT type.
                # Return a message so the LLM knows to call read_page then click.
                print(f"           [fill combobox-aria] static dropdown: {items_visible} items visible — skipping type")
                self._last_selectors = self._extract_selectors(loc)
                return (
                    f"opened dropdown '{label}' — {items_visible} options are visible. "
                    f"Call read_page to see them, then click your choice."
                )

            # --- Phase 2: XHR autocomplete — clear existing value then type ---
            # Step 1: dismiss any selected chip/token via delete button (Ivalua chip widget)
            try:
                del_btn = loc.locator(
                    "xpath=following::button[contains(@title,'Delete') "
                    "or contains(text(),'Delete')]"
                ).first
                if del_btn.count() == 0:
                    page.evaluate(f"""() => {{
                        const cb = document.querySelector('[aria-label="{label}"]')
                                || document.querySelector('[name="{label}"]');
                        if (!cb) return;
                        const container = cb.closest('[data-iv-role="controlWrapper"]')
                                       || cb.parentElement;
                        if (!container) return;
                        const btn = container.querySelector(
                            'button[title*="Delete"], button[aria-label*="Delete"]'
                        );
                        if (btn) btn.click();
                    }}""")
                    page.wait_for_timeout(300)
                else:
                    del_btn.click(timeout=1000)
                    page.wait_for_timeout(300)
            except Exception:
                pass

            # Step 2: focus → Ctrl+A → press_sequentially (same safe pattern as
            # _fill_ivalua_autocomplete — avoids click side-effects on the widget).
            loc.focus(timeout=2000)
            page.wait_for_timeout(150)
            page.keyboard.press("Control+a")
            page.wait_for_timeout(50)
            loc.press_sequentially(value, delay=80)
            page.wait_for_timeout(3000)
            self._last_selectors = self._extract_selectors(loc)
            return f"filled '{label}' with '{value}' (combobox-aria)"
        except Exception as e:
            print(f"           [fill combobox-aria] failed: {e}")
        return None

    def _fill_date_end(self, slug: str, value: str) -> str | None:
        """
        Strategy: Ivalua date-range end field ("to:" heading layout).

        Ivalua renders date range end inputs after an <h*> element whose text
        starts with "to:". Sibling-walk fails when the input is in a nested
        subtree; a DOM TreeWalker traverses the full document tree instead.
        """
        if slug not in ("to", "enddate", "todate", "contractperiodend"):
            return None

        page = self._page
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
                loc = (
                    page.locator(f"#{inp_id}").first
                    if inp_id
                    else page.locator(f'input[name="{inp_name}"]').first
                )
                print(f"           [fill date-end] id={inp_id!r}")
                loc.fill(value, timeout=2000)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                self._last_selectors = self._extract_selectors(loc)
                return f"filled 'to' date with '{value}' (ivalua-date-end)"
            else:
                print(f"           [fill date-end] not found: {result}")
        except Exception as e:
            print(f"           [fill date-end] failed: {e}")
        return None
