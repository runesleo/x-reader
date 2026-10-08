"""Safe config and allowlist checks; no Railway/GitHub credentials required."""

import base64
import unittest
from types import SimpleNamespace

from x_reader.cloud_config import load_cloud_config


def example_env():
    return {
        "PUBLIC_URL": "https://x-reader-cloud.up.railway.app",
        "GITHUB_OAUTH_CLIENT_ID": "fake-test-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "fake-test-client-secret",
        "X_READER_ALLOWED_GITHUB_LOGIN": "runesleo",
        "X_READER_JWT_SIGNING_KEY": "test-value-only-" + "x" * 48,
        "X_READER_STORAGE_FERNET_KEY": base64.urlsafe_b64encode(b"\x02" * 32).decode(),
        "PORT": "8000",
    }


class CloudConfigTest(unittest.TestCase):
    def test_valid_https_config(self):
        c = load_cloud_config(example_env())
        self.assertEqual(c.allowed_login, "runesleo")
        self.assertEqual(c.port, 8000)

    def test_missing_credentials_fail_closed(self):
        env = example_env()
        env.pop("GITHUB_OAUTH_CLIENT_SECRET")
        with self.assertRaisesRegex(ValueError, "GITHUB_OAUTH_CLIENT_SECRET"):
            load_cloud_config(env)

    def test_plain_http_rejected(self):
        env = example_env()
        env["PUBLIC_URL"] = "http://my-cloud.example"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_cloud_config(env)

    def test_weak_signing_key_rejected(self):
        env = example_env()
        env["X_READER_JWT_SIGNING_KEY"] = "short"
        with self.assertRaises(ValueError):
            load_cloud_config(env)

    def test_invalid_encryption_key_rejected(self):
        env = example_env()
        env["X_READER_STORAGE_FERNET_KEY"] = "test-not-a-real-32-byte-key"
        with self.assertRaises(ValueError):
            load_cloud_config(env)

    def test_private_allowlist_not_optional(self):
        env = example_env()
        env.pop("X_READER_ALLOWED_GITHUB_LOGIN")
        with self.assertRaises(ValueError):
            load_cloud_config(env)

    def test_port_bounds(self):
        env = example_env()
        env["PORT"] = "0"
        with self.assertRaises(ValueError):
            load_cloud_config(env)


if __name__ == "__main__":
    unittest.main()
