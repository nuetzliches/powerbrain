"""
Tests for Audit-Log PII Protection
===================================
Structural + unit tests for the /scan endpoint, log_access PII scanning,
audit RLS migration, and retention anonymization.

External deps (httpx, asyncpg, presidio) are only in Docker —
tests use structural analysis or exec()-based extraction.
"""

import unittest
import os
import re


class TestScanEndpoint(unittest.TestCase):
    """Structural tests: ingestion_api.py must have a /scan endpoint."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "ingestion", "ingestion_api.py")
        with open(path) as f:
            cls.source = f.read()

    def test_scan_endpoint_exists(self):
        """POST /scan endpoint must be defined."""
        self.assertIn('@app.post("/scan")', self.source)

    def test_scan_endpoint_calls_scanner(self):
        """The /scan handler must use get_scanner() or PIIScanner."""
        self.assertIn("scan_text", self.source)
        self.assertIn("mask_text", self.source)

    def test_scan_request_model_has_text_field(self):
        """A ScanRequest Pydantic model must exist with a text field."""
        self.assertIn("class ScanRequest", self.source)
        self.assertIn("text:", self.source)

    def test_scan_response_has_required_fields(self):
        """Response must include contains_pii, masked_text, entity_types."""
        self.assertIn("contains_pii", self.source)
        self.assertIn("masked_text", self.source)
        self.assertIn("entity_types", self.source)
