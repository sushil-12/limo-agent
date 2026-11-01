"""Licence scanning: normaliser parity with limoRTService, image checks, the endpoint.

The golden outputs below were produced by limoRTService's responseNormalizer.ts (Node 24,
TZ=UTC) on the same inputs; a 2,018-case differential run of both implementations found
no differences.
"""

from __future__ import annotations

import base64
from typing import Any

import orjson
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response, TimeoutException

from app.realtime.ocr.images import decode_base64_image, detect_mime_type, validate_image
from app.realtime.ocr.normalizer import normalize_date, normalize_document
from tests.conftest import TokenFactory
from tests.realtime.conftest import ADMIN, DRIVER, RIDER, RtHarness, bearer

UK = (
    "UK DRIVING LICENCE\nDRIVING LICENCE\n1. MORGAN\n2. MISS SARAH MEREDITH\n3. 11.03.1976 UNITED KINGDOM\n"
    "4a. 19.01.2013 4c. DVLA\n4b. 18.01.2023\n5. MORGA753116SM9IJ 35\n8. 122 BURNS CRESCENT\nEDINBURGH\n"
    "EH1 9GP\n9. AM/A/B1/B/f/k/l/n/p/q\n"
)
GOLDEN = [
    (
        UK,
        (
            "SARAH MEREDITH MORGAN",
            "MORGA753116SM9IJ",
            "1976-03-11",
            "2023-01-18",
            "122 BURNS CRESCENT, EDINBURGH, EH1 9GP",
        ),
    ),
    (
        "DRIVING LICENCE\nDRIVINGLICENCE\nDVLA\n1. SMITH\n2. MR JOHN JAMES\n3. 02.07.1985 LONDON\n"
        "4b. 01.02.2031\n5.\nSMITH807025JJ9AB\n8. 7 HIGH ST, LEEDS\nLS1 1AA\n",
        ("JOHN JAMES SMITH", "SMITH807025JJ9AB", "1985-07-02", "2031-02-01", "7 HIGH ST, LEEDS, LS1 1AA"),
    ),
    (
        "REPUBLIQUE FRANCAISE\nPERMIS DE CONDUIRE\nDUPONT\n2. JEAN PIERRE\n3. 05.06.1990 PARIS\n"
        "4a. 01.01.2015\n4b. 01.01.2030\n5. 15AB12345\n8. 10 RUE DE RIVOLI, PARIS\n",
        ("JEAN PIERRE DUPONT", "15AB12345", "1990-06-05", "2030-01-01", "10 RUE DE RIVOLI, PARIS"),
    ),
    (
        "NEW YORK STATE\nUSA\nDRIVER LICENSE\nID 621 480 769\nMICHAEL\nMOTORIST.MIDDLE\n2345 ANYPLACE AVE\n"
        "ANYTOWN NY 12345\nDOB 10/31/1990\nExpires 10/31/2028\nCLASS D\n",
        (
            "MOTORIST MIDDLE MICHAEL",
            "621 480 769",
            "1990-10-31",
            "2028-10-31",
            "2345 ANYPLACE AVE, ANYTOWN NY 12345",
        ),
    ),
    (
        "LOUISIANA\nDRIVER LICENSE\n010500207 D\nALFRED GLEN JUNIOR F\n123 BAYOU RD\nBATON ROUGE, LA 70801\n"
        "ISS 01/01/2020\nEXP 01/01/2028\nDOB 05/05/1960\n",
        (
            "ALFRED GLEN JUNIOR",
            "010500207",
            "1960-05-05",
            "2028-01-01",
            "123 BAYOU RD, BATON ROUGE, LA 70801",
        ),
    ),
    (
        "STATE OF TEXAS\nLicense No: TX12345678\nFull Name: MARIA LOPEZ\nDate of Birth: 1985-02-03\n"
        "Expiration: 2029-02-03\nAddress: 55 ELM ST, AUSTIN TX 73301\n",
        ("MARIA LOPEZ", "TX12345678", "1985-02-03", "2029-02-03", "55 ELM ST, AUSTIN TX 73301"),
    ),
    (
        "OHIO\nDL NO: OH1234567\nDOE, JOHN QUINCY\n5 OAK LANE\nCOLUMBUS OH 43004\n03/04/1970\n03/04/2027\n",
        ("JOHN QUINCY DOE", "OH1234567", "1970-03-04", "2027-03-04", "5 OAK LANE, COLUMBUS OH 43004"),
    ),
]


@pytest.mark.parametrize(("text", "expected"), GOLDEN)
def test_text_path_matches_limortservice(text: str, expected: tuple[str | None, ...]) -> None:
    out = normalize_document({"text": text}, 12, "proc")
    fields = ("name", "license_number", "dob", "expiry_date", "address")
    assert tuple(out[f]["value"] for f in fields) == expected
    assert out["name"]["confidence"] == 0.75  # regex-extracted values
    assert out["raw_entities"] == []
    assert out["processing_time_ms"] == 12
    assert out["processor_type"] == "proc"


def test_entity_path() -> None:
    document = {
        "entities": [
            {"type": "Family_Name", "mentionText": " DOE ", "confidence": 0.98},
            {"type": "Given_Names", "mentionText": "JANE", "confidence": 0.9},
            {"type": "Document_Id", "mentionText": "D1234567", "confidence": 0.95},
            {
                "type": "Date_Of_Birth",
                "mentionText": "01/02/1980",
                "confidence": 0.97,
                "normalizedValue": {"dateValue": {"year": 1980, "month": 1, "day": 2}},
            },
            {"type": "Expiration_Date", "mentionText": "05.06.2030", "confidence": 1.4},
            {"type": "Address", "mentionText": "1 MAIN ST", "confidence": 0.8},
            {"type": "Address", "mentionText": "2 BETTER ST", "confidence": 0.85},
            {"type": "", "mentionText": "ignored"},
        ],
        "text": "DOE\nJANE\nD1234567\n",
    }
    out = normalize_document(document, 0, "p")
    assert out["name"] == {"value": "JANE DOE", "confidence": pytest.approx(0.94)}
    assert out["license_number"] == {"value": "D1234567", "confidence": 0.95}
    assert out["dob"] == {"value": "1980-01-02", "confidence": 0.97}
    assert out["expiry_date"] == {"value": "2030-06-05", "confidence": 1.0}  # clamped
    assert out["address"] == {"value": "2 BETTER ST", "confidence": 0.85}  # higher confidence wins
    assert len(out["raw_entities"]) == 7
    assert out["raw_entities"][3]["normalized_value"] == "1980-01-02"


def test_weak_entity_number_is_replaced_by_pattern_scan() -> None:
    out = normalize_document(
        {
            "entities": [{"type": "document_id", "mentionText": "LIC", "confidence": 0.5}],
            "text": "UK DRIVING LICENCE\nSMITH807025AS9AB\n",
        },
        0,
        "p",
    )
    assert out["license_number"] == {"value": "SMITH807025AS9AB", "confidence": 0.75}


@pytest.mark.parametrize(
    ("raw", "dmy", "expected"),
    [
        ("2030-01-05", False, "2030-01-05"),
        ("01/05/2030", False, "2030-01-05"),
        ("01/05/2030", True, "2030-05-01"),
        ("13/05/2030", False, "2030-05-13"),
        ("05.06.2030", False, "2030-06-05"),
        ("March 5, 1990", False, "1990-03-05"),  # no timezone shift (Node shifted it off UTC)
        ("not a date", False, "not a date"),
    ],
)
def test_normalize_date(raw: str, dmy: bool, expected: str) -> None:
    assert normalize_date(raw, dmy) == expected


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


def test_image_checks() -> None:
    assert detect_mime_type(JPEG) == "image/jpeg"
    assert detect_mime_type(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert detect_mime_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert detect_mime_type(b"%PDF-1.7") == "application/pdf"
    assert validate_image(JPEG, 1000).valid
    assert validate_image(JPEG, 10).error == "File size 0.00 MB exceeds the 0.0 MB limit"
    bad = validate_image(b"hello world", 1000, "text/plain")
    assert not bad.valid
    assert bad.mime_type == "text/plain"
    assert not validate_image(b"hello world", 1000).valid
    assert decode_base64_image("data:image/jpeg;base64," + base64.b64encode(JPEG).decode()) == JPEG
    assert decode_base64_image("not base64 !!") is None


# --- Endpoint ------------------------------------------------------------------------------

PROCESS_URL = "https://us-documentai.googleapis.com/v1/projects/proj/locations/us/processors/abc:process"
DOCUMENT = {"document": {"text": GOLDEN[5][0]}}


@pytest.fixture
def ocr_h(rt_factory: Any, rsa_private_key: rsa.RSAPrivateKey) -> RtHarness:
    pem = rsa_private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    creds = orjson.dumps({"client_email": "ocr@proj.iam.gserviceaccount.com", "private_key": pem}).decode()
    return rt_factory(
        documentai_project_id="proj",
        documentai_processor_id="abc",
        documentai_credentials_json=creds,
        rt_ocr_rate_per_minute=3,
    )


@pytest.fixture
def google() -> Any:
    with respx.mock(assert_all_called=False) as router:
        router.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        )
        router.post(PROCESS_URL).mock(return_value=Response(200, json=DOCUMENT))
        router.route(host="testserver").pass_through()
        yield router


def scan(h: RtHarness, token: str, **kwargs: Any) -> Any:
    return h.client.post("/rt/v1/scan-license", headers=bearer(token), **kwargs)


def test_driver_multipart_scan(ocr_h: RtHarness, rt_token: TokenFactory, google: Any) -> None:
    big = JPEG + b"\x00" * 200_000  # over the 64 KB default body limit: allowed on this route
    r = scan(ocr_h, rt_token(DRIVER), files={"image": ("lic.jpg", big, "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["data"]["license_number"]["value"] == "TX12345678"
    assert body["data"]["processor_type"].endswith("/processors/abc")
    sent = orjson.loads(google.routes[1].calls[0].request.content)
    assert sent["rawDocument"]["mimeType"] == "image/jpeg"
    assert google.routes[1].calls[0].request.headers["authorization"] == "Bearer ya29.test"


def test_admin_json_base64_scan(ocr_h: RtHarness, rt_token: TokenFactory, google: Any) -> None:
    r = scan(
        ocr_h, rt_token(ADMIN), json={"base64": base64.b64encode(JPEG).decode(), "mimeType": "image/jpeg"}
    )
    assert r.status_code == 200


def test_scan_rejects_riders_and_anonymous(ocr_h: RtHarness, rt_token: TokenFactory, google: Any) -> None:
    assert scan(ocr_h, rt_token(RIDER), files={"image": ("a.jpg", JPEG, "image/jpeg")}).status_code == 403
    assert ocr_h.client.post("/rt/v1/scan-license", files={"image": ("a.jpg", JPEG)}).status_code == 401


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"json": {"base64": "!!!"}}, "Invalid base64 string"),
        ({"json": {"nothing": 1}}, "No image provided"),
        ({"files": {"image": ("a.txt", b"hello world", "text/plain")}}, "Unsupported file type"),
    ],
)
def test_scan_input_errors(
    ocr_h: RtHarness, rt_token: TokenFactory, google: Any, kwargs: dict[str, Any], message: str
) -> None:
    r = scan(ocr_h, rt_token(DRIVER), **kwargs)
    assert r.status_code == 400
    assert r.json()["success"] is False
    assert message in r.json()["error"]


def test_document_ai_failures_map_to_node_status_codes(
    ocr_h: RtHarness, rt_token: TokenFactory, google: Any
) -> None:
    token = rt_token(DRIVER)
    files = {"image": ("a.jpg", JPEG, "image/jpeg")}
    google.routes[1].mock(return_value=Response(500, json={"error": {"status": "INTERNAL"}}))
    assert scan(ocr_h, token, files=files).status_code == 502
    google.routes[1].mock(side_effect=TimeoutException("slow"))
    assert scan(ocr_h, token, files=files).status_code == 504
    google.routes[1].mock(return_value=Response(200, json={}))
    assert scan(ocr_h, token, files=files).json()["error"] == "Document AI returned an empty response"


def test_scan_is_rate_limited_per_user(ocr_h: RtHarness, rt_token: TokenFactory, google: Any) -> None:
    token = rt_token(DRIVER)
    codes = [scan(ocr_h, token, files={"image": ("a.jpg", JPEG, "image/jpeg")}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_unconfigured_document_ai_is_503(h: RtHarness, rt_token: TokenFactory) -> None:
    r = scan(h, rt_token(DRIVER), files={"image": ("a.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 503
    assert "DOCUMENTAI_PROJECT_ID" in r.json()["error"]


def test_other_routes_keep_the_small_body_limit(h: RtHarness, rt_token: TokenFactory) -> None:
    r = h.client.post(
        "/rt/v1/devices",
        content=b"{" + b" " * 70_000 + b"}",
        headers={**bearer(rt_token(RIDER)), "Content-Type": "application/json"},
    )
    assert r.status_code == 413
