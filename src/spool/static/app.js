import { html, render, useState, useEffect, useLayoutEffect, useRef } from "./vendor/preact-htm.module.js";
import * as api from "./api.js";
import * as f from "./format.js";

// Icons: one stroke weight, decorative beside text, so hidden from the accessibility tree.
const Icon = ({ d, cls = "" }) => html`
  <svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d=${d} /></svg>`;
const ICONS = {
  alert: "M12 4 2.5 20h19L12 4Zm0 6v4.5m0 3v.01",
  stop: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-6.4 2.6 12.8 12.8",
  wait: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.5V12l3 2",
  more: "M5.5 12h.01M12 12h.01M18.5 12h.01",
  folder: "M3.5 6.5h6l2 2h9v10h-17Z",
};
// A reel: the flange with five round windows and the spindle hole cut out.
const REEL = "M2.00 16.00a14 14 0 1 0 28.00 0a14 14 0 1 0-28.00 0ZM13.10 7.60a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM21.09 13.40a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM18.04 22.80a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM8.16 22.80a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM5.11 13.40a2.9 2.9 0 1 0 5.80 0a2.9 2.9 0 1 0-5.80 0ZM14.00 16.00a2 2 0 1 0 4.00 0a2 2 0 1 0-4.00 0Z";
const Reel = ({ turning }) => html`
  <svg class="reel ${turning ? "turning" : ""}" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d=${REEL} /></svg>`;

// Only a 0.2 server sends `update`, and only it can quit or open a file.
const newer = (snap) => "update" in snap;
const isEditable = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
const revealWord = (platform) => (platform === "darwin" ? "Show in Finder" : "Show in folder");
const modKey = (platform) => (platform === "darwin" ? "Cmd" : "Ctrl");
const local = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window: forgotten on close */ } },
};
const canNotify = () => "Notification" in window;
const notifying = () => canNotify() && Notification.permission === "granted" && local.get("spool-notify") === "on";
const UNDO_MS = 5000, FLASH_MS = 2000, SHOWN = 10;

function App() {
  const [snap, setSnap] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [connected, setConnected] = useState(true);
  const [said, setSaid] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const [away, setAway] = useState(new Set()); // rows taken off the screen; the server call waits for Undo's time
  const [undo, setUndo] = useState(null);
  const [flash, setFlash] = useState(new Set());
  const [unseen, setUnseen] = useState(false); // a download finished while the tab was hidden
  const [ask, setAsk] = useState(false); // asked once, after the first download this page added
  const [, setNotifyPref] = useState(local.get("spool-notify"));
  const seen = useRef({});
  const pending = useRef(null);
  const lastRow = useRef(null);
  const jobsNow = useRef(jobs);
  jobsNow.current = jobs;
  const field = useRef(null);
  const settings = useRef(null);
  const undoButton = useRef(null);
  const waiting = useRef([]); // each Remove or Clear not sent yet: { ids, send, states, timer }

  const say = (text) => { setSaid(""); setTimeout(() => setSaid(text), 50); };
  const tint = (ids) => {
    setFlash((s) => new Set([...s, ...ids]));
    setTimeout(() => setFlash((s) => new Set([...s].filter((id) => !ids.includes(id)))), FLASH_MS);
  };

  useEffect(() => api.onUnauthorized(() => setSignedOut(true)), []);
  useEffect(() => api.listen({
    connected: setConnected,
    snapshot: (s) => {
      seen.current = Object.fromEntries(s.jobs.map((j) => [j.id, j.state]));
      followUp(s);
      setSnap(s);
      setJobs(s.jobs);
    },
    job: (j) => {
      const before = seen.current[j.id];
      seen.current[j.id] = j.state;
      // A row taken off here that another tab starts again is no longer this tab's to remove.
      for (const w of waiting.current.filter((w) => w.ids.includes(j.id) && w.states[j.id] !== j.state)) {
        w.ids = w.ids.filter((id) => id !== j.id);
        setAway((s) => new Set([...s].filter((id) => id !== j.id)));
        if (!w.ids.length) drop(w);
      }
      if (before && before !== j.state && j.state === "done") {
        say(`Finished: ${j.title}`);
        tint([j.id]);
        if (document.hidden) {
          setUnseen(true);
          if (notifying()) new Notification("Finished", { body: j.title || j.url });
        }
      }
      if (before && before !== j.state && j.state === "failed") say(`Stopped: ${j.title}`);
      setJobs((js) => (js.some((x) => x.id === j.id) ? js.map((x) => (x.id === j.id ? j : x)) : [j, ...js]));
    },
    removed: (id) => setJobs((js) => js.filter((x) => x.id !== id)),
  }), []);
  useEffect(() => {
    const visible = () => { if (!document.hidden) { setUnseen(false); api.refresh(); } };
    document.addEventListener("visibilitychange", visible);
    return () => document.removeEventListener("visibilitychange", visible);
  }, []);

  // "Update and try again": run the follow-up once the engine is idle after the update started. Checked on every
  // snapshot, not every render: busy and idle can arrive together. A fast update may be over before its 202 answer,
  // so an idle snapshot that came after the request counts too.
  const followUp = (s) => {
    const p = pending.current, e = s?.engines[p?.name];
    if (!e) return;
    p.last = s;
    if (e.busy) p.saw = true;
    else if (p.saw || p.started) { pending.current = null; if (!e.error) p.then(); }
  };
  const whenUpdated = async (name, then) => {
    const p = pending.current = { name, then, saw: false, started: false };
    try { await api.updateEngine(name); } catch (err) {
      if (err.status !== 409) { pending.current = null; return say(err.message); }
    }
    p.started = true;
    if (pending.current === p && p.last) followUp(p.last);
  };

  // Remove and Clear: off the screen at once, each sent when its own Undo time has passed (or the tab closes).
  const drop = (w) => {
    clearTimeout(w.timer);
    waiting.current = waiting.current.filter((x) => x !== w);
    setUndo((u) => (u?.w === w ? null : u));
  };
  const send = (w) => {
    drop(w);
    w.send(w.ids).catch((e) => { setAway((s) => new Set([...s].filter((id) => !w.ids.includes(id)))); say(e.message); });
  };
  useEffect(() => {
    const all = () => waiting.current.forEach(send);
    window.addEventListener("pagehide", all);
    return () => window.removeEventListener("pagehide", all);
  }, []);
  const takeAway = (ids, words, call) => {
    const w = { ids, send: call, states: Object.fromEntries(ids.map((id) => [id, seen.current[id]])) };
    w.timer = setTimeout(() => {
      const focused = document.activeElement === undoButton.current;
      send(w);
      if (focused) field.current?.focus();
    }, UNDO_MS);
    waiting.current = [...waiting.current, w];
    setAway((s) => new Set([...s, ...ids]));
    setUndo({ words, w });
    say(words);
  };
  const putBack = () => {
    const w = undo?.w;
    if (!w || !waiting.current.includes(w)) return;
    drop(w);
    setAway((s) => new Set([...s].filter((id) => !w.ids.includes(id))));
    lastRow.current = { id: w.ids[0], index: 0, back: true };
  };
  // Focus moves in the same step that draws its target. Moved a frame later, it could land after the person
  // (or a test) had already acted, and undo what they did.
  useLayoutEffect(() => { if (undo) undoButton.current?.focus(); }, [undo]);

  // Keep keyboard focus when the focused row changes state, moves list or goes away.
  useLayoutEffect(() => {
    const last = lastRow.current, active = document.activeElement;
    // Only when the focused button left the page; focus the person put elsewhere (even on the page itself) stays.
    if (!last || (!last.back && ((active && active !== document.body) || last.el?.isConnected))) return;
    if (last.back) lastRow.current = { ...last, back: false };
    const rows = [...document.querySelectorAll("[data-job]")];
    const row = rows.find((r) => r.dataset.job === last.id) || rows[Math.min(last.index, rows.length - 1)];
    (row?.querySelector("button") || field.current)?.focus();
  }, [jobs, away]);
  const trackRow = (e) => {
    const li = e.target.closest("[data-job]");
    lastRow.current = li ? { id: li.dataset.job, index: [...document.querySelectorAll("[data-job]")].indexOf(li), el: e.target } : null;
  };

  const shown = jobs.filter((j) => !away.has(j.id));
  const running = shown.filter((j) => j.state === "running");
  useEffect(() => {
    const pct = running.length === 1 ? f.percent(running[0]) : null;
    document.title = running.length > 1 || (running.length && pct == null) ? `${running.length} downloading - Spool`
      : running.length ? `${pct}% - Spool` : unseen ? "Finished - Spool" : "Spool";
  });

  // A tab whose token Spool does not know (another machine's link, or a token that changed) can do nothing.
  if (signedOut) return html`
    <main class="signed-out measure">
      <h1 class="wordmark"><${Reel} />Spool</h1>
      <p>This tab lost its link to Spool. Run <code>spool</code> in your terminal to open a new one.</p>
    </main>`;
  if (!snap) return html`<p class="connecting">Connecting to Spool…</p>`;

  // After an add: rows that were already in the list are scrolled to and tinted; the first add asks about notifications.
  const added = (res) => {
    if (canNotify() && !local.get("spool-notify")) setAsk(true);
    const before = new Set(jobsNow.current.map((j) => j.id));
    const old = res.jobs.filter((j) => before.has(j.id)).map((j) => j.id);
    if (old.length) {
      tint(old);
      requestAnimationFrame(() => document.querySelector(`[data-job="${old[0]}"]`)?.scrollIntoView({ block: "nearest" }));
    }
  };
  const ctx = {
    snap, say, whenUpdated, field, connected, takeAway,
    openSettings: (focusFolder) => settings.current.open(focusFolder),
  };
  const notify = {
    asking: ask && !local.get("spool-notify"),
    async turnOn() {
      const answer = await Notification.requestPermission();
      local.set("spool-notify", answer === "granted" ? "on" : "off");
      setNotifyPref(answer); // re-render: a refusal also disables the switch
    },
    turnOff() { local.set("spool-notify", "off"); setNotifyPref("off"); },
  };
  const engine = snap.engines[snap.settings.engine], ready = !!engine?.installed;
  return html`
    <main>
      <${Deck} ...${ctx} turning=${running.length > 0} added=${added} notify=${notify} ready=${ready} />
      <${EngineStrip} snap=${snap} />
      ${!snap.tools.ffmpeg && html`<${FfmpegStrip} snap=${snap} />`}
      <div class="queue" onfocusin=${trackRow}>
        <${Queue} ...${ctx} jobs=${shown} flash=${flash} ready=${ready} failed=${!!engine?.error && !engine.busy} />
      </div>
    </main>
    ${undo && html`
      <div class="undo">
        <span>${undo.words}</span>
        <button class="link-button" ref=${undoButton} onClick=${putBack}>Undo</button>
      </div>`}
    <${SettingsDialog} snap=${snap} handle=${settings} notify=${notify} running=${running.length} />
    <div class="visually-hidden" role="status" aria-live="polite">${said}</div>`;
}

function Deck({ snap, say, whenUpdated, field, openSettings, turning, added, notify, ready }) {
  const [url, setUrl] = useState("");
  const [look, setLook] = useState({ status: "idle" });
  const [preset, setPreset] = useState(snap.settings.preset);
  const [picked, setPicked] = useState(new Set());
  const [notice, setNotice] = useState("");
  const [trayError, setTrayError] = useState("");
  const [busy, setBusy] = useState(false);
  const handlers = useRef();
  const lookup = useRef(0); // the newest lookup; an answer to an older one is ignored
  const chosen = useRef(null); // the quality the preview opened with
  const held = useRef(null); // text given while Spool is still setting up
  const noticeTimer = useRef();
  const tray = useRef();

  const tell = (text) => {
    clearTimeout(noticeTimer.current);
    setNotice(text);
    if (text) noticeTimer.current = setTimeout(() => setNotice(""), UNDO_MS);
  };

  const queue = async (entries, usePreset, engine) => {
    setBusy(true);
    try {
      const res = await api.addJobs(entries, usePreset, engine);
      const existing = res.existing || 0, fresh = res.jobs.length - existing;
      setLook({ status: "idle" }); setUrl(""); setTrayError("");
      field.current.focus();
      added(res);
      if (entries.length === 1) tell(existing ? "This one is already in the list." : "");
      else tell([fresh || !existing ? `Added ${fresh} download${fresh === 1 ? "" : "s"}.` : "",
        existing ? `${existing} ${existing === 1 ? "was" : "were"} already in the list.` : ""].filter(Boolean).join(" "));
      return true;
    } catch (e) {
      look.status === "preview" ? setTrayError(e.message) : tell(e.message);
    } finally { setBusy(false); }
  };

  // Any text: several links are queued at the remembered quality, one is looked up, none goes to the server,
  // whose answer says why it isn't a link.
  const lookUp = async (text, quick = false) => {
    if (!ready) { held.current = { text, quick }; return; }
    const ls = f.links(text);
    tell("");
    if (ls.length > 1) { setUrl(""); return queue(ls.map((u) => ({ url: u })), snap.settings.preset); }
    const link = ls[0] ?? text.trim();
    if (ls.length) setUrl(link);
    const n = ++lookup.current;
    setLook({ status: "looking" }); setTrayError("");
    try {
      const data = await api.probe(link);
      if (n !== lookup.current) return;
      const remembered = f.pick(data.entries[0].presets, snap.settings.preset);
      if (quick && data.kind !== "playlist" && !data.entries.some((e) => e.live)) {
        setLook({ status: "idle" });
        return queue(data.entries, remembered, data.engine);
      }
      chosen.current = remembered;
      setPreset(remembered);
      setPicked(new Set(data.entries.map((e, i) => i)));
      setLook({ status: "preview", data });
      say(`Details ready: ${data.title}`);
    } catch (error) {
      if (n === lookup.current) setLook({ status: "error", error, url: link });
    }
  };
  useEffect(() => {
    if (ready && held.current) { const h = held.current; held.current = null; lookUp(h.text, h.quick); }
  }, [ready]);

  const download = async () => {
    if (look.status !== "preview" || busy) return;
    const { data } = look;
    const entries = data.kind === "playlist" ? data.entries.filter((e, i) => picked.has(i)) : data.entries;
    if (!entries.length || data.entries.some((e) => e.live) || !snap.dest.ok) return;
    const choice = preset;
    // Only a quality the person picked becomes the remembered one, not the nearest one Spool fell back to.
    if (await queue(entries, choice, data.engine) && choice !== chosen.current) api.saveSettings({ preset: choice }).catch(() => {});
  };
  const close = () => { lookup.current++; setLook({ status: "idle" }); setTrayError(""); field.current.focus(); };
  const take = (text) => { setUrl(f.links(text)[0] ?? text); lookUp(text); };
  handlers.current = { take, close, download, lookUp, look };

  // The preview opens with focus on Download (Enter downloads), scrolled into view on small or zoomed screens.
  useLayoutEffect(() => {
    if (look.status !== "preview") return;
    const t = tray.current, submit = t.querySelector("button[type=submit]");
    (submit.disabled ? t.querySelector("input:checked") || t.querySelector("button[type=button]") : submit)?.focus({ preventScroll: true });
    submit.scrollIntoView({ block: "nearest" });
  }, [look]);

  useLayoutEffect(() => field.current.focus(), []);
  // Selected, so a paste replaces it rather than joining it.
  useEffect(() => api.onLink((text) => {
    setUrl(f.links(text).join(" ") || text);
    setTimeout(() => { field.current.focus(); field.current.select(); });
  }), []);

  // Paste or drop a link anywhere on the page. Escape cancels a lookup or closes the preview;
  // Cmd+Enter (Ctrl+Enter elsewhere) downloads the quality selected in the preview.
  // A layout effect: the page listens from the moment it is drawn, so a paste in its first instant is not lost.
  useLayoutEffect(() => {
    const blocked = () => document.querySelector("dialog[open]");
    const onPaste = (e) => {
      if (blocked() || (isEditable(e.target) && e.target !== field.current)) return;
      if (e.target === field.current) return setTimeout(() => handlers.current.lookUp(field.current.value));
      e.preventDefault();
      handlers.current.take(e.clipboardData.getData("text"));
    };
    const onDragOver = (e) => e.preventDefault();
    const onDrop = (e) => {
      e.preventDefault();
      if (!blocked()) handlers.current.take(e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text"));
    };
    const onKey = (e) => {
      const { look } = handlers.current;
      if (blocked() || document.querySelector(":popover-open")) return;
      if (e.key === "Escape" && look.status !== "idle") return handlers.current.close();
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && look.status === "preview") {
        e.preventDefault();
        handlers.current.download();
      }
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
    if ((e.metaKey || e.ctrlKey) && look.status !== "preview") { e.preventDefault(); lookUp(url, true); }
  };
  const onSubmit = (e) => { e.preventDefault(); lookUp(url); };

  const changeFolder = async () => {
    if (!snap.tools.can_choose_folder) return openSettings(true);
    try {
      const res = await api.chooseFolder();
      if (res?.path) await api.saveSettings({ dest: res.path });
    } catch (e) { tell(e.message); }
  };
  const recheck = () => api.saveSettings({ dest: snap.settings.dest }).catch((e) => tell(e.message));
  const showFolder = () => api.revealFolder().catch((e) => tell(e.message));

  const err = look.status === "error" ? look.error : null;
  const looking = look.status === "looking";
  const open = look.status === "preview";
  const retry = () => lookUp(look.url);

  return html`
    <div class="deck">
      <div class="measure">
        <div class="deck-top">
          <h1 class="wordmark"><${Reel} turning=${turning} />Spool</h1>
          <button class="quiet on-deck" onClick=${() => openSettings(false)}>Settings</button>
        </div>
        <form class="link" onSubmit=${onSubmit} novalidate>
          <label for="link">Link to download</label>
          <div class="link-row">
            <input id="link" ref=${field} type="text" inputmode="url" autocomplete="off" spellcheck="false"
              placeholder="https://" value=${url}
              aria-invalid=${err ? "true" : undefined} aria-describedby=${err ? "link-error" : "link-help"}
              onInput=${(e) => { setUrl(e.target.value); tell(""); }} onKeyDown=${onKeyDown} />
            <button class=${open ? "secondary on-deck" : "primary"} type="submit" disabled=${looking}>
              ${looking ? "Getting details…" : "Get details"}</button>
          </div>
        </form>
        ${err ? html`
          <div class="deck-message" role="alert">
            <p id="link-error"><${Icon} d=${ICONS.alert} />${err.message}</p>
            <${Fix} hint=${err.hint} snap=${snap} engine=${snap.settings.engine} retry=${retry} whenUpdated=${whenUpdated}
              changeFolder=${changeFolder} cls="secondary on-deck" />
          </div>`
          : html`<p id="link-help" class="help">Enter gets the details. ${modKey(snap.tools.platform)}+Enter downloads at ${f.presetName(snap.settings.preset)}.</p>`}
        <p class="notice" role="status">${notice}</p>
        ${notify.asking && html`
          <div class="ask">
            <p>Get a notification when downloads finish.</p>
            <button class="secondary on-deck" onClick=${notify.turnOn}>Turn on</button>
            <button class="quiet on-deck" onClick=${notify.turnOff}>No thanks</button>
          </div>`}
        <${Destination} dest=${snap.dest} change=${changeFolder} recheck=${recheck} show=${showFolder} />
        ${looking && html`<div class="tray skeleton" aria-hidden="true">
          <div class="tray-head"><div class="thumb" /><div class="lines"><div /><div /></div></div>
          <div class="tiles"><div /><div /><div /><div /></div><div class="bar" /></div>`}
        ${open && html`<${Tray} data=${look.data} preset=${preset} setPreset=${setPreset} trayRef=${tray}
          picked=${picked} setPicked=${setPicked} dest=${snap.dest} busy=${busy} error=${trayError}
          download=${download} close=${close} />`}
      </div>
    </div>`;
}

// The fix button chosen by the server's hint. A null or unknown hint has none.
function Fix({ hint, snap, engine, retry, whenUpdated, changeFolder, cls }) {
  const busy = snap.engines[engine]?.busy;
  if (hint === "retry") return html`<button class=${cls} onClick=${retry}>Try again</button>`;
  if (hint === "resume") return html`<button class=${cls} onClick=${retry}>Resume</button>`;
  if (hint === "update_engine") return html`<button class=${cls} disabled=${!!busy} onClick=${() => whenUpdated(engine, retry)}>
    ${busy ? "Updating…" : "Update and try again"}</button>`;
  if (hint === "check_dest") return html`<button class=${cls} onClick=${changeFolder}>Change folder</button>`;
  if (hint === "install_ffmpeg") return html`<${CopyCommand} command=${f.ffmpegCommand(snap.tools.platform)} platform=${snap.tools.platform} cls=${cls} />`;
  return null;
}

function Destination({ dest, change, recheck, show }) {
  if (!dest.ok) return html`
    <div class="dest problem" role="alert">
      <p id="dest-problem"><${Icon} d=${ICONS.alert} />${dest.message}</p>
      <span class="dest-actions">
        <button class="secondary on-deck" onClick=${recheck}>Try again</button>
        <button class="secondary on-deck" onClick=${change}>Change folder</button>
      </span>
    </div>`;
  return html`
    <p class="dest">
      <span class="dim">Saving to</span>
      <button class="dest-name" title=${dest.path} onClick=${show}><${Icon} d=${ICONS.folder} />${dest.display}</button>
      ${dest.free_bytes != null && html`<span class="dim free num">${f.bytes(dest.free_bytes)} free</span>`}
      <button class="link-button" onClick=${change}>Change</button>
    </p>`;
}

function Tray({ data, preset, setPreset, picked, setPicked, dest, busy, error, download, close, trayRef }) {
  const playlist = data.kind === "playlist";
  const first = data.entries[0];
  const live = !playlist && first.live;
  const n = playlist ? picked.size : 1, total = data.entries.length;
  const [shown, more] = f.split(first.presets);
  const [opened, setOpened] = useState(more.some((p) => p.id === preset));
  const need = first.presets.find((p) => p.id === preset)?.size;
  const tight = !live && dest.ok && need != null && dest.free_bytes != null && need > dest.free_bytes;
  const why = live ? "live-note" : !dest.ok ? "dest-problem" : tight ? "space-note" : undefined;
  const all = useRef();
  useEffect(() => { if (all.current) all.current.indeterminate = n > 0 && n < total; });
  // Picked by position: the entries of one multi-file item can all share a URL.
  const toggle = (i) => { const s = new Set(picked); s.has(i) ? s.delete(i) : s.add(i); setPicked(s); };
  const tile = (p) => html`
    <label class="tile" key=${p.id}>
      <input type="radio" name="quality" value=${p.id} checked=${preset === p.id} onChange=${() => setPreset(p.id)} />
      <span class="tile-label">${p.label}${p.detail && html` <span class="detail">${p.detail}</span>`}</span>
      ${!playlist && html`<span class="tile-size num">${p.size != null ? f.bytes(p.size) : "Size unknown"}${p.ext && html` <span class="ext">${p.ext.toUpperCase()}</span>`}</span>`}
    </label>`;

  return html`
    <section class="tray" aria-labelledby="tray-title" ref=${trayRef}>
      <form onSubmit=${(e) => { e.preventDefault(); download(); }} novalidate>
        <div class="tray-head">
          ${!playlist && first.thumbnail && html`<img class="thumb" src=${first.thumbnail} alt="" width="160" height="90" />`}
          <div class="tray-title">
            <h2 id="tray-title" class="clamp" dir="auto" title=${data.title}>${data.title}</h2>
            <p class="dim">
              ${playlist ? html`<span class="num">${total} videos</span>${data.truncated && html`<span>Showing the newest ${total}.</span>`}`
                : html`${first.uploader && html`<span dir="auto">${first.uploader}</span>`} <span class="num">${f.duration(first.duration)}</span>`}
            </p>
          </div>
        </div>
        ${playlist && html`
          <div class="picks">
            <label class="select-all">
              <input type="checkbox" ref=${all} checked=${n === total}
                onChange=${() => setPicked(n === total ? new Set() : new Set(data.entries.map((e, i) => i)))} />
              Select all <span class="dim num">${n} of ${total}</span>
            </label>
            <ul class="checklist" aria-label="Videos in ${data.title}">
              ${data.entries.map((e, i) => html`
                <li key=${i}><label>
                  <input type="checkbox" checked=${picked.has(i)} onChange=${() => toggle(i)} />
                  <span class="clamp" dir="auto" title=${e.title}>${e.title}</span>
                  <span class="dim num">${f.duration(e.duration)}</span>
                </label></li>`)}
            </ul>
          </div>`}
        ${live ? html`<p id="live-note" class="note"><${Icon} d=${ICONS.wait} />This is live now. Download it after it ends.</p>`
          : html`
          <fieldset class="quality">
            <legend>Quality</legend>
            <div class="tiles">
              ${shown.map(tile)}
              ${opened && more.map(tile)}
            </div>
            ${more.length > 0 && !opened && html`
              <button type="button" class="quiet more" aria-expanded="false" onClick=${() => setOpened(true)}>More qualities</button>`}
          </fieldset>`}
        ${tight && html`<p id="space-note" class="note" role="status"><${Icon} d=${ICONS.alert} />This needs about ${f.bytes(need)}. ${dest.display} has ${f.bytes(dest.free_bytes)} free.</p>`}
        ${error && html`<p class="tray-error" role="alert"><${Icon} d=${ICONS.alert} />${error}</p>`}
        <div class="tray-actions">
          <button class="primary" type="submit" disabled=${live || !dest.ok || busy || n === 0}
            aria-describedby=${why}>${playlist ? `Download ${n}` : "Download"}</button>
          <button class="secondary" type="button" onClick=${close}>Close</button>
        </div>
      </form>
    </section>`;
}

function EngineStrip({ snap }) {
  const broken = Object.entries(snap.engines).filter(([, e]) => e.error && !e.busy);
  const js = snap.tools.js_setup; // Spool fetching YouTube's free helper itself: null, "installing" or "failed"
  if (!broken.length && js) return html`
    <aside class="strip" aria-label="Engine">
      <div class="measure">
        ${js === "failed" ? html`<p role="alert"><${Icon} d=${ICONS.alert} />Spool couldn't set up what it needs for YouTube. Check your internet connection.</p>`
          : html`<p role="status">Spool is setting up what it needs for YouTube.</p>`}
      </div>
    </aside>`;
  if (!broken.length) return null;
  return html`
    <aside class="strip" aria-label="Engine">
      <div class="measure">
        <p role="alert"><${Icon} d=${ICONS.alert} />${broken[0][1].error}</p>
        <button class="secondary" onClick=${() => broken.forEach(([name]) => api.updateEngine(name).catch(() => {}))}>Try again</button>
      </div>
    </aside>`;
}

function FfmpegStrip({ snap }) {
  return html`
    <aside class="strip" aria-label="Free helper">
      <div class="measure">
        <p>The top qualities and MP3 need a free helper. Run this in your terminal:</p>
        <${CopyCommand} command=${f.ffmpegCommand(snap.tools.platform)} platform=${snap.tools.platform} />
      </div>
    </aside>`;
}

function CopyCommand({ command, platform, cls = "secondary" }) {
  const [copied, setCopied] = useState(null);
  const code = useRef();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied("Copied");
    } catch { // the browser refused: select the command so a key press copies it
      getSelection().selectAllChildren(code.current);
      setCopied(`Press ${modKey(platform)}+C`);
    }
    setTimeout(() => setCopied(null), 2000);
  };
  return html`<span class="command"><code ref=${code}>${command}</code>
    <button class=${cls} onClick=${copy}>${copied || "Copy command"}</button></span>`;
}

const canResume = (j) => j.state === "cancelled" || (j.state === "failed" && ["resume", "check_dest"].includes(j.error?.hint));
// Running, then waiting in the order they will start; the server lists newest first.
const downloading = (jobs) => [...jobs.filter((j) => j.state === "running"), ...jobs.filter((j) => j.state === "queued").reverse()];

function Queue({ jobs, flash, ready, failed, ...ctx }) {
  const [all, setAll] = useState(false);
  if (!jobs.length) return ready || failed
    ? html`<div class="empty"><p>Downloads show up here. Paste a link above to start.</p>
        <p class="dim">Works with YouTube, Vimeo and over a thousand other sites.</p></div>`
    : html`<p class="empty" role="status">Setting up Spool. This takes a few seconds.</p>`;
  const row = (j) => html`<${Row} key=${j.id} job=${j} flash=${flash.has(j.id)} ...${ctx} />`;
  const stopped = jobs.filter((j) => j.state === "failed" || j.state === "cancelled");
  const finished = jobs.filter((j) => j.state === "done");
  const resumable = stopped.filter(canResume);
  // A finished row that was tinted (already in the list) must be on screen.
  const many = all || finished.slice(SHOWN).some((j) => flash.has(j.id));
  const active = downloading(jobs);
  return html`
    ${active.length > 0 && html`
      <section class="group" aria-labelledby="group-downloading">
        <div class="group-head"><h2 id="group-downloading">Downloading</h2></div>
        <ul>${active.map(row)}</ul>
      </section>`}
    ${stopped.length > 0 && html`
      <section class="group" aria-labelledby="group-stopped">
        <div class="group-head"><h2 id="group-stopped">Stopped</h2>
          ${resumable.length > 1 && html`<button class="quiet" onClick=${() => resumable.forEach((j) => api.retry(j.id).catch((e) => ctx.say(e.message)))}>Resume all</button>`}
        </div>
        <ul>${stopped.map(row)}</ul>
      </section>`}
    ${finished.length > 0 && html`
      <section class="group" aria-labelledby="group-finished">
        <div class="group-head"><h2 id="group-finished">Finished</h2>
          <button class="quiet" onClick=${() => ctx.takeAway(finished.map((j) => j.id), `Cleared ${finished.length}.`, api.clearFinished)}>Clear list</button>
        </div>
        <ul>${(many ? finished : finished.slice(0, SHOWN)).map(row)}</ul>
        ${!many && finished.length > SHOWN && html`<button class="quiet show-all" onClick=${() => setAll(true)}>Show all ${finished.length}</button>`}
      </section>`}`;
}

function Row({ job, flash, snap, say, whenUpdated, openSettings, connected, takeAway }) {
  const [logText, setLogText] = useState(null);
  const [gone, setGone] = useState(null);
  const [problem, setProblem] = useState(null); // why the last action on this row failed, shown on the row
  const act = (fn) => { setProblem(null); return fn().catch((e) => { setProblem(e.message); say(e.message); }); };
  const why = problem && html`<p class="failure" role="alert">${problem}</p>`;
  const tid = `title-${job.id}`; // each row button is described by its row's title, so "Cancel" says which one
  const title = html`<h3 class="title clamp" id=${tid} dir="auto" title=${job.title || job.url}>${job.title || job.url}</h3>`;
  const removeIt = () => takeAway([job.id], "Removed.", () => api.remove(job.id));
  const remove = html`<button class="quiet" aria-describedby=${tid} onClick=${removeIt}>Remove from list</button>`;
  const cls = `row ${flash ? "flash" : ""}`;

  if (job.state === "running" || job.state === "queued") {
    const pct = f.percent(job);
    const running = job.state === "running";
    const step = f.stepPercent(job);
    return html`
      <li class="${cls} active" data-job=${job.id}>
        <div class="counter num" aria-hidden="true">${running && pct != null ? html`${pct}<span>%</span>` : ""}</div>
        <div class="body">
          ${title}
          ${running && (pct == null
            ? html`<progress key="busy" aria-label="Progress of ${job.title}" />`
            : html`<progress key="pct" value=${pct} max="100" aria-label="Progress of ${job.title}" />`)}
          <p class="meta dim">
            ${running ? html`<span>${f.stageWords(job)}</span>
              ${!connected ? html`<span class="reconnecting">Reconnecting…</span>`
                : job.stage === "waiting" ? html`<span class="num">${f.tryingAgain(job.eta)}</span>`
                : job.stage !== "starting" && html`${step != null && html`<span class="num">${step}%</span>`}<span class="num">${f.speed(job.speed)}</span><span class="num">${f.timeLeft(job.eta)}</span>`}`
              : html`<span><${Icon} d=${ICONS.wait} />Waiting</span>`}
          </p>
          ${why}
        </div>
        <div class="actions"><button class="secondary" aria-describedby=${tid} onClick=${() => act(() => api.cancel(job.id))}>Cancel</button></div>
      </li>`;
  }

  if (job.state === "done") {
    const show = async () => {
      try { await api.reveal(job.id); } catch (e) { e.status === 410 ? setGone(e.message) : (setProblem(e.message), say(e.message)); }
    };
    const menuId = `menu-${job.id}`;
    const place = (e) => {
      if (e.newState !== "open") return;
      const r = document.querySelector(`[popovertarget="${menuId}"]`).getBoundingClientRect();
      e.target.style.top = `${r.bottom + 4}px`;
      e.target.style.left = `${Math.max(8, r.right - e.target.offsetWidth)}px`;
    };
    return html`
      <li class="${cls} done" data-job=${job.id}>
        ${job.thumbnail ? html`<img class="thumb small" src=${job.thumbnail} alt="" width="64" height="36" loading="lazy" />` : html`<div class="thumb small" />`}
        <div class="body">
          ${title}
          <p class="meta dim"><span>${f.presetName(job.preset)}</span><span class="num">${f.bytes(job.size)}</span>
            ${job.dest && job.dest !== snap.settings.dest && html`<span class="folder" title=${job.dest}><${Icon} d=${ICONS.folder} /><span class="visually-hidden">In </span>${f.folderName(job.dest)}</span>`}</p>
          ${gone && html`<p class="dim moved">${gone}</p>`}
          ${why}
        </div>
        <div class="actions">
          ${job.file_exists && !gone ? html`<button class="secondary" aria-describedby=${tid} onClick=${show}>${revealWord(snap.tools.platform)}</button>`
            : !gone && html`<span class="dim moved">File moved or deleted</span>`}
          <button class="icon-button" popovertarget=${menuId} aria-label="More actions for ${job.title}" title="More actions">
            <${Icon} d=${ICONS.more} cls="bold" /></button>
          <div class="menu" id=${menuId} popover="auto" onToggle=${place}>
            ${newer(snap) && job.file_exists && !gone && html`<button popovertarget=${menuId} popovertargetaction="hide" onClick=${() => act(() => api.open(job.id))}>Open</button>`}
            <button onClick=${() => act(() => api.retry(job.id))}>Download again</button>
            <button onClick=${removeIt}>Remove from list</button>
          </div>
        </div>
      </li>`;
  }

  if (job.state === "cancelled") return html`
    <li class="${cls} stopped" data-job=${job.id}>
      <div class="state-icon"><${Icon} d=${ICONS.stop} /></div>
      <div class="body">${title}<p class="meta dim"><span>Cancelled</span></p>
        <div class="row-actions">
          <button class="fill" aria-describedby=${tid} onClick=${() => act(() => api.retry(job.id))}>Resume</button>${remove}
        </div>
        ${why}
      </div>
    </li>`;

  // Failed: the server's sentence, and the button its hint names.
  const error = job.error || {};
  const showLog = async () => {
    if (logText != null) return setLogText(null);
    try { setLogText(await api.log(job.id)); } catch (e) { setProblem(e.message); say(e.message); }
  };
  const retry = () => act(() => api.retry(job.id));
  const again = ["show_log", "check_dest", "install_ffmpeg"].includes(error.hint); // fixed outside Spool, then Try again
  return html`
    <li class="${cls} stopped failed" data-job=${job.id}>
      <div class="state-icon danger"><${Icon} d=${ICONS.alert} /></div>
      <div class="body">
        ${title}
        <p class="failure"><span class="visually-hidden">Stopped: </span>${error.message}</p>
        <div class="row-actions">
          ${again && html`<button class="fill" aria-describedby=${tid} onClick=${retry}>Try again</button>`}
          ${error.hint === "show_log" ? html`
            <button class="secondary" aria-describedby=${tid} aria-expanded=${logText != null ? "true" : "false"} aria-controls="log-${job.id}" onClick=${showLog}>
              ${logText != null ? "Hide log" : "Show log"}</button>`
            : html`<${Fix} hint=${error.hint} snap=${snap} engine=${job.engine} retry=${retry} whenUpdated=${whenUpdated}
                changeFolder=${() => openSettings(true)} cls=${again ? "secondary" : "fill"} />`}
          ${remove}
        </div>
        ${why}
        ${logText != null && html`<pre class="log" id="log-${job.id}" tabindex="0" role="region" aria-label="Download log">${logText || "The log is empty."}</pre>`}
      </div>
    </li>`;
}

// handle.current.open(focusFolder) lets "Change folder" land on the folder field.
function SettingsDialog({ snap, handle, notify, running }) {
  const dialog = useRef(), folder = useRef();
  const [quitting, setQuitting] = useState(null); // null, "asking", or the sentence for why Spool did not quit
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  handle.current = {
    open(focusFolder) {
      setDraft(snap.settings.dest); setError(""); setQuitting(null);
      dialog.current.showModal();
      if (focusFolder) folder.current.focus();
    },
  };
  const save = (changes) => api.saveSettings(changes).then(() => setError("")).catch((e) => setError(e.message));
  // The folder saves when the field loses focus, on Enter and on Done; unchanged text is not sent again.
  const saveFolder = () => { const d = draft.trim(); if (d && d !== snap.settings.dest) save({ dest: d }); };
  const choose = async () => {
    try { const res = await api.chooseFolder(); if (res?.path) { setDraft(res.path); save({ dest: res.path }); } }
    catch (e) { setError(e.message); }
  };
  const busy = Object.values(snap.engines).some((e) => e.busy);
  const check = () => {
    setChecking(true);
    Object.entries(snap.engines).forEach(([name, e]) => e.installed && api.updateEngine(name).catch(() => {}));
  };
  const engineError = Object.values(snap.engines).some((e) => e.error);
  // A new tab with no way back to the site: a named window never matches from another site, and the handle
  // window.open returns would let that site navigate Spool's tab.
  const bookmark = `javascript:void(window.open(${JSON.stringify(`${location.origin}/#url=`)}+encodeURIComponent(location.href),"_blank","noopener"))`;
  // After a quit the page shows what it shows for any Spool that stopped.
  const quit = () => api.quit().then(() => dialog.current.close()).catch((e) => setQuitting(e.message));
  // The question takes the Quit button's place, so focus moves to it and back.
  useLayoutEffect(() => {
    if (dialog.current.open) dialog.current.querySelector(quitting === "asking" ? ".quit-ask button" : ".quit")?.focus();
  }, [quitting]);
  const on = notifying();
  const blocked = canNotify() && Notification.permission === "denied";

  return html`
    <dialog class="settings" ref=${dialog} aria-labelledby="settings-title" onClose=${saveFolder}>
      <h2 id="settings-title">Settings</h2>
      <form class="stack" onSubmit=${(e) => { e.preventDefault(); saveFolder(); }} novalidate>
        <label for="folder">Folder</label>
        <div class="folder-row">
          <input id="folder" ref=${folder} type="text" spellcheck="false" autocomplete="off" value=${draft}
            aria-describedby="folder-state" onInput=${(e) => setDraft(e.target.value)} onBlur=${saveFolder} />
          ${snap.tools.can_choose_folder && html`<button type="button" class="secondary" onClick=${choose}>Choose…</button>`}
        </div>
        <p id="folder-state" class=${snap.dest.ok && !error ? "dim" : "danger-text"} role="status" title=${snap.dest.path}>
          ${error || (snap.dest.ok ? `${snap.dest.display}, ${f.bytes(snap.dest.free_bytes)} free` : snap.dest.message)}</p>
        ${!error && snap.dest.problem === "missing" && html`
          <button type="button" class="secondary create" onClick=${() => save({ dest: snap.settings.dest, create: true })}>Create this folder</button>`}
      </form>
      ${canNotify() && html`
        <label class="switch">
          <input type="checkbox" checked=${on} disabled=${blocked} aria-describedby=${blocked ? "notify-blocked" : undefined}
            onChange=${async (e) => { await (on ? notify.turnOff() : notify.turnOn()); e.target.checked = notifying(); }} />
          Get a notification when downloads finish
        </label>
        ${blocked && html`<p id="notify-blocked" class="dim notify-blocked">Your browser blocks notifications from Spool.</p>`}`}
      <div class="stack bookmark">
        <p>Save from any page. Drag this to your bookmarks bar:</p>
        <p><a class="bookmarklet" href=${bookmark} onClick=${(e) => e.preventDefault()}>Save with Spool</a></p>
      </div>
      ${snap.update && html`
        <div class="stack bookmark">
          <p>Spool ${snap.update.version} is ready.</p>
          <${CopyCommand} command="uv tool upgrade spool-dl" platform=${snap.tools.platform} />
          <p class="dim">When it finishes, quit Spool and start it again.</p>
        </div>`}
      <p class="version dim">
        <span>Spool ${snap.version || ""}</span>
        ${busy ? html`<span role="status">Checking…</span>`
          : checking && !engineError ? html`<span role="status">Up to date</span>`
          : html`<button class="link-button" onClick=${check}>Check for updates</button>`}
      </p>
      ${quitting === "asking" ? html`
        <div class="dialog-actions quit-ask">
          <p>Downloads running now stop. You can resume them next time.</p>
          <button class="secondary" onClick=${quit}>Quit now</button>
          <button class="quiet" onClick=${() => setQuitting(null)}>Keep running</button>
        </div>`
        : html`
        ${quitting && html`<p class="danger-text" role="alert">${quitting}</p>`}
        <div class="dialog-actions">
          ${newer(snap) && html`<button class="quiet quit" onClick=${() => (running ? setQuitting("asking") : quit())}>Quit Spool</button>`}
          <button class="secondary" onClick=${() => dialog.current.close()}>Done</button>
        </div>`}
    </dialog>`;
}

render(html`<${App} />`, document.getElementById("app"));
