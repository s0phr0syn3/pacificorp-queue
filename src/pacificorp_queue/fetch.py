"""Download a page over TLS, trusting only OATI's own root CA."""

import hashlib
import http.client
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files

log = logging.getLogger(__name__)

# SHA-256 of the OATI WebCARES Root CA certificate in certs/. oasis.oati.com
# chains to this private root, which is not in public trust stores. We trust it
# alone (not the system store) and refuse to start if the file does not match.
# The leaf certificate rotates (the current one expires 2026-12-13), the root
# does not (valid to 2038), so the root is what we pin.
PINNED_ROOT_SHA256 = (
    "7A:77:C6:C6:1E:EE:B9:AA:65:C4:EA:41:0D:65:D8:95:"
    "B2:6A:81:12:32:83:00:9D:B1:04:B4:8D:E8:0B:24:79"
)
USER_AGENT = "pacificorp-queue/0.1 (+interconnection queue ingestion)"


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    body: bytes
    fetched_at: datetime
    last_modified: str | None = None
    etag: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def root_ca_pem() -> str:
    return (
        files("pacificorp_queue") / "certs" / "oati-webcares-root-ca.pem"
    ).read_text("ascii")


def fingerprint(pem: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def build_ssl_context() -> ssl.SSLContext:
    pem = root_ca_pem()
    actual = fingerprint(pem)
    if actual != PINNED_ROOT_SHA256:
        raise FetchError(f"bundled root CA fingerprint {actual} != pinned value")
    # cadata given -> system trust store is NOT loaded. Verification and
    # hostname checking stay at their secure defaults.
    return ssl.create_default_context(cadata=pem)


def fetch_page(
    url: str,
    context: ssl.SSLContext,
    *,
    timeout: float = 30.0,
    attempts: int = 3,
    backoff: float = 2.0,
) -> FetchedPage:
    """GET `url`. Retries timeouts, connection errors, 5xx and truncated bodies.
    Never retries a certificate failure or a 4xx: those will not fix themselves."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, context=context, timeout=timeout) as r:
                body = r.read()  # raises IncompleteRead if the body is cut short
                return FetchedPage(
                    url=url,
                    body=body,
                    fetched_at=datetime.now(UTC),
                    last_modified=r.headers.get("Last-Modified"),
                    etag=r.headers.get("ETag"),
                )
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise FetchError(f"{url}: HTTP {exc.code}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise FetchError(
                    f"{url}: TLS verification failed: {exc.reason}"
                ) from exc
            last_error = exc
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            last_error = exc
        log.warning(
            "fetch failed url=%s attempt=%d/%d error=%s",
            url,
            attempt,
            attempts,
            last_error,
        )
        if attempt < attempts:
            time.sleep(backoff * attempt)
    raise FetchError(f"{url}: gave up after {attempts} attempts: {last_error}")
