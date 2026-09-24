"""Marks `/api/*` as a Python Function for Vercel's builder to detect.

The actual entrypoint used at runtime is `console.app:Handler`, set
explicitly in `pyproject.toml` -- Vercel's Python builder statically
inspects this file for a top-level `app`/`application`/`handler` variable,
and can't trace one re-exported through a runtime `sys.path` import (see
`console/app.py`'s `Handler` for the real class, and
`_ensure_ledger_warm()` for how warm-up still runs exactly once per warm
process regardless of which file Vercel treats as the entrypoint).
"""
