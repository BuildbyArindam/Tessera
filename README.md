# Tessera

Policy-bounded treasury agent for cross-border SMEs, built for the Airwallex Agentic Banking Hackathon.

An LLM **proposes** treasury actions. A deterministic policy engine **disposes**: every proposal is approved, escalated to a human, or blocked before anything touches money.

> Work in progress: Day 1 of 20. The full README (results, architecture diagram, quickstart) lands on Day 20.

## Quickstart (dev)

```bash
make setup      # venv + editable installs
make lint
make typecheck
make test
```

## Dependency rule

`tessera_core` imports nothing from other packages. `tessera_agent` can read core types but cannot import `tessera_airwallex`. Only the worker calls Airwallex, and only after `tessera_core.policy` approves. Enforced with import-linter from Day 13.
