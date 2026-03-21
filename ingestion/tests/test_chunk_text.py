"""Tests for ingestion text chunking."""

from ingestion_api import chunk_text


class TestChunkText:
    def test_short_text_single_chunk(self):
        text = "Short text."
        chunks = chunk_text(text, max_chars=100)
        assert chunks == [text]

    def test_exact_max_chars(self):
        text = "x" * 100
        chunks = chunk_text(text, max_chars=100)
        assert chunks == [text]

    def test_splits_long_text(self):
        text = "a" * 250
        chunks = chunk_text(text, max_chars=100, overlap=20)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_overlap_between_chunks(self):
        text = "a" * 250
        chunks = chunk_text(text, max_chars=100, overlap=20)
        assert len(chunks) >= 2

    def test_covers_entire_text(self):
        text = "Hello world, this is a longer text that needs chunking."
        chunks = chunk_text(text, max_chars=20, overlap=5)
        # First chunk starts at beginning
        assert chunks[0][:15] == text[:15]
        # Last chunk contains the end of original text
        assert text[-5:] in chunks[-1]

    def test_empty_text(self):
        chunks = chunk_text("", max_chars=100)
        assert chunks == [""]

    def test_default_parameters(self):
        text = "x" * 500
        chunks = chunk_text(text)  # max_chars=1000, overlap=200
        assert chunks == [text]
