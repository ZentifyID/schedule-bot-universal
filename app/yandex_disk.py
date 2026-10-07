from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

RESOURCES_URL = "https://cloud-api.yandex.net/v1/disk/public/resources"
DOWNLOAD_URL = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _read_url(request: urllib.request.Request, timeout: int, max_bytes: int) -> bytes:
    requested_url = urllib.parse.urlparse(request.full_url)
    if requested_url.scheme != "https" or not requested_url.hostname:
        raise ValueError("Refusing an unsafe network URL")
    for attempt in range(3):
        try:
            # The HTTPS scheme is validated before this request and after redirects.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=timeout
            ) as response:
                final_url = urllib.parse.urlparse(
                    response.geturl()
                    if hasattr(response, "geturl")
                    else request.full_url
                )
                if final_url.scheme != "https" or not final_url.hostname:
                    raise ValueError("Remote server redirected to an unsafe URL")
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > max_bytes:
                        raise ValueError(
                            f"Remote file is larger than the {max_bytes} byte limit"
                        )
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ValueError(
                        f"Remote file is larger than the {max_bytes} byte limit"
                    )
                return content
        except (OSError, http.client.HTTPException) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
                if error.code != 429 and error.code < 500:
                    raise
            if attempt == 2:
                raise
            time.sleep(1 + attempt * 2)


def _get_json(url: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": "schedule-bot/2"}
    )
    payload = json.loads(_read_url(request, timeout, MAX_JSON_BYTES).decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Yandex Disk returned an invalid JSON response")
    return payload


def list_public_files(public_url: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    offset = 0
    limit = 200
    while True:
        payload = _get_json(
            RESOURCES_URL,
            {"public_key": public_url, "limit": limit, "offset": offset},
        )
        if payload.get("type") == "file":
            return [payload]
        embedded = payload.get("_embedded") or {}
        items = embedded.get("items") or []
        files.extend(item for item in items if item.get("type") == "file")
        offset += len(items)
        if not items or offset >= int(embedded.get("total", offset)):
            return files


def download_public_file(public_url: str, item: dict[str, Any]) -> bytes:
    direct_url = item.get("file")
    if not direct_url:
        payload = _get_json(
            DOWNLOAD_URL,
            {"public_key": public_url, "path": str(item.get("path", ""))},
        )
        direct_url = payload.get("href")
    if not direct_url:
        raise ValueError(
            f"Yandex Disk did not return a download URL for {item.get('name')}"
        )
    parsed_url = urllib.parse.urlparse(str(direct_url))
    if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username:
        raise ValueError("Yandex Disk returned an unsafe download URL")
    request = urllib.request.Request(
        str(direct_url), headers={"User-Agent": "schedule-bot/2"}
    )
    return _read_url(request, timeout=90, max_bytes=MAX_DOWNLOAD_BYTES)


def file_fingerprint(item: dict[str, Any]) -> str:
    return "|".join(
        str(item.get(key, "")) for key in ("name", "path", "modified", "size", "md5")
    )
