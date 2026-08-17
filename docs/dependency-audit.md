# Dependency Audit and Accepted Risks

**Scope:** how Powerbrain audits its Python dependencies, and the ledger of
advisories that were assessed and consciously accepted rather than fixed.
**Status:** living document — every accepted entry carries a review date and the
triggers that reopen it.

## How the audit runs

The `security-scan` job in `.github/workflows/pr-validate.yml` runs
`pip-audit --strict` over **every** tracked `requirements*.txt`, discovered via
`git ls-files` rather than a hand-kept list. The per-file result is written to
the GitHub job summary, and a finding raises a workflow annotation.

The step is `continue-on-error: true`. A CVE published in a transitive
dependency is not something a PR author can fix, and blocking every unrelated PR
on it converts a security signal into an obstacle to route around. The trade
only works if a finding is actually visible, which is why the result goes to the
job summary instead of a collapsed log group (see [#257]).

A finding is then triaged into exactly one of two outcomes:

1. **Fix it** — bump the pin, or bump the direct dependency that pulls the
   vulnerable package in. This is the default.
2. **Accept it** — only when the vulnerable code is not reachable from this
   codebase *and* there is no upgrade path. It gets an entry below, a
   `--ignore-vuln` suppression in the workflow, a review date, and the
   conditions that reopen it.

Suppression is what keeps the audit readable: with known-and-assessed advisories
filtered out, anything the summary reports is new and unassessed. An audit that
always shows the same three findings trains everyone to skip it.

## Accepted risks

### A-01 — `cryptography` 48.0.1, three advisories, capped by `presidio-anonymizer`

| Field | Value |
| --- | --- |
| Package | `cryptography==48.0.1` (transitive) |
| Advisories | PYSEC-2026-3552 (CVE-2026-69247, GHSA-g6cj-pr64-35w5)<br>PYSEC-2026-3553 (CVE-2026-69249, GHSA-jwv3-5hgf-82ww)<br>PYSEC-2026-3554 (CVE-2026-69248, GHSA-m2h6-j472-rp4c) |
| Affected image | `pb-ingestion` only |
| Assessed | 2026-08-18 |
| Decision | Accepted — vulnerable APIs not reachable |
| Review by | 2026-11-18, or earlier on any trigger below |
| Issue | [#258] |

#### Why a pin does not fix it

`cryptography` appears in no `requirements*.txt` — it is pulled in transitively.
Three packages declare it:

| Declaring package | Constraint | Where |
| --- | --- | --- |
| `presidio-anonymizer==2.2.364` | `cryptography>=48.0.1,<49.0.0` | `ingestion/requirements.txt` |
| `pyjwt[crypto]==2.13.0` | `cryptography>=3.4.0` | `ingestion/requirements.txt` |
| `msal==1.37.0` | `cryptography>=2.5,<51` | `ingestion/adapters/office365/requirements.txt` |

The fixes ship in 49.0.0 (3553, 3554) and 50.0.0 (3552). `presidio-anonymizer`
caps below 49, so pinning `cryptography` higher produces a resolver conflict
rather than a fix. `2.2.364` was the latest release on PyPI when this was
assessed and still carries the cap, so there is no upgrade path to wait for
today.

The cap is also what scopes the exposure: `pb-ingestion` is the only image that
installs `presidio-anonymizer`, and therefore the only one that resolves
`cryptography` to 48.0.1. Where nothing caps it, the resolver takes a fixed
version.

#### Exposure assessment

All three advisories are confined to two API surfaces. Neither is used here:

| Advisory | Vulnerable API | Reachable? |
| --- | --- | --- |
| PYSEC-2026-3552 — PKCS#7 `EnvelopedData` decryption leaks a Bleichenbacher oracle through distinguishable errors and timing | `pkcs7_decrypt_der`, `pkcs7_decrypt_pem`, `pkcs7_decrypt_smime` | **No** — no PKCS#7 decryption anywhere in the codebase. The advisory's error-path case additionally requires OpenSSL 3.0/3.1, LibreSSL or BoringSSL; the PyPI wheels link OpenSSL 3.2+, where implicit rejection removes it. |
| PYSEC-2026-3553 — duplicate self-signed intermediates cause exponential path-building (DoS) | `cryptography.x509.verification` | **No** — no X.509 chain verification. |
| PYSEC-2026-3554 — a wildcard DNS SAN escapes an intermediate CA's `permittedSubtrees` | `cryptography.x509.verification` | **No** — same as above. |

What the three declaring packages actually use `cryptography` for, none of which
touches those surfaces:

- **`pyjwt[crypto]`** — RS256 signing of the GitHub App assertion in
  [`ingestion/adapters/providers/github.py:203`](../ingestion/adapters/providers/github.py).
  RSA signature primitives, and only on the opt-in `github_app` auth mode.
- **`presidio-anonymizer`** — declares `cryptography` for its AES-based
  `encrypt`/`decrypt` operators. `ingestion/pii_scanner.py` instantiates
  `AnonymizerEngine()` but pseudonymises by direct replacement and never
  configures the `encrypt` operator, so the AES path is not entered either.
- **`msal`** — Entra ID authentication for the Office 365 adapter. The adapter
  uses the `client_credentials` grant with a client secret
  ([`graph_client.py:125`](../ingestion/adapters/office365/graph_client.py)),
  not certificate credentials, so no certificate is parsed or validated.

Certificate validation for the services' own outbound TLS goes through Python's
`ssl` module against OpenSSL and `certifi`, not through `cryptography`'s
verifier, so 3553 and 3554 are out of the path there as well. Inbound TLS is
terminated by Caddy, which is Go and does not use this library at all.

`.msg` and `.eml` attachments are the closest adjacent surface — they are parsed
for their message structure by `markitdown`, with no key material available and
no S/MIME decryption. That is why a change there is a review trigger below.

#### Decision

Accepted. The three advisories are suppressed in the audit step via
`--ignore-vuln` so that anything the audit reports is new, and this entry
records why.

Suppression is not a fix: if any of the assumptions above changes, the risk is
real again. Reopen this entry when any of the following happens:

- **`presidio-anonymizer` ships a release that widens the `cryptography` cap** —
  the actual fix. Bump it, let `cryptography` resolve to ≥50.0.0, remove all
  three suppressions. Dependabot watches `/ingestion` weekly and will propose it.
- **A new `cryptography` advisory appears** — it is not suppressed and surfaces
  in the job summary on its own. Assess it fresh; do not assume this entry
  covers it.
- **The codebase starts verifying X.509 chains** — most plausibly by switching
  the Office 365 adapter to certificate credentials, or by adding certificate
  validation of any kind that goes through `cryptography.x509.verification`.
  3553 and 3554 become reachable that day.
- **The codebase starts decrypting PKCS#7 / S/MIME** — e.g. extending the `.msg`
  or `.eml` extraction path to handle encrypted mail. 3552 becomes reachable.
- **2026-11-18 passes** — re-check upstream regardless. An accepted risk with no
  expiry is an unmonitored one.

[#257]: https://github.com/nuetzliches/powerbrain/issues/257
[#258]: https://github.com/nuetzliches/powerbrain/issues/258
