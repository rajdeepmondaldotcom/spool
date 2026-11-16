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

// The engine ids stay in the API; people see only these names, and only where they choose between them.
export const ENGINE_NAMES = { "yt-dlp": "Main", "youtube-dl": "Backup" };
export const engineName = (id) => ENGINE_NAMES[id] || "Main";
export const tryOther = (other) => (other === "youtube-dl" ? "Try the backup engine" : "Try the main engine");
// Server and engine messages can name the program underneath; the page never does.
export const plain = (text = "") => text.replace(/\b(yt[-_]dlp|youtube[-_]dl)\b/gi, "the engine");

// Failure wording by error kind (design brief, "Words"). Returns the message and the fix button, if any.
// `newer`: a newer engine exists, so updating is worth offering. `probe`: a probe has no log to point at.
export function failure(error, { engine, dest, newer = false, probe = false }) {
  const other = otherEngine(engine);
  if (error.hint === "install_js_runtime") return ["YouTube needs one more tool, deno.", "install_js_runtime"];
  switch (error.kind) {
    case "blocked": return ["The site refused this download. It often works on a second try.", newer ? "update_engine" : "retry"];
    case "live": return ["This is live now. Download it after it ends.", null];
    case "unavailable": return ["This video is private, removed, or needs a sign-in. Spool can't download it.", null];
    case "unsupported": return ["Spool can't read this link.", "try_other_engine", other];
    case "empty": return ["Nothing was found at this link.", "try_other_engine", other];
    case "network": return ["Couldn't reach the site. Check your connection.", "retry"];
    case "dest_unplugged": return [`${drive(dest)} isn't connected. Plug it in, or choose another folder.`, "check_dest"];
    case "dest_missing": return [`${folderName(dest)} doesn't exist. Choose another folder.`, "check_dest"];
    case "dest_not_folder": return [`${dest} is a file, not a folder.`, "check_dest"];
    case "dest_full": return [`${drive(dest)} is full. Free some space, or choose another folder.`, "check_dest"];
    case "dest_denied": return [`Spool isn't allowed to write to ${folderName(dest)}.`, "check_dest"];
    case "no_ffmpeg": return ["This quality needs ffmpeg to join picture and sound.", "install_ffmpeg"];
    case "interrupted": return ["Spool was closed before this finished.", "resume"];
    case "invalid_link": return ["That isn't a link Spool can use. Paste a link that starts with http or https.", null];
    case "engine_not_ready": return ["Spool is still setting up. Try again in a few seconds.", null];
    case "engine": return probe ? [plain(error.message) || "Spool couldn't read this link.", "retry"] : ["The download stopped with an error. The log has the details.", "show_log"];
    default: return [plain(error.message) || "Something went wrong.", "retry"];
  }
}

export const folderName = (path = "") => path.split(/[\\/]/).filter(Boolean).pop() || path;
// The drive's name for a folder under /Volumes, otherwise the folder's own name.
const drive = (path = "") => /^\/Volumes\/([^/]+)/.exec(path)?.[1] || folderName(path);

export function destProblem(dest) {
  const name = dest.display || folderName(dest.path);
  switch (dest.problem) {
    case "unplugged": return `${drive(dest.path)} isn't connected. Plug it in, or choose another folder.`;
    case "missing": return `${folderName(dest.path)} doesn't exist. Choose another folder.`;
    case "not_a_folder": return `${name} is a file, not a folder.`;
    case "not_writable": return `Spool isn't allowed to write to ${name}. Choose another folder.`;
    case "not_responding": return `${drive(dest.path)} isn't answering. If macOS is asking whether Spool may use it, choose Allow.`;
    default: return dest.problem ? `${name} can't be used right now. Choose another folder.` : "";
  }
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
