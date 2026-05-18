"""
Ivalua platform adapter.

Extends BrowserSession with Ivalua-specific DOM interaction strategies.
All knowledge of Ivalua's widget patterns (data-iv-role, iv-menu-container,
chip/token deletion, "to:" date heading) lives here — not in the core tools.

Element selector strategies are declared in ivalua_elements.yaml (the element
registry). Python methods load strategies from the registry at runtime via
_get_selectors() and resolve the best live match via _discover_element() —
so selector updates never require touching Python code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import ClassVar

from auditor.tools import BrowserSession


@dataclass
class IvaluaBrowserSession(BrowserSession):

    # ------------------------------------------------------------------
    # Behavioral patterns — injected into LLM system prompt at run start.
    # These are planning-layer rules the LLM needs before it acts.
    # DOM-level strategies (how to execute) live in the methods below.
    # ------------------------------------------------------------------
    """BrowserSession configured for Ivalua SaaS procurement platform."""

    # Instance-level selector cache: maps element_key → winning CSS selector for this session.
    _selector_cache: dict = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # Platform guidance — loaded from ivalua_guidance.md at import time
    # ------------------------------------------------------------------

    _guidance: ClassVar[str | None] = None
    PLATFORM_GUIDANCE: ClassVar[str] = ""  # populated by _init_guidance() below

    @classmethod
    def _init_guidance(cls) -> None:
        if cls._guidance is None:
            from pathlib import Path
            cls._guidance = (Path(__file__).parent / "ivalua_guidance.md").read_text()
            cls.PLATFORM_GUIDANCE = cls._guidance

    # ------------------------------------------------------------------
    # Element registry — load once per class, cache forever
    # ------------------------------------------------------------------

    _registry: ClassVar[dict | None] = None   # class-level cache, loaded once

    @classmethod
    def _load_registry(cls) -> dict:
        if cls._registry is None:
            from pathlib import Path
            import yaml
            p = Path(__file__).parent / "ivalua_elements.yaml"
            cls._registry = yaml.safe_load(p.read_text())
        return cls._registry

    def _get_selectors(self, element_key: str) -> list[str]:
        """Return the ordered CSS selector strategies for a named element."""
        entry = self._load_registry().get(element_key, {})
        return entry.get("selectors", [])

    def _discover_element(self, element_key: str) -> str | None:
        """
        Find the CSS selector for a named element on the current page.

        Tries each strategy from the registry in order. Caches the winning
        selector for the rest of the session. On cache miss (selector no longer
        matches), re-runs discovery automatically.

        Returns the CSS selector string, or None if no strategy matched.
        """
        # Check session cache first
        cached = self._selector_cache.get(element_key)
        if cached:
            try:
                if self._page.evaluate(f"!!document.querySelector({json.dumps(cached)})"):
                    return cached
                # Cached selector no longer works — re-discover
                print(f"           [discover] stale cache for {element_key!r}, re-discovering")
                del self._selector_cache[element_key]
            except Exception:
                self._selector_cache.pop(element_key, None)  # clear silently

        # Try each strategy from registry
        for selector in self._get_selectors(element_key):
            try:
                found = self._page.evaluate(
                    f"!!document.querySelector({json.dumps(selector)})"
                )
                if found:
                    self._selector_cache[element_key] = selector
                    print(f"           [discover] {element_key!r} → {selector!r}")
                    return selector
            except Exception:
                continue

        # Scan visible Ivalua-semantic elements on the page as hints for the developer
        try:
            candidates = self._page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll(
                    '[data-iv-role], [data-iv-control-type-name], button[type="submit"], ' +
                    'button[name], input[name], [class*="iv-filter"]'
                )).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }).slice(0, 20);
                return els.map(el => ({
                    tag:   el.tagName,
                    id:    el.id || '',
                    name:  el.getAttribute('name') || '',
                    role:  el.getAttribute('data-iv-role') || '',
                    type:  el.getAttribute('type') || '',
                    text:  el.textContent.trim().slice(0, 50),
                    ivCls: Array.from(el.classList).filter(c => c.startsWith('iv-')).join(' ')
                }));
            }""")
            if candidates:
                print(f"           [discover] visible Ivalua elements on page (add to ivalua_elements.yaml):")
                for c in candidates:
                    parts = [f"<{c['tag']}>"]
                    if c['id']:    parts.append(f"id={c['id']!r}")
                    if c['name']:  parts.append(f"name={c['name']!r}")
                    if c['role']:  parts.append(f"data-iv-role={c['role']!r}")
                    if c['type']:  parts.append(f"type={c['type']!r}")
                    if c['ivCls']: parts.append(f"class={c['ivCls']!r}")
                    if c['text']:  parts.append(f"text={c['text']!r}")
                    print(f"             {' '.join(parts)}")
        except Exception:
            pass
        print(f"           [discover] {element_key!r}: no strategy matched on this page")
        return None

    # ------------------------------------------------------------------
    # Browse list page — Search button + chip multiselect
    # ------------------------------------------------------------------

    def _trigger_panel_search(self) -> str | None:
        """Click the left filter panel Search button via JavaScript.

        Browse list pages have two Search buttons:
          - Left panel (filter): name contains "cmdSearchBtn" and "FilterBar"
            — submits the active filter panel selections (Status, Agency, etc.)
          - Main content header: a separate Search input/button for keyword search

        Text-based click strategies cannot reliably distinguish them, and
        Playwright actionability checks fail when a chip-multiselect dropdown
        is open on top of the button. This method uses _discover_element() to
        resolve the best live CSS selector from the registry, then fires a JS
        click unconditionally to bypass Playwright actionability checks.
        """
        page = self._page
        try:
            selector = self._discover_element('panel_search_button')
            if not selector:
                return None
            css = selector.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
            found = page.evaluate(f"""() => {{
                const btn = document.querySelector('{css}');
                if (!btn) return false;
                btn.click();
                return true;
            }}""")
            if found:
                page.wait_for_load_state("networkidle", timeout=8000)
                self._last_selectors = [
                    {"type": "css", "value": selector, "successes": 1, "failures": 0}
                ]
                print(f"           [panel-search] clicked via discovered selector: {selector!r}")
                return "clicked panel Search button"
        except Exception as e:
            print(f"           [panel-search] failed: {e}")
        return None

    def _fill_chip_multiselect(self, label: str, value: str) -> str | None:
        """Add a value to a chip/token multi-select field (e.g. PO Type).

        Ivalua chip fields look like a tag input — typing triggers a dropdown,
        clicking the item adds it as a chip. Delegates to the autocomplete
        strategy since the widget behaves identically after the first click.
        """
        page = self._page
        try:
            label_selectors = json.dumps(self._get_selectors('field_label'))
            wrapper_selectors = json.dumps(self._get_selectors('field_control_wrapper'))
            input_selectors = json.dumps(self._get_selectors('autocomplete_input'))
            result = page.evaluate(f"""(lbl) => {{
                const labelSels = {label_selectors};
                const wrapperSels = {wrapper_selectors};
                const inputSels = {input_selectors};

                const labels = Array.from(document.querySelectorAll(labelSels.join(', ')));
                const match = labels.find(el => {{
                    const t = el.textContent.trim().replace(/\\s*\\*\\s*$/, '').trim();
                    return t === lbl || t.startsWith(lbl);
                }});
                if (!match) return {{found: false, reason: 'label not found'}};

                let wrapper = null;
                for (const sel of wrapperSels) {{
                    wrapper = match.closest(sel);
                    if (wrapper) break;
                }}
                if (!wrapper) wrapper = match.parentElement;
                if (!wrapper) return {{found: false, reason: 'no wrapper'}};

                let inp = null;
                for (const sel of inputSels) {{
                    inp = wrapper.querySelector(sel);
                    if (inp) break;
                }}
                if (!inp) return {{found: false, reason: 'no input'}};
                return {{found: true, id: inp.id, name: inp.name}};
            }}""", label)

            if result and result.get("found"):
                inp_id = result.get("id")
                loc = page.locator(f"#{inp_id}").first if inp_id else None
                if not loc:
                    return None
                loc.click(timeout=2000)
                page.wait_for_timeout(150)
                loc.press_sequentially(value, delay=80)
                clicked = self._click_autocomplete_item(value)
                self._last_selectors = self._extract_selectors(loc)
                return (
                    f"added chip '{value}' to '{label}' (chip-multiselect)"
                    if clicked else
                    f"typed '{value}' in '{label}' — chip may not have appeared"
                )
            else:
                print(f"           [chip-multiselect] not found: {result}")
        except Exception as e:
            print(f"           [chip-multiselect] failed: {e}")
        return None

    def _clear_chip(self, chip_text: str) -> str | None:
        """Remove a specific chip/token from a multi-select field by clicking its × button.

        Searches all visible chip delete buttons for one whose sibling text
        matches chip_text (exact, then contains). Used when a filter chip
        needs to be cleared before setting a fresh value.
        """
        page = self._page
        js_text = chip_text.replace("\\", "\\\\").replace('"', '\\"').lower()
        chip_selectors = json.dumps(self._get_selectors('chip_widget'))
        delete_selectors = json.dumps(self._get_selectors('chip_delete_button'))
        try:
            result = page.evaluate(f"""() => {{
                const chipSels = {chip_selectors};
                const deleteSels = {delete_selectors};
                const chips = document.querySelectorAll(chipSels.join(', '));
                for (const chip of chips) {{
                    const text = chip.textContent.trim().toLowerCase();
                    if (text === "{js_text}" || text.includes("{js_text}")) {{
                        let del = null;
                        for (const sel of deleteSels) {{
                            del = chip.querySelector(sel);
                            if (del) break;
                        }}
                        if (del) {{ del.click(); return {{removed: chip.textContent.trim()}}; }}
                    }}
                }}
                return {{removed: null}};
            }}""")
            if result and result.get("removed"):
                page.wait_for_timeout(400)
                print(f"           [clear-chip] removed chip: {result['removed']!r}")
                return f"removed chip '{result['removed']}'"
        except Exception as e:
            print(f"           [clear-chip] failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Browse result table — click a record link by text
    # ------------------------------------------------------------------

    def _click_result_row_link(self, text: str) -> str | None:
        """Click a link or table cell in browse list results by exact text match.

        Ivalua browse list tables render record IDs (PO IDs, requisition IDs,
        etc.) as <a> links in the first column.  Playwright's generic click
        strategies (get_by_role/get_by_text) fail with "not visible" or timeout
        when the element is inside an overflow:hidden table container whose
        scroll position keeps the row outside the viewport — a common situation
        on Ivalua list pages with many results.

        This strategy uses JavaScript to:
          1. Find the link by exact text (falls back to contains).
          2. Call scrollIntoView() to bring it into the visible viewport.
          3. Click it atomically in the same JS frame (avoids the race condition
             where Playwright re-checks visibility between scroll and click).

        getBoundingClientRect() is used for the post-scroll visibility check
        because offsetParent is null for position:fixed/absolute children of
        overflow:hidden containers in headless Chrome.
        """
        page = self._page
        js_text = text.replace("\\", "\\\\").replace('"', '\\"')
        link_selectors = json.dumps(self._get_selectors('results_table_link'))
        cell_selectors = json.dumps(self._get_selectors('results_table_cell'))
        try:
            result = page.evaluate(f"""() => {{
                const search = "{js_text}";
                const linkSels = {link_selectors};
                const cellSels = {cell_selectors};

                // Search for <a> tags inside tbody data cells only —
                // excludes thead/th column headers and nav links.
                const linkCandidates = Array.from(
                    document.querySelectorAll(linkSels.join(', '))
                );
                let el = linkCandidates.find(e => e.textContent.trim() === search);
                if (!el) el = linkCandidates.find(e => e.textContent.trim().includes(search));

                if (el) {{
                    el.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
                    // Verify it's now in viewport after scroll
                    const r = el.getBoundingClientRect();
                    el.click();
                    return {{clicked: el.textContent.trim(), tag: el.tagName, visible: r.width > 0}};
                }}

                // Fallback: <td> cells in tbody with onclick (some Ivalua grids use td-level handlers)
                const tdCandidates = Array.from(
                    document.querySelectorAll(cellSels.join(', '))
                );
                const td = tdCandidates.find(e => e.textContent.trim() === search)
                        || tdCandidates.find(e => e.textContent.trim().includes(search));
                if (td) {{
                    td.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
                    // Walk up to the nearest clickable ancestor (a, button, onclick)
                    let target = td;
                    for (let i = 0; i < 4 && target && target !== document.body; i++) {{
                        if (target.onclick || target.tagName === 'A' || target.tagName === 'BUTTON') {{
                            target.click();
                            return {{clicked: target.textContent.trim(), tag: target.tagName, via: 'walk-up'}};
                        }}
                        target = target.parentElement;
                    }}
                    td.click();
                    return {{clicked: td.textContent.trim(), tag: 'TD', via: 'direct'}};
                }}

                return null;
            }}""")

            if result:
                clicked = result.get("clicked", "")
                tag = result.get("tag", "")
                via = result.get("via", "")
                page.wait_for_load_state("networkidle", timeout=10000)
                print(f"           [result-row-link] clicked <{tag}>: {clicked!r} {via or ''}")
                self._last_selectors = [
                    {"type": "text", "value": clicked, "successes": 1, "failures": 0}
                ]
                return f"clicked result link '{clicked}'"
        except Exception as e:
            print(f"           [result-row-link] failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Click: priority hook
    # ------------------------------------------------------------------

    def _platform_click_priority(self, desc: str) -> str | None:
        """
        Ivalua-specific priority click handler. Runs before generic strategies.

        1. Search button disambiguation: if desc is "Search" (or close variant),
           try the left panel Search button first to avoid hitting the header Search.
        2. Listbox/autocomplete: when a dropdown is open, target visible listbox
           containers before falling through to generic get_by_text strategies.
        """
        desc_lower = desc.lower().strip()

        # -- Search button disambiguation ----------------------------------
        # Route to the left panel Search button for both the explicit panel
        # description and the plain "Search" fallback the LLM sometimes uses.
        # "main search" / "keyword search" are excluded so they fall through
        # to generic strategies and hit the main-pane Search button instead.
        _panel_search_triggers = (
            "filter panel search", "panel search", "left panel search",
            "search",   # exact bare "Search" — most common LLM fallback
        )
        _main_search_exclusions = ("main search", "keyword search", "header search")
        if (any(desc_lower == t for t in _panel_search_triggers)
                and not any(desc_lower == ex for ex in _main_search_exclusions)):
            result = self._trigger_panel_search()
            if result:
                return result
            # Panel button not found on this page — fall through to generic strategies
        page = self._page
        js_desc = desc.replace("\\", "\\\\").replace('"', '\\"')
        listbox_selectors = json.dumps(self._get_selectors('autocomplete_dropdown'))
        try:
            result = page.evaluate(f"""() => {{
                const containerSelectors = {listbox_selectors};
                for (const sel of containerSelectors) {{
                    const containers = document.querySelectorAll(sel);
                    for (const lb of containers) {{
                        // Use getBoundingClientRect instead of offsetParent —
                        // offsetParent is null for position:fixed/absolute in headless
                        // Chrome even when the element is visually present.
                        const rect = lb.getBoundingClientRect();
                        if (rect.width === 0 && rect.height === 0) continue;
                        const items = Array.from(lb.querySelectorAll('li, [role="option"], a, span'));
                        // Exact match first
                        let item = items.find(el => el.textContent.trim() === "{js_desc}");
                        // Fall back to contains match
                        if (!item) item = items.find(el => el.textContent.trim().includes("{js_desc}"));
                        if (item) {{
                            item.click();
                            return {{text: item.textContent.trim(), id: item.id || ''}};
                        }}
                    }}
                }}
                return null;
            }}""")
            if result:
                # Wait for Ivalua's conditional-field logic to finish rendering.
                # 800ms is not enough when selecting a value triggers multiple
                # conditional question groups (e.g. Emergency → 3 questions).
                page.wait_for_timeout(2500)
                item_text = result.get("text", "")
                print(f"           [ivalua-listbox] clicked item: {item_text!r}")
                # Store a text selector — listbox <li> items rarely have IDs,
                # but text is stable and works for replay via get_by_text().
                if item_text:
                    self._last_selectors = [{"type": "text", "value": item_text,
                                             "successes": 1, "failures": 0}]
                else:
                    self._last_selectors = []
                return f"clicked '{desc}' (ivalua-listbox)"
        except Exception as e:
            print(f"           [ivalua-listbox] failed: {e}")

        # -- Browse list result table links --------------------------------
        # Record IDs (PO IDs, requisition IDs, etc.) appear as <a> links in
        # the first column of browse result tables. Try the dedicated strategy
        # only when no dropdown/listbox is currently open (to avoid clicking
        # a table cell instead of a listbox option when both are visible).
        # Only try the table-link strategy for compact single-token identifiers
        # (no spaces, e.g. "PO074788") — never for common interactive button
        # labels (Submit, Cancel, Save, etc.) or UI labels like "Search".
        # These must fall through to generic role="button" / get_by_text strategies.
        _INTERACTIVE_LABELS: frozenset[str] = frozenset({
            "submit", "cancel", "save", "ok", "yes", "no", "close",
            "delete", "edit", "create", "add", "remove", "reset",
            "apply", "confirm", "next", "back", "previous", "continue",
            "done", "finish", "proceed", "update", "approve", "reject",
            "send", "export", "import", "upload", "download", "print",
            "refresh", "reload", "clear", "open", "view", "select",
        })
        if ' ' not in desc and desc.lower() not in _INTERACTIVE_LABELS:
            try:
                listbox_open = page.evaluate(f"""() => {{
                    const sels = {listbox_selectors};
                    for (const sel of sels) {{
                        const lbs = document.querySelectorAll(sel);
                        for (const lb of lbs) {{
                            const r = lb.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return true;
                        }}
                    }}
                    return false;
                }}""")
                if not listbox_open:
                    result = self._click_result_row_link(desc)
                    if result:
                        return result
            except Exception as e:
                print(f"           [result-row-link guard] failed: {e}")

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

    def _click_autocomplete_item(self, value: str) -> str | None:
        """
        Ivalua-specific: after typing into an autocomplete field, poll for a
        matching dropdown item and click it atomically (up to 8 s, 1 s per try).

        Uses getBoundingClientRect for visibility — offsetParent is null for
        absolutely-positioned dropdowns in headless Chrome even when the element
        is visually present, so offsetParent checks always fail in Docker/CI.

        Match strategy (in priority order, all case-insensitive):
          1. Exact match       — "Emergency" → "Emergency"
          2. startsWith match  — "Department" → "Department of Finance"
          3. includes match    — "yard" → '"D" YARD INTERNATIONAL, INC'

        Searches ALL li and [role="option"] elements inside visible dropdown
        containers resolved from the registry — the Ivalua dropdown structure
        varies across widget types and versions, and the only reliable marker
        is the item text itself.
        """
        page = self._page
        js_value = value.replace("\\", "\\\\").replace('"', '\\"').lower()
        container_selectors = json.dumps(self._get_selectors('autocomplete_dropdown'))

        for attempt in range(8):
            page.wait_for_timeout(1000)
            try:
                result = page.evaluate(f"""() => {{
                    const search = "{js_value}";

                    // ONLY search inside visible dropdown containers — never scan the
                    // full page DOM, which picks up navigation menus and hidden elements.
                    const containerSelectors = {container_selectors};
                    let candidates = [];
                    for (const sel of containerSelectors) {{
                        const containers = document.querySelectorAll(sel);
                        for (const c of containers) {{
                            const rect = c.getBoundingClientRect();
                            if (rect.width === 0 && rect.height === 0) continue;
                            // Container is visible — collect its items
                            const items = Array.from(c.querySelectorAll(
                                'li, [role="option"], a, span'
                            ));
                            candidates.push(...items);
                        }}
                    }}
                    // Deduplicate by DOM reference
                    candidates = [...new Set(candidates)];

                    const candidateInfo = candidates.map(el => {{
                        const text = (el.textContent || '').trim().substring(0, 60);
                        const rect = el.getBoundingClientRect();
                        return text + (rect.width > 0 && rect.height > 0 ? ' [visible]' : ' [hidden]');
                    }});

                    for (const el of candidates) {{
                        const text = (el.textContent || '').trim();
                        const textLower = text.toLowerCase();
                        if (textLower === search || textLower.startsWith(search) || textLower.includes(search)) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {{
                                el.click();
                                return {{clicked: text, candidates: candidateInfo}};
                            }}
                        }}
                    }}
                    return {{clicked: null, candidates: candidateInfo}};
                }}""")
                if result:
                    candidates = result.get("candidates", [])
                    clicked = result.get("clicked")
                    print(f"           [autocomplete-click] attempt {attempt + 1}: {len(candidates)} candidates — {candidates[:10]}")
                    if clicked:
                        page.wait_for_timeout(2500)
                        print(f"           [autocomplete-click] selected '{clicked}'")
                        return clicked
            except Exception as e:
                print(f"           [autocomplete-click] attempt {attempt + 1} error: {e}")

        print(f"           [autocomplete-click] no item found for '{value}' after 8 s")
        return None

    # ------------------------------------------------------------------
    # select_option override — dismiss open dropdown after selection
    # ------------------------------------------------------------------

    def select_option(self, field_label: str, option_value: str) -> str:
        """Select an option and close any open dropdown that Ivalua leaves open.

        Ivalua's chip multi-select widget (Status, PO Type, etc.) keeps the
        dropdown open after selecting an item — the selected option becomes a
        chip and the remaining options stay visible for further selections.
        If left open, the LLM sees an active listbox in the next read_page and
        gets confused about whether the selection was applied.

        This override calls the base select_option, then presses Escape to
        dismiss the dropdown, giving the LLM a clean page state on return.
        Escape only closes the open dropdown — it does NOT close the filter
        panel (tested: the panel remains open after Escape on browse list pages).
        """
        result = super().select_option(field_label, option_value)
        if "error" not in result.lower():
            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(400)
            except Exception:
                pass
        return result

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
            or self._fill_chip_multiselect(label, value)
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
        label_selectors = json.dumps(self._get_selectors('field_label'))
        wrapper_selectors = json.dumps(self._get_selectors('field_control_wrapper'))
        input_selectors = json.dumps(
            [s for s in self._get_selectors('autocomplete_input')
             if 'combobox' in s or 'search' in s or 'autocomplete' in s]
        )
        try:
            result = page.evaluate(f"""(lbl) => {{
                const labelSels = {label_selectors};
                const wrapperSels = {wrapper_selectors};
                const inputSels = {input_selectors};

                const labelEls = Array.from(document.querySelectorAll(labelSels.join(', ')));
                const match = labelEls.find(el => {{
                    const txt = el.textContent.trim().replace(/\\s*\\*\\s*$/, '').trim();
                    return txt === lbl || txt.startsWith(lbl);
                }});
                if (!match) return {{found: false, reason: 'label not found'}};

                let wrapper = null;
                for (const sel of wrapperSels) {{
                    wrapper = match.closest(sel);
                    if (wrapper) break;
                }}
                if (!wrapper) wrapper = match.parentElement;
                if (!wrapper) return {{found: false, reason: 'no wrapper'}};

                let inp = null;
                for (const sel of inputSels) {{
                    inp = wrapper.querySelector(sel);
                    if (inp) break;
                }}
                if (!inp) return {{found: false, reason: 'no search input'}};
                return {{found: true, id: inp.id, name: inp.name}};
            }}""", label)

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
                # Use click() rather than focus() — in headless Docker the
                # Division field is often below the fold and focus() doesn't
                # scroll it into view or fire the mouse events that Ivalua
                # needs to activate its autocomplete XHR listener.
                # click() auto-scrolls + dispatches the full mouse event chain.
                loc.click(timeout=2000)
                page.wait_for_timeout(150)
                page.keyboard.press("Control+a")
                page.wait_for_timeout(50)
                loc.press_sequentially(value, delay=80)
                # Atomically click the matching dropdown item.
                # _click_autocomplete_item polls for up to 8 s and uses
                # getBoundingClientRect (not offsetParent) so it works in
                # headless Docker/CI where offsetParent is null for positioned
                # elements. Falls back to None if item never appears.
                clicked = self._click_autocomplete_item(value)
                self._last_selectors = self._extract_selectors(loc)
                if clicked:
                    return f"filled '{label}' with '{value}' — selected '{clicked}' (ivalua-autocomplete)"
                print(f"           [fill ivalua-autocomplete] typed but dropdown did not appear for '{value}' — XHR may not have fired")
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
        dropdown_selector_list = self._get_selectors('autocomplete_dropdown')
        try:
            loc = page.get_by_role("combobox", name=label).first
            if loc.count() == 0:
                return None

            print(f"           [fill combobox-aria] found combobox '{label}'")

            # --- Phase 1: click to open, check if static dropdown ---
            loc.click(timeout=2000)
            page.wait_for_timeout(1200)   # allow dropdown animation / data load

            items_visible = page.evaluate(f"""() => {{
                const containers = document.querySelectorAll(
                    {json.dumps(', '.join(dropdown_selector_list))}
                );
                for (const c of containers) {{
                    if (c.offsetParent) {{
                        return c.querySelectorAll('li, [role="option"]').length;
                    }}
                }}
                return 0;
            }}""")

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
            # Atomically click the matching dropdown item — same approach as
            # _fill_ivalua_autocomplete (see comment there for rationale).
            clicked = self._click_autocomplete_item(value)
            self._last_selectors = self._extract_selectors(loc)
            if clicked:
                return f"filled '{label}' with '{value}' — selected '{clicked}' (combobox-aria)"
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


# Populate PLATFORM_GUIDANCE at import time so run.py can access it as a class attribute.
IvaluaBrowserSession._init_guidance()
