// The HTTP contract (contract.md). Every call resolves to the parsed body or throws an Error with `kind`.

async function call(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw Object.assign(new Error("Spool isn't responding. Check that it is still running."), { kind: "offline" });
  }
  if (res.status === 204) return null;
  const json = (res.headers.get("Content-Type") || "").includes("json");
  const data = json ? await res.json() : await res.text();
  if (!res.ok) {
    const e = data?.error || {};
    throw Object.assign(new Error(e.message || `Request failed (${res.status})`), { kind: e.kind, status: res.status });
  }
  return data;
}

export const probe = (url, engine) => call("POST", "/api/probe", { url, engine });
export const addJobs = (entries, preset, engine) => call("POST", "/api/jobs", { entries, preset, engine });
export const cancel = (id) => call("POST", `/api/jobs/${id}/cancel`);
export const retry = (id, engine) => call("POST", `/api/jobs/${id}/retry`, engine ? { engine } : {});
export const remove = (id) => call("DELETE", `/api/jobs/${id}`);
export const reveal = (id) => call("POST", `/api/jobs/${id}/reveal`);
export const log = (id) => call("GET", `/api/jobs/${id}/log`);
export const saveSettings = (changes) => call("PUT", "/api/settings", changes);
export const chooseFolder = () => call("POST", "/api/choose-folder");
export const updateEngine = (name) => call("POST", `/api/engines/${name}/update`);

// Live events. EventSource reconnects by itself after a dropped stream, but not after an HTTP error, so retry those.
export function listen({ snapshot, job, removed, connected }) {
  let source;
  const open = () => {
    source = new EventSource("/api/events");
    source.addEventListener("snapshot", (e) => { connected(true); snapshot(JSON.parse(e.data)); });
    source.addEventListener("job", (e) => job(JSON.parse(e.data)));
    source.addEventListener("job_removed", (e) => removed(JSON.parse(e.data).id));
    source.onerror = () => {
      connected(false);
      if (source.readyState === EventSource.CLOSED) setTimeout(open, 2000);
    };
  };
  open();
}
