import json

from yt_report.ai_recommendations import metrics_payload
from yt_report.models import ChannelTotals
from yt_report.recommendations import build_recommendations


def test_two_to_three_recommendations(report):
    recs = build_recommendations(report)
    assert 2 <= len(recs) <= 3
    assert all(r.source == "rules" for r in recs)
    assert len({r.title for r in recs}) == len(recs)


def test_recommendations_cite_real_numbers(report):
    text = " ".join(r.body for r in build_recommendations(report, limit=10))
    # views fell 17.5% (5,000 -> 4,125)
    assert "17.5%" in text and "4,125" in text


def test_negative_net_subs_is_flagged(report):
    report.current = ChannelTotals(4125, 8250.0, 120.0, 55.0, 3, 9)
    titles = [r.title for r in build_recommendations(report)]
    assert "Investigate subscriber losses" in titles


def test_low_data_channel(report):
    report.current = ChannelTotals(12, 5.0, 25.0, 30.0, 0, 0)
    recs = build_recommendations(report)
    assert recs[0].title == "Build a consistent upload rhythm"
    assert len(recs) >= 2


def test_ai_payload_is_json_serialisable(report):
    payload = metrics_payload(report)
    json.dumps(payload)
    assert payload["comparisons"][0]["metric"] == "Views"


def test_ai_payload_has_per_format_retention(report):
    payload = metrics_payload(report)
    statuses = {x["format"]: x["status"] for x in payload["retention_by_format"]}
    assert set(statuses) == {"Shorts", "Long-form", "Livestream"}
