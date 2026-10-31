# Fonts for the certificate PDF

Used only by `app/certificate.py` (fpdf2 embeds the characters a certificate uses). They
live here, not under `static/`, so they are never served over HTTP.

Noto Sans 2.015, static (not variable) instances, SIL Open Font License 1.1 (`OFL.txt`,
copyright 2022 The Noto Project Authors). Covers Latin (including Vietnamese), Greek and
Cyrillic; no CJK, Armenian or Hangul (those print as "?").

Upstream: https://github.com/notofonts/latin-greek-cyrillic (release `NotoSans-v2.015`),
taken from the published builds in https://github.com/notofonts/notofonts.github.io at
commit 412a7edb2c8c535d1066157e5b17e661fd5591fe:

| File | Source path | sha256 |
|------|-------------|--------|
| `NotoSans-Regular.ttf` | `fonts/NotoSans/full/ttf/NotoSans-Regular.ttf` | `f5f552c8c5edb61fe6efb824baf4d4de47b1a8689ab4925ff43f7bd6a4ebece5` |
| `NotoSans-Bold.ttf` | `fonts/NotoSans/full/ttf/NotoSans-Bold.ttf` | `3a08a47daa00cade516425c15c57615aef2fd418ec9811a7b9f465088f92cc05` |
| `OFL.txt` | `OFL.txt` of the latin-greek-cyrillic repo | `cee9892f9f0cc8fe882c9e9537ee6a89621d86ee7ceaf70b02e2b2b1c25c061a` |
