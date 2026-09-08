"use strict";

(function exposeTimeHelpers(root) {
  const PACIFIC_TIME_ZONE = "America/Los_Angeles";
  const LOCALE = "en-US";

  const pacificDateFormatter = new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: PACIFIC_TIME_ZONE,
  });
  const sourceDateFormatter = new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const pacificDateTimeFormatter = new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: PACIFIC_TIME_ZONE,
    timeZoneName: "short",
  });
  const pacificMonthDayFormatter = new Intl.DateTimeFormat(LOCALE, {
    month: "short",
    day: "numeric",
    timeZone: PACIFIC_TIME_ZONE,
  });
  const pacificPartsFormatter = new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZone: PACIFIC_TIME_ZONE,
  });

  function partsInPacific(date) {
    const parts = {};
    for (const part of pacificPartsFormatter.formatToParts(date)) {
      if (part.type !== "literal") parts[part.type] = Number(part.value);
    }
    return parts;
  }

  function formatPacificDate(date) {
    return pacificDateFormatter.format(date);
  }

  function formatPacificDateTime(date) {
    return pacificDateTimeFormatter.format(date);
  }

  function formatPacificMonthDay(date) {
    return pacificMonthDayFormatter.format(date);
  }

  function isDateOnlyTimestamp(value) {
    return /T00:00:00(?:\.000)?Z$/.test(value);
  }

  function formatEventTimestamp(event) {
    const prefix = event.confidence === "approx" ? "~" : "";
    const date = event.date instanceof Date ? event.date : new Date(event.announced_at);
    // Hand-curated sources often supply only a calendar date at UTC midnight.
    // Preserve that stated date instead of inventing a Pacific clock time.
    if (isDateOnlyTimestamp(event.announced_at)) {
      return prefix + sourceDateFormatter.format(date);
    }
    return prefix + formatPacificDateTime(date);
  }

  function toPacificInputValue(date) {
    const p = partsInPacific(date);
    const pad = (value) => String(value).padStart(2, "0");
    return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour)}:${pad(p.minute)}`;
  }

  function parsePacificInputValue(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
    if (!match) return null;

    const [, yearText, monthText, dayText, hourText, minuteText] = match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    const hour = Number(hourText);
    const minute = Number(minuteText);
    const targetMs = Date.UTC(year, month - 1, day, hour, minute, 0);
    const target = new Date(targetMs);
    if (
      target.getUTCFullYear() !== year ||
      target.getUTCMonth() !== month - 1 ||
      target.getUTCDate() !== day ||
      target.getUTCHours() !== hour ||
      target.getUTCMinutes() !== minute
    ) {
      return null;
    }

    // Convert a wall-clock time in America/Los_Angeles into an instant. The
    // iteration also rejects nonexistent local times during the spring DST jump.
    let guessMs = targetMs + 8 * 60 * 60 * 1000;
    for (let i = 0; i < 4; i += 1) {
      const p = partsInPacific(new Date(guessMs));
      const observedMs = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
      const correction = targetMs - observedMs;
      if (correction === 0) return new Date(guessMs);
      guessMs += correction;
    }
    return null;
  }

  const api = {
    PACIFIC_TIME_ZONE,
    formatPacificDate,
    formatPacificDateTime,
    formatPacificMonthDay,
    formatEventTimestamp,
    toPacificInputValue,
    parsePacificInputValue,
  };

  root.AIResetTime = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis === "object" ? globalThis : window);
