# Spool

Save videos from YouTube and over a thousand other sites to your own disk. Paste a link, get the file.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/spool-dark.png">
  <img alt="Spool showing a video's details, its quality choices with file sizes, and a download in progress" src="docs/spool-light.png">
</picture>

- Any quality the site offers, up to 4K, with the file size shown before you download.
- Whole playlists or only the videos you pick, or audio only as MP3.
- Files go to a folder you choose. Spool runs on your computer, needs no account, and nothing you save leaves your machine.

## Install

1. Open Terminal and install [uv](https://docs.astral.sh/uv/), which installs and runs Spool:

   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   On Windows, use the command on [uv's install page](https://docs.astral.sh/uv/getting-started/installation/).

   Then close Terminal and open it again, so it finds `uv`.

2. Install Spool:

   ```
   uv tool install git+https://github.com/rajdeepmondaldotcom/spool
   ```

   If Terminal later says `spool: command not found`, run `uv tool update-shell` and open Terminal again.

3. Add the free helper that joins video and audio. The top qualities and MP3 need it.

   | System | Command |
   |---|---|
   | macOS | `brew install ffmpeg` (needs [Homebrew](https://brew.sh)) |
   | Windows | `winget install ffmpeg` |
   | Linux | `sudo apt install ffmpeg` |

   Spool works without it at lower qualities, and tells you when a download needs it.

## First use

Type `spool` and press Enter. A browser tab opens.

1. Paste a video link anywhere on the page.
2. Pick a quality.
3. Press Download.

The file lands in your Downloads folder. To save somewhere else, including an external drive, press Change next to the folder name.

Keep the Terminal window open while Spool works. Press Ctrl+C in it to stop Spool. The next time you start it, press Resume on a download that was running and it continues from where it stopped.

## Faster ways in

| Do this | And Spool |
|---|---|
| Paste a link, then press Enter | Downloads it at the quality you used last |
| Paste several links at once | Queues them all |
| Press Cmd+Enter in the link field (Ctrl+Enter on Windows and Linux) | Downloads without showing the choices |
| Run `spool https://…` | Opens with that link ready |
| Drag "Save with Spool" from Settings to your bookmarks bar, then click it on a video's page | Opens with that page's link ready |

## What you get

- The quality you used last is remembered, so most downloads are a paste and one key.
- The title, channel, date and chapter markers are saved inside the file.
- Files are named `Title [id].ext`. A fixed quality adds itself, as in `Title [id] 720p.mp4`, so two qualities of one video can sit side by side.
- Spool warns you before a download when the file won't fit, and says so when a drive is unplugged or full.
- Cancel and resume. A download you cancelled, or one cut short by closing Spool, continues from where it stopped when you press Resume.
- When a site changes how it serves video, Spool updates its engine the next time it starts.

## Update and uninstall

```
uv tool upgrade spool-dl
```

```
uv tool uninstall spool-dl
```

Spool keeps its settings, its list and its engine in one folder, which you can delete after uninstalling:

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/spool` |
| Windows | `%APPDATA%\spool` |
| Linux | `~/.local/share/spool` |

Your downloaded files are not in that folder, and Spool does not delete them.

## When something fails

| You see | What to do |
|---|---|
| "The site refused the download." | Press Try again. Sites refuse some requests and accept the next. |
| "This video is private, removed, or needs a sign-in." | Spool can't sign in to sites, so it can't download these. |
| "This quality needs a free helper…" | Run the command from step 3 of Install, then press Try again. |
| "… isn't connected." | Plug the drive in, or press Change and pick another folder. |
| "This tab lost its link to Spool." | Run `spool` again. It opens a working tab. |
| "Joining video and audio" takes a long time | The drive is slow, and the row shows its speed and the time left. A drive plugged straight into the computer with a fast cable finishes this in minutes. |
| macOS asks whether Spool may use a removable volume | Choose Allow. It asks the first time you save to an external drive. |

For anything else, press Show log on the failed download and [open an issue](https://github.com/rajdeepmondaldotcom/spool/issues) with what it says.

Options: `spool --port 8000` picks the port, and `spool --no-open` starts without opening a tab.

## Privacy

- Spool listens on your own machine only (`127.0.0.1`). It talks to the sites you download from, and to GitHub to fetch its engine and updates.
- Other websites can't drive it, and neither can other people using your computer. Every request needs a token that your tab holds, and Spool makes a new one each time it starts.
- It has no analytics, and it loads no third-party scripts or fonts.

## Limits

- Spool can't sign in to sites, so private and age-restricted videos won't download.
- It doesn't download subtitles yet.
- It is built and used on macOS. Linux and Windows pass the automated tests but have had no hands-on run.

## Use it fairly

Download only what you have the right to keep.

## Develop

```
git clone https://github.com/rajdeepmondaldotcom/spool
cd spool
uv sync
uv run playwright install chromium
uv run pytest
```

The tests drive the real server and a real browser against a stand-in engine, so they need no network. `uv run pytest -m network` runs the same checks against real sites.

| Path | What lives there |
|---|---|
| `src/spool/server.py` | The local server: access checks, the API, live events |
| `src/spool/jobs.py` | The queue, job states, the saved list |
| `src/spool/worker.py` | One child process per lookup or download |
| `src/spool/engines.py` | Fetching, verifying and updating the engine |
| `src/spool/system.py` | Folders, drives, helpers |
| `src/spool/static/` | The page: plain modules and CSS, no build step |

Spool needs nothing at run time beyond Python's standard library. The words the code uses are defined in [GLOSSARY.md](GLOSSARY.md).

## Licence

MIT. Third-party notices are in [NOTICE.md](NOTICE.md).
