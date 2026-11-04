"""Look-alike propensity model (domain.lookalike).

The seed is a set of "bookers" interested in hip replacement. Candidates split into
ones that resemble the seed (same interest + intent events) and ones that do not.
A good model ranks the resemblers above the rest.
"""
import pytest

from db import execute, insert_row
from domain import lookalike
from domain.consent import record_consent
from util import dumps, new_id, now_iso

TENANT = "demo"


def _profile(pid, status, treatment, events, *, consent=True):
    execute("delete from profile where id=?", [pid])
    insert_row("profile", id=pid, tenant_id=TENANT, status=status,
               interests_json=dumps({"treatment": {treatment: len(events) or 1}}),
               first_seen="2026-10-01T00:00:00+00:00", last_seen="2026-11-01T00:00:00+00:00")
    for ev in events:
        insert_row("event", id=new_id("e_"), tenant_id=TENANT, event=ev, ts=now_iso(),
                   profile_id=pid, origin_profile_id=pid, properties_json="{}")
    if consent:
        record_consent(TENANT, pid, "marketing_email", True)
        record_consent(TENANT, pid, "identity_resolution", True)


def _population():
    """8 seed bookers, 4 look-alikes (hip + intent), 6 unrelated profiles."""
    seed = []
    for i in range(8):
        pid = f"seed_{i}"
        _profile(pid, "known", "hip replacement", ["product_viewed", "booking_started"])
        seed.append(pid)
    similar = []
    for i in range(4):
        pid = f"sim_{i}"
        _profile(pid, "anonymous", "hip replacement", ["product_viewed"])
        similar.append(pid)
    far = []
    for i in range(6):
        pid = f"far_{i}"
        _profile(pid, "anonymous", "dental checkup", ["page_viewed"])
        far.append(pid)
    return seed, similar, far


def test_sklearn_ranks_lookalikes_above_unrelated():
    if not lookalike.sklearn_available():
        pytest.skip("scikit-learn not installed")
    seed, similar, far = _population()
    res = lookalike.score_lookalike(TENANT, seed, engine="sklearn", min_seed=5)

    assert res["ok"] is True
    assert res["engine"] == "sklearn"
    assert res["seed_size"] == 8
    assert res["scored"] == len(similar) + len(far)
    assert res["train_auc"] is not None and res["train_auc"] >= 0.8

    scores = {row["profile_id"]: row["score"] for row in res["audience"]}
    # every look-alike should outscore every unrelated profile
    assert min(scores[p] for p in similar) > max(scores[p] for p in far)
    # the model should lean on the hip-replacement interest
    assert any(f["feature"] == "int=treatment:hip replacement" and f["weight"] > 0
               for f in res["top_features"])
    # consent flags are reported, not used as features
    assert all("marketing_email_consent" in row for row in res["audience"])


def test_builtin_fallback_also_ranks_sensibly():
    seed, similar, far = _population()
    res = lookalike.score_lookalike(TENANT, seed, engine="builtin", min_seed=5)
    assert res["ok"] is True
    assert res["engine"] == "builtin"
    scores = {row["profile_id"]: row["score"] for row in res["audience"]}
    assert min(scores[p] for p in similar) > max(scores[p] for p in far)


def test_seed_members_are_excluded_from_the_audience():
    seed, _similar, _far = _population()
    res = lookalike.score_lookalike(TENANT, seed, engine="builtin", min_seed=5)
    returned = {row["profile_id"] for row in res["audience"]}
    assert returned.isdisjoint(set(seed))


def test_small_seed_is_refused():
    seed, _similar, _far = _population()
    res = lookalike.score_lookalike(TENANT, seed[:3], min_seed=25)
    assert res["ok"] is False
    assert "too small" in res.get("reason", "")
    assert res["audience"] == []


def test_min_score_and_limit_are_applied():
    seed, similar, far = _population()
    res = lookalike.score_lookalike(TENANT, seed, engine="builtin", min_seed=5, limit=3)
    assert len(res["audience"]) <= 3
    res2 = lookalike.score_lookalike(TENANT, seed, engine="builtin", min_seed=5, min_score=0.99)
    assert all(row["score"] >= 0.99 for row in res2["audience"])


def test_lookalike_for_segment_uses_segment_members_as_seed():
    _population()
    seg_id = new_id("seg_")
    insert_row("segment", id=seg_id, tenant_id=TENANT, name="Hip interest",
               definition_json=dumps({"all": [{"type": "interest", "key": "treatment", "value": "hip"}]}))
    res = lookalike.lookalike_for_segment(TENANT, seg_id, engine="builtin", min_seed=5)
    assert res["ok"] is True
    # the seed is every hip profile (seed bookers + look-alikes = 12)
    assert res["seed_size"] == 12
