"""Command-line entry point: `work-assistant <command>`."""
from __future__ import annotations

import argparse
import getpass
import socket
import sqlite3
import sys
import time
import traceback
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import httpx

from work_assistant import (backups, credentials, db, desktop, names, pipeline, retention, rubric, scheduler, scoring,
                      server_config, settings, timeutil, transfer)
from work_assistant.activity import collect
from work_assistant.activity import store as activity_store
from work_assistant.activity import sync as activity_sync
from work_assistant.models import RubricVersion, TimedParams
from work_assistant.procutil import pid_alive, resume_lock, resume_lock_path
from work_assistant.reviewer.catalog import MODELS
from work_assistant.runlog import LOG_DIR
from work_assistant.seed_rubric import default_checks, default_reference
from work_assistant.timing import format_minutes
from work_assistant.zendesk.client import ZendeskAuthError

SECRET_PROMPTS = {
    "zendesk_token": "Zendesk API token (hidden, blank keeps the saved one): ",
    "anthropic_api_key": "Anthropic API key (hidden, blank keeps the saved one): ",
}


def _require(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        sys.exit(f"{label} is required.")
    return value


def normalize_subdomain(raw: str) -> str:
    """Reduce `acme`, `acme.zendesk.com` or `https://acme.zendesk.com/` to `acme`, or exit with an error."""
    try:
        return settings.normalize_subdomain(raw)
    except ValueError as exc:
        sys.exit(str(exc))


def cmd_setup(db_path: Path) -> int:
    subdomain = normalize_subdomain(_require(input("Zendesk subdomain (the part before .zendesk.com): "), "Zendesk subdomain"))
    email = _require(input("Zendesk login email: "), "Zendesk login email")
    # Collect and validate every secret before saving anything.
    secrets: dict[str, str] = {}
    for name, prompt in SECRET_PROMPTS.items():
        raw = getpass.getpass(prompt)
        if raw and not raw.strip():
            sys.exit(f"{name} contains only whitespace; enter the value, or press Enter on its own to keep the saved one.")
        value = raw.strip()
        if value:
            secrets[name] = value
        elif not credentials.get_secret(name):
            sys.exit(f"{name} is required; none is saved yet.")

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        settings.set_setting(conn, "zendesk_subdomain", subdomain)
        settings.set_setting(conn, "zendesk_email", email)
    finally:
        conn.close()
    for name, value in secrets.items():
        credentials.set_secret(name, value)
    print("Saved.")
    return 0


SERVER_SECRET_PROMPTS = {"zendesk_token": "Zendesk API token (hidden): "}   # what `set-secret` accepts


def cmd_set_secret(name: str) -> int:
    """Save one secret where the server config says (its secrets file, else the keyring), typed at a hidden
    prompt. The value is never shown, printed or logged."""
    value = getpass.getpass(SERVER_SECRET_PROMPTS[name])
    if not value.strip():
        sys.exit(f"Nothing saved: the {name} was blank.")
    try:
        credentials.set_secret(name, value)
    except ValueError as exc:   # a secrets file that isn't a JSON object; the message never quotes it
        sys.exit(str(exc))
    print("Saved.")
    return 0


def _load_config(path: Path) -> server_config.ServerConfig:
    """The server config at `path`, or exit with what is wrong with it."""
    try:
        return server_config.load(path)
    except server_config.ConfigError as exc:
        sys.exit(str(exc))


def _noop() -> None:
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Services:
    """The outside world a run talks to. Production builds the real ones; tests pass fakes."""

    anthropic_client: Any
    fetcher: pipeline.Fetcher | None = None
    ids_fetcher: pipeline.IdsFetcher | None = None   # chosen tickets by id, for Review again
    images_loader: pipeline.ImagesLoader | None = None
    log_dir: Path = LOG_DIR
    secrets: tuple[str, ...] = ()
    close: Callable[[], None] = field(default=_noop)
    now: Callable[[], datetime] = field(default=_utcnow)  # the clock that picks "last week"
    alive: Callable[[int], bool] = field(default=pid_alive)  # whether a run's recorded process still runs


def build_services(conn: sqlite3.Connection, *, need_zendesk: bool) -> Services:
    """Real clients from the saved settings and the keyring. Exits with a setup hint when missing."""
    import anthropic

    from work_assistant.zendesk.client import ZendeskClient
    from work_assistant.zendesk.fetch import fetch_contacts, fetch_tickets

    api_key = credentials.get_secret("anthropic_api_key")
    if not api_key:
        sys.exit("No Anthropic API key is saved. Run: work-assistant setup")
    # Default SDK retries for reads; reviewer.batch.submit turns them off for batches.create.
    client = anthropic.Anthropic(api_key=api_key)
    if not need_zendesk:
        return Services(anthropic_client=client, secrets=(api_key,))

    subdomain = settings.get_setting(conn, "zendesk_subdomain")
    email = settings.get_setting(conn, "zendesk_email")
    token = credentials.get_secret("zendesk_token")
    if not (subdomain and email and token):
        sys.exit("Zendesk is not set up yet. Run: work-assistant setup")
    zendesk = ZendeskClient(subdomain, email, token)
    team = settings.team_emails(conn)
    if not team:
        print("Warning: no team members are configured, so no calls will qualify.")

    def fetcher(window: pipeline.Window, log) -> list:
        return fetch_contacts(zendesk, window, team, log)

    def ids_fetcher(ticket_ids, log) -> list:
        return fetch_tickets(zendesk, ticket_ids, team, log)

    return Services(
        anthropic_client=client,
        fetcher=fetcher,
        ids_fetcher=ids_fetcher,
        images_loader=pipeline.zendesk_images_loader(zendesk),
        secrets=(api_key, token),
        close=zendesk.close,
    )


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date in the form YYYY-MM-DD") from None


def _email(text: str) -> str:
    try:
        return settings.normalize_email(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an email address") from None


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of at least 1")
    return value


def _window(conn: sqlite3.Connection, args: argparse.Namespace, now: datetime) -> pipeline.Window:
    tz = settings.get_timezone(conn)
    if args.last_week:
        return timeutil.previous_week(now, tz)
    if args.week_of is not None:
        return timeutil.week_window(args.week_of, tz)
    if args.to_date is None:
        sys.exit("--from needs --to.")
    if args.to_date < args.from_date:
        sys.exit("--from must be on or before --to.")
    return timeutil.date_range_window(args.from_date, args.to_date, tz)


def _report(conn: sqlite3.Connection, run_id: int) -> int:
    """Print the run's outcome (never secrets or customer content) and return the exit code."""
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    estimate = run["estimate_usd"] or 0.0
    actual = run["actual_usd"] or 0.0
    status = run["status"]
    print(
        f"Run {run_id}: {status}. Contacts: {run['contact_count'] or 0}. "
        f"Estimate: ${estimate:.2f}. Actual: ${actual:.2f}."
    )
    test = run["kind"] == pipeline.TEST_KIND
    if run["excluded_purged"]:
        count = run["excluded_purged"]
        if test:
            print(f"{count} purged call{'s' if count != 1 else ''} left out: "
                  f"{'they' if count != 1 else 'it'} can't be tested because {'their' if count != 1 else 'its'} "
                  "content was erased after the retention period.")
        else:
            print(f"{count} purged call{'s' if count != 1 else ''} left out: their content was erased after the "
                  "retention period, so they cannot be reviewed again.")
    errored = conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE run_id = ? AND status = 'error'", (run_id,)
    ).fetchone()[0]
    if errored:
        print(f"{errored} call{'s' if errored != 1 else ''} could not be reviewed "
              f"(see work-assistant show --run {run_id})")
    if status == "awaiting_confirmation":
        limit = float(settings.get_setting(conn, "budget_limit_usd"))
        print(f"The estimate is over the ${limit:.2f} limit. To go ahead, run: work-assistant confirm {run_id}")
        print(f"Or, to drop it without sending anything, run: work-assistant cancel-run {run_id}")
        return 0
    if status == "failed":
        print(f"Error: {run['error']}")
        return 1
    if status == "submitted":
        print(f"The batch is still in flight ({run['error']}). To collect it later, run: work-assistant resume")
        return 1
    if status in ("submission_unknown", "submitting"):
        print(f"Error: {pipeline.SUBMISSION_UNRECORDED}")
        if test:
            print(f"A model test holds no calls. Once you have checked, run: work-assistant clear-run {run_id}")
        else:
            print(f"Its calls stay held until you check, then run: work-assistant clear-run {run_id}")
        return 1
    return 0


def _local_days(window: pipeline.Window, tz: str) -> tuple[date, date]:
    """The first and last local day of a window (its end is the midnight after the last day)."""
    zone = ZoneInfo(tz)
    return window[0].astimezone(zone).date(), window[1].astimezone(zone).date() - timedelta(days=1)


def _plural(count: int, word: str, plural: str | None = None) -> str:
    """`count` and `word`, or `plural` (default `word` + "s") when count isn't 1."""
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _scheduled_days(conn: sqlite3.Connection, now: datetime) -> list[pipeline.Window]:
    """The days the daily task reviews: each complete day after the latest period a scheduled run covers.

    That period is a day, or a week from before the switch to daily runs. A failed or cancelled run
    covers nothing, so its days are tried again. Days older than the retention period are skipped,
    with a line saying so.
    """
    tz = settings.get_timezone(conn)
    latest_end = conn.execute(
        "SELECT MAX(window_end) FROM runs WHERE kind = 'scheduled' AND status NOT IN ('failed', 'cancelled')"
    ).fetchone()[0]
    retention = int(settings.get_setting(conn, "retention_days"))
    days, skipped = timeutil.missed_days(
        datetime.fromisoformat(latest_end) if latest_end else None, now, tz, retention)
    if skipped:
        print(f"{_plural(skipped, 'missed day')} {'was' if skipped == 1 else 'were'} skipped: "
              f"{'it' if skipped == 1 else 'they'} started more than {retention} days ago (the data retention "
              f"period), so {'its' if skipped == 1 else 'their'} calls are not reviewed.")
    if not days:
        yesterday = _local_days(timeutil.previous_day(now, tz), tz)[0]
        print(f"Every day up to {yesterday.isoformat()} is already reviewed; nothing new to review.")
    return days


def _catch_up(conn: sqlite3.Connection, args: argparse.Namespace, services: Services, now: datetime,
              common: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Review each missed day, oldest first, as its own scheduled run. Returns (run ids, exit codes).

    A day paused by the budget limit does not stop the later days. A failed day does: later days
    would otherwise count as covered, and the failed one would never be retried.
    """
    days = _scheduled_days(conn, now)
    tz = settings.get_timezone(conn)
    run_ids: list[int] = []
    codes: list[int] = []
    for index, day in enumerate(days):
        local = _local_days(day, tz)[0]
        print(f"Reviewing {local:%A} {local.isoformat()}.")
        run_id = pipeline.start_run(conn, "scheduled", day, fetcher=services.fetcher,
                                    images_loader=services.images_loader, confirm=args.yes, limit=args.limit,
                                    **common)
        run_ids.append(run_id)
        codes.append(_report(conn, run_id))
        later = len(days) - index - 1
        failed = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] == "failed"
        if failed and later:
            print(f"Stopped after a failed day: it and the {_plural(later, 'later day')} "
                  "are tried again at the next daily run.")
            break
    return run_ids, codes


def cmd_run(conn: sqlite3.Connection, args: argparse.Namespace, services: Services, lock: Path) -> int:
    if args.scheduled and args.rereview:
        sys.exit("--scheduled and --rereview cannot be combined.")
    if args.scheduled and (args.week_of is not None or args.from_date is not None):
        sys.exit("--scheduled picks its own days (each one since the last scheduled run), so it takes no "
                 "--week-of or --from.")
    now = services.now()
    # A scheduled run always catches up day by day. A task installed before the switch to daily runs
    # still passes --last-week, which changes nothing here. Review again of chosen tickets has no period.
    window = None if args.scheduled or args.ticket_ids is not None else _window(conn, args, now)
    common = dict(anthropic_client=services.anthropic_client, log_dir=services.log_dir, secrets=services.secrets)
    if args.scheduled:
        try:
            # A run whose process died while fetching would count as covering its days; fail it, as the
            # app's startup does, so its days are reviewed again. A live fetch (the app's) is left alone.
            for run_id in pipeline.fail_interrupted_fetching(conn, now, services.alive):
                _report_interrupted(conn, run_id)
            with resume_lock(lock) as acquired:
                if acquired:
                    for run_id in pipeline.resume_runs(conn, **common):
                        _report(conn, run_id)
                else:
                    print("Another process (the app) is collecting interrupted runs; not collecting them here.")
            run_ids, codes = _catch_up(conn, args, services, now, common)
        finally:
            _daily_activity(conn, services)  # the sync costs nothing, so a failed review never skips it
    elif args.ticket_ids is not None:
        try:
            run_id = pipeline.rereview_tickets(conn, args.ticket_ids, fetcher_for_ids=services.ids_fetcher,
                                               images_loader=services.images_loader, confirm=args.yes, **common)
        except ValueError as exc:
            sys.exit(str(exc))
        run_ids, codes = [run_id], [_report(conn, run_id)]
    else:
        if args.rereview:
            run_id = pipeline.rereview(conn, window, fetcher=services.fetcher, images_loader=services.images_loader,
                                       confirm=args.yes, limit=args.limit, **common)
        else:
            run_id = pipeline.start_run(conn, "manual", window, fetcher=services.fetcher,
                                        images_loader=services.images_loader, confirm=args.yes, limit=args.limit,
                                        **common)
        run_ids, codes = [run_id], [_report(conn, run_id)]
    _report_other_open_runs(conn, run_ids)
    return max(codes, default=0)


PC_SYNC_OFF = "Team activity sync is off in Settings, so the daily task doesn't read Zendesk activity."


def _daily_activity(conn: sqlite3.Connection, services: Services | None) -> None:
    """The daily task's Team activity sync. `services` is None when the review side could not be set up (no Anthropic
    key): the sync needs only Zendesk. A failure is logged in one line and never changes the run's result. With the
    "Sync Team activity" setting off (the PC once the hosted copy collects the activity) it doesn't run."""
    if not settings.get_setting(conn, "activity_sync_enabled"):
        print(PC_SYNC_OFF)
        return
    try:
        cmd_activity_sync(conn, argparse.Namespace(kind="daily"), services)
    except Exception as exc:
        print(f"Team activity sync failed: {activity_sync.describe_error(exc)}")


def _activity_client(conn: sqlite3.Connection) -> Any:
    """A read-only Zendesk client from the saved settings and the keyring, or None when Zendesk isn't set up.
    Team activity and what the sync collects for 1:1 Prep and Trends read through it."""
    from work_assistant.zendesk.client import ZendeskClient

    subdomain = settings.get_setting(conn, "zendesk_subdomain")
    email = settings.get_setting(conn, "zendesk_email")
    token = credentials.get_secret("zendesk_token")
    if not (subdomain and email and token):
        return None
    return ZendeskClient(subdomain, email, token)


def _zendesk_not_set_up(args: argparse.Namespace) -> None:
    """Say that Zendesk isn't set up, and how to set it up: with the server config's secrets file when the command
    was given one (`work-assistant setup` would ask for an Anthropic key and use the keyring), else with `setup`."""
    config = getattr(args, "config", None)
    if config is not None:
        print("Zendesk is not set up yet. Save the Zendesk subdomain and login email in Settings, and the token "
              f"with: work-assistant set-secret zendesk_token --config {config}")
    else:
        print("Zendesk is not set up yet. Run: work-assistant setup")


def cmd_activity_sync(conn: sqlite3.Connection, args: argparse.Namespace, services: Services | None = None,
                      log_dir: Path = LOG_DIR) -> int:
    """Sync the team's activity from Zendesk. Needs Zendesk, never Anthropic. Returns 1 when it doesn't complete.
    `log_dir` is the logs folder (the server config's with `--config`) unless `services` names one."""
    return _sync_activity(conn, args, services, log_dir)


def _sync_activity(conn: sqlite3.Connection, args: argparse.Namespace, services: Services | None,
                   log_dir: Path, fill: list[str] | None = None) -> int:
    client = _activity_client(conn)
    if client is None:
        _zendesk_not_set_up(args)
        return 1
    now = services.now() if services is not None else _utcnow()
    log_dir = services.log_dir if services is not None else log_dir
    alive = services.alive if services is not None else pid_alive
    try:
        extra = {"fill": fill} if fill else {}
        if getattr(args, "reach_back", None) is not None:
            extra["reach_back"] = timeutil.day_window(args.reach_back, settings.get_timezone(conn))[0]
        result = activity_sync.sync(conn, client, kind=args.kind, now=now, log=print, logs_dir=log_dir, alive=alive,
                                    **extra)
    except activity_sync.SyncBusy as exc:
        print(exc)
        return 1
    finally:
        client.close()
    if result["status"] != "complete":
        return 1
    print(f"Team activity synced through {result['synced_through']:%Y-%m-%d %H:%M} UTC: "
          f"{_plural(result['pages'], 'page')}, {_plural(result['actions'], 'new action')}.")
    return 0


def cmd_activity_count_collected(conn: sqlite3.Connection, args: argparse.Namespace,
                                 services: Services | None = None) -> int:
    """Count, read-only, the department's call legs, calls, ratings and changed tickets in the last `--days` days
    (`collect.count_volumes`), before the first history read-back. Stores nothing; needs no Anthropic key. A failed
    read prints one plain line, as `activity sync` reports a failure (`describe_error`), and returns 1; rejected
    credentials print the message the other commands give."""
    client = _activity_client(conn)
    if client is None:
        _zendesk_not_set_up(args)
        return 1
    now = services.now() if services is not None else _utcnow()
    try:
        # The span ends RATINGS_BEFORE_NOW back: Zendesk refuses a ratings end_time that hasn't passed.
        counts = collect.count_volumes(client, start=now - timedelta(days=args.days),
                                       end=now - collect.RATINGS_BEFORE_NOW)
    except ZendeskAuthError:
        print(pipeline.zendesk_token_rejected(getattr(args, "config", None)))
        return 1
    except Exception as exc:
        print(f"Counting failed: {activity_sync.describe_error(exc)}")
        return 1
    finally:
        client.close()
    print(f"{counts['legs']} call legs, {counts['calls']} calls, {counts['ratings']} ratings, "
          f"{counts['tickets']} changed tickets in the last {args.days} days.")
    return 0


FILL_AGAIN = "Run the same command again: it carries on from where it stopped."
FILL_NEXT = ("Their past 1:1 preps count this history as soon as their team lead sets their 1:1 schedule "
             "(Settings, 1:1 schedule). Days they were off in that time count as missed until someone enters them "
             "in Settings, Time off.")


def cmd_activity_fill_techs(conn: sqlite3.Connection, args: argparse.Namespace, services: Services | None = None,
                            log_dir: Path = LOG_DIR) -> int:
    """Fill the history of the techs added after the stored activity began (`activity_sync.techs_to_fill`), once
    the owner says yes: a backfill sync that first reads their activity back to the start of everyone's. Read-only
    on Zendesk; needs no Anthropic key."""
    covered = activity_store.synced_from(conn)
    skip = {email.strip().lower() for email in args.skip}
    late = [(email, start) for email, start in activity_sync.techs_to_fill(conn) if email not in skip]
    if covered is None:
        print("No history is stored yet: the first sync reads every tech's history. Nothing to fill.")
        return 0
    first = activity_sync.first_covered_day(conn, covered)
    if not late:
        print(f"Every tech's history already starts {first.isoformat()}"
              f"{', apart from those skipped' if skip else ''}. Nothing to fill.")
        return 0
    print(f"The stored history starts {first.isoformat()}. These techs' history starts later:")
    for email, start in late:
        print(f"  {email}: from {start.isoformat()}")
    print("This reads Zendesk (read-only) back to the start for them, which can take hours. It is safe to stop: "
          "running it again carries on from where it stopped.")
    if not args.yes and input("Fill their history now? [y/N] ").strip().lower() != "y":
        print("Nothing was changed.")
        return 0
    code = _sync_activity(conn, argparse.Namespace(kind="backfill", config=getattr(args, "config", None)), services,
                          log_dir, fill=[email for email, _ in late])
    print(FILL_NEXT if code == 0 else FILL_AGAIN)
    return code


def _counts_line(counts: dict[str, int]) -> str:
    return (f"{_plural(counts['activity_actions'], 'action')}, {_plural(counts['activity_signins'], 'sign-in')}, "
            f"{_plural(counts['activity_sync'], 'sync')}, {_plural(counts['shift_versions'], 'shift version')}, "
            f"{_plural(counts['time_off'], 'time off entry', 'time off entries')}, "
            f"{_plural(counts['tech_names'], 'tech name')}")


def cmd_export_activity(path: Path, out: Path) -> int:
    """Write this copy's Team activity history to a new file, for `import-activity` on the hosted copy."""
    conn = db.connect(path)
    try:
        db.init_db(conn)
        counts = transfer.export_activity(conn, out)
    except (FileExistsError, FileNotFoundError) as exc:
        sys.exit(str(exc))
    finally:
        conn.close()
    print(f"Exported Team activity to {out}: {_counts_line(counts)}.")
    print("Copy it to the hosted copy and run: work-assistant import-activity FILE --config PATH. It holds ticket "
          "subjects, so delete it once it's imported.")
    return 0


def cmd_import_activity(config: server_config.ServerConfig, source: Path) -> int:
    """Put the Team activity history in `source` (from `export-activity`) in place of the config database's,
    and assign the whole roster to the config's admin."""
    if not config.admin_email:
        sys.exit("The server config names no admin_email: the import assigns every tech to the admin.")
    conn = db.connect(config.db_path)
    try:
        db.init_db(conn)
        counts = transfer.import_activity(conn, source, admin_email=config.admin_email)
    except ValueError as exc:
        sys.exit(str(exc))
    finally:
        conn.close()
    # The newest copy is the one this import just made (copy_beside adds "-2" to a name taken that second, which sorts
    # before the plain name, so pick by time, not by name).
    saved = max(config.db_path.parent.glob(f"{config.db_path.name}.before-import-*.db"),
                key=lambda p: p.stat().st_mtime_ns)
    print(f"Imported Team activity from {source}: {_counts_line(counts)}.")
    print(f"Every tech on the roster is assigned to {config.admin_email}. The database as it was before is "
          f"saved as {saved}.")
    return 0


def cmd_backup_db(config: server_config.ServerConfig, keep_days: int, now: datetime,
                  before_update: bool = False) -> int:
    """Copy the config's database into `backups/` beside it, named by the day `now` falls on in the config's time
    zone, and delete the copies more than `keep_days` days old, the one kept for a rollback included (backups.py); the
    hosted copy's nightly timer runs it. With `before_update` (update.ps1, before it installs a new version) the copy
    is the one kept for a rollback, `before-update-<UTC time>.db`, in place of the last update's, and the nightly
    copies are left as they are. With no database yet (a first install) there is nothing to copy, which is not a
    failure."""
    if not config.db_path.is_file():
        print(f"There is no database at {config.db_path} yet, so there is nothing to back up.")
        return 0
    try:
        if before_update:
            target, deleted = backups.back_up_before_update(config.db_path, now)
        else:
            conn = db.connect(config.db_path)
            try:
                zone = settings.get_timezone(conn)   # the config's zone, which `main` pinned
            finally:
                conn.close()
            target, deleted = backups.back_up(config.db_path, now.astimezone(ZoneInfo(zone)).date(), keep_days)
    except (sqlite3.Error, OSError) as exc:
        sys.exit(f"The backup of {config.db_path} failed: {exc}")
    if before_update:
        print(f"Backed up the database to {target}, the copy a rollback with --restore-db puts back.")
        for old in deleted:
            print(f"Deleted {old.name}, the copy the previous update kept.")
        return 0
    print(f"Backed up the database to {target}.")
    nightly = [old for old in deleted if not old.name.startswith(backups.BEFORE_UPDATE)]
    if nightly:
        print(f"Deleted {_plural(len(nightly), 'backup')} older than {_plural(keep_days, 'day')}.")
    for old in deleted:
        if old not in nightly:
            print(f"Deleted {old.name}, the copy taken before an update more than {_plural(keep_days, 'day')} ago: "
                  "a rollback can no longer put the database back.")
    return 0


def _report_interrupted(conn: sqlite3.Connection, run_id: int) -> None:
    """Say that a run whose process died while fetching (or, for a model test, downloading images) is failed.

    Plain ASCII: the daily task's output can go to a console that can't encode an arrow. Only a scheduled
    run's days are retried by themselves; any other run is started again by the owner.
    """
    kind = conn.execute("SELECT kind FROM runs WHERE run_id = ?", (run_id,)).fetchone()["kind"]
    if kind == pipeline.TEST_KIND:
        print(f"Model test {run_id} was interrupted while preparing and is marked failed. Start the test again "
              "from Settings > Test a model, or with work-assistant test-model.")
    elif kind == "scheduled":
        print(f"Run {run_id} was interrupted while fetching and is marked failed, so its days are reviewed again.")
    else:
        print(f"Run {run_id} was interrupted while fetching and is marked failed. "
              "Start it again to review its calls.")


def _report_other_open_runs(conn: sqlite3.Connection, run_ids: list[int]) -> None:
    """Point at runs that hold tickets, since a new run skips the tickets they hold. Model tests hold none."""
    holding = pipeline.HOLDING_STATUSES
    for run in conn.execute(
        f"SELECT run_id, status FROM runs WHERE status IN ({', '.join('?' for _ in holding)}) AND kind != ? "
        "ORDER BY run_id",
        (*holding, pipeline.TEST_KIND),
    ):
        if run["run_id"] in run_ids:
            continue
        other = run["run_id"]
        if run["status"] == "awaiting_confirmation":
            print(f"Run {other} is awaiting confirmation and keeps its calls; "
                  f"to go ahead with it, run: work-assistant confirm {other} (or work-assistant cancel-run {other} to drop it)")
        elif run["status"] == "submitted":
            print(f"Run {other} is still submitted and keeps its calls; to collect it, run: work-assistant resume")
        elif run["status"] == "submission_unknown":
            print(f"Run {other} keeps its calls: {pipeline.SUBMISSION_UNRECORDED} "
                  f"Then release them with: work-assistant clear-run {other}")
        else:
            print(f"Run {other} is submitting a batch and keeps its calls.")


def _check_test_choices(args: argparse.Namespace) -> None:
    """Exit with the choices when `test-model` names a model or effort the app doesn't know."""
    if args.model not in MODELS:
        choices = ", ".join(f"{m.id} ({m.label})" for m in MODELS.values())
        sys.exit(f"Unknown model {args.model!r}. Choose one of: {choices}.")
    if args.effort not in settings.EFFORTS:
        sys.exit(f"Unknown effort {args.effort!r}. Choose one of: {', '.join(settings.EFFORTS)}.")
    if args.to_date < args.from_date:
        sys.exit("--from must be on or before --to.")


def cmd_test_model(conn: sqlite3.Connection, args: argparse.Namespace, services: Services) -> int:
    """Score the range's already-reviewed calls with another model, apart from what the app shows."""
    window = timeutil.date_range_window(args.from_date, args.to_date, settings.get_timezone(conn))
    plan = pipeline.estimate_test(conn, window, model_id=args.model, effort=args.effort, limit=args.limit)
    print(f"Model test: {MODELS[args.model].label} at effort {args.effort}, calls from {args.from_date.isoformat()} "
          f"to {args.to_date.isoformat()}.")
    # The Test a model panel's estimate line. Skipped calls have nothing to compare against.
    skipped = plan.excluded_unreviewed + plan.excluded_purged + plan.excluded_errored
    print(f"{plan.contacts} call{'s' if plan.contacts != 1 else ''} will be tested · {skipped} skipped "
          f"({plan.excluded_unreviewed} with no review, {plan.excluded_purged} erased by retention, "
          f"{plan.excluded_errored} with an errored review) · estimate ${plan.estimate_usd:.2f}")
    run_id = pipeline.start_test(conn, window, model_id=args.model, effort=args.effort, limit=args.limit,
                                 anthropic_client=services.anthropic_client, images_loader=services.images_loader,
                                 confirm=args.yes, log_dir=services.log_dir, secrets=services.secrets)
    code = _report(conn, run_id)
    print("A model test never changes the results the app shows.")
    return code


def cmd_resume(conn: sqlite3.Connection, services: Services, lock: Path) -> int:
    with resume_lock(lock) as acquired:
        if not acquired:
            print("Another process (the app) is already collecting interrupted runs. Try again later.")
            return 1
        run_ids = pipeline.resume_runs(conn, anthropic_client=services.anthropic_client, log_dir=services.log_dir,
                                       secrets=services.secrets)
    if not run_ids:
        print("No runs to resume.")
    return max((_report(conn, run_id) for run_id in run_ids), default=0)


def cmd_confirm(conn: sqlite3.Connection, run_id: int, services: Services) -> int:
    try:
        pipeline.confirm_run(conn, run_id, anthropic_client=services.anthropic_client,
                             images_loader=services.images_loader, log_dir=services.log_dir,
                             secrets=services.secrets)
    except ValueError as exc:
        sys.exit(str(exc))
    return _report(conn, run_id)


def cmd_clear_run(conn: sqlite3.Connection, run_id: int, assume_yes: bool, log_dir: Path) -> int:
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row["status"] != "submission_unknown":
        status = row["status"] if row is not None else "does not exist"
        sys.exit(f"Run {run_id} cannot be cleared: only a run in submission_unknown can (status: {status}).")
    print(f"Run {run_id}: {pipeline.SUBMISSION_UNRECORDED}")
    print("If a batch for this run is there, it is being (or was) paid for; clearing lets later runs "
          "review these calls again.")
    if not assume_yes:
        answer = input(f"Clear run {run_id} and release its calls? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Not cleared.")
            return 1
    pipeline.clear_run(conn, run_id, log_dir=log_dir)
    print(f"Run {run_id} cleared; its calls are released.")
    return 0


def cmd_cancel_run(conn: sqlite3.Connection, run_id: int, log_dir: Path) -> int:
    """Cancel a run or model test waiting for the owner's OK. Nothing was sent, so there is nothing to check."""
    try:
        pipeline.cancel_run(conn, run_id, log_dir=log_dir)
    except ValueError as exc:
        sys.exit(str(exc))
    kind = conn.execute("SELECT kind FROM runs WHERE run_id = ?", (run_id,)).fetchone()["kind"]
    if kind == pipeline.TEST_KIND:
        print(f"Model test {run_id} cancelled. Nothing was sent or charged.")
    else:
        print(f"Run {run_id} cancelled. Nothing was sent or charged, and its calls are free for later runs.")
    return 0


def cmd_purge(conn: sqlite3.Connection, log_dir: Path) -> int:
    days = int(settings.get_setting(conn, "retention_days"))
    count = retention.purge_expired(conn, datetime.now(timezone.utc), days, log_dir)
    print(f"Purged {count} call{'s' if count != 1 else ''} older than {days} days.")
    return 0


# -- schedule: the daily Task Scheduler entry ---------------------------------------------------------

SCHEDULE_SUMMARY = "every day at 06:00 it reviews the previous day's calls, and any days it missed."


def _schedule_line(state: dict) -> str:
    return (f"Next run: {state['next_run'] or 'none scheduled'}. Last run: {state['last_run'] or 'not yet'}. "
            f"Last result: {state['last_result'] or 'none yet'}.")


def cmd_schedule(action: str) -> int:
    """Install, remove or report the daily task. Never touches the database or any credential."""
    try:
        if action == "install":
            try:
                scheduler.install()
            except scheduler.SchedulerError as exc:
                print(f"Could not install the daily schedule: {exc}")
                return 1
            print(f"Installed the daily schedule: {SCHEDULE_SUMMARY}")
            try:
                next_run = scheduler.status()["next_run"]
            except (scheduler.SchedulerUnavailable, scheduler.SchedulerError):
                print("Couldn't read the next run time just now; run `work-assistant schedule status` later.")
            else:
                print(f"Next run: {next_run or 'none scheduled'}.")
            print("It runs only while you are logged on to this PC, and as soon as possible after a missed start.")
            return 0
        if action == "remove":
            try:
                removed = scheduler.remove()
            except scheduler.SchedulerError as exc:
                print(f"Could not remove the schedule: {exc}")
                return 1
            if not removed:
                print("The daily schedule is not installed, so there is nothing to remove.")
            else:
                print("Removed the daily schedule.")
            return 0
        state = scheduler.status()
    except scheduler.SchedulerUnavailable as exc:
        print(str(exc))
        return 1
    except scheduler.SchedulerError as exc:
        print(f"Could not read the daily schedule: {exc}")
        return 1
    if state["installed"]:
        print(f"The daily schedule is installed. {SCHEDULE_SUMMARY[0].upper()}{SCHEDULE_SUMMARY[1:]}")
        print(_schedule_line(state))
    else:
        print("The daily schedule is not installed. To install it, run: work-assistant schedule install")
    return 0


# -- show: read-only calibration view -----------------------------------------------------------------


def _open_read_only(path: Path) -> sqlite3.Connection | None:
    """Open the database for SELECTs only, or return None when the file does not exist."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _print_rubric(version: RubricVersion) -> None:
    enabled = [c for c in version.checks if c.enabled]
    print("Scorecard checks")
    for check in enabled:
        if check.kind != "scorecard":
            continue
        fail = ", auto-fail" if check.auto_fail else ""
        print(f"[{check.check_id}] #{check.number} {check.summary} ({check.points} pts{fail})")
        print(f"    {check.definition}")
        print(f"    Applies when: {check.applies_when}")
    print("\nProcess rules")
    for check in enabled:
        if check.kind != "process":
            continue
        print(f"[{check.check_id}] {check.summary}")
        print(f"    {check.definition}")
        print(f"    Applies when: {check.applies_when}")
    params = version.params
    print("\nTimed settings")
    print(f"  Call length limit: {params.call_limit_minutes} minutes")
    print(f"  Exempt priorities: {', '.join(params.exempt_priorities) or 'none'}")
    print(f"  Exempt tags: {', '.join(params.exempt_tags) or 'none'}")
    print(f"  Summary email deadline: {format_minutes(params.email_deadline_minutes)} after the call ends")
    for title, text in version.reference.items():
        print(f"\nReference document: {title}\n{text.rstrip()}")


def _show_rubric(conn: sqlite3.Connection | None) -> int:
    has_version = conn is not None and conn.execute("SELECT 1 FROM rubric_versions LIMIT 1").fetchone() is not None
    if has_version:
        version = rubric.current_version(conn)
        print(f"Rubric version {version.version_id} ({version.created_at}): {version.note}\n")
    else:
        # Nothing saved yet: the first run saves exactly these defaults. Show them without writing.
        version = RubricVersion(0, "", "", tuple(default_checks()), default_reference(), TimedParams())
        print("No rubric is saved yet. This is the default rubric the first run will save.\n")
    _print_rubric(version)
    return 0


def _show_run(conn: sqlite3.Connection | None, run_id: int) -> int:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone() if conn is not None else None
    if run is None:
        sys.exit(f"Run {run_id} does not exist.")
    print(f"Run {run_id}: {run['kind']}, {run['window_start']} to {run['window_end']}, status {run['status']}")
    print(f"Contacts: {run['contact_count'] or 0}. Estimate: ${run['estimate_usd'] or 0.0:.2f}. "
          f"Actual: ${run['actual_usd'] or 0.0:.2f}.")
    if run["error"]:
        print(f"Error: {run['error']}")
    threshold = int(settings.get_setting(conn, "problem_threshold"))
    versions: dict[int, RubricVersion] = {}
    verdicts = {"good": 0, "needs_review": 0, "problem": 0, "review_failed": 0}
    not_counted = 0
    contacts = conn.execute(
        "SELECT c.ticket_id, c.agent_email, c.agent_name, c.call_started_at, c.priority, c.attribution_ok "
        "FROM run_contacts rc JOIN contacts c ON c.ticket_id = rc.ticket_id WHERE rc.run_id = ? "
        "ORDER BY c.ticket_id", (run_id,)).fetchall()
    # A tech's typed name comes first, as everywhere the app shows a tech (names.py). A database the app has not
    # opened since typed names were added lacks their table (show is read-only, so it cannot add it).
    has_names = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tech_names'").fetchone()
    typed = names.stored(conn) if has_names else {}
    for contact in contacts:
        ticket_id = contact["ticket_id"]
        name = typed.get(contact["agent_email"].strip().lower(), contact["agent_name"])
        head = (f"\nTicket {ticket_id}  {name}  call {contact['call_started_at']}  "
                f"priority {contact['priority'] or 'none'}")
        review = conn.execute("SELECT * FROM reviews WHERE run_id = ? AND ticket_id = ? ORDER BY review_id DESC LIMIT 1",
                              (run_id, ticket_id)).fetchone()
        if review is None:
            print(f"{head}\n  No review stored for this ticket in this run.")
            continue
        if review["rubric_version_id"] not in versions:
            versions[review["rubric_version_id"]] = rubric.get_version(conn, review["rubric_version_id"])
        version = versions[review["rubric_version_id"]]
        by_id = {c.check_id: c for c in version.checks if c.enabled}
        rows = scoring.enabled_results(version, conn.execute(
            "SELECT * FROM check_results WHERE review_id = ? ORDER BY rowid", (review["review_id"],)))
        # A database the pipeline has not opened since Not counted was added lacks the column
        # (show is read-only, so it cannot upgrade it); every review there was counted.
        if "counted" in review.keys() and not review["counted"]:
            not_counted += 1
            print(f"{head}\n  NOT COUNTED: {review['not_counted_reason'] or 'no reason given'}  "
                  f"(rubric version {version.version_id})")
        else:
            result = scoring.evaluate(version, rows, bool(contact["attribution_ok"]), review["status"] == "ok",
                                      threshold)
            verdicts[result.verdict] += 1
            print(f"{head}\n  Score {result.earned}/{result.total} ({result.percent}%)  verdict {result.verdict}  "
                  f"resolved on call: {review['resolved_on_call'] or 'unknown'}  (rubric version {version.version_id})")
        if review["status"] != "ok":
            print(f"  Review error: {review['error']}")
        met = 0
        for r in rows:
            if r["outcome"] == "met" and r["override_outcome"] is None:
                met += 1
                continue
            mark = {"missed": "✗ missed", "unsure": "? unsure", "na": "– na", "met": "✓ met"}[r["outcome"]]
            line = f"  {mark} [{r['check_id']}] {by_id[r['check_id']].summary}: {r['evidence']}"
            if r["override_outcome"] is not None:
                line += f" (overridden → {r['override_outcome']}: {r['override_note'] or 'no note'})"
            print(line)
        print(f"  {met} checks met")
    print(f"\nSummary: {verdicts['good']} good, {verdicts['needs_review']} needs review, {verdicts['problem']} problem, "
          f"{verdicts['review_failed']} review failed. Not counted (left out of these counts): {not_counted}.")
    return 0


def cmd_show(path: Path, args: argparse.Namespace) -> int:
    # The summary uses ticks and arrows; a redirected Windows console may not encode them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    conn = _open_read_only(path)
    try:
        if args.rubric:
            return _show_rubric(conn)
        return _show_run(conn, args.run)
    finally:
        if conn is not None:
            conn.close()


# -- serve: the local app -------------------------------------------------------------------------------

DEMO_DB_PATH = Path("data/demo.db")
DEMO_LOG_DIR = Path("data/demo-logs")
DEMO_UNAVAILABLE = "Not available in the demo: it uses invented data and never contacts Zendesk or Anthropic."
# The demo of the hosted edition (`serve --demo --edition hosted`): its admin, who is also who the demo
# signs in as unless `--demo-user` names someone else, and the zone it judges days in.
DEMO_ADMIN = "team.lead@example.com"
DEMO_TIMEZONE = "America/Chicago"


APP_LOG = "app.log"
# Windows takes about 2 seconds to refuse a connection to a closed local port, so a shorter timeout
# would mistake "nothing there" for "something there that doesn't answer".
PROBE_TIMEOUT_SECONDS = 5
STOP_WAIT_CHECKS = 20       # how many times `stop` checks that the app has let go of its port
STOP_WAIT_SECONDS = 0.5     # the pause between those checks


def serve_config(port: int | None = None) -> dict[str, Any]:
    """Where the app listens: this PC only, on port 8765 unless another port is asked for."""
    return {"host": "127.0.0.1", "port": 8765 if port is None else port}


def app_url(config: dict[str, Any]) -> str:
    return f"http://{config['host']}:{config['port']}/"


def _http(method: str, url: str, timeout: float) -> httpx.Response:
    """One request to the app on this PC. Never through a proxy: the app is only on 127.0.0.1."""
    return httpx.request(method, url, timeout=timeout, trust_env=False)


def port_free(host: str, port: int) -> bool:
    """Whether nothing is listening on the port: binding it succeeds at once, with no 2-second wait
    for Windows to refuse a connection. The socket is closed again straight away."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _run_server(server: Any) -> None:
    server.run()


# Tests replace these, so no test binds the real port, starts a server or opens a browser.
_port_free: Callable[[str, int], bool] = port_free
_open_browser: Callable[[str], Any] = webbrowser.open
_sleep: Callable[[float], None] = time.sleep


def probe_app(config: dict[str, Any], health_check: bool = False) -> str:
    """What is at the app's address: "free" (nothing is listening), "app" (this app, already
    running), "demo" (this app's demo, already running), "hosted demo" (the demo of the hosted edition,
    which has no quit address), or "other" (another program has the port, or something there doesn't answer).

    With `health_check` (a copy started from a server config, whose other addresses need a sign-in) it reads
    `/healthz` instead, which doesn't say whether it is the demo: "app" means this app."""
    if _port_free(config["host"], config["port"]):
        return "free"
    url = app_url(config)
    try:
        response = _http("GET", url + ("healthz" if health_check else "api/settings"), PROBE_TIMEOUT_SECONDS)
    except httpx.ConnectError:
        return "free"
    except httpx.HTTPError:
        return "other"
    try:
        body = response.json()
    except ValueError:
        return "other"
    if health_check:
        return "app" if response.status_code == 200 and body == {"ok": True} else "other"
    if response.status_code == 200 and isinstance(body, dict) and {"retention_days", "credentials"} <= body.keys():
        if body.get("demo") is True:
            return "hosted demo" if body.get("edition") == "hosted" else "demo"
        return "app"
    return "other"


def _demo_unavailable(*_args: Any) -> Any:
    from work_assistant.web.app import Unavailable

    raise Unavailable(DEMO_UNAVAILABLE)


def cmd_serve(path: Path, demo_mode: bool, open_browser: bool, port: int | None = None,
              demo_date: date | None = None, server: tuple[Path, server_config.ServerConfig] | None = None,
              demo_user: str | None = None) -> int:
    """Start the app, or, when it is already running (the Start menu shortcut clicked again), just open it.

    `demo_date` builds the demo as of that day and pins the app's clock to it (noon on that day, this PC's time
    zone), so the demo looks the same whatever day it is opened: Eyes baselines rely on it. `server` is the server
    config file and what it says (`serve --config`): the app then listens, keeps its data and runs the edition the
    config names. `demo_user`, with the demo, runs the demo as the hosted edition signed in as that email
    (`serve --demo --edition hosted`): a config built here, on this PC's local addresses only, with no
    Cloudflare Access values and no token checked."""
    import threading

    import uvicorn

    from work_assistant.web.app import create_app, subprocess_run_starter, zendesk_factory_for

    config = serve_config(port) if server is None else {"host": server[1].bind_host, "port": server[1].port}
    url = app_url(config)
    state = probe_app(config, health_check=server is not None)
    if state == "app" and demo_mode:
        # Opening the real app for someone who asked for the demo could get real data changed by mistake.
        print(f"Work Assistant is already running at {url} with your real data, so the demo can't start. "
              "To use the demo, stop the app first with: work-assistant stop")
        return 1
    if state in ("app", "demo", "hosted demo"):
        which = "Work Assistant" if state == "app" else "The Work Assistant demo"
        print(f"{which} is already running at {url}" + ("; opening it in the browser." if open_browser else "."))
        if state == "demo" and not demo_mode:
            print("To use your real data, stop the demo with: work-assistant stop, then open the app again.")
        elif state == "hosted demo" and not demo_mode:   # that edition has no quit address for `work-assistant stop`
            print("To use your real data, stop the demo by pressing Ctrl+C in the window it runs in, then open the "
                  "app again.")
        if open_browser:
            _open_browser(url)
        return 0
    if state == "other":
        print(f"Port {config['port']} is in use by another program, so Work Assistant can't start. "
              "Close that program, or restart the PC, then try again.")
        return 1

    servers: list[Any] = []

    def quit_app() -> None:
        servers[0].should_exit = True  # uvicorn finishes the requests in hand, then stops

    if demo_mode:
        from work_assistant import demo

        pinned = demo.as_of(demo_date) if demo_date is not None else None
        print(f"Building the demo database at {DEMO_DB_PATH} (invented data only"
              + (f", as of {demo_date.isoformat()}" if demo_date is not None else "") + ")...")
        demo.build_demo_db(DEMO_DB_PATH, log_dir=DEMO_LOG_DIR, now=pinned)
        hosted_demo = None
        if demo_user is not None:
            demo.seed_team_lead(DEMO_DB_PATH, DEMO_ADMIN, now=pinned)   # the admin, team leads and a manager
            hosted_demo = server_config.ServerConfig(edition="hosted", port=config["port"],
                                                     db_path=DEMO_DB_PATH, log_dir=DEMO_LOG_DIR,
                                                     admin_email=DEMO_ADMIN, timezone=DEMO_TIMEZONE)
            print(f"The demo runs the hosted edition, signed in as {demo_user}.")
        app = create_app(DEMO_DB_PATH, run_starter=_demo_unavailable, zendesk_factory=_demo_unavailable,
                         anthropic_factory=lambda: None, log_dir=DEMO_LOG_DIR, demo=True, port=config["port"],
                         on_quit=quit_app, now=pinned, config=hosted_demo, demo_identity=demo_user)
    elif server is not None:
        config_path, chosen = server
        app = create_app(chosen.db_path, log_dir=chosen.log_dir, config=chosen, config_path=config_path,
                         on_quit=quit_app,
                         run_starter=subprocess_run_starter(chosen.db_path, chosen.log_dir, config_path),
                         zendesk_factory=zendesk_factory_for(chosen.db_path, config_path))
    else:
        app = create_app(path, port=config["port"], on_quit=quit_app)
    servers.append(uvicorn.Server(uvicorn.Config(app, host=config["host"], port=config["port"],
                                                 log_level="warning")))
    edition = server[1].edition if server is not None else ("hosted" if demo_user is not None else None)
    if edition is not None:   # the hosted edition has no quit address, so `work-assistant stop` can't stop it
        print(f"Work Assistant ({edition} edition) is running at {url} (to stop it, press Ctrl+C here)")
    else:
        print(f"Work Assistant is running at {url} (to stop it, press Ctrl+C here, or run: work-assistant stop)")
    if open_browser:
        threading.Timer(1.5, _open_browser, args=(url,)).start()
    _run_server(servers[0])
    print("Work Assistant stopped.")
    return 0


def cmd_stop() -> int:
    """Ask the running app to shut down, then wait until it has let go of its port."""
    config = serve_config()
    url = app_url(config)
    if _port_free(config["host"], config["port"]):
        print("Work Assistant is not running.")
        return 0
    try:
        response = _http("POST", url + "api/quit", PROBE_TIMEOUT_SECONDS)
    except httpx.ConnectError:
        print("Work Assistant is not running.")
        return 0
    except httpx.HTTPError:
        response = None
    if response is None or response.status_code != 200:
        state = probe_app(config) if response is not None else None
        if state == "hosted demo":
            print("The hosted demo can't be stopped this way. Press Ctrl+C in the window it runs in.")
        elif state in ("app", "demo"):
            print("This copy of Work Assistant was started before it could be stopped this way. Close the window it "
                  "runs in, or restart the PC.")
        else:
            print(f"The program on port {config['port']} did not accept the request to stop. If it is Work Assistant, "
                  "restart the PC to stop it.")
        return 1
    for _ in range(STOP_WAIT_CHECKS):
        if _port_free(config["host"], config["port"]):
            print("Work Assistant has stopped.")
            return 0
        _sleep(STOP_WAIT_SECONDS)
    print("Work Assistant was asked to stop but is still running. Wait a minute and run work-assistant stop again.")
    return 1


# -- shortcut: Start menu and desktop entries ---------------------------------------------------------


def cmd_shortcut() -> int:
    try:
        paths = desktop.create()
    except desktop.ShortcutError as exc:
        print(f"Could not make the Work Assistant shortcuts: {exc}")
        return 1
    print("Made the Work Assistant shortcuts (Start menu and desktop):")
    for path in paths:
        print(f"  {path}")
    print('Press the Windows key and type "Work Assistant" to open the app. If it is already running, it just '
          "opens it in your browser. To keep it on the taskbar, right-click it there and choose Pin to taskbar.")
    return 0


# -- the daily task's log and the app's log ----------------------------------------------------------------

SCHEDULED_LOG = "scheduled.log"


class _Tee:
    """A text stream that appends to the log file and, when there is one, the console stream too."""

    encoding = "utf-8"

    def __init__(self, log: TextIO, console: TextIO | None) -> None:
        self._log = log
        self._console = console

    def write(self, text: str) -> int:
        self._log.write(text)
        self._log.flush()
        if self._console is not None:
            try:
                self._console.write(text)
            except (OSError, ValueError):  # a console that is gone, or can't encode the text
                pass
        return len(text)

    def flush(self) -> None:
        self._log.flush()
        if self._console is not None:
            try:
                self._console.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False


@contextmanager
def output_log(path: Path, started: datetime, title: str, what: str) -> Iterator[None]:
    """Append everything printed while the body works to `path`, under a `title` header with a timestamp.

    Under pythonw.exe (how the daily task and the Start menu shortcut run, so no window opens) there is
    no console and sys.stdout is None; the log is then the only record. The printed lines carry
    summaries and error messages only, never secrets or customer content. An unexpected error is
    logged as "The `what` stopped..." with its type and where it happened, without its message,
    which could quote anything.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as log:
        stamp = started.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.write(f"=== {stamp}: {title} ===\n")
        log.flush()
        console_out, console_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(log, console_out), _Tee(log, console_err)
        try:
            yield
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)  # Python would print it to the console only
                raise SystemExit(1) from None
            raise
        except BaseException as exc:
            log.write(f"The {what} stopped with an unexpected error: {type(exc).__name__}\n")
            log.writelines(traceback.format_list(traceback.extract_tb(exc.__traceback__)))
            raise
        finally:
            sys.stdout, sys.stderr = console_out, console_err


CONFIG_HELP = ("The server config file (the hosted copy): its database, log folder, secrets file, time zone and "
               "edition are used.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="work-assistant")
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)  # the app passes its own database file
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Save the Zendesk and Anthropic credentials.")

    run = commands.add_parser("run", help="Fetch a period's calls and review them.")
    period = run.add_mutually_exclusive_group()  # needed unless --scheduled; checked in main
    period.add_argument("--last-week", action="store_true", help="The previous Sunday-Saturday week.")
    period.add_argument("--week-of", type=_date, metavar="YYYY-MM-DD",
                        help="The Sunday-Saturday week containing this day.")
    period.add_argument("--from", dest="from_date", type=_date, metavar="YYYY-MM-DD", help="First day (with --to).")
    run.add_argument("--to", dest="to_date", type=_date, metavar="YYYY-MM-DD", help="Last day, included.")
    run.add_argument("--rereview", action="store_true",
                     help="Review the period's stored calls again under the current rubric.")
    run.add_argument("--tickets", metavar="N,N,...",
                     help="With --rereview and no period: review these calls again (ticket numbers, comma "
                          "separated), as the app's Review again does.")
    run.add_argument("--tickets-file", type=Path, metavar="PATH",
                     help="Like --tickets, with the ticket numbers in a file (one per line). The file is deleted "
                          "once read: the app passes long lists this way.")
    run.add_argument("--yes", action="store_true", help="Go ahead even if the estimate is over the budget limit.")
    run.add_argument("--limit", type=_positive_int, metavar="N",
                     help="Review only the first N calls (by ticket number), for calibration.")
    run.add_argument("--scheduled", action="store_true",
                     help="Used by the daily task: collect interrupted runs first, then review each complete day "
                          "since the last scheduled run, oldest first. Takes no period (--last-week is ignored).")

    test = commands.add_parser(
        "test-model", help="Score a range's already-reviewed calls with another model, without changing what "
                           "the app shows.")
    test.add_argument("--from", dest="from_date", type=_date, required=True, metavar="YYYY-MM-DD", help="First day.")
    test.add_argument("--to", dest="to_date", type=_date, required=True, metavar="YYYY-MM-DD",
                      help="Last day, included.")
    test.add_argument("--model", required=True, metavar="ID", help=f"One of: {', '.join(MODELS)}.")
    test.add_argument("--effort", required=True, metavar="LEVEL", help=f"One of: {', '.join(settings.EFFORTS)}.")
    test.add_argument("--limit", type=_positive_int, metavar="N", help="Test only the newest N calls.")
    test.add_argument("--yes", action="store_true", help="Go ahead even if the estimate is over the budget limit.")

    activity = commands.add_parser("activity", help="Team activity, read from Zendesk into the local database.")
    activity_commands = activity.add_subparsers(dest="activity_command", required=True)
    activity_sync_parser = activity_commands.add_parser(
        "sync", help="Read the team's recent Zendesk activity (the first run reads back to the start of the "
                     "retention period; --kind backfill also reads older activity once the retention period grows). "
                     "Read-only; needs no Anthropic key.")
    activity_sync_parser.add_argument(
        "--kind", choices=("daily", "refresh", "backfill"), default="refresh",
        help="What started the sync, recorded with it (default: refresh). Each reads on from where the last "
             "sync stopped; backfill first reads any older activity in the retention period that is not stored "
             "yet, which can take over an hour.")
    activity_sync_parser.add_argument("--config", type=Path, metavar="PATH", help=CONFIG_HELP)
    activity_sync_parser.add_argument(
        "--from", dest="reach_back", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="With --kind backfill: also read the older activity back to this day, when it is before the retention "
             "period (retention itself is unchanged). Once, so whole-month periods have every day they need.")

    count_collected = activity_commands.add_parser(
        "count-collected", help="Count the department's call legs, calls, ratings and changed tickets in Zendesk over "
                                "the last N days, read-only and stored nowhere: run once before the first backfill "
                                "that reads them back. Needs no Anthropic key.")
    count_collected.add_argument("--days", type=_positive_int, default=30, metavar="N",
                                 help="How many days back to count (default: 30).")
    count_collected.add_argument("--config", type=Path, metavar="PATH", help=CONFIG_HELP)

    fill_techs = activity_commands.add_parser(
        "fill-techs", help="Once, for techs added after the stored activity began (their shift starts later than "
                           "the history): list them, then on a yes read their activity and sign-ins back to the start "
                           "of everyone's and start their shifts there. Read-only on Zendesk; can take hours, and "
                           "running it again after a stop carries on.")
    fill_techs.add_argument("--config", type=Path, metavar="PATH", help=CONFIG_HELP)
    fill_techs.add_argument("--yes", action="store_true", help="Fill without asking first.")
    fill_techs.add_argument("--skip", action="append", default=[], metavar="EMAIL",
                            help="Leave this tech out (a new employee has no earlier history). Repeat for more.")

    export = commands.add_parser(
        "export-activity", help="Write this PC's Team activity history (activity, shifts, time off, the roster and "
                                "its settings; nothing from Call QA) to a new file, for import-activity.")
    export.add_argument("--out", type=Path, required=True, metavar="FILE", help="The new file to write.")
    imported = commands.add_parser(
        "import-activity", help="Replace the hosted copy's Team activity with a file from export-activity, after "
                                "saving a copy of its database, and assign every tech to the config's admin.")
    imported.add_argument("file", type=Path, metavar="FILE", help="The file export-activity wrote.")
    imported.add_argument("--config", type=Path, metavar="PATH", required=True, help=CONFIG_HELP)
    backup = commands.add_parser(
        "backup-db", help="Copy the server config's database into backups/ beside it, named by today's date in the "
                          "config's time zone (a second copy the same day replaces the first), then delete the "
                          "copies older than --keep-days. The hosted copy's nightly timer runs it, and update.ps1 "
                          "runs it with --before-update.")
    backup.add_argument("--config", type=Path, metavar="PATH", required=True, help=CONFIG_HELP)
    backup.add_argument("--keep-days", type=_positive_int, default=backups.KEEP_DAYS, metavar="N",
                        help=f"Delete the copies more than N days old (default: {backups.KEEP_DAYS}).")
    backup.add_argument("--before-update", action="store_true",
                        help="Make the copy kept for a rollback instead: backups/before-update-<UTC time>.db, which "
                             "rollback.sh --restore-db puts back. It replaces the last update's and leaves the daily "
                             "copies as they are; the nightly backup deletes it once it is older than --keep-days. "
                             "update.ps1 runs it before it installs a new version.")

    commands.add_parser(
        "purge", help="Erase the customer content of calls older than the retention period now.")
    commands.add_parser("resume", help="Collect the results of runs interrupted while their batch was in flight.")
    confirm = commands.add_parser("confirm", help="Go ahead with a run paused by the budget limit.")
    confirm.add_argument("run_id", type=int)
    clear = commands.add_parser(
        "clear-run", help="Release the calls of a run whose submission outcome is unknown, after checking the Console.")
    clear.add_argument("run_id", type=int)
    clear.add_argument("--yes", action="store_true", help="Skip the confirmation question.")
    cancel = commands.add_parser(
        "cancel-run", help="Cancel a run paused by the budget limit instead of confirming it. Nothing is sent.")
    cancel.add_argument("run_id", type=int)

    serve = commands.add_parser("serve", help="Open the reporting app in your browser (this PC only).")
    serve.add_argument("--demo", action="store_true",
                       help="Use a database of invented calls (data/demo.db) instead of the real one.")
    serve.add_argument("--no-browser", action="store_true", help="Start the app without opening a browser.")
    serve.add_argument("--port", type=int, default=None,
                       help="Listen on this port instead of 8765, for example to run the demo beside the real app.")
    serve.add_argument("--demo-date", type=_date, default=None, metavar="YYYY-MM-DD",
                       help="With --demo: build the demo as of this day and keep the app's clock on it, so it looks "
                            "the same on any day (for screenshot baselines).")
    serve.add_argument("--config", type=Path, metavar="PATH",
                       help=CONFIG_HELP + " Not with --demo or --port: the config sets the port.")
    serve.add_argument("--edition", choices=server_config.EDITIONS, default=None,
                       help="With --demo: run the demo as this edition (default: full). hosted is the copy on the "
                            "server, signed in as --demo-user.")
    serve.add_argument("--demo-user", type=_email, default=None, metavar="EMAIL",
                       help=f"With --demo --edition hosted: who the demo is signed in as (default: "
                            f"{DEMO_ADMIN}, its admin). No token is checked.")
    secret = commands.add_parser(
        "set-secret", help="Save a secret in the server config's secrets file, typed at a hidden prompt (the "
                           "hosted copy; on this PC, work-assistant setup saves them).")
    secret.add_argument("name", choices=tuple(SERVER_SECRET_PROMPTS))
    secret.add_argument("--config", type=Path, metavar="PATH", required=True, help=CONFIG_HELP)
    commands.add_parser("stop", help="Stop the running app (for example one opened from the Start menu).")
    commands.add_parser("shortcut", help='Add "Work Assistant" shortcuts (Start menu and desktop) that open the app.')

    schedule = commands.add_parser("schedule", help="Install, remove or check the daily Windows scheduled task.")
    schedule.add_argument("action", choices=("install", "remove", "status"))

    show = commands.add_parser("show", help="Print the rubric or a run's results in the terminal (read-only).")
    what = show.add_mutually_exclusive_group(required=True)
    what.add_argument("--rubric", action="store_true", help="The current rubric: checks, rules and reference documents.")
    what.add_argument("--run", type=int, metavar="RUN_ID", help="Per-call results of one run.")
    return parser


TICKETS_NOT_NUMBERS = "--tickets takes ticket numbers separated by commas, such as 484753,485001."


def _parse_ticket_ids(text: str) -> list[int]:
    parts = [p for p in text.replace(",", " ").split() if p]
    if not parts or not all(p.isdigit() for p in parts):
        sys.exit(TICKETS_NOT_NUMBERS)
    return [int(p) for p in parts]


def _ticket_ids(args: argparse.Namespace) -> list[int] | None:
    """The tickets `run --rereview --tickets` (or `--tickets-file`) names, or None for a period run.

    A tickets file is deleted once read, whatever it holds: the app writes one for each long list it starts.
    """
    if args.tickets is None and args.tickets_file is None:
        return None
    if args.tickets is not None and args.tickets_file is not None:
        sys.exit("Use --tickets or --tickets-file, not both.")
    if not args.rereview:
        sys.exit("--tickets needs --rereview: it reviews those calls again.")
    if args.scheduled or args.last_week or args.week_of is not None or args.from_date is not None \
            or args.to_date is not None:
        sys.exit("--tickets takes no period: the calls are named by ticket number.")
    if args.limit is not None:
        sys.exit("--tickets takes no --limit: every named call is reviewed again.")
    if args.tickets is not None:
        return _parse_ticket_ids(args.tickets)
    try:
        text = args.tickets_file.read_text(encoding="utf-8")
    except OSError:
        sys.exit(f"Couldn't read the tickets file {args.tickets_file}.")
    finally:
        args.tickets_file.unlink(missing_ok=True)
    return _parse_ticket_ids(text)


def main(argv: list[str] | None = None, db_path: Path | None = None, services: Services | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        args.ticket_ids = _ticket_ids(args)
    if (args.command == "run" and not args.scheduled and args.ticket_ids is None
            and not (args.last_week or args.week_of is not None or args.from_date is not None)):
        parser.error("run: choose a period: --last-week, --week-of, or --from with --to (or --scheduled)")
    if args.command == "serve" and args.demo_date is not None and not args.demo:
        parser.error("serve: --demo-date only goes with --demo (it dates the invented data, never your real data)")
    if args.command == "serve" and not args.demo and (args.edition is not None or args.demo_user is not None):
        parser.error("serve: --edition and --demo-user only go with --demo (a server config sets the edition, and "
                     "Cloudflare Access says who is signed in)")
    if args.command == "serve" and args.demo_user is not None and args.edition != "hosted":
        parser.error("serve: --demo-user only goes with --edition hosted (the full edition has no sign-in)")
    config_path = getattr(args, "config", None)
    if config_path is not None:
        if args.db is not None:
            parser.error("--db and --config can't be combined: the config names the database")
        if args.command == "serve" and (args.demo or args.port is not None):
            parser.error("serve: --config doesn't go with --demo or --port (the config sets the port)")
    config = _load_config(config_path) if config_path is not None else None
    if config is not None:
        server_config.apply(config)   # its secrets file and time zone, for the whole command
    if db_path is not None:
        path = Path(db_path)
    elif config is not None:
        path = config.db_path
    else:
        path = args.db if args.db is not None else db.DB_PATH
    config_logs = config.log_dir if config is not None else LOG_DIR
    if args.command == "serve":
        log_dir = services.log_dir if services is not None else config_logs
        with output_log(Path(log_dir) / APP_LOG, _utcnow(), "app started", "app"):
            return cmd_serve(path, args.demo, not args.no_browser, args.port, args.demo_date,
                             (config_path, config) if config is not None else None,
                             (args.demo_user or DEMO_ADMIN) if args.edition == "hosted" else None)
    if args.command == "set-secret":
        return cmd_set_secret(args.name)
    if args.command == "export-activity":
        return cmd_export_activity(path, args.out)
    if args.command == "import-activity":
        return cmd_import_activity(config, args.file)
    if args.command == "backup-db":
        return cmd_backup_db(config, args.keep_days, (services.now if services is not None else _utcnow)(),
                             args.before_update)
    if args.command == "stop":
        return cmd_stop()
    if args.command == "shortcut":
        return cmd_shortcut()
    if args.command == "setup":
        return cmd_setup(path)
    if args.command == "show":
        return cmd_show(path, args)
    if args.command == "schedule":
        return cmd_schedule(args.action)
    if args.command == "run" and args.to_date is not None and args.from_date is None:
        sys.exit("--to needs --from.")
    if getattr(args, "reach_back", None) is not None and args.kind != "backfill":
        sys.exit("--from needs --kind backfill.")
    if args.command == "test-model":
        _check_test_choices(args)

    if args.command == "run" and args.scheduled:
        log_dir = services.log_dir if services is not None else LOG_DIR
        clock = services.now if services is not None else _utcnow
        with output_log(Path(log_dir) / SCHEDULED_LOG, clock(), "daily scheduled run", "scheduled run"):
            return _main_db(args, path, services)
    return _main_db(args, path, services, config_logs)


def _main_db(args: argparse.Namespace, path: Path, services: Services | None, log_dir: Path = LOG_DIR) -> int:
    """The commands that work on the database. `log_dir` is the logs folder of `activity sync` (the server
    config's with `--config`)."""
    conn = db.connect(path)
    try:
        db.init_db(conn)
        rubric.ensure_seeded(conn)
        if args.command == "clear-run":
            return cmd_clear_run(conn, args.run_id, args.yes, services.log_dir if services else LOG_DIR)
        if args.command == "cancel-run":
            return cmd_cancel_run(conn, args.run_id, services.log_dir if services else LOG_DIR)
        if args.command == "purge":
            return cmd_purge(conn, services.log_dir if services else LOG_DIR)
        if args.command == "activity":  # Zendesk only: no Anthropic key needed
            if args.activity_command == "fill-techs":
                return cmd_activity_fill_techs(conn, args, services, log_dir)
            if args.activity_command == "count-collected":
                return cmd_activity_count_collected(conn, args, services)
            return cmd_activity_sync(conn, args, services, log_dir)
        if services is None:
            # A model test reads calls from the database, but downloads their images from Zendesk.
            try:
                services = build_services(conn, need_zendesk=args.command in ("run", "confirm", "test-model"))
            except (Exception, SystemExit):
                if args.command == "run" and args.scheduled:
                    _daily_activity(conn, None)  # no Anthropic key must not stop the daily sync
                raise
        try:
            if args.command == "run":
                return cmd_run(conn, args, services, resume_lock_path(path))
            if args.command == "test-model":
                return cmd_test_model(conn, args, services)
            if args.command == "resume":
                return cmd_resume(conn, services, resume_lock_path(path))
            if args.command == "confirm":
                return cmd_confirm(conn, args.run_id, services)
        finally:
            services.close()
    finally:
        conn.close()
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
