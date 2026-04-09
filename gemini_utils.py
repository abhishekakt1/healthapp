import os, io, re, json, time
from PIL import Image
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# API KEY ROTATION
# Keys are loaded lazily: env vars take priority, then DB config table.
# This allows zero-config startup — keys added via Admin UI after first boot.
# ---------------------------------------------------------------------------
import json as _json
_key_index = 0
_USAGE_FILE = "/data/gemini_usage.json"
_DB_PATH    = "/data/health_records.db"

def _load_api_keys() -> list[str]:
    """Load Gemini API keys: env vars first, then DB config table."""
    # 1. Env var (comma-separated for multiple keys)
    raw = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            print(f"[gemini] loaded {len(keys)} key(s) from environment")
            return keys
    # 2. DB config table
    try:
        import sqlite3 as _sq
        conn = _sq.connect(_DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key='gemini_keys'")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            keys = [k.strip() for k in row[0].split(",") if k.strip()]
            if keys:
                print(f"[gemini] loaded {len(keys)} key(s) from DB config")
                return keys
    except Exception as e:
        print(f"[gemini] DB key load skipped: {e}")
    return []

# Module-level key list — reloaded on demand via reload_keys()
API_KEYS: list[str] = _load_api_keys()

def reload_keys():
    """Re-read keys from env/DB — called after Admin UI saves new keys."""
    global API_KEYS, _key_index
    API_KEYS = _load_api_keys()
    _key_index = 0
    print(f"[gemini] keys reloaded: {len(API_KEYS)} key(s)")

def has_keys() -> bool:
    """Return True if at least one API key is configured."""
    if not API_KEYS:
        reload_keys()
    return bool(API_KEYS)

def _load_usage():
    try:
        with open(_USAGE_FILE) as f:
            return _json.load(f)
    except:
        return {}

def _save_usage(data):
    try:
        with open(_USAGE_FILE, "w") as f:
            _json.dump(data, f)
    except Exception as e:
        print(f"[gemini] usage save error: {e}")

def _track_usage(key_index, model, input_chars, output_chars):
    import datetime
    today = datetime.date.today().isoformat()
    data = _load_usage()
    key_id = f"key_{key_index}"
    if today not in data:
        data[today] = {}
    if key_id not in data[today]:
        data[today][key_id] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "models": {}}
    data[today][key_id]["requests"] += 1
    data[today][key_id]["input_tokens"] += input_chars // 4
    data[today][key_id]["output_tokens"] += output_chars // 4
    m = data[today][key_id]["models"]
    m[model] = m.get(model, 0) + 1
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    data = {k: v for k, v in data.items() if k >= cutoff}
    _save_usage(data)

def get_usage_summary():
    import datetime
    data = _load_usage()
    today = datetime.date.today().isoformat()
    FREE_TIER_RPD = 1500
    summary = {"today": today, "keys": [], "total_requests_today": 0,
                "daily_limit_per_key": FREE_TIER_RPD, "history": []}
    today_data = data.get(today, {})
    keys_snapshot = list(API_KEYS) if API_KEYS else []
    for i in range(len(keys_snapshot)):
        key_id = f"key_{i}"
        kd = today_data.get(key_id, {})
        reqs = kd.get("requests", 0)
        pct = round(reqs / FREE_TIER_RPD * 100, 1)
        summary["keys"].append({
            "key_index": i,
            "label": f"Key {i+1} (...{API_KEYS[i][-6:]})" if i < len(API_KEYS) else f"Key {i+1}",
            "requests_today": reqs,
            "input_tokens": kd.get("input_tokens", 0),
            "output_tokens": kd.get("output_tokens", 0),
            "percent_used": pct,
            "status": "critical" if pct > 80 else "warning" if pct > 50 else "ok",
            "models": kd.get("models", {})
        })
        summary["total_requests_today"] += reqs
    for i in range(7):
        day = (datetime.date.today() - datetime.timedelta(days=6-i)).isoformat()
        day_total = sum(v.get("requests", 0) for v in data.get(day, {}).values())
        summary["history"].append({"date": day, "requests": day_total})
    return summary



def _get_client():
    if not API_KEYS:
        reload_keys()
    if not API_KEYS:
        raise RuntimeError("No Gemini API key configured. Add one in Admin → ⚙️ API Keys.")
    return genai.Client(api_key=API_KEYS[_key_index % len(API_KEYS)])

def _rotate_key():
    global _key_index
    if not API_KEYS:
        reload_keys()
    if API_KEYS:
        _key_index = (_key_index + 1) % len(API_KEYS)
        print(f"[gemini] rotated to key index {_key_index}")

PRIMARY_MODEL  = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-flash-lite"
MAX_IMAGE_PX   = 1280  # longest side in pixels — keeps each JPEG page under ~200KB

# ---------------------------------------------------------------------------
# SCHEMAS
# ---------------------------------------------------------------------------
prescription_schema = {
    "type": "OBJECT",
    "properties": {
        "Date":            {"type": "STRING"},
        "Doctor_Name":     {"type": "STRING"},
        "Patient_Name":    {"type": "STRING"},
        "Age_Gender":      {"type": "STRING"},
        "Weight":          {"type": "STRING"},
        "Chief_Complaint": {"type": "STRING"},
        "History":         {"type": "STRING"},
        "Diagnosis":       {"type": "STRING"},
        "Medicines": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "Name":         {"type": "STRING"},
            "Dosage":       {"type": "STRING"},
            "Frequency":    {"type": "STRING"},
            "Duration":     {"type": "STRING"},
            "Instructions": {"type": "STRING"}
        }}},
        "Investigations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "Advice":         {"type": "ARRAY", "items": {"type": "STRING"}},
        "Follow_Up":      {"type": "STRING"},
        "Notes":          {"type": "STRING"}
    },
    "required": ["Date", "Doctor_Name", "Chief_Complaint", "Diagnosis",
                 "Medicines", "Investigations", "Advice", "Follow_Up", "Notes"]
}

lab_report_schema = {
    "type": "OBJECT",
    "properties": {
        "Patient_Name": {"type": "STRING"},
        "Date":         {"type": "STRING"},
        "Lab_Name":     {"type": "STRING"},
        "Results": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "Test_Name":       {"type": "STRING"},
            "Measured_Value":  {"type": "STRING"},
            "Reference_Range": {"type": "STRING"},
            "Category":        {"type": "STRING"}
        }, "required": ["Test_Name", "Measured_Value", "Reference_Range", "Category"]}}
    },
    "required": ["Patient_Name", "Date", "Lab_Name", "Results"]
}


# ---------------------------------------------------------------------------
# JSON REPAIR
# ---------------------------------------------------------------------------
def heal_truncated_json(raw_text: str) -> dict:
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty response from Gemini")

    text = raw_text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    start = text.find('{')
    if start == -1:
        raise ValueError("No JSON object in response")
    chunk = text[start:]

    # Attempt 1: parse as-is
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        pass

    # Attempt 2: close open brackets/strings
    try:
        working = chunk
        # Close dangling string (odd number of unescaped quotes)
        unescaped_quotes = len(re.findall(r'(?<!\\)"', working))
        if unescaped_quotes % 2 == 1:
            working = working + '"'
        opens_obj = working.count('{') - working.count('}')
        opens_arr = working.count('[') - working.count(']')
        working = working + (']' * max(0, opens_arr)) + ('}' * max(0, opens_obj))
        result = json.loads(working)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 3: truncate at last complete Results entry (last "}" before a ",{" or "]}")
    try:
        # Find the last fully-closed object in the Results array
        last_good = -1
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(chunk):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 1:  # closed a top-level Results item
                    last_good = i
        if last_good > 0:
            truncated = chunk[:last_good + 1]
            opens_obj = truncated.count('{') - truncated.count('}')
            opens_arr = truncated.count('[') - truncated.count(']')
            repaired = truncated + (']' * max(0, opens_arr)) + ('}' * max(0, opens_obj))
            result = json.loads(repaired)
            if isinstance(result, dict):
                print(f"[gemini] healed truncated JSON: recovered to depth={last_good}")
                return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 4: cut at last comma (original fallback)
    try:
        last_comma = chunk.rfind(',\n')
        if last_comma == -1:
            last_comma = chunk.rfind(',')
        if last_comma > 0:
            truncated = chunk[:last_comma]
            opens_obj = truncated.count('{') - truncated.count('}')
            opens_arr = truncated.count('[') - truncated.count(']')
            repaired = truncated + (']' * max(0, opens_arr)) + ('}' * max(0, opens_obj))
            return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        pass

    raise ValueError(f"Could not parse Gemini response\nRaw: {raw_text[:200]}")


# ---------------------------------------------------------------------------
# FILE -> JPEG PARTS
#
# THE BUG THAT KILLED ALL 4 KEYS IN ONE UPLOAD:
#   Old code did:  return file_bytes, "application/pdf"
#   A 2-page scanned prescription PDF = ~5MB raw.
#   Gemini counts this as thousands of tokens per request.
#   4 keys x 2 models = 8 attempts all firing in ~2 seconds.
#   One prescription upload consumed the entire daily token quota for all keys.
#
# THE FIX:
#   Convert every PDF page to a compressed JPEG before sending.
#   Each page at 1280px JPEG = ~150KB = ~200 tokens. Nearly free.
#   2-page prescription: 5000KB raw -> 300KB as 2 JPEGs = 17x smaller.
# ---------------------------------------------------------------------------

def _to_jpeg(img: Image.Image) -> bytes:
    """Resize a PIL image to MAX_IMAGE_PX on longest side and encode as JPEG."""
    w, h = img.size
    if max(w, h) > MAX_IMAGE_PX:
        scale = MAX_IMAGE_PX / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=82)
    return buf.getvalue()


def _file_to_parts(file_bytes: bytes, mime_type: str = "") -> list:
    """Convert file to Gemini Parts.
    PDF  -> single raw PDF part (Gemini reads text natively — much better than vision on images)
    Image -> resized JPEG part (vision mode for actual scan images / handwritten docs)
    """
    if file_bytes.startswith(b'%PDF'):
        # Send raw PDF — Gemini 1.5+ natively extracts text from typed PDFs
        print(f"[gemini] PDF {len(file_bytes)//1024}KB -> raw PDF part (native text extraction)")
        return [types.Part.from_bytes(data=file_bytes, mime_type="application/pdf")]
    else:
        try:
            img = Image.open(io.BytesIO(file_bytes))
            jpeg = _to_jpeg(img)
            print(f"[gemini] image {len(file_bytes)//1024}KB -> {len(jpeg)//1024}KB JPEG")
            return [types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")]
        except Exception as e:
            print(f"[gemini] image resize failed: {e}")
            return [types.Part.from_bytes(data=file_bytes, mime_type=mime_type or "image/jpeg")]


# ---------------------------------------------------------------------------
# CORE GEMINI CALL
# ---------------------------------------------------------------------------
def _call_gemini(contents, schema, max_output: int = 2048, model_override=None) -> dict:
    """Call Gemini with key rotation and proper rate-limit backoff.

    Free tier per key: 15 requests/minute, 1500 requests/day.
    4 keys from different accounts = 60 req/min, 6000 req/day.

    - 4s sleep between key attempts keeps rate well under 15 RPM per key
    - If all 4 keys still hit (burst), wait 65s for the minute window to reset
    - Retry once after the wait before giving up on this model
    """
    models_to_try = [model_override, model_override] if model_override else [PRIMARY_MODEL, FALLBACK_MODEL]
    for model_name in models_to_try:
        # Round-robin: start from next key after last successful call
        start_key = _key_index
        for key_attempt in range(len(API_KEYS)):
            _rotate_key()  # always advance before each attempt for true round-robin
            try:
                client = _get_client()
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=max_output,
                        response_mime_type="application/json",
                        response_schema=schema
                    )
                )
                result = heal_truncated_json(response.text)
                in_sz = len(str(contents)) if isinstance(contents, str) else sum(len(str(p)) for p in (contents if isinstance(contents, list) else [contents]))
                _track_usage(_key_index, model_name, in_sz, len(response.text))
                return result
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    print(f"[gemini] {model_name} key[{_key_index}] rate/quota hit")
                    _rotate_key()
                    if key_attempt < len(API_KEYS) - 1:
                        time.sleep(4)  # spread requests, stay under 15 RPM per key
                elif "404" in err or "NOT_FOUND" in err:
                    print(f"[gemini] {model_name} not found, skipping model")
                    break
                elif "503" in err or "UNAVAILABLE" in err:
                    print(f"[gemini] {model_name} unavailable, waiting 10s")
                    time.sleep(10)
                else:
                    print(f"[gemini] {model_name} error: {err[:120]}")
                    return {"error": err}

        # All keys exhausted for this model — wait for per-minute window to reset
        print(f"[gemini] {model_name}: all keys hit, waiting 65s for rate limit reset...")
        time.sleep(65)
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=max_output,
                    response_mime_type="application/json",
                    response_schema=schema
                )
            )
            result2 = heal_truncated_json(response.text)
            in_sz = len(str(contents)) if isinstance(contents, str) else sum(len(str(p)) for p in (contents if isinstance(contents, list) else [contents]))
            _track_usage(_key_index, model_name, in_sz, len(response.text))
            return result2
        except Exception as e:
            print(f"[gemini] {model_name}: retry after wait failed: {str(e)[:80]}")

    return {"error": "quota_exhausted"}


# ---------------------------------------------------------------------------
# PRESCRIPTION — vision only, no local OCR
# ---------------------------------------------------------------------------
RX_VISION_PROMPT = """You are an expert at reading handwritten Indian doctor prescriptions. Extract EVERY piece of information visible. Be thorough — do not skip any section.

FIELD EXTRACTION RULES:

Date: Look near patient name (e.g. 3.7.25 = 03-Jul-2025)
Doctor_Name: From printed letterhead
Patient_Name: Written after "Name:"
Age_Gender: After "Age/Gender:" (e.g. 37yr / Male)
Weight: Any weight noted (e.g. 106 kg)

Chief_Complaint: Main symptoms the patient came with. Look for arrows pointing to symptoms, or c/o section. Include ALL symptoms (e.g. Gas, Bloating, Burping, Epigastric fullness).

History: Past medical history — dates with conditions (e.g. 2021 Lap. Cholecystectomy, GSD), past illnesses (e.g. Hyperthyroidism), past medications (e.g. on Cabersctin 7yrs, not on any medicine now).

Diagnosis: Look for "Imp:" or "Impression:" or "Dx:" (e.g. Imp: Dyspepsia)

Medicines: Look under "Adv" or "Rx" or right side of prescription. For EACH medicine:
  - Name: full name (T.=Tablet, Cap=Capsule)
  - Dosage: mg/ml amount
  - Frequency: expanded (BD=twice daily, OD=once daily, TDS=thrice daily, HS=at bedtime)
  - Duration: x7day, x10days etc expanded
  - Instructions: BBF=before breakfast, AF=after food

Investigations: ALL tests ordered — look in "Plan" section or left side (e.g. USG whole abdomen, LFT, Lipid profile, HbA1c, HBsAg, Anti-HCV, HIV, S.TSH/T3/T4).

Advice: Lifestyle/dietary advice (e.g. Avoid simple sugar, packaged/processed foods).

Follow_Up: When to return (look for "10days", "review after" etc)

Notes: Other observations (e.g. No diarrhoea, No constipation)

ABBREVIATIONS — always expand:
OD=once daily, BD=twice daily, TDS=thrice daily, QID=four times daily,
SOS/PRN=as needed, HS=at bedtime, BBF/AC=before breakfast, AF/PC=after food,
x7d/x7day=for 7 days, x10days=for 10 days,
T./Tab.=Tablet, Cap=Capsule, Inj=Injection, Syp=Syrup,
USG=Ultrasound, LFT=Liver Function Test, HbA1c=Glycated Haemoglobin,
TSH/T3/T4=Thyroid Profile, HBsAg=Hepatitis B Surface Antigen,
Anti-HCV=Hepatitis C Antibody, HIV=HIV Test, GSD=Gallstone Disease,
Lap. Cholecystectomy=Laparoscopic Gallbladder Removal,
Imp=Impression/Diagnosis, Adv=Medicines/Advice,
SR=Sustained Release, -D=with Domperidone

CRITICAL: Extract from BOTH left and right sides. Indian prescriptions typically have history/investigations on the LEFT and medicines on the RIGHT."""

def extract_prescription_smart(file_bytes: bytes, mime_type: str, ocr_text: str = "") -> dict:
    """Called on prescription upload.
    ocr_text parameter kept for API compatibility but ignored —
    Gemini vision is always more accurate than local OCR for handwritten prescriptions,
    and local OCR was burning quota anyway by sending garbled text that still failed."""
    print("[gemini] prescription upload -> vision")
    file_parts = _file_to_parts(file_bytes, mime_type)
    return _call_gemini([RX_VISION_PROMPT] + file_parts, prescription_schema, max_output=8192)


def extract_prescription_ai(file_bytes: bytes, mime_type: str) -> dict:
    """Called by the AI Decode button on existing prescriptions."""
    print("[gemini] AI Decode -> vision")
    file_parts = _file_to_parts(file_bytes, mime_type)
    return _call_gemini([RX_VISION_PROMPT] + file_parts, prescription_schema, max_output=8192)


# ---------------------------------------------------------------------------
# DOCUMENT TYPE DETECTION  
# ---------------------------------------------------------------------------
def _call_gemini_text(contents: list, max_output: int = 1000) -> str:
    """Call Gemini with full key rotation + retry, returning plain text (no JSON schema).
    Used for investigation visual analysis and findings summary."""
    for model_name in [PRIMARY_MODEL, FALLBACK_MODEL]:
        for key_attempt in range(len(API_KEYS)):
            _rotate_key()
            try:
                client = _get_client()
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=max_output,
                    )
                )
                txt = (response.text or "").strip()
                in_sz = sum(len(str(p)) for p in contents)
                _track_usage(_key_index, model_name, in_sz, len(txt))
                return txt
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    print(f"[gemini] {model_name} key[{_key_index}] rate/quota hit — rotating")
                    if key_attempt < len(API_KEYS) - 1:
                        time.sleep(4)
                elif "404" in err or "NOT_FOUND" in err:
                    print(f"[gemini] {model_name} not found, trying fallback")
                    break
                else:
                    print(f"[gemini] text call error: {err[:120]}")
                    return ""
        print(f"[gemini] {model_name}: all keys exhausted, waiting 65s...")
        time.sleep(65)
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(max_output_tokens=max_output)
            )
            txt = (response.text or "").strip()
            _track_usage(_key_index, model_name, 0, len(txt))
            return txt
        except Exception as e:
            print(f"[gemini] text call retry failed: {str(e)[:80]}")
    return ""


def detect_doc_type_ai(file_bytes: bytes, mime_type: str) -> str:
    """Quick AI check to determine document type. Returns: lab | prescription | investigation | unknown."""
    file_parts = _file_to_parts(file_bytes, mime_type)
    prompt = (
        "Look at this medical document. Reply with EXACTLY one word:\n"
        "- 'lab' if it is a blood test / pathology / lab report with numeric values and reference ranges\n"
        "- 'prescription' if it is a doctor prescription / medication list / Rx\n"
        "- 'investigation' if it is an imaging report (ultrasound, X-ray, ECG, MRI, CT scan, endoscopy)\n"
        "- 'unknown' if none of the above\n"
        "Reply with only the single word, nothing else."
    )
    try:
        _rotate_key()
        client = _get_client()
        resp = client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=[prompt] + file_parts,
            config=types.GenerateContentConfig(max_output_tokens=5)
        )
        word = resp.text.strip().lower().split()[0] if resp.text else "unknown"
        _track_usage(_key_index, PRIMARY_MODEL, len(prompt), len(resp.text or ""))
        result = word if word in ("lab", "prescription", "investigation") else "unknown"
        print(f"[gemini] doc_type AI: {result!r}")
        return result
    except Exception as e:
        print(f"[gemini] doc_type AI failed: {e}")
        return "unknown"



def analyze_lab_from_file(file_bytes: bytes, mime_type: str) -> dict:
    is_pdf = file_bytes.startswith(b"%PDF")
    mode = "PDF native text" if is_pdf else "image vision"
    print(f"[gemini] lab extract -> {mode}")
    file_parts = _file_to_parts(file_bytes, mime_type)
    prompt = (
        "Read this lab report and extract every single test result from every page. "
        "Return ONLY a JSON object, no markdown:\n"
        "{\n"
        '  \"patient\": \"<full patient name>\",\n'
        '  \"date\": \"<DD-Mon-YYYY>\",\n'
        '  \"lab\": \"<lab name>\",\n'
        '  \"tests\": [[\"name\",\"value\",\"unit\",\"ref_range\",\"panel\"], ...]\n'
        "}\n\n"
        "Rules:\n"
        "- Include EVERY test from EVERY page (blood, urine, hormones, vitamins, lipids, thyroid, etc.)\n"
        "- Each test is ONE inner array: [name, value, unit, ref_range, panel]\n"
        "- panel = the test section/group name from the report, e.g.: Complete Blood Count, "
        "Thyroid Profile, Lipid Profile, Liver Function Test, Kidney Function Test, "
        "Urine Routine, Vitamins, Hormones, Diabetes, Electrolytes, Iron Studies\n"
        "- For qualitative tests (Absent/Present): value=actual result, unit=N/A, ref=expected\n"
        "- Missing fields use empty string\n"
        "- Do NOT include section headers or panel/group names as tests\n"
        "- Do NOT include calculated/derived sub-values that appear indented under a primary test "
        "(e.g. eAG or Estimated Average Glucose shown under HbA1c should be SKIPPED — "
        "extract HbA1c only)\n"
        "- Do NOT extract 'Liver' or 'Kidney' or other organ names as test names — "
        "those are section headers, not tests\n"
        "- Glucose Fasting means the PRIMARY fasting glucose test ONLY, "
        "NOT eAG or average glucose sub-calculations\n"
        "- Do NOT stop early. Extract ALL tests on every page"
    )
    _rotate_key()
    client = _get_client()
    raw = ""
    try:
        resp = client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=[prompt] + file_parts,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=65536),
        )
        raw = (resp.text or "").strip()
        _track_usage(_key_index, PRIMARY_MODEL, len(prompt), len(raw))
        print(f"[gemini] lab raw response: {len(raw)} chars, finish={getattr(resp.candidates[0], 'finish_reason', '?') if resp.candidates else '?'}")
    except Exception as e:
        print(f"[gemini] lab extract API error: {e}")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()
    data = {}
    try:
        data = json.loads(raw)
        print(f"[gemini] lab JSON parsed clean: {len(data.get('tests',[]))} tests")
    except Exception:
        try:
            data = heal_truncated_json(raw)
            print(f"[gemini] lab JSON healed: {len(data.get('tests',[]))} tests")
        except Exception as e2:
            print(f"[gemini] lab extract JSON parse failed: {e2}")
            print(f"[gemini] raw[:500]: {raw[:500]}")
    tests = data.get("tests", [])
    results = []
    for t in tests:
        if not isinstance(t, list) or len(t) < 2:
            continue
        name = str(t[0]).strip()
        if not name:
            continue
        val  = str(t[1]).strip() if len(t) > 1 else ""
        unit = str(t[2]).strip() if len(t) > 2 else ""
        ref  = str(t[3]).strip() if len(t) > 3 else ""
        panel = str(t[4]).strip() if len(t) > 4 else "General"
        if not panel or panel.upper() in ("HIGH", "LOW", "NORMAL", ""):
            panel = "General"
        results.append({
            "Test_Name":       name,
            "Measured_Value":  val,
            "Unit":            unit,
            "Reference_Range": ref,
            "Category":        panel,
        })
    pname = data.get("patient", "Not Found") or "Not Found"
    master = {
        "Patient_Name": pname,
        "Date":         data.get("date",  "Not Found") or "Not Found",
        "Lab_Name":     data.get("lab",   "Not Found") or "Not Found",
        "Results":      results,
    }
    print(f"[gemini] lab extract done: {len(results)} tests, patient='{pname}'")
    return master

def process_health_query(user_context: str, query: str, asking_user: str = "the user") -> str:
    """Answer a health question using the user's medical records as context."""
    # Keep from START — context is pre-sorted with asking user's data first
    trimmed = user_context[:10000] if len(user_context) > 10000 else user_context
    prompt = (
        f"You are a medical records assistant for a family health tracking app.\n"
        f"The records below cover MULTIPLE family members. "
        f"Each person\'s section is headed \'--- Patient: NAME ---\'.\n\n"
        f"THE PERSON ASKING RIGHT NOW IS: {asking_user}\n"
        f"STRICT RULE: When the question uses \'I\', \'my\', \'me\', \'mine\', or \'my last\' — "
        f"you MUST ONLY look at the section labelled \'--- Patient: {asking_user} ---\'. "
        f"Data from ANY other patient section is COMPLETELY IRRELEVANT to a personal question "
        f"and must be IGNORED. Do not cite values from other patients when answering a "
        f"personal question.\n"
        f"If the question explicitly names someone else (e.g. \'what is Jyoti\'s TSH\'), "
        f"answer for that named person only.\n"
        f"Always cite the exact patient name, date and value you are referencing.\n"
        f"Recommend consulting a doctor for medical decisions.\n\n"
        f"FAMILY MEDICAL RECORDS (asking user\'s data appears first):\n"
        f"{trimmed}\n\n"
        f"QUESTION from {asking_user}: {query}"
    )
    _rotate_key()
    client = _get_client()
    try:
        resp = client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1024,
            )
        )
        _track_usage(_key_index, PRIMARY_MODEL, len(prompt), len(resp.text or ""))
        return resp.text or "No response generated."
    except Exception as e:
        return f"Error: {str(e)}"
