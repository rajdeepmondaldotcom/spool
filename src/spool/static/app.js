import { html, render, useState, useEffect, useLayoutEffect, useRef } from "./vendor/preact-htm.module.js";
import * as api from "./api.js";
import * as f from "./format.js";

// Icons: one stroke weight, decorative beside text, so hidden from the accessibility tree.
const Icon = ({ d, cls = "" }) => html`
  <svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d=${d} /></svg>`;
const ICONS = {
  alert: "M12 4 2.5 20h19L12 4Zm0 6v4.5m0 3v.01",
  check: "m5 12.5 4.5 4.5L19 7.5",
  stop: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-6.4 2.6 12.8 12.8",
  wait: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.5V12l3 2",
  more: "M5.5 12h.01M12 12h.01M18.5 12h.01",
  folder: "M3.5 6.5h6l2 2h9v10h-17Z",
};
// A reel: the flange with five round windows and the spindle hole cut out.
const REEL = "M2.00 16.00a14 14 0 1 0 28.00 0a14 14 0 1 0-28.00 0ZM13.10 7.60a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM21.09 13.40a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM18.04 22.80a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM8.16 22.80a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM5.11 13.40a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM14.00 16.00a2 2 0 1 0 4.00 0a2 2 0 1 0-4.00 0Z";
const Reel = ({ turning }) => html`
  <svg class="reel ${turning ? "turning" : ""}" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d=${REEL} /></svg>`;

const isEditable = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
const newerEngine = (snap, name) => { const e = snap.engines[name]; return !!(e?.latest && e.latest !== e.version); };
const revealWord = (platform) => (platform === "darwin" ? "Show in Finder" : "Show in folder");

function App() {
  const [snap, setSnap] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [connected, setConnected] = useState(true);
  const [said, setSaid] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const seen = useRef({});
  const pending = useRef(null);
  const lastRow = useRef(null);
  const field = useRef(null);
  const settings = useRef(null);

  const say = (text) => { setSaid(""); setTimeout(() => setSaid(text), 50); };

  useEffect(() => api.onUnauthorized(() => setSignedOut(true)), []);
  useEffect(() => api.listen({
    connected: setConnected,
    snapshot: (s) => {
      seen.current = Object.fromEntries(s.jobs.map((j) => [j.id, j.state]));
      setSnap(s);
      setJobs(s.jobs);
    },
    job: (j) => {
      const before = seen.current[j.id];
      seen.current[j.id] = j.state;
      if (before && before !== j.state && j.state === "done") say(`Finished: ${j.title}`);
      if (before && before !== j.state && j.state === "failed") say(`Failed: ${j.title}`);
      setJobs((js) => (js.some((x) => x.id === j.id) ? js.map((x) => (x.id === j.id ? j : x)) : [j, ...js]));
    },
    removed: (id) => setJobs((js) => js.filter((x) => x.id !== id)),
  }), []);

  // "Update engine and retry": run the follow-up once the engine has been busy and is idle again.
  useEffect(() => {
    const p = pending.current, e = snap?.engines[p?.name];
    if (!e) return;
    if (e.busy) p.saw = true;
    else if (p.saw) { pending.current = null; if (!e.error) p.then(); }
  }, [snap]);
  const whenUpdated = async (name, then) => {
    const e = snap.engines[name];
    if (e.installed && e.latest && e.latest === e.version) return then();
    pending.current = { name, then, saw: false };
    try { await api.updateEngine(name); } catch (err) { if (err.status !== 409) { pending.current = null; say(err.message); } }
  };

  // Keep keyboard focus when the focused row changes state, moves list or goes away.
  useLayoutEffect(() => {
    const last = lastRow.current, active = document.activeElement;
    if (!last || (active && active !== document.body)) return;
    const rows = [...document.querySelectorAll("[data-job]")];
    const row = rows.find((r) => r.dataset.job === last.id) || rows[Math.min(last.index, rows.length - 1)];
    (row?.querySelector("button") || field.current)?.focus();
  }, [jobs]);
  const trackRow = (e) => {
    const li = e.target.closest("[data-job]");
    lastRow.current = li ? { id: li.dataset.job, index: [...document.querySelectorAll("[data-job]")].indexOf(li) } : null;
  };

  const running = jobs.filter((j) => j.state === "running");
  useEffect(() => {
    document.title = running.length > 1 ? `${running.length} downloading, Spool`
      : running.length ? `${f.percent(running[0]) ?? 0}% Spool` : "Spool";
  });

  // A tab whose token Spool does not know (another machine's link, or a token that changed) can do nothing.
  if (signedOut) return html`
    <main class="signed-out measure">
      <h1>Spool</h1>
      <p>This tab isn't signed in to Spool. Run <code>spool</code> in your terminal and it opens the right link.</p>
    </main>`;
  if (!snap) return html`<p class="connecting">Connecting to Spool…</p>`;

  const ctx = { snap, say, whenUpdated, field, openSettings: (focusFolder) => settings.current.open(focusFolder) };
  return html`
    <main>
      <${Deck} ...${ctx} turning=${running.length > 0} />
      ${!snap.tools.ffmpeg && html`<${FfmpegStrip} ...${ctx} />`}
      <div class="queue" onfocusin=${trackRow}>
        <${Queue} ...${ctx} jobs=${jobs} />
      </div>
    </main>
    <${StatusBar} ...${ctx} connected=${connected} />
    <${SettingsDialog} snap=${snap} handle=${settings} />
    <div class="visually-hidden" role="status" aria-live="polite">${said}</div>`;
}

function Deck({ snap, say, whenUpdated, field, openSettings, turning }) {
  const [url, setUrl] = useState("");
  const [look, setLook] = useState({ status: "idle" });
  const [preset, setPreset] = useState(snap.settings.preset);
  const [picked, setPicked] = useState(new Set());
  const [notice, setNotice] = useState("");
  const [trayError, setTrayError] = useState("");
  const [busy, setBusy] = useState(false);
  const handlers = useRef();

  const engineName = snap.settings.engine, engine = snap.engines[engineName];
  const ready = engine.installed;
  const dest = snap.dest;

  useEffect(() => { if (ready) field.current.focus(); }, [ready]);

  const lookUp = async (link, useEngine) => {
    setNotice(""); setTrayError("");
    if (!link.trim()) return setLook({ status: "error", error: { kind: "empty_field" } });
    setLook({ status: "looking" });
    try {
      const data = await api.probe(link.trim(), useEngine);
      const ids = data.entries[0].presets.map((p) => p.id);
      setPreset(ids.includes(snap.settings.preset) ? snap.settings.preset : "best");
      setPicked(new Set(data.entries.map((e, i) => i)));
      setLook({ status: "preview", data });
      say(`Details ready: ${data.title}`);
    } catch (error) {
      setLook({ status: "error", error, engine: useEngine || engineName, url: link.trim() });
    }
  };

  const done = (n) => {
    setLook({ status: "idle" }); setUrl(""); setTrayError("");
    if (n > 1) setNotice(`Added ${n} downloads.`);
    field.current.focus();
  };

  const queue = async (entries, usePreset, useEngine) => {
    setBusy(true);
    try {
      const res = await api.addJobs(entries, usePreset, useEngine);
      done(res.jobs.length);
    } catch (e) {
      look.status === "preview" ? setTrayError(e.message) : setNotice(e.message);
    } finally { setBusy(false); }
  };

  const download = () => {
    const { data } = look;
    const entries = data.kind === "playlist" ? data.entries.filter((e, i) => picked.has(i)) : data.entries;
    queue(entries, preset, data.engine);
  };

  // Cmd+Enter (Ctrl+Enter elsewhere): skip the preview and download at the default quality.
  const quick = () => {
    if (look.status === "preview") return queue(look.data.entries.filter((e, i) => picked.has(i) || look.data.kind === "video"), snap.settings.preset, look.data.engine);
    const ls = f.links(url);
    if (!ls.length) return setLook({ status: "error", error: { kind: url.trim() ? "invalid_link" : "empty_field" } });
    if (ls.length > 1) return queue(ls.map((u) => ({ url: u })), snap.settings.preset);
    // One link: read its details first so the row has a title and a live stream is caught, but show no preview.
    setLook({ status: "looking" });
    api.probe(ls[0]).then(
      (data) => { setLook({ status: "idle" }); if (!data.entries.some((e) => e.live)) return queue(data.entries, snap.settings.preset, data.engine); setLook({ status: "preview", data }); },
      (error) => setLook({ status: "error", error, engine: engineName, url: ls[0] }),
    );
  };

  const handleText = (text) => {
    const ls = f.links(text);
    if (ls.length > 1) { setUrl(""); queue(ls.map((u) => ({ url: u })), snap.settings.preset); }
    else if (ls.length === 1) { setUrl(ls[0]); lookUp(ls[0]); }
    return ls.length > 0;
  };
  const close = () => { setLook({ status: "idle" }); setTrayError(""); field.current.focus(); };
  handlers.current = { handleText, close, open: look.status !== "idle" };

  // Paste or drop a link anywhere on the page.
  useEffect(() => {
    const onPaste = (e) => {
      if (isEditable(e.target) && e.target !== field.current) return;
      if (field.current.disabled) return;
      if (handlers.current.handleText(e.clipboardData.getData("text"))) e.preventDefault();
    };
    const onDragOver = (e) => e.preventDefault();
    const onDrop = (e) => {
      e.preventDefault();
      if (!field.current.disabled) handlers.current.handleText(e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text"));
    };
    const onKey = (e) => {
      if (e.key !== "Escape" || !handlers.current.open) return;
      if (document.querySelector("dialog[open], :popover-open")) return;
      handlers.current.close();
    };
    document.addEventListener("paste", onPaste);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("paste", onPaste);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  const onKeyDown = (e) => {
    if (e.key !== "Enter") return;
    if (e.isComposing || e.keyCode === 229) return e.preventDefault(); // Enter that confirms an IME composition
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); quick(); }
  };
  const onSubmit = (e) => {
    e.preventDefault();
    const ls = f.links(url);
    ls.length > 1 ? handleText(url) : lookUp(url);
  };

  const changeFolder = async () => {
    if (!snap.tools.can_choose_folder) return openSettings(true);
    try {
      const res = await api.chooseFolder();
      if (res?.path) await api.saveSettings({ dest: res.path });
    } catch (e) { setNotice(e.message); }
  };
  const recheck = () => api.saveSettings({ dest: snap.settings.dest }).catch((e) => setNotice(e.message));

  const err = look.status === "error" ? look.error : null;
  const looking = look.status === "looking";

  return html`
    <div class="deck">
      <div class="measure">
        <h1 class="wordmark"><${Reel} turning=${turning} />Spool</h1>
        <form class="link" onSubmit=${onSubmit} novalidate>
          <label for="link">Link to download</label>
          <div class="link-row">
            <input id="link" ref=${field} type="text" inputmode="url" autocomplete="off" spellcheck="false"
              placeholder="https://" value=${url} disabled=${!ready}
              aria-invalid=${err ? "true" : undefined} aria-describedby=${err ? "link-error" : "link-help"}
              onInput=${(e) => setUrl(e.target.value)} onKeyDown=${onKeyDown} />
            <button class="primary" type="submit" disabled=${!ready || looking}>${looking ? "Looking…" : "Get details"}</button>
          </div>
        </form>
        ${!ready ? html`<${EngineInstall} engine=${engine} name=${engineName} />`
          : err ? html`<${ProbeError} error=${err} look=${look} snap=${snap} whenUpdated=${whenUpdated} lookUp=${lookUp} />`
          : html`<p id="link-help" class="help">Enter looks it up. ${snap.tools.platform === "darwin" ? "Cmd" : "Ctrl"}+Enter downloads at ${f.presetName(snap.settings.preset)}.</p>`}
        <p class="notice" role="status">${notice}</p>
        <${Destination} dest=${dest} change=${changeFolder} recheck=${recheck} />
        ${looking && html`<div class="tray skeleton" aria-hidden="true"><div class="thumb" /><div class="lines"><div /><div /><div /></div></div>`}
        ${look.status === "preview" && html`<${Tray} data=${look.data} preset=${preset} setPreset=${setPreset}
          picked=${picked} setPicked=${setPicked} dest=${dest} busy=${busy} error=${trayError}
          download=${download} close=${close} />`}
      </div>
    </div>`;
}

function EngineInstall({ engine, name }) {
  if (engine.error && !engine.busy) return html`
    <div id="link-help" class="deck-message" role="alert">
      <p><${Icon} d=${ICONS.alert} />${f.plain(engine.error)}</p>
      <button class="secondary on-deck" onClick=${() => api.updateEngine(name).catch(() => {})}>Try again</button>
    </div>`;
  return html`<p id="link-help" class="help" role="status">Setting up Spool. This takes a few seconds.</p>`;
}

function ProbeError({ error, look, snap, whenUpdated, lookUp }) {
  if (error.kind === "empty_field") return html`<p id="link-error" class="deck-message" role="alert"><${Icon} d=${ICONS.alert} />Paste a link first.</p>`;
  const [message, fix, other] = f.failure(error, { engine: look.engine, dest: snap.settings.dest, newer: newerEngine(snap, look.engine), probe: true });
  const busy = snap.engines[look.engine]?.busy;
  const button = {
    update_engine: [busy ? "Updating…" : "Update and retry", () => whenUpdated(look.engine, () => lookUp(look.url, look.engine))],
    try_other_engine: [f.tryOther(other), () => lookUp(look.url, other)],
    retry: ["Retry", () => lookUp(look.url, look.engine)],
  }[fix];
  return html`
    <div id="link-error" class="deck-message" role="alert">
      <p><${Icon} d=${ICONS.alert} />${message}</p>
      ${button && html`<button class="secondary on-deck" disabled=${!!busy} onClick=${button[1]}>${button[0]}</button>`}
      ${fix === "install_js_runtime" && html`<${CopyCommand} command=${f.installCommand("deno", snap.tools.platform)} label="Copy install command" cls="secondary on-deck" />`}
    </div>`;
}

function Destination({ dest, change, recheck }) {
  if (!dest.ok) return html`
    <div class="dest problem" id="dest-problem" role="alert">
      <p><${Icon} d=${ICONS.alert} />${f.destProblem(dest)}</p>
      <span class="dest-actions">
        <button class="secondary on-deck" onClick=${recheck}>Try again</button>
        <button class="secondary on-deck" onClick=${change}>Change folder</button>
      </span>
    </div>`;
  return html`
    <div class="dest">
      <p><span><span class="dim">Saving to</span> <span class="dest-name" title=${dest.path}>${dest.display}</span></span>
        ${dest.free_bytes != null && html`<span class="dim free num">${f.bytes(dest.free_bytes)} free</span>`}</p>
      <button class="link-button" onClick=${change}>Change folder</button>
    </div>`;
}

function Tray({ data, preset, setPreset, picked, setPicked, dest, busy, error, download, close }) {
  const playlist = data.kind === "playlist";
  const first = data.entries[0];
  const live = !playlist && first.live;
  const n = playlist ? picked.size : 1;
  const need = first.presets.find((p) => p.id === preset)?.size;
  const tight = !live && dest.ok && need != null && dest.free_bytes != null && need > dest.free_bytes;
  const why = live ? "live-note" : !dest.ok ? "dest-problem" : tight ? "space-note" : undefined;
  // Picked by position: the entries of one multi-file item can all share a URL.
  const toggle = (i) => { const s = new Set(picked); s.has(i) ? s.delete(i) : s.add(i); setPicked(s); };

  return html`
    <section class="tray" aria-labelledby="tray-title">
      <div class="tray-head">
        ${!playlist && first.thumbnail && html`<img class="thumb" src=${first.thumbnail} alt="" width="160" height="90" />`}
        <div class="tray-title">
          <h2 id="tray-title" class="clamp" title=${data.title}>${data.title}</h2>
          <p class="dim">
            ${playlist ? html`<span>${n} of ${data.entries.length} selected</span>`
              : html`${first.uploader && html`<span>${first.uploader}</span>`} <span class="num">${f.duration(first.duration)}</span>`}
          </p>
        </div>
      </div>
      ${playlist && html`
        <div class="picks">
          <div class="pick-actions">
            <button class="secondary" onClick=${() => setPicked(new Set(data.entries.map((e, i) => i)))}>Select all</button>
            <button class="secondary" onClick=${() => setPicked(new Set())}>Select none</button>
          </div>
          <ul class="checklist" aria-label="Videos in ${data.title}">
            ${data.entries.map((e, i) => html`
              <li key=${i}><label>
                <input type="checkbox" checked=${picked.has(i)} onChange=${() => toggle(i)} />
                <span class="clamp" title=${e.title}>${e.title}</span>
                <span class="dim num">${f.duration(e.duration)}</span>
              </label></li>`)}
          </ul>
        </div>`}
      ${live ? html`<p id="live-note" class="note"><${Icon} d=${ICONS.wait} />This is live now. Download it after it ends.</p>`
        : html`
        <fieldset class="quality">
          <legend>Quality</legend>
          ${first.presets.map((p) => html`
            <label key=${p.id}>
              <input type="radio" name="quality" value=${p.id} checked=${preset === p.id} onChange=${() => setPreset(p.id)} />
              ${p.label}${p.size != null && html` <span class="dim num">${f.bytes(p.size)}</span>`}
            </label>`)}
        </fieldset>`}
      ${tight && html`<p id="space-note" class="note" role="status"><${Icon} d=${ICONS.alert} />This needs about ${f.bytes(need)}. ${dest.display} has ${f.bytes(dest.free_bytes)} free.</p>`}
      ${error && html`<p class="tray-error" role="alert"><${Icon} d=${ICONS.alert} />${error}</p>`}
      <div class="tray-actions">
        <button class="primary" onClick=${download} disabled=${live || !dest.ok || busy || n === 0}
          aria-describedby=${why}>${playlist ? `Download ${n}` : "Download"}</button>
        <button class="secondary" onClick=${close}>Close</button>
      </div>
    </section>`;
}

function FfmpegStrip({ snap }) {
  return html`
    <aside class="strip" aria-label="Missing tool">
      <div class="measure">
        <p><${Icon} d=${ICONS.alert} />Higher qualities need one more tool, ffmpeg.</p>
        <${CopyCommand} command=${f.installCommand("ffmpeg", snap.tools.platform)} />
      </div>
    </aside>`;
}

function CopyCommand({ command, label = "Copy", cls = "secondary" }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return html`<span class="command"><code>${command}</code>
    <button class=${cls} onClick=${copy}>${copied ? "Copied" : label}</button></span>`;
}

const GROUPS = [
  ["Downloading", (j) => j.state === "running" || j.state === "queued"],
  ["Stopped", (j) => j.state === "failed" || j.state === "cancelled"],
  ["Finished", (j) => j.state === "done"],
];

function Queue({ jobs, ...ctx }) {
  if (!jobs.length) return html`<p class="empty">Nothing here yet. Paste a link above.</p>`;
  return GROUPS.map(([name, test]) => {
    const list = jobs.filter(test);
    return list.length > 0 && html`
      <section class="group" aria-labelledby="group-${name}" key=${name}>
        <h2 id="group-${name}">${name}</h2>
        <ul>${list.map((j) => html`<${Row} key=${j.id} job=${j} ...${ctx} />`)}</ul>
      </section>`;
  });
}

function Row({ job, snap, say, whenUpdated, openSettings }) {
  const [logText, setLogText] = useState(null);
  const [gone, setGone] = useState(false);
  const act = (fn) => fn().catch((e) => say(e.message));
  const tid = `title-${job.id}`; // each row button is described by its row's title, so "Cancel" says which one
  const title = html`<h3 class="title clamp" id=${tid} title=${job.title || job.url}>${job.title || job.url}</h3>`;
  const remove = html`<button class="quiet" aria-describedby=${tid} onClick=${() => act(() => api.remove(job.id))}>Remove</button>`;

  if (job.state === "running" || job.state === "queued") {
    const pct = f.percent(job);
    const running = job.state === "running";
    return html`
      <li class="row active" data-job=${job.id}>
        <div class="counter num" aria-hidden="true">${running ? pct ?? 0 : ""}<span>${running ? "%" : ""}</span></div>
        <div class="body">
          ${title}
          ${running && (pct == null || job.stage === "processing"
            ? html`<progress key="busy" aria-label="Progress of ${job.title}" />`
            : html`<progress key="pct" value=${pct} max="100" aria-label="Progress of ${job.title}" />`)}
          <p class="meta dim">
            ${running ? html`<span>${f.stageWords(job)}</span>
              ${job.stage === "downloading" && html`<span class="num">${f.speed(job.speed)}</span><span class="num">${f.timeLeft(job.eta)}</span>`}`
              : html`<span><${Icon} d=${ICONS.wait} />Waiting</span>`}
          </p>
        </div>
        <div class="actions"><button class="secondary" aria-describedby=${tid} onClick=${() => act(() => api.cancel(job.id))}>Cancel</button></div>
      </li>`;
  }

  if (job.state === "done") {
    const show = async () => {
      try { await api.reveal(job.id); } catch (e) { e.status === 410 ? setGone(true) : say(e.message); }
    };
    const menuId = `menu-${job.id}`;
    const place = (e) => {
      if (e.newState !== "open") return;
      const r = document.querySelector(`[popovertarget="${menuId}"]`).getBoundingClientRect();
      e.target.style.top = `${r.bottom + 4}px`;
      e.target.style.left = `${Math.max(8, r.right - e.target.offsetWidth)}px`;
    };
    return html`
      <li class="row done" data-job=${job.id}>
        ${job.thumbnail ? html`<img class="thumb small" src=${job.thumbnail} alt="" width="64" height="36" loading="lazy" />` : html`<div class="thumb small" />`}
        <div class="body">
          ${title}
          <p class="meta dim"><span><${Icon} d=${ICONS.check} />Done</span><span class="num">${f.bytes(job.size)}</span>
            ${job.dest && job.dest !== snap.settings.dest && html`<span class="folder" title=${job.dest}><${Icon} d=${ICONS.folder} /><span class="visually-hidden">In </span>${f.folderName(job.dest)}</span>`}</p>
        </div>
        <div class="actions">
          ${job.file_exists && !gone ? html`<button class="secondary" aria-describedby=${tid} onClick=${show}>${revealWord(snap.tools.platform)}</button>`
            : html`<span class="dim moved">File moved or deleted</span>`}
          <button class="icon-button" popovertarget=${menuId} aria-label="More actions for ${job.title}" title="More actions">
            <${Icon} d=${ICONS.more} cls="bold" /></button>
          <div class="menu" id=${menuId} popover="auto" onToggle=${place}>
            <button onClick=${() => act(() => api.retry(job.id))}>Download again</button>
            <button onClick=${() => act(() => api.remove(job.id))}>Remove from list</button>
          </div>
        </div>
      </li>`;
  }

  if (job.state === "cancelled") return html`
    <li class="row stopped" data-job=${job.id}>
      <div class="state-icon"><${Icon} d=${ICONS.stop} /></div>
      <div class="body">${title}<p class="meta dim"><span>Cancelled</span></p></div>
      <div class="actions">
        <button class="secondary" aria-describedby=${tid} onClick=${() => act(() => api.retry(job.id))}>Resume</button>${remove}
      </div>
    </li>`;

  // Failed.
  const error = job.error || { kind: "engine" };
  const newer = newerEngine(snap, job.engine);
  const [message, kindFix, other] = f.failure(error, { engine: job.engine, dest: job.dest, newer });
  // The server's hint wins, except that updating is only offered when a newer engine exists.
  const fix = error.hint === "update_engine" && !newer ? kindFix : error.hint || kindFix;
  const live = error.kind === "live";
  const showLog = async () => {
    if (logText != null) return setLogText(null);
    try { setLogText(await api.log(job.id)); } catch (e) { say(e.message); }
  };
  const fixButton = {
    update_engine: ["Update and retry", () => whenUpdated(job.engine, () => act(() => api.retry(job.id)))],
    try_other_engine: [f.tryOther(other), () => act(() => api.retry(job.id, other))],
    check_dest: ["Change folder", () => openSettings(true)],
  }[fix];
  const copyFix = { install_ffmpeg: "ffmpeg", install_js_runtime: "deno" }[fix];
  return html`
    <li class="row stopped failed" data-job=${job.id}>
      <div class="state-icon danger"><${Icon} d=${ICONS.alert} /></div>
      <div class="body">
        ${title}
        <p class="failure"><span class="visually-hidden">Failed: </span>${message}</p>
        <div class="row-actions">
          ${fixButton && html`<button class="fill" aria-describedby=${tid} onClick=${fixButton[1]}>${fixButton[0]}</button>`}
          ${copyFix && html`<${CopyCommand} command=${f.installCommand(copyFix, snap.tools.platform)} label="Copy install command" cls="fill" />`}
          ${!live && html`<button class=${fixButton || copyFix ? "secondary" : "fill"} aria-describedby=${tid} onClick=${() => act(() => api.retry(job.id))}>${error.kind === "interrupted" ? "Resume" : "Retry"}</button>
          <button class="quiet" aria-describedby=${tid} aria-expanded=${logText != null ? "true" : "false"} aria-controls="log-${job.id}" onClick=${showLog}>
            ${logText != null ? "Hide log" : "Show log"}</button>`}
          ${remove}
        </div>
        ${logText != null && html`<pre class="log" id="log-${job.id}" tabindex="0" role="region" aria-label="Download log">${logText || "The log is empty."}</pre>`}
      </div>
    </li>`;
}

function StatusBar({ snap, connected, openSettings }) {
  const name = snap.settings.engine;
  return html`
    <footer class="status">
      <div class="status-inner">
        <span><${EngineUpdate} name=${name} engine=${snap.engines[name]} bar=${true} /></span>
        ${!connected && html`<span class="reconnecting" role="status">Reconnecting…</span>`}
        <button class="link-button settings-button" onClick=${() => openSettings(false)}>Settings</button>
      </div>
    </footer>`;
}

// The update control for one engine: in the status bar for the engine in use, and in Settings for each.
function EngineUpdate({ name, engine, bar = false }) {
  const update = () => api.updateEngine(name).catch(() => {});
  const which = html`<span class="visually-hidden"> ${f.engineName(name)} engine</span>`;
  if (engine.busy === "updating") return html`<span>Updating…</span>`;
  if (engine.busy === "installing" || (!engine.installed && !engine.error)) return html`<span>Setting up…</span>`;
  if (engine.error) return html`<span class="danger-text">${engine.installed ? "Update failed" : "Setup failed"}</span>
    <button class="link-button" onClick=${update}>Try again${!bar && which}</button>`;
  if (engine.latest && engine.latest !== engine.version) return html`<button class="link-button" onClick=${update}>${bar ? "Update available" : html`Update${which}`}</button>`;
  return html`<span>${engine.latest ? "Up to date" : ""}</span>`;
}

const PRESETS = ["best", "2160", "1440", "1080", "720", "480", "360", "240", "audio", "mp3"];

// handle.current.open(focusFolder) lets "Change folder" land on the folder field.
function SettingsDialog({ snap, handle }) {
  const dialog = useRef(), folder = useRef();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  handle.current = {
    open(focusFolder) {
      setDraft(snap.settings.dest); setError("");
      dialog.current.showModal();
      if (focusFolder) folder.current.focus();
    },
  };
  const save = (changes) => api.saveSettings(changes).then(() => setError("")).catch((e) => setError(e.message));
  const choose = async () => {
    try { const res = await api.chooseFolder(); if (res?.path) { setDraft(res.path); save({ dest: res.path }); } }
    catch (e) { setError(e.message); }
  };
  const onSubmit = (e) => { e.preventDefault(); save({ dest: draft.trim() }); };
  const presets = PRESETS.filter((p) => p !== "mp3" || snap.tools.ffmpeg);
  const missing = [!snap.tools.ffmpeg && "ffmpeg", !snap.tools.js_runtime && "deno"].filter(Boolean);

  return html`
    <dialog class="settings" ref=${dialog} aria-labelledby="settings-title">
      <h2 id="settings-title">Settings</h2>
      <fieldset>
        <legend>Engine</legend>
        ${Object.entries(snap.engines).map(([name, e]) => html`
          <div class="engine-choice" key=${name}>
            <label><input type="radio" name="engine" value=${name} checked=${snap.settings.engine === name}
              disabled=${!e.installed} onChange=${() => save({ engine: name })} />
              <strong>${f.engineName(name)}</strong> <span class="dim num">${e.version || ""}</span></label>
            <span class="dim"><${EngineUpdate} name=${name} engine=${e} /></span>
          </div>`)}
        <p class="dim helper">If a link fails on one, the other may work.</p>
      </fieldset>
      <label class="stack">Default quality
        <select value=${snap.settings.preset} onChange=${(e) => save({ preset: e.target.value })}>
          ${presets.map((p) => html`<option value=${p}>${f.presetName(p)}</option>`)}
        </select>
      </label>
      <form class="stack" onSubmit=${onSubmit} novalidate>
        <label for="folder">Folder</label>
        <div class="folder-row">
          <input id="folder" ref=${folder} type="text" spellcheck="false" autocomplete="off" value=${draft}
            aria-describedby="folder-state" onInput=${(e) => setDraft(e.target.value)} />
          ${snap.tools.can_choose_folder && html`<button type="button" class="secondary" onClick=${choose}>Choose…</button>`}
          <button type="submit" class="secondary">Save folder</button>
        </div>
        <p id="folder-state" class=${snap.dest.ok && !error ? "dim" : "danger-text"} role="status" title=${snap.dest.path}>
          ${error || (snap.dest.ok ? `${snap.dest.display}, ${f.bytes(snap.dest.free_bytes)} free` : f.destProblem(snap.dest))}</p>
      </form>
      ${missing.length > 0 && html`<p class="tools dim">Missing tools: ${missing.join(", ")}.</p>`}
      <div class="dialog-actions"><button class="primary" onClick=${() => dialog.current.close()}>Done</button></div>
    </dialog>`;
}

render(html`<${App} />`, document.getElementById("app"));
