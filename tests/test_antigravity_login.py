"""The sign-in helper, and the one thing about it that is easy to get wrong.

`agy` draws its consent URL inside a box and HARD-WRAPS it across six rows. The
first version of this helper grepped the pane for `https://...` and took what it
found, which was the first row and nothing else. That produced
`...apps.googleus` — a non-empty string, so every emptiness check passed, and a
completely dead link handed to whoever was trying to log in.

So the fixture here is a real pane, wrapping and all, with the PKCE challenge
and state nonce replaced by placeholders. Neither is a credential (the challenge
is the public half of the pair and the nonce is meaningless outside the live
process), but a recorded file has no business carrying either.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "antigravity_login.sh"
PANE = REPO / "tests" / "fixtures" / "antigravity-consent-pane.txt"

BASH = shutil.which("bash")


@unittest.skipIf(BASH is None, "bash is not available on this machine")
class ConsentUrlTests(unittest.TestCase):
    """Run the helper's own function over a recorded pane."""

    def run_consent_url(self, pane_text: str) -> str:
        # Source the script's function rather than reimplementing it: a copy of
        # the awk program in this file would pass while the shipped one broke.
        harness = (
            f'set -euo pipefail\n'
            f'eval "$(sed -n "/^consent_url()/,/^}}/p" {SCRIPT!s})"\n'
            f'consent_url\n'
        )
        result = subprocess.run(
            [BASH, "-c", harness],
            input=pane_text, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_whole_url_is_rebuilt_from_six_wrapped_rows(self):
        url = self.run_consent_url(PANE.read_text(encoding="utf-8"))
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/auth?"))
        # `state` is the final query parameter, so reaching it means the tail
        # survived. This is the assertion the old grep failed.
        self.assertTrue(url.endswith("&state=REDACTED-STATE-NONCE"), url[-60:])
        self.assertIn("client_id=", url)
        self.assertIn("redirect_uri=", url)
        self.assertNotIn(" ", url)
        self.assertNotIn("\n", url)

    def test_it_recovers_more_than_the_first_row(self):
        # Guards the exact regression: the first row of the box is 130-odd
        # characters, and the real URL is several times that.
        url = self.run_consent_url(PANE.read_text(encoding="utf-8"))
        first_row = next(
            line.strip() for line in PANE.read_text(encoding="utf-8").splitlines()
            if "https://accounts.google.com" in line
        )
        self.assertGreater(len(url), 2 * len(first_row))

    def test_it_stops_at_the_box_border_and_takes_no_prose(self):
        url = self.run_consent_url(PANE.read_text(encoding="utf-8"))
        self.assertNotIn("After", url)
        self.assertNotIn("authorization", url)
        self.assertNotIn("─", url)

    def test_a_pane_with_no_url_yields_nothing_rather_than_garbage(self):
        url = self.run_consent_url("Welcome to the Antigravity CLI.\nSelect login method:\n")
        self.assertEqual(url, "")


class ScriptShapeTests(unittest.TestCase):
    def test_the_script_is_executable(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "not executable")

    @unittest.skipIf(BASH is None, "bash is not available on this machine")
    def test_it_parses(self):
        result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_usage_text_names_exactly_the_subcommands_that_exist(self):
        # The header doubles as the no-argument help, so a subcommand added
        # without a line there is a subcommand nobody can find.
        text = SCRIPT.read_text(encoding="utf-8")
        documented = set(re.findall(r"antigravity_login\.sh (\w+)", text))
        implemented = set(re.findall(r"^  (\w+)\)$", text, re.MULTILINE))
        self.assertEqual(documented, implemented)

    def test_the_url_is_validated_before_it_is_shown_to_anyone(self):
        # Non-empty is not the test; whole is. Without this the helper's failure
        # mode is a plausible-looking truncated link.
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("*client_id=*state=*)", text)

    def test_the_google_cloud_project_option_is_never_selected(self):
        # Option 2 measures a GCP-billed quota, not the subscription quota this
        # project exists to observe — the wrong ground truth entirely.
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Google Cloud project", text)
        self.assertNotIn('send-keys -t "$SESSION" 2', text)


if __name__ == "__main__":
    unittest.main()
