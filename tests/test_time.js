"use strict";

const assert = require("node:assert/strict");
const {
  formatPacificDateTime,
  formatEventTimestamp,
  parsePacificInputValue,
  toPacificInputValue,
} = require("../site/time.js");

assert.equal(
  formatPacificDateTime(new Date("2026-07-29T04:09:02Z")),
  "Jul 28, 2026, 9:09 PM PDT",
);
assert.equal(
  formatPacificDateTime(new Date("2026-01-29T04:09:02Z")),
  "Jan 28, 2026, 8:09 PM PST",
);
assert.equal(
  formatEventTimestamp({
    announced_at: "2026-07-16T00:00:00Z",
    confidence: "approx",
  }),
  "~Jul 16, 2026",
);

const summer = parsePacificInputValue("2026-07-28T21:09");
assert.equal(summer.toISOString(), "2026-07-29T04:09:00.000Z");
assert.equal(toPacificInputValue(summer), "2026-07-28T21:09");

const winter = parsePacificInputValue("2026-01-28T20:09");
assert.equal(winter.toISOString(), "2026-01-29T04:09:00.000Z");
assert.equal(toPacificInputValue(winter), "2026-01-28T20:09");

assert.equal(parsePacificInputValue("2026-03-08T02:30"), null);
assert.equal(parsePacificInputValue("not-a-time"), null);

console.log("Pacific time helpers: ok");
