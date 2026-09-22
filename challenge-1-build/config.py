"""Constants and tunables for the crew-ops agent pipeline.

Everything an operator might want to change lives here. Nothing in this
module imports from the rest of the package, so it is safe to import
anywhere — including from `deploy.sh`-adjacent scripts before `.env` is
even loaded.
"""

from __future__ import annotations

import os
from pathlib import Path


# --------------------------------------------------------------------------
# Models
#
# dCortex Crew Ops Advisor split this across three model calls by role
# (a cheap classifier, the tool-loop advisor, and a narration pass). Foundry
# hackathon environments deploy one model, so all three Agents below use
# MODEL_DEPLOYMENT_NAME by default — set TRIAGE_MODEL_DEPLOYMENT_NAME to a
# second, cheaper deployment if your Foundry project has one, since the
# Triage Agent only ever sees a couple of sentences at a time.
# --------------------------------------------------------------------------
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
TRIAGE_MODEL_DEPLOYMENT_NAME = os.getenv("TRIAGE_MODEL_DEPLOYMENT_NAME", MODEL_DEPLOYMENT_NAME)
ADVISOR_MODEL_DEPLOYMENT_NAME = os.getenv("ADVISOR_MODEL_DEPLOYMENT_NAME", MODEL_DEPLOYMENT_NAME)
EXPLAINER_MODEL_DEPLOYMENT_NAME = os.getenv("EXPLAINER_MODEL_DEPLOYMENT_NAME", MODEL_DEPLOYMENT_NAME)

MAX_TOOL_ITERATIONS = 8  # hard stop on the Resolution Advisor's tool loop

# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent / "data"

# The dataset is a fixed week. Bare dates like "15 Sep" resolve into it.
SNAPSHOT_UTC = "2026-09-14T18:00:00Z"
WEEK_START = "2026-09-14"
WEEK_END = "2026-09-20"
DEFAULT_YEAR = 2026

# --------------------------------------------------------------------------
# Controlled vocabulary (derived from the dataset)
# --------------------------------------------------------------------------
STATIONS = frozenset({"BLR", "BOM", "CCU", "COK", "DEL", "GOI", "HYD", "MAA"})
AIRCRAFT = frozenset({"VT-DXA", "VT-DXB", "VT-DXC", "VT-DXD", "VT-DXE", "VT-DXF"})
AIRCRAFT_TYPES = frozenset({"A320", "ATR72"})
ROLES = ("Captain", "First Officer", "Senior Cabin Crew", "Cabin Crew")
CERT_TYPES = frozenset(
    {"dangerous_goods", "licence", "medical_class1", "recurrent_training"}
)

ALL_RULE_IDS = (
    "RULE-FDP-01",
    "RULE-DUTY-02",
    "RULE-FLT-03",
    "RULE-REST-04",
    "RULE-QUAL-05",
    "RULE-CERT-06",
    "RULE-BASE-07",
)

# --------------------------------------------------------------------------
# Verifier
# --------------------------------------------------------------------------
# Numbers below this are prose ("all 7 rules", "the 2 options") rather than
# claims about the world, so the verifier does not demand a source for them.
VERIFIER_NUMERIC_FLOOR = 10.0
VERIFIER_FLOAT_TOLERANCE = 0.01
