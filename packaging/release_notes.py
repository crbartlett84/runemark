"""Print one version's section of CHANGELOG.md (used for the GitHub release notes)."""
import re
import sys
from pathlib import Path

# On Windows, redirected output uses the ANSI code page, which can't encode characters like "→"; the script then
# crashed and the release was published with empty notes (v0.3.0). Always write UTF-8.
sys.stdout.reconfigure(encoding="utf-8")

version = sys.argv[1]
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.S | re.M)
body = match.group(1).strip() if match else f"Runemark {version}"
print(body)
print()
print("**Download:** `Runemark-" + version + "-win64.zip` below. Unzip it anywhere and run `Runemark.exe`. "
      "Windows may warn that the app is unrecognised because it isn't code-signed: choose "
      "**More info → Run anyway**.")
