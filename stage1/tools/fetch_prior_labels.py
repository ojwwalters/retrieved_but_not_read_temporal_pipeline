"""Fetch the FULL archived label for every FDA history row (the novelty-guard input).

WHY THIS EXISTS
---------------
The Highlights extractor (``compute_highlights_delta``) certifies a concise
Highlights line as NEW by checking it is absent from the cutoff-anchored prior.
Until this tool existed, the only prior text available was
``prior_cutoff_full_section`` — the archived copy of ONE BODY SECTION. That is
the wrong comparand for a Highlights line, for two reasons:

  1. HIGHLIGHTS was never fetched at all. A Highlights bullet that has sat
     unchanged at the top of the label for years does not appear verbatim in
     body prose (Highlights is condensed summary language), so it reads as
     "added" against a body-only comparison. This is the dominant failure —
     not the body->Highlights "promotion" the original guard was designed for.
  2. Content living in ANOTHER section (8.1 Pregnancy, 8.3 Reproductive
     Potential, 17 Patient Counseling) was invisible to a section-scoped check.

An audit of the 140-fact release found 26 facts whose "added" text is present
VERBATIM in the archived label; 0 of them were findable in the section extract
the guard was given, 22 were findable in the full archived page. The guard's
logic was sound — its input was too narrow.

WHAT IT WRITES
--------------
``stage1/cache/fda_prior_labels.jsonl``, one row per archived capture:

    {"set_id", "snapshot_ts", "snapshot_url", "chars", "sha1", "text"}

plus a ``.meta.json`` sidecar (tool version, retrieved_at, source cache + sha1,
row/failure counts) so a release binds the cache bytes to when and how they
were built — the same provenance contract as the sports P54 and wikitext caches.

DETERMINISM
-----------
Wayback URLs are TIMESTAMP-PINNED immutable captures
(``/web/20260122211822id_/``; the ``id_`` suffix requests the original bytes
with no archive banner injected), so refetching a row yields identical bytes.
The cache is therefore a frozen, sha1-pinned input and the adapter that reads
it stays OFFLINE-ONLY. Only this tool touches the network, exactly like
``fetch_dailymed_history`` and ``fetch_polygon``.

Usage:
    python3 -m stage1.tools.fetch_prior_labels --out stage1/cache
    python3 -m stage1.tools.fetch_prior_labels --out stage1/cache --resume
    python3 -m stage1.tools.fetch_prior_labels --out stage1/cache --seed-from DIR
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_prior_labels:v1"
REPO = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY = REPO / "stage1/cache/fda_dailymed_history.jsonl"
OUT_FILENAME = "fda_prior_labels.jsonl"
UA = user_agent()
SLEEP, TRIES, TIMEOUT = 1.5, 3, 90
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def to_text(raw: str) -> str:
    """HTML -> plain text: drop tags, unescape entities, collapse whitespace.

    Deliberately lossy but STABLE: the same bytes always yield the same text,
    which is what makes the cached row a reproducible pipeline input.
    """
    return WS.sub(" ", html.unescape(TAG.sub(" ", raw))).strip()


# Markers every DailyMed label carries. A capture that has none is not a label:
# most often it is an undecompressed gzip stream decoded as text (mojibake),
# which would silently make the novelty guard certify EVERYTHING as new because
# nothing can ever match. Such a row must be refetched, never cached.
LABEL_MARKERS = ("HIGHLIGHTS", "PRESCRIBING INFORMATION", "DOSAGE", "WARNINGS",
                 "INDICATIONS", "ADVERSE REACTIONS")


def is_label_text(text: str) -> bool:
    if not text or len(text) < 500:
        return False
    head = text[:4000]
    if sum(1 for c in head if ord(c) > 127) / len(head) > 0.10:
        return False            # binary / mojibake
    upper = text.upper()
    return sum(m in upper for m in LABEL_MARKERS) >= 2


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def read_history(path: Path):
    """(set_id, snapshot_ts, snapshot_url) for every row with a usable prior."""
    seen, rows = set(), []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = r.get("prior_cutoff_snapshot_url")
        set_id = r.get("set_id")
        ts = str(r.get("prior_cutoff_snapshot_ts") or "")
        if not (url and set_id):
            continue
        key = (set_id, ts)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"set_id": set_id, "snapshot_ts": ts, "snapshot_url": url})
    return rows


def fetch(url: str) -> str | None:
    require_contact_email()
    for attempt in range(TRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - report, never abort the batch
            if attempt == TRIES - 1:
                print(f"    FAIL {url[:90]}: {exc}", file=sys.stderr)
                return None
            time.sleep(SLEEP * (attempt + 2))
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                    help="FDA history cache to read prior snapshot URLs from")
    ap.add_argument("--out", type=Path, default=REPO / "stage1/cache",
                    help="output directory for fda_prior_labels.jsonl")
    ap.add_argument("--resume", action="store_true",
                    help="keep rows already present in the output and fetch only the rest")
    ap.add_argument("--seed-from", type=Path, default=None,
                    help="directory of pre-fetched <set_id>.txt or <fact_id>.txt raw "
                         "captures to use instead of refetching (offline seeding)")
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N rows (testing)")
    args = ap.parse_args(argv)

    if not args.history.is_file():
        print(f"error: history cache not found: {args.history}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / OUT_FILENAME

    rows = read_history(args.history)
    existing: dict[tuple[str, str], dict] = {}
    if args.resume and out_path.is_file():
        for line in out_path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                existing[(r["set_id"], r.get("snapshot_ts", ""))] = r
        print(f"resume: {len(existing)} row(s) already cached")

    seed: dict[str, Path] = {}
    if args.seed_from and args.seed_from.is_dir():
        seed = {p.stem: p for p in args.seed_from.glob("*.txt")}
        print(f"seeding from {len(seed)} pre-fetched capture(s) in {args.seed_from}")

    out_rows, failures = [], 0
    todo = rows[: args.limit] if args.limit else rows
    for i, r in enumerate(todo, 1):
        key = (r["set_id"], r["snapshot_ts"])
        if key in existing and is_label_text(existing[key].get("text", "")):
            out_rows.append(existing[key])
            continue
        text = None
        # A seeded capture is a HINT, never trusted: it is only used if it
        # actually parses as label text (see is_label_text).
        if r["set_id"] in seed:
            candidate = to_text(seed[r["set_id"]].read_text(encoding="utf-8", errors="replace"))
            if is_label_text(candidate):
                text = candidate
            else:
                print(f"    seed rejected (not label text): {r['set_id']}", file=sys.stderr)
        if text is None:
            raw = fetch(r["snapshot_url"])
            time.sleep(SLEEP)
            text = to_text(raw) if raw is not None else None
        if text is None or not is_label_text(text):
            print(f"    UNUSABLE {r['set_id']}: capture is not readable label text",
                  file=sys.stderr)
            failures += 1
            continue
        out_rows.append({**r, "chars": len(text), "sha1": sha1_text(text), "text": text})
        if i % 20 == 0:
            print(f"  {i}/{len(todo)} ({failures} failed)", flush=True)

    # Deterministic order so the cache bytes are reproducible.
    out_rows.sort(key=lambda x: (x["set_id"], x.get("snapshot_ts", "")))
    with out_path.open("w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")

    meta = {
        "tool_version": TOOL_VERSION,
        "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history_file": args.history.name,
        "history_sha1": hashlib.sha1(args.history.read_bytes()).hexdigest(),
        "rows": len(out_rows),
        "candidates": len(rows),
        "failures": failures,
        "note": ("Full archived DailyMed label text per pinned Wayback capture. The "
                 "novelty comparand for the Highlights extractor: includes the prior "
                 "HIGHLIGHTS block and every body section, not one section extract."),
    }
    (args.out / f"{OUT_FILENAME}.meta.json").write_text(
        json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print(f"\nwrote {out_path}  rows={len(out_rows)}  failures={failures}")
    print(f"      {out_path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
