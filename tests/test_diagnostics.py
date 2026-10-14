from fetch.diagnostics import redact_sensitive_text


def test_redacts_cookie_file_and_browser_profile_paths() -> None:
    diagnostic = redact_sensitive_text(
        "yt-dlp --cookies '/home/alice/private data/cookies.txt' "
        "--cookies-from-browser 'firefox:/home/alice/.mozilla/firefox/abc.default'"
    )
    assert "--cookies cookies.txt" in diagnostic
    assert "firefox:<profile>" in diagnostic
    assert "/home/alice" not in diagnostic


def test_redacts_proxy_credentials_but_keeps_proxy_address() -> None:
    diagnostic = redact_sensitive_text("--proxy https://alice:secret@proxy.example:8443")
    assert diagnostic == "--proxy https://<redacted>@proxy.example:8443"


def test_redaction_handles_equals_and_log_lines() -> None:
    diagnostic = redact_sensitive_text(
        "--cookies=/home/alice/cookies.txt\nERROR: proxy http://user:pass@proxy.example"
    )
    assert "--cookies=cookies.txt" in diagnostic
    assert "http://<redacted>@proxy.example" in diagnostic
