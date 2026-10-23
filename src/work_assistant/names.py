"""People added by name (hosted Team activity design §4, "Adding people by name"). Every work email is
firstname.lastname at the company's domain (the `email_domain` setting), so the admin types a tech's or a team
lead's name and the app makes their email from it (`email_for`). The typed name is then the name the app shows for
them.

A tech's typed name is kept in `tech_names` (email, name), which the export carries to the hosted copy
(transfer.py); a team lead's in `people.name` (people.py), which stays on the hosted copy. Wherever the app shows a
tech by name (Team activity, the People panel's roster, Call QA's screens), the typed name comes first (`stored`),
then the name on their newest reviewed call, then the email.
"""
from __future__ import annotations

import sqlite3
import unicodedata

TWO_WORDS = "Enter their first and last name."


def clean(name: str) -> str:
    """A typed name as it is kept and shown: trimmed, with one space between words."""
    return " ".join(name.split())


def email_for(name: str, domain: str) -> str:
    """The work email for `name` at `domain`: lowercased, accents dropped (NFKD, then ASCII only), apostrophes and
    periods removed, hyphens kept, and the first and last words joined with "."; middle words are left out.
    ValueError (TWO_WORDS) when fewer than two words are left. app.js's `emailFor` makes the same email."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    words = plain.replace("'", "").replace(".", "").split()
    if len(words) < 2:
        raise ValueError(TWO_WORDS)
    return f"{words[0]}.{words[-1]}@{domain}"


def store(conn: sqlite3.Connection, email: str, name: str) -> None:
    """Keep `name` as the tech's name, in place of any kept before."""
    conn.execute("INSERT INTO tech_names (email, name) VALUES (?, ?) "
                 "ON CONFLICT(email) DO UPDATE SET name = excluded.name", (email.strip().lower(), clean(name)))
    conn.commit()


def stored(conn: sqlite3.Connection) -> dict[str, str]:
    """Each tech's typed name, by lowercased email."""
    return {row["email"]: row["name"]
            for row in conn.execute("SELECT lower(trim(email)) AS email, name FROM tech_names") if row["name"]}
