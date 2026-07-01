"""Tests for RPO/RTO tier policy resolution (src/core/policy.py)."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core import policy  # noqa: E402

FAKE_CONFIG = {
    "policies": {
        "default_tier": "standard",
        "tiers": {
            "critical": {"rpo_minutes": 60, "rto_minutes": 15},
            "standard": {"rpo_minutes": 1440, "rto_minutes": 240},
        },
        "namespace_tiers": {"payments": "critical", "auth": "critical"},
    }
}


class TestResolveTier:
    def test_resolves_configured_namespace_to_its_tier(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            assert policy.resolve_tier(["payments"]) == "critical"

    def test_unmapped_namespace_falls_back_to_default_tier(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            assert policy.resolve_tier(["some-other-namespace"]) == "standard"

    def test_picks_strictest_tier_across_multiple_namespaces(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            tier = policy.resolve_tier(["payments", "some-other-namespace"])
            assert tier == "critical"

    def test_empty_namespaces_returns_default_tier(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            assert policy.resolve_tier([]) == "standard"

    def test_missing_policies_block_returns_hardcoded_default(self) -> None:
        with patch("core.policy.load_config", return_value={}):
            assert policy.resolve_tier(["payments"]) == policy.DEFAULT_TIER


class TestGetTierPolicy:
    def test_returns_configured_policy(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            assert policy.get_tier_policy("critical") == {
                "rpo_minutes": 60,
                "rto_minutes": 15,
            }

    def test_unknown_tier_returns_empty_dict(self) -> None:
        with patch("core.policy.load_config", return_value=FAKE_CONFIG):
            assert policy.get_tier_policy("nonexistent") == {}
