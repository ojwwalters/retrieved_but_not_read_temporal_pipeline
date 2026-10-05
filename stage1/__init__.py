"""Stage-1 unified fact-change pipeline.

Builds a temporal benchmark of fact changes straddling LLM training cutoffs
from heterogeneous sources (SEC filings, FDA drug labels, Wikipedia, ...).
Each source contributes an adapter (stage1.adapters.<source>) that emits
FactChangeRecord drafts; shared normalization (stage1.normalize) and gating
(stage1.gates) turn drafts into records with explicit dispositions.

Design invariants (enforced throughout the package):

* Determinism — no randomness, no wall-clock-dependent logic in outputs
  (timestamps appear only in manifest metadata), stable ordering (records
  sorted by fact_id before writing), stable hashing (hashlib.sha1 over
  explicit utf-8 strings).
* Nothing is silently dropped — every candidate ends with an explicit
  disposition ('included', 'excluded:<gate>', or 'review'); unparseable or
  ambiguous data becomes a 'review' verdict, never a default include/exclude
  and never a crash.
* Stdlib only — the pipeline must run from a fresh checkout with Python 3.11
  and no pip installs.
"""

PIPELINE_VERSION = "0.3.0"
