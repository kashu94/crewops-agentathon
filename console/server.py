#!/usr/bin/env python3
"""Crew Ops Console -- local/VM front door: a stdlib `ThreadingHTTPServer`
wrapping the shared engine in `app.py`. Three controllers share one running
server and one Postgres ledger -- open this URL in three browser tabs, each
with a different `?controller=`, to see contention appear live across them,
like the reference UI this was modeled on.

Usage:
    python server.py                  # http://localhost:8600
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from app import Handler, PORT, _ensure_advisor_agents, _ensure_ledger_warm


def _warm_up() -> None:
    """Ledger and Advisor Agent setup, off the startup path.

    Both are network round trips to services outside this process (Postgres,
    Azure AI Foundry) that can be slow or briefly unreachable -- e.g. a cold
    credential chain the first time this runs somewhere new. Doing this
    before the socket opens meant the whole console was unreachable, even
    for the deterministic pipeline that needs neither, until both finished.
    """
    _ensure_ledger_warm()
    _ensure_advisor_agents()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=_warm_up, daemon=True).start()
    print(f"Serving on http://localhost:{PORT}")
    print("Open in 3 tabs with ?controller=Ananya+Iyer / Rohit+Malhotra / Divya+Rao")
    server.serve_forever()


if __name__ == "__main__":
    main()
