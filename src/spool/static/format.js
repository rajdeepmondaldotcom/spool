// Display formats for sizes, speeds, times and job wording. Decimal units, like Finder.

export function bytes(n) {
  if (n == null) return "";
  if (n < 1e6) return `${Math.max(1, Math.round(n / 1e3))} KB`;
  const [v, unit] = n < 1e9 ? [n / 1e6, "MB"] : [n / 1e9, "GB"];
  return `${v < 100 ? v.toFixed(1) : Math.round(v)} ${unit}`;
}

// Speed always in MB/s, so the column does not jump between units.
export const speed = (n) => (n ? `${(n / 1e6).toFixed(n < 1e6 ? 2 : 1)} MB/s` : "");

export function duration(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function span(s) {
  if (s < 60) return `${Math.max(1, Math.round(s))} s`;
  if (s < 3600) return `${Math.ceil(s / 60)} min`;
  const h = Math.floor(s / 3600), m = Math.ceil((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}
export const timeLeft = (s) => (s == null ? "" : `${span(s)} left`);
// A download waiting to try again by itself: the seconds until the next try.
export const tryingAgain = (s) => (s == null ? "Trying again soon" : `Trying again in ${span(s)}`);

export function presetName(id) {
  if (id === "best") return "Best";
  if (id === "audio") return "Audio";
  if (id === "mp3") return "MP3";
  return `${id}p`;
}

const AUDIO = new Set(["audio", "mp3"]);
const STEPS = { join: "Joining video and audio", tags: "Writing chapters and details", mp3: "Converting to MP3" };

export function stageWords(job) {
  if (job.stage === "waiting") return job.note || ""; // the server's sentence for what happened
  if (job.stage === "processing") return STEPS[job.what] || (job.preset === "mp3" ? "Converting to MP3" : job.part > 1 ? "Joining video and audio" : "Finishing");
  if (job.stage === "downloading") return job.what === "audio" || AUDIO.has(job.preset) ? "Downloading audio" : "Downloading video";
  return "Starting";
}

// How far the processing step is, as a whole number; null unless the server names the step and its size (an older
// server leaves the download's own numbers in place, which must not read as the step's).
export const stepPercent = (job) => (job.stage === "processing" && STEPS[job.what] && job.total
  ? Math.min(100, Math.floor((job.downloaded / job.total) * 100)) : null);

// The server's one percentage for the whole job (0 to 1), as a whole number; null when it knows nothing yet.
export const percent = (job) => (job.progress == null ? null : Math.min(100, Math.max(0, Math.floor(job.progress * 100))));

export const folderName = (path = "") => path.split(/[\\/]/).filter(Boolean).pop() || path;

const FFMPEG = { darwin: "brew install ffmpeg", linux: "sudo apt install ffmpeg", win32: "winget install ffmpeg" };
export const ffmpegCommand = (platform) => FFMPEG[platform] || FFMPEG.linux;

// The quality to select for a video: the remembered one, else the nearest lower class (Best counts as its own
// class, which the server leaves out of the list), else Best. A video with no Best (no helper, and no stream with
// both picture and sound) gets the first quality it does offer.
export function pick(presets, remembered) {
  const offered = (id) => presets.some((p) => p.id === id);
  if (offered(remembered)) return remembered;
  const fallback = offered("best") ? "best" : presets[0]?.id;
  if (!/^\d+$/.test(remembered)) return fallback;
  const height = (p) => parseInt(p.id === "best" ? p.detail : p.id, 10);
  const lower = presets.filter((p) => height(p) < +remembered).sort((a, b) => height(b) - height(a));
  return lower[0]?.id || fallback;
}

// At most four tiles up front (Best, two classes below it, Audio); the rest go under "More qualities".
export function split(presets) {
  const classes = presets.filter((p) => /^\d+$/.test(p.id));
  const usual = (p) => p.id === "1080" || p.id === "720";
  const two = [...classes.filter(usual), ...classes.filter((p) => !usual(p))].slice(0, 2);
  const shown = presets.filter((p) => p.id === "best" || p.id === "audio" || two.includes(p));
  return [shown, presets.filter((p) => !shown.includes(p))];
}

// Every link in any text: Markdown links, wrappers and trailing punctuation are taken off, and a bare host with a
// path or www. ("youtu.be/x", "www.site.com") gets https://, so a word like "Node.js" stays a word. Paste, drop,
// Enter and the address bar all read links through this.
const BARE = /^(?:www\.(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?[/?#])/i;
export function links(text = "") {
  return text
    .replace(/\[[^\]]*\]\(((?:[^()\s]|\([^()\s]*\))+)\)/g, " $1 ")
    .split(/\s+|,(?=https?:\/\/)/i) // a comma inside a link is part of it
    .map(clean)
    .map((t) => (BARE.test(t) ? `https://${t}` : t))
    .filter((t) => /^https?:\/\/[^\s/?#]+\.[^\s/?#]/i.test(t) || /^https?:\/\/(localhost|\[[\d:]+\]|[\d.]+)(?=[:/?#]|$)/i.test(t))
    .filter((t) => !/^https?:\/\/[^/?#]*@/i.test(t)); // user:pw@shown.host@real.host shows one host and fetches another
}
function clean(t) {
  for (let before; before !== t;) {
    before = t;
    t = t.replace(/^[<(["']+/, "").replace(/[>\]"'.,;:!?]+$/, "");
    // A closing bracket belongs to the link only when the link opened it (Foo_(bar)).
    if (t.endsWith(")") && (t.match(/\(/g) || []).length < (t.match(/\)/g) || []).length) t = t.slice(0, -1);
  }
  return t;
}
