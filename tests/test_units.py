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

import os
import pwd
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

    def test_the_second_codex_home_is_writable_only_optionally(self):
        # The template's own promise, before any rendering: account b's write
        # path carries the `-` that keeps a missing home from stopping the unit.
        found = directives(template("ai-resets-probe.service").read_text(encoding="utf-8"))
        second = [value for value in found["ReadWritePaths"] if "__CODEX_HOME_B__" in value]
        self.assertEqual(second, ["-__CODEX_HOME_B__"])
        self.assertIn("AI_RESETS_CODEX_HOME_B=__CODEX_HOME_B__", found["Environment"])

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


def installer(*args: str, env_extra: dict | None = None, unset: tuple = ()) -> str:
    """Run the installer in a mode that installs nothing; return its stdout."""
    env = {key: value for key, value in os.environ.items() if key not in unset}
    result = subprocess.run(
        ["bash", str(INSTALLER), *args],
        capture_output=True, text=True, timeout=120, cwd=REPO,
        env={**env, **(env_extra or {})},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


# The second home's own variable is unset unless a test sets it: a shell that
# exports one must not decide what these tests see.
SECOND = "AI_RESETS_CODEX_HOME_B"


class InstallerResolutionTests(unittest.TestCase):
    """The installer's host-value resolution, checked without installing."""

    def resolve(self, env_extra: dict, unset: tuple = (SECOND,)) -> dict[str, str]:
        """The `--show` report as {name: value}."""
        values = {}
        for line in installer("--show", env_extra=env_extra, unset=unset).splitlines():
            if line.startswith("  ") and not line.startswith("   "):
                name, _, value = line.strip().partition(" ")
                values[name] = value.strip()
        return values

    def test_a_leaked_interactive_codex_home_does_not_move_the_probe(self):
        # The owner exports CODEX_HOME for their own profile-based Codex.
        # Under `sudo -E` that export reaches this installer, and the probe
        # unit would render paths for a home the spawned codex never reads
        # while its real one stays read-only behind ProtectHome.
        values = self.resolve({"CODEX_HOME": "/home/bogus/interactive"})
        self.assertNotIn("bogus", values["CODEX_HOME"])
        # Nor does it become the second account.
        self.assertNotIn("bogus", values["CODEX_HOME_B"])

    def test_the_probe_codex_home_has_its_own_override(self):
        values = self.resolve(
            {"CODEX_HOME": "/home/bogus/interactive", "AI_RESETS_CODEX_HOME": "/home/bogus/probe"}
        )
        self.assertEqual(values["CODEX_HOME"], "/home/bogus/probe")

    def test_the_second_codex_home_has_its_own_override(self):
        values = self.resolve(
            {"CODEX_HOME": "/home/bogus/interactive", SECOND: "/home/bogus/second"}
        )
        self.assertEqual(values["CODEX_HOME_B"], "/home/bogus/second")
        self.assertNotIn("bogus/second", values["CODEX_HOME"])

    def test_an_explicitly_empty_second_home_means_no_second_account(self):
        values = self.resolve({SECOND: ""}, unset=())
        self.assertTrue(values["CODEX_HOME_B"].startswith("(none"), values)

    def test_the_default_is_the_profile_home_only_when_it_holds_a_login(self):
        # Whichever way this machine is set up, the installer must agree with
        # the rule: the profile home when its auth.json exists, else none.
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
        home = Path(pwd.getpwnam(user).pw_dir) / ".codex-profiles" / "main1"
        values = self.resolve({})
        if (home / "auth.json").is_file():
            self.assertEqual(values["CODEX_HOME_B"], str(home))
        else:
            self.assertTrue(values["CODEX_HOME_B"].startswith("(none"), values)

    def test_a_profile_home_without_a_login_is_not_a_second_account(self):
        # `nobody` has no home at all, so no auth.json can be there.
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            raise unittest.SkipTest("no `nobody` account on this host")
        if (Path(account.pw_dir) / ".codex-profiles" / "main1" / "auth.json").exists():
            raise unittest.SkipTest("`nobody` has a Codex login here")
        values = self.resolve({"AI_RESETS_USER": "nobody"})
        self.assertTrue(values["CODEX_HOME_B"].startswith("(none"), values)


class RenderTests(unittest.TestCase):
    """What `--render` writes for the probe unit, with and without account b."""

    def render(self, second: str | None, **extra: str) -> str:
        """The probe unit; `second` None leaves the installer to its default."""
        extra = {"AI_RESETS_CODEX_HOME": "/srv/probe/codex-a", **extra}
        if second is not None:
            extra[SECOND] = second
        return installer("--render", "ai-resets-probe.service", env_extra=extra, unset=(SECOND,))

    def test_a_second_home_is_passed_and_punched_through_optionally(self):
        found = directives(self.render("/srv/probe/codex-b"))
        self.assertIn("AI_RESETS_CODEX_HOME_B=/srv/probe/codex-b", found["Environment"])
        self.assertIn("CODEX_HOME=/srv/probe/codex-a", found["Environment"])
        # `-`: a second home that has gone missing must not stop the unit.
        self.assertEqual(found["ReadWritePaths"], ["/srv/probe/codex-a", "-/srv/probe/codex-b"])

    def assert_no_second_account(self, text: str) -> None:
        found = directives(text)
        self.assertEqual(found["ReadWritePaths"], ["/srv/probe/codex-a"])
        self.assertFalse(
            any(value.startswith("AI_RESETS_CODEX_HOME_B") for value in found["Environment"])
        )
        self.assertNotIn("__CODEX_HOME_B__", text)

    def test_no_second_home_leaves_no_line_for_it(self):
        # An empty `ReadWritePaths=` RESETS the list, which would take account
        # a's own write path with it and hang every read.
        self.assert_no_second_account(self.render(""))

    def test_a_host_with_no_second_login_renders_no_second_account(self):
        try:
            pwd.getpwnam("nobody")
        except KeyError:
            raise unittest.SkipTest("no `nobody` account on this host")
        self.assert_no_second_account(self.render(None, AI_RESETS_USER="nobody"))

    def test_every_unit_renders_with_no_placeholder_left(self):
        for name in UNIT_NAMES:
            for second in ("/srv/probe/codex-b", ""):
                with self.subTest(unit=name, second=second):
                    text = installer("--render", name, env_extra={SECOND: second}, unset=(SECOND,))
                    self.assertEqual(PLACEHOLDER.findall(text), [])

    def test_an_unknown_unit_is_refused_with_its_usage(self):
        with self.assertRaisesRegex(AssertionError, "usage: .* --render <unit name>"):
            installer("--render", "no-such.service")


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
