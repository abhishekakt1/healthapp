import os
import re
import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, status, Form, UploadFile, File, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
import bcrypt as _bcrypt
from jose import JWTError, jwt

from gemini_utils import extract_prescription_ai, extract_prescription_smart, process_health_query

app = FastAPI(title="Family Health Record System")
templates = Jinja2Templates(directory="templates")

# ── Bulk-upload job store ──
import threading as _threading
_bulk_jobs: dict = {}          # job_id -> {status, total, done, results, error}
_bulk_jobs_lock = _threading.Lock()

SECRET_KEY = os.getenv("JWT_SECRET", "dev_secret")
ALGORITHM = "HS256"
DB_PATH = "/data/health_records.db"
UPLOAD_BASE = "/data/uploads"
# bcrypt used directly — no passlib wrapper needed


# ── DATE UTILS ──────────────────────────────────────────────────────────────

def normalize_date_str(raw_date: str) -> str:
    if not raw_date or raw_date in ("Not Found", "-", ""):
        return raw_date
    s = raw_date.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                "%d %B %Y", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    try:
        parts = re.split(r"[\s\-\/\,]+", s)
        if len(parts) >= 3:
            if len(parts[0]) == 4:
                year, month, day = parts[0], parts[1], parts[2]
            else:
                day, month, year = parts[0], parts[1], parts[2]
            if len(year) == 2:
                year = "20" + year
            day = day.zfill(2)
            if month.isalpha():
                return datetime.strptime(f"{day} {month[:3].capitalize()} {year}", "%d %b %Y").strftime("%d-%b-%Y")
            else:
                return datetime.strptime(f"{day} {month.zfill(2)} {year}", "%d %m %Y").strftime("%d-%b-%Y")
    except Exception:
        pass
    return raw_date


def date_for_filename(date_str: str) -> str:
    """Convert any date to dd-mm-yyyy for filenames."""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%d-%m-%Y")
        except Exception:
            continue
    return datetime.now().strftime("%d-%m-%Y")


# ── TEST NAME CANONICALIZER ──────────────────────────────────────────────────
# Maps known aliases → single canonical display name so the same test from
# different labs/uploads doesn't appear as multiple duplicate rows in the matrix.

_CANONICAL_TESTS = [
    # Urine-specific tests — MUST come before bare Glucose/Protein/Bilirubin
    # so "URINARY GLUCOSE" doesn't collapse to same row as "Glucose (Fasting)"
    (["urinary glucose", "urine glucose", "glucose urine",
      "glucose - urine", "urine glucose negative"],             "Glucose (Urine)"),
    (["urinary protein", "urine protein", "protein urine",
      "urinary protein pei"],                                  "Protein (Urine)"),
    (["urinary bilirubin", "urine bilirubin", "bilirubin urine"], "Bilirubin (Urine)"),
    (["urinary ketone", "urine ketone", "ketone urine"],        "Ketone (Urine)"),
    (["urine blood", "urinary blood", "blood urine",
      "occult blood urine"],                                   "Blood (Urine)"),
    # Diabetes — MUST be before CBC so HbA1c doesn't match "hb" alias for haemoglobin
    (["hba1c", "glycated haemoglobin", "glycated hemoglobin",
      "glycosylated haemoglobin", "a1c", "hb a1c"],        "HbA1c"),
    (["fasting glucose", "fasting blood glucose", "fasting blood sugar",
      "fbs", "glucose fasting", "blood glucose fasting",
      "glucose (fasting)", "blood glucose - fasting",
      "serum glucose fasting"],                             "Glucose (Fasting)"),
    (["postprandial glucose", "pp glucose", "post prandial blood sugar",
      "ppbs", "glucose pp", "2hr postprandial"],           "Glucose (Post-Prandial)"),
    (["random glucose", "random blood sugar", "rbs", "glucose random",
      "blood glucose random"],                             "Glucose (Random)"),
    # eAG — must come BEFORE any bare "glucose" alias so eAG is caught first
    (["estimated average glucose", "eag", "e-ag",
      "average blood glucose", "average glucose",
      "mean blood glucose"],                               "Average Blood Glucose (eAG)"),
    # Thyroid
    (["tsh", "thyroid stimulating hormone", "thyrotropin", "tsh ultrasensitive",
      "tsh ultra sensitive", "tsh - ultrasensitive", "thyroid stimulating hormone ultra sensitive"],
     "TSH (Thyroid Stimulating Hormone)"),
    (["t4 total", "total thyroxine", "thyroxine total", "total t4", "t4,total",
      "t4 , total", "total t4 thyroxine"],
     "T4 Total (Thyroxine)"),
    (["t3 total", "total triiodothyronine", "triiodothyronine total", "total t3",
      "t3,total", "t3 , total"],
     "T3 Total (Triiodothyronine)"),
    (["free t4", "ft4", "free thyroxine", "thyroxine free", "t4 free"],
     "T4 Free (FT4)"),
    (["free t3", "ft3", "free triiodothyronine", "triiodothyronine free", "t3 free"],
     "T3 Free (FT3)"),
    # ── Urine Routine — specific names BEFORE bare glucose/protein/bilirubin ──
    # Physical
    (["urine colour", "colour", "color", "urine color"],          "Urine Colour"),
    (["urine appearance", "appearance"],                           "Urine Appearance"),
    (["urine volume", "volume"],                                   "Urine Volume"),
    (["urine specific gravity", "specific gravity"],               "Urine Specific Gravity"),
    (["urine ph", "ph"],                                           "Urine pH"),
    # Chemical
    (["urinary protein", "urine protein"],                         "Urine Protein"),
    (["urinary glucose", "urine glucose", "glucose urine"],        "Urine Glucose"),
    (["urine ketone", "urinary ketone", "ketone"],                 "Urine Ketone"),
    (["urinary bilirubin", "urine bilirubin"],                     "Urine Bilirubin"),
    (["urobilinogen"],                                             "Urobilinogen"),
    (["bile salt"],                                                "Bile Salt"),
    (["bile pigment"],                                             "Bile Pigment"),
    (["urine blood"],                                              "Urine Blood (Occult)"),
    (["nitrite"],                                                  "Urine Nitrite"),
    (["leucocyte esterase"],                                       "Leucocyte Esterase"),
    # Microscopy
    (["urinary leucocytes", "urine wbc", "pus cell", "urine leucocyte"], "Urine WBC / Pus Cells"),
    (["urine rbc", "red blood cell"],                              "Urine RBC"),
    (["epithelial cell"],                                          "Epithelial Cells"),
    (["mucus"],                                                    "Mucus"),
    (["cast"],                                                     "Casts"),
    (["crystal"],                                                  "Crystals"),
    (["bacteria"],                                                 "Bacteria"),
    (["yeast"],                                                    "Yeast"),
    (["parasite"],                                                 "Parasite"),
    # ── CBC ──
    (["haemoglobin", "hemoglobin", "hgb", "haemoglobin hb"],
     "Haemoglobin (Hb)"),
    (["rbc count", "rbc", "red blood cell count", "red blood cells",
      "red blood corpuscles", "erythrocyte count"],
     "RBC Count"),
    (["wbc count", "wbc", "white blood cell count", "white blood cells",
      "leucocyte count", "leukocyte count", "total leucocyte count", "tlc"],
     "WBC Count"),
    (["platelet count", "plt", "platelet", "thrombocyte count", "platelets"],
     "Platelet Count"),
    (["haematocrit", "hematocrit", "hct", "pcv", "packed cell volume"],
     "HCT / PCV"),
    (["mcv"], "MCV"),
    (["mch"], "MCH"),
    (["mchc"], "MCHC"),
    (["rdw", "rdw-cv", "rdw cv", "red cell distribution width"],
     "RDW"),
    # Absolute counts BEFORE bare names (order matters: _clean_for_match strips suffix)
    (["neutrophils - absolute count", "neutrophils absolute", "neutrophil absolute count",
      "neutrophils-absolute"],                              "Neutrophils - Absolute Count"),
    (["lymphocytes - absolute count", "lymphocytes absolute", "lymphocyte absolute count",
      "lymphocytes-absolute"],                              "Lymphocytes - Absolute Count"),
    (["monocytes - absolute count", "monocytes absolute", "monocyte absolute count",
      "monocytes-absolute"],                                "Monocytes - Absolute Count"),
    (["eosinophils - absolute count", "eosinophils absolute", "eosinophil absolute count",
      "eosinophils-absolute"],                              "Eosinophils - Absolute Count"),
    (["basophils - absolute count", "basophils absolute", "basophil absolute count",
      "basophils-absolute"],                                "Basophils - Absolute Count"),
    # Bare differential (percentage) entries after absolute
    (["neutrophil", "neutrophils", "neutrophil count", "polymorphs",
      "neutrophils percentage", "neutrophil %"],            "Neutrophils"),
    (["lymphocyte", "lymphocytes", "lymphocyte count",
      "lymphocytes percentage", "lymphocyte %"],            "Lymphocytes"),
    (["monocyte", "monocytes", "monocytes percentage", "monocyte %"], "Monocytes"),
    (["eosinophil", "eosinophils", "eosinophils percentage", "eosinophil %"], "Eosinophils"),
    (["basophil", "basophils", "basophils percentage", "basophil %"], "Basophils"),
    # Lipid
    (["total cholesterol", "cholesterol total", "serum cholesterol",
      "cholesterol serum"],                                 "Total Cholesterol"),
    (["triglycerides", "triglyceride", "tg", "serum triglycerides"],
     "Triglycerides"),
    (["hdl cholesterol", "hdl", "hdl-c", "high density lipoprotein"],
     "HDL Cholesterol"),
    (["ldl cholesterol", "ldl", "ldl-c", "low density lipoprotein"],
     "LDL Cholesterol"),
    (["vldl cholesterol", "vldl", "vldl-c"],               "VLDL Cholesterol"),
    # Glucose / Diabetes — entries moved above CBC section
    # Kidney
    (["creatinine", "serum creatinine", "s.creatinine"],   "Creatinine"),
    (["urea", "blood urea", "bun", "blood urea nitrogen",
      "serum urea"],                                       "Blood Urea / BUN"),
    (["uric acid", "serum uric acid", "s.uric acid"],      "Uric Acid"),
    (["egfr", "gfr", "estimated gfr", "glomerular filtration rate",
      "estimated glomerular filtration"],                  "eGFR"),
    # Liver
    (["sgpt", "alt", "alanine aminotransferase", "alanine transaminase",
      "sgpt/alt"],                                         "SGPT / ALT"),
    (["sgot", "ast", "aspartate aminotransferase", "aspartate transaminase",
      "sgot/ast"],                                         "SGOT / AST"),
    (["alkaline phosphatase", "alp", "alk phos", "serum alkaline phosphatase"],
     "Alkaline Phosphatase"),
    (["total bilirubin", "bilirubin total", "bilirubin-total",
      "serum bilirubin", "bilirubin serum"],
     "Bilirubin Total"),
    (["direct bilirubin", "bilirubin direct", "bilirubin-direct"],
     "Bilirubin Direct"),
    (["indirect bilirubin", "bilirubin indirect", "bilirubin-indirect"],
     "Bilirubin Indirect"),
    (["albumin", "serum albumin"],                         "Albumin"),
    (["total protein", "protein total", "serum total protein",
      "protein - total", "proteins total"],
     "Total Protein"),
    (["globulin", "serum globulin"],                       "Globulin"),
    (["ggt", "gamma gt", "gamma glutamyl transferase", "gamma glutamyl transpeptidase"],
     "GGT"),
    # Electrolytes
    (["sodium", "na+", "serum sodium"],                    "Sodium (Na)"),
    (["potassium", "k+", "serum potassium"],               "Potassium (K)"),
    (["chloride", "cl-", "serum chloride"],                "Chloride (Cl)"),
    (["calcium", "serum calcium", "ca2+"],                 "Calcium (Ca)"),
    (["phosphorus", "phosphate", "serum phosphorus"],      "Phosphorus"),
    (["magnesium", "serum magnesium"],                     "Magnesium (Mg)"),
    # Vitamins / Iron
    (["vitamin d", "vit d", "25-oh vitamin d", "25 hydroxy vitamin d",
      "25(oh)d", "25-hydroxyvitamin d", "calcidiol"],      "Vitamin D (25-OH)"),
    (["vitamin b12", "vit b12", "cobalamin", "cyanocobalamin",
      "b12", "vitamin b-12", "vit b-12"],                   "Vitamin B12"),
    (["ferritin", "serum ferritin"],                       "Ferritin"),
    (["serum iron", "iron", "fe"],                         "Serum Iron"),
    (["tibc", "total iron binding capacity"],              "TIBC"),
    (["transferrin saturation", "% transferrin saturation"], "Transferrin Saturation"),
    (["folate", "folic acid", "serum folate"],             "Folate / Folic Acid"),
    # Inflammation / Cardiac
    (["crp", "c reactive protein", "c-reactive protein",
      "hs-crp", "high sensitivity crp"],                   "CRP"),
    (["esr", "erythrocyte sedimentation rate", "sed rate"], "ESR"),
    (["troponin i", "troponin-i", "cardiac troponin i"],   "Troponin I"),
    (["troponin t", "troponin-t", "cardiac troponin t"],   "Troponin T"),
    # Thyroid antibodies
    (["anti tpo", "anti-tpo", "tpo antibody", "thyroid peroxidase antibody",
      "anti thyroid peroxidase"],                          "Anti-TPO"),
    (["anti thyroglobulin", "anti-tg", "tg antibody",
      "thyroglobulin antibody"],                           "Anti-Thyroglobulin"),
    # Urine
    (["urine specific gravity", "specific gravity"],       "Urine Specific Gravity"),
    (["urine ph", "ph urine"],                             "Urine pH"),
    (["urine protein", "protein urine", "urine albumin"],  "Urine Protein"),
    (["urine glucose", "glucose urine"],                   "Urine Glucose"),
    (["urine rbc", "rbc urine", "red cells urine"],        "Urine RBC"),
    (["urine wbc", "wbc urine", "pus cells", "urine pus cells"],
     "Urine WBC / Pus Cells"),
]

def _clean_for_match(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

# ── PANEL INFERENCE ──────────────────────────────────────────────────────────
_PANEL_KEYWORDS = [
    # Urine FIRST — catches "urinary bilirubin", "urine blood", "urine glucose" etc.
    # before Liver/Diabetes/CBC panels grab bilirubin/glucose/wbc generically
    ("Urine Routine", [
        "urine", "urinary", "leucocyte esterase", "nitrite", "urobilinogen",
        "specific gravity", "urine specific gravity", "mucus", "cast",
        "epithelial cell", "pus cell", "wbc count", "rbc count",
        "bacteria", "yeast", "parasite", "crystal",
        "bile salt", "bile pigment", "urine blood", "urine ketone",
        "urine protein", "urine glucose", "urine bilirubin",
        "urinary protein", "urinary glucose", "urinary bilirubin",
        "urinary leucocyte", "red blood cell",
        "colour", "color", "appearance", "transparency", "turbidity", "clarity",
        "volume", "ph", "urine ph",
        "complete urine", "urinogram", "urinalysis", "microscopy",
        "complete urine analysis", "urine routine",
    ]),
    ("Diabetes", [
        "hba1c", "glycated haemoglobin", "glycated hemoglobin", "glycosylated",
        "a1c", "hb a1c",
        "postprandial", "ppbs", "fbs", "insulin",
        "c peptide", "average blood glucose", "estimated average glucose",
        "average glucose", "eag",
        "fasting blood sugar", "fasting glucose", "glucose fasting",
        "glucose (fasting)", "glucose pp", "glucose random",
    ]),
    ("Complete Blood Count", [
        "haemoglobin", "hemoglobin", "hematocrit", "haematocrit",
        "wbc", "platelet", "neutrophil", "lymphocyte", "monocyte",
        "eosinophil", "basophil", "mcv", "mch", "mchc", "rdw", "mpv", "hct",
        "tlc", "dlc", "packed cell", "white blood", "reticulocyte",
        "leucocyte count", "leukocyte count", "differential leucocyte",
        "immature granulocyte", "nucleated red", "mentzer", "rdwi", "pdw",
        "absolute count", "neutrophils - absolute", "lymphocytes - absolute",
        "monocytes - absolute", "basophils - absolute", "eosinophils - absolute",
        "total rbc", "total leucocyte", "total leukocyte",
    ]),
    ("Thyroid Profile", [
        "tsh", "t3", "t4", "ft3", "ft4", "free t3", "free t4",
        "thyroxine", "triiodothyronine", "thyroid", "ustsh",
    ]),
    ("Lipid Profile", [
        "cholesterol", "triglyceride", "hdl", "ldl", "vldl",
        "non-hdl", "lipoprotein", "lipid", "tc/ hdl", "trig / hdl",
        "ldl / hdl", "hdl / ldl", "tc/hdl", "trig/hdl",
    ]),
    ("Liver Function Test", [
        "bilirubin", "sgot", "sgpt", "ast", "alt",
        "alkaline phosphatase", "alp", "ggt", "ggtp", "albumin",
        "globulin", "total protein", "a/g ratio", "a/g", "liver",
        "serum albumin", "serum globulin", "protein - total",
    ]),
    ("Kidney Function Test", [
        "creatinine", "urea", "bun", "uric acid", "egfr", "cystatin",
        "blood urea", "glomerular filtration", "urea nitrogen",
        "sr.creatinine", "urea / sr", "bun / sr",
    ]),
    ("Vitamins", [
        "vitamin", "vit b", "vit d", "vit c", "folate", "folic acid",
        "b12", "d3", "d 25", "cobalamin",
    ]),
    ("Iron Studies", [
        "ferritin", "tibc", "transferrin", "serum iron",
        "iron", "iron binding", "transferrin saturation", "uibc",
        "unsat.iron", "iron deficiency",
    ]),
    ("Electrolytes", [
        "sodium", "potassium", "chloride", "bicarbonate",
        "phosphorus", "calcium", "magnesium", "electrolyte",
    ]),
    ("Hormones", [
        "testosterone", "estrogen", "oestrogen", "progesterone",
        "fsh", "lh", "prolactin", "cortisol", "dhea", "amh",
        "estradiol", "hormone",
    ]),
    ("Inflammatory Markers", [
        "crp", "c reactive", "esr", "erythrocyte sedimentation", "procalcitonin",
    ]),
    ("Cardiac Markers", [
        "troponin", "ck mb", "cpk", "bnp", "nt probnp", "d dimer", "cardiac",
    ]),
    ("Coagulation", [
        "inr", "aptt", "ptt", "fibrinogen", "prothrombin",
        "bleeding time", "clotting time",
    ]),
    ("Infection / Serology", [
        "widal", "typhoid", "malaria", "dengue", "hiv", "hepatitis",
        "hbsag", "anti hcv", "vdrl", "torch", "igm", "igg",
        "antibody", "antigen", "elisa",
    ]),
]

# Panel names that are "canonical" — already correct, just normalise case/plurals
_CANONICAL_PANEL_NAMES = {
    "complete blood count", "cbc", "hemogram", "haemogram",
    "thyroid profile", "thyroid function",
    "lipid profile", "lipid panel",
    "liver function test", "liver function tests", "lft",
    "kidney function test", "kidney function tests", "kft", "renal function",
    "urine routine", "urine routine & microscopy", "urine analysis",
    "complete urine analysis", "urinalysis",
    "diabetes", "blood sugar", "glycemic",
    "iron studies", "iron deficiency profile", "iron profile",
    "vitamins", "vitamin panel",
    "electrolytes", "electrolyte panel",
    "hormones",
    "inflammatory markers",
    "cardiac markers",
    "coagulation",
    "infection / serology",
}

# Map stored panel variants → canonical panel name
_PANEL_NORMALISE = {
    "complete hemogram": "Complete Blood Count",
    "hemogram": "Complete Blood Count",
    "haemogram": "Complete Blood Count",
    "cbc": "Complete Blood Count",
    "6 part diff": "Complete Blood Count",
    "thyroid function": "Thyroid Profile",
    "t3-t4-ustsh": "Thyroid Profile",
    "t3 t4 tsh": "Thyroid Profile",
    "lipid panel": "Lipid Profile",
    "liver function tests": "Liver Function Test",
    "lft": "Liver Function Test",
    "hepatic function": "Liver Function Test",
    "kidney function tests": "Kidney Function Test",
    "kft": "Kidney Function Test",
    "renal function": "Kidney Function Test",
    "kidpro": "Kidney Function Test",
    "renal profile": "Kidney Function Test",
    "urine routine & microscopy": "Urine Routine",
    "urine analysis": "Urine Routine",
    "complete urine analysis": "Urine Routine",
    "urinalysis": "Urine Routine",
    "blood sugar": "Diabetes",
    "glycemic": "Diabetes",
    "iron deficiency profile": "Iron Studies",
    "iron deficiency": "Iron Studies",
    "iron profile": "Iron Studies",
    "electrolyte panel": "Electrolytes",
}

def infer_panel(test_name: str, current_category: str = "") -> str:
    """Derive canonical panel name.

    Strategy:
    1. Always normalise the stored category (fixes THYROID PROFILE, Liver Function Tests etc.)
    2. Derive panel from test name keywords
    3. If keyword match contradicts a valid stored category (ambiguous test like "Bilirubin"),
       trust the stored category as a tiebreaker
    4. Fallback to General
    """
    import re as _re

    bad_cats = {"high", "low", "normal", "general", "", "absent", "present",
                "abnormal", "borderline", "elevated", "decreased",
                "high normal", "low normal"}

    # Step 1: Normalise stored category
    stored_norm = ""
    if current_category:
        cl = current_category.strip().lower()
        if cl not in bad_cats:
            stored_norm = _PANEL_NORMALISE.get(cl, current_category.strip().title())

    # Step 2: Derive from test name keywords
    low = test_name.lower().strip()
    keyword_panel = None
    for panel, keywords in _PANEL_KEYWORDS:
        for kw in keywords:
            kw = kw.strip()
            if len(kw) <= 3:
                if _re.search(r'\b' + _re.escape(kw) + r'\b', low):
                    keyword_panel = panel
                    break
            else:
                if kw in low:
                    keyword_panel = panel
                    break
        if keyword_panel:
            break

    # Step 3: Tiebreak ambiguous cases
    # If keyword_panel and stored_norm disagree AND stored_norm is a valid panel,
    # trust stored_norm (handles "Bilirubin" in Urine context vs LFT context)
    if keyword_panel and stored_norm and keyword_panel != stored_norm:
        # Only trust stored_norm if it's a known canonical panel name
        known_panels = {p for p, _ in _PANEL_KEYWORDS}
        if stored_norm in known_panels:
            return stored_norm

    if keyword_panel:
        return keyword_panel
    if stored_norm:
        return stored_norm
    return "General"

def canonical_test_name(raw: str) -> str:
    """Return canonical name if a known alias matches, else return raw unchanged.

    Matching rules (strict to prevent false aliases):
      1. Exact match after _clean_for_match normalisation.
      2. cleaned starts with alias + ' ' (alias is a prefix word boundary).
      3. alias appears as a whole-word sequence inside cleaned
         BUT only when the alias is >=4 chars — prevents short tokens
         like 'abg', 'ph', 'hb' from matching inside longer strings.
    """
    if not raw:
        return raw
    cleaned = _clean_for_match(raw)
    for aliases, canonical in _CANONICAL_TESTS:
        for alias in aliases:
            ac = _clean_for_match(alias)
            if not ac:
                continue
            if cleaned == ac:
                return canonical
            # Prefix match: "glucose fasting something" still maps correctly
            if cleaned.startswith(ac + ' '):
                return canonical
            # Whole-word substring — only for aliases >=4 chars to avoid
            # short tokens ('hb', 'fe', 'na') matching inside longer words
            if len(ac) >= 4 and (' ' + ac + ' ') in (' ' + cleaned + ' '):
                return canonical
    return raw


# ── FILE PATH BUILDER ────────────────────────────────────────────────────────
# Structure: /data/uploads/<username>/<lab|prescription>/<username>_<type>_<dd-mm-yyyy>.pdf

def build_upload_path(full_name: str, doc_type: str, date_str: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "_", (full_name or "unknown").strip().lower())
    type_dir = "lab" if doc_type == "lab_report" else "prescription"
    type_label = "lab" if doc_type == "lab_report" else "prescription"
    date_label = date_for_filename(date_str) if date_str and date_str != "Not Found" else datetime.now().strftime("%d-%m-%Y")

    dir_path = os.path.join(UPLOAD_BASE, safe, type_dir)
    os.makedirs(dir_path, exist_ok=True)

    base_name = f"{safe}_{type_label}_{date_label}"
    file_path = os.path.join(dir_path, base_name + ".pdf")
    counter = 1
    while os.path.exists(file_path):
        file_path = os.path.join(dir_path, f"{base_name}_{counter}.pdf")
        counter += 1
    return file_path


def make_scanned_pdf(file_bytes: bytes) -> bytes:
    """
    Convert a photo/image into a professional-looking scanned PDF.
    Pipeline: EXIF rotation → perspective deskew → adaptive threshold
              → sharpen → embed in A4 via ReportLab.
    Returns PDF bytes.
    """
    import io
    import numpy as np
    import cv2
    from PIL import Image, ImageFilter, ImageEnhance, ImageOps
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader

    # ── 1. Load + fix phone EXIF rotation ──
    pil_orig = Image.open(io.BytesIO(file_bytes))
    pil_orig = ImageOps.exif_transpose(pil_orig)
    if pil_orig.mode != "RGB":
        pil_orig = pil_orig.convert("RGB")
    img_cv = cv2.cvtColor(np.array(pil_orig), cv2.COLOR_RGB2BGR)
    h, w = img_cv.shape[:2]

    # ── 2. Detect document edges + perspective warp ──
    def find_doc_corners(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 75, 200)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:5]
        for c in cnts:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                return approx.reshape(4, 2)
        return None

    corners = find_doc_corners(img_cv)
    if corners is not None:
        pts = corners.astype("float32")
        s = pts.sum(axis=1); d = np.diff(pts, axis=1)
        rect = np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                         pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")
        tl, tr, br, bl = rect
        maxW = max(int(np.linalg.norm(br - bl)), int(np.linalg.norm(tr - tl)))
        maxH = max(int(np.linalg.norm(tr - br)), int(np.linalg.norm(tl - bl)))
        if maxW > 200 and maxH > 200:
            dst = np.array([[0,0],[maxW-1,0],[maxW-1,maxH-1],[0,maxH-1]], dtype="float32")
            M = cv2.getPerspectiveTransform(rect, dst)
            img_cv = cv2.warpPerspective(img_cv, M, (maxW, maxH))

    # ── 3. Adaptive threshold → clean scan look ──
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    scanned = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 10
    )

    # ── 4. Sharpen + contrast via Pillow ──
    pil = Image.fromarray(scanned)
    pil = pil.filter(ImageFilter.UnsharpMask(radius=1, percent=130, threshold=2))
    pil = ImageEnhance.Contrast(pil).enhance(1.2)

    # ── 5. Embed in A4 PDF via ReportLab ──
    PAGE_W, PAGE_H = A4
    MARGIN = 24
    avail_w, avail_h = PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN
    iw, ih = pil.size
    scale = min(avail_w / iw, avail_h / ih)
    dw, dh = iw * scale, ih * scale
    x = MARGIN + (avail_w - dw) / 2
    y = MARGIN + (avail_h - dh) / 2

    img_buf = io.BytesIO()
    pil.save(img_buf, format="PNG", optimize=True)
    img_buf.seek(0)

    pdf_buf = io.BytesIO()
    c = rl_canvas.Canvas(pdf_buf, pagesize=A4)
    c.drawImage(ImageReader(img_buf), x, y, dw, dh, preserveAspectRatio=True, mask="auto")
    c.setStrokeColorRGB(0.88, 0.88, 0.88)
    c.setLineWidth(0.5)
    c.rect(MARGIN - 2, MARGIN - 2, avail_w + 4, avail_h + 4)
    c.save()
    return pdf_buf.getvalue()


def save_as_pdf(file_bytes: bytes, mime_type: str, dest_path: str):
    """Save file as PDF. Images are converted to professional scanned PDFs."""
    if mime_type == "application/pdf":
        with open(dest_path, "wb") as f:
            f.write(file_bytes)
        return

    # Images → scanned PDF pipeline
    try:
        pdf_bytes = make_scanned_pdf(file_bytes)
        with open(dest_path, "wb") as f:
            f.write(pdf_bytes)
        print(f"[pdf] scanned PDF saved: {dest_path} ({os.path.getsize(dest_path)//1024}KB)")
        return
    except Exception as e:
        print(f"[pdf] scan pipeline failed: {e}, trying plain Pillow fallback")

    # Plain Pillow fallback
    try:
        import io
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(dest_path, "PDF", resolution=150, optimize=True)
        return
    except Exception as e:
        print(f"[pdf] Pillow fallback failed: {e}")

    with open(dest_path, "wb") as f:
        f.write(file_bytes)


# ── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(UPLOAD_BASE, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT, role TEXT,
        full_name TEXT, must_change_password INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS prescriptions (
        id INTEGER PRIMARY KEY, user_id INTEGER, doc_id INTEGER,
        date TEXT, doctor TEXT, data JSON
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY, user_id INTEGER, raw_text TEXT,
        upload_date TEXT, file_path TEXT, mime_type TEXT, doc_type TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS lab_reports (
        id INTEGER PRIMARY KEY, user_id INTEGER, doc_id INTEGER,
        date TEXT, lab_name TEXT, test_name TEXT, measured_value TEXT,
        reference_range TEXT, category TEXT
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, doc_id INTEGER,
        date TEXT, inv_type TEXT,
        summary TEXT, ai_analysis TEXT,
        file_path TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_enabled', 'false')")

    # Safe migrations
    for sql in [
        "ALTER TABLE lab_reports ADD COLUMN doc_id INTEGER",
        "ALTER TABLE prescriptions ADD COLUMN doc_id INTEGER",
        "ALTER TABLE documents ADD COLUMN file_path TEXT",
        "ALTER TABLE documents ADD COLUMN mime_type TEXT",
        "ALTER TABLE documents ADD COLUMN doc_type TEXT",
        "ALTER TABLE users ADD COLUMN full_name TEXT",
        "ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0",
        "ALTER TABLE investigations ADD COLUMN notes TEXT",
        "ALTER TABLE investigations ADD COLUMN doctor TEXT",
        "ALTER TABLE investigations ADD COLUMN clinic TEXT",
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass

    # Fix rows where category was stored as HIGH/LOW/NORMAL instead of panel name
    # (bug from lab extractor that repurposed Category field for status)
    c.execute("SELECT id, test_name, category FROM lab_reports WHERE UPPER(TRIM(category)) IN ('HIGH','LOW','NORMAL','ABSENT','PRESENT')")
    bad_rows = c.fetchall()
    if bad_rows:
        for row_id, tname, _ in bad_rows:
            panel = infer_panel(tname or "", "")
            c.execute("UPDATE lab_reports SET category=? WHERE id=?", (panel, row_id))
        print(f"[init_db] re-categorised {len(bad_rows)} lab_report rows with bad category")

    # ── Config table for app settings (Gemini keys, app name, etc.) ──────────
    c.execute("""CREATE TABLE IF NOT EXISTS config (
        key   TEXT PRIMARY KEY,
        value TEXT
    )""")
    # default app name
    c.execute("INSERT OR IGNORE INTO config (key,value) VALUES ('app_name','Family Health')")

    # ── Auto-bootstrap admin account ──────────────────────────────────────────
    # Priority: env vars > existing DB admin > auto-generate
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    admin_pass  = os.getenv("ADMIN_PASSWORD", "").strip()

    c.execute("SELECT id FROM users WHERE role='admin'")
    has_admin = c.fetchone()

    if not has_admin:
        if not admin_email:
            admin_email = "admin@family.health"
        if not admin_pass:
            # Generate memorable password: Word-Word-NNNN
            import random, string
            words = ["Tiger","River","Stone","Cloud","Maple","Frost","Cedar",
                     "Ember","Amber","Blaze","Creek","Delta","Eagle","Falcon"]
            admin_pass = f"{random.choice(words)}-{random.choice(words)}-{random.randint(1000,9999)}"
            must_change = 1
            # Print clearly to logs — only shown once on first boot
            banner = f"""
╔══════════════════════════════════════════════════╗
║          FAMILY HEALTH — FIRST BOOT              ║
║                                                  ║
║  Admin account created automatically:            ║
║  Email   : {admin_email:<38s}║
║  Password: {admin_pass:<38s}║
║                                                  ║
║  ⚠  Change this password after first login!     ║
║  Then add your Gemini API key in Admin → Keys    ║
╚══════════════════════════════════════════════════╝"""
            print(banner)
        else:
            must_change = 0  # user supplied their own password via env

        hashed = _bcrypt.hashpw(admin_pass.encode(), _bcrypt.gensalt()).decode()
        c.execute("""INSERT INTO users (email, password, role, full_name, must_change_password)
                     VALUES (?,?,?,?,?)""",
                  (admin_email, hashed, "admin", "Admin", must_change))
        print(f"[init_db] admin account created: {admin_email}")

    elif admin_email and admin_pass:
        # Env vars supplied but admin already exists — update credentials
        c.execute("SELECT email FROM users WHERE role='admin' ORDER BY id LIMIT 1")
        existing_email = c.fetchone()[0]
        if existing_email != admin_email or admin_pass:
            hashed = _bcrypt.hashpw(admin_pass.encode(), _bcrypt.gensalt()).decode()
            c.execute("UPDATE users SET email=?, password=?, must_change_password=0 WHERE role='admin' AND id=(SELECT MIN(id) FROM users WHERE role='admin')",
                      (admin_email, hashed))
            print(f"[init_db] admin credentials updated from env vars")

    # Family members are created manually via the Users tab — no auto-seeding

    # One-time dedup: remove duplicate emails, keep lowest id (oldest account)
    c.execute("DELETE FROM users WHERE id NOT IN (SELECT MIN(id) FROM users GROUP BY email)")
    duped = conn.total_changes
    if duped:
        print(f"[init_db] removed {duped} duplicate user row(s)")

    # Fix lab_reports where category is HIGH/LOW/NORMAL/General — infer real panel from test name
    bad_cats = ("HIGH","LOW","NORMAL","General","ABSENT","PRESENT","")
    placeholders = ",".join("?" * len(bad_cats))
    c.execute(f"SELECT id, test_name, category FROM lab_reports WHERE UPPER(category) IN ({placeholders})",
              [x.upper() for x in bad_cats])
    rows = c.fetchall()
    fixed = 0
    for rid, tname, cat in rows:
        panel = infer_panel(tname or "", cat or "")
        if panel != (cat or ""):
            c.execute("UPDATE lab_reports SET category=? WHERE id=?", (panel, rid))
            fixed += 1
    if fixed:
        print(f"[init_db] fixed panel category for {fixed} lab_report rows")

    conn.commit()
    conn.close()

init_db()


def _maybe_show_credentials_banner():
    """Print login credentials to logs if admin still has default password."""
    env_pass = os.getenv("ADMIN_PASSWORD", "").strip()
    if env_pass:
        return  # user set their own password via env — don't echo it
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Check if any admin still has must_change_password=1
    c.execute("""SELECT email FROM users 
                 WHERE role='admin' AND must_change_password=1 
                 ORDER BY id LIMIT 1""")
    row = c.fetchone()
    conn.close()
    if not row:
        return  # password already changed — stay silent
    # Admin hasn't changed password yet — remind them on every startup
    # We can't recover the plaintext password from the hash, so just remind
    # them to check the FIRST BOOT log or restart with a fresh data/ folder
    print("""
╔══════════════════════════════════════════════════╗
║        FAMILY HEALTH — PASSWORD NOT SET          ║
║                                                  ║
║  Admin password has not been changed yet.        ║
║  Check your FIRST BOOT log for the password, or  ║
║  set ADMIN_PASSWORD in docker-compose.yml to     ║
║  reset it to a known value.                      ║
║                                                  ║
║  Email: admin@family.health                      ║
╚══════════════════════════════════════════════════╝""")

_maybe_show_credentials_banner()


# ── AUTH ──────────────────────────────────────────────────────────────────────

def get_user_info(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token.replace("Bearer ", ""), SECRET_KEY, algorithms=[ALGORITHM])
        return {
            "id":        int(payload["sub"]),
            "role":      payload.get("role", "user"),
            "full_name": payload.get("full_name", ""),
        }
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def require_admin(request: Request) -> dict:
    info = get_user_info(request)
    if info["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return info


def match_patient_to_user(patient_name: str, conn) -> int | None:
    """Fuzzy-match extracted patient name to a known user. Returns user_id or None."""
    if not patient_name or patient_name in ("Not Found", ""):
        return None
    # Strip age/gender suffixes: "John Smith(36Y/M)" -> "John Smith"
    import re as _re2
    clean_name = _re2.sub(r'[\(\[].*?[\)\]]', '', patient_name)       # remove (...)
    clean_name = _re2.sub(r'\s*[-–]\s*\d+\s*[YyMm].*', '', clean_name)  # remove - 36Y...
    clean_name = _re2.sub(r'(Mr\.?|Mrs\.?|Ms\.?|Dr\.?|M/s\.?)\s*', '', clean_name, flags=_re2.IGNORECASE)
    clean_name = clean_name.strip()
    patient_name = clean_name or patient_name
    c = conn.cursor()
    c.execute("SELECT id, full_name FROM users WHERE full_name IS NOT NULL AND full_name != ''")
    pwords = set(patient_name.lower().split())
    best_uid, best_score = None, 0.0
    for uid, full_name in c.fetchall():
        fwords = set(full_name.lower().split())
        overlap = len(pwords & fwords)
        score = overlap / max(len(pwords), len(fwords), 1)
        if score >= 0.5 and score > best_score:
            best_score, best_uid = score, uid
    if best_uid:
        print(f"[match] '{patient_name}' → user_id={best_uid} (score={best_score:.2f})")
    return best_uid


# ── ROUTES: AUTH ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, password, role, full_name, must_change_password FROM users WHERE email=?", (email,))
    user = c.fetchone()
    conn.close()

    if not user or not _bcrypt.checkpw(password.encode(), user[1].encode() if isinstance(user[1], str) else user[1]):
        print(f"[login] FAILED for: {email}")
        return RedirectResponse(url="/?error=invalid", status_code=status.HTTP_302_FOUND)

    token = jwt.encode(
        {"sub": str(user[0]), "role": user[2], "full_name": user[3] or "",
         "exp": datetime.utcnow() + timedelta(hours=24)},
        SECRET_KEY, algorithm=ALGORITHM)

    dest = "/change-password" if user[4] == 1 else "/dashboard"
    resp = RedirectResponse(url=dest, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(key="access_token", value=f"Bearer {token}", httponly=True)
    print(f"[login] OK: {email} role={user[2]}")
    return resp


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    get_user_info(request)
    return templates.TemplateResponse(request, "change_password.html")


@app.post("/change-password")
async def change_password(request: Request, new_password: str = Form(...)):
    info = get_user_info(request)
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password too short (min 6 chars)")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
              (_bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode(), info["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        get_user_info(request)
        return templates.TemplateResponse(request, "dashboard.html")
    except Exception:
        return RedirectResponse(url="/")


# ── ROUTES: SETTINGS ──────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Simple health check for Docker and load balancers."""
    return {"status": "ok", "app": "Family Health"}


@app.get("/api/version")
async def get_version():
    """Return app version and build info baked in at image build time."""
    return {
        "version":   os.getenv("APP_VERSION", "dev"),
        "build":     os.getenv("APP_BUILD",   "local"),
        "app":       "Family Health",
    }


@app.get("/api/settings")
def get_settings(request: Request):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key='ai_enabled'")
    row = c.fetchone()
    conn.close()
    return {
        "ai_enabled": row[0] == "true" if row else False,
        "role":       info["role"],
        "user_id":    info["id"],
        "full_name":  info["full_name"],
    }


@app.post("/api/settings")
async def update_settings(request: Request, ai_enabled: str = Form(...)):
    get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE settings SET value=? WHERE key='ai_enabled'", (ai_enabled,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ── ROUTES: UPLOAD ────────────────────────────────────────────────────────────

@app.post("/upload/auto")
async def upload_auto_detect(request: Request, file: UploadFile = File(...),
                             type_hint: str = Form(None)):
    """Auto-detect whether file is a lab report or prescription, then process it.
    type_hint: optional 'lab' or 'prescription' from the UI tab — overrides AI if provided."""
    info = get_user_info(request)
    uploader_id = info["id"]
    file_bytes = await file.read()
    mime = file.content_type or "application/pdf"

    # Step 1: extract text
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Convert image to scanned PDF for uniform handling
    import io
    if mime.startswith("image/"):
        try:
            file_bytes = make_scanned_pdf(file_bytes)
        except Exception as _e:
            from PIL import Image, ImageOps
            img = Image.open(io.BytesIO(file_bytes))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PDF", optimize=True)
            file_bytes = buf.getvalue()
        mime = "application/pdf"

    # Step 2: classify document type
    # If the user uploaded from a specific tab (type_hint), trust that intent.
    # Only run AI detection when we have no hint — avoids misclassification.
    if type_hint in ("lab", "prescription"):
        doc_type = type_hint
        print(f"[upload] doc_type from UI hint: {doc_type!r}")
    else:
        doc_type = "lab"
        try:
            from gemini_utils import detect_doc_type_ai
            detected_word = detect_doc_type_ai(file_bytes, mime)
            if detected_word in ("lab", "prescription"):
                doc_type = detected_word
            print(f"[upload] doc_type from AI detection: {doc_type!r}")
        except Exception:
            doc_type = "lab"  # safe default
            print("[upload] doc_type AI failed, defaulting to 'lab'")

    # Step 3: Insert document record
    c.execute("SELECT full_name, email FROM users WHERE id=?", (uploader_id,))
    urow = c.fetchone()
    uploader_name = (urow[0] or urow[1] or "unknown") if urow else "unknown"

    c.execute(
        "INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) VALUES (?,?,?,?,?,?)",
        (uploader_id, "", datetime.now().isoformat(), "pending", mime, doc_type)
    )
    doc_id = c.lastrowid
    conn.commit()

    result = {"doc_type": doc_type, "doc_id": doc_id}

    if doc_type == "lab":
        from gemini_utils import analyze_lab_from_file
        extracted = analyze_lab_from_file(file_bytes, mime)

        date     = normalize_date_str(extracted.get("Date", "Not Found"))
        lab_name = extracted.get("Lab_Name", "Not Found")
        patient_name = extracted.get("Patient_Name", "")
        owner_id = match_patient_to_user(patient_name, conn) or uploader_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name

        file_path = build_upload_path(owner_name, "lab_report", date)
        save_as_pdf(file_bytes, mime, file_path)
        c.execute("UPDATE documents SET user_id=?, file_path=?, doc_type='lab_report' WHERE id=?", (owner_id, file_path, doc_id))

        results = extracted.get("Results", [])
        for res in results:
            c.execute(
                "INSERT INTO lab_reports (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category) VALUES (?,?,?,?,?,?,?,?)",
                (owner_id, doc_id, date, lab_name,
                 canonical_test_name(res.get("Test_Name", "")),
                 str(res.get("Measured_Value", "")),
                 res.get("Reference_Range", ""),
                 infer_panel(res.get("Test_Name",""), res.get("Category","")))
            )
        conn.commit()
        conn.close()
        n = len(results)
        result.update({"tests": n, "patient": owner_name, "date": date, "lab": lab_name, "message": f"Lab report — {n} tests extracted for {owner_name}"})

    else:  # prescription
        from gemini_utils import extract_prescription_smart
        extracted = extract_prescription_smart(file_bytes, mime, "")
        date     = normalize_date_str(extracted.get("Date", "Not Found"))
        extracted["Date"] = date
        doc_name = extracted.get("Doctor_Name", extracted.get("Doctor", "Not Found"))
        patient_name = extracted.get("Patient_Name", "")
        owner_id = match_patient_to_user(patient_name, conn) or uploader_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name

        file_path = build_upload_path(owner_name, "prescription", date)
        save_as_pdf(file_bytes, mime, file_path)
        c.execute("UPDATE documents SET user_id=?, file_path=?, doc_type='prescription' WHERE id=?", (owner_id, file_path, doc_id))
        c.execute(
            "INSERT INTO prescriptions (user_id, doc_id, date, doctor, data) VALUES (?,?,?,?,?)",
            (owner_id, doc_id, date, doc_name, json.dumps(extracted))
        )
        conn.commit()
        conn.close()
        meds = len(extracted.get("Medicines", []))
        result.update({"medicines": meds, "patient": owner_name, "date": date, "doctor": doc_name, "message": f"Prescription — {meds} medicines for {owner_name} (Dr. {doc_name})"})

    return result


@app.post("/upload")
async def upload_document(request: Request, doc_type: str = Form(...), file: UploadFile = File(...)):
    info = get_user_info(request)
    uploader_id = info["id"]
    file_bytes = await file.read()
    mime = file.content_type

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, email FROM users WHERE id=?", (uploader_id,))
    urow = c.fetchone()
    uploader_name = (urow[0] or urow[1] or "unknown") if urow else "unknown"

    # Insert document record first (with temp path)
    c.execute("INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) VALUES (?,?,?,?,?,?)",
              (uploader_id, "", datetime.now().isoformat(), "pending", mime, doc_type))
    doc_id = c.lastrowid
    conn.commit()

    if doc_type == "lab_report":
        # Send file directly to Gemini — no local OCR
        from gemini_utils import analyze_lab_from_file
        extracted = analyze_lab_from_file(file_bytes, mime)
        raw_text = ""  # no local OCR
        c.execute("UPDATE documents SET raw_text=? WHERE id=?", (raw_text, doc_id))
        conn.commit()

        date = normalize_date_str(extracted.get("Date", "Not Found"))
        lab  = extracted.get("Lab_Name", "Not Found")
        patient_name = extracted.get("Patient_Name", "")

        # Determine owner
        owner_id = match_patient_to_user(patient_name, conn) or uploader_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name

        # Save file with structured name
        file_path = build_upload_path(owner_name, "lab_report", date)
        save_as_pdf(file_bytes, mime, file_path)

        # Update document record
        c.execute("UPDATE documents SET user_id=?, file_path=?, mime_type='application/pdf' WHERE id=?",
                  (owner_id, file_path, doc_id))

        # Insert lab results
        results = extracted.get("Results", [])
        for res in results:
            c.execute("""INSERT INTO lab_reports
                         (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (owner_id, doc_id, date, lab,
                       canonical_test_name(res.get("Test_Name", "")),
                       str(res.get("Measured_Value", "")),
                       res.get("Reference_Range", ""),
                       infer_panel(res.get("Test_Name",""), res.get("Category",""))))
        conn.commit()
        conn.close()
        n = len(results)
        print(f"[upload] lab doc_id={doc_id} owner={owner_id} path={file_path} tests={n}")
        return {"message": f"Extracted {n} tests" if n else "Saved — use 🧠 AI Enhance to extract data", "data": extracted}

    elif doc_type == "prescription":
        # Send file directly to Gemini Vision — no local OCR
        raw_text = ""
        c.execute("UPDATE documents SET raw_text=? WHERE id=?", (raw_text, doc_id))
        conn.commit()
        extracted = extract_prescription_smart(file_bytes, mime, "")

        date = normalize_date_str(extracted.get("Date", "Not Found"))
        extracted["Date"] = date
        doc_name = extracted.get("Doctor_Name", "Not Found")
        patient_name = extracted.get("Patient_Name", "")

        owner_id = match_patient_to_user(patient_name, conn) or uploader_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name

        file_path = build_upload_path(owner_name, "prescription", date)
        save_as_pdf(file_bytes, mime, file_path)

        c.execute("UPDATE documents SET user_id=?, file_path=?, mime_type='application/pdf' WHERE id=?",
                  (owner_id, file_path, doc_id))
        c.execute("INSERT INTO prescriptions (user_id, doc_id, date, doctor, data) VALUES (?,?,?,?,?)",
                  (owner_id, doc_id, date, doc_name, json.dumps(extracted)))
        conn.commit()
        conn.close()
        print(f"[upload] rx doc_id={doc_id} owner={owner_id} path={file_path}")
        return {"message": "Extracted with AI Vision", "data": extracted}

    conn.close()
    return {"message": "Unknown doc_type"}


# ── ROUTES: ENHANCE ───────────────────────────────────────────────────────────

@app.post("/api/enhance-lab/{doc_id}")
async def enhance_lab(request: Request, doc_id: int):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if info["role"] == "admin":
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=?", (doc_id,))
    else:
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=? AND user_id=?", (doc_id, info["id"]))
    record = c.fetchone()
    if not record:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Document not found"})

    fpath, fmime = record[0], (record[1] if len(record) > 1 else "application/pdf")
    try:
        with open(fpath, "rb") as fh: fbytes = fh.read()
    except Exception as e:
        conn.close()
        return JSONResponse(status_code=404, content={"error": f"File missing: {e}"})
    from gemini_utils import analyze_lab_from_file
    ai_data = analyze_lab_from_file(fbytes, fmime)
    if "error" in ai_data:
        conn.close()
        return JSONResponse(status_code=503, content=ai_data)

    c.execute("SELECT test_name, measured_value FROM lab_reports WHERE doc_id=?", (doc_id,))
    existing = {row[0].lower(): row[1] for row in c.fetchall()}

    date = normalize_date_str(ai_data.get("Date", "Not Found"))
    lab  = ai_data.get("Lab_Name", "Not Found")
    added = 0
    for res in ai_data.get("Results", []):
        name = canonical_test_name(res.get("Test_Name", ""))
        val  = str(res.get("Measured_Value", ""))
        ref  = res.get("Reference_Range", "")
        cat  = infer_panel(res.get("Test_Name",""), res.get("Category",""))
        if not name or not val:
            continue
        if name.lower() in existing and existing[name.lower()] not in ("", "Missing"):
            c.execute("UPDATE lab_reports SET reference_range=?, category=? WHERE doc_id=? AND LOWER(test_name)=?",
                      (ref, cat, doc_id, name.lower()))
        else:
            c.execute("""INSERT OR REPLACE INTO lab_reports
                         (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category)
                         VALUES ((SELECT user_id FROM documents WHERE id=?),?,?,?,?,?,?,?)""",
                      (doc_id, doc_id, date, lab, name, val, ref, cat))
            added += 1
    if date != "Not Found":
        c.execute("UPDATE lab_reports SET date=?, lab_name=? WHERE doc_id=?", (date, lab, doc_id))
    conn.commit()
    conn.close()
    print(f"[enhance-lab] doc_id={doc_id} added={added}")
    return {"message": f"AI merged: {added} new results", "data": ai_data}


@app.post("/api/enhance-rx/{doc_id}")
async def enhance_rx(request: Request, doc_id: int):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if info["role"] == "admin":
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=?", (doc_id,))
    else:
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=? AND user_id=?", (doc_id, info["id"]))
    record = c.fetchone()
    if not record:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "File not found"})

    with open(record[0], "rb") as f:
        file_bytes = f.read()
    extracted = extract_prescription_ai(file_bytes, record[1])
    if "error" in extracted:
        conn.close()
        return JSONResponse(status_code=400, content=extracted)

    date = normalize_date_str(extracted.get("Date", "Not Found"))
    extracted["Date"] = date
    c.execute("SELECT user_id FROM documents WHERE id=?", (doc_id,))
    orow = c.fetchone()
    uid = orow[0] if orow else info["id"]
    c.execute("DELETE FROM prescriptions WHERE doc_id=?", (doc_id,))
    c.execute("INSERT INTO prescriptions (user_id, doc_id, date, doctor, data) VALUES (?,?,?,?,?)",
              (uid, doc_id, date, extracted.get("Doctor_Name", "Not Found"), json.dumps(extracted)))
    conn.commit()
    conn.close()
    return {"message": "AI Enhancement Complete", "data": extracted}


# ── ROUTES: DELETE ────────────────────────────────────────────────────────────

@app.delete("/api/labs")
async def delete_lab(request: Request, raw_date: str, lab_name: str, doc_id: int = None):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if doc_id and str(doc_id) != "null":
        c.execute("SELECT file_path FROM documents WHERE id=?", (doc_id,))
        row = c.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            try: os.remove(row[0])
            except Exception: pass
        c.execute("DELETE FROM lab_reports WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    else:
        c.execute("DELETE FROM lab_reports WHERE date=? AND lab_name=? AND user_id=?",
                  (raw_date, lab_name, info["id"]))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@app.patch("/api/prescriptions/{rx_id}")
async def edit_prescription(rx_id: int, request: Request):
    """Admin quick-edit: update date, doctor, and any data fields."""
    require_admin(request)
    body = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT data FROM prescriptions WHERE id=?", (rx_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Prescription not found")
    existing = json.loads(row[0] or "{}")
    # Merge top-level fields
    if "date" in body:
        c.execute("UPDATE prescriptions SET date=? WHERE id=?", (body["date"], rx_id))
    if "doctor" in body:
        c.execute("UPDATE prescriptions SET doctor=? WHERE id=?", (body["doctor"], rx_id))
    # Merge data JSON fields
    data_fields = ["Patient_Name","Age_Gender","Chief_Complaint","History",
                   "Diagnosis","Medicines","Investigations","Advice","Follow_Up","Notes"]
    for f in data_fields:
        if f in body:
            existing[f] = body[f]
    c.execute("UPDATE prescriptions SET data=? WHERE id=?", (json.dumps(existing), rx_id))
    conn.commit(); conn.close()
    return {"ok": True}


@app.patch("/api/investigations/{inv_id}/edit")
async def edit_investigation(inv_id: int, request: Request):
    """Admin quick-edit: update date, inv_type, doctor, clinic."""
    require_admin(request)
    body = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    fields, vals = [], []
    for col in ("date", "inv_type", "doctor", "clinic"):
        if col in body:
            fields.append(f"{col}=?")
            vals.append(body[col])
    if fields:
        vals.append(inv_id)
        c.execute(f"UPDATE investigations SET {','.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/prescriptions/{rx_id}")
async def delete_prescription(request: Request, rx_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT doc_id FROM prescriptions WHERE id=?", (rx_id,))
    row = c.fetchone()
    if row and row[0]:
        c.execute("SELECT file_path FROM documents WHERE id=?", (row[0],))
        drow = c.fetchone()
        if drow and drow[0] and os.path.exists(drow[0]):
            try: os.remove(drow[0])
            except Exception: pass
    c.execute("DELETE FROM prescriptions WHERE id=?", (rx_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ── ROUTES: LAB MATRIX ────────────────────────────────────────────────────────


@app.post("/api/admin/normalize-tests")
async def normalize_existing_tests(request: Request):
    """One-time: canonicalize all existing test_name values in DB."""
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, test_name FROM lab_reports")
    rows = c.fetchall()
    updated = 0
    for rid, raw_name in rows:
        canon = canonical_test_name(raw_name or "")
        if canon != raw_name:
            c.execute("UPDATE lab_reports SET test_name=? WHERE id=?", (canon, rid))
            updated += 1
    conn.commit()
    conn.close()
    print(f"[normalize-tests] renamed {updated} of {len(rows)} rows")
    return {"normalized": updated, "total": len(rows)}


@app.post("/api/admin/reclassify-doc")
async def reclassify_doc(request: Request):
    """Move a misclassified document from lab→prescription or vice versa.
    Deletes the wrong extraction rows and re-runs the correct one."""
    require_admin(request)
    body = await request.json()
    doc_id  = int(body.get("doc_id", 0))
    new_type = body.get("new_type", "")  # "lab" or "prescription"
    if not doc_id or new_type not in ("lab", "prescription"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="doc_id and new_type ('lab'|'prescription') required")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_path, mime_type, doc_type, user_id FROM documents WHERE id=?", (doc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")

    file_path, mime, old_type, user_id = row
    if old_type == new_type:
        conn.close()
        return {"ok": True, "message": "Already classified as " + new_type}

    # Read the file
    if not file_path or not os.path.exists(file_path):
        conn.close()
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Source file not found on disk")
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    mime = mime or "application/pdf"

    # Delete old extraction rows
    if old_type in ("lab", "lab_report"):
        c.execute("DELETE FROM lab_reports WHERE doc_id=?", (doc_id,))
        print(f"[reclassify] deleted lab_reports for doc_id={doc_id}")
    elif old_type == "prescription":
        c.execute("DELETE FROM prescriptions WHERE doc_id=?", (doc_id,))
        print(f"[reclassify] deleted prescriptions for doc_id={doc_id}")
    conn.commit()

    # Get uploader name for path building
    c.execute("SELECT full_name, email FROM users WHERE id=?", (user_id,))
    urow = c.fetchone()
    uploader_name = (urow[0] or urow[1] or "unknown") if urow else "unknown"

    if new_type == "prescription":
        from gemini_utils import extract_prescription_smart
        extracted = extract_prescription_smart(file_bytes, mime, "")
        patient_name = extracted.get("Patient_Name", "")
        owner_id = match_patient_to_user(patient_name, conn) or user_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name
        date = normalize_date_str(extracted.get("Date", "Not Found"))
        new_path = build_upload_path(owner_name, "prescription", date)
        import shutil; shutil.move(file_path, new_path)
        doctor = extracted.get("Doctor_Name", "Not Found")
        c.execute("UPDATE documents SET doc_type='prescription', file_path=?, user_id=? WHERE id=?",
                  (new_path, owner_id, doc_id))
        c.execute("INSERT INTO prescriptions (user_id, doc_id, date, doctor, data) VALUES (?,?,?,?,?)",
                  (owner_id, doc_id, date, doctor, json.dumps(extracted)))
        conn.commit(); conn.close()
        print(f"[reclassify] doc_id={doc_id} lab→prescription doctor={doctor!r} date={date}")
        return {"ok": True, "new_type": "prescription", "doctor": doctor, "date": date,
                "patient": owner_name}

    else:  # lab
        from gemini_utils import analyze_lab_from_file
        extracted = analyze_lab_from_file(file_bytes, mime)
        patient_name = extracted.get("Patient_Name", "")
        owner_id = match_patient_to_user(patient_name, conn) or user_id
        c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
        orow = c.fetchone()
        owner_name = (orow[0] or orow[1] or uploader_name) if orow else uploader_name
        date  = normalize_date_str(extracted.get("Date", "Not Found"))
        lab   = extracted.get("Lab_Name", "Not Found")
        new_path = build_upload_path(owner_name, "lab_report", date)
        import shutil; shutil.move(file_path, new_path)
        c.execute("UPDATE documents SET doc_type='lab_report', file_path=?, user_id=? WHERE id=?",
                  (new_path, owner_id, doc_id))
        results = extracted.get("Results", [])
        for res in results:
            c.execute("""INSERT INTO lab_reports
                         (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (owner_id, doc_id, date, lab,
                       canonical_test_name(res.get("Test_Name", "")),
                       res.get("Measured_Value", ""), res.get("Reference_Range", ""),
                       infer_panel(res.get("Test_Name", ""), res.get("Category", ""))))
        conn.commit(); conn.close()
        print(f"[reclassify] doc_id={doc_id} prescription→lab {len(results)} tests")
        return {"ok": True, "new_type": "lab", "tests": len(results),
                "patient": owner_name, "date": date, "lab": lab}


# ── CONFIG / API KEYS ─────────────────────────────────────────────────────

@app.get("/api/admin/config")
async def get_config(request: Request):
    """Return app config (keys masked)."""
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT key, value FROM config")
    cfg = dict(c.fetchall())
    conn.close()
    # Mask key values — return only last 6 chars + count
    raw_keys = cfg.get("gemini_keys", "")
    keys_list = [k.strip() for k in raw_keys.split(",") if k.strip()]
    masked = [f"...{k[-6:]}" for k in keys_list]
    return {
        "app_name":    cfg.get("app_name", "Family Health"),
        "gemini_keys": masked,
        "key_count":   len(masked),
        "source":      "env" if os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") else "db",
    }

@app.post("/api/admin/config/keys")
async def save_gemini_keys(request: Request):
    """Save Gemini API keys to DB config (comma-separated). Env vars take priority."""
    require_admin(request)
    body = await request.json()
    raw = body.get("keys", "")
    keys = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
    if not keys:
        raise HTTPException(status_code=400, detail="At least one key required")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key,value) VALUES ('gemini_keys',?)",
              (",".join(keys),))
    conn.commit(); conn.close()
    # Reload into gemini_utils immediately
    from gemini_utils import reload_keys
    reload_keys()
    print(f"[config] saved {len(keys)} Gemini key(s) to DB")
    return {"ok": True, "count": len(keys)}

@app.post("/api/admin/config/keys/append")
async def append_gemini_key(request: Request):
    """Append a single new key to the DB config list."""
    require_admin(request)
    body = await request.json()
    key = body.get("key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Key required")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key='gemini_keys'")
    row = c.fetchone()
    existing = [k.strip() for k in (row[0] if row else "").split(",") if k.strip()]
    if key in existing:
        conn.close()
        return {"ok": True, "count": len(existing), "note": "key already exists"}
    existing.append(key)
    c.execute("INSERT OR REPLACE INTO config (key,value) VALUES ('gemini_keys',?)",
              (",".join(existing),))
    conn.commit(); conn.close()
    from gemini_utils import reload_keys
    reload_keys()
    print(f"[config] appended key, total now: {len(existing)}")
    return {"ok": True, "count": len(existing)}


@app.post("/api/admin/config/keys/test")
async def test_gemini_key(request: Request):
    """Test a single Gemini API key by making a minimal API call."""
    require_admin(request)
    body = await request.json()
    key = body.get("key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Key required")
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=["Reply with just the word: ok"],
            config=types.GenerateContentConfig(max_output_tokens=5)
        )
        result = (resp.text or "").strip().lower()
        ok = "ok" in result or len(result) < 20
        return {"ok": ok, "response": resp.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.delete("/api/admin/config/keys/{index}")
async def delete_gemini_key(index: int, request: Request):
    """Remove a key by index from DB config."""
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key='gemini_keys'")
    row = c.fetchone()
    keys = [k.strip() for k in (row[0] if row else "").split(",") if k.strip()]
    if index < 0 or index >= len(keys):
        conn.close()
        raise HTTPException(status_code=404, detail="Key index out of range")
    keys.pop(index)
    c.execute("INSERT OR REPLACE INTO config (key,value) VALUES ('gemini_keys',?)",
              (",".join(keys),))
    conn.commit(); conn.close()
    from gemini_utils import reload_keys
    reload_keys()
    return {"ok": True, "remaining": len(keys)}

@app.post("/api/admin/config/app-name")
async def save_app_name(request: Request):
    require_admin(request)
    body = await request.json()
    name = (body.get("name") or "Family Health").strip()[:50]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key,value) VALUES ('app_name',?)", (name,))
    conn.commit(); conn.close()
    return {"ok": True, "app_name": name}


@app.post("/api/admin/merge-tests")
async def merge_tests(request: Request):
    """Rename all lab_reports rows where test_name matches `from_name` → `to_name`."""
    require_admin(request)
    body = await request.json()
    from_name = (body.get("from_name") or "").strip()
    to_name   = (body.get("to_name")   or "").strip()
    if not from_name or not to_name:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both from_name and to_name are required")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Count rows that will be affected
    c.execute("SELECT COUNT(*) FROM lab_reports WHERE test_name=?", (from_name,))
    count = c.fetchone()[0]
    c.execute("UPDATE lab_reports SET test_name=? WHERE test_name=?", (to_name, from_name))
    conn.commit(); conn.close()
    print(f"[merge-tests] '{from_name}' → '{to_name}': {count} rows renamed")
    return {"merged": count, "from_name": from_name, "to_name": to_name}


@app.get("/api/admin/test-names")
async def list_test_names(request: Request, q: str = ""):
    """Return distinct test names (optionally filtered) for the merge UI autocomplete."""
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if q:
        c.execute("SELECT DISTINCT test_name FROM lab_reports WHERE test_name LIKE ? ORDER BY test_name LIMIT 60",
                  (f"%{q}%",))
    else:
        c.execute("SELECT DISTINCT test_name FROM lab_reports ORDER BY test_name LIMIT 200")
    names = [r[0] for r in c.fetchall() if r[0]]
    conn.close()
    return {"names": names}


@app.delete("/api/admin/test-row")
async def delete_test_row(request: Request, test_name: str, user_id: int = None):
    """Delete all lab_reports rows for a given test_name (optionally for one user)."""
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id:
        c.execute("SELECT COUNT(*) FROM lab_reports WHERE test_name=? AND user_id=?", (test_name, user_id))
        count = c.fetchone()[0]
        c.execute("DELETE FROM lab_reports WHERE test_name=? AND user_id=?", (test_name, user_id))
    else:
        c.execute("SELECT COUNT(*) FROM lab_reports WHERE test_name=?", (test_name,))
        count = c.fetchone()[0]
        c.execute("DELETE FROM lab_reports WHERE test_name=?", (test_name,))
    conn.commit(); conn.close()
    print(f"[delete-test-row] '{test_name}' user_id={user_id}: {count} rows deleted")
    return {"deleted": count, "test_name": test_name}


@app.post("/api/admin/reset-all-data")
async def reset_all_data(request: Request):
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM lab_reports")
    c.execute("DELETE FROM prescriptions")
    c.execute("DELETE FROM documents")
    conn.commit()
    conn.close()
    deleted_files = 0
    if os.path.exists(UPLOAD_BASE):
        for root, dirs, files in os.walk(UPLOAD_BASE):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                    deleted_files += 1
                except Exception:
                    pass
    print(f"[reset-all-data] cleared all records, removed {deleted_files} files")
    return {"ok": True, "deleted_files": deleted_files}


@app.post("/api/admin/bulk-upload")
async def bulk_upload(
    background_tasks: BackgroundTasks,
    request: Request,
    files: list[UploadFile] = File(...),
    user_id: int = Form(None),
    doc_type: str = Form("auto")
):
    """Accepts files, returns job_id immediately. Processing runs in background."""
    require_admin(request)
    import uuid as _uuid
    admin_info = get_user_info(request)
    admin_uid  = admin_info["id"]

    # Read file bytes NOW (UploadFile closes after response)
    file_payloads = []
    for uf in files:
        b = await uf.read()
        file_payloads.append({"filename": uf.filename, "content_type": uf.content_type or "application/pdf", "bytes": b})

    job_id = str(_uuid.uuid4())[:8]
    with _bulk_jobs_lock:
        _bulk_jobs[job_id] = {"status": "running", "total": len(file_payloads), "done": 0, "results": [], "error": None}

    background_tasks.add_task(_run_bulk_job, job_id, file_payloads, user_id, doc_type, admin_uid)
    return {"job_id": job_id, "total": len(file_payloads), "status": "running"}


def _run_bulk_job(job_id, file_payloads, user_id, doc_type, admin_uid):
    """Background worker - processes each file and updates job store."""
    from datetime import date as _date_cls
    import io
    results = []

    for fp in file_payloads:
        fname         = fp["filename"]
        mime          = fp["content_type"]
        content_bytes = fp["bytes"]
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            effective_uid = user_id or admin_uid
            c.execute("SELECT full_name, email FROM users WHERE id=?", (effective_uid,))
            row = c.fetchone()
            username = (row[0] or row[1]).split("@")[0] if row else "unknown"
            today = _date_cls.today().strftime("%d-%m-%Y")
            effective_type = doc_type if doc_type != "auto" else "lab"
            file_path = build_upload_path(username, effective_type, today)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            if mime.startswith("image/"):
                try:
                    file_bytes = make_scanned_pdf(content_bytes)
                except Exception:
                    from PIL import Image, ImageOps
                    img = Image.open(io.BytesIO(content_bytes))
                    img = ImageOps.exif_transpose(img)
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="PDF", optimize=True)
                    file_bytes = buf.getvalue()
                mime = "application/pdf"
            else:
                file_bytes = content_bytes
            with open(file_path, "wb") as fh:
                fh.write(file_bytes)
            upload_date = _date_cls.today().isoformat()
            c.execute(
                "INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) VALUES (?,?,?,?,?,?)",
                (effective_uid, "", upload_date, file_path, mime, effective_type)
            )
            doc_id = c.lastrowid
            conn.commit()
            conn.close()

            try:
                from gemini_utils import detect_doc_type_ai, analyze_lab_from_file, extract_prescription_smart

                if doc_type == "auto":
                    detected = detect_doc_type_ai(file_bytes, mime)
                    if detected == "unknown":
                        detected = "lab"
                else:
                    detected = doc_type

                print(f"[bulk] {fname!r} -> detected={detected!r}")
                raw_text = ""

                conn2 = sqlite3.connect(DB_PATH)
                c2 = conn2.cursor()

                if detected == "prescription":
                    rx = extract_prescription_smart(file_bytes, mime, raw_text)
                    patient_name = rx.get("Patient_Name","") or rx.get("patient","")
                    if patient_name in ("Not Found", "null", "N/A", None): patient_name = ""
                    tagged_uid = match_patient_to_user(patient_name, sqlite3.connect(DB_PATH)) or effective_uid
                    c2.execute("UPDATE documents SET user_id=? WHERE id=?", (tagged_uid, doc_id))
                    c2.execute("INSERT INTO prescriptions (user_id,doc_id,date,doctor,data) VALUES (?,?,?,?,?)",
                        (tagged_uid, doc_id, rx.get("Date",""), rx.get("Doctor",""), json.dumps(rx)))
                    conn2.commit(); conn2.close()
                    results.append({"file": fname, "ok": True, "type": "Prescription", "patient": patient_name})

                elif detected == "lab":
                    parsed = analyze_lab_from_file(file_bytes, mime)
                    n_tests = len(parsed.get("Results", []))
                    if n_tests == 0:
                        print(f"[bulk] 0 tests, retrying as prescription")
                        conn2.close()
                        rx = extract_prescription_smart(file_bytes, mime, raw_text)
                        patient_name = rx.get("Patient_Name","") or rx.get("patient","")
                        if patient_name in ("Not Found", "null", "N/A", None): patient_name = ""
                        tagged_uid = match_patient_to_user(patient_name, sqlite3.connect(DB_PATH)) or effective_uid
                        conn2b = sqlite3.connect(DB_PATH); c2b = conn2b.cursor()
                        c2b.execute("UPDATE documents SET user_id=? WHERE id=?", (tagged_uid, doc_id))
                        c2b.execute("INSERT INTO prescriptions (user_id,doc_id,date,doctor,data) VALUES (?,?,?,?,?)",
                            (tagged_uid, doc_id, rx.get("Date",""), rx.get("Doctor",""), json.dumps(rx)))
                        conn2b.commit(); conn2b.close()
                        results.append({"file": fname, "ok": True, "type": "Prescription (auto)", "patient": patient_name})
                    else:
                        patient_name = parsed.get("Patient_Name","")
                        if patient_name in ("Not Found", "null", "N/A", None): patient_name = ""
                        tagged_uid = match_patient_to_user(patient_name, sqlite3.connect(DB_PATH)) or effective_uid
                        lab_name   = parsed.get("Lab_Name","") or ""
                        lab_date   = parsed.get("Date","") or ""
                        if lab_name in ("Not Found", "null", "N/A", None): lab_name = ""
                        if lab_date in ("Not Found", "null", "N/A", None): lab_date = ""
                        c2.execute("UPDATE documents SET user_id=?, doc_type='lab' WHERE id=?", (tagged_uid, doc_id))
                        saved = 0
                        for entry in parsed.get("Results", []):
                            if not isinstance(entry, dict): continue
                            # analyze_lab_from_file returns: Test_Name, Measured_Value, Unit, Reference_Range, Category
                            raw_test = entry.get("Test_Name","") or entry.get("test_name","")
                            test = canonical_test_name(raw_test.strip())
                            if not test: continue
                            val_raw = str(entry.get("Measured_Value","") or entry.get("value","")).strip()
                            ref     = str(entry.get("Reference_Range","") or entry.get("reference_range","")).strip()
                            cat_raw = str(entry.get("Category","") or entry.get("status","")).strip()
                            # Re-derive panel from test name (ignore status-like values in Category)
                            category = infer_panel(test, cat_raw)
                            c2.execute(
                                """INSERT INTO lab_reports
                                   (user_id, doc_id, date, lab_name, test_name, measured_value, reference_range, category)
                                   VALUES (?,?,?,?,?,?,?,?)""",
                                (tagged_uid, doc_id, lab_date, lab_name, test, val_raw, ref, category)
                            )
                            saved += 1
                        conn2.commit(); conn2.close()
                        print(f"[bulk] saved {saved} lab rows for doc_id={doc_id} user={tagged_uid}")
                        results.append({"file": fname, "ok": True, "type": "Lab", "patient": patient_name, "tests": n_tests})
                else:
                    conn2.close()
                    results.append({"file": fname, "ok": False,
                                    "error": f"Detected as '{detected}' - use Investigations tab for imaging reports"})

            except Exception as ex:
                import traceback; traceback.print_exc()
                results.append({"file": fname, "ok": True, "warn": str(ex)})
        except Exception as ex:
            import traceback; traceback.print_exc()
            results.append({"file": fname, "ok": False, "error": str(ex)})

        with _bulk_jobs_lock:
            _bulk_jobs[job_id]["done"] += 1
            _bulk_jobs[job_id]["results"] = results[:]

    with _bulk_jobs_lock:
        _bulk_jobs[job_id]["status"] = "done"
    print(f"[bulk] job {job_id} complete: {len(results)} files")


@app.get("/api/admin/bulk-upload/status/{job_id}")
async def bulk_upload_status(job_id: str, request: Request):
    require_admin(request)
    with _bulk_jobs_lock:
        job = _bulk_jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job




@app.get("/api/document/{doc_id}/view")
async def view_document(doc_id: int, request: Request):
    """Serve the original uploaded file (PDF) for inline browser viewing."""
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Admins can view any doc; users only their own
    if info["role"] == "admin":
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=?", (doc_id,))
    else:
        c.execute("SELECT file_path, mime_type FROM documents WHERE id=? AND user_id=?",
                  (doc_id, info["id"]))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = row[0]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )

@app.get("/api/matrix")
async def get_lab_matrix(request: Request, filter_days: int = 36500, view_user_id: int = None, year: int = None):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Build date cutoff string — supports dd-Mon-yyyy, yyyy-mm-dd, dd/mm/yyyy, dd-mm-yyyy
    from datetime import timedelta
    cutoff_dt = datetime.now() - timedelta(days=filter_days)

    base_q = """SELECT lr.doc_id, lr.date, lr.lab_name, lr.test_name, lr.measured_value,
                       lr.reference_range, lr.category,
                       COALESCE(u.full_name, u.email, 'Unknown') as patient_name
                FROM lab_reports lr
                LEFT JOIN users u ON lr.user_id = u.id"""

    if info["role"] == "admin" and view_user_id:
        c.execute(base_q + " WHERE lr.user_id=? ORDER BY lr.id DESC", (view_user_id,))
    elif info["role"] == "admin":
        c.execute(base_q + " ORDER BY lr.id DESC")
    else:
        c.execute(base_q + " WHERE lr.user_id=? ORDER BY lr.id DESC", (info["id"],))

    all_records = c.fetchall()
    conn.close()

    # Apply date filter in Python (handles multiple date formats robustly)
    def parse_date(s):
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y"):
            try: return datetime.strptime(str(s).strip(), fmt)
            except: pass
        return datetime.min

    if year:
        records = [r for r in all_records if str(parse_date(r[1]).year) == str(year)]
    elif filter_days < 36500:
        records = [r for r in all_records if parse_date(r[1]) >= cutoff_dt]
    else:
        records = all_records

    records = sorted(records, key=lambda r: parse_date(r[1]), reverse=True)

    col_meta, col_labels = [], []
    seen_doc_ids = set()
    for r in records:
        doc_id = r[0]
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        # Use doc_id as tie-breaker so two uploads on same date from same lab
        # each get their own column rather than merging into one
        lbl = f"{r[1]} ({r[2]})"
        # If this label already exists (same date+lab, different doc), make unique
        base_lbl = lbl
        suffix = 2
        while lbl in col_labels:
            lbl = f"{base_lbl} #{suffix}"
            suffix += 1
        col_labels.append(lbl)
        col_meta.append({
            "label":        lbl,
            "doc_id":       doc_id,
            "raw_date":     r[1],
            "lab_name":     r[2],
            "patient_name": r[7] if len(r) > 7 else "",
        })

    # Build doc_id → label map for O(1) lookup when populating cells
    doc_id_to_lbl = {cm["doc_id"]: cm["label"] for cm in col_meta}

    categories = {}
    test_refs = {}
    for r in records:
        doc_id = r[0]
        lbl = doc_id_to_lbl.get(doc_id)
        if not lbl:
            continue  # doc not in columns (filtered out)
        bname = canonical_test_name(r[3])
        if bname not in test_refs:
            test_refs[bname] = r[5]
        tkey = f"{bname} (Ref: {test_refs[bname]})"
        # Always re-derive category via infer_panel so old/inconsistent DB values
        # (e.g. "THYROID PROFILE", "HIGH", "NORMAL") all map to the canonical name
        cat  = infer_panel(r[3], r[6] or "")
        if cat not in categories:
            categories[cat] = {}
        if tkey not in categories[cat]:
            categories[cat][tkey] = {col: "-" for col in col_labels}
        categories[cat][tkey][lbl] = r[4]

    # ── Sort categories into canonical display order ──────────────────────────
    PANEL_ORDER = [
        "Complete Blood Count",
        "Diabetes",
        "Thyroid Profile",
        "Kidney Function Test",
        "Liver Function Test",
        "Lipid Profile",
        "Iron Studies",
        "Vitamins",
        "Electrolytes",
        "Inflammatory Markers",
        "Cardiac Markers",
        "Hormones",
        "Coagulation",
        "Infection / Serology",
        "Urine Routine",
    ]
    def _panel_sort_key(panel_name):
        try:
            return PANEL_ORDER.index(panel_name)
        except ValueError:
            return len(PANEL_ORDER)  # unknown panels go after Urine Routine

    ordered_categories = dict(
        sorted(categories.items(), key=lambda kv: _panel_sort_key(kv[0]))
    )
    return {"column_meta": col_meta, "categories": ordered_categories}


# ── ROUTES: PRESCRIPTIONS ─────────────────────────────────────────────────────

@app.get("/api/prescriptions")
async def get_prescriptions(request: Request, view_user_id: int = None, filter_days: int = 36500, year: int = None):
    info = get_user_info(request)
    from datetime import timedelta
    cutoff_dt = datetime.now() - timedelta(days=filter_days)

    def parse_d(s):
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try: return datetime.strptime(str(s).strip(), fmt)
            except: pass
        return datetime.min

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if info["role"] == "admin" and view_user_id:
        c.execute("""SELECT p.id, p.date, p.doctor, p.data, p.doc_id,
                            COALESCE(u.full_name, u.email) as pname
                     FROM prescriptions p LEFT JOIN users u ON p.user_id=u.id
                     WHERE p.user_id=? ORDER BY p.id DESC""", (view_user_id,))
    elif info["role"] == "admin":
        c.execute("""SELECT p.id, p.date, p.doctor, p.data, p.doc_id,
                            COALESCE(u.full_name, u.email) as pname
                     FROM prescriptions p LEFT JOIN users u ON p.user_id=u.id
                     ORDER BY p.id DESC""")
    else:
        c.execute("SELECT id, date, doctor, data, doc_id, NULL FROM prescriptions WHERE user_id=? ORDER BY id DESC",
                  (info["id"],))

    result = []
    for r in c.fetchall():
        try:
            d = parse_d(r[1])
            if year and d.year != year:
                continue
            if not year and filter_days < 36500 and d < cutoff_dt:
                continue
            result.append({"id": r[0], "date": r[1], "doctor": r[2],
                           "data": json.loads(r[3]), "doc_id": r[4], "patient_name": r[5]})
        except Exception:
            pass
    conn.close()
    result.sort(key=lambda x: parse_d(x["date"]), reverse=True)
    return result


# ── ROUTES: USER MANAGEMENT ───────────────────────────────────────────────────

@app.get("/api/users")
def list_users(request: Request):
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, email, role, full_name FROM users ORDER BY id")
    users = [{"id": r[0], "email": r[1], "role": r[2], "full_name": r[3]} for r in c.fetchall()]
    conn.close()
    return users


@app.post("/api/users")
async def create_user(request: Request, full_name: str = Form(...), email: str = Form(...),
                      password: str = Form(...), role: str = Form("user")):
    require_admin(request)
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password too short")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (email, password, role, full_name) VALUES (?,?,?,?)",
                  (email, _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode(), role, full_name))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Email already exists")
    conn.close()
    return {"message": f"User {full_name} created"}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, request: Request):
    """Admin: update a user's full_name and/or email."""
    require_admin(request)
    body = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, email FROM users WHERE id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    old_name = row[0] or ""
    new_name = body.get("full_name", old_name).strip()
    new_email = body.get("email", row[1]).strip()
    c.execute("UPDATE users SET full_name=?, email=? WHERE id=?",
              (new_name, new_email, user_id))
    conn.commit(); conn.close()
    return {"ok": True, "old_name": old_name, "new_name": new_name}


@app.post("/api/admin/rename-user-files")
async def rename_user_files(request: Request):
    """Admin: rename upload folders when a user's name changes."""
    require_admin(request)
    body = await request.json()
    old_name = (body.get("old_name") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    if not old_name or not new_name or old_name == new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name required and must differ")

    import re as _re
    def _safe(n): return _re.sub(r"[^a-zA-Z0-9]", "_", n.strip().lower())

    old_dir = os.path.join(UPLOAD_BASE, _safe(old_name))
    new_dir = os.path.join(UPLOAD_BASE, _safe(new_name))

    renamed_files = 0
    if os.path.exists(old_dir):
        if os.path.exists(new_dir):
            raise HTTPException(status_code=409,
                detail=f"Target folder already exists: {new_dir}")
        import shutil
        shutil.move(old_dir, new_dir)
        print(f"[rename-files] {old_dir} → {new_dir}")

    # Update file_path in documents table
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    old_slug = _safe(old_name)
    new_slug = _safe(new_name)
    c.execute("SELECT id, file_path FROM documents WHERE file_path LIKE ?",
              (f"%/{old_slug}/%",))
    docs = c.fetchall()
    for doc_id, fp in docs:
        if fp:
            new_fp = fp.replace(f"/{old_slug}/", f"/{new_slug}/")
            c.execute("UPDATE documents SET file_path=? WHERE id=?", (new_fp, doc_id))
            renamed_files += 1
    # Same for investigations
    c.execute("SELECT id, file_path FROM investigations WHERE file_path LIKE ?",
              (f"%/{old_slug}/%",))
    for inv_id, fp in c.fetchall():
        if fp:
            new_fp = fp.replace(f"/{old_slug}/", f"/{new_slug}/")
            c.execute("UPDATE investigations SET file_path=? WHERE id=?", (new_fp, inv_id))
            renamed_files += 1
    conn.commit(); conn.close()
    return {"ok": True, "renamed_files": renamed_files,
            "old_dir": old_dir, "new_dir": new_dir}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    info = require_admin(request)
    if user_id == info["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "User deleted"}


@app.post("/api/users/{user_id}/reset-password")
async def reset_password(user_id: int, request: Request, new_password: str = Form(...)):
    require_admin(request)
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET password=?, must_change_password=0 WHERE id=?",
              (_bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode(), user_id))
    conn.commit()
    conn.close()
    print(f"[admin] Password reset for user_id={user_id}")
    return {"message": "Password reset successfully"}


@app.post("/api/reassign")
async def reassign_report(request: Request, record_type: str = Form(...),
                          record_id: int = Form(...), new_user_id: int = Form(...)):
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if record_type == "prescription":
        c.execute("UPDATE prescriptions SET user_id=? WHERE id=?", (new_user_id, record_id))
        c.execute("SELECT doc_id FROM prescriptions WHERE id=?", (record_id,))
        row = c.fetchone()
        if row and row[0]:
            c.execute("UPDATE documents SET user_id=? WHERE id=?", (new_user_id, row[0]))
    elif record_type == "lab":
        c.execute("UPDATE lab_reports SET user_id=? WHERE doc_id=?", (new_user_id, record_id))
        c.execute("UPDATE documents SET user_id=? WHERE id=?", (new_user_id, record_id))
    conn.commit()
    conn.close()
    return {"message": "Reassigned"}


# ── ROUTES: GEMINI USAGE ──────────────────────────────────────────────────────

@app.get("/api/gemini-usage")
def gemini_usage(request: Request):
    require_admin(request)
    try:
        from gemini_utils import get_usage_summary
        return get_usage_summary()
    except Exception as e:
        return {"error": str(e), "keys": [], "total_requests_today": 0,
                "daily_limit_per_key": 1500, "history": []}


# ── ROUTES: PAGE MERGER ──────────────────────────────────────────────────────

@app.post("/api/merge-pages")
async def merge_pages(request: Request, files: list[UploadFile] = File(...)):
    """Merge multiple image/PDF pages into a single scanned PDF."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    import numpy as np

    pdf_buf = io.BytesIO()
    c = rl_canvas.Canvas(pdf_buf, pagesize=A4)
    PAGE_W, PAGE_H = A4
    MARGIN = 24

    for uf in files:
        raw = await uf.read()
        mime = uf.content_type or ""

        if "pdf" in mime or uf.filename.lower().endswith(".pdf"):
            # Extract pages from existing PDF and re-embed
            import base64
            try:
                from pdf2image import convert_from_bytes
                images = convert_from_bytes(raw, dpi=150)
                for pil_img in images:
                    if pil_img.mode != "RGB":
                        pil_img = pil_img.convert("RGB")
                    ibuf = io.BytesIO()
                    pil_img.save(ibuf, format="PNG")
                    ibuf.seek(0)
                    iw, ih = pil_img.size
                    avail_w, avail_h = PAGE_W - 2*MARGIN, PAGE_H - 2*MARGIN
                    scale = min(avail_w/iw, avail_h/ih)
                    dw, dh = iw*scale, ih*scale
                    x = MARGIN + (avail_w - dw)/2
                    y = MARGIN + (avail_h - dh)/2
                    c.drawImage(ImageReader(ibuf), x, y, dw, dh, preserveAspectRatio=True)
                    c.showPage()
            except Exception:
                # fallback: embed PDF pages as-is via pikepdf
                try:
                    import pikepdf
                    from pikepdf import Pdf as _Pdf
                    src = _Pdf.open(io.BytesIO(raw))
                    for page in src.pages:
                        tmp = io.BytesIO()
                        out = _Pdf.new()
                        out.pages.append(page)
                        out.save(tmp)
                        tmp.seek(0)
                        images2 = convert_from_bytes(tmp.read(), dpi=150)
                        for img2 in images2:
                            ibuf2 = io.BytesIO()
                            img2.save(ibuf2, format="PNG")
                            ibuf2.seek(0)
                            c.drawImage(ImageReader(ibuf2), MARGIN, MARGIN, PAGE_W-2*MARGIN, PAGE_H-2*MARGIN)
                            c.showPage()
                except Exception as e2:
                    print(f"[merge] PDF fallback failed: {e2}")
        else:
            # Image — run through scan pipeline
            try:
                scanned_pdf = make_scanned_pdf(raw)
                # Extract that single page as image to re-embed
                from pdf2image import convert_from_bytes as _cfb
                imgs = _cfb(scanned_pdf, dpi=150)
                for img in imgs:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    ibuf = io.BytesIO()
                    img.save(ibuf, "PNG")
                    ibuf.seek(0)
                    iw, ih = img.size
                    avail_w, avail_h = PAGE_W - 2*MARGIN, PAGE_H - 2*MARGIN
                    scale = min(avail_w/iw, avail_h/ih)
                    dw, dh = iw*scale, ih*scale
                    x = MARGIN + (avail_w - dw)/2
                    y = MARGIN + (avail_h - dh)/2
                    c.drawImage(ImageReader(ibuf), x, y, dw, dh, preserveAspectRatio=True)
                    c.showPage()
            except Exception as e:
                print(f"[merge] image page failed: {e}")

    c.save()
    pdf_buf.seek(0)
    from fastapi.responses import Response
    return Response(content=pdf_buf.getvalue(), media_type="application/pdf")


# ── ROUTES: INVESTIGATIONS ───────────────────────────────────────────────────

@app.get("/api/investigations")
async def get_investigations(request: Request, view_user_id: int = None, filter_days: int = 36500, year: int = None):
    info = get_user_info(request)
    from datetime import timedelta
    cutoff_dt = datetime.now() - timedelta(days=filter_days)

    def parse_d(s):
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try: return datetime.strptime(str(s).strip(), fmt)
            except: pass
        return datetime.min

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if info["role"] == "admin" and view_user_id:
        c.execute("""SELECT i.id, i.date, i.inv_type, i.summary, i.ai_analysis, i.doc_id, i.file_path,
                            COALESCE(u.full_name, u.email, 'Unknown') as patient, COALESCE(i.notes,'') as notes,
                            COALESCE(i.doctor,'') as doctor, COALESCE(i.clinic,'') as clinic
                     FROM investigations i LEFT JOIN users u ON i.user_id=u.id
                     WHERE i.user_id=? ORDER BY i.id DESC""", (view_user_id,))
    elif info["role"] == "admin":
        c.execute("""SELECT i.id, i.date, i.inv_type, i.summary, i.ai_analysis, i.doc_id, i.file_path,
                            COALESCE(u.full_name, u.email, 'Unknown') as patient, COALESCE(i.notes,'') as notes,
                            COALESCE(i.doctor,'') as doctor, COALESCE(i.clinic,'') as clinic
                     FROM investigations i LEFT JOIN users u ON i.user_id=u.id
                     ORDER BY i.id DESC""")
    else:
        c.execute("""SELECT i.id, i.date, i.inv_type, i.summary, i.ai_analysis, i.doc_id, i.file_path,
                            COALESCE(u.full_name, u.email, 'Unknown') as patient, COALESCE(i.notes,'') as notes,
                            COALESCE(i.doctor,'') as doctor, COALESCE(i.clinic,'') as clinic
                     FROM investigations i LEFT JOIN users u ON i.user_id=u.id
                     WHERE i.user_id=? ORDER BY i.id DESC""", (info["id"],))

    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        d = parse_d(r[1])
        if year and d.year != year:
            continue
        if not year and filter_days < 36500 and d < cutoff_dt:
            continue
        result.append({
            "id": r[0], "date": r[1], "inv_type": r[2],
            "summary": r[3], "ai_analysis": r[4],
            "doc_id": r[5], "file_path": r[6], "patient": r[7], "notes": r[8],
            "doctor": r[9] if len(r) > 9 else "",
            "clinic": r[10] if len(r) > 10 else "",
        })
    result.sort(key=lambda x: parse_d(x["date"]), reverse=True)
    return result


@app.post("/api/investigations/upload-multi")
async def upload_investigation_multi(
    request: Request,
    files: list[UploadFile] = File(...)
):
    """
    Accept one or more files (PDFs, images, scan photos).
    - Files sent directly to Gemini — no local OCR
    - Images → BOTH text extraction (OCR via scanned PDF) AND visual analysis by Gemini
    AI auto-detects investigation type and produces structured report + image analysis.
    """
    import io, base64
    info = get_user_info(request)
    uid = info["id"]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, email FROM users WHERE id=?", (uid,))
    urow = c.fetchone()
    uname = ((urow[0] or urow[1]) if urow else "unknown")

    # ── Step 1: Read all files, classify ──
    pdf_files   = []   # (bytes) — PDFs for text extraction
    image_files = []   # (bytes, mime) — images for BOTH OCR and visual AI

    for uf in files:
        raw   = await uf.read()
        mime  = uf.content_type or "application/octet-stream"
        fname = (uf.filename or "").lower()
        if "pdf" in mime or fname.endswith(".pdf"):
            pdf_files.append(raw)
        else:
            image_files.append((raw, mime if mime.startswith("image/") else "image/jpeg"))

    # ── Step 2: No local OCR — Gemini reads everything directly ──
    raw_text = ""  # will be filled by Gemini, not local OCR

    # ── Step 3: Merge all pages into single PDF for storage ──
    merged_pdf_bytes = None
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
        from pdf2image import convert_from_bytes
        from PIL import Image as _PIL

        pdf_buf = io.BytesIO()
        cv = rl_canvas.Canvas(pdf_buf, pagesize=A4)
        PAGE_W, PAGE_H = A4; MARGIN = 24

        def _embed_pil(pil_img):
            if pil_img.mode != "RGB": pil_img = pil_img.convert("RGB")
            ibuf = io.BytesIO(); pil_img.save(ibuf, "PNG"); ibuf.seek(0)
            iw, ih = pil_img.size
            avail_w, avail_h = PAGE_W - 2*MARGIN, PAGE_H - 2*MARGIN
            scale = min(avail_w / iw, avail_h / ih)
            dw, dh = iw * scale, ih * scale
            cv.drawImage(ImageReader(ibuf), MARGIN + (avail_w-dw)/2, MARGIN + (avail_h-dh)/2, dw, dh)
            cv.showPage()

        # Embed PDFs
        for pdf_bytes in pdf_files:
            try:
                for img in convert_from_bytes(pdf_bytes, dpi=150):
                    _embed_pil(img)
            except Exception as e:
                print(f"[inv] embed PDF page failed: {e}")

        # Embed images directly (no OpenCV processing for medical scans)
        for img_bytes, img_mime in image_files:
            try:
                pil_img = _PIL.open(io.BytesIO(img_bytes))
                _embed_pil(pil_img)
            except Exception as e:
                print(f"[inv] embed image failed: {e}")

        cv.save()
        merged_pdf_bytes = pdf_buf.getvalue()
        print(f"[inv] merged PDF: {len(merged_pdf_bytes)} bytes")
    except Exception as e:
        print(f"[inv] merge PDF failed: {e}")
        if pdf_files:
            merged_pdf_bytes = pdf_files[0]
        elif image_files:
            merged_pdf_bytes = image_files[0][0]

    # ── Step 4: Gemini OCR — read the report text ──
    inv_type             = "Other Imaging"
    report_text          = ""
    ai_analysis_combined = ""
    patient_name         = ""
    date_found           = datetime.now().strftime("%d-%m-%Y")
    owner_id             = uid

    import re as _re

    def _type_from_text(txt):
        t = txt.lower()
        for kw, label in [
            ("electrocardiog", "ECG/EKG"), ("ecg", "ECG/EKG"), ("ekg", "ECG/EKG"),
            ("ultrasound", "Ultrasound"), ("sonograph", "Ultrasound"), ("usg", "Ultrasound"),
            ("x-ray", "X-Ray"), ("xray", "X-Ray"), ("radiograph", "X-Ray"),
            ("ct scan", "CT Scan"), ("computed tomography", "CT Scan"),
            ("mri", "MRI"), ("magnetic resonance", "MRI"),
            ("echocardiog", "Echocardiogram"), ("endoscop", "Endoscopy"),
            ("biopsy", "Biopsy"), ("pet scan", "PET Scan"),
        ]:
            if kw in t: return label
        return "Other Imaging"

    def _dates_from_text(txt):
        for pat in [
            r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b',
            r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b',
            r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})[,\s]+(\d{4})\b',
        ]:
            m = _re.search(pat, txt, _re.IGNORECASE)
            if m: return normalize_date_str(m.group(0))
        return None

    print(f"[inv] pdf_files={len(pdf_files)}  image_files={len(image_files)}")

    try:
        from gemini_utils import _call_gemini_text, _file_to_parts
        from google.genai import types as _gtypes

        # Gather all content into one OCR prompt — PDFs + images
        all_parts = [_gtypes.Part.from_text(text=(
            "Read this medical investigation report carefully. "
            "Extract ALL text exactly as written: patient name, age, date, "
            "referring doctor, centre/hospital name, all findings, measurements, "
            "and the final impression/conclusion. "
            "Output plain text only — no markdown, no extra commentary."
        ))]

        for pdf_bytes in pdf_files:
            for part in _file_to_parts(pdf_bytes, "application/pdf"):
                all_parts.append(part)

        for img_bytes, img_mime in image_files[:4]:
            all_parts.append(_gtypes.Part.from_bytes(data=img_bytes, mime_type=img_mime))

        report_text = _call_gemini_text(all_parts, max_output=2000)
        print(f"[inv] OCR result: {len(report_text)} chars — {report_text[:120]!r}")

        inv_type = _type_from_text(report_text)
        d = _dates_from_text(report_text)
        if d: date_found = d; print(f"[inv] date: {d!r}")

        # Extract patient name
        m_pat = _re.search(r'(?:patient|name)\s*[:\-]\s*([A-Z][a-zA-Z .]+)', report_text, _re.IGNORECASE)
        if m_pat: patient_name = m_pat.group(1).strip()

        owner_id = match_patient_to_user(patient_name, conn) or uid
        print(f"[inv] done: type={inv_type!r} date={date_found!r} owner={owner_id} ocr_len={len(report_text)}")

    except Exception as ex:
        print(f"[inv] OCR failed: {ex}")
        import traceback; traceback.print_exc()
        report_text = ""
        inv_type = "Other Imaging"
        owner_id = uid


    # ── Step 5: Save ──
    uname_clean = re.sub(r"[^a-zA-Z0-9]", "_", (uname or "unknown").strip().lower())
    safe_type   = inv_type.replace("/", "_").replace(" ", "_")
    today_str   = datetime.now().strftime("%d-%m-%Y")
    file_path   = os.path.join(UPLOAD_BASE, uname_clean, "investigations",
                               f"{uname_clean}_{safe_type}_{today_str}.pdf")
    base_fp, ext = os.path.splitext(file_path)
    i = 1
    while os.path.exists(file_path):
        file_path = f"{base_fp}_{i}{ext}"; i += 1
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if merged_pdf_bytes:
        with open(file_path, "wb") as fh:
            fh.write(merged_pdf_bytes)

    c.execute("INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) VALUES (?,?,?,?,?,?)",
              (owner_id, report_text[:20000], datetime.now().isoformat(), file_path, "application/pdf", "investigation"))
    doc_id = c.lastrowid

    c.execute("INSERT INTO investigations (user_id, doc_id, date, inv_type, summary, ai_analysis, file_path) VALUES (?,?,?,?,?,?,?)",
              (owner_id, doc_id, date_found, inv_type, report_text, "", file_path))
    c.execute("UPDATE documents SET user_id=? WHERE id=?", (owner_id, doc_id))
    conn.commit()
    conn.close()

    return {"ok": True, "inv_type": inv_type, "patient": uname,
            "summary": report_text[:200], "date": date_found}


@app.post("/api/investigations/analyse-smart")
async def analyse_investigation_smart(
    request: Request,
    file: UploadFile = File(...),
    patient_hint: str = Form(""),
    sections: str = Form("doctor,patient,type,findings,impression,values,abnormal,summary,advice"),
):
    """
    Browser-triggered investigation analysis.
    Runs full Gemini structured analysis, returns clean JSON result.
    Does NOT save to DB — caller decides whether to save.
    """
    info = get_user_info(request)
    file_bytes = await file.read()
    mime       = file.content_type or "application/pdf"
    section_list = [s.strip() for s in sections.split(",") if s.strip()]

    SECTION_PROMPTS = {
        "doctor":      "Doctor / Facility: Name of the referring and reporting doctor, hospital or diagnostic centre name and address",
        "patient":     "Patient Details: Full name, age, gender, patient ID or MR number if visible",
        "type":        "Investigation Type: Type of test (e.g. Ultrasound Abdomen, X-Ray Chest PA, CBC, MRI Brain, ECG, Endoscopy)",
        "findings":    "Key Findings: All findings from the report, organised by organ or system. Include measurements and observations verbatim where important",
        "impression":  "Impression / Diagnosis: The radiologist or pathologist final impression, conclusion or diagnosis",
        "values":      "All Test Values: Every numerical value with its unit and reference range, formatted as a clean list",
        "abnormal":    "Abnormal Results: Only values or findings outside normal range or flagged as abnormal, with brief clinical significance",
        "summary":     "Plain-English Summary: 3-5 sentences a non-medical family member can understand, avoiding jargon. Note any urgent findings",
        "advice":      "Suggested Follow-up: Recommended next steps, follow-up tests, or specialist referrals based on these findings",
    }

    numbered = "\n".join(
        f"{i+1}. {SECTION_PROMPTS.get(s, s.title())}"
        for i, s in enumerate(section_list)
    )
    patient_ctx = f"\nPatient context: {patient_hint}" if patient_hint.strip() else ""

    prompt = f"""You are a medical report analyst. Analyse this investigation/diagnostic report carefully.{patient_ctx}

Provide a structured response with EXACTLY these sections in this order:

{numbered}

Format rules:
- Use the exact section heading above for each section
- If a section has no relevant information, write "Not available"
- For All Test Values use format: Test Name: value unit (ref: range)
- Flag critical findings with WARNING
- Keep Plain-English Summary jargon-free for a family member
- Do not add extra sections"""

    try:
        from gemini_utils import _call_gemini_text, _file_to_parts
        from google.genai import types as _gtypes

        parts = [_gtypes.Part.from_text(text=prompt)]
        for p in _file_to_parts(file_bytes, mime):
            parts.append(p)

        raw = _call_gemini_text(parts, max_output=4096)
        if not raw:
            raise ValueError("Empty response from Gemini")

        # Parse numbered sections into dict
        result_sections = {}
        current_key = None
        current_lines = []

        for line in raw.split("\n"):
            matched = False
            for i, sec in enumerate(section_list):
                heading = SECTION_PROMPTS.get(sec, sec.title()).split(":")[0]
                stripped = line.strip()
                if stripped.startswith(f"{i+1}.") or (heading.lower() in line.lower() and (":" in line)):
                    if current_key:
                        result_sections[current_key] = "\n".join(current_lines).strip()
                    current_key = sec
                    after = line.split(":", 1)[1].strip() if ":" in line else ""
                    current_lines = [after] if after else []
                    matched = True
                    break
            if not matched and current_key:
                current_lines.append(line)

        if current_key:
            result_sections[current_key] = "\n".join(current_lines).strip()

        if not result_sections:
            result_sections = {"summary": raw}

        print(f"[inv-smart] analysis done: {len(result_sections)} sections, {len(raw)} chars")
        return {"ok": True, "sections": result_sections, "section_order": section_list, "raw": raw}

    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, f"Analysis failed: {e}")


@app.post("/api/investigations/save-analysis")
async def save_investigation_analysis(request: Request):
    """Save a browser-analysed investigation result to the DB."""
    info = get_user_info(request)
    body = await request.json()

    inv_type    = body.get("inv_type", "Other Imaging")
    summary     = body.get("summary", "")
    ai_analysis = body.get("raw", "")
    date_str    = body.get("date", datetime.now().strftime("%d-%m-%Y"))
    file_b64    = body.get("file_b64", "")
    mime        = body.get("mime", "application/pdf")
    patient_name= body.get("patient_name", "")
    uid         = info["id"]

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    # Honour explicit user_id if provided (browser AI sends this directly)
    explicit_uid = body.get('user_id')
    if explicit_uid:
        owner_id = int(explicit_uid)
    else:
        owner_id = match_patient_to_user(patient_name, conn) or uid
    c.execute("SELECT full_name, email FROM users WHERE id=?", (owner_id,))
    urow  = c.fetchone()
    uname = ((urow[0] or urow[1]) if urow else "unknown")
    uname_clean = re.sub(r"[^a-zA-Z0-9]", "_", uname.strip().lower())

    file_path = os.path.join(UPLOAD_BASE, uname_clean, "investigations",
                             f"{uname_clean}_{inv_type.replace(' ','_')}_{date_str}.pdf")
    base_fp, ext = os.path.splitext(file_path)
    idx = 1
    while os.path.exists(file_path):
        file_path = f"{base_fp}_{idx}{ext}"; idx += 1
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if file_b64:
        import base64 as _b64
        try:
            with open(file_path, "wb") as fh:
                fh.write(_b64.b64decode(file_b64))
        except Exception:
            pass

    c.execute("INSERT INTO documents (user_id,raw_text,upload_date,file_path,mime_type,doc_type) VALUES (?,?,?,?,?,?)",
              (owner_id, summary[:20000], datetime.now().isoformat(), file_path, "application/pdf", "investigation"))
    doc_id = c.lastrowid
    c.execute("INSERT INTO investigations (user_id,doc_id,date,inv_type,summary,ai_analysis,file_path) VALUES (?,?,?,?,?,?,?)",
              (owner_id, doc_id, date_str, inv_type, summary, ai_analysis, file_path))
    conn.commit(); conn.close()

    print(f"[inv-save] saved doc_id={doc_id} type={inv_type!r} owner={owner_id}")
    return {"ok": True, "doc_id": doc_id, "inv_type": inv_type, "message": f"Saved {inv_type} for {uname}"}


@app.post("/api/investigations/upload")
async def upload_investigation(
    request: Request,
    inv_type: str = Form(...),
    file: UploadFile = File(...),
    image_file: UploadFile = File(None)   # optional photo (X-ray image, ultrasound photo)
):
    info = get_user_info(request)
    uid = info["id"]
    import io

    file_bytes = await file.read()
    mime = file.content_type or "application/pdf"

    # Convert image report to scanned PDF
    if mime.startswith("image/"):
        try:
            file_bytes = make_scanned_pdf(file_bytes)
        except Exception:
            pass
        mime = "application/pdf"

    raw_text = ""  # no local OCR

    # Save file
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, email FROM users WHERE id=?", (uid,))
    urow = c.fetchone()
    uname = re.sub(r"[^a-zA-Z0-9]", "_", ((urow[0] or urow[1]) if urow else "unknown").strip().lower())
    today = datetime.now().strftime("%d-%m-%Y")
    safe_type = inv_type.replace("/", "_").replace(" ", "_")
    file_path = os.path.join(UPLOAD_BASE, uname, "investigations",
                             f"{uname}_{safe_type}_{today}.pdf")
    # handle collisions
    base, ext = os.path.splitext(file_path)
    i = 1
    while os.path.exists(file_path):
        file_path = f"{base}_{i}{ext}"; i += 1
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as fh:
        fh.write(file_bytes)

    c.execute("INSERT INTO documents (user_id, raw_text, upload_date, file_path, mime_type, doc_type) VALUES (?,?,?,?,?,?)",
              (uid, "", datetime.now().isoformat(), file_path, "application/pdf", "investigation"))
    doc_id = c.lastrowid
    conn.commit()

    # ── AI OCR: read report text ──
    summary = ""
    ai_analysis = ""
    date_found = today
    owner_id = uid

    # Optional image (ultrasound photo, X-ray scan)
    image_bytes = None
    image_mime  = None
    if image_file and image_file.filename:
        image_bytes = await image_file.read()
        image_mime  = image_file.content_type or "image/jpeg"

    try:
        import re as _re2
        from gemini_utils import _call_gemini_text, _file_to_parts
        from google.genai import types as _gtypes

        all_parts = [_gtypes.Part.from_text(text=(
            "Read this medical investigation report carefully. "
            "Extract ALL text exactly as written: patient name, age, date, "
            "referring doctor, centre/hospital name, all findings, measurements, "
            "and the final impression/conclusion. "
            "Output plain text only — no markdown, no extra commentary."
        ))]
        for part in _file_to_parts(file_bytes, "application/pdf"):
            all_parts.append(part)
        if image_bytes and image_mime:
            all_parts.append(_gtypes.Part.from_bytes(data=image_bytes, mime_type=image_mime))

        ocr_text = _call_gemini_text(all_parts, max_output=2000)
        summary = ocr_text

        # Extract date
        for pat in [r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b',
                    r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b']:
            m = _re2.search(pat, ocr_text, _re2.IGNORECASE)
            if m: date_found = normalize_date_str(m.group(0)); break

        # Extract patient name and auto-tag
        m_p = _re2.search(r'(?:patient|name)\s*[:\-]\s*([A-Z][a-zA-Z .]+)', ocr_text, _re2.IGNORECASE)
        patient_name = m_p.group(1).strip() if m_p else ""
        owner_id = match_patient_to_user(patient_name, conn) or uid
        print(f"[inv2] OCR done: {len(ocr_text)} chars, owner={owner_id}")
    except Exception as ex:
        print(f"[inv2] OCR failed: {ex}")
        import traceback; traceback.print_exc()

    c.execute("""INSERT INTO investigations (user_id, doc_id, date, inv_type, summary, ai_analysis, file_path)
                 VALUES (?,?,?,?,?,?,?)""",
              (owner_id, doc_id, date_found, inv_type, summary, ai_analysis, file_path))
    c.execute("UPDATE documents SET user_id=? WHERE id=?", (owner_id, doc_id))
    conn.commit()
    conn.close()
    return {"ok": True, "summary": summary, "ai_analysis": ai_analysis,
            "date": date_found, "inv_type": inv_type}




@app.patch("/api/investigations/{inv_id}/meta")
async def update_investigation_meta(inv_id: int, request: Request):
    """Update doctor and clinic fields on an investigation."""
    info = get_user_info(request)
    body = await request.json()
    doctor = body.get("doctor", "")
    clinic = body.get("clinic", "")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if info["role"] == "admin":
        c.execute("UPDATE investigations SET doctor=?, clinic=? WHERE id=?", (doctor, clinic, inv_id))
    else:
        c.execute("UPDATE investigations SET doctor=?, clinic=? WHERE id=? AND user_id=?",
                  (doctor, clinic, inv_id, info["id"]))
    conn.commit(); conn.close()
    return {"ok": True}

@app.patch("/api/investigations/{inv_id}/notes")
async def update_investigation_notes(inv_id: int, request: Request):
    info = get_user_info(request)
    body = await request.json()
    notes = body.get("notes", "")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if info["role"] == "admin":
        c.execute("UPDATE investigations SET notes=? WHERE id=?", (notes, inv_id))
    else:
        c.execute("UPDATE investigations SET notes=? WHERE id=? AND user_id=?",
                  (notes, inv_id, info["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/investigations/{inv_id}")
async def delete_investigation(inv_id: int, request: Request):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if info["role"] == "admin":
        c.execute("SELECT file_path FROM investigations WHERE id=?", (inv_id,))
    else:
        c.execute("SELECT file_path FROM investigations WHERE id=? AND user_id=?", (inv_id, info["id"]))
    row = c.fetchone()
    if row:
        if row[0] and os.path.exists(row[0]):
            try: os.remove(row[0])
            except: pass
        c.execute("DELETE FROM investigations WHERE id=?", (inv_id,))
        conn.commit()
    conn.close()
    return {"ok": True}


# ── ROUTES: AI CHAT ───────────────────────────────────────────────────────────

@app.post("/chat")
async def chat_query(request: Request, query: str = Form(...)):
    info = get_user_info(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Admins get all users' data with patient names; regular users get their own
    if info["role"] == "admin":
        c.execute("""
            SELECT lr.date, lr.test_name, lr.measured_value, lr.reference_range,
                   lr.category, COALESCE(u.full_name, u.email, 'Unknown') as patient
            FROM lab_reports lr
            LEFT JOIN users u ON lr.user_id = u.id
            ORDER BY patient, lr.test_name, lr.date DESC
        """)
    else:
        c.execute("""
            SELECT lr.date, lr.test_name, lr.measured_value, lr.reference_range,
                   lr.category, COALESCE(u.full_name, u.email, 'Unknown') as patient
            FROM lab_reports lr
            LEFT JOIN users u ON lr.user_id = u.id
            WHERE lr.user_id = ?
            ORDER BY lr.test_name, lr.date DESC
        """, (info["id"],))
    lab_rows = c.fetchall()

    if info["role"] == "admin":
        c.execute("""
            SELECT p.date, p.doctor, p.data,
                   COALESCE(u.full_name, u.email, 'Unknown') as patient
            FROM prescriptions p
            LEFT JOIN users u ON p.user_id = u.id
            ORDER BY patient, p.date DESC
        """)
    else:
        c.execute("""
            SELECT p.date, p.doctor, p.data,
                   COALESCE(u.full_name, u.email, 'Unknown') as patient
            FROM prescriptions p
            LEFT JOIN users u ON p.user_id = u.id
            WHERE p.user_id = ?
            ORDER BY p.date DESC
        """, (info["id"],))
    rx_rows = c.fetchall()
    # NOTE: conn stays open — investigations query below also needs it

    # Identify the asking user FIRST — needed for context sorting below
    asking_user = info.get("full_name") or info.get("email") or "the user"

    # Build structured plain-text context (much more readable for LLM than raw JSON)
    # ── DEBUG: print raw lab rows so we can see what's fetched ──
    print(f"[chat] lab_rows count: {len(lab_rows)}")
    for r in lab_rows[:10]:
        print(f"  row: date={r[0]} test={r[1]!r} val={r[2]!r} patient={r[5]!r}")

    lines = []

    # Group labs by patient → canonical_test_name, deduplicate, sort by parsed date
    from collections import defaultdict
    from datetime import datetime as _dt

    def _parse_lab_date(s):
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try: return _dt.strptime(str(s).strip(), fmt)
            except: pass
        return _dt.min

    labs_by_patient = defaultdict(lambda: defaultdict(list))
    seen_lab = set()
    for date, test, val, ref, cat, patient in lab_rows:
        canon = canonical_test_name(test)   # normalise aliases before grouping
        key = (patient, canon, date, str(val))
        if key in seen_lab:
            continue
        seen_lab.add(key)
        labs_by_patient[patient][canon].append({"date": date, "value": val, "ref": ref, "category": cat})

    # Build compact context — every row on one short line to fit 400+ tests in <20K chars
    # Format: "TEST | DATE | VALUE | REF | FLAG" — AI understands this fine
    lines.append("=== LAB RESULTS ===")
    # Put the asking user's data FIRST so it's never truncated
    asking_lower = asking_user.lower()
    sorted_patients = sorted(labs_by_patient.keys(),
        key=lambda p: (0 if asking_lower in p.lower() else 1, p))
    for patient in sorted_patients:
        tests = labs_by_patient[patient]
        lines.append(f"\n--- Patient: {patient} ---")
        for test, entries in sorted(tests.items()):
            entries_sorted = sorted(entries, key=lambda e: _parse_lab_date(e["date"]), reverse=True)
            for e in entries_sorted:
                flag = ""
                try:
                    v = float(str(e["value"]).split()[0].replace(",",""))
                    parts = str(e["ref"]).replace(" ","").split("-")
                    if len(parts) == 2:
                        lo, hi = float(parts[0]), float(parts[1])
                        if v > hi: flag = " HIGH"
                        elif v < lo: flag = " LOW"
                except: pass
                lines.append(f"  {test}|{e['date']}|{e['value']}|ref:{e['ref']}{flag}")

    lines.append("\n=== PRESCRIPTIONS ===")
    for date, doctor, data_json, patient in rx_rows:
        lines.append(f"\n--- Patient: {patient} ---  Date: {date}  Doctor: {doctor}")
        try:
            d = json.loads(data_json) if isinstance(data_json, str) else data_json
            if d.get("Diagnosis"): lines.append(f"  Diagnosis: {d['Diagnosis']}")
            meds = d.get("Medicines", [])
            for m in meds:
                lines.append(f"  Rx: {m.get('Name','')} {m.get('Dosage','')} {m.get('Frequency','')}".strip())
        except: pass

    # Investigations
    if info["role"] == "admin":
        c.execute("""SELECT i.date, i.inv_type, i.summary, i.ai_analysis,
                            COALESCE(u.full_name, u.email, 'Unknown') as patient
                     FROM investigations i LEFT JOIN users u ON i.user_id=u.id
                     ORDER BY patient, i.date DESC""")
    else:
        c.execute("""SELECT i.date, i.inv_type, i.summary, i.ai_analysis,
                            COALESCE(u.full_name, u.email, 'Unknown') as patient
                     FROM investigations i LEFT JOIN users u ON i.user_id=u.id
                     WHERE i.user_id=? ORDER BY i.date DESC""", (info["id"],))
    inv_rows = c.fetchall()
    conn.close()  # safe to close now — all queries done
    if inv_rows:
        lines.append("\n=== INVESTIGATIONS (X-Ray / Ultrasound / ECG etc.) ===")
        for date, inv_type, summary, ai_analysis, patient in inv_rows:
            lines.append(f"\n--- Patient: {patient} ---  Date: {date}  Type: {inv_type}")
            if summary:   lines.append(f"  Findings: {summary}")
            if ai_analysis: lines.append(f"  AI Analysis: {ai_analysis[:300]}")

    context = "\n".join(lines)
    print(f"[chat] context length: {len(context)}")
    print(f"[chat] context[:800]:\n{context[:800]}")
    return {"response": process_health_query(context, query, asking_user)}


# ── Google Sheets sync router (add-on, no changes to existing code) ──────────
try:
    from sheets_router import sheets_router
    app.include_router(sheets_router)
    print("[startup] Google Sheets sync enabled")
except ImportError:
    print("[startup] sheets_router not found — Google Sheets sync disabled (optional feature)")