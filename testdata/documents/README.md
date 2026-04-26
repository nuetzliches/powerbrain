# Document fixtures for E2E tests

Binary fixtures consumed by
`tests/integration/e2e/test_document_attachment.py` (B-51). Two
representative document types ship pre-generated so the E2E test does
not need `reportlab` / `python-docx` at runtime.

| File                    | Format | Purpose                                               |
|-------------------------|--------|-------------------------------------------------------|
| `sample_with_pii.pdf`   | PDF    | Tiny PDF carrying German PII (PERSON + EMAIL_ADDRESS) |
| `sample_with_pii.docx`  | DOCX   | Tiny DOCX with the same PII for the alternate path    |

## Regenerating

The fixtures are produced by `generate_fixtures.py`. Run it whenever
the corpus needs to change:

```bash
pip install --user reportlab python-docx
python3 testdata/documents/generate_fixtures.py
```

The script overwrites both files deterministically; commit the result.
