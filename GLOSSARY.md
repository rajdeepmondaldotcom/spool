# Glossary

The words the code uses. Where the screen uses a plainer word, it is in brackets.

| Term | Meaning |
|---|---|
| **Link** | A URL the user gives Spool. Always `http` or `https`. |
| **Probe** (get details) | Reading a link's details without downloading anything. A probe returns entries. |
| **Entry** | One downloadable video found by a probe. A single video is one entry. A playlist is many. |
| **Item** | An entry's position in its playlist, counted from 1. Used when an entry has no link of its own. |
| **Preset** | A named quality choice (`best`, `1080`, `audio`, `mp3` and so on). |
| **Class** | A numeric preset's size, measured on the picture's shorter side. A 1080×1920 video is class 1080. |
| **Job** | One entry being downloaded with one preset to one destination. Jobs are what the queue holds. |
| **State** | Where a job is in its life: `queued`, `running`, `done`, `failed` or `cancelled`. |
| **Stage** | What a running job is doing right now: `starting`, `downloading` or `processing`. |
| **Part** | One stream of a running job. Most video jobs have two parts, picture then sound, joined at the end. |
| **Engine** | The program Spool runs to fetch a video. Spool keeps its own copies of two, a main one and a backup. |
| **Worker** | A child process that runs one engine call (a probe or a download) and reports as JSON lines. |
| **Destination** (folder) | The folder finished files are saved to. |
| **Snapshot** | The whole state the browser needs, sent when it connects: settings, destination, engines, tools and jobs. |
