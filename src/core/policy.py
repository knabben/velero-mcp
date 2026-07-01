"""RPO/RTO tier policy resolution from kmcp.yaml's top-level `policies` block.

No LLM reasoning needed here - this is a cheap, deterministic lookup: a
backup's namespaces map to a tier, and the tier carries fixed RPO/RTO
targets that `list_backups` scores backups against.
"""

from typing import Any

from .utils import load_config

DEFAULT_TIER = "standard"


def get_policies() -> dict[str, Any]:
    """Load the `policies` block from kmcp.yaml."""
    policies = load_config("kmcp.yaml").get("policies")
    return policies if isinstance(policies, dict) else {}


def resolve_tier(namespaces: list[str]) -> str:
    """Pick the strictest (lowest RPO) tier among a backup's namespaces."""
    policies = get_policies()
    namespace_tiers: dict[str, str] = policies.get("namespace_tiers") or {}
    tiers = policies.get("tiers") or {}
    default_tier: str = policies.get("default_tier", DEFAULT_TIER)

    candidate_tiers: set[str] = {
        namespace_tiers.get(ns, default_tier) for ns in namespaces
    }
    if not candidate_tiers:
        return default_tier

    def rpo_of(tier: str) -> float:
        rpo = tiers.get(tier, {}).get("rpo_minutes")
        return rpo if isinstance(rpo, int | float) else float("inf")

    result: str = min(candidate_tiers, key=rpo_of)
    return result


def get_tier_policy(tier: str) -> dict[str, Any]:
    """RPO/RTO targets (in minutes) for a tier, or {} if the tier is unconfigured."""
    tiers = get_policies().get("tiers") or {}
    policy = tiers.get(tier, {})
    return policy if isinstance(policy, dict) else {}
