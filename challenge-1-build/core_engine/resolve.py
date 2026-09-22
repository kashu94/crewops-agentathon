"""Entity resolution — suggest, never substitute.

When a controller types an id that is not in the database, three things must
happen and one must not:

  * say plainly that it does not exist;
  * offer the closest real candidates, each with enough detail to recognise;
  * never fail silently, and never return an empty result as though the
    question had been answered;
  * **never quietly act on the suggestion.**

That last one is the whole point. `C-1042` and `C-1024` differ by one
transposed digit and, in this dataset, one of them is a real captain and the
other is nobody. Auto-correcting would silently dispatch a different human
being to an aircraft — the exact failure this system exists to prevent. So a
near match is returned as a *question for the controller*, never as an answer.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# Ids one transposition apart are the highest-risk near miss: they look like
# typos, they are typos, and acting on the wrong one is dangerous.
TRANSPOSE_CONFIDENCE = "likely"
CLOSE_CONFIDENCE = "possible"

MAX_SUGGESTIONS = 4


@dataclass(slots=True)
class Suggestion:
    value: str
    label: str
    confidence: str

    def __str__(self) -> str:
        return f"{self.value} ({self.label})"


@dataclass(slots=True)
class Resolution:
    kind: str
    query: str
    exists: bool
    suggestions: list[Suggestion] = field(default_factory=list)
    universe_size: int = 0
    example: str = ""

    @property
    def likely(self) -> list[Suggestion]:
        """Transpositions — a rearrangement of the very same digits."""
        return [s for s in self.suggestions if s.confidence == TRANSPOSE_CONFIDENCE]

    @property
    def needs_confirmation(self) -> bool:
        """Only a probable typo earns a "did you mean". Anything else is a
        wrong id that happens to have a neighbour."""
        return not self.exists and bool(self.likely)

    def message(self) -> str:
        if self.exists:
            return f"{self.query} exists"

        held = (f"The database holds {self.universe_size} "
                f"{self.kind}{'s' if self.universe_size != 1 else ''}")

        if likely := self.likely:
            best, others = likely[0], likely[1:]
            text = f"There is no {self.kind} {self.query}. Did you mean {best}?"
            if others:
                text += " Or " + ", ".join(str(s) for s in others) + "?"
            return text + " Confirm which and I will run it — I will not guess."

        if self.suggestions:
            # Near in spelling but not a transposition. String distance cannot
            # separate a typo from a wrong id — C-1024/C-1042 and
            # C-9999/C-4999 both score 0.833 — so this says what it found
            # rather than implying a correction.
            nearest = ", ".join(str(s) for s in self.suggestions)
            return (f"There is no {self.kind} {self.query}, and nothing that looks "
                    f"like a typo of it. Nearest existing: {nearest}. "
                    f"If you meant one of those, say so explicitly.")

        return (f"There is no {self.kind} {self.query}, and nothing close to it. "
                + held + (f", for example {self.example}." if self.example else "."))


def _transpositions(value: str) -> set[str]:
    """Every id reachable by swapping two adjacent characters."""
    out = set()
    for i in range(len(value) - 1):
        if value[i] == value[i + 1]:
            continue
        out.add(value[:i] + value[i + 1] + value[i] + value[i + 2:])
    return out


def _digit_variants(value: str) -> set[str]:
    """Ids reachable by transposing digits anywhere in the numeric part.

    "C-1042" -> "C-1024", "C-4102", ... Controllers read these aloud and
    mistype them constantly, and adjacent-swap alone misses the pattern where
    two non-adjacent digits are exchanged.
    """
    match = re.search(r"\d+", value)
    if not match:
        return set()
    head, digits, tail = (value[:match.start()], match.group(),
                          value[match.end():])
    out = set()
    for i in range(len(digits)):
        for j in range(i + 1, len(digits)):
            if digits[i] == digits[j]:
                continue
            swapped = list(digits)
            swapped[i], swapped[j] = swapped[j], swapped[i]
            out.add(head + "".join(swapped) + tail)
    return out


def resolve(
    kind: str,
    query: str,
    universe: dict[str, Any],
    label: Callable[[Any], str] | None = None,
) -> Resolution:
    """Whether `query` names something real, and if not, what is nearest.

    `universe` maps id -> record; `label` renders a record so a controller can
    recognise it. Showing "C-1024" alone is not enough to confirm against —
    "C-1024 (M. Rao, First Officer, DEL)" is.
    """
    ids = list(universe)
    render = label or (lambda _: "")

    if query in universe:
        return Resolution(kind, query, exists=True, universe_size=len(ids))

    seen: set[str] = set()
    suggestions: list[Suggestion] = []

    def offer(candidate: str, confidence: str) -> None:
        if candidate in universe and candidate not in seen:
            seen.add(candidate)
            suggestions.append(
                Suggestion(candidate, render(universe[candidate]), confidence))

    # Transpositions first — the typo that most often produces another real id.
    for candidate in sorted(_transpositions(query) | _digit_variants(query)):
        offer(candidate, TRANSPOSE_CONFIDENCE)

    # Then general nearness, for a wrong digit or a missing character.
    for candidate in difflib.get_close_matches(query, ids, n=MAX_SUGGESTIONS,
                                               cutoff=0.8):
        offer(candidate, CLOSE_CONFIDENCE)

    return Resolution(
        kind=kind,
        query=query,
        exists=False,
        suggestions=suggestions[:MAX_SUGGESTIONS],
        universe_size=len(ids),
        example=ids[0] if ids else "",
    )
