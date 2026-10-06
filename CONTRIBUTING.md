# Contributing

```bash
make install   # uv sync + git hooks (pre-commit runs ruff and the unit tests)
make check     # ruff, mypy --strict on src/, unit tests — what CI runs first
make up && make test-int   # integration + e2e against the compose stack (separate *_test DB)
make eval-smoke            # EvalForge suite in mock mode (plumbing check, as in CI)
```

- Conventional commits (`feat(scope): …`, `fix(scope): …`).
- Unit tests must not touch the network: record HTTP fixtures and serve them with `respx`.
- Changes to the writer, critic, prompts or graders: run `make eval PROVIDER=ollama` (or
  anthropic) and include the gate result. Never report mock-provider numbers as quality results.
- `RESULTS.md` is generated (`make results`); don't edit numbers by hand.
- Keep the guardrails in [SECURITY.md](SECURITY.md) intact.
