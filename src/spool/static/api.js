// The HTTP contract (contract.md). Every call resolves to the parsed body or throws an Error with `kind` and `hint`.
// The server owns every sentence: an error's message is shown as it arrives.

// The launch link carries the token in its fragment (/#t=<token>), which never reaches a server or a Referer.
// A link to fill in can ride there too (#url=<link>, from the bookmark button or `spool <link>`).
// The token is kept for later tabs only once the server has accepted it: any website can open /#t=<anything>.
const KEY = "spool-token";
const fragment = () => new URLSearchParams(location.hash.slice(1));
const first = fragment();
const kept = () => { try { return localStorage.getItem(KEY) || ""; } catch { return ""; } };
const valid = (t) => t && /^[\w-]+$/.test(t);
const given = first.get("t");
const token = valid(given) ? given : kept();
const remember = () => { try { localStorage.setItem(KEY, token); } catch { /* private window: the token lives only in this page */ } };
if (location.hash) history.replaceState(null, "", location.pathname + location.search);
const auth = { "X-Spool-Token": token };
// A tab left open across a restart holds a token that opens nothing. When a newer tab is let in, this one follows.
window.addEventListener("storage", (e) => { if (e.key === KEY && e.newValue && e.newValue !== token) location.reload(); });
// A launch link put in this tab's address changes only the fragment, so the page loads again to read it. Added before
// onLink's listener, it stops that one from clearing the fragment the new load needs.
window.addEventListener("hashchange", (e) => {
  const t = fragment().get("t");
  if (valid(t) && t !== token) { e.stopImmediatePropagation(); location.reload(); }
});

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
export const open = (id) => call("POST", `/api/jobs/${id}/open`);
export const quit = () => call("POST", "/api/quit");
export const revealFolder = () => call("POST", "/api/dest/reveal");
export const log = (id) => call("GET", `/api/jobs/${id}/log`);
export const saveSettings = (changes) => call("PUT", "/api/settings", changes);
export const chooseFolder = () => call("POST", "/api/choose-folder");
export const updateEngine = (name) => call("POST", `/api/engines/${name}/update`);

// Live events. EventSource cannot send a header, so read the stream with fetch and parse the
// server-sent-events format ourselves: "event:" and "data:" lines, a blank line ends each event,
// lines starting with ":" are pings. A dropped, refused or silent stream is retried every two seconds.
// A browser allows six connections to one host, so the tabs share one stream: the tab holding the lock reads it
// and passes every event to the others; when that tab closes, another takes the lock. A tab the browser suspends keeps
// the lock: one that hears nothing from it asks, and if no answer comes, takes the stream over.
const STALL_MS = 40000; // the server pings every 15 seconds; silence this long means it is suspended or gone
const ASK_MS = 2000; // a running holder answers within milliseconds, even while Spool is down, and one taken from steps down
let reopen = () => {};
// A fresh snapshot (the server sends one on every connect), e.g. when the tab is seen again.
export const refresh = () => reopen();
// Spool was quit: every tab hears it and none opens the stream again. The server says so too, but this tab
// knows already, and a stream may end before the word arrives.
let announce = () => {};
export const stopped = () => announce();

export function listen({ snapshot, job, removed, connected, stopped }) {
  const handlers = { snapshot, job, job_removed: (d) => removed(d.id), stopped };
  let ended = false;
  // shortcut: a browser without Web Locks reads its own stream in every tab, so a seventh tab stalls there
  const shared = !!(navigator.locks && window.BroadcastChannel);
  const channel = shared ? new BroadcastChannel(`spool-${token}`) : { postMessage() {} };
  const deliver = ({ event, data }) => {
    if (event === "connected") return connected(data);
    if (event === "unauthorized") return unauthorized();
    if (event === "snapshot") { remember(); connected(true); }
    if (event === "stopped") { ended = true; stream?.abort(); }
    handlers[event]?.(data);
  };
  // leading: when this tab took the stream, 0 while it follows. Each message names the newest lead its sender knows,
  // so one from a tab that lost the stream while it was suspended is ignored.
  let leading = 0, newest = 0, stream, again = false, refused = false, watch;
  const post = (m) => channel.postMessage({ ...m, from: leading || newest });
  const emit = (m) => { deliver(m); post(m); };
  announce = () => emit({ event: "stopped", data: {} });
  const name = `spool-events-${token}`;
  // Only a lock taken by another tab steps down; a browser that refuses locks reads its own stream, as one without them.
  const take = (options = {}) => navigator.locks.request(name, options, lead)
    .catch((e) => (e.name === "AbortError" ? down(!options.steal) : leading || lead()));
  // A tab that follows asks for a snapshot and waits for the answer; one that hears nothing for long asks again.
  const ask = () => {
    if (ended || leading) return;
    post({ event: "refresh" });
    clearTimeout(watch);
    watch = setTimeout(() => take({ steal: true }), ASK_MS);
  };
  channel.onmessage = ({ data }) => {
    if (data.event === "refresh") return leading && (post({ event: "ping" }), reopen());
    if (data.from < newest) return;
    newest = data.from;
    if (!leading) { clearTimeout(watch); watch = setTimeout(ask, STALL_MS); }
    deliver(data);
  };
  reopen = ask;

  const dispatch = (block) => {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length && handlers[event]) emit({ event, data: JSON.parse(data.join("\n")) });
    else post({ event: "ping" }); // a sign of life for the tabs that follow
  };
  const open = async (run) => {
    if (ended || run !== leading) return;
    stream = new AbortController();
    let timer;
    const quiet = () => { clearTimeout(timer); timer = setTimeout(() => stream.abort(), STALL_MS); };
    quiet();
    try {
      const res = await fetch("/api/events", { headers: auth, cache: "no-store", signal: stream.signal });
      if (res.status === 401) { clearTimeout(timer); refused = true; return emit({ event: "unauthorized" }); }
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
    if (ended || run !== leading) return; // stepped down: the tab that took over reads the stream now
    if (again) { again = false; return open(run); }
    emit({ event: "connected", data: false });
    setTimeout(() => open(run), 2000);
  };
  const lead = () => {
    clearTimeout(watch);
    leading = newest = Math.max(Date.now(), newest + 1);
    // a tab that joins after the token was refused has to hear it too: the stream is not coming back
    reopen = () => { if (refused) return emit({ event: "unauthorized" }); again = true; stream?.abort(); };
    open(leading);
    return new Promise(() => {}); // held until this tab closes, or another takes it
  };
  // The lock was taken while this tab was suspended: it follows again, and asks for the events it missed.
  const down = (requeue) => {
    leading = 0;
    stream?.abort();
    reopen = ask;
    if (requeue) take(); // one that took the lock by force is still in line from before
    ask();
  };
  if (shared) { ask(); take(); } else lead();
}
