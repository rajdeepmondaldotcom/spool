// The HTTP contract (contract.md). Every call resolves to the parsed body or throws an Error with `kind` and `hint`.
// The server owns every sentence: an error's message is shown as it arrives.

// The launch link carries the token in its fragment (/#t=<token>), which never reaches a server or a Referer.
// A link to fill in can ride there too (#url=<link>, from the bookmark button or `spool <link>`).
// The token is kept for later tabs only once the server has accepted it: any website can open /#t=<anything>.
const KEY = "spool-token";
const fragment = () => new URLSearchParams(location.hash.slice(1));
const first = fragment();
const kept = () => { try { return localStorage.getItem(KEY) || ""; } catch { return ""; } };
const given = first.get("t");
const token = given && /^[\w-]+$/.test(given) ? given : kept();
const remember = () => { try { localStorage.setItem(KEY, token); } catch { /* private window: the token lives only in this page */ } };
if (location.hash) history.replaceState(null, "", location.pathname + location.search);
const auth = { "X-Spool-Token": token };

let unauthorized = () => {};
export const onUnauthorized = (fn) => { unauthorized = fn; };

// A link given in the address, now or later (a fragment change to this tab).
export function onLink(fn) {
  if (first.get("url")) fn(first.get("url"));
  const changed = () => {
    const link = fragment().get("url");
    if (!link) return;
    history.replaceState(null, "", location.pathname + location.search);
    fn(link);
  };
  window.addEventListener("hashchange", changed);
  return () => window.removeEventListener("hashchange", changed);
}

async function call(method, path, body, keepalive = false) {
  let res;
  try {
    res = await fetch(path, {
      method,
      keepalive, // the browser refuses a keepalive body over 64 KiB, so only the small sends that must outlive the tab
      headers: body ? { ...auth, "Content-Type": "application/json" } : auth,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw Object.assign(new Error("Spool isn't responding. Check that it's still running."), { kind: "offline" });
  }
  if (res.status === 401) unauthorized();
  if (res.status === 204 || res.status === 202) return null; // no body (an engine update answers 202)
  const json = (res.headers.get("Content-Type") || "").includes("json");
  const data = json ? await res.json() : await res.text();
  if (!res.ok) {
    const e = data?.error || {};
    throw Object.assign(new Error(e.message || `Request failed (${res.status})`), { kind: e.kind, hint: e.hint, status: res.status });
  }
  return data;
}

// Only the fields the server reads: a playlist's presets would make a large add too big to send.
const entry = ({ url, id, title, uploader, thumbnail, duration, item }) => ({ url, id, title, uploader, thumbnail, duration, item });
export const probe = (url) => call("POST", "/api/probe", { url });
export const addJobs = (entries, preset, engine) => call("POST", "/api/jobs", { entries: entries.map(entry), preset, engine });
export const cancel = (id) => call("POST", `/api/jobs/${id}/cancel`);
export const retry = (id) => call("POST", `/api/jobs/${id}/retry`, {});
export const remove = (id) => call("DELETE", `/api/jobs/${id}`, undefined, true);
export const clearFinished = (ids) => call("POST", "/api/jobs/clear", { ids }, true);
export const reveal = (id) => call("POST", `/api/jobs/${id}/reveal`);
export const revealFolder = () => call("POST", "/api/dest/reveal");
export const log = (id) => call("GET", `/api/jobs/${id}/log`);
export const saveSettings = (changes) => call("PUT", "/api/settings", changes);
export const chooseFolder = () => call("POST", "/api/choose-folder");
export const updateEngine = (name) => call("POST", `/api/engines/${name}/update`);

// Live events. EventSource cannot send a header, so read the stream with fetch and parse the
// server-sent-events format ourselves: "event:" and "data:" lines, a blank line ends each event,
// lines starting with ":" are pings. A dropped, refused or silent stream is retried every two seconds.
// A browser allows six connections to one host, so the tabs share one stream: the tab holding the lock reads it
// and passes every event to the others; when that tab closes, another takes the lock.
const STALL_MS = 40000; // the server pings every 15 seconds; silence this long means it is suspended or gone
let reopen = () => {};
// A fresh snapshot (the server sends one on every connect), e.g. when the tab is seen again.
export const refresh = () => reopen();

export function listen({ snapshot, job, removed, connected }) {
  const handlers = { snapshot, job, job_removed: (d) => removed(d.id) };
  const channel = new BroadcastChannel(`spool-${token}`);
  const deliver = ({ event, data }) => {
    if (event === "connected") return connected(data);
    if (event === "unauthorized") return unauthorized();
    if (event === "snapshot") { remember(); connected(true); }
    handlers[event]?.(data);
  };
  const emit = (m) => { deliver(m); channel.postMessage(m); };
  let leading = false, stream, again = false;
  channel.onmessage = ({ data }) => (data.event === "refresh" ? leading && reopen() : deliver(data));
  reopen = () => channel.postMessage({ event: "refresh" });
  reopen(); // a tab that joins asks for a snapshot

  const dispatch = (block) => {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length && handlers[event]) emit({ event, data: JSON.parse(data.join("\n")) });
  };
  const open = async () => {
    stream = new AbortController();
    let timer;
    const quiet = () => { clearTimeout(timer); timer = setTimeout(() => stream.abort(), STALL_MS); };
    quiet();
    try {
      const res = await fetch("/api/events", { headers: auth, cache: "no-store", signal: stream.signal });
      if (res.status === 401) { clearTimeout(timer); return emit({ event: "unauthorized" }); }
      if (!res.ok) throw new Error(`events ${res.status}`);
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        quiet();
        buffer += value.replace(/\r\n?/g, "\n");
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          dispatch(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
        }
      }
    } catch { /* the server went away, went quiet, or we asked for a fresh stream; fall through */ }
    clearTimeout(timer);
    if (again) { again = false; return open(); }
    emit({ event: "connected", data: false });
    setTimeout(open, 2000);
  };
  navigator.locks.request(`spool-events-${token}`, () => {
    leading = true;
    reopen = () => { again = true; stream?.abort(); };
    open();
    return new Promise(() => {}); // held until this tab closes
  });
}
