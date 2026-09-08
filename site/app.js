"use strict";

// ─── Config ───

const VENDOR_ORDER = ["anthropic", "openai", "google"];
const VENDOR_META = {
  anthropic: { company: "Anthropic", product: "Claude Code", short: "Claude", varName: "--v-anthropic" },
  openai: { company: "OpenAI", product: "Codex / ChatGPT", short: "Codex", varName: "--v-openai" },
  google: { company: "Google", product: "Gemini CLI / Code Assist", short: "Gemini", varName: "--v-google" },
};

const WINDOW_META = {
  last_7d: { label: "7d", days: 7 },
  last_30d: { label: "30d", days: 30 },
  last_90d: { label: "90d", days: 90 },
  all_time: { label: "All", days: null },
};

// Empirical forecast: backtesting (rolling-mean / median / naive-persistence
// predictors against actual next-gap, per vendor) showed MAE around 6.5d+ on
// gaps whose median is ~4d — a single-date point prediction would be false
// precision. This shows an empirical conditional probability instead: of
// past gaps that had *already* lasted at least as long as the current wait,
// what fraction concluded within the next FORECAST_HORIZON_DAYS?
const FORECAST_HORIZON_DAYS = 7;
const FORECAST_MIN_GAPS = 3;      // fewer than this (e.g. Google's 1) → don't show at all
const FORECAST_LOW_CONFIDENCE = 15; // fewer than this (e.g. Anthropic's 8) → show, but flag small sample

// ─── Own-account observations ───
//
// The interactive twin of build_observations() / probe_badge() in
// scripts/build.py. Same classes, same data-verdict values, same label
// vocabulary; tests/test_build.py fails the build if one side drifts.
// `self_applied` and `credit_granted` have no label here on purpose — they
// describe what the account owner did, and build.py never puts them in
// data.json in the first place.
const VERDICT_LABELS = {
  vendor_reset: "Cleared early · unexplained",
  natural_expiry: "Scheduled expiry",
  unresolved: "Cannot tell",
  limit_change: "Limits rescaled",
};
const PROBE_STALE_SECONDS = 900;
const OBSERVATIONS_SHOWN = 6;
const DEFAULT_COVERAGE = "Own-account probe; see the methodology for what it can and cannot see.";
const NO_ANNOUNCEMENTS_COPY =
  "No tracked announcements for this provider yet. The lines below are what our own account measured.";

const RING_R_OUTER = 68;
const RING_R_INNER = 52;
const RING_C_OUTER = 2 * Math.PI * RING_R_OUTER;
const RING_C_INNER = 2 * Math.PI * RING_R_INNER;
const EVENTS_PREVIEW_LIMIT = 8;
const TIMING_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const TIMING_PERIODS = [
  { label: "Overnight", start: 0, end: 6 },
  { label: "Morning", start: 6, end: 12 },
  { label: "Afternoon", start: 12, end: 18 },
  { label: "Evening", start: 18, end: 24 },
];
const PACIFIC_TIMING_FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  weekday: "long",
  hour: "numeric",
  hourCycle: "h23",
});
const {
  formatPacificDateTime,
  formatPacificMonthDay,
  formatEventTimestamp,
  toPacificInputValue,
  parsePacificInputValue,
} = window.AIResetTime;

const CALC_PRESETS = {
  "anthropic-week": { days: 7, rate: 95 },
  "anthropic-5h": { days: 5 / 24, rate: 95 },
  "openai-week": { days: 7, rate: 95 },
  "gemini-day": { days: 1, rate: 95 },
};

// ─── State ───

let DATA = null;
let activeVendors = new Set(VENDOR_ORDER);
let activeWindow = "last_30d";
let eventsExpanded = false;

// ─── Helpers ───

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmtDays(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (n < 1) return `${Math.round(n * 24)}h`;
  return `${n.toFixed(1)}d`;
}

// Mirrors describe_age() in scripts/timefmt.py so the static snapshot and the
// live page use one vocabulary for "how long ago".
function describeAge(seconds) {
  const s = Math.max(0, Math.trunc(seconds));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.floor(s / 60 + 0.5)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

function probeAgeSeconds(probe, now) {
  const raw = probe && probe.last_verified_at;
  if (typeof raw !== "string" || !raw) return null;
  const verified = new Date(raw);
  if (Number.isNaN(verified.getTime())) return null;
  return Math.max(0, (now - verified) / 1000);
}

// Anthropic and Google have no own-account probe. The card used to show a
// hardcoded "Tracking" for all three, which let them borrow the credibility of
// the one vendor that is actually measured.
function probeBadge(probe, now, observations) {
  const status = probe && probe.status;
  if (status !== "ok" && status !== "blind" && status !== "absent") {
    return { text: "Announcements only", state: "none" };
  }
  if (status === "absent") {
    // Mirrors probe_badge() in scripts/build.py exactly. A probe that ships but
    // has never reported, on a card with nothing to show for it, must not
    // promise ground truth; one WITH observations must not be called
    // "announcements only" directly above its own measurements.
    if (!observations || !observations.length) {
      return { text: "Announcements only", state: "none" };
    }
    return { text: "Ground truth · probe state unknown", state: "offline" };
  }
  const age = probeAgeSeconds(probe, now);
  if (age === null) return { text: "Ground truth · probe state unknown", state: "offline" };
  if (status === "ok" && age <= PROBE_STALE_SECONDS) {
    return { text: `Ground truth · verified ${describeAge(age)} ago`, state: "verified" };
  }
  return { text: `Ground truth · probe offline ${describeAge(age)}`, state: "offline" };
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (children) for (const c of children) {
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// ─── Data prep ───

function parseAll() {
  for (const v of VENDOR_ORDER) {
    const vendor = DATA.vendors[v];
    if (!vendor) continue;
    vendor.eventsParsed = vendor.events
      .map((e) => ({ ...e, date: new Date(e.announced_at) }))
      .sort((a, b) => a.date - b.date);
  }
}

function computeStats(eventsParsed, windowKey, now) {
  const total = eventsParsed.length;
  const daysSinceLast = total ? (now - eventsParsed[total - 1].date) / 86400000 : null;

  let longestGapDays = null;
  if (total >= 2) {
    let maxGap = 0;
    for (let i = 1; i < total; i++) {
      maxGap = Math.max(maxGap, (eventsParsed[i].date - eventsParsed[i - 1].date) / 86400000);
    }
    longestGapDays = maxGap;
  }

  const days = WINDOW_META[windowKey].days;
  const inWindow = days == null
    ? eventsParsed
    : eventsParsed.filter((e) => (now - e.date) / 86400000 <= days);

  let avgIntervalDays = null;
  if (inWindow.length >= 2) {
    let sum = 0;
    for (let i = 1; i < inWindow.length; i++) sum += (inWindow[i].date - inWindow[i - 1].date) / 86400000;
    avgIntervalDays = sum / (inWindow.length - 1);
  }

  return { total, daysSinceLast, longestGapDays, inWindow, avgIntervalDays };
}

function computeForecast(eventsParsed, now) {
  const n = eventsParsed.length;
  if (n < 2) return null;

  const gaps = [];
  for (let i = 1; i < n; i++) gaps.push((eventsParsed[i].date - eventsParsed[i - 1].date) / 86400000);
  if (gaps.length < FORECAST_MIN_GAPS) return null;

  const daysSinceLast = (now - eventsParsed[n - 1].date) / 86400000;
  const atRisk = gaps.filter((g) => g >= daysSinceLast);
  if (atRisk.length === 0) {
    return { available: false, noPrecedent: true, daysSinceLast, maxGap: Math.max(...gaps) };
  }

  const concluded = atRisk.filter((g) => g <= daysSinceLast + FORECAST_HORIZON_DAYS).length;
  const probability = concluded / atRisk.length;

  return {
    available: true,
    probability,
    sampleSize: atRisk.length,
    totalGaps: gaps.length,
    lowConfidence: gaps.length < FORECAST_LOW_CONFIDENCE,
  };
}

function sharedDomain(windowKey, now) {
  const days = WINDOW_META[windowKey].days;
  if (days != null) return [new Date(now - days * 86400000), now];
  let min = now;
  for (const v of VENDOR_ORDER) {
    const evs = DATA.vendors[v]?.eventsParsed || [];
    if (evs.length) min = Math.min(min, evs[0].date.getTime());
  }
  return [new Date(min), now];
}

// ─── Tooltip ───

const tooltipEl = document.getElementById("tooltip");

function showTooltip(x, y, dateStr, text) {
  tooltipEl.textContent = "";
  const dateRow = el("div", { class: "tt-date" }, [dateStr]);
  const textRow = el("div", { class: "tt-text" }, [text]);
  tooltipEl.appendChild(dateRow);
  tooltipEl.appendChild(textRow);
  const pad = 14;
  let left = x + pad;
  let top = y + pad;
  tooltipEl.style.left = `${left}px`;
  tooltipEl.style.top = `${top}px`;
  tooltipEl.classList.add("show");
  // clamp after layout
  requestAnimationFrame(() => {
    const rect = tooltipEl.getBoundingClientRect();
    if (rect.right > window.innerWidth) tooltipEl.style.left = `${x - rect.width - pad}px`;
    if (rect.bottom > window.innerHeight) tooltipEl.style.top = `${y - rect.height - pad}px`;
  });
}

function hideTooltip() {
  tooltipEl.classList.remove("show");
}

// ─── Timeline (small-multiples dot strip, one row per active vendor) ───

function buildTimelineSVG(vendorKey, domain, now) {
  const W = 900, H = 64, PAD = 34;
  const meta = VENDOR_META[vendorKey];
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": `${meta.company} ${meta.product} timeline` });
  const color = cssVar(VENDOR_META[vendorKey].varName);
  const surface = cssVar("--surface");

  const [d0, d1] = domain;
  const span = d1 - d0 || 1;
  const x = (d) => PAD + ((d - d0) / span) * (W - 2 * PAD);

  // baseline
  svg.appendChild(svgEl("line", { x1: PAD, x2: W - PAD, y1: 40, y2: 40, stroke: "var(--baseline)", "stroke-width": 1 }));

  // 4 evenly spaced date ticks
  for (let i = 0; i <= 4; i++) {
    const t = new Date(d0.getTime() + (span * i) / 4);
    const tx = x(t);
    svg.appendChild(svgEl("line", { x1: tx, x2: tx, y1: 36, y2: 44, stroke: "var(--gridline)", "stroke-width": 1 }));
    const label = svgEl("text", { x: tx, y: 58, "text-anchor": i === 4 ? "end" : i === 0 ? "start" : "middle", class: "tl-axis-label" });
    label.textContent = formatPacificMonthDay(t);
    svg.appendChild(label);
  }

  const events = (DATA.vendors[vendorKey]?.eventsParsed || []).filter((e) => e.date >= d0 && e.date <= d1);

  for (const e of events) {
    const cx = x(e.date);
    const g = svgEl("g", { class: "tl-dot", tabindex: "0", role: "img" });
    const approx = e.confidence === "approx";
    const dot = svgEl("circle", {
      cx, cy: 40, r: 6,
      fill: color,
      stroke: surface,
      "stroke-width": 2,
      "stroke-dasharray": approx ? "2,2" : "none",
    });
    const hit = svgEl("circle", { cx, cy: 40, r: 12, fill: "transparent" });
    g.appendChild(dot);
    g.appendChild(hit);

    const dateStr = formatEventTimestamp(e);
    const onEnter = (evt) => {
      const point = evt.type.startsWith("focus") ? dot.getBoundingClientRect() : evt;
      showTooltip(point.clientX ?? (point.x + point.width / 2), point.clientY ?? point.y, dateStr, e.text);
    };
    g.addEventListener("mouseenter", onEnter);
    g.addEventListener("mousemove", onEnter);
    g.addEventListener("mouseleave", hideTooltip);
    g.addEventListener("focus", onEnter);
    g.addEventListener("blur", hideTooltip);
    g.addEventListener("click", () => window.open(e.url, "_blank", "noopener"));
    svg.appendChild(g);
  }

  return svg;
}

// ─── Render ───

function buildVendorChips() {
  const wrap = document.getElementById("vendor-chips");
  for (const v of VENDOR_ORDER) {
    if (!DATA.vendors[v]) continue;
    const vColor = cssVar(VENDOR_META[v].varName);
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": "true", style: `color:${vColor};--vchip-tint:${vColor}22` }, [
      el("span", { class: "dot", style: `background:${cssVar(VENDOR_META[v].varName)}` }),
      VENDOR_META[v].short,
    ]);
    chip.addEventListener("click", () => {
      if (activeVendors.has(v)) activeVendors.delete(v); else activeVendors.add(v);
      eventsExpanded = false;
      render();
    });
    chip.dataset.vendor = v;
    wrap.appendChild(chip);
  }
}

function wireWindowSegmented() {
  const seg = document.getElementById("window-segmented");
  for (const btn of seg.querySelectorAll("button")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.window === activeWindow));
    btn.addEventListener("click", () => {
      activeWindow = btn.dataset.window;
      eventsExpanded = false;
      render();
    });
  }
}

function renderForecastNote(forecast) {
  const note = el("div", { class: "forecast-note" });
  if (!forecast.available) {
    note.appendChild(el("div", { class: "forecast-value" }, ["Outlier"]));
    note.appendChild(el("p", { class: "forecast-copy" }, [
      `The current wait is longer than every previous gap (past maximum ${fmtDays(forecast.maxGap)}). History cannot estimate the next seven days.`,
    ]));
    return note;
  }

  const pct = Math.round(forecast.probability * 100);
  note.appendChild(el("div", { class: "forecast-value" }, [`${pct}%`]));
  const copy = el("p", { class: "forecast-copy" }, [
    `historical chance of a move within ${FORECAST_HORIZON_DAYS} days, from ${forecast.sampleSize} comparable gap${forecast.sampleSize === 1 ? "" : "s"}.`,
  ]);
  if (forecast.lowConfidence) {
    copy.appendChild(el("strong", {}, [
      ` Small sample: ${forecast.totalGaps} gaps total.`,
    ]));
  }
  note.appendChild(copy);
  return note;
}

function renderObservations(vendor) {
  const rows = vendor.observations || [];
  if (!rows.length) return null;
  const shown = rows.slice(0, OBSERVATIONS_SHOWN);

  const list = el("ol", { class: "observation-list" });
  for (const row of shown) {
    // Falling back to the raw string would print an internal label such as
    // `self_applied` to a reader; the static renderer drops the row, so this
    // one must too or the two pages disagree on the one case that matters.
    if (!VERDICT_LABELS[row.verdict]) continue;
    const item = el("li", { class: "observation", "data-verdict": row.verdict });
    const head = el("div", { class: "observation-head" }, [
      el("span", { class: "observation-verdict" }, [VERDICT_LABELS[row.verdict]]),
    ]);
    const observedAt = row.observed_at ? new Date(row.observed_at) : null;
    if (observedAt && !Number.isNaN(observedAt.getTime())) {
      head.appendChild(el("time", { datetime: row.observed_at }, [formatPacificDateTime(observedAt)]));
    }
    item.appendChild(head);
    if (row.headline) item.appendChild(el("p", { class: "observation-headline" }, [row.headline]));
    if (row.evidence) item.appendChild(el("p", { class: "observation-evidence" }, [row.evidence]));
    list.appendChild(item);
  }

  const parts = [];
  if (rows.length > shown.length) parts.push(`Showing the ${shown.length} most recent of ${rows.length}.`);
  const coverage = vendor.probe && vendor.probe.coverage;
  parts.push(typeof coverage === "string" && coverage.trim() ? coverage.trim() : DEFAULT_COVERAGE);
  const note = el("p", { class: "observation-note" }, [`${parts.join(" ")} `]);
  note.appendChild(el("a", { href: "methodology.html#ground-truth" }, ["How a reset is told from an expiry ↗"]));

  return el("div", { class: "observations" }, [
    el("p", { class: "observations-label" }, ["Our own account"]),
    list,
    note,
  ]);
}

function hasSignal(vendor) {
  return Boolean(vendor && ((vendor.events && vendor.events.length) || (vendor.observations && vendor.observations.length)));
}

function renderVendorSection(vendorKey, now) {
  const vendor = DATA.vendors[vendorKey];
  const meta = VENDOR_META[vendorKey];
  const vendorColor = cssVar(meta.varName);
  const events = vendor.eventsParsed || [];
  const badge = probeBadge(vendor.probe, now, vendor.observations);

  const section = el("article", { class: "vendor", style: `--vendor-color:${vendorColor}` });
  section.appendChild(el("div", { class: "vendor-head" }, [
    el("div", { class: "vendor-identity" }, [
      el("span", { class: "vendor-number" }, [`0${VENDOR_ORDER.indexOf(vendorKey) + 1}`]),
      el("div", {}, [
        el("p", { class: "vendor-company" }, [meta.company]),
        el("h3", {}, [meta.product]),
      ]),
    ]),
    el("span", { class: "signal-status", "data-probe": badge.state }, [badge.text]),
  ]));

  // A vendor can be probe-only: data/openai.json is a gitignored fetch cache,
  // so the announcement feed can be empty while the probe still has verdicts.
  // Everything below this point is about announcements and must not pretend to
  // summarise an empty list.
  if (events.length) {
    const stats = computeStats(events, activeWindow, now);
    const windowLabel = WINDOW_META[activeWindow].label === "All" ? "all time" : `last ${WINDOW_META[activeWindow].label}`;

    section.appendChild(el("div", { class: "primary-stat" }, [
      el("div", { class: "stat-label" }, ["Last tracked move"]),
      el("div", { class: "primary-stat-value" }, [
        el("strong", {}, [fmtDays(stats.daysSinceLast)]),
        el("span", {}, ["ago"]),
      ]),
    ]));

    const kpiRow = el("div", { class: "kpi-row" });
    const tiles = [
      ["Typical gap", fmtDays(stats.avgIntervalDays), stats.inWindow.length < 2 ? `insufficient data · ${windowLabel}` : windowLabel],
      ["Longest gap", fmtDays(stats.longestGapDays), "all time"],
      ["Tracked", String(stats.total), "events"],
    ];
    for (const [label, value, sub] of tiles) {
      kpiRow.appendChild(el("div", { class: "kpi-tile" }, [
        el("div", { class: "kpi-label" }, [label]),
        el("div", { class: "kpi-value" }, [value]),
        el("div", { class: "kpi-sub" }, [sub]),
      ]));
    }
    section.appendChild(kpiRow);

    const forecast = computeForecast(events, now);
    if (forecast) section.appendChild(renderForecastNote(forecast));

    const domain = sharedDomain(activeWindow, now);
    const timelineWrap = el("div", { class: "timeline-wrap" });
    timelineWrap.appendChild(el("p", { class: "timeline-label" }, [`Moves · ${windowLabel}`]));
    timelineWrap.appendChild(buildTimelineSVG(vendorKey, domain, now));
    section.appendChild(timelineWrap);
  } else {
    section.appendChild(el("p", { class: "static-event-copy" }, [NO_ANNOUNCEMENTS_COPY]));
  }

  if (vendor.source) {
    const details = el("details", { class: "source-details" });
    details.appendChild(el("summary", {}, ["About this source"]));
    const sourceCopy = el("p", {}, []);
    sourceCopy.appendChild(el("a", { href: vendor.source.url, target: "_blank", rel: "noopener" }, [vendor.source.name]));
    if (vendor.source.note) sourceCopy.appendChild(document.createTextNode(` — ${vendor.source.note}`));
    details.appendChild(sourceCopy);
    section.appendChild(details);
  }

  const observations = renderObservations(vendor);
  if (observations) section.appendChild(observations);

  return section;
}

function renderEventsFeed(now) {
  const list = document.getElementById("events-list");
  const count = document.getElementById("events-count");
  const toggle = document.getElementById("events-toggle");
  list.textContent = "";
  let rows = [];
  for (const v of VENDOR_ORDER) {
    if (!activeVendors.has(v) || !DATA.vendors[v]) continue;
    const stats = computeStats(DATA.vendors[v].eventsParsed, activeWindow, now);
    for (const e of stats.inWindow) rows.push({ vendor: v, ...e });
  }
  rows.sort((a, b) => b.date - a.date);
  count.textContent = `${rows.length} event${rows.length === 1 ? "" : "s"}`;

  const visibleRows = eventsExpanded ? rows : rows.slice(0, EVENTS_PREVIEW_LIMIT);
  for (const r of visibleRows) {
    const meta = VENDOR_META[r.vendor];
    const cleanText = r.text.replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim();
    const item = el("article", {
      class: "event-item",
      style: `--vendor-color:${cssVar(meta.varName)}`,
    });
    item.appendChild(el("div", { class: "event-meta" }, [
      el("time", { datetime: r.announced_at }, [formatEventTimestamp(r)]),
      el("span", {}, [meta.company]),
    ]));
    item.appendChild(el("div", { class: "event-content" }, [
      el("span", { class: "event-kind" }, [r.kind]),
      el("p", {}, [cleanText]),
    ]));
    item.appendChild(el("a", { class: "event-link", href: r.url, target: "_blank", rel: "noopener" }, ["Original ↗"]));
    list.appendChild(item);
  }

  if (!rows.length) {
    list.appendChild(el("p", { class: "empty-state" }, ["No tracked moves in this window. Try a longer range."]));
  }

  toggle.hidden = rows.length <= EVENTS_PREVIEW_LIMIT;
  toggle.textContent = eventsExpanded ? `Show latest ${EVENTS_PREVIEW_LIMIT}` : `Show all ${rows.length} events`;
}

function pacificTimingParts(date) {
  const parts = Object.fromEntries(
    PACIFIC_TIMING_FORMAT.formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return { weekday: parts.weekday, hour: Number(parts.hour) % 24 };
}

function computeTimingPattern(events, now) {
  const cells = TIMING_WEEKDAYS.map(() => TIMING_PERIODS.map(() => 0));
  const recent = [];
  const dated = [];
  const cutoff = new Date(now - 30 * 86400000);

  for (const event of events) {
    const date = event.date || new Date(event.announced_at);
    if (Number.isNaN(date.getTime())) continue;
    const { weekday, hour } = pacificTimingParts(date);
    const weekdayIndex = TIMING_WEEKDAYS.indexOf(weekday);
    const periodIndex = TIMING_PERIODS.findIndex((period) => hour >= period.start && hour < period.end);
    if (weekdayIndex < 0 || periodIndex < 0) continue;
    cells[weekdayIndex][periodIndex] += 1;
    dated.push(date);
    if (date >= cutoff && date <= now) recent.push({ date, weekdayIndex, periodIndex });
  }

  return {
    cells,
    total: dated.length,
    recentTotal: recent.length,
    fridayTotal: cells[4].reduce((sum, count) => sum + count, 0),
    fridayEvening: cells[4][3],
    recentFriday: recent.filter((event) => event.weekdayIndex === 4).length,
    first: dated.length ? new Date(Math.min(...dated)) : null,
    last: dated.length ? new Date(Math.max(...dated)) : null,
  };
}

function renderTimingPattern(now) {
  const target = document.getElementById("timing-insight");
  const events = DATA.vendors.openai?.eventsParsed || [];
  const stats = computeTimingPattern(events, now);
  const maximum = Math.max(1, ...stats.cells.flat());
  target.textContent = "";

  const callout = el("article", { class: "pattern-callout" }, [
    el("p", { class: "panel-label" }, ["Recent observation"]),
    el("strong", {}, [
      stats.recentTotal
        ? `${stats.recentFriday} of ${stats.recentTotal} announcements landed on Friday in the last 30 days.`
        : "No announcements were tracked in the last 30 days.",
    ]),
    el("p", {}, [
      `Across all ${stats.total} tracked events, Friday accounts for ${stats.fridayTotal} (${stats.total ? Math.round(100 * stats.fridayTotal / stats.total) : 0}%), including ${stats.fridayEvening} Friday evenings. That is not yet a statistically reliable weekly routine.`,
    ]),
  ]);
  if (stats.first && stats.last) {
    callout.appendChild(el("p", { class: "pattern-range" }, [
      `${formatPacificMonthDay(stats.first)}, ${stats.first.getUTCFullYear()} – ${formatPacificMonthDay(stats.last)}, ${stats.last.getUTCFullYear()} PT · public announcement time, not internal execution time`,
    ]));
  }

  const heatmap = el("div", { class: "heatmap", role: "table", "aria-label": "Codex announcement counts by Pacific weekday and time of day" });
  const head = el("div", { class: "heat-head", role: "row" }, [el("span", { "aria-hidden": "true" })]);
  for (const period of TIMING_PERIODS) head.appendChild(el("span", { role: "columnheader" }, [period.label]));
  heatmap.appendChild(head);
  TIMING_WEEKDAYS.forEach((weekday, weekdayIndex) => {
    const row = el("div", { class: "heat-row", role: "row" }, [
      el("span", { class: "heat-day", role: "rowheader" }, [weekday.slice(0, 3)]),
    ]);
    TIMING_PERIODS.forEach((period, periodIndex) => {
      const count = stats.cells[weekdayIndex][periodIndex];
      row.appendChild(el("span", {
        class: "heat-cell",
        role: "cell",
        style: `--heat:${(count / maximum).toFixed(3)}`,
        "aria-label": `${weekday} ${period.label}: ${count} events`,
      }, [el("strong", {}, [String(count)])]));
    });
    heatmap.appendChild(row);
  });
  const legend = el("p", { class: "heat-legend" }, [el("span", {}, ["Fewer"])]);
  for (let index = 0; index < 4; index++) legend.appendChild(el("i"));
  legend.appendChild(el("span", {}, ["More announcements"]));
  const heatmapWrap = el("div", { class: "heatmap-wrap" }, [heatmap, legend]);
  target.appendChild(el("div", { class: "timing-summary-grid" }, [callout, heatmapWrap]));
}

function render() {
  const now = new Date();

  for (const chip of document.querySelectorAll("#vendor-chips .chip")) {
    chip.setAttribute("aria-pressed", String(activeVendors.has(chip.dataset.vendor)));
  }
  for (const btn of document.querySelectorAll("#window-segmented button")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.window === activeWindow));
  }

  const main = document.getElementById("vendor-sections");
  main.textContent = "";
  for (const v of VENDOR_ORDER) {
    if (activeVendors.has(v) && hasSignal(DATA.vendors[v])) main.appendChild(renderVendorSection(v, now));
  }
  if (!activeVendors.size) main.appendChild(el("p", { style: "color:var(--text-muted)" }, ["Pick at least one vendor above."]));

  renderEventsFeed(now);
  renderTimingPattern(now);
}

// ─── Pace calculator ───

function setRing(el, circumference, fraction) {
  const clamped = Math.max(0, Math.min(1, fraction));
  el.style.strokeDasharray = `${circumference} ${circumference}`;
  el.style.strokeDashoffset = String(circumference * (1 - clamped));
}

function initCalc() {
  const presetSel = document.getElementById("calc-vendor");
  const windowInput = document.getElementById("calc-window-days");
  const lastResetInput = document.getElementById("calc-last-reset");
  const rateInput = document.getElementById("calc-target-rate");
  const actualInput = document.getElementById("calc-actual");
  const figureEl = document.getElementById("calc-target-figure");
  const detailEl = document.getElementById("calc-detail");
  const formulaEl = document.getElementById("calc-formula");
  const targetArc = document.getElementById("ring-target-arc");
  const actualArc = document.getElementById("ring-actual-arc");

  const threeDaysAgo = new Date(Date.now() - 3 * 86400000);
  lastResetInput.value = toPacificInputValue(threeDaysAgo);

  function applyPreset() {
    const preset = CALC_PRESETS[presetSel.value];
    if (preset) {
      windowInput.value = preset.days;
      rateInput.value = preset.rate;
    }
    recompute();
  }

  function recompute() {
    const windowDays = parseFloat(windowInput.value);
    const rate = parseFloat(rateInput.value);
    const lastReset = lastResetInput.value ? parsePacificInputValue(lastResetInput.value) : null;

    if (!lastReset || !windowDays || windowDays <= 0 || Number.isNaN(rate)) {
      figureEl.textContent = "—";
      detailEl.textContent = "Fill in a last-reset time and a window length to see your target.";
      formulaEl.textContent = "";
      setRing(targetArc, RING_C_OUTER, 0);
      setRing(actualArc, RING_C_INNER, 0);
      return;
    }

    const now = new Date();
    const elapsedMs = now - lastReset;
    const elapsedPct = (elapsedMs / (windowDays * 86400000)) * 100;
    const clampedElapsedPct = Math.max(0, Math.min(100, elapsedPct));
    const targetPct = clampedElapsedPct * (rate / 100);

    figureEl.textContent = targetPct.toFixed(1);
    setRing(targetArc, RING_C_OUTER, targetPct / 100);
    formulaEl.textContent =
      `target% = elapsed% × target rate = ${clampedElapsedPct.toFixed(1)}% × ${rate}% ` +
      `(window ${windowDays.toFixed(2)}d from last reset)`;

    const actualRaw = actualInput.value;
    if (actualRaw === "") {
      setRing(actualArc, RING_C_INNER, 0);
      detailEl.textContent = elapsedPct > 100
        ? "This window is overdue for a reset by this math — treat the target as moot until it actually resets."
        : "Enter your current usage % above to compare against this target.";
      return;
    }

    const actual = parseFloat(actualRaw);
    setRing(actualArc, RING_C_INNER, actual / 100);
    const delta = actual - targetPct;
    const sign = delta >= 0 ? "+" : "";
    let status, color;
    if (delta < -5) { status = "conserving quota, well under pace"; color = "var(--text-muted)"; }
    else if (delta <= 5) { status = "on pace"; color = "var(--accent)"; }
    else { status = "ahead of pace"; color = "var(--good)"; }
    actualArc.style.stroke = color;
    detailEl.textContent = `Your usage: ${actual}% (${sign}${delta.toFixed(0)}% vs target) — ${status}.`;
  }

  presetSel.addEventListener("change", applyPreset);
  for (const inp of [windowInput, lastResetInput, rateInput, actualInput]) {
    inp.addEventListener("input", recompute);
  }
  applyPreset();
}

// ─── Theme toggle ───

function initTheme() {
  const btn = document.getElementById("theme-toggle");
  const saved = localStorage.getItem("ai-resets-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  btn.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ai-resets-theme", next);
    render(); // re-read CSS vars for the new theme's dot colors
  });
}

// ─── Email subscriptions ───

function initSubscription() {
  const form = document.getElementById("subscribe-form");
  const emailInput = document.getElementById("subscribe-email");
  const websiteInput = document.getElementById("subscribe-website");
  const submit = form.querySelector("button[type=\"submit\"]");
  const submitLabel = submit.querySelector("span");
  const status = document.getElementById("subscribe-status");

  function setStatus(message, state) {
    status.textContent = message;
    status.dataset.state = state || "";
  }

  const url = new URL(window.location.href);
  const result = url.searchParams.get("subscription");
  const resultMessages = {
    confirmed: ["Subscription confirmed. We’ll email you when one of your selected providers has a new signal.", "success"],
    preferences: ["Alert preferences saved.", "success"],
    unsubscribed: ["You’re unsubscribed. No more alerts will be sent.", "success"],
    invalid: ["That link is invalid or expired. You can request a fresh one below.", "error"],
  };
  if (resultMessages[result]) {
    setStatus(...resultMessages[result]);
    url.searchParams.delete("subscription");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    document.getElementById("subscribe").scrollIntoView({ block: "center" });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const topics = [...form.querySelectorAll('input[name="topics"]:checked')].map((input) => input.value);
    if (!topics.length) {
      setStatus("Choose at least one provider.", "error");
      return;
    }
    submit.disabled = true;
    submitLabel.textContent = "Sending…";
    setStatus("", "");
    try {
      const response = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: emailInput.value,
          website: websiteInput.value,
          topics,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not subscribe.");
      setStatus(payload.message, "success");
      window.aiResetTrack?.("subscription_requested", { providers: topics.join(",") });
      emailInput.value = "";
    } catch (error) {
      setStatus(error.message || "Could not subscribe. Please try again.", "error");
    } finally {
      submit.disabled = false;
      submitLabel.textContent = "Subscribe";
    }
  });
}

// ─── Footer ───

function initFooter() {
  const footer = document.getElementById("footer-meta");
  footer.textContent = "";
  const generated = new Date(DATA.generated_at);
  document.getElementById("hero-updated").textContent = `Updated ${formatPacificDateTime(generated)}`;
  footer.appendChild(el("p", {}, [
    `Data built ${formatPacificDateTime(generated)} · independent and unaffiliated.`,
  ]));
  const credits = el("p", {}, ["Sources: "]);
  credits.appendChild(el("a", { href: "https://codex-resets.com/", target: "_blank", rel: "noopener" }, ["codex-resets.com"]));
  credits.appendChild(document.createTextNode(" (OpenAI), "));
  credits.appendChild(el("a", { href: "https://x.com/ClaudeDevs", target: "_blank", rel: "noopener" }, ["@ClaudeDevs"]));
  credits.appendChild(document.createTextNode(" (Anthropic), and Google’s official blogs."));
  footer.appendChild(credits);
}

// ─── Init ───

initSubscription();
document.getElementById("events-toggle").addEventListener("click", () => {
  eventsExpanded = !eventsExpanded;
  render();
});

fetch("data.json")
  .then((r) => r.json())
  .then((data) => {
    DATA = data;
    parseAll();
    buildVendorChips();
    wireWindowSegmented();
    render();
    initCalc();
    initFooter();
    initTheme();
  })
  .catch((err) => {
    document.getElementById("vendor-sections").textContent = `Failed to load data.json: ${err}`;
  });
