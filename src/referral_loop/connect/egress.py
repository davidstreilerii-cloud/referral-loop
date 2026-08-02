"""The only module in this repository that opens an outbound socket.

That is enforced by an AST test in tests/test_import_closure.py rather than by convention,
because it is the property the narrowed README claim rests on: no model calls, and egress only
to configured connectors. A second `import urllib.request` anywhere under src/ fails the suite.

`urllib` rather than httpx or requests: the package has two runtime dependencies and
test_install_closure asserts the surface stays small. Nothing here needs pooling, HTTP/2 or a
retry policy. If the read client's pagination and backoff genuinely outgrow the stdlib, adding a
dependency then is a decision made with evidence rather than in advance.

Four urllib defaults are reasonable for a general client and unsafe for this one. Each is
overridden below and each override has a test.
"""
from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..errors import ReferralLoopError
from .connectors import ConnectorProfile, ConnectorRegistry, endpoint_of

logger = logging.getLogger(__name__)

# Generous and explicit rather than absent. An Epic CapabilityStatement is legitimately large --
# megabytes -- but a response is still something a hostile or broken server chooses the size of.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# An unbounded wait is a resource the other end controls. Same argument peers.py makes with
# TLS_HANDSHAKE_SECONDS, one layer up.
DEFAULT_TIMEOUT_SECONDS = 30.0

# TLS 1.2 floor, matching peers._MINIMUM_TLS. Older versions are not a compatibility question
# for a link being configured from scratch on both ends.
_MINIMUM_TLS = ssl.TLSVersion.TLSv1_2


class EgressRefused(ReferralLoopError):
    """A request would have left for somewhere the registry does not name.

    Never retried. This is a configuration bug or an attempted redirect, and neither becomes
    acceptable on a second attempt.
    """


class ConnectorUnreachable(ReferralLoopError):
    """Network or TLS failure reaching a configured endpoint."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, including one to an allowlisted host.

    urllib follows redirects by default. A 302 from the token endpoint sends our signed client
    assertion -- or a live bearer token -- to whoever answered, and the assertion is replayable
    until its exp.

    Not re-resolved against the allowlist, deliberately: a redirect to a *listed* host is still
    a server we did not intend to talk to for this request, and a FHIR base URL that redirects
    is a misconfiguration worth surfacing rather than absorbing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EgressRefused(
            f"refused a {code} redirect to {newurl!r}. Redirects are never followed: the "
            "destination of a signed credential is a local decision, not the remote's"
        )


def check_allowed(registry: ConnectorRegistry, url: str) -> None:
    """Raise unless `url` names a destination the registry configured."""
    scheme, host, port = endpoint_of(url)
    if scheme != "https" and not (registry.allow_plaintext and host in registry.plaintext_hosts):
        raise EgressRefused(
            f"refused {url!r}: https is required. Plaintext needs allow_plaintext together "
            "with the host named in plaintext_hosts"
        )
    if (scheme, host, port) not in registry.endpoints():
        raise EgressRefused(
            f"refused {url!r}: {host}:{port} is not a configured connector endpoint"
        )


def _tls_context(profile: ConnectorProfile) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(profile.ca_file) if profile.ca_file else None)
    context.minimum_version = _MINIMUM_TLS
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def build_opener(profile: ConnectorProfile) -> urllib.request.OpenerDirector:
    """An opener with the four defaults corrected.

    Built per request rather than cached. There is no pooling to preserve, and a per-connector
    TLS context means a shared opener would need keying anyway.
    """
    return urllib.request.build_opener(
        # DO NOT DELETE THIS AS DEAD WEIGHT. It looks inert and is load-bearing, by a two-step
        # mechanism worth spelling out because the obvious reading is wrong.
        #
        # build_opener installs its own ProxyHandler -- which reads http_proxy/https_proxy from
        # the environment -- unless an instance of ProxyHandler is among the handlers passed in.
        # Passing this one suppresses that default. Then add_handler drops this one too, because
        # a ProxyHandler built from an empty mapping registers no *_open methods and add_handler
        # keeps only handlers that register at least one.
        #
        # So the opener ends up with no ProxyHandler whatsoever, which is the goal: on a hospital
        # network https_proxy is frequently set, and honouring it would route PHI and credentials
        # through a host nobody put in the registry. Remove this argument and the default comes
        # back. tests/test_egress.py asserts the chain is proxy-free with the environment set,
        # which is what fails if someone tidies this away.
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_tls_context(profile)),
        _RefuseRedirects(),
    )


def fetch(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Response:
    """The one call. Refuses before opening a socket if the destination is not configured."""
    check_allowed(registry, url)

    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    opener = build_opener(profile)
    try:
        with opener.open(request, timeout=timeout) as raw:
            return Response(status=raw.status, body=_read_capped(raw, url))
    except urllib.error.HTTPError as exc:
        # A 4xx is a response, not a transport failure, and its body carries the reason -- the
        # token endpoint returns invalid_client as a 400 with JSON. Callers need to read it.
        with exc:
            return Response(status=exc.code, body=_read_capped(exc, url))
    except EgressRefused:
        raise
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        raise ConnectorUnreachable(f"{profile.connector_id}: could not reach {url}: {exc}") from exc


def _read_capped(stream: object, url: str) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(body) > MAX_RESPONSE_BYTES:
        raise ConnectorUnreachable(
            f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes and was not read"
        )
    return body
