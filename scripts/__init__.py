"""AI Reset Watch scripts.

A real package so that the entry-point scripts (run by absolute path from
systemd and cron) and the unit tests (run from the repository root) resolve the
shared library modules — timefmt, incidents, groundtruth — by the same name.
"""
