# Composit Integration

Powerbrain is a reference provider for [composit], an open-source
governance-as-code tool for AI-generated infrastructure. This directory
documents how powerbrain uses composit.

[composit]: https://github.com/nuetzliches/composit

## Files in this repo

| Path                               | Purpose                                    |
|------------------------------------|--------------------------------------------|
| `Compositfile`                     | SHOULD-state governance declaration (HCL)  |
| `composit.config.yaml`             | Scanner configuration                      |
| `.well-known/composit.json`        | Public provider manifest (served at root)  |

## Why publish a Compositfile on a public repo?

The typical use case for composit is private ops: declare intent, scan
your repo, fail CI on drift. On a public project the value is flipped:

1. **Self-documenting topology.** Adopters see the canonical service
   inventory expressed as code (resource limits, required components)
   instead of reverse-engineering it from `docker-compose.yml`.

2. **Policy surface as a first-class citizen.** The `policy` blocks in
   the Compositfile reference the existing `opa-policies/pb/` rules.
   This isn't drift detection — it's a public index of what
   governance layers powerbrain ships.

3. **Dogfooding the spec.** powerbrain is listed in composit's
   reference-provider set. Publishing a real `.well-known/composit.json`
   and a non-trivial Compositfile proves the format works on a
   production-grade OSS project.

## Running composit locally

```bash
# Install composit (see https://github.com/nuetzliches/composit)
cargo install --git https://github.com/nuetzliches/composit

# 1. Produce the IS-state
composit scan --no-providers

# 2. Compare against the SHOULD-state
composit diff --output terminal

# 3. HTML report (dark-mode, shareable)
composit diff --output html
open composit-diff.html
```

Expected output today: 0 errors, a small number of warnings
(`policy_file_missing` is NOT among them — the `.rego` files exist and
the paths are real; composit just doesn't execute them yet).

## Current diff baseline

On a clean scan of the main branch, composit should report:

- **Resources:** ~33 counted, well below every declared ceiling.
- **Providers:** 0 (powerbrain consumes no upstream providers; the
  approved-providers block in the Compositfile is deliberately empty).
- **Budget:** passes (no cost adapter attached).
- **Policies:** all 6 referenced `.rego` files resolved.

The 1 prometheus_config requirement and 1 workflow requirement are both
satisfied — these represent architectural invariants (observability and
CI gate must exist).

## Manifest hosting

`.well-known/composit.json` in this repo is the source of truth. When
powerbrain is deployed, the deployment's reverse proxy (Caddy, by
default) should expose this file at `/.well-known/composit.json`.

Composit clients can then discover powerbrain via:

```
composit scan --providers https://mcp.nuetzliche.it
```

## Known limitations

- Policy runtime evaluation through composit is **not yet
  implemented**. composit currently only verifies the file paths in
  each `policy` block resolve. Powerbrain's runtime already enforces
  the same `.rego` files against live requests via OPA at :8181.

- The `eu_ai_act` section of the manifest is an extension, not yet
  part of composit's spec. See the proposed RFC in the composit repo.
