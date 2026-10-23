"""Default rubric content: the 16-item scorecard, the seven process rules, and reference documents."""
from __future__ import annotations

from importlib import resources

from work_assistant.models import DEFAULT_REFERENCE, CheckDef

_ALWAYS = "Always"
_NOT_RESOLVED = "Only when the issue was not resolved on the call"
_RESOLVED_UNKNOWN_CAUSE = "Only when the issue was resolved on the call but the root cause is unknown"
_RESOLVED = "Only when the issue was resolved on the call"

# (number, summary, definition, points, applies_when)
_SCORECARD: tuple[tuple[int, str, str, int, str], ...] = (
    (
        1,
        "Opening: Identify yourself & the company",
        '"Thank you for calling [COMPANY NAME] Technical Support, this is [YOUR NAME]…"',
        5,
        _ALWAYS,
    ),
    (
        2,
        "Opening: Verify or collect the caller's contact information in Zendesk",
        "Based on the information in Zendesk (first & last name, phone number, email address), "
        "ask the caller to verify it. If no contact exists, collect name, phone, and email and "
        "document them in the ticket. How it must be done: the agent asks the customer to spell "
        "their name and email address, then reads each back using phonetic spelling to confirm it "
        '("M as in Mike, A as in Alpha…"). Reading the letters back plainly ("M, A, R…") does not '
        "count. Any clear word-per-letter phonetic spelling counts, NATO alphabet or not. Phone "
        "numbers must be confirmed, and confirming the last 4 digits is enough.",
        5,
        _ALWAYS,
    ),
    (
        3,
        "Opening: Identify the location the caller is contacting us about",
        '"What location can I assist you with today?"',
        5,
        _ALWAYS,
    ),
    (
        4,
        "Triage: Request what the question, issue, or problem is",
        'Ask "How can I help you today with [THE LOCATION NAME]?" Gather basic details (error '
        "messages, devices involved, etc.) and document them in the ticket.",
        5,
        _ALWAYS,
    ),
    (
        5,
        "Triage: Ask appropriate probing follow-up questions",
        "Ask for specifics such as when the issue began, how many devices are affected, or whether "
        "there were recent changes at the location.",
        10,
        _ALWAYS,
    ),
    (
        6,
        "Triage: Verify understanding",
        "Confirm understanding of the problem, including summarizing it back to the caller.",
        5,
        _ALWAYS,
    ),
    (
        7,
        "Triage: Determine the correct priority",
        "Use the priority definitions (Impact and Urgency) to set the correct priority, and update "
        "the ticket.",
        5,
        _ALWAYS,
    ),
    (
        8,
        "Resolution: Identify accurate steps to resolve the issue",
        "Use available resources (agent knowledge, existing tickets for the same problem, Help "
        "Centre articles, peer knowledge, internal documentation).",
        10,
        _ALWAYS,
    ),
    (
        9,
        "Resolution: Take ownership and assure the customer",
        'Demonstrate ownership. Example: "I understand how important this issue is and will work '
        'with you to resolve it as quickly as possible."',
        10,
        _ALWAYS,
    ),
    (
        10,
        "Resolution: Provide clear direction",
        "Verify the customer understands and can follow the directions given.",
        10,
        _ALWAYS,
    ),
    (
        11,
        "Escalation: Unable to resolve",
        "If the agent can't resolve the issue, escalate to the next level/tier and set clear "
        "expectations with the customer.",
        5,
        _NOT_RESOLVED,
    ),
    (
        12,
        "Escalation: Unable to identify root cause",
        "If resolved but root cause unknown, escalate for further research and root cause analysis.",
        5,
        _RESOLVED_UNKNOWN_CAUSE,
    ),
    (
        13,
        "Confirmation: Confirm the problem has been resolved",
        "Have the customer repeat the action (e.g. a test transaction) and confirm the original "
        "problem is resolved.",
        5,
        _RESOLVED,
    ),
    (
        14,
        "Closing: Ask if you can assist with anything else",
        '"Is there anything else I can assist you with today?" If yes, create a separate ticket '
        "for each additional issue.",
        5,
        _ALWAYS,
    ),
    (
        15,
        "Closing: Remind the customer what comes next",
        "Thank the caller, and remind them they'll receive a summary email and, once the ticket is "
        "resolved, a survey link.",
        5,
        _ALWAYS,
    ),
    (
        16,
        "Professionalism",
        "Clear, accurate, professional responses with proper grammar, spelling, and a friendly "
        "tone, including the ticket subject line and any public reply (the summary email). "
        "The summary email must also be accurate: the issue, equipment, and next steps it "
        "states must match what was actually discussed on the call (per the transcript and "
        "call summary).",
        5,
        _ALWAYS,
    ),
)

_PROCESS_RULES: tuple[CheckDef, ...] = (
    CheckDef(
        check_id="pr-triage-macro",
        kind="process",
        number=None,
        summary="Triage macro completed",
        definition=(
            "Every reviewed call has the triage macro, with required fields filled. Default "
            "required fields: Location, Customer Name, Callback Number, Customer Email, "
            "Station/Product Affected, Devices/Software Affected, Symptoms, When Did It Begin?, "
            "Priority. A field left blank still counts as met if an attached image clearly "
            "provides that information (for example, an error screenshot covering Symptoms). "
            "Only the triage macro format counts. Other internal-note layouts (for example "
            '"Incident details / Troubleshooting steps / Solution") don\'t satisfy this rule. '
            "Any reviewed call without a triage macro is a miss."
        ),
        points=0,
        auto_fail=False,
        applies_when=_ALWAYS,
        enabled=True,
    ),
    CheckDef(
        check_id="pr-call-length",
        kind="process",
        number=None,
        summary="10-minute rule",
        definition=(
            "For any priority other than Urgent or Site Down, the call itself must last no more "
            "than 10 minutes. The measure is the call's length, not the time until the summary "
            "email goes out. For Urgent or Site Down issues, the agent is expected to stay on the "
            "line. Ending the call early on a critical issue and moving it to email is a miss. "
            "The threshold (10) and the exempt priorities are configurable. "
            "This rule doesn't apply to dropped calls: when a call disconnects before the issue is "
            "handled, only the callback is checked (see the dropped-call rule)."
        ),
        points=0,
        auto_fail=False,
        applies_when="Always, except dropped calls",
        enabled=True,
        timed="call_length",
    ),
    CheckDef(
        check_id="pr-scope",
        kind="process",
        number=None,
        summary="Stayed in scope",
        definition=(
            "The agent didn't support a product on the unsupported products list or take an "
            "action on the not-authorized list. If the customer asked for either, the agent "
            "followed the redirect procedure (pointing the customer to self-service resources). "
            "Both lists and the procedure are reference material the owner maintains in the app."
        ),
        points=0,
        auto_fail=False,
        applies_when=_ALWAYS,
        enabled=True,
    ),
    CheckDef(
        check_id="pr-email-sent",
        kind="process",
        number=None,
        summary="Summary email sent",
        definition=(
            "After the call, the agent sends the customer a summary email (a public reply on the "
            "ticket). A missing email is a miss. Default deadline: within 30 minutes of the call "
            "ending, configurable."
        ),
        points=0,
        auto_fail=False,
        applies_when=_ALWAYS,
        enabled=True,
        timed="email_deadline",
    ),
    CheckDef(
        check_id="pr-email-qualities",
        kind="process",
        number=None,
        summary="Summary email has the required qualities",
        definition=(
            "The email doesn't need to match the template's exact shape or wording, but it must "
            "have these qualities:\n"
            "- It greets the customer.\n"
            "- It summarizes what was discussed: the issue, the affected equipment or software, "
            "and what was already tried.\n"
            "- It states the next steps: what happens now, and who does it.\n"
            "- It invites the customer to reply, and references the ticket number.\n"
            "- It closes with the agent's sign-off.\n"
            "The reviewer judges substance, not format. Any missing quality is a miss, and the "
            "evidence names which ones. The standard footer is not required."
        ),
        points=0,
        auto_fail=False,
        applies_when=_ALWAYS,
        enabled=True,
    ),
    CheckDef(
        check_id="pr-dropped-callback",
        kind="process",
        number=None,
        summary="Dropped call: called back right away",
        definition=(
            "The agent must call the customer back on the same ticket right after the disconnect, "
            "within 5 minutes of the drop. The app measures the gap from call end to the next "
            "outbound call on the ticket, and gives it to the reviewer as a fact. The callback is a "
            "continuation of the same contact: later scorecard steps (confirming the fix, "
            '"anything else?", what comes next) can be completed on it. No callback, a late '
            "callback, or a callback on a different ticket is a miss."
        ),
        points=0,
        auto_fail=False,
        applies_when="Only when the transcript shows the call disconnected before the issue was handled",
        enabled=True,
    ),
    CheckDef(
        check_id="pr-merge-correct",
        kind="process",
        number=None,
        summary="Merged into the correct ticket",
        definition=(
            "A merge is how agents handle a call about an issue that already has an existing open "
            "ticket. It's correct when the target was an existing open ticket for the same location "
            "(Location ID) and the same issue. The caller can be a different person at that "
            "location. The reviewer gets the target ticket's number, subject, location, creation "
            "time, and its comments from this call's start through 24 hours after the call ends. "
            "When the merge is correct, the triage macro and summary email rules are judged on the "
            "merged-into ticket's comments, since that's where agents document after merging. A "
            "wrong merge is a miss. Merges and dropped calls are separate situations. A dropped "
            "call stays on its own ticket (see the dropped-call rule)."
        ),
        points=0,
        auto_fail=False,
        applies_when="Only when this call's ticket was merged into another ticket",
        enabled=True,
    ),
)


def default_checks() -> tuple[CheckDef, ...]:
    """The 16 scorecard items followed by the seven process rules."""
    scorecard = tuple(
        CheckDef(
            check_id=f"sc{number:02d}",
            kind="scorecard",
            number=number,
            summary=summary,
            definition=definition,
            points=points,
            auto_fail=False,
            applies_when=applies_when,
            enabled=True,
        )
        for number, summary, definition, points, applies_when in _SCORECARD
    )
    return scorecard + _PROCESS_RULES


def default_reference() -> dict[str, str]:
    """Seed reference documents, keyed by title, read from the packaged seed_reference folder."""
    folder = resources.files("work_assistant") / "seed_reference"
    return {
        title: (folder / filename).read_text(encoding="utf-8").strip()
        for title, filename in DEFAULT_REFERENCE.items()
    }
