"""Unit tests for the OCR HTTP client (app/services/ocr.py): that the
optional `lang` argument is passed through to the service's `/ocr` `lang`
query param (and omitted otherwise), and that the WP-B `images` array in the
response is decoded into OcrResult.images."""

import base64
import functools

import httpx
import pytest

from app.services import ocr

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _capturing_client(monkeypatch, response_json=None):
    """Swap httpx.AsyncClient for one wired to a MockTransport that records
    the request and returns a canned OCR response."""
    seen = {}
    body = response_json if response_json is not None else {"text": "ok", "tables": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ocr.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )
    return seen


async def test_extract_text_omits_lang_param_by_default(monkeypatch):
    seen = _capturing_client(monkeypatch)

    result = await ocr.extract_text("scan.png", b"\x89PNG-bytes")

    assert result.text == "ok"
    assert "lang" not in seen["params"]


async def test_extract_text_passes_lang_through(monkeypatch):
    seen = _capturing_client(monkeypatch)

    await ocr.extract_text("scan.png", b"\x89PNG-bytes", lang="eng+guj")

    assert seen["params"]["lang"] == "eng+guj"


async def test_extract_text_empty_lang_is_treated_as_omitted(monkeypatch):
    seen = _capturing_client(monkeypatch)

    await ocr.extract_text("scan.png", b"\x89PNG-bytes", lang="")

    assert "lang" not in seen["params"]


async def test_extract_text_decodes_image_regions(monkeypatch):
    raw = b"\x89PNG\r\n\x1a\nfake-crop"
    _capturing_client(
        monkeypatch,
        response_json={
            "text": "ok",
            "tables": [],
            "images": [
                {
                    "bbox": [25.0, 7.0, 585.0, 108.5],
                    "bbox_space": "pdf_points",
                    "page": 0,
                    "source": "embedded",
                    "region_type_guess": "logo",
                    "width": 640,
                    "height": 116,
                    "format": "png",
                    "image_base64": base64.b64encode(raw).decode(),
                },
                {"source": "detected", "image_base64": "!!!not-valid-base64!!!"},  # skipped
                {"source": "detected"},                                            # skipped
            ],
        },
    )

    result = await ocr.extract_text("report.pdf", b"%PDF-fake")

    assert len(result.images) == 1
    img = result.images[0]
    assert img.data == raw
    assert img.source == "embedded"
    assert img.region_type_guess == "logo"
    assert img.bbox_space == "pdf_points"
    assert img.image_format == "png"


async def test_extract_text_without_images_key_is_fine(monkeypatch):
    _capturing_client(monkeypatch, response_json={"text": "ok", "tables": []})
    result = await ocr.extract_text("scan.png", b"bytes")
    assert result.images == []
    assert result.table_regions == []
    assert result.text_source == "ocr"                  # default when absent


async def test_extract_text_parses_ruled_line_table_regions(monkeypatch):
    _capturing_client(
        monkeypatch,
        response_json={
            "text": "ok",
            "tables": [],
            "text_source": "ocr",
            "table_regions": [
                {"bbox": [32, 427, 715, 883], "page": 0,
                 "region_text": "Type Result\nEYE 6/6", "source": "ruled_line_region"},
                {"bbox": [0, 0, 1, 1], "region_text": "   "},   # blank -> skipped
                {"bbox": [0, 0, 1, 1]},                          # no text -> skipped
            ],
        },
    )
    result = await ocr.extract_text("scan.png", b"bytes")
    assert result.text_source == "ocr"
    assert len(result.table_regions) == 1
    tr = result.table_regions[0]
    assert tr.bbox == [32, 427, 715, 883]
    assert "EYE 6/6" in tr.region_text


async def test_extract_text_native_pdf_text_source_passthrough(monkeypatch):
    _capturing_client(
        monkeypatch,
        response_json={"text": "ok", "tables": [[["A", "B"], ["1", "2"]]],
                       "text_source": "native_pdf"},
    )
    result = await ocr.extract_text("r.pdf", b"%PDF")
    assert result.text_source == "native_pdf"
    assert result.table_regions == []
    assert result.tables == [[["A", "B"], ["1", "2"]]]
