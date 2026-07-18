"""Stage 1 — acquire the source extract from kaikki.org (plan §4.1).

Downloads with HTTP range-resume, records provenance (URL, dump date from the
``Last-Modified`` header, byte size, sha256), and never mutates the input.
Uses only the stdlib so the pipeline stays dependency-light.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

_CHUNK = 1 << 20  # 1 MiB


@dataclass
class Provenance:
    url: str
    dump_date: str          # YYYY-MM-DD from Last-Modified (source dump date)
    bytes: int
    sha256: str
    downloaded_at: str


def _head(url: str) -> tuple[int | None, str | None]:
    """Return (content_length, last_modified_iso_date) via a HEAD request."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        length = resp.headers.get("Content-Length")
        lm = resp.headers.get("Last-Modified")
    total = int(length) if length else None
    dump_date = None
    if lm:
        try:
            dt = email.utils.parsedate_to_datetime(lm).astimezone(timezone.utc)
            dump_date = dt.date().isoformat()
        except (TypeError, ValueError):
            dump_date = None
    return total, dump_date


def download(
    url: str,
    dest: str,
    *,
    resume: bool = True,
    progress: bool = True,
) -> Provenance:
    """Download ``url`` to ``dest`` (resumable) and return provenance.

    A ``.done`` sidecar marks a fully-verified download so re-runs skip it.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    total, dump_date = _head(url)

    done_marker = dest + ".done"
    if os.path.exists(done_marker) and os.path.exists(dest):
        with open(done_marker) as f:
            return Provenance(**json.load(f))

    existing = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
    if total is not None and existing >= total:
        existing = 0  # stale/oversized partial — restart

    mode = "ab" if existing else "wb"
    req = urllib.request.Request(url)
    if existing:
        req.add_header("Range", f"bytes={existing}-")

    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, mode) as out:
        got = existing
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress and total:
                pct = 100.0 * got / total
                print(f"\r  downloading {pct:5.1f}%  ({got:,}/{total:,} B)", end="")
    if progress:
        print()

    size = os.path.getsize(dest)
    sha = _sha256_file(dest)
    prov = Provenance(
        url=url,
        dump_date=dump_date or "unknown",
        bytes=size,
        sha256=sha,
        downloaded_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    with open(done_marker, "w") as f:
        json.dump(asdict(prov), f, indent=2)
    return prov


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
