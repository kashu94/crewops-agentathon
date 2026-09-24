"""Answer object -> controller prose.

Templates first, model second. The deterministic renderer below produces a
correct, citable answer with no model in the loop at all. That means the
system degrades to "terse but accurate" rather than to "broken" when the
Explainer Agent is unavailable, and it gives the verifier something to check
even on that path.

`polish()` sends the template output to the Explainer Agent (`agents.py`) to
rewrite into something a controller would actually say. It may reword, but
may not introduce a fact. `verifier.verify()` runs after it in `deploy.py`
to enforce that — a rejected draft falls back to `render()`'s template
verbatim.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable

import prompts
from schemas import (
    AdvisorResponse,
    Intent,
    Citation,
    ConsequenceAnswer,
    LookupAnswer,
    NotificationAnswer,
    Option,
    ReplacementAnswer,
    RuleVerdict,
)


ROW_LIMIT = 25
"""Rows shown before a lookup listing is truncated — generous enough to
clear every tier-1 gold answer (BLR alone carries a dozen reserves), with
room to spare."""


def fmt_value(value: Any) -> str:
    """Render a backend value as a controller would read it.

    `datetime.date`/`time`/`Decimal` reprs are unreadable, and worse, get
    misread by the verifier as unsourced numeric claims. ISO strings fix
    both problems.
    """
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(fmt_value(v) for v in value)
    if isinstance(value, dict):
        return " ".join(f"{k}={fmt_value(v)}" for k, v in value.items())
    return str(value)


def who(crew_id: str | None, name: str | None = None,
        rank: str | None = None) -> str:
    """How a crew member is written, everywhere, without exception.

    `Captain A. Nair (C-1042)` — the person first, since that's who the
    controller phones, and the id in brackets since that's what goes into
    the roster system and two people can share a surname. Falling back to
    the bare id when no name is loaded is correct; inventing one is not.
    """
    if not crew_id:
        return ""
    label = " ".join(str(part) for part in (rank, name) if part)
    return f"{label} ({crew_id})" if label else crew_id


def _inr(amount: int) -> str:
    return f"₹{amount:,}"


def _hours(value: float) -> str:
    """3.0 -> '3h'; 1.33 -> '1h20m'. Matches how the answer keys read."""
    whole = int(value)
    minutes = round((value - whole) * 60)
    if minutes == 60:
        whole, minutes = whole + 1, 0
    return f"{whole}h{minutes:02d}m" if minutes else f"{whole}h"


# --------------------------------------------------------------------------
# Rule verdicts
# --------------------------------------------------------------------------


def render_verdict(v: RuleVerdict) -> str:
    head = f"{v.rule_id}  {v.status}"
    if v.detail:
        return f"{head} — {v.detail}"
    if v.used is not None and v.limit is not None:
        return f"{head} — {v.used} / {v.limit}"
    return head


def render_verdicts(verdicts: list[RuleVerdict]) -> str:
    if not verdicts:
        return ""
    failures = [v for v in verdicts if v.failed]
    lines = [render_verdict(v) for v in (failures or verdicts)]
    header = "Blocking:" if failures else f"All {len(verdicts)} rules pass:"
    return header + "\n" + "\n".join(f"  {line}" for line in lines)


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


def render_option(option: Option, show_rank: bool = True) -> str:
    prefix = f"#{option.rank} " if show_rank and option.rank else ""
    parts = [f"{prefix}{option.action}", _inr(option.cost_inr)]

    if option.reachability_minutes is not None:
        parts.append(f"reachable in {option.reachability_minutes} min")

    if option.delay_hours:
        parts.append(f"{_hours(option.delay_hours)} delay")
    if option.blast_radius:
        parts.append(f"blast radius {option.blast_radius}")
    if not option.legal:
        blocking = [v.rule_id for v in option.verdicts if v.failed]
        parts.append("ILLEGAL" + (f" ({', '.join(blocking)})" if blocking else ""))
    if option.unlock:
        parts.append(f"unlocks if {option.unlock}")

    return " · ".join(parts)


def render_strategies(strategies: list[Option]) -> str:
    """The four-line executive summary: one representative per strategy,
    cheapest first, cancel always last (`find_options` already orders the
    list this way — this just labels each line, it doesn't re-sort). Not
    `render_option()`, because that appends its own "· Xh delay", which
    would double up with the delay this strategy's `action` text already
    states."""
    if not strategies:
        return ""
    lines = ["Strategies considered:"]
    for s in strategies:
        lines.append(f"  #{s.rank} {s.strategy_label} — {s.action} · {_inr(s.cost_inr)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Per-tier narratives
# --------------------------------------------------------------------------


_COLUMN_ORDER: tuple[str, ...] = (
    "crew_id", "name", "rank", "role", "base",
    "flight_id", "flight_no", "pairing_id", "aircraft", "aircraft_type",
    "date", "dep_station", "arr_station", "dep_utc", "arr_utc",
    "oncall_start_utc", "oncall_end_utc", "report_utc", "release_utc",
    "cert_type", "valid_from", "valid_to",
)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union of the rows' keys, preferred columns first."""
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    ranked = [c for c in _COLUMN_ORDER if c in seen]
    return ranked + [c for c in seen if c not in ranked]


def render_lookup(answer: LookupAnswer) -> str:
    """A tier-1 result as an aligned table."""
    if not answer.rows:
        return "No records match that query."

    noun = "record" if answer.count == 1 else "records"
    shown = answer.rows[:ROW_LIMIT]

    # Rows of different shapes are different questions, and interleaving
    # them would produce a mostly-blank table. Group by shape instead and
    # give each its own table.
    groups: list[list[dict[str, Any]]] = []
    signatures: list[tuple[str, ...]] = []
    for row in shown:
        signature = tuple(sorted(row))
        if signature in signatures:
            groups[signatures.index(signature)].append(row)
        else:
            signatures.append(signature)
            groups.append([row])

    lines = [f"{answer.count} {noun}."]
    for group in groups:
        lines.extend(_table(group))
    if answer.count > ROW_LIMIT:
        lines.append("  (list truncated)")
    return "\n".join(lines)


def _table(shown: list[dict[str, Any]]) -> list[str]:
    """One aligned table for one set of same-shaped rows."""
    columns = _columns(shown)

    constant: list[str] = []
    if len(shown) > 1:
        for column in list(columns):
            values = {fmt_value(row.get(column, "")) for row in shown}
            if len(values) == 1 and len(next(iter(values))) > 20:
                constant.append(f"{column}: {values.pop()}")
                columns.remove(column)

    cells = [[fmt_value(row.get(c, "")) for c in columns] for row in shown]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]

    def line(values: list[str]) -> str:
        padded = [v.ljust(w) for v, w in zip(values, widths)]
        return "  " + "  ".join(padded).rstrip()

    lines = [f"  every row — {c}" for c in constant]
    lines += ["", line(columns), "  " + "  ".join("─" * w for w in widths)]
    lines += [line(row) for row in cells]
    return lines


def render_replacement(answer: ReplacementAnswer) -> str:
    """Lead with the recommendation; the ranking is support, not the answer."""
    lines: list[str] = []

    if answer.verdicts:
        subject = who(answer.subject, answer.subject_name,
                      answer.subject_rank) or "That assignment"
        blocking = [v for v in answer.verdicts if v.failed]
        if blocking:
            lines.append(f"▸ {subject} would breach "
                         f"{len(blocking)} rule{'s' if len(blocking) > 1 else ''}:")
            lines += [f"  {render_verdict(v)}" for v in blocking]
        else:
            lines.append(f"▸ {subject} is legal — all "
                         f"{len(answer.verdicts)} rules pass.")
            lines += [f"  {render_verdict(v)}" for v in answer.verdicts]
        if not answer.options:
            return "\n".join(lines)
        lines.append("")

    if rec := answer.recommended:
        head = f"▸ {rec.action} — {_inr(rec.cost_inr)}"
        if rec.delay_hours:
            head += f", {_hours(rec.delay_hours)} delay"
        else:
            head += ", no delay"
        lines.append(head)

        if answer.equal_cost_alternatives:
            n = answer.equal_cost_alternatives
            lines.append(f"  ({n} other option{'s' if n > 1 else ''} cost the same "
                         f"— this is not a uniquely correct choice.)")
        if rec.rules_checked:
            lines.append(f"  Clears all {len(rec.rules_checked)} rules.")
        lines.append("")

    if strategies_text := render_strategies(answer.strategies):
        lines.append(strategies_text)

    if answer.uncovered_flights:
        lines.append(
            f"{len(answer.uncovered_flights)} flight(s) uncrewed: "
            + ", ".join(answer.uncovered_flights)
        )
    if answer.at_risk_flights:
        lines.append(
            f"{len(answer.at_risk_flights)} downstream at risk: "
            + ", ".join(answer.at_risk_flights)
        )
    if answer.passengers_affected:
        lines.append(f"{answer.passengers_affected} passengers affected.")

    if answer.options:
        cancel = next((o for o in answer.options if not o.crew_id), None)
        alternatives = [
            o for o in answer.options if o.crew_id
            and o.crew_id != (answer.recommended.crew_id if answer.recommended else None)
        ]

        if alternatives:
            lines.append("\nAlternatives:")
            lines += [f"  {render_option(o)}" for o in alternatives]
            if answer.next_tier_premium_inr:
                lines.append(f"  ({_inr(answer.next_tier_premium_inr)} more "
                             f"than the recommended option.)")

        if cancel:
            lines.append(f"\nAgainst cancelling: {_inr(cancel.cost_inr)}")
            if answer.cancellation_multiple:
                lines.append(f"  {answer.cancellation_multiple}× the recommended option. "
                             f"Even the deadhead is far cheaper than cancelling.")
    elif not answer.near_misses and answer.funnel:
        lines.append("\nNo legal option found.")

    if answer.near_misses:
        lines.append("\nNear misses — not legal now, but reachable:")
        lines += [f"  {render_option(o, show_rank=False)}" for o in answer.near_misses]

    if answer.funnel:
        considered = answer.funnel[0].count
        legal = answer.funnel[-1].count
        lines.append(f"\nConsidered {considered}, {legal} legal:")
        for stage in answer.funnel:
            if stage.dropped:
                lines.append(f"  −{stage.dropped} {stage.stage}: {stage.reason}")

    if answer.excluded:
        # The funnel above is stage counts; this is "who, specifically, and
        # why". It was already captured into the answer but never rendered
        # until now, so "who got excluded and why" previously had nothing
        # to draw on but whatever the model recalled from the raw tool
        # result itself.
        shown = answer.excluded[:ROW_LIMIT]
        lines.append(f"\nExcluded ({len(answer.excluded)}):")
        for e in shown:
            subject = who(e.get("crew_id"), e.get("name"), e.get("rank")) or e.get("crew_id", "?")
            lines.append(f"  {subject}: {e.get('reason', '')}")
        if len(answer.excluded) > ROW_LIMIT:
            lines.append(f"  ... and {len(answer.excluded) - ROW_LIMIT} more.")

    return "\n".join(lines)


def render_consequence(answer: ConsequenceAnswer) -> str:
    lines: list[str] = []

    if answer.joint_plan:
        plan = answer.joint_plan
        total = plan.get("total_cost_inr")
        if total is not None:
            lines.append(f"Optimal joint plan — {_inr(int(total))} total.")
        for key, value in plan.items():
            if key.startswith("assign") and isinstance(value, dict):
                lines.append(f"  {key}: {value.get('action', '')}")
        if (alts := plan.get("equal_cost_alternatives")) and int(alts) > 1:
            lines.append(
                f"  {int(alts) - 1} other assignments cost exactly the same — "
                "this is one of several equally correct plans."
            )

    if strategies_text := render_strategies(answer.strategies):
        lines.append(f"\n{strategies_text}")

    if answer.options:
        lines.append("\nRanked options:")
        lines += [f"  {render_option(o)}" for o in answer.options]

    if br := answer.blast_radius:
        lines.append(
            f"\nBlast radius: {br.nodes} nodes · {br.flights} flights · "
            f"{br.aircraft} aircraft · {br.passengers} passengers"
        )

    if answer.world_diff is not None:
        # An empty `changed` list is a real, computed finding — "nobody's
        # legal status flips under this scenario" — not the absence of one.
        # Rendering nothing for it would be indistinguishable from the tool
        # never having run at all, which then falls through to
        # `render_unavailable()` and can surface a stale error from an
        # earlier, already-superseded attempt instead of this result.
        changed = answer.world_diff.get("changed") or []
        pairing_id = answer.world_diff.get("pairing_id")
        delay = answer.world_diff.get("delay_hours")
        scope = f" for {pairing_id}" if pairing_id else ""
        scope += f" with a {delay}h delay" if delay else ""
        if changed:
            lines.append(f"\n{len(changed)} change(s){scope} versus the base world:")
            for c in changed:
                lines.append(
                    f"  {c.get('crew_id')} ({c.get('role')}): "
                    f"{c.get('legal_before')} -> {c.get('legal_after')} "
                    f"— {c.get('detail')}")
        else:
            lines.append(f"\nNo change{scope}: every crew member's legal "
                         f"status is unaffected.")

    return "\n".join(lines)


def has_content(answer: Any) -> bool:
    """Whether the answer object actually carries a finding."""
    match answer:
        case LookupAnswer() as a:
            return bool(a.rows)
        case ReplacementAnswer() as a:
            return bool(a.options or a.near_misses or a.uncovered_flights
                        or a.funnel or a.verdicts)
        case ConsequenceAnswer() as a:
            return bool(a.options or a.blast_radius or a.world_diff or a.joint_plan)
    return False


def render_unavailable(response: AdvisorResponse) -> str:
    """What to say when the tools that would answer this did not run."""
    failed = [e for e in response.trace if e.error]
    if not failed:
        return "No data was returned for this question."

    about_the_query = [e for e in failed
                       if e.error.startswith(("NEEDS_CONFIRMATION",
                                              "UNRESOLVED_ENTITY",
                                              "AMBIGUOUS_QUERY"))]
    if about_the_query:
        seen: list[str] = []
        for entry in about_the_query:
            detail = entry.error.split(":", 1)[-1].strip()
            if detail not in seen:
                seen.append(detail)
        return "\n\n".join(seen)

    lines = ["Cannot answer this yet — the tools it needs are unavailable:"]
    for entry in failed:
        detail = entry.error.split(":", 1)[-1].strip()
        lines.append(f"  {entry.tool}: {detail}")
    lines.append("\nThis is a missing capability, not a finding about the operation.")
    return "\n".join(lines)


def render(response: AdvisorResponse) -> str:
    """Deterministic prose for any answer body. No model involved."""
    match response.answer:
        case LookupAnswer() as a:
            body = render_lookup(a) if a.rows else ""
        case ReplacementAnswer() as a:
            body = render_replacement(a)
        case ConsequenceAnswer() as a:
            body = render_consequence(a)
        case NotificationAnswer() as a:
            body = a.message
        case _:
            body = ""

    return body.strip() or render_unavailable(response)


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


def collect_citations(response: AdvisorResponse) -> list[Citation]:
    """Every rule and record the answer leans on, deduplicated."""
    seen: set[tuple[str, str]] = set()
    citations: list[Citation] = []

    def add(kind: str, ident: str, source: str | None = None) -> None:
        if (kind, ident) not in seen:
            seen.add((kind, ident))
            citations.append(Citation(kind=kind, id=ident, source=source))

    options: list[Option] = []
    if isinstance(response.answer, (ReplacementAnswer, ConsequenceAnswer)):
        options = list(response.answer.options)
    if isinstance(response.answer, ReplacementAnswer):
        options += response.answer.near_misses

    for option in options:
        for rule_id in option.rules_checked:
            add("rule", rule_id)
        for verdict in option.verdicts:
            add("rule", verdict.rule_id)
        if option.crew_id:
            add("record", option.crew_id, "crew.json")

    for entry in response.trace:
        if entry.tool == "explain_rule" and (rid := entry.args.get("rule_id")):
            add("rule", str(rid))

    return citations


# --------------------------------------------------------------------------
# Explainer Agent pass
# --------------------------------------------------------------------------

# Both passes below append `prompts.naming_rule()` instead of restating it.
# How a person is written is one rule for the whole system. It lives in
# `prompts/system.md`, the Resolution Advisor reads it there, and these use
# the same words so the two can't drift apart.

_LOOKUP_INSTRUCTIONS = """\
You are answering an airline crew controller's factual question.

Below is a question and the rows the tools returned for it. Answer the
question in one short paragraph, in plain language, using only those rows.

Rules, in order of importance:

1. Every name, id, number, date and status you write must appear in the rows.
   Invent nothing. If the rows do not answer part of the question, say that
   part is not in the data.
2. Do NOT recommend anything, do NOT infer a situation, do NOT describe a
   problem. Nothing here is a disruption; it is a record.
3. Name people exactly as the rule at the end of this message says.
4. Lead with what was asked. Mention what a controller would want next only
   if it is in the rows — current pairing, duty headroom, anything expiring.
5. No preamble, no "based on the data provided". Two to five sentences.
6. Your prose is the ENTIRE answer — state the actual values: the hours, the
   dates, the ids, the names.
7. If the rows are a list of people, list them — id, name and the one field
   that was asked about, one per line. Do not summarise a roster into
   "several crew are on reserve".
8. Plain English. No fixed-width columns, no ASCII rules — it is read in a
   chat bubble a few hundred pixels wide.
"""

_POLISH_INSTRUCTIONS = """\
You are writing for an airline crew controller under time pressure, in a
chat widget that may be the only place they see this answer — a ranked-
options card is not guaranteed to be visible anywhere else on their screen.

Write three to six sentences:

1. The recommendation, and the single reason it wins.
2. The other legal alternatives, briefly — action and cost for each, as a
   compact list. A controller comparing options needs to see them, not just
   be told one was picked for them.
3. Only if it is not already obvious: the one thing the controller should
   know before accepting — a risk, a trade-off, or what to fall back to.

Do NOT recite rule ids with their hour figures unless that rule is itself the
reason the recommendation is what it is. Do NOT repeat the funnel counts.

Absolute constraint: you may reword, reorder and compress. You may NOT add any
identifier, number, name or claim that is not already present. If something is
missing, leave it missing — a verifier will reject this answer otherwise.
"""

_IMPACT_INSTRUCTIONS = """\
You are writing for an airline crew controller under time pressure.

This answer is an assessment, NOT a recommendation. The controller asked what
an event breaks — which flights are uncrewed, who is now at risk, what the
knock-on is. There is no options card on this answer, so do not tell them to
accept anything, and do not name a top-ranked option. Nothing has been chosen
yet.

Answer the question that was asked, in two or three sentences:

1. What is broken, concretely — the flights, the pairing, the people.
2. The consequence that is not visible at a glance: what breaks next, or who
   is closest to a limit as a result.

Do not recommend a replacement unless they asked for one. Do not recite rule
ids with their hour figures.

Absolute constraint: you may reword, reorder and compress. You may NOT add any
identifier, number, name or claim that is not already present. If something is
missing, leave it missing — a verifier will reject this answer otherwise.
"""


def _with_naming(instructions: str) -> str:
    return f"{instructions.rstrip()}\n\n{prompts.naming_rule()}\n"


LOOKUP_INSTRUCTIONS = _with_naming(_LOOKUP_INSTRUCTIONS)
POLISH_INSTRUCTIONS = _with_naming(_POLISH_INSTRUCTIONS)
IMPACT_INSTRUCTIONS = _with_naming(_IMPACT_INSTRUCTIONS)


ExplainFn = Callable[[str, str], str]
"""What `ExplainerAgent.run(system, user_content)` looks like: a single,
tool-free Foundry Agent call that returns text."""


def polish(response: AdvisorResponse, explain: ExplainFn | None = None) -> str:
    """Rewrite template output into controller language via the Explainer Agent.

    Falls back to the template verbatim when no Explainer Agent is wired up.

    Tier 1 gets its own instructions instead of the recommendation ones, and
    keeps its tables underneath the prose. Two lookup shapes still get no
    model pass at all: an answer with no rows has only the tools' own error
    text to offer, and EXPLAIN_RULE's answer *is* the regulation text, so
    summarising it could only drift from it.
    """
    template = render(response)
    if explain is None:
        return template

    if isinstance(response.answer, LookupAnswer):
        if not response.answer.rows or response.intent is Intent.EXPLAIN_RULE:
            return template
        text = (explain(LOOKUP_INSTRUCTIONS,
                        f"Question: {response.query}\n\n{template}") or "").strip()
        return text or template

    # Never paraphrase a tool failure. The error text is already written for
    # a controller and often carries the only actionable content, e.g.
    # "there is no crew C-1045, did you mean C-1042?".
    if not has_content(response.answer):
        return template

    # An answer with no options is an assessment, not a recommendation.
    has_options = bool(getattr(response.answer, "options", None))
    text = (explain(POLISH_INSTRUCTIONS if has_options else IMPACT_INSTRUCTIONS,
                    template) or "").strip()
    return text or template
