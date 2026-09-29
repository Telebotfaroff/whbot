import mimetypes
import os
from pathlib import Path
import time

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor


class GofileUploadError(RuntimeError):
    """Raised when a GoFile guest upload fails."""


class GofileUploader:
    """Upload files to GoFile using a streaming multipart guest-upload flow."""

    ENDPOINT = os.getenv(
        "GOFILE_UPLOAD_URL",
        "https://upload.gofile.io/uploadfile",
    )

    def upload(self, path, progress_callback=None):
        path = Path(path)
        if not path.is_file():
            raise GofileUploadError("File does not exist: {}".format(path))

        content_type = (
            mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        file_size = path.stat().st_size
        started = time.monotonic()

        print(
            "[gofile] START file={} size={} endpoint={}".format(
                path.name, file_size, self.ENDPOINT
            ),
            flush=True,
        )

        with path.open("rb") as file_handle:
            encoder = MultipartEncoder(
                fields={
                    "file": (
                        path.name.replace('"', "_"),
                        file_handle,
                        content_type,
                    )
                }
            )

            def on_progress(monitor):
                if progress_callback:
                    # monitor.bytes_read includes multipart overhead. Report
                    # only the actual file bytes to Telegram.
                    overhead = max(monitor.len - file_size, 0)
                    file_bytes = min(
                        max(monitor.bytes_read - overhead, 0),
                        file_size,
                    )
                    progress_callback(file_bytes, file_size, started)

            body = MultipartEncoderMonitor(encoder, on_progress)

            try:
                response = requests.post(
                    self.ENDPOINT,
                    data=body,
                    headers={"Content-Type": body.content_type},
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

        print(
            "[gofile] SUCCESS url={}".format(download_page),
            flush=True,
        )
        return download_page
