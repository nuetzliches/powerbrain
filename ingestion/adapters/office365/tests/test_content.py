"""Tests for the content extraction module."""

from __future__ import annotations

import pytest

from ingestion.adapters.office365.content import (
    ContentExtractor,
    can_extract,
    detect_content_type,
    should_skip_file,
)


# ── File Classification ─────────────────────────────────────


class TestShouldSkipFile:
    def test_skip_png(self):
        assert should_skip_file("logo.png") is True

    def test_skip_zip(self):
        assert should_skip_file("archive.zip") is True

    def test_skip_exe(self):
        assert should_skip_file("installer.exe") is True

    def test_allow_docx(self):
        assert should_skip_file("report.docx") is False

    def test_allow_python(self):
        assert should_skip_file("main.py") is False

    def test_allow_markdown(self):
        assert should_skip_file("README.md") is False

    def test_case_insensitive(self):
        assert should_skip_file("IMAGE.PNG") is True


class TestCanExtract:
    def test_docx(self):
        assert can_extract("report.docx") is True

    def test_xlsx(self):
        assert can_extract("data.xlsx") is True

    def test_pptx(self):
        assert can_extract("slides.pptx") is True

    def test_pdf(self):
        assert can_extract("manual.pdf") is True

    def test_python(self):
        assert can_extract("main.py") is True

    def test_markdown(self):
        assert can_extract("README.md") is True

    def test_binary_cannot(self):
        assert can_extract("image.png") is False

    def test_msg(self):
        assert can_extract("email.msg") is True


class TestDetectContentType:
    def test_docx_is_markdown(self):
        assert detect_content_type("report.docx") == "markdown"

    def test_pptx_is_markdown(self):
        assert detect_content_type("slides.pptx") == "markdown"

    def test_xlsx_is_markdown(self):
        assert detect_content_type("data.xlsx") == "markdown"

    def test_html(self):
        assert detect_content_type("page.html") == "html"

    def test_json(self):
        assert detect_content_type("config.json") == "json"

    def test_unknown_is_text(self):
        assert detect_content_type("file.xyz") == "text"


# ── Content Extraction ──────────────────────────────────────


class TestContentExtractor:
    @pytest.fixture
    def extractor(self):
        return ContentExtractor()

    def test_extract_text_file(self, extractor):
        data = b"Hello, World!\nLine 2."
        result = extractor.extract_from_bytes(data, "test.txt")
        assert result == "Hello, World!\nLine 2."

    def test_extract_python_file(self, extractor):
        data = b"def hello():\n    print('hello')\n"
        result = extractor.extract_from_bytes(data, "main.py")
        assert "def hello" in result

    def test_extract_markdown(self, extractor):
        data = b"# Title\n\nSome content."
        result = extractor.extract_from_bytes(data, "README.md")
        assert "# Title" in result

    def test_skip_binary_returns_none(self, extractor):
        result = extractor.extract_from_bytes(b"\x89PNG", "image.png")
        assert result is None

    def test_extract_utf8_error_returns_none(self, extractor):
        data = b"\xff\xfe\x00\x01"  # invalid UTF-8
        result = extractor.extract_from_bytes(data, "weird.txt")
        assert result is None

    def test_html_to_text(self, extractor):
        html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
        text = extractor.extract_html_to_text(html)
        assert "Title" in text
        assert "Content here" in text
        assert "<" not in text

    def test_html_to_text_strips_scripts(self, extractor):
        html = "<html><head><script type='text/javascript'>alert('xss')</script></head><body><p>Safe content</p></body></html>"
        text = extractor.extract_html_to_text(html)
        assert "alert" not in text
        assert "Safe content" in text

    def test_html_to_text_handles_empty(self, extractor):
        text = extractor.extract_html_to_text("")
        assert text == ""

    def test_extract_csv(self, extractor):
        data = b"name,age\nAlice,30\nBob,25\n"
        result = extractor.extract_from_bytes(data, "data.csv")
        assert "Alice" in result
        assert "Bob" in result
