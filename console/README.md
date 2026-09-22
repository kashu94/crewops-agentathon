# Crew Ops Console (bonus)

A browser UI in front of `challenge-1-build`'s engine — Command Center,
Disruption Workspace, Flights & Gates, and a Decision Ledger — built to show
three things the CLI-only challenges can't: the four-strategy resilience
ranking, cross-disruption contention across multiple controllers, and a real
commit that writes into the shared Postgres schema.

This isn't part of the Microsoft challenge-0..4 track (that stays CLI/agent
only, matching the lab template). It's an additional demo surface for the
same backend.

## What it demonstrates

- **Resilience score + 4 recommendation policies** (Balanced / Cheapest /
  Fastest / Most resilient) on every disruption with a named crew member —
  `core_engine/world.py::resilience_score` and `core_engine/port.py`'s
  `policies` block in `find_options`.
- **Cross-disruption contention**: open two disruptions that compete for the
  same reserve pool (try `S6A` and `S6B` — the vendored "two simultaneous
  captain sick calls" scenario) in two different browser tabs as two
  different controllers, and each sees the other's claim on a shared
  candidate.
- **Commit that sticks**: approving an option writes a real row into
  `pairing_crew` and an audit row into `controller_decisions` — the same
  tables the wider schema already defines. Any other open disruption that
  shares a date with the committed pairing re-evaluates on its next load and
  correctly excludes that crew member as `double-booked`, via the existing
  `RULE-REST-04` check — no bespoke exclusivity logic.

## Run it

```bash
cd console
python server.py
```

Requires `LEDGER_DATABASE_URL` in the repo-root `.env` (contention and
commit are no-ops without it — everything else still works). Open
`http://localhost:8600` in three browser tabs with a different
`?controller=` each:

```
http://localhost:8600/?controller=Ananya%20Iyer
http://localhost:8600/?controller=Rohit%20Malhotra
http://localhost:8600/?controller=Divya%20Rao
```

(or use the controller switcher in the top bar — it's just a query param).

## Demo script

1. As **Divya Rao**, open **S6A** (Disruption Workspace → #S6A). Note the top
   recommendation and its resilience score.
2. As **Ananya Iyer** (a different tab), open **S6B**. The contention box at
   the top names S6A directly, and the same candidates show a `contended`
   badge in the options table.
3. Back on Divya's tab, click a recommendation card, then **Approve &
   commit**.
4. Refresh Ananya's S6B tab — the committed crew member has moved from the
   options table into **Excluded candidates**, reason `double-booked`, and
   the recommendation has shifted to the next-cheapest legal option.
5. Check **Decision Ledger** — the commit is there, in the same table the
   reference schema's own fixture rows live in.

## Cleaning up test data

Everything this console writes lives in three places, easy to reset without
touching anything else in the shared database:

```sql
DELETE FROM open_disruptions WHERE disruption_id IN ('S1','S2','S3','S4','S5','S6A','S6B');
DELETE FROM pairing_crew WHERE pairing_id = '<the pairing you committed to>' AND crew_id = '<the crew_id you committed>';
DELETE FROM controller_decisions WHERE request_id = '<the disruption_id you committed>';
```

`open_disruptions`/`open_disruption_candidates` are new tables this bonus
track added — dropping them entirely is safe and won't affect anything else:

```sql
DROP TABLE IF EXISTS open_disruption_candidates;
DROP TABLE IF EXISTS open_disruptions;
```

## Deploying to Azure

This is a second, separate deployable from the Foundry agents in
`challenge-0-setup`/`challenge-4-deploy` — it doesn't call Foundry at all, it
calls `challenge-1-build`'s engine directly and reads/writes the Postgres
ledger. It's a single Python process (stdlib `http.server`, no framework),
so it deploys as one container:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt
EXPOSE 8600
CMD ["python", "console/server.py"]
```

Then, either:

- **Azure Container Apps** (recommended — simplest for a single stateless
  container that talks out to Postgres): `az containerapp up` pointed at
  this Dockerfile, with `LEDGER_DATABASE_URL` set as a secret/env var on the
  container app.
- **Azure App Service** (Linux, custom container): same Dockerfile, deploy
  via `az webapp create` + `az webapp config container set`.

Either way, `LEDGER_DATABASE_URL` is the only secret to configure — the rest
of the app has no other external dependency (the dataset is vendored JSON,
already in the image).
