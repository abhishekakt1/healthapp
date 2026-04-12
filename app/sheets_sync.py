"""
sheets_sync.py  —  One-way sync: Health App DB  →  Google Sheets
                   Import:        Excel / Google Sheet  →  DB

One sheet, one tab per family member.
Tab layout: Date | Lab Name | Test Name | Value | Ref Range | Panel | Status | Synced At

── One-time setup ──────────────────────────────────────────────────────────
1. Google Cloud Console → enable Sheets API + Drive API
2. IAM → Service Accounts → create one → download JSON key
   → save the file to  /data/gsheet_creds.json
3. Open (or create) a Google Spreadsheet.
   Share it with the service-account email  (Editor role).
   Copy the Sheet ID from the URL:
     https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
4. In the app admin panel, paste the Sheet ID and upload the JSON key.
   The app writes  /data/gsheet_config.json  for you.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
import io, json, os, sqlite3, csv
from datetime import datetime
from typing import Optional

# ── must match main.py ───────────────────────────────────────────────────────
DB_PATH     = "/data/health_records.db"
CONFIG_PATH = "/data/gsheet_config.json"
CREDS_PATH  = "/data/gsheet_creds.json"

HEADERS = ["Date", "Lab Name", "Test Name", "Value",
           "Reference Range", "Panel / Category", "Status", "Synced At"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── internal helpers ─────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"Sheets not configured yet. "
            f"Save a Sheet ID and credentials via the admin panel first."
        )
    return json.load(open(CONFIG_PATH))


def _build_service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Google API libraries missing. "
            "Add  google-auth  google-api-python-client  to requirements.txt "
            "and rebuild the container."
        )
    cfg        = _load_config()
    creds_path = cfg.get("creds_path", CREDS_PATH)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Service-account key not found at {creds_path}. "
            "Re-upload the JSON key in the admin panel."
        )
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    svc   = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return svc, cfg["sheet_id"]


def _parse_date(s: str):
    from datetime import datetime as _dt
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y"):
        try: return _dt.strptime(str(s).strip(), fmt)
        except: pass
    from datetime import datetime as _dt
    return _dt.min


def _flag(val_str: str, ref_str: str) -> str:
    try:
        v     = float(str(val_str).split()[0].replace(",", ""))
        parts = str(ref_str).replace(" ", "").split("-")
        if len(parts) == 2:
            lo, hi = float(parts[0]), float(parts[1])
            if v > hi: return "HIGH ⚠"
            if v < lo: return "LOW ⚠"
            return "Normal"
    except Exception:
        pass
    return ""


def _existing_tabs(svc, sheet_id: str) -> dict[str, int]:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


def _ensure_tab(svc, sheet_id: str, title: str, cache: dict[str, int]) -> int:
    if title in cache:
        return cache[title]
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    ).execute()
    new_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    cache[title] = new_id
    return new_id


def _write_tab(svc, sheet_id: str, title: str, rows: list[list]):
    # Clear generously — use R1C1 range to cover any size
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"'{title}'"
    ).execute()
    if not rows:
        return
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{title}'!A1",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()


def _style_matrix_tab(svc, sheet_id: str, tab_id: int, n_data_cols: int,
                       panel_row_indices: list[int], n_total_rows: int):
    """
    Styling to match the dashboard matrix:
    - Row 0 (header): dark blue bg, white bold text, frozen
    - Panel header rows: light blue-grey bg, bold text
    - Col 0 (Test Name): frozen, slightly wider
    - Data columns: auto-resize
    - Alternate data row shading
    """
    total_cols = 1 + n_data_cols  # col 0 = test name, rest = date columns

    requests = [
        # ── Header row: dark blue ──
        {"repeatCell": {
            "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True,
                               "foregroundColor": {"red":1,"green":1,"blue":1},
                               "fontSize": 10},
                "backgroundColor": {"red":0.13,"green":0.30,"blue":0.53},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat",
        }},
        # ── Freeze row 1 + col 1 ──
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_id,
                           "gridProperties": {"frozenRowCount": 1,
                                              "frozenColumnCount": 1}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }},
        # ── Auto-resize all columns ──
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": tab_id, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": total_cols},
        }},
        # ── Col 0 (Test Name): left-align ──
        {"repeatCell": {
            "range": {"sheetId": tab_id,
                      "startRowIndex": 1, "endRowIndex": n_total_rows,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "LEFT",
                "textFormat": {"fontSize": 9},
            }},
            "fields": "userEnteredFormat",
        }},
        # ── Data cells: center-align ──
        {"repeatCell": {
            "range": {"sheetId": tab_id,
                      "startRowIndex": 1, "endRowIndex": n_total_rows,
                      "startColumnIndex": 1, "endColumnIndex": total_cols},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "textFormat": {"fontSize": 9},
            }},
            "fields": "userEnteredFormat",
        }},
    ]

    # ── Panel header rows: steel-blue background, bold ──
    for row_idx in panel_row_indices:
        requests.append({"repeatCell": {
            "range": {"sheetId": tab_id,
                      "startRowIndex": row_idx, "endRowIndex": row_idx + 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "fontSize": 9,
                               "foregroundColor": {"red":1,"green":1,"blue":1}},
                "backgroundColor": {"red":0.25,"green":0.45,"blue":0.68},
            }},
            "fields": "userEnteredFormat",
        }})

    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()


def _build_matrix_for_user(records: list) -> tuple[list[str], list[list], list[int]]:
    """
    Build the matrix exactly like the dashboard:
      Col 0       = "Test Name (Ref: X–Y)"
      Col 1..N    = "DD-Mon-YYYY (Lab Name)"  newest → oldest left → right
      Rows grouped by panel with a panel-header row between groups.

    Returns (col_headers, all_rows, panel_row_indices)
      col_headers      — the header row values (col0 label + date labels)
      all_rows         — list of rows including panel headers and data rows
      panel_row_indices — 0-based indices of panel-header rows (for styling)
    """
    try:
        from main import canonical_test_name, infer_panel
    except ImportError:
        def canonical_test_name(x): return x.strip()
        def infer_panel(x, y=""): return y or "General"

    # ── Step 1: build ordered unique columns (newest → oldest), one per doc_id ──
    records_sorted = sorted(records, key=lambda r: _parse_date(r[1]), reverse=True)

    col_labels   = []   # display labels in order
    seen_doc_ids = set()
    doc_id_to_lbl = {}

    for doc_id, date, lab, test, val, ref, cat in records_sorted:
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        lbl = f"{date} ({lab})" if lab else date
        base_lbl = lbl
        suffix = 2
        while lbl in col_labels:
            lbl = f"{base_lbl} #{suffix}"
            suffix += 1
        col_labels.append(lbl)
        doc_id_to_lbl[doc_id] = lbl

    # ── Step 2: group tests by panel, collect values per column ──
    # categories[panel][test_key][col_label] = value
    categories   = {}
    test_refs    = {}

    for doc_id, date, lab, test, val, ref, cat in records_sorted:
        lbl = doc_id_to_lbl.get(doc_id)
        if not lbl:
            continue
        bname = canonical_test_name(test)
        if bname not in test_refs:
            test_refs[bname] = ref or ""
        tkey = f"{bname}  (Ref: {test_refs[bname]})" if test_refs[bname] else bname
        panel = infer_panel(test, cat or "")
        if panel not in categories:
            categories[panel] = {}
        if tkey not in categories[panel]:
            categories[panel][tkey] = {c: "-" for c in col_labels}
        categories[panel][tkey][lbl] = val or "-"

    # ── Step 3: flatten into rows ──
    header_row = ["Test Name"] + col_labels
    all_rows   = [header_row]
    panel_row_indices = []   # track which rows are panel headers (1-based → 0-based in sheet)

    for panel in sorted(categories.keys()):
        # Panel header row
        panel_row_indices.append(len(all_rows))   # 0-based index in all_rows
        all_rows.append([f"▸  {panel}"] + [""] * len(col_labels))
        # Test rows
        for tkey in sorted(categories[panel].keys()):
            row = [tkey] + [categories[panel][tkey].get(c, "-") for c in col_labels]
            all_rows.append(row)

    return col_labels, all_rows, panel_row_indices


# ── public API ───────────────────────────────────────────────────────────────

def sync_to_sheets(user_id: Optional[int] = None) -> dict:
    """
    Push lab_reports → Google Sheets in matrix format matching the dashboard.
    user_id=None → all users.
    Returns {"synced_users": [...], "total_rows": int, "sheet_url": str}
    """
    svc, sheet_id = _build_service()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    if user_id:
        c.execute("SELECT id, full_name, email FROM users WHERE id=?", (user_id,))
    else:
        c.execute("SELECT id, full_name, email FROM users ORDER BY full_name")
    users = c.fetchall()

    tabs   = _existing_tabs(svc, sheet_id)
    synced = []
    total  = 0

    for uid, full_name, email in users:
        name      = (full_name or email.split("@")[0]).strip()
        tab_title = name[:90]

        # Fetch same columns the matrix API uses
        c.execute("""
            SELECT lr.doc_id, lr.date, lr.lab_name, lr.test_name,
                   lr.measured_value, lr.reference_range, lr.category
            FROM   lab_reports lr
            WHERE  lr.user_id = ?
            ORDER  BY lr.id DESC
        """, (uid,))
        records = c.fetchall()

        tab_id = _ensure_tab(svc, sheet_id, tab_title, tabs)

        if not records:
            _write_tab(svc, sheet_id, tab_title, [["No lab data yet for " + name]])
            synced.append({"user": name, "rows": 0})
            continue

        col_labels, all_rows, panel_row_indices = _build_matrix_for_user(records)

        _write_tab(svc, sheet_id, tab_title, all_rows)
        _style_matrix_tab(svc, sheet_id, tab_id,
                          n_data_cols=len(col_labels),
                          panel_row_indices=panel_row_indices,
                          n_total_rows=len(all_rows))

        data_rows = len(all_rows) - 1 - len(panel_row_indices)  # exclude header + panel rows
        total += data_rows
        synced.append({"user": name, "rows": data_rows})
        print(f"[sheets_sync] ✓  {name}: {data_rows} tests across {len(col_labels)} uploads")

    conn.close()
    return {
        "synced_users": synced,
        "total_rows":   total,
        "sheet_url":    f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    }


def sync_prescriptions_to_sheets(user_id=None) -> dict:
    """
    Push prescriptions → Google Sheets.
    Each user gets a tab named "NAME - Prescriptions".
    Each prescription = one row: Date | Doctor | Patient | Diagnosis |
    Chief Complaint | Medicines | Investigations | Advice | Follow-up | Notes
    """
    svc, sheet_id = _build_service()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    if user_id:
        c.execute("SELECT id, full_name, email FROM users WHERE id=?", (user_id,))
    else:
        c.execute("SELECT id, full_name, email FROM users ORDER BY full_name")
    users = c.fetchall()

    tabs   = _existing_tabs(svc, sheet_id)
    synced = []
    total  = 0

    HEADER = ["Date", "Doctor", "Patient", "Diagnosis", "Chief Complaint",
              "Medicines", "Investigations Advised", "Advice", "Follow-Up", "Notes"]

    for uid, full_name, email in users:
        name      = (full_name or email.split("@")[0]).strip()
        tab_title = f"{name[:70]} - Prescriptions"

        c.execute("""SELECT p.date, p.doctor, p.data
                     FROM prescriptions p
                     WHERE p.user_id=?
                     ORDER BY p.date DESC""", (uid,))
        rows_db = c.fetchall()

        if not rows_db:
            _ensure_tab(svc, sheet_id, tab_title, tabs)
            _write_tab(svc, sheet_id, tab_title,
                       [HEADER, [f"No prescriptions yet for {name}"]])
            synced.append({"user": name, "rows": 0})
            continue

        sheet_rows = [HEADER]
        for date, doctor, data_json in rows_db:
            try:
                d = json.loads(data_json or "{}")
            except Exception:
                d = {}
            # Medicines: "DrugName Dose Freq Duration | DrugName ..."
            meds = d.get("Medicines") or []
            meds_str = " | ".join(
                " ".join(filter(None, [
                    m.get("Name",""), m.get("Dosage",""),
                    m.get("Frequency",""), m.get("Duration","")
                ])) for m in meds if m.get("Name")
            )
            invs_str  = ", ".join(d.get("Investigations") or [])
            advice_str = " | ".join(d.get("Advice") or [])
            sheet_rows.append([
                date or "",
                doctor or d.get("Doctor_Name","") or "",
                d.get("Patient_Name","") or name,
                d.get("Diagnosis","") or "",
                d.get("Chief_Complaint","") or "",
                meds_str,
                invs_str,
                advice_str,
                d.get("Follow_Up","") or "",
                d.get("Notes","") or "",
            ])

        _ensure_tab(svc, sheet_id, tab_title, tabs)
        _write_tab(svc, sheet_id, tab_title, sheet_rows)
        rx_count = len(sheet_rows) - 1
        total += rx_count
        synced.append({"user": name, "rows": rx_count})
        print(f"[sheets_sync] ✓  {name}: {rx_count} prescriptions")

    conn.close()
    return {
        "synced_users": synced,
        "total_rows":   total,
        "sheet_url":    f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    }


# ── import helpers ────────────────────────────────────────────────────────────

def _map_columns(header_cells: list[str]) -> dict[str, int]:
    """Return {logical_name: col_index} from a header row."""
    m = {}
    for i, raw in enumerate(header_cells):
        h = raw.lower().strip()
        if   "test" in h:                         m.setdefault("test", i)
        elif any(k in h for k in ("value","result","measured")): m.setdefault("value", i)
        elif any(k in h for k in ("ref","range","normal")):      m.setdefault("ref", i)
        elif "date" in h:                         m.setdefault("date", i)
        elif any(k in h for k in ("lab","laboratory")): m.setdefault("lab", i)
        elif any(k in h for k in ("panel","category","section")): m.setdefault("panel", i)
    return m


def _row_to_dict(row: list, col_map: dict[str, int]) -> dict | None:
    def g(key):
        idx = col_map.get(key)
        if idx is None or idx >= len(row): return ""
        v = row[idx]
        return str(v).strip() if v is not None else ""
    test = g("test")
    if not test or test.lower() in ("none", "test name", "test"):
        return None
    return {"date": g("date"), "lab_name": g("lab"), "test_name": test,
            "value": g("value"), "ref_range": g("ref"), "panel": g("panel")}


def import_from_excel(file_bytes: bytes) -> dict:
    """
    Parse .xlsx → list of importable row dicts.
    Handles multiple sheets; auto-detects header row.
    Returns {"rows": [...], "errors": [...], "total": int}
    """
    import openpyxl
    wb   = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    rows, errors = [], []

    for sheet in wb.worksheets:
        raw_rows = list(sheet.iter_rows(values_only=True))
        if not raw_rows:
            continue
        # Find first row with ≥2 recognisable columns
        col_map, header_idx = {}, None
        for i, row in enumerate(raw_rows[:8]):
            cm = _map_columns([str(c) if c else "" for c in row])
            if "test" in cm and "value" in cm:
                col_map, header_idx = cm, i
                break
        if header_idx is None:
            errors.append(f"Sheet '{sheet.title}': no recognisable header (need Test + Value columns)")
            continue
        for row in raw_rows[header_idx + 1:]:
            if not any(row): continue
            d = _row_to_dict(list(row), col_map)
            if d:
                d["source_tab"] = sheet.title
                rows.append(d)

    return {"rows": rows, "errors": errors, "total": len(rows)}


def import_from_gsheet_url(url: str, gid: str = "0") -> dict:
    """
    Fetch a publicly-shared Google Sheet tab as CSV and parse it.
    Returns {"rows": [...], "errors": [...], "total": int}
    """
    import re, urllib.request
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError("Cannot extract Sheet ID from URL — paste the full Google Sheets URL")
    sid     = m.group(1)
    csv_url = (f"https://docs.google.com/spreadsheets/d/{sid}"
               f"/export?format=csv&gid={gid}")
    try:
        with urllib.request.urlopen(csv_url, timeout=20) as r:
            raw = r.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Could not fetch sheet: {e}. "
            "Make sure the sheet is shared → Anyone with the link → Viewer."
        )

    all_rows = list(csv.reader(raw.splitlines()))
    if not all_rows:
        return {"rows": [], "errors": ["Empty sheet"], "total": 0}

    col_map = _map_columns(all_rows[0])
    if "test" not in col_map or "value" not in col_map:
        return {"rows": [], "errors": ["No Test + Value columns found"], "total": 0}

    rows = []
    for row in all_rows[1:]:
        if not any(row): continue
        d = _row_to_dict(row, col_map)
        if d:
            d["source_tab"] = "google_sheet"
            rows.append(d)
    return {"rows": rows, "errors": [], "total": len(rows)}