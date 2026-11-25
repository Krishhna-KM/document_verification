# Document Verification API

Send a document (image or PDF) and a JSON file of key/value pairs. Every key is searched for in the document,
the value found is compared with yours, and each key comes back as **fully matched**, **partially matched** or
**not matched**, with the extracted value and a match score. There is no document type to choose: it works for
marksheets, scorecards, certificates and any other document.

```
OCR New/
├── main.py                  # python main.py
├── requirements.txt
├── .env.example             # copy to .env
├── backend/
│   ├── app.py  config.py  errors.py  routes.py
│   ├── document_types.json  # internal "profiles" (extra knowledge for known kinds of document)
│   └── services/
│       ├── resolver.py      # JSON keys -> what to look for and how to compare it
│       ├── keys.py          # key normalisation + comparison type inferred for unknown keys
│       ├── extractor.py     # prompt building + parsing the model's answer
│       ├── model_client.py  # model API calls: ocr_api / openai / mock
│       ├── matcher.py       # name / text / id / number / percentage / date comparison
│       ├── postprocess.py   # marksheet / scorecard logic (totals, percentage ...)
│       ├── verifier.py      # orchestration + the three statuses
│       └── document_loader.py, registry.py
└── frontend/index.html      # simple test page, served at /
```

## Run
```
pip install -r requirements.txt
copy .env.example .env      # set MODEL_PROVIDER (ocr_api | openai | mock), MODEL_API_URL, MODEL_API_KEY, MODEL_NAME
python main.py
```
Open the address in `HOST`/`PORT` (default `http://127.0.0.1:5000`) for the test page.
`MODEL_PROVIDER=mock` calls no model (it echoes your values back) - only for testing the plumbing.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/verify` | verify a document against key/value pairs |
| GET | `/api/health` | health check; `status` is `degraded` when `.env` is misconfigured |

### POST /api/verify
`multipart/form-data`:

| Field | |
|---|---|
| `file` | the document (png, jpg, tiff, bmp, webp or PDF) - also accepted as `document` |
| `json_file` | a **.json file** holding a flat object of key/value pairs |
| `metadata` | alternative to `json_file`: the same JSON as text |
| `debug` | `true` to add a `debug` object with the technical details (see Response) |

Or `application/json`: `{"document_base64": "...", "metadata": {...}}`.

The JSON file is a flat object. Keys are free text: use the labels printed on the document or your own names.
```json
{
  "name": "Krishna Malviya",
  "father_name": "Ram Kumar Malviya",
  "roll_number": "1221654",
  "percentage": 81,
  "Date of Birth": "02/06/2003"
}
```
```
curl -X POST http://127.0.0.1:5000/api/verify -F file=@marksheet.jpg -F json_file=@data.json
```
A value can carry matching options: `"percentage": {"value": 81, "tolerance": 1}`
(options: `tolerance`, `threshold` (0-100, for text), `contains`, `weight`, `type`). Keys with an empty value are skipped.

### Response
The response contains **only** these five values per key, in the order you sent them:
```json
{
  "results": [
    {"key": "name", "value": "Krishna Malviya", "doc_value": "KRISHNA MALVIYA", "match_score": 100.0, "status": "FULLY_MATCHED"},
    {"key": "Roll No.", "value": "1221655", "doc_value": "1221654", "match_score": 85.7, "status": "PARTIALLY_MATCHED"},
    {"key": "blood group", "value": "O+", "doc_value": null, "match_score": 0.0, "status": "NOT_MATCHED"}
  ]
}
```
| Value | |
|---|---|
| `key` | exactly what you sent |
| `value` | your value |
| `doc_value` | the value found on the document (`null` if the key is not on the document) |
| `match_score` | 0-100 |
| `status` | see below |

| Status | Meaning |
|---|---|
| `FULLY_MATCHED` | the document value matches yours (within the key's tolerance / threshold) |
| `PARTIALLY_MATCHED` | close but not a match: one name part differs, an id is a near miss, a number is slightly outside tolerance - score >= `PARTIAL_MATCH_MIN_SCORE` (65) |
| `NOT_MATCHED` | different, or not found on the document (`doc_value` is `null`) |

The request ID is in the `X-Request-ID` response header (and in every error body).

**`debug=true`** adds a separate `debug` object next to `results`, for troubleshooting:
`request_id`, `overall_status`, `overall_match_score`, `summary` (counts), `detected_document_type`, `extracted_data`
(everything the model read, including derived values), `warnings`, `model_notes`, `processing` (model, pages, time),
`raw_model_output`, and `results` - one entry per key, same order, with `field` (the internal name the key was
understood as), `found`, `extraction_confidence`, `method` and `note`. `extraction_confidence` is the model's own
claim; qwen2.5vl:7b reports ~100 for nearly everything, so rely on `match_score` and the `warnings`.

## How keys are understood
1. **Keys are normalised** (`Father's Name` -> `fathers_name`, `Roll No.` -> `roll_no`).
2. **An internal profile is picked automatically** from the keys (`document_types.json`: marksheet, scorecard, caste /
   income certificate). It only adds knowledge: field meanings and aliases, prompt rules for that kind of document, and
   post-processing - e.g. a marksheet `percentage` / `total_marks_obtained` is **calculated from the subject marks** when
   the document prints none, and the marksheet rules know MP Board / CBSE layouts. Keys that select no profile
   (only names and dates of birth, or unknown keys) use generic rules. `debug.detected_document_type` tells you which was used.
3. **Any other key still works.** The model is asked for it, and the comparison type is inferred from the key and value:
   `*_name` of a person -> name, `*_no` / `*_number` / `*_id` -> exact id, `*_date` / `dob` / a date value -> date,
   numbers and `12,000` -> number, `*_percent` / `81%` -> percentage, anything else -> fuzzy text.
4. **Only the keys you send are asked of the model** (plus what their calculation needs): small vision models skip values
   when given long lists.

To teach the service a new kind of document, add an entry to `backend/document_types.json` (reloaded automatically).

### Matching rules
Titles (Shri, Smt, Dr ...) are ignored; "Ram Kumar" matches "Ramkumar"; initials match ("R K Malviya" ~ "Ram Kumar
Malviya", scored at most 95); a bracketed part can be matched on its own ("Prajapati" matches "Kumhar(Prajapati)");
a wrong name *part* is never forgiven by a long identical surname ("Ram Kumar Malviya" vs "Shyam Kumar Malviya" is
only PARTIALLY_MATCHED, never fully); abbreviations match their full form ("CBSE", "IIT Guwahati", "GATE 2026");
differing numbers inside text always differ ("Semester 4" vs "5"); `result` matches by containment ("PASS" matches
"PASS IN FIRST DIVISION", not "NOT PASS"); dates are parsed in many formats; ids must be identical (a near miss is
only partial). Known limitation: "Mohd" vs "Mohammad" is treated as different.

### Marksheet totals
Small vision models read table rows well but sometimes invent a "grand total", and misread column groups (MP Board
prints Maximum / Minimum / Obtained). The service arbitrates: rows whose components add up are verified and win over
a printed total; otherwise a sane printed grand total is used; a printed total equal to the sum of a majority of the
rows is the official aggregate (an additional subject left out). Percentage is calculated from the chosen totals and
every decision is listed in `warnings`. CBSE subjects are always out of 100. A date of birth that is implausible for
the exam year (e.g. the date under the photo) is discarded. CGPA is never converted to a percentage.
Known limitation: on wide, loosely spaced tables the per-subject marks can still be misread - the totals and
percentage stay correct, but treat `extracted_data.subjects` as unverified when the warning says so.

## Errors
Every error has the same shape; the `request_id` is also in the `X-Request-ID` response header and in the server log:
```json
{"error": "The model service did not answer within 600 seconds.", "code": "MODEL_TIMEOUT", "request_id": "3f9c1a7e2b40"}
```
(`details` with upstream information is added only when `DEBUG=true`.)

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `BAD_REQUEST` | missing document / JSON, invalid JSON (with line and column), bad keys or values, bad matching option |
| 413 | `PAYLOAD_TOO_LARGE` | upload over `MAX_UPLOAD_MB` |
| 415 | `UNSUPPORTED_DOCUMENT` / `UNSUPPORTED_MEDIA_TYPE` | not an image/PDF, or not multipart/JSON |
| 422 | `INVALID_DOCUMENT` / `PDF_ENCRYPTED` / `IMAGE_TOO_LARGE` | empty, corrupt, password-protected or oversized document |
| 502 | `MODEL_UNAVAILABLE` / `MODEL_AUTH_FAILED` / `MODEL_REJECTED_REQUEST` / `MODEL_BAD_RESPONSE` / `EXTRACTION_FAILED` | the model service failed or answered with unusable data |
| 504 | `MODEL_TIMEOUT` | the model service was too slow |
| 500 | `SERVER_MISCONFIGURED` / `INTERNAL_ERROR` | wrong `.env`, or an unexpected bug |

Failures inside a request are isolated: a key that cannot be compared comes back `NOT_MATCHED` (the reason is in `debug.results[].note`), and if
post-processing fails the raw extracted values are still used, with a warning in `debug.warnings`.

## Configuration (`.env`)
`MODEL_PROVIDER`, `MODEL_API_URL`, `MODEL_API_KEY` (the model server's credential), `MODEL_NAME`,
`MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_TOKENS`, `MODEL_PROMPT_MAX_CHARS` (the `/v1/ocr` endpoint rejects prompts over
4000 characters), `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `IMAGE_MAX_SIDE`, `PARTIAL_MATCH_MIN_SCORE`, `HOST`, `PORT`, `DEBUG`.
A bad value stops startup with a one-line message.

