from fetch.queue import extract_http_urls, is_playlist_url


def test_extract_http_urls_ignores_blanks_invalid_and_duplicates() -> None:
    urls, skipped = extract_http_urls(
        "https://example.com/a\n\nftp://example.com/file\nhttps://example.com/a\nnot a URL\nhttp://example.org/b"
    )
    assert urls == ["https://example.com/a", "http://example.org/b"]
    assert skipped == 3


def test_extract_http_urls_rejects_malformed_authorities() -> None:
    assert extract_http_urls("https:///missing-host\nhttps://[bad") == ([], 2)


def test_playlist_url_detection_handles_playlist_and_list_query() -> None:
    assert is_playlist_url("https://www.youtube.com/playlist?list=PL123")
    assert is_playlist_url("https://www.youtube.com/watch?v=video&list=PL123")
    assert not is_playlist_url("https://www.youtube.com/watch?v=video")
