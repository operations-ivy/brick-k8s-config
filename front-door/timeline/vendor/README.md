# Vendored for timeline.brick.nozdormu.cloud

Served from the cluster so the page makes no outside requests.

| File | From | Upstream sha256 |
| --- | --- | --- |
| `timelinejs3-3.9.13.js` | `@knight-lab/timelinejs` 3.9.13 (github.com/NUKnightLab/TimelineJS3), `dist/js/timeline.js` | `3d38229869a2d1d9f303626e141b02e9acd0fc972c1150bbf5b1e5655cc4d5b8` |
| `timelinejs3-3.9.13.dark.css` | the same package, `dist/css/themes/timeline.theme.dark.css` | `d6a8ff9876ffcd645dca6a418aaff1ee7abab328bf3106c50e7a351c33f54c5e` |
| `tl-icons.woff2`, `tl-icons.woff` | the same package, `dist/css/icons/` | `a41eae68…`, `788723b2…` (unchanged) |
| `js-yaml-5.5.0.min.js` | `js-yaml` 5.5.0, `dist/browser/js-yaml.umd.min.js` | `02970a652376ad653ff069c606ee7440b0514f52ed94a98426cb52426c8e5a02` (unchanged) |

Licences: `LICENSE-timelinejs3` (MPL-2.0), `LICENSE-js-yaml` (MIT).

## Changed from upstream

- **The CSS:** the icon font's URLs (`../icons/tl-icons.*`) point at the same
  folder, since the ConfigMap is served flat.
- **The JS:** the three demo API keys Knight Lab ships (Flickr's and Google
  Maps' `api_key_*` defaults, and the Instagram oEmbed `access_token`) are
  blanked. They aren't ours, the timeline embeds none of that media, and a
  key-shaped string doesn't belong in this repo (the user, 2026-11-09).
  `tests/test_timeline.py` checks they stay out.

To update: download the new versions, redo both changes, rename the files
with the new version (and the page's references), and run
`scripts/build-timeline`.
