"""People added by name (hosted Team activity design §4, "Adding people by name", plan Task 7): the work email made
from a typed name, the company's email domain setting, where the typed names are kept, and that every place showing
a tech by name shows the typed one first, then the name on their newest reviewed call, then the email."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from test_compare import add_shown, add_test_run, add_tested
from test_queries import add_review, all_met, version  # noqa: F401  (fixture)
from test_web_overview import STATIC, js_function, run_node
from work_assistant import names, people, periods, settings, transfer
from work_assistant.activity import report
from work_assistant.db import connect, init_db
from work_assistant.web import compare, queries

DOMAIN = "example.com"


# -- making the email ---------------------------------------------------------------------------------------


def test_email_for_plain_name():
    assert names.email_for("Desmond Salazar", DOMAIN) == "desmond.salazar@example.com"
    assert names.email_for("  desmond   SALAZAR ", DOMAIN) == "desmond.salazar@example.com"
    assert names.email_for("Desmond Salazar", "example.com") == "desmond.salazar@example.com"


def test_email_for_accents_apostrophes_hyphens_middle():
    assert names.email_for("José O'Brien-Lee", DOMAIN) == "jose.obrien-lee@example.com"
    assert names.email_for("Mary Ann Smith", DOMAIN) == "mary.smith@example.com", "middle words are left out"
    assert names.email_for("Zoë d’Arcy", DOMAIN) == "zoe.darcy@example.com", "a curly apostrophe goes too"
    assert names.email_for("J. R. Smith", DOMAIN) == "j.smith@example.com", "periods are removed"
    assert names.email_for("Anne-Marie St. Clair", DOMAIN) == "anne-marie.clair@example.com"
    assert names.email_for("Ñuñez Çelik", DOMAIN) == "nunez.celik@example.com"


@pytest.mark.parametrize("name", ["Cher", "", "   ", "O' .", "Desmond ."])
def test_email_for_single_word_refused(name):
    with pytest.raises(ValueError) as refused:
        names.email_for(name, DOMAIN)
    assert str(refused.value) == names.TWO_WORDS == "Enter their first and last name."


def test_the_page_makes_the_same_email_as_the_server():
    """The People panel shows the made email before saving, so app.js makes it too (`emailFor`); both must agree.
    For a name of one word the page has none to show."""
    typed = ["Desmond Salazar", "José O'Brien-Lee", "Mary Ann Smith", "Zoë d’Arcy", "J. R. Smith",
             "  Anne-Marie   St. Clair ", "Ñuñez Çelik", "Cher", "", "O' ."]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    made = run_node(js_function(js, "emailFor") + f"\nprocess.stdout.write(JSON.stringify({json.dumps(typed)}"
                    f".map(function (n) {{ return emailFor(n, {json.dumps(DOMAIN)}); }})));")

    def server(name):
        try:
            return names.email_for(name, DOMAIN)
        except ValueError:
            return ""

    assert made == [server(name) for name in typed]


def test_the_page_makes_no_email_without_a_domain():
    """With no domain known (settings.email_domain None) the page makes no email, and the email fields ask for one."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    out = run_node(js_function(js, "emailFor") + js_function(js, "emailPlaceholder") + "\nprocess.stdout.write("
                   "JSON.stringify([emailFor('Des Salazar', null), emailPlaceholder(null), emailPlaceholder('x.example')]));")
    assert out == ["", "Work email", "firstname.lastname@x.example"]


def test_clean_keeps_the_name_as_typed_with_single_spaces():
    assert names.clean("  Desmond   Salazar ") == "Desmond Salazar"
    assert names.clean("José O'Brien-Lee") == "José O'Brien-Lee"


# -- the company's domain ------------------------------------------------------------------------------------


def test_email_domain_setting_has_no_default_and_is_cleaned(conn):
    assert settings.get_setting(conn, "email_domain") is None, "the app has no built-in domain"
    cleaned, problems = settings.validate_changes({"email_domain": " @Example.CO.uk "})
    assert cleaned == {"email_domain": "example.co.uk"} and problems == {}
    for blank in ("", "  ", None):
        assert settings.validate_changes({"email_domain": blank}) == ({"email_domain": None}, {}),             "blank saves nothing, so the domain in use is worked out again"
    for bad in ("example", "ex ample.com", "a@b.com", 7, "-x.com"):
        cleaned, problems = settings.validate_changes({"email_domain": bad})
        assert problems == {"email_domain": settings.EMAIL_DOMAIN_PROBLEM}, bad


def test_email_domain_in_use_is_the_saved_one_else_the_one_its_people_have(conn):
    assert settings.email_domain(conn) is None, "nothing to go on"
    settings.set_setting(conn, "zendesk_email", " Owner@Login.Example ")
    assert settings.email_domain(conn) == "login.example", "the Zendesk login's, with no team or people"
    settings.set_setting(conn, "team", ["a@team.example", "b@other.example", "c@team.example"])
    assert settings.email_domain(conn) == "team.example", "the one most of the team has"
    people.add_person(conn, "x@other.example", "tl")
    assert settings.email_domain(conn) == "team.example", "a tie goes to the team's first"
    people.add_person(conn, "y@other.example", "tl")
    assert settings.email_domain(conn) == "other.example", "the people count too (the hosted copy's leads)"
    settings.set_setting(conn, "email_domain", "saved.example")
    assert settings.email_domain(conn) == "saved.example", "a saved domain always wins"


# -- where the names are kept --------------------------------------------------------------------------------


def test_store_keeps_one_name_per_tech(conn):
    names.store(conn, " Desmond.Salazar@Example.com ", "  Desmond  Salazar ")
    names.store(conn, "desmond.salazar@example.com", "Des Salazar")
    assert names.stored(conn) == {"desmond.salazar@example.com": "Des Salazar"}


def test_people_gain_a_name_column_on_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE people (email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','tl')), "
                "added_at TEXT NOT NULL)")
    old.execute("INSERT INTO people VALUES ('owner@example.com', 'admin', '2026-10-20T00:00:00Z')")
    old.commit()
    old.close()
    conn = connect(path)
    try:
        init_db(conn)
        init_db(conn)
        assert people.everyone(conn) == [{"email": "owner@example.com", "role": "admin", "name": None, "techs": []}]
        people.add_person(conn, "pat.lead@example.com", "tl", name="  Pat   Lead ")
        people.add_person(conn, "pat.lead@example.com", "tl")
        assert people.everyone(conn)[1]["name"] == "Pat Lead", "a change without a name keeps the one stored"
        people.add_person(conn, "pat.lead@example.com", "tl", name="Patricia Lead")
        assert people.everyone(conn)[1]["name"] == "Patricia Lead"
    finally:
        conn.close()


def test_typed_names_travel_with_the_export_and_tl_names_stay(tmp_path):
    """A tech's typed name goes over with the PC's Team activity; the hosted copy's people, and their names, are
    its own and the import keeps them."""
    pc = connect(tmp_path / "pc.db")
    init_db(pc)
    settings.set_setting(pc, "team", ["desmond.salazar@example.com"])
    names.store(pc, "desmond.salazar@example.com", "Des Salazar")
    transfer.export_activity(pc, tmp_path / "moving.db")
    pc.close()
    vm = connect(tmp_path / "vm.db")
    try:
        init_db(vm)
        people.add_person(vm, "pat.lead@example.com", "tl", name="Pat Lead")
        transfer.import_activity(vm, tmp_path / "moving.db", admin_email="owner@example.com")
        assert names.stored(vm) == {"desmond.salazar@example.com": "Des Salazar"}
        assert {p["email"]: p["name"] for p in people.everyone(vm)} == {
            "owner@example.com": None, "pat.lead@example.com": "Pat Lead"}
    finally:
        vm.close()


# -- the typed name comes first, everywhere a tech is shown by name ----------------------------------------------

DES = ("desmond.salazar@example.com", "Desmond Salázar (Zendesk)")   # the name on his reviewed calls
SAM = ("sam@example.com", "Sam Ortiz")                               # never typed: his calls' name


def test_stored_name_first_then_the_calls_then_the_email(conn, version):  # noqa: F811
    settings.set_setting(conn, "team", [DES[0], SAM[0], "lee@example.com"])
    dan_call = add_shown(conn, version, 1, all_met(version), agent=DES)
    add_review(conn, version, 2, all_met(version), agent=SAM)
    names.store(conn, DES[0], "Des Salazar")
    shown = {DES[0]: "Des Salazar", SAM[0]: "Sam Ortiz"}

    assert {t["email"]: t["name"] for t in report.techs(conn)} == {**shown, "lee@example.com":
                                                                    "lee@example.com"}, "Team activity"
    assert {a["email"]: a["name"] for a in queries.agent_list(conn)} == {**shown, "lee@example.com": None}, \
        "Techs (the page shows the email for a tech with no name)"
    assert [a["has_history"] for a in queries.agent_list(conn) if a["email"] == "lee@example.com"] == [False]
    week = periods.resolve("custom", start=date(2026, 10, 4), end=date(2026, 10, 10), today=date(2026, 10, 17),
                           last_reviewed=date(2026, 10, 10), latest_week_with_data=None)
    overview = queries.overview(conn, week, None, last_reviewed=date(2026, 10, 10))
    assert {a["email"]: a["name"] for a in overview["agents"]} == shown, "Team overview"
    assert {o["email"]: o["name"] for o in overview["agent_options"]} == shown
    listed = queries.tickets(conn, start=None, end=None, agent=None, verdict=None, missed_check=None, priority=None,
                             q="Des Salazar", sort="agent")
    assert {r["agent_email"]: r["agent_name"] for r in listed["rows"]} == {DES[0]: "Des Salazar"}, \
        "Tickets, found by the name shown"
    assert {o["email"]: o["name"] for o in listed["agent_options"]} == shown
    assert queries.agent_detail(conn, DES[0], week, last_reviewed=date(2026, 10, 10))["name"] == "Des Salazar"
    assert queries.agent_detail(conn, SAM[0], week, last_reviewed=date(2026, 10, 10))["name"] == "Sam Ortiz"
    assert queries.ticket_detail(conn, 1)["contact"]["agent_name"] == "Des Salazar"
    assert queries.ticket_detail(conn, 2)["contact"]["agent_name"] == "Sam Ortiz"
    test_run = add_test_run(conn)
    add_tested(conn, test_run, dan_call, all_met(version))
    assert [c["agent"] for c in compare.report(conn, test_run)["calls"]] == ["Des Salazar"], "a model test"
    assert compare.call_detail(conn, test_run, 1)["agent"] == "Des Salazar"
