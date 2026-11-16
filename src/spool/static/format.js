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

export function timeLeft(s) {
  if (s == null) return "";
  if (s < 60) return `${Math.max(1, Math.round(s))} s left`;
  if (s < 3600) return `${Math.ceil(s / 60)} min left`;
  const h = Math.floor(s / 3600), m = Math.ceil((s % 3600) / 60);
  return m ? `${h} h ${m} min left` : `${h} h left`;
}

export function presetName(id) {
  if (id === "best") return "Best";
  if (id === "audio") return "Audio";
  if (id === "mp3") return "MP3";
  return `${id}p`;
}

const AUDIO = new Set(["audio", "mp3"]);

export function stageWords(job) {
  if (job.stage === "starting" || !job.stage) return "Starting";
  if (job.stage === "processing") return job.preset === "mp3" ? "Converting to MP3" : "Joining video and sound";
  return AUDIO.has(job.preset) || job.part > 1 ? "Downloading sound" : "Downloading video";
}

export function percent(job) {
  return job.total ? Math.min(100, Math.floor((100 * (job.downloaded || 0)) / job.total)) : null;
}

export const otherEngine = (name) => (name === "yt-dlp" ? "youtube-dl" : "yt-dlp");

// Failure wording by error kind (design brief, "Words"). Returns the message and the fix button, if any.
export function failure(error, { engine, dest }) {
  const other = otherEngine(engine);
  switch (error.kind) {
    case "blocked": return ["The site refused this download. Updating the engine usually fixes it.", "update_engine"];
    case "unavailable": return ["This video is private, removed, or needs a sign-in. Spool can't download it.", null];
    case "unsupported": return [`${engine} can't read this link.`, "try_other_engine", other];
    case "empty": return [`${engine} found nothing at this link.`, "try_other_engine", other];
    case "network": return ["Couldn't reach the site. Check your connection.", "retry"];
    case "dest_missing": return [destMissing(folderName(dest), dest), "check_dest"];
    case "dest_full": return [`${/^\/Volumes\/([^/]+)/.exec(dest || "")?.[1] || folderName(dest)} is full. Free some space, or choose another folder.`, "check_dest"];
    case "dest_denied": return [`Spool isn't allowed to write to ${folderName(dest)}.`, "check_dest"];
    case "no_ffmpeg": return ["This quality needs ffmpeg to join video and sound.", "install_ffmpeg"];
    case "interrupted": return ["Spool was closed before this finished.", "resume"];
    case "invalid_link": return ["That isn't a link Spool can use. Paste a link that starts with http or https.", null];
    case "engine_not_ready": return [`${engine} isn't ready yet. Wait for it to finish installing.`, null];
    case "engine": return ["The engine stopped with an error. The log has the details.", "show_log"];
    default: return [error.message || "Something went wrong.", "retry"];
  }
}

const folderName = (path = "") => path.split(/[\\/]/).filter(Boolean).pop() || path;

// "T7 isn't connected" for a drive under /Volumes, otherwise the folder's display name.
export function destMissing(display, path = "") {
  const drive = /^\/Volumes\/([^/]+)/.exec(path)?.[1];
  return `${drive || display} isn't connected. Plug it in, or choose another folder.`;
}

export function destProblem(dest) {
  if (dest.problem === "missing") return destMissing(dest.display, dest.path);
  if (dest.problem === "not_a_folder") return `${dest.display} isn't a folder. Choose another folder.`;
  if (dest.problem === "not_writable") return `Spool isn't allowed to write to ${dest.display}. Choose another folder.`;
  return "";
}

export const INSTALL = {
  ffmpeg: { darwin: "brew install ffmpeg", linux: "sudo apt install ffmpeg", win32: "winget install ffmpeg" },
  deno: { darwin: "brew install deno", linux: "curl -fsSL https://deno.land/install.sh | sh", win32: "winget install DenoLand.Deno" },
};
export const installCommand = (tool, platform) => INSTALL[tool][platform] || INSTALL[tool].linux;

// Splits pasted text into http(s) links; anything else is ignored.
export function links(text) {
  return text.split(/\s+/).filter((t) => /^https?:\/\/[^\s/]+/i.test(t));
}
