// The HTTP contract (contract.md). Every call resolves to the parsed body or throws an Error with `kind` and `hint`.
// The server owns every sentence: an error's message is shown as it arrives.

// The launch link carries the token in its fragment (/#t=<token>), which never reaches a server or a Referer.
// A link to fill in can ride there too (#url=<link>, from the bookmark button or `spool <link>`).
// Keep the token for later loads of this tab and take both out of the address bar.
const KEY = "spool-token";
const fragment = () => new URLSearchParams(location.hash.slice(1));
const first = fragment();
const token = (() => {
  const given = first.get("t");
  if (given && /^[\w-]+$/.test(given)) {
    try { localStorage.setItem(KEY, given); } catch { /* private window: the token lives only in this page */ }
    return given;
  }
  try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
})();
if (location.hash) history.replaceState(null, "", location.pathname + location.search);
const auth = { "X-Spool-Token": token };

let unauthorized = () => {};
export const onUnauthorized = (fn) => { unauthorized = fn; };

// A link given in the address, now or later: the bookmark button re-uses this tab, which only changes the fragment.
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

async function call(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      keepalive: true, // a Remove still pending when the tab closes is sent on the way out
      headers: body ? { ...auth, "Content-Type": "application/json" } : auth,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw Object.assign(new Error("Spool isn't responding. Check that it's still running."), { kind: "offline" });
  }
  if (res.status === 401) unauthorized();
  if (res.status === 204) return null;
  const json = (res.headers.get("Content-Type") || "").includes("json");
  const data = json ? await res.json() : await res.text();
  if (!res.ok) {
    const e = data?.error || {};
    throw Object.assign(new Error(e.message || `Request failed (${res.status})`), { kind: e.kind, hint: e.hint, status: res.status });
  }
  return data;
}

export const probe = (url) => call("POST", "/api/probe", { url });
export const addJobs = (entries, preset, engine) => call("POST", "/api/jobs", { entries, preset, engine });
export const cancel = (id) => call("POST", `/api/jobs/${id}/cancel`);
export const retry = (id) => call("POST", `/api/jobs/${id}/retry`, {});
export const remove = (id) => call("DELETE", `/api/jobs/${id}`);
export const clearFinished = () => call("POST", "/api/jobs/clear", { state: "done" });
export const reveal = (id) => call("POST", `/api/jobs/${id}/reveal`);
export const revealFolder = () => call("POST", "/api/dest/reveal");
export const log = (id) => call("GET", `/api/jobs/${id}/log`);
export const saveSettings = (changes) => call("PUT", "/api/settings", changes);
export const chooseFolder = () => call("POST", "/api/choose-folder");
export const updateEngine = (name) => call("POST", `/api/engines/${name}/update`);

// Live events. EventSource cannot send a header, so read the stream with fetch and parse the
// server-sent-events format ourselves: "event:" and "data:" lines, a blank line ends each event,
// lines starting with ":" are pings. A dropped or refused stream is retried every two seconds.
let reopen = () => {};
// A fresh snapshot (the server sends one on every connect), e.g. when the tab is seen again.
export const refresh = () => reopen();

export function listen({ snapshot, job, removed, connected }) {
  const handlers = { snapshot, job, job_removed: (d) => removed(d.id) };
  const dispatch = (block) => {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (!data.length || !handlers[event]) return;
    if (event === "snapshot") connected(true);
    handlers[event](JSON.parse(data.join("\n")));
  };
  let stream, again = false;
  reopen = () => { again = true; stream?.abort(); };
  const open = async () => {
    stream = new AbortController();
    try {
      const res = await fetch("/api/events", { headers: auth, cache: "no-store", signal: stream.signal });
      if (res.status === 401) return unauthorized();
      if (!res.ok) throw new Error(`events ${res.status}`);
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value.replace(/\r\n?/g, "\n");
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          dispatch(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
        }
      }
    } catch { /* the server went away, or we asked for a fresh stream; fall through */ }
    if (again) { again = false; return open(); }
    connected(false);
    setTimeout(open, 2000);
  };
  open();
}
