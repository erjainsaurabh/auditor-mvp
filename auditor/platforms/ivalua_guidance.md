
IVALUA BROWSE LIST PAGE RULES:
- Browse list pages (Browse Contract Budgets, Browse Requisitions, etc.) have TWO Search buttons:
  1. LEFT FILTER PANEL Search — submits the filter panel (Status, Agency, Doc ID, etc.).
     To trigger this, call: click('filter panel search') or click('Search')
     This routes directly to the panel's Search button (name=body:x:prxAdvFilterBar:x:cmdSearchBtn).
  2. MAIN PANE Search — keyword/text search in the main content header area.
     To trigger this, call: click('main search') or click('keyword search')
     This uses normal text matching against the header Search button.
  IMPORTANT: always use the exact descriptions above — never call click('Search') alone,
  as it is ambiguous when both buttons are visible.
- To open a specific record from results, click its ID link (e.g. "PO074788") — blue hyperlinks
  in the first column of the results table.
- Filter chips appear above the results table showing active filters (e.g. Status: Active ×,
  PO Type: CT1 × POCR ×). These persist across searches — check the chips row before setting
  filters to avoid duplicating selections already active.
- Multi-select chip fields (e.g. PO Type, Status): to ADD a value use fill_field with the field
  label and the value; to REMOVE a specific chip click the × button next to that chip's text.
