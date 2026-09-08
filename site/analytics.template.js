"use strict";
// ai-resets-analytics: enabled

// Privacy-minimized PostHog fallback for a site behind DNS-only DNS, where the
// CDN's own analytics are unavailable. No SDK, cookies, localStorage, session
// recording, autocapture, query strings, email addresses, or persistent user
// profiles.
//
// TEMPLATE. `scripts/build.py` renders this into the gitignored
// `site/analytics.js`, substituting the project key from
// AI_RESETS_POSTHOG_KEY. With that variable unset — the default, and what a
// fork gets — build.py writes an inert file instead of this one, so nothing
// here is ever served without a key deliberately supplied. The key lives in
// the environment rather than in this file because a project key committed to
// a public repository is a project key anyone can write events into.
(() => {
  const POSTHOG_KEY = "__AI_RESETS_POSTHOG_KEY__";
  const POSTHOG_INGEST = "https://us.i.posthog.com/i/v0/e/";
  const isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0)$/.test(window.location.hostname);
  const privacyRequested = navigator.globalPrivacyControl === true || navigator.doNotTrack === "1";
  if (isLocal || privacyRequested) return;

  const pageViewId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const safeReferrerHost = (() => {
    if (!document.referrer) return "";
    try { return new URL(document.referrer).hostname; } catch (_error) { return ""; }
  })();

  function track(event, properties = {}) {
    const payload = JSON.stringify({
      api_key: POSTHOG_KEY,
      event,
      properties: {
        distinct_id: pageViewId,
        site: "ai-reset-watch",
        $host: window.location.host,
        $pathname: window.location.pathname,
        $current_url: `${window.location.origin}${window.location.pathname}`,
        $title: document.title,
        $referrer_host: safeReferrerHost,
        $process_person_profile: false,
        $geoip_disable: true,
        ...properties,
      },
    });
    const body = new Blob([payload], { type: "application/json" });
    if (!navigator.sendBeacon?.(POSTHOG_INGEST, body)) {
      fetch(POSTHOG_INGEST, { method: "POST", body, keepalive: true, mode: "cors" }).catch(() => {});
    }
  }

  window.aiResetTrack = track;
  track("$pageview");
})();
