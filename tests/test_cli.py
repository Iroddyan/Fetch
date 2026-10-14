from fetch.cli import parse_cli_arguments


def test_cli_urls_are_deduplicated_and_invalid_arguments_counted() -> None:
    assert parse_cli_arguments([
        "https://example.com/a", "not-a-url", "https://example.com/a", "http://example.net/b",
    ]) == ("urls", ["https://example.com/a", "http://example.net/b"], 2)


def test_cli_without_urls_presents_window() -> None:
    assert parse_cli_arguments([]) == ("present", [], 0)


def test_cli_help_and_version_modes() -> None:
    assert parse_cli_arguments(["--version"]) == ("version", [], 0)
    assert parse_cli_arguments(["--help"]) == ("help", [], 0)
