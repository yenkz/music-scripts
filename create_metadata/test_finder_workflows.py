import plistlib
import unittest
from pathlib import Path


WORKFLOW_DIRECTORY = Path(__file__).parent / "finder_workflows"


class FinderWorkflowTests(unittest.TestCase):
    def test_all_workflows_are_portable_applescript_actions(self):
        expected_arguments = {
            "Music Metadata — 1 Analyze.workflow": ' & " " & ',
            "Music Metadata — 2 Write Accepted.workflow": " --write ",
            "Music Metadata — Refresh Analysis.workflow": " --refresh-cache ",
            "Music Metadata — Force Analyze.workflow": " --force ",
        }

        for name, expected_argument in expected_arguments.items():
            with self.subTest(workflow=name):
                contents = WORKFLOW_DIRECTORY / name / "Contents"
                with (contents / "Info.plist").open("rb") as source:
                    info = plistlib.load(source)
                with (contents / "document.wflow").open("rb") as source:
                    document = plistlib.load(source)

                action = document["actions"][0]["action"]
                script = action["ActionParameters"]["source"]
                self.assertEqual(
                    action["ActionBundlePath"],
                    "/System/Library/Automator/Run AppleScript.action",
                )
                self.assertIn('set cmPath to', script)
                self.assertIn('".local/bin/cm"', script)
                self.assertIn('set shellCommand to "/bin/zsh "', script)
                self.assertIn(expected_argument, script)
                self.assertIn('set cmTab to do script ""', script)
                self.assertIn("repeat while (busy of cmTab)", script)
                self.assertIn("do script shellCommand in cmTab", script)
                self.assertLess(
                    script.index('set cmTab to do script ""'),
                    script.index("do script shellCommand in cmTab"),
                )
                self.assertNotIn("/Users/", script)
                self.assertEqual(
                    info["NSServices"][0]["NSSendFileTypes"], ["public.folder"]
                )

    def test_write_workflow_requires_confirmation(self):
        path = (
            WORKFLOW_DIRECTORY
            / "Music Metadata — 2 Write Accepted.workflow"
            / "Contents"
            / "document.wflow"
        )
        with path.open("rb") as source:
            document = plistlib.load(source)
        script = document["actions"][0]["action"]["ActionParameters"]["source"]

        self.assertIn("display dialog", script)
        self.assertIn('default button "Cancel"', script)
        self.assertNotIn("--force --write", script)


if __name__ == "__main__":
    unittest.main()
