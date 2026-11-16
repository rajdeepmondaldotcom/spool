# Third-party notices

## Included in this repository

| What | Where | Licence |
|---|---|---|
| Preact | `src/spool/static/vendor/preact-htm.module.js` | MIT (`LICENSE-preact.txt` beside it) |
| htm | `src/spool/static/vendor/preact-htm.module.js` | Apache-2.0 (`LICENSE-htm.txt` beside it) |
| Barlow and Barlow Condensed | `src/spool/static/fonts/` | SIL Open Font License 1.1 (`OFL.txt` beside the fonts) |

The screenshots in the README show open movies by the Blender Foundation, licensed CC BY.

## Fetched when Spool runs, not included here

On first run Spool fetches two engines from their official releases and checks each file against that release's checksums. It keeps them in its data folder. On a machine with no JavaScript runtime it fetches Deno the same way.

| What | Source | Licence |
|---|---|---|
| yt-dlp | https://github.com/yt-dlp/yt-dlp/releases | Unlicense |
| youtube-dl | https://github.com/ytdl-org/ytdl-nightly/releases | Unlicense |
| Deno | https://github.com/denoland/deno/releases | MIT |
