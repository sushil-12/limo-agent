"""Document AI response -> licence fields. Line-for-line port of limoRTService's
``src/utils/responseNormalizer.ts`` so the apps get identical results.

Three passes:

1. structured entities returned by the processor (entity path);
2. OCR text with region-aware parsers when there are no entities (text path):
   UK / EU field codes ("2. NAME", "4b. DATE"), AAMVA codes ("1 LAST", "40 LIC NO"),
   labelled fields ("Name:", "DOB:") and heuristics;
3. region-specific licence-number patterns replace a weak or missing value.

Regex notes for the port: JavaScript's ``\\d``, ``\\b`` and ``\\s`` are ASCII here
(``re.ASCII``), and an end anchor that JavaScript applies to a whole multi-line string is
``\\Z`` (Python's ``$`` would also match before a trailing newline).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

A = re.ASCII
AI = re.ASCII | re.IGNORECASE

DocRegion = Literal["uk", "eu", "us_aamva", "us_other", "indian", "unknown"]

# Entity types, in priority order (JS Set iteration order).
NAME_TYPES = ("name", "full_name", "holder_name", "document_holder_name")
GIVEN_NAME_TYPES = ("given_names", "given_name", "first_name", "forename")
FAMILY_NAME_TYPES = ("family_name", "last_name", "surname")
LICENSE_NUMBER_TYPES = (
    "document_id",
    "id_number",
    "license_number",
    "licence_number",
    "driver_license_number",
    "dl_number",
    "card_number",
)
DOB_TYPES = ("date_of_birth", "birth_date", "dob", "birthdate")
EXPIRY_DATE_TYPES = ("expiration_date", "expiry_date", "date_of_expiry", "expires", "expiry", "valid_until")
ADDRESS_TYPES = ("address", "residential_address", "address_street", "address_full")


@dataclass(frozen=True, slots=True)
class LicenseField:
    value: str | None
    confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "confidence": self.confidence}


NULL_FIELD = LicenseField(None, None)


# --- Dates ---------------------------------------------------------------------------------

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}", A)
_A_SEP_B_Y = re.compile(r"(\d{1,2})([/\-.])(\d{1,2})\2(\d{4})", A)
# Formats JavaScript's Date() also understands, for the "last resort" branch.
_FALLBACK_FORMATS = (
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
    "%b %d %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%a %b %d %Y",
)


def normalize_date(raw: str, prefer_dmy: bool = False) -> str:
    """Common date formats -> YYYY-MM-DD. Unparseable input comes back unchanged.

    '.' means DD.MM.YYYY; '/' means MM/DD/YYYY unless prefer_dmy (EU/UK documents) or the
    first part is over 12; ISO stays ISO.
    """
    if not raw:
        return raw
    s = raw.strip()
    if _ISO.fullmatch(s):
        return s
    m = _A_SEP_B_Y.fullmatch(s)
    if m:
        a, sep, b, y = m.groups()
        is_dmy = sep == "." or prefer_dmy or int(a) > 12
        return f"{y}-{b.zfill(2)}-{a.zfill(2)}" if is_dmy else f"{y}-{a.zfill(2)}-{b.zfill(2)}"
    return _js_date_fallback(raw)


def _js_date_fallback(raw: str) -> str:
    s = raw.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in _FALLBACK_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


# --- Region detection and noise -------------------------------------------------------------

_UK = re.compile(r"uk\s+driving\s+licen[cs]e|dvla", AI)
_EU_CODE_LINE = re.compile(r"\d{1,2}[a-z]?\.\s+\S", A)
_AAMVA_CODE_LINE = re.compile(r"(?:40|[1-9]|[1-3]\d)[a-z]?\s+[A-Z\d]", A)
_INDIAN = re.compile(r"\b[A-Z]{2}\d{2}[\s-]?\d{7,11}\b", A)


def detect_doc_region(lines: list[str], full_text: str) -> DocRegion:
    if _UK.search(full_text):
        return "uk"
    if any(_EU_CODE_LINE.match(line) for line in lines):
        return "eu"
    if any(_AAMVA_CODE_LINE.match(line) for line in lines):
        return "us_aamva"
    if _INDIAN.search(full_text):
        return "indian"
    return "us_other"


_DL_CHARS = frozenset("DRIVINGLICENSECO ")
_DL_WORD = re.compile(r"[A-Z]*(?:DRIVIN[GQ]|LICEN[CS]E)[A-Z]*", A)


def filter_noise_lines(lines: list[str]) -> list[str]:
    """Drop lines that are only the "DRIVING LICENCE" security-background print."""
    kept = []
    for line in lines:
        upper = line.upper()
        if (
            re.search(r"\d", line, A)
            or (re.search(r"[,./]", line) and not _DL_WORD.fullmatch(upper))
            or any(c not in _DL_CHARS for c in upper)
        ):
            kept.append(line)
    return kept


# --- Licence-number patterns (pass 3) --------------------------------------------------------

LICENSE_PATTERNS: dict[DocRegion, tuple[re.Pattern[str], ...]] = {
    "uk": (re.compile(r"\b([A-Z]{5}\d{6}[A-Z0-9]{2,6})\b", A),),
    "eu": (re.compile(r"\b([A-Z0-9]{7,20})\b", A),),
    "us_aamva": (
        re.compile(r"\b([A-Z]\d[A-Z0-9\-]{6,14})\b", A),
        re.compile(r"^(\d{2,4}(?:-\d{2,6}){1,4})\Z", A),
    ),
    "us_other": (
        re.compile(r"^ID\s+(\d[\d ]{4,20}\d)\s*\Z", AI),
        re.compile(r"\b([A-Z]{1,3}[A-Z0-9\-]{6,16})\b", A),
    ),
    "indian": (re.compile(r"\b([A-Z]{2}\d{2}[\s-]?\d{7,11})\b", A),),
    "unknown": (),
}


def scan_for_license_number(raw_text: str, region: DocRegion) -> str | None:
    for pattern in LICENSE_PATTERNS[region]:
        m = pattern.search(raw_text)
        if m:
            return m.group(1).strip()
    return None


_UK_LICENSE = re.compile(r"[A-Z]{5}\d{6}[A-Z0-9]{2,6}", A)


def is_weak_license_field(field: LicenseField, region: DocRegion) -> bool:
    v = field.value
    if not v:
        return True
    if len(v) < 5:
        return True
    if re.search(r"LICEN", v, re.IGNORECASE):
        return True
    return region == "uk" and not _UK_LICENSE.fullmatch(v)


# --- European / UK field codes ---------------------------------------------------------------

_EU_DATE = re.compile(r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{4}|\d{4}-\d{2}-\d{2})", A)
_ANY_CODE = re.compile(r"\d{1,2}[a-z]?\.\s+", AI)


def parse_european_fields(lines: list[str], prefer_dmy: bool) -> dict[str, str | None]:
    """EU Directive 2006/126/EC codes: 1 family, 2 given, 3 DOB, 4b expiry, 5 number, 8 address."""
    family = given = dob = expiry = number = address = None
    for i, line in enumerate(lines):
        if m := re.match(r"1\.\s+(.+)\Z", line, A):
            family = m.group(1).strip()
            continue
        if m := re.match(r"2\.\s+(.+)\Z", line, A):
            given = re.sub(r"^(MR|MRS|MS|MISS|DR|PROF)\.?\s+", "", m.group(1), flags=AI).strip()
            continue
        if re.match(r"3\.\s+", line, A):
            if dm := _EU_DATE.search(line):
                dob = normalize_date(dm.group(1), prefer_dmy)
            continue
        if re.match(r"4b\.\s+", line, AI):
            if dm := _EU_DATE.search(line):
                expiry = normalize_date(dm.group(1), prefer_dmy)
            continue
        if m := re.match(r"5\.\s*(.*)\Z", line, A):
            inline = m.group(1).strip()
            if len(inline) >= 5:
                number = inline
            else:  # value on one of the next lines
                for j in range(i + 1, min(len(lines), i + 4)):
                    nxt = lines[j].strip()
                    if len(nxt) >= 5 and not re.match(r"\d{1,2}[a-z]?\.\s", nxt, A):
                        number = nxt
                        break
            continue
        if m := re.match(r"8\.\s+(.+)\Z", line, A):
            parts = [m.group(1).strip()]
            for j in range(i + 1, min(len(lines), i + 6)):
                if _ANY_CODE.match(lines[j]):
                    break
                if re.match(r"[A-Z]{1,3}/", lines[j], A):  # vehicle categories "AM/A/B1/..."
                    break
                if nxt := lines[j].strip():
                    parts.append(nxt)
            address = ", ".join(p for p in parts if p)
            continue

    # No "1." line: the surname is often an all-caps word printed just above "2.".
    if not family and given:
        for i, line in enumerate(lines):
            if re.match(r"2\.\s+", line, A):
                for j in range(i - 1, max(-1, i - 5), -1):
                    candidate = lines[j].strip()
                    if re.fullmatch(r"[A-Z]{2,30}", candidate, A):
                        family = candidate
                        break
                break
    return {
        "family": family,
        "given": given,
        "dob": dob,
        "expiry": expiry,
        "number": number,
        "address": address,
    }


# --- Text path -------------------------------------------------------------------------------

_DATE_STR = r"(\d{1,2}[/\-.]?\d{1,2}[/\-.]?\d{4}|\d{4}-\d{2}-\d{2})"
_NON_NAME = frozenset(
    [
        "DRIVER",
        "LICENSE",
        "LICENCE",
        "PERMIT",
        "STATE",
        "USA",
        "FEDERAL",
        "CLASS",
        "TYPE",
        "PURPOSES",
        "DONOR",
        "EXCELSIOR",
        "GOLD",
        "STAR",
        "NOT",
        "VOID",
        "REAL",
        "ID",
        "DL",
        "CDL",
        "SEX",
        "HEIGHT",
        "WEIGHT",
        "EYES",
        "HAIR",
        "REST",
        "PEND",
        "ISS",
        "EXP",
        "DOB",
        "NONE",
        "ISSUED",
        "EXPIRES",
        "RESTRICTIONS",
        "ENDORSEMENTS",
        "MOTOR",
        "VEHICLES",
    ]
)
_NON_NAME_SINGLE = frozenset(
    [
        "DRIVER",
        "LICENSE",
        "LICENCE",
        "PERMIT",
        "STATE",
        "USA",
        "FEDERAL",
        "CLASS",
        "TYPE",
        "PURPOSES",
        "DONOR",
        "EXCELSIOR",
        "GOLD",
        "STAR",
        "NOT",
        "VOID",
        "REAL",
        "ID",
        "DL",
        "CDL",
        "NONE",
        "ISSUED",
        "EXPIRES",
        "RESTRICTIONS",
        "ENDORSEMENTS",
        "MOTOR",
        "VEHICLES",
        "CHAUFFEUR",
        "IDENTIFICATION",
        "FOR",
        "COMMERCIAL",
    ]
)
_CITY = re.compile(r"[A-Z][A-Z\s]+,?\s+[A-Z]{2}\s+[\d\-]{4,10}", A)
_SKIP_LINE = re.compile(
    r"(CLASS\s|USA\Z|TYPE[:\s]|SEX[:\s]|HGT[:\s]|WGT[:\s]|EYES[:\s]|REST[:\s]|PEND[:\s]|SBP\s|CDL\Z|DONOR|"
    r"DRIVER|LICENSE|LICENCE|PERMIT|PURPOSES|IDENTIFICATION)",
    AI,
)
_STOP_ADDR = re.compile(
    r"(CLASS\s|USA\Z|TYPE[:\s]|SEX[:\s]|HGT[:\s]|WGT[:\s]|EYES[:\s]|HAIR[:\s]|REST[:\s]|PEND[:\s]|DOB[:\s]|"
    r"EXP[:\s]|ISS[:\s]|\d{1,2}[a-z]?\s+\S|CDL\Z|DRIVER|LICENSE|PURPOSES|DONOR|EXCELSIOR|STAR)",
    AI,
)


def _tf(value: str | None, conf: float = 0.75) -> LicenseField:
    """A field read by regex: whitespace collapsed, fixed 0.75 confidence."""
    if not value:
        return NULL_FIELD
    return LicenseField(re.sub(r"\s+", " ", value.strip()), conf)


def _find_license_number(lines: list[str]) -> str | None:
    for line in lines:
        if m := re.match(
            r"40\s+(?:lic(?:ense)?\s*(?:no\.?|#)?\s*:?\s*)?([A-Z0-9][A-Z0-9\s\-]{4,20})", line, AI
        ):
            return m.group(1).strip()
        if m := re.search(
            r"(?:lic(?:ense)?\s*(?:no\.?|#|number)?|dl\s*(?:no\.?|#|number)?)\s*:?\s*([A-Z0-9][A-Z0-9\s\-]{4,20})",
            line,
            AI,
        ):
            return m.group(1).strip()
        if m := re.match(r"ID\s+(\d[\d ]{4,20}\d)\s*\Z", line, AI):  # NY "ID 621 480 769"
            return re.sub(r"\s+", " ", m.group(1)).strip()
        if m := re.search(r"\b([A-Z]{2}\d{2}[\s\-]?\d{7,11})\b", line, A):  # Indian DL
            return re.sub(r"\s+", " ", m.group(1), count=1).strip()
        if m := re.search(r"\b([A-Z]{2,5}(?:-[A-Z0-9]{2,6}){2,5})\b", line, A):  # NGA-123-456-789
            return m.group(1).strip()
        m = re.search(r"\b([A-Z]{1,3}[A-Z0-9\-]{6,16})\b", line, A)  # generic US
        if m and re.search(r"\d", m.group(1), A):
            return m.group(1).strip()
        m = re.match(r"(\d{2,4}(?:-\d{2,6}){1,4})\s*(?:[A-Z])?\Z", line, A)  # "97-148-1136"
        if m and len(m.group(1).replace("-", "")) >= 6:
            return m.group(1).strip()
        if m := re.match(r"(\d{6,12})(?:\s+[A-Z])?\Z", line, A):  # "010500207 D"
            return m.group(1).strip()
    return None


def _find_name(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """(full name, family name, given names)."""
    full = family = given = None
    for i, line in enumerate(lines):
        a1 = re.match(r"1\s+([A-Z][A-Z '\-]{1,35})\Z", line, A)
        if a1 and not family:
            family = a1.group(1).strip()
            continue
        a2 = re.match(r"2\s+([A-Z][A-Z '\-]{1,35})\Z", line, A)
        if a2 and not given:
            given = a2.group(1).strip()
            continue
        if labeled := re.match(r"(?:full\s+)?name\s*:\s*(.+)\Z", line, AI):
            full = labeled.group(1).strip()
            break
        if re.fullmatch(r"(?:full\s+)?name\s*:?\s*", line, AI) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if not re.match(r"\d{1,2}[a-z]?\s|[A-Z]{2,}\s*:", nxt, AI):
                full = nxt.strip()
                break
        last_first = re.match(r"([A-Z][A-Z'\-]{1,30}),\s+([A-Z][A-Z '\-]{1,40})\Z", line, A)
        if last_first and ":" not in line:
            family, given = last_first.group(1).strip(), last_first.group(2).strip()

    def caps_name(s: str) -> bool:
        return bool(re.fullmatch(r"[A-Z][A-Z'\-. ]{1,40}", s, A)) and not any(
            w in _NON_NAME for w in re.split(r"\s+", s)
        )

    if not (full or family or given):  # NY-style: surname line, then given names
        for l1, l2 in itertools.pairwise(lines):
            if not re.search(r"\s", l1) and caps_name(l1) and caps_name(l2):
                family = l1
                given = re.sub(r"\s+", " ", l2.replace(".", " ")).strip()
                break

    if not (full or family or given):  # Louisiana-style: "ALFRED GLEN JUNIOR F"
        for line in lines:
            if not re.fullmatch(r"[A-Z][A-Z '\-]{3,50}", line, A) or ":" in line:
                continue
            words = re.split(r"\s+", line)
            if not 2 <= len(words) <= 6:
                continue
            meaningful = words[:-1] if len(words[-1]) == 1 else words
            if len(meaningful) >= 2 and all(len(w) >= 2 and w not in _NON_NAME_SINGLE for w in meaningful):
                full = " ".join(meaningful)
                break
    return full, family, given


def _find_address(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if a8 := re.match(r"8\s+(.+)\Z", line, A):  # AAMVA field 8
            parts = [a8.group(1).strip()]
            for j in range(i + 1, min(len(lines), i + 8)):
                nxt = lines[j]
                if re.match(r"\d{1,2}[a-z]?\s+\S", nxt, AI) and not _CITY.match(nxt):
                    continue
                if _CITY.match(nxt):
                    parts.append(nxt)
                    break
                if _SKIP_LINE.match(nxt):
                    continue
                if re.match(r"(APT|UNIT|SUITE|STE|FLOOR|FL|#)\b", nxt, AI) or nxt[0] in "0123456789":
                    parts.append(nxt)
            return ", ".join(parts)
        if re.fullmatch(r"address\s*:?\s*", line, AI):  # "Address:" then lines
            parts = []
            for j in range(i + 1, min(len(lines), i + 5)):
                if re.match(r"(name|dob|date|blood|organ|holder|issue|valid|\d{1,2}[a-z]?\s)", lines[j], AI):
                    break
                parts.append(lines[j])
            if parts:
                return ", ".join(parts)
        if inline := re.match(r"address\s*:\s*(.+)\Z", line, AI):
            return inline.group(1)

    for i, line in enumerate(lines):  # street number heuristic: "12A JACKMAN DR"
        if re.match(r"\d{1,5}[A-Z]?\s+[A-Z].{3,}", line, AI):
            parts = [line]
            for j in range(i + 1, min(len(lines), i + 4)):
                if _STOP_ADDR.match(lines[j]):
                    break
                parts.append(lines[j])
                if _CITY.match(lines[j]):
                    break
            return ", ".join(parts)
    return None


def extract_from_text(raw_text: str) -> dict[str, LicenseField]:
    all_lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    region = detect_doc_region(all_lines, raw_text)
    prefer_dmy = region in ("uk", "eu")
    lines = filter_noise_lines(all_lines)

    family = given = full = dob = expiry = number = address = None
    if prefer_dmy:
        eu = parse_european_fields(lines, prefer_dmy)
        family, given, dob, expiry = eu["family"], eu["given"], eu["dob"], eu["expiry"]
        number, address = eu["number"], eu["address"]

    if not number:
        number = _find_license_number(lines)

    if not dob:
        for line in lines:
            m = re.search(r"(?:^3\s+)?d\.?o\.?b\.?\s*:?\s*" + _DATE_STR, line, AI) or re.search(
                r"date\s+of\s+birth\s*:?\s*" + _DATE_STR, line, AI
            )
            if m:
                dob = normalize_date(m.group(1), prefer_dmy)
                break

    if not expiry:
        for line in lines:
            m = re.search(
                r"(?:^4[ab]?\s+)?exp(?:ires?|iry|iration)?\s*:?\s*" + _DATE_STR, line, AI
            ) or re.search(
                r"valid(?:ity|(?:\s+(?:through|until|thru|to)))?\s*[:\s()\w]*" + _DATE_STR, line, AI
            )
            if m:
                expiry = normalize_date(m.group(1), prefer_dmy)
                break

    if not dob or not expiry:  # all dates: earliest = DOB, latest = expiry
        found = {
            normalize_date(m.group(1), prefer_dmy)
            for line in lines
            for m in re.finditer(r"\b(\d{2}[./\-]\d{2}[./\-]\d{4}|\d{4}-\d{2}-\d{2})\b", line, A)
        }
        ordered = sorted(found)
        if len(ordered) >= 2:
            dob = dob or ordered[0]
            expiry = expiry or ordered[-1]

    if not (full or family or given):
        full, family, given = _find_name(lines)
    name = full or (" ".join(p for p in (given, family) if p) or None)

    if not address:
        address = _find_address(lines)

    if is_weak_license_field(_tf(number), region):
        number = scan_for_license_number(raw_text, region) or number

    return {
        "name": _tf(name),
        "license_number": _tf(number),
        "dob": _tf(dob),
        "expiry_date": _tf(expiry),
        "address": _tf(address),
    }


# --- Entity path -------------------------------------------------------------------------------


def _make_field(value: str | None, confidence: Any) -> LicenseField:
    normalized = value.strip() if value else None
    is_number = isinstance(confidence, int | float) and not isinstance(confidence, bool)
    conf = min(1.0, max(0.0, float(confidence))) if is_number else None
    return LicenseField(normalized or None, conf)


def _extract_value(entity: dict[str, Any]) -> str | None:
    normalized = entity.get("normalizedValue") or {}
    date_value = normalized.get("dateValue")
    if date_value:
        y, mo, d = date_value.get("year"), date_value.get("month"), date_value.get("day")
        if y and mo and d:
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    if normalized.get("text"):
        return str(normalized["text"]).strip()
    mention = entity.get("mentionText")
    return mention.strip() if isinstance(mention, str) else None


def normalize_document(
    document: dict[str, Any], processing_time_ms: int, processor_type: str
) -> dict[str, Any]:
    """Document AI ``document`` (REST JSON, camelCase) -> the DTO limoRTService returned."""
    entities: list[dict[str, Any]] = document.get("entities") or []
    text: str | None = document.get("text")

    if not entities and text:
        fields = extract_from_text(text)
        return {
            **{k: v.to_dict() for k, v in fields.items()},
            "raw_entities": [],
            "processing_time_ms": processing_time_ms,
            "processor_type": processor_type,
        }

    raw_entities = []
    for e in entities:
        if not e.get("type"):
            continue
        item: dict[str, Any] = {
            "type": str(e["type"]).lower(),
            "value": (e.get("mentionText") or "").strip(),
            "confidence": e["confidence"] if isinstance(e.get("confidence"), int | float) else 0,
        }
        if (normalized := _extract_value(e)) is not None:
            item["normalized_value"] = normalized
        raw_entities.append(item)

    by_type: dict[str, dict[str, Any]] = {}
    for e in entities:
        key = str(e.get("type") or "").lower()
        existing = by_type.get(key)
        if existing is None or (e.get("confidence") or 0) > (existing.get("confidence") or 0):
            by_type[key] = e

    def pick_first(types: tuple[str, ...]) -> LicenseField:
        for t in types:
            if (entity := by_type.get(t)) is not None:
                return _make_field(_extract_value(entity), entity.get("confidence"))
        return NULL_FIELD

    name = pick_first(NAME_TYPES)
    if not name.value:
        given, family = pick_first(GIVEN_NAME_TYPES), pick_first(FAMILY_NAME_TYPES)
        if given.value or family.value:
            confs = [c for c in (given.confidence, family.confidence) if c is not None]
            name = LicenseField(
                " ".join(v for v in (given.value, family.value) if v),
                sum(confs) / len(confs) if confs else None,
            )

    number = pick_first(LICENSE_NUMBER_TYPES)
    if text:
        region = detect_doc_region([line.strip() for line in text.split("\n") if line.strip()], text)
        if is_weak_license_field(number, region) and (scanned := scan_for_license_number(text, region)):
            number = _make_field(scanned, 0.75)

    raw_dob, raw_expiry = pick_first(DOB_TYPES), pick_first(EXPIRY_DATE_TYPES)
    dob = LicenseField(normalize_date(raw_dob.value) if raw_dob.value else None, raw_dob.confidence)
    expiry = LicenseField(
        normalize_date(raw_expiry.value) if raw_expiry.value else None, raw_expiry.confidence
    )

    return {
        "name": name.to_dict(),
        "license_number": number.to_dict(),
        "dob": dob.to_dict(),
        "expiry_date": expiry.to_dict(),
        "address": pick_first(ADDRESS_TYPES).to_dict(),
        "raw_entities": raw_entities,
        "processing_time_ms": processing_time_ms,
        "processor_type": processor_type,
    }


def is_iso_date(value: str | None) -> bool:
    if not value or not _ISO.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
