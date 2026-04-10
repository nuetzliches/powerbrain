## Summary

What does this PR do and why?

## Changes

- ...

## Test plan

- [ ] Unit tests pass (`python -m pytest -m 'not integration'`)
- [ ] OPA tests pass (`opa test opa-policies/pb/ -v`)
- [ ] Docker images build
- [ ] Manual verification: ...

## Checklist

- [ ] Code follows project conventions (async/await, type hints, Pydantic models)
- [ ] No hardcoded secrets or internal URLs
- [ ] Graceful degradation preserved (reranker down, OPA down, etc.)
- [ ] CLAUDE.md updated if architecture changed
