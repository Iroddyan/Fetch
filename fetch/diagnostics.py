from __future__ import annotations

import re
import shlex
from pathlib import Path


_COOKIE_FILE = re.compile(r"(--cookies(?:=|\s+))(?P<path>\"[^\"]*\"|'[^']*'|[^\s]+)", re.IGNORECASE)
_BROWSER_COOKIES = re.compile(
    r"(--cookies-from-browser(?:=|\s+))(?P<spec>\"[^\"]*\"|'[^']*'|[^\s]+)", re.IGNORECASE
)
_PROXY_CREDENTIALS = re.compile(r"(?P<scheme>(?:https?|socks[45]h?)://)?[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


def redact_sensitive_text(value: str) -> str:
    def basename_cookie(match: re.Match[str]) -> str:
        raw = match.group("path")
        quote = raw[0] if raw[:1] in {"'", '"'} else ""
        path = raw[1:-1] if quote and raw.endswith(quote) else raw
        basename = Path(path).name or "<cookie-file>"
        return match.group(1) + (shlex.quote(basename) if quote else basename)

    def hide_profile(match: re.Match[str]) -> str:
        raw = match.group("spec")
        quote = raw[0] if raw[:1] in {"'", '"'} else ""
        spec = raw[1:-1] if quote and raw.endswith(quote) else raw
        browser, separator, profile = spec.partition(":")
        if separator and profile.startswith(("/", "~")):
            spec = f"{browser}:<profile>"
        return match.group(1) + (shlex.quote(spec) if quote else spec)

    value = _COOKIE_FILE.sub(basename_cookie, value)
    value = _BROWSER_COOKIES.sub(hide_profile, value)
    return _PROXY_CREDENTIALS.sub(lambda match: f"{match.group('scheme') or ''}<redacted>@", value)
