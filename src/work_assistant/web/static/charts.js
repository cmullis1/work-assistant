/* Charts for Work Assistant: plain inline SVG, drawn at the element's real width so text stays at its
   true size from the narrow side pane to a desktop window, and redrawn when that width changes.

   Rules every chart keeps (they follow the approved mockup and the dataviz method):
   - one y-scale per chart; a recessive grid; thin marks with rounded data-ends anchored to the baseline;
   - hover tooltips via <title> on hit areas larger than the marks;
   - a legend when there are two series, plus a direct label on each series' latest point; a single
     series has no legend box (the panel title names it);
   - text uses the ink tokens, never the series color;
   - colors come only from classes styled in app.css (no color values here);
   - steps with entries in `opts.markers` get a thin vertical marker, whose tooltip lists each change in
     the step's own words: "Rubric changed this day", "Model changed this week", "… this month", or both;
   - `opts.shade` marks the steps of the chosen period, drawn as one background band when the chart also
     shows context steps. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var MARKER_TEXT = {
    rubric: { day: "Rubric changed this day", week: "Rubric changed this week", month: "Rubric changed this month" },
    model: { day: "Model changed this day", week: "Model changed this week", month: "Model changed this month" }
  };
  var DEFAULT_WIDTH = 520;
  var HEIGHT = 200;
  var M = { top: 22, right: 18, bottom: 26, left: 38 };
  var MIN_LABEL_GAP = 50;      // px between x-axis labels before some are skipped
  var BAR_MAX = 24;            // px, the thickest a column gets
  var RADIUS = 4;              // px, rounded data-end
  var SPARK_LABEL_GAP = 6;     // px between a sparkline's lone point and its value label
  var NICE_STEPS = [1, 2, 2.5, 5, 10, 20, 25, 50];
  var TRENDS_HEIGHT = 360;     // px, the Trends graph: taller than the small charts, it carries every tech
  var END_ROOM = 104;          // px right of the Trends plot, where the two end labels sit
  var END_LABEL_GAP = 14;      // px, the least distance between the two end labels
  // px: a Trends graph narrower than this (a phone; an iPad's is wider) draws its narrow form: the plot across the whole
  // width with no end labels, as tall as 0.8 of the width (230 to 300px), and the latest figures above it (readout).
  var TRENDS_NARROW = 560;
  var TREND_CARD_HEIGHT = 130; // px, a 1:1 trend: one of a grid of small charts
  var TM = { top: 10, right: 12, bottom: 22, left: 48 };   // a 1:1 trend's margins: room for "10:30" and "7.50%"
  var TREND_TIME_STEP = 30;    // seconds: a time scale ends on whole half minutes
  // Seconds: the steps a duration axis (m:ss) picks from: 1, 2, 5, 10, 20 and 30 minutes, then hours.
  var MINUTE_STEPS = [60, 120, 300, 600, 1200, 1800, 3600, 7200, 18000, 36000];
  var MINUTES_AT_LEAST = 180;  // seconds: a whole-minute axis shorter than 3 minutes steps every 30 seconds instead
  var TREND_SMALL_DOTS = 13;   // past this many 1:1s, the dots get smaller
  var TOUCH_GAP = 36;          // px between a finger and the hovercard above it, clear of the fingertip

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
  }

  // -- building blocks ------------------------------------------------------------------------------

  function s(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    var key;
    attrs = attrs || {};
    for (key in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, key) || attrs[key] == null) {
        continue;
      }
      if (key === "text") {
        el.textContent = attrs[key];
      } else {
        el.setAttribute(key, String(attrs[key]));
      }
    }
    for (var i = 2; i < arguments.length; i++) {
      if (arguments[i]) {
        el.appendChild(arguments[i]);
      }
    }
    return el;
  }

  /* A chart mark's hovercard (the owner, 2026-10-22: a card of the app's own, not the browser's tooltip). `content`
     is its text, a line each (the first its title), or { title, sub, rows: [{ key ("line", "dot", "team" or none:
     the mark it names), label, value, note }], items: [{ kind, head, text }] (a list of things: a day's actions, each
     its words and its ticket, keyed by kind as the day chart draws it), foot }. It rides on the mark as a <desc>, which screen readers read,
     and the page's one card (`hovercard`) shows it while the pointer is over the mark. */
  function tip(content) {
    var d = s("desc", { class: "hover-data", text: plainText(content) });
    d._hover = content;
    return d;
  }

  function plainText(content) {
    if (typeof content === "string") {
      return content;
    }
    return [content.title, content.sub].concat((content.rows || []).map(function (r) {
      return r.label + " " + r.value + (r.note ? " (" + r.note + ")" : "");
    }), (content.items || []).map(function (i) {
      return i.head + (i.text ? " · " + i.text : "");
    }), [content.foot]).filter(Boolean).join("\n");
  }

  var card = null;   // the page's one hovercard, made on first use

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    e.className = cls;
    if (text != null) {
      e.textContent = text;
    }
    return e;
  }

  function fillCard(content) {
    card.textContent = "";
    if (typeof content === "string") {
      content.split("\n").forEach(function (line, k) { card.appendChild(el("div", k ? "hv-line" : "hv-title", line)); });
      return;
    }
    card.appendChild(el("div", "hv-title", content.title));
    if (content.sub) {
      card.appendChild(el("div", "hv-sub", content.sub));
    }
    if (content.rows && content.rows.length) {
      var rows = el("div", "hv-rows");
      content.rows.forEach(function (r) {
        rows.appendChild(el("span", "hv-key" + (r.key ? " " + r.key : "")));
        rows.appendChild(el("span", "hv-label", r.label));
        rows.appendChild(el("span", "hv-value", r.value));
        rows.appendChild(el("span", "hv-note", r.note || ""));
      });
      card.appendChild(rows);
    }
    (content.items || []).forEach(function (i) {
      var item = el("div", "hv-item");
      item.appendChild(el("span", "hv-key " + (i.kind || "act")));
      var words = el("div", "hv-item-words");
      words.appendChild(el("div", "hv-item-head", i.head));
      if (i.text) {
        words.appendChild(el("div", "hv-item-text", i.text));
      }
      item.appendChild(words);
      card.appendChild(item);
    });
    if (content.foot) {
      card.appendChild(el("div", "hv-foot", content.foot));
    }
  }

  /* The mark under the pointer that has a hovercard: the nearest of `node` and its parents with one. */
  function hoverData(node) {
    for (var n = node; n && n.nodeType === 1; n = n.parentNode) {
      for (var c = n.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 1 && c.getAttribute && c.getAttribute("class") === "hover-data") {
          return c;
        }
      }
      if (n.tagName && n.tagName.toLowerCase() === "svg") {
        return null;
      }
    }
    return null;
  }

  var lit = null;   // the mark a finger last read: its `g.hit`, classed `on` so it shows as a hovered one does

  function light(hit) {
    if (lit && lit !== hit) {
      lit.classList.toggle("on", false);
    }
    lit = hit && hit.classList.contains("hit") ? hit : null;
    if (lit) {
      lit.classList.toggle("on", true);
    }
  }

  function hideCard() {
    if (card) {
      card.hidden = true;
    }
    light(null);
  }

  /* The hovercard of the mark under the pointer. A mouse's sits beside the pointer and hides off the marks. A finger
     (iPad and phone design §10.1) reads the mark under it, found by its place since a touch keeps its first target:
     the card floats TOUCH_GAP above the finger, centred on it and kept 8px inside the window's sides (below the finger
     when there is no room above), and the mark is lit. It stays when the finger lifts or slides off the marks, until
     a touch away from them, a scroll or another screen. */
  function showCard(event) {
    var touch = event.pointerType === "touch";
    var data = hoverData(touch ? document.elementFromPoint(event.clientX, event.clientY) : event.target);
    if (!data) {
      if (!touch || event.type === "pointerdown") {
        hideCard();
      }
      return;
    }
    if (!card) {
      card = el("div", "hovercard");
      card.setAttribute("aria-hidden", "true");
      document.body.appendChild(card);
    }
    if (card._shown !== data) {
      fillCard(data._hover);
      card._shown = data;
    }
    card.hidden = false;
    if (touch) {
      light(data.parentNode);
      card.style.left = "0px";   // measured with the window's whole width to grow into, not squeezed where it last was
      var cw = card.offsetWidth, ch = card.offsetHeight;
      var above = event.clientY - TOUCH_GAP - ch;
      card.style.left = Math.max(8, Math.min(event.clientX - cw / 2, window.innerWidth - 8 - cw)) + "px";
      card.style.top = (above < 8 ? event.clientY + TOUCH_GAP : above) + "px";
      return;
    }
    light(null);
    var gap = 16, w = card.offsetWidth, h = card.offsetHeight;
    var x = event.clientX + gap, y = event.clientY + gap;
    if (x + w > window.innerWidth - 8) {
      x = event.clientX - gap - w;
    }
    if (y + h > window.innerHeight - 8) {
      y = event.clientY - gap - h;
    }
    card.style.left = Math.max(8, x) + "px";
    card.style.top = Math.max(8, y) + "px";
  }

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("pointermove", showCard);
    document.addEventListener("pointerdown", function (event) {
      if (event.pointerType === "touch") {
        showCard(event);
      }
    });
    document.addEventListener("scroll", hideCard, true);
    // A card a finger left up belongs to the screen it was read on: moving to another screen puts it away.
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("hashchange", hideCard);
    }
  }

  function r1(n) {
    return Math.round(n * 10) / 10;
  }

  function svgRoot(width, height, label, extraClass) {
    var svg = s("svg", {
      class: "chart" + (extraClass ? " " + extraClass : ""),
      width: width, height: height, viewBox: "0 0 " + width + " " + height
    });
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label || "");
    return svg;
  }

  function present(values) {
    return values.filter(function (v) { return v != null; });
  }

  /* Draw now if the element has a width, and again whenever its width changes. A resize redraws on the next
     frame, not inside the observer: the redraw changes the element's height (the old chart had scaled with
     the width), and a size change made inside the observer is reported as a ResizeObserver loop error. */
  function mount(el, draw) {
    if (el._chartObserver) {
      el._chartObserver.disconnect();
    }
    var lastWidth = -1;
    var pending = false;
    function redraw() {
      pending = false;
      var width = Math.floor(el.clientWidth) || DEFAULT_WIDTH;
      if (width === lastWidth) {
        return;
      }
      lastWidth = width;
      el.textContent = "";
      draw(width);
    }
    redraw();
    if (typeof ResizeObserver === "function") {
      el._chartObserver = new ResizeObserver(function () {
        if (!pending) {
          pending = true;
          requestAnimationFrame(redraw);
        }
      });
      el._chartObserver.observe(el);
    }
  }

  /* The tick steps niceScale picks from: NICE_STEPS, plus, when `fine`, finer ones below 1 (0.000001 up to
     0.5) for ranges too narrow for a step of 1. With `integer`, only whole-number steps. */
  function niceSteps(integer, fine) {
    var steps = [];
    for (var e = fine ? -6 : 0; e <= -1; e++) {
      NICE_STEPS.forEach(function (b) {
        var v = b * Math.pow(10, e);
        if (v < 1 - 1e-12) {
          steps.push(v);
        }
      });
    }
    steps.sort(function (a, b) { return a - b; });
    steps = steps.concat(NICE_STEPS);
    return integer ? steps.filter(function (v) { return v >= 1 && Math.floor(v) === v; }) : steps;
  }

  /* A y-domain with clean tick steps. `opts.min`/`opts.max` clamp it (0 and 100 for percents);
     `opts.zero` anchors it at the minimum (bars always start at the baseline). `opts.pad` (a fraction of the
     data's range, default 0.15) and `opts.minPad` (an absolute amount, default 2) set the room kept around
     the data; `opts.integer` puts every tick on a whole number. A caller that sets `opts.minPad` accepts
     ranges under 4 and gets finer steps for them (0.01, 0.02, ...), each a whole multiple of minPad so a
     tick never needs more decimals than the measure shows; every other call picks from NICE_STEPS
     alone, as it always has. `opts.steps` replaces the steps picked from (a duration's: durationScale). */
  function niceScale(values, opts) {
    var lo = Math.min.apply(null, values);
    var hi = Math.max.apply(null, values);
    var floor = opts.min != null ? opts.min : -Infinity;
    var ceil = opts.max != null ? opts.max : Infinity;
    if (opts.zero) {
      lo = floor === -Infinity ? 0 : floor;
    } else {
      var pad = Math.max((hi - lo) * (opts.pad != null ? opts.pad : 0.15), opts.minPad != null ? opts.minPad : 2);
      lo -= pad;
      hi += pad;
    }
    if (hi <= lo) {
      hi = lo + 1;
    }
    var fine = opts.minPad != null;
    var steps = opts.steps || niceSteps(opts.integer, fine);
    if (fine) {
      // Never a step finer than the room asked for: identical values must not tick 99, 99.5, 100. And only
      // whole multiples of it, so every tick shows exactly at the measure's precision (no 2.5 read as "3").
      var coarse = steps.filter(function (v) {
        var k = v / opts.minPad;
        return v >= opts.minPad - 1e-12 && Math.abs(k - Math.round(k)) < 1e-9;
      });
      steps = coarse.length ? coarse : steps;
    }
    var tolerance = fine ? 1e-9 : 0;   // the fine-step path absorbs floating-point noise; every other caller is unchanged
    var step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) {
      if ((hi - lo) / steps[i] <= 4 + tolerance) {
        step = steps[i];
        break;
      }
    }
    lo = Math.max(floor, Math.floor(lo / step + tolerance) * step);
    hi = Math.min(ceil, Math.ceil(hi / step - tolerance) * step);
    if (hi <= lo) {
      hi = lo + step;
    }
    var ticks = [];
    if (fine) {
      for (var t = lo, k = 0; t <= hi + 1e-9; k++, t = lo + k * step) {
        ticks.push(step < 1 ? Math.round(t * 1e6) / 1e6 : r1(t));
      }
    } else {
      for (var u = lo; u <= hi + 1e-9; u += step) {
        ticks.push(r1(u));
      }
    }
    return { lo: lo, hi: hi, ticks: ticks };
  }

  /* A y-domain for seconds read as m:ss, with niceScale's room around the data and never below 0: ticks on whole
     minutes (MINUTE_STEPS: "2:00, 3:00, 4:00"), or every 30 seconds when whole minutes would leave the axis under
     MINUTES_AT_LEAST (a few seconds' spread would otherwise get two ticks a minute apart). */
  function durationScale(values) {
    var opts = { pad: 0.12, minPad: 1, min: 0 };
    var scale = niceScale(values, Object.assign({ steps: MINUTE_STEPS }, opts));
    return scale.hi - scale.lo < MINUTES_AT_LEAST ? niceScale(values, Object.assign({ steps: [30] }, opts)) : scale;
  }

  function frame(svg, width, height, scale, format) {
    var y = function (v) { return M.top + (scale.hi - v) / (scale.hi - scale.lo) * (height - M.top - M.bottom); };
    scale.ticks.forEach(function (t, i) {
      svg.appendChild(s("line", { x1: M.left, x2: width - M.right, y1: y(t), y2: y(t), class: i === 0 ? "base" : "grid" }));
      svg.appendChild(s("text", { x: M.left - 6, y: y(t) + 4, class: "tick", "text-anchor": "end", text: format(t) }));
    });
    return y;
  }

  function xLabels(svg, xs, labels, height) {
    if (!labels) {
      return;
    }
    var gap = xs.length > 1 ? xs[1] - xs[0] : Infinity;
    var every = Math.max(1, Math.ceil(MIN_LABEL_GAP / gap));
    xs.forEach(function (x, i) {
      if ((xs.length - 1 - i) % every === 0) {
        svg.appendChild(s("text", { x: x, y: height - 8, class: "tick", "text-anchor": "middle", text: labels[i] }));
      }
    });
  }

  /* The marker labels for one trend step from the API, whose `unit` is "day", "week" or "month": the
     rubric change, the model change, both or neither. */
  function stepMarkers(step, unit) {
    var labels = [];
    if (step.rubric_changed) {
      labels.push(MARKER_TEXT.rubric[unit]);
    }
    if (step.model_changed) {
      labels.push(MARKER_TEXT.model[unit]);
    }
    return labels;
  }

  /* The chosen period's background band: one rect from the first to the last shaded step, each step owning
     a `slot` px wide around its x, kept inside the plot. Only drawn when some steps are context (not
     shaded): a band over every step would tell nothing. */
  function periodBand(svg, xs, shade, slot, width, height) {
    if (!shade || !shade.some(Boolean) || shade.every(Boolean)) {
      return;
    }
    var x0 = Math.max(M.left, xs[shade.indexOf(true)] - slot / 2);
    var x1 = Math.min(width - M.right, xs[shade.lastIndexOf(true)] + slot / 2);
    svg.appendChild(s("rect", { x: r1(x0), y: M.top, width: r1(x1 - x0), height: height - M.top - M.bottom,
      class: "period-band" }));
  }

  /* Change markers: one thin vertical line through the plot per marked step, with its tooltip (each
     change, one per line) on a hit area in the top margin. The plot itself belongs to the per-step
     tooltips, which also list the changes. `marks[i]` is step i's list of labels. */
  function markers(svg, xs, marks, height) {
    (marks || []).forEach(function (labels, i) {
      if (!labels || !labels.length) {
        return;
      }
      var x = xs[i];
      svg.appendChild(s("g", { class: "marker-hit" },
        s("line", { x1: x, x2: x, y1: M.top - 10, y2: height - M.bottom, class: "marker" }),
        s("path", { d: "M" + (x - 4) + "," + (M.top - 14) + "h8l-4,5z", class: "marker-flag" }),
        s("rect", { x: x - 8, y: 0, width: 16, height: M.top, class: "hitarea" }),
        tip(labels.join("\n"))));
    });
  }

  function withMarker(text, marks, i) {
    var labels = marks && marks[i];
    return labels && labels.length ? text + "\n" + labels.join("\n") : text;
  }

  function emptyNote(svg, width, height, text) {
    svg.appendChild(s("text", { x: width / 2, y: height / 2, class: "empty", "text-anchor": "middle", text: text }));
  }

  function segments(values, xs, y) {
    var out = [];
    var current = [];
    values.forEach(function (v, i) {
      if (v == null) {
        if (current.length) {
          out.push(current);
        }
        current = [];
      } else {
        current.push(r1(xs[i]) + "," + r1(y(v)));
      }
    });
    if (current.length) {
      out.push(current);
    }
    return out;
  }

  function lastIndex(values) {
    for (var i = values.length - 1; i >= 0; i--) {
      if (values[i] != null) {
        return i;
      }
    }
    return -1;
  }

  // -- line chart -------------------------------------------------------------------------------------

  /* `series`: one or two `{ name, values, short }`; the second is drawn as the dashed reference line.
     `short`, when given, prefixes the series' direct label ("Team 91").
     `opts`: labels (x-axis text per point), format(v), tooltip(i) (text for step i), markers (per point,
     a list of change labels: see stepMarkers), shade (per point, true for the chosen period's steps),
     min/max (domain clamps), ariaLabel, empty (text when there is no value at all). */
  function lineChart(el, series, opts) {
    opts = opts || {};
    var format = opts.format || function (v) { return String(v); };
    mount(el, function (width) {
      if (series.length > 1) {
        el.appendChild(legend(series));
      }
      var svg = svgRoot(width, HEIGHT, opts.ariaLabel);
      el.appendChild(svg);
      var all = [];
      series.forEach(function (sr) { all = all.concat(present(sr.values)); });
      var n = series[0].values.length;
      var plotW = width - M.left - M.right;
      var xs = [];
      for (var i = 0; i < n; i++) {
        xs.push(M.left + (n > 1 ? plotW * i / (n - 1) : plotW / 2));
      }
      var band = n > 1 ? plotW / (n - 1) : plotW;
      periodBand(svg, xs, opts.shade, band, width, HEIGHT);
      if (!all.length) {
        frame(svg, width, HEIGHT, { lo: opts.min || 0, hi: opts.max || 100, ticks: [opts.min || 0, opts.max || 100] }, format);
        xLabels(svg, xs, opts.labels, HEIGHT);
        emptyNote(svg, width, HEIGHT, opts.empty || "No data in these weeks");
        return;
      }
      var y = frame(svg, width, HEIGHT, niceScale(all, opts), format);
      markers(svg, xs, opts.markers, HEIGHT);

      // Lines: the reference first, so the main series sits on top.
      series.slice().reverse().forEach(function (sr, k) {
        var ref = series.length > 1 && k === 0;
        segments(sr.values, xs, y).forEach(function (points) {
          if (points.length > 1) {
            svg.appendChild(s("polyline", { points: points.join(" "), class: ref ? "line ref" : "line" }));
          }
        });
      });

      // One hit band per step: a guide line, the dots, and the tooltip.
      xs.forEach(function (x, idx) {
        var dots = [];
        series.forEach(function (sr, k) {
          var v = sr.values[idx];
          if (v != null) {
            dots.push(s("circle", { cx: x, cy: y(v), r: idx === n - 1 ? 5 : 4, class: k === 1 ? "dot ref" : "dot" }));
          }
        });
        if (!dots.length) {
          return;
        }
        var text = opts.tooltip ? opts.tooltip(idx) : series.map(function (sr) {
          return sr.name + ": " + (sr.values[idx] == null ? "no data" : format(sr.values[idx]));
        }).join("\n");
        var g = s("g", { class: "hit" },
          s("rect", { x: x - band / 2, y: M.top, width: band, height: HEIGHT - M.top - M.bottom, class: "hitarea" }),
          s("line", { x1: x, x2: x, y1: M.top, y2: HEIGHT - M.bottom, class: "guide" }));
        dots.slice().reverse().forEach(function (d) { g.appendChild(d); });
        g.appendChild(tip(withMarker(text, opts.markers, idx)));
        svg.appendChild(g);
      });

      // Direct labels on each series' latest point; the higher one above, the lower one below.
      var ends = series.map(function (sr, k) {
        var i = lastIndex(sr.values);
        return i < 0 ? null : { k: k, x: xs[i], v: sr.values[i] };
      });
      ends.forEach(function (end) {
        if (!end) {
          return;
        }
        var other = ends[1 - end.k];
        var below = other && other.v > end.v;
        svg.appendChild(s("text", {
          x: Math.max(M.left + 12, end.x - 8), y: below ? y(end.v) + 18 : y(end.v) - 10,
          class: end.k === 1 ? "dlabel ref" : "dlabel", "text-anchor": "end",
          text: (series[end.k].short ? series[end.k].short + " " : "") + format(end.v)
        }));
      });
      xLabels(svg, xs, opts.labels, HEIGHT);
    });
  }

  function legend(series) {
    var box = document.createElement("div");
    box.className = "chart-legend";
    series.forEach(function (sr, k) {
      var key = document.createElement("span");
      key.className = k === 0 ? "k1" : "k2";
      key.setAttribute("aria-hidden", "true");
      box.appendChild(key);
      box.appendChild(document.createTextNode(sr.name));
    });
    return box;
  }

  // -- Trends graph -----------------------------------------------------------------------------------

  /* x-axis text for steps that may share a label (a period's edge splits a week in two, so two neighbours can
     both read "Aug 30"): every `every`-th step from the right, leaving out a label that repeats the one
     drawn just before it. */
  function stepLabels(svg, xs, labels, height) {
    var gap = xs.length > 1 ? xs[1] - xs[0] : Infinity;
    var every = Math.max(1, Math.ceil(MIN_LABEL_GAP / gap));
    var previous = null;
    for (var i = xs.length - 1; i >= 0; i -= every) {
      if (labels[i] === previous) {
        continue;
      }
      previous = labels[i];
      svg.appendChild(s("text", { x: xs[i], y: height - 8, class: "tick", "text-anchor": "middle", text: labels[i] }));
    }
  }

  /* Labels under steps of different widths: from the right, each one at least MIN_LABEL_GAP from the last drawn,
     and none repeating the one beside it. */
  function spacedLabels(svg, xs, labels, height) {
    var last = null;
    var previous = null;
    for (var i = xs.length - 1; i >= 0; i--) {
      if (labels[i] === previous || (last !== null && last - xs[i] < MIN_LABEL_GAP)) {
        continue;
      }
      previous = labels[i];
      last = xs[i];
      svg.appendChild(s("text", { x: xs[i], y: height - 8, class: "tick", "text-anchor": "middle", text: labels[i] }));
    }
  }

  /* One graph of one measure by step for a whole team. `data`: labels (x-axis text per step), shade (true for
     the chosen period's steps), part (true for a step covering only part of its week: a hollow dot), sizes
     (optional: each step's number of days, so a step is as wide as its days and its point sits at its middle;
     without it every step is as wide as the next),
     series (`{ key, name, values }` per tech; null is a step with no figure), team (`{ name, values }`) and
     highlight (a series key, or null for none). A part step with no figure leaves no gap: it exists only because
     the period's edge cut its week, so the line carries on past it; a whole step with none breaks the line.
     Every tech but the highlighted one is a faint line, the team
     is dashed, and the highlighted tech is the bold line with dots; the highlighted tech and the team end
     in direct labels.
     `opts`: format(v), teamFormat(v) (the team's values in the default tooltip and its end label, default
     format: a team count is a mean per tech and may keep a decimal while the axis stays whole), kind ("count":
     from 0 in whole numbers; "percent": fitted, never past 100; "duration": seconds, fitted on whole minutes or
     half minutes (durationScale); "number": fitted), whole (a percent drawn on its whole 0 to 100 scale, not
     fitted), digits (decimals the measure shows), tooltip(i, key) (text
     for step i), ariaLabel,
     empty (text when there is no value at all).
     Narrower than TRENDS_NARROW it takes its narrow form (iPad and phone design §10.3): the plot uses the whole width,
     the end labels give way to a readout above the graph (`readout`), and the height follows the width. */
  function highlightLines(el, data, opts) {
    opts = opts || {};
    var format = opts.format || function (v) { return String(v); };
    var teamFormat = opts.teamFormat || format;
    var kind = opts.kind || "number";
    mount(el, function (width) {
      // Narrow by the graph's own width: one not laid out yet (drawn at mount's DEFAULT_WIDTH) keeps the wide form
      // until it has a width, when it is drawn again.
      var narrow = el.clientWidth > 0 && width < TRENDS_NARROW;
      var height = narrow ? Math.round(Math.min(300, Math.max(230, width * 0.8))) : TRENDS_HEIGHT;
      var box = narrow ? el.appendChild(document.createElement("div")) : null;
      var svg = svgRoot(width, height, opts.ariaLabel);
      el.appendChild(svg);
      // The width the shared helpers see: the plot ends before the end labels, or at the edge when there are none.
      var inner = narrow ? width : width - END_ROOM + M.right;
      var n = data.labels.length;
      var plotW = inner - M.left - M.right;
      var xs = [];
      var lefts = [];
      var rights = [];
      if (data.sizes) {
        var total = data.sizes.reduce(function (a, b) { return a + b; }, 0);
        var at = 0;
        data.sizes.forEach(function (size) {
          lefts.push(M.left + plotW * at / total);
          at += size;
          rights.push(M.left + plotW * at / total);
          xs.push((lefts[lefts.length - 1] + rights[rights.length - 1]) / 2);
        });
      } else {
        var band = n > 1 ? plotW / (n - 1) : plotW;
        for (var i = 0; i < n; i++) {
          xs.push(M.left + (n > 1 ? plotW * i / (n - 1) : plotW / 2));
          lefts.push(xs[i] - band / 2);
          rights.push(xs[i] + band / 2);
        }
      }
      var labelsUnder = data.sizes ? spacedLabels : stepLabels;
      if (data.shade && data.shade.some(Boolean) && !data.shade.every(Boolean)) {
        var x0 = Math.max(M.left, lefts[data.shade.indexOf(true)]);
        var x1 = Math.min(inner - M.right, rights[data.shade.lastIndexOf(true)]);
        svg.appendChild(s("rect", { x: r1(x0), y: M.top, width: r1(x1 - x0), height: height - M.top - M.bottom,
          class: "period-band" }));
      }
      var all = present(data.team.values);
      data.series.forEach(function (sr) { all = all.concat(present(sr.values)); });
      var chosenRaw = null;
      data.series.forEach(function (sr) {
        if (sr.key === data.highlight && sr.raw) {
          chosenRaw = sr.raw;
          all = all.concat(present(sr.raw));
        }
      });
      if (!all.length) {
        if (box) {
          box.remove();   // no figures to read out
        }
        frame(svg, inner, height, { lo: 0, hi: 100, ticks: [0, 100] }, format);
        labelsUnder(svg, xs, data.labels, height);
        emptyNote(svg, inner, height, opts.empty || "No data in these weeks");
        return;
      }
      var scale;
      if (kind === "count") {
        scale = niceScale([0, Math.max(3, Math.ceil(Math.max.apply(null, all)))], { zero: true, min: 0, integer: true });
      } else if (kind === "duration") {
        scale = durationScale(all);
      } else if (opts.whole) {
        scale = niceScale([0, 100], { min: 0, max: 100 });
      } else {
        scale = niceScale(all, { pad: 0.12, minPad: Math.pow(10, -(opts.digits || 0)), min: 0,
          max: kind === "percent" ? 100 : null });
      }
      var y = frame(svg, inner, height, scale, format);
      var chosen = data.highlight == null ? null : data.series.filter(function (sr) {
        return sr.key === data.highlight;
      })[0] || null;

      // The steps a line passes through: all but a part step with no figure. Returns the steps whose figure has no
      // neighbour to join: a lone point, which only a dot can show.
      function line(values, cls, key) {
        var at = [];
        values.forEach(function (v, i) {
          if (v != null || !(data.part && data.part[i])) {
            at.push(i);
          }
        });
        var lone = [];
        var run = [];
        function close() {
          if (run.length > 1) {
            var poly = s("polyline", { points: run.map(function (i) {
              return r1(xs[i]) + "," + r1(y(values[i]));
            }).join(" "), class: cls });
            if (key) {
              poly.setAttribute("data-key", key);
            }
            svg.appendChild(poly);
          } else if (run.length === 1) {
            lone.push(run[0]);
          }
          run = [];
        }
        at.forEach(function (i) {
          if (values[i] == null) {
            close();
          } else {
            run.push(i);
          }
        });
        close();
        return lone;
      }
      // Every other tech is a faint line, tagged with their key so `peek` can lift one: brighter, on top, with their
      // name and latest figure at its end and a dot on any lone point, made then (svg._peek holds where they go).
      svg._peek = {};
      data.series.forEach(function (sr) {
        if (sr !== chosen) {
          var lone = line(sr.values, "line faint", sr.key);
          var end = lastIndex(sr.values);
          svg._peek[sr.key] = {
            dots: lone.map(function (i) { return [r1(xs[i]), r1(y(sr.values[i]))]; }),
            label: end < 0 ? null : { x: r1(xs[end] + 8), y: r1(y(sr.values[end]) + 4),
              name: sr.name.split(" ")[0], value: format(sr.values[end]) }
          };
        }
      });
      // A lone team value draws no line: a small dot, so the "Team" label always sits beside a mark.
      line(data.team.values, "line ref").forEach(function (i) {
        svg.appendChild(s("circle", { cx: r1(xs[i]), cy: r1(y(data.team.values[i])), r: 3, class: "dot ref" }));
      });
      // The highlighted tech's own figure for each step, a dot behind their line.
      if (chosenRaw) {
        chosenRaw.forEach(function (v, i) {
          if (v != null) {
            svg.appendChild(s("circle", { cx: r1(xs[i]), cy: r1(y(v)), r: 4, class: "dot raw" }));
          }
        });
      }
      var chosenLone = chosen ? line(chosen.values, "line") : [];

      // One hit band per step: a guide line, the highlighted tech's dot, and the tooltip.
      var lastChosen = chosen ? lastIndex(chosen.values) : -1;
      xs.forEach(function (x, idx) {
        var text;
        if (opts.tooltip) {
          text = opts.tooltip(idx, chosen ? chosen.key : null);
        } else {
          text = [data.labels[idx]];
          if (chosen && chosen.values[idx] != null) {
            text.push(chosen.name + ": " + format(chosen.values[idx]));
          }
          if (data.team.values[idx] != null) {
            text.push(data.team.name + ": " + teamFormat(data.team.values[idx]));
          }
          text = text.join("\n");
        }
        var g = s("g", { class: "hit" },
          s("rect", { x: lefts[idx], y: M.top, width: rights[idx] - lefts[idx], height: height - M.top - M.bottom,
            class: "hitarea" }),
          s("line", { x1: x, x2: x, y1: M.top, y2: height - M.bottom, class: "guide" }));
        if (chosen && chosen.values[idx] != null && (idx === lastChosen || chosenLone.indexOf(idx) >= 0 || !chosen.raw)) {
          g.appendChild(s("circle", { cx: x, cy: y(chosen.values[idx]), r: idx === lastChosen ? 5 : 4,
            class: data.part && data.part[idx] ? "dot part" : "dot" }));
        }
        g.appendChild(tip(text));
        svg.appendChild(g);
      });

      // End labels, right of each line's last point, kept END_LABEL_GAP apart in the order of their figures
      // (spreadLabels); each keeps its line's end (data-y) so a lifted line's label can join them later.
      function addEnd(values, text, cls) {
        var at = lastIndex(values);
        if (at >= 0) {
          var y0 = r1(y(values[at]) + 4);
          var label = s("text", { x: r1(xs[at] + 8), y: y0, class: cls, "text-anchor": "start", text: text(values[at]) });
          label.setAttribute("data-y", y0);
          svg.appendChild(label);
        }
      }
      var lastTeam = lastIndex(data.team.values);
      if (box) {
        readout(box, lastChosen < 0 ? null : [chosen.name.split(" ")[0], format(chosen.values[lastChosen])],
          lastTeam < 0 ? null : [data.team.name, teamFormat(data.team.values[lastTeam])],
          data.labels[Math.max(lastChosen, lastTeam)]);
        svg._readout = box;
      } else {
        if (chosen) {
          addEnd(chosen.values, function (v) { return chosen.name.split(" ")[0] + " " + format(v); }, "dlabel");
        }
        addEnd(data.team.values, function (v) { return data.team.name + " " + teamFormat(v); }, "dlabel ref");
        spreadLabels(svg);
      }
      labelsUnder(svg, xs, data.labels, height);
    });
  }

  /* A narrow Trends graph's readout (`.trend-readout`), above the graph in place of its end labels: the focus's first
     name and latest figure (`me`, a [name, figure] pair or null), the team's (`ref`, likewise) and the step they are
     from (`when`: its label, or undefined with neither). A tech lifted from the ranking joins it before `when` (peek). */
  function readout(box, me, ref, when) {
    box.className = "trend-readout";
    if (me) {
      box.appendChild(readoutFigure("me", me[0], me[1]));
    }
    if (ref) {
      box.appendChild(readoutFigure("ref", ref[0], ref[1]));
    }
    if (when != null) {
      box.appendChild(el("span", "when", "latest · " + when));
    }
  }

  /* One figure in a readout: a span of class `cls` holding `name` and the figure in bold. */
  function readoutFigure(cls, name, value) {
    var part = el("span", cls, name + " ");
    var b = document.createElement("b");
    b.textContent = value;
    part.appendChild(b);
    return part;
  }

  /* Lift one tech's faint line in a highlightLines graph in `el` (the owner, 2026-10-22: hovering their row in the
     ranking): brighter and above the others, with its end label (in a narrow graph, its figure in the readout),
     leaving the highlighted tech the focus; `key` null puts every line back. */
  function peek(el, key) {
    var svg = el.querySelector("svg");
    if (!svg || !svg._peek) {
      return;
    }
    var box = svg._readout;
    Array.prototype.forEach.call(svg.querySelectorAll(".peek-only"), function (node) { node.remove(); });
    if (box) {
      Array.prototype.forEach.call(box.querySelectorAll("span.peek"), function (node) { node.remove(); });
    }
    Array.prototype.forEach.call(svg.querySelectorAll("polyline.faint"), function (line) {
      var on = line.getAttribute("data-key") === key;
      line.classList.toggle("peek", on);
      if (on) {
        svg.insertBefore(line, svg.querySelector("polyline.ref"));   // above the other faint lines, under the team's
      }
    });
    var spot = key == null ? null : svg._peek[key];
    if (spot) {
      spot.dots.forEach(function (at) {
        svg.appendChild(s("circle", { cx: at[0], cy: at[1], r: 3, class: "dot peek-only" }));
      });
      if (spot.label && box) {
        box.insertBefore(readoutFigure("peek", spot.label.name, spot.label.value), box.querySelector("span.when"));
      } else if (spot.label) {
        var label = s("text", { x: spot.label.x, y: spot.label.y, class: "dlabel peek-only", "text-anchor": "start",
          text: spot.label.name + " " + spot.label.value });
        label.setAttribute("data-y", spot.label.y);
        svg.appendChild(label);
      }
    }
    spreadLabels(svg);
  }

  /* Lay out the end labels shown (the focus's, the team's and a lifted line's) in the order of their lines' ends, top
     to bottom, at least END_LABEL_GAP apart: labels that would crowd are spread as a group centred on their lines'
     ends, then the whole set is kept inside the plot. Each label keeps its line's end (data-y), so the layout starts
     afresh each time. */
  function spreadLabels(svg) {
    var labels = Array.prototype.filter.call(svg.querySelectorAll("text.dlabel"), function (t) {
      if (t.getAttribute("data-y") == null) {
        t.setAttribute("data-y", t.getAttribute("y"));
      }
      return true;
    });
    labels.sort(function (p, q) { return Number(p.getAttribute("data-y")) - Number(q.getAttribute("data-y")); });
    var groups = [];
    labels.forEach(function (t) {
      var end = Number(t.getAttribute("data-y"));
      groups.push({ sum: end, n: 1, top: end });
      while (groups.length > 1) {
        var below = groups[groups.length - 1], above = groups[groups.length - 2];
        if (above.top + above.n * END_LABEL_GAP <= below.top) {
          break;
        }
        groups.pop();
        above.sum += below.sum;
        above.n += below.n;
        above.top = above.sum / above.n - (above.n - 1) * END_LABEL_GAP / 2;
      }
    });
    var ys = [];
    groups.forEach(function (g) {
      for (var k = 0; k < g.n; k++) {
        ys.push(g.top + k * END_LABEL_GAP);
      }
    });
    var low = M.top + 4, high = TRENDS_HEIGHT - M.bottom + 4;
    var shift = ys.length ? Math.max(0, low - ys[0]) - Math.max(0, ys[ys.length - 1] - high) : 0;
    labels.forEach(function (t, n) { t.setAttribute("y", r1(ys[n] + shift)); });
  }

  // -- 1:1 trend ------------------------------------------------------------------------------------

  /* m:ss from seconds ("8:48"). */
  function minSec(seconds) {
    var whole = Math.round(seconds);
    return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
  }

  /* A figure as a 1:1 trend writes it when the caller gives no format: m:ss for seconds, a percent or a count with
     at most one decimal (the team's figure is a mean, so it may have one). */
  function trendFormat(unit) {
    if (unit === "seconds") {
      return minSec;
    }
    return function (v) { return String(r1(v)) + (unit === "percent" ? "%" : ""); };
  }

  /* The y-domain of a 1:1 trend: a count from 0 to a whole number at or past its largest value; seconds fitted
     to whole half minutes; anything else fitted to the data at its precision, never below 0 or past `scaleMax`. */
  function trendScale(all, spec) {
    var lo = Math.min.apply(null, all);
    var hi = Math.max.apply(null, all);
    if (spec.unit === "count") {
      return { lo: 0, hi: niceScale([0, Math.max(1, Math.ceil(hi))], { zero: true, min: 0, integer: true }).hi };
    }
    if (spec.unit === "seconds") {
      var pad = Math.max((hi - lo) * 0.12, TREND_TIME_STEP / 2);
      return { lo: Math.max(0, Math.floor((lo - pad) / TREND_TIME_STEP) * TREND_TIME_STEP),
        hi: Math.ceil((hi + pad) / TREND_TIME_STEP) * TREND_TIME_STEP };
    }
    var scale = niceScale(all, { pad: 0.12, minPad: Math.pow(10, -(spec.digits || 0)), min: 0,
      max: spec.scaleMax != null ? spec.scaleMax : null });
    return { lo: scale.lo, hi: scale.hi };
  }

  /* "10/20" from "2026-10-20". */
  function monthDay(iso) {
    var parts = iso.split("-");
    return Number(parts[1]) + "/" + Number(parts[2]);
  }

  /* One small chart of one measure over a tech's 1:1s, for a grid of them. `spec`: values (the tech's, per 1:1; null
     is a gap), team (the team average per 1:1, dashed), days (each 1:1's ISO day), unit ("count": from 0;
     "percent"; "seconds": m:ss), scaleMax (100 caps a percent's scale), digits (decimals the measure shows,
     default 0), format(v) and teamFormat(v) (default by unit), ariaLabel. It has min and max ticks, the first,
     middle and last 1:1 days, and a dot per 1:1 whose tooltip reads "1:1 10/20: 36 (team 31.5)". */
  function trendLine(el, spec) {
    var format = spec.format || trendFormat(spec.unit);
    var teamFormat = spec.teamFormat || format;
    var n = spec.days.length;
    var raw = spec.raw || null;   // each 1:1's own figure, drawn as dots behind the line
    mount(el, function (width) {
      var height = (spec.height && spec.height()) || TREND_CARD_HEIGHT;
      var svg = svgRoot(width, height, spec.ariaLabel);
      el.appendChild(svg);
      var plotW = width - TM.left - TM.right;
      var xs = [];
      for (var i = 0; i < n; i++) {
        xs.push(TM.left + (n > 1 ? plotW * i / (n - 1) : plotW / 2));
      }
      var big = height > TREND_CARD_HEIGHT;   // enlarged: every 1:1's day (every other past 13) and more gridlines
      var dayIndexes = !n ? [] : big ? xs.map(function (x, k) { return k; }).filter(function (k) {
        return n <= TREND_SMALL_DOTS || (n - 1 - k) % 2 === 0;
      }) : [0, Math.floor((n - 1) / 2), n - 1].filter(function (k, at, list) {
        return list.indexOf(k) === at;
      });
      dayIndexes.forEach(function (k) {
        var anchor = n === 1 ? "middle" : k === 0 ? "start" : k === n - 1 ? "end" : "middle";
        svg.appendChild(s("text", { x: r1(xs[k]), y: height - 6, class: "tick", "text-anchor": anchor,
          text: monthDay(spec.days[k]) }));
      });
      var all = present(spec.values).concat(present(spec.team)).concat(raw ? present(raw) : []);
      var bottom = height - TM.bottom;
      if (!all.length) {
        // The note in the middle of the plot, 4px down so the text centres on that line.
        svg.appendChild(s("line", { x1: TM.left, x2: width - TM.right, y1: bottom, y2: bottom, class: "base" }));
        svg.appendChild(s("text", { x: r1(TM.left + plotW / 2), y: r1((TM.top + bottom) / 2 + 4), class: "empty",
          "text-anchor": "middle", text: "No values in these 1:1s" }));
        return;
      }
      var scale = trendScale(all, spec);
      var y = function (v) { return TM.top + (scale.hi - v) / (scale.hi - scale.lo) * (bottom - TM.top); };
      var ticks = big ? [0, 1, 2, 3, 4].map(function (k) { return scale.lo + (scale.hi - scale.lo) * k / 4; })
        : [scale.lo, scale.hi];
      ticks.forEach(function (t, k) {
        svg.appendChild(s("line", { x1: TM.left, x2: width - TM.right, y1: r1(y(t)), y2: r1(y(t)), class: k ? "grid" : "base" }));
        svg.appendChild(s("text", { x: TM.left - 6, y: r1(y(t) + 4), class: "tick", "text-anchor": "end", text: format(t) }));
      });
      // The team's dashed line first, so the tech's sits on top; a lone team value between gaps is a small dot.
      segments(spec.team, xs, y).forEach(function (points) {
        if (points.length > 1) {
          svg.appendChild(s("polyline", { points: points.join(" "), class: "line ref" }));
        } else {
          var at = String(points[0]).split(",");
          svg.appendChild(s("circle", { cx: at[0], cy: at[1], r: 2.5, class: "dot ref" }));
        }
      });
      if (raw) {
        raw.forEach(function (v, k) {
          if (v != null) {
            svg.appendChild(s("circle", { cx: r1(xs[k]), cy: r1(y(v)), r: n > TREND_SMALL_DOTS ? 2.5 : 3.5,
              class: "dot raw" }));
          }
        });
      }
      var lone = [];
      segments(spec.values, xs, y).forEach(function (points) {
        if (points.length > 1) {
          svg.appendChild(s("polyline", { points: points.join(" "), class: "line" }));
        } else {
          lone.push(points[0]);
        }
      });
      // One hit band per 1:1 with a value: the tooltip, and the line's dot where it ends or stands alone.
      var band = n > 1 ? plotW / (n - 1) : plotW;
      var last = lastIndex(spec.values);
      xs.forEach(function (x, k) {
        var v = spec.values[k];
        var team = spec.team[k];
        if (v == null && team == null && !(raw && raw[k] != null)) {
          return;
        }
        var g = s("g", { class: "hit" },
          s("rect", { x: r1(x - band / 2), y: TM.top, width: r1(band), height: bottom - TM.top, class: "hitarea" }));
        // Enlarged, it answers a hover as Trends' graph does: a guide line at the 1:1 and the line's own point.
        if (big) {
          g.appendChild(s("line", { x1: r1(x), x2: r1(x), y1: TM.top, y2: bottom, class: "guide" }));
        }
        if (v != null && (!raw || k === last || lone.indexOf(r1(x) + "," + r1(y(v))) >= 0)) {
          g.appendChild(s("circle", { cx: r1(x), cy: r1(y(v)), r: n > TREND_SMALL_DOTS ? 2.5 : 3.5, class: "dot" }));
        } else if (v != null && big) {
          g.appendChild(s("circle", { cx: r1(x), cy: r1(y(v)), r: 4, class: "dot faint" }));
        }
        g.appendChild(tip(spec.tooltip ? spec.tooltip(k) : "1:1 " + monthDay(spec.days[k]) + ": " +
          (v == null ? "-" : format(v)) + " (team " + (team == null ? "-" : teamFormat(team)) + ")"));
        svg.appendChild(g);
      });
      if (raw && last >= 0 && spec.endLabel) {
        svg.appendChild(s("text", { x: r1(Math.min(xs[last], width - TM.right) - 6), y: r1(y(spec.values[last]) - 8),
          class: "dlabel", "text-anchor": "end", text: format(spec.values[last]) }));
      }
    });
  }

  // -- column chart -----------------------------------------------------------------------------------

  /* One column per value (null: no column). The last column is emphasized and labeled.
     `opts`: labels, format(v), tooltip(i), markers, shade, min/max, ariaLabel, empty. */
  function barChart(el, values, opts) {
    opts = opts || {};
    var format = opts.format || function (v) { return String(v); };
    mount(el, function (width) {
      var svg = svgRoot(width, HEIGHT, opts.ariaLabel);
      el.appendChild(svg);
      var n = values.length;
      var plotW = width - M.left - M.right;
      var band = plotW / n;
      var xs = values.map(function (_, i) { return M.left + band * (i + 0.5); });
      periodBand(svg, xs, opts.shade, band, width, HEIGHT);
      var data = present(values);
      if (!data.length) {
        frame(svg, width, HEIGHT, { lo: 0, hi: opts.max || 100, ticks: [0, opts.max || 100] }, format);
        xLabels(svg, xs, opts.labels, HEIGHT);
        emptyNote(svg, width, HEIGHT, opts.empty || "No data in these weeks");
        return;
      }
      var scale = niceScale(data.concat([0]), { zero: true, min: 0, max: opts.max });
      var y = frame(svg, width, HEIGHT, scale, format);
      markers(svg, xs, opts.markers, HEIGHT);
      var barW = Math.min(BAR_MAX, band * 0.6);
      var base = y(scale.lo);
      values.forEach(function (v, i) {
        var g = s("g", { class: "hit" },
          s("rect", { x: xs[i] - band / 2, y: M.top, width: band, height: HEIGHT - M.top - M.bottom, class: "hitarea" }));
        if (v != null && v > 0) {
          g.appendChild(s("path", { d: columnPath(xs[i] - barW / 2, barW, base, y(v)), class: i === n - 1 ? "bar last" : "bar" }));
        }
        var text = opts.tooltip ? opts.tooltip(i) : (v == null ? "No data" : format(v));
        g.appendChild(tip(withMarker(text, opts.markers, i)));
        svg.appendChild(g);
      });
      var last = values[n - 1];
      if (last != null) {
        svg.appendChild(s("text", { x: xs[n - 1], y: y(last) - 8, class: "dlabel", "text-anchor": "middle", text: format(last) }));
      }
      xLabels(svg, xs, opts.labels, HEIGHT);
    });
  }

  /* A column from the baseline up to `top`: square at the baseline, rounded at the data end. */
  function columnPath(x, w, base, top) {
    var r = Math.min(RADIUS, w / 2, base - top);
    return "M" + r1(x) + "," + r1(base) + "V" + r1(top + r) + "Q" + r1(x) + "," + r1(top) + " " + r1(x + r) + "," + r1(top) +
      "H" + r1(x + w - r) + "Q" + r1(x + w) + "," + r1(top) + " " + r1(x + w) + "," + r1(top + r) + "V" + r1(base) + "Z";
  }

  /* A bar from the left baseline to `len`: square at the baseline, rounded at the data end. */
  function barPath(len, y0, h) {
    var r = Math.min(RADIUS, h / 2, len);
    return "M0," + y0 + "H" + r1(len - r) + "Q" + r1(len) + "," + y0 + " " + r1(len) + "," + (y0 + r) +
      "V" + (y0 + h - r) + "Q" + r1(len) + "," + (y0 + h) + " " + r1(len - r) + "," + (y0 + h) + "H0Z";
  }

  // -- horizontal bars --------------------------------------------------------------------------------

  /* Ranked rows: name, bar, count, and a change note. `rows`: `{ label, value, title, delta, deltaClass,
     href, max }`; every bar shares one scale, 0 to the largest value (or the largest `max` given, so a
     shortened list keeps the full list's scale). */
  function hBars(el, rows) {
    var max = Math.max.apply(null, rows.map(function (r) { return Math.max(r.value, r.max || 0); }).concat([1]));
    el.textContent = "";
    var list = document.createElement("div");
    list.className = "hbars";
    var tracks = [];
    rows.forEach(function (row) {
      var line = document.createElement("div");
      line.className = "mrow";
      line.title = row.title || "";
      var name;
      if (row.href) {
        name = document.createElement("a");
        name.href = row.href;
      } else {
        name = document.createElement("div");
      }
      name.className = "mname";
      name.textContent = row.label;
      var track = document.createElement("div");
      track.className = "mtrack";
      var num = document.createElement("div");
      num.className = "mnum";
      num.textContent = String(row.value);
      var delta = document.createElement("div");
      delta.className = "mdelta" + (row.deltaClass ? " " + row.deltaClass : "");
      delta.textContent = row.delta || "";
      line.appendChild(name);
      line.appendChild(track);
      line.appendChild(num);
      line.appendChild(delta);
      list.appendChild(line);
      tracks.push({ el: track, row: row });
    });
    el.appendChild(list);
    tracks.forEach(function (t) {
      mount(t.el, function (width) {
        var h = 14;
        var svg = svgRoot(width, h, t.row.label + ": " + t.row.value, "hbar");
        var len = width * t.row.value / max;
        if (len > 0) {
          svg.appendChild(s("path", { d: barPath(len, 1, 12), class: "fillbar" }));
        }
        svg.appendChild(tip(t.row.title || (t.row.label + ": " + t.row.value)));
        t.el.appendChild(svg);
      });
    });
  }

  // -- pass-rate bar ----------------------------------------------------------------------------------

  /* One check's pass rate on a 0-100 track, with a tick at the team's rate. `opts`: flagged (draws the
     bar in the alert style), title (tooltip), ariaLabel. A null rate draws the track only. */
  function passBar(el, rate, teamRate, opts) {
    opts = opts || {};
    var w = 160;
    var h = 14;
    el.textContent = "";
    var svg = svgRoot(w, h, opts.ariaLabel, "passbar");
    svg.appendChild(s("rect", { x: 0, y: 3, width: w, height: 8, rx: RADIUS, class: "track" }));
    if (rate != null && rate > 0) {
      svg.appendChild(s("rect", { x: 0, y: 3, width: r1(w * rate / 100), height: 8, rx: RADIUS,
        class: opts.flagged ? "fillbar crit" : "fillbar" }));
    }
    if (teamRate != null) {
      var x = r1(Math.min(w - 1, Math.max(1, w * teamRate / 100)));
      svg.appendChild(s("line", { x1: x, x2: x, y1: 0, y2: h, class: "refmark" }));
    }
    if (opts.title) {
      svg.appendChild(tip(opts.title));
    }
    el.appendChild(svg);
  }

  // -- sparkline --------------------------------------------------------------------------------------

  /* A small trend line (nulls leave gaps), the latest point dotted, at the --spark-w × --spark-h size from
     app.css. A lone point, with no line to read, is labelled with its value. `opts`: domain [lo, hi]
     (share one across a table so rows compare), labels (tooltip text per point), ariaLabel. */
  function sparkline(el, values, opts) {
    opts = opts || {};
    var w = parseFloat(token("spark-w"));
    var h = parseFloat(token("spark-h"));
    var pad = 4;
    el.textContent = "";
    var svg = svgRoot(w, h, opts.ariaLabel, "spark");
    var data = present(values);
    el.appendChild(svg);
    if (!data.length) {
      svg.appendChild(s("line", { x1: pad, x2: w - pad, y1: h / 2, y2: h / 2, class: "grid" }));
      return;
    }
    var lo = opts.domain ? opts.domain[0] : Math.min.apply(null, data);
    var hi = opts.domain ? opts.domain[1] : Math.max.apply(null, data);
    if (hi <= lo) {
      hi = lo + 1;
      lo -= 1;
    }
    var n = values.length;
    var xs = values.map(function (_, i) { return pad + (w - 2 * pad) * (n > 1 ? i / (n - 1) : 0.5); });
    var y = function (v) { return pad + (hi - v) / (hi - lo) * (h - 2 * pad); };
    segments(values, xs, y).forEach(function (points) {
      if (points.length > 1) {
        svg.appendChild(s("polyline", { points: points.join(" "), class: "line thin" }));
      }
    });
    var last = lastIndex(values);
    var step = n > 1 ? (w - 2 * pad) / (n - 1) : w;
    values.forEach(function (v, i) {
      if (v == null) {
        return;
      }
      var g = s("g", { class: "hit" },
        s("rect", { x: xs[i] - step / 2, y: 0, width: step, height: h, class: "hitarea" }),
        s("circle", { cx: xs[i], cy: y(v), r: i === last ? 2.5 : 1.5, class: i === last ? "dot last" : "dot faint" }));
      g.appendChild(tip(opts.labels ? opts.labels[i] : String(v)));
      svg.appendChild(g);
    });
    if (data.length === 1) {
      // Beside the point, on the side with room: left of a point in the right half, else right of it.
      var right = xs[last] > w / 2;
      svg.appendChild(s("text", {
        x: xs[last] + (right ? -SPARK_LABEL_GAP : SPARK_LABEL_GAP), y: h / 2, class: "dlabel",
        "text-anchor": right ? "end" : "start", "dominant-baseline": "central", text: String(r1(data[0]))
      }));
    }
  }

  // -- one tech's day -------------------------------------------------------------------------------

  var DAY_FIRST_HOUR = 6;      // the day axis runs from 6 AM ...
  var DAY_LAST_HOUR = 19;      // ... to 7 PM, wider when an action, the sign-in or the shift falls outside it
  var DAY_HEIGHT = 100;
  var DAY_SIDE = 22;           // px kept clear at each end for the first and last hour labels
  var DAY_TOP = 18;            // the plot's top and bottom edges
  var DAY_BOTTOM = 66;
  var DAY_ACTION_W = 4;        // px, an action's bar
  var DAY_LABEL_Y = 86;        // the hour labels
  var DAY_NOTE_Y = 12;         // the shift label, and the sign-in label when there is no shift
  var DAY_NOTE_LOW_Y = 98;     // the sign-in label under the axis when the shift label is on top
  var DAY_GAP_LABEL_Y = 47;
  var DAY_GAP_LINE = 18;       // px: a narrow chart moves its plot down this much, for the gap's label on a line above it
  var DAY_CHAR_W = 7;          // px, about the widest a 12px chart character runs (Segoe UI's digits and capitals)
  var DAY_LABEL_GAP = 8;       // px, the least room between two hour labels on a narrow chart
  var LATE_FRACTION = 0.8;     // a sign-in label past this share of the width is written leftward
  var ACTION_CLASS = { note: "note", call: "call" };   // every other kind of action is "act"

  function minutesOf(hhmm) {
    var parts = hhmm.split(":");
    return Number(parts[0]) * 60 + Number(parts[1]);
  }

  function hourText(hour) {
    return hour === 12 ? "Noon" : (hour % 12 || 12) + (hour % 24 >= 12 ? " PM" : " AM");
  }

  function clockText(hhmm) {
    var m = minutesOf(hhmm);
    var hour = Math.floor(m / 60);
    return (hour % 12 || 12) + ":" + String(m % 60).padStart(2, "0") + (hour >= 12 ? " PM" : " AM");
  }

  function shiftClockText(hhmm) {
    return clockText(hhmm).replace(":00 ", " ");
  }

  var DAY_HIT_W = 12;   // px, the hover target over one minute's actions: wider than a bar, easy to point at

  function gapText(minutes) {
    return minutes < 60 ? minutes + " m" : Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " m";
  }

  /* The even hours from `h0` to `h1` a narrow day chart labels, `x` placing each: the first and the last, and between
     them each one whose label clears the last kept by DAY_LABEL_GAP, every label taken as wide as the widest
     (DAY_CHAR_W a character). When the last is too close to the one kept before it, that one gives way, unless it is
     the first (a chart too narrow for two labels keeps only the first). */
  function dayHourLabels(h0, h1, x) {
    var hours = [];
    for (var hour = h0; hour <= h1; hour++) {
      if (hour % 2 === 0) {
        hours.push(hour);
      }
    }
    var apart = DAY_LABEL_GAP + DAY_CHAR_W * Math.max.apply(null, hours.map(function (hr) { return hourText(hr).length; }));
    var kept = [];
    hours.forEach(function (hr, i) {
      var room = !kept.length || x(hr * 60) - x(kept[kept.length - 1] * 60) >= apart;
      if (!room && i === hours.length - 1 && kept.length > 1) {
        kept.pop();   // kept labels stand `apart`, so the one before it is far enough from the last
        room = true;
      }
      if (room) {
        kept.push(hr);
      }
    });
    return kept;
  }

  /* Whether the hovercard shows `data` (a mark's <desc>) now. */
  function cardShows(data) {
    return Boolean(card) && !card.hidden && card._shown === data;
  }

  /* One tech's day on a time axis: the shift as a band, each recorded action as a thin bar (a reply or ticket
     change "act", an internal "note", a logged "call"), the sign-in as a marker and a long gap as a band of its
     own. `data` is {shift: {start, end} | null, signin: "HH:MM" | null, actions: [{at, kind}],
     gap: {start: "HH:MM", minutes} | null}; the caller passes a gap only when it is long enough to flag. `opts`:
     describe(actions) (the hovercard of the actions at one minute, which share one hover target wider than the bars;
     default their time) and open(actions) (what a click on them does; none: not clickable). On a touch screen the
     first tap on a moment shows its card and a tap on the moment whose card shows opens it; a mouse click opens it.
     Narrower than TRENDS_NARROW (a phone) it takes its narrow form: fewer hour labels (dayHourLabels), and the gap's
     label on a line of its own above the plot ("2 h 55 m gap" when the whole words don't fit the plot's width). */
  function dayChart(el, data, opts) {
    opts = opts || {};
    mount(el, function (width) {
      // Narrow by the chart's own width: one not laid out yet (drawn at mount's DEFAULT_WIDTH) keeps the wide form.
      var narrow = el.clientWidth > 0 && width < TRENDS_NARROW;
      var lift = narrow && data.gap ? DAY_GAP_LINE : 0;   // the plot moves down a line, for the gap's label
      var top = DAY_TOP + lift;
      var bottom = DAY_BOTTOM + lift;
      var times = data.actions.map(function (a) { return minutesOf(a.at); });
      if (data.signin) {
        times.push(minutesOf(data.signin));
      }
      if (data.shift) {
        times.push(minutesOf(data.shift.start), minutesOf(data.shift.end));
      }
      var h0 = Math.floor(Math.min.apply(null, times.concat([DAY_FIRST_HOUR * 60])) / 60);
      var h1 = Math.ceil(Math.max.apply(null, times.concat([DAY_LAST_HOUR * 60])) / 60);
      var x = function (minutes) {
        return r1(DAY_SIDE + (minutes - h0 * 60) / ((h1 - h0) * 60) * (width - 2 * DAY_SIDE));
      };
      var svg = svgRoot(width, DAY_HEIGHT + lift, opts.ariaLabel || "Actions through the day");
      el.appendChild(svg);
      if (data.shift) {
        var x0 = x(minutesOf(data.shift.start));
        svg.appendChild(s("rect", { x: x0, y: top, width: r1(x(minutesOf(data.shift.end)) - x0),
          height: bottom - top, class: "period-band" }));
        svg.appendChild(s("text", { x: x0 + 4, y: DAY_NOTE_Y, class: "shiftlabel",
          text: "Shift " + shiftClockText(data.shift.start) + " – " + shiftClockText(data.shift.end) }));
      }
      var labelled = narrow ? dayHourLabels(h0, h1, x) : null;   // wider: every even hour
      for (var hour = h0; hour <= h1; hour++) {
        svg.appendChild(s("line", { x1: x(hour * 60), x2: x(hour * 60), y1: top, y2: bottom, class: "grid" }));
        if (hour % 2 === 0 && (!labelled || labelled.indexOf(hour) >= 0)) {
          svg.appendChild(s("text", { x: x(hour * 60), y: DAY_LABEL_Y + lift, class: "tick", "text-anchor": "middle",
            text: hourText(hour) }));
        }
      }
      svg.appendChild(s("line", { x1: DAY_SIDE, x2: width - DAY_SIDE, y1: bottom, y2: bottom, class: "base" }));
      if (data.gap) {
        var g0 = x(minutesOf(data.gap.start));
        var g1 = x(minutesOf(data.gap.start) + data.gap.minutes);
        svg.appendChild(s("rect", { x: g0, y: top + 2, width: r1(g1 - g0), height: bottom - top - 4,
          rx: 4, class: "gapband" }));
        var said = gapText(data.gap.minutes) + " with nothing recorded";
        var gx = r1((g0 + g1) / 2);
        if (narrow) {
          // Above the plot, over the gap as far as the plot's ends allow; shortened when the words don't fit it.
          if (said.length * DAY_CHAR_W > width - 2 * DAY_SIDE) {
            said = gapText(data.gap.minutes) + " gap";
          }
          var half = said.length * DAY_CHAR_W / 2;
          gx = r1(Math.min(Math.max(gx, DAY_SIDE + half), width - DAY_SIDE - half));
        }
        svg.appendChild(s("text", { x: gx, y: narrow ? top - 8 : DAY_GAP_LABEL_Y, class: "gaplabel",
          "text-anchor": "middle", text: said }));
      }
      var atMinute = {};
      data.actions.forEach(function (a) {
        svg.appendChild(s("rect", { x: r1(x(minutesOf(a.at)) - DAY_ACTION_W / 2), y: top + 4, width: DAY_ACTION_W,
          height: bottom - top - 8, rx: 1, class: "bar " + (ACTION_CLASS[a.kind] || "act") }));
        (atMinute[a.at] = atMinute[a.at] || []).push(a);
      });
      Object.keys(atMinute).forEach(function (at) {
        var those = atMinute[at];
        var about = tip(opts.describe ? opts.describe(those) : clockText(at));
        var hit = s("rect", { x: r1(x(minutesOf(at)) - DAY_HIT_W / 2), y: top, width: DAY_HIT_W,
          height: bottom - top, class: "day-hit" + (opts.open ? " link" : "") }, about);
        if (opts.open) {
          // A finger's tap on a moment whose card isn't showing only shows it (showCard, from the page's own
          // pointerdown, which comes after this one); a tap on the moment whose card shows opens it, as a click does.
          var showOnly = false;
          hit.addEventListener("pointerdown", function (event) {
            showOnly = event.pointerType === "touch" && !cardShows(about);
          });
          hit.addEventListener("click", function () {
            if (showOnly) {
              showOnly = false;
              return;
            }
            opts.open(those);
          });
        }
        svg.appendChild(hit);
      });
      if (data.signin) {
        var sx = x(minutesOf(data.signin));
        var late = sx > width * LATE_FRACTION;
        svg.appendChild(s("line", { x1: sx, x2: sx, y1: top - 4, y2: bottom + 4, class: "marker" },
          tip(clockText(data.signin))));
        svg.appendChild(s("text", { x: sx + (late ? -4 : 4), y: data.shift ? DAY_NOTE_LOW_Y + lift : DAY_NOTE_Y,
          class: "tick", "text-anchor": late ? "end" : "start", text: "signed in " + clockText(data.signin) }));
      }
    });
  }

  window.Charts = {
    token: token,
    lineChart: lineChart,
    barChart: barChart,
    hBars: hBars,
    sparkline: sparkline,
    passBar: passBar,
    dayChart: dayChart,
    stepMarkers: stepMarkers,
    highlightLines: highlightLines,
    peek: peek,
    trendLine: trendLine,
    minSec: minSec,
    niceScale: niceScale
  };
})();
