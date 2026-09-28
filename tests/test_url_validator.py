import queue
import socket
import unittest
from unittest.mock import patch

from x_reader.utils import url_validator


class UrlValidatorResolutionTest(unittest.TestCase):
    def test_resolve_safe_ips_returns_exact_global_addresses(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
        ]
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            return_value=resolved,
        ):
            self.assertEqual(
                url_validator.resolve_safe_ips("example.com", 443),
                ["8.8.8.8", "1.1.1.1"],
            )

    def test_rejects_ipv4_mapped_loopback(self):
        resolved = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("::ffff:127.0.0.1", 443, 0, 0),
            ),
        ]
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            return_value=resolved,
        ):
            with self.assertRaisesRegex(ValueError, "non-global address 127.0.0.1"):
                url_validator.resolve_safe_ips("example.com", 443)

    def test_rejects_cgnat_and_link_local(self):
        for address in ("100.64.0.1", "169.254.1.1", "fe80::1", "fc00::1"):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            resolved = [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
            with self.subTest(address=address), patch.object(
                url_validator,
                "_getaddrinfo_with_timeout",
                return_value=resolved,
            ):
                with self.assertRaisesRegex(ValueError, "non-global address"):
                    url_validator.resolve_safe_ips("example.com", 443)

    def test_metadata_ip_literal_is_blocked_before_dns(self):
        with patch.object(url_validator, "_getaddrinfo_with_timeout") as resolver:
            with self.assertRaisesRegex(ValueError, "not globally routable"):
                url_validator.resolve_safe_ips("100.100.100.200", 80)
        resolver.assert_not_called()

    def test_dns_resolution_timeout_fails_closed(self):
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            side_effect=TimeoutError("DNS resolution timed out"),
        ):
            with self.assertRaisesRegex(TimeoutError, "DNS resolution timed out"):
                url_validator.resolve_safe_ips("example.com", 443, timeout=0.1)


    def test_fake_ip_domain_uses_doh_real_public_address(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.141", 443)),
        ]
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            return_value=resolved,
        ), patch.object(
            url_validator,
            "_resolve_via_doh",
            return_value=["172.66.147.243", "104.20.23.154"],
        ) as doh:
            self.assertEqual(
                url_validator.resolve_safe_ips("example.com", 443),
                ["172.66.147.243", "104.20.23.154"],
            )
        doh.assert_called_once_with("example.com", url_validator.DNS_TIMEOUT)

    def test_literal_fake_ip_is_rejected_without_doh(self):
        with patch.object(url_validator, "_resolve_via_doh") as doh:
            with self.assertRaisesRegex(ValueError, "not globally routable"):
                url_validator.resolve_safe_ips("198.18.2.141", 443)
        doh.assert_not_called()

    def test_doh_non_global_answer_fails_closed(self):
        payload = {
            "Answer": [
                {"type": 1, "data": "127.0.0.1"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "public DNS returned non-global"):
            url_validator._parse_doh_addresses(payload)


    def test_rejects_multicast_legacy_nat64_and_6to4(self):
        for address in (
            "224.0.0.1",
            "239.1.1.1",
            "192.0.0.8",
            "192.88.99.1",
            "fec0::1",
            "4000::1",
            "::127.0.0.1",
            "64:ff9b::808:808",
            "64:ff9b:1::808:808",
            "2002:0808:0808::1",
        ):
            with self.subTest(address=address):
                ip = __import__("ipaddress").ip_address(address)
                self.assertFalse(url_validator._is_globally_routable(ip))

    def test_unsafe_doh_answer_is_not_swallowed(self):
        with patch.object(
            url_validator,
            "_query_doh_pinned",
            side_effect=url_validator.UnsafeResolutionError("unsafe answer"),
        ):
            with self.assertRaisesRegex(
                url_validator.UnsafeResolutionError,
                "unsafe answer",
            ):
                url_validator._resolve_via_doh("example.com", 1.0)

    def test_pinned_doh_query_uses_fixed_ip_and_resolver_sni(self):
        response_body = (
            b'{"Status":0,"Answer":[{"name":"example.com","type":1,'
            b'"TTL":60,"data":"8.8.8.8"}]}'
        )
        recorded = {}

        class Response:
            status = 200
            headers = {"Content-Type": "application/dns-json"}
            def read(self, _n):
                return response_body
            def close(self):
                recorded["response_closed"] = True

        class Pool:
            def __init__(self, *args, **kwargs):
                recorded["pool_init"] = (args, kwargs)
            def request(self, method, target, **kwargs):
                recorded["request"] = (method, target, kwargs)
                return Response()
            def close(self):
                recorded["pool_closed"] = True

        with patch.object(url_validator, "HTTPSConnectionPool", Pool):
            addresses = url_validator._query_doh_pinned(
                "1.1.1.1",
                "cloudflare-dns.com",
                "/dns-query",
                "example.com",
                "A",
                2.0,
            )

        self.assertEqual(addresses, ["8.8.8.8"])
        _, kwargs = recorded["pool_init"]
        self.assertEqual(kwargs["assert_hostname"], "cloudflare-dns.com")
        self.assertEqual(kwargs["server_hostname"], "cloudflare-dns.com")
        self.assertEqual(recorded["request"][0], "GET")
        self.assertIn("name=example.com", recorded["request"][1])
        self.assertTrue(recorded["response_closed"])
        self.assertTrue(recorded["pool_closed"])


    def test_mixed_fake_and_real_system_answers_use_doh(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.141", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2001:4860:4860::8888", 443, 0, 0),
            ),
        ]
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            return_value=resolved,
        ), patch.object(
            url_validator,
            "_resolve_via_doh",
            return_value=["8.8.8.8"],
        ) as doh:
            self.assertEqual(
                url_validator.resolve_safe_ips("example.com", 443),
                ["8.8.8.8"],
            )
        doh.assert_called_once_with("example.com", url_validator.DNS_TIMEOUT)

    def test_invalid_idna_fails_closed(self):
        with patch.object(
            url_validator.idna,
            "encode",
            side_effect=url_validator.idna.IDNAError("bad idna"),
        ):
            with self.assertRaisesRegex(ValueError, "not valid IDNA"):
                url_validator.resolve_safe_ips("bad.example", 443)

    def test_hostname_canonicalization_accepts_case_and_single_trailing_dot(self):
        self.assertEqual(
            url_validator._canonical_hostname("Example.COM."),
            "example.com",
        )

    def test_hostname_canonicalization_rejects_multiple_trailing_dots(self):
        with self.assertRaisesRegex(ValueError, "not valid IDNA"):
            url_validator._canonical_hostname("example.com..")

    def test_hostname_canonicalization_rejects_mixed_unicode_trailing_dots(self):
        for hostname in (
            "example.com\u3002.",
            "example.com\uff0e.",
            "example.com\uff61.",
        ):
            with self.subTest(hostname=repr(hostname)):
                with self.assertRaisesRegex(ValueError, "not valid IDNA"):
                    url_validator._canonical_hostname(hostname)

    def test_hostname_canonicalization_rejects_illegal_ascii(self):
        for hostname in ("bad_name.example", "bad/name.example", "bad\x00.example"):
            with self.subTest(hostname=repr(hostname)):
                with self.assertRaisesRegex(ValueError, "not valid IDNA"):
                    url_validator._canonical_hostname(hostname)

    def test_absolute_hostname_preserves_trailing_dot_for_system_dns(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]
        with patch.object(
            url_validator,
            "_getaddrinfo_with_timeout",
            return_value=resolved,
        ) as resolver:
            self.assertEqual(
                url_validator.resolve_safe_ips("Example.COM.", 443),
                ["8.8.8.8"],
            )
        resolver.assert_called_once_with("example.com.", 443, url_validator.DNS_TIMEOUT)

    def test_legacy_ipv4_hostname_forms_are_rejected_before_dns(self):
        for hostname in (
            "127.1",
            "2130706433",
            "0x7f000001",
            "0177.0.0.1",
            "１２７．０．０．１",
        ):
            with self.subTest(hostname=hostname), patch.object(
                url_validator, "_getaddrinfo_with_timeout"
            ) as resolver:
                with self.assertRaisesRegex(ValueError, "ambiguous numeric hostname"):
                    url_validator.resolve_safe_ips(hostname, 443)
                resolver.assert_not_called()

    def test_global_ipv4_literal_bypasses_idna_and_dns(self):
        with patch.object(url_validator, "_getaddrinfo_with_timeout") as resolver:
            self.assertEqual(
                url_validator.resolve_safe_ips("8.8.8.8", 443),
                ["8.8.8.8"],
            )
        resolver.assert_not_called()

    def test_global_ipv6_literal_bypasses_idna_and_dns(self):
        with patch.object(url_validator, "_getaddrinfo_with_timeout") as resolver:
            self.assertEqual(
                url_validator.resolve_safe_ips("2606:4700:4700::1111", 443),
                ["2606:4700:4700::1111"],
            )
        resolver.assert_not_called()

    def test_ipv6_loopback_literal_is_rejected_before_dns(self):
        with patch.object(url_validator, "_getaddrinfo_with_timeout") as resolver:
            with self.assertRaisesRegex(ValueError, "not globally routable"):
                url_validator.resolve_safe_ips("::1", 443)
        resolver.assert_not_called()

    def test_blocked_pattern_table_is_canonical(self):
        for pattern in url_validator._BLOCKED_HOSTNAME_PATTERNS:
            self.assertTrue(pattern.isascii())
            self.assertEqual(pattern, pattern.lower())
            self.assertFalse(pattern.endswith("."))

    def test_dns_capacity_exhaustion_fails_closed_without_new_worker(self):
        acquired = 0
        try:
            for _ in range(url_validator.DNS_MAX_INFLIGHT):
                self.assertTrue(url_validator._DNS_WORKER_SLOTS.acquire(timeout=0.1))
                acquired += 1
            with patch.object(socket, "getaddrinfo") as resolver:
                with self.assertRaisesRegex(
                    TimeoutError,
                    "DNS resolver capacity exhausted",
                ):
                    url_validator._getaddrinfo_with_timeout(
                        "example.com",
                        443,
                        0.1,
                    )
            resolver.assert_not_called()
        finally:
            for _ in range(acquired):
                url_validator._DNS_WORKER_SLOTS.release()


if __name__ == "__main__":
    unittest.main()
