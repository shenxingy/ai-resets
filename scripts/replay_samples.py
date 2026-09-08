#!/usr/bin/env python3
"""Re-derive the event log from the samples the probe already kept.

The sample log is the raw record and it is complete back to 2026-08-30. The
event log is only what the detector concluded AT THE TIME, so it is missing
everything the rules of the day threw away. Two known losses:

  2026-08-30 7:26:56 PM PDT   16% -> 0%, the window re-anchored, the banked
      credit count unchanged, 140.6 hours before its scheduled expiry. A real
      vendor reset, which @thsottiaux announced two and a half minutes LATER.
      CLEAR_DROP_MIN was 20 at the time, so a 16-point drop was not even
      considered. Nothing was written.

  2026-09-02 00:10 EDT        a confirmed clear that the pre-fix retraction
      rule then re-emitted as "reverted" when ordinary usage resumed on the
      new anchor. Both records are still in the log and contradict each other.

Both are recoverable, because the samples were never lost. This replays the
whole sample history through the CURRENT detector and reports what the log is
missing. With --apply it appends those records, each stamped `backfilled` with
the ruleset that produced it, so a reader can always tell a live detection from
a later re-derivation.

Default is a dry run. The probe appends to the same file, so writes here use
O_APPEND and add only whole lines; records already present by event_id are
never duplicated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.quota_probe import (  # noqa: E402
    EVENTS_FILE,
    SAMPLES_FILE,
    Detector,
)
from scripts.timefmt import format_epoch_pacific  # noqa: E402


def event_key(record: dict) -> str:
    """Identity that survives records written before event_id existed."""
    stated = record.get("event_id")
    if stated:
        return f"{record.get('kind') or 'clear'}:{stated}"
    slot = record.get("slot")
    moment = record.get("detected_at") or record.get("t")
    kind = record.get("kind") or ("retraction" if record.get("reverted_at") else "clear")
    return f"{kind}:{slot}:{moment}"


def read_jsonl(path: Path) -> list[dict]:
    records = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(loaded, dict):
                    records.append(loaded)
    except OSError:
        return []
    return records


def derive(samples: list[dict]) -> list[dict]:
    detector = Detector()
    derived: list[dict] = []
    for row in samples:
        derived.extend(detector.observe(row))
    return derived


def describe(record: dict) -> str:
    moment = record.get("detected_at") or record.get("t")
    when = format_epoch_pacific(moment) if isinstance(moment, int) else "unknown time"
    kind = record.get("kind") or "clear"
    if kind == "credit_granted":
        detail = f"credits {record.get('credits_before')} -> {record.get('credits_after')}"
    else:
        detail = (
            f"{record.get('used_before', 0):.0f}% -> {record.get('used_after', 0):.0f}%"
            f" [{record.get('classification')}]"
        )
    return f"{when}  {kind:15s} {record.get('slot', '?'):22s} {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append the missing records (default is a dry run)",
    )
    parser.add_argument("--samples", type=Path, default=SAMPLES_FILE)
    parser.add_argument("--events", type=Path, default=EVENTS_FILE)
    args = parser.parse_args(argv)

    samples = read_jsonl(args.samples)
    if not samples:
        print(f"no samples at {args.samples}; nothing to replay")
        return 1
    existing = read_jsonl(args.events)
    known = {event_key(record) for record in existing}

    derived = derive(samples)
    missing = [record for record in derived if event_key(record) not in known]

    print(
        f"replayed {len(samples)} samples -> {len(derived)} events; "
        f"the log holds {len(existing)}; {len(missing)} missing"
    )
    for record in missing:
        print(f"  + {describe(record)}")
    if not missing:
        return 0
    if not args.apply:
        print("dry run; pass --apply to append these")
        return 0

    stamped = [
        {**record, "backfilled": True, "backfill_note": "re-derived from quota_samples.jsonl"}
        for record in missing
    ]
    with args.events.open("a", encoding="utf-8") as handle:
        for record in stamped:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    print(f"appended {len(stamped)} records to {args.events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
