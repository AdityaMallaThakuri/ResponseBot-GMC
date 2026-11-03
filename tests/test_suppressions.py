import pytest

from app.db.suppressions import SuppressionScope, build_suppression


def test_campaign_scope_needs_campaign_id() -> None:
    with pytest.raises(ValueError):
        build_suppression("a@b.com", SuppressionScope.CAMPAIGN, "not interested")
    s = build_suppression(" A@B.com ", SuppressionScope.CAMPAIGN, "not interested", campaign_id=1)
    assert s.email == "a@b.com" and s.scope is SuppressionScope.CAMPAIGN


def test_client_scope_needs_client() -> None:
    with pytest.raises(ValueError):
        build_suppression("a@b.com", SuppressionScope.CLIENT, "r")


def test_global_scope_needs_human_confirmation() -> None:
    with pytest.raises(ValueError, match="confirmed_by"):
        build_suppression("a@b.com", SuppressionScope.GLOBAL, "remove me from all lists")
    s = build_suppression("a@b.com", SuppressionScope.GLOBAL, "legal", confirmed_by="ray")
    assert s.scope is SuppressionScope.GLOBAL
