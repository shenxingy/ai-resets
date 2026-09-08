"""The systemd unit templates, and whether the deployed copies still match.

This file exists because of a real gap, not a hypothetical one. The
`EnvironmentFile=-/etc/ai-resets/notify.env` line that carries OWNER_EMAIL was
written into the notify unit and committed, and then never installed.
`systemctl show ... -p EnvironmentFiles` was empty for days while the repository
said otherwise, so every operational alert went to the journal and nowhere else,
and nothing anywhere said so.

The units are templates now, because they carry absolute paths that would
otherwise publish one machine's home directory. That makes the drift check
harder in the obvious implementation and easier in the right one: instead of
re-deriving this host's values in Python and comparing — a second copy of the
logic, free to drift from the installer it is supposed to be checking — the
test runs `infra/install-units.sh --show` and asserts it has nothing to install.
Whatever the installer would write is what the test compares against, by
construction.

The check runs only on a host that actually has these units, so it is a no-op
in a clone and a real gate on the machine that serves the site.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO / "infra" / "units"
INSTALLER = REPO / "infra" / "install-units.sh"
INSTALLED = Path("/etc/systemd/system")

UNIT_NAMES = (
    "ai-resets-probe.service",
    "ai-resets-claude-probe.service",
    "ai-resets-subscribe.service",
    "ai-resets-notify.service",
    "ai-resets-notify.timer",
    "ai-resets-alert@.service",
)

# Every placeholder the installer knows how to fill. A template that grows a
# new one without teaching the installer would render a unit with a literal
# `__SOMETHING__` in a path, and systemd would accept it.
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


def template(name: str) -> Path:
    return TEMPLATE_DIR / f"{name}.in"


def directives(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        found.setdefault(key.strip(), []).append(value.strip())
    return found


class UnitContentTests(unittest.TestCase):
    """Properties every unit must have, checked against the templates."""

    def test_every_unit_named_here_exists(self):
        self.assertEqual(
            sorted(p.name for p in TEMPLATE_DIR.glob("*.in")),
            sorted(f"{name}.in" for name in UNIT_NAMES),
            "a unit was renamed or removed without updating UNIT_NAMES",
        )

    def test_no_template_carries_a_personal_path(self):
        # The whole point of the templates. A path under /home is exactly what
        # they exist to keep out of a public repository.
        for name in UNIT_NAMES:
            with self.subTest(unit=name):
                self.assertNotIn("/home/", template(name).read_text(encoding="utf-8"))

    def test_every_placeholder_is_one_the_installer_fills(self):
        known = set(PLACEHOLDER.findall(INSTALLER.read_text(encoding="utf-8")))
        for name in UNIT_NAMES:
            with self.subTest(unit=name):
                used = set(PLACEHOLDER.findall(template(name).read_text(encoding="utf-8")))
                self.assertLessEqual(
                    used, known,
                    f"{sorted(used - known)} appears in the template but "
                    "install-units.sh does not substitute it",
                )

    def test_a_probe_that_restarts_cannot_poll_faster_than_its_budget(self):
        # OnFailure= fires on EVERY restart cycle, and the Claude endpoint
        # throttles per token, so a short RestartSec turns a crash loop into a
        # rate-limit ban and an alert storm.
        for name, floor in (
            ("ai-resets-probe.service", 10),
            ("ai-resets-claude-probe.service", 60),
        ):
            with self.subTest(unit=name):
                found = directives(template(name).read_text(encoding="utf-8"))
                self.assertIn("RestartSec", found, name)
                self.assertGreaterEqual(int(found["RestartSec"][0]), floor)

    def test_the_claude_probe_never_gains_write_access_to_a_credential(self):
        # It reads the OAuth token and must never refresh it: refresh tokens
        # rotate, and a refresh from here would log the owner out of their own
        # editor.
        text = template("ai-resets-claude-probe.service").read_text(encoding="utf-8")
        found = directives(text)
        self.assertEqual(found.get("ProtectHome"), ["read-only"])
        for value in found.get("ReadWritePaths", []):
            self.assertNotIn(".claude", value)
        # tmpfs would hide /home entirely, including the repository the
        # ExecStart points at, and the unit would not start at all.
        self.assertNotIn("ProtectHome=tmpfs", text)

    def test_every_exec_start_is_an_absolute_path(self):
        for name in UNIT_NAMES:
            found = directives(template(name).read_text(encoding="utf-8"))
            for value in found.get("ExecStart", []):
                with self.subTest(unit=name):
                    self.assertTrue(value.startswith("/"), value)

    def test_the_notifier_can_be_told_where_to_send_alerts(self):
        # Without this the address has to be edited into the unit itself, and
        # the alerts silently stay in the journal until someone notices.
        found = directives(template("ai-resets-notify.service").read_text(encoding="utf-8"))
        files = found.get("EnvironmentFile", [])
        self.assertTrue(files, "the notifier has no EnvironmentFile")
        self.assertTrue(
            all(value.startswith("-") for value in files),
            "the file must be optional, or a missing one stops the notifier",
        )

    def test_every_unit_that_sends_mail_is_given_a_sending_identity(self):
        # RESEND_FROM_EMAIL has no default any more. A unit that can send but
        # cannot read the file holding that address fails at its next tick, and
        # for the alert unit that means failing precisely during an outage.
        for name in (
            "ai-resets-subscribe.service",
            "ai-resets-notify.service",
            "ai-resets-alert@.service",
        ):
            with self.subTest(unit=name):
                found = directives(template(name).read_text(encoding="utf-8"))
                self.assertIn(
                    "-/etc/ai-resets/service.env", found.get("EnvironmentFile", []),
                )


class InstalledUnitDriftTests(unittest.TestCase):
    """On the serving host, the installed units must match this repository.

    Skipped everywhere else. A missing unit here is not a failure — a laptop
    checkout has none — but a unit that exists and DIFFERS is the deploy gap
    that hid OWNER_EMAIL, and it is silent by nature.
    """

    def test_the_installer_has_nothing_left_to_install(self):
        if shutil.which("bash") is None:
            raise unittest.SkipTest("bash is not available on this machine")
        if not any((INSTALLED / name).is_file() for name in UNIT_NAMES):
            raise unittest.SkipTest("no ai-resets units are installed on this host")
        result = subprocess.run(
            ["bash", str(INSTALLER), "--show"],
            capture_output=True, text=True, timeout=120, cwd=REPO,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        offenders = [
            line for line in result.stdout.splitlines()
            if line.strip().startswith(("DIFFERS", "new "))
        ]
        self.assertEqual(
            offenders, [],
            "the installed units no longer match this repository. Reinstall "
            "them with `sudo -E ./infra/install-units.sh`. Full report:\n"
            + result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
