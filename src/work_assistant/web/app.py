"""The app server: page shell, Settings API, runs, the status banner, the report screens' data, the
rubric editor, and startup work.

Rules this module keeps:
- One SQLite connection per request or thread, opened and closed here (never shared).
- The review pipeline never runs in this process. "Run now" and "Confirm" go through the injected
  `run_starter`, which in production starts the `work-assistant` command as a separate process.
- Responses never carry secret values; credential status is set / not set only.
"""
from __future__ import annotations

import base64
import hashlib
import calendar
import io
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from work_assistant import (access, backups, costs, credentials, db, names, people, periods, pipeline, regions,
                            rubric, scheduler, server_config, settings, shifts_import, sync_schedule, timeutil)
from work_assistant.activity import report as activity_report
from work_assistant.activity import store as activity_store
from work_assistant.activity import trends as activity_trends
from work_assistant.activity.model import TIME_OFF_KINDS, ShiftVersion
from work_assistant.oneonone import report as prep_report
from work_assistant.procutil import pid_alive, resume_lock, resume_lock_path
from work_assistant.retention import purge_expired
from work_assistant.reviewer.catalog import DEFAULT_MODEL, MODELS
from work_assistant.reviewer.schema import OUTCOMES
from work_assistant.server_config import ServerConfig
from work_assistant.web import compare, export, queries, rubric_edit
from work_assistant.runlog import LOG_DIR, log_path

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8765
SAFE_METHODS = ("GET", "HEAD")
RUNS_SHOWN = 50
LOG_TAIL_LINES = 50
AGENT_ROLES = ("agent", "admin")
MAX_WEEKS = 52             # the longest period the Agents screen covers
NOTE_MAX_CHARS = 10000     # a coaching note's longest body (and an override note's)
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCHEDULE_PLACEHOLDER = {"installed": False, "text": "Not installed yet"}
# The Call QA routes: each path and everything under it. The hosted edition answers all of them with 404
# NOT_FOUND, as if they didn't exist. That includes quitting: on the server systemd owns the process.
CALL_QA_PATHS = ("/api/runs", "/api/review-again", "/api/tests", "/api/overview", "/api/agents", "/api/notes",
                 "/api/tickets", "/api/export.xlsx", "/api/reviews", "/api/rubric", "/api/agreement", "/api/schedule",
                 "/api/status", "/api/quit")
# The hosted copy's own routes (who can use it, which techs each team lead sees, and the Teams Shifts import, which
# writes regions: the hosted copy's). The PC copy has no sign-in and no people, so it answers them with 404 NOT_FOUND
# and its people tables stay empty.
HOSTED_PATHS = ("/api/people", "/api/shifts-import")
# Hosted-only too, but only what is under each: taking a tech off the roster by address (`DELETE /api/team/{email}`,
# the Techs panel's Remove), and a manager picking their regions (`PUT /api/me/regions`: the PC has no managers).
# `POST /api/team` and `GET /api/me` themselves are both editions'; the PC's Team panel removes a tech by saving its
# whole team through `PUT /api/settings`.
HOSTED_UNDER = ("/api/team", "/api/me")
NOT_FOUND = "Not found."
ADMIN_ONLY = "Only the admin can do this."
# What a manager may not do (Managers design): their role, checked by the server whichever tech is named.
LEADS_SET_THESE = "Shifts, time off and 1:1 dates: each team lead sets these for their own techs."
MANAGERS_ONLY = "Only a manager picks the regions they see."
ADD_LEADS_ONLY = "A manager can add team leads only."
REMOVE_LEADS_ONLY = "A manager can remove team leads only."
LEADS_OUTSIDE = "This team lead has techs outside your regions. Ask the admin to remove them."
SCHEDULE_FROM_PANEL = "Save 1:1 schedules from the 1:1 schedule panel."
TEAM_FROM_PANEL = "Add and remove techs in the Techs panel."
SCHEDULE_BODY = "Send the 1:1 date (a day, or null to clear it) and the repeat."
SHIFTS_FILES = "Choose the Teams Shifts export files."
ZENDESK_NOT_SET_UP = "Zendesk is not set up yet. Run `work-assistant setup` in a terminal, then try again."
ANTHROPIC_NOT_SET_UP = "No Anthropic API key is saved. Run `work-assistant setup` in a terminal, then try again."
SIGN_IN = "Sign in through the company link."
SIGN_IN_UNREACHABLE = "The sign-in check couldn't reach Cloudflare. Try again in a few minutes."
SIGN_IN_HEADER = "cf-access-jwt-assertion"   # the token Cloudflare Access puts on every request it lets through
SIGN_IN_COOKIE = "CF_Authorization"          # the same token, as the browser keeps it


def zendesk_not_set_up(config_path: Path | None = None) -> str:
    """What to do when Zendesk isn't set up. A copy started from a server config keeps its token where the config
    says, which `work-assistant set-secret` writes; `work-assistant setup` would ask for an Anthropic key and use the keyring."""
    if config_path is None:
        return ZENDESK_NOT_SET_UP
    return ("Zendesk is not set up yet. Save the Zendesk subdomain and login email in Settings, and the token with "
            f"`work-assistant set-secret zendesk_token --config {config_path}` in a terminal, then try again.")

logger = logging.getLogger("work_assistant.web")

RunStarter = Callable[[dict], int]
ZendeskFactory = Callable[[], Any]
# Returns (anthropic client, secrets to scrub from run logs), or None when no key is saved.
AnthropicFactory = Callable[[], "tuple[Any, tuple[str | None, ...]] | None"]


_STATIC_LINK = re.compile(r'((?:href|src)="/static/)([\w.-]+\.(?:js|css))"')


def versioned_page(static_dir: Path) -> str:
    """index.html with each `/static/` script and stylesheet address carrying `?v=` and the first 12 hex of its file's
    SHA-256, so the address changes exactly when the file does: after an update, a browser or Cloudflare that kept an
    old app.js fetches the new one instead of running it against the new API (Cloudflare's browser cache setting can
    override the files' own `no-cache`)."""
    page = (static_dir / "index.html").read_text(encoding="utf-8")

    def versioned(match: re.Match) -> str:
        name = match.group(2)
        digest = hashlib.sha256((static_dir / name).read_bytes()).hexdigest()[:12]
        return f'{match.group(1)}{name}?v={digest}"'

    return _STATIC_LINK.sub(versioned, page)


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must revalidate before each use (`Cache-Control: no-cache`), so after
    an update the owner never runs a stale app.js against the new API. Unchanged files still come back
    as a cheap 304 thanks to their ETag."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class Unavailable(Exception):
    """A service a request needs (Zendesk, the run launcher) is not available. The message is shown
    to the owner as is, so it must say what to do and never carry secrets or customer content."""


@contextmanager
def _connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _model_choices(conn: sqlite3.Connection, effort: str) -> list[dict[str, Any]]:
    """The Settings Model choice: each model with its rough cost per call at the selected effort."""
    choices = []
    for model in MODELS.values():
        cost, measured = costs.approx_cost_per_call(conn, model.id, effort)
        choices.append({"id": model.id, "label": model.label, "approx_cost_per_call": cost, "measured": measured})
    return choices


# -- production collaborators -----------------------------------------------------------------------


def zendesk_factory_for(db_path: Path, config_path: Path | None = None) -> ZendeskFactory:
    """A factory for the real read-only Zendesk client, from the saved settings and the keyring (or the secrets file
    of the server config at `config_path`, which its not-set-up message names)."""

    def factory() -> Any:
        from work_assistant.zendesk.client import ZendeskClient

        with _connection(db_path) as conn:
            subdomain = settings.get_setting(conn, "zendesk_subdomain")
            email = settings.get_setting(conn, "zendesk_email")
        token = credentials.get_secret("zendesk_token")
        if not (subdomain and email and token):
            raise Unavailable(zendesk_not_set_up(config_path))
        return ZendeskClient(subdomain, email, token)

    return factory


def default_anthropic_factory() -> tuple[Any, tuple[str | None, ...]] | None:
    key = credentials.get_secret("anthropic_api_key")
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key), (key, credentials.get_secret("zendesk_token"))


def command_args(spec: dict) -> list[str]:
    """The `work-assistant` arguments for a run request.

    No `--yes`: the run re-estimates from freshly fetched data, and if that comes out above the budget
    limit the pipeline's guard pauses it for the owner to confirm in the banner.
    """
    if spec["command"] == "run":
        args = ["run", "--from", spec["from"], "--to", spec["to"]]
        if spec["rereview"]:
            args.append("--rereview")
        return args
    if spec["command"] == "rereview-tickets":
        # The production starter puts the ids in a file (`subprocess_run_starter`), since a long list would
        # pass Windows' command-line limit. Without one, they go on the line.
        if "tickets_file" in spec:
            return ["run", "--rereview", "--tickets-file", str(spec["tickets_file"])]
        return ["run", "--rereview", "--tickets", ",".join(str(int(t)) for t in spec["ticket_ids"])]
    if spec["command"] == "confirm":
        return ["confirm", str(int(spec["run_id"]))]
    if spec["command"] == "activity-sync":
        return ["activity", "sync", "--kind", spec["kind"]]
    if spec["command"] == "test-model":
        args = ["test-model", "--from", spec["from"], "--to", spec["to"], "--model", spec["model"],
                "--effort", spec["effort"]]
        if spec["limit"] is not None:
            args += ["--limit", str(int(spec["limit"]))]
        return args
    raise ValueError(f"Unknown command: {spec['command']!r}")


def cli_args(spec: dict, db_path: Path, config_path: Path | None = None) -> list[str]:
    """The full command line, against the same database file as the app. A team activity sync of an app started from
    a server config gets that config instead (`--config`), so it uses the same database, log folder and secrets
    file."""
    if config_path is not None and spec["command"] == "activity-sync":
        return [sys.executable, "-m", "work_assistant.cli", *command_args(spec), "--config", str(config_path)]
    return [sys.executable, "-m", "work_assistant.cli", "--db", str(db_path), *command_args(spec)]


def subprocess_run_starter(db_path: Path, log_dir: Path = LOG_DIR, config_path: Path | None = None) -> RunStarter:
    """Starts `work-assistant run|confirm|test-model|activity sync` (Review again is a `run --rereview
    --tickets-file`) as a detached process and returns its process id. `config_path` is the server config the app
    was started from, if any (see `cli_args`).

    Checks the credentials first so a run that could only fail is never started. The command's own
    summary (never secrets or customer content) is appended to `logs/launcher.log`.
    """

    def start(spec: dict) -> int:
        with _connection(db_path) as conn:
            subdomain = settings.get_setting(conn, "zendesk_subdomain")
            email = settings.get_setting(conn, "zendesk_email")
        if not (subdomain and email and credentials.get_secret("zendesk_token")):
            raise Unavailable(zendesk_not_set_up(config_path))
        if spec["command"] != "activity-sync" and not credentials.get_secret("anthropic_api_key"):
            raise Unavailable(ANTHROPIC_NOT_SET_UP)  # a team activity sync only reads Zendesk
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            # A hidden console, not none: the venv's python.exe starts the real Python as a child, and a child
            # of a console-less (detached) process gets a fresh, visible console window.
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        tickets_file = None
        if spec["command"] == "rereview-tickets":
            # Review again's ticket ids, one per line, in the logs folder (git-ignored). The run deletes the
            # file once it has read it; a launch that fails deletes it here.
            tickets_file = Path(log_dir) / f"review-again-{uuid.uuid4().hex}.txt"
            tickets_file.write_text("".join(f"{int(t)}\n" for t in spec["ticket_ids"]), encoding="utf-8")
            spec = {**spec, "tickets_file": str(tickets_file)}
        try:
            with open(Path(log_dir) / "launcher.log", "a", encoding="utf-8") as out:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                out.write(f"{stamp} starting: work-assistant {' '.join(command_args(spec))}\n")
                out.flush()
                process = subprocess.Popen(cli_args(spec, db_path, config_path), stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=subprocess.STDOUT, env=env, **options)
        except BaseException:
            if tickets_file is not None:
                tickets_file.unlink(missing_ok=True)
            raise
        return process.pid

    return start


# -- startup work -------------------------------------------------------------------------------------


def startup_work(db_path: Path, *, log_dir: Path, anthropic_factory: AnthropicFactory,
                 now: datetime | None = None, alive: Callable[[int], bool] = pid_alive, call_qa: bool = True) -> None:
    """Purge expired content, fail runs whose fetching process died, and collect interrupted batches.

    Runs in a background thread with its own connection. Every step is logged and contained: a
    failure (including missing credentials) never stops the app or the other steps. Without Call QA (the Team
    activity edition) interrupted batches are never collected, so `anthropic_factory` is never called.
    """
    now = now or datetime.now(timezone.utc)
    try:
        conn = db.connect(db_path)
    except Exception:
        logger.exception("Startup work skipped: the database could not be opened")
        return
    try:
        db.init_db(conn)
        try:
            days = int(settings.get_setting(conn, "retention_days"))
            purged = purge_expired(conn, now, days, log_dir)
            if purged:
                logger.info("Erased the customer content of %d calls older than %d days", purged, days)
        except Exception:
            conn.rollback()
            logger.exception("Startup purge failed; it runs again after the next review run")
        try:
            failed = pipeline.fail_interrupted_fetching(conn, now, alive)
            if failed:
                logger.info("Marked interrupted runs as failed: %s", ", ".join(map(str, failed)))
        except Exception:
            conn.rollback()
            logger.exception("Could not check for interrupted runs")
        if call_qa:
            _resume(conn, db_path, log_dir, anthropic_factory)
    finally:
        conn.close()


_run_startup_work = startup_work  # create_app's `startup_work` flag shadows the name there


def _resume(conn: sqlite3.Connection, db_path: Path, log_dir: Path, anthropic_factory: AnthropicFactory) -> None:
    pending = [r for r in conn.execute("SELECT pid FROM runs WHERE status IN ('submitted', 'submitting')")
               if not pipeline.driven_elsewhere(r["pid"])]
    if not pending:
        return
    try:
        with resume_lock(resume_lock_path(db_path)) as acquired:
            if not acquired:
                logger.info("Another process is collecting interrupted runs; not collecting them here")
                return
            services = anthropic_factory()
            if services is None:
                logger.warning("Interrupted runs were not collected: no Anthropic API key is saved. "
                               "Run: work-assistant setup")
                return
            client, secrets = services
            run_ids = pipeline.resume_runs(conn, anthropic_client=client, log_dir=log_dir, secrets=secrets)
            if run_ids:
                logger.info("Collected interrupted runs: %s", ", ".join(map(str, run_ids)))
    except Exception:
        conn.rollback()
        logger.exception("Collecting interrupted runs failed; `work-assistant resume` retries it")


# -- request helpers ----------------------------------------------------------------------------------


def _error(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, **extra}, status_code=status)


class _BadRequest(Exception):
    """A request the app can't act on: 400 with the message, and any `extra` fields of the answer (`email`: the
    address for the admin to correct)."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


class _NotFound(Exception):
    """A tech (or a tech's entry) outside the team lead's scope: answered 404 NOT_FOUND, exactly as for one that
    doesn't exist."""


class _Forbidden(Exception):
    """Something the signed-in person's role doesn't allow: answered 403 with the message, by default ADMIN_ONLY (only
    the admin may do it, asked by a team lead)."""

    def __init__(self, message: str = ADMIN_ONLY) -> None:
        super().__init__(message)


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether `path` is one of `prefixes` or anything under one."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether `path` is under one of `prefixes`, not the prefix itself."""
    return any(path.startswith(prefix + "/") for prefix in prefixes)


def _run_request(body: Any) -> dict:
    first, last = _range_request(body)
    rereview = body.get("rereview", False)
    if not isinstance(rereview, bool):
        raise _BadRequest("Re-review must be true or false.")
    return {"command": "run", "from": first, "to": last, "rereview": rereview}


def _test_request(body: Any) -> dict:
    """A model test request: the range, a model from the catalog, an effort level, and an optional call limit
    (a whole number of at least 1, or null for every call)."""
    first, last = _range_request(body)
    model, effort, limit = body.get("model"), body.get("effort"), body.get("limit")
    if model not in MODELS:
        raise _BadRequest(f"Choose a model: {', '.join(m.label for m in MODELS.values())}.")
    if effort not in settings.EFFORTS:
        raise _BadRequest(f"Choose an effort: {', '.join(settings.EFFORTS)}.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise _BadRequest("The call limit must be a whole number of at least 1, or empty to test every call.")
    return {"command": "test-model", "from": first, "to": last, "model": model, "effort": effort, "limit": limit}


def _range_request(body: Any) -> tuple[str, str]:
    """The first and last day (YYYY-MM-DD, both included) of a run or test request."""
    if not isinstance(body, dict):
        raise _BadRequest("Send the first and last day of the range.")
    first, last = body.get("from"), body.get("to")
    if not isinstance(first, str) or not isinstance(last, str):
        raise _BadRequest("Choose the first and last day of the range.")
    try:
        first_day, last_day = date.fromisoformat(first), date.fromisoformat(last)
    except ValueError:
        raise _BadRequest("Dates must be in the form YYYY-MM-DD.") from None
    if len(first) != 10 or len(last) != 10:
        raise _BadRequest("Dates must be in the form YYYY-MM-DD.")
    if last_day < first_day:
        raise _BadRequest("The first day must be on or before the last day.")
    return first, last


def _iso_day(value: str | None, name: str) -> date | None:
    """An optional YYYY-MM-DD query parameter."""
    if value is None or value == "":
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise _BadRequest(f"The {name} must be a date in the form YYYY-MM-DD.") from None


def _weeks(value: str | None) -> int:
    """The optional `weeks` query parameter: a whole number of weeks from 1 to MAX_WEEKS."""
    if value is None or value == "":
        return queries.TREND_WEEKS
    if not value.isdigit() or not 1 <= int(value) <= MAX_WEEKS:
        raise _BadRequest(f"The weeks must be a whole number from 1 to {MAX_WEEKS}.")
    return int(value)


# -- Team activity requests -----------------------------------------------------------------------------


SYNC_KINDS = ("refresh", "backfill")   # what the owner can start; the daily task records "daily"
_CLOCK_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _strict_day(value: Any, name: str) -> date:
    """A required YYYY-MM-DD value from a request body."""
    if not isinstance(value, str) or len(value) != 10:
        raise _BadRequest(f"The {name} must be a date in the form YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _BadRequest(f"The {name} must be a date in the form YYYY-MM-DD.") from None


def _clock_minutes(value: Any, name: str) -> int:
    match = _CLOCK_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _BadRequest(f"The {name} must be a time in the form HH:MM.")
    return int(match[1]) * 60 + int(match[2])


def _shift_request(email: str, body: Any) -> ShiftVersion:
    """A new shift version from `{effective_from, work_days, start, end}`: days 0 (Sunday) to 6, and an end
    after the start on the same day."""
    if not isinstance(body, dict):
        raise _BadRequest("Send the shift as an object.")
    effective = _strict_day(body.get("effective_from"), "first day of the shift")
    days = body.get("work_days")
    if (not isinstance(days, list) or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
        raise _BadRequest("Choose the days worked, from Sunday (0) to Saturday (6).")
    start, end = _clock_minutes(body.get("start"), "start"), _clock_minutes(body.get("end"), "end")
    if end <= start:
        raise _BadRequest("The shift must end after it starts.")
    return ShiftVersion(email, effective, frozenset(days), start, end, True)


def _time_off_kind(value: Any) -> str:
    if value not in TIME_OFF_KINDS:
        raise _BadRequest("The kind must be pto or sick.")
    return value


def _time_off_request(body: Any) -> tuple[str, date, date, str, str]:
    if not isinstance(body, dict):
        raise _BadRequest("Send the time off as an object.")
    email = body.get("email")
    if not isinstance(email, str):
        raise _BadRequest("Choose a tech.")
    first, last = _strict_day(body.get("first"), "first day"), _strict_day(body.get("last"), "last day")
    if last < first:
        raise _BadRequest("The first day must be on or before the last day.")
    note = body.get("note", "")
    if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
        raise _BadRequest("The note must be text.")
    kind = _time_off_kind(body.get("kind", "pto"))
    return email.strip().lower(), first, last, note.strip(), kind


def _asked_tech(tech: str | None) -> str | None:
    """A `tech` query parameter as the email it names, or None when it names none (All techs)."""
    return (tech or "").strip().lower() or None


def _in_region(conn: sqlite3.Connection, visible: list[str] | None, region: str | None,
               today: date) -> list[str] | None:
    """The techs of `visible` (the viewer's, from `_scope`; None is every tech the section lists) whose current region,
    the one in force on `today` (the app's), is `region`, a `region` query parameter in any case (Regions design: a
    region in the Tech picker is that region's techs among the viewer's). With no region (None or blank) `visible` as
    it is; an unknown one is a bad request."""
    name = (region or "").strip().upper()
    if not name:
        return visible
    if name not in regions.REGIONS:
        raise _BadRequest(regions.NOT_A_REGION)
    among = visible if visible is not None else [t["email"] for t in activity_report.techs(conn)]
    current = regions.regions_of(conn, among, today)
    return [email for email in among if current[email] == name]


NOT_A_TEAM = "Choose a team you can see."
ONLY_MANAGERS_HAVE_REGIONS = "Only a manager has regions."


def _in_team(conn: sqlite3.Connection, viewer: str | None, visible: list[str] | None,
             lead: str | None) -> list[str] | None:
    """The techs of `visible` (the viewer's, from `_scope`) on `lead`'s team, `lead` a lead's email or None for no team
    (`visible` as it is): one tech in Trends is compared with their team lead's team (Regions design), and Activity
    and Trends can show one team (Managers design). `viewer` is who is signed in (None on the PC). A team lead sees
    only their own team, the admin every lead's, and a manager the team of any lead with a tech among theirs; a
    manager holds no team. So another team, a manager, an email that isn't a person's, and any team on the PC, which
    has none, is a bad request."""
    if lead is None:
        return visible
    viewer = (viewer or "").strip().lower()
    members = set(people.visible_techs(conn, lead)) if people.role(conn, lead) in ("admin", "tl") else None
    seen_by = people.role(conn, viewer)
    if members is None or not (lead == viewer or seen_by == "admin"
                               or seen_by == "manager" and any(email in members for email in visible)):
        raise _BadRequest(NOT_A_TEAM)
    return [email for email in visible if email in members]


# -- people (the hosted copy) -----------------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


NO_EMAIL_DOMAIN = "Enter their work email. No work email domain is saved in Settings yet."


def _person_request(body: Any, domain: str | None) -> tuple[str, str, str | None]:
    """(email, role, name) from `{name, role}`, the email made from the name at `domain` (`names.email_for`), or
    `{name, email, role}` or `{email, role}`, the email as given: an email address, "admin", "tl" or "manager", and
    the typed name (None when none is given)."""
    if not isinstance(body, dict):
        raise _BadRequest("Send the person as an object.")
    email, role, name = body.get("email"), body.get("role"), body.get("name")
    if role not in people.ROLES:
        raise _BadRequest("The role must be admin, tl or manager.")
    if not (name is None or isinstance(name, str)):
        raise _BadRequest("The name must be text.")
    name = names.clean(name) if name else None
    if _blank(email):
        if not name:
            raise _BadRequest("Enter their name.")
        if domain is None:
            raise _BadRequest(NO_EMAIL_DOMAIN)
        try:
            email = names.email_for(name, domain)
        except ValueError as exc:
            raise _BadRequest(str(exc)) from None
    try:
        return settings.normalize_email(email if isinstance(email, str) else ""), role, name or None
    except ValueError:
        raise _BadRequest("Enter their work email address.") from None


def _tech_request(body: Any, domain: str | None) -> tuple[str, str]:
    """(name, email) of a tech added by name: `{name}`, the email made from it at `domain` (`names.email_for`), or
    `{name, email}`, the email as given. A name that makes no email, or an email that isn't one, is _BadRequest with
    `email`, the address for the admin to correct ("" when none could be made)."""
    if not isinstance(body, dict):
        raise _BadRequest("Send the tech as an object.")
    name, email = body.get("name"), body.get("email")
    if not isinstance(name, str) or not names.clean(name):
        raise _BadRequest("Enter the tech's name.")
    if not (email is None or isinstance(email, str)):
        raise _BadRequest("The email must be text.")
    if _blank(email):
        if domain is None:
            raise _BadRequest(NO_EMAIL_DOMAIN, email="")
        try:
            email = names.email_for(name, domain)
        except ValueError as exc:
            raise _BadRequest(str(exc), email="") from None
    try:
        return names.clean(name), settings.normalize_email(email)
    except ValueError as exc:
        raise _BadRequest(str(exc), email=email.strip()) from None


def _region_request(body: dict) -> str | None:
    """The region a tech added by name starts in: `region`, one of `regions.REGIONS` as written, or None when it is
    left out (no region set: NASA on the app's zone)."""
    region = body.get("region")
    if _blank(region):
        return None
    if not isinstance(region, str) or region not in regions.REGIONS:
        raise _BadRequest(regions.NOT_A_REGION)
    return region


def _techs_request(body: Any, team: list[str]) -> list[str]:
    """The techs from `{techs: [...]}`: emails of techs on the team."""
    techs = body.get("techs") if isinstance(body, dict) else None
    if not isinstance(techs, list) or not all(isinstance(t, str) for t in techs):
        raise _BadRequest("Send the techs as a list of emails.")
    chosen = [t.strip().lower() for t in techs]
    for email in chosen:
        if email not in team:
            raise _BadRequest(f"{email} is not on the team.")
    return chosen


def _shifts_files(body: Any) -> list[tuple[str, bytes]]:
    """(name, contents) of each Teams Shifts export sent: `{"files": [{"name", "data"}]}`, `data` the workbook in
    base64. They stay in memory: never written to disk or stored. SHIFTS_FILES for no files, and a file's export error
    copy for one that isn't base64."""
    files = body.get("files") if isinstance(body, dict) else None
    if not isinstance(files, list) or not files or not all(
            isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("data"), str) for f in files):
        raise _BadRequest(SHIFTS_FILES)
    decoded = []
    for f in files:
        try:
            decoded.append((f["name"], base64.b64decode(f["data"], validate=True)))
        except ValueError:
            raise _BadRequest(shifts_import.NOT_AN_EXPORT.format(name=f["name"])) from None
    return decoded


def _shifts_plan(conn: sqlite3.Connection, body: Any) -> shifts_import.ImportPlan:
    """The Teams Shifts import of the files sent (`_shifts_files`), their times on the app's zone, planned against the
    database (`shifts_import.plan_import`, which writes nothing). 400 with the error copy for a file that isn't an
    export, or files with no shifts."""
    files = _shifts_files(body)
    zone = settings.get_timezone(conn)
    shifts, offs = [], []
    try:
        for name, data in files:
            rows, off_rows = shifts_import.read_workbook(data, name, zone)
            shifts += rows
            offs += off_rows
        return shifts_import.plan_import(conn, shifts, offs)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from None


# -- periods (the Team overview's and Agents' period and comparison parameters) -----------------------


def _today(tz: str) -> date:
    """Today's date in the owner's time zone. Tests pin it (tests/conftest.py)."""
    return datetime.now(ZoneInfo(tz)).date()


def _period_facts(conn: sqlite3.Connection, today: date | None = None) -> dict[str, date | None]:
    """What the presets need: `today`, `last_reviewed` and `latest_week_with_data`.

    `last_reviewed` is the later of the latest local call day with a current, counted review and the
    last local day wholly covered by a finished review run (scheduled, manual or re-review, complete;
    never a model test), capped at today. So a reviewed day that had no calls still counts as reviewed,
    and a call dated in the future can't move the periods on. `latest_week_with_data` is the Sunday of
    the latest week with a counted review, capped at this week. `today` overrides the real date (the demo's
    pinned day).
    """
    tz = settings.get_timezone(conn)
    today = today or _today(tz)
    latest_call = conn.execute(
        "SELECT MAX(c.call_started_at) FROM reviews r JOIN contacts c ON c.ticket_id = r.ticket_id "
        "WHERE r.is_current = 1 AND r.counted = 1").fetchone()[0]
    covered = conn.execute(
        "SELECT window_start, window_end FROM runs WHERE kind IN ('scheduled', 'manual', 'rereview') "
        "AND status = 'complete' ORDER BY window_end DESC LIMIT 1").fetchone()
    call_day = min(timeutil.local_date(latest_call, tz), today) if latest_call else None
    run_day = date.fromisoformat(timeutil.window_days(covered[0], covered[1], tz)["last_day"]) if covered else None
    days = [day for day in (call_day, run_day) if day is not None]
    return {"today": today, "last_reviewed": min(max(days), today) if days else None,
            "latest_week_with_data": timeutil.week_start(call_day) if call_day else None}


def _activity_facts(today: date) -> dict[str, date | None]:
    """What the presets need for Team activity, which is current to today rather than to the last Call QA review:
    every day up to today counts as reviewed, and no week is "the latest with data", so Last 8 weeks is the 8
    whole weeks ending last Saturday."""
    return {"today": today, "last_reviewed": today, "latest_week_with_data": None}


def _period_request(facts: dict, *, period: str | None, start: str | None,
                    end: str | None) -> periods.Period | None:
    """The `period`, `start` and `end` query parameters as a Period (`start` and `end` alone mean a custom
    range); None when none of them is given, and the caller falls back on the legacy parameters or its
    default."""
    first, last = _iso_day(start, "start day"), _iso_day(end, "end day")
    if not period:
        if first is None and last is None:
            return None
        period = "custom"
    return periods.resolve(period, start=first, end=last, **facts)


def _whole_period(period: periods.Period, asked_end: date | None) -> periods.Period:
    """A so-far period (one reaching today, which `periods` ends at today) back to its full length, so Team
    activity can show the days still to come: the week or month it names, or the custom range as asked."""
    if not period.so_far:
        return period
    if period.preset == "custom" and asked_end is not None:
        end = asked_end
    elif period.weeks is not None:
        end = period.start + timedelta(weeks=period.weeks, days=-1)
    elif period.preset in ("this_month", "last_month"):
        end = period.start.replace(day=calendar.monthrange(period.start.year, period.start.month)[1])
    elif period.preset in periods.MONTH_PRESETS:
        last = periods.months_back(period.start, -(periods.MONTH_PRESETS[period.preset] - 1))
        end = last.replace(day=calendar.monthrange(last.year, last.month)[1])
    else:
        return period
    return replace(period, end=max(end, period.end))


def _comparison_request(facts: dict, period: periods.Period, *, compare: str | None, compare_start: str | None,
                        compare_end: str | None) -> periods.Period | None:
    """The `compare`, `compare_start` and `compare_end` query parameters as the comparison Period (None
    for "none"). `compare` is a COMPARES mode (default "previous"), or a date: the old overview address,
    comparing with the week containing it."""
    mode = compare or "previous"
    first, last = _iso_day(compare_start, "comparison's start day"), _iso_day(compare_end, "comparison's end day")
    if mode not in periods.COMPARES:
        try:
            day = _iso_day(mode, "comparison")
        except _BadRequest:
            raise _BadRequest(f"The comparison must be one of: {', '.join(periods.COMPARES)}, or a week's date "
                              "in the form YYYY-MM-DD.") from None
        return periods.week_of(day, today=facts["today"], last_reviewed=facts["last_reviewed"])
    return periods.comparison(period, mode, start=first, end=last)


def _ticket_sort(sort: str | None) -> str:
    """The Tickets list's optional `sort` query parameter: one of `queries.TICKET_SORTS`, newest by default."""
    value = (sort or "").strip() or "newest"
    if value not in queries.TICKET_SORTS:
        raise _BadRequest("The sort must be one of: " + ", ".join(queries.TICKET_SORTS) + ".")
    return value


def _ticket_filters(start: str | None, end: str | None, agent: str | None, verdict: str | None,
                    missed_check: str | None, priority: str | None, q: str | None) -> dict[str, Any]:
    """The Tickets list's query parameters as `queries.tickets` arguments; empty means no filter."""
    verdict = (verdict or "").strip() or None
    if verdict is not None and verdict not in queries.VERDICT_FILTERS:
        raise _BadRequest("The verdict must be good, needs_review, problem, review_failed or not_counted.")
    return {
        "start": _iso_day(start, "first day"), "end": _iso_day(end, "last day"),
        "agent": (agent or "").strip() or None, "verdict": verdict,
        "missed_check": (missed_check or "").strip() or None, "priority": (priority or "").strip() or None,
        "q": (q or "").strip() or None,
    }


TICKET_FILTER_KEYS = ("start", "end", "agent", "verdict", "missed_check", "priority", "q")
NO_MATCHING_CALLS = "No calls match these filters."


def _review_again_request(body: Any) -> tuple[int | None, dict[str, Any] | None]:
    """(ticket_id, None) for one call's Review again, or (None, filters) for the Tickets list's: the list's
    own filters (`_ticket_filters`), each a string or null."""
    if not isinstance(body, dict) or ("ticket_id" in body) == ("filters" in body):
        raise _BadRequest("Send the call's ticket_id, or the Tickets list's filters.")
    if "ticket_id" in body:
        ticket_id = body["ticket_id"]
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id < 1:
            raise _BadRequest("The ticket_id must be a ticket number.")
        return ticket_id, None
    filters = body["filters"]
    if not isinstance(filters, dict):
        raise _BadRequest("The filters must be an object, as the Tickets list sends them.")
    unknown = sorted(key for key in filters if key not in TICKET_FILTER_KEYS)
    if unknown:
        raise _BadRequest(f"Unknown filters: {', '.join(unknown)}")
    if any(value is not None and not isinstance(value, str) for value in filters.values()):
        raise _BadRequest("Each filter must be text, or null for no filter.")
    return None, _ticket_filters(*(filters.get(key) for key in ("start", "end", "agent", "verdict", "missed_check",
                                                                  "priority", "q")))


def _override_request(body: Any) -> tuple[str, str | None]:
    """(outcome, note) from a Disagree request. A blank note is stored as NULL."""
    if not isinstance(body, dict):
        raise _BadRequest("Send the outcome you think is right, and an optional note.")
    outcome, note = body.get("outcome"), body.get("note")
    if outcome not in OUTCOMES:
        raise _BadRequest("The outcome must be met, missed, na or unsure.")
    if note is not None and not isinstance(note, str):
        raise _BadRequest("The note must be text.")
    note = (note or "").strip() or None
    if note is not None and len(note) > NOTE_MAX_CHARS:
        raise _BadRequest(f"Notes can be up to {NOTE_MAX_CHARS:,} characters.")
    return outcome, note


def _log_tail(log_dir: Path, run_id: int) -> list[str]:
    path = log_path(log_dir, run_id)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-LOG_TAIL_LINES:]


def _zendesk_failure(exc: Exception, config_path: Path | None = None) -> str:
    from work_assistant.zendesk.client import ZendeskAuthError

    if isinstance(exc, ZendeskAuthError):
        return pipeline.zendesk_token_rejected(config_path)
    return f"Zendesk could not be reached ({type(exc).__name__}). Try again in a few minutes."


ZENDESK_UNREACHABLE = "Zendesk couldn't be reached. Check the internet connection and try Run now again."
ANTHROPIC_UNREACHABLE = "Anthropic couldn't be reached. Try again later."
RUN_INTERRUPTED = ("The run was interrupted (the PC or app was closed while it was fetching). "
                   "Start it again with Run now.")
# A model test is started again from its own panel, never Run now. Zendesk only serves a test the calls'
# images, so a Zendesk failure stops a test while it downloads them.
TEST_AGAIN = "Start the test again from Settings → Test a model, or with work-assistant test-model."
TEST_INTERRUPTED = f"The model test was interrupted (the PC or app was closed while it was preparing). {TEST_AGAIN}"
TEST_ZENDESK_UNREACHABLE = ("Zendesk couldn't be reached while downloading the calls' images. Check the internet "
                            f"connection. {TEST_AGAIN}")
ZENDESK_FAILURES = ("ZendeskError", "ConnectError", "ConnectTimeout", "ReadTimeout", "HTTPStatusError",
                    "TransportError")
FAILURE_LABELS = {
    **dict.fromkeys(ZENDESK_FAILURES, ZENDESK_UNREACHABLE),
    **dict.fromkeys(("APIConnectionError", "APITimeoutError", "InternalServerError"), ANTHROPIC_UNREACHABLE),
    pipeline.INTERRUPTED: RUN_INTERRUPTED,
}
TEST_FAILURE_LABELS = {
    **FAILURE_LABELS,
    **dict.fromkeys(ZENDESK_FAILURES, TEST_ZENDESK_UNREACHABLE),
    pipeline.INTERRUPTED: TEST_INTERRUPTED,
}


def failure_label(error: str | None, kind: str | None = None) -> str | None:
    """The plain-English text for a stored `runs.error` of a run of this `kind`.

    The pipeline stores either a sentence the app wrote (the credential messages, "cleared by owner",
    "superseded by run N", the submission warning), which is shown as it is, or an exception's class
    name, which is translated here. The raw text stays in `error` for the run history. A model test's
    labels point to Test a model rather than Run now.
    """
    if error is None:
        return None
    labels = TEST_FAILURE_LABELS if kind == pipeline.TEST_KIND else FAILURE_LABELS
    if error in labels:
        return labels[error]
    if error.isidentifier():  # an exception class name nobody translated
        return f"The run failed ({error}). See the run log in Settings."
    return error


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        close()


# -- the app ------------------------------------------------------------------------------------------


def create_app(db_path: Path, *, run_starter: RunStarter | None = None,
               zendesk_factory: ZendeskFactory | None = None,
               anthropic_factory: AnthropicFactory = default_anthropic_factory,
               log_dir: Path = LOG_DIR, startup_work: bool = True, demo: bool = False,
               port: int = DEFAULT_PORT, schedule_runner: scheduler.Runner | None = None,
               on_quit: Callable[[], None] | None = None, now: datetime | None = None,
               config: ServerConfig | None = None, access_verifier: access.Verifier | None = None,
               demo_identity: str | None = None, config_path: Path | None = None) -> FastAPI:
    """Build the app for the database at `db_path`, served on 127.0.0.1:`port`.

    `run_starter(spec)` starts a run (`{"command": "run", "from", "to", "rereview"}`), starts Review again of
    chosen calls (`{"command": "rereview-tickets", "ticket_ids"}`), confirms one (`{"command": "confirm",
    "run_id"}`) or starts a model test (`{"command": "test-model", "from", "to",
    "model", "effort", "limit"}`) or starts a team activity sync (`{"command": "activity-sync", "kind":
    "refresh"|"backfill"}`) outside this process and returns a process id.
    `zendesk_factory()` returns a read-only Zendesk client (anything with `users_by_email`, and for
    estimates what `fetch_contacts` uses). Either may raise `Unavailable`. `schedule_runner` runs
    `schtasks` for the daily schedule's status (`subprocess.run` when None). `on_quit()` asks the
    server to shut down (`POST /api/quit`, which `work-assistant stop` sends); without it that request is refused.
    `now` pins the app's clock (the demo built as of a fixed day, `serve --demo --demo-date`): today's date, the
    periods that follow it, and the startup purge go by it instead of the real time.

    Every `/api/` request must name this app as its Host (127.0.0.1 or localhost on `port`), which
    defeats DNS rebinding, and every `/api/` request that changes something must come from this app's
    own page (an Origin header, when present, of this app), so no other web page open in the owner's
    browser can confirm a paid run or change settings.

    `config` is the server config the app was started from (`serve --config`); None on the owner's PC, where
    everything is as described above. With one, the process follows it (`server_config.apply`: its secrets file
    and time zone), its `port` replaces the argument, and its `public_host` is accepted too, with an Origin of
    `https://` that host. In its `hosted` edition every Call QA route (CALL_QA_PATHS) answers 404 and
    nothing needs Anthropic: `anthropic_factory` is never called. `GET /healthz` answers `{"ok": true}` in every
    edition, with no checks and no data. The caller passes the config's database and log folder as `db_path`
    and `log_dir`, and its file as `config_path`, which the answer to a rejected Zendesk token names
    (`work-assistant set-secret ... --config`, not the PC's `setup`).

    The `hosted` edition also needs a sign-in on every request but `/healthz` and `/static/`: a token
    Cloudflare Access signed, from the `Cf-Access-Jwt-Assertion` header or else the `CF_Authorization` cookie, checked
    by `access_verifier` (by default one for the config's Access team and AUD tag). Without a good token the answer is
    403 SIGN_IN; when Cloudflare's certificates can't be read, 503. A signed-in request carries the person's email in
    `request.state.email`, which is None in the full edition (no sign-in) and on the unchecked paths.
    `demo_identity`, for the demo only (`demo` must be true), signs every request in as that email with no token and
    no verifier: `serve --demo --edition hosted`. A server config can't turn it on.

    In the `hosted` edition only the people the admin added may use the API (`people`): the config's
    `admin_email` is made the admin at start, and every `/api/` request from anyone else answers 403
    `{"error": "not_added", "email", "admin"}`. Everyone, the admin included, sees and changes only their own team
    (their assigned techs), and a manager every tech in the regions they picked (Managers design): every route that
    names techs goes through `_scope`, and another tech is 404 NOT_FOUND whether or not they exist. Saving settings, a
    backfill and the Teams Shifts import (`/api/shifts-import`) are the admin's alone. The roster (`/api/team`, the
    start date) and the People API (`/api/people`, the only place the whole roster shows) are the admin's and the
    managers' (`_admin_or_manager`), a manager's limited to their regions' techs, adding and removing team leads only
    and never changing a lead's techs elsewhere. A manager writes no shift, time off or 1:1 date (`_not_manager`). The
    full edition has no people: everyone there is the admin, nothing is scoped, and `/api/people`,
    `/api/shifts-import`, `PUT /api/me/regions` and `DELETE /api/team/{email}` answer 404.
    """
    if demo_identity is not None and not demo:
        raise ValueError("A fixed sign-in (demo_identity) is for the demo only.")
    db_path = Path(db_path)
    log_dir = Path(log_dir)
    if config is not None:
        server_config.apply(config)
        port = config.port
    edition = config.edition if config is not None else "full"
    call_qa = edition != "hosted"
    hosted = edition == "hosted"   # sign-in, people and per-TL scoping
    admin_email = config.admin_email if hosted else None
    if hosted and demo_identity is None and access_verifier is None:
        access_verifier = access.Verifier(config.access_team_domain, config.access_aud)
    starter = run_starter or subprocess_run_starter(db_path, log_dir, config_path)
    make_zendesk = zendesk_factory or zendesk_factory_for(db_path, config_path)
    with _connection(db_path) as conn:
        db.init_db(conn)
        rubric.ensure_seeded(conn)
        if admin_email:
            people.ensure_admin(conn, admin_email)

    run_startup = startup_work

    def today_of(conn: sqlite3.Connection) -> date:
        """Today in the owner's time zone: the pinned `now`'s date when there is one."""
        tz = settings.get_timezone(conn)
        return now.astimezone(ZoneInfo(tz)).date() if now is not None else _today(tz)

    def now_of() -> datetime:
        """The moment it is: the pinned `now` (the demo's) when there is one."""
        return now if now is not None else datetime.now(timezone.utc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_startup:
            threading.Thread(
                target=_run_startup_work, name="work-assistant-startup", daemon=True,
                kwargs={"db_path": db_path, "log_dir": log_dir, "anthropic_factory": anthropic_factory, "now": now,
                        "call_qa": call_qa},
            ).start()
        yield

    app = FastAPI(title="Work Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}
    if config is not None and config.public_host:
        allowed_hosts.add(config.public_host)
        allowed_origins.add(f"https://{config.public_host}")

    def role_of(email: str) -> str | None:
        with _connection(db_path) as conn:
            return people.role(conn, email)

    @app.middleware("http")
    async def same_origin_api(request: Request, call_next):
        """In this order: the host and origin checks (`/api/`), the sign-in (the hosted edition), whether the
        app knows the person signed in (`/api/`), then the routes the edition doesn't have, so nobody learns which
        addresses exist without signing in and being added."""
        path = request.url.path
        request.state.email = None
        if path.startswith("/api/"):
            if request.headers.get("host", "").lower() not in allowed_hosts:
                return _error(403, "This app only answers requests addressed to it on this PC.")
            origin = request.headers.get("origin")
            if request.method not in SAFE_METHODS and origin is not None and origin.lower() not in allowed_origins:
                return _error(403, "Changes are only accepted from the Work Assistant page itself.")
        if hosted and path != "/healthz" and not path.startswith("/static/"):
            if demo_identity is not None:
                request.state.email = demo_identity
            else:
                token = request.headers.get(SIGN_IN_HEADER) or request.cookies.get(SIGN_IN_COOKIE) or ""
                try:
                    # In a worker thread: now and then it reads Cloudflare's certificates over the network.
                    request.state.email = await run_in_threadpool(access_verifier.email, token)
                except access.Denied as exc:
                    logger.warning("Refused a request without a valid sign-in: %s", exc)
                    return _error(403, SIGN_IN)
                except access.Unreachable as exc:
                    logger.warning("Couldn't check a sign-in: %s", exc)
                    return _error(503, SIGN_IN_UNREACHABLE)
        if hosted and path.startswith("/api/") and await run_in_threadpool(role_of, request.state.email) is None:
            # Cloudflare let them in, but the admin hasn't added them: the page says so, and names the admin.
            return JSONResponse({"error": "not_added", "email": request.state.email, "admin": admin_email},
                                status_code=403)
        if path.startswith("/api/") and (_matches(path, CALL_QA_PATHS) if hosted
                                         else _matches(path, HOSTED_PATHS) or _under(path, HOSTED_UNDER)):
            return _error(404, NOT_FOUND)
        return await call_next(request)

    @app.exception_handler(Unavailable)
    async def unavailable(request, exc: Unavailable) -> JSONResponse:
        return _error(503, str(exc))

    @app.exception_handler(_BadRequest)
    async def bad_request(request, exc: _BadRequest) -> JSONResponse:
        return _error(400, str(exc), **exc.extra)

    @app.exception_handler(_NotFound)
    async def not_found(request, exc: _NotFound) -> JSONResponse:
        return _error(404, NOT_FOUND)

    @app.exception_handler(_Forbidden)
    async def forbidden(request, exc: _Forbidden) -> JSONResponse:
        return _error(403, str(exc))

    @app.exception_handler(periods.PeriodError)
    async def bad_period(request, exc: periods.PeriodError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(rubric_edit.EditError)
    async def bad_edit(request, exc: rubric_edit.EditError) -> JSONResponse:
        return _error(409 if isinstance(exc, rubric_edit.StaleEdit) else 400, str(exc))

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(versioned_page(STATIC_DIR), headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """That the app answers: for the deploy scripts and the operator on the server. No data, no checks."""
        return {"ok": True}

    def schedule_body() -> dict:
        """The daily task's state in plain English, for `GET /api/schedule` only: asking schtasks takes
        a moment, so the Settings page fetches it on its own and nothing else waits for it. The demo,
        a PC without schtasks, and any schtasks failure all give the placeholder."""
        if demo:
            return dict(SCHEDULE_PLACEHOLDER)
        try:
            state = scheduler.status(schedule_runner)
        except scheduler.SchedulerUnavailable:
            return dict(SCHEDULE_PLACEHOLDER)
        except Exception as exc:  # a schedule problem must never break the Settings page
            logger.warning("Could not read the daily schedule: %s", exc)
            return dict(SCHEDULE_PLACEHOLDER)
        if not state["installed"]:
            return dict(SCHEDULE_PLACEHOLDER)
        return {**state, "text": "Installed"}

    # -- who may see and change what (the hosted edition) -----------------------------------------------------

    def _scope(request: Request, conn: sqlite3.Connection, tech: str | None = None) -> list[str] | None:
        """The one scoping check, which every route that names techs goes through: the techs this request may see
        (`people.visible_techs`). None is every tech: the full edition (no sign-in). In the hosted edition a team
        lead and the admin get their own team, and a manager every tech whose current region is one they ticked. With
        `tech`, a tech outside them raises _NotFound, answered exactly as for a tech that doesn't exist; "" names no
        tech at all (an entry that doesn't exist), which only the unscoped see past. A manager changes less than they
        see: `_not_manager` and `_admin_or_manager` say what."""
        visible = people.visible_techs(conn, request.state.email or "") if hosted else None
        if visible is not None and tech is not None and tech.strip().lower() not in visible:
            raise _NotFound()
        return visible

    def _role(request: Request, conn: sqlite3.Connection) -> str | None:
        """The signed-in person's role ("admin", "tl" or "manager"); on the PC, which has no sign-in, everyone is the
        admin."""
        return people.role(conn, request.state.email or "") if hosted else "admin"

    def _admin_only(request: Request, conn: sqlite3.Connection) -> None:
        """Refuse (_Forbidden) anyone but the admin, in the hosted edition; on the PC everyone is the admin."""
        if _role(request, conn) != "admin":
            raise _Forbidden()

    def _admin_or_manager(request: Request, conn: sqlite3.Connection, tech: str | None = None) -> None:
        """Refuse (_Forbidden, ADMIN_ONLY as before for a team lead) anyone but the admin or a manager: the roster and
        the People API (Managers design). A manager naming `tech` outside their regions gets 404, through `_scope`;
        the admin changes any tech on the roster."""
        kind = _role(request, conn)
        if kind not in ("admin", "manager"):
            raise _Forbidden()
        if kind == "manager" and tech is not None:
            _scope(request, conn, tech)

    def _not_manager(request: Request, conn: sqlite3.Connection) -> None:
        """Refuse (_Forbidden, LEADS_SET_THESE) a manager: shifts, time off and 1:1 dates are each team lead's, for
        their own techs (and the admin's, for theirs). Checked before the tech, so it holds for any tech named."""
        if _role(request, conn) == "manager":
            raise _Forbidden(LEADS_SET_THESE)

    def _entry_tech(conn: sqlite3.Connection, item_id: int) -> str:
        """The tech a time-off entry belongs to; "" when there is no such entry."""
        return next((i.email for i in activity_store.time_off(conn) if i.id == item_id), "")

    def settings_body(visible: list[str] | None = None) -> dict:
        """The settings. `visible` (the viewer's own team, from `_scope`) limits `team` and the 1:1 schedule to them,
        so nobody's settings name a tech outside their team, the admin's included (ruling R3, hosted Team design
        §3)."""
        with _connection(db_path) as conn:
            body: dict[str, Any] = {key: settings.get_setting(conn, key) for key in settings.DEFAULTS}
            body["email_domain"] = settings.email_domain(conn)   # the one in use, saved or worked out
            body["timezone_effective"] = settings.get_timezone(conn)
            body["models"] = _model_choices(conn, body["effort"])
        if visible is not None:
            body["team"] = [email for email in body["team"] if email.strip().lower() in visible]
            body["oneonone_schedule"] = {email: entry for email, entry in body["oneonone_schedule"].items()
                                         if email.strip().lower() in visible}
        # Without Call QA no Anthropic key is ever read, so only the Zendesk token's status is given.
        body["credentials"] = (credentials.secret_status() if call_qa
                               else {"zendesk_token": credentials.get_secret("zendesk_token") is not None})
        body["efforts"] = list(settings.EFFORTS)
        body["demo"] = demo
        body["edition"] = edition
        return body

    @app.get("/api/settings")
    def get_settings(request: Request) -> dict:
        with _connection(db_path) as conn:
            visible = _scope(request, conn)
        return settings_body(visible)

    @app.get("/api/me")
    def me(request: Request) -> dict:
        """Who the page is for: the signed-in email (None without a sign-in), their role and the edition. On the PC
        copy, which has no sign-in, everyone is the admin. The hosted copy adds their typed `name` (None for none),
        the `regions` a manager picked (None for anyone else) and `all_regions`, each region with the city whose time
        it keeps, in their order (Your regions' tick boxes)."""
        if not hosted:
            return {"email": request.state.email, "role": "admin", "edition": edition}
        email = request.state.email
        with _connection(db_path) as conn:
            kind = people.role(conn, email)
            return {"email": email, "role": kind, "edition": edition,
                    "name": next((p["name"] for p in people.everyone(conn) if p["email"] == email), None),
                    "regions": people.manager_regions(conn, email) if kind == "manager" else None,
                    "all_regions": [{"region": region, "city": regions.CITIES[region]} for region in regions.ORDER]}

    @app.put("/api/me/regions")
    def put_my_regions(request: Request, body: Any = Body(...)) -> dict:
        """A manager picks the regions they see (Your regions): `{regions: [...]}`, each one of `regions.REGIONS`, in
        place of those before (empty: none, and so no techs). The answer is `{regions}`, as kept. Anyone but a manager
        is 403. The hosted copy's only (HOSTED_UNDER: the PC answers 404)."""
        with _connection(db_path) as conn:
            if _role(request, conn) != "manager":
                raise _Forbidden(MANAGERS_ONLY)
            picked = body.get("regions") if isinstance(body, dict) else None
            if not isinstance(picked, list):
                raise _BadRequest("Send the regions as a list.")
            try:
                people.set_manager_regions(conn, request.state.email, picked)
            except ValueError as exc:
                raise _BadRequest(str(exc)) from None
            return {"regions": people.manager_regions(conn, request.state.email)}

    @app.get("/api/schedule")
    def get_schedule() -> dict:
        return schedule_body()

    @app.post("/api/quit")
    def quit_app() -> dict:
        if on_quit is None:
            raise Unavailable("This app can't be stopped from here. Close the window it runs in.")
        on_quit()
        return {"stopping": True}

    @app.put("/api/settings")
    def put_settings(request: Request, body: Any = Body(...)) -> Any:
        """Change settings (the admin's only). The answer is the settings as the admin reads them: `team` and the 1:1
        schedule are their own team's. On the hosted copy this save refuses both, since the admin reads only their own
        team's: each person saves their own techs' 1:1 schedules through `PUT /api/prep/schedule/{email}` (hosted Team
        design §4), and the roster changes only through `POST` and `DELETE /api/team`, so no save sending back an
        edited `team` can take another lead's techs off the roster (ruling S7). The PC copy saves `team` here."""
        with _connection(db_path) as conn:
            _admin_only(request, conn)
        if not isinstance(body, dict):
            return _error(400, "Send the settings to change as an object.")
        if hosted and "oneonone_schedule" in body:
            return _error(400, SCHEDULE_FROM_PANEL)
        if hosted and "team" in body:
            return _error(400, TEAM_FROM_PANEL)
        unknown = sorted(key for key in body if key not in settings.DEFAULTS)
        if unknown:
            return _error(400, f"Unknown settings: {', '.join(unknown)}", unknown_settings=unknown)
        cleaned, problems = settings.validate_changes(body)
        if problems:
            return _error(400, " ".join(problems.values()), invalid=problems)
        current: list[str] = []
        if "team" in cleaned:
            with _connection(db_path) as conn:
                current = settings.team_emails(conn)
            added = [email for email in cleaned["team"] if email not in current]
            if added:
                unknown_agents = _unknown_agents(added)
                if isinstance(unknown_agents, JSONResponse):
                    return unknown_agents
                if unknown_agents:
                    return _error(400, "Not found as a Zendesk agent: " + ", ".join(unknown_agents),
                                  unknown_agents=unknown_agents)
        with _connection(db_path) as conn:
            previous_days = int(settings.get_setting(conn, "retention_days"))
            if "team" in cleaned:
                # A tech taken off the team is taken off every TL's techs, in the transaction set_settings commits:
                # adding them back later never quietly shows them to their old TLs again.
                people.drop_techs(conn, [e for e in current if e not in cleaned["team"]])
            settings.set_settings(conn, cleaned)
            if "team" in cleaned:  # shifts follow the team: a leaver's days after tomorrow stop being theirs
                activity_store.apply_team_change(
                    conn, [e for e in cleaned["team"] if e not in current],
                    [e for e in current if e not in cleaned["team"]],
                    today=today_of(conn))
            visible = _scope(request, conn)   # the team change may have taken techs off their team
        body = settings_body(visible)
        body["retention_message"] = None
        if "retention_days" in cleaned and cleaned["retention_days"] < previous_days:
            body["retention_message"] = _erase_now(cleaned["retention_days"])
        return body

    def _erase_now(days: int) -> str:
        """Purge right away after the retention period was shortened; return what to tell the owner."""
        try:
            with _connection(db_path) as conn:
                erased = purge_expired(conn, datetime.now(timezone.utc), days, log_dir)
        except Exception:
            logger.exception("Erasing after the retention change failed; it runs again after the next review run")
            return "Couldn't erase older calls now; will retry on the next run."
        if erased:
            logger.info("Erased the customer content of %d calls older than %d days", erased, days)
            return ("1 older call was erased." if erased == 1 else f"{erased} older calls were erased.")
        return "No older calls needed erasing."

    @app.post("/api/team")
    def add_tech(request: Request, body: Any = Body(...)) -> Any:
        """Add a tech by name (`{name}`, or `{name, email}` once the admin corrected the email made from it). The
        email must be a Zendesk agent's, as for Settings' team. Found: the tech joins the team, with the typed name as
        theirs (`names.store`), and the answer is the settings (as the admin reads them) with `added`, their email.
        Not found: 400 "No Zendesk agent at <email>" with `email`, for the admin to correct; nothing is saved.

        In the hosted edition the tech also joins a team lead's team in the same change: `lead`, a team lead or the
        admin (400 for anyone else: a manager holds no team), or when it is left out the admin themselves, adding, and
        no team for a manager adding; and starts in `region` (one of `regions.REGIONS`; left out, none is set), from
        today. The admin and the managers add techs (a manager into any region: regions are a view). The PC copy has
        no teams or regions."""
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn)
            name, email = _tech_request(body, settings.email_domain(conn))
            if email in settings.team_emails(conn):
                raise _BadRequest(f"{email} is already on the team.")
            lead, region = None, None
            if hosted:
                adding = request.state.email if _role(request, conn) == "admin" else None
                lead, region = _lead_request(conn, body, adding), _region_request(body)
        unknown = _unknown_agents([email])
        if isinstance(unknown, JSONResponse):
            return unknown
        if unknown:
            raise _BadRequest(f"No Zendesk agent at {email}", email=email)
        with _connection(db_path) as conn:
            names.store(conn, email, name)
            if lead is not None:
                people.assign(conn, lead, email)   # committed with the team below: one change
            settings.set_settings(conn, {"team": [*settings.team_emails(conn), email]})
            activity_store.apply_team_change(conn, [email], [], today=today_of(conn))
            if region is not None:
                regions.set_regions(conn, email, [(today_of(conn), region)])
            visible = _scope(request, conn)
        return {**settings_body(visible), "added": email}

    def _lead_request(conn: sqlite3.Connection, body: dict, default: str | None) -> str | None:
        """The team lead a new tech joins: `lead` from the body, a team lead or the admin, or `default` when it is left
        out (the admin adding; None, no team, for a manager adding). A manager is CANT_LEAD: managers hold no team."""
        lead = body.get("lead")
        if _blank(lead):
            return default.strip().lower() if default else None
        if not isinstance(lead, str):
            raise _BadRequest("The team lead must be an email.")
        lead = lead.strip().lower()
        kind = people.role(conn, lead)
        if kind is None:
            raise _BadRequest(f"{lead} hasn't been added.")
        if kind == "manager":
            raise _BadRequest(people.CANT_LEAD)
        return lead

    @app.put("/api/team/{email}")
    def edit_tech(request: Request, email: str, body: Any = Body(...)) -> Any:
        """Techs' Edit (the admin's and the managers'): any of the tech's `name`, `started` (their first tracked day,
        `store.set_start`), `leads` (the people whose team holds them, replacing who did; empty is no team) and
        `regions` (their whole region history, `[{since, region}]`, replacing what was; empty is none,
        `regions.set_regions`), all checked before any is saved; what isn't sent stays. The email can't change: it is
        how Zendesk and their history know them. The answer is the settings as the admin reads them, with `edited`.
        A manager edits a tech in their regions (another is 404) and may move them to any region: when the tech's
        current region is then outside the manager's, the answer adds `moved_out: true`. A lead that is a manager is
        CANT_LEAD, for anyone. The hosted copy's only."""
        if not isinstance(body, dict):
            raise _BadRequest("Send the tech's changes as an object.")
        address = email.strip().lower()
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn, address)
            if address not in settings.team_emails(conn):
                return _error(404, f"{address} is not on the team.")
            name = started = leads = region_changes = None
            if "name" in body:
                if not isinstance(body["name"], str):
                    raise _BadRequest("The name must be text.")
                name = names.clean(body["name"])
                if len(name.split()) < 2:
                    raise _BadRequest(names.TWO_WORDS)
            if "started" in body:
                started = _strict_day(body["started"], "start date")
                if started > today_of(conn):
                    raise _BadRequest("The start date can't be after today.")
                if not activity_store.shift_versions(conn, address):
                    raise _BadRequest(f"Save {address}'s shift first, then their start date.")
            if "leads" in body:
                asked = body["leads"]
                if not isinstance(asked, list) or not all(isinstance(lead, str) for lead in asked):
                    raise _BadRequest("The team leads must be a list of emails.")
                leads = [lead.strip().lower() for lead in asked]
                unknown = [lead for lead in leads if people.role(conn, lead) is None]
                if unknown:
                    raise _BadRequest(f"{unknown[0]} isn't added.")
                if any(people.role(conn, lead) == "manager" for lead in leads):
                    raise _BadRequest(people.CANT_LEAD)
            if "regions" in body:
                asked = body["regions"]
                if not isinstance(asked, list) or not all(isinstance(change, dict) for change in asked):
                    raise _BadRequest("The regions must be a list of changes, each with its date (since) and region.")
                region_changes = [(_strict_day(change.get("since"), "region date"), change.get("region"))
                                  for change in asked]
                try:
                    region_changes = regions.checked(region_changes)
                except ValueError as refused:
                    raise _BadRequest(str(refused)) from None
            if name is not None:
                names.store(conn, address, name)
            if started is not None:
                activity_store.set_start(conn, address, started)
            if leads is not None:
                people.set_leads(conn, address, leads)
            if region_changes is not None:
                regions.set_regions(conn, address, region_changes)
            visible = _scope(request, conn)
            moved_out = _role(request, conn) == "manager" and address not in visible
        return {**settings_body(visible), "edited": address, **({"moved_out": True} if moved_out else {})}

    @app.delete("/api/team/{email}")
    def remove_tech(request: Request, email: str) -> Any:
        """Take a tech off the roster (the admin's, and a manager's for a tech in their regions, whoever leads them),
        and off every team in the same change (`people.drop_techs`), with their shifts following as a team save's do
        (`apply_team_change`). The answer is the settings as the admin reads them, with `removed`, their email. A tech
        not on the roster is 404. The hosted copy's only (HOSTED_UNDER: the PC answers 404)."""
        address = email.strip().lower()
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn, address)
            current = settings.team_emails(conn)
            if address not in current:
                return _error(404, f"{address} is not on the team.")
            people.drop_techs(conn, [address])   # committed with the team below: one change
            settings.set_settings(conn, {"team": [e for e in current if e != address]})
            activity_store.apply_team_change(conn, [], [address], today=today_of(conn))
            visible = _scope(request, conn)
        return {**settings_body(visible), "removed": address}

    @app.post("/api/shifts-import/preview")
    def preview_shifts_import(request: Request, body: Any = Body(...)) -> dict:
        """Import from Teams Shifts' Preview (the admin's only; the hosted copy's, HOSTED_PATHS): what importing the
        export files sent would write for each tech, as `shifts_import.preview_json` gives it. Nothing is written."""
        with _connection(db_path) as conn:
            _admin_only(request, conn)
            return shifts_import.preview_json(_shifts_plan(conn, body))

    @app.post("/api/shifts-import/apply")
    def apply_shifts_import(request: Request, body: Any = Body(...)) -> dict:
        """Import from Teams Shifts' Apply (the admin's only; the hosted copy's): the files sent are planned again,
        the database is copied beside itself (`backups.copy_beside`, "before-shifts-import"), and then every tech in
        the plan is written (`shifts_import.apply_import`). The answer names the copy (`backup`), counts what was
        written (`techs`, `entries`, `time_off`) and carries the plan as Preview showed it (`preview`)."""
        with _connection(db_path) as conn:
            _admin_only(request, conn)
            plan = _shifts_plan(conn, body)
            backup = backups.copy_beside(conn, "before-shifts-import")
            written = shifts_import.apply_import(conn, plan)
            return {"backup": str(backup), **written, "preview": shifts_import.preview_json(plan)}

    if demo and hosted:
        from work_assistant import demo as demo_data

        @app.get("/demo/shifts-export.xlsx", include_in_schema=False)
        def demo_shifts_export() -> Response:
            """The hosted demo's invented Teams Shifts export (`demo.shifts_export`, built in memory), to choose in
            Import from Teams Shifts: the hosted demo's only, so there is never one to fetch from a real copy."""
            return Response(demo_data.shifts_export(), media_type=XLSX_TYPE, headers={
                "Content-Disposition": f'attachment; filename="{demo_data.SHIFTS_EXPORT_NAME}"',
                "Cache-Control": "no-store",
            })

    def _unknown_agents(emails: list[str]) -> list[str] | JSONResponse:
        from work_assistant.zendesk.client import ZendeskError

        client = make_zendesk()
        try:
            found = client.users_by_email(emails)
        except ZendeskError as exc:
            return _error(503, _zendesk_failure(exc, config_path))
        finally:
            _close(client)
        return [e for e in emails if (found.get(e) or {}).get("role") not in AGENT_ROLES]

    @app.get("/api/runs")
    def list_runs() -> dict:
        with _connection(db_path) as conn:
            tz = settings.get_timezone(conn)
            rows = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (RUNS_SHOWN,)).fetchall()
            total = conn.execute("SELECT COALESCE(SUM(actual_usd), 0) FROM runs").fetchone()[0]
        runs = [
            {
                "run_id": r["run_id"], "kind": r["kind"], "status": r["status"],
                "window_start": r["window_start"], "window_end": r["window_end"],
                **timeutil.window_days(r["window_start"], r["window_end"], tz),
                "estimate_usd": r["estimate_usd"], "actual_usd": r["actual_usd"],
                "contact_count": r["contact_count"], "excluded_purged": r["excluded_purged"],
                "error": r["error"], "error_label": failure_label(r["error"], r["kind"]),
                "created_at": r["created_at"], "finished_at": r["finished_at"],
                "log_tail": _log_tail(log_dir, r["run_id"]),
            }
            for r in rows
        ]
        return {"runs": runs, "total_spent_usd": round(total, 2)}

    @app.post("/api/runs/estimate")
    def estimate(body: Any = Body(...)) -> Any:
        from work_assistant.zendesk.client import ZendeskError
        from work_assistant.zendesk.fetch import fetch_contacts

        spec = _run_request(body)
        with _connection(db_path) as conn:
            tz = settings.get_timezone(conn)
            team = settings.team_emails(conn)
        window = timeutil.date_range_window(date.fromisoformat(spec["from"]), date.fromisoformat(spec["to"]), tz)
        client = make_zendesk()
        try:
            with _connection(db_path) as conn:
                result = pipeline.estimate_window(
                    conn, window, rereview=spec["rereview"],
                    fetcher=lambda w, log: fetch_contacts(client, w, team, log))
        except ZendeskError as exc:
            return _error(503, _zendesk_failure(exc, config_path))
        finally:
            _close(client)
        return {"estimate_usd": result.estimate_usd, "contacts": result.contacts,
                "excluded_purged": result.excluded_purged}

    @app.post("/api/runs", status_code=202)
    def start_run(body: Any = Body(...)) -> dict:
        spec = _run_request(body)
        pid = starter(spec)
        return {"started": True, "pid": pid}

    def _review_again_ids(body: Any) -> list[int] | JSONResponse:
        """The calls a Review again request covers: the one call, or every call the Tickets list shows for its
        filters (`queries.tickets`, the list's own query), not only the first 200 on the page."""
        ticket_id, filters = _review_again_request(body)
        with _connection(db_path) as conn:
            if ticket_id is not None:
                if conn.execute("SELECT 1 FROM contacts WHERE ticket_id = ?", (ticket_id,)).fetchone() is None:
                    return _error(404, f"Ticket {ticket_id} has no review on record.")
                return [ticket_id]
            ids = sorted(row["ticket_id"] for row in queries.tickets(conn, **filters)["rows"])
        if not ids:
            raise _BadRequest(NO_MATCHING_CALLS)
        return ids

    @app.post("/api/review-again/estimate")
    def review_again_estimate(body: Any = Body(...)) -> Any:
        """What Review again of a call, or of the Tickets list's filtered calls, would cost now
        (`pipeline.estimate_tickets`): the calls are read fresh from Zendesk by id, and purged calls and calls
        held by a run in progress are counted apart. Nothing is stored or sent."""
        from work_assistant.zendesk.client import ZendeskError
        from work_assistant.zendesk.fetch import fetch_tickets

        ids = _review_again_ids(body)
        if isinstance(ids, JSONResponse):
            return ids
        with _connection(db_path) as conn:
            team = settings.team_emails(conn)
            limit = float(settings.get_setting(conn, "budget_limit_usd"))
        client = make_zendesk()
        try:
            with _connection(db_path) as conn:
                result = pipeline.estimate_tickets(
                    conn, ids, fetcher_for_ids=lambda chosen, log: fetch_tickets(client, chosen, team, log))
        except ZendeskError as exc:
            return _error(503, _zendesk_failure(exc, config_path))
        finally:
            _close(client)
        return {"calls": len(ids), "estimate_usd": result.estimate_usd, "contacts": result.contacts,
                "excluded_purged": result.excluded_purged, "excluded_held": result.excluded_held,
                "budget_limit_usd": limit}

    @app.post("/api/review-again", status_code=202)
    def review_again(body: Any = Body(...)) -> Any:
        """Start Review again (`work-assistant run --rereview --tickets-file`) outside this process. Its own estimate
        goes through the budget guard, so one over the limit waits for Confirm in the banner."""
        ids = _review_again_ids(body)
        if isinstance(ids, JSONResponse):
            return ids
        pid = starter({"command": "rereview-tickets", "ticket_ids": ids})
        return {"started": True, "pid": pid, "calls": len(ids)}

    @app.post("/api/tests/estimate")
    def estimate_test(body: Any = Body(...)) -> dict:
        """What a model test (`pipeline.estimate_test`) would test and cost: read from the stored calls,
        with no Zendesk call. Calls left out are counted by reason."""
        spec = _test_request(body)
        with _connection(db_path) as conn:
            window = timeutil.date_range_window(date.fromisoformat(spec["from"]), date.fromisoformat(spec["to"]),
                                                settings.get_timezone(conn))
            result = pipeline.estimate_test(conn, window, model_id=spec["model"], effort=spec["effort"],
                                            limit=spec["limit"])
        return {"estimate_usd": result.estimate_usd, "contacts": result.contacts,
                "excluded_purged": result.excluded_purged, "excluded_errored": result.excluded_errored,
                "excluded_unreviewed": result.excluded_unreviewed}

    @app.post("/api/tests", status_code=202)
    def start_test(body: Any = Body(...)) -> dict:
        """Start a model test (`work-assistant test-model`) outside this process. Its own estimate goes through
        the budget guard, so one over the limit waits for Confirm in the banner."""
        spec = _test_request(body)
        pid = starter(spec)
        return {"started": True, "pid": pid}

    @app.get("/api/tests")
    def list_tests() -> dict:
        """Every model test, newest first (`compare.test_list`)."""
        with _connection(db_path) as conn:
            return {"tests": compare.test_list(conn)}

    @app.get("/api/tests/{run_id}")
    def test_report(run_id: int) -> Any:
        """A model test's comparison report (`compare.report`)."""
        with _connection(db_path) as conn:
            result = compare.report(conn, run_id)
        if result is None:
            return _error(404, f"There is no model test {run_id}.")
        return result

    @app.get("/api/tests/{run_id}/calls/{ticket_id}")
    def test_call(run_id: int, ticket_id: int) -> Any:
        """One call of a model test, every check side by side (`compare.call_detail`)."""
        with _connection(db_path) as conn:
            known = compare.is_test(conn, run_id)
            detail = compare.call_detail(conn, run_id, ticket_id) if known else None
        if not known:
            return _error(404, f"There is no model test {run_id}.")
        if detail is None:
            return _error(404, f"Model test {run_id} has no result for call {ticket_id}.")
        return detail

    def _run_status(run_id: int) -> str | None:
        with _connection(db_path) as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row["status"] if row is not None else None

    @app.post("/api/runs/{run_id}/confirm", status_code=202)
    def confirm_run(run_id: int) -> Any:
        status = _run_status(run_id)
        if status is None:
            return _error(404, f"Run {run_id} does not exist.")
        if status != "awaiting_confirmation":
            return _error(409, f"Run {run_id} is not waiting for confirmation (it is {status}).")
        pid = starter({"command": "confirm", "run_id": run_id})
        return {"started": True, "pid": pid}

    @app.post("/api/runs/{run_id}/clear")
    def clear_run(run_id: int) -> Any:
        status = _run_status(run_id)
        if status is None:
            return _error(404, f"Run {run_id} does not exist.")
        if status != "submission_unknown":
            return _error(409, f"Run {run_id} cannot be cleared (it is {status}).")
        with _connection(db_path) as conn:
            pipeline.clear_run(conn, run_id, log_dir=log_dir)
        return {"cleared": True}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int) -> Any:
        """Cancel a run or model test waiting for the OK (`pipeline.cancel_run`). Nothing was sent yet."""
        if _run_status(run_id) is None:
            return _error(404, f"Run {run_id} does not exist.")
        try:
            with _connection(db_path) as conn:
                pipeline.cancel_run(conn, run_id, log_dir=log_dir)
        except ValueError as exc:   # not waiting for the OK, or a confirm moved it on just now
            return _error(409, str(exc))
        return {"run_id": run_id, "status": "cancelled"}

    @app.get("/api/overview")
    def overview(period: str | None = None, start: str | None = None, end: str | None = None,
                 compare: str | None = None, compare_start: str | None = None, compare_end: str | None = None,
                 week: str | None = None, agent: str | None = None) -> dict:
        """The Team overview (`queries.overview`) for a period (default Last week) against its comparison
        (default the period before). An empty `agent` means the whole team.

        The old address still opens what it did: `week` is the week containing that day, and `compare`
        as a date the week containing it.
        """
        week_day = _iso_day(week, "week")
        with _connection(db_path) as conn:
            facts = _period_facts(conn, today_of(conn))
            chosen = _period_request(facts, period=period, start=start, end=end)
            if chosen is None:
                chosen = (periods.week_of(week_day, today=facts["today"], last_reviewed=facts["last_reviewed"])
                          if week_day is not None else periods.resolve("last_week", **facts))
            against = _comparison_request(facts, chosen, compare=compare, compare_start=compare_start,
                                          compare_end=compare_end)
            return queries.overview(conn, chosen, against, (agent or "").strip() or None,
                                    last_reviewed=facts["last_reviewed"])

    @app.get("/api/agents")
    def list_agents() -> dict:
        """The Agents screen's picker, and the agent it shows by default: the first active agent with
        calls on record, else the first active agent, else the first agent."""
        with _connection(db_path) as conn:
            agents = queries.agent_list(conn)
        default = next((a for a in agents if a["active"] and a["has_history"]), None) \
            or next((a for a in agents if a["active"]), None) or (agents[0] if agents else None)
        return {"agents": agents, "default": default["email"] if default else None}

    def _known_agent(conn: sqlite3.Connection, email: str) -> str | None:
        """The normalized email, if the Agents screen knows this agent (see `queries.agent_list`)."""
        email = email.strip().lower()
        return email if any(a["email"] == email for a in queries.agent_list(conn)) else None

    def _unknown_agent(email: str) -> JSONResponse:
        return _error(404, f"No tech {email.strip()} on record: no calls, not on the team, and no notes.")

    @app.get("/api/agents/{email}")
    def agent_detail(email: str, period: str | None = None, start: str | None = None, end: str | None = None,
                     compare: str | None = None, compare_start: str | None = None, compare_end: str | None = None,
                     weeks: str | None = None, week: str | None = None) -> Any:
        """One agent's page (`queries.agent_detail`) for a period (default Last 8 weeks), with its
        comparison (default the period before).

        The old address still opens what it did: `weeks` weeks (1 to 52, default 8) ending with the week
        containing `week`, or with the latest week with reviewed calls.
        """
        count, week_day = _weeks(weeks), _iso_day(week, "week")
        with _connection(db_path) as conn:
            facts = _period_facts(conn, today_of(conn))
            chosen = _period_request(facts, period=period, start=start, end=end)
            if chosen is None:
                chosen = (periods.weeks_ending(count, week_day, **facts) if weeks or week_day is not None
                          else periods.resolve("last_8_weeks", **facts))
            against = _comparison_request(facts, chosen, compare=compare, compare_start=compare_start,
                                          compare_end=compare_end)
            known = _known_agent(conn, email)
            if known is None:
                return _unknown_agent(email)
            body = queries.agent_detail(conn, known, chosen, last_reviewed=facts["last_reviewed"])
        return {**body, "comparison": against.as_dict() if against else None}

    @app.post("/api/agents/{email}/notes", status_code=201)
    def add_note(email: str, body: Any = Body(...)) -> Any:
        """Save a private coaching note. The body is plain text; outer whitespace is trimmed."""
        text = body.get("body") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return _error(400, "Write the note first.")
        text = text.strip()
        if len(text) > NOTE_MAX_CHARS:
            return _error(400, f"Notes can be up to {NOTE_MAX_CHARS:,} characters.")
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with _connection(db_path) as conn:
            known = _known_agent(conn, email)
            if known is None:
                return _unknown_agent(email)
            note_id = conn.execute(
                "INSERT INTO coaching_notes (agent_email, created_at, body) VALUES (?, ?, ?)",
                (known, created_at, text)).lastrowid
            conn.commit()
        return {"note_id": note_id, "created_at": created_at, "body": text}

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: int) -> Any:
        with _connection(db_path) as conn:
            deleted = conn.execute("DELETE FROM coaching_notes WHERE note_id = ?", (note_id,)).rowcount
            conn.commit()
        if not deleted:
            return _error(404, f"Note {note_id} does not exist (it may already be deleted).")
        return {"deleted": True}

    @app.get("/api/tickets")
    def list_tickets(start: str | None = None, end: str | None = None, agent: str | None = None,
                     verdict: str | None = None, missed_check: str | None = None, priority: str | None = None,
                     q: str | None = None, sort: str | None = None) -> dict:
        """The Tickets list (`queries.tickets`): without a verdict filter, Not counted calls are left out.
        `sort` is one of `queries.TICKET_SORTS` (newest first when absent)."""
        filters = _ticket_filters(start, end, agent, verdict, missed_check, priority, q)
        with _connection(db_path) as conn:
            return queries.tickets(conn, **filters, sort=_ticket_sort(sort))

    @app.get("/api/tickets/{ticket_id}")
    def ticket(ticket_id: int) -> Any:
        with _connection(db_path) as conn:
            detail = queries.ticket_detail(conn, ticket_id)
        if detail is None:
            return _error(404, f"Ticket {ticket_id} has no review on record.")
        return detail

    @app.get("/api/export.xlsx")
    def export_xlsx(start: str | None = None, end: str | None = None, agent: str | None = None,
                    verdict: str | None = None, missed_check: str | None = None, priority: str | None = None,
                    q: str | None = None, sort: str | None = None) -> Response:
        """The current filtered Tickets list, in its sort order, as an Excel download built in memory (never
        on disk)."""
        filters = _ticket_filters(start, end, agent, verdict, missed_check, priority, q)
        with _connection(db_path) as conn:
            rows = queries.tickets(conn, **filters, sort=_ticket_sort(sort))["rows"]
            subdomain = settings.get_setting(conn, "zendesk_subdomain")
            today = today_of(conn).isoformat()
        buffer = io.BytesIO()
        export.write_xlsx(rows, buffer, subdomain=subdomain)
        return Response(buffer.getvalue(), media_type=XLSX_TYPE, headers={
            "Content-Disposition": f'attachment; filename="work-assistant-tickets-{today}.xlsx"',
            "Cache-Control": "no-store",
        })

    def _override_target(conn: sqlite3.Connection, review_id: int, check_id: str) -> JSONResponse | int:
        """The ticket id of a current review's enabled check, or the error response."""
        review = conn.execute("SELECT ticket_id, rubric_version_id, is_current FROM reviews WHERE review_id = ?",
                              (review_id,)).fetchone()
        if review is None:
            return _error(404, f"Review {review_id} does not exist.")
        enabled = {c.check_id for c in rubric.get_version(conn, review["rubric_version_id"]).checks if c.enabled}
        found = conn.execute("SELECT 1 FROM check_results WHERE review_id = ? AND check_id = ?",
                             (review_id, check_id)).fetchone()
        if found is None or check_id not in enabled:
            return _error(404, f"Review {review_id} has no result for check {check_id}.")
        if not review["is_current"]:
            return _error(409, "This call has been reviewed again since this page loaded. Reload to see the "
                               "current review.")
        return review["ticket_id"]

    @app.put("/api/reviews/{review_id}/checks/{check_id}/override")
    def set_override(review_id: int, check_id: str, body: Any = Body(...)) -> Any:
        """Disagree: record the owner's outcome and note (the reviewer's stays as it was) and clear the
        check's recheck flag. Returns the ticket's refreshed detail."""
        outcome, note = _override_request(body)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with _connection(db_path) as conn:
            target = _override_target(conn, review_id, check_id)
            if isinstance(target, JSONResponse):
                return target
            conn.execute(
                "UPDATE check_results SET override_outcome = ?, override_note = ?, override_at = ?, "
                "needs_recheck = 0 WHERE review_id = ? AND check_id = ?", (outcome, note, now, review_id, check_id))
            conn.commit()
            return queries.ticket_detail(conn, target)

    @app.delete("/api/reviews/{review_id}/checks/{check_id}/override")
    def clear_override(review_id: int, check_id: str) -> Any:
        """Undo the owner's override: all three override columns go back to NULL (never an empty
        string, which scoring.effective would treat as an override). Returns the refreshed detail."""
        with _connection(db_path) as conn:
            target = _override_target(conn, review_id, check_id)
            if isinstance(target, JSONResponse):
                return target
            conn.execute(
                "UPDATE check_results SET override_outcome = NULL, override_note = NULL, override_at = NULL "
                "WHERE review_id = ? AND check_id = ?", (review_id, check_id))
            conn.commit()
            return queries.ticket_detail(conn, target)

    @app.get("/api/rubric")
    def get_rubric() -> dict:
        """The current rubric version: checks, reference documents, timed rule settings."""
        with _connection(db_path) as conn:
            return rubric_edit.version_json(rubric.current_version(conn))

    @app.get("/api/rubric/versions")
    def rubric_versions() -> dict:
        """Every version's id, date and note, oldest first."""
        with _connection(db_path) as conn:
            versions = rubric.list_versions(conn)
        return {"versions": [{"version_id": v, "created_at": created, "note": note} for v, created, note in versions]}

    @app.get("/api/rubric/versions/{version_id}")
    def rubric_version(version_id: int) -> Any:
        with _connection(db_path) as conn:
            try:
                return rubric_edit.version_json(rubric.get_version(conn, version_id))
            except KeyError:
                return _error(404, f"Rubric version {version_id} does not exist.")

    @app.put("/api/rubric")
    def put_rubric(body: Any = Body(...)) -> dict:
        """Save the edited rubric as a new version (see `rubric_edit.parse_edit` for the rules). It applies
        from the next run; past results keep the version they were scored with."""
        with _connection(db_path) as conn:
            edit = rubric_edit.parse_edit(body, rubric.current_version(conn), rubric_edit.known_checks(conn))
            saved = rubric.save_version(conn, edit.checks, edit.reference, edit.params, edit.note)
            return rubric_edit.version_json(saved)

    @app.get("/api/agreement")
    def agreement(weeks: str | None = None) -> dict:
        """How often the owner kept the reviewer's call (`queries.agreement`) over the last `weeks` full
        weeks (1 to 52, default 8) plus the current week so far, in the configured time zone."""
        count = _weeks(weeks)
        with _connection(db_path) as conn:
            today = today_of(conn)
            first = timeutil.week_start(today) - timedelta(weeks=count)
            result = queries.agreement(conn, first, today)
        return {**result, "weeks": count, "first_day": first.isoformat(), "last_day": today.isoformat()}

    # -- Team activity -------------------------------------------------------------------------------------

    def _listed_tech(conn: sqlite3.Connection, tech: str | None, *, required: bool) -> str | None:
        """The normalized email of a tech the section lists (the team, or anyone with a shift on record)."""
        email = (tech or "").strip().lower()
        if not email:
            if required:
                raise _BadRequest("Choose a tech.")
            return None
        if not any(t["email"] == email for t in activity_report.techs(conn)):
            raise _BadRequest(f"{email} is not a tech on the team or with a shift on record.")
        return email

    def _team_tech(conn: sqlite3.Connection, email: str) -> str:
        email = email.strip().lower()
        if email not in settings.team_emails(conn):
            raise _BadRequest(f"{email} is not on the team.")
        return email

    @app.get("/api/activity")
    def activity(request: Request, period: str | None = None, start: str | None = None, end: str | None = None,
                 tech: str | None = None, region: str | None = None, team: str | None = None) -> dict:
        """The Team activity grid (`activity.report.range_view`) for a period (default This week so far; the owner,
        2026-10-21), for the whole team, one region (`_in_region`), one team (`team`, a lead's email in any case, as on
        Trends: `_in_team`) or one tech, each tech's row on their own clock at `now_of`. The days cover the
        whole period, including days that have not happened yet. `picker`, the Tech picker's lines, is every tech the
        viewer sees in the period with their region (`activity_report.picker`), whatever region, team or tech is asked
        for. A team lead's grid and picker hold only their techs, a manager's their regions' techs."""
        with _connection(db_path) as conn:
            seen = _scope(request, conn, _asked_tech(tech))
            viewer = request.state.email if hosted else None
            facts = _activity_facts(today_of(conn))
            visible = _in_region(conn, _in_team(conn, viewer, seen, (team or "").strip().lower() or None), region,
                                 facts["today"])
            chosen = _period_request(facts, period=period, start=start, end=end) \
                or periods.resolve("this_week", **facts)
            email = _listed_tech(conn, tech, required=False)
            answer = activity_report.range_view(conn, _whole_period(chosen, _iso_day(end, "end day")), email,
                                                today=facts["today"], emails=visible, now=now_of())
            days = [date.fromisoformat(day) for day in answer["days"]]
            if hosted:   # the hosted copy has no Refresh: the page says when the next scheduled sync comes
                answer["synced"] = {**answer["synced"], "next": next_sync_iso(conn)}
            return {**answer, "picker": activity_report.picker(conn, days, seen, facts["today"])}

    @app.get("/api/activity/trends")
    def activity_trends_view(request: Request, period: str | None = None, start: str | None = None,
                             end: str | None = None, compare: str | None = None, compare_start: str | None = None,
                             compare_end: str | None = None, tech: str | None = None,
                             region: str | None = None, team: str | None = None) -> dict:
        """Team activity's Trends (`activity.trends.trends_view`): the ten measures for the team and each tech over
        a period (default Last 8 weeks) against its comparison (default none; compare=previous asks for the period
        before: the owner, 2026-10-21), week by week (month by month for a long period), each tech's days on their
        own clock at `now_of`. Last year is compared like any period: the counts are kept beyond retention, and
        coverage_from says where the stored history starts. Days still to come count nothing, so the period is used
        as resolved. `region` limits the techs, and the team's figures, to one region's (`_in_region`), and `team`, a
        lead's email in any case, to that lead's team's (`_in_team`). `tech` is checked as `/api/activity` checks it
        and echoed back; with neither it stands for the tech's comparison team (`people.comparison_team`), since one
        tech is compared with their team lead's team (Regions design, T-144), so every tech of that team is returned
        for the graph; on the PC, which has no teams, that is every tech. `team` in the answer is the lead whose team
        the techs are (None for every tech or a region's). `picker`, the Tech picker's lines, is every tech the
        viewer sees over the days read, with their region (`activity_report.picker`), whatever is asked for. For a
        team lead, and for the admin, "every tech", the picker and the team's figures are their own team's (Review
        Focus 5)."""
        with _connection(db_path) as conn:
            asked = _asked_tech(tech)
            seen = _scope(request, conn, asked)
            viewer = request.state.email if hosted else None
            lead = (team or "").strip().lower() or None
            if lead is None and asked is not None and not (region or "").strip():
                lead = people.comparison_team(conn, viewer, asked)
            facts = _activity_facts(today_of(conn))
            visible = _in_region(conn, _in_team(conn, viewer, seen, lead), region, facts["today"])
            chosen = _period_request(facts, period=period, start=start, end=end) \
                or periods.resolve("last_8_weeks", **facts)
            against = _comparison_request(facts, chosen, compare=compare or "none", compare_start=compare_start,
                                          compare_end=compare_end)
            email = _listed_tech(conn, tech, required=False)
            answer = activity_trends.trends_view(conn, chosen, against, today=facts["today"], tech=email,
                                                 emails=visible, now=now_of())
            return {**answer, "team": lead,
                    "picker": activity_report.picker(conn, activity_trends.days_of(chosen, against), seen,
                                                     facts["today"])}

    @app.get("/api/activity/day")
    def activity_day(request: Request, tech: str | None = None, day: str | None = None) -> dict:
        """One tech's day (`activity.report.day_view`), on their own clock at `now_of`. For a team lead, an assignee
        change's old or new value that isn't one of their techs is null."""
        chosen = _iso_day(day, "day")
        if chosen is None:
            raise _BadRequest("Choose a day.")
        with _connection(db_path) as conn:
            visible = _scope(request, conn, _asked_tech(tech))
            email = _listed_tech(conn, tech, required=True)
            return activity_report.day_view(conn, email, chosen, today=today_of(conn), emails=visible, now=now_of())

    @app.post("/api/activity/sync", status_code=202)
    def start_activity_sync(request: Request, body: Any = Body(...)) -> Any:
        """Start reading the team's activity from Zendesk now (`refresh`, or `backfill` to read back through the
        retention period). Refused while a sync is running. Refresh is open to anyone the app knows: it reads the
        whole roster, and says nothing about any tech. A backfill, the long read, is the admin's only."""
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in SYNC_KINDS:
            raise _BadRequest(f"Choose what to start: {', '.join(SYNC_KINDS)}.")
        with _connection(db_path) as conn:
            if kind == "backfill":
                _admin_only(request, conn)
            if activity_report.sync_running(conn):
                return _error(409, "A team activity sync is already running. Wait for it to finish.")
        pid = starter({"command": "activity-sync", "kind": kind})
        return {"started": True, "pid": pid}

    # -- 1:1 prep ---------------------------------------------------------------------------------------------
    # Each prep is counted from the collected data when it is asked for (`oneonone.report`); nothing is made. In the
    # hosted edition each of these is limited to the viewer's techs (`_scope`): a lead's and the admin's own team, a
    # manager's regions. Another tech is 404, as on Team activity's addresses.

    def prep_email(email: str | None) -> str:
        if not email or not email.strip():
            raise _BadRequest("Choose a tech.")
        return email.strip().lower()

    @app.get("/api/prep")
    def prep_page(request: Request) -> dict:
        """The 1:1 prep page (`oneonone.report.latest_view`): each tech's latest 1:1 period, counted now, and when
        the data was last synced."""
        with _connection(db_path) as conn:
            page = prep_report.latest_view(conn, today=today_of(conn), now=now_of(), emails=_scope(request, conn))
            if hosted:
                page["next_sync"] = next_sync_iso(conn)
            return page

    def next_sync_iso(conn: sqlite3.Connection) -> str:
        """When the hosted copy's next scheduled sync runs (`sync_schedule`), as ISO UTC."""
        return activity_store.iso(sync_schedule.next_sync(now_of()))

    @app.get("/api/prep/tech")
    def prep_tech(request: Request, email: str | None = None, day: str | None = None) -> dict:
        """One tech's 1:1 detail (`oneonone.report.tech_view`): their periods and the lists behind a day, by
        default their latest."""
        chosen = _iso_day(day, "day")
        tech = prep_email(email)
        with _connection(db_path) as conn:
            _scope(request, conn, tech)
            try:
                return prep_report.tech_view(conn, tech, chosen, today=today_of(conn), now=now_of())
            except ValueError as exc:
                raise _BadRequest(str(exc)) from None

    @app.get("/api/prep/trends")
    def prep_trends(request: Request, email: str | None = None, n: str | None = None) -> dict:
        """One tech's trends over their last `n` 1:1s (4, 8, 13 or 26; 8 by default)."""
        tech = prep_email(email)
        if n is None or n == "":
            size = 8
        elif n.isdecimal():
            size = int(n)
        else:
            raise _BadRequest(prep_report.BAD_TREND_SIZE)
        with _connection(db_path) as conn:
            visible = _scope(request, conn, tech)
            try:
                return prep_report.trends_view(conn, tech, size, today=today_of(conn), now=now_of(),
                                                emails=visible)
            except ValueError as exc:
                raise _BadRequest(str(exc)) from None

    @app.get("/api/prep/schedule")
    def prep_schedule(request: Request) -> list[dict]:
        """The Settings 1:1 schedule rows (`oneonone.report.schedule_rows`)."""
        with _connection(db_path) as conn:
            return prep_report.schedule_rows(conn, today=today_of(conn), emails=_scope(request, conn))

    @app.put("/api/prep/schedule/{email}")
    def put_prep_schedule(request: Request, email: str, body: Any = Body(...)) -> dict:
        """Save one tech's 1:1 schedule (`{"date": "YYYY-MM-DD" | null, "weeks": 1-8}`; a null date clears it): a tech
        on the viewer's team, whoever the viewer is, but never a manager (`_not_manager`). Every other tech's entry
        stays as stored. Returns the tech's Settings row (`oneonone.report.schedule_rows`)."""
        if not isinstance(body, dict) or "date" not in body:
            raise _BadRequest(SCHEDULE_BODY)
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, email)
            address = _team_tech(conn, email)
            conn.execute("BEGIN IMMEDIATE")   # leads share the one setting: read and write it without a gap
            stored = settings.get_setting(conn, "oneonone_schedule")
            if body["date"] is None:
                stored.pop(address, None)
            else:
                try:
                    stored.update(settings.normalize_schedule(
                        {address: {"date": body["date"], "weeks": body.get("weeks")}}))
                except ValueError as exc:
                    raise _BadRequest(str(exc)) from None
            settings.set_setting(conn, "oneonone_schedule", stored)
            return prep_report.schedule_rows(conn, today=today_of(conn), emails=[address])[0]

    @app.get("/api/shifts")
    def get_shifts(request: Request) -> dict:
        with _connection(db_path) as conn:
            return activity_report.shifts_view(conn, _scope(request, conn), today_of(conn))

    @app.put("/api/shifts/{email}")
    def put_shift(request: Request, email: str, body: Any = Body(...)) -> dict:
        """Add a shift version for a tech on the team, in force from its first day until the next one. A version
        for a day that already has one replaces it. Returns that tech's versions. Never a manager's."""
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, email)
            address = _team_tech(conn, email)
            activity_store.add_shift_version(conn, _shift_request(address, body))
            activity_store.merge_repeats(conn, address)
            return next(t for t in activity_report.shifts_view(conn, today=today_of(conn))["techs"]
                        if t["email"] == address)

    @app.put("/api/shifts/{email}/start")
    def put_shift_start(request: Request, email: str, body: Any = Body(...)) -> dict:
        """Make a day, up to today, the tech's first tracked day (`store.set_start`): days before it are not
        tracked. Returns that tech's versions. In the hosted edition the admin sets it, for any tech on the roster, from
        the Techs panel (the owner, 2026-10-21): a start date is the tech's, not their lead's; and a manager, for a tech
        in their regions (another is 404)."""
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn, email)
            address = _team_tech(conn, email)
            if not isinstance(body, dict):
                raise _BadRequest("Send the start as an object.")
            started = _strict_day(body.get("started"), "start date")
            if started > today_of(conn):
                raise _BadRequest("The start date can't be after today.")
            if not activity_store.shift_versions(conn, address):
                raise _BadRequest(f"Save {address}'s shift first, then their start date.")
            activity_store.set_start(conn, address, started)
            return next(t for t in activity_report.shifts_view(conn, today=today_of(conn))["techs"]
                        if t["email"] == address)

    @app.get("/api/time-off")
    def get_time_off(request: Request) -> dict:
        with _connection(db_path) as conn:
            visible = _scope(request, conn)
            return {"items": [activity_report.time_off_json(i) for i in activity_store.time_off(conn)
                              if visible is None or i.email in visible]}

    @app.post("/api/time-off", status_code=201)
    def add_time_off(request: Request, body: Any = Body(...)) -> dict:
        email, first, last, note, kind = _time_off_request(body)
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, email)
            # Anyone the section lists: a tech who has left can still have a past sick day marked as time off.
            address = _listed_tech(conn, email, required=True)
            new_id = activity_store.add_time_off(conn, address, first, last, note, kind)
        return {"id": new_id, "email": address, "first": first.isoformat(), "last": last.isoformat(), "note": note,
                "kind": kind}

    @app.patch("/api/time-off/{item_id}")
    def edit_time_off(request: Request, item_id: int, body: Any = Body(...)) -> dict:
        """Change any of an entry's `first`, `last`, `note` and `kind` (Time off's Edit); what isn't sent stays.
        Returns the entry."""
        if not isinstance(body, dict):
            raise _BadRequest("Send the changes as an object.")
        if not {"first", "last", "note", "kind"} & set(body):
            raise _BadRequest("Nothing to change.")
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, _entry_tech(conn, item_id))
            entry = next((i for i in activity_store.time_off(conn) if i.id == item_id), None)
            if entry is None:
                return _error(404, f"There is no time off numbered {item_id}.")
            first = _strict_day(body["first"], "first day") if "first" in body else entry.first
            last = _strict_day(body["last"], "last day") if "last" in body else entry.last
            if last < first:
                raise _BadRequest("The first day must be on or before the last day.")
            note = body.get("note", entry.note)
            if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
                raise _BadRequest("The note must be text.")
            kind = _time_off_kind(body["kind"]) if "kind" in body else entry.kind
            activity_store.edit_time_off(conn, item_id, first, last, note.strip(), kind)
            return activity_report.time_off_json(next(i for i in activity_store.time_off(conn) if i.id == item_id))

    @app.delete("/api/time-off/day")
    def remove_time_off_day(request: Request, email: str | None = None, day: str | None = None) -> Response:
        """Take one day out of the tech's time off (shortening or splitting an entry that spans more days)."""
        chosen = _iso_day(day, "day")
        if not email or not email.strip():
            raise _BadRequest("Choose a tech.")
        if chosen is None:
            raise _BadRequest("Choose a day.")
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, email)
            if not activity_store.remove_time_off_day(conn, email.strip().lower(), chosen):
                return _error(404, f"{email.strip().lower()} has no time off on {chosen.isoformat()}.")
        return Response(status_code=204)

    @app.delete("/api/time-off/{item_id}")
    def remove_time_off(request: Request, item_id: int) -> Response:
        with _connection(db_path) as conn:
            _not_manager(request, conn)
            _scope(request, conn, _entry_tech(conn, item_id))
            if not any(i.id == item_id for i in activity_store.time_off(conn)):
                return _error(404, f"There is no time off numbered {item_id}.")
            activity_store.remove_time_off(conn, item_id)
        return Response(status_code=204)

    # -- people (the hosted edition; the full edition answers 404, HOSTED_PATHS) ------------------------------

    def people_body(conn: sqlite3.Connection, visible: list[str] | None = None) -> dict:
        """Everyone who can use the app (the admin first, with their own team), with their typed names, the techs on
        each one's team and, on each manager's row, the regions they picked (`regions`); and the whole roster, by name,
        with each tech's leads, first tracked day and regions (`{email, name, leads, started, region, regions}`;
        `leads` is empty for a tech on no team, `region` is their current region and `regions` their region history,
        `[{since, region}]` oldest first, empty for a tech never set). The admin reads it whole: the Techs panel and
        the Edit techs picker are the only places the whole roster shows (hosted Team design §3). A manager reads what
        they run (Managers design), `visible` being their regions' techs (from `_scope`): the roster of those techs,
        and the admin and the team leads with any of them, each with only those, and the team leads with no techs yet;
        never a manager."""
        team = settings.team_emails(conn)
        if visible is not None:
            team = [email for email in team if email in visible]
        current = regions.regions_of(conn, team, today_of(conn))

        def started(email: str) -> str | None:
            versions = activity_store.shift_versions(conn, email)
            return versions[0].effective_from.isoformat() if versions else None

        def history(email: str) -> list[dict]:
            return [{"since": since.isoformat(), "region": region}
                    for since, region in regions.region_history(conn, email)]

        everyone = [{**p, "regions": people.manager_regions(conn, p["email"])} if p["role"] == "manager" else p
                    for p in people.everyone(conn)]
        if visible is not None:
            seen = set(visible)
            everyone = [{**p, "techs": [t for t in p["techs"] if t in seen]} for p in everyone
                        if p["role"] != "manager" and seen.intersection(p["techs"])
                        or p["role"] == "tl" and not p["techs"]]
        return {"people": everyone,
                "roster": [{"email": t["email"], "name": t["name"], "leads": people.leads_of(conn, t["email"]),
                            "started": started(t["email"]), "region": current[t["email"]],
                            "regions": history(t["email"])}
                           for t in activity_report.techs(conn) if t["email"] in team]}

    def _people_for(request: Request, conn: sqlite3.Connection) -> dict:
        """The People answer for who asks (`people_body`): the admin's whole, a manager's for their regions."""
        return people_body(conn, _scope(request, conn) if _role(request, conn) == "manager" else None)

    def _assigned(conn: sqlite3.Connection, lead: str) -> list[str]:
        """The techs assigned to `lead`, as stored (`people.everyone`)."""
        return next((p["techs"] for p in people.everyone(conn) if p["email"] == lead), [])

    def _not_added(email: str) -> JSONResponse:
        return _error(404, f"{email} hasn't been added.")

    @app.get("/api/people")
    def get_people(request: Request) -> dict:
        """The People answer (`people_body`): the admin's and, limited to their regions, a manager's."""
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn)
            return _people_for(request, conn)

    @app.post("/api/people")
    def post_person(request: Request, body: Any = Body(...)) -> dict:
        """Add someone by name (`{name, role}`, their email made from it; `{name, email, role}` once the admin
        corrected it), or change the role, and a given name, of someone already added (`{email, role}`), dropping what
        their old role held (`people.add_person`). No Zendesk check: a TL needn't be an agent. The admin can't change
        their own role. A manager adds team leads only (ADD_LEADS_ONLY), so changes nobody's role: an email already
        added as the admin or a manager is refused. They also need adding to the Cloudflare Access policy, which the
        page says. The answer is the People answer for who asks."""
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn)
            email, role, name = _person_request(body, settings.email_domain(conn))
            if email == request.state.email and role != people.role(conn, email):
                raise _BadRequest("You can't change your own role.")
            if _role(request, conn) == "manager":
                if role != "tl":
                    raise _BadRequest(ADD_LEADS_ONLY)
                if people.role(conn, email) not in (None, "tl"):
                    raise _BadRequest(f"{email} is already added, as someone other than a team lead.")
            people.add_person(conn, email, role, name)
            return _people_for(request, conn)

    @app.delete("/api/people/{email}")
    def delete_person(request: Request, email: str) -> Any:
        """Take someone off (their next request gets "not added"), with their assigned techs and a manager's regions.
        Not the admin themselves. A manager takes off team leads only (REMOVE_LEADS_ONLY), and only one whose techs
        are all in the manager's regions (LEADS_OUTSIDE otherwise), so no team outside their view goes with them."""
        address = email.strip().lower()
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn)
            if address == request.state.email:
                raise _BadRequest("You can't remove yourself.")
            if _role(request, conn) == "manager":
                kind = people.role(conn, address)
                if kind is None:
                    return _not_added(address)
                if kind != "tl":
                    raise _BadRequest(REMOVE_LEADS_ONLY)
                if not set(_assigned(conn, address)) <= set(_scope(request, conn)):
                    raise _BadRequest(LEADS_OUTSIDE)
            if not people.remove_person(conn, address):
                return _not_added(address)
            return _people_for(request, conn)

    @app.put("/api/people/{email}/techs")
    def put_person_techs(request: Request, email: str, body: Any = Body(...)) -> Any:
        """Replace a person's whole team (`{techs: [...]}`, techs on the roster), the admin's own included: each person
        sees only their team. A tech may be on more than one team. A manager holds no team (CANT_LEAD). A manager's
        Edit techs (Review Focus 2) sends the lead's techs in the manager's regions, and only those change: the lead's
        techs elsewhere stay, and a tech outside the manager's regions sent is 404, with nothing changed."""
        address = email.strip().lower()
        with _connection(db_path) as conn:
            _admin_or_manager(request, conn)
            kind = people.role(conn, address)
            if kind is None:
                return _not_added(address)
            if kind == "manager":
                raise _BadRequest(people.CANT_LEAD)
            chosen = _techs_request(body, settings.team_emails(conn))
            if _role(request, conn) == "manager":
                visible = set(_scope(request, conn))
                if not set(chosen) <= visible:
                    raise _NotFound()
                chosen = [tech for tech in _assigned(conn, address) if tech not in visible] + chosen
            people.set_assignments(conn, address, chosen)
            return _people_for(request, conn)

    @app.put("/api/people/{email}/regions")
    def put_person_regions(request: Request, email: str, body: Any = Body(...)) -> Any:
        """The admin sets a manager's regions (`{regions: [...]}`), as Edit techs sets a lead's team; the manager can
        still change their own in Your regions (`PUT /api/me/regions`; the owner, 2026-10-22: both). Only a manager
        has regions."""
        address = email.strip().lower()
        with _connection(db_path) as conn:
            _admin_only(request, conn)
            kind = people.role(conn, address)
            if kind is None:
                return _not_added(address)
            if kind != "manager":
                raise _BadRequest(ONLY_MANAGERS_HAVE_REGIONS)
            picked = body.get("regions") if isinstance(body, dict) else None
            if not isinstance(picked, list):
                raise _BadRequest("Send the regions as a list.")
            try:
                people.set_manager_regions(conn, address, picked)
            except ValueError as refused:
                raise _BadRequest(str(refused)) from None
            return _people_for(request, conn)

    @app.get("/api/status")
    def status() -> dict:
        """The banner's state. `last_run` is the latest run other than a model test, so a test never hides a
        failed daily run's alert; a test waiting for Confirm or with an unknown submission is still listed.
        Each listed run carries its `kind` and `model` (and `model_label`), so the banner can name a test."""
        def listed(conn: sqlite3.Connection, status: str) -> list[dict]:
            return [{"run_id": r["run_id"], "kind": r["kind"], "model": r["model"] or DEFAULT_MODEL,
                     "model_label": queries.model_label(r["model"] or DEFAULT_MODEL)}
                    for r in conn.execute("SELECT run_id, kind, model FROM runs WHERE status = ? ORDER BY run_id",
                                          (status,))]

        with _connection(db_path) as conn:
            last = conn.execute(
                "SELECT run_id, status, error, finished_at FROM runs WHERE kind != ? ORDER BY run_id DESC LIMIT 1",
                (pipeline.TEST_KIND,)).fetchone()
            awaiting = listed(conn, "awaiting_confirmation")
            unknown = listed(conn, "submission_unknown")
        last_run = None
        if last is not None:
            last_run = {
                "run_id": last["run_id"], "status": last["status"], "error": last["error"],
                "error_label": failure_label(last["error"]), "finished_at": last["finished_at"],
                "alert": last["status"] == "failed" and last["error"] != pipeline.CLEARED_BY_OWNER,
            }
        return {
            "last_run": last_run,
            "awaiting_confirmation": awaiting,
            "submission_unknown": [{**r, "message": pipeline.SUBMISSION_UNRECORDED} for r in unknown],
        }

    return app
