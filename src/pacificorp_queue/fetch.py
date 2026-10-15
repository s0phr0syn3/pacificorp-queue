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
from typing import IO
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# SHA-256 of the OATI WebCARES Root CA certificate in certs/. oasis.oati.com
# chains to this private root, which is not in public trust stores. We trust it
# alone (not the system store) and refuse to start if the file does not match.
# The current leaf certificate expires 2026-12-13, the root expires in 2038.
PINNED_ROOT_SHA256 = (
    "7A:77:C6:C6:1E:EE:B9:AA:65:C4:EA:41:0D:65:D8:95:"
    "B2:6A:81:12:32:83:00:9D:B1:04:B4:8D:E8:0B:24:79"
)

# Custom user agent string to identify our requests. Some servers block the
# default Python user agent.
USER_AGENT = "pacificorp-queue/0.1 (+interconnection queue ingestion)"


class FetchError(RuntimeError):
    """A page could not be downloaded, or the TLS trust setup is wrong."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A downloaded page: raw bytes plus where and when they came from."""

    url: str
    body: bytes  # raw bytes; decoding is parse.py's job
    fetched_at: datetime
    last_modified: str | None = None
    etag: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def root_ca_pem() -> str:
    """The bundled OATI root certificate, as PEM text."""
    return (
        files("pacificorp_queue") / "certs" / "oati-webcares-root-ca.pem"
    ).read_text("ascii")


def fingerprint(pem: str) -> str:
    """SHA-256 of the certificate, formatted like openssl's -fingerprint -sha256.
    Hashes the DER bytes, so PEM line wrapping cannot change the result."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def build_ssl_context() -> ssl.SSLContext:
    """TLS context that trusts only the bundled root, after checking its hash."""
    pem = root_ca_pem()
    actual = fingerprint(pem)
    if actual != PINNED_ROOT_SHA256:
        raise FetchError(f"bundled root CA fingerprint {actual} != pinned value")
    # cadata given -> system trust store is NOT loaded. Verification and
    # hostname checking stay at their secure defaults.
    return ssl.create_default_context(cadata=pem)


class _RefuseHttpsDowngrade(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never from https to a weaker scheme."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if (
            urlsplit(req.full_url).scheme == "https"
            and urlsplit(newurl).scheme != "https"
        ):
            raise FetchError(f"{req.full_url}: refusing redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(
    url: str,
    context: ssl.SSLContext,
    *,
    timeout: float = 30.0,  # seconds per socket operation, not the whole request
    attempts: int = 3,
    backoff: float = 2.0,
) -> FetchedPage:
    """GET `url`. Retries timeouts, connection errors, 5xx and truncated bodies.
    Never retries a certificate failure or a 4xx: those will not fix themselves.
    Refuses redirects away from https."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(
        _RefuseHttpsDowngrade, urllib.request.HTTPSHandler(context=context)
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(request, timeout=timeout) as r:
                body = r.read()  # raises IncompleteRead if the body is cut short
                return FetchedPage(
                    url=url,
                    body=body,
                    fetched_at=datetime.now(UTC),
                    last_modified=r.headers.get("Last-Modified"),
                    etag=r.headers.get("ETag"),
                )
        # HTTPError subclasses URLError, so it must be caught first.
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                # 4xx: our request is wrong; retrying cannot help.
                raise FetchError(f"{url}: HTTP {exc.code}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            # A bad certificate will not fix itself, so fail fast.
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
            # linear backoff: 2 s, then 4 s
            time.sleep(backoff * attempt)
    raise FetchError(
        f"{url}: gave up after {attempts} attempts: {last_error}"
    ) from last_error
