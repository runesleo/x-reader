# -*- coding: utf-8 -*-
"""
URL validation — blocks SSRF attempts (private IPs, metadata endpoints, DNS rebinding).
"""

import ipaddress
import socket
from urllib.parse import urlparse

try:
    import idna
    IDNA_AVAILABLE = True
except ImportError:
    IDNA_AVAILABLE = False

# Private/reserved networks that should never be accessed via user-supplied URLs
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
]

# Blocked hostname patterns (homograph attacks, suspicious domains)
_BLOCKED_HOSTNAME_PATTERNS = [
    "localhost",
    "metadata.google.internal",  # GCP metadata
    "metadata.googleusercontent.com",  # GCP metadata
    "169.254.169.254",  # AWS/GCP/Azure metadata endpoints
]


def _validate_hostname(hostname: str) -> None:
    """Validate hostname for security issues like homograph attacks."""
    hostname_lower = hostname.lower()

    # Check for blocked hostnames
    for blocked in _BLOCKED_HOSTNAME_PATTERNS:
        if hostname_lower == blocked or hostname_lower.endswith(f".{blocked}"):
            raise ValueError(f"Blocked: hostname '{hostname}' is reserved or blocked")

    # If idna library available, validate IDN encoding (prevents homograph attacks)
    if IDNA_AVAILABLE:
        try:
            idna.encode(hostname).decode('ascii')
        except idna.core.InvalidCodepoint:
            raise ValueError(f"Blocked: hostname '{hostname}' contains invalid IDN characters")
        except Exception:
            pass  # If IDN encoding fails, fall through to other checks


def validate_url(url: str) -> str:
    """
    Validate a URL is safe to fetch (not targeting internal resources).

    Checks:
    1. Scheme must be http or https
    2. Hostname must exist
    3. Resolved IP must not be private/loopback/link-local

    Returns the validated URL. Raises ValueError if blocked.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked: unsupported scheme '{parsed.scheme}'")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Blocked: no hostname in URL")

    # Security: validate hostname for blocked patterns and IDN homograph attacks
    _validate_hostname(hostname)

    # Resolve hostname to IP — catches DNS rebinding (evil.com → 127.0.0.1)
    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"Blocked: cannot resolve hostname '{hostname}'")

    for family, _, _, _, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(
                    f"Blocked: '{hostname}' resolves to private address {ip}"
                )

    return url
