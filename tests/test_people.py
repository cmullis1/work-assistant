"""People, roles and assignments for the hosted copy (hosted Team activity design §4, plan Task 3): the admin from
the server config, team leads (TLs) added by the admin, which roster techs each TL sees, and the tables that hold
them, created by the migration and left empty on the owner's PC. Managers (Managers design): the regions each picks,
and every tech in them they see, whoever's team the tech is on."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from work_assistant import people, regions, settings
from work_assistant.db import connect, init_db

ADMIN, LEE, KIM = "owner@example.com", "lee.lead@example.com", "kim.lead@example.com"
AVERY, BLAKE, CASEY = "avery@example.com", "blake@example.com", "casey@example.com"
DREW, ELLIS = "drew@example.com", "ellis@example.com"
MANAGER, OTHER_MANAGER = "m@x.com", "n@x.com"
JAN_1 = date(2026, 1, 1)
CANT_LEAD = "A manager can't lead a team."


@pytest.fixture
def roster(conn):
    settings.set_setting(conn, "team", [AVERY, BLAKE, CASEY])
    return conn


def test_tables_are_created_and_empty(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"people", "assignments", "manager_regions"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM manager_regions").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO people (email, role, added_at) VALUES ('x@example.com', 'boss', '2026-10-20')")
    conn.execute("INSERT INTO people (email, role, added_at) VALUES ('x@example.com', 'manager', '2026-10-20')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO manager_regions (email, region) VALUES ('x@example.com', 'Mars')")


def test_an_older_database_gains_the_tables(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect(path)
    init_db(conn)
    assert people.everyone(conn) == []
    conn.close()


def test_ensure_admin_adds_the_config_admin_once(conn):
    people.ensure_admin(conn, " Owner@Example.com ")
    people.ensure_admin(conn, ADMIN)
    assert people.role(conn, ADMIN) == "admin"
    assert people.role(conn, "OWNER@example.com") == "admin"
    assert people.everyone(conn) == [{"email": ADMIN, "role": "admin", "name": None, "techs": []}]


def test_ensure_admin_makes_a_tl_the_admin(conn):
    people.add_person(conn, ADMIN, "tl")
    people.ensure_admin(conn, ADMIN)
    assert people.role(conn, ADMIN) == "admin"


def test_role_of_someone_unknown_is_none(conn):
    assert people.role(conn, LEE) is None
    assert people.role(conn, "") is None


def test_add_person_checks_the_email_and_role(conn):
    with pytest.raises(ValueError):
        people.add_person(conn, "not an email", "tl")
    with pytest.raises(ValueError):
        people.add_person(conn, LEE, "boss")
    people.add_person(conn, " Lee.Lead@Example.com", "tl")
    assert people.role(conn, LEE) == "tl"
    people.add_person(conn, LEE, "admin")
    assert people.role(conn, LEE) == "admin", "adding someone again changes their role"
    people.add_person(conn, MANAGER, "manager", "Pat Lane")
    assert people.role(conn, MANAGER) == "manager"


def test_visible_techs(roster):
    conn = roster
    people.ensure_admin(conn, ADMIN)
    people.add_person(conn, LEE, "tl")
    people.set_assignments(conn, LEE, [CASEY, " Avery@Example.com", AVERY])
    assert people.visible_techs(conn, ADMIN) == [], "the admin sees their own team, like any lead: none yet"
    people.set_assignments(conn, ADMIN, [BLAKE])
    assert people.visible_techs(conn, ADMIN) == [BLAKE]
    assert people.visible_techs(conn, None) is None, "no sign-in (the PC copy): everyone"
    assert people.visible_techs(conn, LEE) == [AVERY, CASEY], "in roster order, once each"
    assert people.visible_techs(conn, KIM) == [], "someone who wasn't added sees nobody"
    assert people.visible_techs(conn, "") == []


def test_a_tl_sees_only_assigned_techs_still_on_the_roster(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    settings.set_setting(conn, "team", [BLAKE, CASEY])
    assert people.visible_techs(conn, LEE) == [BLAKE]
    settings.set_setting(conn, "team", [AVERY, BLAKE, CASEY])
    assert people.visible_techs(conn, LEE) == [AVERY, BLAKE], "a tech who comes back is theirs again"


def test_a_tech_can_be_assigned_to_two_tls(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.set_assignments(conn, KIM, [BLAKE, CASEY])
    assert people.visible_techs(conn, LEE) == [AVERY, BLAKE]
    assert people.visible_techs(conn, KIM) == [BLAKE, CASEY]


def test_set_assignments_replaces_the_tls_techs(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.set_assignments(conn, LEE, [CASEY])
    assert people.visible_techs(conn, LEE) == [CASEY]
    people.set_assignments(conn, LEE, [])
    assert people.visible_techs(conn, LEE) == []


def test_remove_person_drops_their_assignments(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, LEE, [AVERY])
    people.set_assignments(conn, KIM, [AVERY])
    assert people.remove_person(conn, " LEE.lead@example.com") is True
    assert people.role(conn, LEE) is None
    assert [tuple(r) for r in conn.execute("SELECT tl_email, tech_email FROM assignments")] == [(KIM, AVERY)]
    assert people.remove_person(conn, LEE) is False, "nobody left to remove"


def test_everyone_lists_the_admin_first_with_each_persons_techs(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.add_person(conn, KIM, "tl")
    people.ensure_admin(conn, ADMIN)
    people.set_assignments(conn, LEE, [BLAKE, AVERY])
    assert people.everyone(conn) == [
        {"email": ADMIN, "role": "admin", "name": None, "techs": []},
        {"email": KIM, "role": "tl", "name": None, "techs": []},
        {"email": LEE, "role": "tl", "name": None, "techs": [AVERY, BLAKE]},
    ]


def test_leads_of_names_everyone_whose_team_holds_the_tech(roster):
    conn = roster
    people.ensure_admin(conn, ADMIN)
    people.add_person(conn, LEE, "tl")
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.set_assignments(conn, KIM, [BLAKE])
    people.set_assignments(conn, ADMIN, [BLAKE])
    assert people.leads_of(conn, " Blake@Example.com") == [ADMIN, KIM, LEE], "the admin first, then by email"
    assert people.leads_of(conn, AVERY) == [LEE]
    assert people.leads_of(conn, CASEY) == [], "a tech on no team"


def test_assign_waits_for_the_callers_commit(roster):
    conn = roster
    people.add_person(conn, LEE, "tl")
    people.assign(conn, " Lee.Lead@Example.com", "Casey@Example.com")
    conn.rollback()
    assert people.leads_of(conn, CASEY) == [], "nothing is kept until the caller commits"
    people.assign(conn, LEE, CASEY)
    people.assign(conn, LEE, CASEY)
    conn.commit()
    assert people.leads_of(conn, CASEY) == [LEE]


# -- managers ---------------------------------------------------------------------------------------------------

@pytest.fixture
def regioned(conn):
    """A roster (not in email order) across all three regions and several teams: Casey APAC on Kim's team, Avery NASA
    and Blake EMEA on Lee's, Ellis APAC on the admin's, Drew with no region (NASA) on no team; and a manager."""
    settings.set_setting(conn, "team", [CASEY, AVERY, BLAKE, ELLIS, DREW])
    for tech, region in ((AVERY, "NASA"), (BLAKE, "EMEA"), (CASEY, "APAC"), (ELLIS, "APAC")):
        regions.set_regions(conn, tech, [(JAN_1, region)])
    people.ensure_admin(conn, ADMIN)
    people.add_person(conn, LEE, "tl")
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.set_assignments(conn, KIM, [CASEY])
    people.set_assignments(conn, ADMIN, [ELLIS])
    people.add_person(conn, MANAGER, "manager", "Pat Lane")
    return conn


def test_manager_sees_their_regions_techs(regioned):
    conn = regioned
    assert people.manager_regions(conn, MANAGER) == []
    assert people.visible_techs(conn, MANAGER) == [], "a manager with no region picked sees no techs"
    people.set_manager_regions(conn, " M@X.com ", ["APAC", "NASA", "APAC"])
    assert people.manager_regions(conn, MANAGER) == ["NASA", "APAC"], "in regions.ORDER, once each"
    assert people.visible_techs(conn, MANAGER) == [CASEY, AVERY, ELLIS, DREW], \
        "every tech in their regions, whoever's team, in roster order; a tech with no region is NASA"
    assert people.visible_techs(conn, LEE) == [AVERY, BLAKE], "a team lead still sees only their own team"
    people.set_manager_regions(conn, MANAGER, ["EMEA"])
    assert people.visible_techs(conn, MANAGER) == [BLAKE], "a save replaces the regions picked before"
    people.set_manager_regions(conn, MANAGER, [])
    assert people.visible_techs(conn, MANAGER) == []


def test_a_region_that_isnt_one_is_refused(regioned):
    conn = regioned
    people.set_manager_regions(conn, MANAGER, ["NASA"])
    for picked in (["NASA", "Mars"], ["nasa"], [["NASA"]], [None]):
        with pytest.raises(ValueError) as refused:
            people.set_manager_regions(conn, MANAGER, picked)
        assert str(refused.value) == regions.NOT_A_REGION
    assert people.manager_regions(conn, MANAGER) == ["NASA"], "nothing changed"


def test_region_move_changes_what_a_manager_sees(regioned):
    conn = regioned
    people.set_manager_regions(conn, MANAGER, ["NASA"])
    assert BLAKE not in people.visible_techs(conn, MANAGER)
    today = regions.app_today(conn)
    regions.set_regions(conn, BLAKE, [(JAN_1, "EMEA"), (today - timedelta(days=1), "NASA")])
    assert people.visible_techs(conn, MANAGER) == [AVERY, BLAKE, DREW], "a tech moved into their region joins it"
    regions.set_regions(conn, AVERY, [(JAN_1, "NASA"), (today, "APAC")])
    assert people.visible_techs(conn, MANAGER) == [BLAKE, DREW], "a tech moved out of it leaves it"
    regions.set_regions(conn, CASEY, [(JAN_1, "APAC"), (today + timedelta(days=30), "NASA")])
    assert CASEY not in people.visible_techs(conn, MANAGER), "a move dated later isn't current yet"


def test_manager_cant_lead(regioned):
    conn = regioned
    before = sorted(tuple(r) for r in conn.execute("SELECT tl_email, tech_email FROM assignments"))
    for attempt in (lambda: people.set_assignments(conn, " M@X.com", [AVERY]),
                    lambda: people.set_assignments(conn, MANAGER, []),
                    lambda: people.set_leads(conn, AVERY, [LEE, MANAGER]),
                    lambda: people.assign(conn, MANAGER, DREW)):
        with pytest.raises(ValueError) as refused:
            attempt()
        assert str(refused.value) == CANT_LEAD
    conn.commit()
    assert sorted(tuple(r) for r in conn.execute("SELECT tl_email, tech_email FROM assignments")) == before
    assert people.leads_of(conn, AVERY) == [LEE], "the refused set_leads changed nothing"


def test_removing_a_manager_drops_their_regions(regioned):
    conn = regioned
    people.add_person(conn, OTHER_MANAGER, "manager")
    people.set_manager_regions(conn, MANAGER, ["NASA", "EMEA"])
    people.set_manager_regions(conn, OTHER_MANAGER, ["APAC"])
    assert people.remove_person(conn, " M@X.com") is True
    assert people.role(conn, MANAGER) is None
    assert [tuple(r) for r in conn.execute("SELECT email, region FROM manager_regions")] == [(OTHER_MANAGER, "APAC")]
    people.add_person(conn, MANAGER, "manager")
    assert people.manager_regions(conn, MANAGER) == [], "added again, they pick again"
    assert people.visible_techs(conn, MANAGER) == []


def test_a_new_role_drops_what_the_old_one_held(regioned):
    """Giving someone a new role takes what their old one held in the same change: a team lead made a manager loses
    their team (managers hold no team), and a manager made a team lead (or the admin) loses their regions. Given their
    role again, nothing is dropped."""
    conn = regioned
    people.set_manager_regions(conn, MANAGER, ["NASA"])
    people.add_person(conn, LEE, "manager")
    assert people.leads_of(conn, AVERY) == [] and people.leads_of(conn, BLAKE) == []
    assert people.manager_regions(conn, MANAGER) == ["NASA"], "another manager keeps theirs"
    people.add_person(conn, MANAGER, "tl", "Pat Lane")
    assert people.manager_regions(conn, MANAGER) == []
    people.set_manager_regions(conn, LEE, ["APAC"])
    people.add_person(conn, LEE, "manager", "Lee Park")
    assert people.manager_regions(conn, LEE) == ["APAC"], "the same role again keeps what it holds"
    people.add_person(conn, KIM, "tl")
    assert people.leads_of(conn, CASEY) == [KIM]
