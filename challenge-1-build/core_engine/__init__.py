"""The reasoning core — legality rules, duty arithmetic, candidate search and
cascade analysis for the Resolution Advisor Agent's tools.

Ported from dCortex Crew Ops Advisor's `core/` package. Backed here by the
vendored JSON dataset in `../data/` rather than Postgres, so the whole lab
runs with no database to stand up — one `World`, loaded once, held in memory.
"""
