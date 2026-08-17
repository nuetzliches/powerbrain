# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest `master` | Yes |

## Reporting a Vulnerability

If you discover a security vulnerability in Powerbrain, please report it responsibly:

1. **Do not open a public issue.**
2. Use [GitHub Security Advisories](https://github.com/nuetzliches/powerbrain/security/advisories/new) to report the vulnerability privately.
3. Include: description, reproduction steps, affected components, and potential impact.

## Response Timeline

- **Acknowledgment:** within 3 business days
- **Assessment:** within 7 business days
- **Fix:** depending on severity, typically within 14 days for critical issues

## Scope

This policy covers the Powerbrain codebase and its Docker Compose deployment. Third-party dependencies (Qdrant, PostgreSQL, OPA, Ollama) should be reported to their respective maintainers.

## Dependency Advisories

Every pull request audits all Python requirement files with `pip-audit`. Where an
advisory has no upgrade path and the vulnerable code is not reachable from this
codebase, it is accepted rather than fixed — with the assessment, a review date
and the conditions that reopen it recorded in
[docs/dependency-audit.md](docs/dependency-audit.md). If you believe an accepted
entry is wrong, that is worth reporting through the process above.
