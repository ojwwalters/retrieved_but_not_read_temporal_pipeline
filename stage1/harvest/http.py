"""Shared polite EDGAR HTTP client, reused by every harvester.

EDGAR is a public data API (no key) but requires a descriptive User-Agent with
a contact and asks callers to stay under ~10 req/s. This module centralises the
politeness so a future harvester cannot accidentally hammer the API: a Pacer
(copied from stage1.tools.fetch_polygon.Pacer, capped at <=8 req/s), get_json /
get_text with retry + exponential backoff, 404 -> empty, and the legacy
'Request Rate Threshold' body-sentinel detection (ported from sec_verify.get) —
EDGAR sometimes answers a burst with HTTP 200 whose BODY is a rate-limit notice,
which a naive client would mistake for real content.

Contract:
* get_json(url) -> parsed object; a 404 returns {} (a no-data resource is a
  normal outcome, not an error); a persistent failure raises HttpError so the
  caller records the miss in fetch_stats and degrades a field honestly — it is
  NEVER a bare except that silently drops data.
* get_text(url) -> decoded body; 404 -> "".
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from stage1.config import contact_email, require_contact_email, user_agent

MAX_RPS_CAP = 8.0
DEFAULT_TIMEOUT = 45.0
DEFAULT_TRIES = 6
BACKOFF_CAP = 30.0

# A body whose first bytes carry this notice is EDGAR throttling us with a
# 200 status; treat it as a transient rate limit and back off (legacy sentinel).
_RATE_SENTINEL = b"Request Rate Threshold"


class HttpError(Exception):
    """A request that could not be completed after all retries. Callers catch
    this specifically (never a bare except) to record the miss and degrade."""


def build_user_agent(contact: str | None) -> str:
    """The descriptive UA EDGAR requires, built from the caller's contact, else
    the configured one (stage1.config). The header is always well-formed (EDGAR
    rejects a bare/empty UA); EdgarClient refuses to send it without a real
    contact."""
    return user_agent(contact=(contact or "").strip() or None)


class Pacer:
    """Enforce a minimum interval between requests. Copied from
    stage1.tools.fetch_polygon.Pacer: wait() blocks until the interval has
    elapsed since the last mark(); every attempt marks, so a retried call still
    respects the cadence."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._last = None  # monotonic timestamp of the previous attempt

    def wait(self) -> None:
        if self._last is None:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def mark(self) -> None:
        self._last = time.monotonic()


class EdgarClient:
    """Polite EDGAR client. One instance per harvest run; all traffic for a
    harvester routes through it so the pacer (<=8 req/s) is global."""

    def __init__(self, contact: str, max_rps: float = MAX_RPS_CAP,
                 timeout: float = DEFAULT_TIMEOUT, tries: int = DEFAULT_TRIES,
                 verbose: bool = True):
        try:
            rps = float(max_rps)
        except (TypeError, ValueError):
            rps = MAX_RPS_CAP
        if rps <= 0 or rps > MAX_RPS_CAP:
            rps = MAX_RPS_CAP
        self.max_rps = rps
        self.pacer = Pacer(1.0 / rps)
        self.contact = (contact or "").strip() or contact_email()
        self.user_agent = build_user_agent(self.contact)
        self.timeout = float(timeout)
        self.tries = int(tries)
        self.verbose = verbose
        # every recovered/unrecovered miss is recorded here, never swallowed
        self.errors: list = []

    # -- low level ----------------------------------------------------------

    def _open(self, url: str):
        if not self.contact:
            require_contact_email()
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _backoff(self, attempt: int, why: str, url: str) -> None:
        wait = min(BACKOFF_CAP, 2.0 ** attempt)
        if self.verbose:
            print(f"  [edgar {why}] backoff {wait:.0f}s (attempt {attempt + 1}/{self.tries}) {url}",
                  file=sys.stderr, flush=True)
        time.sleep(wait)

    def get_bytes(self, url: str, tries: int = None, expect_json: bool = False):
        """(status, data). 404 -> (404, b''). 200 with a real body -> (200,
        data). Rate-limit body sentinel / 429 / 5xx / transient network errors
        back off and retry; other HTTP errors also retry (EDGAR 403s can be
        transient under load, matching the legacy retry-everything policy).
        Raises HttpError once all tries are exhausted."""
        tries = int(tries) if tries else self.tries
        last = "no attempt made"
        for attempt in range(tries):
            self.pacer.wait()
            try:
                with self._open(url) as resp:
                    data = resp.read()
                self.pacer.mark()
            except urllib.error.HTTPError as exc:
                self.pacer.mark()
                if exc.code == 404:
                    return 404, b""
                last = f"HTTP {exc.code}"
                if attempt == tries - 1:
                    break
                self._backoff(attempt, last, url)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.pacer.mark()
                last = f"{type(exc).__name__}: {exc}"
                if attempt == tries - 1:
                    break
                self._backoff(attempt, last, url)
                continue
            # HTTP 200 — but EDGAR may have handed us a rate-limit notice body,
            # or (for a JSON endpoint) something that is not JSON at all.
            head = data[:400]
            not_json = expect_json and data.lstrip()[:1] not in (b"{", b"[")
            if _RATE_SENTINEL in head or not_json:
                last = "rate-limit body sentinel" if _RATE_SENTINEL in head else "non-JSON body"
                if attempt == tries - 1:
                    break
                self._backoff(attempt, last, url)
                continue
            return 200, data
        raise HttpError(f"EDGAR unreachable after {tries} tries ({last}): {url}")

    # -- typed helpers ------------------------------------------------------

    def get_json(self, url: str, tries: int = None):
        """Parsed JSON object, or {} on 404. Raises HttpError on persistent
        failure or an unparseable 200 body."""
        status, data = self.get_bytes(url, tries=tries, expect_json=True)
        if status == 404:
            return {}
        try:
            return json.loads(data)
        except ValueError as exc:
            raise HttpError(f"EDGAR returned unparseable JSON for {url}: {exc}") from exc

    def get_text(self, url: str, tries: int = None) -> str:
        """Decoded body text, or '' on 404. Raises HttpError on persistent
        failure."""
        status, data = self.get_bytes(url, tries=tries)
        if status == 404:
            return ""
        return data.decode("utf-8", "ignore")

    def record_miss(self, where: str, url: str, exc: Exception) -> None:
        """Append an HTTP miss to the client's error log (surfaced in the
        snapshot manifest's fetch_stats.http_errors). This is how a degraded
        field stays honest instead of being silently empty."""
        self.errors.append({"where": where, "url": url, "error": repr(exc)})
