"""Tests for mcp-server/login_page.py — OAuth login HTML rendering."""

from login_page import render_login_page


class TestRenderLoginPage:
    def test_renders_html(self):
        html = render_login_page("/callback", "sess_123")
        assert "<!DOCTYPE html>" in html
        assert '<form method="POST"' in html
        assert 'name="api_key"' in html
        assert 'name="login_session_id"' in html

    def test_xss_in_error(self):
        html = render_login_page("/cb", "s1", error='<script>alert("xss")</script>')
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_xss_in_callback_url(self):
        html = render_login_page('"><script>x</script>', "s1")
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html

    def test_xss_in_session_id(self):
        html = render_login_page("/cb", '"><script>x</script>')
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html

    def test_no_error_block(self):
        html = render_login_page("/cb", "s1")
        assert 'class="error"' not in html

    def test_error_block_present(self):
        html = render_login_page("/cb", "s1", error="Invalid key")
        assert 'class="error"' in html
        assert "Invalid key" in html
