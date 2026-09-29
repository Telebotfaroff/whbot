import mimetypes
import os
from pathlib import Path

import requests
import time
import uuid


class GofileUploadError(RuntimeError):
    """Raised when a GoFile guest upload fails."""


class GofileUploader:
    """Upload files to GoFile using its anonymous guest-upload flow."""

    ENDPOINT = os.getenv(
        "GOFILE_UPLOAD_URL",
        "https://upload.gofile.io/uploadfile",
    )

    def upload(self, path, progress_callback=None):
        path = Path(path)
        if not path.is_file():
            raise GofileUploadError("File does not exist: {}".format(path))

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        file_size = path.stat().st_size
        boundary = "----WHBot{}".format(uuid.uuid4().hex)
        prefix = (
            "--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: {content_type}\r\n\r\n"
        ).format(
            boundary=boundary,
            filename=path.name.replace('"', "_"),
            content_type=content_type,
        ).encode("utf-8")
        suffix = "\r\n--{}--\r\n".format(boundary).encode("ascii")
        total_bytes = len(prefix) + file_size + len(suffix)
        started = time.monotonic()

        print(
            "[gofile] START file={} size={} endpoint={}".format(
                path.name, file_size, self.ENDPOINT
            ),
            flush=True,
        )

        def body():
            sent = 0

            yield prefix
            if progress_callback:
                progress_callback(0, file_size, started)

            with path.open("rb") as file_handle:
                while True:
                    chunk = file_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
                    sent += len(chunk)
                    if progress_callback:
                        progress_callback(sent, file_size, started)

            yield suffix
            if progress_callback:
                progress_callback(file_size, file_size, started)

        headers = {
            "Content-Type": "multipart/form-data; boundary={}".format(boundary),
            "Content-Length": str(total_bytes),
        }

        try:
            response = requests.post(
                self.ENDPOINT,
                data=body(),
                headers=headers,
                timeout=(30, None),
            )
        except requests.RequestException as exc:
            raise GofileUploadError(
                "GoFile network error: {}".format(exc)
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise GofileUploadError(
                "GoFile returned non-JSON HTTP {}: {}".format(
                    response.status_code,
                    response.text[:300],
                )
            ) from exc

        if response.status_code >= 400 or payload.get("status") != "ok":
            raise GofileUploadError(
                "GoFile upload failed (HTTP {}): {}".format(
                    response.status_code,
                    payload,
                )
            )

        data = payload.get("data") or {}
        download_page = data.get("downloadPage")
        if not download_page:
            raise GofileUploadError(
                "GoFile upload succeeded but no downloadPage was returned."
            )

        if progress_callback:
            progress_callback(file_size, file_size, started)

        print("[gofile] SUCCESS url={}".format(download_page), flush=True)
        return download_page

