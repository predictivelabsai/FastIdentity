from collect import process
from db import fetchall, fetchone
from domain.identity import canonical_id
from domain.matching import quality_metrics, review, run_matching
from tests.conftest import ANALYTICS_ONLY, DEVICE, ctx

IP = "86.10.20.30"


def _known(web, aid, email, cmp=None, ip=IP, device=None):
    c = {**ctx(cmp or ctx()["consent"]), **(device or {})}
    return process(web, {"type": "identify", "anonymousId": aid, "context": c, "traits": {"email": email}}, ip)["profile_id"]


def _anon(web, aid, cmp=None, ip=IP, device=None):
    c = {**ctx(cmp or ctx()["consent"]), **(device or {})}
    return process(web, {"type": "track", "event": "product_viewed", "anonymousId": aid, "context": c,
                         "properties": {"treatment": "MRI scan"}}, ip)["profile_id"]


def test_same_device_same_network_auto_merges_anon_into_known(web):
    k = _known(web, "a1", "ada@example.com")
    a = _anon(web, "a2")
    stats = run_matching("demo", actor="test")
    assert stats["auto_merged"] == 1
    assert canonical_id(a) == k
    assert fetchone("select method from merge_log")["method"] == "probabilistic_auto"


def test_same_device_other_network_goes_to_review(web):
    k = _known(web, "a1", "ada@example.com")
    a = _anon(web, "a2", ip="92.1.2.3")
    stats = run_matching("demo")
    assert stats["auto_merged"] == 0 and stats["candidates"] == 1
    c = fetchone("select * from match_candidate")
    assert 0.7 <= c["score"] < 0.97
    assert review("demo", c["id"], True, actor="reviewer")
    assert canonical_id(a) == k
    assert quality_metrics("demo")["review_precision"] == 1.0


def test_no_identity_resolution_consent_no_candidate(web):
    _known(web, "a1", "ada@example.com")
    # analytics-only visitor: no signals stored at all, so nothing to block on
    _anon(web, "a2", cmp=ANALYTICS_ONLY)
    assert run_matching("demo")["candidates"] == 0


def test_known_known_never_auto_merges(web):
    a = _known(web, "a1", "mum@example.com")
    b = _known(web, "a2", "son@example.com")
    stats = run_matching("demo")
    assert stats["auto_merged"] == 0
    assert canonical_id(a) != canonical_id(b)
    assert fetchone("select status from match_candidate")["status"] == "pending"


def test_household_different_device_not_candidate(web):
    _known(web, "a1", "ada@example.com")
    _anon(web, "a2", device={"userAgent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/128.0 Mobile",
                             "screen": {"width": 412, "height": 915, "density": 2.6}})
    assert run_matching("demo")["candidates"] == 0


def test_ambiguous_anon_matching_two_people_not_auto_merged(web):
    _known(web, "a1", "ada@example.com")
    _known(web, "a2", "bob@example.com", ip="92.9.9.9")  # same device model, other network
    a = _anon(web, "a3")
    stats = run_matching("demo")
    assert stats["auto_merged"] == 0 and stats["ambiguous"] == 1
    assert canonical_id(a) == a
