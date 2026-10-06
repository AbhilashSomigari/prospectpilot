## What

## Why

## Checks
- [ ] `make check` passes (ruff, mypy --strict, unit tests)
- [ ] integration tests pass if the pipeline/DB changed (`make test-int`)
- [ ] eval gate result attached if writer/critic/prompts/graders changed (`make eval PROVIDER=…`)
- [ ] guardrails unchanged (sandbox-only sending, allowed sources, no SMTP probing)
