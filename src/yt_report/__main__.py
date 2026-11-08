"""Command-line entry point.

    python -m yt_report auth            # one-time Google authorization (opens browser)
    python -m yt_report run             # build report, store in SQLite, send to Discord
    python -m yt_report run --dry-run   # build and print only (no DB write, no Discord)
    python -m yt_report run --dry-run --allow-incomplete   # preview a clearly labelled partial report
    python -m yt_report dashboard       # local web dashboard
    python -m yt_report test-discord    # send a test message to the webhook
    python -m yt_report check           # validate configuration (no secrets printed)
    python -m yt_report demo            # synthetic demo: no API, no Discord, separate DB
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .config import ConfigError, load_config
from .logging_utils import redact, setup_logging

log = logging.getLogger("yt_report")


def _print_summary(report) -> None:
    from .analysis import compare_totals
    from .formatting import fmt_change, fmt_pct, fmt_value
    from .models import FORMAT_LABELS, REPORTED_FORMATS

    if report.partial:
        t = report.data_through
        print(f"\nPARTIAL REPORT — {report.channel_title} — data {report.period.start:%b} {report.period.start.day} – "
              f"{t:%b} {t.day} only ({report.days_with_data}/{report.period.days} days of {report.period.label()}); "
              "values only, no week-over-week comparison")
        for c in compare_totals(report.current, report.previous):
            print(f"  {c.label:<20} {fmt_value(c.current, c.kind):>12}")
    else:
        print(f"\n{report.channel_title} — {report.period.label()} (vs {report.previous_period.label()})")
        for c in compare_totals(report.current, report.previous):
            ch = fmt_change(c.change_pct)
            print(f"  {c.label:<20} {fmt_value(c.current, c.kind):>12}   prev {fmt_value(c.previous, c.kind):>12}   {ch}")
    order = {ct: i for i, ct in enumerate(REPORTED_FORMATS)}
    for ct, f in sorted(report.formats_current.items(), key=lambda kv: order.get(kv[0], 99)):
        print(f"  [{f.label}] views={fmt_value(f.views, 'int')} watch={fmt_value(f.watch_hours, 'hours')} "
              f"avd={fmt_value(f.avg_view_duration_s, 'duration')} pct={fmt_value(f.avg_view_pct, 'pct')} ({f.source})")
    print("  Top videos:")
    for i, v in enumerate(report.top_videos, 1):
        label = FORMAT_LABELS.get(v.content_type or "", "format unavailable")
        print(f"    {i}. [{label}] {v.title} — {v.views:,} views — {v.url}")
    if report.weakest:
        print(f"  Weakest: [{FORMAT_LABELS.get(report.weakest.video.content_type or '', '?')}] "
              f"{report.weakest.video.title} ({fmt_pct(report.weakest.video.avg_view_pct)} vs median "
              f"{fmt_pct(report.weakest.format_median_pct)})")
    else:
        print(f"  Weakest: {report.weakest_note}")
    pc, pp = report.publications_current, report.publications_previous
    if pc is not None and pc.available:
        counts = " ".join(f"{FORMAT_LABELS[ct]}={pc.count(ct)}" + (f"(prev {pp.count(ct)})" if pp and pp.available else "")
                          for ct in REPORTED_FORMATS)
        print(f"  Published (public, PT): {counts} unclassified={pc.unclassified} "
              f"excluded: private={pc.private} unlisted={pc.unlisted} scheduled={pc.upcoming}"
              f"{'' if pc.complete and pp is not None and pp.complete else '  [INCOMPLETE]'}")
    else:
        print("  Published: unavailable")
    for which, cov in (("current", report.coverage_current), ("previous", report.coverage_previous)):
        if cov is not None:
            print(f"  Coverage {which}: {cov.views_rows:,}/{fmt_value(cov.views_reference, 'int')} "
                  f"views in {cov.rows} rows via {cov.method}{' (capped)' if cov.capped else ''}"
                  f"{'' if cov.complete else '  [INCOMPLETE]'}")
    a = report.views_by_age
    if a is not None:
        print(f"  Views by upload age: new {a.new_prev:,} -> {a.new_cur:,}, last period's uploads now {a.prev_uploads_cur:,}, "
              f"older catalog {a.older_prev:,} -> {a.older_cur:,}{'' if a.complete else '  [INCOMPLETE]'}")
    print("  Recommendations:")
    for r in report.recommendations:
        print(f"    - [{r.source}] {r.title}: {r.body}")
    for n in report.notes:
        print(f"  Note: {n}")


EXIT_LOCKED = 8
EXIT_INCOMPLETE = 9


def cmd_run(args, cfg) -> int:
    """Runs that may store or send hold an exclusive OS lock; a concurrent run exits with code 8."""
    if args.dry_run:
        return _run_report(args, cfg)
    from .locking import RunLock, lock_path_for

    lock = RunLock(lock_path_for(cfg.database_path))
    if not lock.acquire():
        log.error("Another yt-report run is in progress (lock file %s); this run did nothing.", lock.path)
        return EXIT_LOCKED
    try:
        return _run_report(args, cfg)
    finally:
        lock.release()


def _stored_periods(cfg, dry_run: bool):
    """Periods of stored reports for the overlap check; None if they cannot be read."""
    import sqlite3

    from .storage import Storage, read_periods_readonly

    try:
        if dry_run:
            return read_periods_readonly(cfg.database_path)  # never writes, never migrates
        if not cfg.database_path.exists():
            return []
        with Storage(cfg.database_path) as db:
            return db.periods()
    except sqlite3.Error as exc:
        log.warning("Stored report periods could not be read (%s); period-overlap check skipped.", exc)
        return None


def _handle_partial(args, cfg, report, send_discord: bool) -> int:
    """A partial report is never stored (it must not occupy the period's snapshot or sent-flag, so the
    complete report for the same week is stored and sent normally) and is posted to Discord only with
    the explicit --send-partial flag."""
    from .discord_notifier import DiscordError, send_report

    if not (send_discord and args.send_partial):
        log.info("Partial report built (%d/%d days); not stored and not sent. Add --send-partial to post it.",
                 report.days_with_data, report.period.days)
        return 0
    try:
        send_report(cfg.discord_webhook_url, report)
    except DiscordError as exc:
        log.error("Discord delivery of the partial report failed: %s", exc)
        return 5
    log.info("Partial report sent (%d/%d days); not stored, the complete report will follow normally.",
             report.days_with_data, report.period.days)
    return 0


def _run_report(args, cfg) -> int:
    import sqlite3

    from googleapiclient.errors import HttpError

    from .analytics_client import AnalyticsClient
    from .auth import AuthError, build_services, get_credentials
    from .discord_notifier import DiscordError, send_error, send_report
    from .pipeline import IncompletePeriodError, ReportDataError, build_report
    from .retry import http_error_reason
    from .storage import Storage
    from .youtube_client import YouTubeClient

    if args.send_partial and not args.allow_incomplete:
        log.error("--send-partial requires --allow-incomplete.")
        return 2
    send_discord = not args.dry_run and not args.no_discord
    if send_discord and not cfg.discord_configured:
        log.error("DISCORD_WEBHOOK_URL is not configured. Use --no-discord to skip sending.")
        return 2

    def notify_failure(title: str, detail: str) -> None:
        log.error("%s: %s", title, detail)
        if send_discord:
            send_error(cfg.discord_webhook_url, title, detail)

    try:
        creds = get_credentials(cfg, interactive=False, save_refreshed=not args.dry_run)
        yt_svc, an_svc = build_services(creds)
        report = build_report(cfg, YouTubeClient(yt_svc), AnalyticsClient(an_svc), end=args.end_date,
                              history=_stored_periods(cfg, args.dry_run), allow_incomplete=args.allow_incomplete)
    except AuthError as exc:
        notify_failure("YouTube authorization required", str(exc))
        return 3
    except HttpError as exc:
        hint = ""
        if getattr(exc.resp, "status", None) == 403:
            hint = " Check that YouTube Data API v3 and YouTube Analytics API are enabled and the quota is not exhausted."
        notify_failure("YouTube API error", http_error_reason(exc) + hint)
        return 4
    except IncompletePeriodError as exc:
        notify_failure("Reporting period not complete yet", str(exc))
        return EXIT_INCOMPLETE
    except ReportDataError as exc:
        notify_failure("No analytics data for the reporting period", str(exc))
        return 6
    except Exception as exc:  # noqa: BLE001 - top-level guard, details go to the log only
        log.exception("Report generation failed")
        notify_failure("Report generation failed", f"{type(exc).__name__}: {redact(str(exc))[:500]}")
        return 1

    if args.dry_run or args.print:
        _print_summary(report)
    if args.dry_run:
        log.info("Dry run: nothing stored or sent.")
        return 0
    if report.partial:
        return _handle_partial(args, cfg, report, send_discord)

    try:
        db = Storage(cfg.database_path)
        report_id = db.save_report(report)
        already_sent = db.sent_at(report_id)
        log.info("Snapshot stored (report id %d, %d report(s) in history).", report_id, db.report_count())
    except sqlite3.Error as exc:
        # Without a stored snapshot the duplicate-send guard cannot work, so do not send either.
        notify_failure("Database error", f"Could not store the snapshot ({type(exc).__name__}: {exc}). Nothing was sent.")
        return 7

    with db:
        if not send_discord:
            return 0
        if already_sent and not args.force_send:
            log.info("Report for this period was already sent at %s; use --force-send to resend.", already_sent)
            return 0
        try:
            send_report(cfg.discord_webhook_url, report)
        except DiscordError as exc:
            log.error("Discord delivery failed: %s", exc)
            return 5
        try:
            db.mark_sent(report_id)
        except sqlite3.Error as exc:
            log.error("Report sent, but the sent-flag could not be stored (%s); a retry may resend it.", exc)
            return 7
    return 0


def cmd_auth(args, cfg) -> int:
    from .auth import AuthError, build_services, get_credentials
    from .youtube_client import YouTubeClient

    try:
        creds = get_credentials(cfg, interactive=True, open_browser=not args.no_browser)
        yt_svc, _ = build_services(creds)
        ch = YouTubeClient(yt_svc).get_channel()
    except AuthError as exc:
        log.error("%s", exc)
        return 3
    print(f"Authorized for channel: {ch.title} ({ch.channel_id})")
    return 0


def cmd_test_discord(args, cfg) -> int:
    from .discord_notifier import DiscordError, send_test

    if not cfg.discord_configured:
        log.error("DISCORD_WEBHOOK_URL is not configured in .env")
        return 2
    try:
        send_test(cfg.discord_webhook_url)
    except DiscordError as exc:
        log.error("%s", exc)
        return 5
    print("Test message delivered to Discord.")
    return 0


def cmd_check(args, cfg) -> int:
    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'OK' if good else '!!'}] {label}{(' — ' + detail) if detail else ''}")

    print("Configuration check (no secret values are printed):")
    line("OAuth client file", cfg.client_secrets_file.exists() or bool(cfg.token_json), cfg.client_secrets_file.name)
    line("OAuth token", cfg.token_file.exists() or bool(cfg.token_json),
         "from YT_TOKEN_JSON" if cfg.token_json else cfg.token_file.name)
    line("Discord webhook", cfg.discord_configured)
    line("Database directory writable", _writable(cfg.database_path.parent), str(cfg.database_path))
    print(f"  [--] Claude API recommendations: {'enabled' if cfg.ai_enabled else 'disabled'}")
    from .periods import WEEKDAYS

    week_end = "rolling window" if cfg.report_week_end is None else WEEKDAYS[cfg.report_week_end].title()
    print(f"  [--] Report week ends on: {week_end} (Pacific time, DATA_LAG_DAYS={cfg.data_lag_days})")
    return 0 if ok else 1


def _writable(path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_demo(args, cfg) -> int:
    import sqlite3

    from .demo import DemoIsolationError, run_demo

    try:
        db_path, preview_path, reports = run_demo(cfg, weeks=args.weeks)
    except DemoIsolationError as exc:
        log.error("%s", exc)
        return 2
    except sqlite3.Error as exc:
        log.error("Demo database could not be written (%s: %s). Delete or restore data/demo/demo.db "
                  "and run the demo again.", type(exc).__name__, exc)
        return 7
    print("DEMO MODE — synthetic data, not real channel analytics. No API calls, nothing sent to Discord.")
    print(f"  {len(reports)} synthetic weekly reports stored in {db_path}")
    print(f"  Discord payload preview (not sent): {preview_path}")
    if args.print:
        _print_summary(reports[-1])
    if args.no_dashboard:
        return 0
    return _serve(cfg, host=cfg.dashboard_host, port=args.port or cfg.dashboard_port,
                  open_browser=not args.no_browser, db_path=db_path, mode="demo")


def _serve(cfg, **kwargs) -> int:
    from .dashboard import DashboardStartError, serve

    try:
        serve(cfg, **kwargs)
    except DashboardStartError as exc:
        log.error("%s", exc)
        return 2
    return 0


def cmd_dashboard(args, cfg) -> int:
    return _serve(cfg, host=args.host or cfg.dashboard_host, port=args.port or cfg.dashboard_port,
                  open_browser=not args.no_browser)


def _bounded_int(low: int, high: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"must be an integer, got {raw!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {value}")
        return value
    return parse


PORT = _bounded_int(1, 65535)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yt-report", description="Weekly YouTube Analytics report bot")
    p.add_argument("--env-file", help="Path to .env (default: search from current directory)")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Build, store and send the weekly report")
    run.add_argument("--dry-run", action="store_true", help="Fetch and print only; no DB write, no Discord")
    run.add_argument("--no-discord", action="store_true", help="Store in SQLite but do not send to Discord")
    run.add_argument("--force-send", action="store_true", help="Send even if this period was already sent")
    run.add_argument("--print", action="store_true", help="Print a text summary to the console")
    run.add_argument("--end-date", type=date.fromisoformat, help="Last day of the period (YYYY-MM-DD, Pacific time)")
    run.add_argument("--allow-incomplete", action="store_true",
                     help="Build a clearly labelled PARTIAL report if the last days of the period have no data yet "
                          "(no comparisons, no recommendations, never stored). Does not send by itself.")
    run.add_argument("--send-partial", action="store_true",
                     help="Also post a partial report to Discord (requires --allow-incomplete; not with --dry-run)")
    run.set_defaults(func=cmd_run)

    auth = sub.add_parser("auth", help="Authorize Google account (one-time)")
    auth.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    auth.set_defaults(func=cmd_auth)

    dash = sub.add_parser("dashboard", help="Start the local dashboard")
    dash.add_argument("--host")
    dash.add_argument("--port", type=PORT)
    dash.add_argument("--no-browser", action="store_true")
    dash.set_defaults(func=cmd_dashboard)

    demo = sub.add_parser("demo", help="Synthetic demo (no API, no Discord, separate database)")
    demo.add_argument("--weeks", type=_bounded_int(1, 104), default=8,
                      help="Number of synthetic weekly reports, 1-104 (default 8)")
    demo.add_argument("--print", action="store_true", help="Print a text summary of the latest synthetic report")
    demo.add_argument("--no-dashboard", action="store_true", help="Only build the demo data")
    demo.add_argument("--port", type=PORT)
    demo.add_argument("--no-browser", action="store_true")
    demo.set_defaults(func=cmd_demo)

    sub.add_parser("test-discord", help="Send a webhook test message").set_defaults(func=cmd_test_discord)
    sub.add_parser("check", help="Validate configuration").set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows pipes/cron logs may use a legacy code page; video titles contain emoji/diacritics.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level, cfg.log_dir)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
