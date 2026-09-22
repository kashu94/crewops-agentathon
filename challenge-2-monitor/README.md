# Challenge 2: Monitor with Application Insights

Time: ~20 minutes

## Objectives

By the end of this challenge, you will have:

- ✅ OpenTelemetry GenAI tracing enabled on your Foundry project
- ✅ A traced agent call flowing into Application Insights
- ✅ Verified you can see it in the Foundry portal's **Traces** panel and in
  Azure Monitor's Transaction Search

## Context

Three model calls sit inside every answer this scenario produces (Triage,
Resolution Advisor, Explainer), plus up to eight tool round-trips inside the
Resolution Advisor's own loop. When a controller-facing answer looks wrong,
the question is always "which of those calls did it, and with what input?" —
and the only honest way to answer that is a trace, not a guess.

## Get Started

1. Confirm `.env` has `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` and
   `APPLICATIONINSIGHTS_CONNECTION_STRING` set — `challenge-0-setup/deploy.sh`
   wrote both.

2. Run:

   ```bash
   cd challenge-2-monitor
   python monitor.py
   ```

   This instruments the SDK (`AIProjectInstrumentor`), wires up the Azure
   Monitor exporter, makes one traced agent call, and waits 30 seconds for
   the trace to land.

3. In the **Foundry portal** → your project → **Monitor** → **Traces**, find
   the `crew-tracing-test-agent` call and open it — you should see the full
   request/response, token counts, and latency.

4. In the **Azure Portal** → your Application Insights resource → 
   **Transaction search**, filter to the last 5 minutes and `Event type:
   Dependency` to see the same call from the Azure Monitor side.

5. **Now point the same tracing setup at the real pipeline.** Re-run
   `challenge-1-build/agents.py` with the same environment variables set —
   every Triage / Resolution Advisor / Explainer call it makes, and every
   tool call the Resolution Advisor dispatches, will show up the same way.
   This is what turns "the answer looked right" into "here is the exact tool
   call, with these arguments, that produced the number in it" — the
   verifier already checks that at answer time; tracing is what lets a human
   check it after the fact.

## Success Criteria

- [ ] `monitor.py` runs to completion with no errors
- [ ] The `crew-tracing-test-agent` call is visible in the Foundry portal's
      Traces panel
- [ ] The same call is visible in Application Insights' Transaction Search

Next: [Challenge 3 — Evaluate](../challenge-3-evaluate/README.md)
