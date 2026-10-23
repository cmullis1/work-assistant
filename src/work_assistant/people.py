"""Who can use the hosted copy (hosted Team activity design §4): the admin, named in the server config, and the team
leads (TLs) and managers the admin adds, each by the email they sign in with and the name typed when they were added
(names.py); which roster techs each lead (the admin too) has on their team; the regions each manager picked; and what
that lets each person see: a lead their own team only (hosted Team design §3), a manager every tech whose current
region is one they picked, whoever's team the tech is on (Managers design). Managers hold no team.

The tables (`people`, `assignments`, `manager_regions`) are created by `db.init_db` everywhere but used only by the
Team activity edition, so on the owner's PC they stay empty. Emails are stored stripped and lowercased, as the sign-in
check and the roster give them.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from work_assistant import names, regions, settings

ROLES = ("admin", "tl", "manager")
CANT_LEAD = "A manager can't lead a team."


def _email(raw: str) -> str:
    return raw.strip().lower()


def ensure_admin(conn: sqlite3.Connection, email: str) -> None:
    """Make `email` (the server config's admin) the admin, adding them if they aren't in the list yet."""
    _put(conn, _email(email), "admin")


def add_person(conn: sqlite3.Connection, email: str, role: str, name: str | None = None) -> None:
    """Add someone with `role` ("admin", "tl" or "manager") and their typed `name`, or give someone already added that
    role (and that name, when one is given; without one they keep theirs), dropping what their old role held (`_put`).
    ValueError for an email that isn't one, or another role."""
    if role not in ROLES:
        raise ValueError("The role must be admin, tl or manager.")
    _put(conn, settings.normalize_email(email), role, names.clean(name) if name else None)


def _put(conn: sqlite3.Connection, email: str, role: str, name: str | None = None) -> None:
    """Give `email` `role`, in one change with dropping what another role held: made a manager, the techs on their team
    (managers hold no team); made anything else, the regions they picked as a manager (made one again, they pick
    again)."""
    added_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        if role == "manager":
            conn.execute("DELETE FROM assignments WHERE tl_email = ?", (email,))
        else:
            conn.execute("DELETE FROM manager_regions WHERE email = ?", (email,))
        conn.execute("INSERT INTO people (email, role, added_at, name) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(email) DO UPDATE SET role = excluded.role,"
                     " name = COALESCE(excluded.name, people.name)", (email, role, added_at, name or None))


def remove_person(conn: sqlite3.Connection, email: str) -> bool:
    """Take someone off the list, with the techs they were assigned and the regions they picked (added again, a manager
    picks again). Returns whether they were on it."""
    email = _email(email)
    removed = conn.execute("DELETE FROM people WHERE email = ?", (email,)).rowcount
    conn.execute("DELETE FROM assignments WHERE tl_email = ?", (email,))
    conn.execute("DELETE FROM manager_regions WHERE email = ?", (email,))
    conn.commit()
    return removed > 0


def _no_manager(conn: sqlite3.Connection, leads: list[str]) -> None:
    """ValueError (CANT_LEAD) when any of `leads` is a manager: managers hold no team."""
    if any(role(conn, lead) == "manager" for lead in leads):
        raise ValueError(CANT_LEAD)


def set_assignments(conn: sqlite3.Connection, tl_email: str, techs: list[str]) -> None:
    """Replace the techs assigned to `tl_email` with `techs`. ValueError (CANT_LEAD), with nothing changed, for a
    manager."""
    tl_email = _email(tl_email)
    _no_manager(conn, [tl_email])
    with conn:
        conn.execute("DELETE FROM assignments WHERE tl_email = ?", (tl_email,))
        conn.executemany("INSERT OR IGNORE INTO assignments (tl_email, tech_email) VALUES (?, ?)",
                         [(tl_email, _email(tech)) for tech in techs])


def set_leads(conn: sqlite3.Connection, tech: str, leads: list[str]) -> None:
    """Replace the people whose team holds `tech` with `leads` (empty: on no team). ValueError (CANT_LEAD), with
    nothing changed, when one of them is a manager."""
    tech = _email(tech)
    _no_manager(conn, leads)
    with conn:
        conn.execute("DELETE FROM assignments WHERE tech_email = ?", (tech,))
        conn.executemany("INSERT OR IGNORE INTO assignments (tl_email, tech_email) VALUES (?, ?)",
                         [(_email(lead), tech) for lead in leads])


def assign(conn: sqlite3.Connection, tl_email: str, tech: str) -> None:
    """Put `tech` on `tl_email`'s team as well (a tech already there stays once). It doesn't commit: the caller commits
    it with the team change that adds the tech, so a new tech joins the roster and their lead's team as one change.
    ValueError (CANT_LEAD), with nothing changed, for a manager."""
    _no_manager(conn, [tl_email])
    conn.execute("INSERT OR IGNORE INTO assignments (tl_email, tech_email) VALUES (?, ?)",
                 (_email(tl_email), _email(tech)))


def drop_techs(conn: sqlite3.Connection, techs: list[str]) -> None:
    """Take techs who left the roster off every TL's assignments, so a tech added back later is seen by nobody until
    the admin ticks them again. It doesn't commit: the caller commits it with the team change that removes them, so
    the two are one change."""
    conn.executemany("DELETE FROM assignments WHERE tech_email = ?", [(_email(tech),) for tech in techs])


def _assigned(conn: sqlite3.Connection, tl_email: str) -> list[str]:
    return [r["tech_email"] for r in conn.execute(
        "SELECT tech_email FROM assignments WHERE tl_email = ? ORDER BY tech_email", (tl_email,))]


def role(conn: sqlite3.Connection, email: str | None) -> str | None:
    """The person's role ("admin", "tl" or "manager"), or None for someone who hasn't been added."""
    if not email:
        return None
    row = conn.execute("SELECT role FROM people WHERE email = ?", (_email(email),)).fetchone()
    return row["role"] if row is not None else None


def visible_techs(conn: sqlite3.Connection, email: str | None) -> list[str] | None:
    """The techs `email` may see and change: None for everyone only when there is no sign-in at all (the PC copy);
    otherwise their own team, the techs assigned to them that are on the roster (`settings.team_emails`), in roster
    order. The admin's team works the same way as a lead's (hosted Team design §3): every lead sees only their own
    team. A manager sees every roster tech whose current region (`regions.regions_of`) is one they picked, in roster
    order: none with no region picked. Someone who hasn't been added sees nobody."""
    if email is None:
        return None
    kind = role(conn, email)
    if kind is None:
        return []
    if kind == "manager":
        picked = set(manager_regions(conn, email))
        roster = settings.team_emails(conn)
        current = regions.regions_of(conn, roster)
        return [tech for tech in roster if current[tech] in picked]
    assigned = set(_assigned(conn, _email(email)))
    return [tech for tech in settings.team_emails(conn) if tech in assigned]


def manager_regions(conn: sqlite3.Connection, email: str) -> list[str]:
    """The regions the manager picked to see, in `regions.ORDER` (empty: none picked)."""
    picked = {r["region"] for r in conn.execute("SELECT region FROM manager_regions WHERE email = ?", (_email(email),))}
    return [region for region in regions.ORDER if region in picked]


def set_manager_regions(conn: sqlite3.Connection, email: str, picked: list[str]) -> None:
    """Make `picked` the regions the manager sees, in place of any before (empty: none, and so no techs). ValueError
    (`regions.NOT_A_REGION`), with nothing changed, for one that isn't a region."""
    if not all(isinstance(region, str) and region in regions.REGIONS for region in picked):
        raise ValueError(regions.NOT_A_REGION)
    email = _email(email)
    with conn:
        conn.execute("DELETE FROM manager_regions WHERE email = ?", (email,))
        conn.executemany("INSERT OR IGNORE INTO manager_regions (email, region) VALUES (?, ?)",
                         [(email, region) for region in picked])


def leads_of(conn: sqlite3.Connection, tech: str) -> list[str]:
    """The emails of everyone whose team holds `tech`: the admin first, then by email. Empty for a tech on no team."""
    return [r["email"] for r in conn.execute(
        "SELECT p.email, p.role FROM assignments a JOIN people p ON p.email = a.tl_email WHERE a.tech_email = ?"
        " ORDER BY p.role = 'admin' DESC, p.email", (_email(tech),))]


def comparison_team(conn: sqlite3.Connection, viewer: str | None, tech: str) -> str | None:
    """The lead whose team `tech` is compared with in Trends when `viewer` picks them (Regions design, "Team average in
    Trends"): a team lead's own team; the admin's own team when the tech is on it, else the team of the tech's lead, the
    first by the lead's name (the one typed, else their email; in any case) when there are several. None on the PC (no
    sign-in), and for a tech on no team: they are compared with everyone the viewer sees."""
    if viewer is None:
        return None
    viewer, leads = _email(viewer), leads_of(conn, tech)
    kind = role(conn, viewer)
    if kind is None or not leads:
        return None
    if kind == "tl" or viewer in leads:
        return viewer
    names = {r["email"]: r["name"] or r["email"] for r in conn.execute("SELECT email, name FROM people")}
    return min(leads, key=lambda lead: (names[lead].lower(), lead))


def everyone(conn: sqlite3.Connection) -> list[dict]:
    """Every person as `{email, role, name, techs}`: the admin first, then by email; `name` is the one typed when
    they were added (None for none), and `techs` are the assigned techs by email, whether or not they are still on the
    roster."""
    rows = conn.execute("SELECT email, role, name FROM people ORDER BY role = 'admin' DESC, email").fetchall()
    return [{"email": r["email"], "role": r["role"], "name": r["name"], "techs": _assigned(conn, r["email"])}
            for r in rows]
