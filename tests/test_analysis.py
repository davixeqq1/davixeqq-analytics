import pytest

from yt_report import analysis
from yt_report.analytics_client import derive_formats, derived_avg_view_duration
from yt_report.models import LONG_FORM, SHORTS, ChannelTotals, SOURCE_DERIVED
from conftest import make_video


def test_pct_change_edge_cases():
    assert analysis.pct_change(150, 100) == pytest.approx(50.0)
    assert analysis.pct_change(50, 100) == pytest.approx(-50.0)
    assert analysis.pct_change(10, 0) is None          # zero division
    assert analysis.pct_change(None, 10) is None        # missing current
    assert analysis.pct_change(10, None) is None        # missing previous
    assert analysis.pct_change(-5, -10) == pytest.approx(50.0)  # negative net subs improve


def test_compare_totals_and_inverse_metric():
    comps = {c.key: c for c in analysis.compare_totals(ChannelTotals(110, 600, 60, 40, 10, 6), ChannelTotals(100, 540, 50, 42, 8, 3))}
    assert comps["watch_hours"].current == pytest.approx(10.0)
    assert comps["net_subs"].current == 4 and comps["net_subs"].previous == 5
    assert comps["net_subs"].is_improvement is False
    assert comps["subs_lost"].is_improvement is False   # more lost = worse
    assert comps["views"].is_improvement is True
    assert comps["avg_view_pct"].is_improvement is False


def test_net_subs_missing():
    assert ChannelTotals(subs_gained=None, subs_lost=3).net_subs is None


def test_top_videos(videos):
    top = analysis.top_videos(videos, 3)
    assert [v.video_id for v in top] == ["S1", "L1", "S2"]


def test_weakest_is_relative_to_same_format(videos):
    weak, note = analysis.find_weakest(videos, min_views=30, min_peers=3)
    assert note is None
    # L3: 25 / median(48,42,25,45)=43.5 -> 0.57 ; S3: 60 / median(92,85,60)=85 -> 0.71
    assert weak.video.video_id == "L3"
    assert weak.format_median_pct == pytest.approx(43.5)
    assert weak.peers == 4


def test_weakest_excludes_low_view_videos(videos):
    weak, _ = analysis.find_weakest(videos, min_views=30)
    assert weak.video.video_id != "TINY"


def test_weakest_insufficient_data():
    weak, note = analysis.find_weakest([make_video("A", 100, 40), make_video("B", 100, 30)], min_peers=3)
    assert weak is None and "Not enough data" in note


def test_weakest_ignores_unknown_format():
    vids = [make_video(f"V{i}", 100, 40 + i, ct=None) for i in range(5)]
    weak, note = analysis.find_weakest(vids)
    assert weak is None


def test_derive_formats_sums_and_engaged_weighted_avd():
    vids = [make_video("A", 100, 50.0, minutes=200, avd=240, engaged=40),
            make_video("B", 300, 30.0, minutes=300, avd=120, engaged=120),
            make_video("S", 50, 90.0, SHORTS, minutes=20, avd=30, engaged=20)]
    f = derive_formats(vids, complete=True)
    lf = f[LONG_FORM]
    assert lf.source == SOURCE_DERIVED
    assert lf.views == 400 and lf.minutes_watched == 500          # additive metrics: plain sums
    assert lf.avg_view_duration_s == pytest.approx((240 * 40 + 120 * 120) / 160)  # 150 s
    assert lf.avg_view_duration_s != pytest.approx(500 * 60 / 400)  # not watch time / views (75 s)
    assert lf.avg_view_pct is None                                   # unavailable in derived mode
    assert f[SHORTS].views == 50 and f[SHORTS].avg_view_duration_s == pytest.approx(30)


def test_derived_avd_reproduces_api_channel_avd():
    # Synthetic format-level figures: AVD = engagedViews-weighted mean = 78,000 / 900 = 86.67 s.
    groups = [make_video("short", 1200, 60.0, SHORTS, minutes=300, avd=30, engaged=500),
              make_video("vod", 700, 30.0, minutes=800, avd=150, engaged=300),
              make_video("live", 250, 5.0, LIVE, minutes=200, avd=180, engaged=100)]
    assert derived_avg_view_duration(groups) == pytest.approx(86.67, abs=0.01)
    assert round(derived_avg_view_duration(groups)) == 87


def test_derived_avd_zero_engaged_views_is_unavailable():
    vids = [make_video("A", 10, 50.0, avd=30, engaged=0), make_video("B", 5, 40.0, avd=20, engaged=0)]
    assert derived_avg_view_duration(vids) is None
    assert derive_formats(vids, complete=True)[LONG_FORM].avg_view_duration_s is None


def test_derived_avd_missing_engaged_views_is_unavailable():
    vids = [make_video("A", 100, 50.0, avd=200, engaged=40), make_video("B", 50, 40.0, avd=100, engaged=None)]
    assert derived_avg_view_duration(vids) is None  # partial weighting would be a guess


def test_derived_avd_missing_avd_is_unavailable():
    vids = [make_video("A", 100, 50.0, avd=None, engaged=40)]
    assert derived_avg_view_duration(vids) is None


def test_derived_avd_ignores_zero_view_rows():
    vids = [make_video("A", 100, 50.0, avd=200, engaged=40), make_video("Z", 0, 0.0, avd=None, engaged=None)]
    assert derived_avg_view_duration(vids) == pytest.approx(200)


def test_derived_avd_no_videos_is_unavailable():
    assert derived_avg_view_duration([]) is None


def test_derive_formats_empty_format_has_no_averages():
    f = derive_formats([make_video("A", 10, 50.0)], complete=False)
    assert f[SHORTS].views == 0 and f[SHORTS].avg_view_pct is None and f[SHORTS].complete is False


def test_format_share(report):
    share = analysis.format_share(report.formats_current)
    assert share[SHORTS]["views"] + share[LONG_FORM]["views"] == pytest.approx(100)


# ---- three formats (Shorts / long-form / livestream) ------------------------
from yt_report.models import LIVE, FormatStats, SOURCE_API as _API  # noqa: E402


def test_derive_formats_keeps_livestream_separate():
    vids = [make_video("A", 100, 50.0, minutes=200), make_video("L", 40, 5.0, LIVE, minutes=80),
            make_video("S", 50, 90.0, SHORTS, minutes=20), make_video("U", 30, 40.0, ct=None, minutes=10)]
    f = derive_formats(vids, complete=True)
    assert (f[LONG_FORM].views, f[LIVE].views, f[SHORTS].views) == (100, 40, 50)
    assert f[LIVE].avg_view_pct is None  # unavailable in derived mode
    assert sum(x.views for x in f.values()) == 190  # unclassified video is not forced into any format


def test_weakest_never_mixes_formats():
    vids = [make_video(f"L{i}", 100, p, LIVE) for i, p in enumerate((5.0, 6.0, 4.0))]
    vids += [make_video(f"V{i}", 100, p) for i, p in enumerate((40.0, 42.0, 38.0))]
    weak, _ = analysis.find_weakest(vids, min_views=30, min_peers=3)
    # Livestream 4% is low in absolute terms but only compared with other livestreams (4/5 = 0.8);
    # long-form 38/40 = 0.95 -> the livestream is the relatively weakest.
    assert weak.video.video_id == "L2" and weak.format_median_pct == pytest.approx(5.0)


def test_format_consistency_note():
    totals = ChannelTotals(views=2150)
    ok = {LONG_FORM: FormatStats(LONG_FORM, 700, source=_API), LIVE: FormatStats(LIVE, 250, source=_API),
          SHORTS: FormatStats(SHORTS, 1200, source=_API)}
    assert analysis.format_consistency_note(ok, totals) is None
    bad = dict(ok); bad[LIVE] = FormatStats(LIVE, 0, source=_API)
    assert "sums to 1,900 views" in analysis.format_consistency_note(bad, totals)
    unavailable = {k: FormatStats(k) for k in ok}
    assert analysis.format_consistency_note(unavailable, totals) is None  # nothing to check, no false alarm


def test_format_share_three_formats(report):
    share = analysis.format_share(report.formats_current)
    assert sum(share[ct]["views"] for ct in (SHORTS, LONG_FORM, LIVE)) == pytest.approx(100)
