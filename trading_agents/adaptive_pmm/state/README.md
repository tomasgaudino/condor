# state/ — persisted agent memory

Every tick of the adaptive supervisor writes here. Layout:

```
state/
├── audit_log.jsonl          # append-only — one line per proposal (proposed/approved/rejected/applied/snoozed/expired)
├── anti_evidence_log.jsonl  # patterns that backtested worse than baseline (one line per occurrence)
├── last_changes.json        # canonical_id → {field: {old_value, new_value, applied_at}} for fast cooldown lookup
├── agent_status.json        # active | paused, reason, since
├── capital_history/<canonical_id>.jsonl     # written by routines/capital_state.py
├── regime_history/<connector>__<pair>.jsonl # written by routines/market_regime.py
└── controller_performance/<canonical_id>.jsonl  # written by routines/controller_performance.py
```

See `.planning/strategy-framework/MEMORY_SPEC.md` for the schemas and lifecycle.

Today (2026-05-12, end of Phase 5.5) the directory only carries this README
and a `.gitkeep`. Routines write under `state/<routine>/` at the repo root
for now (see `state/controller_performance/` in the repo root). They migrate
here in Phase 5.6 when the agent loop runs and owns the state lifecycle.
