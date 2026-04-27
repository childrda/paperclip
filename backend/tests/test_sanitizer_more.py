"""Additional sanitizer coverage for rarer attack shapes."""

from __future__ import annotations

from foia.sanitizer import sanitize_html


def test_strips_form_inputs_and_buttons():
    html = "<form action='x'><input name='n'/><button>go</button></form><p>ok</p>"
    out = sanitize_html(html)
    for tag in ("form", "input", "button"):
        assert tag not in out.lower()
    assert "ok" in out


def test_strips_svg_and_canvas():
    html = (
        "<svg xmlns='http://www.w3.org/2000/svg'>"
        "<script>alert(1)</script><image href='x.png'/></svg>"
        "<canvas>.</canvas><p>keep</p>"
    )
    out = sanitize_html(html)
    assert "svg" not in out.lower()
    assert "canvas" not in out.lower()
    assert "alert" not in out
    assert "keep" in out


def test_strips_data_url_in_disallowed_tag():
    # The img is stripped entirely, so data: URL cannot survive.
    html = "<img src='data:image/png;base64,AAAA'/><p>t</p>"
    out = sanitize_html(html)
    assert "data:" not in out.lower()
    assert "t" in out


def test_strips_base_and_meta():
    html = (
        "<base href='https://evil.example/'>"
        "<meta http-equiv='refresh' content='0;url=https://evil.example'>"
        "<p>body</p>"
    )
    out = sanitize_html(html)
    assert "base" not in out.lower()
    assert "meta" not in out.lower()
    assert "evil.example" not in out
    assert "body" in out


def test_preserves_mailto_but_strips_unknown_protocol():
    html = (
        "<a href='mailto:a@b.com'>a</a>"
        "<a href='ftp://files.example/x'>ftp</a>"
        "<a href='data:text/html,<script>'>data</a>"
    )
    out = sanitize_html(html)
    assert "mailto:a@b.com" in out
    assert "ftp://" not in out
    assert "data:text/html" not in out


def test_keeps_tables():
    html = "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>v</td></tr></tbody></table>"
    out = sanitize_html(html)
    for tag in ("table", "thead", "tbody", "tr", "th", "td"):
        assert f"<{tag}" in out


def test_html_comments_removed():
    html = "<!-- secret --><p>body</p><!-- another -->"
    out = sanitize_html(html)
    assert "<!--" not in out
    assert "secret" not in out
    assert "body" in out
