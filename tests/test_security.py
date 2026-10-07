from __future__ import annotations

import http.client
import io
import logging
import unittest
import urllib.error
import urllib.request
import zipfile
from typing import Self
from unittest.mock import patch

from app.logging_config import RedactingFormatter
from app.replacement_service import parse_replacements_all
from app.schedule_service import _validated_pdf
from app.yandex_disk import _read_url, download_public_file


class FakeResponse:
    def __init__(self, content: bytes, content_length: int | None = None) -> None:
        self.content = content
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]


def docx_with_xml(xml: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


class SecurityTests(unittest.TestCase):
    def test_network_retries_only_transient_errors_and_closes_http_errors(self) -> None:
        for code, calls in ((404, 1), (503, 2)):
            body = io.BytesIO(b"error")
            error = urllib.error.HTTPError(
                "https://example.invalid", code, "error", {}, body
            )
            with (
                self.subTest(code=code),
                patch(
                    "urllib.request.urlopen", side_effect=[error, FakeResponse(b"ok")]
                ) as request,
                patch("app.yandex_disk.time.sleep") as sleep,
            ):
                if code == 404:
                    with self.assertRaises(urllib.error.HTTPError):
                        _read_url(
                            urllib.request.Request("https://example.invalid"), 1, 100
                        )
                    sleep.assert_not_called()
                else:
                    self.assertEqual(
                        _read_url(
                            urllib.request.Request("https://example.invalid"), 1, 100
                        ),
                        b"ok",
                    )
                    sleep.assert_called_once_with(1)
                self.assertEqual(request.call_count, calls)
                self.assertTrue(body.closed)
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[
                    http.client.IncompleteRead(b"partial"),
                    FakeResponse(b"ok"),
                ],
            ),
            patch("app.yandex_disk.time.sleep"),
        ):
            self.assertEqual(
                _read_url(urllib.request.Request("https://example.invalid"), 1, 100),
                b"ok",
            )
        with (
            patch(
                "urllib.request.urlopen", side_effect=TimeoutError("offline")
            ) as request,
            patch("app.yandex_disk.time.sleep") as sleep,
            self.assertRaisesRegex(TimeoutError, "offline"),
        ):
            _read_url(urllib.request.Request("https://example.invalid"), 1, 100)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3])

    def test_download_rejects_insecure_direct_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe download URL"):
            download_public_file(
                "https://disk.yandex.ru/example",
                {"file": "http://127.0.0.1/private", "name": "file.pdf"},
            )

    def test_download_size_is_bounded(self) -> None:
        response = FakeResponse(b"small", content_length=101)
        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaisesRegex(ValueError, "larger"),
        ):
            _read_url(urllib.request.Request("https://example.invalid"), 1, 100)

    def test_non_pdf_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            _validated_pdf(b"<html>login page</html>", "schedule.pdf")
        self.assertEqual(_validated_pdf(b"%PDF-1.7", "schedule.pdf"), b"%PDF-1.7")

    def test_docx_rejects_entity_declarations(self) -> None:
        malicious = docx_with_xml(
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]><x>&a;</x>'
        )
        with self.assertRaisesRegex(ValueError, "forbidden XML"):
            parse_replacements_all(malicious)

    def test_logs_redact_telegram_tokens(self) -> None:
        token = "123456789:" + "A" * 35
        formatter = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed %s", (token,), None
        )
        rendered = formatter.format(record)
        self.assertNotIn(token, rendered)
        self.assertIn("REDACTED", rendered)
