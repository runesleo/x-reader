# -*- coding: utf-8 -*-
"""
URL validation — SSRF-safe resolution with TUN fake-IP compatibility.
"""

import ipaddress
import json
import queue
import socket
import threading
import time
from urllib.parse import urlencode, urlparse

from urllib3 import HTTPSConnectionPool, Timeout
import idna


class UnsafeResolutionError(ValueError):
    """A resolver returned an address that must fail closed immediately."""


_BLOCKED_HOSTNAME_PATTERNS = [
    "localhost",
    "metadata.google.internal",
    "metadata.googleusercontent.com",
    "169.254.169.254",
    "100.100.100.200",
]

# TUN fake-IP range used by Clash/sing-box style DNS interception.
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

# Python 3.11's ipaddress classification is too permissive for some special ranges.
_LEGACY_SPECIAL_V4 = ipaddress.ip_network("192.0.0.0/24")
_LEGACY_RELAY_V4 = ipaddress.ip_network("192.88.99.0/24")
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")

# No system DNS is needed for these trusted DoH transports.
_DOH_RESOLVERS = (
    ("1.1.1.1", "cloudflare-dns.com", "/dns-query"),
    ("8.8.8.8", "dns.google", "/resolve"),
)

DNS_TIMEOUT = 5.0
DNS_MAX_INFLIGHT = 4
_DNS_WORKER_SLOTS = threading.BoundedSemaphore(DNS_MAX_INFLIGHT)
DOH_MAX_BYTES = 64 * 1024
_DNS_DOT_SEPARATORS = (".", "\u3002", "\uff0e", "\uff61")


def _validate_blocked_pattern_table() -> None:
    for pattern in _BLOCKED_HOSTNAME_PATTERNS:
        if (
            not pattern.isascii()
            or pattern != pattern.lower()
            or pattern.endswith(".")
        ):
            raise RuntimeError(
                "blocked hostname patterns must be lowercase ASCII without trailing dots"
            )


_validate_blocked_pattern_table()


def _looks_like_legacy_ipv4_hostname(hostname: str) -> bool:
    """Reject inet_aton-style numeric hostnames that are not canonical IP literals."""
    parts = hostname.split(".")
    if not 1 <= len(parts) <= 4 or any(not part for part in parts):
        return False

    def is_numeric_part(part: str) -> bool:
        if part.startswith("0x"):
            digits = part[2:]
            return bool(digits) and all(ch in "0123456789abcdef" for ch in digits)
        return part.isdigit()

    return all(is_numeric_part(part) for part in parts)


def _canonical_hostname(hostname: str) -> str:
    """Return a strict ASCII/IDNA hostname or fail closed."""
    # Encode the original spelling first. UTS46 maps Unicode dot separators to
    # ASCII ".", so pre-stripping would let mixed Unicode/ASCII empty labels hide.
    try:
        ascii_hostname = idna.encode(
            hostname,
            uts46=True,
            std3_rules=True,
            transitional=False,
        ).decode("ascii")
    except idna.IDNAError as exc:
        raise ValueError("Blocked: hostname is not valid IDNA") from exc

    hostname_lower = ascii_hostname.lower()
    if hostname_lower.endswith("."):
        hostname_lower = hostname_lower[:-1]
    if not hostname_lower:
        raise ValueError("Blocked: empty hostname")
    if len(hostname_lower) > 253:
        raise ValueError("Blocked: hostname exceeds 253 characters")
    if _looks_like_legacy_ipv4_hostname(hostname_lower):
        raise ValueError("Blocked: ambiguous numeric hostname")

    for blocked in _BLOCKED_HOSTNAME_PATTERNS:
        if hostname_lower == blocked or hostname_lower.endswith(f".{blocked}"):
            raise ValueError("Blocked: hostname is reserved or blocked")

    return hostname_lower


def _is_globally_routable(ip: ipaddress._BaseAddress) -> bool:
    """Conservative, version-independent public-unicast policy."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_globally_routable(ip.ipv4_mapped)

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _LEGACY_SPECIAL_V4 or ip in _LEGACY_RELAY_V4:
            return False

    if isinstance(ip, ipaddress.IPv6Address):
        # Current IPv6 global-unicast allocation is 2000::/3. Fail closed for
        # legacy/site-local/future-special ranges and for translation/tunneling
        # ranges whose effective IPv4 destination can differ from this literal.
        if ip not in _GLOBAL_UNICAST_V6:
            return False
        if ip in _NAT64_WKP or ip in _NAT64_LOCAL or ip in _SIX_TO_FOUR:
            return False

    return ip.is_global


def _getaddrinfo_with_timeout(
    hostname: str,
    port: int | None,
    timeout: float,
):
    """
    Run getaddrinfo in a bounded daemon worker.

    getaddrinfo itself is not cancellable on all platforms. A timed-out worker
    keeps its slot until it actually exits, so at most DNS_MAX_INFLIGHT stuck
    resolver threads can exist. New calls fail closed when the pool is saturated.
    """
    wait = max(0.1, timeout)
    if not _DNS_WORKER_SLOTS.acquire(timeout=wait):
        raise TimeoutError("DNS resolver capacity exhausted")

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result = socket.getaddrinfo(
                hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            result_queue.put(("ok", result))
        except BaseException as exc:
            result_queue.put(("error", exc))
        finally:
            _DNS_WORKER_SLOTS.release()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    try:
        status, payload = result_queue.get(timeout=wait)
    except queue.Empty:
        raise TimeoutError(f"DNS resolution timed out for '{hostname}'")

    if status == "error":
        if isinstance(payload, socket.gaierror):
            raise ValueError(f"Blocked: cannot resolve hostname '{hostname}'")
        raise payload

    return payload


def _parse_doh_addresses(payload: dict) -> list[str]:
    addresses = []
    for answer in payload.get("Answer") or []:
        if answer.get("type") not in (1, 28):
            continue
        value = answer.get("data")
        try:
            ip = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        if not _is_globally_routable(ip):
            raise UnsafeResolutionError(
                f"Blocked: public DNS returned non-global address {ip}"
            )
        normalized = str(ip)
        if normalized not in addresses:
            addresses.append(normalized)
    return addresses


def _query_doh_pinned(
    resolver_ip: str,
    resolver_host: str,
    path: str,
    hostname: str,
    query_type: str,
    timeout: float,
) -> list[str]:
    """DoH over a hardcoded public resolver IP with hostname SNI verification."""
    timeout_value = max(0.1, min(timeout, 3.0))
    pool = HTTPSConnectionPool(
        resolver_ip,
        port=443,
        timeout=Timeout(connect=timeout_value, read=timeout_value),
        cert_reqs="CERT_REQUIRED",
        assert_hostname=resolver_host,
        server_hostname=resolver_host,
        maxsize=1,
        block=True,
    )
    response = None
    query = urlencode({"name": hostname, "type": query_type})
    try:
        response = pool.request(
            "GET",
            f"{path}?{query}",
            headers={
                "Host": resolver_host,
                "Accept": "application/dns-json",
                "User-Agent": "x-reader/0.2",
            },
            redirect=False,
            preload_content=False,
            retries=False,
        )
        if response.status != 200:
            raise ValueError(f"DoH HTTP status {response.status}")

        raw_type = (response.headers.get("Content-Type") or "").lower()
        if "json" not in raw_type:
            raise ValueError(f"DoH unexpected Content-Type: {raw_type or '<missing>'}")

        body = response.read(DOH_MAX_BYTES + 1)
        if len(body) > DOH_MAX_BYTES:
            raise ValueError("DoH response exceeds size limit")

        payload = json.loads(body.decode("utf-8", errors="strict"))
        return _parse_doh_addresses(payload)
    finally:
        if response is not None:
            response.close()
        pool.close()


def _resolve_via_doh(hostname: str, timeout: float) -> list[str]:
    """
    Resolve a fake-IP domain through trusted pinned DoH.

    UnsafeResolutionError is never swallowed: one trusted resolver returning a
    non-public destination is enough to fail closed.
    """
    deadline = time.monotonic() + max(0.1, timeout)
    errors = []

    for resolver_ip, resolver_host, path in _DOH_RESOLVERS:
        for query_type in ("A", "AAAA"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"DoH resolution timed out for '{hostname}'")
            try:
                addresses = _query_doh_pinned(
                    resolver_ip,
                    resolver_host,
                    path,
                    hostname,
                    query_type,
                    remaining,
                )
            except UnsafeResolutionError:
                raise
            except Exception as exc:
                errors.append(f"{resolver_host}: {exc}")
                continue

            if addresses:
                return addresses

    detail = "; ".join(errors[-4:])
    raise ValueError(
        f"Blocked: fake-IP DNS detected but trusted DoH could not resolve "
        f"'{hostname}' to a public address"
        + (f" ({detail})" if detail else "")
    )


def resolve_safe_ips(
    hostname: str,
    port: int | None = None,
    timeout: float = DNS_TIMEOUT,
) -> list[str]:
    """
    Resolve exact public IPs for pinned connections.

    System DNS is used normally. If any system answer is in 198.18/15,
    x-reader treats the answer set as local TUN fake-DNS and obtains the real
    destination from pinned trusted DoH. Literal fake-IP addresses are never accepted.
    """
    # IP literals are not IDNA hostnames. Recognize them first so IPv6
    # literals remain supported, then apply the same public-unicast policy.
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if not _is_globally_routable(literal_ip):
            raise ValueError(
                f"Blocked: literal address {literal_ip} is not globally routable"
            )
        return [str(literal_ip)]

    is_absolute_name = any(
        hostname.endswith(separator)
        for separator in _DNS_DOT_SEPARATORS
    )
    canonical_hostname = _canonical_hostname(hostname)
    lookup_hostname = (
        f"{canonical_hostname}."
        if is_absolute_name
        else canonical_hostname
    )

    resolved = _getaddrinfo_with_timeout(
        lookup_hostname,
        port,
        timeout,
    )

    system_ips = []
    seen = set()
    for _family, _, _, _, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        normalized = str(ip)
        if normalized not in seen:
            seen.add(normalized)
            system_ips.append(ip)

    if not system_ips:
        raise ValueError(f"Blocked: hostname '{hostname}' resolved to no address")

    # Any fake-IP answer means the local resolver is being intercepted.
    # Ignore the whole mixed answer set and resolve the canonical hostname through
    # pinned trusted DoH rather than trusting a fake-A + real-AAAA combination.
    if any(
        isinstance(ip, ipaddress.IPv4Address) and ip in _FAKE_IP_NETWORK
        for ip in system_ips
    ):
        return _resolve_via_doh(canonical_hostname, timeout)

    safe_ips = []
    for ip in system_ips:
        effective_ip = (
            ip.ipv4_mapped
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None
            else ip
        )
        if not _is_globally_routable(ip):
            raise ValueError(
                f"Blocked: '{hostname}' resolves to non-global address {effective_ip}"
            )
        safe_ips.append(str(ip))

    return safe_ips


def validate_url(url: str) -> str:
    """
    Preflight an http(s) URL against the public-destination policy.

    This function does not pin a later network connection. Direct fetchers must
    use resolve_safe_ips() for the exact IP they connect to (as _request_pinned
    does) and must re-resolve/revalidate every redirect target.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked: unsupported scheme '{parsed.scheme}'")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Blocked: no hostname in URL")

    resolve_safe_ips(hostname, parsed.port)
    return url
