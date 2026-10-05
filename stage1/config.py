"""Operator settings: the contact address, API keys and input paths.

Nothing personal is committed. Copy ``config.example.toml`` at the repo root to
``config.toml`` (git-ignored) and fill in what your sources need. An environment
variable of the same name always wins over the file, so a one-off run or CI job
needs no file at all.

Every outbound request names a contact in its User-Agent: Wikimedia, openFDA,
DailyMed and SEC EDGAR all ask for one, and EDGAR refuses generic agents. The
placeholder address is never sent — ``require_contact_email`` stops the run
first with instructions.
"""

from __future__ import annotations

import os
import sys
import tomllib
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.toml"
PLACEHOLDER_EMAIL = "you@example.com"
PRODUCT = "temporal-fact-pipeline/1.0"


@lru_cache(maxsize=None)
def _file_settings() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}


def setting(name: str) -> str | None:
    """The environment value of ``name``, else the config.toml value, else None.
    Blank values count as unset."""
    value = os.environ.get(name)
    if value is None:
        value = _file_settings().get(name)
    value = "" if value is None else str(value).strip()
    return value or None


def contact_email() -> str | None:
    """The configured contact address, or None while it is unset/placeholder."""
    email = setting("STAGE1_CONTACT_EMAIL")
    return None if email in (None, PLACEHOLDER_EMAIL) else email


def require_contact_email() -> str:
    """The contact address, or exit 2 with instructions — called before any
    request leaves the machine."""
    email = contact_email()
    if email is None:
        print(
            "error: no contact email configured. Copy config.example.toml to "
            "config.toml and set STAGE1_CONTACT_EMAIL (or export it). Wikimedia, "
            "openFDA and SEC EDGAR ask for a real contact in the User-Agent.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return email


def user_agent(product: str = PRODUCT, contact: str | None = None) -> str:
    """The descriptive User-Agent sent with every request."""
    contact = contact or contact_email() or PLACEHOLDER_EMAIL
    return f"{product} (research on temporal fact drift; contact: {contact})"
