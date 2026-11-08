from yt_report.storage import Storage


def test_save_and_load_roundtrip(tmp_path, report):
    with Storage(tmp_path / "t.db") as db:
        rid = db.save_report(report)
        data = db.dashboard_data()
    assert len(data["reports"]) == 1
    rep = data["reports"][0]
    assert rep["id"] == rid
    assert rep["totals"]["current"]["views"] == 4125
    assert rep["totals"]["previous"]["views"] == 5000
    assert set(rep["formats"]["current"]) == {"SHORTS", "VIDEO_ON_DEMAND", "LIVE_STREAM"}
    assert sum(v["is_top"] for v in rep["videos"]) == 3
    assert sum(v["is_weakest"] for v in rep["videos"]) == 1
    assert len(rep["recommendations"]) == len(report.recommendations)
    assert len(data["daily"]) == 14


def test_same_period_is_not_duplicated_and_keeps_sent_flag(tmp_path, report):
    with Storage(tmp_path / "t.db") as db:
        rid1 = db.save_report(report)
        db.mark_sent(rid1)
        report.current.views = 9999
        rid2 = db.save_report(report)
        assert rid1 == rid2
        assert db.report_count() == 1
        assert db.sent_at(rid2) is not None
        data = db.dashboard_data()
    assert data["reports"][0]["totals"]["current"]["views"] == 9999
    assert len(data["reports"][0]["videos"]) == len(report.videos)  # children replaced, not appended
    assert len(data["daily"]) == 14


def test_different_period_creates_new_row(tmp_path, report):
    with Storage(tmp_path / "t.db") as db:
        db.save_report(report)
        report.period = report.period.shift(7)
        report.previous_period = report.previous_period.shift(7)
        db.save_report(report)
        assert db.report_count() == 2


def test_empty_db(tmp_path):
    with Storage(tmp_path / "sub" / "t.db") as db:
        assert db.dashboard_data() == {"reports": [], "daily": []}
