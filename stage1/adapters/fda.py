"""FDA drug-label section-change adapter (source 'fda') — the 5th source.

RMC-PRIMARY MODEL (owner decision 2026-07-24). openFDA's Recent Major Changes
(RMC) field IS the authoritative change record: the FDA lists a section under
RMC *because* it changed, and the row carries everything the fact needs —
generic/brand, set_id, the changed section + subsection number (e.g. "5.6"),
the change_date (MM/YYYY), the label's effective_time, and the CURRENT text of
the changed section (``section_text``). So the FACT is SELF-CONTAINED in the
RMC row: "as of <change_date>, <drug>'s <section> (§<section_num>) now states:
<section_text>." No diff and no archived 'before' are needed to establish the
fact or its date.

This is a deliberate REFRAME away from the earlier before-vs-after DIFF model,
under which each record's fact was found by diffing an Internet-Archive
(Wayback) archived section (the "before") against the current openFDA section
(the "after"). That was fragile: the two sides were extracted at inconsistent
anchors and truncated at ~1200 chars, so for most records a clean diff was
impossible and the "fact" degraded to "the section changed, compare the
wording" — useless for question generation, and ~59 records sat in review
purely for before/after alignment.

What the reframe does:

* PRIMARY FACT = the RMC row. ``after`` (the recorded value) = the FULL CURRENT
  SECTION (owner decision 2026-07-24 "Option B"): the WHOLE current SPL section
  from the live DailyMed label, frozen into the cache as ``current_full_section``
  by ``stage1.tools.refetch_current_sections`` and extracted with the raised full
  cap. This REPLACES the earlier ``after`` = RMC ``section_text``, which openFDA
  HARD-CAPS at exactly 1200 chars: a full-prior-vs-1200-capped-current delta MISSED
  genuinely-new content beyond char 1200 (a false NEGATIVE the truncation guard
  cannot recover — it only removes the truncation false POSITIVES). DailyMed serves
  the whole section, so the complete current section is the honest recorded value.
  When the current fetch was unavailable (section_not_found / no_current_label /
  fetch_error / out_of_window_not_fetched) ``after`` falls back to the RMC
  ``section_text`` (then the history after_text). ``after.evidence.kind =
  "openfda_rmc"`` (the CHANGE record is the RMC — effective_time / change_date),
  ``ref.source`` names the actual value source (``dailymed_current_full_section``
  or ``rmc_section_text``), and it records section / section_num / set_id /
  effective_time / change_date. ``change_date`` = the RMC change month (month
  precision). ``property`` = the NORMALIZED section (warnings_and_precautions,
  dosage_and_administration, indications_and_usage, contraindications,
  boxed_warning, adverse_reactions, drug_interactions, ...).

* The Wayback "before" is DEMOTED to SUPPLEMENTARY provenance. When a clean
  archived prior section exists it is preserved in ``provenance``
  (``prior_section_text`` / ``prior_section_full`` + ``prior_snapshot_url`` /
  ``prior_snapshot_ts``) and mirrored into ``before.raw`` for continuity; when
  it does NOT (the ~47 no_pre_change_snapshot / section_not_found rows), the
  ``before`` carries an explicit sentinel and ``before.evidence.kind =
  "no_prior_snapshot"``. A missing, misaligned, or truncated before NO LONGER
  blocks inclusion or routes to review — the RMC fact stands alone. (The before
  is kept parseable only because the shared 'normalize' gate the runner
  prepends passes when both sides parse; it is never diffed and never gates.)

* A CUTOFF-ANCHORED CONTENT DELTA (owner decision 2026-07-24 (2), completed by
  (B)) isolates the ACTUAL new clause. The current section alone does NOT reveal
  WHAT changed for a MODIFIED section, so the fact could not generate a
  temporal-change question. The delta is FULL-vs-FULL at the SENTENCE level: the
  PRIOR side is the WHOLE section AS IT STOOD AT THE CUTOFF
  (``prior_cutoff_full_section``, frozen by ``stage1.tools.reanchor_dailymed_cutoff``
  and COMPLETED to the raised cap by ``refetch_current_sections`` where it had been
  reused at the old 6000 cap), and the CURRENT side is the WHOLE current section
  (``section_text`` = ``current_full_section``, the live DailyMed label at the
  raised cap — NOT the 1200-capped RMC snippet). Normalize both (strip
  cross-reference parentheticals, leading section/subsection numbers, collapse
  whitespace), split into sentences (>12 chars), and flag each current sentence
  whose best ``difflib.SequenceMatcher(autojunk=False)`` ratio against ALL prior
  sentences is < 0.85 AND is not a strict character-prefix of any prior sentence
  (the truncation/subset guard, KEPT as a cheap safety for a fallback truncated
  current) as GENUINELY NEW. The DELTA = that ordered new-sentence list. Being
  content-based AND whole-section it is robust to subsection RENUMBERING (a
  renumbered-but-unchanged subsection's prose still matches a prior sentence and is
  NOT flagged) and to the harvest→fetch label drift (both sides are DailyMed). Two
  truncation artifacts the FULL-vs-FULL recompute REMOVES: (a) the current-side
  1200-cap false NEGATIVE — new content beyond char 1200 is now visible; (b) the
  prior-side 6000-cap false POSITIVE — long-standing content past char 6000 of a
  truncated prior no longer looks new (verified: trametinib's ILD/Pneumonitis body
  sat past the old prior cap, so the earlier "new_subsection recovers the ILD
  warning" was a TRUNCATION ARTIFACT — the full prior at cutoff already contains
  it, and the honest delta is a small ``modified``). A ``modified`` whose sole
  candidate is boilerplate chopped at a fallback cap correctly falls to
  unchanged_or_reworded -> review. Each record is CLASSIFIED:
  ``new_subsection`` (all current sentences new), ``modified`` (some new),
  ``unchanged_or_reworded`` (no new sentence -> the delta gate routes it to
  review; a real RMC with no detectable new content is suspect), or
  ``delta_unavailable`` (no clean pre-cutoff prior -> the RMC fact stands as a
  whole-section change, flagged). A ``delta_unavailable`` record carries an EMPTY
  new-sentence list and EMPTY added_text: with no prior to difference against, no
  current sentence can be honestly claimed new, so the delta fields never present
  un-differenced long-standing boilerplate as "new content". The structured delta
  lives in ``provenance['delta']`` and NO LLM is involved (difflib/re only).

* A deterministic, human-readable CHANGE DESCRIPTOR
  (``provenance['change_descriptor']``) is added to every record for downstream
  question generation and the workbook: drug + section + §section_num +
  change_date. For a modified / new_subsection record its ``preview`` RENDERS THE
  DELTA (the new content), not the whole current section; for
  delta_unavailable / unchanged_or_reworded it falls back to the whole-section
  preview (``section_preview`` is always the whole-section preview and the full
  ``section_text`` is preserved). Where a clean Wayback change-month before
  exists, a legacy best-effort ``new_content_added`` flag (a word-level added
  span, via difflib on folded tokens) is also recorded — ADVISORY only, never
  gating; the cutoff-anchored delta is the authoritative signal.

value_type = "text_span". entity = {name: generic (fallback brand, else
set_id), ids: {set_id, brand?}}. predictability = "announced" (owner decision
B; purely additive Stage-2 metadata, never gates).

Gate order (first FAIL in ledger order names the disposition; EVERY gate always
runs; the runner PREPENDS the shared 'normalize' gate before these):

 1. temporal_window   (FDA-specific, PRECISION-AWARE, version fda_v2): the
                       change's MONTH interval vs [cutoff, asof]. Fully inside
                       -> pass; fully outside -> FAIL (excluded:temporal_window,
                       the genuine pre-cutoff / post-asof exclusions); a month
                       straddling a bound -> review (window_edge). v2 also
                       REVIEWS an in-window change whose after label is EFFECTIVE
                       before the cutoff (the changed text was then provably
                       public pre-cutoff, or the evidence is internally
                       inconsistent — the rivaroxaban 03/2026 vs effective
                       2026-01-16 case). First: cheapest scope check.
 2. fda_section_text  (FDA-specific): the RMC section_text (the recorded value)
                       must be non-empty after normalization -> pass; EMPTY ->
                       FAIL (excluded:fda_section_text — the genuine
                       empty-section-text exclusion: there is no value to state
                       the change). Carries the advisory new_content_added flag
                       and the section_text length in evidence.
 3. fda_delta         (FDA-specific, version fda_v2): the cutoff-anchored content
                       DELTA classification. unchanged_or_reworded -> REVIEW (a
                       real RMC with no detectable new content is suspect); a
                       TRUNCATED-fallback current section (the live DailyMed
                       full-section fetch was unavailable so ``after`` fell back to
                       the openFDA RMC section_text AT its 1200-char cap, cut
                       mid-sentence) -> REVIEW (v2, takes precedence over the
                       classification — a truncated value ships neither a complete
                       delta nor an honest whole-section value; refetch the label);
                       new_subsection / modified / delta_unavailable -> pass (the
                       last flagged as a whole-section fact). NEVER fails — the
                       delta is a quality/question-generation signal, not a scope
                       exclusion. After fda_section_text (non-empty first).
 4. evidence_resolvable(shared): both sides carry kind + (url or ref).
 5. fda_significance   (FDA-specific, DEFERRED/OPTIONAL): NOT configured ->
                       PASS-THROUGH for every record (the release ships all real
                       in-window changes with the filter dormant); configured ->
                       significant -> pass, not_significant -> fail
                       (excluded:fda_significance), unlabelled -> review. The
                       owner's future manual whitelist plugs in here as a
                       committed deterministic filter WITHOUT an LLM; its bytes /
                       resolved mapping are fingerprinted into the manifest.
 6. dedup              (shared, last): key (entity.name, property, change_date)
                       — the SAME tuple fact_id is built from (source constant),
                       so the included set is deduplicated BY fact_id. Two
                       subsections of one label collapse; two DIFFERENT labels of
                       the same generic (distinct set_ids) collapse — the first
                       fully-valid record is included and a differing-text
                       sibling routes to review (two possibly-distinct edits a
                       human collapses), never a silent drop.

NO fda_status / value_parsed / value_changed gate: those existed only to hold a
record whose archived before was missing, misaligned, or snippet-truncated, and
the reframe removes that dependence entirely. NO UniverseMembershipGate (FDA has
no fixed universe) and NO CorroborationGate (single authoritative source).

record_id discriminator: "{set_id}|{section_num}|{change_date}" — uniquely
identifies the physical change. fact_id is the SEMANTIC identity (source,
entity_name, property, change-month): two subsections of the same section
changing in the same month legitimately share a fact_id and are collapsed by
dedup.

Offline-only: the frozen caches are built once by the fetch tool; ``--online``
raises. Each present input (the history cache, its sidecar, and the read-only
RMC enrichment file) is fingerprinted into the manifest via
cfg['extra_input_files'] / cfg['extra_input_meta'] under a LOCATION-INDEPENDENT
identifier, so facts.jsonl is byte-identical across checkouts. A missing RMC
file degrades enrichment (section_text falls back to the history after_text;
known_top300/effective_time become None) but NEVER crashes and never drops a
candidate.
"""

from __future__ import annotations

import calendar
import difflib
import hashlib
import json
import re
import unicodedata
from datetime import date as _date
from pathlib import Path

import stage1.normalize.text_span  # noqa: F401  (registers the text_span comparator)
from stage1.adapters import Adapter
from stage1.gates import Gate
from stage1.predictability import ANNOUNCED, check_predictability
from stage1.gates.standard import (
    DedupGate,
    EvidenceResolvableGate,
)
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)

SOURCE = "fda"
VALUE_TYPE = "text_span"

# --------------------------------------------------------------------------- #
# Input files (data_dir vendored copy first, else the package/repo default).
# Identifiers recorded in provenance/manifest are LOCATION-INDEPENDENT.
# --------------------------------------------------------------------------- #
HISTORY_FILENAME = "fda_dailymed_history.jsonl"
HISTORY_META_FILENAME = "fda_dailymed_history.meta.json"
RMC_FILENAME = "fda_rmc_2026.jsonl"
# The FULL archived label per pinned Wayback capture — the novelty comparand for
# the Highlights extractor (built once by stage1.tools.fetch_prior_labels). See
# _highlights_prior() for why the section extract is not a valid substitute.
PRIOR_LABELS_FILENAME = "fda_prior_labels.jsonl"

PACKAGE_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent          # 1_temporal_facts/
# The FDA harvest inputs belong to the prior-work study, a SIBLING of 1_temporal_facts/.
DEFAULT_RMC_PATH = REPO_ROOT.parent / "0_prior_work" / "drugs" / RMC_FILENAME
PACKAGE_HISTORY_ID = f"stage1/cache/{HISTORY_FILENAME}"
PACKAGE_HISTORY_META_ID = f"stage1/cache/{HISTORY_META_FILENAME}"
PACKAGE_RMC_ID = "drugs/fda_rmc_2026.jsonl"
PACKAGE_PRIOR_LABELS_ID = f"stage1/cache/{PRIOR_LABELS_FILENAME}"

STATUS_OK = "ok"

# The ``before`` slot when no archived prior section exists. The reframe demotes
# the before to supplementary provenance; this sentinel keeps the record's
# ``before`` structurally valid and PARSEABLE (so the shared 'normalize' gate the
# runner prepends passes) WITHOUT fabricating a prior label section. It is never
# diffed and never gates — the RMC fact stands on ``after`` alone.
PRIOR_UNAVAILABLE_SENTINEL = "(no archived prior section available)"

# ctx keys (the runner copies cfg into ctx — the sanctioned channel).
RMC_INDEX_CTX_KEY = "fda_rmc_index"            # (set_id, section_num, change_date) -> rmc fields
PRIOR_LABELS_CTX_KEY = "fda_prior_labels"      # (set_id, snapshot_ts) -> {text, sha1, chars}
CACHE_INFO_CTX_KEY = "fda_cache_info"          # {history: {...}, rmc: {...}, prior_labels: {...}}
SIGNIFICANCE_LABELS_CTX_KEY = "fda_significance_labels"          # dict|path (owner input)
SIGNIFICANCE_RESOLVED_CTX_KEY = "fda_significance_labels_resolved"  # dict|None (loaded)
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUTS_CTX_KEY = "extra_input_files"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"
POLICY_ACTIVE_CTX_KEY = "policy"
LOADED_FLAG_CTX_KEY = "_fda_loaded"

# PREDICTABILITY tag (owner decision B): regulatory label changes are publicly
# disclosed -> the "announced" class. THE BORDERLINE CASE the owner deferred:
# a label change is disclosed the instant the label posts but is not
# forecastable the way a scheduled succession is; "announced" is the revisable
# default. Stage-2 stratification METADATA only — read by no gate.
PREDICTABILITY = ANNOUNCED

# Significance labels (owner's future manual whitelist).
SIG_SIGNIFICANT = "significant"
SIG_NOT_SIGNIFICANT = "not_significant"
# Location-independent identifier under which a significance-labels FILE is
# fingerprinted into the manifest (cfg['extra_input_files']/['extra_input_meta']),
# so an applied whitelist binds the released exclusion set to its exact bytes.
SIGNIFICANCE_INPUT_ID = "fda_significance_labels"

# SPL-section synonym fold: the FDA label taxonomy treats these older/short
# headings as the same section as the modern merged heading. Applied AFTER the
# generic lowercase/underscore normalization (which already folds the
# "Boxed warning"/"Boxed Warning" case difference).
_SECTION_ALIASES = {
    "warnings": "warnings_and_precautions",
    "indications": "indications_and_usage",
}

# The recorded value (RMC section_text) narrowed to a subsection frequently
# begins with the subsection numbers themselves (e.g. "5.6 , 2.4 )") — an
# artifact of the subsection extraction, not prose. This strips exactly that
# leading numeric run for a clean human-readable preview; it never strips a
# leading dose like "2 mg" (which is not a "<int>.<int>" subsection token).
_LEADING_SUBSEC_RE = re.compile(r"^\s*\d+\.\d+(?:\s*,\s*[\d.]+)*\s*\)?\s*")
# leading SPL section header like "4 CONTRAINDICATIONS" / "5 WARNINGS AND
# PRECAUTIONS" / "1 INDICATIONS AND USAGE": a section number then a KNOWN
# section title. Anchored to the fixed SPL title set (not a greedy caps run) so
# a following all-caps brand (e.g. "4 CONTRAINDICATIONS TZIELD ...") is kept,
# and a genuine caps opener without a number ("WARNING: LACTIC ACIDOSIS") is kept.
_SPL_TITLES = (
    "INDICATIONS AND USAGE", "DOSAGE AND ADMINISTRATION",
    "DOSAGE FORMS AND STRENGTHS", "CONTRAINDICATIONS",
    "WARNINGS AND PRECAUTIONS", "ADVERSE REACTIONS", "DRUG INTERACTIONS",
    "USE IN SPECIFIC POPULATIONS", "OVERDOSAGE", "BOXED WARNING",
)
_LEADING_SECTION_HDR_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s+(?:" + "|".join(_SPL_TITLES) + r")\s*[:•]?\s*")
_PREVIEW_CAP = 220

# --------------------------------------------------------------------------- #
# Content-based cutoff-anchored DELTA (owner decision 2026-07-24 (2)).
#
# The RMC section_text (the "after") is the CURRENT content of a changed section
# — for a MODIFIED section (e.g. metformin's long-standing lactic-acidosis boxed
# warning) it does NOT reveal WHAT is new. The delta isolates the ACTUAL new
# clause deterministically by comparing the CUTOFF-ANCHORED prior FULL section
# (frozen into the cache as prior_cutoff_full_section) against the current
# section_text at the SENTENCE level:
#
#   1. NORMALIZE both: strip cross-reference parentheticals ("(5.1)" /
#      "( 2.4 , 7.1 )"), strip a leading section header / subsection-number
#      artifact, collapse whitespace.
#   2. SPLIT into sentences with the fixed regex below, keeping sentences longer
#      than 12 chars.
#   3. For EACH current(after) sentence, compute its best difflib
#      SequenceMatcher(autojunk=False) ratio against ALL prior sentences; a
#      current sentence whose best match is < 0.85 is a candidate NEW sentence.
#   4. TRUNCATION / SUBSET GUARD (_is_prior_prefix): drop a candidate that is a
#      STRICT character-prefix of any prior sentence. The current section_text is
#      captured at a LENGTH CAP (openFDA's 1200-char RMC snippet), so its trailing
#      sentence is frequently a prior sentence chopped mid-sentence — its ratio
#      against the FULL prior sentence dips below 0.85 and the plain test (3) would
#      falsely flag long-standing boilerplate as new (metformin's lactic-acidosis
#      boxed warning was the flagship false-positive: '… in these high risk groups
#      are' is the prior '… are provided [see …].' cut at char 1200). A leading
#      substring of prior content is old, not new — this guard removes exactly
#      that class (also the splitter's run-on case where a bare numeric subsection
#      marker leaves a prior sentence unsplit) and NEVER suppresses a real new
#      clause (which diverges from, or extends beyond, any prior sentence).
#
# The DELTA = the ordered list of the surviving new sentences. Because it is
# CONTENT-based (not number/position-based) it is robust to FDA subsection
# RENUMBERING: validated on real records — it recovered trametinib's whole new
# ILD/Pneumonitis warning under a §5.6 renumber and canagliflozin's missed-dose
# guidance; a truncation-only 'modified' like metformin's (whose sole candidate
# new sentence is the boilerplate cut at the 1200-char cap — the genuinely new
# text, if any, lies beyond the cap and is invisible) correctly falls to
# unchanged_or_reworded -> REVIEW, never shipping old boilerplate as new.
# stdlib only (difflib/re); NO LLM anywhere; a pure function of the frozen cache.
# --------------------------------------------------------------------------- #
DELTA_THRESHOLD = 0.85
_MIN_SENTENCE_LEN = 12
# openFDA HARD-CAPS the RMC ``section_text`` at exactly 1200 chars. When the live
# DailyMed full-section fetch is unavailable the ``after`` falls back to that RMC
# snippet; a fallback value AT this cap is provably TRUNCATED (cut mid-sentence),
# so it can present neither a complete delta nor an honest whole-section value —
# the delta gate routes such a record to review rather than ship a truncated value.
RMC_SECTION_TEXT_CAP = 1200
# Cross-reference parentheticals: "(5.1)", "( 2.4 , 7.1 )", "(6.1)" etc.
_XREF_PAREN_RE = re.compile(r"\(\s*\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*\s*\)")
# A bare leading section/subsection number left after the header/subsec strips.
_LEADING_NUM_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*")
# The validated sentence splitter (owner decision 2026-07-24 (2)): break after a
# '.' or ':' followed by whitespace and a capital / '(', OR on a bullet.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.:])\s+(?=[A-Z(])|\s*•\s*")

# Delta classifications.
CLASS_NEW_SUBSECTION = "new_subsection"        # essentially all current sentences are new
CLASS_MODIFIED = "modified"                    # some current sentences are new
CLASS_UNCHANGED = "unchanged_or_reworded"      # no new sentences -> route to review (suspect)
CLASS_DELTA_UNAVAILABLE = "delta_unavailable"  # no clean pre-cutoff prior -> whole-section fact


def normalize_for_delta(text) -> str:
    """Normalize a section's text for sentence-level delta comparison: strip
    cross-reference parentheticals, collapse whitespace, then strip a leading SPL
    section header / subsection-number artifact and a bare leading number. Pure
    and total."""
    if not isinstance(text, str):
        return ""
    t = _XREF_PAREN_RE.sub(" ", text)
    t = " ".join(t.split())
    t = _LEADING_SECTION_HDR_RE.sub("", t)
    t = _LEADING_SUBSEC_RE.sub("", t)
    t = _LEADING_NUM_RE.sub("", t)
    return " ".join(t.split())


def split_sentences(text) -> list:
    """Split normalized section text into sentences longer than 12 chars, using
    the validated fixed splitter. Pure and total."""
    if not isinstance(text, str):
        return []
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(text)
            if p and len(p.strip()) > _MIN_SENTENCE_LEN]


def _is_prior_prefix(sentence: str, prior_sents: list) -> bool:
    """True iff ``sentence`` is a STRICT character-prefix of some prior sentence
    — its full text already appears, verbatim, at the START of prior content, so
    it carries NO information the prior did not. Such a current sentence is NOT
    genuinely new, for either of two mechanical reasons:

    * TRUNCATION: the current section_text is captured at a length cap (openFDA's
      1200-char RMC snippet), so the TRAILING sentence is frequently a prior
      sentence cut mid-sentence — e.g. metformin's boxed-warning boilerplate
      'Steps to reduce the risk of and manage metformin-associated lactic acidosis
      in these high risk groups are' is the full prior sentence '… are provided
      [see …].' chopped at char 1200. Its SequenceMatcher ratio against the FULL
      prior sentence falls below 0.85 (2·len(cut)/(len(cut)+len(full)) is small
      when the full sentence is >1.35× longer), so the plain ratio test falsely
      flags long-standing boilerplate as NEW.
    * SUBSET / RUN-ON: a current sentence that is a leading substring of a prior
      sentence (including a prior sentence the fixed splitter left as a run-on
      because a bare numeric subsection marker like '… established. 2.2 …' is not
      a split point) is old content, not a new clause.

    A strict prefix is unambiguous — every character of ``sentence`` matches the
    start of a longer prior sentence — so this NEVER suppresses genuinely new
    content (a real new clause diverges from, or extends beyond, any prior
    sentence). It is a pure, total, deterministic subset check (no difflib, no
    LLM); when the visible prefix is identical to old content we decline to claim
    it is new (erring toward review, never fabricating a new-content claim)."""
    return any(len(sentence) < len(ps) and ps.startswith(sentence)
               for ps in prior_sents)


def compute_content_delta(prior_full_section, current_section_text,
                          threshold: float = DELTA_THRESHOLD):
    """(new_sentences, current_sentences, prior_sentences). A current(after)
    sentence is GENUINELY NEW iff (a) its best SequenceMatcher(autojunk=False)
    ratio against ALL prior sentences is < ``threshold`` (0.85) AND (b) it is not
    a STRICT character-prefix of any prior sentence (the truncation / subset guard
    — see ``_is_prior_prefix``: the current section_text is length-capped, so its
    trailing sentence is often a prior sentence cut mid-sentence, and a leading
    substring of prior content is old, not new). The DELTA is the ordered list of
    those new sentences. Deterministic — a pure function of the two frozen texts
    (difflib/re only, no LLM)."""
    prior_sents = split_sentences(normalize_for_delta(prior_full_section))
    current_sents = split_sentences(normalize_for_delta(current_section_text))
    new_sents = _new_sentences(current_sents, prior_sents, threshold)
    return new_sents, current_sents, prior_sents


def _new_sentences(current_sents: list, prior_sents: list,
                   threshold: float = DELTA_THRESHOLD) -> list:
    """The ordered current sentences that are GENUINELY NEW: best
    SequenceMatcher(autojunk=False) ratio against ALL prior sentences < threshold
    AND not a strict prior-prefix (the truncation/subset guard). Factored out so
    the whole-section delta and the subsection-aware delta share ONE new-detection
    rule (byte-identical). A current sentence is NEW iff NO prior sentence matches
    it at >= threshold, so we EARLY-EXIT the instant a prior sentence clears the
    threshold and skip the O(L^2) ``ratio()`` whenever difflib's cheap UPPER BOUNDS
    (``real_quick_ratio``/``quick_ratio``, both >= ``ratio``) already fall below it
    — a pure speed optimization, identical result to the exhaustive pairwise scan."""
    new_sents = []
    for cs in current_sents:
        sm = difflib.SequenceMatcher(autojunk=False, a=cs)
        is_new = True
        for ps in prior_sents:
            sm.set_seq2(ps)
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            if sm.ratio() >= threshold:
                is_new = False
                break
        if is_new and not _is_prior_prefix(cs, prior_sents):
            new_sents.append(cs)
    return new_sents


def classify_delta(prior_available: bool, new_sents: list, current_sents: list) -> str:
    """Classify the delta:

    * no clean pre-cutoff prior (or no comparable current sentences) ->
      ``delta_unavailable`` (the RMC fact still stands as a whole-section change);
    * no new sentences -> ``unchanged_or_reworded`` (a real RMC with no detectable
      new content is suspect -> the delta gate routes it to review);
    * every current sentence new -> ``new_subsection``;
    * some current sentences new -> ``modified``."""
    if not prior_available or not current_sents:
        return CLASS_DELTA_UNAVAILABLE
    if not new_sents:
        return CLASS_UNCHANGED
    if len(new_sents) == len(current_sents):
        return CLASS_NEW_SUBSECTION
    return CLASS_MODIFIED


# --------------------------------------------------------------------------- #
# SUBSECTION-AWARE isolation (owner decision 2026-07-24 (C)).
#
# The whole-section sentence delta above turns a MODIFIED prose section into a
# WALL: an FDA label routinely INSERTS one new subsection (e.g. Dextrose's new
# "5.1 Neonatal Hypoglycemia") and RENUMBERS + lightly REWORDS every following
# subsection ("dextrose infusions" -> "Dextrose Injection (5% and 10%)"). Each
# reworded sentence drops below the 0.85 match and floods the delta with old
# content re-flagged as new. The isolation below fixes that by working at the
# SUBSECTION level:
#
#   * TABLE-dense sections (dosage / dosage-forms / drug-interactions) are dosing
#     and interaction GRIDS that serialise into cell fragments no deterministic
#     diff can reduce to "the change is Y" -> routed WHOLESALE to review, never an
#     isolated included fact.
#   * A prose section is split into its numbered subsections (SECTION-SCOPED: only
#     the section's own integer, e.g. 5.x for warnings, so a stray "0.6"/"8.8" in
#     prose is not mistaken for a header). Each CURRENT subsection is matched to
#     its best-similarity PRIOR subsection (content-based, so renumbering is
#     invisible): >=0.85 -> unchanged renumber (emit nothing); a genuinely NEW
#     subsection (best match < 0.45 AND no prior title match) -> emit its opening
#     sentence(s) as "new_subsection: <the new warning>"; otherwise the subsection
#     is a reworded/expanded MODIFIED and we sentence-diff it against the WHOLE
#     prior section (so content merely MOVED between subsections is not called new).
#   * A section with no numbered subsections (boxed warning) falls to the plain
#     whole-section sentence diff.
#
# The isolated change is INCLUDABLE only when it is small and prose-shaped
# (<= 6 changes, not table-fragment-dominated). Otherwise -> review:
#     unchanged_or_reworded (0 new) / restructured_wall (> 6) / table_shaped
#     (isolated change is serialised table cells) / delta_unavailable (no prior)
#     / table_section (dosage-family, wholesale). stdlib only; NO LLM.
# --------------------------------------------------------------------------- #
# Canonical FDA section number (the integer prefix of a section's subsections).
SECTION_NUM = {
    "indications_and_usage": 1,
    "dosage_and_administration": 2,
    "dosage_forms_and_strengths": 3,
    "contraindications": 4,
    "warnings_and_precautions": 5,
    "adverse_reactions": 6,
    "drug_interactions": 7,
    "use_in_specific_populations": 8,
    "boxed_warning": None,
}
# Table-dense sections -> review wholesale (a dosing/interaction grid has no
# prose "the change is Y").
TABLE_SECTIONS = frozenset({
    "dosage_and_administration", "dosage_forms_and_strengths", "drug_interactions",
})
SUBSEC_T_NEW = 0.45         # best whole-subsection ratio below which (no title hit) -> NEW
SUBSEC_T_SAME = 0.85        # at/above -> unchanged renumber
SUBSEC_TITLE_MATCH = 0.72   # prior-title similarity that reclassifies a low-content-match subsection as reworded
SUBSEC_TITLE_CHARS = 42
# A clean FDA fact is SHORT and about ONE thing. More than MAX_CLEAN_CHANGES
# separate new clauses (several new indications/warnings at once), or an isolated
# change longer than MAX_CLEAN_ADDED_CHARS, is a multi-item / verbose WALL that a
# reader cannot state as a single change (e.g. KEYTRUDA QLEX gained six indications
# across RCC/TNBC/ovarian in one revision — a ~1,600-char block) -> review.
MAX_CLEAN_CHANGES = 3       # > this many isolated changes -> restructured wall -> review
MAX_CLEAN_ADDED_CHARS = 450  # isolated change longer than this -> restructured wall -> review
_TABLEISH_MAX_LEN = 45      # a change fragment shorter than this (or ending ':') is a table cell

# Additional classifications (join CLASS_NEW_SUBSECTION / CLASS_MODIFIED / etc.).
CLASS_TABLE_SECTION = "table_section"      # dosage-family -> review wholesale
CLASS_TABLE_SHAPED = "table_shaped"        # isolated change is serialised table cells -> review
CLASS_WALL = "restructured_wall"           # > MAX_CLEAN_CHANGES isolated changes -> review

# A current subsection header, scoped to the section's own integer at format time.
_SUBSEC_HDR_TMPL = r"(?<![(\d.])\b(%d)\.(\d{1,2})\s+(?=[A-Z])"
# [see ...] cross-references, stripped before subsection splitting so their inner
# numbers can't be read as headers.
_SEE_XREF_RE = re.compile(r"\[see[^\]]*\]", re.IGNORECASE)
# A trailing next-section header that leaked into a section's text ("... 6 ADVERSE
# REACTIONS ..."), stripped from a rendered change.
_TRAILING_SECTION_HDR_RE = re.compile(
    r"\s+\d+\s+(?:" + "|".join(_SPL_TITLES) + r")\b.*$", re.IGNORECASE)


def _strip_subsec_num(chunk: str) -> str:
    return _LEADING_SUBSEC_RE.sub("", chunk) if isinstance(chunk, str) else ""


def _subsec_title(chunk: str) -> str:
    return _strip_subsec_num(chunk)[:SUBSEC_TITLE_CHARS]


def split_subsections(text, sec_int) -> list:
    """Split a section into (subsection_num, chunk_text) at SECTION-SCOPED numbered
    headers (only the section's own integer). Returns a single (None, whole_text)
    chunk when the section has no numbered subsections. Pure and total."""
    if not isinstance(text, str):
        return []
    t = _SEE_XREF_RE.sub(" ", text)
    t = _XREF_PAREN_RE.sub(" ", t)
    if sec_int is None:
        return [(None, " ".join(t.split()))]
    marks = list(re.compile(_SUBSEC_HDR_TMPL % sec_int).finditer(t))
    if len(marks) < 2:
        return [(None, " ".join(t.split()))]
    chunks = []
    if marks[0].start() > 0:
        pre = " ".join(t[:marks[0].start()].split())
        if pre:
            chunks.append((None, pre))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
        chunks.append((f"{m.group(1)}.{m.group(2)}", " ".join(t[m.start():end].split())))
    return chunks


def _first_change_sentences(chunk: str, cap: int = 240) -> str:
    """Descriptor for a NEW subsection: leading number stripped, first sentence(s)
    up to ``cap`` chars (the title runs into the opening sentence in SPL text, so
    this yields 'Neonatal Hypoglycemia Neonates … are at increased risk …')."""
    body = _strip_subsec_num(chunk)
    out = ""
    for s in re.split(r"(?<=[.;])\s+(?=[A-Z0-9])", body):
        if len(out) + len(s) > cap and out:
            break
        out = (out + " " + s).strip()
    return out[:cap]


def _polish_change(text) -> str:
    """Clean a rendered change: strip a leading section header ('5 WARNINGS AND
    PRECAUTIONS') / subsection number ('5.12'), and a trailing next-section header
    ('... 6 ADVERSE REACTIONS ...') that leaked from the raw section text. Pure
    and total."""
    if not isinstance(text, str):
        return ""
    t = _LEADING_SECTION_HDR_RE.sub("", text)
    t = _LEADING_SUBSEC_RE.sub("", t)
    # a leftover leading close-paren / colon / punctuation from a highlights marker
    # ("2.1 ): 500 mg …" -> ": 500 mg …" -> "500 mg …")
    t = re.sub(r"^[\s):;,.•○-]+", "", t)
    t = _TRAILING_SECTION_HDR_RE.sub("", t)
    return " ".join(t.split())


def _is_tableish(fragment: str) -> bool:
    """A change fragment is a serialised TABLE CELL if it is short or is a bare
    column label ending in ':'."""
    t = (fragment or "").strip()
    return len(t) < _TABLEISH_MAX_LEN or t.endswith(":")


def compute_subsection_delta(prior_full, current_full, sec_int, prop):
    """Isolate the ACTUAL new content of a section. Returns
    (changes, classification, added_text):

    * ``changes`` — ordered list of {kind: 'new_subsection'|'new_clause',
      subsection, text} (each ``text`` polished);
    * ``classification`` — one of table_section / table_shaped / restructured_wall
      / unchanged_or_reworded / new_subsection / modified;
    * ``added_text`` — the joined polished change text for an includable
      (new_subsection/modified) record, else ''.

    Deterministic, stdlib only (difflib/re), NO LLM."""
    if prop in TABLE_SECTIONS:
        return [], CLASS_TABLE_SECTION, ""
    cur_chunks = split_subsections(current_full or "", sec_int)
    all_prior = split_sentences(normalize_for_delta(prior_full or ""))
    changes = []
    if len(cur_chunks) < 2:
        cur_sents = split_sentences(normalize_for_delta(current_full or ""))
        for s in _new_sentences(cur_sents, all_prior):
            changes.append({"kind": "new_clause", "subsection": None,
                            "text": _polish_change(s), "source": "body_section"})
    else:
        prior_chunks = split_subsections(prior_full or "", sec_int)
        pri_norm = [_strip_subsec_num(c[1]) for c in prior_chunks]
        pri_titles = [_subsec_title(c[1]) for c in prior_chunks]
        for num, ctext in cur_chunks:
            cnorm = _strip_subsec_num(ctext)
            # best whole-subsection similarity to any prior subsection. EARLY-EXIT
            # the instant a prior subsection clears SUBSEC_T_SAME (the common case:
            # this current subsection is an unchanged renumber), and skip ratio()
            # whenever difflib's cheap upper bounds already fall below the running
            # best — a pure speed optimization (the classification thresholds see
            # the same best_r as the exhaustive scan whenever it matters).
            best_r = 0.0
            sm = difflib.SequenceMatcher(autojunk=False, b=cnorm)
            for pn in pri_norm:
                sm.set_seq1(pn)
                if sm.real_quick_ratio() <= best_r or sm.quick_ratio() <= best_r:
                    continue
                r = sm.ratio()
                if r > best_r:
                    best_r = r
                    if best_r >= SUBSEC_T_SAME:
                        break
            title_hit = max((difflib.SequenceMatcher(None, _subsec_title(ctext), pt).ratio()
                             for pt in pri_titles), default=0.0)
            if best_r >= SUBSEC_T_SAME:
                continue  # unchanged, only renumbered
            if best_r < SUBSEC_T_NEW and title_hit < SUBSEC_TITLE_MATCH:
                changes.append({"kind": "new_subsection", "subsection": num,
                                "text": _polish_change(_first_change_sentences(ctext)),
                                "source": "body_section"})
            else:
                cur_sents = split_sentences(normalize_for_delta(ctext))
                for s in _new_sentences(cur_sents, all_prior):
                    changes.append({"kind": "new_clause", "subsection": num,
                                    "text": _polish_change(s), "source": "body_section"})
    # Drop changes whose polished text collapsed to a bare header / stub (a section
    # header like '5 WARNINGS AND PRECAUTIONS' alone carries no new clause).
    changes = [c for c in changes if len(c["text"]) > _MIN_SENTENCE_LEN]
    # HONESTY guard (shared with the highlights path): a change is genuinely new
    # only if its normalized text does NOT already appear VERBATIM as a substring of
    # the prior body. The sentence-level 0.85 test and the prefix guard can miss
    # content that exists in the prior across a different sentence boundary or under
    # a renumbered subsection (e.g. a contraindication the prior stated as
    # "...contraindicated in patients who have demonstrated hypersensitivity to X"
    # vs the current terse "Hypersensitivity to X"), so we drop any change whose
    # folded text is a substring of the folded prior body.
    prior_fold = _fold_lower(prior_full or "")
    changes = [c for c in changes if _fold_lower(c["text"]) not in prior_fold]
    if not changes:
        return [], CLASS_UNCHANGED, ""
    frac = sum(1 for c in changes if _is_tableish(c["text"])) / len(changes)
    if len(changes) >= 3 and frac > 0.6:
        return changes, CLASS_TABLE_SHAPED, ""
    added_text = " ".join(c["text"] for c in changes)
    # A clean fact is SHORT and about ONE thing: too many separate new clauses, or
    # an over-long isolated change, is a multi-item / verbose wall -> review.
    if len(changes) > MAX_CLEAN_CHANGES or len(added_text) > MAX_CLEAN_ADDED_CHARS:
        return changes, CLASS_WALL, ""
    kind = (CLASS_NEW_SUBSECTION if all(c["kind"] == "new_subsection" for c in changes)
            else CLASS_MODIFIED)
    return changes, kind, added_text


# --------------------------------------------------------------------------- #
# HIGHLIGHTS delta (owner decision 2026-07-24 (D)) — a SECOND, complementary
# extractor. openFDA's RMC ``section_text`` is the CONCISE *Highlights of
# Prescribing Information* version of the changed section (a curated bulleted
# summary), where many changes read as one short line — e.g. eplontersen's "The
# prefilled syringe must be administered by a healthcare provider." (78 chars),
# which the full-BODY diff walls (the §2 body was reorganised). This path
# recovers those. It is COMPLEMENTARY, not a replacement: the body diff catches
# body-detail changes the terse highlights omit (a new contraindication item, a
# new indication paragraph), and highlights catch concise-summary changes the
# body walls. A record is included by whichever yields a clean SHORT SINGLE change
# (derivation recorded); when both fire the body is primary.
#
# HONESTY: being *in* the highlights does NOT make something new — Highlights is a
# permanent summary of the WHOLE label, rewritten whenever a section is revised,
# so an item can be PROMOTED from the body into the highlights without being new
# (measured: ~9% of candidate-new highlights lines). Novelty is therefore
# confirmed against the prior FULL BODY: a highlights line counts only if its
# genuinely-new added word-run is ABSENT (as a normalized substring) from the
# entire prior body. Boilerplate cross-references ("see full prescribing
# information …") are filtered. stdlib only; NO LLM.
# --------------------------------------------------------------------------- #
HL_MAX_SENTENCE_CHARS = 300   # a highlights line longer than this is not a single crisp statement
_HL_MIN_ADDED_WORDS = 4
# An added run must carry actual PROSE, not just section cross-reference tokens.
# A Highlights line ends in a cross-reference like "( 2.3 , 2.4 , 5.1 )", and a
# diff can isolate that punctuation+digits tail as its own "new" run — four
# tokens, no clinical content, unquotable as a fact. Require this many alphabetic
# words in the run before it can certify a change.
_HL_MIN_ALPHA_WORDS = 3
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# Highlights boilerplate cross-references / reference lines — not clinical changes.
_HL_BOILER_RE = re.compile(
    r"see full prescribing information|see dosage and administration|see warnings|"
    r"see contraindications|see adverse reactions|see use in specific|see clinical|"
    r"for important (?:preparation|administration)|prescribing information for|"
    r"full prescribing information for|fda-authorized test|information on fda|"
    r"fda\.gov|companion diagnostic|to report suspected adverse|medwatch", re.IGNORECASE)


def _fold_lower(text) -> str:
    return " ".join(normalize_for_delta(text).lower().split())


def _longest_added_run(cur_sentence: str, prior_sentence: str):
    """The longest contiguous run of >= _HL_MIN_ADDED_WORDS words present in
    ``cur_sentence`` but not in ``prior_sentence`` (word-level difflib). None if
    no such run. Deterministic."""
    cw, pw = cur_sentence.split(), prior_sentence.split()
    sm = difflib.SequenceMatcher(None, pw, cw, autojunk=False)
    runs = [cw[j1:j2] for tag, _i1, _i2, j1, j2 in sm.get_opcodes()
            if tag in ("insert", "replace") and (j2 - j1) >= _HL_MIN_ADDED_WORDS]
    return max(runs, key=len) if runs else None


def compute_highlights_delta(highlights_text, prior_full_section, section_num):
    """Isolate a crisp SHORT change from the openFDA highlights (``rmc_section_text``)
    against the cutoff prior BODY. Returns (changes, classification, added_text):

    * a highlights line qualifies only if it is short (<= HL_MAX_SENTENCE_CHARS),
      not boilerplate, does NOT match any prior-body sentence at >= 0.85, and its
      longest genuinely-new word-run is ABSENT from the entire prior body (the
      promotion / reformat guard);
    * the change ``text`` is the VERBATIM highlights line (so it is quotable and
      findable in the label's Highlights section); ``added_phrase`` records the
      specific new run; ``source`` = 'highlights';
    * classification is CLASS_MODIFIED when >=1 clean line survives (subject to the
      same short-and-single bounds), else a review class.

    Deterministic, stdlib only, NO LLM."""
    if not isinstance(highlights_text, str) or not isinstance(prior_full_section, str):
        return [], CLASS_UNCHANGED, ""
    prior_body_fold = _fold_lower(prior_full_section)
    prior_sents = split_sentences(normalize_for_delta(prior_full_section))
    changes = []
    for cs in split_sentences(normalize_for_delta(highlights_text)):
        polished = _polish_change(cs)
        if len(polished) < _MIN_SENTENCE_LEN or len(polished) > HL_MAX_SENTENCE_CHARS:
            continue
        if _HL_BOILER_RE.search(polished):
            continue
        # An incomplete lead-in header ('CRESTOR is a statin indicated:', 'Administer
        # continuously or intermittently:') ends in a colon — the actual content
        # follows as bullets we don't have here; not a self-contained change.
        if polished.rstrip().endswith(":") or "○" in polished or "•" in polished:
            continue
        # best-matching prior body sentence
        best_r, best_ps = 0.0, ""
        for ps in prior_sents:
            r = difflib.SequenceMatcher(None, cs, ps, autojunk=False).ratio()
            if r > best_r:
                best_r, best_ps = r, ps
        if best_r >= DELTA_THRESHOLD:
            continue  # this highlights line already appears in the prior body
        run = _longest_added_run(cs, best_ps)
        if not run:
            continue
        phrase = _fold_lower(" ".join(run))
        if len(phrase) < _MIN_SENTENCE_LEN or _HL_BOILER_RE.search(phrase):
            continue
        # Cross-reference tails ("( 2.3 , 2.4 , 5.1 )") diff as a word-run but
        # state nothing; a change must be quotable prose.
        if len(_ALPHA_WORD_RE.findall(" ".join(run))) < _HL_MIN_ALPHA_WORDS:
            continue
        if phrase in prior_body_fold:
            continue  # PROMOTION / reformat: the new run already exists in the prior body
        changes.append({"kind": "highlights_addition", "subsection": section_num,
                        "text": polished, "source": "highlights",
                        "source_sentence": polished, "added_phrase": " ".join(run)})
    if not changes:
        return [], CLASS_UNCHANGED, ""
    added_text = " ".join(c["text"] for c in changes)
    if len(changes) > MAX_CLEAN_CHANGES or len(added_text) > MAX_CLEAN_ADDED_CHARS:
        return changes, CLASS_WALL, ""
    return changes, CLASS_MODIFIED, added_text


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def normalize_section(section) -> str:
    """Canonical property name for an SPL section title. Pure and total.

    Lowercase, every run of non-alphanumerics -> '_', trimmed, then a tiny
    known-synonym fold. A missing/empty/non-string section -> 'unknown_section'
    (kept a valid non-empty property so the record still validates)."""
    if not isinstance(section, str) or not section.strip():
        return "unknown_section"
    base = re.sub(r"[^a-z0-9]+", "_", section.strip().lower()).strip("_")
    if not base:
        return "unknown_section"
    return _SECTION_ALIASES.get(base, base)


def _fold(text) -> str:
    """NFKD accent-fold, lowercase, non-alphanumerics -> single spaces,
    collapsed — the SAME normalization the text_span comparator applies to its
    canonical 'folded'. Used for the section_text presence check and the
    advisory word-level added-span computation."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    spaced = "".join(ch if ch.isalnum() else " " for ch in stripped)
    return " ".join(spaced.split())


def clean_preview(section_text, cap: int = _PREVIEW_CAP) -> str:
    """A deterministic, human-readable preview of the changed section's current
    text: whitespace collapsed, the leading subsection-number artifact stripped,
    truncated at a word boundary with an ellipsis. Pure and total."""
    if not isinstance(section_text, str):
        return ""
    collapsed = " ".join(section_text.split())
    body = _LEADING_SECTION_HDR_RE.sub("", collapsed)
    body = _LEADING_SUBSEC_RE.sub("", body).strip()
    if not body:
        body = collapsed  # stripping emptied it (all-numeric) -> keep the collapsed text
    if len(body) <= cap:
        return body
    head = body[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    if not head:
        head = body[:cap]
    return head + "…"


def new_content_added(prior_text, section_text):
    """Best-effort ADVISORY signal, computed only when a clean archived prior
    section exists: does the current section_text contain word-level content the
    prior section did not? Returns (flag, added_word_count) or (None, None) when
    no clean prior exists (never blocking, never fabricated). Deterministic —
    difflib on the folded token lists."""
    if not isinstance(prior_text, str) or not prior_text.strip():
        return None, None
    before_tokens = _fold(prior_text).split()
    after_tokens = _fold(section_text).split()
    if not before_tokens or not after_tokens:
        return None, None
    matcher = difflib.SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)
    added = 0
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added += (j2 - j1)
    return (added > 0), added


_CHANGE_DATE_RE = re.compile(r"^(\d{2})/(\d{4})$")


def parse_change_month(change_date):
    """Parse 'MM/YYYY' -> ('YYYY-MM-01', 'month'). Raises ValueError on a
    fundamentally malformed value (never occurs on the real cache — all 318
    rows are clean MM/YYYY; a violation is a data-shape bug logged in the
    manifest's build_errors, not a silent drop)."""
    if not isinstance(change_date, str):
        raise ValueError(f"change_date is not a string: {type(change_date).__name__}")
    m = _CHANGE_DATE_RE.match(change_date.strip())
    if not m:
        raise ValueError(f"change_date is not 'MM/YYYY': {change_date!r}")
    month, year = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        raise ValueError(f"change_date month out of range: {change_date!r}")
    return f"{year:04d}-{month:02d}-01", "month"


def _effective_iso(effective_time):
    """RMC effective_time 'YYYYMMDD' -> 'YYYY-MM-DD' when it is a real date,
    else the raw string (or None). Pure and total."""
    if not isinstance(effective_time, str) or not effective_time:
        return None
    if re.fullmatch(r"\d{8}", effective_time):
        try:
            return _date(
                int(effective_time[0:4]), int(effective_time[4:6]), int(effective_time[6:8])
            ).isoformat()
        except ValueError:
            return effective_time
    return effective_time


def _dailymed_current_url(set_id) -> str | None:
    """Stable public URL of the CURRENT DailyMed label for a set_id."""
    if not isinstance(set_id, str) or not set_id:
        return None
    return f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"


def _month_interval(iso_value: str, precision: str):
    """The [start, end] date interval a (value, precision) change_date covers.
    month -> the whole calendar month; year -> the whole year; day/other ->
    the single day. Returns (start, end) dates or (None, reason)."""
    try:
        anchor = _date.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return None, f"change_date value is not 'YYYY-MM-DD': {iso_value!r}"
    if precision == "month":
        last = calendar.monthrange(anchor.year, anchor.month)[1]
        return (_date(anchor.year, anchor.month, 1), _date(anchor.year, anchor.month, last)), None
    if precision == "year":
        return (_date(anchor.year, 1, 1), _date(anchor.year, 12, 31)), None
    return (anchor, anchor), None


def _coerce_date(value):
    """(date, None) for a date or 'YYYY-MM-DD' string, else (None, reason)."""
    if isinstance(value, _date):
        return value, None
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value), None
        except ValueError:
            return None, f"is not a valid 'YYYY-MM-DD' date: {value!r}"
    return None, f"is not a date or 'YYYY-MM-DD' string: {type(value).__name__}"


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha1_json(obj) -> str:
    """sha1 over a canonical JSON encoding (sorted keys, compact, ascii) of an
    object — a byte-stable fingerprint of a resolved mapping regardless of the
    source it came from. Used to bind the released exclusion set to the exact
    significance-label mapping that produced it."""
    payload = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# FDA-specific gates
# --------------------------------------------------------------------------- #
class FdaTemporalWindowGate(Gate):
    """Precision-aware temporal window for a MONTH-precision change_date.

    The change covers a whole month; the shared TemporalWindowGate pins it to
    one day and would silently INCLUDE a month straddling an edge. This gate
    compares the change's month INTERVAL to [cutoff, asof]:

    * whole month inside [cutoff, asof]        -> pass;
    * whole month entirely outside            -> fail (out of window);
    * month straddles a bound (cannot resolve strictly inside)
                                              -> review (window_edge) — NEVER a
      silent include.

    A missing/unparseable date or missing bound is review. Kept under the
    shared gate NAME 'temporal_window' (version fda_v2), like the sports/people
    precision-aware temporal gates.

    v2 adds an EVIDENCE-CONSISTENCY guard on the would-pass path: the pinned
    'after' label's own ``effective_time`` (the date the label containing the
    changed text became effective) must not PREDATE the cutoff. If it does,
    the row is unsafe either way — if effective_time is right, the changed
    text was live in a pre-cutoff label (public inside the model-knowledge
    window: a contamination, whatever the RMC month claims); if the RMC month
    is right, the pinned after evidence is internally inconsistent. The two
    sources contradict each other, so this is ``review`` (contradictory
    evidence a human must resolve), never a silent include. Verified case:
    rivaroxaban's 03/2026 RMC change whose after label is effective
    2026-01-16 — the only such row on the frozen cache. An effective_time
    inside the window, or one merely earlier than the RMC month but still
    post-cutoff, does not trigger (no contamination threat); a missing /
    dateless effective_time contributes nothing (never a fabricated
    verdict)."""

    name = "temporal_window"
    version = "temporal_window:fda_v2"

    @staticmethod
    def _after_effective_date(record):
        """The after label's effective date as a datetime.date, or None. Read
        from provenance['effective_time'] ('YYYYMMDD'), falling back to the
        after evidence's as_of; only a fully-parseable date counts."""
        provenance = getattr(record, "provenance", None)
        raw = provenance.get("effective_time") if isinstance(provenance, dict) else None
        iso = _effective_iso(raw)
        if iso is None:
            after = getattr(record, "after", None)
            after_ev = getattr(after, "evidence", None)
            iso = getattr(after_ev, "as_of", None)
        if not isinstance(iso, str):
            return None
        try:
            return _date.fromisoformat(iso)
        except ValueError:
            return None

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {}
        problems: list = []
        bounds: dict = {}
        for key in ("cutoff", "asof"):
            if key not in ctx:
                problems.append(f"ctx is missing {key!r}")
                continue
            parsed, why = _coerce_date(ctx[key])
            if parsed is None:
                problems.append(f"ctx[{key!r}] {why}")
            else:
                bounds[key] = parsed
                evidence[key] = parsed.isoformat()

        change = getattr(record, "change_date", None)
        if not isinstance(change, ChangeDate):
            problems.append("record has no ChangeDate")
            interval = None
        else:
            evidence["change_date"] = change.value
            evidence["precision"] = change.precision
            interval, why = _month_interval(change.value, change.precision)
            if interval is None:
                problems.append(why)

        if problems:
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        start, end = interval
        cutoff, asof = bounds["cutoff"], bounds["asof"]
        evidence["change_interval"] = [start.isoformat(), end.isoformat()]
        if start >= cutoff and end <= asof:
            # v2 consistency guard: an after label EFFECTIVE before the cutoff
            # contradicts the in-window RMC month — either the changed text was
            # public pre-cutoff (contamination) or the pinned after evidence is
            # inconsistent. Contradictory evidence -> review, never included.
            effective = self._after_effective_date(record)
            if effective is not None:
                evidence["after_effective_date"] = effective.isoformat()
                if effective < cutoff:
                    evidence["placement"] = "effective_pre_cutoff"
                    evidence["problem"] = (
                        "after_label_effective_before_cutoff: the pinned 'after' label's "
                        f"effective_time {effective.isoformat()} predates the cutoff "
                        f"{cutoff.isoformat()} while the RMC change month is in-window — "
                        "either the changed text was live in a pre-cutoff label (public "
                        "inside the model-knowledge window) or the after evidence is "
                        "internally inconsistent; held for review, never included"
                    )
                    return GateResult(name=self.name, version=self.version,
                                      verdict="review", evidence=evidence)
            evidence["placement"] = "inside"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if end < cutoff or start > asof:
            evidence["placement"] = "outside"
            evidence["problem"] = "change month is entirely outside [cutoff, asof]"
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        evidence["placement"] = "window_edge"
        evidence["problem"] = (
            "window_edge: the change month straddles a window bound at month "
            "precision — cannot resolve strictly inside [cutoff, asof]; held for review"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class FdaSectionTextGate(Gate):
    """The RMC section_text (the recorded value) must be present.

    RMC-primary semantics: the fact is "as of <change_date>, <drug>'s <section>
    now states: <section_text>". The recorded value lives in ``after.raw`` (the
    RMC section_text). This gate is the single value check that remains:

    * non-empty section_text (after normalization) -> pass (the fact stands on
      the RMC row alone; the archived 'before' is supplementary and never gates);
    * EMPTY section_text -> FAIL (excluded:fda_section_text) — the one genuine
      value exclusion the reframe keeps: with no current section content there is
      no value to state the change.

    It deliberately does NOT compare before vs after: the Wayback 'before' is
    demoted to provenance and a missing / misaligned / truncated before no
    longer blocks. The advisory ``new_content_added`` flag (computed in
    build_record where a clean before exists) is echoed into evidence, never
    used for the verdict."""

    name = "fda_section_text"
    version = "fda_section_text:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        after = getattr(record, "after", None)
        folded = _fold(getattr(after, "raw", None))
        provenance = getattr(record, "provenance", None)
        prov = provenance if isinstance(provenance, dict) else {}
        evidence = {
            "section_text_len": len(folded),
            "new_content_added": prov.get("new_content_added"),
            "prior_section_available": prov.get("prior_section_available"),
            "semantics": (
                "the RMC section_text is the recorded value; the fact stands on the RMC "
                "row alone (the archived before is supplementary, never gating)"
            ),
        }
        if folded:
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["problem"] = (
            "empty RMC section_text: the changed section has no current content, so there "
            "is no recorded value to state the change"
        )
        return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)


class FdaDeltaGate(Gate):
    """The cutoff-anchored content DELTA classification (owner decision (2)).

    Reads ``provenance['delta']['classification']`` (computed in build_record as
    a pure function of the frozen cutoff-anchored prior FULL section vs the
    current RMC section_text):

    * ``unchanged_or_reworded`` -> REVIEW: the RMC fired for this section but the
      delta found NO new sentence (only rewording/formatting). A real Recent
      Major Change with no detectable new content is suspect — a human must
      confirm it, never a silent include.
    * ``delta_unavailable`` -> PASS (flagged): no clean pre-cutoff prior section,
      so the specific new clause cannot be isolated. The RMC fact still stands as
      a WHOLE-SECTION change (the change_descriptor falls back to the section
      preview); usable, flagged.
    * ``new_subsection`` / ``modified`` -> PASS: the delta isolated the new
      content and the change_descriptor renders it.

    NEVER fails: the delta is a quality/question-generation signal, not a scope
    exclusion — the non-pass verdicts are (a) the review for a suspect no-content
    RMC and (b) the review for a TRUNCATED-fallback current section (v2): the live
    DailyMed full-section fetch was unavailable so ``after`` fell back to the
    openFDA RMC section_text AT its 1200-char hard cap (cut mid-sentence), and a
    truncated value can present neither a complete delta nor an honest
    whole-section value — held for review (refetch the label), never included with
    a truncated value. That truncation review takes precedence over the
    classification (it is the only path that reviews a delta_unavailable). Placed
    after fda_section_text (which guarantees a non-empty value)."""

    name = "fda_delta"
    version = "fda_delta:v3"

    def evaluate(self, record, ctx: dict) -> GateResult:
        provenance = getattr(record, "provenance", None)
        prov = provenance if isinstance(provenance, dict) else {}
        delta = prov.get("delta") if isinstance(prov.get("delta"), dict) else {}
        classification = delta.get("classification")
        evidence = {
            "classification": classification,
            "new_sentence_count": delta.get("new_sentence_count"),
            "current_sentence_count": delta.get("current_sentence_count"),
            "prior_sentence_count": delta.get("prior_sentence_count"),
            "prior_cutoff_status": delta.get("prior_cutoff_status"),
            "prior_cutoff_anchor": delta.get("prior_cutoff_anchor"),
            "current_source": delta.get("current_source"),
            "current_full_status": delta.get("current_full_status"),
            "current_len": delta.get("current_len"),
            "current_truncated": delta.get("current_truncated"),
        }
        # TRUNCATED-fallback current section takes precedence over the
        # classification: the live DailyMed full-section fetch was unavailable, so
        # ``after`` fell back to the openFDA RMC section_text AT its 1200-char hard
        # cap (cut mid-sentence). The recorded value is incomplete, so we can
        # present neither a complete delta nor an honest whole-section value —
        # held for review (refetch the current label), NEVER included with a
        # truncated value. This is the only path that reviews a delta_unavailable.
        if delta.get("current_truncated"):
            evidence["problem"] = (
                "current_section_truncated: the live DailyMed full-section fetch was "
                f"unavailable (current_full_status={delta.get('current_full_status')!r}) so the "
                "recorded 'after' fell back to the openFDA RMC section_text, which is "
                f"HARD-CAPPED at {RMC_SECTION_TEXT_CAP} chars and is cut mid-sentence here — the "
                "current section is incomplete, so neither a complete delta nor an honest "
                "whole-section value can be presented; held for review (refetch the current "
                "label), never included with a truncated value"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        # An INCLUDED FDA record must state the ACTUAL isolated change. Every
        # classification that cannot be reduced to a small, prose-shaped change is
        # held for REVIEW rather than shipping a wall / table / whole section:
        review_reasons = {
            CLASS_UNCHANGED: (
                "unchanged_or_reworded: the RMC fired for this section but the cutoff-anchored "
                "subsection-aware delta found NO new content (every current subsection matches a "
                "prior one) — a real Recent Major Change with no detectable new content is "
                "suspect; held for review, never a silent include"),
            CLASS_TABLE_SECTION: (
                "table_section: a dosing / dosage-forms / drug-interaction section is a TABLE "
                "(grid of cells) that no deterministic diff can reduce to a single new clause; "
                "routed to review wholesale rather than shipping serialised table fragments"),
            CLASS_TABLE_SHAPED: (
                "table_shaped: the isolated change is dominated by short / column-label table "
                "cells, not a prose clause — held for review rather than shipping table fragments"),
            CLASS_WALL: (
                f"restructured_wall: the section was so heavily rewritten that > {MAX_CLEAN_CHANGES} "
                "separate changes were isolated — no single 'the change is Y' exists; held for review"),
            CLASS_DELTA_UNAVAILABLE: (
                "delta_unavailable: no clean pre-cutoff prior section, so the specific new clause "
                "cannot be isolated and the change cannot be stated; held for review"),
        }
        if classification in review_reasons:
            evidence["problem"] = review_reasons[classification]
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        # new_subsection / modified: the delta isolated a small, prose-shaped change.
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class FdaSignificanceGate(Gate):
    """DEFERRED / OPTIONAL editorial filter — the key design point.

    Reads the resolved significance labels from ctx[SIGNIFICANCE_RESOLVED_CTX_KEY]
    (loaded by gate_list from cfg['fda_significance_labels'], a dict or a JSON
    file path keyed by record_id):

    * labels NOT configured (None) -> PASS-THROUGH for EVERY record (note
      "significance labelling deferred; not applied", applied=False), so a
      release ships ALL real in-window changes unfiltered with the filter
      dormant — a working release NOW.
    * labels configured -> look up this record's record_id:
        'significant'     -> pass;
        'not_significant' -> fail (disposition excluded:fda_significance,
                             evidence reason 'not_significant');
        missing/other     -> review (unlabelled: a human must still label it).

    The manifest records whether labels were applied (cfg['policy']). This lets
    the owner's future MANUAL labelling plug in as a committed deterministic
    whitelist WITHOUT an LLM, and lets the release ship today with it dormant."""

    name = "fda_significance"
    version = "fda_significance:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        labels = ctx.get(SIGNIFICANCE_RESOLVED_CTX_KEY) if isinstance(ctx, dict) else None
        if labels is None:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"applied": False,
                          "note": "significance labelling deferred; not applied"},
            )
        record_id = getattr(record, "record_id", None)
        label = labels.get(record_id) if isinstance(labels, dict) else None
        evidence = {"applied": True, "record_id": record_id, "label": label}
        if label == SIG_SIGNIFICANT:
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if label == SIG_NOT_SIGNIFICANT:
            evidence["reason"] = "not_significant"
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        evidence["problem"] = (
            "unlabelled: this record has no significance label in the configured whitelist"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class FdaAdapter(Adapter):
    """Adapter for source 'fda'. See the module docstring for the design.

    Stateless: the RMC enrichment index and cache declarations are loaded once
    per run into cfg (the runner's sanctioned ctx channel), never onto the
    instance — a rewritten input file must be re-read on the next run."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------
    def enumerate_candidates(self, cfg: dict):
        """Yield every history-cache row, sorted by
        (set_id, section_num, change_date, file line). The history cache is 1:1
        with the RMC rows (one per RMC-detected drug-section-month change) and
        carries the Wayback prior-state; the RMC row (the primary fact) is joined
        in build_record. No inclusion decision here. A malformed line is yielded
        as an error candidate so build_record raises it into the manifest's
        build_errors (never a silent skip)."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        path, _ident = self._resolve_input_path(
            cfg, HISTORY_FILENAME, PACKAGE_CACHE_DIR / HISTORY_FILENAME, PACKAGE_HISTORY_ID
        )
        if path is None:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": HISTORY_FILENAME, "line": 0,
                 "error": "fda history cache not found (no candidates)"}
            )
            return
        candidates = []
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"row is {type(row).__name__}, expected object")
                except ValueError as exc:
                    candidates.append({"_line": line_no, "_parse_error": str(exc)})
                    continue
                row["_line"] = line_no
                candidates.append(row)
        candidates.sort(
            key=lambda c: (
                str(c.get("set_id") or ""),
                str(c.get("section_num") or ""),
                str(c.get("change_date") or ""),
                c["_line"],
            )
        )
        yield from candidates

    # -- record building ----------------------------------------------------
    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{HISTORY_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        self._ensure_loaded(cfg)

        set_id = candidate.get("set_id")
        if not isinstance(set_id, str) or not set_id:
            raise ValueError(
                f"{HISTORY_FILENAME} line {candidate.get('_line')}: missing/empty set_id"
            )
        change_date_raw = candidate.get("change_date")
        change_value, precision = parse_change_month(change_date_raw)

        section = candidate.get("section")
        section_num = candidate.get("section_num") or ""
        prop = normalize_section(section)
        status = candidate.get("status")
        section_readable = section if isinstance(section, str) else ""

        generic = (candidate.get("generic") or "").strip()
        brand = (candidate.get("brand") or "").strip()
        entity_name = generic or brand or set_id
        entity_ids = {"set_id": set_id}
        if brand:
            entity_ids["brand"] = brand

        # RMC enrichment (the PRIMARY fact source; keyed on the physical change).
        rmc = self._rmc_lookup(cfg, set_id, section_num, change_date_raw)
        effective_time = rmc.get("effective_time") if rmc else None
        effective_iso = _effective_iso(effective_time)
        known_top300 = rmc.get("known") if rmc else None

        # after / value = the FULL CURRENT SECTION (owner decision 2026-07-24
        # "Option B"). The RMC section_text is HARD-CAPPED by openFDA at 1200 chars,
        # so a full-prior-vs-truncated-current delta MISSED genuinely-new content
        # beyond char 1200 (a false negative the prior-prefix guard cannot recover).
        # stage1.tools.refetch_current_sections froze the WHOLE current SPL section
        # (from the live DailyMed label, raised cap) into the cache as
        # ``current_full_section``; it is the complete, honest recorded value and
        # the delta's current side. Fall back to the RMC section_text (then the
        # history after_text) only when the current fetch was unavailable
        # (section_not_found / no_current_label / fetch_error /
        # out_of_window_not_fetched) — never a crash.
        rmc_section_text = rmc.get("section_text") if rmc else None
        hist_after_text = candidate.get("after_text")
        current_full = candidate.get("current_full_section")
        current_full_status = candidate.get("current_full_status")
        current_full_source = candidate.get("current_full_source")
        current_snapshot_url = candidate.get("current_snapshot_url")
        current_available = (
            current_full_status == "ok"
            and isinstance(current_full, str) and bool(current_full.strip())
        )
        rmc_fallback_text = (
            rmc_section_text if isinstance(rmc_section_text, str) and rmc_section_text
            else (hist_after_text if isinstance(hist_after_text, str) else "")
        )
        if current_available:
            section_text = current_full
            section_text_source = current_full_source or "dailymed_current"
        else:
            section_text = rmc_fallback_text
            section_text_source = "rmc_section_text"

        # before = supplementary Wayback prior-state (DEMOTED). Present only when
        # a clean archived section exists; otherwise an explicit sentinel — the
        # before is never fabricated and never gates.
        before_text = candidate.get("before_text")
        before_full_section = candidate.get("before_full_section")
        prior_available = isinstance(before_text, str) and bool(before_text.strip())
        before_raw = before_text if prior_available else PRIOR_UNAVAILABLE_SENTINEL
        before_snapshot_url = candidate.get("before_snapshot_url") or None
        before_snapshot_ts = candidate.get("before_snapshot_ts") or None

        # Advisory new-content signal (only where a clean before exists). This is
        # the LEGACY word-level span against the CHANGE-MONTH before_text; the
        # authoritative signal is the cutoff-anchored structured DELTA below.
        nca_flag, nca_words = new_content_added(before_text, section_text)

        # ---- CUTOFF-ANCHORED CONTENT DELTA (owner decision 2026-07-24 (2), (B)) ----
        # FULL-vs-FULL: the CURRENT side is now the WHOLE current section
        # (``section_text`` = ``current_full_section`` above, no 1200 cap) and the
        # PRIOR side is the WHOLE section as it stood AT THE CUTOFF
        # (``prior_cutoff_full_section``, frozen by reanchor_dailymed_cutoff and
        # COMPLETED by refetch_current_sections where it was captured at the old
        # 6000 cap — a truncated prior would false-flag long-standing content past
        # the cap as new). Both sides complete makes the content-based delta both
        # renumber-robust AND free of the truncation artifacts (current-side
        # false-negatives beyond char 1200 recovered; prior-side false-positives
        # beyond char 6000 removed). A pure function of the frozen cache.
        prior_cutoff_full = candidate.get("prior_cutoff_full_section")
        prior_cutoff_status = candidate.get("prior_cutoff_status")
        prior_cutoff_anchor = candidate.get("prior_cutoff_anchor")
        prior_cutoff_ts = candidate.get("prior_cutoff_snapshot_ts") or None
        prior_cutoff_url = candidate.get("prior_cutoff_snapshot_url") or None
        prior_cutoff_recut = bool(candidate.get("prior_cutoff_full_recut"))
        prior_cutoff_available = (
            prior_cutoff_status == "ok"
            and isinstance(prior_cutoff_full, str) and bool(prior_cutoff_full.strip())
        )
        # SUBSECTION-AWARE isolation (owner decision 2026-07-24 (C)). The whole
        # current section is split into its numbered subsections and only the
        # GENUINELY-new subsection(s) / clause(s) are isolated; TABLE-dense sections
        # (dosage family, drug interactions) are routed to review wholesale, and a
        # restructured wall (> MAX_CLEAN_CHANGES) or a serialised-table isolation is
        # held for review — so an INCLUDED record states the ACTUAL change, never a
        # wall. current_sents / prior_sents are still computed for the audit counts.
        sec_int = SECTION_NUM.get(prop)
        if prop in TABLE_SECTIONS:
            changes, delta_class, added_text = [], CLASS_TABLE_SECTION, ""
            current_sents = split_sentences(normalize_for_delta(section_text))
            prior_sents = (split_sentences(normalize_for_delta(prior_cutoff_full))
                           if prior_cutoff_available else [])
        elif prior_cutoff_available:
            changes, delta_class, added_text = compute_subsection_delta(
                prior_cutoff_full, section_text, sec_int, prop)
            current_sents = split_sentences(normalize_for_delta(section_text))
            prior_sents = split_sentences(normalize_for_delta(prior_cutoff_full))
        else:
            # No clean pre-cutoff prior: the section CANNOT be differenced, so NO
            # content can be honestly claimed new -> delta_unavailable (review). The
            # current section is split for the counts only; changes stays EMPTY so
            # delta.changes / delta.added_text never present un-differenced
            # long-standing boilerplate as new (an honesty trap for any consumer
            # that reads the delta fields without gating on classification).
            changes, delta_class, added_text = [], CLASS_DELTA_UNAVAILABLE, ""
            current_sents = split_sentences(normalize_for_delta(section_text))
            prior_sents = []
        # DERIVATION: the BODY subsection diff is primary. If it did NOT isolate a
        # clean change (any review class), try the complementary HIGHLIGHTS path
        # (owner decision D) against the openFDA highlights (rmc_section_text) and
        # the same cutoff prior body — recovering concise-summary changes the body
        # walls/misses, with the prior-body promotion guard. When highlights rescue
        # the record, its class/changes/added_text REPLACE the body result and the
        # derivation is recorded as 'highlights'. body_* keeps the body verdict for audit.
        derivation = "body"
        body_class = delta_class
        hl_changes, hl_class, hl_added = [], None, ""
        # NOVELTY COMPARAND (fix, 2026-07-27): the Highlights line is certified
        # against the FULL archived label, never against the one body section.
        # No full label -> no novelty claim -> no Highlights rescue.
        prior_label_row = self._highlights_prior(
            cfg, candidate.get("set_id"), candidate.get("prior_cutoff_snapshot_ts"))
        if delta_class not in (CLASS_NEW_SUBSECTION, CLASS_MODIFIED) and prior_label_row:
            hl_changes, hl_class, hl_added = compute_highlights_delta(
                rmc_section_text, prior_label_row["text"], section_num)
            if hl_class in (CLASS_NEW_SUBSECTION, CLASS_MODIFIED):
                changes, delta_class, added_text = hl_changes, hl_class, hl_added
                derivation = "highlights"
        new_sents = [c["text"] for c in changes]
        # A TRUNCATED-fallback current section (owner constraint): the live DailyMed
        # full-section fetch was unavailable, so ``after`` fell back to the openFDA
        # RMC section_text — and that fallback sits AT the 1200-char hard cap, i.e.
        # it is cut mid-sentence. Such a record can present neither a complete delta
        # nor an honest whole-section value; the delta gate holds it for review
        # (refetch the current label) rather than ship a truncated included value.
        current_truncated = (
            not current_available
            and isinstance(section_text, str)
            and len(section_text) >= RMC_SECTION_TEXT_CAP
        )
        delta = {
            "classification": delta_class,
            "threshold": DELTA_THRESHOLD,
            # HOW the change was derived: 'body' (full-section subsection diff) or
            # 'highlights' (openFDA highlights line, prior-body-guarded). body_class
            # keeps the BODY verdict even when highlights rescued the record.
            "derivation": derivation,
            "body_classification": body_class,
            "highlights_classification": hl_class,
            # the STRUCTURED isolated change(s): {kind, subsection, text, source,
            # source_sentence, [added_phrase]}, each text polished. Empty for review
            # classes. new_sentences keeps the flat text list for back-compat;
            # added_text is their join (the rendered change).
            "changes": changes,
            "new_sentences": new_sents,
            "added_text": added_text,
            "new_sentence_count": len(new_sents),
            "current_sentence_count": len(current_sents),
            "prior_sentence_count": len(prior_sents),
            "prior_cutoff_available": prior_cutoff_available,
            "prior_cutoff_status": prior_cutoff_status,
            "prior_cutoff_anchor": prior_cutoff_anchor,
            "prior_cutoff_snapshot_ts": prior_cutoff_ts,
            "prior_cutoff_snapshot_url": prior_cutoff_url,
            "prior_cutoff_full_recut": prior_cutoff_recut,
            "prior_cutoff_len": len(prior_cutoff_full) if isinstance(prior_cutoff_full, str) else 0,
            # The FULL archived label the Highlights line was certified against
            # (absent => no Highlights rescue was attempted, so no novelty claim
            # rests on a section-scoped comparison).
            "prior_label_available": bool(prior_label_row),
            "prior_label_sha1": (prior_label_row or {}).get("sha1"),
            "prior_label_chars": (prior_label_row or {}).get("chars"),
            # the current side of the delta: the whole current section (or the
            # RMC-section_text fallback when the current fetch was unavailable).
            "current_source": section_text_source,
            "current_full_status": current_full_status,
            "current_len": len(section_text) if isinstance(section_text, str) else 0,
            # True iff ``after`` is a TRUNCATED fallback (current fetch unavailable
            # AND the value sits at the openFDA 1200-char cap): the delta gate holds
            # it for review rather than ship a truncated included value.
            "current_truncated": current_truncated,
        }

        if prior_available:
            before_evidence = Evidence(
                kind="dailymed_archive",
                url=before_snapshot_url,
                ref={
                    "set_id": set_id,
                    "section": section_readable,
                    "section_num": section_num,
                    "before_status": status if isinstance(status, str) else repr(status),
                    "snapshot_count": candidate.get("snapshot_count"),
                    "extract_method": candidate.get("extract_method"),
                    "role": "supplementary_prior_state",
                },
                as_of=before_snapshot_ts,
            )
        else:
            before_evidence = Evidence(
                kind="no_prior_snapshot",
                url=None,
                ref={
                    "set_id": set_id,
                    "section": section_readable,
                    "section_num": section_num,
                    "before_status": status if isinstance(status, str) else repr(status),
                    "role": "no_prior_state",
                },
                as_of=None,
            )
        # after.evidence: the CHANGE is the RMC row (kind openfda_rmc, carrying
        # effective_time / change_date); the recorded VALUE is the WHOLE current
        # DailyMed section (source dailymed_current_full_section) when the current
        # fetch succeeded, else the RMC section_text fallback. The url is the
        # stable public current DailyMed label either way.
        after_evidence = Evidence(
            kind="openfda_rmc",
            url=current_snapshot_url or _dailymed_current_url(set_id),
            ref={
                "set_id": set_id,
                "section": section_readable,
                "section_num": section_num,
                "change_date": change_date_raw,
                "effective_time": effective_time,
                "source": (
                    "dailymed_current_full_section" if current_available
                    else "rmc_section_text"
                ),
                "section_text_source": section_text_source,
                "current_full_status": current_full_status,
            },
            as_of=effective_iso,
        )
        change_date = ChangeDate(
            value=change_value,
            precision=precision,
            evidence=Evidence(
                kind="openfda_rmc",
                url=None,
                ref={
                    "set_id": set_id,
                    "section": section_readable,
                    "section_num": section_num,
                    "change_date_raw": change_date_raw,
                    "effective_time": effective_time,
                    "basis": "rmc_change_month",
                },
                as_of=effective_iso,
            ),
        )

        # The deterministic, human-readable change descriptor (for question
        # generation + the workbook). For a modified / new_subsection record it
        # renders the DELTA (the actual NEW content), NOT the whole current
        # section — so a MODIFIED section (metformin's lactic-acidosis boilerplate
        # + one new high-risk-group sentence) yields a question about WHAT changed.
        # For delta_unavailable / unchanged_or_reworded it falls back to the
        # whole-section preview (no isolable delta).
        section_preview = clean_preview(section_text)
        if delta_class in (CLASS_NEW_SUBSECTION, CLASS_MODIFIED) and added_text:
            rendered = clean_preview(added_text)
            renders = "delta"
        else:
            rendered = section_preview
            renders = "section_fallback"
        sec_label = section_readable or prop
        if section_num:
            sec_label = f"{sec_label} §{section_num}"
        descriptor_text = f"{entity_name} — {sec_label} ({change_date_raw}): {rendered}"
        # AUDIT trail: where the quoted change can be found in the label. The
        # DailyMed URL is the same document for both derivations; the location names
        # which part to read (the terse Highlights summary, or the Full Prescribing
        # Information section body). ``quotes`` carries each exact change string
        # verbatim so an auditor can search the label for it — never a vague
        # "something about dosing somewhere".
        source_url = current_snapshot_url or _dailymed_current_url(set_id)
        if derivation == "highlights":
            source_location = (
                f"Highlights of Prescribing Information — {sec_label}")
        else:
            source_location = f"Full Prescribing Information — {sec_label}"
        quotes = [{"quote": c.get("text"), "source": c.get("source"),
                   "subsection": c.get("subsection"),
                   "added_phrase": c.get("added_phrase"),
                   "source_sentence": c.get("source_sentence", c.get("text"))}
                  for c in changes]
        change_descriptor = {
            "text": descriptor_text,
            "drug": entity_name,
            "section": section_readable,
            "section_num": section_num,
            "property": prop,
            "change_date": change_date_raw,
            # HOW derived + WHERE to verify the quoted change.
            "derivation": derivation,
            "source_location": source_location,
            "source_url": source_url,
            # the exact change text(s), verbatim and quotable (auditable reference).
            "quote": added_text,
            "quotes": quotes,
            # 'preview' renders the DELTA for modified/new_subsection (the new
            # content), else the whole-section preview; 'section_preview' is always
            # the whole-section preview and 'delta_added_text' the isolated delta.
            "preview": rendered,
            "renders": renders,
            "delta_classification": delta_class,
            "delta_added_text": added_text,
            "section_preview": section_preview,
            "section_text": section_text,
            "new_content_added": nca_flag,
            "prior_section_available": prior_available,
            "prior_cutoff_available": prior_cutoff_available,
        }

        provenance = {
            "predictability": check_predictability(PREDICTABILITY),
            "set_id": set_id,
            "brand": brand,
            "generic": generic,
            "section": section_readable,
            "section_num": section_num,
            "change_date_raw": change_date_raw,
            "status": status if isinstance(status, str) else repr(status),
            "known_top300": known_top300,
            "effective_time": effective_time,
            "rmc_matched": rmc is not None,
            # the WHOLE current section (owner decision (B)) — the recorded 'after'
            # value and the current side of the FULL-vs-FULL delta. section_text_source
            # says whether it is the DailyMed current section or the RMC fallback;
            # rmc_section_text keeps the original 1200-capped openFDA value for audit.
            "section_text_source": section_text_source,
            "current_full_status": current_full_status,
            "current_full_source": current_full_source,
            "current_full_len": len(section_text) if isinstance(section_text, str) else 0,
            "current_snapshot_url": current_snapshot_url,
            "prior_cutoff_full_recut": prior_cutoff_recut,
            "rmc_section_text": rmc_section_text,
            # advisory quality signal (word-level added span; None when no clean before)
            "new_content_added": nca_flag,
            "new_content_added_word_count": nca_words,
            # the AUTHORITATIVE cutoff-anchored content DELTA (the new clause + its
            # classification); a pure function of the frozen cutoff prior + section_text.
            "delta": delta,
            # deterministic human-readable change descriptor (question generation)
            "change_descriptor": change_descriptor,
            # DEMOTED Wayback 'before' -> supplementary provenance only.
            "prior_section_available": prior_available,
            "prior_section_text": before_text if prior_available else None,
            "prior_section_full": before_full_section if prior_available else None,
            "prior_snapshot_ts": before_snapshot_ts,
            "prior_snapshot_url": before_snapshot_url,
            # the CUTOFF-anchored prior (the model-knowledge-boundary before-state)
            # the delta is computed against — separate from the demoted change-month
            # before above.
            "prior_cutoff_full_section": prior_cutoff_full if prior_cutoff_available else None,
            "prior_cutoff_snapshot_ts": prior_cutoff_ts,
            "prior_cutoff_snapshot_url": prior_cutoff_url,
            "prior_cutoff_status": prior_cutoff_status,
            "prior_cutoff_anchor": prior_cutoff_anchor,
            "extract_method": candidate.get("extract_method"),
            "history_cache": self._cache_info(cfg, "history"),
            "rmc_cache": self._cache_info(cfg, "rmc"),
        }

        fact_id = compute_fact_id(SOURCE, entity_name, prop, change_value)
        discriminator = f"{set_id}|{section_num}|{change_date_raw}"
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(fact_id, discriminator),
            source=SOURCE,
            entity={"name": entity_name, "ids": entity_ids},
            property=prop,
            value_type=VALUE_TYPE,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_evidence),
            after=ValueState(raw=section_text, canonical=None, evidence=after_evidence),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------
    def gate_list(self, cfg: dict):
        """Ordered gates (rationale in the module docstring). Loads the RMC
        index + cache declarations and resolves the significance labels into
        ctx, and records whether the significance filter was applied in the
        manifest policy."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        self._resolve_significance(cfg)
        labels = cfg.get(SIGNIFICANCE_RESOLVED_CTX_KEY)
        # Record whether the significance filter was applied AND — the audit
        # fix — a byte-stable fingerprint of the exact resolved label mapping,
        # so two whitelists with the same count but different labels (which
        # release different exclusion sets) are distinguishable in the manifest
        # and the release reproduces from a fresh checkout. None while dormant.
        cfg[POLICY_ACTIVE_CTX_KEY] = {
            "fda_significance_labels_applied": labels is not None,
            "fda_significance_labels_count": len(labels) if isinstance(labels, dict) else 0,
            "fda_significance_labels_sha1": _sha1_json(labels) if isinstance(labels, dict) else None,
        }
        return [
            FdaTemporalWindowGate(),
            FdaSectionTextGate(),
            # The cutoff-anchored content DELTA: routes an unchanged_or_reworded
            # RMC (no detectable new content) to review; new_subsection / modified
            # / delta_unavailable pass (the descriptor renders the delta or falls
            # back to the section). After fda_section_text (non-empty value first).
            FdaDeltaGate(),
            EvidenceResolvableGate(),
            FdaSignificanceGate(),
            # Dedup on the SEMANTIC identity (entity.name | property | change_date)
            # — the SAME tuple fact_id is computed from (source is constant here).
            # This makes the included set deduplicated BY fact_id: two subsections
            # of one label (same set_id) sharing a fact_id collapse, AND two
            # DIFFERENT labels of the same generic (distinct set_ids, e.g. CREXONT
            # vs Duopa carbidopa/levodopa) collapse — the first fully-valid record
            # is included and a same-generic sibling with differing section text
            # routes to review (two possibly-distinct edits a human collapses),
            # never a silent drop.
            DedupGate(key_fields=("entity.name", "property", "change_date")),
        ]

    def snapshot_inputs(self, cfg: dict):
        """The core FDA snapshot inputs the coverage check must find LISTED (and
        therefore sha1-verified) in a harvested snapshot's manifest: the
        before/after history cache and the RMC enrichment file (whose
        section_text / effective_time / known_top300 flow into facts.jsonl).
        Without this, a snapshot whose ``files`` block was trimmed to drop the
        history entry — while the on-disk cache is swapped for attacker-supplied
        text — would derive on unverified bytes (rule 5b never runs). Consulted
        ONLY when deriving from a harvested snapshot (a dir carrying
        snapshot_manifest.json); the legacy/package --data-dir has no manifest, so
        this never affects those derives — and the RMC file there lives outside
        data_dir (drugs/), which is fine because the check only runs for snapshots
        the harvester wrote both files into. The history cache's .meta.json
        sidecar is optional provenance (a missing sidecar degrades enrichment, it
        does not drop a candidate), so it is not required here — though the
        harvester sha1-pins it in the manifest and it is verified like any listed
        file. Never changes derive output (mirrors SecAdapter/FinanceAdapter)."""
        return [HISTORY_FILENAME, RMC_FILENAME]

    # -- loading / provenance ----------------------------------------------
    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the fda adapter is offline-only: the DailyMed/Wayback history cache is built "
                "once by stage1.tools.fetch_dailymed_history"
            )

    @staticmethod
    def _resolve_input_path(cfg: dict, filename: str, default_path: Path, default_id: str):
        """(path, location-independent identifier): the vendored copy under
        data_dir when present, else the package/repo default; (None, None) when
        neither exists."""
        data_dir = cfg.get("data_dir")
        if data_dir is not None:
            local = Path(data_dir) / filename
            if local.is_file():
                return local, filename
        if default_path.is_file():
            return default_path, default_id
        return None, None

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the RMC enrichment index and declare the input fingerprints
        into the manifest channels exactly once per run."""
        if cfg.get(LOADED_FLAG_CTX_KEY):
            return
        cfg[LOADED_FLAG_CTX_KEY] = True
        cfg.setdefault(CACHE_INFO_CTX_KEY, {})

        history_path, history_id = self._resolve_input_path(
            cfg, HISTORY_FILENAME, PACKAGE_CACHE_DIR / HISTORY_FILENAME, PACKAGE_HISTORY_ID
        )
        self._declare_input(cfg, "history", history_path, history_id)
        # The history cache's sidecar (its retrieval provenance) — fingerprinted
        # too so a release binds the exact cache bytes to when/how they were built.
        self._declare_sidecar(cfg, history_path)

        rmc_path, rmc_id = self._resolve_input_path(
            cfg, RMC_FILENAME, DEFAULT_RMC_PATH, PACKAGE_RMC_ID
        )
        self._declare_input(cfg, "rmc", rmc_path, rmc_id)
        cfg[RMC_INDEX_CTX_KEY] = self._load_rmc_index(cfg, rmc_path, rmc_id)

        # The full archived label per capture — the Highlights novelty comparand.
        pl_path, pl_id = self._resolve_input_path(
            cfg, PRIOR_LABELS_FILENAME,
            PACKAGE_CACHE_DIR / PRIOR_LABELS_FILENAME, PACKAGE_PRIOR_LABELS_ID
        )
        self._declare_input(cfg, "prior_labels", pl_path, pl_id)
        self._declare_sidecar(
            cfg, pl_path,
            cache_filename=PRIOR_LABELS_FILENAME,
            meta_filename=f"{PRIOR_LABELS_FILENAME}.meta.json",
            package_meta_id=f"{PACKAGE_PRIOR_LABELS_ID}.meta.json",
            cache_tag="prior_labels",
        )
        cfg[PRIOR_LABELS_CTX_KEY] = self._load_prior_labels(cfg, pl_path, pl_id)

    def _load_prior_labels(self, cfg: dict, path, ident) -> dict:
        """(set_id, snapshot_ts) -> archived full-label row.

        A missing/unreadable cache yields an EMPTY index, which disables the
        Highlights rescue entirely (see _highlights_prior) rather than falling
        back to the section extract — the fallback that produced false novelty.
        """
        index: dict = {}
        if path is None:
            return index
        try:
            with Path(path).open(encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                            {"file": ident, "line": lineno, "error": f"unparseable JSON: {exc}"})
                        continue
                    set_id, text = row.get("set_id"), row.get("text")
                    if not set_id or not isinstance(text, str) or not text.strip():
                        continue
                    index[(set_id, str(row.get("snapshot_ts") or ""))] = row
        except OSError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": ident, "line": 0, "error": f"unreadable: {exc}"})
        return index

    @staticmethod
    def _highlights_prior(cfg: dict, set_id, snapshot_ts):
        """The FULL archived label to certify a Highlights line against, or None.

        The Highlights extractor asks "is this concise line genuinely new?". The
        only sound comparand is the WHOLE archived label — including its prior
        HIGHLIGHTS block, which is where an unchanged bullet actually lives, and
        every other body section, where cross-referenced content (8.1 Pregnancy,
        8.3 Reproductive Potential, 17 Patient Counseling) lives.

        The archived copy of ONE BODY SECTION is NOT a valid substitute: an audit
        of the 140-fact release found 26 facts whose "added" text sits verbatim in
        the archived label, 0 of which were findable in the section extract. When
        the full label is unavailable the correct answer is to make NO novelty
        claim — the caller skips the Highlights rescue and the record keeps its
        body verdict (review), never a silently unguarded include.
        """
        index = cfg.get(PRIOR_LABELS_CTX_KEY) or {}
        if not set_id:
            return None
        row = index.get((set_id, str(snapshot_ts or "")))
        if row is None:
            # Same capture recorded under a different timestamp string: accept a
            # unique set_id match, never an ambiguous one.
            hits = [v for (sid, _ts), v in index.items() if sid == set_id]
            row = hits[0] if len(hits) == 1 else None
        return row

    def _declare_input(self, cfg: dict, tag: str, path, ident) -> None:
        info = {"file": None, "sha1": None, "present": False}
        if path is not None:
            digest = _sha1_file(path)
            info = {"file": ident, "sha1": digest, "present": True}
            cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        cfg.setdefault(CACHE_INFO_CTX_KEY, {})[tag] = info

    def _declare_sidecar(self, cfg: dict, cache_path, *, cache_filename=None,
                         meta_filename=None, package_meta_id=None,
                         cache_tag="history") -> None:
        """Fingerprint a frozen cache's .meta.json sidecar and surface its
        retrieval metadata into the manifest's extra_input_meta.

        Defaults describe the DailyMed history cache; the keyword arguments let
        the prior-labels cache declare its own sidecar through the same channel
        rather than silently re-declaring the history one."""
        if cache_path is None:
            return
        cache_filename = cache_filename or HISTORY_FILENAME
        meta_filename = meta_filename or HISTORY_META_FILENAME
        package_meta_id = package_meta_id or PACKAGE_HISTORY_META_ID
        sidecar = cache_path.with_name(meta_filename)
        if not sidecar.is_file():
            return
        # Identifier mirrors the cache's: bare filename when vendored, else the
        # repo-relative package id.
        vendored = cfg.get("data_dir") is not None and (Path(cfg["data_dir"]) / cache_filename).is_file()
        ident = meta_filename if vendored else package_meta_id
        digest = _sha1_file(sidecar)
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        entry = {"sidecar": ident, "sha1": digest}
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            entry["error"] = f"unreadable sidecar: {exc}"
            meta = None
        if isinstance(meta, dict):
            for key in ("tool_version", "retrieved_at", "cache_sha1", "rmc_sha1",
                        "rmc_file", "status_breakdown", "params", "counts",
                        "history_file", "history_sha1", "rows", "candidates",
                        "failures"):
                if key in meta:
                    entry[key] = meta[key]
            if "cache_sha1" in meta:
                cache_info = cfg.get(CACHE_INFO_CTX_KEY, {}).get(cache_tag) or {}
                entry["sidecar_matches_cache"] = meta.get("cache_sha1") == cache_info.get("sha1")
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[ident] = entry

    def _load_rmc_index(self, cfg: dict, rmc_path, rmc_id) -> dict:
        """{(set_id, section_num, change_date): {section_text, effective_time,
        known, section, generic, brand}} from the read-only RMC file (the
        PRIMARY fact source). A missing file yields an empty index (the after
        value then falls back to the history after_text and enrichment degrades
        to None, never a crash); a malformed line is an input_load_error, never a
        silent skip. First row wins per key."""
        index: dict = {}
        if rmc_path is None:
            return index
        errors: list = []
        with open(rmc_path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append({"file": rmc_id, "line": line_no, "error": str(exc)})
                    continue
                if not isinstance(row, dict):
                    errors.append({"file": rmc_id, "line": line_no,
                                   "error": f"row is {type(row).__name__}, expected object"})
                    continue
                key = (row.get("set_id"), row.get("section_num") or "", row.get("change_date"))
                if not isinstance(key[0], str) or not key[0]:
                    continue  # unusable RMC row (no set_id): cannot key an enrichment
                index.setdefault(key, {
                    "section_text": row.get("section_text"),
                    "effective_time": row.get("effective_time"),
                    "known": row.get("known"),
                    "section": row.get("section"),
                    "generic": row.get("generic"),
                    "brand": row.get("brand"),
                })
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        return index

    def _rmc_lookup(self, cfg: dict, set_id: str, section_num: str, change_date_raw):
        index = cfg.get(RMC_INDEX_CTX_KEY)
        if not isinstance(index, dict):
            return None
        return index.get((set_id, section_num or "", change_date_raw))

    def _cache_info(self, cfg: dict, tag: str) -> dict:
        info = (cfg.get(CACHE_INFO_CTX_KEY) or {}).get(tag) or {}
        return {"file": info.get("file"), "sha1": info.get("sha1"),
                "present": bool(info.get("present"))}

    def _resolve_significance(self, cfg: dict) -> None:
        """Resolve cfg['fda_significance_labels'] (a dict, or a JSON file path
        mapping record_id -> label) into cfg[SIGNIFICANCE_RESOLVED_CTX_KEY]:
        a dict when configured, else None (dormant pass-through). An unreadable
        path is logged as an input_load_error and left DORMANT (the release still
        ships all real changes; the error is visible in the manifest).

        When a FILE path is applied, its bytes are ALSO fingerprinted into the
        manifest (cfg['extra_input_files']/['extra_input_meta']) exactly like the
        history/RMC caches, so the (dormant-by-default) exclusion filter, when
        activated, binds the released exclusion set to the exact whitelist bytes
        and reproduces from a fresh checkout. The resolved-mapping fingerprint
        (policy['fda_significance_labels_sha1'], covering the dict source too) is
        added by gate_list."""
        if SIGNIFICANCE_RESOLVED_CTX_KEY in cfg:
            return
        source = cfg.get(SIGNIFICANCE_LABELS_CTX_KEY)
        if source is None:
            cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = None
            return
        if isinstance(source, dict):
            cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = {
                str(k): v for k, v in source.items()
            }
            return
        if isinstance(source, (str, Path)):
            path = Path(source)
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                    {"file": str(source), "line": 0,
                     "error": f"unreadable fda_significance_labels: {exc}"}
                )
                cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = None
                return
            if isinstance(loaded, dict):
                cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = {str(k): v for k, v in loaded.items()}
                self._declare_significance_file(cfg, path)
            else:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                    {"file": str(source), "line": 0,
                     "error": "fda_significance_labels JSON is not an object"}
                )
                cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = None
            return
        # Any other type is a config error: log and stay dormant.
        cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
            {"file": SIGNIFICANCE_LABELS_CTX_KEY, "line": 0,
             "error": f"unexpected type {type(source).__name__}; expected dict or path"}
        )
        cfg[SIGNIFICANCE_RESOLVED_CTX_KEY] = None

    def _declare_significance_file(self, cfg: dict, path: Path) -> None:
        """Fingerprint an APPLIED significance-labels FILE into the manifest
        under a LOCATION-INDEPENDENT identifier (the config supplies an arbitrary
        path, so only the basename — never the machine-absolute path — is
        surfaced), mirroring _declare_input for the history/RMC caches. This
        binds the released exclusion set to the exact whitelist bytes so it
        cannot silently depend on an un-fingerprinted external input."""
        try:
            digest = _sha1_file(path)
        except OSError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": SIGNIFICANCE_INPUT_ID, "line": 0,
                 "error": f"could not fingerprint fda_significance_labels file: {exc}"}
            )
            return
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[SIGNIFICANCE_INPUT_ID] = digest
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[SIGNIFICANCE_INPUT_ID] = {
            "labels_file": SIGNIFICANCE_INPUT_ID,
            "sha1": digest,
            "source": "file",
            "basename": path.name,
        }


ADAPTER = FdaAdapter()
