import http.server
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from pacificorp_queue import fetch
from pacificorp_queue.fetch import FetchError, build_ssl_context, fetch_page


def test_bundled_root_ca_matches_pinned_fingerprint() -> None:
    assert fetch.fingerprint(fetch.root_ca_pem()) == fetch.PINNED_ROOT_SHA256


def test_context_trusts_only_the_pinned_root_and_verifies_hosts() -> None:
    ctx = build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert len(ctx.get_ca_certs()) == 1  # not the system store


def test_wrong_fingerprint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "PINNED_ROOT_SHA256", "00:" * 31 + "00")
    with pytest.raises(FetchError, match="fingerprint"):
        build_ssl_context()


@dataclass
class _Server:
    url: str
    responses: list[tuple[int, bytes]]
    hits: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@pytest.fixture
def server() -> Iterator[_Server]:
    state = _Server(url="", responses=[])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with state.lock:
                status, body = state.responses[
                    min(state.hits, len(state.responses) - 1)
                ]
                state.hits += 1
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Last-Modified", "Mon, 14 Sep 2026 18:00:52 GMT")
            self.send_header("ETag", '"abc"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}/page.htm"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_fetch_returns_body_and_metadata(server: _Server) -> None:
    server.responses = [(200, b"hello")]
    page = fetch_page(server.url, build_ssl_context(), backoff=0)
    assert page.body == b"hello"
    assert page.last_modified == "Mon, 14 Sep 2026 18:00:52 GMT"
    assert page.etag == '"abc"'
    assert len(page.sha256) == 64


def test_server_errors_are_retried(server: _Server) -> None:
    server.responses = [(503, b""), (503, b""), (200, b"ok")]
    page = fetch_page(server.url, build_ssl_context(), backoff=0)
    assert page.body == b"ok"
    assert server.hits == 3


def test_persistent_server_errors_give_up_loudly(server: _Server) -> None:
    server.responses = [(500, b"")]
    with pytest.raises(FetchError, match="gave up after 3 attempts"):
        fetch_page(server.url, build_ssl_context(), backoff=0)
    assert server.hits == 3


def test_client_errors_are_not_retried(server: _Server) -> None:
    server.responses = [(404, b"")]
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_page(server.url, build_ssl_context(), backoff=0)
    assert server.hits == 1
