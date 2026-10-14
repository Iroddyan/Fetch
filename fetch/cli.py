from __future__ import annotations

from .queue import extract_http_urls


def parse_cli_arguments(arguments: list[str]) -> tuple[str, list[str], int]:
    """Resolve CLI mode and URL arguments; arguments exclude the executable name."""
    if "--version" in arguments or "-v" in arguments:
        return "version", [], 0
    if "--help" in arguments or "-h" in arguments:
        return "help", [], 0
    urls, skipped = extract_http_urls("\n".join(argument for argument in arguments if not argument.startswith("-")))
    return ("urls" if urls else "present"), urls, skipped
