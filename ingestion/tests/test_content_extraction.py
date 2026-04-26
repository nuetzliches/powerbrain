"""Unit tests for the shared content_extraction package."""

from __future__ import annotations

from content_extraction import (
    BINARY_SKIP,
    MARKITDOWN_EXTENSIONS,
    TEXT_EXTENSIONS,
    ContentExtractor,
    can_extract,
    detect_content_type,
    mime_type_to_extension,
    should_skip_file,
)


class TestMimeMapping:
    def test_pdf(self):
        assert mime_type_to_extension("application/pdf") == ".pdf"

    def test_docx(self):
        assert mime_type_to_extension(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ) == ".docx"

    def test_case_insensitive(self):
        assert mime_type_to_extension("APPLICATION/PDF") == ".pdf"

    def test_unknown(self):
        assert mime_type_to_extension("application/x-made-up") is None

    def test_none(self):
        assert mime_type_to_extension(None) is None


class TestShouldSkipFile:
    def test_skip_image(self):
        assert should_skip_file("logo.png") is True
        assert should_skip_file("photo.JPG") is True

    def test_skip_archive(self):
        assert should_skip_file("dump.zip") is True

    def test_do_not_skip_pdf(self):
        assert should_skip_file("report.pdf") is False

    def test_do_not_skip_docx(self):
        assert should_skip_file("memo.docx") is False

    def test_do_not_skip_text(self):
        assert should_skip_file("notes.txt") is False


class TestExtractorTextPaths:
    def test_text_file(self):
        extractor = ContentExtractor()
        text, backend = extractor.extract_from_bytes_detailed(b"hello", "note.txt")
        assert text == "hello"
        assert backend == "text"

    def test_markdown_file(self):
        extractor = ContentExtractor()
        text = extractor.extract_from_bytes(b"# Title\n\nbody", "page.md")
        assert text == "# Title\n\nbody"

    def test_binary_is_skipped(self):
        extractor = ContentExtractor()
        text, backend = extractor.extract_from_bytes_detailed(b"\xff\xd8", "photo.jpg")
        assert text is None
        assert backend == "skipped"

    def test_invalid_utf8_text_fails(self):
        extractor = ContentExtractor()
        text, backend = extractor.extract_from_bytes_detailed(b"\xff\xfe\x00", "note.txt")
        assert text is None
        assert backend == "failed"

    def test_unknown_extension_falls_back_to_utf8(self):
        extractor = ContentExtractor()
        text, backend = extractor.extract_from_bytes_detailed(b"hello", "weird.xyz")
        assert text == "hello"
        assert backend == "text"


class TestContentTypeDetection:
    def test_pdf(self):
        assert detect_content_type("x.pdf") == "markdown"

    def test_docx(self):
        assert detect_content_type("x.docx") == "markdown"

    def test_unknown_default(self):
        assert detect_content_type("x.xyz") == "text"


class TestCanExtract:
    def test_office(self):
        assert can_extract("a.docx")
        assert can_extract("a.xlsx")
        assert can_extract("a.pdf")

    def test_text(self):
        assert can_extract("a.py")
        assert can_extract("a.yaml")

    def test_binary(self):
        assert not can_extract("a.png")


class TestExtensionSets:
    """Verify the frozenset membership used by should_skip_file + can_extract."""

    def test_markitdown_contains_common_office(self):
        assert ".pdf" in MARKITDOWN_EXTENSIONS
        assert ".docx" in MARKITDOWN_EXTENSIONS

    def test_binary_skip_contains_common_binary(self):
        assert ".png" in BINARY_SKIP
        assert ".zip" in BINARY_SKIP

    def test_text_contains_common_source_code(self):
        assert ".py" in TEXT_EXTENSIONS
        assert ".yaml" in TEXT_EXTENSIONS
