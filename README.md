# Work Assistant

A browser app for a support team lead. Claude scores the team's support calls against the team's own scorecard, and
each tech's daily activity, trends and 1:1 prep are a click away. I use it every day at work, and a hosted copy of
its Team section serves the other team leads and managers.

![Call QA's team overview: average score, problem tickets, the most-missed checks and each tech's numbers for last week](screenshots/call-qa-overview.png)

*Every screenshot here is from the app's demo mode, which uses invented people and tickets.*

## What it does

**Call QA**
- Each morning it reads yesterday's inbound calls from Zendesk (read-only: it never changes anything there).
- Claude scores each first call against the team's scorecard and process rules and quotes the evidence for every
  check. Measured rules, such as "a call longer than 10 minutes is a miss", are checked from the call's own timing.
- The lead can disagree with any check. The override counts, and the reviewer's own call is kept beside it.
- A budget limit guards every run, the scorecard and rules are edited and versioned in the app, and a "Test a model"
  view compares another Claude model against the shown reviews before switching.
- Team, tech and ticket views, with trends against the period before and an Excel export.

![One call's review: the timeline against the 10-minute limit, then every check with Claude's evidence and a Disagree button](screenshots/call-review.png)

**Team activity**
- What each tech did each day, from Zendesk's own records (replies, internal notes, logged calls, solved tickets and
  sign-ins), set against their shifts, rest days and time off.
- Flags such as a late first action, an early finish or a long gap, measured against limits the lead sets.

![Team activity for one week: tickets touched per tech per day, rest days, a sick day, time off and flags](screenshots/team-activity.png)

**Trends**
- Eighteen measures (output, reliability, time on shift, calls, satisfaction, backlog), each ranked by tech and drawn
  week by week. The lines are pooled averages, so a slow week or a few survey answers don't swing them.

![Trends: tickets per shift ranked by tech, with one tech's 4-week average falling below the team's](screenshots/trends.png)

**1:1 Prep**
- Each tech's numbers for the two weeks before their own 1:1 day, ready to paste into the 1:1 sheet with one click.
- Behind every number, the calls, ratings, aging tickets and days out that make it up, and trend graphs for each
  measure against the team.

![1:1 Prep for one tech: the calls, satisfaction ratings and aging tickets behind the numbers, and a trend graph for each measure](screenshots/one-on-one-trends.png)

**Where it runs**
- On the lead's Windows PC, at `127.0.0.1` only. Call data goes only to a local SQLite file and to Anthropic for the
  review.
- A hosted copy of the Team section (Activity, Trends, 1:1 Prep) runs on an Ubuntu server behind Cloudflare Access
  for the other leads, with admin, team lead and manager roles. The hosted copy never calls Claude.
- On the PC, the Zendesk token and the Anthropic API key are kept in Windows Credential Manager, never in a file.

## Run it

You need Windows and Python 3.12. The demo needs no Zendesk account and no API key:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\work-assistant serve --demo
```

The app opens in your browser with a year of invented history. Add `--demo-date 2026-10-19` to pin the demo to one
day (the screenshots use it), or `--edition hosted` to see the hosted copy as its admin.

Run the tests:

```powershell
.venv\Scripts\python -m pytest
```

To use it for real, `work-assistant setup` asks for a Zendesk API token and an Anthropic API key, and
`work-assistant schedule install` adds the daily review to Windows Task Scheduler. `work-assistant --help` lists
every command.

## How it was built

I'm a support team lead who writes Python and PowerShell. I built Work Assistant with Claude Code: I owned the problem, the design
decisions and the testing, and Claude Code wrote most of the code.

- **The problem.** Call reviews were done by hand, so only a small share of calls was ever checked. Before each 1:1
  I copied a tech's numbers out of a Zendesk report into a spreadsheet by hand, and there was no easy way to see what
  each tech did with their day.
- **The decisions.** What a good call is (the scorecard and rules), what counts as a problem, which numbers a lead
  needs before a 1:1, what data may leave the PC, and how each screen should look, often settled on mockups before
  it was built.
- **The testing.** Before trusting the scores I ran a calibration batch of real calls, compared Claude's judgments
  with mine, and added rules where it misread our standards (a dropped call called straight back, a ticket merged into
  the right one). The project has over 2,400 automated tests, and each screen has screenshot checks at my two monitor
  sizes that I look at and accept before a change is done.
- **Real use.** I use it every day, and the hosted copy serves the other team leads and managers.

About 18,000 lines of Python and 12,000 of JavaScript and CSS, with no front-end framework or build step.

## License

MIT. See [LICENSE](LICENSE).
