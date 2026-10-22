# Security

Runemark runs entirely on your PC. It doesn't connect to the internet, collect data or update itself. It reads and writes the video files you open and its own tag database (in `%APPDATA%\Runemark`, or "Runemark data" beside the app in portable mode).

The build script downloads two tools, each checked against a fixed SHA-256 before use: uv (to set up Python for the build) and FFmpeg. Python packages are installed at pinned versions from PyPI.

Releases aren't code-signed, so Windows SmartScreen may call the app unrecognised, and Smart App Control blocks it. Download releases only from this repository's Releases page.

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security → Report a vulnerability** on this repository. Don't open a public issue for them. Include the steps to reproduce and, if possible, a small sample file.
