"""Stream one day of adsb.lol history straight from GitHub releases.

Each day is a GitHub release split into parts (`.tar.aa`, `.tar.ab`, ...). The parts are
read one after another as a single stream and fed to `tarfile` in streaming mode, so the
raw day (several GB) is never written to disk. Only `traces/` members are looked at.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import date

INSTANCES = ("readsb-prod-0", "readsb-prod-1", "readsb-staging-0")
API = "https://api.github.com/repos/adsblol/{repo}/releases/tags/{tag}"


def _request(url: str, accept: str, auth: bool) -> urllib.request.Request:
    headers = {"Accept": accept, "User-Agent": "aerolink-realdata"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if auth and token:  # set automatically in GitHub Codespaces; raises the API rate limit
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def tag_for(day: date, instance: str) -> str:
    return f"v{day:%Y.%m.%d}-planes-{instance}"


def release_assets(day: date, instance: str) -> list[str] | None:
    """Download URLs of the day's tar parts in order, or None if that release doesn't exist."""
    for repo in (f"globe_history_{day.year}", "globe_history"):
        url = API.format(repo=repo, tag=tag_for(day, instance))
        release = None
        for auth in (True, False):  # a scoped Codespaces token can be refused: retry anonymously
            try:
                with urllib.request.urlopen(_request(url, "application/vnd.github+json", auth), timeout=60) as r:
                    release = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break
                if e.code in (401, 403) and auth:
                    continue
                raise
        if release is None:
            continue
        parts = sorted(
            (a for a in release.get("assets", []) if ".tar" in a["name"]),
            key=lambda a: a["name"],
        )
        if parts:
            return [a["browser_download_url"] for a in parts]  # public downloads, no token needed
    return None


def find_release(day: date) -> tuple[str, list[str]] | None:
    """First instance that published this day (prod first, staging as fallback)."""
    for instance in INSTANCES:
        urls = release_assets(day, instance)
        if urls:
            return instance, urls
    return None


class ConcatStream(io.RawIOBase):
    """Read several byte streams back to back, as one file-like object."""

    def __init__(self, streams: Iterable[io.BufferedIOBase]):
        self._streams: Iterator[io.BufferedIOBase] = iter(streams)
        self._current: io.BufferedIOBase | None = next(self._streams, None)

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[override]
        while self._current is not None:
            n = self._current.readinto(b)
            if n:
                return n
            self._current.close()
            self._current = next(self._streams, None)
        return 0


def open_parts(urls: list[str]) -> io.BufferedReader:
    def responses():
        for url in urls:
            # No token here: the download redirects to a storage host that rejects extra auth.
            yield urllib.request.urlopen(_request(url, "application/octet-stream", auth=False), timeout=300)

    return io.BufferedReader(ConcatStream(responses()), buffer_size=1 << 20)


def iter_trace_files(stream: io.BufferedIOBase) -> Iterator[tuple[str, bytes]]:
    """(name, raw bytes) of every traces/ file in a tar stream, without seeking."""
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile() or "traces/" not in member.name:
                continue
            f = tar.extractfile(member)
            if f is not None:
                yield member.name, f.read()
