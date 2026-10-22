from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).parent / "finder_workflows"


class FinderTests(unittest.TestCase):
    def test_workflows_are_portable_and_commands_are_exact(self) -> None:
        bundles = sorted(WORKFLOWS.glob("*.workflow"))
        self.assertEqual(len(bundles), 2)
        for bundle in bundles:
            info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
            document = plistlib.loads((bundle / "Contents/document.wflow").read_bytes())
            action = document["actions"][0]["action"]
            script = action["ActionParameters"]["source"]
            self.assertEqual(action["ActionBundlePath"],
                             "/System/Library/Automator/Run AppleScript.action")
            self.assertEqual(info["NSServices"][0]["NSSendFileTypes"], ["public.folder"])
            self.assertEqual(document["workflowMetaData"]["serviceInputTypeIdentifier"],
                             "com.apple.Automator.fileSystemObject.folder")
            self.assertEqual(info["NSServices"][0]["NSMenuItem"]["default"], bundle.stem)
            self.assertIn('".local/bin/music-crates"', script)
            self.assertIn("path to home folder", script)
            self.assertIn('set shellCommand to "/bin/zsh " & (quoted form of commandPath)', script)
            self.assertIn("(quoted form of selectedPath)", script)
            self.assertIn("do script shellCommand in reviewTab", script)
            self.assertNotIn("/Users/", script)
            if "Build" in bundle.name:
                self.assertIn(' & " --write " & ', script)
                self.assertIn('default button "Cancel" cancel button "Cancel"', script)
                self.assertLess(script.index("display dialog"), script.index("set shellCommand"))
            else:
                self.assertIn(' & " " & (quoted form of selectedPath)', script)
                self.assertNotIn("--write", script)
                self.assertNotIn("display dialog", script)

    @unittest.skipUnless(sys.platform == "darwin", "macOS Finder installer")
    def test_installer_uses_temporary_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([str(WORKFLOWS / "install.sh")],
                                    env={**os.environ, "FINDER_SERVICES_DIR": temporary},
                                    capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual(result.stdout.count("Installed Music Crates"), 2)
            for source in WORKFLOWS.glob("*.workflow/Contents/*"):
                target = Path(temporary) / source.relative_to(WORKFLOWS)
                self.assertEqual(target.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
