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

from auditor.logger import get_logger
from auditor.tools import BrowserSession

log = get_logger(__name__)


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
                log.debug("[discover] stale cache, re-discovering", extra={"event": "discover", "element_key": element_key})
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
                    log.debug("[discover] resolved selector", extra={"event": "discover", "element_key": element_key, "selector": selector})
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
                log.debug("[discover] visible Ivalua elements on page (add to ivalua_elements.yaml)", extra={"event": "discover", "candidates": candidates})
        except Exception:
            pass
        log.debug("[discover] no strategy matched on this page", extra={"event": "discover", "element_key": element_key})
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
                log.debug("[panel-search] clicked via discovered selector", extra={"event": "panel_search", "selector": selector})
                return "clicked panel Search button"
        except Exception as e:
            log.debug("[panel-search] failed", extra={"event": "panel_search", "error": str(e)})
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
                loc = page.locator(f'xpath=//*[@id="{inp_id}"]').first if inp_id else None
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
                log.debug("[chip-multiselect] not found", extra={"event": "chip_multiselect", "result": result})
        except Exception as e:
            log.debug("[chip-multiselect] failed", extra={"event": "chip_multiselect", "error": str(e)})
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
                log.debug("[clear-chip] removed chip", extra={"event": "clear_chip", "removed": result['removed']})
                return f"removed chip '{result['removed']}'"
        except Exception as e:
            log.debug("[clear-chip] failed", extra={"event": "clear_chip", "error": str(e)})
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
                    return {{clicked: el.textContent.trim().slice(0, 100), tag: el.tagName, visible: r.width > 0}};
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
                            // Use search string, not target.textContent — ancestors like nav
                            // buttons contain all descendant text (entire dropdown menus).
                            return {{clicked: search, tag: target.tagName, via: 'walk-up'}};
                        }}
                        target = target.parentElement;
                    }}
                    td.click();
                    return {{clicked: td.textContent.trim().slice(0, 100), tag: 'TD', via: 'direct'}};
                }}

                return null;
            }}""")

            if result:
                clicked = result.get("clicked", "")
                tag = result.get("tag", "")
                via = result.get("via", "")
                page.wait_for_load_state("networkidle", timeout=10000)
                log.debug("[result-row-link] clicked element", extra={"event": "result_row_link", "tag": tag, "clicked": clicked, "via": via})
                # Use the original search text as the selector value, not the resolved
                # element's textContent. Walking up to a clickable ancestor (e.g. a nav
                # button wrapping a dropdown) produces textContent that includes all
                # descendant text — bookmark lists, request IDs, etc. — which is useless
                # as a replay selector and inflates the fingerprint file massively.
                self._last_selectors = [
                    {"type": "text", "value": text[:100], "successes": 1, "failures": 0}
                ]
                return f"clicked result link '{clicked}'"
        except Exception as e:
            log.debug("[result-row-link] failed", extra={"event": "result_row_link", "error": str(e)})
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
                log.debug("[ivalua-listbox] clicked item", extra={"event": "ivalua_listbox", "item_text": item_text})
                # Store a text selector — listbox <li> items rarely have IDs,
                # but text is stable and works for replay via get_by_text().
                # Only store a text selector if it is short enough to be a stable,
                # unambiguous match target. A long value means the includes() fallback
                # matched a large container (e.g. a select2 wrapper whose textContent
                # contains all option labels + embedded JS) — useless as a selector.
                if item_text and len(item_text) <= 200:
                    self._last_selectors = [{"type": "text", "value": item_text,
                                             "successes": 1, "failures": 0}]
                else:
                    self._last_selectors = []
                return f"clicked '{desc}' (ivalua-listbox)"
        except Exception as e:
            log.debug("[ivalua-listbox] failed", extra={"event": "ivalua_listbox", "error": str(e)})

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
                log.debug("[result-row-link guard] failed", extra={"event": "result_row_link_guard", "error": str(e)})

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
                            log.debug("[dismiss-modal] closed modal", extra={"event": "dismiss_modal", "button": btn_name})
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

    def _click_autocomplete_item(self, value: str, max_attempts: int = 4, scoped_selectors: list[str] | None = None) -> str | None:
        """Poll for a matching dropdown item and click it.

        Polls up to max_attempts times (1 s apart). Gives up early if the
        visible candidate list is stable for 2 consecutive polls — means the
        dropdown is not loading new results and continuing to wait is pointless.

        scoped_selectors: if provided, only search these container selectors.
          Use this to restrict search to the widget that was just opened (e.g.
          only 'ul.select2-results__options' for a select2 widget) so unrelated
          dropdowns on the page are never accidentally clicked.

        Match strategy (case-insensitive, in priority order):
          1. Exact match
          2. startsWith match
          3. includes match
        """
        page = self._page
        js_value = value.replace("\\", "\\\\").replace('"', '\\"').lower()
        if scoped_selectors:
            container_selectors = json.dumps(scoped_selectors)
        else:
            container_selectors = json.dumps(self._get_selectors('autocomplete_dropdown'))

        prev_visible_texts: list[str] = []
        stable_count = 0

        for attempt in range(max_attempts):
            page.wait_for_timeout(1000)
            try:
                result = page.evaluate(f"""() => {{
                    const search = "{js_value}";
                    const containerSelectors = {container_selectors};

                    const getXPath = (el) => {{
                        const parts = [];
                        while (el && el.nodeType === 1) {{
                            let idx = 1;
                            let sib = el.previousElementSibling;
                            while (sib) {{ if (sib.tagName === el.tagName) idx++; sib = sib.previousElementSibling; }}
                            parts.unshift(el.tagName.toLowerCase() + (idx > 1 ? `[${{idx}}]` : ''));
                            el = el.parentElement;
                            if (parts.length >= 5) {{ parts.unshift('...'); break; }}
                        }}
                        return '/' + parts.join('/');
                    }};

                    let candidates = [];
                    let matchedContainers = [];
                    // Search containers in priority order — stop at the first container
                    // that has visible items. This prevents cross-container contamination
                    // (e.g. an always-visible saved-searches listbox matching after the
                    // real results container). autocomplete_dropdown selector order encodes priority.
                    for (const sel of containerSelectors) {{
                        const containers = document.querySelectorAll(sel);
                        let containerCandidates = [];
                        for (const c of containers) {{
                            const rect = c.getBoundingClientRect();
                            if (rect.width === 0 && rect.height === 0) continue;
                            matchedContainers.push({{
                                selector: sel,
                                cls: (c.className || '').slice(0, 60),
                                xpath: getXPath(c),
                            }});
                            containerCandidates.push(...Array.from(c.querySelectorAll(
                                'li, [role="option"], div.ss-option, a, span'
                            )));
                        }}
                        // Only use this container group if it has visible items
                        const visibleInGroup = containerCandidates.filter(el => {{
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }});
                        if (visibleInGroup.length > 0) {{
                            candidates = [...new Set(containerCandidates)];
                            break;
                        }}
                    }}

                    const candidateInfo = candidates.map(el => {{
                        const text = (el.textContent || '').trim().substring(0, 60);
                        const rect = el.getBoundingClientRect();
                        const vis = rect.width > 0 && rect.height > 0 ? '[visible]' : '[hidden]';
                        return `${{text}} ${{vis}} xpath=${{getXPath(el)}}`;
                    }});

                    const visibleTexts = candidates
                        .filter(el => {{ const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }})
                        .map(el => (el.textContent || '').trim().substring(0, 60));

                    for (const el of candidates) {{
                        const text = (el.textContent || '').trim();
                        const textLower = text.toLowerCase();
                        if (textLower === search || textLower.startsWith(search) || textLower.includes(search)) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {{
                                // Return coordinates instead of el.click() — select2 needs real mousedown/mouseup
                                return {{
                                    clicked: text,
                                    clickX: rect.left + rect.width / 2,
                                    clickY: rect.top + rect.height / 2,
                                    candidates: candidateInfo,
                                    matchedContainers,
                                    visibleTexts
                                }};
                            }}
                        }}
                    }}
                    return {{clicked: null, candidates: candidateInfo, matchedContainers, visibleTexts}};
                }}""")
                if result:
                    candidates = result.get("candidates", [])
                    clicked = result.get("clicked")
                    click_x = result.get("clickX")
                    click_y = result.get("clickY")
                    containers = result.get("matchedContainers", [])
                    visible_texts = result.get("visibleTexts", [])

                    log.debug("[autocomplete-click] attempt", extra={
                        "event": "ivalua_listbox",
                        "attempt": attempt + 1,
                        "candidate_count": len(candidates),
                        "visible_count": len(visible_texts),
                        "matched_containers": containers,
                        "candidates": candidates[:10],
                    })

                    if clicked:
                        if click_x is not None and click_y is not None:
                            # Native mouse click fires mousedown/mouseup/click — required by select2
                            page.mouse.click(click_x, click_y)
                        page.wait_for_timeout(2500)
                        log.debug("[autocomplete-click] selected item", extra={"event": "ivalua_listbox", "clicked": clicked, "x": click_x, "y": click_y})
                        return clicked

                    # Early exit if visible candidates unchanged — list is not loading
                    if visible_texts == prev_visible_texts:
                        stable_count += 1
                        if stable_count >= 2:
                            log.debug("[autocomplete-click] stable list, giving up early", extra={
                                "event": "ivalua_listbox", "attempt": attempt + 1, "visible": visible_texts
                            })
                            return None
                    else:
                        stable_count = 0
                    prev_visible_texts = visible_texts

            except Exception as e:
                log.debug("[autocomplete-click] attempt error", extra={"event": "ivalua_listbox", "attempt": attempt + 1, "error": str(e)})

        log.debug("[autocomplete-click] no item found", extra={"event": "ivalua_listbox", "value": value, "attempts": max_attempts})
        return None

    # ------------------------------------------------------------------
    # select_filter — filter panel combobox with no aria-label
    # ------------------------------------------------------------------

    def select_filter(self, filter_label: str, option_value: str, container_attribute: str = "") -> str:
        """Select a value in an unlabeled filter combobox.

        container_attribute: "attr=value" extracted by the LLM from hint HTML
          (e.g. "data-select2-id=33"). Python queries all elements with that attribute,
          finds search inputs inside each, and tries them in order.

        If container_attribute is empty, falls back to label proximity walk.
        Tries every candidate until one produces the target option.
        """
        page = self._page

        try:
            candidates = self._find_filter_inputs(filter_label, container_attribute)
            if not candidates:
                return f"error: could not find any input element for filter '{filter_label}'"

            log.debug("[select-filter] found candidates", extra={
                "event": "select_filter", "filter_label": filter_label,
                "count": len(candidates),
                "selectors": [f"{c['selector']}[{c['nth']}]" for c in candidates],
                "containers": [c.get("container_text", c.get("select_id", "")) for c in candidates],
            })

            for idx, candidate in enumerate(candidates):
                sel = candidate["selector"]
                nth = candidate["nth"]
                loc = page.locator(sel).nth(nth)
                log.debug("[select-filter] trying candidate", extra={
                    "event": "select_filter", "index": idx + 1, "total": len(candidates),
                    "selector": sel, "nth": nth,
                })

                # Click the input to open the dropdown.
                # Do NOT click the combobox ancestor first — that toggles the dropdown
                # open then the second click on the input closes it again.
                # A single click on the search input is sufficient for select2/slimselect.
                try:
                    loc.click(timeout=3000)
                    page.wait_for_timeout(500)
                except Exception as e:
                    log.debug("[select-filter] candidate click failed", extra={
                        "event": "select_filter", "index": idx + 1, "error": str(e),
                    })
                    continue

                # Check if dropdown opened (aria-expanded should be true after click)
                opened = page.evaluate(f"""() => {{
                    const all = Array.from(document.querySelectorAll({json.dumps(sel)}));
                    const inp = all[{nth}];
                    if (!inp) return false;
                    let el = inp.parentElement;
                    for (let i = 0; i < 6 && el; i++) {{
                        if (el.getAttribute('aria-expanded') === 'true') return true;
                        el = el.parentElement;
                    }}
                    return false;
                }}""")
                if not opened:
                    # Dropdown didn't open — try clicking the combobox ancestor explicitly
                    try:
                        page.evaluate(f"""() => {{
                            const all = Array.from(document.querySelectorAll({json.dumps(sel)}));
                            const inp = all[{nth}];
                            if (!inp) return;
                            let el = inp.parentElement;
                            for (let i = 0; i < 6 && el; i++) {{
                                if (el.getAttribute('role') === 'combobox') {{ el.click(); return; }}
                                el = el.parentElement;
                            }}
                        }}""")
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                log.debug("[select-filter] dropdown opened", extra={
                    "event": "select_filter", "index": idx + 1, "opened": opened,
                })

                result = self._select_filter_type_and_click(loc, filter_label, option_value)
                if not result.startswith("typed "):
                    return result

                # This candidate's dropdown didn't contain the option — close and try next
                log.debug("[select-filter] candidate exhausted, trying next", extra={
                    "event": "select_filter", "index": idx + 1, "selector": sel,
                })
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
                except Exception:
                    pass

            tried = [f"{c['selector']}[{c['nth']}]" for c in candidates]
            return f"error: '{option_value}' not found for filter '{filter_label}' — tried {tried} — add scoped_selector to hint if wrong widget"

        except Exception as e:
            log.debug("[select-filter] failed", extra={"event": "select_filter", "error": str(e)})
            return f"error: select_filter failed for '{filter_label}': {e}"

    def _find_filter_inputs(self, filter_label: str, container_attribute: str) -> list[dict]:
        """Return candidate search inputs as [{"selector": css, "nth": N}] dicts.

        With container_attribute ("attr=value" from LLM):
          Query all containers matching [attr="value"], find the search input inside
          each one, return each as a separate candidate for the caller to try.

        Without: fall back to label proximity walk using hidden <select> as anchor.
        """
        page = self._page
        js_label = filter_label.strip().replace("\\", "\\\\").replace('"', '\\"')

        if container_attribute and " " in container_attribute:
            # Full CSS selector passed directly (e.g. "[data-select2-id='33'] input.select2-search__field")
            # Treat as direct input selector — find all matching elements
            direct_sel = container_attribute.strip()
            direct_json = json.dumps(direct_sel)
            return page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll({direct_json}));
                return all.map((inp, nth) => {{
                    const inpCls = (inp.className || '').trim().split(' ')[0];
                    return {{ selector: {direct_json}, nth, container_text: '' }};
                }});
            }}""")

        if container_attribute and "=" in container_attribute:
            # Parse "attr=value" → CSS attribute selector [attr="value"]
            attr, _, value = container_attribute.partition("=")
            attr = attr.strip()
            value = value.strip().strip("\"'")
            css_attr = f'[{attr}="{value}"]'
            attr_json = json.dumps(css_attr)
            return page.evaluate(f"""() => {{
                const containers = Array.from(document.querySelectorAll({attr_json}));
                const results = [];
                for (const container of containers) {{
                    const inp = container.querySelector('input[role="searchbox"], input[type="search"]');
                    if (!inp) continue;
                    const inpCls = (inp.className || '').trim().split(' ')[0];
                    const selector = inpCls
                        ? {attr_json} + ' input.' + inpCls
                        : {attr_json} + ' input[role="searchbox"]';
                    const all = Array.from(document.querySelectorAll(selector));
                    const nth = all.indexOf(inp);
                    results.push({{ selector, nth: nth >= 0 ? nth : 0, container_text: container.textContent.trim().substring(0, 80) }});
                }}
                return results;
            }}""")

        # Label proximity walk using hidden <select> as anchor.
        #
        # Widget libraries (select2, slimselect) always wrap a hidden <select> element
        # and place the visible widget as the select's immediate next sibling.
        # Using the select's stable id to build the CSS selector avoids relying on
        # dynamic container IDs (data-select2-id changes every page load).
        #
        # Strategy:
        #   1. Find the element whose text exactly matches filter_label.
        #   2. Search the surrounding DOM area for <select> elements with an id.
        #   3. For each, check if a next sibling has a search input inside it.
        #   4. Return "#select-id ~ * input[role=searchbox]" — stable across reloads.
        return page.evaluate(f"""() => {{
            const label = "{js_label}".toLowerCase();

            // Find label element — direct text match only (avoid matching parent containers)
            let labelEl = null;
            for (const el of document.querySelectorAll('*')) {{
                if (el.children.length === 0 && el.textContent.trim().toLowerCase() === label) {{
                    labelEl = el; break;
                }}
            }}
            if (!labelEl) return [];

            // Collect candidate DOM roots to search within:
            // walk up to 4 levels, check current element and all its siblings
            const roots = new Set();
            let p = labelEl.parentElement;
            for (let depth = 0; depth < 4 && p; depth++) {{
                roots.add(p);
                // siblings of p — the label cell's peer cells in the same grid/row
                let sib = p.parentElement?.firstElementChild;
                while (sib) {{ roots.add(sib); sib = sib.nextElementSibling; }}
                p = p.parentElement;
            }}

            const results = [];
            for (const root of roots) {{
                // Find hidden <select> elements that have a widget sibling
                for (const sel of root.querySelectorAll('select[id]')) {{
                    // Skip if the select IS in the label element's own subtree
                    if (labelEl.contains(sel)) continue;

                    // Walk forward siblings to find one containing a search input
                    let sib = sel.nextElementSibling;
                    while (sib) {{
                        const inp = sib.querySelector('input[role="searchbox"], input[type="search"]');
                        if (inp) {{
                            // Build stable selector: #select-id ~ container input-class
                            const inpCls = (inp.className || '').trim().split(' ')[0];
                            const selector = inpCls
                                ? `#${{sel.id}} ~ * input.${{inpCls}}`
                                : `#${{sel.id}} ~ * input[role="searchbox"]`;
                            // Verify it resolves to exactly this input (dedup)
                            const matches = Array.from(document.querySelectorAll(selector));
                            const nth = matches.indexOf(inp);
                            if (nth >= 0 && !results.find(r => r.selector === selector && r.nth === nth)) {{
                                results.push({{ selector, nth, select_id: sel.id }});
                            }}
                            break;
                        }}
                        sib = sib.nextElementSibling;
                    }}
                }}
            }}
            return results;
        }}""")

    def _select_filter_type_and_click(self, loc, filter_label: str, option_value: str) -> str:
        """Type into an open filter searchbox and click the matching autocomplete item.

        Tries three strategies in order:
          1. Initial list (no typing) — item may already be visible
          2. Short prefix (first 10 chars) — broader AJAX match
          3. Full value — exact search
        """
        page = self._page

        # Initial check: if option is already in the open list, 1 poll is enough.
        # _click_autocomplete_item uses autocomplete_dropdown selectors in priority
        # order, stopping at the first container that has visible items — so an
        # always-visible saved-searches listbox is never reached when the real
        # widget results container is already populated.
        clicked = self._click_autocomplete_item(option_value, max_attempts=1)
        if not clicked:
            prefix = option_value[:10].strip()
            page.keyboard.press("Control+a")
            page.wait_for_timeout(50)
            loc.press_sequentially(prefix, delay=80)
            page.wait_for_timeout(500)
            # After typing prefix: allow up to 3 polls for AJAX to load results
            clicked = self._click_autocomplete_item(option_value, max_attempts=3)
        if not clicked:
            page.keyboard.press("Control+a")
            page.wait_for_timeout(50)
            loc.press_sequentially(option_value, delay=80)
            # After full value: up to 3 polls; early-exit if list stays stable
            clicked = self._click_autocomplete_item(option_value, max_attempts=3)

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass

        self._last_selectors = self._extract_selectors(loc)
        if clicked:
            return f"selected '{clicked}' in filter '{filter_label}'"
        return f"typed '{option_value}' in filter '{filter_label}' — item may not have appeared in dropdown"

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
                log.debug("[fill ivalua-autocomplete] found input", extra={"event": "fill_field_strategy", "id": inp_id, "inp_name": inp_name})
                loc = (
                    page.locator(f'xpath=//*[@id="{inp_id}"]').first
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
                log.debug("[fill ivalua-autocomplete] typed but dropdown did not appear — XHR may not have fired", extra={"event": "fill_field_strategy", "value": value})
                return f"filled '{label}' with '{value}' (ivalua-autocomplete)"
            else:
                log.debug("[fill ivalua-autocomplete] not found", extra={"event": "fill_field_strategy", "result": result})
        except Exception as e:
            log.debug("[fill ivalua-autocomplete] failed", extra={"event": "fill_field_strategy", "error": str(e)})
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

            log.debug("[fill combobox-aria] found combobox", extra={"event": "fill_field_strategy", "label": label})

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
                log.debug("[fill combobox-aria] static dropdown — skipping type", extra={"event": "fill_field_strategy", "items_visible": items_visible})
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
            log.debug("[fill combobox-aria] failed", extra={"event": "fill_field_strategy", "error": str(e)})
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
                    page.locator(f'xpath=//*[@id="{inp_id}"]').first
                    if inp_id
                    else page.locator(f'input[name="{inp_name}"]').first
                )
                log.debug("[fill date-end] found input", extra={"event": "fill_field_strategy", "id": inp_id})
                loc.fill(value, timeout=2000)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                self._last_selectors = self._extract_selectors(loc)
                return f"filled 'to' date with '{value}' (ivalua-date-end)"
            else:
                log.debug("[fill date-end] not found", extra={"event": "fill_field_strategy", "result": result})
        except Exception as e:
            log.debug("[fill date-end] failed", extra={"event": "fill_field_strategy", "error": str(e)})
        return None


# Populate PLATFORM_GUIDANCE at import time so run.py can access it as a class attribute.
IvaluaBrowserSession._init_guidance()
