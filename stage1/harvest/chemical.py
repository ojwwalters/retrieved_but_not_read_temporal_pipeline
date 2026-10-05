"""Chemical hazard/carcinogenicity reclassification harvester — the SIXTH source.

Writes a FROZEN snapshot the OFFLINE chemical adapter (``stage1/adapters/chemical.py``)
derives from unchanged. A SMALL (~8 facts) high-quality supplement over three
authoritative sub-sources, each a dated CHANGE record: IARC Monographs Volume 142
(three phthalate plasticizers -> Group 2B), California Proposition 65 (four cancer
listings), and EPA TSCA (1,2-dichloroethane final risk evaluation).

DETERMINISM / NO-LLM. The recorded VALUES + citations live in a SMALL COMMITTED,
CITED reference table (``stage1/harvest/chemical_reference.jsonl``) with a source
URL + note per value — the same pattern as a committed whitelist, NOT an LLM. The
harvester's live half:

  * TSCA (LIVE-FETCHED, clean JSON) — fetches the Federal Register API for the
    committed TSCA document and CROSS-CHECKS its publication date / citation /
    CASRN against the committed reference (the strongest verification: the after
    value + citation are confirmed against the authoritative feed live).
  * IARC (PROBED) — the IARC list-of-classifications page is reachable but its
    classification table is a JS DataTables feed not cleanly auto-extractable via
    stdlib, so the prior/new groups + CAS come from the committed cited reference;
    the harvester records the page's reachability.
  * Prop 65 (PROBED) — OEHHA is behind an Incapsula bot-wall (a short challenge
    body), so the listing values come from the committed cited reference; the
    harvester records the block honestly.

Every fetch outcome (a live cross-check, a JS-feed note, a bot-wall block) is
recorded PER SUB-SOURCE in ``chemical_sources.jsonl`` (optional adapter provenance
enrichment) and in ``fetch_stats``; an HTTP miss goes to
``fetch_stats.http_errors``, never a bare except. The committed reference is copied
verbatim into the snapshot as the adapter's primary input.

Snapshot files (the EXACT fixed filenames the chemical adapter reads by name):

    chemical_reference.jsonl      (committed cited reference, adapter input; verbatim)
    chemical_sources.jsonl        (per-sub-source live-fetch provenance; enrichment)
    chemical_reference.meta.json  (sidecar; retrieved_at OMITTED for determinism)
    snapshot_manifest.json  .stage1_snapshot

COVERAGE SEMANTICS — BACK-DATABLE. IARC/Prop 65/TSCA publications are PERMANENT
public records (Federal Register, OEHHA notices, IARC Monographs / The Lancet
Oncology), so ``back_datable=True``: a different window simply selects a different
set of permanent publications. cutoff_exact is left UNSET (SEC-like), so rule (2)
governs: a LATER derive cutoff is allowed (an earlier candidate then becomes an
audit-visible ``excluded:temporal_window`` row) and an EARLIER cutoff is refused;
asof is NOT pinned (``asof_exact=False``) — the change dates are the fixed
publication dates, so a narrower asof still covers every earlier fact and only
temporal-excludes the later ones. precision='day' (all three sub-sources carry a
day-precision determination/publication date).

The sidecar OMITS the wall-clock ``retrieved_at`` (the finance-port determinism
fix), so a snapshot's only wall-clock is ``snapshot_manifest.harvested_at``. A
re-harvest may LEGITIMATELY differ (a live cross-check field changes) — like FDA it
is CONTENT-equivalent, not byte-identical — but the DERIVE off a frozen snapshot is
byte-identical.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "harvest_chemical:v1"

# The EXACT fixed filenames the chemical adapter reads by name.
REFERENCE_FILENAME = "chemical_reference.jsonl"
REFERENCE_META_FILENAME = "chemical_reference.meta.json"
SOURCES_FILENAME = "chemical_sources.jsonl"

# The committed cited reference's canonical home (shared with the adapter).
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_REFERENCE_PATH = PACKAGE_DIR / REFERENCE_FILENAME

# Authoritative endpoints.
FR_API_DOC = "https://www.federalregister.gov/api/v1/documents/{docnum}.json"
IARC_LIST_URL = "https://monographs.iarc.who.int/list-of-classifications"
OEHHA_CRNR_URL = "https://oehha.ca.gov/proposition-65/crnr"

USER_AGENT = user_agent()

# Politeness defaults (overridable via --opt).
DEFAULT_SLEEP = 1.0
DEFAULT_TRIES = 4
DEFAULT_TIMEOUT = 45.0


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _in_window(change_date: str, cutoff, asof) -> bool:
    """True when a 'YYYY-MM-DD' change date is within [cutoff, asof] inclusive.
    Total: an unparseable date is treated as out-of-window for the harvest filter
    (it is still emitted; the adapter's temporal gate makes the exclusion visible)."""
    try:
        d = date.fromisoformat(change_date)
    except (TypeError, ValueError):
        return False
    c = cutoff if isinstance(cutoff, date) else date.fromisoformat(str(cutoff))
    a = asof if isinstance(asof, date) else date.fromisoformat(str(asof))
    return c <= d <= a


# --------------------------------------------------------------------------- #
# HTTP helpers (module-level so tests can monkeypatch; polite + retrying).
# --------------------------------------------------------------------------- #
def http_get(url: str, *, tries: int = DEFAULT_TRIES, sleep: float = DEFAULT_SLEEP,
             timeout: float = DEFAULT_TIMEOUT):
    """GET a URL with retry/backoff. Returns (status, body_bytes) on any HTTP
    response (including 4xx, which carry a body), or raises the last exception
    after ``tries`` transient failures (connection/timeout). gzip-aware."""
    require_contact_email()
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            # An HTTP error status still carries a body we want to inspect.
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 (best-effort body read)
                pass
            return exc.code, raw
        except Exception as exc:  # noqa: BLE001 (transient: connection/timeout/DNS)
            last_exc = exc
            if attempt < tries:
                time.sleep(sleep * attempt)
    raise last_exc if last_exc is not None else RuntimeError(f"http_get failed: {url}")


def fetch_fr_document(document_number: str, *, tries: int = DEFAULT_TRIES,
                      sleep: float = DEFAULT_SLEEP, timeout: float = DEFAULT_TIMEOUT):
    """Fetch one Federal Register document as clean JSON. Returns (status, doc_dict)
    where doc_dict is the parsed JSON (or None when the body is not JSON)."""
    fields = ("&fields[]=document_number&fields[]=publication_date&fields[]=citation"
              "&fields[]=title&fields[]=type&fields[]=html_url&fields[]=abstract"
              "&fields[]=agency_names")
    url = FR_API_DOC.format(docnum=document_number) + "?" + fields.lstrip("&")
    status, raw = http_get(url, tries=tries, sleep=sleep, timeout=timeout)
    doc = None
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(doc, dict):
            doc = None
    except ValueError:
        doc = None
    return status, doc


def probe_url(url: str, *, tries: int = DEFAULT_TRIES, sleep: float = DEFAULT_SLEEP,
              timeout: float = DEFAULT_TIMEOUT):
    """Best-effort reachability probe. Returns (status, reachable, note); never
    raises (a transient failure becomes (None, False, reason)). 'reachable' is a
    conservative signal: a full page vs a tiny bot-wall challenge body."""
    try:
        status, raw = http_get(url, tries=tries, sleep=sleep, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, False, f"fetch failed: {type(exc).__name__}: {exc}"
    body = raw.decode("utf-8", "replace")
    low = body.lower()
    if status != 200:
        return status, False, f"http {status}"
    if "_incapsula_resource" in low or (len(body) < 1000 and "noindex" in low):
        return status, False, "bot-wall challenge body (Incapsula)"
    return status, len(body) >= 1000, f"page reachable ({len(body)} bytes)"


# --------------------------------------------------------------------------- #
# The harvester
# --------------------------------------------------------------------------- #
class ChemicalHarvester(Harvester):
    source = "chemical"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        """Back-datable, day-precision coverage. IARC/Prop 65/TSCA publications are
        permanent public records, so back_datable=True. cutoff_exact is left unset
        (SEC-like): rule (2) allows a LATER derive cutoff (early facts become
        audit-visible temporal exclusions) and refuses an earlier one; asof is NOT
        pinned (asof_exact=False) because the change dates are fixed publication
        dates — a narrower asof still covers every earlier fact and only
        temporal-excludes the later ones."""
        return {
            "cutoff": _iso(cfg["cutoff"]),
            "asof": _iso(cfg["asof"]),
            "asof_exact": False,
            "back_datable": True,
            "precision": "day",
            "window_basis": "determination_or_publication_date",
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        tries = int(cfg.get("http_tries", DEFAULT_TRIES))
        sleep = float(cfg.get("http_sleep", DEFAULT_SLEEP))
        timeout = float(cfg.get("http_timeout", DEFAULT_TIMEOUT))
        http_errors: list = []

        ref_path = self._resolve_reference(cfg)
        if ref_path is None or not ref_path.is_file():
            raise LookupError(
                f"chemical committed reference not found: {DEFAULT_REFERENCE_PATH} "
                "(the harvester requires the committed cited reference table)"
            )
        reference_rows = self._load_reference(ref_path)
        in_window = [r for r in reference_rows
                     if _in_window(str(r.get("change_date") or ""), cutoff, asof)]
        print(f"[harvest chemical] committed reference: {len(reference_rows)} fact(s), "
              f"{len(in_window)} in-window [{_iso(cutoff)}..{_iso(asof)}]",
              file=sys.stderr, flush=True)

        # ---- LIVE verification / probes, per sub-source -------------------
        sub_sources = sorted({str(r.get("sub_source") or "") for r in reference_rows if r.get("sub_source")})
        sources_rows = []
        for sub in sub_sources:
            rows = [r for r in reference_rows if r.get("sub_source") == sub]
            if sub == "tsca":
                sources_rows.append(self._verify_tsca(rows, tries, sleep, timeout, http_errors))
            elif sub == "iarc":
                sources_rows.append(self._probe_source(
                    "iarc", "IARC Monographs", IARC_LIST_URL, tries, sleep, timeout, http_errors,
                    js_feed=True))
            elif sub == "prop65":
                sources_rows.append(self._probe_source(
                    "prop65", "OEHHA Proposition 65 CRNR", OEHHA_CRNR_URL, tries, sleep, timeout,
                    http_errors, js_feed=False))
            else:
                sources_rows.append({
                    "sub_source": sub, "harvest_mode": "committed_reference",
                    "live_ok": False, "authority": "unknown",
                    "note": "no live verifier for this sub-source; values from committed reference",
                })

        self._emit(writer, cfg, ref_path, reference_rows, sources_rows, http_errors)

    # -- TSCA live cross-check ---------------------------------------------
    def _verify_tsca(self, rows, tries, sleep, timeout, http_errors) -> dict:
        """Fetch the committed TSCA document from the Federal Register API and
        cross-check publication_date / citation / CASRN against the reference row.
        A fetch miss is recorded in http_errors and degrades to
        harvest_mode='committed_reference' (never fabricated, never fatal)."""
        row = rows[0] if rows else {}
        ref = (row.get("after_evidence") or {}).get("ref") or {}
        docnum = ref.get("document_number")
        expected = {
            "document_number": docnum,
            "publication_date": ref.get("publication_date"),
            "citation": ref.get("citation"),
            "casrn": ref.get("casrn"),
        }
        out = {
            "sub_source": "tsca",
            "authority": "Federal Register API",
            "probe_url": FR_API_DOC.format(docnum=docnum) if docnum else None,
            "expected": expected,
        }
        if not docnum:
            out.update({"harvest_mode": "committed_reference", "live_ok": False,
                        "note": "reference row carries no document_number to verify"})
            return out
        try:
            status, doc = fetch_fr_document(docnum, tries=tries, sleep=sleep, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            http_errors.append({"stage": "tsca_fr_verify", "document_number": docnum,
                                "error": f"{type(exc).__name__}: {exc}"})
            out.update({"harvest_mode": "committed_reference", "live_ok": False,
                        "http_status": None,
                        "note": "Federal Register fetch failed; TSCA value from committed reference"})
            return out
        out["http_status"] = status
        if status != 200 or not isinstance(doc, dict):
            http_errors.append({"stage": "tsca_fr_verify", "document_number": docnum,
                                "http_status": status})
            out.update({"harvest_mode": "committed_reference", "live_ok": False,
                        "note": f"Federal Register returned http {status}; value from committed reference"})
            return out
        fetched = {
            "document_number": doc.get("document_number"),
            "publication_date": doc.get("publication_date"),
            "citation": doc.get("citation"),
            "title": doc.get("title"),
            "type": doc.get("type"),
            "html_url": doc.get("html_url"),
        }
        abstract = (doc.get("abstract") or "")
        matches = {
            "document_number": fetched["document_number"] == expected["document_number"],
            "publication_date": fetched["publication_date"] == expected["publication_date"],
            "citation": fetched["citation"] == expected["citation"],
            "casrn_in_abstract": bool(expected["casrn"]) and (expected["casrn"] in abstract),
            "unreasonable_risk_in_abstract": "unreasonable risk" in abstract.lower(),
        }
        all_ok = all([matches["document_number"], matches["publication_date"],
                      matches["citation"]])
        out.update({
            "harvest_mode": "live_fetched",
            "live_ok": all_ok,
            "fetched": fetched,
            "matches_reference": matches,
            "note": ("Federal Register final risk evaluation live-verified against the "
                     "committed reference" if all_ok else
                     "Federal Register reachable but a field disagreed with the reference "
                     "(see matches_reference); recorded for audit"),
        })
        print(f"[harvest chemical] TSCA live-verify doc {docnum}: "
              f"{'MATCH' if all_ok else 'MISMATCH'} (http {status})",
              file=sys.stderr, flush=True)
        return out

    # -- IARC / Prop 65 reachability probes --------------------------------
    def _probe_source(self, sub, authority, url, tries, sleep, timeout, http_errors,
                      js_feed) -> dict:
        """Probe an authoritative page whose STRUCTURED data is not cleanly
        auto-extractable (IARC: a JS DataTables feed; OEHHA: an Incapsula
        bot-wall). Records reachability; values come from the committed reference."""
        status, reachable, note = probe_url(url, tries=tries, sleep=sleep, timeout=timeout)
        if status is None:
            http_errors.append({"stage": f"{sub}_probe", "url": url, "error": note})
        detail = ("page reachable but the classification list is a JS DataTables feed "
                  "not cleanly auto-extractable via stdlib; prior/new groups + CAS from "
                  "the committed cited reference" if js_feed else
                  "OEHHA is behind an Incapsula bot-wall; listing values from the "
                  "committed cited reference of the CRNR notices")
        print(f"[harvest chemical] {sub} probe {url}: http={status} reachable={reachable}",
              file=sys.stderr, flush=True)
        return {
            "sub_source": sub,
            "authority": authority,
            "harvest_mode": "committed_reference",
            "live_ok": False,
            "probe_url": url,
            "http_status": status,
            "page_reachable": bool(reachable),
            "probe_note": note,
            "note": detail,
        }

    # -- snapshot emission (PURE given already-fetched rows) ---------------
    def _emit(self, writer, cfg, ref_path, reference_rows, sources_rows, http_errors) -> None:
        """Route the fetched rows through the SnapshotWriter: the committed
        reference copied VERBATIM (add_file, so the snapshot sha1 matches the
        committed table), the per-sub-source sources provenance (add_jsonl), and
        the sidecar (add_json; NO wall-clock retrieved_at). No network here — a
        test drives this with fixture rows to assert layout + determinism."""
        out_dir = writer.out_dir

        # committed cited reference — verbatim byte copy (the adapter's input).
        writer.add_file(REFERENCE_FILENAME, ref_path, rows=len(reference_rows))
        reference_sha1 = sha1_file(out_dir / REFERENCE_FILENAME)

        # per-sub-source live-fetch provenance (adapter enrichment).
        writer.add_jsonl(SOURCES_FILENAME, sources_rows)

        sub_counts = dict(sorted(Counter(
            str(r.get("sub_source") or "") for r in reference_rows).items()))
        live = {r.get("sub_source"): {
            "harvest_mode": r.get("harvest_mode"),
            "live_ok": r.get("live_ok"),
            "http_status": r.get("http_status"),
        } for r in sources_rows}

        # sidecar — retrieved_at OMITTED so a re-harvest of identical data yields a
        # reproducible derive fingerprint; reference_sha1 pins the copied table.
        writer.add_json(REFERENCE_META_FILENAME, {
            "tool_version": TOOL_VERSION,
            "reference_file": REFERENCE_FILENAME,
            "reference_sha1": reference_sha1,
            "counts": {"facts": len(reference_rows), "by_sub_source": sub_counts},
            "sub_sources": sorted(sub_counts),
            "live_verification": live,
            "authorities": {
                "iarc": "IARC Monographs (monographs.iarc.who.int)",
                "prop65": "California OEHHA Proposition 65 (oehha.ca.gov)",
                "tsca": "US EPA via the Federal Register API (federalregister.gov)",
            },
        })

        writer.set_params({
            "reference_file": REFERENCE_FILENAME,
            "reference_sha1": reference_sha1,
            "fr_api_endpoint": FR_API_DOC,
            "iarc_list_url": IARC_LIST_URL,
            "oehha_crnr_url": OEHHA_CRNR_URL,
            "user_agent": USER_AGENT,
            "sub_sources": sorted(sub_counts),
            "back_datable": True,
            "files": [REFERENCE_FILENAME],
        })
        n_live = sum(1 for r in sources_rows if r.get("harvest_mode") == "live_fetched")
        writer.set_stats({
            "facts": len(reference_rows),
            "by_sub_source": sub_counts,
            "sub_sources_live_fetched": n_live,
            "sub_sources_committed_reference": len(sources_rows) - n_live,
            "live_verification": live,
            "http_errors": http_errors,
        })
        print(f"[harvest chemical] DONE. {len(reference_rows)} fact(s) across "
              f"{len(sub_counts)} sub-source(s); {n_live} live-fetched, "
              f"{len(sources_rows) - n_live} committed-reference; "
              f"{len(http_errors)} http miss(es).", file=sys.stderr, flush=True)

    # -- reference loading --------------------------------------------------
    @staticmethod
    def _resolve_reference(cfg: dict):
        """A cfg override (--opt reference=PATH) wins, else the package default."""
        override = cfg.get("reference")
        if isinstance(override, str) and override.strip():
            return Path(override.strip())
        return DEFAULT_REFERENCE_PATH

    @staticmethod
    def _load_reference(path: Path) -> list:
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rows.append(json.loads(line))
        return rows


HARVESTER = ChemicalHarvester()
