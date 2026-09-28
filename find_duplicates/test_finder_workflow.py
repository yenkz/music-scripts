import plistlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class FinderWorkflowTests(unittest.TestCase):
    def test_workflow_is_portable_interactive_review(self):
        contents = (
            Path(__file__).parent
            / "finder_workflows"
            / "Music Duplicates — Review.workflow"
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
        self.assertIn('".local/bin/music-dupes"', script)
        self.assertIn('set shellCommand to "/bin/zsh "', script)
        self.assertIn(' & " --menu " & ', script)
        self.assertNotIn("--auto-remove", script)
        self.assertIn('set reviewTab to do script ""', script)
        self.assertIn("repeat while (busy of reviewTab)", script)
        self.assertIn("do script shellCommand in reviewTab", script)
        self.assertNotIn("--output", script)
        self.assertNotIn("/Users/", script)
        self.assertEqual(
            info["NSServices"][0]["NSSendFileTypes"], ["public.folder"]
        )
        self.assertEqual(
            document["workflowMetaData"]["serviceInputTypeIdentifier"],
            "com.apple.Automator.fileSystemObject.folder",
        )

    def test_installer_copies_menu_workflow_to_temporary_destination(self):
        workflow_dir = Path(__file__).parent / "finder_workflows"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [str(workflow_dir / "install.sh")],
                env={**os.environ, "FINDER_SERVICES_DIR": temporary},
                capture_output=True, text=True, check=True, timeout=30,
            )
            self.assertIn("Installed Music Duplicates", result.stdout)
            for name in ("Info.plist", "document.wflow"):
                relative = Path("Music Duplicates — Review.workflow/Contents") / name
                self.assertEqual(
                    (Path(temporary) / relative).read_bytes(),
                    (workflow_dir / relative).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
