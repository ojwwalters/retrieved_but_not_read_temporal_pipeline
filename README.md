# Retrieved but not Read: a temporal fact pipeline

A deterministic, LLM-free pipeline that generates **verifiable facts that changed
after a date you choose**: a new CEO, a rewritten drug-label section, a football
transfer, a recorded death. Every fact is anchored to a primary source on both sides
of the change, and the pipeline can be regenerated against any future cutoff.

## Why temporal facts

Search-enabled language models attach citations to their answers, and those
citations lend the answers provenance. On an ordinary fact you cannot tell whether
the model actually used the source it cited or answered from memory and attached
the link as decoration.

Temporal facts make that observable. If the answer changed after the model's training
cutoff, memory holds only the old value. A correct answer must come through
retrieval, so citation honesty can be measured separately from correctness.

This pipeline was built to generate those facts for an MSc dissertation, *Retrieved
but not Read: accuracy and citation faithfulness of search-enabled language models
on post-cutoff temporal facts*. On facts from this pipeline, live search lifted a
frontier model's accuracy from 23.3% closed-book to 93.5%. Yet over half of its web
episodes answered from search-result snippets without opening a page, and citation
unfaithfulness varied by an order of magnitude across models given identical
evidence.

## Design principles

* **Primary sources.** Every included fact derives from a retrievable primary
  artefact, ideally capturing the state both before and after the change: an SEC
  filing, an FDA label, a pinned Wikipedia revision.
* **Temporally certified.** The change must fall strictly after the cutoff and on or
  before the as-of date. A change counts as contaminated if either its effective date
  or its public announcement falls on or before the cutoff.
* **Deterministic and LLM-free.** Harvesting freezes raw data into a snapshot.
  Building a release from that snapshot is offline and reproduces byte for byte.
* **Nothing dropped silently.** Every candidate change becomes a record with its
  complete gate ledger and an explicit disposition. Every exclusion has a named
  reason.
* **Stratified by predictability.** A change after the cutoff is not necessarily
  unforeseeable, so each fact carries a tag:
  * *unpredictable*: deaths and transfers, which cannot be announced in advance.
  * *varies*: earnings, officer changes and label changes, which are partly
    anticipable from guidance or domain knowledge.
  * *announced*: IPOs and ticker changes, which are disclosed before they take
    effect.

## Sources

| Source | What changes | Primary ground truth | Corroboration | Back-datable |
|---|---|---|---|---|
| `sec` | CEO, CFO, COO, President and Chair changes at S&P 500 companies | SEC EDGAR 8-K Item 5.02 filings and SOX §302 certification signatures | none needed: the filing is authoritative | yes |
| `finance` | Quarterly revenue, prices and market caps, ticker changes, IPOs | Massive (formerly Polygon.io) | none: single source | yes |
| `fda` | A new clause in a revised drug-label section | openFDA Recent Major Changes and the live DailyMed label, diffed against an Internet Archive capture from before the cutoff | none needed: the label is authoritative | no |
| `sports` | Football club transfers | Wikipedia infobox at pinned revisions | Wikidata P54 | yes |
| `wiki_people` | Deaths (date, plus place and cause when recorded) | Wikipedia infobox at pinned revisions | Wikidata P570 | yes |
| `chemical` | IARC, Prop 65 and EPA TSCA carcinogen reclassifications | A cited reference table you supply | SHA-pinned table plus a live cross-check | yes |

*Back-datable* means a snapshot for a past window can still be rebuilt today. EDGAR,
Wikipedia revisions, Wikidata history and price history are permanent archives.
openFDA only serves current labels, so an FDA harvest is sound only near the window
it ran for.

For scale, a reference run at a 1 February 2026 cutoff produced 20 included SEC
facts from 652 candidate filings, 1,444 finance from 2,273, 212 sports from 959,
112 FDA from 318, 3,473 people records from 6,409, and all 8 chemical facts. No data
from that run is included here.

## Requirements

Python 3.11 or newer. The pipeline uses only the standard library, so there is
nothing to install.

## Setup

```bash
cp config.example.toml config.toml
```

Then fill in `config.toml`, which is git-ignored. Any setting can also be passed as
an environment variable of the same name, which takes precedence over the file.

| Setting | Needed by | What it is |
|---|---|---|
| `STAGE1_CONTACT_EMAIL` | every source | Sent in the User-Agent of every request. Wikimedia, openFDA and SEC EDGAR ask for a real contact, and EDGAR refuses requests without one. No request leaves your machine until this is set. |
| `POLYGON_API_KEY` | `finance` | A Massive / Polygon.io API key. |
| `STAGE1_SP500_UNIVERSE` | `sec`, `finance` | Path to a CSV of the companies in scope, with the header `ticker,name,cik`. Not shipped: build your own. |

## Quick start

```bash
python3 generate_facts.py
```

The wizard asks for a cutoff date, lets you tick sources, then harvests and builds
each one with live progress bars. It is also fully scriptable:

```bash
python3 generate_facts.py --list-sources
python3 generate_facts.py --cutoff 2026-03-01 --sources sports,wiki_people --dry-run   # show the plan
python3 generate_facts.py --cutoff 2026-03-01 --sources sports,wiki_people --yes
```

Every run writes to fresh `stage1/snapshots/<source>_<cutoff>_<asof>/` and
`stage1/releases/<source>_<cutoff>_<asof>/` directories, and the as-of date defaults
to today. A release that already holds facts is never overwritten, a complete
snapshot for the same window is reused, and an interrupted harvest resumes from its
checkpoints.

## How it works

The wizard runs two commands per source. You can also run them yourself.

**1. Harvest.** This is the only networked step. It fetches every candidate change in
`[cutoff, asof]` and freezes it into a snapshot directory. The snapshot's
`snapshot_manifest.json` records the SHA-1 of every file and a coverage contract:
the window, whether each edge is pinned exactly, and whether the source is
back-datable.

```bash
python3 -m stage1.harvest --source sec --cutoff 2026-01-01 --asof 2026-06-30 \
    --universe sp500_universe.csv --out-dir stage1/snapshots/sec_2026-01-01_2026-06-30
```

**2. Build.** This runs offline and is deterministic. The runner refuses a window
the snapshot does not cover, and refuses any file whose SHA-1 has changed. It then
runs four steps:
1. The source's adapter turns every candidate into a draft record.
2. A comparator for the value type (person name, quantity, date, organisation or
   text span) normalises the before and after values.
3. Every gate in the source's chain runs on every record, even after one fails.
4. The disposition is derived from the gate ledger.

```bash
python3 -m stage1.run --source sec --cutoff 2026-02-01 --asof 2026-06-30 \
    --data-dir stage1/snapshots/sec_2026-01-01_2026-06-30 \
    --out-dir stage1/releases/sec_2026-02-01_2026-06-30
```

A build may narrow the cutoff of a wider snapshot where the source allows it. Changes
that then fall before the cutoff stay in the ledger as `excluded:temporal_window`
rows, so nothing disappears.

The release is two files:

* **`facts.jsonl`** holds one record per candidate change, sorted, with nothing
  dropped.
* **`manifest.json`** records the arguments, the pipeline version, every gate's
  version and verdict counts, the SHA-1 of every input, and the snapshot's
  coverage.

### Dispositions

1. The first gate, in ledger order, that returns `fail` gives `excluded:<gate>`.
2. Otherwise, any `review` verdict gives `review`.
3. Otherwise the record is `included`.

Gates never raise on bad data. Anything unparseable or ambiguous becomes `review`,
with evidence saying what could not be decided.

The shared gates live in `stage1/gates/standard.py`:

| Gate | What it checks |
|---|---|
| temporal window | the change date falls inside `[cutoff, asof]` |
| universe membership | the entity is in the source's declared scope |
| value parsed | both sides normalised successfully |
| value actually changed | the comparator says before ≠ after |
| evidence resolvable | both sides' evidence points somewhere |
| garbage value | the raw values pass a plausibility screen |
| corroboration | the after value agrees with an independent second source |
| dedup | duplicate changes keep one winner |

Corroboration disagreement goes to `review`, never `fail`: a mismatch may be
vandalism on either side, timing lag, or a genuinely different event. Each adapter
adds its own source-specific gates and declares the order.

### A record

Values are shortened here, and the canonical forms are elided.

```json
{
  "fact_id": "3f1c0b9e2a47",
  "record_id": "9a07d1c4e8b2",
  "source": "sec",
  "entity": {"name": "Example Corp", "ids": {"cik": "0000000000", "ticker": "EXMP"}},
  "property": "ceo",
  "value_type": "person_name",
  "before": {"raw": "Jane Smith", "canonical": {…}, "evidence": {"kind": "sox_cert", "url": "https://www.sec.gov/…", "ref": {…}, "as_of": "…"}},
  "after":  {"raw": "John Doe",   "canonical": {…}, "evidence": {"kind": "sox_cert", "url": "https://www.sec.gov/…", "ref": {…}, "as_of": "…"}},
  "change_date": {"value": "2026-03-02", "precision": "day", "evidence": {…}},
  "gates": [{"name": "temporal_window", "version": "…", "verdict": "pass", "evidence": {…}}, …],
  "disposition": "included",
  "provenance": {…},
  "pipeline_version": "0.3.0"
}
```

`fact_id` is a hash of source, entity, property and change date. Every record that
observes the same change shares it. `record_id` is unique per record.

## Source notes

### Inputs you supply

* **S&P 500 universe** (`sec`, `finance`): a CSV with the columns `ticker,name,cik`,
  pointed to by `STAGE1_SP500_UNIVERSE`.
* **Chemical reference** (`chemical`): IARC publishes no machine-readable feed, so
  the chemical source reads a cited table at `stage1/harvest/chemical_reference.jsonl`.
  It holds one JSON object per reclassification with these fields:
  * `chemical`, `cas` and `sub_source`.
  * `property`: one of `carcinogen_group`, `prop65_carcinogen_listing` or
    `tsca_risk_determination`.
  * `change_date`, `before_raw` and `after_raw`.
  * `before_evidence`, `after_evidence` and `change_evidence`, each shaped
    `{kind, url, ref, as_of}`.
* **FDA top-300 list** (`fda`, optional): `--opt top300=PATH` marks which drugs count
  as widely known. It is recorded but never gates.

### FDA: the enrichment passes

The FDA harvest records each label's Recent Major Changes and the section text from
before the change month. To isolate the genuinely new clause, the reference run then
ran three enrichment passes over the history file:

* **`reanchor_dailymed_cutoff`** anchors the "before" to the label as it stood at the
  cutoff.
* **`refetch_current_sections`** fetches the full current section, because openFDA
  truncates sections at 1,200 characters.
* **`fetch_prior_labels`** archives each drug's whole pre-cutoff label. The
  Highlights extractor uses it to confirm that a line is genuinely new.

The passes rewrite the history file in place, and the build refuses a snapshot whose
files no longer match its manifest. So enrich a working copy, and build from that:

```bash
S=stage1/snapshots/fda_2026-03-01_2026-08-31
W=stage1/cache/fda_work      # git-ignored working copy
mkdir -p $W && cp -R $S/. $W/ && rm -f $W/snapshot_manifest.json $W/.stage1_snapshot
python3 -m stage1.tools.reanchor_dailymed_cutoff --cache $W/fda_dailymed_history.jsonl --cutoff 2026-03-01
python3 -m stage1.tools.refetch_current_sections --cache $W/fda_dailymed_history.jsonl --cutoff 2026-03-01 --asof 2026-08-31
python3 -m stage1.tools.fetch_prior_labels --history $W/fda_dailymed_history.jsonl --out $W
python3 -m stage1.run --source fda --cutoff 2026-03-01 --asof 2026-08-31 \
    --data-dir $W --out-dir stage1/releases/fda_2026-03-01_2026-08-31
```

A directory without the snapshot manifest and marker builds without the coverage
check, but the release manifest still records the SHA-1 of every input. Without the passes, the
adapter falls back to openFDA's truncated section text and has no cutoff-anchored
prior to diff against. Each tool's docstring covers its options, including
`--resume`.

### Control sources

`sports_controls`, `people_controls` and `finance_controls` build matched sets of
facts that did *not* change, as a difficulty floor beside the treatment facts.
* `sports_controls` and `people_controls` read a treatment release
  (`--opt treatment_release=PATH`).
* `people_controls` and `finance_controls` also read a downstream evaluation's draws
  file (`--opt draws=PATH`, plus `--opt facts=PATH` for finance).

They only make sense alongside an existing study.

## Adding a source

A source is a harvester plus an adapter, and everything else is shared.

* **The harvester** goes in `stage1/harvest/<source>.py` and exposes a module-level
  `HARVESTER`. It has two methods:
  * `coverage(cfg)`: pure, offline. It declares the window and pinning the snapshot
    promises.
  * `harvest(cfg, writer, ctx)`: the only networked method. It fetches candidates
    and hands files to the snapshot writer. An HTTP failure is recorded in
    `fetch_stats`, never swallowed.
* **The adapter** goes in `stage1/adapters/<source>.py` and exposes a module-level
  `ADAPTER`. It has three methods:
  * `enumerate_candidates(cfg)` yields every potential change. Enumeration never
    decides inclusion.
  * `build_record(candidate, cfg)` returns a draft record.
  * `gate_list(cfg)` declares the gates, in order.

Judgement lives only in gates, where it is versioned and logged. A new value type
registers a comparator in `stage1/normalize/`.

## Good citizenship

Requests are paced, retried with backoff, and identified by your contact address.
EDGAR stays under 8 requests a second, and Polygon calls are spaced 12 seconds apart
by default to suit its slowest tier (`--opt rate_min_interval=SECONDS` changes that).
Whatever you harvest stays subject to its source's terms. Wikipedia text is
CC BY-SA, Wikidata is CC0, and Massive / Polygon data falls under your subscription.
Check those terms before you redistribute anything.

## Disclaimer

This is research code. Its records are extracted automatically and can be wrong,
including records about real people. FDA records describe label changes rather than
clinical guidance, and chemical records describe regulatory classifications rather
than exposure or safety guidance. Nothing it produces is medical, financial, safety
or legal advice. Always check a fact against its primary source: the official drug
label, the IARC, Prop 65 or EPA listing, or the SEC filing.

Provided as is, without warranty of any kind (see [LICENSE](LICENSE)). Use it at
your own risk.

## License

MIT for the code (see [LICENSE](LICENSE)). Data you harvest with it is not covered by
that licence.
