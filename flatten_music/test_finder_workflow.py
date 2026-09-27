import plistlib
import unittest
from pathlib import Path


class FinderWorkflowTests(unittest.TestCase):
    def test_workflow_is_portable_and_previews_before_writing(self):
        contents = (
            Path(__file__).parent
            / "finder_workflows"
            / "Flatten Music.workflow"
            / "Contents"
        )
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
        self.assertIn('".local/bin/fm"', script)
        self.assertIn('set fmCommand to "/bin/zsh "', script)
        self.assertIn('fmCommand & " --dry-run "', script)
        self.assertIn("Run for real? [y/N]", script)
        self.assertNotIn("/Users/", script)
        self.assertEqual(
            info["NSServices"][0]["NSSendFileTypes"], ["public.folder"]
        )


if __name__ == "__main__":
    unittest.main()
