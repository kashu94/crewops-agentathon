"""Shared plumbing for every hybrid (BM25 + semantic) search in this repo --
one place to call the embedding model and blend two scores onto a common
scale, reused by `rule_search.py` and `intent_search.py` rather than
duplicated per table.
"""

from __future__ import annotations

import os

EMBEDDING_DIMENSIONS = 384  # matches the *_vec tables' declared vector(384)


def enabled() -> bool:
    return bool(os.getenv("LEDGER_DATABASE_URL") and os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME"))


def embed_query(text: str) -> list[float]:
    """One query embedding, truncated to the same 384 dimensions already
    stored in this project's *_vec tables -- `text-embedding-3-small`
    supports requesting a shorter output directly, so a fresh query is
    comparable to rows this project's original system already embedded,
    with nothing here re-embedded.

    `AZURE_OPENAI_ENDPOINT` here already points at Azure's unified `/openai/v1`
    surface (not the classic `.../openai/deployments/<name>` layout), so this
    uses the plain `OpenAI` client with that URL as `base_url` and a bearer
    token as `api_key` -- `AzureOpenAI(azure_endpoint=...)` double-appends
    `/openai/...` onto a `base_url` that already has it, which 404s.
    """
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = OpenAI(
        base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=token_provider(),
    )
    response = client.embeddings.create(
        model=os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME"),
        input=text, dimensions=EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


def normalize(scores: list[float]) -> list[float]:
    """Min-max to [0, 1] so a 0.5/0.5 blend is an actual 0.5/0.5 blend --
    BM25 (`ts_rank_cd`) and cosine similarity live on unrelated scales, and
    blending raw values would let whichever one happens to run larger
    silently dominate regardless of the requested weighting."""
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [0.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


def _is_flat(scores: list[float]) -> bool:
    """No signal at all -- every candidate scored identically. `ts_rank_cd`
    over `plainto_tsquery` ANDs every non-stopword term together, so it
    returns exactly 0 for every row the moment a real paraphrase doesn't
    share literally every word with the text -- which is the common case,
    not an edge case, for the paraphrased questions this search exists to
    handle at all."""
    return max(scores) - min(scores) < 1e-9


def blend(bm25_scores: list[float], semantic_scores: list[float], alpha: float) -> list[float]:
    """`alpha * bm25 + (1 - alpha) * semantic`, both min-max normalized to
    [0, 1] -- except when one side is flat (see `_is_flat`): blending a
    flat, uninformative side in at its full weight would silently cap
    every achievable score at `alpha` (or `1 - alpha`), no matter how
    strong the other, actually-informative side is. A real paraphrase with
    a perfect semantic match but zero keyword overlap would then be
    mathematically unable to ever clear a threshold above 0.5 -- so a flat
    side is dropped and the informative side carries the blend alone,
    rather than that silent, structural ceiling.
    """
    bm25_flat, semantic_flat = _is_flat(bm25_scores), _is_flat(semantic_scores)
    if bm25_flat and semantic_flat:
        return [0.0 for _ in bm25_scores]
    if bm25_flat:
        return normalize(semantic_scores)
    if semantic_flat:
        return normalize(bm25_scores)
    bm25_norm = normalize(bm25_scores)
    semantic_norm = normalize(semantic_scores)
    return [alpha * b + (1 - alpha) * s for b, s in zip(bm25_norm, semantic_norm)]
