import mimetypes
import os
from pathlib import Path

import requests


class GofileUploadError(RuntimeError):
    """Raised when a GoFile guest upload fails."""


class GofileUploader:
    """Upload files to GoFile using its anonymous guest-upload flow."""

    ENDPOINT = os.getenv(
        "GOFILE_UPLOAD_URL",
        "https://upload.gofile.io/uploadfile",
    )

    def upload(self, path):
        path = Path(path)
        if not path.is_file():
            raise GofileUploadError("File does not exist: {}".format(path))

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        print(
            "[gofile] START file={} size={} endpoint={}".format(
                path.name, path.stat().st_size, self.ENDPOINT
            ),
            flush=True,
        )

        try:
            # No Authorization header/token is supplied. Per GoFile's API,
            # this creates a guest account and a new public folder for the
            # upload, returning a public downloadPage.
            with path.open("rb") as file_handle:
                response = requests.post(
                    self.ENDPOINT,
                    files={
                        "file": (
                            path.name,
                            file_handle,
                            content_type,
                        )
                    },
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

        print("[gofile] SUCCESS url={}".format(download_page), flush=True)
        return download_page
