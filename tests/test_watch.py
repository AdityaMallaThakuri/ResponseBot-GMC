"""Automatic campaign discovery: the rule, the cache, the hourly job (app/smartlead/watch.py)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.config.settings import parse_campaign_ids
from app.smartlead.errors import SmartleadError
from app.smartlead.watch import (
    AttachJob,
    CampaignWatcher,
    is_ours,
    watched_campaigns,
    webhook_url,
)

BASE = "https://bot.example.app"

CAMPAIGNS: list[dict[str, Any]] = [
    {"id": 1, "name": "GMC - Lenders", "status": "ACTIVE"},
    {"id": 2, "name": "Lenders POS REPLY", "parent_campaign_id": 1},
    {"id": 3, "name": "gmc - banks"},  # lower case still counts
    {"id": 4, "name": "Acme - GMC style", "client_id": None},  # not GMC at the start
    {"id": 5, "name": "GMC - Acme", "client_id": 77},  # a client campaign
    {"id": 6, "name": "Acme POS REPLY", "parent_campaign_id": 5},  # its child
    {"id": 7, "name": "GMC - sub", "parent_campaign_id": 1, "client_id": 9},
    {"id": 8, "name": "Child of child", "parent_campaign_id": 2},
    {"id": 9, "name": "Unrelated"},
    {"id": 10, "name": "Orphan POS REPLY", "parent_campaign_id": 999},
]


def ids(watched: list[Any]) -> list[int]:
    return sorted(w.id for w in watched)


def settings(**over: Any) -> SimpleNamespace:
    base = {
        "GMC_CAMPAIGN_IDS": "",
        "IGNORE_CAMPAIGN_IDS": "",
        "PUBLIC_BASE_URL": BASE,
        "AUTO_ATTACH_WEBHOOKS": False,
        "WEBHOOK_SECRET": "s" * 40,
    }
    base.update(over)
    return SimpleNamespace(
        **base,
        gmc_campaign_ids=parse_campaign_ids(base["GMC_CAMPAIGN_IDS"]),
        ignore_campaign_ids=parse_campaign_ids(base["IGNORE_CAMPAIGN_IDS"]),
    )


# ------------------------------------------------------------------ the rule


def test_gmc_name_without_client_id_and_children_by_parent_id() -> None:
    assert ids(watched_campaigns(CAMPAIGNS)) == [1, 2, 3, 8]


def test_a_client_campaign_and_its_pos_reply_child_are_skipped() -> None:
    watched = ids(watched_campaigns(CAMPAIGNS))
    assert 5 not in watched and 6 not in watched  # client campaign, and its child
    assert 7 not in watched  # a child that has a client_id itself


def test_a_child_is_found_by_parent_id_not_by_name() -> None:
    by_id = {w.id: w for w in watched_campaigns(CAMPAIGNS)}
    assert by_id[2].parent_name == "GMC - Lenders" and 10 not in by_id  # POS REPLY, wrong parent
    assert by_id[8].parent_name == "Lenders POS REPLY"


def test_ignored_ids_are_removed() -> None:
    assert ids(watched_campaigns(CAMPAIGNS, ignore=frozenset({3, 2}))) == [1, 8]


def test_the_override_watches_only_those_ids() -> None:
    out = watched_campaigns(CAMPAIGNS, override=frozenset({9, 1}))
    assert ids(out) == [1, 9]  # the rule is not applied: 9 is not a GMC campaign
    assert ids(watched_campaigns(CAMPAIGNS, frozenset({9, 1}), frozenset({9}))) == [1]


# ------------------------------------------------------------------ the cache and the webhook check


class FakeSmartlead:
    def __init__(self, campaigns: list[dict[str, Any]] | None = None) -> None:
        self.campaigns = list(CAMPAIGNS if campaigns is None else campaigns)
        self.hooks: dict[int, list[dict[str, Any]]] = {}
        self.list_calls = 0
        self.created: list[tuple[int, str]] = []
        self.fail_list = False
        self.fail_create: set[int] = set()

    def list_campaigns(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        if self.fail_list:
            raise SmartleadError("down")
        return list(self.campaigns)

    def list_campaign_webhooks(self, campaign_id: int) -> list[dict[str, Any]]:
        return self.hooks.get(campaign_id, [])

    def create_campaign_webhook(self, campaign_id: int, name: str, url: str) -> None:
        if campaign_id in self.fail_create:
            raise SmartleadError("nope")
        self.created.append((campaign_id, url))


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def make_watcher(sl: FakeSmartlead, s: SimpleNamespace, clock: Clock) -> CampaignWatcher:
    return CampaignWatcher(sl, lambda: s, clock=clock)


def test_the_campaign_list_is_cached_for_ten_minutes() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    assert w.is_watched(1) and w.is_watched(2) and not w.is_watched(9)
    assert sl.list_calls == 1
    clock.now = 599
    w.is_watched(1)
    assert sl.list_calls == 1
    clock.now = 601
    w.is_watched(1)
    assert sl.list_calls == 2


def test_an_unknown_campaign_id_refreshes_the_list_once() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    assert not w.is_watched(500)  # still unknown after the refresh: ignored
    assert sl.list_calls == 1  # a fresh fetch already happened in this call, no second one
    sl.campaigns.append({"id": 600, "name": "GMC - New"})
    assert w.is_watched(600)  # unknown to the cache: refreshed once, then found
    assert sl.list_calls == 2


def test_a_known_but_unwatched_campaign_does_not_refresh() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    w.is_watched(1)
    assert not w.is_watched(9) and not w.is_watched(5)
    assert sl.list_calls == 1


def test_the_override_needs_no_smartlead_call() -> None:
    sl = FakeSmartlead()
    w = make_watcher(sl, settings(GMC_CAMPAIGN_IDS="9", IGNORE_CAMPAIGN_IDS="4"), Clock())
    assert w.is_watched(9) and not w.is_watched(1)
    assert sl.list_calls == 0


def test_a_failed_list_raises_so_the_webhook_can_answer_503() -> None:
    sl = FakeSmartlead()
    sl.fail_list = True
    with pytest.raises(SmartleadError):
        make_watcher(sl, settings(), Clock()).is_watched(1)


# ------------------------------------------------------------------ the job


def make_job(sl: FakeSmartlead, s: SimpleNamespace) -> tuple[AttachJob, list[str]]:
    notes: list[str] = []
    return AttachJob(sl, lambda: s, notes.append), notes


def test_with_auto_attach_off_it_reports_and_attaches_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    sl = FakeSmartlead()
    job, notes = make_job(sl, settings())
    result = job.run_once()
    assert sl.created == [] and len(result.would_attach) == 4
    assert "would attach to GMC - Lenders" in caplog.text
    assert notes and all(n.startswith("Would start watching") for n in notes)
    first = len(notes)
    job.run_once()
    assert len(notes) == first  # the same campaign is not announced again


def test_with_auto_attach_on_it_attaches_where_ours_is_missing_and_notes_each() -> None:
    sl = FakeSmartlead()
    sl.hooks[1] = [{"id": 5, "webhook_url": f"{BASE}/webhooks/smartlead?secret=old"}]  # ours
    sl.hooks[3] = [{"id": 6, "webhook_url": "https://crm.example/hook"}]  # someone else's
    job, notes = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True))
    result = job.run_once()
    assert sorted(c for c, _ in sl.created) == [2, 3, 8]  # 1 already has ours
    assert all(u == webhook_url(BASE, "s" * 40) for _, u in sl.created)
    assert sorted(notes) == [
        "Now watching: Child of child",
        "Now watching: Lenders POS REPLY",
        "Now watching: gmc - banks",
    ]
    assert result.attached and result.failed == 0
    assert sl.hooks[3] == [{"id": 6, "webhook_url": "https://crm.example/hook"}]  # untouched


def test_nothing_new_means_no_slack_note() -> None:
    sl = FakeSmartlead()
    for cid in (1, 2, 3, 8):
        sl.hooks[cid] = [{"id": cid, "webhook_url": f"{BASE}/webhooks/smartlead?secret=x"}]
    job, notes = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True))
    job.run_once()
    assert sl.created == [] and notes == []


def test_a_failed_attach_is_retried_next_time_and_has_no_note() -> None:
    sl = FakeSmartlead()
    sl.fail_create = {1}
    job, notes = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True, GMC_CAMPAIGN_IDS="1"))
    assert job.run_once().failed == 1 and notes == []
    sl.fail_create = set()
    job.run_once()
    assert notes == ["Now watching: GMC - Lenders"]


def test_the_job_attaches_only_to_the_override_ids_and_skips_ignored() -> None:
    sl = FakeSmartlead()
    job, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True, GMC_CAMPAIGN_IDS="1,3,9",
                                   IGNORE_CAMPAIGN_IDS="3"))
    job.run_once()
    assert sorted(c for c, _ in sl.created) == [1, 9]


def test_the_job_skips_client_campaigns_and_their_children() -> None:
    sl = FakeSmartlead()
    job, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True))
    job.run_once()
    assert not {5, 6, 7, 9, 10} & {c for c, _ in sl.created}


def test_without_a_public_base_url_the_job_does_nothing() -> None:
    sl = FakeSmartlead()
    job, notes = make_job(sl, settings(PUBLIC_BASE_URL="", AUTO_ATTACH_WEBHOOKS=True))
    assert job.run_once().skipped and sl.list_calls == 0 and sl.created == [] and notes == []


def test_a_slack_failure_does_not_stop_the_job() -> None:
    sl = FakeSmartlead()

    def boom(text: str) -> None:
        raise RuntimeError("slack down")

    job = AttachJob(sl, lambda: settings(AUTO_ATTACH_WEBHOOKS=True), boom)
    assert len(job.run_once().attached) == 4


def test_is_ours_ignores_the_secret_and_other_hosts() -> None:
    assert is_ours({"webhook_url": f"{BASE}/webhooks/smartlead?secret=a"}, BASE)
    assert not is_ours({"webhook_url": "https://x.example/webhooks/smartlead"}, BASE)
    assert not is_ours({}, BASE)
