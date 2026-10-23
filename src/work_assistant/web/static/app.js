/* Work Assistant page logic: hash routing, the status banner, the Team overview, the Agents screen, the
   Tickets screen, the Rubric & rules screen, Team activity, 1:1 prep, and the Settings screen with its model test
   views. Markup is built with class names only; every color and size lives in app.css. */
(function () {
  "use strict";

  var TABS = {
    overview: "Team overview",
    agents: "Techs",
    tickets: "Tickets",
    rubric: "Rubric & rules",
    team: "Activity",
    trends: "Trends",
    oneonone: "1:1 Prep",
    settings: "Settings"
  };
  // The side-panel section each screen is listed under (data-section on its parent link) and the crumb the top
  // bar shows before its name. Settings is a screen of its own, outside any section. 1:1 prep is a screen under Team,
  // beside Activity and Trends, with a switch of its own on the PC (`tabHidden`).
  var SECTION_OF = { overview: "callqa", agents: "callqa", tickets: "callqa", rubric: "callqa", team: "team", trends: "team",
    oneonone: "team" };
  var CRUMBS = { callqa: "Call QA ›", team: "Team ›" };
  var STATUS_LABELS = {
    fetching: "Fetching calls",
    awaiting_confirmation: "Waiting for your OK",
    submitting: "Submitting",
    submitted: "Reviewing",
    submission_unknown: "Needs a check",
    complete: "Complete",
    failed: "Failed",
    cancelled: "Cancelled"
  };
  var KIND_LABELS = { scheduled: "Daily", manual: "Run now", rereview: "Re-review", test: "Model test" };
  var SCHEDULE_FALLBACK = { installed: false, text: "Not installed yet" };  // when the status can't be read
  var REMOVE_TEXT = "Their past results stay in the reports; new calls won't be reviewed.";
  var ADD_HINT = "New techs are reviewed from the next daily run. To include past weeks, use Run now for a " +
    "past range. Calls already reviewed are skipped, so only this tech's calls are reviewed and billed.";
  var STATUS_POLL_MS = 30000;

  var screen = document.getElementById("screen");
  var banners = document.getElementById("banners");
  var loadingBar = document.getElementById("loading-bar");
  var renderToken = 0;
  var screenDrawn = false;      // a screen's content has been drawn (not just its first "Loading…" line)
  var shownHash = location.hash;   // the route on screen, to return to when leaving is cancelled
  var skipRoute = false;
  var showCallQa = true;        // the "Show Call QA" setting; off shows only Team and Settings
  var showTeam = true;          // the "Show Team activity" setting (the PC copy); off drops Activity and Trends
  var showOneonone = true;      // the "Show 1:1 Prep" setting (the PC copy); off drops 1:1 Prep
  var syncOn = true;            // the "Sync Team activity" setting (the PC copy); off hides Refresh
  var sectionsKnown = false;    // the setting has been read: nothing routes before that, so nothing flashes
  var edition = "full";         // the server's edition; "hosted" (the copy on the server) has no Call QA at all
  var role = "admin";           // who is signed in to the hosted copy: "admin", "tl" (a team lead: their techs only) or
                                // "manager" (every tech in the regions they pick)
  var myEmail = null;           // the email signed in to the hosted copy (null on the PC, which has no sign-in)
  var myRegions = [];           // a manager's picked regions, in the regions' order (/api/me); [] for anyone else
  var allRegions = [];          // every region with the city whose time it keeps, {region, city} (/api/me)
  // A manager's people (/api/people: the admin and the team leads with a tech in their regions, each with those
  // techs), which their Tech pickers' teams and Activity's Teams panel are drawn from: read at start, and again with
  // Settings' people (after Save regions, or a roster change). Null for anyone else.
  var teamBook = null;

  // -- helpers ----------------------------------------------------------------------------------

  function h(tag, attrs) {
    var el = document.createElement(tag);
    var key;
    attrs = attrs || {};
    for (key in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, key) || attrs[key] == null || attrs[key] === false) {
        continue;
      }
      var value = attrs[key];
      if (key === "class") {
        el.className = value;
      } else if (key === "text") {
        el.textContent = value;
      } else if (key.slice(0, 2) === "on" && typeof value === "function") {
        el.addEventListener(key.slice(2), value);
      } else if (key === "hidden" || key === "disabled" || key === "checked") {
        el[key] = Boolean(value);
      } else {
        el.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (var i = 2; i < arguments.length; i++) {
      append(el, arguments[i]);
    }
    return el;
  }

  function append(el, child) {
    if (child == null || child === false) {
      return;
    }
    if (Array.isArray(child)) {
      child.forEach(function (c) { append(el, c); });
    } else if (child instanceof Node) {
      el.appendChild(child);
    } else {
      el.appendChild(document.createTextNode(String(child)));
    }
  }

  /* A request to the app. A write (anything but GET) forgets every kept answer as it starts and again when it ends,
     so the next view reads fresh: a save is never hidden behind an answer read before it. */
  function api(method, url, body) {
    var options = { method: method, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    var answered = fetchJson(url, options);
    if (method === "GET") {
      return answered;
    }
    forgetKept();
    return answered.then(function (data) {
      forgetKept();
      return data;
    }, function (error) {
      forgetKept();
      throw error;
    });
  }

  /* `url` answered as JSON; a refusal is an Error saying why, with its status and body. */
  function fetchJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          // A FastAPI validation error (422) has a list in `detail`: never show "[object Object]". The hosted copy
          // refuses someone it doesn't know (yet) with `not_added`, on every read: the page says so wherever it is.
          var text = data.error === "not_added" ? notAddedText(data)
            : typeof data.detail === "string" && data.detail ? data.detail
            : (data.detail != null ? "Something went wrong with that request."
              : "The app returned an error (" + response.status + ").");
          var error = new Error(text);
          error.status = response.status;
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  }

  /* What the hosted copy shows someone Cloudflare let in but the admin hasn't added. */
  function notAddedText(data) {
    return "You're signed in as " + data.email + ", but you haven't been added yet. Ask " + data.admin +
      " to add you.";
  }

  /* Whether the person may change the roster, the settings and the people: on the PC everyone is the admin. */
  function isAdmin() {
    return role === "admin";
  }

  /* Whether the person is a manager (Managers design): they run the roster in the regions they pick and add team
     leads, but hold no team, and shifts, time off and 1:1 dates are each team lead's, which the server refuses them. */
  function isManager() {
    return role === "manager";
  }

  function money(value) {
    return value == null ? "-" : "$" + Number(value).toFixed(2);
  }

  function localDate(iso) {
    var parts = iso.split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function dayLabel(iso, withYear) {
    var options = { month: "short", day: "numeric" };
    if (withYear) {
      options.year = "numeric";
    }
    return localDate(iso).toLocaleDateString(undefined, options);
  }

  function rangeLabel(first, last) {
    if (first === last) {
      return dayLabel(first, true);
    }
    return dayLabel(first, false) + " – " + dayLabel(last, true);
  }

  function timeLabel(isoUtc) {
    if (!isoUtc) {
      return "-";
    }
    return new Date(isoUtc).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    });
  }

  function isoDay(date) {
    var m = String(date.getMonth() + 1).padStart(2, "0");
    var d = String(date.getDate()).padStart(2, "0");
    return date.getFullYear() + "-" + m + "-" + d;
  }

  function lastWeek() {
    var today = new Date();
    var sunday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - today.getDay() - 7);
    var saturday = new Date(sunday.getFullYear(), sunday.getMonth(), sunday.getDate() + 6);
    return { from: isoDay(sunday), to: isoDay(saturday) };
  }

  /* The work email made from a typed name at `domain`, as the server makes it (names.email_for): lowercased,
     accents dropped, apostrophes and periods removed, hyphens kept, and the first and last words joined with ".";
     "" when fewer than two words are left or no domain is known. */
  function emailFor(name, domain) {
    var words = name.normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase().replace(/['.]/g, "")
      .split(/\s+/).filter(Boolean);
    return words.length < 2 || !domain ? "" : words[0] + "." + words[words.length - 1] + "@" + domain;
  }

  /* An email field's placeholder: the made email's shape at `domain`, or just "Work email" when none is known. */
  function emailPlaceholder(domain) {
    return domain ? "firstname.lastname@" + domain : "Work email";
  }

  /* `text` ending as a sentence does. */
  function sentence(text) {
    return /[.!?]$/.test(text) ? text : text + ".";
  }

  /* Enter in any of `inputs` does what `act` does (an Add button's click). */
  function onEnter(inputs, act) {
    inputs.forEach(function (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          act();
        }
      });
    });
  }

  function setMessage(el, text, kind) {
    el.textContent = text || "";
    el.className = "message" + (kind ? " " + kind : "");
  }

  /* A panel: its title, the line under it (`sub`: text, or text and elements such as a link), then its parts. */
  function panel(title, sub) {
    var children = Array.prototype.slice.call(arguments, 2);
    return h("section", { class: "panel" }, h("h4", { class: "panel-label", text: title }), sub ? h("div", { class: "sub" }, sub) : null,
      children);
  }

  /* An email that may break onto a second line just after its "@" (a wbr, which copies as nothing), so a long
     address shown under a name never widens its column. */
  function breakableEmail(email) {
    var at = email.indexOf("@");
    return at < 0 ? email : [email.slice(0, at + 1), h("wbr"), email.slice(at + 1)];
  }

  // -- kept answers and the loading look --------------------------------------------------------
  //
  // Each screen's answers are kept while the app is open, by request URL, as the JSON text the server sent (the owner,
  // 2026-10-21). Returning to a screen, or to filters seen before, draws the kept answers at once, then reads them
  // again in the background and draws again, in place, only when an answer changed. Every write forgets them all
  // (api), and so does a sync that finishes. While a screen waits for an answer it doesn't have, what is on screen
  // stays, faded under the loading bar; only a screen with nothing drawn yet says "Loading…".

  var kept = {};          // request URL -> the JSON text of its last answer
  var keptEpoch = 0;      // moves on each time the kept answers are forgotten

  function forgetKept() {
    kept = {};
    keptEpoch += 1;
  }

  function isKept(url) {
    return Object.prototype.hasOwnProperty.call(kept, url);
  }

  function keep(url, data) {
    kept[url] = JSON.stringify(data);
  }

  /* Read `url` (by `read`, a GET unless given) and keep its answer, unless the kept answers were forgotten while it
     was on its way: it may have been read before a write. */
  function getKept(url, read) {
    var epoch = keptEpoch;
    return (read || readJson)(url).then(function (data) {
      if (epoch === keptEpoch) {
        keep(url, data);
      }
      return data;
    });
  }

  function readJson(url) {
    return api("GET", url);
  }

  /* Read `urls` for what is on screen: `draw(answers, again)` gets them in the order of `urls`, first from the kept
     answers when every one is kept (just after this returns, before the page is painted), then from the server when
     its answers come, if any differs from what was drawn (`again` is then true), or if nothing was kept. `current()`
     says whether what asked is still shown; nothing is drawn once it isn't. `read` reads one URL (a GET unless given).
     The promise fails only when the read failed with nothing drawn. */
  function readKept(urls, current, draw, read) {
    var before = urls.map(function (url) { return kept[url]; });
    var inHand = before.every(function (text) { return text !== undefined; });
    if (inHand) {
      Promise.resolve().then(function () {
        if (current()) {
          draw(before.map(function (text) { return JSON.parse(text); }), false);
        }
      });
    }
    return Promise.all(urls.map(function (url) { return getKept(url, read); })).then(function (answers) {
      var same = inHand && answers.every(function (data, i) { return JSON.stringify(data) === before[i]; });
      if (!same && current()) {
        draw(answers, inHand);
      }
    }, function (error) {
      if (!inHand) {
        throw error;
      }
    });
  }

  /* readKept for a whole screen, drawn under `token` (route's renderToken). Drawing it again from a changed answer
     takes a new token, so whatever the earlier drawing started (a poll, a detail being read) stops; `draw(answers,
     token)` gets the token it draws under. */
  function readScreen(token, urls, draw, read) {
    return readKept(urls, function () { return token === renderToken; }, function (answers, again) {
      if (again) {
        renderToken += 1;
        token = renderToken;
      }
      draw(answers, token);
    }, read);
  }

  /* Before a screen is drawn again: what is on screen stays, faded (.stale) under the loading bar, until the new
     answer replaces it (clearScreen). A screen with nothing drawn yet is emptied for its "Loading…" line. */
  function markLoading() {
    if (screenDrawn) {
      screen.classList.toggle("stale", true);
      loadingBar.hidden = false;
    } else {
      screen.textContent = "";
    }
  }

  /* End the loading look, keeping what is on screen. */
  function endLoading() {
    screen.classList.toggle("stale", false);
    loadingBar.hidden = true;
  }

  /* Empty the screen for what is drawn now, and end the loading look. */
  function clearScreen() {
    screen.textContent = "";
    endLoading();
    screenDrawn = true;
  }

  /* A screen's "Loading…" line, shown only while nothing has been drawn yet: after that, the screen being replaced
     stays on show, faded, instead. */
  function loadingLine(text) {
    if (!screenDrawn) {
      screen.appendChild(h("div", { class: "message", text: text }));
    }
  }

  // -- routing ----------------------------------------------------------------------------------

  function parseRoute() {
    var hash = location.hash.replace(/^#\/?/, "");
    var split = hash.split("?");
    var parts = split[0].split("/");
    var tab = parts[0] || "overview";
    if (tab === "techs") {
      tab = "agents";   // an alias: the Techs screen is the one at #/agents
    }
    var rest = parts.slice(1).filter(Boolean).map(decodeURIComponent);
    if (tab === "team" && rest[0] === "trends") {
      tab = "trends";   // Trends lives under Team activity: #/team/trends
      rest = rest.slice(1);
    } else if (tab === "trends" || !Object.prototype.hasOwnProperty.call(TABS, tab)) {
      tab = "overview";
    }
    return {
      tab: tab,
      rest: rest,
      query: new URLSearchParams(split[1] || "")
    };
  }

  /* The hosted edition (the copy on the server): Call QA doesn't exist there, so its links, banners, Settings
     parts and the Show Call QA switch are never shown, whatever the setting says. */
  function hostedOnly() {
    return edition === "hosted";
  }

  /* Take the Call QA links out of the side panel for good (the hosted edition). */
  function dropCallQaLinks() {
    document.querySelectorAll(".sidenav a").forEach(function (link) {
      var tab = link.getAttribute("data-tab");
      if (link.getAttribute("data-section") === "callqa" || (tab !== null && SECTION_OF[tab] === "callqa")) {
        link.remove();
      }
    });
  }

  /* Show or hide one section's links in the side panel ("callqa" or "team"). Display only: nothing behind them
     changes. */
  function setSectionLinks(section, on) {
    document.querySelectorAll(".sidenav a").forEach(function (link) {
      var tab = link.getAttribute("data-tab");
      if (link.getAttribute("data-section") === section || (tab !== null && SECTION_OF[tab] === section)) {
        link.hidden = !on;
      }
    });
  }

  function setSections(on) {
    showCallQa = on;
    setSectionLinks("callqa", on);
  }

  /* The "Show Team activity" switch (the PC copy): Activity and Trends. The hosted edition always shows the
     whole Team section, 1:1 prep included, to everyone: each person's prep is their own team's. */
  function setTeamSection(on) {
    showTeam = on || hostedOnly();
    setTeamLinks();
  }

  /* The "Show 1:1 Prep" switch (the PC copy, off once the owner uses the hosted copy's 1:1 prep): 1:1 Prep alone. */
  function setOneononeSection(on) {
    showOneonone = on || hostedOnly();
    setTeamLinks();
  }

  /* The Team links as the two switches have them: Activity and Trends, 1:1 Prep, and the Team heading while either is
     shown, leading to the first of them. */
  function setTeamLinks() {
    document.querySelectorAll(".sidenav a").forEach(function (link) {
      var tab = link.getAttribute("data-tab");
      if (tab === "oneonone") {
        link.hidden = !showOneonone;
      } else if (tab !== null && SECTION_OF[tab] === "team") {
        link.hidden = !showTeam;
      } else if (link.getAttribute("data-section") === "team") {
        link.hidden = !(showTeam || showOneonone);
        link.setAttribute("href", showTeam ? "#/team" : "#/oneonone");
      }
    });
  }

  /* Whether a screen is hidden by the Sections switches: Call QA's by Show Call QA, 1:1 prep by Show 1:1 Prep, and
     Activity and Trends by Show Team activity. */
  function tabHidden(tab) {
    if (tab === "oneonone") {
      return !showOneonone;
    }
    return { callqa: !showCallQa, team: !showTeam }[SECTION_OF[tab]] || false;
  }

  /* Where the app opens, and where a hidden screen's address goes: the first section shown, else Settings. */
  function homeHash() {
    return showCallQa ? "#/overview" : (showTeam ? "#/team" : (showOneonone ? "#/oneonone" : "#/settings"));
  }

  function route() {
    if (skipRoute) {
      skipRoute = false;
      return;
    }
    if (!sectionsKnown) {
      return;
    }
    if (tabHidden(parseRoute().tab)) {
      history.replaceState(null, "", homeHash());   // a hidden section's addresses open the home, and Back doesn't bounce
    } else if (!showCallQa && parseRoute().tab === "settings" && parseRoute().rest[0] === "tests") {
      history.replaceState(null, "", "#/settings");   // a model test belongs to Call QA
    }
    if (location.hash !== shownHash && rubricDirty() &&
        !window.confirm("You have unsaved rubric changes. Leave without saving them?")) {
      skipRoute = true;
      location.hash = shownHash;
      return;
    }
    shownHash = location.hash;
    var current = parseRoute();
    document.querySelectorAll(".sidenav a").forEach(function (link) {
      if (link.getAttribute("data-tab") === null) {
        return;   // the Call QA parent link is lit by `open`, below
      }
      var on = link.getAttribute("data-tab") === current.tab;
      link.classList.toggle("on", on);
      if (on) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
    var section = SECTION_OF[current.tab] || null;
    var crumb = section ? CRUMBS[section] : null;
    document.querySelectorAll(".sidenav a.parent").forEach(function (link) {
      link.classList.toggle("open", link.getAttribute("data-section") === section);
    });
    var title = document.querySelector(".appbar .title");
    title.textContent = "";
    if (crumb) {
      title.appendChild(h("span", { class: "crumb", text: crumb }));
    }
    title.appendChild(document.createTextNode((crumb ? " " : "") + TABS[current.tab]));
    document.title = (crumb ? crumb + " " : "") + TABS[current.tab] + " · Work Assistant";
    setMenuOpen(false);
    if (current.tab !== "trends") {
      trendsHighlight = null;   // a tech lit in Trends' graph is forgotten on leaving it
    }
    if (current.tab === "tickets" && ticketsReusable(current.query)) {
      selectTicket(current.query.get("ticket"));  // same list, another call: keep the list and its scroll
      return;
    }
    renderToken += 1;
    markLoading();
    rb = null;
    if (current.tab === "settings" && current.rest[0] === "tests") {
      renderTestView(renderToken, current.rest[1], current.rest[2]);   // a model test, under the Settings tab
    } else if (current.tab === "settings") {
      renderSettings(renderToken);
    } else if (current.tab === "overview") {
      renderOverview(renderToken, current.query);
    } else if (current.tab === "agents") {
      renderAgents(renderToken, current.rest[0], current.query);
    } else if (current.tab === "tickets") {
      renderTickets(renderToken, current.query);
    } else if (current.tab === "team") {
      renderActivity(renderToken, current.query);
    } else if (current.tab === "trends") {
      renderTrends(renderToken, current.query);
    } else if (current.tab === "oneonone") {
      renderPrep(renderToken, current.query);
    } else {
      renderRubric(renderToken, current.rest);
    }
    refreshBanners();
  }

  // The side panel collapses behind the top bar's Menu button below 1024px and on touch screens; opening it puts it
  // over the page, dimmed behind it (.nav-scrim).
  function setMenuOpen(open) {
    var sidenav = document.querySelector(".sidenav");
    var button = document.querySelector(".appbar .menu");
    var scrim = document.querySelector(".nav-scrim");
    if (sidenav) {
      sidenav.classList.toggle("open", open);
    }
    if (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (scrim) {
      scrim.hidden = !open;
    }
    document.body.classList.toggle("menu-open", open);
  }

  // Menu opens and closes the side panel; a tap on the dimmed page, a link in the panel, or Escape closes it.
  function wireSidePanel() {
    var button = document.querySelector(".appbar .menu");
    if (button) {
      button.addEventListener("click", function () {
        setMenuOpen(!document.querySelector(".sidenav").classList.contains("open"));
      });
    }
    document.querySelectorAll(".sidenav a").forEach(function (link) {
      link.addEventListener("click", function () { setMenuOpen(false); });
    });
    var scrim = document.querySelector(".nav-scrim");
    if (scrim) {
      scrim.addEventListener("click", function () { setMenuOpen(false); });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && document.querySelector(".sidenav.open")) {
        setMenuOpen(false);
      }
    });
  }

  // -- banner -----------------------------------------------------------------------------------

  function refreshBanners() {
    if (!showCallQa) {
      banners.textContent = "";   // the review-run and budget banners belong to Call QA
      return Promise.resolve();
    }
    return api("GET", "/api/status").then(renderBanners).catch(function () {
      banners.textContent = "";
      if (!showCallQa) {
        return;
      }
      banners.appendChild(h("div", { class: "banner" },
        h("span", { class: "text", text: "The app could not read the run status. Is it still running?" })));
    });
  }

  function renderBanners(status) {
    banners.textContent = "";
    if (!showCallQa) {
      return;
    }
    if (status.last_run && status.last_run.alert) {
      banners.appendChild(h("div", { class: "banner", role: "alert" },
        h("span", { class: "text" }, "The last run (run " + status.last_run.run_id + ") failed: ",
          h("b", { text: status.last_run.error_label || "no reason recorded" })),
        h("a", { href: "#/settings", text: "Run history" })));
    }
    status.awaiting_confirmation.forEach(function (run) {
      var name = runName(run.run_id, run.kind, true) + (run.kind === "test" ? " (" + run.model_label + ")" : "");
      banners.appendChild(h("div", { class: "banner info" },
        h("span", { class: "text", text: name + " is waiting for your OK: its estimate is over the budget limit." }),
        h("button", { class: "primary", text: "Confirm…", onclick: function () { confirmRun(run.run_id); } }),
        h("button", { text: "Cancel…", onclick: function () { cancelRun(run.run_id, run.kind); } })));
    });
    status.submission_unknown.forEach(function (item) {
      banners.appendChild(h("div", { class: "banner" },
        h("span", { class: "text", text: runName(item.run_id, item.kind, true) + ": " + item.message }),
        h("button", { text: "Clear…", onclick: function () { clearRun(item.run_id, item.kind); } })));
    });
  }

  /* "run 7" or, for a model test, "model test 7"; capitalized to start a sentence. */
  function runName(runId, kind, capital) {
    return (kind === "test" ? (capital ? "Model test " : "model test ") : (capital ? "Run " : "run ")) + runId;
  }

  function findRun(runId) {
    return api("GET", "/api/runs").then(function (data) {
      return data.runs.filter(function (r) { return r.run_id === runId; })[0] || null;
    });
  }

  function confirmRun(runId) {
    Promise.all([findRun(runId), api("GET", "/api/settings")]).then(function (results) {
      var run = results[0];
      var limit = results[1].budget_limit_usd;
      var estimate = run ? money(run.estimate_usd) : "unknown";
      var name = runName(runId, run && run.kind, true);
      var ok = window.confirm(name + "'s estimate is " + estimate + ", over your " + money(limit) +
        " budget limit.\n\nStart it anyway?");
      if (!ok) {
        return null;
      }
      return api("POST", "/api/runs/" + runId + "/confirm").then(function () {
        window.alert(name + " is starting. Its progress shows in the run history.");
        afterRunAction();
      });
    }).catch(function (error) { window.alert(error.message); });
  }

  /* A model test holds no calls, so clearing one releases nothing: its dialog says only what to check. */
  function clearRun(runId, kind) {
    var test = kind === "test";
    var name = runName(runId, kind, false);
    var ok = window.confirm("Only clear " + name + " after checking the Batches page in the Anthropic Console.\n\n" +
      "If a batch for this " + (test ? "test" : "run") + " is there, it is being (or was) paid for." +
      (test ? "" : " Clearing releases its calls so later runs can review them again.") +
      "\n\nClear " + name + "?");
    if (!ok) {
      return;
    }
    api("POST", "/api/runs/" + runId + "/clear").then(afterRunAction)
      .catch(function (error) { window.alert(error.message); });
  }

  /* Cancel a run (or model test) waiting for the OK. Nothing has been sent yet, so nothing is lost. */
  function cancelRun(runId, kind) {
    var ok = window.confirm("Cancel " + runName(runId, kind, false) + "? Nothing has been sent or charged; you can " +
      "start it again any time.");
    if (!ok) {
      return;
    }
    api("POST", "/api/runs/" + runId + "/cancel").then(afterRunAction)
      .catch(function (error) { window.alert(error.message); });
  }

  function afterRunAction() {
    refreshBanners();
    if (parseRoute().tab === "settings") {
      loadRuns();
      loadTests();
    }
  }

  // -- periods (the Team overview's and Agents' period picker) ---------------------------------
  //
  // The address keeps the period and comparison in the API's own parameter names, so the page sends them
  // on as they are, and a bookmark or the back button brings back the same view. Old addresses (`week`,
  // `weeks`, and `compare` as a date) are sent on unchanged too, so they open the same data; the picker
  // then shows them as custom ranges, and the first change writes the new names.

  var PERIOD_CHOICES = [
    { value: "yesterday", text: "Yesterday" },
    { value: "this_week", text: "This week (so far)" },
    { value: "last_week", text: "Last week" },
    { value: "this_month", text: "This month (so far)" },
    { value: "last_month", text: "Last month" },
    { value: "last_8_weeks", text: "Last 8 weeks" },
    { value: "last_6_months", text: "Last 6 months" },
    { value: "last_year", text: "Last year" },
    { value: "custom", text: "Custom range" }
  ];
  var COMPARE_CHOICES = [
    { value: "previous", text: "The period before" },
    { value: "same_days_last_week", text: "Same days last week" },
    { value: "custom", text: "Custom range" },
    { value: "none", text: "Nothing" }
  ];
  var SAME_DAYS_MAX = 7;   // periods.SAME_DAYS_MAX: Same days last week is only for periods this long or shorter
  var SHORT_PRESETS = ["yesterday", "this_week", "last_week"];   // the presets never longer than that
  var FIRST_DAY = "2000-01-01";   // periods.FIRST_DAY and LAST_DAY: the days a custom range may name
  var LAST_DAY = "2099-12-31";
  var NO_CALLS_IN_PERIOD = "No calls in this period";
  var NOTHING_REVIEWED = "Nothing reviewed yet for this period";   // an empty period: the runs haven't reached it
  var NOTHING_TO_COMPARE = "Nothing to compare yet";
  // The API's 400 for Same days last week on a longer period (periods.comparison), the one refusal the
  // overview recovers from by itself.
  var SAME_DAYS_REFUSED = "Same days last week is only for periods of 7 days or less.";

  /* The days from `first` to `last`, both included; 0 for an empty period (it ends the day before it starts). */
  function daysIn(first, last) {
    return Math.round((localDate(last) - localDate(first)) / 86400000) + 1;
  }

  function isEmptyPeriod(period) {
    return period.end < period.start;
  }

  /* What a view without calls says: "Nothing reviewed yet for this period" when the runs haven't reached
     it (an empty period), else `noCalls` (by default "No calls in this period"). */
  function withoutCalls(period, noCalls) {
    return isEmptyPeriod(period) ? NOTHING_REVIEWED : noCalls || NO_CALLS_IN_PERIOD;
  }

  /* Whether `compare` is an old overview address's week date rather than a "Compare to" choice. */
  function isDatedCompare(compare) {
    return !!compare && !COMPARE_CHOICES.some(function (c) { return c.value === compare; });
  }

  /* The address's values for `keys`, null for those it doesn't have. */
  function paramsFrom(query, keys) {
    var params = {};
    keys.forEach(function (key) { params[key] = query.get(key) || null; });
    return params;
  }

  /* Whether the period a choice leads to is 7 days or shorter, before the API has resolved it. This month
     (so far) is counted as longer: it is, except in a month's first week. */
  function shortPeriod(preset, start, end) {
    return preset === "custom" ? daysIn(start, end) <= SAME_DAYS_MAX : SHORT_PRESETS.indexOf(preset) >= 0;
  }

  /* What is wrong with a custom range, in the API's words, or null. `comparison` words it for "Compare to". */
  function rangeProblem(start, end, comparison) {
    if (!start || !end) {
      return comparison ? "Choose the start and end day of the comparison."
        : "Choose the start and end day of the custom range.";
    }
    if (start < FIRST_DAY || end < FIRST_DAY || start > LAST_DAY || end > LAST_DAY) {
      return "Choose dates between 2000 and 2099.";
    }
    if (start > end) {
      return comparison ? "The comparison's start day must be on or before its end day."
        : "The start day must be on or before the end day.";
    }
    return null;
  }

  /* A custom range's From and To dates. Each change that makes a valid range calls `onRange(start, end)`;
     otherwise `message` says what is wrong and the view stays. A single day is start = end. */
  function rangeInputs(ids, start, end, label, comparison, message, onRange) {
    var inputs = ids.map(function (id, i) {
      return h("input", { type: "date", id: id, value: i ? end : start, min: FIRST_DAY, max: LAST_DAY });
    });
    function changed() {
      var problem = rangeProblem(inputs[0].value, inputs[1].value, comparison);
      setMessage(message, problem, problem ? "error" : null);
      if (!problem) {
        onRange(inputs[0].value, inputs[1].value);
      }
    }
    inputs.forEach(function (input) { input.addEventListener("change", changed); });
    return h("div", { class: "range", role: "group", "aria-label": label },
      h("label", { class: "filt", for: ids[0] }, h("b", { text: "From" }), inputs[0]),
      h("label", { class: "filt", for: ids[1] }, h("b", { text: "To" }), inputs[1]));
  }

  /* `iso` moved by `days` (negative for earlier), as an ISO day. */
  function shiftDay(iso, days) {
    var date = localDate(iso);
    return isoDay(new Date(date.getFullYear(), date.getMonth(), date.getDate() + days));
  }

  /* Earlier and Later: each steps the period shown back or forward by its own length, as a custom range.
     Earlier is off when the step would start before FIRST_DAY; Later when it would end after LAST_DAY or the
     period shown already ends today or later (nothing later to show). */
  function stepButtons(prefix, start, end, go) {
    var valid = !rangeProblem(start, end);   // a refused address shows blank or unusable days: nothing to step
    var length = valid ? daysIn(start, end) : 0;
    function button(id, text, sign) {
      var from = valid ? shiftDay(start, sign * length) : "", to = valid ? shiftDay(end, sign * length) : "";
      var off = !valid || (sign < 0 ? from < FIRST_DAY : to > LAST_DAY || end >= isoDay(new Date()));
      return h("button", { type: "button", class: "small", id: prefix + "-" + id, text: text, disabled: off,
        onclick: function () { go({ period: "custom", start: from, end: to }); } });
    }
    return h("span", { class: "stepper" }, button("earlier", "◀ Earlier", -1), button("later", "Later ▶", 1));
  }

  /* The last day the period is meant to cover. The API cuts a so-far period short at the last day reviewed
     (This week so far on a Wednesday ends Tuesday), so the step uses the days asked for: a custom range's
     own, else the preset's full length. */
  function plannedEnd(shown, asked) {
    var shownEnd = isEmptyPeriod(shown) ? shown.start : shown.end;
    if (asked.end) {
      return asked.end;
    }
    if (!shown.so_far) {
      return shownEnd;
    }
    var first = localDate(shown.start);
    if (shown.preset === "this_week" || shown.preset === "last_week") {
      return shiftDay(shown.start, 6);
    }
    if (shown.preset === "this_month" || shown.preset === "last_month") {
      return isoDay(new Date(first.getFullYear(), first.getMonth() + 1, 0));
    }
    var months = { last_6_months: 6, last_year: 12 }[shown.preset];
    if (months) {
      return isoDay(new Date(first.getFullYear(), first.getMonth() + months, 0));
    }
    return shown.preset === "last_8_weeks" ? shiftDay(shown.start, 55) : shownEnd;
  }

  /* The period select, with its From and To dates when it is a custom range, and Earlier and Later to step
     it. `shown` is the period the API answered with (custom for an old week address too) and `asked` the
     address's own values; `go(changes)` moves the address. */
  function periodControl(prefix, shown, asked, message, go) {
    var pick = periodSelect(prefix, shown, asked, message, go);
    pick.appendChild(stepButtons(prefix, shown.start, plannedEnd(shown, asked), go));
    return pick;
  }

  /* The period select with its From and To dates (a custom range): periodControl without Earlier and Later, for
     a screen that places those elsewhere in its filter row. */
  function periodSelect(prefix, shown, asked, message, go) {
    var shownEnd = isEmptyPeriod(shown) ? shown.start : shown.end;
    var select = filterSelect(prefix + "-period", "Period", PERIOD_CHOICES, shown.preset, function (value) {
      go(value === "custom" ? { period: "custom", start: shown.start, end: shownEnd }
        : { period: value, start: null, end: null });
    });
    var dates = shown.preset !== "custom" ? null
      : rangeInputs([prefix + "-start", prefix + "-end"], asked.start || shown.start, asked.end || shownEnd,
        "Custom period", false, message, function (start, end) { go({ period: "custom", start: start, end: end }); });
    return h("div", { class: "period-pick" }, select, dates);
  }

  /* The period as an address names it, so another view opens on the same days. */
  function periodParams(shown, asked) {
    if (shown.preset !== "custom") {
      return { period: shown.preset };
    }
    return { period: "custom", start: asked.start || shown.start,
      end: asked.end || (isEmptyPeriod(shown) ? shown.start : shown.end) };
  }

  /* The comparison as an address names it: a custom range of the days shown (an old address's week). */
  function comparisonParams(comparison) {
    return { compare: "custom", compare_start: comparison.start,
      compare_end: isEmptyPeriod(comparison) ? comparison.start : comparison.end };
  }

  /* The period a screen covers, named once above its tiles as the screen's heading, with its reviewed
     calls and what it is compared with: "Last week (Oct 4 – Oct 10) · 30 calls vs Sep 27 – Oct 3, 2026 ·
     33 calls". `calls` and `compareCalls` are the API's counts (null for none); `by` names the agent the
     calls are by, if any. An empty period (nothing reviewed yet) and an empty comparison have no count. */
  function periodHead(period, calls, by, comparison, compareCalls) {
    var text = period.label;
    if (!isEmptyPeriod(period)) {
      text += " · " + plural(calls || 0, "call", "calls") + (by ? " by " + by : "");
    }
    if (comparison) {
      text += isEmptyPeriod(comparison) ? " · " + NOTHING_TO_COMPARE
        : " vs " + comparison.label + " · " + plural(compareCalls || 0, "call", "calls");
    }
    return h("h3", { class: "period-head", text: text });
  }

  // -- team overview ----------------------------------------------------------------------------

  var OVERVIEW_KEYS = ["period", "start", "end", "compare", "compare_start", "compare_end", "week", "agent"];
  var FLAG_POINTS = 10;   // an agent's problem rate this many points above the team's is flagged
  var MOST_MISSED_SHOWN = 8;

  function num(value) {
    return value == null ? "-" : String(Math.round(value * 10) / 10);
  }

  function pct(value) {
    return value == null ? "-" : num(value) + "%";
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  // A whole number of minutes as timing.format_minutes writes it: "30 min", "1 h", "1 h 30 min", "24 h".
  function formatMinutes(m) {
    if (m < 60) {
      return m + " min";
    }
    var rest = m % 60;
    return Math.floor(m / 60) + " h" + (rest ? " " + rest + " min" : "");
  }

  function weekLabel(iso) {
    return "Week of " + dayLabel(iso, false);
  }

  function vsWeek(iso) {
    return "week of " + dayLabel(iso, false);
  }

  function queryString(params) {
    var query = new URLSearchParams();
    Object.keys(params).forEach(function (key) {
      if (params[key] != null && params[key] !== "") {
        query.set(key, params[key]);
      }
    });
    var text = query.toString();
    return text ? "?" + text : "";
  }

  function renderOverview(token, query) {
    loadingLine("Loading the overview…");
    var params = paramsFrom(query, OVERVIEW_KEYS);
    readScreen(token, ["/api/overview" + queryString(params)], function (got) {
      var data = got[0];
      clearScreen();
      if (!data.has_data) {
        screen.appendChild(firstRunPanel());
        return;
      }
      screen.appendChild(overviewFilters(data, params));
      var calls = data.tiles.calls;
      screen.appendChild(periodHead(data.period, calls.count, null, data.comparison, calls.compare_count));
      var hasCalls = data.has_data_in_period;
      if (hasCalls) {
        screen.appendChild(h("div", { class: "panel-row cols-4-lead" }, overviewTiles(data)));
      } else {
        screen.appendChild(h("section", { class: "panel" }, h("div", { class: "placeholder", role: "status",
          text: withoutCalls(data.period,
            data.agent ? "No calls from " + agentName(data, data.agent) + " in this period" : null) })));
      }
      var trends = trendPanels(data);
      if (trends) {
        screen.appendChild(h("div", { class: "panel-row cols-2 stretch" }, trends.panels));
      }
      if (hasCalls) {
        screen.appendChild(h("div", { class: "panel-row cols-2 stretch" }, mostMissedPanel(data),
          agentsPanel(data, periodParams(data.period, params))));
        screen.appendChild(problemsPanel(data));
      }
      if (trends) {
        trends.draw();  // after the panels are on the page, so the charts measure their real width
      }
    }).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      if (error.status === 400 && params.compare === "same_days_last_week" && error.message === SAME_DAYS_REFUSED) {
        // An address asking for Same days last week on a period longer than 7 days: compare with the
        // period before instead, in place, so the back button doesn't return to the refused address.
        history.replaceState(null, "", "#/overview" + queryString(Object.assign({}, params, { compare: null })));
        route();
        return;
      }
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      screen.appendChild(h("div", { class: "nav-note" }, h("a", { href: "#/overview", text: "Show last week" })));
    });
  }

  function firstRunPanel() {
    return panel("Team overview", null,
      h("div", { class: "placeholder empty-state", role: "status" },
        h("p", null, h("b", { text: "No reviewed calls yet." })),
        h("p", null, "Results show up here after the first review run finishes. Add your team's techs in ",
          h("a", { href: "#/settings", text: "Settings" }),
          ", then use Run now there to review a past week, or wait for the daily run.")));
  }

  function agentName(data, email) {
    var match = data.agent_options.filter(function (a) { return a.email === email; })[0];
    return match ? match.name : email;
  }

  /* A filter's choice, `selected` picked (the first line with that value: a line may be offered twice, as a tech on
     two teams is). Lines may be indented with no-break spaces (the Tech picker's regions, teams and techs): the open
     list shows every indent, but the closed choice shows the picked line without its own ("APAC", not "   APAC"),
     so the picked line drops its indent whenever the choice doesn't have the focus, and gets it back while it does
     (the list opens from the focused choice). */
  function filterSelect(id, label, options, selected, onChange) {
    var picked = -1;
    options.forEach(function (o, i) {
      if (picked < 0 && o.value === selected) {
        picked = i;
      }
    });
    var lines = options.map(function (o, i) {
      return h("option", { value: o.value, text: o.text, selected: i === picked ? "selected" : null });
    });
    var select = h("select", { id: id }, lines);
    var indent = picked >= 0 ? /^\u00a0+/.exec(options[picked].text) : null;
    if (indent) {
      var shut = function (on) {
        lines[picked].textContent = on ? options[picked].text.slice(indent[0].length) : options[picked].text;
      };
      shut(true);
      select.addEventListener("focus", function () { shut(false); });
      select.addEventListener("blur", function () { shut(true); });
    }
    select.addEventListener("change", function () { onChange(select.value); });
    return h("label", { class: "filt", for: id }, h("b", { text: label }), select);
  }

  /* The period, "Compare to" and agent filters. `params` are the address's values (OVERVIEW_KEYS). A change
     of period keeps the comparison, except that Same days last week falls back to the period before when
     the new period is longer than 7 days; Last week, the default, and the period before stay out of the
     address. The owner's first change drops an old address's keys (`week`, a dated `compare`): a new
     period replaces both, comparing with the period before; any other change keeps the days shown, as
     custom ranges. */
  function overviewFilters(data, params) {
    var message = h("div", { class: "message", role: "status" });
    function go(changes) {
      var next = Object.assign({}, params, changes);
      if ("period" in changes) {
        next.week = null;
        if (isDatedCompare(next.compare) ||
            (next.compare === "same_days_last_week" && !shortPeriod(next.period, next.start, next.end))) {
          next.compare = null;
        }
        if (next.period === "last_week") {
          next.period = null;
        }
      } else {
        if (next.week) {
          Object.assign(next, { week: null }, periodParams(data.period, params));
        }
        if (isDatedCompare(next.compare) && data.comparison) {
          Object.assign(next, comparisonParams(data.comparison));
        }
      }
      location.hash = "#/overview" + queryString(next);
    }
    var agents = [{ value: "", text: "All techs" }].concat(data.agent_options.map(function (a) {
      return { value: a.email, text: a.name };
    }));
    if (data.agent && !data.agent_options.some(function (a) { return a.email === data.agent; })) {
      agents.push({ value: data.agent, text: data.agent });
    }
    return h("div", { class: "filters", role: "group", "aria-label": "Filters" },
      periodControl("ov", data.period, params, message, go),
      compareControl(data, params, message, go),
      filterSelect("ov-agent", "Tech", agents, data.agent || "", function (a) { go({ agent: a }); }),
      message);
  }

  /* "Compare to", with its From and To dates when it is a custom range. Same days last week is offered for
     periods of 7 days or less. A custom comparison starts on the comparison shown (the period's own days
     when there is none to start from). The screen's default choice (`fallback`: the period before unless named, as
     on the Team overview; Nothing on Trends) stays out of the address. */
  function compareControl(data, params, message, go, fallback) {
    var shown = data.period;
    var comparison = data.comparison;
    var mode = comparison ? comparison.preset : "none";
    var choices = COMPARE_CHOICES.filter(function (c) {
      return c.value !== "same_days_last_week" || daysIn(shown.start, shown.end) <= SAME_DAYS_MAX;
    });
    var from = comparison && !isEmptyPeriod(comparison) ? comparison : shown;
    var fromEnd = isEmptyPeriod(from) ? from.start : from.end;
    var select = filterSelect("ov-compare", "Compare to", choices, mode, function (value) {
      go(value === "custom" ? { compare: "custom", compare_start: from.start, compare_end: fromEnd }
        : { compare: value === (fallback || "previous") ? null : value, compare_start: null, compare_end: null });
    });
    var dates = mode !== "custom" ? null
      : rangeInputs(["ov-cstart", "ov-cend"], params.compare_start || from.start, params.compare_end || fromEnd,
        "Custom comparison", true, message, function (start, end) {
          go({ compare: "custom", compare_start: start, compare_end: end });
        });
    return h("div", { class: "period-pick" }, select, dates);
  }

  /* What the period is compared with, briefly, for "vs …": "the period before", "same days last week", or a
     custom comparison's own label ("Sep 20 – Sep 29, 2026", "week of Sep 27"). */
  function compareName(comparison) {
    if (comparison.preset === "previous") {
      return "the period before";
    }
    if (comparison.preset === "same_days_last_week") {
      return "same days last week";
    }
    return comparison.label.replace(/^Week of /, "week of ");
  }

  /* "▲ 2 vs the period before": the change against the comparison, colored good or bad by whether it is an
     improvement; "Nothing to compare yet" when the comparison has no calls, and no line at all when the
     owner compares with nothing. `unit` and `extra` follow the number. */
  function delta(change, lowerIsBetter, data, unit, extra) {
    if (!data.comparison) {
      return null;
    }
    if (!data.has_data_in_comparison || change == null) {
      return h("div", { class: "d flat", text: NOTHING_TO_COMPARE });
    }
    var vs = " vs " + compareName(data.comparison);
    var rounded = Math.round(change * 10) / 10;
    if (rounded === 0) {
      return h("div", { class: "d flat", text: "No change" + (extra || "") + vs });
    }
    var better = lowerIsBetter ? rounded < 0 : rounded > 0;
    return h("div", { class: "d " + (better ? "good" : "bad"),
      text: (rounded > 0 ? "▲ " : "▼ ") + num(Math.abs(rounded)) + (unit || "") + (extra || "") + vs });
  }

  /* A figure tile. A lead tile (the row's headline figure) shows its number larger. */
  function tile(label, value, deltaEl, title, lead) {
    return h("div", { class: "tile", title: title || null }, h("div", { class: "k", text: label }),
      h("div", { class: "v " + (lead ? "lead-num" : "tile-num") }, value), deltaEl);
  }

  /* " (+2 calls)": the change in a count, shown beside the change in its share; "" for none. */
  function callsChange(countChange) {
    return countChange == null || countChange === 0 ? "" : " (" + changeText(countChange) + " calls)";
  }

  /* A count tile: "4 · 20% of calls", its arrow from the change in share, and the change in count. */
  function countTile(label, figures, data) {
    var calls = callsChange(figures.count_change);
    var share = h("span", { class: "of", text: " · " + pct(figures.share_pct) + " of calls" });
    return tile(label, [String(figures.count), share], delta(figures.change_pts, true, data, " pts", calls));
  }

  /* The four figure tiles, Avg score first as the lead. */
  function overviewTiles(data) {
    var t = data.tiles;
    return [
      tile("Avg score", num(t.avg_score.avg_percent), delta(t.avg_score.change, false, data),
        "Average score, out of 100, of this period's reviewed calls", true),
      countTile("Problem tickets", t.problems, data),
      countTile("10-minute misses", t.call_length_misses, data),
      countTile("Scope violations", t.scope_misses, data)];
  }

  var STEP_UNITS = { day: ["day", "days"], week: ["week", "weeks"], month: ["month", "months"] };

  /* "14 days", "8 weeks": `count` trend steps of `unit`. */
  function stepCount(unit, count) {
    return plural(count, STEP_UNITS[unit][0], STEP_UNITS[unit][1]);
  }

  /* The name of the trend step of `unit` starting on `start`: "Tue, Oct 14", "Week of Oct 11", or
     "September 2026". */
  function stepName(unit, start) {
    if (unit === "week") {
      return weekLabel(start);
    }
    if (unit === "month") {
      return localDate(start).toLocaleDateString(undefined, { month: "long", year: "numeric" });
    }
    return localDate(start).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  /* A step's name for its tooltip: "(so far)" on a step the runs haven't finished, else "(part)" on an edge
     step that only partly lies in the period. A so-far period cut back to the last reviewed day is both,
     and reads "so far". */
  function stepTip(unit, step) {
    return stepName(unit, step.start) + (step.so_far ? " (so far)" : (step.partial ? " (part)" : ""));
  }

  /* The x-axis labels: a day's date or a week's Sunday ("Oct 11"), a month's short name ("Sep"), with the
     year on the first month shown and on each January ("Sep 2026", "Jan 2027"). */
  function stepLabels(unit, steps) {
    return steps.map(function (s, i) {
      if (unit !== "month") {
        return dayLabel(s.start, false);
      }
      var month = localDate(s.start);
      return month.toLocaleDateString(undefined, i === 0 || month.getMonth() === 0
        ? { month: "short", year: "numeric" } : { month: "short" });
    });
  }

  /* Each step's change markers, in its unit's words. */
  function trendMarkers(trend) {
    return trend.steps.map(function (s) { return Charts.stepMarkers(s, trend.unit); });
  }

  /* Which steps the chart shades: the chosen period's (charts.js draws the band only when some steps are
     context). */
  function trendShade(trend) {
    return trend.steps.map(function (s) { return s.in_period; });
  }

  /* The days a trend's steps cover: "Oct 4 – Oct 17", "Dec 20, 2025 – Jan 5, 2026" (the year when the span
     crosses years), or for months "Jan – Oct 2026", "Nov 2025 – Feb 2026" (always with the year). */
  function trendDays(trend) {
    var steps = trend.steps;
    var first = localDate(steps[0].start);
    var last = localDate(steps[steps.length - 1].end);
    var crosses = first.getFullYear() !== last.getFullYear();
    if (trend.unit !== "month") {
      return dayLabel(steps[0].start, crosses) + " – " + dayLabel(steps[steps.length - 1].end, crosses);
    }
    var to = last.toLocaleDateString(undefined, { month: "short", year: "numeric" });
    return steps.length === 1 ? to
      : first.toLocaleDateString(undefined, crosses ? { month: "short", year: "numeric" } : { month: "short" }) +
        " – " + to;
  }

  /* A trend chart's subtitle: how it steps and the days its steps cover, then `hint` (", out of 100"), then
     the shaded period when context steps are shown: "By day, Oct 4 – Oct 17, out of 100 · shaded: Yesterday
     (Fri, Oct 16)", "By week, Aug 23 – Oct 17", "By month, Jan – Oct 2026". */
  function trendSubtitle(trend, period, hint) {
    var shade = trendShade(trend);
    var shaded = shade.some(Boolean) && !shade.every(Boolean);
    return "By " + trend.unit + ", " + trendDays(trend) + (hint || "") + (shaded ? " · shaded: " + period.label : "");
  }

  /* "in the week of Sep 27", "on Tue, Oct 14", "in October 2026". */
  function stepPhrase(unit, start) {
    if (unit === "week") {
      return "in the " + vsWeek(start);
    }
    return (unit === "day" ? "on " : "in ") + stepName(unit, start);
  }

  /* The two trend panels, or null when the trend holds no data. */
  function trendPanels(data) {
    var trend = data.trend;
    var steps = trend.steps;
    if (!steps.some(function (s) { return s.avg_percent != null || s.problem_share_pct != null; })) {
      return null;
    }
    var who = data.agent ? agentName(data, data.agent) + ". " : "";
    var these = "these " + stepCount(trend.unit, steps.length);
    var starts = steps.map(function (s) { return s.start; });
    var scoreBox = h("div", { class: "chart-box" });
    var rateBox = h("div", { class: "chart-box" });
    var labels = stepLabels(trend.unit, steps);
    var markers = trendMarkers(trend);
    var shade = trendShade(trend);
    var scores = steps.map(function (s) { return s.avg_percent; });
    var rates = steps.map(function (s) { return s.problem_share_pct; });
    return {
      panels: [
        panel(data.agent ? "Average score" : "Team average score", who + trendSubtitle(trend, data.period,
          ", out of 100"), scoreBox),
        panel("Share of tickets with a problem", who + trendSubtitle(trend, data.period), rateBox)],
      draw: function () {
        Charts.lineChart(scoreBox, [{ name: "Average score", values: scores }], {
          labels: labels, markers: markers, shade: shade, format: num, min: 0, max: 100,
          ariaLabel: trendSummary("Average score by " + trend.unit, trend.unit, starts, scores, num),
          empty: "No scores in " + these,
          tooltip: function (i) { return stepTip(trend.unit, steps[i]) + ": avg score " + num(scores[i]); }
        });
        Charts.barChart(rateBox, rates, {
          labels: labels, markers: markers, shade: shade, format: pct, max: 100,
          ariaLabel: trendSummary("Share of tickets with a problem by " + trend.unit, trend.unit, starts, rates, pct),
          empty: "No reviewed calls in " + these,
          tooltip: function (i) {
            return stepTip(trend.unit, steps[i]) + ": " +
              (rates[i] == null ? "no reviewed calls" : pct(rates[i]) + " of tickets had a problem");
          }
        });
      }
    };
  }

  /* A trend's description for screen readers: its first and last values. `starts` are the steps' first days. */
  function trendSummary(title, unit, starts, values, format) {
    var points = [];
    values.forEach(function (v, i) {
      if (v != null) {
        points.push({ v: v, start: starts[i] });
      }
    });
    if (!points.length) {
      return title + ": no data in these " + stepCount(unit, starts.length);
    }
    var first = points[0];
    var last = points[points.length - 1];
    if (points.length === 1) {
      return title + ": " + format(last.v) + " " + stepPhrase(unit, last.start);
    }
    return title + ", from " + format(first.v) + " " + stepPhrase(unit, first.start) + " to " +
      format(last.v) + " " + stepPhrase(unit, last.start);
  }

  /* The Tickets tab's filters for the period's calls (its own start, end and agent filters), or null for an
     empty period, which has no calls to list (and no range whose end comes after its start). */
  function periodRange(period, agent) {
    return isEmptyPeriod(period) ? null : { start: period.start, end: period.end, agent: agent };
  }

  function changeText(change) {
    return change === 0 ? "no change" : (change > 0 ? "+" : "−") + Math.abs(change);
  }

  function mostMissedPanel(data) {
    var title = "Most-missed checks";
    if (!data.most_missed.length) {
      return panel(title, null, h("div", { class: "placeholder", text: "No checks were missed in this period." }));
    }
    var box = h("div");
    var noCompare = !data.has_data_in_comparison;   // as the tiles: no change against an empty comparison
    var range = periodRange(data.period, data.agent);
    // As the count tiles: the share of calls, then the change in share with the change in count beside it,
    // and nothing about a comparison when the owner compares with nothing.
    function changeVs(m) {
      if (noCompare) {
        return NOTHING_TO_COMPARE;
      }
      return (m.change_pts === 0 ? "no change" : changeText(m.change_pts) + " pts") + callsChange(m.count_change) +
        " vs " + compareName(data.comparison);
    }
    var rows = data.most_missed.map(function (m) {
      var share = pct(m.share_pct) + " of calls";
      return {
        label: m.label,
        value: m.count,
        delta: data.comparison ? share + " · " + changeVs(m) : share,
        deltaClass: noCompare ? "flat" : (m.change_pts > 0 ? "worse" : (m.change_pts < 0 ? "better" : "flat")),
        title: m.label + ": missed on " + plural(m.count, "ticket", "tickets") + " in this period, " + share +
          (data.comparison ? " (" + changeVs(m) + ")" : ""),
        href: range ? "#/tickets" + queryString(Object.assign({ missed_check: m.check_id }, range)) : null
      };
    });
    // The top few by default; every missed check is one click away. All bars share one scale either way.
    var toggle = null;
    if (rows.length > MOST_MISSED_SHOWN) {
      var showingAll = false;
      toggle = h("button", { class: "small more-toggle", "aria-expanded": "false" });
      var draw = function () {
        var shown = showingAll ? rows : rows.slice(0, MOST_MISSED_SHOWN);
        var max = rows[0].value;
        Charts.hBars(box, shown.map(function (r) { return Object.assign({ max: max }, r); }));
        toggle.textContent = showingAll ? "Show the top " + MOST_MISSED_SHOWN : "Show all " + rows.length + " missed checks";
        toggle.setAttribute("aria-expanded", String(showingAll));
      };
      toggle.addEventListener("click", function () {
        showingAll = !showingAll;
        draw();
      });
      draw();
    } else {
      Charts.hBars(box, rows);
    }
    return panel(title, "Number of tickets that missed each check" +
      (data.comparison ? ", change in share of calls against " + compareName(data.comparison) : ""), box, toggle);
  }

  /* The Agents panel. `periodLink` is the period in the Agents view's address words (periodParams), so
     clicking an agent opens their page on the same days. */
  function agentsPanel(data, periodLink) {
    var box = h("div", { class: "scroll" });
    fitRowMode(box, function (mode) { return agentsTable(data, mode, periodLink); });
    return panel("Techs", "Click a tech to see their trends and tickets", box);
  }

  /* Fill `box` with the table `build(mode)` makes: "one-line" when that fits the box's width, else
     "two-line". It is decided again whenever the box's width changes, so one table is on the page at a
     time, built for the width it has. The width is watched on an empty ruler at the top of the box, whose
     size never depends on the table: watching the box itself, a swap would change the size being watched
     from inside the observer, which the browser reports as a ResizeObserver loop error. */
  function fitRowMode(box, build) {
    var ruler = h("div", { class: "fit-ruler" });
    var mode = null;
    var lastWidth = -1;
    var observer = null;
    function show(next) {
      mode = next;
      box.replaceChildren(ruler, build(next));
    }
    function fit() {
      if (!ruler.isConnected) {
        observer.disconnect();   // the screen was redrawn without this panel
        return;
      }
      var width = ruler.clientWidth;
      if (!width || width === lastWidth) {
        return;
      }
      lastWidth = width;
      if (mode !== "one-line") {
        show("one-line");
      }
      if (box.scrollWidth > box.clientWidth) {
        show("two-line");
      }
    }
    show("one-line");
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(fit);
      observer.observe(ruler);
    }
  }

  /* A wide table in its box, for the hosted screens' tables (iPad and phone design §8 and §9): `div.scroll`, with
     `.list-scroll` for `opts.list` and `.tall` for `opts.tall`, holding `table`. Each body cell is labelled with its
     column's header (labelCells), and the table turns into cards whenever it doesn't fit the box (fitCards). With
     `opts.first`, a longer list shows its first rows and "Show all <n> <opts.noun>" under them below 1024px (app.css);
     wider, every row shows as before. `table` may instead be a list of items (satisfaction ratings), which gets the
     Show all only. A table whose rows are drawn again once its answer comes calls tableChanged. `opts.plain` is for a
     list whose box was never a positioned `.scroll` (1:1 prep's lists behind the numbers, a tech day's Recent days and
     open tickets): the box isn't positioned either (.scroll.plain), so its text draws exactly as it did. */
  function tableBox(table, opts) {
    opts = opts || {};
    var box = h("div", { class: "scroll" + (opts.list ? " list-scroll" : "") + (opts.tall ? " tall" : "") +
      (opts.plain ? " plain" : ""),
      "data-first": opts.first || null, "data-noun": opts.noun || null }, table);
    if (table.tagName === "TABLE") {
      labelCells(table);
      fitCards(box, table);
    }
    firstRows(box, table);
    return box;
  }

  /* After the rows of a table in a tableBox are drawn again (`part` is the table or its body): its cells labelled, its
     Show all worked out for the new count and its fit checked, as on the first draw. Nothing while the table isn't in
     its box yet: tableBox does it then. */
  function tableChanged(part) {
    var table = part.closest("table");
    var box = table ? table.parentNode : null;
    if (!box) {
      return;
    }
    labelCells(table);
    firstRows(box, table);
    fitNow(box, table);
  }

  /* Each body cell of `table` labelled with its column's header (`data-label`), which a card shows beside or under it
     (app.css), in every row with as many cells as the header has (a row across the columns is left alone). A cell
     with nothing in it to read or use (isBlank) is `blank`, which a card leaves out. Past the first cell (a card's
     title), a cell holding a button and no field or link is the row's actions (`act`), and a column whose every cell
     is a short number (isFigure) is figures (`fig`); a column with any longer cell is a line in every card, so the
     cards of one list look alike. */
  function labelCells(table) {
    var head = table.querySelector("thead tr");
    if (!head) {
      return;
    }
    var labels = Array.prototype.map.call(head.children, headLabel);
    var rows = Array.prototype.filter.call(table.querySelectorAll("tbody tr"), function (tr) {
      return tr.children.length === labels.length;
    });
    var figures = labels.map(function (label, i) {
      return rows.every(function (tr) { return isFigure(tr.children[i]); });
    });
    rows.forEach(function (tr) {
      Array.prototype.forEach.call(tr.children, function (td, i) {
        td.setAttribute("data-label", labels[i]);
        td.classList.toggle("blank", isBlank(td));
        if (!i || hasField(td)) {
          return;
        }
        if (td.querySelector("button") && !td.querySelector("a")) {
          td.classList.toggle("act", true);
        } else {
          td.classList.toggle("fig", figures[i]);
        }
      });
    });
  }

  /* A short number, which a card shows as a figure in a small box: a `td.num` of 12 characters or fewer, no field. */
  function isFigure(td) {
    return td.classList.contains("num") && td.textContent.trim().length <= 12 && !hasField(td);
  }

  /* A cell with no text and no field, button or picture in it (a day with no flags: an empty `.chips`). */
  function isBlank(td) {
    return !td.textContent.trim() && !hasField(td) && !td.querySelector("button, svg, img");
  }

  /* What a column's header calls it: its sort button's name without the sort arrow, else its text. */
  function headLabel(th) {
    var sort = th.querySelector("button.th-sort");
    return (sort ? sort.textContent.replace(/ [▲▼]$/, "") : th.textContent).trim();
  }

  function hasField(el) {
    return ["input", "select", "textarea"].some(function (tag) { return el.querySelector(tag); });
  }

  /* With the box's `data-first`, the rows (or a list's items) past that many are marked `more`, and when there are
     any, "Show all <n> <data-noun>" goes under them, in the box; below 1024px app.css hides both until the button is
     clicked, which shows every row (the box's `all`), fits the table to them and moves focus to the first row it
     showed (focusRow), the button being gone. A list whose selected row is past the first ones shows every row from
     the start. */
  function firstRows(box, table) {
    var first = Number(box.getAttribute("data-first"));
    var old = box.querySelector("button.show-all");
    if (old) {
      old.remove();
    }
    var rows = table.tagName === "TABLE" ? table.querySelectorAll("tbody tr") : table.children;
    Array.prototype.forEach.call(rows, function (row, i) {
      row.classList.toggle("more", Boolean(first) && i >= first);
      if (row.classList.contains("more") && row.classList.contains("sel")) {
        box.classList.toggle("all", true);   // the picked row (a 1:1 period opened from the whole list) stays in view
      }
    });
    if (first && rows.length > first) {
      box.appendChild(h("button", { type: "button", class: "small show-all",
        text: "Show all " + rows.length + " " + box.getAttribute("data-noun"),
        onclick: function () {
          box.classList.toggle("all", true);
          if (table.tagName === "TABLE") {
            fitNow(box, table);
          }
          focusRow(rows[first]);
        } }));
    }
  }

  /* Focus on a row (or a list's item): its first link or button, else the row itself, focusable by script only. */
  function focusRow(row) {
    var control = row.querySelector("a, button");
    if (!control) {
      row.setAttribute("tabindex", "-1");
    }
    (control || row).focus();
  }

  /* `table` in `box` shows as cards (`cards`, app.css) when, and only when, it is wider than the box: decided when the
     box is first laid out and again whenever its width changes (an iPad turned, a window resized), inside the
     ResizeObserver's callback, so the switch lands before the page paints. As in fitRowMode, the width is watched on
     an empty ruler at the top of the box, whose size the switch never changes. */
  function fitCards(box, table) {
    var ruler = h("div", { class: "fit-ruler" });
    var lastWidth = -1;
    var observer = null;
    box.prepend(ruler);
    function fit() {
      if (!ruler.isConnected) {
        observer.disconnect();   // the screen was redrawn without this table
        return;
      }
      var width = ruler.clientWidth;
      if (!width || width === lastWidth) {
        return;
      }
      lastWidth = width;
      fitNow(box, table);
    }
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(fit);
      observer.observe(ruler);
    }
  }

  /* Measure `table` as a table in `box`, and make it cards if it is wider. */
  function fitNow(box, table) {
    table.classList.toggle("cards", false);
    var cards = box.scrollWidth > box.clientWidth;
    table.classList.toggle("cards", cards);
    tableRoles(table, cards);
  }

  /* While a table shows as cards, its parts carry their table roles: shown as blocks, a table would lose its meaning
     to a screen reader. As a table again, they go (the elements carry that meaning themselves). */
  function tableRoles(table, on) {
    function role(el, name) {
      if (on) {
        el.setAttribute("role", name);
      } else {
        el.removeAttribute("role");
      }
    }
    role(table, "table");
    table.querySelectorAll("thead").forEach(function (el) { role(el, "rowgroup"); });
    table.querySelectorAll("tbody").forEach(function (el) { role(el, "rowgroup"); });
    table.querySelectorAll("tr").forEach(function (el) { role(el, "row"); });
    table.querySelectorAll("th").forEach(function (el) { role(el, el.closest("thead") ? "columnheader" : "rowheader"); });
    table.querySelectorAll("td").forEach(function (el) { role(el, "cell"); });
  }

  /* The Agents table. "one-line": every column in one row per agent. "two-line", for a narrower panel: the
     figures, then the sparkline and the most-missed check under them. Each agent's tbody is one click
     target either way. */
  function agentsTable(data, mode, periodLink) {
    var oneLine = mode === "one-line";
    var teamRate = data.tiles.problems.share_pct;
    var trendValues = [];
    data.agents.forEach(function (a) {
      a.trend.forEach(function (v) {
        if (v != null) {
          trendValues.push(v);
        }
      });
    });
    // One scale for every row's sparkline, so the rows compare.
    var domain = trendValues.length ? [Math.min.apply(null, trendValues), Math.max.apply(null, trendValues)] : null;
    var unit = data.trend.unit;
    var steps = data.trend.steps;
    var bodies = data.agents.map(function (a) {
      var link = "#/agents/" + encodeURIComponent(a.email) + queryString(periodLink || {});
      var flagged = teamRate != null && data.agents.length > 1 && a.problem_rate != null &&
        a.problem_rate - teamRate >= FLAG_POINTS;
      var rate = flagged ? h("span", { class: "flag crit", title: FLAG_POINTS + "+ points above the team's " +
        pct(teamRate) }, "▲ " + pct(a.problem_rate)) : pct(a.problem_rate);
      var figures = [
        h("td", null, h("a", { href: link, text: a.name })),
        h("td", { class: "num", text: a.contacts }),
        h("td", { class: "num", text: num(a.avg_percent) }),
        h("td", { class: "num", text: a.problems }),
        h("td", { class: "num" }, rate)
      ];
      var sparkCell = h("td", { class: "spark-cell" });
      var miss = h("td", { class: "miss", colspan: oneLine ? null : 4, text: a.top_miss || "-" });
      var rows = oneLine
        ? h("tbody", { class: "row-group" }, h("tr", { class: "rowlink" }, figures, sparkCell, miss))
        : h("tbody", { class: "row-group" },
          h("tr", { class: "rowlink line1" }, figures),
          h("tr", { class: "rowlink line2" }, sparkCell, miss));
      rows.addEventListener("click", function (event) {
        if (!event.target.closest("a")) {
          location.hash = link;
        }
      });
      Charts.sparkline(sparkCell, a.trend, {
        domain: domain,
        labels: a.trend.map(function (v, i) { return stepTip(unit, steps[i]) + ": avg score " + num(v); }),
        ariaLabel: a.name + " score by " + unit + ", " + trendDays(data.trend) + ": " + a.trend.map(num).join(", ")
      });
      return rows;
    });
    var figureHeads = [
      h("th", { scope: "col", text: "Tech" }),
      h("th", { scope: "col", class: "num", text: "Tickets" }),
      h("th", { scope: "col", class: "num", text: "Avg score" }),
      h("th", { scope: "col", class: "num", text: "Problem tickets" }),
      h("th", { scope: "col", class: "num", text: "Problem rate" })
    ];
    var sparkHead = h("th", { scope: "col", text: "Score by " + unit });   // "Score by week"
    var missHead = h("th", { scope: "col", colspan: oneLine ? null : 4, text: "Most-missed check" });
    var head = oneLine
      ? h("thead", null, h("tr", null, figureHeads, sparkHead, missHead))
      : h("thead", null, h("tr", { class: "line1" }, figureHeads), h("tr", { class: "line2" }, sparkHead, missHead));
    return h("table", { class: "atable " + mode }, head, bodies);
  }

  function priorityText(priority) {
    return priority ? priority.charAt(0).toUpperCase() + priority.slice(1) + " priority" : "No priority";
  }

  function problemsPanel(data) {
    var count = data.tiles.problems.count;
    var title = "Problem tickets (" + count + ")";
    if (!data.problems.length) {
      return panel(title, null, h("div", { class: "placeholder", text: "No problem tickets in this period." }));
    }
    var labels = {};
    data.most_missed.forEach(function (m) { labels[m.check_id] = m.label; });
    var items = data.problems.map(function (t) {
      var score = t.review_error ? "Review failed" : t.earned + "/" + t.total;
      return h("a", { class: "ptk", href: "#/tickets?ticket=" + encodeURIComponent(t.ticket_id) },
        h("span", { class: "tid", text: "#" + t.ticket_id }),
        " · " + t.agent_name + " · " + score + " · " + priorityText(t.priority) + " · " + dayLabel(t.date, false),
        h("div", { class: "chips" }, t.missed_ids.map(function (id, i) {
          return h("span", { class: "chip", text: "✗ " + (labels[id] || t.missed[i]) });
        })));
    });
    var more = count - data.problems.length;
    var range = periodRange(data.period, data.agent);
    return panel(title, "Newest first. Click a ticket for the full breakdown with transcript evidence.", items,
      range ? h("div", { class: "nav-note" }, more > 0 ? "… " + more + " more · " : null,
        h("a", { href: "#/tickets" + queryString(Object.assign({ verdict: "problem" }, range)),
          text: "See all problem tickets" })) : null);
  }

  // -- agents -----------------------------------------------------------------------------------

  var AGENT_KEYS = ["period", "start", "end", "weeks", "week"];   // the period; weeks and week are old addresses'
  var AGENT_PROBLEMS_SHOWN = 10;
  var NOTE_MAX_CHARS = 10000;

  function renderAgents(token, email, query) {
    loadingLine("Loading the tech…");
    var params = paramsFrom(query, AGENT_KEYS);
    readScreen(token, ["/api/agents"], function (got, listToken) {
      var list = got[0];
      if (!list.agents.length) {
        clearScreen();
        screen.appendChild(noAgentsPanel());
        return;
      }
      var chosen = email ? email.trim().toLowerCase() : list.default;
      readScreen(listToken, ["/api/agents/" + encodeURIComponent(chosen) + queryString(params)], function (detail) {
        clearScreen();
        agentPage(list, detail[0], params);
      }).catch(function (error) {
        if (listToken === renderToken) {
          clearScreen();
          screen.appendChild(agentFilters(list, chosen, params, null));
          screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
        }
      });
    }).catch(function (error) {
      if (token === renderToken) {
        clearScreen();
        screen.appendChild(h("div", { class: "message error", text: error.message }));
      }
    });
  }

  function noAgentsPanel() {
    return panel("Techs", null,
      h("div", { class: "placeholder empty-state", role: "status" },
        h("p", null, h("b", { text: "No techs yet." })),
        h("p", null, "Add your team's techs in ", h("a", { href: "#/settings", text: "Settings" }),
          ". Each tech's scores, coaching targets and your notes show here after their calls are reviewed.")));
  }

  function agentLabel(agent) {
    return (agent.name || agent.email) + (agent.active ? "" : " (not on team)");
  }

  function agentHash(email, params) {
    return "#/agents/" + encodeURIComponent(email) + queryString(params);
  }

  /* One pill link per agent (the current one marked), each keeping the period, then the period picker.
     `params` are the address's values (AGENT_KEYS); `shown` is the period the API answered with, or null
     when the page couldn't load, and the picker then shows what the address asked for (refusedPeriod).
     Nothing is compared on this page beyond the team over the same days, so it has no "Compare to". */
  function agentFilters(list, email, params, shown) {
    var agents = list.agents.map(function (a) { return { email: a.email, text: agentLabel(a) }; });
    if (!list.agents.some(function (a) { return a.email === email; })) {
      agents.push({ email: email, text: email });
    }
    var message = h("div", { class: "message", role: "status" });
    function go(changes) {
      var next = Object.assign({}, params, changes, { weeks: null, week: null });
      if (next.period === "last_8_weeks") {
        next.period = null;   // the default
      }
      location.hash = agentHash(email, next);
    }
    shown = shown || refusedPeriod(params);
    return h("div", { class: "filters", role: "group", "aria-label": "Filters" },
      h("div", { class: "pills", role: "group", "aria-label": "Tech" }, agents.map(function (a) {
        var on = a.email === email;
        return h("a", { class: on ? "pill on" : "pill", href: agentHash(a.email, params),
          "aria-current": on ? "page" : null, text: a.text });
      })),
      periodControl("ag", shown, params, message, go),
      message);
  }

  /* What the period picker shows for an address the API refused: a preset it names, the default (Last 8
     weeks) when it names none, and otherwise "Custom range" with the address's dates (blank for an old
     address's `weeks` and `week`), so choosing any preset, Last 8 weeks included, still moves the page. */
  function refusedPeriod(params) {
    var legacy = params.weeks || params.week;
    var preset = params.period && !legacy && PERIOD_CHOICES.some(function (c) { return c.value === params.period; })
      ? params.period : (legacy || params.period || params.start || params.end ? "custom" : "last_8_weeks");
    return { preset: preset, start: legacy ? "" : params.start || "", end: legacy ? "" : params.end || "" };
  }

  function agentPage(list, detail, params) {
    var name = detail.name || detail.email;
    var period = periodRange(detail.period, detail.email);
    screen.appendChild(agentFilters(list, detail.email, params, detail.period));
    if (!detail.active) {
      screen.appendChild(h("div", { class: "note-banner", role: "note",
        text: name + " is not on the team now. Their past results stay here; new calls aren't reviewed." }));
    }
    screen.appendChild(periodHead(detail.period, detail.tiles.contacts, name, null, null));
    screen.appendChild(h("div", { class: "panel-row cols-4-lead" }, agentTiles(detail)));
    var chart = agentChart(detail, name);
    screen.appendChild(h("div", { class: "panel-row cols-2-1" }, chart.panel, notesPanel(detail, name)));
    screen.appendChild(checksPanel(detail, name, period));
    screen.appendChild(agentProblemsPanel(detail, period));
    chart.draw();  // after the panel is on the page, so the chart measures its real width
  }

  function points(value) {
    var n = Math.abs(Math.round(value * 10) / 10);
    return num(n) + (n === 1 ? " pt" : " pts");
  }

  /* The tiles, under the period header (periodHead), so their labels needn't name the period. */
  function agentTiles(detail) {
    var t = detail.tiles;
    var rate;
    if (t.problem_rate == null) {
      rate = h("div", { class: "d flat", text: withoutCalls(detail.period) });
    } else if (t.team_problem_rate == null) {
      rate = h("div", { class: "d flat", text: "Team: -" });
    } else {
      var diff = Math.round((t.problem_rate - t.team_problem_rate) * 10) / 10;
      var team = " team (" + pct(t.team_problem_rate) + ")";
      rate = diff === 0 ? h("div", { class: "d flat", text: "Same as the" + team })
        : h("div", { class: "d " + (diff > 0 ? "bad" : "good"),
          text: (diff > 0 ? "▲ " : "▼ ") + points(diff) + (diff > 0 ? " above" : " below") + team });
    }
    var rank = t.contacts == null ? withoutCalls(detail.period)
      : (t.contacts_rank === 1 ? "Most on the team" : "Rank " + t.contacts_rank + " on the team");
    var gap = t.biggest_gap;
    return [
      tile("Avg score", num(t.avg_percent),
        h("div", { class: "d", text: "Team: " + num(t.team_avg_percent) }),
        "Average score, out of 100, of reviewed calls in this period. Review errors are left out.",
        true),
      tile("Problem rate", pct(t.problem_rate), rate),
      tile("Tickets", num(t.contacts), h("div", { class: "d", text: rank })),
      h("div", { class: "tile" }, h("div", { class: "k", text: "Biggest gap vs team" }),
        h("div", { class: "v v-text", text: gap ? gap.label : "-" }),
        gap ? h("div", { class: "d bad", text: "▼ " + points(gap.gap) + " below team" })
          : h("div", { class: "d flat", text: "Not behind the team on any check" }))];
  }

  function agentChart(detail, name) {
    var trend = detail.trend;
    var steps = trend.steps;
    var box = h("div", { class: "chart-box" });
    var agentValues = steps.map(function (s) { return s.agent; });
    var teamValues = steps.map(function (s) { return s.team; });
    var starts = steps.map(function (s) { return s.start; });
    var first = (detail.name || "Tech").split(" ")[0];
    return {
      panel: panel("Score vs team average", trendSubtitle(trend, detail.period, ", out of 100"), box),
      draw: function () {
        Charts.lineChart(box, [
          { name: name, short: first, values: agentValues },
          { name: "Team average", short: "Team", values: teamValues }
        ], {
          labels: stepLabels(trend.unit, steps),
          markers: trendMarkers(trend),
          shade: trendShade(trend),
          format: num, min: 0, max: 100,
          ariaLabel: trendSummary(name + "'s average score by " + trend.unit, trend.unit, starts, agentValues, num) +
            ". " + trendSummary("Team average", trend.unit, starts, teamValues, num),
          empty: "No scores in these " + stepCount(trend.unit, steps.length),
          tooltip: function (i) {
            return stepTip(trend.unit, steps[i]) + ": " + name + " " + num(agentValues[i]) + " · Team " +
              num(teamValues[i]);
          }
        });
      }
    };
  }

  function gapText(gap) {
    if (gap == null) {
      return "-";
    }
    if (gap === 0) {
      return "even";
    }
    return (gap > 0 ? "+" : "−") + num(Math.abs(gap));
  }

  /* The checks in two groups, each sorted by gap with the furthest behind first: the coaching targets
     (flagged), then everything else, where checks without a gap go last. The API's list is left as is. */
  function checkGroups(checks) {
    function byGap(a, b) {
      if (a.gap == null || b.gap == null) {
        return (a.gap == null) - (b.gap == null);
      }
      return a.gap - b.gap;
    }
    return {
      targets: checks.filter(function (c) { return c.flagged; }).sort(byGap),
      rest: checks.filter(function (c) { return !c.flagged; }).sort(byGap)
    };
  }

  var CHECK_COLUMNS = 5;

  /* `period` is the Tickets filters for the agent's calls in the period (periodRange), null when it is empty. */
  function checksPanel(detail, name, period) {
    var title = "Pass rate by check";
    if (!detail.checks.length) {
      return panel(title, null, h("div", { class: "placeholder", text: withoutCalls(detail.period) }));
    }
    function row(c) {
      var rates = c.label + ": " + name + " " + pct(c.agent_rate) + " · Team " + pct(c.team_rate);
      var label = period && c.agent_rate != null && c.agent_rate < 100
        ? h("a", { class: "checklink", href: "#/tickets" + queryString(Object.assign({ missed_check: c.check_id }, period)),
          title: "See " + name + "'s calls that missed this check", text: c.label })
        : h("span", { text: c.label });
      var bar = h("td", { class: "barcell" });
      Charts.passBar(bar, c.agent_rate, c.team_rate, { flagged: c.flagged, title: rates, ariaLabel: rates });
      return h("tr", { class: c.flagged ? "flagrow" : null, title: rates },
        h("td", null, label, h("div", { class: "note", text: "judged on " + plural(c.applied_count, "call", "calls") })),
        bar,
        h("td", { class: "num", text: pct(c.agent_rate) }),
        h("td", { class: "num muted", text: pct(c.team_rate) }),
        h("td", { class: c.flagged ? "num flag crit" : "num muted", text: (c.flagged ? "▼ " : "") + gapText(c.gap) }));
    }
    function groupHead(text) {
      return h("tr", { class: "group-head" }, h("td", { colspan: CHECK_COLUMNS, text: text }));
    }
    var groups = checkGroups(detail.checks);
    var body = h("tbody", null,
      groupHead("Coaching targets: 10+ points behind the team"),
      groups.targets.length ? groups.targets.map(row)
        : h("tr", { class: "group-empty" },
          h("td", { colspan: CHECK_COLUMNS, class: "muted", text: "No checks are 10+ points behind the team." })),
      groups.rest.length ? [groupHead("Everything else, sorted by gap"), groups.rest.map(row)] : null);
    // The team had calls but the agent had none: the table shows the team's rates alone, so say why.
    var none = detail.has_data_in_period ? null
      : h("div", { class: "period-note", role: "status", text: "No calls from " + name + " in this period" });
    return panel(title, "Rows 10+ points below the team are highlighted. These are the coaching targets. " +
      "Unsure counts as a pass; checks that did not apply are left out.", none,
      h("div", { class: "scroll" }, h("table", { class: "atable" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", text: "Check" }),
          h("th", { scope: "col", class: "barcell" }, "Pass rate ",
            h("span", { class: "legend", "aria-hidden": "true" }, h("span", { class: "sw" }), name,
              h("span", { class: "sw ref" }), "Team")),
          h("th", { scope: "col", class: "num", text: name }),
          h("th", { scope: "col", class: "num", text: "Team" }),
          h("th", { scope: "col", class: "num", text: "Gap" }))),
        body)));
  }

  function agentProblemsPanel(detail, period) {
    var problems = detail.problems;
    var title = "Problem calls (" + problems.length + ")";
    if (!problems.length) {
      return panel(title, null, h("div", { class: "placeholder",
        text: isEmptyPeriod(detail.period) ? NOTHING_REVIEWED : "No problem calls in this period." }));
    }
    var labels = {};
    detail.checks.forEach(function (c) { labels[c.check_id] = c.label; });
    var items = problems.slice(0, AGENT_PROBLEMS_SHOWN).map(function (t) {
      var score = t.review_error ? "Review failed" : t.earned + "/" + t.total;
      return h("a", { class: "ptk", href: "#/tickets?ticket=" + encodeURIComponent(t.ticket_id) },
        h("span", { class: "tid", text: "#" + t.ticket_id }),
        " · " + score + " · " + priorityText(t.priority) + " · " + dayLabel(t.date, false),
        h("div", { class: "chips" }, t.missed_ids.map(function (id, i) {
          return h("span", { class: "chip", text: "✗ " + (labels[id] || t.missed[i]) });
        })));
    });
    var more = problems.length - items.length;
    return panel(title, "Newest first. Click a call for the full breakdown with transcript evidence.", items,
      period ? h("div", { class: "nav-note" }, more > 0 ? "… " + more + " more · " : null,
        h("a", { href: "#/tickets" + queryString(Object.assign({ verdict: "problem" }, period)),
          text: "See all problem calls" })) : null);
  }

  function noteDate(isoUtc) {
    return new Date(isoUtc).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    });
  }

  function notesPanel(detail, name) {
    var notes = detail.notes.slice();
    var list = h("div", { class: "coach-list" });
    var message = h("div", { class: "message", role: "status" });
    var text = h("textarea", { id: "note-body", rows: "3", maxlength: String(NOTE_MAX_CHARS),
      placeholder: "What you talked about, and what to recheck" });
    var add = h("button", { class: "primary", type: "submit", text: "Add note" });

    function draw() {
      list.textContent = "";
      if (!notes.length) {
        list.appendChild(h("div", { class: "placeholder", text: "No notes for " + name + " yet." }));
      }
      notes.forEach(function (note) {
        var when = noteDate(note.created_at);
        list.appendChild(h("div", { class: "coach" },
          h("div", { class: "coach-head" }, h("span", { class: "coach-date", text: when }),
            h("button", { class: "small", type: "button", text: "Delete…", "aria-label": "Delete the note from " + when,
              onclick: function () { remove(note, when); } })),
          h("div", { class: "coach-body", text: note.body })));
      });
    }

    function remove(note, when) {
      if (!window.confirm("Delete your note from " + when + "?\n\nThis can't be undone.")) {
        return;
      }
      api("DELETE", "/api/notes/" + note.note_id).then(function () {
        notes = notes.filter(function (n) { return n.note_id !== note.note_id; });
        draw();
        setMessage(message, "Note deleted.", "ok");
      }).catch(function (error) { setMessage(message, error.message, "error"); });
    }

    var form = h("form", { class: "coach-form" },
      h("label", { for: "note-body", class: "sr-only", text: "New coaching note for " + name }), text,
      h("div", { class: "actions" }, add, message));
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!text.value.trim()) {
        setMessage(message, "Write the note first.", "error");
        return;
      }
      add.disabled = true;
      api("POST", "/api/agents/" + encodeURIComponent(detail.email) + "/notes", { body: text.value }).then(function (note) {
        notes.unshift(note);
        text.value = "";
        draw();
        setMessage(message, "Note saved.", "ok");
      }).catch(function (error) {
        setMessage(message, error.message, "error");
      }).then(function () { add.disabled = false; });
    });
    draw();
    return panel("Your coaching notes", "Private to you, dated, newest first. Notes are kept when old calls' " +
      "content is erased.", form, list);
  }

  // -- tickets ----------------------------------------------------------------------------------

  /* The Tickets screen: filters, the list, and the selected call's detail (timeline, scorecard with
     Disagree, comments). Ticket text is untrusted customer content: it only ever goes in through
     textContent (h()), and the page shows it with pre-wrap. */

  var TICKET_FILTERS = ["start", "end", "agent", "verdict", "missed_check", "priority", "q"];
  // The list's sort travels with the filters in the address and the export, but is not a filter itself.
  var TICKET_PARAMS = TICKET_FILTERS.concat(["sort"]);
  var SORT_CHOICES = [
    { value: "", text: "Newest first" },
    { value: "oldest", text: "Oldest first" },
    { value: "score_low", text: "Lowest score first" },
    { value: "score_high", text: "Highest score first" },
    { value: "agent", text: "Tech (A–Z)" }
  ];
  var TICKETS_PAGE = 200;
  var VERDICT_TEXT = { good: "✓ Good", needs_review: "? Needs review", problem: "✗ Problem",
    review_failed: "! Review failed", not_counted: "Not counted" };
  var VERDICT_CHOICES = [
    { value: "", text: "All counted calls" },
    { value: "good", text: "Good" },
    { value: "needs_review", text: "Needs review" },
    { value: "problem", text: "Problem" },
    { value: "review_failed", text: "Review failed" },
    { value: "not_counted", text: "Not counted" }
  ];
  var OUTCOME_TEXT = { met: "Met", missed: "Missed", na: "N/A", unsure: "Unsure" };
  var OUTCOME_MARK = { met: "✓", missed: "✗", na: "N/A", unsure: "?" };
  var RESOLVED_TEXT = { yes: "Yes", no: "No", unclear: "Unclear" };
  var COMMENT_KIND = {
    voice: "Call", transcript: "Transcript", call_summary: "Call summary", triage_macro: "Triage macro",
    summary_email: "Summary email", other: "Comment"
  };
  var RECHECK_TITLE = "The reviewer looked at this call again after you overrode this check. Your earlier " +
    "override wasn't carried over. Check the new result, and override it again if you still disagree.";
  var ERROR_TEXT_MAX = 300;
  var SINGLE_COLUMN = "(max-width: 1023px)";   // matches the two-column panel-row stacking breakpoint in app.css
  var TOUCH = "(pointer: coarse)";              // a touch screen, as app.css's touch rules read it

  var tickets = { root: null, key: null, filters: null, data: null, listBox: null, detailBox: null,
    selected: null, shown: TICKETS_PAGE, detailToken: 0 };

  function ticketFilters(query) {
    var filters = {};
    TICKET_PARAMS.forEach(function (key) {
      var value = query.get(key);
      filters[key] = value && value.trim() ? value.trim() : null;
    });
    return filters;
  }

  /* The filters alone, without the sort: what Review these N again sends. */
  function onlyFilters(filters) {
    var out = {};
    TICKET_FILTERS.forEach(function (key) { out[key] = filters[key] || null; });
    return out;
  }

  function sortText(sort) {
    var choice = SORT_CHOICES.filter(function (c) { return c.value === (sort || ""); })[0];
    return choice ? choice.text : SORT_CHOICES[0].text;
  }

  function filtersKey(filters) {
    return TICKET_PARAMS.map(function (key) { return key + "=" + (filters[key] || ""); }).join("&");
  }

  function ticketsHash(filters, ticketId) {
    return "#/tickets" + queryString(Object.assign({}, filters, { ticket: ticketId == null ? null : ticketId }));
  }

  /* True when the Tickets list on screen already shows these filters, so only the selection changes. A list left
     faded on screen while another screen loads is not reused: that screen is still on its way. */
  function ticketsReusable(query) {
    return tickets.root !== null && screen.contains(tickets.root) && !screen.classList.contains("stale") &&
      tickets.key === filtersKey(ticketFilters(query));
  }

  function renderTickets(token, query) {
    var filters = ticketFilters(query);
    loadingLine("Loading tickets…");
    readScreen(token, ["/api/tickets" + queryString(filters)], function (got) {
      var data = got[0];
      clearScreen();
      tickets.filters = filters;
      tickets.key = filtersKey(filters);
      tickets.data = data;
      tickets.shown = TICKETS_PAGE;
      tickets.listBox = h("div", { class: "stack" });
      tickets.detailBox = h("div", { class: "stack detail-box", "aria-live": "polite" });
      tickets.root = h("div", null, ticketFilterBar(data, filters),
        h("div", { class: "panel-row cols-1-2" }, tickets.listBox, tickets.detailBox));
      screen.appendChild(tickets.root);
      drawTicketList();
      selectTicket(query.get("ticket"));
    }).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      tickets.root = null;
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      screen.appendChild(h("div", { class: "nav-note" }, h("a", { href: "#/tickets", text: "Clear the filters" })));
    });
  }

  function ticketFilterBar(data, filters) {
    function go(changes) {
      location.hash = ticketsHash(Object.assign({}, filters, changes), parseRoute().query.get("ticket"));
    }
    var search = h("input", { type: "search", id: "tk-q", value: filters.q || "", placeholder: "Ticket #, tech, or text…" });
    var searchForm = h("form", { class: "filt search", role: "search" },
      h("label", { for: "tk-q" }, h("b", { text: "Search" })), search);
    searchForm.addEventListener("submit", function (event) {
      event.preventDefault();
      go({ q: search.value.trim() || null });
    });
    search.addEventListener("change", function () {
      if ((search.value.trim() || null) !== filters.q) {
        go({ q: search.value.trim() || null });
      }
    });
    function dateInput(id, label, key) {
      var input = h("input", { type: "date", id: id, value: filters[key] || "" });
      input.addEventListener("change", function () {
        var change = {};
        change[key] = input.value || null;
        go(change);
      });
      return h("label", { class: "filt", for: id }, h("b", { text: label }), input);
    }
    var agents = [{ value: "", text: "All techs" }].concat(data.agent_options.map(function (a) {
      return { value: a.email, text: a.name };
    }));
    if (filters.agent && !agents.some(function (a) { return a.value === filters.agent.toLowerCase(); })) {
      agents.push({ value: filters.agent, text: filters.agent });
    }
    var checks = [{ value: "", text: "Any" }].concat(data.check_options.map(function (c) {
      return { value: c.check_id, text: c.label };
    }));
    if (filters.missed_check && !data.check_options.some(function (c) { return c.check_id === filters.missed_check; })) {
      checks.push({ value: filters.missed_check, text: filters.missed_check });
    }
    var priorities = [{ value: "", text: "All" }].concat(data.priorities.map(function (p) {
      return { value: p, text: capitalize(p) };
    }));
    var any = TICKET_FILTERS.some(function (key) { return filters[key]; });
    // Two rows: the main filters with Export at the right, then Missed check and Clear filters.
    return h("div", { class: "filters stack", role: "group", "aria-label": "Filters" },
      h("div", { class: "filter-row" },
        searchForm,
        dateInput("tk-start", "From", "start"),
        dateInput("tk-end", "To", "end"),
        filterSelect("tk-verdict", "Verdict", VERDICT_CHOICES, filters.verdict || "", function (v) { go({ verdict: v || null }); }),
        filterSelect("tk-agent", "Tech", agents, filters.agent ? filters.agent.toLowerCase() : "",
          function (v) { go({ agent: v || null }); }),
        filterSelect("tk-priority", "Priority", priorities, filters.priority ? filters.priority.toLowerCase() : "",
          function (v) { go({ priority: v || null }); }),
        h("a", { class: "export", href: "/api/export.xlsx" + queryString(filters), download: true, text: "Export to Excel" })),
      h("div", { class: "filter-row" },
        filterSelect("tk-missed", "Missed check", checks, filters.missed_check || "",
          function (v) { go({ missed_check: v || null }); }),
        filterSelect("tk-sort", "Sort", SORT_CHOICES, filters.sort || "", function (v) { go({ sort: v || null }); }),
        any ? h("a", { class: "clear-filters", href: ticketsHash({ sort: filters.sort }, parseRoute().query.get("ticket")),
          text: "Clear filters" }) : null));
  }

  function capitalize(text) {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
  }

  function shortError(text) {
    return text && text.length > ERROR_TEXT_MAX ? text.slice(0, ERROR_TEXT_MAX) + "…" : text;
  }

  /* A row's or a review's verdict: a review error is "Review failed", never a score-based label. The error is its
     tooltip. */
  function verdictBadge(item) {
    var verdict = item.counted !== false && item.review_error ? "review_failed" : item.verdict;
    return h("span", { class: "verdict " + verdict, text: VERDICT_TEXT[verdict] || verdict,
      title: verdict === "review_failed" && item.error ? "Review failed: " + shortError(item.error) : null });
  }

  function scoreText(item) {
    if (item.counted === false || item.review_error || item.percent == null) {
      return "-";
    }
    return num(item.percent);
  }

  function recheckBadge() {
    return h("span", { class: "badge-recheck", title: RECHECK_TITLE, text: "Recheck" });
  }

  /* One call in the list, a compact row: the score, then the call's line (ticket, agent, date, priority,
     and its verdict at the end), with its misses, or a not-counted call's reason, wrapping under it. */
  function ticketListRow(row, link, selected) {
    var missed = row.counted === false ? row.not_counted_reason || "No reason given"
      : (row.missed.length ? "Missed: " + row.missed.join(", ") : null);
    var tr = h("tr", { class: "rowlink" + (selected ? " sel" : ""), "data-ticket": String(row.ticket_id) },
      h("td", { class: "num trow-score", text: scoreText(row) }),
      h("td", { class: "trow-call" },
        h("div", { class: "trow-line" }, h("a", { href: link, text: "#" + row.ticket_id }), " ",
          h("b", { class: "trow-agent", text: row.agent_name }),
          h("span", { class: "muted", text: " · " + dayLabel(row.date, false) +
            (row.priority ? " · " + capitalize(row.priority) : "") }),
          h("span", { class: "trow-verdict" }, verdictBadge(row),
            row.counted !== false && row.needs_recheck ? recheckBadge() : null)),
        missed ? h("div", { class: "trow-missed", text: missed }) : null));
    tr.addEventListener("click", function (event) {
      if (!event.target.closest("a")) {
        location.hash = link;
      }
    });
    return tr;
  }

  function drawTicketList() {
    var data = tickets.data;
    var filters = tickets.filters;
    var rows = data.rows.slice(0, tickets.shown);
    var body = h("tbody");
    var notCounted = filters.verdict === "not_counted";
    rows.forEach(function (row) {
      body.appendChild(ticketListRow(row, ticketsHash(filters, row.ticket_id),
        String(row.ticket_id) === String(tickets.selected)));
    });
    var more = null;
    if (data.rows.length > rows.length) {
      more = h("button", { class: "small", type: "button", text: "Show " + Math.min(TICKETS_PAGE, data.rows.length - rows.length) + " more" });
      more.addEventListener("click", function () {
        tickets.shown += TICKETS_PAGE;
        drawTicketList();
      });
    }
    var title = plural(data.total, "call", "calls");
    var content = data.rows.length
      ? h("div", { class: "scroll" }, h("table", { class: "atable tlist" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Score" })),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Ticket, tech, date, priority and verdict; " +
            (notCounted ? "reason" : "missed checks") })))),
        body))
      : h("div", { class: "placeholder", role: "status", text: "No calls match these filters." });
    // Every call matching the filters, not only the ones shown: the server resolves the same filters.
    var reviewAll = data.total ? reviewAgainButton(data.total === 1 ? "Review this call again"
      : "Review these " + data.total + " again", { filters: onlyFilters(filters) }) : null;
    tickets.listBox.textContent = "";
    // The list button sits at the right of the panel's title line.
    tickets.listBox.appendChild(h("section", { class: "panel" },
      h("div", { class: "head-row" },
        h("div", null, h("h4", { class: "panel-label", text: title }), h("div", { class: "sub", text: notCounted
          ? "Calls the reviewer left out of every score, with its reason."
          : sortText(filters.sort) + ". Click a row to see its scorecard, evidence and transcript." })),
        reviewAll),
      content,
      h("div", { class: "nav-note" }, "Showing " + rows.length + " of " + data.total, more ? [" · ", more] : null)));
  }

  /* Review again: the call detail's button, and the list's "Review these N again". As in Settings' Run now,
     the estimate comes first, then Start. The run reads the calls fresh from Zendesk, scores them with the
     current rubric and model, and goes through the budget guard: one over the limit waits for your OK in the
     banner. `request` is what /api/review-again takes: { ticket_id } or { filters }. */
  function reviewAgainButton(label, request) {
    var message = h("div", { class: "message" });
    var box = h("div", { class: "estimate", hidden: true });
    var button = h("button", { type: "button", text: label });
    button.addEventListener("click", function () {
      box.hidden = true;
      button.disabled = true;
      setMessage(message, "Estimating… this reads the calls from Zendesk and can take a few minutes.");
      api("POST", "/api/review-again/estimate", request).then(function (estimate) {
        setMessage(message, "");
        showReviewAgainEstimate(box, request, estimate, message);
      }).catch(function (error) { setMessage(message, error.message, "error"); })
        .then(function () { button.disabled = false; });
    });
    return h("div", { class: "review-again" }, h("div", { class: "actions" }, button), message, box);
  }

  /* Review again's estimate, the calls it leaves out and why, and its Start button. */
  function showReviewAgainEstimate(box, request, estimate, message) {
    box.textContent = "";
    box.appendChild(h("div", null, h("b", { text: plural(estimate.contacts, "call", "calls") }),
      " to review again, estimated ", h("b", { text: money(estimate.estimate_usd) }),
      ", with the current rubric and model."));
    if (estimate.excluded_purged > 0) {
      box.appendChild(h("div", { class: "muted", text: estimate.excluded_purged === 1
        ? "1 call older than the retention period can't be reviewed again: its content was erased."
        : estimate.excluded_purged + " calls older than the retention period can't be reviewed again: their " +
          "content was erased." }));
    }
    if (estimate.excluded_held > 0) {
      box.appendChild(h("div", { class: "muted", text: estimate.excluded_held === 1
        ? "1 call is held by a run still in progress, so it is left out."
        : estimate.excluded_held + " calls are held by runs still in progress, so they are left out." }));
    }
    var overLimit = estimate.estimate_usd > estimate.budget_limit_usd;
    if (overLimit) {
      box.appendChild(h("div", { class: "over", text: overLimitText(estimate.budget_limit_usd, false) }));
    }
    if (!estimate.contacts) {
      box.appendChild(h("div", { class: "muted", text: "Nothing to review again." }));
      box.hidden = false;
      return;
    }
    var start = h("button", { class: "primary", type: "button",
      text: "Start review again" + (overLimit ? " (will wait for your OK)" : "") });
    var cancel = h("button", { type: "button", text: "Cancel", onclick: function () { box.hidden = true; } });
    start.addEventListener("click", function () {
      start.disabled = true;
      api("POST", "/api/review-again", request).then(function () {
        box.hidden = true;
        setMessage(message, "Review again started. It appears in the run history on the Settings tab in a few " +
          "seconds, and its results replace the ones shown here when it finishes. If its cost comes out above " +
          "your budget limit, it will wait for you to confirm it in the banner.", "ok");
        window.setTimeout(afterRunAction, 3000);
      }).catch(function (error) {
        start.disabled = false;
        setMessage(message, error.message, "error");
      });
    });
    box.appendChild(h("div", { class: "actions" }, start, cancel));
    box.hidden = false;
  }

  function markSelected() {
    if (!tickets.listBox) {
      return;
    }
    tickets.listBox.querySelectorAll("tr[data-ticket]").forEach(function (tr) {
      tr.classList.toggle("sel", tr.getAttribute("data-ticket") === String(tickets.selected));
    });
  }

  /* Reload the list (after an override) without touching the detail or the filters. */
  function reloadTicketList() {
    var key = tickets.key;
    getKept("/api/tickets" + queryString(tickets.filters)).then(function (data) {
      if (tickets.key === key && tickets.root && screen.contains(tickets.root)) {
        tickets.data = data;
        drawTicketList();
      }
    }).catch(function () { return null; });
  }

  function selectTicket(ticketId) {
    tickets.selected = ticketId || null;
    markSelected();
    tickets.detailToken += 1;
    var token = tickets.detailToken;
    var box = tickets.detailBox;
    box.textContent = "";
    if (!ticketId) {
      box.appendChild(panel("Call details", null, h("div", { class: "placeholder", role: "status",
        text: "Pick a call in the list to see its timeline, scorecard with evidence, and transcript." })));
      return;
    }
    var url = "/api/tickets/" + encodeURIComponent(ticketId);
    if (!isKept(url)) {
      box.appendChild(h("div", { class: "message", text: "Loading ticket #" + ticketId + "…" }));
    }
    readKept([url], function () { return token === tickets.detailToken; }, function (got, again) {
      drawDetail(got[0]);
      if (!again && window.matchMedia(SINGLE_COLUMN).matches) {
        box.scrollIntoView({ block: "start" });  // one column: the detail sits below the whole list
      }
    }).catch(function (error) {
      if (token === tickets.detailToken) {
        box.textContent = "";
        box.appendChild(panel("Ticket #" + ticketId, null, h("div", { class: "message error", role: "alert", text: error.message })));
      }
    });
  }

  /* "Claude Sonnet 5.5 · effort high". Reviews stored before effort was recorded say so. */
  function modelEffortText(label, effort) {
    return label + " · " + (effort ? "effort " + effort : "effort not recorded");
  }

  /* The model and effort that produced a review. */
  function reviewedByText(review) {
    return "Reviewed by " + modelEffortText(review.model_label, review.effort);
  }

  function drawDetail(detail) {
    var box = tickets.detailBox;
    var c = detail.contact;
    var r = detail.review;
    var removed = "Details removed after " + detail.retention_days + " days (retention).";
    box.textContent = "";
    var zendesk = detail.zendesk_url
      ? h("a", { href: detail.zendesk_url, target: "_blank", rel: "noopener noreferrer", text: "Open in Zendesk ↗" })
      : h("span", { class: "note", text: "Set the Zendesk subdomain to link to tickets" });
    var score = r.earned == null
      ? h("div", { class: "score", title: r.review_error ? "No score: the review did not finish" : "Not counted" }, h("b", { text: "-" }))
      : h("div", { class: "score", title: pct(r.percent) }, h("b", { text: String(r.earned) }), h("span", { text: "/" + r.total }));
    var notices = [];
    if (c.purged) {
      notices.push(h("div", { class: "note-banner", role: "note",
        text: removed + " The scores, outcomes and your overrides are kept." }));
    }
    if (r.review_error) {
      notices.push(h("div", { class: "banner" }, h("span", { class: "text" }, h("b", { text: "Review failed. " }),
        r.error ? "The reviewer could not finish this call: " + shortError(r.error) + ". "
          : "The reviewer could not finish this call. ",
        "Only measured checks have results, and the call is left out of every score. To score it, use " +
          "Review again below.")));
    }
    if (!r.counted) {
      notices.push(h("div", { class: "banner info" }, h("span", { class: "text" }, h("b", { text: "Not counted. " }),
        (r.not_counted_reason || "No reason given") + " This call is left out of every score and statistic.")));
    }
    if (!c.attribution_ok) {
      notices.push(h("div", { class: "note-banner", role: "note",
        text: "The answering tech could not be confirmed for this call, so it is marked Needs review." }));
    }
    // Three stacked panels: the header with its notices and timeline, the scorecard, then the comments.
    append(box, [h("section", { class: "panel detail" },
      h("div", { class: "dethead" },
        h("div", null,
          h("div", { class: "big" }, "Ticket #" + c.ticket_id + " ", verdictBadge(r)),
          c.subject ? h("div", { class: "subject", text: c.subject }) : null,
          h("div", { class: "muted" }, c.agent_name + " · " + (c.channel === "voice" ? "Call" : capitalize(c.channel)) + " · " + priorityText(c.priority) + " · " +
            dayLabel(c.date, true) + " · ", zendesk),
          h("div", { class: "note", text: "Resolved on the call: " + (RESOLVED_TEXT[r.resolved_on_call] || "Unknown") +
            " · Rubric version " + r.version_id }),
          h("div", { class: "note", text: reviewedByText(r) })),
        // The score, with Review again under it on the right.
        h("div", { class: "dethead-side" }, score, reviewAgainButton("Review again", { ticket_id: c.ticket_id }))),
      notices,
      timelinePanel(detail)),
    scorecardPanel(detail, removed),
    commentsPanel(detail, removed)]);
  }

  function clockLabel(isoUtc, startIso) {
    var when = new Date(isoUtc);
    var time = when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (startIso && new Date(startIso).toDateString() === when.toDateString()) {
      return time;
    }
    return when.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + time;
  }

  function minutesBetween(a, b) {
    return Math.round((new Date(b) - new Date(a)) / 60000);
  }

  function timelinePanel(detail) {
    var t = detail.timeline;
    var start = t.call_start;
    var purged = detail.contact.purged;
    var items = [];
    function add(at, text, kind, flag) {
      items.push({ at: at, text: text, kind: kind || "", flag: flag || null });
    }
    add(start, "Call answered");
    if (t.call_end) {
      var over = t.call_limit_at && t.call_end > t.call_limit_at ? minutesBetween(t.call_limit_at, t.call_end) : 0;
      add(t.call_end, "Call ended", over > 0 ? "bad" : "", over > 0 ? "✗ " + plural(over, "minute", "minutes") + " over" : null);
    }
    if (t.triage_macro_at) {
      add(t.triage_macro_at, "Triage macro posted");
    } else if (!purged) {
      add(null, "No triage macro found on the ticket", "bad");
    }
    if (t.call_limit_at) {
      add(t.call_limit_at, t.call_limit_minutes + "-minute limit", "lim");
    } else if (t.exempt) {
      add(null, "No call-length limit: this call's priority or tags are exempt", "lim");
    }
    if (t.callback_at) {
      add(t.callback_at, "Called the customer back");
    }
    if (t.summary_email_at) {
      var late = t.email_deadline_at && t.summary_email_at > t.email_deadline_at;
      add(t.summary_email_at, "Summary email sent", late ? "bad" : "", late ? "✗ after the deadline" : null);
    } else if (!purged) {
      add(null, "No summary email on this ticket", t.merged_into ? "lim" : "bad");
    }
    if (t.email_deadline_at) {
      add(t.email_deadline_at, "Summary email deadline (" + formatMinutes(t.email_deadline_minutes) + " after the call)", "lim");
    }
    // Timed entries form the strip, in time order. Untimed entries and the flags become chips under it.
    var timed = items.filter(function (i) { return i.at; }).sort(function (a, b) { return a.at < b.at ? -1 : (a.at > b.at ? 1 : 0); });
    var steps = timed.map(function (i) {
      return h("li", { class: "timeline-step" + (i.kind ? " " + i.kind : "") },
        h("span", { class: "timeline-time", text: clockLabel(i.at, start) }), h("span", { text: i.text }));
    });
    var chips = [];
    timed.forEach(function (i) {
      if (i.flag) {
        chips.push(h("span", { class: "chip timeline-chip bad", text: i.flag }));
      }
    });
    items.filter(function (i) { return !i.at; }).forEach(function (i) {
      chips.push(h("span", { class: "chip timeline-chip" + (i.kind ? " " + i.kind : ""), text: i.text }));
    });
    if (t.merged_into) {
      var m = t.merged_into;
      chips.push(h("span", { class: "chip timeline-chip" }, "Merged into ticket ",
        m.zendesk_url ? h("a", { href: m.zendesk_url, target: "_blank", rel: "noopener noreferrer", text: "#" + m.ticket_id })
          : "#" + m.ticket_id,
        m.subject ? ": " + m.subject : ""));
    }
    return h("div", { class: "timeline-block" }, h("h4", { class: "panel-label", text: "Timeline" }),
      h("ol", { class: "timeline-strip" }, steps),
      chips.length ? h("div", { class: "chips timeline-chips" }, chips) : null,
      purged ? h("div", { class: "note", text: "Only the call's start time is kept after the retention period." }) : null);
  }

  function checkLabel(check) {
    return check.kind === "scorecard" && check.number != null ? "#" + check.number + " " + check.summary : check.summary;
  }

  function outcomeMark(outcome) {
    if (outcome == null) {
      return h("span", { class: "muted", text: "-" });
    }
    var cls = outcome === "met" ? "okv" : (outcome === "missed" ? "flag crit" : (outcome === "unsure" ? "unsure" : "muted"));
    return h("span", { class: cls, title: OUTCOME_TEXT[outcome], text: OUTCOME_MARK[outcome] });
  }

  function scorecardPanel(detail, removed) {
    /* Every check, scorecard items and process rules together, grouped by the outcome that counts (after
       any override), in the API's order within a group. A check with no result (a review that did not
       finish) goes in the last group. Empty groups are left out. */
    function groupByResult(checks) {
      var groups = [
        { outcome: "missed", label: "Missed", cls: "warn" },
        { outcome: "unsure", label: "Unsure", cls: "unsure" },
        { outcome: "na", label: "Not applicable", cls: null },
        { outcome: "met", label: "Met", cls: "good" },
        { outcome: null, label: "No result", cls: null }
      ];
      var known = groups.map(function (g) { return g.outcome; });
      return groups.map(function (g) {
        return { label: g.label, cls: g.cls, checks: checks.filter(function (c) {
          return (known.indexOf(c.effective) >= 0 ? c.effective : null) === g.outcome;
        }) };
      }).filter(function (g) { return g.checks.length; });
    }
    var body = h("tbody");
    groupByResult(detail.checks).forEach(function (group) {
      body.appendChild(h("tr", { class: "group-head" }, h("td", { colspan: "5" },
        h("span", { class: group.cls, text: group.label }), " (" + group.checks.length + ")")));
      group.checks.forEach(function (check) {
        appendCheck(body, detail, check, removed);
      });
    });
    var overridden = detail.checks.filter(function (c) { return c.override_outcome != null; }).length;
    return panel("Scorecard and process rules", "Every check with Claude's evidence. Disagree changes the outcome that counts; " +
        "the reviewer's own call is kept." + (overridden ? " You have overridden " + plural(overridden, "check", "checks") + " here." : ""),
      h("div", { class: "scroll" }, h("table", { class: "atable det" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", class: "ic" }, h("span", { class: "sr-only", text: "Outcome" })),
          h("th", { scope: "col", text: "Check" }),
          h("th", { scope: "col", class: "num", text: "Points" }),
          h("th", { scope: "col", text: "Evidence" }),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Disagree" })))),
        body)));
  }

  function appendCheck(body, detail, check, removed) {
    var purged = detail.contact.purged;
    var by = check.decided_by === "code" ? "Measured" : (check.decided_by === "model" ? "Reviewer" : "Not reviewed");
    var notes = [by];
    if (check.kind !== "scorecard") {
      notes.push("process rule");
    }
    if (check.auto_fail) {
      notes.push("auto-fail");
    }
    var evidence = h("td", { class: "ev" });
    if (purged) {
      evidence.appendChild(h("span", { class: "muted", text: removed }));
    } else if (check.evidence) {
      evidence.appendChild(h("div", { class: "ev-text", text: check.evidence }));
    } else if (check.outcome == null) {
      evidence.appendChild(h("span", { class: "muted", text: "No result: the review did not finish." }));
    }
    if (check.override_outcome != null) {
      evidence.appendChild(h("div", { class: "override" },
        h("b", { text: "Your call: " + OUTCOME_TEXT[check.override_outcome] }),
        " (reviewer said " + (OUTCOME_TEXT[check.outcome] || check.outcome) + ")",
        check.override_note ? h("div", { class: "override-note", text: check.override_note }) : null));
    }
    var editorRow = h("tr", { class: "override-row", hidden: true });
    var action = null;
    if (check.outcome != null) {
      action = h("button", { class: "disagree", type: "button", "aria-expanded": "false",
        "aria-label": (check.override_outcome != null ? "Change your override of " : "Disagree with ") + checkLabel(check),
        text: check.override_outcome != null ? "Change" : "Disagree" });
      action.addEventListener("click", function () {
        var open = editorRow.hidden;
        editorRow.hidden = !open;
        action.setAttribute("aria-expanded", String(open));
        editorRow.textContent = "";
        if (open) {
          editorRow.appendChild(h("td", { colspan: "5" }, overrideForm(detail, check, function () {
            editorRow.hidden = true;
            action.setAttribute("aria-expanded", "false");
          })));
          var first = editorRow.querySelector("select");
          if (first) {
            first.focus();
          }
        }
      });
    }
    var points = check.earned_points == null ? h("span", { class: "muted", text: "-" }) : check.earned_points + " / " + check.points;
    body.appendChild(h("tr", { class: check.effective === "missed" ? "flagrow" : null },
      h("td", { class: "ic" }, outcomeMark(check.effective)),
      h("td", null, checkLabel(check), check.needs_recheck ? [" ", recheckBadge()] : null,
        h("div", { class: "note", text: notes.join(" · ") })),
      h("td", { class: "num nowrap" }, points),
      evidence,
      h("td", null, action)));
    body.appendChild(editorRow);
  }

  function overrideForm(detail, check, close) {
    var ids = "ov-" + detail.review.review_id + "-" + check.check_id;
    var initial = check.override_outcome || (check.outcome === "missed" ? "met" : "missed");
    var outcome = h("select", { id: ids + "-outcome" }, ["met", "missed", "na", "unsure"].map(function (o) {
      return h("option", { value: o, text: OUTCOME_TEXT[o] + (o === check.outcome ? " (the reviewer's call)" : ""),
        selected: o === initial ? "selected" : null });
    }));
    var note = h("textarea", { id: ids + "-note", rows: "2", maxlength: String(NOTE_MAX_CHARS),
      placeholder: "Why you disagree (optional)" });
    note.value = check.override_note || "";
    var message = h("div", { class: "message", role: "status" });
    var save = h("button", { class: "primary", type: "submit", text: "Save my call" });
    var cancel = h("button", { type: "button", text: "Cancel", onclick: close });
    var undo = check.override_outcome != null
      ? h("button", { type: "button", class: "undo", text: "Undo my override" }) : null;
    var url = "/api/reviews/" + detail.review.review_id + "/checks/" + encodeURIComponent(check.check_id) + "/override";
    function done(updated) {
      drawDetail(updated);
      reloadTicketList();
    }
    function fail(error) {
      save.disabled = false;
      if (undo) {
        undo.disabled = false;
      }
      setMessage(message, error.message, "error");
    }
    var form = h("form", { class: "override-form" },
      h("div", { class: "override-fields" },
        h("label", { for: ids + "-outcome" }, h("b", { text: "The right outcome" }), outcome),
        h("label", { for: ids + "-note" }, h("b", { text: "Note" }), note)),
      h("div", { class: "actions" }, save, cancel, undo, message));
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      save.disabled = true;
      api("PUT", url, { outcome: outcome.value, note: note.value }).then(done).catch(fail);
    });
    if (undo) {
      undo.addEventListener("click", function () {
        undo.disabled = true;
        api("DELETE", url).then(done).catch(fail);
      });
    }
    return form;
  }

  function commentList(comments) {
    return comments.map(function (c) {
      var head = [COMMENT_KIND[c.kind] || "Comment"];
      if (c.author_name) {
        head.push(c.author_name);
      }
      if (c.created_at) {
        head.push(timeLabel(c.created_at));
      }
      head.push(c.public ? "public reply" : "internal note");
      return h("div", { class: "comment" + (c.kind === "transcript" ? " transcript" : "") },
        h("div", { class: "comment-head", text: head.join(" · ") }),
        h("div", { class: "comment-body", text: c.text }),
        c.images ? h("div", { class: "note", text: plural(c.images, "image", "images") + " in Zendesk, not shown here" }) : null);
    });
  }

  /* One panel for the call's comments; a merged-into ticket's comments follow under their own heading. */
  function commentsPanel(detail, removed) {
    var title = "Call summary, transcript and notes";
    if (detail.contact.purged) {
      return panel(title, null, h("div", { class: "placeholder", text: removed }));
    }
    var merged = detail.timeline.merged_into;
    return panel(title, "The tech's part of the ticket, as reviewed, oldest first.",
      detail.comments.length ? commentList(detail.comments)
        : h("div", { class: "placeholder", text: "No comments were stored for this call." }),
      merged ? h("section", { class: "merged-comments" },
        h("h4", { class: "group-head", text: "Merged into ticket #" + merged.ticket_id }),
        h("div", { class: "sub", text: (merged.subject ? merged.subject + ". " : "") +
          "Its comments from this call's start until 24 hours after the call ends, or until the call was " +
            "reviewed if that came first." }),
        detail.merged_comments.length ? commentList(detail.merged_comments)
          : h("div", { class: "placeholder", text: "No comments from that window." })) : null);
  }

  // -- rubric & rules ---------------------------------------------------------------------------

  /* The Rubric & rules screen: the scorecard, process rules (with the timed rules' settings) and
     reference documents, edited as one draft and saved as a new version with a note; and the version
     history (any version viewable read-only). Each check's row shows its settings as text; its inputs,
     except the on/off switch, are in its editor behind Edit. Re-scoring a past range is Settings' Run now.
     The draft keeps each check's permanent id; checks left out of it are retired when it is saved. */

  var RUBRIC_BANNER = "Changes apply from the next run. Past results keep the rubric version they were scored " +
    "with and are never re-scored automatically, so trends stay fair. ";
  var AGREEMENT_WEEKS = 8;
  var VERSION_NOTE_MAX = 500;     // rubric_edit.NOTE_MAX_CHARS
  var MAX_POINTS = 100;           // rubric_edit.MAX_POINTS
  var CONFIRM_LINES = 12;         // changes listed in the save confirmation before "and N more"
  var CHECK_BODY_FIELDS = ["check_id", "kind", "summary", "definition", "points", "auto_fail", "applies_when", "enabled"];
  var TIMED_TEXT = { call_length: "Measured: call length", email_deadline: "Measured: summary email time" };
  var DEADLINE_MAX_MINUTES = 1440;   // the summary email deadline's longest setting (24 hours)
  var DEADLINE_WARN_MINUTES = 360;   // longer than the overnight gap before the daily 06:00 run
  var DEADLINE_WARNING = "The daily 06:00 run may review calls before this deadline has passed.";
  var RERUN_POINTER = "To score past calls under this version, use Run now in Settings.";
  var RERUN_LINK = "Run now in Settings";   // the part of RERUN_POINTER that links to Settings
  var AGREEMENT_TITLE = "How often you kept the reviewer's outcome for this check (didn't Disagree), over the last " +
    AGREEMENT_WEEKS + " weeks.";

  var rb = null;        // the screen's state while it is shown
  var draftKeys = 0;

  function draftKey() {
    draftKeys += 1;
    return "d" + draftKeys;
  }

  function renderRubric(token, rest) {
    rb = null;
    var viewing = rest[0] === "version";
    if (viewing && !/^\d+$/.test(rest[1] || "")) {
      clearScreen();
      screen.appendChild(h("div", { class: "message error", text: "There is no such rubric version." }));
      return;
    }
    loadingLine("Loading the rubric…");
    var agreement = "/api/agreement?weeks=" + AGREEMENT_WEEKS;
    // The agreement rates are extra: the rubric shows without them (null) when they can't be read.
    function read(url) {
      return url === agreement ? readJson(url).catch(function () { return null; }) : readJson(url);
    }
    readScreen(token, [viewing ? "/api/rubric/versions/" + rest[1] : "/api/rubric", agreement, "/api/settings"],
      function (results) {
        if (rubricDirty()) {
          return;   // a changed answer never throws away edits not saved yet
        }
        rb = { base: results[0], agreement: results[1], settings: results[2], readOnly: viewing, note: "",
          versions: null, els: {} };
        resetDraft();
        drawRubric();
      }, read).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      clearScreen();
      screen.appendChild(h("div", { class: "message error", text: error.message }));
    });
  }

  function draftCheck(check) {
    return Object.assign({}, check, { _key: draftKey() });
  }

  function resetDraft() {
    var v = rb.base;
    rb.draft = {
      scorecard: v.checks.filter(function (c) { return c.kind === "scorecard"; }).map(draftCheck),
      process: v.checks.filter(function (c) { return c.kind === "process"; }).map(draftCheck),
      docs: Object.keys(v.reference).map(function (title) {
        return { _key: draftKey(), origTitle: title, title: title, text: v.reference[title] };
      }),
      params: {
        call_limit_minutes: String(v.params.call_limit_minutes),
        exempt_priorities: v.params.exempt_priorities.join(", "),
        exempt_tags: v.params.exempt_tags.join(", "),
        email_deadline_minutes: String(v.params.email_deadline_minutes)
      }
    };
    rb.retired = [];
    rb.open = {};
  }

  function rubricDirty() {
    return Boolean(rb && !rb.readOnly && changeList().length);
  }

  function drawRubric() {
    clearScreen();
    rb.els = {};
    var historyBox = h("section", { class: "panel", id: "rb-history" });
    if (rb.readOnly) {
      screen.appendChild(h("div", { class: "banner info" },
        h("span", { class: "text" }, "You're viewing version " + rb.base.version_id + ", saved " +
          timeLabel(rb.base.created_at) + " (read-only): ", h("b", { text: rb.base.note })),
        h("a", { href: "#/rubric", text: "Back to the current rubric" })));
    } else {
      screen.appendChild(h("div", { class: "note-banner" }, RUBRIC_BANNER,
        h("a", { href: "#/rubric", text: "Version history", onclick: function (event) {
          event.preventDefault();
          historyBox.scrollIntoView({ behavior: "smooth" });
        } })));
    }
    rb.els.scorecard = h("section", { class: "panel" });
    rb.els.process = h("section", { class: "panel" });
    rb.els.reference = h("section", { class: "panel" });
    screen.appendChild(appliesList());
    // Spec 5.4: the editable tables on the left; saving, reference documents and history on the right.
    var side = rb.readOnly ? [rb.els.reference, historyBox]
      : [savePanel(), rb.els.reference, historyBox];
    screen.appendChild(h("div", { class: "panel-row cols-2-1" },
      h("div", { class: "stack" }, rb.els.scorecard, rb.els.process),
      h("div", { class: "stack" }, side)));
    fillChecks("scorecard");
    fillChecks("process");
    fillReference();
    fillVersions(historyBox);
    refreshChanges();
  }

  function appliesList() {
    var seen = {};
    rb.draft.scorecard.concat(rb.draft.process).forEach(function (c) { seen[c.applies_when] = true; });
    return h("datalist", { id: "rb-applies" }, Object.keys(seen).sort().map(function (text) {
      return h("option", { value: text });
    }));
  }

  // The scorecard and process rules tables

  function agreementCell(check) {
    var rate = rb.agreement && check.check_id ? rb.agreement.by_check[check.check_id] : null;
    return h("td", { class: "num", title: AGREEMENT_TITLE },
      h("span", { class: "mlabel", text: "Agreement" }), rate == null ? "-" : pct(rate * 100));
  }

  function toggleCell(check, field, label, onChange) {
    if (rb.readOnly) {
      return h("td", null, h("span", { class: "mlabel", text: label }), check[field] ? "Yes" : "-");
    }
    var box = h("input", { type: "checkbox", checked: check[field],
      "aria-label": label + ": " + (check.summary || "new check") });
    box.addEventListener("change", function () {
      check[field] = box.checked;
      if (onChange) {
        onChange();
      }
      refreshChanges();
    });
    return h("td", null, h("label", { class: "tog" }, box, h("span", { class: "mlabel", text: label })));
  }

  function pointsText(check) {
    return String(check.points).trim() || "-";
  }

  function titleCase(word) {
    return word.charAt(0).toUpperCase() + word.slice(1);
  }

  /* A timed rule's settings in one line for its view row, from the draft: "Limit 10 min · Urgent exempt" or
     "Within 24 h of the call". Untimed checks have none. */
  function timedSummary(check) {
    var p = draftParams();
    if (check.timed === "call_length") {
      var parts = ["Limit " + p.call_limit_minutes + " min"];
      if (p.exempt_priorities.length) {
        parts.push(p.exempt_priorities.map(titleCase).join(", ") + " exempt");
      }
      if (p.exempt_tags.length) {
        parts.push("Tags: " + p.exempt_tags.join(", "));
      }
      return parts.join(" · ");
    }
    if (check.timed === "email_deadline") {
      var deadline = p.email_deadline_minutes;
      return "Within " + (typeof deadline === "number" ? formatMinutes(deadline) : deadline + " min") + " of the call";
    }
    return "";
  }

  // A timed rule's settings, in its editor. They are the version's settings, held in the draft's params.
  function timedSettings(check, refs) {
    var p = rb.draft.params;
    var box = h("div", { class: "timed" });
    // Shown while the deadline is longer than the overnight gap; it never blocks the save.
    var deadlineWarning = h("div", { class: "note warn", role: "note", text: DEADLINE_WARNING });
    function warnDeadline() {
      var minutes = wholeOrRaw(p.email_deadline_minutes);
      var show = typeof minutes === "number" && minutes > DEADLINE_WARN_MINUTES;
      if (show && !deadlineWarning.parentNode) {
        box.appendChild(deadlineWarning);
      } else if (!show && deadlineWarning.parentNode) {
        deadlineWarning.remove();
      }
    }
    function field(key, label, type, after, max) {
      var input = h("input", { type: type, value: p[key], class: type === "number" ? "pts" : "tag-field" });
      if (type === "number") {
        input.min = "1";
        input.step = "1";
        if (max) {
          input.max = String(max);
        }
      } else {
        input.placeholder = "none";
      }
      input.addEventListener("input", function () {
        p[key] = input.value;
        refs.refresh();
        refreshChanges();
        if (key === "email_deadline_minutes") {
          warnDeadline();
        }
      });
      return h("label", null, label, input, after);
    }
    append(box, h("span", { class: "chip", text: TIMED_TEXT[check.timed] || check.timed }));
    if (check.timed === "call_length") {
      append(box, [field("call_limit_minutes", "Limit", "number", "minutes"),
        field("exempt_priorities", "Exempt priorities", "text", null),
        field("exempt_tags", "Exempt tags", "text", null),
        h("div", { class: "note", text: "Separate priorities and tags with commas." })]);
    } else if (check.timed === "email_deadline") {
      append(box, field("email_deadline_minutes", "Deadline", "number", "minutes after the call ends", DEADLINE_MAX_MINUTES));
      warnDeadline();
    }
    return box;
  }

  // A scorecard item's points and auto-fail, in its editor.
  function scoringFields(check, refs, id) {
    var points = h("input", { type: "number", id: id + "-points", min: "0", max: String(MAX_POINTS), step: "1",
      value: check.points, class: "pts" });
    points.addEventListener("input", function () {
      check.points = /^\d+$/.test(points.value.trim()) ? Number(points.value.trim()) : points.value;
      refs.refresh();
      refreshChanges();
    });
    var autoFail = h("input", { type: "checkbox", id: id + "-auto-fail", checked: check.auto_fail });
    autoFail.addEventListener("change", function () {
      check.auto_fail = autoFail.checked;
      refs.refresh();
      refreshChanges();
    });
    return [
      h("label", { for: id + "-points" }, h("b", { text: "Points" }),
        h("span", { class: "note", text: "A whole number from 0 to " + MAX_POINTS + "." }), points),
      h("label", { class: "tog", for: id + "-auto-fail" }, autoFail, h("b", { text: "Auto-fail" }),
        h("span", { class: "note", text: "A miss on this item makes the call's score 0." }))
    ];
  }

  /* A check's editor, opened by Edit. Every change goes straight into the draft, so closing the editor keeps
     it (only Discard drops the draft) and refs.refresh keeps the view row showing it. */
  function checkEditor(check, refs, close) {
    if (rb.readOnly) {
      return h("div", { class: "rb-editor" },
        h("div", null, h("b", { text: "Definition" }), h("div", { class: "rb-def", text: check.definition })),
        h("div", null, h("b", { text: "Applies when" }), h("div", { class: "rb-def", text: check.applies_when })));
    }
    var id = "rb-" + check._key;
    var summary = h("input", { type: "text", id: id + "-summary", value: check.summary });
    var definition = h("textarea", { id: id + "-definition", rows: "5" });
    definition.value = check.definition;
    var applies = h("input", { type: "text", id: id + "-applies", list: "rb-applies", value: check.applies_when,
      placeholder: "Always" });
    summary.addEventListener("input", function () {
      check.summary = summary.value;
      refs.title.textContent = check.summary.trim() || "(no summary yet)";
      refreshChanges();
    });
    definition.addEventListener("input", function () {
      check.definition = definition.value;
      refreshChanges();
    });
    applies.addEventListener("input", function () {
      check.applies_when = applies.value;
      refs.applies.textContent = check.applies_when.trim() || "Always";
      refreshChanges();
    });
    return h("div", { class: "rb-editor" },
      h("label", { for: id + "-summary" }, h("b", { text: check.kind === "scorecard" ? "Summary" : "Rule name" }), summary),
      h("label", { for: id + "-definition" }, h("b", { text: "Definition" }),
        h("span", { class: "note", text: "In your words. The reviewer judges this check from this text. Refer to a " +
          "reference document by its title." }), definition),
      h("label", { for: id + "-applies" }, h("b", { text: "Applies when" }),
        h("span", { class: "note", text: "Plain language, for example \"Only when the issue was not resolved on the " +
          "call\". Blank means Always. When it doesn't apply, the check is N/A." }), applies),
      check.kind === "scorecard" ? scoringFields(check, refs, id) : null,
      check.timed ? h("div", null, h("b", { text: "Timed settings" }), timedSettings(check, refs)) : null,
      h("div", { class: "actions" }, h("button", { type: "button", text: "Done", onclick: close })));
  }

  function fillChecks(kind) {
    var box = rb.els[kind];
    var list = rb.draft[kind];
    var scorecard = kind === "scorecard";
    // Two lines per check so the table fits its column: these on line one, then Applies when (with any timed
    // summary) and the actions. Edit opens a check's editor under them.
    var columns = scorecard ? ["#", "Summary", "Points", "On", "Agreement"] : ["Rule", "On", "Agreement"];
    box.textContent = "";
    var title = h("h4", null, scorecard ? "Scorecard " : "Process rules ");
    if (scorecard) {
      rb.els.total = h("span", { class: "count" });
      title.appendChild(rb.els.total);
    } else {
      title.appendChild(h("span", { class: "count", text: "(pass/fail, no points)" }));
    }
    box.appendChild(title);
    box.appendChild(h("div", { class: "sub", text: scorecard
      ? "The number is display order only; each item keeps its identity when reordered or reworded."
      : "Each rule is judged from its definition, like the scorecard. Two rules are measured from the ticket's " +
        "timestamps; their settings show after Applies when, and Edit changes them." }));
    if (scorecard) {
      rb.els.totalNote = h("div", { class: "hint-box", hidden: true });
      box.appendChild(rb.els.totalNote);
    }
    var table = h("table", { class: "atable two-line rb" },
      h("thead", null,
        h("tr", { class: "line1" }, columns.map(function (label, i) {
          var numeric = scorecard ? (i === 0 || i === 2 || i === 4) : i === 2;
          return h("th", { scope: "col", class: numeric ? "num" : null,
            title: label === "Agreement" ? AGREEMENT_TITLE : null, text: label });
        })),
        h("tr", { class: "line2" },
          scorecard ? h("td", { class: "rb-pad" }) : null,
          h("th", { scope: "col", text: "Applies when" }),
          h("th", { scope: "col", colspan: String(columns.length - (scorecard ? 2 : 1)) },
            h("span", { class: "sr-only", text: "Actions" })))));
    list.forEach(function (check, index) {
      appendCheckRow(table, kind, check, index, columns.length);
    });
    box.appendChild(h("div", { class: "scroll rb-scroll" }, table));
    if (!list.length) {
      box.appendChild(h("div", { class: "placeholder", text: scorecard ? "No scorecard items." : "No process rules." }));
    }
    if (!scorecard) {
      var threshold = rb.settings.problem_threshold;
      box.appendChild(h("div", { class: "nav-note" }, "What counts as a problem ticket: ",
        h("b", { text: threshold === 1 ? "any miss" : threshold + " or more misses on one call" }), " · ",
        h("a", { href: "#/settings", text: "Change in Settings" })));
    }
    if (rb.readOnly) {
      return;
    }
    var retired = rb.retired.filter(function (r) { return r.kind === kind; });
    if (retired.length) {
      box.appendChild(h("div", { class: "rb-retired" }, h("span", { class: "muted", text: "Retired when you save: " }),
        retired.map(function (item) {
          return h("span", { class: "chip" }, item.check.summary + " ",
            h("button", { class: "small", type: "button", text: "Undo", onclick: function () { unretire(item); } }));
        })));
    }
    box.appendChild(h("div", { class: "actions" }, h("button", { type: "button",
      text: scorecard ? "+ Add item" : "+ Add rule", onclick: function () { addCheck(kind); } })));
  }

  function appendCheckRow(table, kind, check, index, width) {
    var scorecard = kind === "scorecard";
    var list = rb.draft[kind];
    // The row shows the check's settings as text, which refs.refresh keeps current from the draft. Their inputs
    // are in the editor, so the on/off switch is the row's only input.
    var refs = {
      title: h("span", { class: "rb-title", text: check.summary.trim() || "(no summary yet)" }),
      tags: h("span"),
      points: h("span"),
      applies: h("span", { text: check.applies_when.trim() || "Always" }),
      timed: h("span")
    };
    refs.refresh = function () {
      var tags = [];
      if (!check.check_id) {
        tags.push(h("span", { class: "chip", text: "new" }));
      }
      if (check.auto_fail && scorecard) {
        tags.push(h("span", { class: "chip", text: "auto-fail" }));
      }
      refs.tags.textContent = "";
      append(refs.tags, tags.length ? [" ", tags] : null);
      refs.points.textContent = pointsText(check);
      var timed = timedSummary(check);
      refs.timed.textContent = timed ? " · " + timed : "";
    };
    refs.refresh();
    function lineClass(line) {
      return check.enabled ? line : line + " off";
    }
    var group = h("tbody", { class: "row-group" });
    var line1 = h("tr", { class: lineClass("line1") });
    var line2 = h("tr", { class: lineClass("line2") });
    var editorRow = h("tr", { class: "edit-row", hidden: !rb.open[check._key] });
    var toggle = h("button", { class: "small", type: "button", text: rb.readOnly ? "Definition" : "Edit",
      "aria-expanded": String(Boolean(rb.open[check._key])) });

    function close() {
      rb.open[check._key] = false;
      editorRow.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    }
    function open() {
      rb.open[check._key] = true;
      editorRow.textContent = "";
      editorRow.appendChild(h("td", { colspan: String(width) }, checkEditor(check, refs, close)));
      editorRow.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
    }
    toggle.addEventListener("click", function () {
      if (editorRow.hidden) {
        open();
      } else {
        close();
      }
    });

    var summaryCell = h("td", { class: "rb-summary" }, refs.title, refs.tags);
    if (scorecard) {
      line1.appendChild(h("td", { class: "num rb-num", text: String(index + 1) }));
    }
    line1.appendChild(summaryCell);
    if (scorecard) {
      line1.appendChild(h("td", { class: "num" }, h("span", { class: "mlabel", text: "Points" }), refs.points));
    }
    line1.appendChild(toggleCell(check, "enabled", "On", function () {
      line1.classList.toggle("off", !check.enabled);
      line2.classList.toggle("off", !check.enabled);
    }));
    line1.appendChild(agreementCell(check));
    if (scorecard) {
      line2.appendChild(h("td", { class: "rb-pad" }));
    }
    line2.appendChild(h("td", { class: "rb-applies" }, h("span", { class: "mlabel", text: "Applies when" }),
      refs.applies, refs.timed));
    var actions = h("td", { class: "act", colspan: String(width - (scorecard ? 2 : 1)) }, toggle);
    if (!rb.readOnly) {
      actions.appendChild(h("button", { class: "small", type: "button", text: "Retire",
        "aria-label": "Retire " + (check.summary || "this check"), onclick: function () { retireCheck(kind, index); } }));
      actions.appendChild(h("button", { class: "small", type: "button", text: "↑", "aria-label": "Move up",
        disabled: index === 0, onclick: function () { moveCheck(kind, index, -1); } }));
      actions.appendChild(h("button", { class: "small", type: "button", text: "↓", "aria-label": "Move down",
        disabled: index === list.length - 1, onclick: function () { moveCheck(kind, index, 1); } }));
    }
    line2.appendChild(actions);
    group.appendChild(line1);
    group.appendChild(line2);
    group.appendChild(editorRow);
    table.appendChild(group);
    if (rb.open[check._key]) {
      open();
    }
  }

  function addCheck(kind) {
    var check = { _key: draftKey(), check_id: null, kind: kind, number: null, summary: "", definition: "", points: 0,
      auto_fail: false, applies_when: "Always", enabled: true, timed: null };
    rb.draft[kind].push(check);
    rb.open[check._key] = true;
    fillChecks(kind);
    refreshChanges();
    var input = document.getElementById("rb-" + check._key + "-summary");
    if (input) {
      input.focus();
    }
  }

  function retireCheck(kind, index) {
    var check = rb.draft[kind][index];
    if (check.timed && !window.confirm("Retire \"" + check.summary + "\"?\n\nIt's a timed rule: once saved, adding it " +
      "back needs code. To stop judging it for now, turn it off instead.\n\nRetire it anyway?")) {
      return;
    }
    var list = rb.draft[kind];
    var before = index > 0 ? list[index - 1] : null;
    var after = index + 1 < list.length ? list[index + 1] : null;
    list.splice(index, 1);
    rb.open[check._key] = false;
    if (check.check_id) {
      rb.retired.push({ kind: kind, check: check, index: index, before: before, after: after });
    }
    fillChecks(kind);
    refreshChanges();
  }

  function unretire(item) {
    rb.retired = rb.retired.filter(function (r) { return r !== item; });
    // Back where it was: before its old next neighbour, else after its old previous one, else at its old place.
    var list = rb.draft[item.kind];
    var at = item.after ? list.indexOf(item.after) : -1;
    if (at < 0 && item.before && list.indexOf(item.before) >= 0) {
      at = list.indexOf(item.before) + 1;
    }
    if (at < 0) {
      at = Math.min(item.index, list.length);
    }
    list.splice(at, 0, item.check);
    fillChecks(item.kind);
    refreshChanges();
  }

  function moveCheck(kind, index, step) {
    var list = rb.draft[kind];
    var other = index + step;
    if (other < 0 || other >= list.length) {
      return;
    }
    var moved = list[index];
    list[index] = list[other];
    list[other] = moved;
    fillChecks(kind);
    refreshChanges();
  }

  // Reference documents

  function words(text) {
    return " " + text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim() + " ";
  }

  function rulesMentioning(titles) {
    var wanted = titles.filter(Boolean).map(function (t) { return words(t); }).filter(function (t) { return t.trim(); });
    return rb.draft.scorecard.concat(rb.draft.process).filter(function (check) {
      var text = words(check.definition);
      return wanted.some(function (t) { return text.indexOf(t) >= 0; });
    });
  }

  function checkName(check) {
    var index = rb.draft.scorecard.indexOf(check);
    return index >= 0 ? "#" + (index + 1) + " " + check.summary : check.summary;
  }

  function lineCount(text) {
    var lines = text.trim() ? text.trim().split("\n").length : 0;
    return lines ? plural(lines, "line", "lines") : "empty: rules that need it are marked Unsure";
  }

  function fillReference() {
    var box = rb.els.reference;
    box.textContent = "";
    box.appendChild(h("h4", { text: "Reference material the reviewer uses" }));
    box.appendChild(h("div", { class: "sub", text: "Rules refer to these documents by title. If a rule needs a " +
      "document that's missing or empty, the reviewer marks that rule Unsure." }));
    rb.draft.docs.forEach(function (doc, index) {
      box.appendChild(docItem(doc, index));
    });
    if (!rb.draft.docs.length) {
      box.appendChild(h("div", { class: "placeholder", text: "No reference documents." }));
    }
    if (!rb.readOnly) {
      box.appendChild(h("div", { class: "actions" }, h("button", { type: "button", text: "+ Add document",
        onclick: addDoc })));
    }
  }

  function docItem(doc, index) {
    var title = h("b", { text: doc.title.trim() || "(untitled)" });
    var meta = h("span", { class: "muted", text: " · " + lineCount(doc.text) });
    var editor = h("div", { hidden: !rb.open[doc._key] });
    var toggle = h("button", { class: "small", type: "button", text: rb.readOnly ? "View" : "Edit",
      "aria-expanded": String(Boolean(rb.open[doc._key])) });
    function draw() {
      editor.textContent = "";
      if (rb.readOnly) {
        editor.appendChild(h("div", { class: "ref-text", text: doc.text || "(empty)" }));
        return;
      }
      var id = "rb-" + doc._key;
      var titleInput = h("input", { type: "text", id: id + "-title", value: doc.title });
      var text = h("textarea", { id: id + "-text", rows: "8" });
      text.value = doc.text;
      titleInput.addEventListener("input", function () {
        doc.title = titleInput.value;
        title.textContent = doc.title.trim() || "(untitled)";
        refreshChanges();
      });
      text.addEventListener("input", function () {
        doc.text = text.value;
        meta.textContent = " · " + lineCount(doc.text);
        refreshChanges();
      });
      editor.appendChild(h("div", { class: "rb-editor" },
        h("label", { for: id + "-title" }, h("b", { text: "Title" }),
          h("span", { class: "note", text: "Rules mention the document by this title." }), titleInput),
        h("label", { for: id + "-text" }, h("b", { text: "Text" }), text),
        h("div", { class: "actions" }, h("button", { type: "button", text: "Done", onclick: function () { setOpen(false); } }))));
    }
    function setOpen(on) {
      rb.open[doc._key] = on;
      if (on) {
        draw();
      }
      editor.hidden = !on;
      toggle.setAttribute("aria-expanded", String(on));
    }
    toggle.addEventListener("click", function () { setOpen(editor.hidden); });
    if (rb.open[doc._key]) {
      draw();
    }
    var actions = h("span", { class: "act" }, toggle);
    if (!rb.readOnly) {
      actions.appendChild(h("button", { class: "small", type: "button", text: "Remove",
        "aria-label": "Remove " + (doc.title || "this document"), onclick: function () { removeDoc(index); } }));
    }
    return h("div", { class: "ref" }, h("div", { class: "ref-head" }, title, meta, actions), editor);
  }

  function addDoc() {
    var doc = { _key: draftKey(), origTitle: null, title: "", text: "" };
    rb.draft.docs.push(doc);
    rb.open[doc._key] = true;
    fillReference();
    refreshChanges();
    var input = document.getElementById("rb-" + doc._key + "-title");
    if (input) {
      input.focus();
    }
  }

  function removeDoc(index) {
    var doc = rb.draft.docs[index];
    var name = doc.title.trim() || doc.origTitle || "this untitled document";
    var mentions = rulesMentioning([doc.title, doc.origTitle]);
    var text = "Remove the reference document \"" + name + "\"?";
    if (mentions.length) {
      text += "\n\nThese rules mention it by title. The reviewer will mark them Unsure until you reword them:\n- " +
        mentions.map(checkName).join("\n- ");
    }
    if (!window.confirm(text)) {
      return;
    }
    rb.draft.docs.splice(index, 1);
    fillReference();
    refreshChanges();
  }

  // What changed, and saving

  function listText(raw) {
    var seen = [];
    raw.split(",").forEach(function (item) {
      var word = item.trim().toLowerCase();
      if (word && seen.indexOf(word) < 0) {
        seen.push(word);
      }
    });
    return seen;
  }

  function wholeOrRaw(raw) {
    var text = String(raw).trim();
    return /^\d+$/.test(text) ? Number(text) : text;
  }

  function draftParams() {
    var p = rb.draft.params;
    return {
      call_limit_minutes: wholeOrRaw(p.call_limit_minutes),
      exempt_priorities: listText(p.exempt_priorities),
      exempt_tags: listText(p.exempt_tags),
      email_deadline_minutes: wholeOrRaw(p.email_deadline_minutes)
    };
  }

  function quoted(text) {
    return "“" + text + "”";
  }

  function checkChanges(base, check) {
    var diffs = [];
    if (check.summary.trim() !== base.summary) {
      diffs.push("summary");
    }
    if (check.definition.trim() !== base.definition) {
      diffs.push("definition");
    }
    if ((check.applies_when.trim() || "Always") !== base.applies_when) {
      diffs.push("applies when");
    }
    if (String(check.points) !== String(base.points)) {
      diffs.push("points " + base.points + " → " + check.points);
    }
    if (check.auto_fail !== base.auto_fail) {
      diffs.push(check.auto_fail ? "now auto-fail" : "no longer auto-fail");
    }
    if (check.enabled !== base.enabled) {
      diffs.push(check.enabled ? "turned on" : "turned off");
    }
    return diffs;
  }

  function orderChanged(baseIds, draftIds) {
    var kept = baseIds.filter(function (id) { return draftIds.indexOf(id) >= 0; });
    var moved = draftIds.filter(function (id) { return baseIds.indexOf(id) >= 0; });
    return kept.join() !== moved.join();
  }

  function changeList() {
    var base = rb.base;
    var draft = rb.draft;
    var out = [];
    var baseById = {};
    base.checks.forEach(function (c) { baseById[c.check_id] = c; });
    var all = draft.scorecard.concat(draft.process);
    var kindText = function (c) { return c.kind === "scorecard" ? "scorecard item" : "process rule"; };
    all.forEach(function (c) {
      if (!c.check_id) {
        out.push("Added " + kindText(c) + ": " + (c.summary.trim() || "(no summary yet)"));
      } else if (!baseById[c.check_id]) {
        out.push("Restored " + kindText(c) + ": " + c.summary);
      } else {
        var diffs = checkChanges(baseById[c.check_id], c);
        if (diffs.length) {
          out.push("Edited " + quoted(baseById[c.check_id].summary) + ": " + diffs.join(", "));
        }
      }
    });
    var draftIds = all.map(function (c) { return c.check_id; });
    base.checks.forEach(function (c) {
      if (draftIds.indexOf(c.check_id) < 0) {
        out.push("Retired " + kindText(c) + ": " + c.summary);
      }
    });
    ["scorecard", "process"].forEach(function (kind) {
      var baseIds = base.checks.filter(function (c) { return c.kind === kind; }).map(function (c) { return c.check_id; });
      var ids = draft[kind].map(function (c) { return c.check_id; }).filter(Boolean);
      if (orderChanged(baseIds, ids)) {
        out.push(kind === "scorecard" ? "Scorecard reordered" : "Process rules reordered");
      }
    });
    var params = draftParams();
    var old = base.params;
    if (String(params.call_limit_minutes) !== String(old.call_limit_minutes)) {
      out.push("Call length limit: " + old.call_limit_minutes + " → " + params.call_limit_minutes + " minutes");
    }
    [["exempt_priorities", "Exempt priorities"], ["exempt_tags", "Exempt tags"]].forEach(function (pair) {
      if (params[pair[0]].join(", ") !== old[pair[0]].join(", ")) {
        out.push(pair[1] + ": " + (old[pair[0]].join(", ") || "none") + " → " + (params[pair[0]].join(", ") || "none"));
      }
    });
    if (String(params.email_deadline_minutes) !== String(old.email_deadline_minutes)) {
      out.push("Summary email deadline: " + old.email_deadline_minutes + " → " + params.email_deadline_minutes + " minutes");
    }
    var origTitles = draft.docs.map(function (d) { return d.origTitle; });
    draft.docs.forEach(function (d) {
      var title = d.title.trim();
      if (d.origTitle == null) {
        out.push("Added reference document: " + (title || "(untitled)"));
        return;
      }
      if (title !== d.origTitle) {
        var mentions = rulesMentioning([d.origTitle]);
        out.push("Renamed reference document: " + quoted(d.origTitle) + " → " + quoted(title) +
          (mentions.length ? " (" + plural(mentions.length, "rule still mentions", "rules still mention") +
            " the old title: " + mentions.map(checkName).join("; ") + ")" : ""));
      }
      if (d.text.trim() !== base.reference[d.origTitle]) {
        out.push("Edited reference document: " + (title || d.origTitle));
      }
    });
    Object.keys(base.reference).forEach(function (title) {
      if (origTitles.indexOf(title) < 0) {
        out.push("Removed reference document: " + title);
      }
    });
    return out;
  }

  function draftProblem() {
    var problem = null;
    rb.draft.scorecard.concat(rb.draft.process).some(function (c) {
      var name = c.kind === "scorecard" ? "Scorecard item #" + (rb.draft.scorecard.indexOf(c) + 1)
        : "Process rule " + quoted(c.summary.trim() || "(no name yet)");
      if (!c.summary.trim()) {
        problem = name + " needs a " + (c.kind === "scorecard" ? "summary." : "name.");
      } else if (!c.definition.trim()) {
        problem = name + " needs a definition.";
      } else if (c.kind === "scorecard" && (typeof c.points !== "number" || c.points > MAX_POINTS)) {
        problem = name + ": points must be a whole number from 0 to " + MAX_POINTS + ".";
      }
      return problem !== null;
    });
    if (problem) {
      return problem;
    }
    var titles = {};
    rb.draft.docs.some(function (d) {
      var title = d.title.trim();
      if (!title) {
        problem = "Every reference document needs a title.";
      } else if (titles[title.toLowerCase()]) {
        problem = "Two reference documents are titled " + quoted(title) + ". Titles must be different.";
      }
      titles[title.toLowerCase()] = true;
      return problem !== null;
    });
    if (problem) {
      return problem;
    }
    var params = draftParams();
    if (typeof params.call_limit_minutes !== "number" || params.call_limit_minutes < 1) {
      return "The call length limit must be a whole number of minutes, 1 or more.";
    }
    var deadline = params.email_deadline_minutes;
    if (typeof deadline !== "number" || deadline < 1 || deadline > DEADLINE_MAX_MINUTES) {
      return "The summary email deadline must be a whole number of minutes, from 1 to " + DEADLINE_MAX_MINUTES + ".";
    }
    return null;
  }

  function draftBody(note) {
    var checks = rb.draft.scorecard.concat(rb.draft.process).map(function (c) {
      var out = {};
      CHECK_BODY_FIELDS.forEach(function (key) { out[key] = c[key]; });
      if (!c.check_id) {
        delete out.check_id;
      }
      return out;
    });
    var reference = {};
    rb.draft.docs.forEach(function (d) { reference[d.title.trim()] = d.text; });
    return { checks: checks, reference: reference, params: draftParams(), note: note,
      base_version_id: rb.base.version_id };
  }

  function draftTotal() {
    return rb.draft.scorecard.reduce(function (sum, c) {
      return sum + (c.enabled && typeof c.points === "number" ? c.points : 0);
    }, 0);
  }

  function refreshChanges() {
    if (!rb) {
      return;
    }
    var total = draftTotal();
    if (rb.els.total) {
      rb.els.total.textContent = "(" + plural(rb.draft.scorecard.length, "item", "items") + " · " +
        plural(total, "point", "points") + ")";
      rb.els.totalNote.hidden = total === 100;
      rb.els.totalNote.textContent = "The enabled items add up to " + total + " points, not 100, so scores show " +
        "as percentages.";
    }
    if (rb.readOnly || !rb.els.changes) {
      return;
    }
    var changes = changeList();
    rb.els.changes.textContent = "";
    if (!changes.length) {
      rb.els.changes.appendChild(h("li", { class: "empty", text: "No unsaved changes." }));
    }
    changes.forEach(function (text) { rb.els.changes.appendChild(h("li", { text: text })); });
    rb.els.save.disabled = !changes.length;
    rb.els.discard.disabled = !changes.length;
  }

  function savePanel() {
    var note = h("input", { type: "text", id: "rb-note", maxlength: String(VERSION_NOTE_MAX), value: rb.note,
      placeholder: "For example: Reworded #6, added the subject format rule" });
    note.addEventListener("input", function () { rb.note = note.value; });
    var message = h("div", { class: "message", role: "status" });
    rb.els.changes = h("ul", { class: "changes" });
    rb.els.save = h("button", { class: "primary", type: "button", text: "Save as a new version" });
    rb.els.discard = h("button", { type: "button", text: "Discard changes" });
    rb.els.saveMessage = message;
    rb.els.pointer = rerunPointer();
    rb.els.save.addEventListener("click", function () { saveRubric(note, message); });
    rb.els.discard.addEventListener("click", function () {
      if (!window.confirm("Discard all unsaved changes to the rubric?")) {
        return;
      }
      rb.note = "";
      resetDraft();
      drawRubric();
    });
    return panel("Unsaved changes", "Saving makes version " + (rb.base.version_id + 1) + "; it applies from the next run.",
      rb.els.changes,
      h("div", { class: "form-row" }, h("label", { for: "rb-note", text: "What changed (note)" }), note),
      h("div", { class: "actions" }, rb.els.save, rb.els.discard, message), rb.els.pointer);
  }

  // Shown after a save: date-range runs, re-reviews included, start from Settings' Run now.
  function rerunPointer() {
    var at = RERUN_POINTER.indexOf(RERUN_LINK);
    return h("div", { class: "nav-note", hidden: true }, RERUN_POINTER.slice(0, at),
      h("a", { href: "#/settings", text: RERUN_LINK }), RERUN_POINTER.slice(at + RERUN_LINK.length));
  }

  function saveRubric(noteInput, message) {
    var changes = changeList();
    var problem = draftProblem();
    if (problem) {
      setMessage(message, problem, "error");
      return;
    }
    var note = rb.note.trim();
    if (!note) {
      setMessage(message, "Add a note that says what changed. It shows in the version history.", "error");
      noteInput.focus();
      return;
    }
    var shown = changes.slice(0, CONFIRM_LINES);
    if (changes.length > CONFIRM_LINES) {
      shown.push("…and " + (changes.length - CONFIRM_LINES) + " more");
    }
    if (!window.confirm("Save these changes as a new rubric version?\n\n- " + shown.join("\n- ") +
      "\n\nThey apply from the next run. Past results keep the version they were scored with.")) {
      return;
    }
    rb.els.save.disabled = true;
    setMessage(message, "Saving…");
    api("PUT", "/api/rubric", draftBody(note)).then(function (saved) {
      rb.base = saved;
      rb.note = "";
      rb.versions = null;
      resetDraft();
      drawRubric();
      setMessage(rb.els.saveMessage, "Saved as version " + saved.version_id + ". It applies from the next run.", "ok");
      rb.els.pointer.hidden = false;
    }).catch(function (error) {
      rb.els.save.disabled = false;
      setMessage(message, error.message, "error");
    });
  }

  // Version history

  function fillVersions(box) {
    var body = h("tbody");
    var message = h("div", { class: "message" });
    box.appendChild(h("h4", { text: "Version history" }));
    box.appendChild(h("div", { class: "sub", text: "Every save is a new version. Each call's results keep the " +
      "version they were scored with." }));
    // Two lines per version, so the note reads in whole lines in the narrower right column.
    box.appendChild(h("div", { class: "scroll" }, h("table", { class: "atable two-line versions" },
      h("thead", null,
        h("tr", { class: "line1" }, h("th", { scope: "col", text: "Version" }), h("th", { scope: "col", text: "Saved" }),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "View" }))),
        h("tr", { class: "line2" },
          h("th", { scope: "col", colspan: 3, text: "Note" }))),
      body)));
    box.appendChild(message);
    var load = rb.versions ? Promise.resolve(rb.versions) : api("GET", "/api/rubric/versions").then(function (data) {
      return data.versions;
    });
    load.then(function (versions) {
      if (!rb) {
        return;
      }
      rb.versions = versions;
      var latest = versions.length ? versions[versions.length - 1].version_id : null;
      versions.slice().reverse().forEach(function (v) {
        var current = v.version_id === latest;
        var shown = v.version_id === rb.base.version_id;
        var link = null;
        if (current && !rb.readOnly) {
          link = h("span", { class: "muted", text: "Editing" });
        } else if (current) {
          link = h("a", { href: "#/rubric", text: "Edit" });
        } else if (!shown || !rb.readOnly) {
          link = h("a", { href: "#/rubric/version/" + v.version_id, text: "View" });
        } else {
          link = h("span", { class: "muted", text: "Viewing" });
        }
        body.appendChild(h("tr", { class: shown ? "line1 sel" : "line1" },
          h("td", { text: "Version " + v.version_id + (current ? " (current)" : "") }),
          h("td", { text: timeLabel(v.created_at) }),
          h("td", { class: "ver-link" }, link)));
        body.appendChild(h("tr", { class: shown ? "line2 sel" : "line2" },
          h("td", { class: "note-cell", colspan: 3, text: v.note })));
      });
    }).catch(function (error) { setMessage(message, error.message, "error"); });
  }

  // -- settings ---------------------------------------------------------------------------------

  var view = {};

  // A new tech's first shift, for the owner to edit: Sunday to Thursday, 08:00 to 17:00.
  var SHIFT_DAYS = [0, 1, 2, 3, 4];
  var SHIFT_START = "08:00";
  var SHIFT_END = "17:00";

  function renderSettings(token) {
    loadingLine("Loading settings…");
    readScreen(token, ["/api/settings"], function (got) {
      if (isManager()) {
        drawManagerSettings(got[0]);
      } else if (isAdmin() && hostedOnly()) {
        drawHostedAdminSettings(got[0]);
      } else if (isAdmin()) {
        drawSettings(got[0], null);
      } else {
        drawLeadSettings(got[0]);
      }
    }).catch(function (error) {
      if (token === renderToken) {
        clearScreen();
        screen.appendChild(h("div", { class: "message error", text: error.message }));
      }
    });
  }

  /* Read `urls` for a part of the Settings screen (readKept): `draw(answers, again)` runs while that screen, the one
     whose state is `mine`, is still the one shown. */
  function readPart(mine, urls, draw) {
    return readKept(urls, function () { return view === mine; }, draw);
  }

  function drawSettings(settings, note) {
    clearScreen();
    view = { settings: settings };
    // The PC copy's Settings (the hosted admin's are drawHostedAdminSettings'). What is app-wide on top: each tech's
    // shift, time off and 1:1 schedule beside the sections switch, the activity flags and the connections. Under the
    // Call QA heading (when Call QA is shown), two columns, each built from its own ordered list of panels: running
    // reviews on the left, how the reviews are set up on the right.
    var team = [teamPanel(), timeOffPanel(), prepSchedulePanel()];
    var flags = [sectionsPanel(note), flagsPanel(), zendeskPanel()];
    screen.appendChild(h("div", { class: "panel-row cols-2-1" },
      h("div", { class: "stack" }, team), h("div", { class: "stack" }, flags)));
    showCallQaGroup(!hostedOnly() && settings.show_call_qa !== false);
  }

  /* `part`, a panel in an even row (app.css, .panel-row.even), marked to take the room left in its column: its list's
     box grows into it. */
  function fill(part) {
    part.classList.toggle("fill", true);
    return part;
  }

  /* `part`, a panel in an even row whose list keeps some room even while it holds few entries (app.css, .roomy):
     a few entries' room, or with `large` most of a team's. */
  function roomy(part, large) {
    part.classList.toggle("roomy", true);
    if (large) {
      part.classList.toggle("lg", true);
    }
    return part;
  }

  /* A team lead's Settings on the hosted copy: Shifts and Time off on the left, the 1:1 schedule on the right, in the
     proportions (cols-2-1) that give the shift table the Team table's room, so it fits its panel at the 4K display;
     an even row whose columns end level, Shifts and the 1:1 schedule taking the room left (Managers design). */
  function ownTeamRow() {
    return h("div", { class: "panel-row cols-2-1 even" },
      h("div", { class: "stack" }, fill(shiftsPanel()), roomy(timeOffPanel())),
      h("div", { class: "stack" }, fill(prepSchedulePanel())));
  }

  /* A team lead's Settings (the hosted copy): only those. The roster, the teams, the flag limits and the
     connections are the admin's. */
  function drawLeadSettings(settings) {
    clearScreen();
    view = { settings: settings };
    screen.appendChild(ownTeamRow());
  }

  /* Add or remove only the Call QA heading and its row of panels; every other panel stays as it is, with
     whatever has been typed into it. */
  function showCallQaGroup(on) {
    if (!on) {
      if (view.callQaGroup) {
        view.callQaGroup.forEach(function (part) { part.remove(); });
        view.callQaGroup = null;
      }
      return;
    }
    if (view.callQaGroup) {
      return;
    }
    var left = [runNowPanel(), testPanel(), historyPanel(), schedulePanel()];
    var right = [reviewPanel(), agreementPanel()];
    view.callQaGroup = [
      h("h3", { class: "group-head", text: "Call QA" }),
      h("div", { class: "panel-row cols-2" }, h("div", { class: "stack" }, left), h("div", { class: "stack" }, right))
    ];
    view.callQaGroup.forEach(function (part) { screen.appendChild(part); });
    loadRuns();
    loadTests();
  }

  // Sections (the PC copy): whether Call QA, Team activity and 1:1 prep are shown, and whether this PC reads Team
  // activity from Zendesk at all (off at cut-over: operator guide; this PC's 1:1 Prep then counts from data that no
  // longer changes). Showing is display only: a hidden section keeps its data and Call QA's daily review runs either
  // way. The hosted edition has no Sections panel (null): there the hosted copy's own timer syncs, and the Team
  // section is all there is.

  var SECTIONS_SUB = "What this PC shows, and whether it reads Team activity from Zendesk. A hidden section keeps its " +
    "data, and Call QA's daily review keeps running.";
  // Off only once the hosted copy also counts 1:1 preps: the PC's 1:1 Prep counts from what this PC's sync collects.
  var SYNC_HINT = "Turn off only after the hosted copy collects Team activity and counts 1:1 preps from the collected " +
    "data: the daily task stops reading it, and Refresh is hidden.";

  function sectionsPanel(note) {
    if (hostedOnly()) {
      return null;
    }
    var message = h("div", { class: "message" });
    if (note) {
      setMessage(message, note, "ok");
    }
    return panel("Sections", SECTIONS_SUB,
      sectionSwitch("show_call_qa", "set-show-call-qa", "Show Call QA", null, message, function (on, here) {
        setSections(on);
        refreshBanners();
        if (here) {
          showCallQaGroup(on);   // only the Call QA group comes or goes
        }
      }),
      sectionSwitch("show_team_activity", "set-show-team-activity", "Show Team activity", null, message,
        setTeamSection),
      sectionSwitch("show_oneonone", "set-show-oneonone", "Show 1:1 Prep", null, message, setOneononeSection),
      sectionSwitch("activity_sync_enabled", "set-activity-sync", "Sync Team activity", SYNC_HINT, message,
        function (on) { syncOn = on; }),
      message);
  }

  /* One switch of the Sections panel: a change saves `key` at once, then `apply(on, here)` (`here`: the Settings
     screen is still the one shown). A refused change puts the switch back and says why in `message`. */
  function sectionSwitch(key, id, label, hint, message, apply) {
    var box = h("input", { type: "checkbox", id: id, checked: view.settings[key] !== false });
    var token = renderToken;
    box.addEventListener("change", function () {
      var wanted = box.checked;
      var change = {};
      change[key] = wanted;
      box.disabled = true;
      setMessage(message, "Saving…");
      saveSettings(change).then(function (saved) {
        apply(saved[key] !== false, token === renderToken);
        if (token === renderToken) {
          setMessage(message, "Saved.", "ok");
          box.disabled = false;
        }
      }).catch(function (error) {
        box.checked = !wanted;
        box.disabled = false;
        setMessage(message, error.message, "error");
      });
    });
    return h("div", { class: "form-row" }, h("label", { for: id, text: label }), box,
      hint ? h("span", { class: "hint", text: hint }) : null);
  }

  // Techs and Team leads (the hosted copy's admin, hosted Team design §3): every tech the server follows with their
  // team leads, and each person with the techs on their team, which Edit techs changes. These two panels and the Edit
  // techs picker are the only places the whole roster shows; everything else, the admin's own Shifts, Time off and
  // 1:1 schedule included, holds the viewer's own team. The PC copy has no sign-in, so neither panel. Cloudflare
  // Access keeps its own list of people, which the admin also has to change.

  var ROLE_WORDS = { admin: "Admin", tl: "TL", manager: "Manager" };
  // The three regions in their order, each with the city whose time it keeps (regions.py's ORDER and CITIES).
  var REGIONS = [["NASA", "Dallas"], ["EMEA", "Dubai"], ["APAC", "Manila"]];
  // Cloudflare Zero Trust's dashboard, where the admin opens the Access application and its policy (the app doesn't
  // know the account, so it can't link the policy itself).
  var ACCESS_POLICY_URL = "https://one.dash.cloudflare.com/";
  var USERS_SUB = "Everyone who can sign in: you, the team leads and the managers. Also add each one to the ";
  var REGIONS_SUB = "Pick the regions you manage. Activity, Trends and 1:1 Prep show every team in them, and Techs " +
    "and Team leads here follow them too.";
  var MANAGER_NOTE = "Shifts, time off and 1:1 dates: each team lead sets these for their own techs. You see them " +
    "on Activity, Trends and 1:1 Prep.";

  /* The Techs panel's line: the admin's techs are every tech the server follows, a manager's those in their regions;
     each adds more teams through Edit techs, in Users (the admin's) or Team leads (a manager's). */
  function techsSub() {
    return (isManager() ? "Every tech in your regions." : "Every tech the server follows.") +
      " Add a tech by name and pick their team lead; Edit techs under " + (isManager() ? "Team leads" : "Users") +
      " can add them to more teams. Started is a tech's first day here: days before it aren't tracked, so they're " +
      "never a sick day.";
  }

  /* The admin's Settings on the hosted copy (hosted Team design §6): one even row of two columns, ending level (Managers
     design). On the left Techs first (the owner, 2026-10-21), then Import from Teams Shifts (no list: its own height),
     then their own team's Shifts and Time off, which takes the room left; on the right the 1:1 schedule, then Users,
     which takes the room left, Activity flags and Zendesk. */
  function drawHostedAdminSettings(settings) {
    clearScreen();
    view = { settings: settings, people: null };
    screen.appendChild(h("div", { class: "panel-row cols-2-1 even" },
      h("div", { class: "stack" }, techsPanel(), shiftsImportPanel(), roomy(shiftsPanel(), true), fill(timeOffPanel())),
      h("div", { class: "stack" }, roomy(prepSchedulePanel(), true), fill(roomy(teamLeadsPanel(), true)), flagsPanel(),
        zendeskPanel())));
    renderPeople();
    loadPeople();
  }

  /* A manager's Settings (Managers design): one even row, Techs (the techs in their regions) on the left, taking the
     column; on the right the regions they pick, the team leads in them, taking the room left, and the note saying who
     sets shifts, time off and 1:1 dates. A manager holds no team, so there is no Shifts, Time off or 1:1 schedule of
     their own. */
  function drawManagerSettings(settings) {
    clearScreen();
    view = { settings: settings, people: null };
    screen.appendChild(h("div", { class: "panel-row cols-2-1 even" },
      h("div", { class: "stack" }, fill(techsPanel())),
      h("div", { class: "stack" }, myRegionsPanel(), fill(teamLeadsPanel()), notePanel(MANAGER_NOTE))));
    renderPeople();
    loadPeople();
  }

  /* A panel that only says something: `text` as the page's note, in a panel's box (.panel > .note:only-child). */
  function notePanel(text) {
    return h("section", { class: "panel" }, h("p", { class: "note", text: text }));
  }

  /* Your regions (a manager's): a tick box for each region, with its city's time, the ones they picked ticked. Save
     regions keeps the ticked ones; Techs and Team leads then follow them, as Activity, Trends and 1:1 Prep do. */
  function myRegionsPanel() {
    var mine = view;
    var message = h("div", { class: "message" });
    var boxes = allRegions.map(function (r) {
      return h("input", { type: "checkbox", value: r.region, checked: myRegions.indexOf(r.region) >= 0 });
    });
    var save = h("button", { class: "primary", text: "Save regions", onclick: function () {
      var picked = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute("value"); });
      save.disabled = true;
      setMessage(message, "Saving…");
      api("PUT", "/api/me/regions", { regions: picked }).then(function (got) {
        myRegions = got.regions;
        if (view === mine) {
          setMessage(message, "Saved. You now see " + (myRegions.length ? andList(myRegions) : "no regions") + ".",
            "ok");
          view.rosterRegion.value = startRegion();   // the Techs add row starts on their first region
        }
        loadPeople();   // which techs, and so which teams, they see follow their regions
      }).catch(function (error) {
        setMessage(message, error.message, "error");
      }).then(function () { save.disabled = false; });
    } });
    return panel("Your regions", REGIONS_SUB,
      h("ul", { class: "pick-list" }, allRegions.map(function (r, i) {
        return h("li", null, h("label", null, boxes[i], r.region), h("span", { class: "note", text: r.city + " time" }));
      })),
      h("div", { class: "actions" }, save, message));
  }

  /* "NASA", "NASA and EMEA", "NASA, EMEA and APAC". */
  function andList(items) {
    return items.length < 2 ? items.join("") : items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }

  /* What the page calls a person: "You" for whoever is signed in, else the name typed when they were added, else
     their email. */
  function personName(person) {
    return person.email === myEmail ? "You" : person.name || person.email;
  }

  function leadName(email) {
    var person = view.people.people.filter(function (p) { return p.email === email; })[0];
    return person ? personName(person) : email;
  }

  function rosterName(email) {
    var tech = view.people.roster.filter(function (t) { return t.email === email; })[0];
    return tech ? tech.name : email;
  }

  function byName(a, b) {
    var x = a.toLowerCase();
    var y = b.toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  }

  /* People just read or saved here (the People answer): a manager's teams follow them (teamBook). */
  function keepPeople(data) {
    if (isManager()) {
      teamBook = data;
    }
  }

  /* Read the people again. The answer is kept as a manager's teams whatever screen is shown when it comes, and drawn
     on the Settings with Techs and Users shown then, even one drawn again since the read was asked for (the person
     left Settings and came back before the answer came): the latest people are right for any of them. */
  function loadPeople() {
    readKept(["/api/people"], function () { return true; }, function (got) {
      keepPeople(got[0]);
      if (view.peopleBody) {
        view.people = got[0];
        renderPeople();
      }
    }).catch(function (error) {
      if (view.peopleMessage) {
        setMessage(view.peopleMessage, error.message, "error");
      }
    });
  }

  function renderPeople() {
    renderRoster();
    renderLeads();
  }

  // Techs

  function techsPanel() {
    var mine = view;
    view.rosterBody = h("tbody");
    view.rosterMessage = h("div", { class: "message" });
    view.rosterAddMessage = h("div", { class: "message" });
    view.rosterName = h("input", { type: "text", placeholder: "First and last name", "aria-label": "Tech name" });
    // The email made from the name shows under it as it is typed; the email field shows only when Zendesk has no
    // agent there, on the add row's second line, as on the PC's Team panel.
    view.rosterMade = h("div", { class: "note" });
    view.rosterEmail = h("input", { type: "email", hidden: true,
      placeholder: emailPlaceholder(view.settings.email_domain), "aria-label": "Tech email" });
    view.rosterLead = h("select", { "aria-label": "Team lead" });
    // The region a new tech starts in, from today: each region's whole name, in a column wide enough for it.
    view.rosterRegion = h("select", { "aria-label": "Region" }, REGIONS.map(function (r) {
      return h("option", { value: r[0], text: r[0], selected: r[0] === startRegion() ? "selected" : null });
    }));
    view.rosterName.addEventListener("input", function () {
      mine.rosterMade.textContent = mine.rosterEmail.hidden ?
        emailFor(mine.rosterName.value, mine.settings.email_domain) : "";
    });
    var addButton = h("button", { class: "small", text: "Add tech", onclick: function () { addRosterTech(addButton); } });
    onEnter([view.rosterName, view.rosterEmail], function () { addRosterTech(addButton); });
    // The list scrolls in a taller box than other lists; the add row is a table of its own under it, with the same
    // columns (rosterCols), so it never scrolls away. It has no header, so its cells carry their labels for when it
    // shows as a card (a phone: its fixed columns are wider than the panel), as the list's get them from theirs.
    var addTable = h("table", { class: "atable roster" }, rosterCols(),
      h("tbody", { class: "row-group add-row" },
        h("tr", null, h("td", null, view.rosterName, view.rosterMade), h("td"),
          h("td", { "data-label": "Region" }, view.rosterRegion), h("td", { "data-label": "Team lead" }, view.rosterLead),
          h("td", { class: "num act" }, addButton)),
        h("tr", { class: "add-more" }, h("td", { colspan: "4" }, view.rosterEmail, view.rosterAddMessage),
          h("td"))));
    var foot = h("div", { class: "list-foot" }, addTable);
    fitCards(foot, addTable);
    return panel("Techs", techsSub(),
      tableBox(h("table", { class: "atable roster" }, rosterCols(),
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Tech" }),
          h("th", { scope: "col", text: "Started" }),
          h("th", { scope: "col", text: "Region" }),
          h("th", { scope: "col", text: "Team lead" }),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Edit or remove" })))),
        view.rosterBody), { list: true, tall: true, first: 10, noun: "techs" }),
      foot,
      view.rosterMessage);
  }

  /* The Techs tables' columns (app.css sizes each), so the list and the add row under it line up. */
  function rosterCols() {
    return h("colgroup", null, ["tech", "started", "region", "lead", "act"].map(function (name) {
      return h("col", { class: "col-" + name });
    }));
  }

  function renderRoster() {
    view.rosterBody.textContent = "";
    if (!view.people) {
      view.rosterBody.appendChild(h("tr", null, h("td", { colspan: "5", class: "empty", text: "Loading…" })));
      tableChanged(view.rosterBody);
      return;
    }
    if (!view.people.roster.length) {
      view.rosterBody.appendChild(h("tr", null, h("td", { colspan: "5", class: "empty",
        text: "No techs yet. Add each by name." })));
    }
    view.people.roster.forEach(function (tech) {
      view.rosterBody.appendChild(h("tr", { class: "roster-row" },
        h("td", null, h("b", { text: tech.name }), h("div", { class: "note" }, breakableEmail(tech.email))),
        tech.started ? h("td", { text: dayLabel(tech.started, true) }) : h("td", { class: "muted", text: "-" }),
        h("td", { text: tech.region }),
        tech.leads.length ? h("td", { text: tech.leads.map(leadName).join(", ") })
          : h("td", { class: "muted", text: "No team lead" }),
        h("td", { class: "num" },
          h("button", { class: "small", text: "Edit", onclick: function () { editTech(tech); } }), " ",
          h("button", { class: "small", text: "Remove", onclick: function () { removeRosterTech(tech.email); } }))));
    });
    tableChanged(view.rosterBody);
    fillLeadChoice();
  }

  /* A small window over the page (the shared .dialog, as Edit techs): `title`, a `sub` line, then `parts`, and
     Cancel and Save at its foot. `save(button, message)` sends it; Cancel or Escape closes it and sends nothing. */
  function openDialog(title, sub, parts, save) {
    closeDialog();
    var message = h("div", { class: "message" });
    var cancel = h("button", { class: "small", text: "Cancel", onclick: closeDialog });
    var button = h("button", { class: "primary", text: "Save", onclick: function () { save(button, message); } });
    var box = h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "dialog-title" },
      h("h4", { class: "dialog-title", id: "dialog-title", text: title }), h("div", { class: "sub" }, sub), parts,
      h("div", { class: "actions dialog-actions" }, message, cancel, button));
    view.dialog = h("div", { class: "dialog-scrim" }, box);
    view.dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeDialog();
      }
    });
    screen.appendChild(view.dialog);
    return cancel;
  }

  /* Techs' Edit: the tech's name, Started (their first tracked day: days before it are never a sick day; the owner,
     2026-10-21), their regions and their team leads, saved together. Their email can't change. A region is dated, as
     a shift is (a tech can move to another region): one line per change, in force from its date, and a tech with none
     shows one line, NASA from their Started. The regions are sent, the whole list, only when they changed, so saving
     a tech never set leaves them on the app's own zone (Regions design). */
  function editTech(tech) {
    var mine = view;
    var name = h("input", { type: "text", id: "edit-tech-name", value: tech.name === tech.email ? "" : tech.name,
      placeholder: "First and last name" });
    name.value = tech.name === tech.email ? "" : tech.name;
    var started = h("input", { type: "date", id: "edit-tech-started", max: todayIn(view.settings.timezone_effective) });
    started.value = tech.started || "";
    started.disabled = !tech.started;
    var people = leadChoices();
    var boxes = people.map(function (person) {
      return h("input", { type: "checkbox", value: person.email, checked: tech.leads.indexOf(person.email) >= 0 });
    });
    var regionList = h("ul", { class: "change-list" });
    var regionLines = [];
    function addRegionLine(since, region) {
      var from = h("input", { type: "date", "aria-label": "Region from" });
      from.value = since;
      var choice = h("select", { "aria-label": "Region" }, REGIONS.map(function (r) {
        return h("option", { value: r[0], text: r[0] + " (" + r[1] + " time)" });
      }));
      choice.value = region;
      var line = { from: from, choice: choice };
      line.li = h("li", null, from, choice, h("button", { class: "small", text: "Remove", onclick: function () {
        regionLines.splice(regionLines.indexOf(line), 1);
        line.li.remove();
      } }));
      regionLines.push(line);
      regionList.appendChild(line.li);
    }
    function regionChanges() {
      return regionLines.map(function (line) { return { since: line.from.value, region: line.choice.value }; });
    }
    (tech.regions.length ? tech.regions : [{ since: tech.started || "", region: "NASA" }]).forEach(function (c) {
      addRegionLine(c.since, c.region);
    });
    var regionsAsOpened = JSON.stringify(regionChanges());
    var focus = openDialog("Edit " + tech.name, [breakableEmail(tech.email),
      " · The email can't change: it's how Zendesk and their history know them."], [
      h("div", { class: "form-row" }, h("label", { for: "edit-tech-name", text: "Name" }), name),
      h("div", { class: "form-row" }, h("label", { for: "edit-tech-started", text: "Started" }), started,
        h("span", { class: "hint", text: tech.started ? STARTED_NOTE
          : isManager() ? "Their team lead sets their shift first." : "Set their shift in Shifts first." })),
      h("div", { class: "form-row" }, h("span", { class: "label", text: "Region" }), regionList,
        h("button", { class: "small", text: "Add a region change",
          onclick: function () { addRegionLine("", "NASA"); } })),
      h("div", { class: "form-row" }, h("span", { class: "label", text: "Team leads" }),
        h("ul", { class: "pick-list" }, people.map(function (person, i) {
          return h("li", null, h("label", null, boxes[i], personName(person)));
        })))
    ], function (button, message) {
      var body = { leads: boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute("value"); }) };
      if (name.value.trim() && name.value.trim() !== tech.name) {
        body.name = name.value.trim();
      }
      if (tech.started && started.value && started.value !== tech.started) {
        body.started = started.value;
      }
      var changes = regionChanges();
      if (JSON.stringify(changes) !== regionsAsOpened) {
        if (changes.some(function (c) { return !c.since; })) {
          setMessage(message, "Give each region change its date.", "error");
          return;
        }
        body.regions = changes;
      }
      button.disabled = true;
      setMessage(message, "Saving…");
      api("PUT", "/api/team/" + encodeURIComponent(tech.email), body).then(function (saved) {
        if (view !== mine) {
          loadPeople();   // answered after Settings was left: the people still follow the change
          return;
        }
        closeDialog();
        if (body.started && !isManager()) {   // a manager has no Shifts panel
          delete view.shiftRows[tech.email];
        }
        rosterChanged(saved);
        var saidName = body.name || tech.name;
        // A manager who moved the tech to a region they don't pick no longer sees them: the message says where they are.
        setMessage(view.rosterMessage, saved.moved_out ? saidName + " is in " +
          regionOn(body.regions || tech.regions, todayIn(view.settings.timezone_effective)) +
          " now, outside your regions." : "Saved " + saidName + ".", "ok");
      }, function (error) {
        setMessage(message, error.message, "error");
        button.disabled = false;
      });
    });
    name.focus();
    return focus;
  }

  /* The region in force on `day` (YYYY-MM-DD) in a tech's region changes ([{since, region}]): the latest one from
     that day or before; NASA, the app's region for a tech with none (regions.DEFAULT). */
  function regionOn(changes, day) {
    var current = REGIONS[0][0];
    changes.slice().sort(function (a, b) { return a.since < b.since ? -1 : a.since > b.since ? 1 : 0; })
      .forEach(function (c) {
        if (c.since <= day) {
          current = c.region;
        }
      });
    return current;
  }

  /* Who a tech's Team lead choices list (the add row's and Edit's): the people added, never a manager (managers hold
     no team); "You" first for the admin, then the others by name. A manager is never "You" in them. */
  function leadChoices() {
    var others = view.people.people.filter(function (p) { return p.email !== myEmail && p.role !== "manager"; })
      .sort(function (a, b) { return byName(personName(a), personName(b)); });
    return (isManager() ? [] : [{ email: myEmail }]).concat(others);
  }

  /* The lead the add row starts on: the first listed ("You" for the admin); none when nobody is listed. */
  function startLead() {
    var first = leadChoices()[0];
    return first ? first.email : "";
  }

  /* The region the add row starts on: a manager's first region (NASA when they picked none), else NASA. */
  function startRegion() {
    return isManager() && myRegions.length ? myRegions[0] : REGIONS[0][0];
  }

  /* The add row's Team lead choice (leadChoices), on startLead's to begin with. */
  function fillLeadChoice() {
    var chosen = view.rosterLead.value || startLead();
    view.rosterLead.textContent = "";
    leadChoices().forEach(function (person) {
      view.rosterLead.appendChild(h("option", { value: person.email, text: personName(person),
        selected: person.email === chosen ? "selected" : null }));
    });
  }

  function resetRosterAdd() {
    view.rosterName.value = "";
    view.rosterEmail.value = "";
    view.rosterEmail.hidden = true;
    view.rosterMade.textContent = "";
    view.rosterLead.value = startLead();
    view.rosterRegion.value = startRegion();
    setMessage(view.rosterAddMessage, "");
  }

  /* Add a tech by name (and the email, once the admin corrected the one made from it) to the chosen lead's team, in
     the chosen region. */
  function addRosterTech(button) {
    var name = view.rosterName.value.trim();
    if (!name) {
      setMessage(view.rosterAddMessage, "Enter the tech's name.", "error");
      return;
    }
    var typed = view.rosterEmail.hidden ? "" : view.rosterEmail.value.trim();
    var body = typed ? { name: name, email: typed } : { name: name };
    body.lead = view.rosterLead.value;
    body.region = view.rosterRegion.value;
    var mine = view;
    button.disabled = true;
    setMessage(view.rosterMessage, "");
    setMessage(view.rosterAddMessage, "Checking Zendesk…");
    api("POST", "/api/team", body).then(function (saved) {
      if (view !== mine) {
        loadPeople();   // answered after Settings was left: the people still follow the change
        return;
      }
      resetRosterAdd();
      setMessage(view.rosterMessage, "Added " + saved.added + ".", "ok");
      rosterChanged(saved);
    }, function (error) {
      if (view !== mine) {
        return;
      }
      var fix = error.data && typeof error.data.email === "string" ? error.data.email : null;
      if (fix === null) {
        setMessage(view.rosterAddMessage, error.message, "error");
        return;
      }
      // No Zendesk agent at the email made from the name, or none could be made: the email shows, to correct, and
      // the name and the lead stay as chosen.
      view.rosterEmail.hidden = false;
      view.rosterEmail.value = fix;
      view.rosterMade.textContent = "";
      setMessage(view.rosterAddMessage, sentence(error.message) +
        (fix ? " Correct the email and click Add tech again." : " Or enter their work email."), "error");
    }).then(function () { button.disabled = false; });
  }

  function removeRosterTech(email) {
    if (!window.confirm("Remove " + email + "? They come off every team, and the app stops following them.")) {
      return;
    }
    var mine = view;
    api("DELETE", "/api/team/" + encodeURIComponent(email)).then(function (saved) {
      if (view === mine) {
        setMessage(view.rosterMessage, "Removed " + email + ".", "ok");
        rosterChanged(saved);
      } else {
        loadPeople();   // answered after Settings was left: the people still follow the change
      }
    }).catch(function (error) { setMessage(mine.rosterMessage, error.message, "error"); });
  }

  /* After a tech is added or removed: the roster and the teams read again, and the admin's own team's panels follow
     the settings the server answered with. A manager holds no team, so has no such panels. */
  function rosterChanged(saved) {
    loadPeople();
    if (isManager()) {
      view.settings = saved;
    } else {
      ownTeamChanged(saved);
    }
  }

  /* The signed-in person's own team changed (`settings`, as now read): Shifts, Time off and the 1:1 schedule read
     it again. */
  function ownTeamChanged(settings) {
    var mine = view;
    view.settings = settings;
    Object.keys(view.shiftRows).forEach(function (email) {
      if (settings.team.indexOf(email) < 0) {
        delete view.shiftRows[email];
      }
    });
    loadShifts(renderTeam).catch(function (error) {
      if (view === mine) {
        setMessage(view.teamMessage, error.message, "error");
      }
    });
    loadTimeOff();
    refreshPrepSchedule();
  }

  // Import from Teams Shifts (the hosted copy's admin; Shifts import design, 2026-10-21-shift-finder-design.md): the
  // admin chooses the files Teams Shifts' "Export schedule" wrote, Preview shows what importing them would write for
  // each tech on the roster (their regions, their shift week by week, Started and their leave), and Apply writes it
  // after the server backs up the database. The files are read here, in the page, and sent as base64; neither the
  // page nor the server keeps them. Apply sends the very files the preview on show was made from.

  var SHIFTS_IMPORT_SUB = "Export the schedule in Teams Shifts (⋯ › Export schedule) and choose the " +
    "files. You'll see what would change before anything is saved.";
  var SHIFTS_FILES = "Choose the Teams Shifts export files.";

  function shiftsImportPanel() {
    var mine = view;
    var input = h("input", { type: "file", id: "shifts-files", accept: ".xlsx", multiple: true });
    var box = h("div", { class: "import-preview", hidden: true });
    var message = h("div", { class: "message" });
    var previewButton = h("button", { text: "Preview" });
    var applyButton = h("button", { class: "primary", text: "Apply", disabled: true });
    var shown = null;   // the files the preview on show was made from: what Apply sends
    var asking = 0;     // moves on with each preview asked for and each new choice of files

    function forget() {
      asking += 1;
      shown = null;
      applyButton.disabled = true;
      box.hidden = true;
      box.textContent = "";
    }

    input.addEventListener("change", function () {
      forget();
      setMessage(message, "");
    });
    previewButton.addEventListener("click", function () {
      if (!input.files || !input.files.length) {
        setMessage(message, SHIFTS_FILES, "error");
        return;
      }
      forget();
      var ask = asking;
      previewButton.disabled = true;
      setMessage(message, "Reading the files…");
      readFiles(input.files).then(function (files) {
        return api("POST", "/api/shifts-import/preview", { files: files }).then(function (plan) {
          if (view === mine && ask === asking) {
            shown = files;
            showShiftsPreview(box, plan);
            applyButton.disabled = false;
            setMessage(message, "");
          }
        });
      }).catch(function (error) {
        if (view === mine && ask === asking) {
          setMessage(message, error.message, "error");
        }
      }).then(function () { previewButton.disabled = false; });
    });
    applyButton.addEventListener("click", function () {
      if (!shown || !window.confirm("Apply this import? A backup is made first.")) {
        return;
      }
      var files = shown;
      var ask = asking;
      applyButton.disabled = true;
      previewButton.disabled = true;
      setMessage(message, "Saving…");
      api("POST", "/api/shifts-import/apply", { files: files }).then(function (done) {
        if (view !== mine) {
          return;
        }
        if (ask === asking) {   // the plan as applied stays on show; Apply waits for the next preview
          shown = null;
          showShiftsPreview(box, done.preview);
        }
        setMessage(message, "Saved. Backup: " + done.backup.split(/[\\/]/).pop() + ".", "ok");
        shiftsImported();
      }, function (error) {
        if (view === mine) {
          setMessage(message, error.message, "error");
          applyButton.disabled = ask !== asking;
        }
      }).then(function () { previewButton.disabled = false; });
    });
    return panel("Import from Teams Shifts", SHIFTS_IMPORT_SUB,
      h("div", { class: "form-row" }, h("label", { for: "shifts-files", text: "Export files" }), input),
      box,
      h("div", { class: "actions" }, previewButton, applyButton, message));
  }

  /* Each file of `list` (the file picker's) as {name, data}, `data` its bytes in base64, read in the page. */
  function readFiles(list) {
    return Promise.all(Array.prototype.map.call(list, function (file) {
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () {
          var url = String(reader.result);   // "data:<type>;base64,<data>"
          resolve({ name: file.name, data: url.slice(url.indexOf(",") + 1) });
        };
        reader.onerror = function () {
          reject(new Error("Couldn't read " + file.name + ". Choose it again."));
        };
        reader.readAsDataURL(file);
      });
    }));
  }

  /* An import's preview (`plan`, as shifts_import.preview_json gives it) in `box`: one row group per tech, in the
     server's order (by region then name), then the problems that belong to no one tech, the labels the import
     doesn't know and read as working shifts (with how many rows have each, most common first), the people skipped
     and the dates the files cover. */
  function showShiftsPreview(box, plan) {
    var groups = plan.techs.length ? plan.techs.map(previewTech) : h("tbody", null, h("tr", null,
      h("td", { colspan: "4", class: "empty", text: "No one on the roster is in these files." })));
    box.textContent = "";
    box.appendChild(tableBox(h("table", { class: "atable" },
      h("thead", null, h("tr", null, ["Tech", "Region", "Shift changes", "Time off"].map(function (head) {
        return h("th", { scope: "col", text: head });
      }))),
      groups), { list: true, tall: true }));
    plan.problems.forEach(function (problem) {
      box.appendChild(h("div", { class: "note warn table-note", text: problem }));
    });
    if (plan.unrecognised.length) {
      box.appendChild(h("div", { class: "note table-note", text: "Read as working shifts: " +
        plan.unrecognised.map(function (u) { return u.label + " (" + u.count + ")"; }).join(", ") }));
    }
    if (plan.skipped.length) {
      box.appendChild(h("div", { class: "note table-note", text: "Skipped (not on the roster): " +
        plan.skipped.join(", ") }));
    }
    box.appendChild(h("div", { class: "note table-note",
      text: "Dates covered: " + dayLabel(plan.first, true) + " – " + dayLabel(plan.last, true) + "." }));
    box.hidden = false;
  }

  /* One tech of an import's preview: their name and Started (when the import sets it), their regions, each shift
     change ("from Oct 4, 2026  Mon-Fri  08:00-17:00"), their PTO and sick days, and on a line of their own, their
     problems. */
  function previewTech(tech) {
    var leave = plural(tech.pto.length, "PTO day", "PTO days") + ", " +
      plural(tech.sick.length, "sick day", "sick days");
    return h("tbody", { class: "row-group" },
      h("tr", null,
        h("td", null, h("b", { text: tech.name }),
          tech.started ? h("span", { class: "cell-sub", text: "Started " + dayLabel(tech.started, true) }) : null),
        h("td", null, tech.regions.map(function (r) {
          return h("div", { text: r.region + " from " + dayLabel(r.since, true) });
        })),
        h("td", null, tech.changes.map(function (c) {
          return h("div", { class: "kept-spaces",
            text: "from " + dayLabel(c.since, true) + "  " + shiftDays(c.days) + "  " + c.start + "-" + c.end });
        })),
        h("td", { text: leave })),
      tech.problems.length ? h("tr", null, h("td", { colspan: "4" }, tech.problems.map(function (problem) {
        return h("div", { class: "note warn", text: problem });
      }))) : null);
  }

  /* A shift's work days as the import writes them (shifts_import._described): "Mon-Fri" for a run of three or more
     days, else "Sun,Tue"; "no days" for a week with none. */
  function shiftDays(days) {
    var sorted = days.slice().sort(function (a, b) { return a - b; });
    if (sorted.length > 2 && sorted[sorted.length - 1] - sorted[0] === sorted.length - 1) {
      return WEEKDAYS[sorted[0]] + "-" + WEEKDAYS[sorted[sorted.length - 1]];
    }
    return sorted.map(function (d) { return WEEKDAYS[d]; }).join(",") || "no days";
  }

  /* After an import is applied: Techs (Started and Region) read again, and so do the admin's own team's Shifts and
     Time off, which the import rewrote; Shifts' rows start over from what is stored. */
  function shiftsImported() {
    var mine = view;
    loadPeople();
    loadShifts(function () {
      view.shiftRows = {};
      renderTeam();
    }).catch(function (error) {
      if (view === mine) {
        setMessage(view.teamMessage, error.message, "error");
      }
    });
    loadTimeOff();
  }

  // Users (the admin's: everyone who can sign in) and Team leads (a manager's)

  function teamLeadsPanel() {
    var mine = view;
    // The admin's panel is Users, for team leads and managers, with a choice of the two; a manager's is Team leads,
    // the only people they add.
    var users = !isManager();
    view.peopleBody = h("tbody");
    view.peopleMessage = h("div", { class: "message" });
    // A person is added by name. The email made from it shows as the name is typed, because it is also the address
    // the admin enters in the Cloudflare Access policy; once the admin edits it, typing the name leaves it alone.
    view.peopleName = h("input", { type: "text", placeholder: "First and last name",
      "aria-label": users ? "User name" : "Team lead name" });
    view.peopleEmail = h("input", { type: "email", placeholder: emailPlaceholder(view.settings.email_domain),
      "aria-label": users ? "User email" : "Team lead email" });
    view.peopleRole = users ? h("select", { "aria-label": "Role" }, h("option", { value: "tl", text: "Team lead" }),
      h("option", { value: "manager", text: "Manager" })) : null;
    view.peopleEmailEdited = false;
    view.peopleName.addEventListener("input", function () {
      if (!mine.peopleEmailEdited) {
        mine.peopleEmail.value = emailFor(mine.peopleName.value, mine.settings.email_domain);
      }
    });
    view.peopleEmail.addEventListener("input", function () {
      mine.peopleEmailEdited = mine.peopleEmail.value.trim() !== "";
    });
    var addButton = h("button", { class: "small", text: users ? "Add" : "Add team lead",
      onclick: function () { addPerson(addButton); } });
    onEnter([view.peopleName, view.peopleEmail], function () { addPerson(addButton); });
    return panel(users ? "Users" : "Team leads", [users ? USERS_SUB : "Also add them to the ",
        h("a", { href: ACCESS_POLICY_URL, target: "_blank", rel: "noopener noreferrer", text: "Cloudflare Access policy" }),
        ", or they can't reach this page."],
      tableBox(h("table", { class: "atable" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: users ? "User" : "Lead" }),
          h("th", { scope: "col", text: users ? "What they see" : "Techs they see" }),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Edit techs or remove" })))),
        view.peopleBody), { list: true }),
      h("div", { class: "actions people-add" }, view.peopleName, view.peopleEmail, view.peopleRole, addButton,
        view.peopleMessage));
  }

  function renderLeads() {
    view.peopleBody.textContent = "";
    if (!view.people) {
      view.peopleBody.appendChild(h("tr", null, h("td", { colspan: "3", class: "empty", text: "Loading…" })));
      tableChanged(view.peopleBody);
      return;
    }
    view.people.people.forEach(function (person) { view.peopleBody.appendChild(personRow(person)); });
    tableChanged(view.peopleBody);
    if (view.dialog) {
      view.dialog.remove();   // what it showed may have changed
      view.dialog = null;
    }
  }

  /* One person: "You" for the admin signed in, else their typed name (with their email under it) or their email; what
     they see, a tag each: the techs on their team by name, or a manager's regions (managers hold no team, so no Edit
     techs). Remove on everyone but whoever is signed in; a manager signed in removes team leads only (the server's
     rule). */
  function personRow(person) {
    var me = person.email === myEmail;
    var manager = person.role === "manager";
    var seen = manager ? person.regions || [] : person.techs.map(rosterName).sort(byName);
    return h("tr", { class: "person-row" },
      h("td", null, h("b", { text: personName(person) }),
        h("div", { class: "note" }, !me && person.name ? [breakableEmail(person.email), " · "] : null,
          ROLE_WORDS[person.role])),
      seen.length ? h("td", null, h("div", { class: "tags" }, seen.map(function (name) {
        return h("span", { class: "tag", text: name });
      }))) : h("td", { class: "muted", text: manager ? "No regions yet" : "No techs yet" }),
      h("td", { class: "num" },
        manager ? (isManager() ? null : h("button", { class: "small", text: "Edit regions",
          onclick: function () { editRegions(person); } }))
          : h("button", { class: "small", text: "Edit techs", onclick: function () { editTechs(person); } }),
        me || isManager() && person.role !== "tl" ? null : [manager && isManager() ? null : " ", h("button", { class: "small",
          text: "Remove", onclick: function () { removePerson(person.email); } })]));
  }

  function personUrl(email) {
    return "/api/people/" + encodeURIComponent(email);
  }

  /* Edit techs: a picker over the page listing every tech on the roster, ticked for the person's team, the admin's
     own techs marked "Your tech". Save replaces the person's whole team with what is ticked (a tech may stay on more
     than one team); Cancel, or Escape, closes it and sends nothing. */
  function editTechs(person) {
    var me = person.email === myEmail;
    var first = person.name ? person.name.split(" ")[0] : person.email;
    var roster = view.people.roster;
    var boxes = roster.map(function (tech) {
      return h("input", { type: "checkbox", value: tech.email, checked: person.techs.indexOf(tech.email) >= 0 });
    });
    var cancel = openDialog(me ? "Your techs" : personName(person) + "'s techs",
      me ? "Tick the techs you see and edit." : "Tick the techs " + first + " sees and edits.",
      roster.length ? h("ul", { class: "pick-list" }, roster.map(function (tech, i) {
        return h("li", null, h("label", null, boxes[i], tech.name),
          tech.leads.indexOf(myEmail) >= 0 ? h("span", { class: "note", text: "Your tech" }) : null);
      })) : h("div", { class: "placeholder", text: "No techs yet. Add each by name under Techs." }),
      function (button, message) { saveTechs(person, boxes, button, message); });
    (boxes[0] || cancel).focus();
  }

  /* Edit regions (the admin's, on a manager's row): the three regions, ticked for the manager's; Save replaces them.
     The manager can still change them in their own Settings (the owner, 2026-10-22). */
  function editRegions(person) {
    var first = person.name ? person.name.split(" ")[0] : person.email;
    var picked = person.regions || [];
    var boxes = REGIONS.map(function (r) {
      return h("input", { type: "checkbox", value: r[0], checked: picked.indexOf(r[0]) >= 0 });
    });
    var cancel = openDialog(personName(person) + "'s regions",
      "Tick the regions " + first + " sees. They can change these in their own Settings too.",
      h("ul", { class: "pick-list" }, REGIONS.map(function (r, i) {
        return h("li", null, h("label", null, boxes[i], r[0]), h("span", { class: "note", text: r[1] + " time" }));
      })),
      function (button, message) { saveRegions(person, boxes, button, message); });
    (boxes[0] || cancel).focus();
  }

  /* Save the ticked regions as the manager's. A refused save keeps the picker open and says why. */
  function saveRegions(person, boxes, button, message) {
    var mine = view;
    var regions = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute("value"); });
    button.disabled = true;
    setMessage(message, "Saving…");
    api("PUT", personUrl(person.email) + "/regions", { regions: regions }).then(function (data) {
      keepPeople(data);
      if (view !== mine) {
        return;
      }
      view.people = data;
      renderPeople();   // closes the picker
      setMessage(view.peopleMessage, "Saved " + personName(person) + "'s regions.", "ok");
    }, function (error) {
      setMessage(message, error.message, "error");
      button.disabled = false;
    });
  }

  function closeDialog() {
    if (view.dialog) {
      view.dialog.remove();
      view.dialog = null;
    }
  }

  /* Save the ticked techs as the person's team. A refused save keeps the picker open and says why. */
  function saveTechs(person, boxes, button, message) {
    var mine = view;
    var techs = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute("value"); });
    var me = person.email === myEmail;
    button.disabled = true;
    setMessage(message, "Saving…");
    api("PUT", personUrl(person.email) + "/techs", { techs: techs }).then(function (data) {
      keepPeople(data);
      if (view !== mine) {
        return null;
      }
      view.people = data;
      renderPeople();   // closes the picker
      setMessage(view.peopleMessage, "Saved " + (me ? "your" : personName(person) + "'s") + " techs.", "ok");
      return me ? api("GET", "/api/settings").then(function (settings) {
        if (view === mine) {
          ownTeamChanged(settings);
        }
      }) : null;
    }, function (error) {
      setMessage(message, error.message, "error");
      button.disabled = false;
    }).catch(function (error) {
      if (view === mine) {
        setMessage(view.peopleMessage, error.message, "error");
      }
    });
  }

  /* Add someone by name, with the email shown (made from the name, or as the admin corrected it), as the role chosen
     (the admin's: a team lead or a manager; a manager adds team leads only). With no email shown, the server makes
     it, or says why it can't. */
  function addPerson(button) {
    var name = view.peopleName.value.trim();
    var shown = view.peopleEmail.value.trim();
    if (!name) {
      setMessage(view.peopleMessage, isManager() ? "Enter the team lead's name." : "Enter their name.", "error");
      return;
    }
    var email = (shown || emailFor(name, view.settings.email_domain)).toLowerCase();
    var mine = view;
    var asRole = view.peopleRole ? view.peopleRole.value : "tl";
    button.disabled = true;
    setMessage(view.peopleMessage, "Saving…");
    var body = shown ? { name: name, email: shown, role: asRole } : { name: name, role: asRole };
    api("POST", "/api/people", body).then(function (data) {
      keepPeople(data);
      if (view === mine) {
        view.people = data;
        view.peopleName.value = "";
        view.peopleEmail.value = "";
        view.peopleEmailEdited = false;
        if (view.peopleRole) {
          view.peopleRole.value = "tl";
        }
        renderPeople();
        setMessage(view.peopleMessage, "Added " + email + (asRole === "manager" ?
          ". Click Edit regions to choose their regions, or they pick them in their own Settings." : ". Click Edit techs to choose their techs."), "ok");
      }
    }).catch(function (error) {
      setMessage(mine.peopleMessage, error.message, "error");
    }).then(function () { button.disabled = false; });
  }

  function removePerson(email) {
    if (!window.confirm("Remove " + email + "? They won't be able to use the app.")) {
      return;
    }
    var mine = view;
    api("DELETE", personUrl(email)).then(function (data) {
      keepPeople(data);
      if (view === mine) {
        view.people = data;
        renderPeople();
        setMessage(view.peopleMessage, "Removed " + email + ".", "ok");
      }
    }).catch(function (error) { setMessage(mine.peopleMessage, error.message, "error"); });
  }

  function saveSettings(changes) {
    return api("PUT", "/api/settings", changes).then(function (settings) {
      view.settings = settings;
      return settings;
    });
  }

  /* Today's date (YYYY-MM-DD) in the owner's time zone, as the server counts it; this PC's date if the zone is
     unknown to the browser. */
  function todayIn(zone) {
    try {
      var parts = {};
      new Intl.DateTimeFormat("en-US", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" })
        .formatToParts(new Date()).forEach(function (part) { parts[part.type] = part.value; });
      return parts.year + "-" + parts.month + "-" + parts.day;
    } catch (e) {
      return isoDay(new Date());
    }
  }

  function offsetDay(iso, days) {
    var day = localDate(iso);
    return isoDay(new Date(day.getFullYear(), day.getMonth(), day.getDate() + days));
  }

  function techName(email) {
    var tech = (view.shifts || []).filter(function (t) { return t.email === email; })[0];
    return tech && tech.name ? tech.name : email;
  }

  function byTechName(a, b) {
    var x = techName(a).toLowerCase();
    var y = techName(b).toLowerCase();
    return x < y ? -1 : x > y ? 1 : a < b ? -1 : 1;
  }

  // Team: who the app follows, and each tech's shift

  /* The days, hours and effective-from date of one shift, as the inputs a Team row and the add row share. The add
     row has no date: the server picks a new tech's first day (apply_team_change), so days before they could have
     been synced never read as "Sick day". */
  function shiftControls(undated) {
    var controls = {
      checks: WEEKDAYS.map(function (day) { return h("input", { type: "checkbox", "aria-label": day }); }),
      start: h("input", { type: "time", "aria-label": "Start time" }),
      end: h("input", { type: "time", "aria-label": "End time" }),
      date: undated ? null : h("input", { type: "date", "aria-label": "Effective from" })
    };
    controls.pick = h("span", { class: "days-pick" }, controls.checks.map(function (box, i) {
      return h("label", null, box, WEEKDAYS[i].slice(0, 2));
    }));
    return controls;
  }

  function fillControls(controls, days, start, end, date) {
    controls.checks.forEach(function (box, i) { box.checked = days.indexOf(i) >= 0; });
    controls.start.value = start;
    controls.end.value = end;
    if (controls.date) {
      controls.date.value = date;
    }
  }

  function pickedDays(controls) {
    return controls.checks.map(function (box, i) { return box.checked ? i : -1; })
      .filter(function (i) { return i >= 0; });
  }

  function shiftBody(controls) {
    return { effective_from: controls.date ? controls.date.value : null, work_days: pickedDays(controls),
      start: controls.start.value, end: controls.end.value };
  }

  function shiftUrl(email) {
    return "/api/shifts/" + encodeURIComponent(email);
  }

  function upsertTech(tech) {
    var at = view.shifts.map(function (t) { return t.email; }).indexOf(tech.email);
    if (at < 0) {
      view.shifts.push(tech);
    } else {
      view.shifts[at] = tech;
    }
  }

  /* Read each tech's shifts into view.shifts, then `then()` (what shows them), while this Settings screen is shown: at
     once from the kept answer, and again from a changed one, whose rows start over from what is stored. The promise is
     for the server's answer. */
  function loadShifts(then) {
    return readPart(view, ["/api/shifts"], function (got, again) {
      view.shifts = got[0].techs;
      if (again) {
        view.shiftRows = {};
      }
      if (then) {
        then();
      }
    });
  }

  /* Fill a Team row from a tech's shift versions. A tech with none is an unsaved row (ruling 12): Sunday to
     Thursday, 08:00 to 17:00, from the first day of the retention window, and nothing is stored until Save team. */
  function loadShiftRow(row, versions) {
    var latest = versions.length ? versions[versions.length - 1] : null;
    row.started = versions.length ? versions[0].effective_from : null;
    if (row.startInput) {
      row.startInput.value = row.started || "";
      row.startInput.disabled = !row.started;
      if (row.started) {
        row.startInput.removeAttribute("title");
      } else {
        row.startInput.setAttribute("title", "Save the tech's shift first, then their start date.");
      }
    }
    row.saved = latest ? { days: latest.work_days.slice(), start: latest.start, end: latest.end } : null;
    row.initialDate = latest ? view.today : view.retentionStart;
    fillControls(row.controls, latest ? latest.work_days : SHIFT_DAYS, latest ? latest.start : SHIFT_START,
      latest ? latest.end : SHIFT_END, row.initialDate);
    row.status.textContent = "";
    if (!latest) {
      row.status.appendChild(h("div", { class: "note", text: "Not saved yet" }));
    }
  }

  function shiftRow(email) {
    if (view.shiftRows[email]) {
      return view.shiftRows[email];
    }
    var tech = view.shifts.filter(function (t) { return t.email === email; })[0];
    var row = { email: email, controls: shiftControls(), status: h("div"), message: h("div", { class: "message" }),
      startInput: view.addRow ? h("input", { type: "date", "aria-label": "Started", max: view.today }) : null };
    row.tr = h("tr", { class: "tech-row" },
      h("td", null, h("b", { text: techName(email) }), h("div", { class: "note" }, breakableEmail(email)), row.status,
        row.message),
      row.startInput ? h("td", null, row.startInput) : null,
      h("td", null, row.controls.pick),
      h("td", { class: "num" }, row.controls.start, " – ", row.controls.end),
      h("td", null, row.controls.date),
      view.addRow ? h("td", { class: "num" },
        h("button", { class: "small", text: "Remove", onclick: function () { removeTech(email); } })) : null);
    loadShiftRow(row, tech ? tech.versions : []);
    view.shiftRows[email] = row;
    return row;
  }

  function hoursChanged(row) {
    var now = shiftBody(row.controls);
    return !row.saved || now.effective_from !== row.initialDate || JSON.stringify(now.work_days) !==
      JSON.stringify(row.saved.days) || now.start !== row.saved.start || now.end !== row.saved.end;
  }

  function startChanged(row) {
    return !!row.startInput && !!row.started && row.startInput.value !== row.started;
  }

  function shiftChanged(row) {
    return startChanged(row) || hoursChanged(row);
  }

  var SHIFT_CHANGE_NOTE = "A change starts from its effective-from date, so earlier days keep the shift that applied " +
    "then.";
  var STARTED_NOTE = "Started is a tech's first day here: days before it aren't tracked, so they're never a sick day.";

  /* Team: who the app follows, and each tech's shift. */
  function teamPanel() {
    return panel("Team", "Who the app follows, and each tech's usual shift. " + SHIFT_CHANGE_NOTE + " " + STARTED_NOTE,
      shiftTable(true));
  }

  /* Shifts: the signed-in person's own techs' shifts (the hosted copy, the admin's too), with no Add tech and no
     Remove: the roster is the admin's Techs panel's. */
  function shiftsPanel() {
    return panel("Shifts", "Each of your techs' usual shift. " + SHIFT_CHANGE_NOTE, shiftTable(false));
  }

  /* The table of each tech's shift, then Save, which Team and Shifts share. With `roster` it also adds a tech (the
     last row) and removes one (each row's Remove). */
  function shiftTable(roster) {
    var mine = view;
    view.today = todayIn(view.settings.timezone_effective);
    view.retentionStart = offsetDay(view.today, -view.settings.retention_days);
    view.shifts = null;
    view.shiftRows = {};
    view.teamBody = h("tbody");
    view.teamMessage = h("div", { class: "message" });
    view.teamHint = h("div", { class: "hint-box", hidden: true, text: hostedOnly() ? null : ADD_HINT });
    view.addRow = roster ? addTechRow() : null;
    var saveButton = h("button", { class: "primary", text: roster ? "Save team" : "Save shifts",
      onclick: function () { saveTeam(saveButton); } });
    renderTeam();
    loadShifts(function () {
      renderTeam();
      renderTimeOff();
    }).catch(function (error) {
      if (view === mine) {
        setMessage(view.teamMessage, error.message, "error");
      }
    });
    return [
      tableBox(h("table", { class: "atable team-shifts" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Tech" }),
          roster ? h("th", { scope: "col", text: "Started" }) : null, h("th", { scope: "col", text: "Work days" }),
          h("th", { scope: "col", class: "num", text: "Hours" }), h("th", { scope: "col", text: "Effective from" }),
          roster ? h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Remove" })) : null)),
        view.teamBody, view.addRow), { list: true }),
      h("div", { class: "actions" }, saveButton, view.teamMessage), view.teamHint];
  }

  /* The Team table's last rows, a row group of their own after the techs: a new tech's name and shift, and Add tech;
     then a line for the email and the add row's message. The server makes the email from the name and checks it with
     Zendesk; the email field shows only when that email needs correcting, across the Tech and Work days columns so a
     long address fits (app.css hides the line while it has nothing to show). */
  function addTechRow() {
    view.addName = h("input", { type: "text", placeholder: "First and last name", "aria-label": "Tech name" });
    view.addEmail = h("input", { type: "email", hidden: true,
      placeholder: emailPlaceholder(view.settings.email_domain), "aria-label": "Tech email" });
    view.addControls = shiftControls(true);
    view.addMessage = h("div", { class: "message" });
    var addButton = h("button", { class: "small", text: "Add tech", onclick: function () { addTech(addButton); } });
    onEnter([view.addName, view.addEmail], function () { addTech(addButton); });
    var rows = h("tbody", { class: "row-group add-row" },
      h("tr", null,
        h("td", null, view.addName),
        h("td"),
        h("td", null, view.addControls.pick),
        h("td", { class: "num" }, view.addControls.start, " – ", view.addControls.end),
        h("td"),
        h("td", { class: "num" }, addButton)),
      h("tr", { class: "add-more" },
        h("td", { colspan: "3" }, view.addEmail, view.addMessage),
        h("td", { colspan: "3" })));
    resetAddRow();
    return rows;
  }

  function resetAddRow() {
    view.addName.value = "";
    view.addEmail.value = "";
    view.addEmail.hidden = true;
    fillControls(view.addControls, SHIFT_DAYS, SHIFT_START, SHIFT_END, null);
  }

  function renderTeam() {
    var columns = view.addRow ? "6" : "4";
    view.teamBody.textContent = "";
    if (!view.shifts) {
      view.teamBody.appendChild(h("tr", null, h("td", { colspan: columns, class: "empty", text: "Loading…" })));
    } else if (!view.settings.team.length) {
      view.teamBody.appendChild(h("tr", null, h("td", { colspan: columns, class: "empty",
        text: view.addRow ? "No techs yet. Add each by name." : "No techs are assigned to you yet." })));
    } else {
      view.settings.team.slice().sort(byTechName).forEach(function (email) {
        view.teamBody.appendChild(shiftRow(email).tr);
      });
    }
    tableChanged(view.teamBody);
    if (view.offTech) {
      fillOffTechs();
    }
  }

  function saveTeam(button) {
    if (!view.shifts) {
      setMessage(view.teamMessage, "Still loading the shifts. Try again in a moment.", "error");
      return;
    }
    var pending = view.settings.team.slice().sort(byTechName).map(shiftRow).filter(shiftChanged);
    if (!pending.length) {
      setMessage(view.teamMessage, "Nothing to change.", "ok");
      return;
    }
    button.disabled = true;
    setMessage(view.teamMessage, "Saving…");
    var failed = 0;
    pending.reduce(function (chain, row) {
      return chain.then(function () {
        return saveShift(row).catch(function () { failed += 1; });
      });
    }, Promise.resolve()).then(function () {
      button.disabled = false;
      if (failed) {
        setMessage(view.teamMessage, failed + " of " + pending.length + " shifts weren't saved. See the notes " +
          "on those rows.", "error");
      } else {
        setMessage(view.teamMessage, "Saved.", "ok");
      }
    });
  }

  /* The start first, then the hours: a start and hours on the same day give a new tech those hours from their first
     day. Both are read from the row before either is sent. */
  function saveShift(row) {
    var from = row.controls.date.value;
    var started = startChanged(row) ? row.startInput.value : null;
    var hours = hoursChanged(row) ? shiftBody(row.controls) : null;
    var done = [];
    setMessage(row.message, "Saving…");
    var chain = Promise.resolve(null);
    if (started) {
      chain = chain.then(function () {
        return api("PUT", shiftUrl(row.email) + "/start", { started: started }).then(function (tech) {
          done.push("Tracked from " + dayLabel(started, true) + ".");
          return tech;
        });
      });
    }
    if (hours) {
      chain = chain.then(function () {
        return api("PUT", shiftUrl(row.email), hours).then(function (tech) {
          done.push("The shift applies from " + dayLabel(from, true) + ".");
          return tech;
        });
      });
    }
    return chain.then(function (tech) {
      upsertTech(tech);
      loadShiftRow(row, tech.versions);
      setMessage(row.message, "Saved. " + done.join(" "), "ok");
    }, function (error) {
      setMessage(row.message, error.message, "error");
      throw error;
    });
  }

  /* Add a tech by name (and the email, once the admin corrected the one made from it), then their shift. */
  function addTech(button) {
    var name = view.addName.value.trim();
    if (!name) {
      setMessage(view.teamMessage, "Enter the tech's name.", "error");
      return;
    }
    var typed = view.addEmail.hidden ? "" : view.addEmail.value.trim();
    var shift = shiftBody(view.addControls);
    var address;
    button.disabled = true;
    setMessage(view.teamMessage, "Checking Zendesk…");
    setMessage(view.addMessage, "");
    view.teamHint.hidden = true;
    api("POST", "/api/team", typed ? { name: name, email: typed } : { name: name }).then(function (saved) {
      view.settings = saved;
      address = saved.added;
      refreshPrepSchedule();   // the new tech's 1:1 schedule row
      // Saving the team gave the new tech a starting version from the day the server chose: the retention start
      // before the first activity sync, else today. The row's days and hours replace it on that same day.
      return loadShifts().then(function () {
        var tech = view.shifts.filter(function (t) { return t.email === address; })[0];
        var placed = tech && tech.versions.length ? tech.versions[tech.versions.length - 1] : null;
        if (!placed) {
          throw new Error("The app didn't find the shift it started for them.");
        }
        shift.effective_from = placed.effective_from;
        return api("PUT", shiftUrl(address), shift);
      }).then(function (tech) {
        upsertTech(tech);
        resetAddRow();
        renderTeam();
        setMessage(view.teamMessage, "Added " + address + ".", "ok");
        view.teamHint.hidden = hostedOnly();   // the hint is about Call QA's reviews
      }, function (error) {
        // The tech is on the team with the server's starting shift; the owner can correct it in the table.
        resetAddRow();
        renderTeam();
        setMessage(view.teamMessage, "");
        setMessage(view.addMessage, "Added " + address + " to the team, but their shift wasn't saved: " +
          error.message + " Fix it in the table and click Save team.", "error");
      });
    }, function (error) {
      var fix = error.data && typeof error.data.email === "string" ? error.data.email : null;
      if (fix === null) {
        setMessage(view.teamMessage, error.message, "error");
        return;
      }
      // No Zendesk agent at the email made from the name (a nickname, a two-word last name), or none could be made:
      // the email shows, to correct, and the name and shift stay as typed.
      setMessage(view.teamMessage, "");
      view.addEmail.hidden = false;
      view.addEmail.value = fix;
      setMessage(view.addMessage, sentence(error.message) +
        (fix ? " Correct the email and click Add tech again." : " Or enter their work email."), "error");
    }).then(function () { button.disabled = false; });
  }

  function removeTech(email) {
    var reviews = hostedOnly() ? "" : REMOVE_TEXT;   // about Call QA's results and reviews
    if (!window.confirm("Remove " + email + "?" + (reviews ? "\n\n" + reviews : ""))) {
      return;
    }
    var team = view.settings.team.filter(function (e) { return e !== email; });
    saveSettings({ team: team }).then(function () {
      delete view.shiftRows[email];
      renderTeam();
      refreshPrepSchedule();
      view.teamHint.hidden = true;
      setMessage(view.teamMessage, "Removed " + email + "." + (reviews ? " " + reviews : ""), "ok");
    }).catch(function (error) { setMessage(view.teamMessage, error.message, "error"); });
  }

  // Time off

  function timeOffLabel(item) {
    return item.first === item.last ? dayWithWeekday(item.first)
      : dayLabel(item.first, false) + " – " + dayLabel(item.last, false);
  }

  var TIME_OFF_WORDS = { pto: "PTO", sick: "Sick" };

  function timeOffPanel() {
    view.timeOff = null;
    view.offTech = h("select", { "aria-label": "Tech" });
    view.offFrom = h("input", { type: "date", value: view.today, "aria-label": "First day" });
    view.offTo = h("input", { type: "date", value: view.today, "aria-label": "Last day" });
    view.offKind = h("select", { "aria-label": "Kind" }, h("option", { value: "pto", text: TIME_OFF_WORDS.pto }),
      h("option", { value: "sick", text: TIME_OFF_WORDS.sick }));
    view.offNote = h("input", { type: "text", placeholder: "Note", "aria-label": "Note" });
    view.offMessage = h("div", { class: "message" });
    view.offBody = h("tbody");
    view.offFrom.addEventListener("change", function () {
      if (view.offTo.value < view.offFrom.value) {
        view.offTo.value = view.offFrom.value;
      }
    });
    var addButton = h("button", { class: "small", text: "Add time off", onclick: function () { addTimeOff(addButton); } });
    fillOffTechs();
    renderTimeOff();
    loadTimeOff();
    return panel("Time off", "Days a tech wasn't expected to work. PTO shows as \"Time off\". Sick counts as a sick day, " +
      "even if the tech signed in or did some work.",
      tableBox(h("table", { class: "atable time-off" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Tech" }), h("th", { scope: "col", text: "Dates" }),
          h("th", { scope: "col", text: "Kind" }), h("th", { scope: "col", text: "Note" }),
          h("th", { scope: "col" }, h("span", { class: "sr-only", text: "Edit or remove" })))),
        view.offBody), { list: true }),
      h("div", { class: "actions" }, view.offTech, view.offFrom, view.offTo, view.offKind, view.offNote, addButton),
      view.offMessage);
  }

  function loadTimeOff() {
    var mine = view;
    readPart(mine, ["/api/time-off"], function (got) {
      view.timeOff = got[0].items;
      renderTimeOff();
    }).catch(function (error) {
      if (view === mine) {
        setMessage(view.offMessage, error.message, "error");
      }
    });
  }

  /* The select lists the current team, keeping the tech picked. */
  function fillOffTechs() {
    var chosen = view.offTech.value;
    view.offTech.textContent = "";
    view.settings.team.slice().sort(byTechName).forEach(function (email) {
      view.offTech.appendChild(h("option", { value: email, text: techName(email),
        selected: email === chosen ? "selected" : null }));
    });
  }

  function renderTimeOff() {
    view.offBody.textContent = "";
    if (!view.timeOff) {
      view.offBody.appendChild(h("tr", null, h("td", { colspan: "5", class: "empty", text: "Loading…" })));
      tableChanged(view.offBody);
      return;
    }
    if (!view.timeOff.length) {
      view.offBody.appendChild(h("tr", null, h("td", { colspan: "5", class: "empty", text: "No time off recorded." })));
    }
    view.timeOff.forEach(function (item) {
      view.offBody.appendChild(h("tr", { class: "off-row" },
        h("td", { text: techName(item.email) }),
        h("td", { text: timeOffLabel(item) }),
        h("td", { text: TIME_OFF_WORDS[item.kind] }),
        h("td", { class: "muted", text: item.note }),
        h("td", { class: "num" },
          h("button", { class: "small", text: "Edit", onclick: function () { editTimeOff(item); } }), " ",
          h("button", { class: "small", text: "Remove", onclick: function () { removeTimeOff(item); } }))));
    });
    tableChanged(view.offBody);
  }

  function addTimeOff(button) {
    if (!view.offTech.value) {
      setMessage(view.offMessage, "Add a tech to the team first.", "error");
      return;
    }
    button.disabled = true;
    setMessage(view.offMessage, "Saving…");
    api("POST", "/api/time-off", { email: view.offTech.value, first: view.offFrom.value, last: view.offTo.value,
      note: view.offNote.value.trim(), kind: view.offKind.value }).then(function (item) {
      view.timeOff = (view.timeOff || []).concat([item]);
      view.offNote.value = "";
      renderTimeOff();
      setMessage(view.offMessage, "Added.", "ok");
    }).catch(function (error) {
      setMessage(view.offMessage, error.message, "error");
    }).then(function () { button.disabled = false; });
  }

  /* Time off's Edit: an entry's days, kind and note; the tech stays (remove it and add another to move it). The list
     shows what the server stored. */
  function editTimeOff(item) {
    var mine = view;
    var first = h("input", { type: "date", id: "edit-off-first" });
    var last = h("input", { type: "date", id: "edit-off-last" });
    first.value = item.first;
    last.value = item.last;
    first.addEventListener("change", function () {
      if (last.value < first.value) {
        last.value = first.value;
      }
    });
    var kind = h("select", { id: "edit-off-kind" }, h("option", { value: "pto", text: TIME_OFF_WORDS.pto }),
      h("option", { value: "sick", text: TIME_OFF_WORDS.sick }));
    kind.value = item.kind;
    var note = h("input", { type: "text", id: "edit-off-note", placeholder: "Note" });
    note.value = item.note;
    openDialog("Edit time off", techName(item.email), [
      h("div", { class: "form-row" }, h("label", { for: "edit-off-first", text: "First day" }), first),
      h("div", { class: "form-row" }, h("label", { for: "edit-off-last", text: "Last day" }), last),
      h("div", { class: "form-row" }, h("label", { for: "edit-off-kind", text: "Kind" }), kind),
      h("div", { class: "form-row" }, h("label", { for: "edit-off-note", text: "Note" }), note)
    ], function (button, message) {
      button.disabled = true;
      setMessage(message, "Saving…");
      api("PATCH", "/api/time-off/" + item.id, { first: first.value, last: last.value, kind: kind.value,
        note: note.value.trim() }).then(function (changed) {
        if (view !== mine) {
          return;
        }
        closeDialog();
        view.timeOff = view.timeOff.map(function (other) { return other.id === item.id ? changed : other; });
        renderTimeOff();
        setMessage(view.offMessage, "Saved.", "ok");
      }, function (error) {
        setMessage(message, error.message, "error");
        button.disabled = false;
      });
    });
    first.focus();
  }

  function removeTimeOff(item) {
    api("DELETE", "/api/time-off/" + item.id).then(function () {
      view.timeOff = view.timeOff.filter(function (other) { return other.id !== item.id; });
      renderTimeOff();
      setMessage(view.offMessage, "Removed.", "ok");
    }).catch(function (error) { setMessage(view.offMessage, error.message, "error"); });
  }

  // Activity flags

  var FLAG_FIELDS = [
    ["Late start", "activity_late_minutes", "min after the shift starts"],
    ["Early finish", "activity_early_minutes", "min before the shift ends"],
    ["Long gap", "activity_gap_minutes", "min with nothing recorded"]
  ];

  function flagsPanel() {
    var message = h("div", { class: "message" });
    var inputs = FLAG_FIELDS.map(function (spec, i) {
      return h("input", { type: "number", id: "set-flag-" + i, min: "1", step: "1", "aria-label": spec[0],
        value: view.settings[spec[1]] });
    });
    function save() {
      var changes = {};
      var valid = inputs.every(function (input, i) {
        changes[FLAG_FIELDS[i][1]] = Number(input.value);
        return /^[0-9]+$/.test(input.value.trim()) && Number(input.value) >= 1;
      });
      if (!valid) {
        setMessage(message, "Enter each as a whole number of minutes, 1 or more.", "error");
        return;
      }
      setMessage(message, "Saving…");
      saveSettings(changes).then(function (saved) {
        inputs.forEach(function (input, i) { input.value = saved[FLAG_FIELDS[i][1]]; });
        setMessage(message, "Saved.", "ok");
      }).catch(function (error) { setMessage(message, error.message, "error"); });
    }
    return panel("Activity flags", "When Team activity flags a day",
      FLAG_FIELDS.map(function (spec, i) {
        return h("div", { class: "form-row" }, h("label", { for: "set-flag-" + i, text: spec[0] }), inputs[i],
          h("span", { class: "muted", text: spec[2] }));
      }),
      h("div", { class: "form-row" }, h("span", { class: "label", text: "Always flagged" }),
        h("span", { class: "muted", text: "Sick day · Signed in, no activity" })),
      h("div", { class: "actions" }, h("button", { class: "primary", text: "Save flags", onclick: save }), message));
  }

  /* 1:1 schedule (spec §3): each tech's 1:1 date and repeat, the oneonone_schedule setting. A row per tech on the team
     (GET /api/prep/schedule), keeping its inputs while the page is open, as Team's rows do. Latest 1:1, Next 1:1 and
     That prep covers are the server's, read again after a save and after the team changes, and worked out here with
     the same math (oneonone.cycle) as the owner edits a date or a repeat. */

  var PREP_SCHEDULE_NOTE = "Changing a date or the repeat takes effect at once. A tech added to the team needs a 1:1 " +
    "date before their preps start.";
  var PREP_DEFAULT_WEEKS = 2;   // the repeat a tech with no 1:1 date starts with
  var PREP_MAX_WEEKS = 8;       // cycle.MAX_WEEKS
  var PREP_CYCLE_DAYS = 14;     // cycle.CYCLE_DAYS: a prep covers the 14 days before its 1:1
  var DAY_MS = 86400000;
  var SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* A YYYY-MM-DD day as a whole count of days, so the schedule math is in whole days whatever the clocks do. */
  function dayCount(iso) {
    var parts = iso.split("-").map(Number);
    return Math.round(Date.UTC(parts[0], parts[1] - 1, parts[2]) / DAY_MS);
  }

  /* "Tue Oct 20" and "10/20" for a day count, as oneonone.report labels them. */
  function countLabel(n) {
    var d = new Date(n * DAY_MS);
    return WEEKDAYS[d.getUTCDay()] + " " + SHORT_MONTHS[d.getUTCMonth()] + " " + d.getUTCDate();
  }

  function countMonthDay(n) {
    var d = new Date(n * DAY_MS);
    return (d.getUTCMonth() + 1) + "/" + d.getUTCDate();
  }

  /* A row's 1:1 date when it is a whole date: "" for a field left empty, null for one half typed or not a date. */
  function prepDateOf(row) {
    var value = row.date.value;
    if (row.date.validity && row.date.validity.badInput) {
      return null;
    }
    if (!value) {
      return "";
    }
    return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
  }

  /* A row's repeat in weeks when it is a whole number from 1 to PREP_MAX_WEEKS, else null. */
  function prepWeeksOf(row) {
    var value = row.weeks.value;
    var weeks = Number(value);
    return /^\d+$/.test(value) && weeks >= 1 && weeks <= PREP_MAX_WEEKS ? weeks : null;
  }

  /* The row's Latest 1:1, Next 1:1 and That prep covers from what is typed, as oneonone.report.schedule_rows has
     them (cycle.latest_day, next_day and period_for, in the owner's today); dashes while either field isn't whole. */
  function workOutPrepRow(row) {
    var day = prepDateOf(row);
    var weeks = prepWeeksOf(row);
    if (!day || !weeks) {
      row.latest.textContent = row.next.textContent = row.covers.textContent = "-";
      return;
    }
    var step = weeks * 7;
    var base = dayCount(day);
    var today = dayCount(view.today || todayIn(view.settings.timezone_effective));
    var latest = base + Math.floor((today - base) / step) * step;
    var next = latest + step;
    row.latest.textContent = countLabel(latest);
    row.next.textContent = countLabel(next);
    row.covers.textContent = countMonthDay(next - PREP_CYCLE_DAYS) + " – " + countMonthDay(next - 1);
  }

  function prepSchedulePanel() {
    view.prepItems = null;
    view.prepRows = {};
    view.prepBody = h("tbody");
    view.prepMessage = h("div", { class: "message" });
    var save = h("button", { class: "primary", text: "Save schedule",
      onclick: function () { savePrepSchedule(save); } });
    renderPrepSchedule(false);
    refreshPrepSchedule();
    return panel("1:1 schedule", "Pick any one of a tech's 1:1 dates and how often they repeat. Each prep covers the " +
      "14 days before that tech's 1:1.",
      tableBox(h("table", { class: "atable one-line prep-schedule" },
        h("thead", null, h("tr", null, ["Tech", "A 1:1 date", "Repeats every", "Latest 1:1", "Next 1:1",
          "That prep covers"].map(function (title) { return h("th", { scope: "col", text: title }); }))),
        view.prepBody), { list: true }),
      h("div", { class: "note table-note", text: PREP_SCHEDULE_NOTE }),
      h("div", { class: "actions" }, save, view.prepMessage));
  }

  /* Read the rows; `refill` puts the saved dates and repeats back into rows already shown, as a changed answer does. */
  function loadPrepSchedule(refill) {
    return readPart(view, ["/api/prep/schedule"], function (got, again) {
      view.prepItems = got[0];
      renderPrepSchedule(refill || again);
    });
  }

  /* Read the rows again for the techs on the team now (on opening Settings, and after a tech is added or removed),
     keeping what was typed in rows still shown. */
  function refreshPrepSchedule() {
    var mine = view;
    loadPrepSchedule(false).catch(function (error) {
      if (view === mine) {
        setMessage(view.prepMessage, error.message, "error");
      }
    });
  }

  function renderPrepSchedule(refill) {
    view.prepBody.textContent = "";
    if (!view.prepItems) {
      view.prepBody.appendChild(h("tr", null, h("td", { colspan: "6", class: "empty", text: "Loading…" })));
    } else if (!view.prepItems.length) {
      view.prepBody.appendChild(h("tr", null,
        h("td", { colspan: "6", class: "empty", text: "No techs yet. Add your team's Zendesk emails." })));
    }
    var shown = {};   // a tech taken off the team and added again starts from what is saved
    (view.prepItems || []).forEach(function (item) {
      shown[item.email] = prepScheduleRow(item, refill);
      view.prepBody.appendChild(shown[item.email].tr);
    });
    view.prepRows = shown;
    tableChanged(view.prepBody);
  }

  function prepScheduleRow(item, refill) {
    var row = view.prepRows[item.email];
    if (!row) {
      row = { name: h("b"), latest: h("td"), next: h("td"), covers: h("td", { class: "muted" }),
        date: h("input", { type: "date", "aria-label": "A 1:1 date" }),
        weeks: h("input", { type: "number", min: "1", max: String(PREP_MAX_WEEKS), step: "1",
          "aria-label": "Repeats every" }) };
      row.tr = h("tr", { class: "sched-row" }, h("td", null, row.name), h("td", null, row.date),
        h("td", null, row.weeks, " weeks"), row.latest, row.next, row.covers);
      var edited = workOutPrepRow.bind(null, row);
      ["input", "change"].forEach(function (type) {
        row.date.addEventListener(type, edited);
        row.weeks.addEventListener(type, edited);
      });
      view.prepRows[item.email] = row;
      refill = true;
    }
    if (refill) {
      row.date.value = item.date || "";
      row.weeks.value = String(item.weeks || PREP_DEFAULT_WEEKS);
    }
    row.name.textContent = item.name;
    row.latest.textContent = item.latest_label || "-";
    row.next.textContent = item.next_label || "-";
    row.covers.textContent = item.covers || "-";
    return row;
  }

  /* Save every row with a date (an empty date takes that tech off the schedule). On the PC copy that is one settings
     save, which keeps the entries of techs no longer on the team (the server keeps but ignores them). On the hosted
     copy each tech whose date or repeat changed is saved on its own (`PUT /api/prep/schedule/{email}`): the general
     settings save refuses the schedule, since it would reach other leads' techs. A half-typed date (the browser gives
     it an empty value and validity.badInput) or a repeat outside 1-8 is refused here, naming the tech, and nothing is
     sent; the server checks each date and repeat again. */
  function savePrepSchedule(button) {
    if (!view.prepItems) {
      setMessage(view.prepMessage, "Still loading the schedule. Try again in a moment.", "error");
      return;
    }
    var mine = view;
    var schedule = Object.assign({}, view.settings.oneonone_schedule);
    var refused = null;
    view.prepItems.forEach(function (item) {
      if (refused) {
        return;
      }
      var row = view.prepRows[item.email];
      var day = prepDateOf(row);
      if (day === null) {
        refused = "Enter the whole 1:1 date for " + item.name + ", or clear it to take them off the schedule";
      } else if (day === "") {
        delete schedule[item.email];
      } else if (prepWeeksOf(row) === null) {
        refused = "The repeat for " + item.name + " must be a whole number of weeks from 1 to " + PREP_MAX_WEEKS;
      } else {
        schedule[item.email] = { date: day, weeks: prepWeeksOf(row) };
      }
    });
    if (refused) {
      setMessage(view.prepMessage, refused, "error");
      return;
    }
    button.disabled = true;
    setMessage(view.prepMessage, "Saving…");
    (hostedOnly() ? saveOwnPrepSchedules(schedule) : saveSettings({ oneonone_schedule: schedule })).then(function () {
      return loadPrepSchedule(true);
    }).then(function () {
      if (view === mine) {
        setMessage(view.prepMessage, "Saved.", "ok");
      }
    }).catch(function (error) {
      if (view === mine) {
        setMessage(view.prepMessage, error.message, "error");
      }
    }).then(function () { button.disabled = false; });
  }

  /* The hosted copy's save: one request for each of this person's techs whose entry is not what is stored, one after
     another so a refusal stops at the tech it names. `schedule` is what the rows say, for the whole team. The settings
     held on the page follow what was saved. */
  function saveOwnPrepSchedules(schedule) {
    var saved = Object.assign({}, view.settings.oneonone_schedule);
    var same = function (a, b) { return !a && !b || !!a && !!b && a.date === b.date && a.weeks === b.weeks; };
    var changed = view.prepItems.map(function (item) { return item.email; }).filter(function (email) {
      return !same(schedule[email], saved[email]);
    });
    var mine = view;
    return changed.reduce(function (done, email) {
      return done.then(function () {
        var entry = schedule[email];
        return api("PUT", "/api/prep/schedule/" + encodeURIComponent(email),
          entry ? { date: entry.date, weeks: entry.weeks } : { date: null });
      }).then(function () {
        if (view === mine) {
          if (schedule[email]) {
            view.settings.oneonone_schedule[email] = schedule[email];
          } else {
            delete view.settings.oneonone_schedule[email];
          }
        }
      });
    }, Promise.resolve());
  }

  // Zendesk and credentials

  function credentialStatus(isSet) {
    return h("span", { class: isSet ? "status-set" : "status-unset", text: isSet ? "set" : "not set" });
  }

  function zendeskPanel() {
    var s = view.settings;
    var subdomain = h("input", { type: "text", id: "set-subdomain", value: s.zendesk_subdomain || "",
      placeholder: "yourcompany" });
    var login = h("input", { type: "email", id: "set-login", value: s.zendesk_email || "",
      placeholder: "you@company.com" });
    var domain = h("input", { type: "text", id: "set-email-domain", value: s.email_domain || "",
      placeholder: "company.com" });
    var message = h("div", { class: "message" });
    var save = h("button", { text: "Save", onclick: function () {
      setMessage(message, "Saving…");
      var changes = { zendesk_subdomain: subdomain.value, zendesk_email: login.value, email_domain: domain.value };
      saveSettings(changes).then(function (saved) {
        subdomain.value = saved.zendesk_subdomain || "";
        login.value = saved.zendesk_email || "";
        domain.value = saved.email_domain || "";
        setMessage(message, "Saved.", "ok");
      }).catch(function (error) { setMessage(message, error.message, "error"); });
    } });
    return panel("Zendesk and credentials", "The app only reads from Zendesk. Secrets are never shown here.",
      h("div", { class: "form-row" }, h("label", { for: "set-subdomain", text: "Zendesk subdomain" }), subdomain,
        h("span", { class: "muted", text: ".zendesk.com" })),
      h("div", { class: "form-row" }, h("label", { for: "set-login", text: "Login email" }), login),
      h("div", { class: "form-row" }, h("label", { for: "set-email-domain", text: "Work email domain" }), domain,
        h("span", { class: "hint", text: (hostedOnly() ? "A tech or TL" : "A tech") +
          " added by name gets the email firstname.lastname at this domain." })),
      h("div", { class: "actions" }, save, message),
      h("div", { class: "form-row" }, h("span", { class: "label", text: "Zendesk API token" }),
        credentialStatus(s.credentials.zendesk_token)),
      hostedOnly() ? null : h("div", { class: "form-row" },
        h("span", { class: "label", text: "Anthropic API key" }),
        credentialStatus(s.credentials.anthropic_api_key),
        h("span", { class: "hint" }, "Run ", h("code", { text: "work-assistant setup" }), " to change")));
  }

  // Review settings

  function timeZones() {
    try {
      return Intl.supportedValuesOf("timeZone");
    } catch (e) {
      return [];
    }
  }

  var SONNET_MODEL = "claude-sonnet-5-5";
  var SONNET_EFFORT_NOTE = "Sonnet's effort levels are calibrated differently; its own default is high.";

  /* "about $0.07 per call (estimate)": a model's rough cost per call from the settings' model list. */
  function costPerCallText(m) {
    return "about $" + m.approx_cost_per_call.toFixed(2) + " per call (" + (m.measured ? "measured" : "estimate") + ")";
  }

  function reviewPanel() {
    var s = view.settings;
    var zoneList = h("datalist", { id: "zone-list" }, timeZones().map(function (zone) {
      return h("option", { value: zone });
    }));
    var zone = h("input", { type: "text", id: "set-zone", list: "zone-list", value: s.timezone || "",
      placeholder: "This PC's zone (" + s.timezone_effective + ")" });
    var budget = h("input", { type: "number", id: "set-budget", min: "0.01", step: "0.01", value: s.budget_limit_usd });
    var threshold = h("input", { type: "number", id: "set-threshold", min: "1", step: "1", value: s.problem_threshold });
    var model = h("select", { id: "set-model" }, s.models.map(function (m) {
      return h("option", { value: m.id, text: m.label, selected: m.id === s.model ? "selected" : null });
    }));
    var modelCost = h("span", { class: "hint" });
    var effort = h("select", { id: "set-effort" }, s.efforts.map(function (level) {
      return h("option", { value: level, text: level, selected: level === s.effort ? "selected" : null });
    }));
    var sonnetNote = h("span", { class: "hint", text: SONNET_EFFORT_NOTE });
    var retention = h("input", { type: "number", id: "set-retention", min: "1", step: "1", value: s.retention_days });
    var message = h("div", { class: "message" });

    // The cost line follows the model picked in the select; the Sonnet note shows only while Sonnet is picked.
    function showModel(id) {
      var chosen = view.settings.models.filter(function (m) { return m.id === id; })[0];
      modelCost.textContent = chosen ? costPerCallText(chosen) : "";
      sonnetNote.hidden = id !== SONNET_MODEL;
    }
    model.addEventListener("change", function () { showModel(model.value); });
    showModel(s.model);

    function save() {
      var changes = {
        timezone: zone.value.trim() || null,
        budget_limit_usd: Number(budget.value),
        problem_threshold: Number(threshold.value),
        model: model.value,
        effort: effort.value,
        retention_days: Number(retention.value)
      };
      var days = retention.value.trim();
      if (!/^[0-9]+$/.test(days) || Number(days) < 1) {
        setMessage(message, "Enter the data retention as a whole number of days, 1 or more.", "error");
        retention.focus();
        return;
      }
      if (changes.retention_days < view.settings.retention_days && !window.confirm(
        "Calls older than " + changes.retention_days + " days will be permanently erased now: their " +
        "customer content (transcript, emails and evidence) can't be brought back.\n\nSave the shorter retention?")) {
        return;
      }
      setMessage(message, "Saving…");
      saveSettings(changes).then(function (saved) {
        zone.placeholder = "This PC's zone (" + saved.timezone_effective + ")";
        showModel(saved.model);   // a measured cost is measured at the saved effort
        setMessage(message, "Saved." + (saved.retention_message ? " " + saved.retention_message : ""), "ok");
      }).catch(function (error) { setMessage(message, error.message, "error"); });
    }

    return panel("Reviews", "Applies from the next run.",
      h("div", { class: "form-row" }, h("label", { for: "set-zone", text: "Time zone" }), zone, zoneList,
        h("span", { class: "hint", text: "Weeks run Sunday to Saturday in this zone. Leave blank to use this PC's zone." })),
      h("div", { class: "form-row" }, h("label", { for: "set-budget", text: "Budget limit per run ($)" }), budget,
        h("span", { class: "hint", text: "A run estimated above this waits for your OK before it is submitted." })),
      h("div", { class: "form-row" }, h("label", { for: "set-threshold", text: "Problem threshold" }), threshold,
        h("span", { class: "hint", text: "Misses on one call that make it a problem ticket." })),
      h("div", { class: "form-row" }, h("label", { for: "set-model", text: "Model" }), model, modelCost),
      h("div", { class: "form-row" }, h("label", { for: "set-effort", text: "Reviewer effort" }), effort,
        h("span", { class: "hint", text: "Higher effort is more thorough and costs more." }), sonnetNote),
      h("div", { class: "form-row" }, h("label", { for: "set-retention", text: "Data retention (days)" }), retention,
        h("span", { class: "hint", text: "Customer content older than this is permanently erased." })),
      h("div", { class: "actions" }, h("button", { class: "primary", text: "Save", onclick: save }), message));
  }

  function schedulePanel() {
    // The status comes from its own request: asking Task Scheduler takes a moment, and nothing else waits on it.
    var shown = [scheduleRow("Schedule status", "Checking…")];
    function show(rows) {
      shown[0].replaceWith.apply(shown[0], rows);
      shown.slice(1).forEach(function (row) { row.remove(); });
      shown = rows;
    }
    readPart(view, ["/api/schedule"], function (got) { show(scheduleRows(got[0])); }).catch(function () {
      show(scheduleRows(SCHEDULE_FALLBACK));
    });
    return panel("Schedule", "Every day at 06:00 it reviews the previous day's calls, and any days it missed. " +
      "It runs only while you are logged on to this PC.",
      shown[0],
      h("div", { class: "form-row" },
        h("span", { class: "hint" }, "To turn it on, run ", h("code", { text: "work-assistant schedule install" }),
          " in a terminal. To turn it off, run ", h("code", { text: "work-assistant schedule remove" }), ".")),
      h("div", { class: "form-row" },
        h("span", { class: "hint" }, "Each scheduled run writes its results to ", h("code", { text: "logs/scheduled.log" }),
          ".")));
  }

  function scheduleRows(s) {
    var rows = [scheduleRow("Schedule status", s.text)];
    if (s.installed) {
      rows.push(scheduleRow("Next run", s.next_run || "None scheduled"),
        scheduleRow("Last run", s.last_run || "Not yet"),
        scheduleRow("Last result", s.last_result || "None yet"));
    }
    return rows;
  }

  function scheduleRow(label, value) {
    return h("div", { class: "form-row" }, h("span", { class: "label", text: label }), h("span", { text: value }));
  }

  function agreementPanel() {
    var line = h("div", { class: "agreement-line", text: "Loading…" });
    readPart(view, ["/api/agreement?weeks=" + AGREEMENT_WEEKS], function (got) {
      var data = got[0];
      line.textContent = data.overall == null
        ? "No reviewed checks in the last " + data.weeks + " weeks yet."
        : "The reviewer's call was kept on " + pct(data.overall * 100) + " of checks over the last " + data.weeks + " weeks.";
    }).catch(function (error) { setMessage(line, error.message, "error"); });
    return panel("Reviewer agreement", "The share of checks where you kept the reviewer's outcome (no Disagree). " +
      "The main sign of whether the reviewer can be trusted. Rates per check are on Rubric & rules.", line);
  }

  // Run now

  function runNowPanel() {
    var week = lastWeek();
    var from = h("input", { type: "date", id: "run-from", value: week.from });
    var to = h("input", { type: "date", id: "run-to", value: week.to });
    var rereview = h("input", { type: "checkbox", id: "run-rereview" });
    var message = h("div", { class: "message" });
    var box = h("div", { class: "estimate", hidden: true });
    var estimateButton = h("button", { class: "primary", text: "Get estimate" });

    function request() {
      return { from: from.value, to: to.value, rereview: rereview.checked };
    }

    function stale() {
      box.hidden = true;
      setMessage(message, "");
    }
    [from, to, rereview].forEach(function (input) { input.addEventListener("change", stale); });

    estimateButton.addEventListener("click", function () {
      var body = request();
      box.hidden = true;
      estimateButton.disabled = true;
      setMessage(message, "Estimating… this reads the calls from Zendesk and can take a few minutes.");
      api("POST", "/api/runs/estimate", body).then(function (estimate) {
        setMessage(message, "");
        showEstimate(box, body, estimate, message, view.settings.budget_limit_usd);
      }).catch(function (error) { setMessage(message, error.message, "error"); })
        .then(function () { estimateButton.disabled = false; });
    });

    return panel("Run now for a date range", "Shows the cost estimate first; nothing is reviewed until you start it.",
      h("div", { class: "form-row" }, h("label", { for: "run-from", text: "First day" }), from),
      h("div", { class: "form-row" }, h("label", { for: "run-to", text: "Last day" }), to),
      h("div", { class: "form-row" }, h("label", { for: "run-rereview", text: "Re-review" }), rereview,
        h("span", { class: "hint", text: "Score calls in this range again under the current rubric, including calls already reviewed." })),
      h("div", { class: "actions" }, estimateButton), message, box);
  }

  /* The estimate and its Start button, for Settings' Run now.
     Starting a run never skips the pipeline's budget guard: an over-limit run waits for the banner's OK. */
  function showEstimate(box, body, estimate, message, limit) {
    var calls = estimate.contacts === 1 ? "1 call" : estimate.contacts + " calls";
    box.textContent = "";
    box.appendChild(h("div", null, h("b", { text: calls }), " to review, estimated " , h("b", { text: money(estimate.estimate_usd) }),
      " (" + rangeLabel(body.from, body.to) + (body.rereview ? ", re-review" : "") + ")."));
    if (estimate.excluded_purged > 0) {
      box.appendChild(h("div", { class: "muted", text: (estimate.excluded_purged === 1 ? "1 call" :
        estimate.excluded_purged + " calls") + " older than the retention period can't be re-reviewed: their " +
        "content was erased." }));
    }
    var overLimit = estimate.estimate_usd > limit;
    if (overLimit) {
      box.appendChild(h("div", { class: "over", text: overLimitText(limit, false) }));
    }
    if (!estimate.contacts) {
      box.appendChild(h("div", { class: "muted", text: "Nothing to review in this range." }));
      box.hidden = false;
      return;
    }
    var start = h("button", { class: "primary", text: (body.rereview ? "Start re-review" : "Start run") +
      (overLimit ? " (will wait for your OK)" : "") });
    var cancel = h("button", { text: "Cancel", onclick: function () { box.hidden = true; } });
    start.addEventListener("click", function () {
      start.disabled = true;
      api("POST", "/api/runs", body).then(function () {
        box.hidden = true;
        setMessage(message, (body.rereview ? "Re-review" : "Run") + " started. It appears in the run history " +
          (parseRoute().tab === "settings" ? "below" : "on the Settings tab") + " in a few seconds. If its cost " +
          "comes out above your budget limit, it will wait for you to confirm it in the banner.", "ok");
        window.setTimeout(afterRunAction, 3000);
      }).catch(function (error) {
        start.disabled = false;
        setMessage(message, error.message, "error");
      });
    });
    box.appendChild(h("div", { class: "actions" }, start, cancel));
    box.hidden = false;
  }

  /* What an estimate over the budget limit says, for Run now and for Test a model (`test`), which reads the
     stored calls and never fetches them. */
  function overLimitText(limit, test) {
    return "This is over your " + money(limit) + " budget limit. Starting it " +
      (test ? "reads the stored calls" : "fetches the calls") +
      ", then pauses for your OK in the banner before anything is paid for.";
  }

  // Test a model

  var TEST_STARTED = "Test started. It appears in Run history and under Model tests; the comparison opens when it " +
    "finishes.";

  /* Test a model: score calls that already have a shown review again with another model and effort, to
     compare. The estimate comes first, then Start test, as in Run now. The model tests list sits under the
     form, each linking to its comparison. */
  function testPanel() {
    var s = view.settings;
    var week = lastWeek();
    // Start on a model other than the one reviewing now: that is what a test is for.
    var first = s.models.filter(function (m) { return m.id !== s.model; })[0] || s.models[0];
    var model = h("select", { id: "test-model" }, s.models.map(function (m) {
      return h("option", { value: m.id, text: m.label + " · " + costPerCallText(m),
        selected: m.id === first.id ? "selected" : null });
    }));
    var effort = h("select", { id: "test-effort" }, s.efforts.map(function (level) {
      return h("option", { value: level, text: level, selected: level === s.effort ? "selected" : null });
    }));
    var sonnetNote = h("span", { class: "hint", text: SONNET_EFFORT_NOTE });
    var from = h("input", { type: "date", id: "test-from", value: week.from });
    var to = h("input", { type: "date", id: "test-to", value: week.to });
    var limit = h("input", { type: "number", id: "test-limit", min: "1", step: "1", placeholder: "Every call" });
    var message = h("div", { class: "message" });
    var box = h("div", { class: "estimate", hidden: true });
    var estimateButton = h("button", { class: "primary", text: "Get estimate" });
    view.testsList = h("div", { class: "muted", text: "Loading…" });

    function request() {
      var cap = limit.value.trim();
      return { from: from.value, to: to.value, model: model.value, effort: effort.value,
        limit: cap === "" ? null : Number(cap) };
    }

    function stale() {
      box.hidden = true;
      setMessage(message, "");
    }
    [model, effort, from, to, limit].forEach(function (input) { input.addEventListener("change", stale); });
    // The Sonnet note shows only while Sonnet is picked, as under Reviews.
    function showNote() {
      sonnetNote.hidden = model.value !== SONNET_MODEL;
    }
    model.addEventListener("change", showNote);
    showNote();

    estimateButton.addEventListener("click", function () {
      var body = request();
      box.hidden = true;
      estimateButton.disabled = true;
      setMessage(message, "Estimating…");
      api("POST", "/api/tests/estimate", body).then(function (estimate) {
        setMessage(message, "");
        showTestEstimate(box, body, estimate, message, view.settings.budget_limit_usd);
      }).catch(function (error) { setMessage(message, error.message, "error"); })
        .then(function () { estimateButton.disabled = false; });
    });

    return panel("Test a model", "Scores calls that already have a shown review again with another model or " +
      "effort, so you can compare. What the app shows never changes. Shows the cost estimate first.",
      h("div", { class: "form-row" }, h("label", { for: "test-model", text: "Model" }), model),
      h("div", { class: "form-row" }, h("label", { for: "test-effort", text: "Effort" }), effort, sonnetNote),
      h("div", { class: "form-row" }, h("label", { for: "test-from", text: "First day" }), from),
      h("div", { class: "form-row" }, h("label", { for: "test-to", text: "Last day" }), to),
      h("div", { class: "form-row" }, h("label", { for: "test-limit", text: "Call limit" }), limit,
        h("span", { class: "hint", text: "Optional: test only the newest this many calls. Leave it empty to test " +
          "every reviewed call in the range." })),
      h("div", { class: "actions" }, estimateButton), message, box,
      h("h4", { class: "group-head", text: "Model tests" }),
      view.testsList);
  }

  /* The test's estimate: how many calls it tests, how many it skips and why, and the cost; then Start test.
     Starting never skips the budget guard: a test over the limit waits for the banner's OK. */
  function showTestEstimate(box, body, estimate, message, limit) {
    var skipped = estimate.excluded_unreviewed + estimate.excluded_purged + estimate.excluded_errored;
    box.textContent = "";
    box.appendChild(h("div", null, h("b", { text: plural(estimate.contacts, "call", "calls") + " will be tested" }),
      " · " + skipped + " skipped (" + estimate.excluded_unreviewed + " with no review, " + estimate.excluded_purged +
      " erased by retention, " + estimate.excluded_errored + " with an errored review) · estimate ",
      h("b", { text: money(estimate.estimate_usd) })));
    var overLimit = estimate.estimate_usd > limit;
    if (overLimit) {
      box.appendChild(h("div", { class: "over", text: overLimitText(limit, true) }));
    }
    if (!estimate.contacts) {
      box.appendChild(h("div", { class: "muted", text: "Nothing to test in this range." }));
      box.hidden = false;
      return;
    }
    var start = h("button", { class: "primary", text: "Start test" + (overLimit ? " (will wait for your OK)" : "") });
    var cancel = h("button", { text: "Cancel", onclick: function () { box.hidden = true; } });
    start.addEventListener("click", function () {
      start.disabled = true;
      api("POST", "/api/tests", body).then(function () {
        box.hidden = true;
        setMessage(message, TEST_STARTED, "ok");
        window.setTimeout(afterRunAction, 3000);
      }).catch(function (error) {
        start.disabled = false;
        setMessage(message, error.message, "error");
      });
    });
    box.appendChild(h("div", { class: "actions" }, start, cancel));
    box.hidden = false;
  }

  function loadTests() {
    var box = view.testsList;
    if (!box) {
      return;
    }
    readPart(view, ["/api/tests"], function (got) {
      var data = got[0];
      box.className = "";
      box.replaceChildren(data.tests.length ? testsTable(data.tests)
        : h("div", { class: "muted", text: "No model tests yet." }));
    }).catch(function (error) {
      box.className = "";
      box.replaceChildren(h("div", { class: "message error", text: error.message }));
    });
  }

  /* The model tests, newest first, two lines each like Run history: what was tested and where it stands, then
     the window, calls and cost. Each test links to its comparison. */
  function testsTable(tests) {
    function heads(labels) {
      return labels.map(function (label) { return h("th", { scope: "col", text: label }); });
    }
    return h("div", { class: "scroll" }, h("table", { class: "atable two-line runs tests" },
      h("thead", null, h("tr", { class: "line1" }, heads(["Model", "Effort", "Status"])),
        h("tr", { class: "line2" }, heads(["Window", "Calls", "Cost"]))),
      tests.map(function (t) {
        var link = testHash(t.run_id);
        var rows = h("tbody", { class: "row-group" },
          h("tr", { class: "rowlink line1" },
            h("td", null, h("a", { href: link, text: t.model_label })),
            h("td", { text: t.effort }),
            h("td", { class: "run-state" },
              h("span", { class: "run-status " + t.status, text: STATUS_LABELS[t.status] || t.status }))),
          h("tr", { class: "rowlink line2" },
            h("td", { class: "run-window", text: runWindowText(t.window.first_day, t.window.last_day) }),
            h("td", { text: t.calls }),
            h("td", { text: money(t.cost_usd) })));
        rows.addEventListener("click", function (event) {
          if (!event.target.closest("a")) {
            location.hash = link;
          }
        });
        return rows;
      })));
  }

  // Run history

  function historyPanel() {
    view.totalTile = h("div", { class: "v", text: "-" });
    view.runCount = h("div", { class: "d", text: "" });
    view.historyMessage = h("div", { class: "message" });
    // Two lines per run, so the table fits its half-width column: what the run is and where it stands (with
    // its Confirm… or Clear… action under the status), then its figures. Each run is its own tbody, with its
    // log row last.
    view.historyTable = h("table", { class: "atable two-line runs" },
      h("thead", null,
        h("tr", { class: "line1" }, ["Run", "Kind", "Window", "Status"].map(function (label) {
          return h("th", { scope: "col", text: label });
        })),
        h("tr", { class: "line2" }, ["Calls", "Estimate", "Actual", "Started"].map(function (label) {
          return h("th", { scope: "col", text: label });
        }))));
    return panel("Run history", "The latest 50 runs. Costs are what Anthropic charged for each run's reviews.",
      h("div", { class: "tiles" }, h("div", { class: "tile" }, h("div", { class: "k", text: "Total spent, all runs" }),
        view.totalTile, view.runCount)),
      h("div", { class: "scroll" }, view.historyTable),
      view.historyMessage);
  }

  function loadRuns() {
    if (!view.historyTable) {
      return;
    }
    readPart(view, ["/api/runs"], function (got) {
      var data = got[0];
      view.totalTile.textContent = money(data.total_spent_usd);
      view.runCount.textContent = data.runs.length ? "" : "No runs yet";
      view.historyTable.querySelectorAll("tbody").forEach(function (rows) { rows.remove(); });
      data.runs.forEach(function (run) { appendRun(view.historyTable, run); });
    }).catch(function (error) { setMessage(view.historyMessage, error.message, "error"); });
  }

  /* A run's window for a two-line table, which wraps only between its two days: "Oct 4 –" over "Oct 10, 2026". */
  function runWindowText(first, last) {
    return rangeLabel(first, last).split(" – ").map(function (day) {
      return day.replace(/ /g, "\u00a0");
    }).join("\u00a0– ");
  }

  // A scheduled run is labelled by its window: a day since T-040, a week from the old weekly schedule. Whole
  // days are counted (rounded), since a window across a DST change is an hour short or long.
  function kindLabel(run) {
    if (run.kind === "scheduled") {
      var days = Math.round((Date.parse(run.window_end) - Date.parse(run.window_start)) / 86400000);
      return days < 7 ? KIND_LABELS.scheduled : "Weekly";
    }
    return KIND_LABELS[run.kind] || run.kind;
  }

  function appendRun(table, run) {
    var action = null;
    if (run.status === "awaiting_confirmation") {
      action = [h("button", { class: "small", text: "Confirm…", onclick: function () { confirmRun(run.run_id); } }),
        h("button", { class: "small", text: "Cancel…", onclick: function () { cancelRun(run.run_id, run.kind); } })];
    } else if (run.status === "submission_unknown") {
      action = h("button", { class: "small", text: "Clear…", onclick: function () { clearRun(run.run_id, run.kind); } });
    }
    var flagged = run.status === "failed" || run.status === "submission_unknown";
    var days = runWindowText(run.first_day, run.last_day);
    // A finished model test links to its comparison.
    var compared = run.kind === "test" && run.status === "complete";
    var rows = h("tbody", { class: flagged ? "row-group flagged" : "row-group" },
      h("tr", { class: "line1" },
        h("td", { text: run.run_id }),
        h("td", null, compared ? h("a", { href: testHash(run.run_id), title: "See how the test compares",
          text: kindLabel(run) }) : kindLabel(run)),
        h("td", { class: "run-window", text: days }),
        h("td", { class: "run-state" },
          h("span", { class: "run-status " + run.status, text: STATUS_LABELS[run.status] || run.status }), action)),
      h("tr", { class: "line2" },
        h("td", { text: run.contact_count == null ? "-" : run.contact_count }),
        h("td", { text: money(run.estimate_usd) }),
        h("td", { text: money(run.actual_usd) }),
        h("td", { text: timeLabel(run.created_at) })));
    if (run.error || run.log_tail.length || run.excluded_purged) {
      rows.appendChild(h("tr", { class: "logrow" }, h("td", null), h("td", { colspan: 3 },
        run.error ? h("div", { class: flagged ? "flag failure" : "muted", text: run.error_label || run.error }) : null,
        run.error && run.error_label && run.error_label !== run.error
          ? h("div", { class: "note", text: "Recorded error: " + run.error }) : null,
        run.excluded_purged ? h("div", { class: "note", text: run.excluded_purged + " purged calls left out." }) : null,
        run.log_tail.length ? h("details", { class: "log" }, h("summary", { text: "Log (last " + run.log_tail.length + " lines)" }),
          h("pre", { text: run.log_tail.join("\n") })) : null)));
    }
    table.appendChild(rows);
  }

  // -- model tests ------------------------------------------------------------------------------

  /* A model test's comparison (#/settings/tests/<run_id>) and one of its calls side by side
     (#/settings/tests/<run_id>/<ticket_id>), sub-views of Settings. The main numbers compare the test model
     with the original reviewer's outcomes, before any Disagree; the owner's overrides get their own line. */

  var MISSING_TEST = "This model test doesn't exist.";
  var OVERRIDE_HINT = "The original model matches 0 by definition: overruling a check means you disagreed with " +
    "its outcome.";
  var MISMATCH_TITLE = "The shown review was scored under a different rubric version, so differences may come " +
    "from the rules rather than the model.";
  var JUDGMENTS = [
    { key: "counted", label: "Counted in scores" },
    { key: "resolved_on_call", label: "Resolved on the call" },
    { key: "call_dropped", label: "Dropped call" }
  ];

  function testHash(runId, ticketId) {
    return "#/settings/tests/" + runId + (ticketId == null ? "" : "/" + ticketId);
  }

  /* A score difference with its sign: "+2.5", "−1.3", "0". */
  function signed(value) {
    if (value == null) {
      return "-";
    }
    return value === 0 ? "0" : (value > 0 ? "+" : "−") + num(Math.abs(value));
  }

  function backLink(href, text) {
    return h("div", { class: "back-link" }, h("a", { href: href, text: text }));
  }

  function renderTestView(token, runId, ticketId) {
    view = {};
    var toSettings = backLink("#/settings", "← Back to Settings");
    function fail(text, back) {
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: text }));
      screen.appendChild(back);
    }
    if (!/^\d+$/.test(runId || "")) {
      fail(MISSING_TEST, toSettings);
      return;
    }
    var noCall = "Model test " + runId + " has no result for call " + ticketId + ".";
    var back = ticketId == null ? toSettings : backLink(testHash(runId), "← Back to the comparison");
    if (ticketId != null && !/^\d+$/.test(ticketId)) {
      fail(noCall, back);
      return;
    }
    loadingLine(ticketId == null ? "Loading the model test…" : "Loading call #" + ticketId + "…");
    // The list says whether the test exists, and gives its window and status.
    readScreen(token, ["/api/tests"], function (got, listToken) {
      var test = got[0].tests.filter(function (t) { return String(t.run_id) === runId; })[0];
      if (!test) {
        fail(MISSING_TEST, toSettings);
        return;
      }
      var url = "/api/tests/" + runId + (ticketId == null ? "" : "/calls/" + ticketId);
      readScreen(listToken, [url], function (data) {
        clearScreen();
        append(screen, ticketId == null ? testReportView(data[0], test) : testCallView(data[0], test));
      }).catch(function (error) {
        if (listToken === renderToken) {
          fail(error.message, back);
        }
      });
    }).catch(function (error) {
      if (token === renderToken) {
        fail(error.message, back);
      }
    });
  }

  /* The comparison, top to bottom: the header, the summary tiles, the verdict changes (beside the override
     line when there is one), the checks they disagree on most beside the other judgments, the calls, and the
     calls the test couldn't review. */
  function testReportView(report, test) {
    var changes = verdictChangesPanel(report.summary);
    var errors = report.calls.filter(function (c) { return c.test_error; });
    return [
      testHeader(report.summary, test),
      h("div", { class: "panel-row cols-5-lead" }, testTiles(report.summary)),
      report.overrides.checks ? h("div", { class: "panel-row cols-2 stretch" }, changes, overridePanel(report))
        : changes,
      h("div", { class: "panel-row cols-2-1" }, disagreePanel(report.checks), judgmentsPanel(report.judgments)),
      compareCallsPanel(report.calls, test.run_id),
      errors.length ? testErrorsPanel(errors, test.run_id) : null
    ];
  }

  function testStatusNote(test) {
    var label = STATUS_LABELS[test.status] || test.status;
    if (test.status === "complete") {
      return null;
    }
    return h("div", { class: "note-banner", role: "note", text: test.status === "failed" || test.status === "cancelled"
      ? "This test didn't finish (" + label + "). Only the calls it reviewed before it stopped are shown."
      : "This test hasn't finished (" + label + "). Its calls show here once it does." });
  }

  function testHeader(summary, test) {
    var many = summary.shown_models.length > 1;
    var shown = summary.shown_models.map(function (m) {
      return modelEffortText(m.model_label, m.effort) + (many ? ", " + plural(m.calls, "call", "calls") : "");
    }).join("; ");
    return h("section", { class: "panel detail" },
      backLink("#/settings", "← Back to Settings"),
      h("div", { class: "dethead" }, h("div", null,
        h("div", { class: "big", text: "Model test: " + modelEffortText(summary.test_model, summary.test_effort) +
          " vs shown results" + (shown ? " (" + shown + ")" : "") }),
        h("div", { class: "muted", text: "Calls from " + rangeLabel(test.window.first_day, test.window.last_day) +
          " · " + (STATUS_LABELS[test.status] || test.status) }),
        h("div", { class: "note", text: "The numbers compare the test model with the original reviewer's outcomes, " +
          "before any Disagree. Nothing the app shows was changed by this test." }))),
      testStatusNote(test));
  }

  function testTiles(s) {
    var callsNote = [];
    if (s.test_errors) {
      callsNote.push(plural(s.test_errors, "test error", "test errors") + " left out");
    }
    if (s.rubric_mismatch_calls) {
      callsNote.push(s.rubric_mismatch_calls + " under a different rubric version");
    }
    var unrecorded = [];
    if (s.cost_not_recorded.shown) {
      unrecorded.push(s.cost_not_recorded.shown + " shown");
    }
    if (s.cost_not_recorded.test) {
      unrecorded.push(s.cost_not_recorded.test + " test");
    }
    function note(text) {
      return h("div", { class: "d", text: text });
    }
    return [
      tile("Check agreement", pct(s.check_agreement_pct), note("same outcome from both models"),
        "Of the checks both reviews have, the share where the test model gave the original reviewer's outcome", true),
      tile("Calls compared", String(s.calls), note(callsNote.join(" · "))),
      tile("Avg score difference", signed(s.avg_score_diff), note("test minus shown, out of 100")),
      tile("Test run total", money(s.test_run_total_usd), note("the whole test, errors included")),
      tile("Cost per call", [money(s.cost_per_call.test), h("span", { class: "of", text: " test" })],
        note(money(s.cost_per_call.shown) + " shown results" +
          (unrecorded.length ? " · not recorded for " + unrecorded.join(", ") : "")))
    ];
  }

  function verdictChangesPanel(summary) {
    var title = "Verdict changes";
    if (!summary.verdict_changes.length) {
      return panel(title, null, h("div", { class: "placeholder", text: "No call's verdict changed." }));
    }
    return panel(title, "Calls whose verdict differs, shown → test. Here Not counted is a verdict of its own.",
      h("ul", { class: "vchanges" }, summary.verdict_changes.map(function (c) {
        return h("li", null, verdictBadge({ verdict: c.from }), " → ", verdictBadge({ verdict: c.to }),
          h("b", { text: ": " + c.count }));
      })));
  }

  /* "On the 5 checks you overruled: Claude Opus 5.5 matched you 0 of 5, Claude Sonnet 5.5 matched you 3 of 5." */
  function overridePanel(report) {
    var s = report.summary;
    var o = report.overrides;
    var single = s.shown_models.length === 1 ? s.shown_models[0] : null;
    var original = single ? single.model_label : "the original reviewer";
    var tested = s.test_model;
    if (single && original === tested) {   // the same model at another effort: name the efforts too
      original = modelEffortText(original, single.effort);
      tested = modelEffortText(tested, s.test_effort);
    }
    if (original === tested) {
      original = "the original review";
      tested = "the test";
    }
    var n = o.checks;
    return panel("Checks you overruled", null,
      h("p", { class: "override-line", text: "On the " + plural(n, "check", "checks") + " you overruled: " + original +
        " matched you " + o.original_matched + " of " + n + ", " + tested + " matched you " + o.test_matched + " of " +
        n + "." }),
      h("div", { class: "note", text: OVERRIDE_HINT }));
  }

  function disagreePanel(checks) {
    var title = "Checks they disagree on most";
    if (!checks.length) {
      return panel(title, null, h("div", { class: "placeholder", text: "No calls were compared." }));
    }
    var rows = checks.map(function (c) {
      var text = c.label + ": the same outcome on " + pct(c.agreement_pct) + " of calls";
      var bar = h("td", { class: "barcell" });
      Charts.passBar(bar, c.agreement_pct, null, { flagged: c.differing > 0, title: text, ariaLabel: text });
      return h("tr", { title: text },
        h("td", { text: c.label }),
        bar,
        h("td", { class: "num", text: pct(c.agreement_pct) }),
        h("td", { class: "num muted", text: String(c.differing) }));
    });
    return panel(title, "How often the two models gave a check the same outcome, lowest first.",
      h("div", { class: "scroll" }, h("table", { class: "atable" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", text: "Check" }),
          h("th", { scope: "col", class: "barcell", text: "Agreement" }),
          h("th", { scope: "col", class: "num", text: "Agree" }),
          h("th", { scope: "col", class: "num", text: "Calls that differ" }))),
        h("tbody", null, rows))));
  }

  function judgmentsPanel(judgments) {
    return panel("Other judgments", "Calls where the two models made the same call on these, or a different one. " +
      "Counted means kept in scores rather than Not counted.",
      h("div", { class: "scroll" }, h("table", { class: "atable" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", text: "Judgment" }),
          h("th", { scope: "col", class: "num", text: "Same" }),
          h("th", { scope: "col", class: "num", text: "Different" }),
          h("th", { scope: "col", class: "num", text: "Not recorded" }))),
        h("tbody", null, JUDGMENTS.map(function (j) {
          var counts = judgments[j.key];
          return h("tr", null,
            h("td", { text: j.label }),
            h("td", { class: "num", text: String(counts.same) }),
            h("td", { class: counts.different ? "num flag crit" : "num", text: String(counts.different) }),
            h("td", { class: "num muted", text: String(counts.not_recorded) }));
        })))));
  }

  function compareCallsPanel(calls, runId) {
    if (!calls.length) {
      return panel("Calls", null, h("div", { class: "placeholder", text: "No calls have test results yet." }));
    }
    var box = h("div", { class: "scroll" });
    fitRowMode(box, function (mode) { return compareCallsTable(calls, runId, mode); });
    return panel("Calls", "Largest score difference first. Click a call to see every check side by side.", box);
  }

  /* One row per call when the panel is wide enough ("one-line"), else two: the call, its scores and verdicts,
     then the checks that differ, each side's cost and the rubric flag. Each call's tbody is one click target. */
  function compareCallsTable(calls, runId, mode) {
    var oneLine = mode === "one-line";
    var bodies = calls.map(function (c) {
      var link = testHash(runId, c.ticket_id);
      var testVerdict = c.test_error ? verdictBadge({ review_error: true, error: c.test_error })
        : verdictBadge({ verdict: c.test_verdict });
      var first = [
        h("td", null, h("a", { href: link, text: "#" + c.ticket_id })),
        h("td", { text: c.agent }),
        h("td", { class: "nowrap", text: dayLabel(c.date, false) }),
        h("td", { class: "num nowrap", text: num(c.shown_score) + " → " + num(c.test_score) }),
        h("td", { class: "nowrap" }, verdictBadge({ verdict: c.shown_verdict }), " → ", testVerdict)
      ];
      var second = [
        h("td", { class: "num", text: c.differing_checks == null ? "-" : String(c.differing_checks) }),
        h("td", { class: "num nowrap", text: money(c.shown_cost) + " / " + money(c.test_cost) }),
        h("td", { colspan: oneLine ? null : 3 }, c.rubric_mismatch
          ? h("span", { class: "flag crit", title: MISMATCH_TITLE, text: "Different rubric version" })
          : h("span", { class: "muted", text: "Same" }))
      ];
      var rows = oneLine
        ? h("tbody", { class: "row-group" }, h("tr", { class: "rowlink" }, first, second))
        : h("tbody", { class: "row-group" }, h("tr", { class: "rowlink line1" }, first),
          h("tr", { class: "rowlink line2" }, second));
      rows.addEventListener("click", function (event) {
        if (!event.target.closest("a")) {
          location.hash = link;
        }
      });
      return rows;
    });
    var firstHeads = ["Ticket", "Tech", "Date", "Score (shown → test)", "Verdict (shown → test)"].map(function (label) {
      return h("th", { scope: "col", text: label });
    });
    var secondHeads = [h("th", { scope: "col", text: "Checks that differ" }),
      h("th", { scope: "col", text: "Cost (shown / test)" }),
      h("th", { scope: "col", colspan: oneLine ? null : 3, text: "Rubric" })];
    var head = oneLine
      ? h("thead", null, h("tr", null, firstHeads, secondHeads))
      : h("thead", null, h("tr", { class: "line1" }, firstHeads), h("tr", { class: "line2" }, secondHeads));
    return h("table", { class: "atable compare-calls " + mode }, head, bodies);
  }

  function testErrorsPanel(calls, runId) {
    return panel("Calls the test couldn't review", "Left out of every number above.",
      h("ul", { class: "err-list" }, calls.map(function (c) {
        return h("li", null, h("a", { href: testHash(runId, c.ticket_id), text: "#" + c.ticket_id }),
          h("span", { class: "muted", text: " · " + c.agent + " · " + dayLabel(c.date, false) + " · " }),
          h("span", { class: "flag failure", text: c.test_error }));
      })));
  }

  /* One call side by side: the header with both sides' scores and verdicts, then every check with the
     original outcome, the owner's call (only when overruled), the test's outcome and each side's evidence.
     Checks that differ come first, highlighted. */
  function testCallView(detail, test) {
    var mismatch = detail.shown.rubric_version_id !== detail.test.rubric_version_id;
    function side(title, s, error) {
      var facts = [];
      if (!s.counted && !error) {
        facts.push("Not counted" + (s.not_counted_reason ? ": " + s.not_counted_reason : ""));
      }
      facts.push("Resolved on the call: " + (RESOLVED_TEXT[s.resolved_on_call] || "not recorded"));
      facts.push("Dropped call: " + (s.call_dropped ? capitalize(s.call_dropped) : "not recorded"));
      if (mismatch) {
        facts.push("Rubric version " + s.rubric_version_id);
      }
      return h("div", { class: "cmp-side" },
        h("div", { class: "cmp-side-head", text: title + ": " + modelEffortText(s.model_label, s.effort) }),
        h("div", { class: "cmp-side-score" }, "Score ", h("b", { text: num(s.score) }), " ",
          error ? verdictBadge({ review_error: true, error: error }) : verdictBadge({ verdict: s.verdict })),
        error ? h("div", { class: "flag failure", text: "The test model couldn't review this call: " + error }) : null,
        h("div", { class: "note", text: facts.join(" · ") }));
    }
    var header = h("section", { class: "panel detail" },
      backLink(testHash(test.run_id), "← Back to the comparison"),
      h("div", { class: "dethead" }, h("div", null,
        h("div", { class: "big", text: "Ticket #" + detail.ticket_id }),
        h("div", { class: "muted" }, detail.agent + " · " + dayLabel(detail.date, true) + " · ",
          h("a", { href: "#/tickets?ticket=" + encodeURIComponent(detail.ticket_id), text: "See it on the Tickets tab" })),
        mismatch ? h("div", { class: "note" }, h("span", { class: "flag crit", text: "Different rubric version" }),
          " " + MISMATCH_TITLE) : null)),
      detail.purged ? h("div", { class: "note-banner", role: "note",
        text: "This call's content was erased after the retention period. The outcomes are kept." }) : null,
      h("div", { class: "cmp-sides" }, side("Shown results", detail.shown, null),
        side("Model test", detail.test, detail.test.error)));
    function evidence(text) {
      return h("td", { class: "ev" }, text ? h("div", { class: "ev-text", text: text })
        : h("span", { class: "muted", text: "-" }));
    }
    var differing = detail.checks.filter(function (c) { return c.differs; }).length;
    var rows = detail.checks.map(function (c) {
      return h("tr", { class: c.differs ? "flagrow" : null },
        h("td", { class: "cmp-check", text: c.label }),
        h("td", { class: "ic" }, outcomeMark(c.original)),
        h("td", { class: "ic" }, outcomeMark(c.owner)),
        h("td", { class: "ic" }, outcomeMark(c.test)),
        evidence(c.original_evidence),
        evidence(c.test_evidence));
    });
    return [header, panel("Every check", (differing ? plural(differing, "check differs", "checks differ") +
        ", highlighted at the top. " : "Both models gave every check the same outcome. ") +
        "Original is the reviewer's outcome before any Disagree; Your call shows only where you overruled it.",
      h("div", { class: "scroll" }, h("table", { class: "atable cmp" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", text: "Check" }),
          h("th", { scope: "col", class: "ic", text: "Original" }),
          h("th", { scope: "col", class: "ic", text: "Your call" }),
          h("th", { scope: "col", class: "ic", text: "Test" }),
          h("th", { scope: "col", text: "Original evidence" }),
          h("th", { scope: "col", text: "Test evidence" }))),
        h("tbody", null, rows))))];
  }

  // -- team activity ----------------------------------------------------------------------------

  var ACTIVITY_KEYS = ["period", "start", "end", "tech", "region", "team", "view"];
  var ACTIVITY_POLL_MS = 5000;   // while a sync runs, how often the page asks whether it has finished
  var WEEK_VIEW_ABOVE = 42;      // a range of more days than this opens By week
  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTH_LONG_PX = 140;       // a month whose columns span more than this is named in full
  var MONTH_CLEAR_PX = 16;       // a month name keeps this far from the content on its line to the left
  var MONTH_OVERHANG_PX = 40;    // and may reach this far past its last column (the Total column follows)
  var MONTH_FIT = 0.6;           // a name that is wider than its columns by more than this is left out
  var FLAG_KINDS = ["missed", "no_activity", "late_start", "early_finish", "long_gap"];
  var FLAG_LABELS = {
    missed: "Sick day",
    no_activity: "Signed in, no activity",
    late_start: "Late start",
    early_finish: "Early finish",
    long_gap: "Long gap"
  };
  var SCHEDULED_DAYS = ["worked", "zero", "missed"];   // the cell states of a day the tech was due to work
  // ... and of one whose shift is still to be judged (today's, before it ends): the days a shift is drawn on
  var DUE_SHIFT_STATES = SCHEDULED_DAYS.concat(["future", "unsynced"]);
  var DAY_BY_DAY_STATES = ["worked", "zero", "missed", "off", "pto"];   // the days Day by day lists
  var ACTIVITY_NOTE = "Built from Zendesk's own records, read-only: replies, internal notes, logged calls, " +
    "ticket changes and sign-ins. Zendesk doesn't record opening or reading a ticket, calls not logged in " +
    "Zendesk, remote sessions, or work in other tools, so a gap means \"nothing recorded\", not proof of idleness.";

  function teamHash(params) {
    return "#/team" + queryString(params);
  }

  /* A tech over some days, as an address: one day for a cell, the range shown for a name. */
  function techRangeHash(email, first, last) {
    return teamHash({ tech: email, period: "custom", start: first, end: last || first });
  }

  function renderActivity(token, query) {
    loadingLine("Loading team activity…");
    var params = paramsFrom(query, ACTIVITY_KEYS);
    // What the picker shows (techSelected): the tech, else the team, else the region.
    var url = "/api/activity" + queryString({ period: params.period, start: params.start, end: params.end,
      tech: params.tech, region: params.tech || params.team ? null : params.region,
      team: params.tech ? null : params.team });
    readScreen(token, [url], function (got, drawToken) {
      showActivity(drawToken, url, params, got[0]);
    }).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      screen.appendChild(h("div", { class: "nav-note" }, h("a", { href: "#/team", text: "Show this week" })));
    });
  }

  /* The whole screen for `data`, the API's answer at `url`. A sync that is running is polled until it ends. */
  function showActivity(token, url, params, data) {
    clearScreen();
    screen.appendChild(activityFilters(token, url, params, data));
    var days = data.days.length;
    var body;
    if (!days) {
      body = [panel("Team activity", null, h("div", { class: "placeholder", role: "status",
        text: "No days in this period" }))];
    } else if (days === 1) {
      body = params.tech && data.rows.length ? [techDayView(token, data)] : allTechsDayView(data);
    } else if (params.tech && data.rows.length) {
      body = techRangeView(data);
    } else {
      body = allTechsRangeView(data, params);
    }
    body.forEach(function (node) { screen.appendChild(node); });
    screen.appendChild(h("p", { class: "note", text: ACTIVITY_NOTE }));
    screen.querySelectorAll("section.panel").forEach(watchMonthTags);
    if (data.synced.running) {
      pollActivity(token, url, params);
    }
  }

  /* While a sync runs, ask again every few seconds; when it has ended, every kept answer is forgotten (the sync
     changed them) and the screen is drawn from this one, kept in their place. A poll that fires after the route
     changed does nothing. */
  function pollActivity(token, url, params) {
    setTimeout(function () {
      if (token !== renderToken) {
        return;
      }
      api("GET", url).then(function (data) {
        if (token !== renderToken) {
          return;
        }
        if (data.synced.running) {
          pollActivity(token, url, params);
        } else {
          forgetKept();
          keep(url, data);
          showActivity(token, url, params, data);
        }
      }, function () {
        if (token === renderToken) {
          pollActivity(token, url, params);
        }
      });
    }, ACTIVITY_POLL_MS);
  }

  var REGION_LINE = "region:";                       // a Tech picker line's value that picks a region: "region:APAC"
  var TEAM_LINE = "team:";                           // and one that picks a team lead's team: "team:<lead's email>"
  var REGION_INDENT = "\u00a0\u00a0\u00a0";          // a region's line, slightly in under All techs
  var TECH_INDENT = REGION_INDENT + REGION_INDENT;   // and a tech's, further in under their region (or a team's line)
  var TEAM_TECH_INDENT = TECH_INDENT + REGION_INDENT;   // and a tech's under their team, further in again

  /* The Tech picker's lines (Activity and Trends) for `techs`, an answer's picker: every tech the viewer sees, each
     with their region, whatever the answer itself is narrowed to. For a viewer whose techs span more than one region
     they are grouped (Regions design): All techs, then each region in REGIONS' order, slightly indented, followed by
     its techs further in, every line pickable. Otherwise All techs and the techs, as before regions. A manager's are
     grouped by region and then by team (teamLines), even in one region: a team is how they narrow a screen. */
  function techChoices(techs) {
    var all = { value: "", text: "All techs" };
    var held = REGIONS.map(function (r) { return r[0]; }).filter(function (name) {
      return techs.some(function (t) { return t.region === name; });
    });
    if (isManager() && teamBook) {
      return [all].concat(teamLines(techs, held));
    }
    if (held.length < 2) {
      return [all].concat(techs.map(function (t) { return { value: t.email, text: t.name }; }));
    }
    var choices = [all];
    held.forEach(function (name) {
      choices.push({ value: REGION_LINE + name, text: REGION_INDENT + name });
      techs.forEach(function (t) {
        if (t.region === name) {
          choices.push({ value: t.email, text: TECH_INDENT + t.name });
        }
      });
    });
    return choices;
  }

  /* A manager's picker lines under All techs (Managers design): each region they see, then each team lead's team with
     a tech listed in it, by the lead's name ("Jamie Rivera's team"), then that team's techs there, further in. A tech
     on two teams is under each; a tech on no team follows their region's teams as "Name (no team lead)". */
  function teamLines(techs, held) {
    var leads = teamLeads();
    var lines = [];
    held.forEach(function (region) {
      lines.push({ value: REGION_LINE + region, text: REGION_INDENT + region });
      var here = techs.filter(function (t) { return t.region === region; });
      leads.forEach(function (lead) {
        var theirs = here.filter(function (t) { return lead.techs.indexOf(t.email) >= 0; });
        if (theirs.length) {
          lines.push({ value: TEAM_LINE + lead.email, text: TECH_INDENT + teamName(lead) });
          theirs.forEach(function (t) { lines.push({ value: t.email, text: TEAM_TECH_INDENT + t.name }); });
        }
      });
      here.filter(function (t) { return leadIndex(leads, t.email) < 0; }).forEach(function (t) {
        lines.push({ value: t.email, text: TECH_INDENT + t.name + " (no team lead)" });
      });
    });
    return lines;
  }

  /* A manager's team leads (teamBook), by name: the order their teams are listed and shown in. */
  function teamLeads() {
    return teamBook.people.slice().sort(function (a, b) { return byName(a.name || a.email, b.name || b.email); });
  }

  /* "Jamie Rivera's team": a lead's team as the picker and the Teams panel name it. */
  function teamName(lead) {
    return (lead.name || lead.email) + "'s team";
  }

  /* Where the first of `leads` with `email` on their team is in them; -1 for a tech on no team. */
  function leadIndex(leads, email) {
    for (var i = 0; i < leads.length; i++) {
      if (leads[i].techs.indexOf(email) >= 0) {
        return i;
      }
    }
    return -1;
  }

  /* The Tech picker's line for the address's `params`: the tech when it names one, else the team, else the region,
     else All techs. */
  function techSelected(params) {
    return params.tech || (params.team ? TEAM_LINE + params.team.trim().toLowerCase()
      : params.region ? REGION_LINE + params.region.toUpperCase() : "");
  }

  /* The address's changes for a picked line: a region writes `region`, a team `team` and a tech `tech`, each dropping
     the other two, and All techs drops all three. */
  function techPicked(value) {
    var region = value.indexOf(REGION_LINE) === 0 ? value.slice(REGION_LINE.length) : null;
    var team = value.indexOf(TEAM_LINE) === 0 ? value.slice(TEAM_LINE.length) : null;
    return { tech: region || team ? null : value || null, region: region, team: team };
  }

  /* The Tech and Period filters, then Refresh with when the data was last read. A change of period drops the
     By day / By week choice (the new range has its own default); This week (so far), the default, stays out of the
     address. */
  function activityFilters(token, url, params, data) {
    var message = h("div", { class: "message", role: "status" });
    function go(changes) {
      var next = Object.assign({}, params, changes);
      if ("period" in changes) {
        next.view = null;
        if (next.period === "this_week") {
          next.period = null;
        }
      }
      location.hash = teamHash(next);
    }
    var techs = techChoices(data.picker);
    if (params.tech && !data.picker.some(function (t) { return t.email === params.tech; })) {
      var row = data.rows.filter(function (r) { return r.email === params.tech; })[0];
      techs.push({ value: params.tech, text: row ? row.name : params.tech });
    }
    var sync = syncControls(token, url, params, data);
    return h("div", { class: "filters", role: "group", "aria-label": "Filters" },
      filterSelect("ta-tech", "Tech", techs, techSelected(params), function (value) { go(techPicked(value)); }),
      periodControl("ta", data.period, params, message, go),
      syncOn && !hostedOnly() ? sync.button : null,   // no Refresh: the PC with its sync off, and the hosted copy
      sync.message, message);
  }

  /* " · next update 1:00 PM" (with the weekday when it isn't today), from the hosted copy's next scheduled sync;
     nothing without one (the PC, which has Refresh). */
  function nextText(at) {
    if (!at) {
      return "";
    }
    var when = new Date(at);
    var clock = when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return " · next update " + (when.toDateString() === new Date().toDateString() ? clock
      : when.toLocaleDateString(undefined, { weekday: "short" }) + " " + clock);
  }

  /* "Updated 9:42 AM" (with the day when it wasn't today), from the instant the last sync was current to. */
  function updatedText(at) {
    if (!at) {
      return "Not updated yet";
    }
    var when = new Date(at);
    if (when.toDateString() !== new Date().toDateString()) {
      return "Updated " + timeLabel(at);
    }
    return "Updated " + when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  /* The Refresh button and its message. Refresh starts a sync and waits for it; a sync that is already running
     (someone else's, or an earlier click) is waited for the same way. */
  function syncControls(token, url, params, data) {
    var running = data.synced.running;
    var message = h("span", { class: "message", role: "status",
      text: running ? "Updating…" : updatedText(data.synced.at) + nextText(data.synced.next) });
    var button = h("button", { type: "button", class: "small", id: "ta-refresh", text: "Refresh", disabled: running });
    button.addEventListener("click", function () {
      button.disabled = true;
      setMessage(message, "Updating…");
      api("POST", "/api/activity/sync", { kind: "refresh" }).then(function () {
        pollActivity(token, url, params);
      }).catch(function (error) {
        if (error.status === 409) {
          pollActivity(token, url, params);
          return;
        }
        button.disabled = false;
        setMessage(message, error.message, "error");
      });
    });
    return { button: button, message: message };
  }

  // The figures --------------------------------------------------------------------------------------

  /* Days to come, days off the team, and days no sync has reached hold nothing: totals, tiles and flags count
     only the days that were judged. */
  function isCounted(c) {
    return c.state !== "future" && c.state !== "untracked" && c.state !== "unsynced";
  }

  function allCells(rows) {
    return rows.reduce(function (list, r) { return list.concat(r.cells); }, []);
  }

  /* The sums a view shows, over the days that have happened. "Worked" is a scheduled day with activity. */
  function tally(cells) {
    var t = { tickets: 0, replies: 0, notes: 0, calls: 0, solved: 0, flags: 0, missed: 0, scheduled: 0, worked: 0,
      workedTickets: 0, workedReplies: 0, workedNotes: 0 };
    cells.filter(isCounted).forEach(function (c) {
      t.tickets += c.tickets;
      t.replies += c.replies;
      t.notes += c.notes;
      t.calls += c.calls;
      t.solved += c.solved;
      t.flags += c.flags.length;
      c.flags.forEach(function (f) {
        if (f.kind === "missed") {
          t.missed += 1;
        }
      });
      if (SCHEDULED_DAYS.indexOf(c.state) >= 0) {
        t.scheduled += 1;
      }
      if (c.state === "worked") {
        t.worked += 1;
        t.workedTickets += c.tickets;
        t.workedReplies += c.replies;
        t.workedNotes += c.notes;
      }
    });
    return t;
  }

  function perShift(total, shifts) {
    return shifts ? num(total / shifts) : "-";
  }

  function missedShiftsLine(t) {
    return t.missed ? h("div", { class: "d" }, h("span", { class: "warn", text: plural(t.missed, "sick day", "sick days") }))
      : h("div", { class: "d", text: "No sick days" });
  }

  function activityTile(label, value, detail, lead) {
    return tile(label, value, typeof detail === "string" ? h("div", { class: "d", text: detail }) : detail, null, lead);
  }

  function shiftsWorked(t) {
    return [String(t.worked), h("span", { class: "of", text: " of " + t.scheduled })];
  }

  // Clock times and day names ------------------------------------------------------------------------

  /* "08:04" as "8:04 AM". */
  function hhmmLabel(hhmm) {
    var parts = hhmm.split(":").map(Number);
    return (parts[0] % 12 || 12) + ":" + String(parts[1]).padStart(2, "0") + (parts[0] >= 12 ? " PM" : " AM");
  }

  /* The same on the hour without its minutes: "8 AM". */
  function hhmmShort(hhmm) {
    return hhmmLabel(hhmm).replace(":00 ", " ");
  }

  function shiftText(c) {
    if (c.state === "pto") {
      return "Time off";
    }
    if (c.state === "off" || c.state === "rest") {
      return "Rest day";
    }
    if (!c.shift) {
      return "-";
    }
    return hhmmShort(c.shift.start) + " – " + hhmmShort(c.shift.end);
  }

  function dayWithWeekday(iso) {
    return WEEKDAYS[localDate(iso).getDay()] + " " + dayLabel(iso, false);
  }

  function monthName(index, full) {
    return new Date(2000, index, 1).toLocaleDateString(undefined, { month: full ? "long" : "short" });
  }

  /* Each week of `days` as the positions of its days: weeks run Sunday to Saturday, the first and last may be partial. */
  function weeksOf(days) {
    var weeks = [];
    days.forEach(function (day, i) {
      if (!weeks.length || localDate(day).getDay() === 0) {
        weeks.push([]);
      }
      weeks[weeks.length - 1].push(i);
    });
    return weeks;
  }

  // Flags --------------------------------------------------------------------------------------------

  function flagText(f) {
    return FLAG_LABELS[f.kind] + (f.minutes != null ? " · " + formatMinutes(f.minutes) : "");
  }

  function flagChip(f) {
    return h("span", { class: "chip " + (f.kind === "missed" ? "fail" : "bad"), text: flagText(f) });
  }

  // The cells ----------------------------------------------------------------------------------------

  /* One tech's day as a cell. `dense`: a grid of more than 14 days writes a sick day as a cross. Rest days,
     time off, days to come and days off the team are not links; every other day opens that tech's day. */
  function heatCell(c, email, dense) {
    if (c.state === "future") {
      return h("span", { class: "hc future", title: "Still to come" });
    }
    if (c.state === "unsynced") {
      return h("span", { class: "hc future", title: "Not synced yet" });
    }
    if (c.state === "untracked") {
      return h("span", { class: "hc rest", title: "Not on the team", text: "-" });
    }
    if (c.state === "pto") {
      return h("span", { class: "hc pto", title: "Time off", text: "PTO" });
    }
    if (c.state === "rest") {
      return h("span", { class: "hc rest", text: "Rest" });
    }
    var link = techRangeHash(email, c.day);
    if (c.state === "missed") {
      return h("a", { class: "hc miss", href: link, title: "Sick day", text: dense ? "✕" : "Sick" });
    }
    if (c.state === "zero") {
      return h("a", { class: "hc zero", href: link, title: "Signed in, no activity", text: "0" });
    }
    if (c.state === "off") {
      return h("a", { class: "hc off", href: link, title: "Worked a rest day", text: String(c.tickets) });
    }
    var shade = c.tickets >= 12 ? "h4" : c.tickets >= 8 ? "h3" : c.tickets >= 4 ? "h2" : "h1";
    var title = plural(c.tickets, "ticket", "tickets") + c.flags.map(function (f) { return " · " + flagText(f); }).join("");
    return h("a", { class: "hc " + shade, href: link, title: title }, String(c.tickets),
      c.flags.length ? h("span", { class: "fdot" }) : null);
  }

  /* A week of one tech's days as a cell: its tickets, with the shifts worked under them. The shade is tickets
     per shift worked; the dot is a flag, or a sick day in the failure color. It opens that tech's week. */
  function weekCell(row, positions, days) {
    var cells = positions.map(function (i) { return row.cells[i]; });
    if (!cells.some(isCounted)) {
      if (cells.some(function (c) { return c.state === "future"; })) {
        return h("span", { class: "hc future", title: "Still to come" });
      }
      return cells.some(function (c) { return c.state === "unsynced"; })
        ? h("span", { class: "hc future", title: "Not synced yet" })
        : h("span", { class: "hc rest", title: "Not on the team", text: "-" });
    }
    var t = tally(cells);
    if (!t.scheduled && !t.tickets) {
      return h("span", { class: "hc rest", text: "-" });
    }
    var per = t.worked ? t.workedTickets / t.worked : 0;
    var shade = !t.worked ? "zero" : per >= 10 ? "h4" : per >= 7 ? "h3" : per >= 4 ? "h2" : "h1";
    var title = plural(t.tickets, "ticket", "tickets") + " · " + t.worked + " of " + plural(t.scheduled, "shift", "shifts") +
      " worked · " + num(per) + " per shift · " + plural(t.flags, "flag", "flags");
    var dot = t.missed ? h("span", { class: "fdot fail" }) : t.flags ? h("span", { class: "fdot" }) : null;
    return h("a", { class: "hc " + shade, href: techRangeHash(row.email, days[positions[0]], days[positions[positions.length - 1]]),
      title: title }, String(t.tickets), h("span", { class: "shifts", text: t.worked + "/" + t.scheduled + " shifts" }), dot);
  }

  /* The two cells that end a grid row: tickets touched on the days that have happened, and the flags there.
     `split`: cells of the three-part grid (splitGrid) rather than of a table. */
  function totalCells(cells, split) {
    var t = tally(cells);
    var tag = split ? "div" : "td";
    var role = split ? "cell" : null;
    return [h(tag, { class: "num tot", role: role, text: String(t.tickets) }),
      h(tag, { class: "num flg", role: role }, t.flags ? h("span", { class: "chip bad", text: String(t.flags) })
        : h("span", { class: "muted", text: "0" }))];
  }

  function totalHeads(split) {
    var tag = split ? "div" : "th";
    var role = split ? "columnheader" : null;
    return [h(tag, { class: "tot", role: role, text: "Total" }), h(tag, { class: "flg", role: role, text: "Flags" })];
  }

  function nameCell(row, days) {
    return h("div", { class: "who", role: "rowheader" },
      h("a", { href: techRangeHash(row.email, days[0], days[days.length - 1]), title: row.name, text: row.name }));
  }

  // The grids ----------------------------------------------------------------------------------------

  var heatGridCount = 0;   // numbers each three-part grid, for the ids that tie its rows together

  /* The grid for `data` (the API's answer): one row per tech, a cell per day (mode "day") or per week
     (mode "week", for ranges of more than a week). */
  function heatGrid(data, mode) {
    return mode === "week" && data.days.length > 7 ? weekGrid(data) : dayGrid(data);
  }

  /* The all-techs grid in three parts side by side: the tech names (.heat-names), the days or weeks (.heat-days)
     and Total and Flags (.heat-ends). Only the middle part scrolls sideways, so no cell ever passes under another
     (owner, 2026-10-21, T-126). Every part's rows take the same fixed heights from app.css, whatever is in them,
     so a row lines up across the three. A screen reader meets one table: each row in the names part owns that
     row's cells in the other two parts (aria-owns). `lines` are the head line then a line per tech, each
     { who, cells, ends }; `count` is how many day (or week) columns there are. */
  function splitGrid(cls, label, count, lines) {
    heatGridCount += 1;
    var names = [];
    var middle = [];
    var ends = [];
    lines.forEach(function (line, i) {
      var id = "heat-" + heatGridCount + "-" + i;
      var kind = "heat-row" + (i === 0 ? " heat-head" : "");
      names.push(h("div", { class: kind, role: "row", "aria-owns": id + "-days " + id + "-ends" }, line.who));
      middle.push(h("div", { class: kind, id: id + "-days", role: "none" }, line.cells));
      ends.push(h("div", { class: kind, id: id + "-ends", role: "none" }, line.ends));
    });
    return h("div", { class: "heat split" + cls, role: "table", "aria-label": label, style: "--days: " + count },
      h("div", { class: "heat-names", role: "none" }, names),
      h("div", { class: "heat-days", role: "none" }, middle),
      h("div", { class: "heat-ends", role: "none" }, ends));
  }

  /* A cell per day. Up to 14 days the day name and date head each column and "Rest", "PTO" and "Sick" are
     written; from 15 days rest days are dimmed and a sick day is a cross (mid), and past 31 days the head is the
     date alone (dates). Columns never shrink below --heat-day-min-mid: a range longer than the panel can show
     scrolls sideways inside it, with the month names following (the owner's call, 2026-10-21, replacing the
     compact and colour-only grids of spec 6.3). Every day column is the same width. Each column head names its
     month for placeMonthTags. */
  function dayGrid(data) {
    var days = data.days;
    var n = days.length;
    var heads = days.map(function (day) {
      var d = localDate(day);
      var content = n > 31 ? String(d.getDate()) : [WEEKDAYS[d.getDay()], h("br"), String(d.getDate())];
      return h("div", { class: n > 31 ? "dnum" : null, role: "columnheader", "data-mo": d.getMonth() }, content);
    });
    var lines = [{ who: h("div", { class: "who", role: "columnheader" }), cells: heads, ends: totalHeads(true) }];
    data.rows.forEach(function (row) {
      lines.push({ who: nameCell(row, days), ends: totalCells(row.cells, true),
        cells: row.cells.map(function (c) { return h("div", { role: "cell" }, heatCell(c, row.email, n > 14)); }) });
    });
    return splitGrid((n > 14 ? " mid" : "") + (n > 31 ? " dates" : ""), "Tickets touched per day", n, lines);
  }

  /* A cell per week, headed by the week's first day. */
  function weekGrid(data) {
    var weeks = weeksOf(data.days);
    var heads = weeks.map(function (positions) {
      return h("div", { role: "columnheader", text: dayLabel(data.days[positions[0]], false) });
    });
    var lines = [{ who: h("div", { class: "who", role: "columnheader", text: "Week of" }), cells: heads,
      ends: totalHeads(true) }];
    data.rows.forEach(function (row) {
      lines.push({ who: nameCell(row, data.days), ends: totalCells(row.cells, true),
        cells: weeks.map(function (positions) { return h("div", { role: "cell" }, weekCell(row, positions, data.days)); }) });
    });
    return splitGrid(" weeks", "Tickets touched per week", weeks.length, lines);
  }

  /* One tech's days as a calendar: a row per week, Sunday to Saturday, with the week's total and flags. A
     first or last week that is partial is padded with empty slots. */
  function techCalendar(row, days) {
    var dense = days.length > 14;
    var head = h("tr", null, h("th", { class: "who" }), WEEKDAYS.map(function (d) { return h("th", { text: d }); }), totalHeads());
    var body = weeksOf(days).map(function (positions) {
      var cells = positions.map(function (i) { return row.cells[i]; });
      var lead = localDate(days[positions[0]]).getDay();
      var tail = 6 - localDate(days[positions[positions.length - 1]]).getDay();
      var tds = [h("td", { class: "wklabel", text: weekLabel(shiftDay(days[positions[0]], -lead)) })];
      var i;
      for (i = 0; i < lead; i++) {
        tds.push(h("td"));
      }
      cells.forEach(function (c) { tds.push(h("td", null, datedCell(heatCell(c, row.email, dense), c.day))); });
      for (i = 0; i < tail; i++) {
        tds.push(h("td"));
      }
      return h("tr", null, tds, totalCells(cells));
    });
    return h("table", { class: "heat cal" }, h("thead", null, head), h("tbody", null, body));
  }

  /* One tech's calendar cell with its date in the corner: the day of the month, and the month too on the 1st, so a
     long period reads without counting along the row. */
  function datedCell(cell, iso) {
    var d = localDate(iso);
    return cornerDate(cell, d.getDate() === 1 ? SHORT_MONTHS[d.getMonth()] + " 1" : String(d.getDate()));
  }

  /* `cell` with `text` in its top-left corner (.cdate): a calendar day's date, or a stacked week's first day. */
  function cornerDate(cell, text) {
    cell.appendChild(h("span", { class: "cdate", "aria-hidden": "true", text: text }));
    return cell;
  }

  // The grids stacked for a narrow screen ---------------------------------------------------------------

  /* The all-techs grid stacked (iPad and phone design §7): a block per tech, its name and "Total N · Flags N" over
     its cells across the full width. Up to a week, one head line of the days and 7 equal columns (a shorter period
     keeps the 7-column width, so cells keep one size); By day past a week, a Sun–Sat calendar per tech under one
     Sun–Sat head, each cell dated; By week, each tech's weeks four to a row, each dated by its first day. The
     cells are the three-part grid's own (heatCell, weekCell). gridPanel puts it beside that grid and app.css shows
     one or the other by the width. A screen reader meets one table with a row per tech. */
  function stackedGrid(data, mode) {
    var days = data.days;
    var n = days.length;
    if (mode === "week" && n > 7) {
      var weeks = weeksOf(days);
      return heatStack(" weeks dated", "Tickets touched per week", 4, null, data.rows.map(function (row) {
        return stackBlock(nameCell(row, days), row.cells, weeks.map(function (positions) {
          return cornerDate(weekCell(row, positions, days), dayLabel(days[positions[0]], false));
        }));
      }));
    }
    if (n > 7) {
      return heatStack(" dated" + (n > 14 ? " mid" : ""), "Tickets touched per day", 7, weekdayHeads(),
        data.rows.map(function (row) {
          return stackBlock(nameCell(row, days), row.cells, calendarSlots(days[0], days[n - 1],
            row.cells.map(function (c) { return datedCell(heatCell(c, row.email, n > 14), c.day); })));
        }));
    }
    var heads = days.map(function (day) {
      var d = localDate(day);
      return h("div", { role: "columnheader" }, WEEKDAYS[d.getDay()], h("br"), String(d.getDate()));
    });
    return heatStack("", "Tickets touched per day", 7, heads, data.rows.map(function (row) {
      return stackBlock(nameCell(row, days), row.cells,
        row.cells.map(function (c) { return heatCell(c, row.email, false); }));
    }));
  }

  /* One tech's calendar stacked (iPad and phone design §8): under one Sun–Sat head, a line per week, "Week of …"
     and its "Total N · Flags N", over its 7 days, a partial week padded with blank days. The cells are the calendar
     table's own (techCalendar); techRangeView puts it beside that table and app.css shows one by the width. */
  function stackedCalendar(row, days) {
    var dense = days.length > 14;
    var blocks = weeksOf(days).map(function (positions) {
      var cells = positions.map(function (i) { return row.cells[i]; });
      var first = days[positions[0]];
      var label = h("div", { class: "wklabel", role: "rowheader",
        text: weekLabel(shiftDay(first, -localDate(first).getDay())) });
      return stackBlock(label, cells, calendarSlots(first, days[positions[positions.length - 1]],
        cells.map(function (c) { return datedCell(heatCell(c, row.email, dense), c.day); })));
    });
    return heatStack(" cal dated" + (dense ? " mid" : ""), "Tickets touched per day", 7, weekdayHeads(), blocks);
  }

  /* A stacked grid: `cols` equal columns (--cols, for app.css), an optional head line, then the blocks. */
  function heatStack(cls, label, cols, heads, blocks) {
    return h("div", { class: "heat-stack" + cls, role: "table", "aria-label": label, style: "--cols: " + cols },
      heads ? h("div", { class: "hs-head", role: "row" }, heads) : null, blocks);
  }

  function weekdayHeads() {
    return WEEKDAYS.map(function (d) { return h("div", { role: "columnheader", text: d }); });
  }

  /* One block of a stacked grid, a row to a screen reader: its heading (`who`) and the sum of `cells` on a line,
     over `slots`, each a cell or null for a blank day. */
  function stackBlock(who, cells, slots) {
    var t = tally(cells);
    var sum = h("div", { class: "hs-sum", role: "cell" }, h("span", { class: "muted", text: "Total " }),
      h("b", { text: String(t.tickets) }), h("span", { class: "muted", text: " · Flags " }),
      h("b", { class: t.flags ? "warn" : null, text: String(t.flags) }));
    return h("div", { class: "hs-block", role: "row" },
      h("div", { class: "hs-line", role: "none" }, who, sum),
      h("div", { class: "hs-cells", role: "none" },
        slots.map(function (slot) { return h("div", { role: "cell" }, slot); })));
  }

  /* `cells`, from the day `first` to the day `last`, laid out Sunday to Saturday: a blank slot for each day of the
     first week before `first` and of the last week after `last`. */
  function calendarSlots(first, last, cells) {
    var lead = localDate(first).getDay();
    var tail = 6 - localDate(last).getDay();
    return new Array(lead).fill(null).concat(cells, new Array(tail).fill(null));
  }

  function legendItem(swatch, text) {
    return h("span", null, swatch, text);
  }

  function swatch(kind, text, dot) {
    return h("span", { class: "hc " + kind }, text, dot ? h("span", { class: "fdot" + (dot === "fail" ? " fail" : "") }) : null);
  }

  /* What each kind of cell means. "Still to come" and "Not synced yet" only when the grid has such a day. */
  function dayLegend(cells) {
    function has(state) {
      return cells.some(function (c) { return c.state === state; });
    }
    return h("div", { class: "heat-legend" },
      legendItem(swatch("h2", "6"), "Tickets touched"),
      legendItem(swatch("h2", "6", true), "With a flag"),
      legendItem(swatch("zero", "0"), "Signed in, did nothing"),
      legendItem(swatch("miss", "✕"), "Sick day"),
      legendItem(swatch("off", "3"), "Worked a rest day"),
      legendItem(swatch("rest"), "Rest day"),
      legendItem(swatch("pto"), "Time off"),
      has("future") ? legendItem(swatch("future"), "Still to come") : null,
      has("unsynced") ? legendItem(swatch("future"), "Not synced yet") : null);
  }

  function weekLegend() {
    return h("div", { class: "heat-legend" },
      legendItem(swatch("h2", "38"), "Tickets that week · shade = tickets per shift worked"),
      legendItem(swatch("h2", "38", true), "Has flags"),
      legendItem(swatch("h2", "38", "fail"), "Has a sick day"),
      h("span", { text: "Click a week to open it" }));
  }

  // Month names over their columns --------------------------------------------------------------------

  /* Lay each month's name over its day columns, centered, on `bar`'s `.months` layer (an absolutely placed
     layer on a line the panel already has, so no row is added). The name is the full month when its columns
     span more than MONTH_LONG_PX, kept MONTH_CLEAR_PX clear of what is already on the bar's left, and left
     out when its columns are too narrow to hold it. `grid` is the grid whose column heads carry data-mo. */
  function placeMonthTags(bar, grid) {
    var layer = bar.querySelector(".months");
    if (!layer) {
      return;
    }
    layer.textContent = "";
    var groups = [];
    // A grid wider than its panel scrolls its days part (.heat-days) sideways, between the tech names and the
    // Total column: a month is placed over the part of its columns in view there.
    var scroller = grid.querySelector(".heat-days");
    var view = null;
    if (scroller && scroller.scrollWidth > scroller.clientWidth) {
      var frame = scroller.getBoundingClientRect();
      view = { left: frame.left, right: frame.right };
    }
    grid.querySelectorAll("[data-mo]").forEach(function (th) {
      var rect = th.getBoundingClientRect();
      if (view && (rect.right <= view.left || rect.left >= view.right)) {
        return;
      }
      if (view) {
        rect = { left: Math.max(rect.left, view.left), right: Math.min(rect.right, view.right) };
      }
      var month = th.getAttribute("data-mo");
      var last = groups[groups.length - 1];
      if (last && last.month === month) {
        last.left = Math.min(last.left, rect.left);
        last.right = Math.max(last.right, rect.right);
      } else {
        groups.push({ month: month, left: rect.left, right: rect.right });
      }
    });
    var box = bar.getBoundingClientRect();
    var edge = box.left;
    Array.prototype.slice.call(bar.children).forEach(function (child) {
      if (child !== layer) {
        edge = Math.max(edge, child.getBoundingClientRect().right);
      }
    });
    groups.forEach(function (g) {
      var tag = h("span", { class: "month-tag", text: monthName(Number(g.month), g.right - g.left > MONTH_LONG_PX) });
      layer.appendChild(tag);
      var width = tag.getBoundingClientRect().width;
      var x = (g.left + g.right) / 2 - box.left;
      x = Math.max(x, edge - box.left + MONTH_CLEAR_PX + width / 2);
      x = Math.min(x, g.right - box.left - width / 2 + MONTH_OVERHANG_PX);
      tag.style.left = x + "px";
      // Too narrow to hold its name, or pushed back over the content on the line's left: left out.
      if (g.right - g.left < width * MONTH_FIT || x - width / 2 < edge - box.left + MONTH_CLEAR_PX) {
        tag.remove();
      }
    });
  }

  /* Place the month names of the grid in `box` (a panel) now, and again whenever its width changes. The
     names are absolutely placed, so placing them never resizes the panel being watched (see fitRowMode). */
  function watchMonthTags(box) {
    var bar = box.querySelector(".has-months");
    var grid = box.querySelector(".heat.split");
    if (!bar || !grid) {
      return;
    }
    placeMonthTags(bar, grid);
    if (typeof ResizeObserver !== "function") {
      return;
    }
    var lastWidth = box.clientWidth;
    var observer = new ResizeObserver(function () {
      if (!box.isConnected) {
        observer.disconnect();   // the screen was redrawn without this panel
        return;
      }
      if (box.clientWidth !== lastWidth) {
        lastWidth = box.clientWidth;
        placeMonthTags(bar, grid);
      }
    });
    observer.observe(box);
    var scroller = grid.querySelector(".heat-days");
    if (scroller) {
      scroller.addEventListener("scroll", function () {
        if (box.isConnected) {
          placeMonthTags(bar, grid);
        }
      });
    }
  }

  // All techs over a range ---------------------------------------------------------------------------

  /* Ranges of more days than a week open By week when they are longer than 6 weeks; the address's `view` wins. */
  function gridMode(params, days) {
    if (days <= 7) {
      return "day";
    }
    if (params.view === "day" || params.view === "week") {
      return params.view;
    }
    return days > WEEK_VIEW_ABOVE ? "week" : "day";
  }

  /* The rank of a region in REGIONS' order (a region not among them after them). */
  function regionRank(region) {
    var at = REGIONS.map(function (r) { return r[0]; }).indexOf(region);
    return at < 0 ? REGIONS.length : at;
  }

  /* Each tech's current region, from an answer's techs. */
  function regionsOf(data) {
    var of = {};
    data.techs.forEach(function (t) { of[t.email] = t.region; });
    return of;
  }

  /* A manager's rows in team order (Managers design): by region, then team lead (a tech on two teams with the first
     of their leads by name, one on no team after the region's teams), then name. Anyone else's stay in the answer's
     order, by name. */
  function inTeamOrder(data) {
    if (!isManager() || !teamBook) {
      return data;
    }
    var regionOf = regionsOf(data);
    var leads = teamLeads();
    function lead(email) {
      var at = leadIndex(leads, email);
      return at < 0 ? leads.length : at;
    }
    var rows = data.rows.slice().sort(function (a, b) {
      return regionRank(regionOf[a.email]) - regionRank(regionOf[b.email]) || lead(a.email) - lead(b.email) ||
        byName(a.name, b.name);
    });
    return Object.assign({}, data, { rows: rows });
  }

  /* A manager's teams side by side (Managers design): a row per team lead with any of the techs shown (Team lead,
     Region, Techs, Tickets touched, Per shift worked, Shifts worked, Sick days, Flags), by region (their techs' first),
     then lead. A tech on two teams counts in both rows, so there is no total row. Clicking a row, or the lead's name,
     shows only that team, keeping the period and the view. Null for anyone else, and with one team shown: one picked,
     or only one with any tech here. */
  function teamsPanel(data, params) {
    if (!isManager() || !teamBook || params.team) {
      return null;
    }
    var regionOf = regionsOf(data);
    var lines = teamLeads().map(function (lead) {
      var rows = data.rows.filter(function (r) { return lead.techs.indexOf(r.email) >= 0; });
      var held = REGIONS.map(function (r) { return r[0]; }).filter(function (region) {
        return rows.some(function (r) { return regionOf[r.email] === region; });
      });
      return { lead: lead, rows: rows, held: held, t: tally(allCells(rows)) };
    }).filter(function (line) { return line.rows.length; });
    if (lines.length < 2) {
      return null;
    }
    lines.sort(function (a, b) { return regionRank(a.held[0]) - regionRank(b.held[0]); });   // stable: leads by name
    function count(n, kind) {
      return n ? h("span", { class: "chip " + kind, text: String(n) }) : h("span", { class: "muted", text: "0" });
    }
    var head = h("tr", null, h("th", { scope: "col", text: "Team lead" }), h("th", { scope: "col", text: "Region" }),
      ["Techs", "Tickets touched", "Per shift worked", "Shifts worked", "Sick days", "Flags"].map(function (text) {
        return h("th", { scope: "col", class: "num", text: text });
      }));
    var body = lines.map(function (line) {
      var t = line.t;
      var link = teamHash(Object.assign({}, params, { team: line.lead.email, tech: null, region: null }));
      var tr = h("tr", { class: "rowlink" },
        h("td", null, h("a", { href: link }, h("b", { text: line.lead.name || line.lead.email }))),
        h("td", { text: line.held.join(", ") }),
        h("td", { class: "num", text: String(line.rows.length) }),
        h("td", { class: "num", text: String(t.tickets) }),
        h("td", { class: "num", text: perShift(t.workedTickets, t.worked) }),
        h("td", { class: "num", text: t.worked + " of " + t.scheduled }),
        h("td", { class: "num" }, count(t.missed, "fail")),
        h("td", { class: "num" }, count(t.flags, "bad")));
      tr.addEventListener("click", function (event) {
        if (!event.target.closest("a")) {
          location.hash = link;
        }
      });
      return tr;
    });
    return panel("Teams · " + lines.length, "Each team lead's techs in the regions you see, side by side · " +
      "click a team to show only it",
      tableBox(h("table", { class: "atable" }, h("thead", null, head), h("tbody", null, body))));
  }

  function allTechsRangeView(data, params) {
    data = inTeamOrder(data);
    var days = data.days.length;
    var t = tally(allCells(data.rows));
    var nodes = [
      h("h3", { class: "period-head", text: data.period.label + " · " + plural(data.rows.length, "tech", "techs") +
        " · " + plural(t.scheduled, "scheduled shift", "scheduled shifts") }),
      h("div", { class: "panel-row cols-5-lead" },
        activityTile("Tickets touched", String(t.tickets), perShift(t.workedTickets, t.worked) + " per shift worked", true),
        activityTile("Shifts worked", shiftsWorked(t), (t.scheduled - t.worked) + " with no activity"),
        activityTile("Replies", String(t.replies), plural(t.notes, "internal note", "internal notes")),
        activityTile("Solved", String(t.solved), plural(t.calls, "call logged", "calls logged")),
        activityTile("Flags", String(t.flags), missedShiftsLine(t))),
      teamsPanel(data, params),
      gridPanel(data, params, gridMode(params, days))
    ].filter(Boolean);
    if (days > 7) {
      var byTech = flagsByTech(data.rows);
      if (byTech) {
        nodes.push(byTech);
      }
    }
    nodes.push(everyFlag(data.rows, days));
    return nodes;
  }

  /* By day / By week, written into the address as `view`. When the grid is by day the month names sit on this
     line too (in .months). */
  function gridModePills(params, mode) {
    function pill(value, text) {
      return h("button", { type: "button", class: "pill" + (mode === value ? " on" : ""), text: text,
        onclick: function () { location.hash = teamHash(Object.assign({}, params, { view: value })); } });
    }
    var byDay = mode === "day";
    return h("div", { class: "pills grid-mode" + (byDay ? " has-months" : "") }, pill("day", "By day"), pill("week", "By week"),
      byDay ? h("span", { class: "months" }) : null);
  }

  /* Techs with no row and no shift on record at all: no shift is set for them yet, so there are no days to show. A
     manager sets no shifts (each team lead does), so their line says who hasn't, with no link to Settings. */
  function missingShifts(data) {
    var shown = {};
    data.rows.forEach(function (r) { shown[r.email] = true; });
    var names = data.techs.filter(function (t) { return !t.has_shift && !shown[t.email]; })
      .map(function (t) { return t.name; });
    if (!names.length) {
      return null;
    }
    if (isManager()) {
      return h("div", { class: "placeholder", role: "status", text: names.length === 1
        ? names[0] + "'s team lead hasn't set their shift yet."
        : "The team leads of " + andList(names) + " haven't set their shifts yet." });
    }
    var settings = h("a", { href: "#/settings", text: "Settings" });
    if (names.length === 1) {
      return h("div", { class: "placeholder", role: "status" }, "Set " + names[0] + "'s shift in ", settings, " to see their days.");
    }
    return h("div", { class: "placeholder", role: "status" }, "Set shifts in ", settings, " for " +
      names.slice(0, -1).join(", ") + " and " + names[names.length - 1] + " to see their days.");
  }

  /* "Tickets touched per day": the grid (and its stacked form for a narrow screen), its legend, and above them the
     line the month names go on. */
  function gridPanel(data, params, mode) {
    var days = data.days.length;
    var weekly = mode === "week" && days > 7;
    var text = "Each tech's shifts from Settings · click a cell to open it, a name for that tech";
    var line = days > 7 ? h("div", { class: "sub", text: text })
      : h("div", { class: "sub has-months" }, h("span", { text: text }), h("span", { class: "months" }));
    var grid = null;
    var legend = null;
    if (data.rows.length) {
      grid = h("div", { class: "scroll" }, heatGrid(data, mode), stackedGrid(data, mode));
      legend = weekly ? weekLegend() : dayLegend(allCells(data.rows));
    }
    var nobody = data.rows.length || data.techs.length ? null : h("div", { class: "placeholder", role: "status" },
      "No techs are on the team yet. Add them in ", h("a", { href: "#/settings", text: "Settings" }), ".");
    return panel("Tickets touched per " + (weekly ? "week" : "day"), null, line, missingShifts(data), nobody,
      days > 7 ? gridModePills(params, mode) : null, grid, legend);
  }

  // Flags --------------------------------------------------------------------------------------------

  /* "Flags by tech": a row per tech with flags, a column per kind of flag, and totals. Null when nothing is flagged.
     As cards (`flag-counts`, app.css) a tech's kinds at 0 (`zero`) are left out and the others read as chips. */
  function flagsByTech(rows) {
    var lines = rows.map(function (row) {
      var counts = {};
      var total = 0;
      FLAG_KINDS.forEach(function (kind) { counts[kind] = 0; });
      row.cells.filter(isCounted).forEach(function (c) {
        c.flags.forEach(function (f) {
          counts[f.kind] += 1;
          total += 1;
        });
      });
      return { name: row.name, counts: counts, total: total };
    }).filter(function (line) { return line.total; });
    if (!lines.length) {
      return null;
    }
    var head = h("tr", null, h("th", { text: "Tech" }),
      FLAG_KINDS.map(function (kind) { return h("th", { class: "num", text: FLAG_LABELS[kind] }); }),
      h("th", { class: "num", text: "Total" }));
    var body = lines.map(function (line) {
      return h("tr", null, h("td", { text: line.name }),
        FLAG_KINDS.map(function (kind) {
          var n = line.counts[kind];
          return h("td", { class: n ? "num" : "num zero" },
            n ? h("span", { class: "chip " + (kind === "missed" ? "fail" : "bad"), text: String(n) })
              : h("span", { class: "muted", text: "0" }));
        }),
        h("td", { class: "num" }, h("b", { text: String(line.total) })));
    });
    return panel("Flags by tech", "How often each kind of flag came up in this period",
      tableBox(h("table", { class: "atable flag-counts" }, h("thead", null, head), h("tbody", null, body))));
  }

  /* What the records show for a day that was flagged: the sign-in, and when the tech was active. */
  function recordedText(c) {
    return (c.signin ? "Signed in " + hhmmLabel(c.signin) : "No sign-in") +
      (c.first ? " · active " + hhmmLabel(c.first) + " – " + hhmmLabel(c.last) : "");
  }

  /* Whether a day is covered by time off the owner can undo: a PTO day, or a scheduled day marked as time off
     (a marked sick day is a Sick day with `time_off` "sick"). */
  function dayHasTimeOff(c) {
    return c.time_off != null && (c.state === "pto" || SCHEDULED_DAYS.indexOf(c.state) >= 0);
  }

  /* The "Mark as day off" button for one tech's day (or "Undo day off" when `undo` is set), with the place its error
     is written. "Mark as day off" turns into the choice Sick, PTO or Cancel; Cancel puts the button back and sends
     nothing. Choosing posts the day as that kind of time off and undoing removes just that day from it; either way
     the screen is read again, where the day shows its new state. */
  function dayOffControl(email, day, undo) {
    var message = h("span", { class: "message", role: "status" });
    var slot = h("span");
    function send(request, buttons) {
      buttons.forEach(function (b) { b.disabled = true; });
      setMessage(message, "");
      request.then(function () {
        route();
      }).catch(function (error) {
        buttons.forEach(function (b) { b.disabled = false; });
        setMessage(message, error.message, "error");
      });
    }
    var main = h("button", { type: "button", class: "small", text: undo ? "Undo day off" : "Mark as day off" });
    main.addEventListener("click", function () {
      if (undo) {
        send(api("DELETE", "/api/time-off/day?email=" + encodeURIComponent(email) + "&day=" + encodeURIComponent(day)),
          [main]);
        return;
      }
      var choice = h("span", { class: "day-off-choice" });
      var opened = Date.now();
      var buttons = [["sick", "Sick"], ["pto", "PTO"]].map(function (pair) {
        var button = h("button", { type: "button", class: "small", text: pair[1] });
        button.addEventListener("click", function () {
          if (Date.now() - opened < 400) {
            return;
          }
          send(api("POST", "/api/time-off", { email: email, first: day, last: day, note: "", kind: pair[0] }),
            choice.querySelectorAll("button"));
        });
        return button;
      });
      var cancel = h("button", { type: "button", class: "small", text: "Cancel" });
      cancel.addEventListener("click", function () {
        setMessage(message, "");
        slot.replaceChildren(main);
        main.focus();
      });
      buttons.concat([cancel]).forEach(function (button) { choice.appendChild(button); });
      slot.replaceChildren(choice);
      buttons[0].focus();
    });
    slot.appendChild(main);
    return h("span", null, slot, message);
  }

  /* Every flag, oldest day first, each opening that tech's day. Any flagged day can be marked as a day off, and a day
     already marked as one shows Undo day off instead, except for a manager, who marks none (each team lead does):
     their list has no such column. The panel is "Flags" for a week or less and "Every flag" for a longer range. As
     cards (`flag-list`, app.css) each flag is a compact card: day and tech, what was recorded, then chip and button. */
  function everyFlag(rows, days) {
    var list = [];
    rows.forEach(function (row, order) {
      row.cells.filter(isCounted).forEach(function (c) {
        c.flags.forEach(function (f) { list.push({ row: row, cell: c, flag: f, order: order }); });
      });
    });
    list.sort(function (a, b) {
      return a.cell.day < b.cell.day ? -1 : a.cell.day > b.cell.day ? 1 : a.order - b.order;
    });
    var title = (days > 7 ? "Every flag" : "Flags") + " · " + list.length;
    var sub = "Compared with each tech's shift and the flag limits in Settings · click a row to open that day";
    if (!list.length) {
      return panel(title, sub, h("div", { class: "placeholder", text: "Nothing flagged in this period." }));
    }
    var dayOff = !isManager();
    var body = list.map(function (item) {
      var missed = item.flag.kind === "missed";
      var link = techRangeHash(item.row.email, item.cell.day);
      var tr = h("tr", { class: "rowlink " + (missed ? "failrow" : "flagrow") },
        h("td", null, h("a", { href: link, text: dayWithWeekday(item.cell.day) })),
        h("td", { text: item.row.name }),
        h("td", null, flagChip(item.flag)),
        h("td", { class: "muted", text: recordedText(item.cell) }),
        dayOff ? h("td", { class: "num" }, dayOffControl(item.row.email, item.cell.day, dayHasTimeOff(item.cell)))
          : null);
      tr.addEventListener("click", function (event) {
        if (!event.target.closest("a") && !event.target.closest("button")) {
          location.hash = link;
        }
      });
      return tr;
    });
    var head = h("tr", null, h("th", { text: "Day" }), h("th", { text: "Tech" }), h("th", { text: "Flag" }),
      h("th", { text: "What was recorded" }), dayOff ? h("th") : null);
    return panel(title, sub, tableBox(h("table", { class: "atable flag-list" }, h("thead", null, head),
      h("tbody", null, body)), { list: true, first: 10, noun: "flags" }));
  }

  // One tech over a range ----------------------------------------------------------------------------

  function techRangeView(data) {
    var row = data.rows[0];
    var t = tally(row.cells);
    return [
      h("h3", { class: "period-head", text: row.name + " · " + data.period.label }),
      h("div", { class: "panel-row cols-5-lead" },
        activityTile("Tickets per shift", perShift(t.workedTickets, t.worked),
          t.workedTickets + " tickets over " + plural(t.worked, "shift", "shifts"), true),
        activityTile("Shifts worked", shiftsWorked(t),
          !t.scheduled ? "None scheduled" : t.scheduled > t.worked
            ? h("div", { class: "d" }, h("span", { class: "warn", text: (t.scheduled - t.worked) + " with no activity" }))
            : "All worked"),
        activityTile("Replies per shift", perShift(t.workedReplies, t.worked), perShift(t.workedNotes, t.worked) + " notes per shift"),
        activityTile("Solved", String(t.solved), plural(t.calls, "call logged", "calls logged")),
        activityTile("Flags", String(t.flags), missedShiftsLine(t))),
      panel("Tickets touched per day", row.name + "'s shift from Settings · click a day to open it",
        h("div", { class: "scroll" }, techCalendar(row, data.days), stackedCalendar(row, data.days)),
        dayLegend(row.cells)),
      dayByDay(row)
    ];
  }

  /* "Day by day": the days the tech was due to work or did, one line each. Rows are tinted by flag. */
  function dayByDay(row) {
    var sub = "Days that were neither scheduled nor worked are left out · click a day to open it";
    var shown = row.cells.filter(function (c) { return DAY_BY_DAY_STATES.indexOf(c.state) >= 0; });
    if (!shown.length) {
      return panel("Day by day", sub, h("div", { class: "placeholder", text: "No scheduled or worked days in this period." }));
    }
    var dayOff = !isManager();   // a manager marks no day off (each team lead does): no Day off column
    var lines = shown.map(function (c) {
      var link = techRangeHash(row.email, c.day);
      var undo = dayHasTimeOff(c);
      var control = !dayOff ? undefined
        : undo || SCHEDULED_DAYS.indexOf(c.state) >= 0 ? dayOffControl(row.email, c.day, undo) : null;
      return dayLine(c, link, h("a", { href: link, text: dayWithWeekday(c.day) }), control);
    });
    return panel("Day by day", sub, dayTable("Day", lines, dayOff, { first: 10, noun: "days" }));
  }

  // -- one day -------------------------------------------------------------------------------------

  var LATE_GRACE_MIN = 15;   // a sign-in within this many minutes of the shift start is "on time"
  var ACTION_WORDS = { reply: "Public reply", note: "Internal note", call: "Call logged" };

  function clockMinutes(hhmm) {
    var parts = hhmm.split(":");
    return Number(parts[0]) * 60 + Number(parts[1]);
  }

  /* A length of time as the day chart writes it: "45 m", "4 h 05 m". */
  function durText(minutes) {
    return minutes < 60 ? minutes + " m" : Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " m";
  }

  function firstNameOf(name) {
    return name.indexOf("@") >= 0 ? "this tech" : name.split(" ")[0];
  }

  /* One line of a table of days, for a tech's day or one tech among all: the tint follows the flags and a click
     anywhere on the line but a link opens `link`. `first` is its first cell's content. One tech's table passes
     `control` (the day-off button, or null) for a last cell of its own; a click on the button stays off the line. */
  function dayLine(c, link, first, control) {
    var kind = c.flags.some(function (f) { return f.kind === "missed"; }) ? " failrow" : c.flags.length ? " flagrow"
      : c.state === "off" || c.state === "pto" ? " restrow" : "";
    var tr = h("tr", { class: "rowlink" + kind },
      h("td", null, first),
      h("td", { class: "muted", text: shiftText(c) }),
      h("td", { class: "num", text: c.signin ? hhmmLabel(c.signin) : "-" }),
      h("td", { class: "num", text: c.first ? hhmmLabel(c.first) + " – " + hhmmLabel(c.last) : "-" }),
      h("td", { class: "num", text: c.gap_min ? formatMinutes(c.gap_min) : "-" }),
      h("td", { class: "num", text: String(c.tickets) }),
      h("td", { class: "num", text: String(c.replies) }),
      h("td", { class: "num", text: String(c.notes) }),
      h("td", { class: "num", text: String(c.calls) }),
      h("td", { class: "num", text: String(c.solved) }),
      h("td", null, h("span", { class: "chips" }, c.flags.map(flagChip))));
    if (control !== undefined) {
      if (control) {
        control.addEventListener("click", function (event) { event.stopPropagation(); });
      }
      tr.appendChild(h("td", null, control));
    }
    tr.addEventListener("click", function (event) {
      if (!event.target.closest("a")) {
        location.hash = link;
      }
    });
    return tr;
  }

  /* A table of days. `dayOff` adds the last, unlabelled column that holds each day's day-off button. `more` gives a
     long list its first rows and Show all on a narrow screen (tableBox's `first` and `noun`). */
  function dayTable(firstHead, lines, dayOff, more) {
    var heads = [[firstHead], ["Shift"], ["Signed in", true], ["First – last action", true], ["Longest gap", true],
      ["Tickets", true], ["Replies", true], ["Notes", true], ["Calls", true], ["Solved", true], ["Flags"]];
    var cells = heads.map(function (head) { return h("th", { class: head[1] ? "num" : null, text: head[0] }); });
    if (dayOff) {
      cells.push(h("th", null, h("span", { class: "sr-only", text: "Day off" })));
    }
    return tableBox(h("table", { class: "atable one-line" }, h("thead", null, h("tr", null, cells)), h("tbody", null, lines)),
      Object.assign({ list: true }, more));
  }

  // All techs on one day -------------------------------------------------------------------------

  function allTechsDayView(data) {
    var day = data.days[0];
    var cells = data.rows.map(function (r) { return r.cells[0]; });
    var t = tally(cells);
    var scheduled = cells.filter(function (c) { return SCHEDULED_DAYS.indexOf(c.state) >= 0; }).length;
    var touching = cells.filter(function (c) { return isCounted(c) && c.tickets > 0; }).length;
    var lines = data.rows.map(function (r) {
      return dayLine(r.cells[0], techRangeHash(r.email, day), h("a", { href: techRangeHash(r.email, day), text: r.name }));
    });
    return [
      h("h3", { class: "period-head", text: data.period.label + " · " + plural(scheduled, "tech", "techs") + " scheduled" }),
      h("div", { class: "panel-row cols-4-lead" },
        activityTile("Tickets touched", String(t.tickets), "by " + touching + " of " + plural(scheduled, "scheduled tech", "scheduled techs"), true),
        activityTile("Replies", String(t.replies), plural(t.notes, "internal note", "internal notes")),
        activityTile("Solved", String(t.solved), plural(t.calls, "call logged", "calls logged")),
        activityTile("Flags", String(t.flags), missedShiftsLine(t))),
      panel("Everyone", "Click a tech to open their day", dayTable("Tech", lines))
    ];
  }

  // One tech on one day --------------------------------------------------------------------------

  /* The tech's day: the row from the range answer, the day's detail and the Zendesk subdomain (or none) for
     ticket links. The detail is read here, so the box fills in when it arrives. */
  function techDayView(token, data) {
    var row = data.rows[0];
    var urls = ["/api/activity/day" + queryString({ tech: row.email, day: data.days[0] }), "/api/settings"];
    var box = h("div", null, urls.every(isKept) ? null : h("div", { class: "message", text: "Loading the day…" }));
    readKept(urls, function () { return token === renderToken; }, function (got) {
      box.textContent = "";
      techDayNodes(row, got[0], got[1].zendesk_subdomain).forEach(function (node) { box.appendChild(node); });
    }).catch(function (error) {
      if (token === renderToken) {
        box.textContent = "";
        box.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      }
    });
    return box;
  }

  function textTile(label, value, detail) {
    var node = activityTile(label, value, detail);
    node.children[1].className += " v-text";
    return node;
  }

  /* The first and last action as "8:04 – 4:51 PM", dropping the first's AM / PM when the last has the same. */
  function spanLabel(first, last) {
    var a = hhmmLabel(first);
    var b = hhmmLabel(last);
    return a.slice(-2) === b.slice(-2) ? a.slice(0, -3) + " – " + b : a + " – " + b;
  }

  /* The shift the tech was due to work on the day of `c`, or null: a rest day (worked or not), time off and a day
     off the team have none, whatever hours were in force. */
  function dueShift(c) {
    return c.shift && DUE_SHIFT_STATES.indexOf(c.state) >= 0 ? c.shift : null;
  }

  function signedInTile(c) {
    var detail = "";
    var shift = dueShift(c);
    if (c.signin && shift) {
      var late = clockMinutes(c.signin) - clockMinutes(shift.start);
      detail = late > LATE_GRACE_MIN ? h("div", { class: "d" }, h("span", { class: "warn", text: durText(late) + " after shift start" }))
        : "on time";
    }
    return textTile("Signed in", c.signin ? hhmmLabel(c.signin) : "-", detail);
  }

  function dayTiles(c) {
    return h("div", { class: "panel-row cols-5-lead" },
      activityTile("Tickets touched", String(c.tickets), plural(c.calls, "call logged", "calls logged"), true),
      signedInTile(c),
      textTile("First – last action", c.first ? spanLabel(c.first, c.last) : "-",
        c.first ? durText(clockMinutes(c.last) - clockMinutes(c.first)) + " span" : ""),
      activityTile("Replies", String(c.replies), plural(c.notes, "internal note", "internal notes")),
      activityTile("Solved", String(c.solved), ""));
  }

  function longGap(c) {
    var f = c.flags.filter(function (flagged) { return flagged.kind === "long_gap"; })[0];
    return f && c.gap_start ? { start: c.gap_start, minutes: f.minutes } : null;
  }

  function dayLegendRow() {
    function item(kind, text) {
      return [h("span", { class: "sw " + kind }), text];
    }
    return h("div", { class: "chart-legend" }, item("act", "Reply or ticket change"), item("note", "Internal note"),
      item("call", "Logged call"), item("shift", "Shift"));
  }

  function dayChartPanel(c, actions, row, subdomain) {
    var box = h("div", { class: "chart-box" });
    // Hovering a moment says what the tech did then, on which ticket; clicking it opens the ticket in Zendesk
    // (the owner, 2026-10-22), when the subdomain is set.
    Charts.dayChart(box, { shift: dueShift(c), signin: c.signin, actions: actions, gap: longGap(c) }, {
      describe: function (those) { return momentCard(those, row.email); },
      open: subdomain ? function (those) {
        window.open(ticketUrl(those[0].ticket_id, subdomain), "_blank", "noopener");
      } : null });
    return panel("The day", "Every recorded action, with the shift shaded", dayLegendRow(), box,
      c.flags.length ? h("div", { class: "timeline-chips chips" }, c.flags.map(function (f) { return dayFlagChip(c, f); })) : null);
  }

  /* A flag chip as the day view writes it: a Sick day says whether the owner marked it or it is automatic. */
  function dayFlagChip(c, f) {
    return f.kind === "missed"
      ? h("span", { class: "chip fail", text: FLAG_LABELS.missed + " · " + (c.time_off === "sick" ? "marked" : "no sign-in or activity") })
      : flagChip(f);
  }

  /* What one save did, in plain words. An assignee change reads "took the ticket" only when the record names this
     tech (by email) as the new assignee; anything else, including older rows that hold an id, is "changed the assignee". */
  function changeWord(a, email) {
    switch (a.kind) {
      case "status": return a.new === "solved" ? "solved" : a.new ? "set " + capitalize(a.new) : "changed the status";
      case "assignee": return a.new && email && String(a.new).toLowerCase() === String(email).toLowerCase()
        ? "took the ticket" : "changed the assignee";
      case "group": return "moved group";
      case "priority": return "priority " + (a.new || "cleared");
      case "fields": return "edited fields";
      case "created": return "created the ticket";
      case "merge": return "merged";
      default: return a.kind;
    }
  }

  /* One row per save: actions with the same event_id are joined, replies, notes and calls first and then the
     ticket's changes, each part starting with a capital ("Public reply · Set Pending"). Each is {minutes, at, text,
     ticket_id, subject}. */
  function joinActions(actions, email) {
    var groups = [];
    var byEvent = {};
    actions.forEach(function (a) {
      var group = a.event_id != null ? byEvent[a.event_id] : null;
      if (!group) {
        group = { at: a.at, ticket_id: a.ticket_id, subject: a.subject, said: [], changes: [] };
        groups.push(group);
        if (a.event_id != null) {
          byEvent[a.event_id] = group;
        }
      }
      var list = ACTION_WORDS[a.kind] ? group.said : group.changes;
      var word = ACTION_WORDS[a.kind] || changeWord(a, email);
      if (list.indexOf(word) < 0) {
        list.push(word);
      }
    });
    return groups.map(function (g) {
      return { at: g.at, minutes: clockMinutes(g.at), text: g.said.concat(g.changes).map(capitalize).join(" · "),
        ticket_id: g.ticket_id, subject: g.subject };
    });
  }

  /* The hovercard of a day's actions at one minute: the time, then each save (`joinActions`) with its ticket. Its foot
     says how to open the ticket: a click with a mouse, a second tap on a touch screen (the first tap reads it). */
  function momentCard(actions, email) {
    var touch = window.matchMedia && window.matchMedia(TOUCH).matches;
    return { title: hhmmLabel(actions[0].at),
      items: joinActions(actions, email).map(function (g, k) {
        var kind = actions.filter(function (a) { return a.ticket_id === g.ticket_id; })
          .map(function (a) { return a.kind; })[0];
        return { kind: kind === "note" || kind === "call" ? kind : "act", head: g.text,
          text: g.ticket_id == null ? null : "#" + g.ticket_id + (g.subject ? " " + g.subject : "") };
      }),
      foot: actions.some(function (a) { return a.ticket_id != null; })
        ? (touch ? "Tap again to open the ticket in Zendesk." : "Click to open the ticket in Zendesk.") : null };
  }

  function ticketUrl(ticketId, subdomain) {
    return "https://" + subdomain + ".zendesk.com/agent/tickets/" + ticketId;
  }

  /* "#4821" linking to the ticket in Zendesk, or plain text when the subdomain isn't set. */
  function ticketRef(ticketId, subdomain) {
    var id = "#" + ticketId;
    return subdomain
      ? h("a", { href: ticketUrl(ticketId, subdomain), target: "_blank",
        rel: "noopener noreferrer", text: id })
      : h("span", { text: id });
  }

  function ticketCell(item, subdomain) {
    return h("td", null, ticketRef(item.ticket_id, subdomain), " ", h("span", { class: "muted", text: item.subject || "-" }));
  }

  /* Why a day with no actions has none: not on the team, still to come, not synced yet, time off, a rest day,
     marked as a sick day, signed in and idle, or scheduled and absent. */
  function emptyDayText(c) {
    if (c.state === "untracked") {
      return "Not on the team on this day.";
    }
    if (c.state === "future") {
      return "Still to come.";
    }
    if (c.state === "unsynced") {
      return "Not synced yet.";
    }
    if (c.state === "pto") {
      return "Time off. Nothing recorded.";
    }
    if (c.state === "missed" && c.time_off === "sick") {
      return "Marked as a sick day. No activity recorded.";
    }
    if (!c.shift || c.state === "rest" || c.state === "off") {
      return "Rest day. Nothing recorded.";
    }
    return c.signin ? "Signed in at " + hhmmLabel(c.signin) + ", then nothing recorded for the rest of the shift: " +
      "no replies, notes, calls or ticket changes."
      : "Scheduled " + hhmmShort(c.shift.start) + " – " + hhmmShort(c.shift.end) + ", with no sign-in and no activity.";
  }

  function actionsPanel(row, c, day, subdomain) {
    var title = "What " + firstNameOf(row.name) + " did, in order";
    var items = joinActions(day.actions, row.email);
    if (!items.length) {
      var scheduled = c.shift && (c.state === "missed" || c.state === "zero" || c.state === "worked");
      return panel(title, null, h("div", { class: "placeholder" }, emptyDayText(c),
        scheduled && !c.signin && !c.time_off && !isManager()
          ? h("div", { class: "actions" }, dayOffControl(row.email, c.day, false)) : null));
    }
    var gap = longGap(c);
    var gapEnd = gap ? clockMinutes(gap.start) + gap.minutes : null;
    var gapDone = false;
    var lines = [];
    function gapLine() {
      gapDone = true;
      lines.push(h("tr", { class: "flagrow" }, h("td", { class: "num warn", text: durText(gap.minutes) }),
        h("td", { class: "warn", colspan: 2, text: "Nothing recorded" })));
    }
    items.forEach(function (item) {
      if (gap && !gapDone && item.minutes >= gapEnd) {
        gapLine();
      }
      lines.push(h("tr", null, h("td", { class: "num", text: hhmmLabel(item.at) }), h("td", { text: item.text }),
        ticketCell(item, subdomain)));
    });
    if (gap && !gapDone) {
      gapLine();
    }
    return panel(title, null, tableBox(h("table", { class: "atable" },
      h("thead", null, h("tr", null, h("th", { class: "num", text: "Time" }), h("th", { text: "Action" }), h("th", { text: "Ticket" }))),
      h("tbody", null, lines))));
  }

  /* Recent days: at first the 7 days ending on the day shown; Earlier and Later step a week back or forward (the
     owner, 2026-10-22), read from the grid's answer for the tech (/api/activity), while the day shown stays the
     page's. Rest, untracked and future days are left out, the day shown never. Clicking a row opens that day. */
  function recentDaysPanel(row, recent, current) {
    var body = h("tbody");
    var head = h("span", { class: "recent-head" });
    var message = h("div", { class: "message" });
    var asked = 0;

    function rows(cells) {
      body.textContent = "";
      cells.filter(function (c) { return c.day === current || DAY_BY_DAY_STATES.indexOf(c.state) >= 0; })
        .forEach(function (c) {
          var link = techRangeHash(row.email, c.day);
          var kind = c.flags.some(function (f) { return f.kind === "missed"; }) ? " failrow" : c.flags.length ? " flagrow" : "";
          var tr = h("tr", { class: "rowlink" + kind },
            h("td", null, c.day === current ? h("b", { text: dayWithWeekday(c.day) }) : h("a", { href: link, text: dayWithWeekday(c.day) })),
            h("td", { class: "num", text: c.state === "pto" ? "PTO" : String(c.tickets) }),
            h("td", { class: "num", text: String(c.replies) }),
            h("td", { class: "num", text: String(c.solved) }),
            h("td", { class: "num", text: c.flags.length ? String(c.flags.length) : "" }));
          tr.addEventListener("click", function (event) {
            if (!event.target.closest("a")) {
              location.hash = link;
            }
          });
          body.appendChild(tr);
        });
      if (!body.children.length) {
        body.appendChild(h("tr", null, h("td", { colspan: "5", class: "empty", text: "No work days in this week." })));
      }
      tableChanged(body);
    }
    function show(cells, start, end) {
      rows(cells);
      head.textContent = "";
      head.appendChild(h("span", { class: "recent-span", text: spanDays({ start: start, end: end }) }));
      head.appendChild(stepButtons("rd", start, end, load));
    }
    function load(step) {
      var mine = ++asked;
      setMessage(message, "");
      getKept("/api/activity" + queryString({ period: "custom", start: step.start, end: step.end, tech: row.email }))
        .then(function (data) {
          if (mine !== asked) {
            return;
          }
          var theirs = data.rows.filter(function (r) { return r.email === row.email; })[0];
          show(theirs ? theirs.cells : [], step.start, step.end);
        }, function (error) {
          if (mine === asked) {
            setMessage(message, error.message, "error");
          }
        });
    }
    show(recent, recent.length ? recent[0].day : current, recent.length ? recent[recent.length - 1].day : current);
    return panel("Recent days", head, tableBox(h("table", { class: "atable" },
      h("thead", null, h("tr", null, h("th", { text: "Day" }), h("th", { class: "num", text: "Tickets" }),
        h("th", { class: "num", text: "Replies" }), h("th", { class: "num", text: "Solved" }), h("th", { class: "num", text: "Flags" }))),
      body), { plain: true }), message);
  }

  /* "Oct 14, 3:12 PM" from a local "2026-10-14T15:12". */
  function actedText(local) {
    if (!local) {
      return "-";
    }
    var parts = local.split("T");
    return dayLabel(parts[0], false) + ", " + hhmmLabel(parts[1]);
  }

  function openTicketsPanel(row, open, subdomain) {
    var first = firstNameOf(row.name);
    var title = "Open tickets assigned to " + first + " now";
    var aging = open.tickets.filter(function (t) { return t.aging; }).length;
    // Aging tickets (open past 1:1 Prep's aging rule) are picked out (the owner, 2026-10-22): tinted, with their age.
    var sub = [open.as_of ? "As of " + timeLabel(open.as_of) : null,
      open.tickets.length ? open.tickets.length + " open" + (aging ? " · " + aging + " aging" : "") : null]
      .filter(Boolean).join(" · ") || null;
    if (!open.tickets.length) {
      return panel(title, sub, h("div", { class: "placeholder", text: "No open tickets are assigned to " + first + "." }));
    }
    var lines = open.tickets.map(function (t) {
      return h("tr", { class: t.aging ? "flagrow aging" : null }, ticketCell(t, subdomain),
        h("td", { class: "nowrap" }, capitalize(t.status),
          t.aging ? h("span", { class: "chip bad", text: "Aging · " + t.age_days + " days" }) : null),
        h("td", { class: "num", text: actedText(t.last_acted) }));
    });
    return panel(title, sub, tableBox(h("table", { class: "atable" },
      h("thead", null, h("tr", null, h("th", { text: "Ticket" }), h("th", { text: "Status" }),
        h("th", { class: "num", text: first + " last acted" }))),
      h("tbody", null, lines)), { plain: true }));
  }

  /* The day-off button beside a tech day's heading: "Mark as day off" on a day the tech was due to work, "Undo day
     off" on one covered by time off (PTO or a marked sick day), and nothing on any other day, nor for a manager. */
  function dayOffInHeading(row, c) {
    var undo = dayHasTimeOff(c);
    if (isManager() || !undo && SCHEDULED_DAYS.indexOf(c.state) < 0) {
      return null;
    }
    return h("span", { class: "day-off" }, dayOffControl(row.email, c.day, undo));
  }

  function techDayNodes(row, day, subdomain) {
    var c = day.cell;
    return [
      h("h3", { class: "period-head", text: row.name + " · " + dayWithWeekday(c.day) + " · " + shiftText(c) },
        dayOffInHeading(row, c)),
      dayTiles(c),
      dayChartPanel(c, day.actions, row, subdomain),
      h("div", { class: "panel-row cols-2-1" }, actionsPanel(row, c, day, subdomain),
        h("div", { class: "stack" }, recentDaysPanel(row, day.recent, c.day), openTicketsPanel(row, day.open, subdomain)))
    ];
  }

  // -- team activity trends -----------------------------------------------------------------------

  var TRENDS_KEYS = ["period", "start", "end", "compare", "compare_start", "compare_end", "tech", "region", "team",
    "measure", "sort"];
  var TRENDS_ASKED = ["period", "start", "end", "compare", "compare_start", "compare_end"];   // what the API is asked
  var TREND_SLIDE_MS = 280;   // the Trends graph's slide open and shut (app.css, --slide and .metric-detail)
  var SORT_COLUMNS = ["change", "now", "tech"];
  var SORT_TEXT = {   // [ascending, descending]
    change: ["Biggest slip first", "Biggest improvement first"],
    now: ["Weakest first", "Strongest first"],
    tech: ["By name", "By name"]
  };
  var TREND_UNITS = { percent: "%", minutes: " min" };
  var TRENDS_NOTE = "Days off, rest days, untracked days and days not yet synced aren't counted. " +
    "Built from Zendesk's own records, read-only.";
  var trendsHighlight = null;   // the email of the tech whose row the owner clicked, or null
  // Each tech's comparison team, the lead's email (null: every tech), as the Trends answers for one tech or one team
  // read since the kept answers were last forgotten give it.
  var trendsTeams = { epoch: keptEpoch, of: Object.create(null) };

  function trendsHash(params) {
    return "#/team/trends" + queryString(params);
  }

  /* The team the tech is compared with as the Trends answers give it: the lead's email, or null for every tech (a
     tech on no team, as on the PC); undefined when none has since the kept answers were last forgotten: a write may
     have moved them. */
  function trendsTeam(email) {
    var of = trendsTeams.epoch === keptEpoch ? trendsTeams.of : null;
    return of && email in of ? of[email] : undefined;
  }

  /* An answer for one tech or one team lists the techs of one team, `team` (null: every tech), and each of them is
     compared with it. */
  function learnTeams(data) {
    if (trendsTeams.epoch !== keptEpoch) {
      trendsTeams = { epoch: keptEpoch, of: Object.create(null) };
    }
    data.techs.forEach(function (t) {
      trendsTeams.of[t.email] = data.team;
    });
  }

  /* The answer holds the techs the graph draws: every tech, a picked region's, or with one tech picked, the team
     they are compared with (their team lead's, T-144; every tech on the PC, which has no teams). So the read for a
     tech is the read for their team, or All techs', once an answer for one tech or one team has listed them, and
     every tech on a team shares that one kept answer; a tech no answer has placed yet is asked for by name, and the
     API checks them. A team's read that comes back without the tech (their team was changed elsewhere since) forgets
     what was learned and asks for them by name. A team a manager picked is asked for as it is; its answer places
     no one, since a tech on two teams is compared with only one of them. A measure, a sort or a highlight is drawn
     from the answer kept for the same read. */
  function renderTrends(token, query) {
    var params = paramsFrom(query, TRENDS_KEYS);
    var asked = {};
    TRENDS_ASKED.forEach(function (key) { asked[key] = params[key]; });
    var learned = params.tech ? trendsTeam(params.tech) : undefined;
    if (learned !== undefined) {
      asked.team = learned;
    } else if (params.tech) {
      asked.tech = params.tech;
    } else if (params.team) {
      asked.team = params.team;
    } else {
      asked.region = params.region;
    }
    loadingLine("Loading trends…");
    readScreen(token, ["/api/activity/trends" + queryString(asked)], function (got, drawToken) {
      if (asked.tech || learned) {
        learnTeams(got[0]);
      }
      if (learned !== undefined && !got[0].techs.some(function (t) { return t.email === params.tech; })) {
        delete trendsTeams.of[params.tech];
        renderTrends(drawToken, query);
        return;
      }
      showTrends(params, got[0]);
    }).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      screen.appendChild(h("div", { class: "nav-note" }, h("a", { href: "#/team/trends", text: "Show the last 8 weeks" })));
    });
  }

  /* A figure as the screen writes it: to `digits` decimals (default the measure's own), with its unit; m:ss for a
     duration, which is in seconds ("2:05"); a dash for none. The team's figures use `m.team_digits`: the team's count
     is a mean per tech, so it keeps a decimal. */
  function trendValue(m, value, digits) {
    if (value == null) {
      return "-";
    }
    if (m.unit === "duration") {
      return Charts.minSec(value);
    }
    return value.toFixed(digits == null ? m.digits : digits) + (TREND_UNITS[m.unit] || "");
  }

  /* The change against the comparison, as `{ text, cls }`: "▲ 1.2", "▼ 12 min" or "▼ 0:15" (cls "better" or
     "worse" by the measure's own direction), "no change", "new" when there is nothing earlier, "-" when there is
     nothing now; null when the owner compares with nothing. `digits` as for trendValue. */
  function trendChange(m, figs, comparison, digits) {
    if (!comparison) {
      return null;
    }
    if (figs.now == null) {
      return { text: "-", cls: "flat" };
    }
    if (figs.before == null) {
      return { text: "new", cls: "flat" };
    }
    if (!figs.change) {
      return { text: "no change", cls: "flat" };
    }
    var better = m.better === "higher" ? figs.change > 0 : figs.change < 0;
    return { text: (figs.change > 0 ? "▲ " : "▼ ") + trendValue(m, Math.abs(figs.change), digits), cls: better ? "better" : "worse" };
  }

  function trendDelta(change) {
    return change ? h("span", { class: "mdelta " + change.cls, text: change.text }) : null;
  }

  /* The sort the address names: `{ key, desc }`. Change needs a comparison; without one, and for anything
     unknown, the default is Now, weakest first. */
  function trendSort(text, comparison) {
    var key = (text || "").replace(/^-/, "");
    if (SORT_COLUMNS.indexOf(key) < 0 || (key === "change" && !comparison)) {
      return { key: comparison ? "change" : "now", desc: false };
    }
    return { key: key, desc: /^-/.test(text) };
  }

  /* The address's `sort`, or null when it is the default. */
  function trendSortParam(sort, comparison) {
    if (sort.key === (comparison ? "change" : "now") && !sort.desc) {
      return null;
    }
    return (sort.desc ? "-" : "") + sort.key;
  }

  /* The ranking, in order: a row per tech with a value to sort by (null: no value, which sorts last either way).
     Higher or lower is better by the measure, so ascending is always weakest first and biggest slip first. */
  function trendRows(data, m, sort) {
    var higher = m.better === "higher";
    var rows = data.techs.map(function (tech, i) {
      var figs = m.techs[i];
      var by = { tech: tech.name, now: figs.now == null ? null : (higher ? figs.now : -figs.now),
        change: figs.change == null ? null : (higher ? figs.change : -figs.change) };
      return { email: tech.email, name: tech.name, figs: figs, by: by[sort.key] };
    });
    var dir = sort.desc ? -1 : 1;
    rows.sort(function (a, b) {
      if (a.by == null || b.by == null) {
        return a.by == null && b.by == null ? a.name.localeCompare(b.name) : (a.by == null ? 1 : -1);
      }
      var order = sort.key === "tech" ? a.by.localeCompare(b.by) : a.by - b.by;
      return order * dir || a.name.localeCompare(b.name);
    });
    return rows;
  }

  /* The Trends screen: the filters, the period, then a panel per measure. No ranking or graph shows until a panel is
     picked; then the measure's ranking and graph slide open under that panel's row, pushing the rows below down.
     Another panel in the same row redraws them in place; one in another row slides them shut, then open under its
     own row; the picked panel again slides them shut. The pick is kept in the address (`measure`, only while a
     panel is open) without drawing the screen again, so the slide is seen; a measure in the address opens at once,
     with no slide. The panel rows follow the page's width, so a width change moves the open part under the picked
     panel's row as it now falls. */
  function showTrends(params, data) {
    var comparison = data.comparison;
    var m = data.measures.filter(function (x) { return x.key === params.measure; })[0] || null;
    var picked = -1;
    data.techs.forEach(function (tech, i) {
      if (tech.email === params.tech) {
        picked = i;
      }
    });
    var sort = trendSort(params.sort, comparison);
    function lightFor(rows) {
      var highlight = null;
      [trendsHighlight, params.tech, rows.length ? rows[0].email : null].forEach(function (email) {
        if (highlight === null && email && data.techs.some(function (t) { return t.email === email; })) {
          highlight = email;
        }
      });
      return highlight;
    }
    function go(changes) {
      var next = Object.assign({}, params, changes);
      if ("period" in changes) {
        if (next.compare === "same_days_last_week" && !shortPeriod(next.period, next.start, next.end)) {
          next.compare = "previous";   // still a comparison: the period before
        }
        if (next.period === "last_8_weeks") {
          next.period = null;
        }
      }
      if ("tech" in changes) {
        trendsHighlight = null;   // picking a tech (or a region, or All techs) starts the highlight over
      }
      location.hash = trendsHash(next);
    }
    clearScreen();
    screen.appendChild(trendsFilters(params, data, go));
    screen.appendChild(trendsHead(data));
    var who = picked >= 0 ? data.techs[picked].name.split(" ")[0] + "'s" : "Team";
    screen.appendChild(h("p", { class: "metric-lead",
      text: who + " averages for this period · click a panel to see it week by week" }));
    var tiles = [];
    var grid = h("div", { class: "metric-tiles" });
    data.measures.forEach(function (x) {
      var figs = picked >= 0 ? x.techs[picked] : x.team;
      var tile = metricTile(x, figs, picked >= 0 ? x.digits : x.team_digits, comparison, x === m, function () {
        pickMeasure(x);
      });
      tile.measure = x;
      tiles.push(tile);
      grid.appendChild(tile);
    });
    screen.appendChild(grid);
    screen.appendChild(h("p", { class: "note", text: TRENDS_NOTE }));

    var open = null;   // the part open under a row: { node, inner, measure }

    function tileOf(x) {
      return tiles.filter(function (t) { return t.measure === x; })[0];
    }
    // The last panel in `tile`'s row as the rows fall with nothing open: the panels fill the grid's columns in order.
    // The columns are counted from the panels' left edges, which an open part doesn't move; their tops it does (the
    // part takes a row of its own, so the panels after it start a new row).
    function lastInRow(tile) {
      var lefts = [];
      tiles.forEach(function (t) {
        if (lefts.indexOf(t.offsetLeft) < 0) {
          lefts.push(t.offsetLeft);
        }
      });
      var at = tiles.indexOf(tile);
      return tiles[Math.min(at - at % lefts.length + lefts.length - 1, tiles.length - 1)];
    }
    function fill(inner, x) {
      inner.textContent = "";
      var rows = trendRows(data, x, sort);
      var light = lightFor(rows);
      var box = h("div", { class: "chart-box" });
      // Pointing at a tech's row (or focusing their name) lifts their line in the graph; the highlighted tech stays
      // the focus, and leaving the table puts every line back (the owner, 2026-10-22).
      function preview(email) {
        Charts.peek(box, email && email !== light ? email : null);
      }
      inner.appendChild(h("div", { class: "panel-row trends-row" },
        rankingPanel(data, x, rows, sort, light, go, function (email) {
          trendsHighlight = email;
          fill(inner, x);
        }, preview),
        graphPanel(data, x, light, box)));
      drawTrendsGraph(box, data, x, light);   // once on the page, so the graph measures its real width
    }
    function openUnder(x, slide) {
      var inner = h("div", { class: "metric-detail-in" });
      var node = h("div", { class: "metric-detail" + (slide ? "" : " open") }, inner);
      lastInRow(tileOf(x)).after(node);
      fill(inner, x);
      if (slide) {
        void node.offsetHeight;   // laid out shut first, so opening it slides
        node.classList.toggle("open", true);
      }
      return { node: node, inner: inner, measure: x };
    }
    // Slide `part` shut, then take it off the page and run `then`. A page in the background runs no transitions, and
    // reduced motion has none to end, so a timer finishes it too.
    function shut(part, then) {
      var done = false;
      function finish() {
        if (!done) {
          done = true;
          part.node.remove();
          if (then) {
            then();
          }
        }
      }
      // Only once it is shut: a page whose frames are throttled can deliver the opening slide's end late, after this
      // shut has begun.
      part.node.addEventListener("transitionend", function (event) {
        if (event.target === part.node && part.node.offsetHeight === 0) {
          finish();
        }
      });
      setTimeout(finish, TREND_SLIDE_MS + 100);
      part.node.classList.toggle("open", false);
    }
    // Keep the pick in the address without drawing the screen again: replaceState fires no hashchange, and the
    // route on screen becomes the new address.
    function remember(x) {
      params = Object.assign({}, params, { measure: x ? x.key : null });
      history.replaceState(null, "", trendsHash(params));
      shownHash = location.hash;
      tiles.forEach(function (t) {
        var on = t.measure === x;
        t.classList.toggle("on", on);
        t.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    function pickMeasure(x) {
      if (open && open.measure === x) {
        var closing = open;
        open = null;
        remember(null);
        shut(closing);
        return;
      }
      remember(x);
      if (open && lastInRow(tileOf(open.measure)) === lastInRow(tileOf(x))) {
        open.measure = x;
        fill(open.inner, x);
      } else if (open) {
        var leaving = open;
        open = null;
        shut(leaving, function () {
          if (params.measure === x.key && !open) {
            open = openUnder(x, true);
          }
        });
      } else {
        open = openUnder(x, true);
      }
    }
    if (m) {
      open = openUnder(m, false);
    }
    // Watch the grid's width. The move waits for a timer: not inside the observer (a change made there is reported
    // as a ResizeObserver loop error), and not for an animation frame (a page in the background runs none).
    var width = grid.clientWidth;
    function follow() {
      if (open && open.node.isConnected) {
        var anchor = lastInRow(tileOf(open.measure));
        if (open.node.previousElementSibling !== anchor) {
          anchor.after(open.node);
        }
      }
    }
    if (typeof ResizeObserver === "function") {
      var watcher = new ResizeObserver(function () {
        if (!grid.isConnected) {
          watcher.disconnect();   // the screen was redrawn without this grid
        } else if (grid.clientWidth !== width) {
          width = grid.clientWidth;
          setTimeout(follow, 0);
        }
      });
      watcher.observe(grid);
    }
  }

  /* Tech, Period, Compare to, then Earlier and Later. The defaults (Last 8 weeks, compared with Nothing) stay out of
     the address. */
  function trendsFilters(params, data, go) {
    var message = h("div", { class: "message", role: "status" });
    var techs = techChoices(data.picker);
    if (params.tech && !data.picker.some(function (t) { return t.email === params.tech; })) {
      techs.push({ value: params.tech, text: params.tech });
    }
    return h("div", { class: "filters", role: "group", "aria-label": "Filters" },
      filterSelect("tr-tech", "Tech", techs, techSelected(params), function (value) { go(techPicked(value)); }),
      periodSelect("tr", data.period, params, message, go),
      compareControl(data, params, message, go, "none"),
      stepButtons("tr", data.period.start, plannedEnd(data.period, params), go),
      message);
  }

  /* "Last 8 weeks (Aug 16 – Oct 10) vs the 8 weeks before · 4 techs", plus " · data from Jul 21" when the days
     asked for reach back before the first synced day. */
  function trendsHead(data) {
    var text = data.period.label + (data.comparison ? " vs " + data.comparison.label : "") + " · " +
      plural(data.techs.length, "tech", "techs");
    if (data.coverage_from) {
      text += " · data from " + dayLabel(data.coverage_from, false);
    }
    return h("h3", { class: "period-head", text: text });
  }

  /* One measure's panel: its name, the figure (to `digits` decimals, or m:ss for a duration), the change against the
     comparison (titled with the comparison's label) and its group. Clicking it graphs the measure. */
  function metricTile(m, figs, digits, comparison, selected, onSelect) {
    var unit = TREND_UNITS[m.unit];
    var number = m.unit === "duration" ? trendValue(m, figs.now) : (figs.now == null ? "-" : figs.now.toFixed(digits));
    var change = trendDelta(trendChange(m, figs, comparison, digits));
    return h("button", { type: "button", class: "tile metric-tile" + (selected ? " on" : ""),
      "aria-pressed": selected ? "true" : "false", onclick: onSelect },
      h("div", { class: "k", text: m.label }),
      h("div", { class: "v tile-num" }, number, unit && figs.now != null ? h("span", { class: "of", text: unit }) : null),
      change ? h("div", { class: "d", title: comparison.label }, change) : null,
      h("div", { class: "grp", text: m.group }));
  }

  /* A period's days as a column head names them: "Aug 23 – Oct 17", "Dec 20, 2025 – Jan 5, 2026" (the year when
     the days cross years), "Oct 17" for one day. */
  function spanDays(period) {
    var crosses = period.start.slice(0, 4) !== period.end.slice(0, 4);
    return period.start === period.end ? dayLabel(period.start, false)
      : dayLabel(period.start, crosses) + " – " + dayLabel(period.end, crosses);
  }

  /* The ranking table for the measure: Tech, Average (Total for a count measure) and (with a comparison) vs
     before, headers that sort, a row per tech that highlights it in the graph, and the team below, unsorted. The
     figure heads name the days they cover, so a Total is never read as the whole graph's. */
  function rankingPanel(data, m, rows, sort, highlight, go, lit, preview) {
    var comparison = data.comparison;
    function head(key, label, cls, period) {
      var active = sort.key === key;
      return h("th", { scope: "col", class: cls, "aria-sort": active ? (sort.desc ? "descending" : "ascending") : "none" },
        h("button", { type: "button", class: "th-sort" + (active ? " on" : ""),
          text: label + (active ? (sort.desc ? " ▼" : " ▲") : ""),
          onclick: function () {
            var next = active ? { key: key, desc: !sort.desc } : { key: key, desc: false };
            go({ sort: trendSortParam(next, comparison) });
          } }),
        period ? h("span", { class: "th-days", text: spanDays(period) }) : null);
    }
    function figureCells(figs, digits) {
      return [h("td", { class: "num" }, h("b", { text: trendValue(m, figs.now, digits) })),
        comparison ? h("td", null, trendDelta(trendChange(m, figs, comparison, digits))) : null];
    }
    var body = rows.map(function (row) {
      var on = row.email === highlight;
      var name = h("button", { type: "button", class: "rank-name", "aria-pressed": on ? "true" : "false", text: row.name,
        onclick: function () { lit(row.email); } });
      var tr = h("tr", { class: "rowlink" + (on ? " sel" : "") }, h("td", null, name), figureCells(row.figs));
      tr.addEventListener("click", function (event) {
        if (!event.target.closest("button")) {
          lit(row.email);
        }
      });
      if (preview) {
        tr.addEventListener("mouseenter", function () { preview(row.email); });
        name.addEventListener("focus", function () { preview(row.email); });
        name.addEventListener("blur", function () { preview(null); });
      }
      return tr;
    });
    body.push(h("tr", { class: "teamrow" }, h("td", { class: "muted", text: "Team average" }), figureCells(m.team, m.team_digits)));
    var sub = SORT_TEXT[sort.key][sort.desc ? 1 : 0] + " · click a column to sort, a tech to focus";
    var tbody = h("tbody", null, body);
    if (preview) {
      tbody.addEventListener("mouseleave", function () { preview(null); });
    }
    return panel(m.label + " by tech", sub, tableBox(h("table", { class: "atable trend-rank" },
      h("thead", null, h("tr", null, head("tech", "Tech"), head("now", m.unit === "count" ? "Total" : "Average", "num", data.period),
        comparison ? head("change", "vs before", null, comparison) : null)),
      tbody)));
  }

  function graphPanel(data, m, highlight, box) {
    var name = highlight ? data.techs.filter(function (t) { return t.email === highlight; })[0].name : null;
    var unit = data.unit === "month" ? "month" : "week";
    var span = m.window;
    var legend = h("div", { class: "chart-legend" },
      name ? [h("span", { class: "k1" }), name.split(" ")[0] + (span ? ", " + windowName(m) + " average " : " ")] : null,
      name && span ? [h("span", { class: "k4" }), "each " + unit + " "] : null,
      h("span", { class: "k2" }), "Team average ", h("span", { class: "k3" }),
      "Other techs" + (m.unit === "minutes" ? " · minutes" : ""));
    var how = span
      ? "each point pools the " + span.count + " " + span.unit + "s up to it (all " + m.base + " together)"
      : "each point pools the " + unit + " (all " + m.base + " together)";
    return panel(m.label + ", " + unit + " by " + unit, trendSubtitle(data, data.period) + " · " + how, legend, box);
  }

  /* The likely range of a share of `n` (Wilson, 95%), as whole percents: "49–94%". */
  function likelyRange(percent, n) {
    var p = percent / 100, z = 1.96, d = 1 + z * z / n;
    var mid = (p + z * z / (2 * n)) / d, half = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d;
    return Math.round(100 * Math.max(0, mid - half)) + "–" + Math.round(100 * Math.min(1, mid + half)) + "%";
  }

  /* The decimals of a tech's line: a total's line averages its weeks' totals, so it keeps a decimal
     (`line_digits`); every other measure's are its own. */
  function lineDigits(m) {
    return m.line_digits == null ? m.digits : m.line_digits;
  }

  /* A measure's trailing window as the legend and tooltip name it: "8-week", "2-month"; "" when each point is its
     own step. */
  function windowName(m) {
    return m.window ? m.window.count + "-" + m.window.unit : "";
  }

  /* A step's days as a tooltip writes them: "Oct 4", "Oct 5–10", "Oct 28–Nov 3". */
  function stepDays(step) {
    if (step.start === step.end) {
      return dayLabel(step.start, false);
    }
    var sameMonth = step.start.slice(0, 7) === step.end.slice(0, 7);
    return dayLabel(step.start, false) + "–" + (sameMonth ? String(localDate(step.end).getDate()) : dayLabel(step.end, false));
  }

  /* "Week of Aug 30 · Jordan 4-week 7.2 (18 shifts), that week 6.9 (5) · Team 4-week 6.8 (90 shifts)", and
     " · part week (Oct 18–19)" on a step the period's edge cut short. Each step's own start and end tell apart two
     steps that share a label. */
  function trendTip(data, m, i, email) {
    var step = data.steps[i];
    var unit = data.unit === "month" ? "month" : "week";
    var index = -1;
    data.techs.forEach(function (t, k) {
      if (t.email === email) {
        index = k;
      }
    });
    var average = m.window ? ", " + windowName(m) + " average" : "";
    var rows = [];
    if (index >= 0) {
      var figs = m.techs[index];
      rows.push({ key: "line", label: data.techs[index].name.split(" ")[0] + average,
        value: trendValue(m, figs.rolling[i], lineDigits(m)), note: countNote(m, figs.rolling_n[i], figs.rolling[i]) });
      if (m.window) {
        rows.push({ key: "dot", label: "That " + unit + " alone", value: trendValue(m, figs.steps[i]),
          note: countNote(m, figs.steps_n[i], figs.steps[i]) });
      }
    }
    rows.push({ key: "team", label: "Team" + average, value: trendValue(m, m.team.rolling[i], m.team_digits),
      note: countNote(m, m.team.rolling_n[i], m.team.rolling[i]) });
    return { title: (unit === "month" ? "Month of " : "Week of ") + step.label,
      sub: stepDays(step) + (step.part ? " · part " + unit : ""), rows: rows,
      foot: m.window ? "The line pools every " + m.base.replace(/s$/, "") + " in the " + m.window.count + " " +
        m.window.unit + "s up to each point." : null };
  }

  /* What is behind a figure, for its hovercard row: "17 ratings · likely 60–94%" (the range for Satisfaction only),
     "31 calls"; "" with nothing. */
  function countNote(m, n, v) {
    if (!n) {
      return "";
    }
    return n + " " + (n === 1 ? m.base.replace(/s$/, "") : m.base) +
      (m.key === "satisfaction" && v != null ? " · likely " + likelyRange(v, n) : "");
  }

  var WHOLE_SCALE = ["satisfaction"];   // drawn from 0% to 100% (the owner, 2026-10-22); other percents are fitted

  function drawTrendsGraph(box, data, m, highlight) {
    Charts.highlightLines(box, {
      labels: data.steps.map(function (s) { return s.tick; }),
      sizes: data.steps.map(function (s) { return daysIn(s.start, s.end); }),
      shade: data.steps.map(function (s) { return s.in_period; }),
      part: data.steps.map(function (s) { return s.part; }),
      series: data.techs.map(function (t, i) {
        var figs = m.techs[i];
        return { key: t.email, name: t.name, values: figs.rolling, raw: m.window ? figs.steps : null };
      }),
      team: { name: "Team", values: m.team.rolling },
      highlight: highlight
    }, {
      format: function (v) { return m.unit === "minutes" ? v.toFixed(lineDigits(m)) : trendValue(m, v, lineDigits(m)); },
      teamFormat: function (v) { return m.unit === "minutes" ? v.toFixed(m.team_digits) : trendValue(m, v, m.team_digits); },
      kind: m.unit === "count" || m.unit === "percent" || m.unit === "duration" ? m.unit : "number",
      whole: WHOLE_SCALE.indexOf(m.key) >= 0,
      digits: m.digits,
      tooltip: function (i, email) { return trendTip(data, m, i, email); },
      ariaLabel: m.label + " by " + (data.unit === "month" ? "month" : "week"),
      empty: "Nothing recorded in these " + (data.unit === "month" ? "months" : "weeks")
    });
  }

  // -- 1:1 prep -----------------------------------------------------------------------------------
  //
  // Each tech's latest 1:1 prep (oneonone.report.latest_view): a row per tech on the team, in the sheet's column
  // order, with Copy row for the sheet. Every prep is counted from the collected data when the page asks for it, so
  // there is nothing to make or wait for: the page says when that data was last synced, as Activity does. Clicking a
  // tech opens their detail below the table (tech_view): their past periods, any of which can be picked, the lists
  // behind the picked period's numbers, and Trends over their last 1:1s (trends_view). The address keeps the tech, the
  // picked period and how many 1:1s Trends compares, when that isn't the default:
  // #/oneonone?tech=<email>&day=YYYY-MM-DD&n=26.

  var PREP_KEYS = ["tech", "day", "n"];
  var TREND_SIZES = [4, 8, 13, 26];   // Compare's choices: the last 4, 8, 13 or 26 1:1s
  var TREND_SIZE_DEFAULT = 8;
  var TREND_DIGITS = { share: 2 };    // % of calls keeps the sheet's two decimals; every other measure is whole
  var WEEKS_A_YEAR = 52;
  var TOAST_MS = 2600;       // how long a toast stays
  // The sheet's ten numbers in its order (oneonone.report.COLUMNS), headed as the main table and past periods head them.
  var PREP_COLUMNS = [
    { key: "calls", head: "Total calls" },
    { key: "share", head: "% of calls" },
    { key: "declined_missed", head: "Declined / missed" },
    { key: "dm_share", head: "% D/M" },
    { key: "talk", head: "Total call time" },
    { key: "avg", head: "Avg. call time" },
    { key: "solved", head: "Solved" },
    { key: "satisfaction", head: "Satisfaction" },
    { key: "aging", head: "Aging" },
    { key: "attendance", head: "Attendance" }
  ];
  var PREP_HINT = "Each tech's prep covers the 14 days before their own 1:1, counted from the data as of the last " +
    "update. % of calls is out of the department's answered inbound calls in that tech's two weeks. Copy row pastes " +
    "into the tech's sheet tab.";
  var NO_SCHEDULE = "No 1:1 date set";
  var NO_RATINGS = "No ratings this period";
  var NOT_COUNTED = "Not counted for this period.";
  var NO_TRENDS = "No 1:1 periods to show trends for yet";
  var COPY_REFUSED = "The browser didn't allow copying, so nothing was copied.";
  var TICKET_STATUS = { "new": "New", open: "Open", pending: "Pending", hold: "On-hold" };
  var DAY_OUT_KINDS = { pto: "PTO", sick: "Sick day" };   // as Team activity labels them
  // The screen's answers are kept as every screen's are (readKept): the table, the settings, and each tech detail and
  // Trends read, by address, so picking a period, a tech or a range draws the whole screen at once.
  var toastEl = null;
  var toastTimer = null;
  var prepOpening = 0;   // counts the tech, period and range changes asked for: only the latest one is opened

  function prepHash(params) {
    return "#/oneonone" + queryString(params);
  }

  function prepTechUrl(email, day) {
    return "/api/prep/tech" + queryString({ email: email, day: day });
  }

  /* How many 1:1s Trends compares: the address's `n` when it is one of Compare's choices, else the default. */
  function trendSize(params) {
    var n = Number(params.n);
    return TREND_SIZES.indexOf(n) >= 0 ? n : TREND_SIZE_DEFAULT;
  }

  function prepTrendsUrl(email, params) {
    return "/api/prep/trends" + queryString({ email: email, n: trendSize(params) });
  }

  /* What the address opens for its tech: their detail and their Trends. */
  function prepTechUrls(params) {
    return [prepTechUrl(params.tech, params.day), prepTrendsUrl(params.tech, params)];
  }

  /* Read the tech's detail and Trends the address opens, keeping them, unless they are kept already. */
  function loadPrepTech(params) {
    return Promise.all(prepTechUrls(params).map(function (url) {
      return isKept(url) ? null : getKept(url, readPrep);
    }));
  }

  /* A 1:1 prep read: a refusal (a 400, such as no prep for that period) is that address's answer, `{ refused }`, kept
     as any answer is. Any other failure fails, so the next try reads again. */
  function readPrep(url) {
    return readJson(url).catch(function (error) {
      if (error.status === 400) {
        return { refused: { status: error.status, message: error.message } };
      }
      throw error;
    });
  }

  /* An answer as the detail takes it: `{ view }`, or `{ error }` for a refusal. */
  function prepGot(answer) {
    return answer.refused ? { error: answer.refused } : { view: answer };
  }

  function renderPrep(token, query) {
    var params = paramsFrom(query, PREP_KEYS);
    loadingLine("Loading the 1:1 prep…");
    // The settings give ticket links and each tech's repeat.
    readScreen(token, ["/api/prep", "/api/settings"], function (got, drawToken) {
      showPrep(drawToken, params, { data: got[0], settings: got[1] });
    }).catch(function (error) {
      if (token !== renderToken) {
        return;
      }
      clearScreen();
      screen.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
    });
  }

  /* The whole screen from the answers in hand (`page`: the table's `data` and the `settings`): the heading with when
     the data was last synced, the table, and the open tech's detail. */
  function showPrep(token, params, page) {
    var data = page.data;
    // The address of a change; Trends' range goes with the tech, so closing the tech drops it.
    function nextParams(changes) {
      var next = Object.assign({}, params, changes);
      if (!next.tech) {
        next.n = null;
      }
      return next;
    }
    // Read what a change opens before changing the address, so the screen is drawn whole and keeps its scroll. While
    // it is read, the screen shows the shared loading look (markLoading: faded under the loading bar) and stays usable:
    // a later change replaces this one, which then opens nothing.
    function open(changes) {
      var next = nextParams(changes);
      var mine = ++prepOpening;
      function go() {
        if (token !== renderToken || mine !== prepOpening) {
          return;
        }
        if (location.hash === prepHash(next)) {
          endLoading();   // back to what is shown (Compare changed and changed back): nothing to draw
        } else {
          location.hash = prepHash(next);
        }
      }
      if (next.tech && prepHash(next) !== location.hash && !prepTechUrls(next).every(isKept)) {
        markLoading();
      }
      (next.tech ? loadPrepTech(next) : Promise.resolve()).then(go, go);
    }
    clearScreen();
    var row = data.rows.filter(function (r) { return r.email === params.tech; })[0] || null;
    // "Updated 9:42 AM", from the latest sync, as Activity says it (updatedText).
    screen.appendChild(h("div", { class: "head-line" }, h("h3", { class: "period-head", text: latestHead(data) }),
      h("span", { class: "message", role: "status", id: "pp-updated", text: updatedText(data.synced) + nextText(data.next_sync) })));
    screen.appendChild(prepTable(data, params.tech, open));
    if (params.tech) {
      screen.appendChild(prepDetail(token, params, page, row, open));
    } else if (data.rows.length) {
      screen.appendChild(h("p", { class: "note",
        text: "Click a tech for their past periods and the calls, tickets and days behind each number." }));
    }
  }

  /* "Each tech's latest 1:1 prep · 4 techs". */
  function latestHead(data) {
    return "Each tech's latest 1:1 prep · " + plural(data.rows.length, "tech", "techs");
  }

  /* Satisfaction with no ratings: blank, as the sheet leaves it (a dash means a count that failed, with its reason). */
  function noRatings(key, cell) {
    return key === "satisfaction" && cell.text == null && !cell.reason;
  }

  /* A number as a table shows it: the sheet text, a faint "blank" for no ratings, else a dash; and its title: why it
     is blank or dashed, or a note on it (the unreachable rings left out of Declined / missed). */
  function prepCellText(key, cell) {
    if (cell.text != null) {
      return { node: cell.text, title: cell.reason || null };
    }
    if (noRatings(key, cell)) {
      return { node: h("span", { class: "muted", text: "blank" }), title: NO_RATINGS };
    }
    return { node: "-", title: cell.reason || null };
  }

  function prepNumberCells(cells) {
    return PREP_COLUMNS.map(function (col) {
      var shown = prepCellText(col.key, cells[col.key]);
      return h("td", { class: "num nowrap", title: shown.title }, shown.node);
    });
  }

  /* A small pill beside a date in a table: Today, Tomorrow, In 2 days. */
  function cellPill(text, kind) {
    return h("span", { class: "pill cell-pill " + kind, text: text });
  }

  /* A tech's row: their name (click it for their detail, again to close it), their latest 1:1 day and its two weeks,
     the ten numbers, their next 1:1 and Copy row. A tech with no 1:1 date set has only that, linking to Settings
     (noSchedule). */
  function prepRow(row, tech, open) {
    var on = row.email === tech;
    function toggle() {
      open({ tech: on ? null : row.email, day: null });
    }
    var name = h("td", null, h("button", { type: "button", class: "rank-name", "aria-pressed": on ? "true" : "false",
      text: row.name, onclick: toggle }));
    var tr;
    if (row.state === "no_schedule") {
      tr = h("tr", { class: "rowlink" + (on ? " sel" : "") }, name, h("td", { colspan: "13" }, noSchedule()), h("td"));
    } else {
      tr = prepCountedRow(row, on, name);
    }
    // The whole row opens (or closes) the tech's detail; its buttons and links keep their own clicks.
    tr.addEventListener("click", function (event) {
      if (!event.target.closest("button, a")) {
        toggle();
      }
    });
    return tr;
  }

  /* "No 1:1 date set", for a tech with none: a link to Settings, where their 1:1 date is set, except for a manager,
     who sets none (each team lead does), so reads it as plain text. */
  function noSchedule() {
    return isManager() ? NO_SCHEDULE
      : h("a", { href: "#/settings", title: "Set their 1:1 date in Settings", text: NO_SCHEDULE });
  }

  function prepCountedRow(row, on, name) {
    return h("tr", { class: "rowlink" + (on ? " sel" : "") }, name,
      h("td", { class: "nowrap" }, row.label, row.today ? [" ", cellPill("Today", "today")] : null),
      h("td", { class: "nowrap muted", text: row.frame }),
      prepNumberCells(row.cells),
      h("td", { class: "nowrap" }, row.next_label, row.soon ? [" ", cellPill(row.soon, "soon")] : null),
      h("td", { class: "num" }, h("button", { type: "button", class: "small", text: "Copy row", onclick: function () {
        copyText(row.copy, "Copied " + row.name + "'s row. Paste it into their tab.");
      } })));
  }

  function prepTable(data, tech, open) {
    var counted = data.rows.filter(function (r) { return r.state === "counted"; });
    var copyAll = h("button", { type: "button", class: "small", text: "Copy all", disabled: !counted.length,
      onclick: function () {
        copyText(counted.map(function (r) { return r.copy; }).join("\n"),
          "Copied " + plural(counted.length, "row", "rows") + ", one per tech");
      } });
    return h("section", { class: "panel" },
      tableBox(h("table", { class: "atable", id: "pp-latest" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Tech" }), h("th", { scope: "col", text: "1:1 day" }),
          h("th", { scope: "col", text: "Time frame" }),
          PREP_COLUMNS.map(function (col) { return h("th", { scope: "col", class: "num", text: col.head }); }),
          h("th", { scope: "col", text: "Next 1:1" }), h("th", { scope: "col", class: "num" }, copyAll))),
        h("tbody", null, data.rows.map(function (row) { return prepRow(row, tech, open); })))),
      h("p", { class: "note", text: PREP_HINT }));
  }

  /* Put `text` on the clipboard, then say so in the toast (or that the browser refused). */
  function copyText(text, done) {
    var writing;
    try {
      writing = navigator.clipboard.writeText(text);
    } catch (e) {
      writing = Promise.reject(e);
    }
    Promise.resolve(writing).then(function () { toast(done); }, function () { toast(COPY_REFUSED); });
  }

  /* A short confirmation in the page's corner for a moment; one toast, reused. */
  function toast(text) {
    if (!toastEl) {
      toastEl = h("div", { class: "toast", role: "status", "aria-live": "polite" });
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.classList.toggle("show", true);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.toggle("show", false); }, TOAST_MS);
  }

  /* The tech's detail below the table: drawn from their detail and Trends kept (just after the box is put on the page,
     so the charts measure their real width), else read now and drawn when they come. */
  function prepDetail(token, params, page, row, open) {
    var urls = prepTechUrls(params);
    var box = h("div", null, urls.every(isKept) ? null : h("div", { class: "message", text: "Loading the tech's detail…" }));
    readKept(urls, function () { return token === renderToken; }, function (got) {
      var draws = [];
      box.textContent = "";
      append(box, prepDetailNodes(params, page, row, prepGot(got[0]), prepGot(got[1]), open, draws));
      draws.forEach(function (draw) { draw(); });
    }, readPrep).catch(function (error) {
      if (token === renderToken) {
        box.textContent = "";
        box.appendChild(h("div", { class: "message error", role: "alert", text: error.message }));
      }
    });
    return box;
  }

  /* The detail: its heading, the past periods, what's behind the picked one and Trends (`trends`, whose charts are
     put in `draws`). Opening a tech picks their latest period. A tech with no 1:1 date set gets "No 1:1 date set",
     linking to Settings (noSchedule). */
  function prepDetailNodes(params, page, row, got, trends, open, draws) {
    var view = got.view || null;
    var name = view ? view.name : row ? row.name : params.tech;
    var next = view ? view.next_label : row ? row.next_label : null;
    var nodes = [h("div", { class: "head-line" },
      h("h3", { class: "period-head", text: name + " · past periods" + (next ? " · next 1:1 " + next : "") }))];
    if (got.error && got.error.status === 400 && row && row.state === "no_schedule") {
      // No 1:1 date: what the table's row says, as it says it (noSchedule).
      nodes.push(h("div", { class: "placeholder", role: "status" }, noSchedule()));
      return nodes;
    }
    if (got.error) {
      nodes.push(h("div", { class: "message error", role: "alert", text: got.error.message }));
      return nodes;
    }
    nodes.push(pastPeriodsPanel(view, view.selected, open));
    nodes.push(h("h4", { class: "panel-label", text: "Behind the numbers · " + view.behind.label }));
    nodes.push(behindPanels(view, page.settings.zendesk_subdomain));
    var schedule = (page.settings.oneonone_schedule || {})[params.tech];
    nodes.push(trendsBlock(trends, name, schedule ? schedule.weeks : null, trendSize(params), open, draws));
    return nodes;
  }

  /* "Last 8 1:1s (4 months)": the span in brackets worked out from the tech's repeat (`weeks`), left off without one. */
  function trendSizeText(n, weeks) {
    var text = "Last " + n + " 1:1s";
    if (!weeks) {
      return text;
    }
    var months = Math.round(n * weeks * 12 / WEEKS_A_YEAR);
    var span = months % 12 === 0 ? plural(months / 12, "year", "years") : plural(months, "month", "months");
    return text + " (" + span + ")";
  }

  /* A Trends figure as its card and chart write it: m:ss for a time, a percent to the measure's decimals, a count
     whole (the team's, a mean per tech, to one decimal at most: "31.3", "33"). */
  function trendFigure(m, team) {
    var digits = TREND_DIGITS[m.key] || 0;
    if (m.unit === "seconds") {
      return Charts.minSec;
    }
    if (m.unit === "percent") {
      return function (v) { return v.toFixed(digits) + "%"; };
    }
    return function (v) { return team ? String(Number(v.toFixed(1))) : v.toFixed(0); };
  }

  /* A card's change against the average of the 1:1s before the latest: "▲ 4", "▼ 15 pts" or "▲ 0:50", better or
     worse by the measure's direction (flat when it has none), "no change" when it rounds to zero; null with nothing
     to compare. */
  function trendCardChange(m) {
    if (m.latest == null || m.change == null) {
      return null;
    }
    var digits = TREND_DIGITS[m.key] || 0;
    var size = Number(Math.abs(m.change).toFixed(digits));
    if (!size) {
      return h("span", { class: "mdelta flat", text: "no change" });
    }
    var text = m.unit === "seconds" ? Charts.minSec(size) : size.toFixed(digits) + (m.unit === "percent" ? " pts" : "");
    var cls = !m.better ? "flat" : (m.better === "higher") === (m.change > 0) ? "better" : "worse";
    return h("span", { class: "mdelta " + cls, text: (m.change > 0 ? "▲ " : "▼ ") + text });
  }

  /* Trends (spec §8), below Behind the numbers: Trends, Compare, the shared chart key and what the change is against
     on one line, then a small chart per measure, four to a row. They always end at the tech's latest 1:1. */
  function trendsBlock(got, name, weeks, size, open, draws) {
    var first = firstNameOf(name);
    var compare = filterSelect("pp-compare", "Compare", TREND_SIZES.map(function (n) {
      return { value: String(n), text: trendSizeText(n, weeks) };
    }), String(size), function (value) {
      open({ n: Number(value) === TREND_SIZE_DEFAULT ? null : value });
    });
    var view = got.view;
    var days = view ? view.days.length : 0;
    var head = h("div", { class: "trend-head" }, h("h4", { class: "panel-label", text: "Trends" }), compare,
      h("div", { class: "chart-legend" }, h("span", { class: "k1", "aria-hidden": "true" }), first,
        h("span", { class: "k2", "aria-hidden": "true" }), "Team average"),
      days > 1 ? h("span", { class: "trend-note",
        text: "Change: the latest 1:1 against the average of the " + (days - 1) + " before it" }) : null,
      // Fewer 1:1s than Compare asks for: the data starts later than that, so the graphs show every one there is.
      days && days < size ? h("span", { class: "trend-note", id: "pp-trend-fewer",
        text: "Only " + days + " 1:1" + (days === 1 ? "" : "s") + " with data so far (since " +
          dayLabel(view.days[0], view.days[0].slice(0, 4) !== view.days[days - 1].slice(0, 4)) + ")" }) : null);
    if (!view) {
      return h("div", { id: "pp-trends" }, head, h("div", { class: "message error", role: "alert", text: got.error.message }));
    }
    if (!days) {
      return h("div", { id: "pp-trends" }, head, h("div", { class: "placeholder", role: "status", text: NO_TRENDS }));
    }
    var grid = h("div", { class: "trend-grid" });
    view.measures.forEach(function (m) {
      var format = trendFigure(m, false);
      // A count's line averages its periods' counts, so it keeps a decimal, as the team's figure does.
      var lineFormat = m.rolling && m.unit === "count" ? trendFigure(m, true) : format;
      var chart = h("div", { class: "chart-box" });
      // On a touch screen with the cards side by side (CSS shows it then), the button enlarges the card, since a tap
      // on the card reads its chart instead (iPad and phone design §10.2).
      var grow = h("button", { type: "button", class: "small trend-grow", "aria-pressed": "false", text: "Enlarge" });
      var card = h("section", { class: "panel trend-card", title: "Click to enlarge" },
        h("h4", { class: "panel-label", text: m.label + (m.window_weeks ? " · " + m.window_weeks + "-week average" : "") }),
        h("div", { class: "trend-figs" }, h("span", { class: "trend-now", text: m.latest == null ? "-" : format(m.latest) }),
          trendCardChange(m), grow),
        chart);
      draws.push(function () {
        Charts.trendLine(chart, { values: m.rolling || m.values, raw: m.rolling ? m.values : null,
          team: m.team_rolling || m.team, days: view.days, unit: m.unit,
          scaleMax: m.unit === "percent" ? 100 : null, digits: TREND_DIGITS[m.key] || 0, format: lineFormat,
          teamFormat: trendFigure(m, true), ariaLabel: first + "'s " + m.label + " by 1:1", endLabel: true,
          // Big only while the cards sit side by side: one to a row, a card is drawn at its own height (§10.2).
          height: function () {
            return card.classList.contains("big") && trendColumns(grid) > 1 ? BIG_TREND_HEIGHT : null;
          },
          tooltip: function (k) { return prepTrendTip(m, view.days[k], k, first, lineFormat, format); } });
      });
      // The card keeps what pressed it last: a tap is for reading values, so only a mouse (or pen) click enlarges it,
      // and only when the cards sit side by side (one to a row it is already full width); a big one still shrinks.
      card.addEventListener("pointerdown", function (event) { card._pointer = event.pointerType; });
      card.addEventListener("click", function (event) {
        if (!event.target.closest("button") && card._pointer !== "touch" &&
            (trendColumns(grid) > 1 || card.classList.contains("big"))) {
          growTrendCard(grid, card, chart);
        }
      });
      grow.addEventListener("click", function () { growTrendCard(grid, card, chart); });
      grid.appendChild(card);
    });
    return h("div", { id: "pp-trends" }, head, grid);
  }

  var BIG_TREND_HEIGHT = 380;   // a clicked 1:1 trend's chart: its card is two rows tall

  var PREP_BASES = { satisfaction: ["rating", "ratings"], avg: ["call", "calls"], share: ["queue call", "queue calls"] };

  /* A 1:1 trend's hovercard: the 1:1 and the days its prep covers, the tech's pooled figure with what is behind it,
     that 1:1's own, and the team's. */
  function prepTrendTip(m, day, k, first, format, own) {
    var start = new Date(localDate(day).getTime() - 14 * 864e5), end = new Date(localDate(day).getTime() - 864e5);
    function iso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0"); }
    var average = m.window_weeks ? ", " + m.window_weeks + "-week average" : "";
    var v = m.rolling ? m.rolling[k] : m.values[k];
    var n = m.rolling_n ? m.rolling_n[k] : 0;
    var base = PREP_BASES[m.key] || ["1:1", "1:1s"];
    var note = n ? (PREP_BASES[m.key] ? "" : "over ") + n + " " + (n === 1 ? base[0] : base[1]) +
      (m.key === "satisfaction" && v != null ? " · likely " + likelyRange(v, n) : "") : "";
    var rows = [{ key: "line", label: first + average, value: v == null ? "-" : format(v), note: note }];
    if (m.rolling) {
      rows.push({ key: "dot", label: "That 1:1 alone", value: m.values[k] == null ? "-" : own(m.values[k]), note: "" });
    }
    var team = m.team_rolling ? m.team_rolling[k] : m.team[k];
    rows.push({ key: "team", label: "Team" + average, value: team == null ? "-" : trendFigure(m, true)(team), note: "" });
    return { title: "1:1 " + dayLabel(day, false), sub: "Covers " + dayLabel(iso(start), false) + " – " +
      dayLabel(iso(end), false), rows: rows };
  }

  /* How many columns the trend grid has now (its computed tracks, one per column). */
  function trendColumns(grid) {
    return getComputedStyle(grid).gridTemplateColumns.split(" ").length;
  }

  /* Click a 1:1 trend: it takes two columns and two rows from where it sits (moved left when it is in the last
     column), the others glide to their new places, and its chart is drawn again at its new size; clicking it again
     puts it back, and clicking another swaps which one is big. Each card's Enlarge button says which it would do. */
  function growTrendCard(grid, card, chart) {
    var cards = Array.prototype.slice.call(grid.children);
    var before = cards.map(function (c) { return c.getBoundingClientRect(); });
    var columns = trendColumns(grid);
    var wasBig = card.classList.contains("big");
    shrinkTrendCards(grid);
    if (!wasBig && columns > 1) {
      var at = cards.indexOf(card);
      var column = Math.min(at % columns, columns - 2);
      card.classList.add("big");
      card.style.gridColumn = (column + 1) + " / span 2";
      card.style.gridRow = (Math.floor(at / columns) + 1) + " / span 2";
      var grow = card.querySelector("button.trend-grow");
      grow.textContent = "Shrink";
      grow.setAttribute("aria-pressed", "true");
    }
    cards.forEach(function (c, i) {
      var after = c.getBoundingClientRect();
      var dx = before[i].left - after.left, dy = before[i].top - after.top;
      if ((dx || dy || before[i].width !== after.width) && c.animate) {
        c.animate([{ transform: "translate(" + dx + "px, " + dy + "px)", transformOrigin: "top left" },
          { transform: "none", transformOrigin: "top left" }], { duration: 320, easing: "cubic-bezier(.2, .8, .2, 1)" });
      }
    });
    if (card.classList.contains("big") || wasBig) {
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  /* Every 1:1 trend in `grid` at its own size and place, its button saying Enlarge. */
  function shrinkTrendCards(grid) {
    Array.prototype.forEach.call(grid.children, function (c) {
      c.classList.remove("big");
      c.style.gridColumn = "";
      c.style.gridRow = "";
      var grow = c.querySelector("button.trend-grow");
      grow.textContent = "Enlarge";
      grow.setAttribute("aria-pressed", "false");
    });
  }

  /* When the page narrows to one trend column (SINGLE_COLUMN, where app.css stacks the trend grid: an iPad turned
     upright, a window made narrow), a big 1:1 trend goes back: one card to a row is already full width, and there a
     tap reads its chart and the Enlarge button is hidden, so nothing could shrink it (iPad and phone design §10.2). */
  function watchTrendColumns() {
    window.matchMedia(SINGLE_COLUMN).addEventListener("change", function (event) {
      if (event.matches) {
        document.querySelectorAll(".trend-grid").forEach(shrinkTrendCards);
      }
    });
  }

  /* One row per period the tech has a prep in, newest first, as their sheet tab has them; the picked one is lit, and
     clicking a row picks it. */
  function pastPeriodsPanel(view, day, open) {
    var rows = view.past.map(function (p) {
      var on = p.day === day;
      function pick() {
        if (!on) {
          open({ day: p.day });
        }
      }
      var tr = h("tr", { class: "rowlink" + (on ? " sel" : "") },
        h("td", null, h("button", { type: "button", class: "rank-name", "aria-pressed": on ? "true" : "false",
          text: p.sheet_day, onclick: pick })),
        h("td", { class: "nowrap", text: p.frame }),
        prepNumberCells(p.cells));
      tr.addEventListener("click", function (event) {
        if (!event.target.closest("button")) {
          pick();
        }
      });
      return tr;
    });
    return h("section", { class: "panel" },
      h("div", { class: "sub", text: "Click a period to see what's behind its numbers" }),
      tableBox(h("table", { class: "atable", id: "pp-past" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "Date" }), h("th", { scope: "col", text: "Data time frame" }),
          PREP_COLUMNS.map(function (col) { return h("th", { scope: "col", class: "num", text: col.head }); }))),
        h("tbody", null, rows)), { list: true, first: 4, noun: "periods" }));
  }

  /* The four lists behind the picked period's numbers, side by side, Days out narrower: answered calls (non-urgent
     calls over the limit lit), satisfaction ratings, aging tickets (or why they weren't counted) and days out. A list
     whose number failed says why. */
  function behindPanels(view, subdomain) {
    var b = view.behind;
    var here = view.past.filter(function (p) { return p.day === view.selected; })[0];
    function failed(key) {
      var cell = here ? here.cells[key] : null;
      if (!cell || cell.text != null || noRatings(key, cell)) {
        return null;
      }
      return cell.reason || NOT_COUNTED;
    }
    function ticket(id) {
      return h("td", null, id == null ? "-" : ticketRef(id, subdomain));
    }
    return h("div", { class: "panel-row cols-4 last-narrow stretch", id: "pp-behind" },
      answeredPanel(b, failed("calls"), ticket),
      ratingsPanel(b, failed("satisfaction"), subdomain),
      agingPanel(b, ticket),
      daysOutPanel(b, failed("attendance")));
  }

  /* A list's table, for a tableBox that scrolls it inside its panel under headers that stay in place. */
  function listTable(cls, heads, rows) {
    return h("table", { class: "atable" + (cls ? " " + cls : "") },
      h("thead", null, h("tr", null, heads.map(function (head) {
        return h("th", { scope: "col", class: head.num ? "num" : null, text: head.text });
      }))),
      h("tbody", null, rows));
  }

  function placeholder(text) {
    return h("div", { class: "placeholder", text: text });
  }

  function answeredPanel(b, reason, ticket) {
    if (reason) {
      return panel("Answered calls", null, placeholder(reason));
    }
    var over = "Non-urgent call over " + b.limit + " minutes";
    var key = b.over_count ? h("div", { class: "flag-key",
      text: plural(b.over_count, "non-urgent call", "non-urgent calls") + " over " + b.limit + " min, highlighted" }) : null;
    return panel("Answered calls · " + b.answered.length, null, key, b.answered.length
      ? tableBox(listTable("tight", [{ text: "Day" }, { text: "Ticket" }, { text: "Talk", num: true }, { text: "Priority" }],
        b.answered.map(function (c) {
          return h("tr", { class: c.over ? "flagrow over-limit" : null, title: c.over ? over : null },
            h("td", { text: c.day }), ticket(c.ticket_id), h("td", { class: "num", text: c.talk }),
            h("td", { text: c.priority ? capitalize(c.priority) : "-" }));
        })), { list: true, plain: true, first: 10, noun: "calls" })
      : placeholder("None"));
  }

  function ratingsPanel(b, reason, subdomain) {
    if (reason) {
      return panel("Satisfaction ratings", null, placeholder(reason));
    }
    return panel("Satisfaction ratings · " + b.ratings.length, null, b.ratings.length
      ? tableBox(h("div", null, b.ratings.map(function (r) { return ratingItem(r, subdomain); })),
        { list: true, plain: true, first: 10, noun: "ratings" })
      : placeholder(NO_RATINGS));
  }

  /* One rating: its day, ticket and Good or Bad, then Zendesk's reason when there is one, then the customer's comment. */
  function ratingItem(r, subdomain) {
    var good = r.score === "good";
    return h("div", { class: "rating-item" },
      h("div", { class: "rating-line" }, h("span", { class: "rating-day", text: r.day }),
        r.ticket_id == null ? "-" : ticketRef(r.ticket_id, subdomain),
        h("span", { class: "chip " + (good ? "good" : "bad"), text: good ? "▲ Good" : "▼ Bad" })),
      r.reason ? h("div", { class: "rating-note", text: "Reason: " + r.reason }) : null,
      r.comment ? h("div", { class: "rating-say", text: "“" + r.comment + "”" })
        : h("div", { class: "rating-note", text: "No comment" }));
  }

  /* An aging ticket's last reply by its tech: "10/16 · 4d ago" counted from the 1:1 day, "10/20 · today", or Never. */
  function lastReplyCell(t) {
    if (t.last_reply == null) {
      return h("td", { class: "muted", text: "Never" });
    }
    return h("td", { class: "nowrap", text: t.last_reply + " · " + (t.reply_days_ago === 0 ? "today" : t.reply_days_ago + "d ago") });
  }

  function agingPanel(b, ticket) {
    if (b.aging == null) {
      return panel("Aging tickets", null, placeholder(b.aging_reason || NOT_COUNTED));
    }
    return panel("Aging tickets · " + b.aging.length, null, b.aging.length
      ? tableBox(listTable(null, [{ text: "Ticket" }, { text: "Age" }, { text: "Status" }, { text: "Last reply" }],
        b.aging.map(function (t) {
          return h("tr", null, ticket(t.ticket_id), h("td", { class: "nowrap", text: plural(t.age_days, "day", "days") }),
            h("td", { text: TICKET_STATUS[t.status] || capitalize(t.status) }), lastReplyCell(t));
        })), { list: true, plain: true, first: 10, noun: "tickets" })
      : placeholder("None"));
  }

  function daysOutPanel(b, reason) {
    if (reason) {
      return panel("Days out", null, placeholder(reason));
    }
    return panel("Days out · " + b.days_out.length, null, b.days_out.length
      ? tableBox(listTable(null, [{ text: "Day" }, { text: "From Team activity" }], b.days_out.map(function (d) {
        return h("tr", null, h("td", { text: d.day }), h("td", { text: DAY_OUT_KINDS[d.kind] || d.kind }));
      })), { list: true, plain: true, first: 10, noun: "days" })
      : placeholder("No PTO or sick days"));
  }

  // -- start ------------------------------------------------------------------------------------

  // The Call QA and Team links start hidden (index.html) until the "Show Call QA", "Show Team activity" and "Show 1:1
  // Prep" settings and the edition are read, so a hidden section's links never flash; the first route waits for them
  // too, and for the PC's "Sync Team activity" setting. The hosted edition drops the Call QA links, reads who is
  // signed in (the admin, a team lead who sees only their techs, or a manager, with the regions they picked, and
  // then a manager's people) and opens to Team activity. If a first read
  // fails (the hosted copy's sign-in refused, someone it doesn't know yet, the app down), both sections stay hidden,
  // nothing routes or polls the run status, and the page says why.
  setSections(false);
  var startError = null;
  getKept("/api/settings").then(function (settings) {
    edition = settings.edition || "full";
    document.getElementById("demo-badge").hidden = !settings.demo;
    if (!hostedOnly()) {
      showTeam = settings.show_team_activity !== false;
      showOneonone = settings.show_oneonone !== false;
      syncOn = settings.activity_sync_enabled !== false;
      return settings.show_call_qa !== false;
    }
    return api("GET", "/api/me").then(function (me) {
      role = me.role;
      myEmail = me.email;
      myRegions = me.regions || [];
      allRegions = me.all_regions || [];
      // A manager's teams, kept as Settings reads them, so Settings draws its people at once from this read.
      return isManager() ? getKept("/api/people").then(function (got) {
        teamBook = got;
        return false;
      }) : false;
    });
  }).catch(function (error) {
    startError = error;
    return false;
  }).then(function (on) {
    if (startError) {
      clearScreen();
      if (startError.data && startError.data.error === "not_added") {
        // Not an error: the one page someone the admin hasn't added yet sees.
        screen.appendChild(h("div", { class: "placeholder", role: "status", text: startError.message }));
        return;
      }
      screen.appendChild(h("div", { class: "message error", role: "alert",
        text: startError.status ? startError.message : "The app couldn't be reached. Reload the page to try again." }));
      return;
    }
    if (hostedOnly()) {
      dropCallQaLinks();
    }
    setSections(on);
    setTeamSection(showTeam);
    sectionsKnown = true;
    if (!location.hash) {
      history.replaceState(null, "", homeHash());
    }
    route();
  });
  wireSidePanel();
  watchTrendColumns();
  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", function (event) {
    if (rubricDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.setInterval(refreshBanners, STATUS_POLL_MS);
})();
