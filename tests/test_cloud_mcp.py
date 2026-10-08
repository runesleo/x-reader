"""OAuth owner guard tests, independent of live upstream GitHub."""

import unittest
from types import SimpleNamespace

from x_reader.cloud_mcp import owner_only, reserve_request


class OwnerAuthTest(unittest.TestCase):
    def test_owner_only(self):
        owner = SimpleNamespace(token=SimpleNamespace(claims={"login": "runesleo"}))
        self.assertTrue(owner_only(owner, "runesleo"))
        self.assertFalse(owner_only(owner, "another-user"))

    def test_other_user_denied(self):
        stranger = SimpleNamespace(token=SimpleNamespace(claims={"login": "attacker"}))
        self.assertFalse(owner_only(stranger, "runesleo"))

    def test_missing_claim_denied(self):
        ctx = SimpleNamespace(token=SimpleNamespace(claims={"sub": "foo"}))
        self.assertFalse(owner_only(ctx, "runesleo"))
        self.assertFalse(owner_only(SimpleNamespace(token=None), "runesleo"))


if __name__ == "__main__":
    unittest.main()
