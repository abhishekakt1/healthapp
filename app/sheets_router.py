"""
sheets_router.py  —  FastAPI APIRouter for Google Sheets sync & import
======================================================================
Add ONE line to the bottom of main.py to activate (no other changes):

    from sheets_router import sheets_router
    app.include_router(sheets_router)

Endpoints exposed:
  GET  /api/sheets/config                — check configuration status
  POST /api/sheets/config                — save Sheet ID + upload creds JSON
  POST /api/sheets/sync                  — sync ALL users → Google Sheets
  POST /api/sheets/sync/{user_id}        — sync ONE user  → Google Sheets
  POST /api/sheets/import/excel          — parse uploaded .xlsx, return preview
  POST /api/sheets/import/gsheet         — fetch public Google Sheet, return preview
  POST /api/sheets/import/save           — write previewed rows into the DB
"""

from __future__ import annotations
import json, os, sqlite3, traceback
from datetime import date as _date_cls

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException

sheets_router = APIRouter(prefix="/api/sheets", tags=["sheets"])

# ── constants must mirror main.py ────────────────────────────────────────────
DB_PATH     = "/data/health_records.db"
CONFIG_PATH = "/data/gsheet_config.json"
CREDS_PATH  = "/data/gsheet_creds.json"


# ── auth: re-use main.py helpers at call time (avoids circular import) ────────

def _require_admin(request: Request) -> dict:
    from main import require_admin       # imported lazily — app is fully loaded by then
    return require_admin(request)


def _canon(name: str) -> str:
    try:
        from main import canonical_test_name
        return canonical_test_name(name)
    except Exception:
        return name.strip()


def _panel(name: str, cat: str = "") -> str:
    try:
        from main import infer_panel
        return infer_panel(name, cat)
    except Exception:
        return cat or "General"


# ── /config ──────────────────────────────────────────────────────────────────

@sheets_router.get("/config")
async def sheets_config_get(request: Request):
    _require_admin(request)
    cfg_ok   = os.path.exists(CONFIG_PATH)
    creds_ok = os.path.exists(CREDS_PATH)
    sheet_id, svc_email = "", ""
    if cfg_ok:
        try:
            sheet_id = json.load(open(CONFIG_PATH)).get("sheet_id", "")
        except Exception:
            pass
    if creds_ok:
        try:
            svc_email = json.load(open(CREDS_PATH)).get("client_email", "")
        except Exception:
            pass
    return {
        "configured":    cfg_ok and creds_ok and bool(sheet_id),
        "config_exists": cfg_ok,
        "creds_exists":  creds_ok,
        "sheet_id":      sheet_id,
        "service_email": svc_email,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else "",
    }


@sheets_router.post("/config")
async def sheets_config_save(
    request:    Request,
    sheet_id:   str        = Form(...),
    creds_json: UploadFile = File(None),
):
    _require_admin(request)
    sid = sheet_id.strip()
    if not sid:
        raise HTTPException(400, "Sheet ID cannot be empty")

    # Save / update config JSON
    existing = {}
    if os.path.exists(CONFIG_PATH):
        try: existing = json.load(open(CONFIG_PATH))
        except Exception: pass
    existing.update({"sheet_id": sid, "creds_path": CREDS_PATH})
    with open(CONFIG_PATH, "w") as fh:
        json.dump(existing, fh, indent=2)

    svc_email = ""
    if creds_json and creds_json.filename:
        raw = await creds_json.read()
        try:
            parsed = json.loads(raw)
            if "client_email" not in parsed or "private_key" not in parsed:
                raise ValueError("File does not look like a service-account key (missing client_email / private_key)")
            svc_email = parsed["client_email"]
        except Exception as e:
            raise HTTPException(400, f"Invalid credentials file: {e}")
        with open(CREDS_PATH, "wb") as fh:
            fh.write(raw)
        return {"ok": True,
                "message": f"Config saved. Share your sheet with: {svc_email}",
                "service_email": svc_email}

    return {"ok": True, "message": "Sheet ID updated (existing credentials kept)"}


# ── /sync ────────────────────────────────────────────────────────────────────

@sheets_router.post("/sync")
async def sync_all(request: Request):
    _require_admin(request)
    try:
        from sheets_sync import sync_to_sheets
        return sync_to_sheets()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Sync error: {e}")


@sheets_router.post("/sync/{user_id}")
async def sync_one(user_id: int, request: Request):
    _require_admin(request)
    try:
        from sheets_sync import sync_to_sheets
        return sync_to_sheets(user_id=user_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Sync error: {e}")


# ── /import/excel ────────────────────────────────────────────────────────────

@sheets_router.post("/import/excel")
async def import_excel(request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Please upload an .xlsx file")
    raw = await file.read()
    try:
        from sheets_sync import import_from_excel
        return import_from_excel(raw)
    except ImportError:
        raise HTTPException(500, "openpyxl not installed — add it to requirements.txt")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


# ── /import/gsheet ───────────────────────────────────────────────────────────

@sheets_router.post("/import/gsheet")
async def import_gsheet(
    request:   Request,
    sheet_url: str = Form(...),
    gid:       str = Form("0"),
):
    _require_admin(request)
    try:
        from sheets_sync import import_from_gsheet_url
        return import_from_gsheet_url(sheet_url.strip(), gid.strip() or "0")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


# ── /import/save ─────────────────────────────────────────────────────────────

@sheets_router.post("/import/save")
async def import_save(request: Request):
    """
    Body: { "user_id": int, "rows": [{date,lab_name,test_name,value,ref_range,panel},...] }
    Inserts rows as a new synthetic document + lab_reports records.
    """
    _require_admin(request)
    body    = await request.json()
    user_id = body.get("user_id")
    rows    = body.get("rows", [])

    if not user_id:
        raise HTTPException(400, "user_id is required")
    if not rows:
        raise HTTPException(400, "No rows provided")

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT full_name, email FROM users WHERE id=?", (user_id,))
    user_row = c.fetchone()
    if not user_row:
        conn.close()
        raise HTTPException(404, f"User {user_id} not found")

    today = _date_cls.today().isoformat()
    # Synthetic document record so it appears in the document list
    c.execute(
        "INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, "", today, f"import/sheets/{today}", "application/vnd.import", "lab")
    )
    doc_id = c.lastrowid

    saved, skipped = 0, 0
    for row in rows:
        raw_test = str(row.get("test_name", "")).strip()
        if not raw_test:
            skipped += 1
            continue
        test  = _canon(raw_test)
        val   = str(row.get("value",    "")).strip()
        ref   = str(row.get("ref_range","")).strip()
        lab   = str(row.get("lab_name", "")).strip()
        panel = _panel(test, str(row.get("panel","")).strip())
        date  = str(row.get("date",     "")).strip() or today

        c.execute(
            """INSERT INTO lab_reports
               (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user_id, doc_id, date, lab, test, val, ref, panel)
        )
        saved += 1

    conn.commit()
    conn.close()
    name = user_row[0] or user_row[1]
    print(f"[sheets_import] saved {saved} rows for user '{name}' (doc_id={doc_id})")
    return {"ok": True, "saved": saved, "skipped": skipped, "doc_id": doc_id,
            "message": f"Imported {saved} lab results for {name}"}