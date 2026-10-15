# Vendored front-end files

Served as-is from `/static/vendor/`, so the CSP can allow `'self'` only. All MIT licensed but the font (SIL Open Font License 1.1, `fonts/OFL.txt`).

| File | Source | SHA-256 |
|---|---|---|
| `htmx-2.0.7.min.js` | `cdn.jsdelivr.net/npm/htmx.org@2.0.7/dist/htmx.min.js` | `60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9` |
| `htmx-ext-ws-2.0.4.min.js` | `cdn.jsdelivr.net/npm/htmx-ext-ws@2.0.4/dist/ws.min.js` | `896afc2936ded7a56373edf16ce75b0fe9bf49c03d1f55aee9baa94cb87c8d3e` |
| `franken-ui-2.0.0/core.min.css` | `cdn.jsdelivr.net/npm/franken-ui@2.0.0/dist/css/core.min.css` | `d4e7df4e1b133ca61d5936a600c86bf2ebc3596830d9114ed09e226f505b7756` |
| `franken-ui-2.0.0/core.iife.js` | `cdn.jsdelivr.net/npm/franken-ui@2.0.0/dist/js/core.iife.js` | `dc8f1eb8ffeeecb8d391b40831a4ba51b22e2fc2c6821d54ade4aaebedf7365f` |
| `daisyui-4.12.24/full.min.css` | `cdn.jsdelivr.net/npm/daisyui@4.12.24/dist/full.min.css` | `85ab76db67c89f094af6170e05109bdcfc3642f2d7c4bd5a62e931d6aede4792` |
| `fonts/nunito-latin-wght-normal.woff2` (Nunito, variable weight 200-1000, Latin) | `cdn.jsdelivr.net/npm/@fontsource-variable/nunito@5.2.5/files/nunito-latin-wght-normal.woff2` | `1a48b70f97555c13f84b8f088a417f9179d99b5101250819350acaf6e91bb92f` |
| `fonts/OFL.txt` | `raw.githubusercontent.com/google/fonts/a5bd0ea86b2576f86672aab557a6024d272187a5/ofl/nunito/OFL.txt` | `580df76c95a1ec5ab878ceb25bb3d85c6a076804e9c970c8c6972aea775fdf65` |
| `app.css` | built by `scripts/build_css.sh` (Tailwind 3.4.17) | changes with the classes used |

These are the versions MonsterUI 1.0.47's `Theme.headers()` linked. Franken UI's icon script is not included: nothing here renders a `UkIcon`.
