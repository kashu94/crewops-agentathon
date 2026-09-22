#!/usr/bin/env python3
"""Upload the self-distilled training set and run an Azure OpenAI fine-tuning
job against it.

This targets the Resolution Advisor's tool-selection behavior only (see
`generate_training_data.py`'s module docstring for why) -- `verifier.py`
still gates every answer regardless of which model is behind the Advisor, so
nothing about correctness depends on this succeeding.

Which endpoint this needs
--------------------------
Fine-tuning is a classic Azure OpenAI resource operation, not a Foundry
*project* operation -- it may or may not be exposed through the same
`AIProjectClient.get_openai_client()` used elsewhere in this repo, depending
on your Foundry setup. If `AZURE_OPENAI_ENDPOINT` isn't set, this script
falls back to `PROJECT_CONNECTION_STRING`; if that doesn't work for your
project, open **Foundry portal -> your project -> Fine-tuning -> new job**
once by hand, which shows the exact endpoint and SDK snippet for your
resource, and set `AZURE_OPENAI_ENDPOINT` in `.env` to match.

Which model to fine-tune
-------------------------
The fine-tunable model list is region- and subscription-specific and changes
over time. Check **Foundry portal -> Fine-tuning -> Create -> base model**
for what your subscription can actually use, then set `FINETUNE_BASE_MODEL`
in `.env` (defaults to `gpt-4o-mini-2024-07-18`, a commonly available
fine-tunable model at time of writing -- not a promise it's available in
your region).

Usage:
    python generate_training_data.py   # writes data/training.jsonl + validation.jsonl
    python finetune.py                 # uploads them and runs the job
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _find_repo_root()
load_dotenv(REPO_ROOT / ".env")

DATA_DIR = Path(__file__).resolve().parent / "data"
TRAINING_FILE = DATA_DIR / "training.jsonl"
VALIDATION_FILE = DATA_DIR / "validation.jsonl"

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("PROJECT_CONNECTION_STRING")
FINETUNE_BASE_MODEL = os.getenv("FINETUNE_BASE_MODEL", "gpt-4o-mini-2024-07-18")
FINETUNE_SUFFIX = os.getenv("FINETUNE_SUFFIX", "crew-advisor")
POLL_SECONDS = 30


def _client():
    """Azure AD auth (`DefaultAzureCredential`), matching the rest of this
    repo's keyless-auth convention -- no API key needed if your identity has
    the Cognitive Services OpenAI Contributor role on the resource."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2026-01-01-preview"),
    )


def _upload(client, path: Path) -> str:
    print(f"Uploading {path.name} ({path.stat().st_size / 1024:.1f} KB)...")
    with path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="fine-tune")

    while True:
        status = client.files.retrieve(uploaded.id).status
        if status == "processed":
            break
        if status in ("error", "deleted"):
            raise RuntimeError(f"{path.name} failed to process: status={status}")
        time.sleep(5)

    print(f"  -> file id {uploaded.id} (processed)")
    return uploaded.id


def main() -> None:
    if not AZURE_OPENAI_ENDPOINT:
        print("Set AZURE_OPENAI_ENDPOINT (or PROJECT_CONNECTION_STRING) in .env first.")
        sys.exit(1)
    if not TRAINING_FILE.exists():
        print(f"{TRAINING_FILE} not found -- run generate_training_data.py first.")
        sys.exit(1)

    client = _client()

    training_id = _upload(client, TRAINING_FILE)
    validation_id = _upload(client, VALIDATION_FILE) if VALIDATION_FILE.exists() else None

    print(f"\nCreating fine-tuning job (base model: {FINETUNE_BASE_MODEL})...")
    job = client.fine_tuning.jobs.create(
        training_file=training_id,
        validation_file=validation_id,
        model=FINETUNE_BASE_MODEL,
        suffix=FINETUNE_SUFFIX,
    )
    print(f"  -> job id {job.id}, status={job.status}")

    seen_events: set[str] = set()
    while True:
        job = client.fine_tuning.jobs.retrieve(job.id)
        for event in client.fine_tuning.jobs.list_events(job.id, limit=20).data:
            if event.id not in seen_events:
                seen_events.add(event.id)
                print(f"  [{event.created_at}] {event.message}")

        if job.status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(POLL_SECONDS)

    print(f"\nJob finished: status={job.status}")
    if job.status != "succeeded":
        print("Fine-tuning did not succeed -- see events above.")
        sys.exit(1)

    print(f"\nFine-tuned model: {job.fine_tuned_model}")
    print("""
Next steps:
  1. Foundry portal -> your project -> Deployments -> Create -> select the
     fine-tuned model above, and deploy it under a new deployment name.
  2. Add to .env:
         ADVISOR_MODEL_DEPLOYMENT_NAME=<your new deployment name>
     challenge-1-build/config.py already reads this exact variable, so
     agents.py picks up the fine-tuned model with no code change.
  3. Re-run challenge-4-deploy/deploy.py's evaluation loop and compare the
     scorecard (verified rate, tool-call count per question, iterations
     before the loop stopped) against the base-model run from before this
     job. The fine-tune is working if tool-call counts drop and the
     verified rate holds -- not if the verified rate itself moves, which
     verifier.py already pins regardless of which model is behind it.
""")


if __name__ == "__main__":
    main()
