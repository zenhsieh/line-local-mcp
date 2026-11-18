from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings
from .media import MediaPayload, normalize_media_id, normalize_mime, parse_filename
from .redaction import Redactor


class LineApiError(RuntimeError):
    """Raised when the local LINE archive API cannot answer safely."""


def _too_large(path: str, max_bytes: int) -> str:
    return f"LINE API response for {path} exceeds the configured {max_bytes} byte limit"


class LineArchiveClient:
    def __init__(self, settings: Settings, redactor: Redactor):
        self.settings = settings
        self.redactor = redactor

    def _token(self) -> str:
        if self.settings.api_token:
            return self.settings.api_token
        if self.settings.token_file:
            try:
                flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
                fd = os.open(self.settings.token_file, flags)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                        raise LineApiError("token file must be owner-owned, regular, and mode 0600")
                    raw = os.read(fd, 4097)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise LineApiError("cannot read token file") from exc
            if not raw or len(raw) > 4096:
                raise LineApiError("token file is empty or too large")
            try:
                token = raw.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise LineApiError("token file is not UTF-8") from exc
            if not token or "\n" in token or "\r" in token:
                raise LineApiError("token file has invalid content")
            return token
        if not self.settings.token_command:
            raise LineApiError("no token source is configured")
        try:
            result = subprocess.run(
                self.settings.token_command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LineApiError(f"token command failed: {exc}") from exc
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise LineApiError("token command did not return a token")
        return token

    def _read(
        self,
        method: str,
        path: str,
        params: dict[str, str | int] | None,
        accept: str,
        max_bytes: int,
    ) -> tuple[bytes, dict[str, str]]:
        url = self.settings.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method=method,
            data=b"" if method == "POST" else None,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": accept},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise LineApiError(_too_large(path, max_bytes))
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise LineApiError(_too_large(path, max_bytes))
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raise LineApiError(f"LINE API returned HTTP {exc.code} for {path}") from exc
        except urllib.error.URLError as exc:
            raise LineApiError(f"cannot reach LINE API: {exc.reason}") from exc
        return raw, headers

    def request(
        self, method: str, path: str, params: dict[str, str | int] | None = None
    ) -> Any:
        raw, _ = self._read(
            method, path, params, "application/json", self.settings.max_response_bytes
        )
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LineApiError("LINE API returned invalid JSON") from exc
        return self.redactor.redact(data)

    def fetch_media(self, media_id: str) -> MediaPayload:
        """Fetch one attachment as raw bytes. The archive API must answer 404 for unknown ids."""
        media_id = normalize_media_id(media_id)
        raw, headers = self._read(
            "GET",
            "/media",
            {"id": media_id},
            "*/*",
            self.settings.max_media_bytes,
        )
        filename = parse_filename(headers.get("content-disposition"))
        return MediaPayload(
            media_id=media_id,
            mime=normalize_mime(headers.get("content-type"), filename),
            filename=filename,
            data=raw,
        )
