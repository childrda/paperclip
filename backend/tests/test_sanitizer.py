from __future__ import annotations

from foia.sanitizer import html_to_text, sanitize_html


def test_empty_inputs():
    assert sanitize_html(None) == ""
    assert sanitize_html("") == ""
    assert html_to_text(None) == ""


def test_strips_script_tags():
    html = "<p>Hi</p><script>alert('x')</script>"
    out = sanitize_html(html)
    assert "script" not in out.lower()
    assert "alert" not in out


def test_strips_iframe_and_embed_and_object():
    html = (
        "<iframe src='https://evil.example'></iframe>"
        "<embed src='x'/><object data='y'></object><p>ok</p>"
    )
    out = sanitize_html(html)
    for tag in ("iframe", "embed", "object"):
        assert tag not in out.lower()
    assert "ok" in out


def test_strips_tracking_images():
    html = (
        "<p>Before</p>"
        "<img src='https://track.example/p.gif' width='1' height='1'/>"
        "<img src='https://cdn.example/logo.png'/>"
        "<p>After</p>"
    )
    out = sanitize_html(html)
    assert "<img" not in out.lower()
    assert "track.example" not in out
    assert "cdn.example" not in out
    assert "Before" in out and "After" in out


def test_strips_event_handlers_and_js_hrefs():
    html = (
        "<a href='javascript:alert(1)' onclick='bad()'>click</a>"
        "<a href='https://ok.example'>good</a>"
    )
    out = sanitize_html(html)
    assert "javascript" not in out.lower()
    assert "onclick" not in out.lower()
    assert "https://ok.example" in out


def test_keeps_basic_formatting():
    html = "<p><strong>Bold</strong> and <em>italic</em></p>"
    out = sanitize_html(html)
    assert "<strong>" in out
    assert "<em>" in out


def test_strips_style_urls():
    html = "<p style=\"background:url('https://evil.example/x.png')\">hi</p>"
    out = sanitize_html(html)
    assert "evil.example" not in out


def test_html_to_text():
    html = "<p>Hello <b>world</b></p><script>alert(1)</script>"
    assert html_to_text(html) == "Hello world"
