# Glossary

The words this codebase uses, and what each one means here. Code, tests and UI copy use these words and no synonyms.

| Term | Meaning |
|---|---|
| **Link** | A URL the user gives Spool. Always `http` or `https`. |
| **Probe** | Reading a link's details without downloading anything. A probe returns entries. |
| **Entry** | One downloadable video found by a probe. A single video is one entry. A playlist is many. |
| **Preset** | A named quality choice (`best`, `1080`, `audio`, `mp3` and so on). A preset maps to an engine format selector. |
| **Job** | One entry being downloaded with one preset to one destination. Jobs are what the queue holds. |
| **State** | Where a job is in its life: `queued`, `running`, `done`, `failed` or `cancelled`. |
| **Stage** | What a running job is doing right now: `starting`, `downloading` or `processing`. |
| **Part** | One stream of a running job. Most video jobs have two parts, video then audio, which are merged at the end. |
| **Engine** | The program that does the downloading: `yt-dlp` or `youtube-dl`. Spool keeps its own copy of each. |
| **Worker** | A child process that runs one engine call (a probe or a download) and reports as JSON lines. |
| **Destination** | The folder finished files are saved to. |
| **Snapshot** | The whole state the browser needs, sent when it connects: settings, destination, engines, tools and jobs. |
