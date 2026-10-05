# retrieved_but_not_read: temporal fact pipeline

A pipeline that builds datasets of real-world facts that **changed after a chosen
date**: a CEO replaced, a drug label rewritten, a player transferred, a person who
died. Pick a cutoff (for example, a language model's training cutoff) and the
pipeline pulls every qualifying change between that cutoff and an as-of date from
public sources. Each change becomes one audited record with its before value, its
after value, and the evidence for both.

The point is evaluation: a model trained before the cutoff cannot know these facts,
so they test whether it reads the sources it retrieves or answers from memory.

**This repository contains code only.** No harvested data, cached pages, snapshots
or fact releases are included. You pull your own, and everything the pipeline
writes is git-ignored.

## Requirements

Python 3.11 or newer. The pipeline uses the standard library only, so there is
nothing to install.

## Setup

```bash
cp config.example.toml config.toml
```

Then fill in `config.toml` (it is git-ignored). Any of these can also be set as an
environment variable of the same name, which takes precedence over the file.

| Setting | Needed by | What it is |
|---|---|---|
| `STAGE1_CONTACT_EMAIL` | every source | Sent in the User-Agent of every request. Wikimedia, openFDA and SEC EDGAR ask for a real contact, and EDGAR refuses requests without one. The pipeline will not send a request until this is set. |
| `POLYGON_API_KEY` | `finance` | A [Polygon.io](https://polygon.io) API key. |
| `STAGE1_SP500_UNIVERSE` | `sec`, `finance` | Path to a CSV of the companies in scope, with the header `ticker,name,cik`. Not shipped: build your own. |

## Quick start

```bash
python3 generate_facts.py
```

The interactive wizard asks for a cutoff date, lets you tick sources, then harvests
and builds each one with live progress. It is also fully scriptable:

```bash
python3 generate_facts.py --list-sources
python3 generate_facts.py --cutoff 2026-03-01 --sources fda,sports --dry-run   # show the plan only
python3 generate_facts.py --cutoff 2026-03-01 --sources fda,sports --yes
```

Every run writes to fresh `stage1/snapshots/<source>_<cutoff>_<asof>/` and
`stage1/releases/<source>_<cutoff>_<asof>/` directories. A release that already
holds facts is never overwritten, a complete snapshot for the same window is
reused, and an interrupted harvest resumes from its checkpoints.

## How it works

The pipeline runs in two steps, and the wizard runs both for you.

1. **Harvest** (the only step that touches the network) fetches one source for the
   window `[cutoff, asof]` and freezes it as a snapshot directory, with a manifest
   that records SHA-1 hashes, coverage and fetch statistics.

   ```bash
   python3 -m stage1.harvest --source sec --cutoff 2026-01-01 --asof 2026-06-30 \
       --universe sp500_universe.csv --out-dir stage1/snapshots/sec_2026-01-01_2026-06-30
   ```

2. **Build** runs offline over the frozen snapshot: a source adapter turns raw rows
   into candidate records, normalizers parse the before and after values, and a
   chain of gates decides each record's fate. The same snapshot always produces the
   same release.

   ```bash
   python3 -m stage1.run --source sec --cutoff 2026-01-01 --asof 2026-06-30 \
       --data-dir stage1/snapshots/sec_2026-01-01_2026-06-30 \
       --out-dir stage1/releases/sec_2026-01-01_2026-06-30
   ```

Each release holds a `facts.jsonl`, with one record per candidate change, and a
manifest. Nothing is dropped silently: every candidate keeps its full gate ledger and
an explicit disposition. The first failing gate gives `excluded:<gate>`. Otherwise
any gate that could not decide gives `review`, and the remainder are `included`.

## Sources

| Source | What changes | Where it comes from |
|---|---|---|
| `sec` | CEO and CFO appointments at S&P 500 companies | SEC EDGAR 8-K Item 5.02 filings, confirmed by SOX certifications |
| `finance` | Prices, market caps, revenue, ticker changes, IPOs | Polygon.io (needs an API key) |
| `fda` | Changed sections of prescription drug labels | openFDA, DailyMed and the Wayback Machine |
| `sports` | Football club transfers | Wikipedia infobox revisions, corroborated by Wikidata |
| `wiki_people` | Deaths | Wikidata and Wikipedia |
| `chemical` | IARC, Prop 65 and EPA TSCA carcinogen reclassifications | A cited reference table you supply (below), with a live cross-check |

The three `*_controls` sources build matched sets of facts that did *not* change,
paired with an existing study. `sports_controls` and `people_controls` read a
treatment release (`--opt treatment_release=PATH`). `people_controls` and
`finance_controls` also read a downstream evaluation's draws file
(`--opt draws=PATH`, plus `--opt facts=PATH` for finance).

### Inputs you supply

* **S&P 500 universe** (`sec`, `finance`): a CSV with the columns `ticker,name,cik`,
  pointed to by `STAGE1_SP500_UNIVERSE`.
* **Chemical reference** (`chemical`): `stage1/harvest/chemical_reference.jsonl`, one
  JSON object per reclassification, with the fields `chemical`, `cas`, `property`
  (`carcinogen_group`, `prop65_carcinogen_listing` or `tsca_risk_determination`),
  `sub_source`, `change_date`, `before_raw`, `after_raw`, and `before_evidence`,
  `after_evidence` and `change_evidence` (each `{kind, url, ref, as_of}`). IARC
  publishes no machine-readable feed, so every row must cite its source.
* **FDA top-300 list** (`fda`, optional): `--opt top300=PATH` marks which drugs count
  as widely known.

## Good citizenship

Requests are paced, retried with backoff, and identified by your contact address.
EDGAR stays under 8 requests a second, and Polygon calls are spaced 12 seconds
apart by default to suit its slowest tier (`--opt rate_min_interval=SECONDS`
changes that).
Whatever you pull stays subject to its source's terms. Wikipedia text is CC BY-SA,
Wikidata is CC0, and Polygon data falls under your Polygon subscription. Check those
terms before you redistribute any data you harvest.

## Disclaimer

This is research code. The records it produces are extracted automatically and can
be wrong. It is not medical, financial or legal advice. In particular, always check
drug-label facts against the official label.

## License

MIT for the code (see `LICENSE`). Data you harvest with it is not covered by that
licence.
