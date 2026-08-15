const state = {
  root: null,
  indexed: false,
  suggestions: [],
  folders: [],
  browsePath: null,
  model: null,
  visionModel: null,
  // Both folder levels a run planned for itself. Not configuration: they are
  // named per run from the files in the folder, and only used here to offer
  // the same names as autocomplete when a row is corrected by hand.
  categories: [],
  planSource: "organize",
};

const $ = (id) => document.getElementById(id);

async function api(url, body, method = "POST", signal) {
  const opts = { method, headers: { "Content-Type": "application/json" }, signal };
  if (method !== "GET" && method !== "HEAD") opts.body = JSON.stringify(body || {});
  const res = await fetch(url, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detailText(err.detail));
  }
  return res.json();
}

// FastAPI raises with a plain string, but a request that fails validation comes
// back as a list of per-field objects, which stringifies to "[object Object]".
function detailText(detail) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => d && d.msg).filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return "Request failed";
}

/* ---------------- run state (start / stop) ---------------- */

// Long operations run one at a time per panel. While running, the panel's
// primary button becomes a Stop button that aborts the request.
const running = { scan: null, ask: null, organize: null };

function beginRun(key, btn) {
  const controller = new AbortController();
  running[key] = controller;
  btn.dataset.idleLabel = btn.textContent;
  btn.textContent = "Stop";
  btn.classList.add("btn-stop");
  return controller;
}

function endRun(key, btn) {
  running[key] = null;
  if (btn.dataset.idleLabel) btn.textContent = btn.dataset.idleLabel;
  btn.classList.remove("btn-stop");
}

const isAbort = (e) => e && (e.name === "AbortError" || e.name === "TimeoutError");

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function setStatus(el, msg, kind) {
  el.textContent = msg;
  el.className = `inline-status${kind ? " " + kind : ""}`;
}

/* ---------------- theme ---------------- */

function initTheme() {
  const btn = $("theme-toggle");
  const mq = window.matchMedia("(prefers-color-scheme: dark)");

  const effective = () =>
    document.documentElement.getAttribute("data-theme") || (mq.matches ? "dark" : "light");

  const updateIcon = () => {
    btn.textContent = effective() === "dark" ? "🌙" : "☀️";
  };

  btn.addEventListener("click", () => {
    const next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    updateIcon();
  });

  mq.addEventListener("change", () => {
    if (!localStorage.getItem("theme")) updateIcon();
  });

  updateIcon();
}

/* ---------------- init ---------------- */

// Populate a model picker from a list of installed tags. Returns the tag that
// ended up selected, or null when there's nothing to pick.
function fillModelSelect(sel, models, preferred, emptyLabel) {
  sel.innerHTML = "";
  if (!models.length) {
    sel.innerHTML = `<option>${esc(emptyLabel)}</option>`;
    sel.disabled = true;
    return null;
  }
  sel.disabled = false;
  const selected = models.includes(preferred) ? preferred : models[0];
  models.forEach((m) => {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    if (m === selected) o.selected = true;
    sel.appendChild(o);
  });
  return selected;
}

async function init() {
  initTheme();
  try {
    const info = await api("/api/models", null, "GET");

    const sel = $("model-select");
    state.model = fillModelSelect(sel, info.models, info.default, "No models found");
    sel.addEventListener("change", () => (state.model = sel.value));

    // Image reading needs a vision-capable model, and which one is a separate
    // choice from the chat model, so it gets its own picker. Disable the
    // option when none is installed rather than letting it fail at scan time.
    const visionSel = $("vision-select");
    state.visionModel = fillModelSelect(
      visionSel,
      info.vision_models || [],
      info.vision_default,
      "None installed",
    );
    visionSel.addEventListener("change", () => (state.visionModel = visionSel.value));

    if (!state.visionModel) {
      $("read-images").disabled = true;
      $("vision-row").classList.add("is-disabled");
      $("vision-sub").innerHTML =
        'No vision model installed. Run <code>ollama pull qwen2.5vl:3b</code> to enable searching images by their contents.';
    }
  } catch (e) {
    /* model list is non-fatal */
  }

  try {
    const data = await api("/api/browse", null, "GET");
    renderQuickPicks(data.quick_picks, data.recent_roots);
  } catch (e) {
    /* ignore */
  }

}

function renderQuickPicks(picks, recent) {
  const row = $("quick-picks");
  row.innerHTML = "";
  picks.forEach((p) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = p.name;
    b.title = p.path;
    b.addEventListener("click", () => selectRoot(p.path));
    row.appendChild(b);
  });

  renderRecent(recent);
}

function renderRecent(recent) {
  const block = $("recent-block");
  const rr = $("recent-roots");
  rr.innerHTML = "";
  block.classList.toggle("hidden", !recent || !recent.length);
  disarmClearRecent();
  if (!recent || !recent.length) return;

  recent.forEach((r) => {
    const name = r.root.split("/").pop() || r.root;

    // A wrapper rather than one button, since a remove button can't nest
    // inside the button it sits on. The title stays on the wrapper because
    // selectRoot() reads it back to mark the selected chip.
    const chip = document.createElement("span");
    chip.className = "chip chip-removable";
    chip.title = `${r.root} — ${r.file_count} files`;
    if (r.root === state.root) chip.classList.add("is-selected");

    const label = document.createElement("button");
    label.className = "chip-label";
    label.textContent = name;
    label.addEventListener("click", () => selectRoot(r.root));

    const x = document.createElement("button");
    x.className = "chip-x";
    x.innerHTML = "&times;";
    x.setAttribute("aria-label", `Forget ${name}`);
    x.addEventListener("click", () => forgetRecent(r.root));

    chip.append(label, x);
    rr.appendChild(chip);
  });
}

async function forgetRecent(root) {
  try {
    const data = await api("/api/recent/forget", { root });
    renderRecent(data.recent_roots);
    // Forgetting drops the folder's indexed rows, so a folder still selected
    // here has to go back to "needs a scan" rather than answer from nothing.
    if (state.root === root) selectRoot(root);
  } catch (e) {
    /* leave the list as-is; the folder is still there to try again */
  }
}

/* Clearing everything drops the whole index, so the button asks once first. */
let clearRecentTimer = null;

function disarmClearRecent() {
  clearTimeout(clearRecentTimer);
  clearRecentTimer = null;
  const btn = $("clear-recent");
  btn.textContent = "Clear";
  btn.classList.remove("is-armed");
}

$("clear-recent").addEventListener("click", async () => {
  const btn = $("clear-recent");
  if (!clearRecentTimer) {
    btn.textContent = "Clear all?";
    btn.classList.add("is-armed");
    clearRecentTimer = setTimeout(disarmClearRecent, 4000);
    return;
  }
  disarmClearRecent();
  try {
    const data = await api("/api/recent/forget", { root: null });
    renderRecent(data.recent_roots);
    if (state.root) selectRoot(state.root);
  } catch (e) {
    /* ignore */
  }
});

/* ---------------- step gating ---------------- */

function selectRoot(path) {
  state.root = path;
  state.indexed = false;
  state.suggestions = [];

  const disp = $("selected-folder");
  disp.textContent = path;
  disp.classList.add("has-value");

  document.querySelectorAll("#quick-picks .chip, #recent-roots .chip").forEach((c) => {
    c.classList.toggle("is-selected", c.title.split(" — ")[0] === path);
  });

  $("step-scan").classList.remove("is-locked");
  $("index-btn").disabled = false;
  $("step-actions").classList.add("is-locked");
  document.querySelectorAll("#step-actions [data-needs-index]").forEach((el) => (el.disabled = true));
  $("diff-block").classList.add("hidden");
  setStatus($("scan-status"), "");
  setStatus($("organize-status"), "");
  $("ask-answer").classList.add("hidden");
  $("ask-results").innerHTML = "";
}

function unlockActions(count) {
  state.indexed = true;
  $("step-actions").classList.remove("is-locked");
  // Any control a feature marks data-needs-index comes alive with the index.
  document.querySelectorAll("#step-actions [data-needs-index]").forEach((el) => (el.disabled = false));
  document.querySelectorAll("#step-scan .step, #step-folder .step").forEach((s) => s.classList.add("done"));
}

/* ---------------- folder browser sheet ---------------- */

$("browse-btn").addEventListener("click", () => openSheet(state.root));
$("sheet-cancel").addEventListener("click", closeSheet);
$("sheet-backdrop").addEventListener("click", (e) => {
  if (e.target === $("sheet-backdrop")) closeSheet();
});
$("sheet-select").addEventListener("click", () => {
  if (state.browsePath) {
    selectRoot(state.browsePath);
    closeSheet();
  }
});
$("sheet-up").addEventListener("click", () => {
  if (state.parentPath) openSheet(state.parentPath);
});

async function openSheet(path) {
  $("sheet-backdrop").classList.remove("hidden");
  await loadBrowse(path);
}

function closeSheet() {
  $("sheet-backdrop").classList.add("hidden");
}

async function loadBrowse(path) {
  let data;
  try {
    data = await api(`/api/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`, null, "GET");
  } catch (e) {
    return;
  }
  state.browsePath = data.path;
  state.parentPath = data.parent;
  $("sheet-path").textContent = data.path;
  $("sheet-up").disabled = !data.parent;
  $("sheet-select").disabled = !data.selectable;

  const list = $("browse-list");
  list.innerHTML = "";
  if (!data.entries.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No subfolders";
    list.appendChild(li);
    return;
  }
  data.entries.forEach((e) => {
    const li = document.createElement("li");
    li.textContent = e.name;
    li.addEventListener("click", () => loadBrowse(e.path));
    list.appendChild(li);
  });
}

/* ---------------- scan ---------------- */

/** Reads an SSE stream from a POST endpoint, calling onEvent per message. */
async function streamPost(url, body, onEvent, signal) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      const line = chunk.trim();
      if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

$("index-btn").addEventListener("click", async () => {
  const btn = $("index-btn");
  if (running.scan) {
    running.scan.abort();
    return;
  }

  const readImages = $("read-images").checked;
  const controller = beginRun("scan", btn);
  $("scan-progress").classList.remove("hidden");
  $("scan-progress-bar").style.width = "0%";
  $("scan-progress-file").textContent = "Starting…";
  $("scan-progress-count").textContent = "";
  setStatus($("scan-status"), "");

  const started = performance.now();
  let indexed = 0;
  let lastSeen = 0;
  try {
    await streamPost(
      "/api/index/stream",
      { root: state.root, read_images: readImages, vision_model: state.visionModel },
      (ev) => {
        if (ev.type === "begin" && ev.images) {
          setStatus($("scan-status"), `Reading ${ev.images} images with ${state.visionModel}…`);
        } else if (ev.type === "reading") {
          lastSeen = ev.i - 1;
          $("scan-progress-bar").style.width = `${((ev.i - 1) / ev.total) * 100}%`;
          $("scan-progress-count").textContent = `${ev.i} / ${ev.total}`;
          $("scan-progress-file").innerHTML =
            `${ev.vision ? "Looking at" : "Reading"} <code>${esc(ev.filename)}</code>…`;
        } else if (ev.type === "done") {
          indexed = ev.indexed;
        } else if (ev.type === "error") {
          throw new Error(ev.detail);
        }
      },
      controller.signal
    );
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    setStatus($("scan-status"), `${indexed} files indexed in ${secs}s`, "ok");
    unlockActions(indexed);
  } catch (e) {
    if (isAbort(e)) {
      // Files read before stopping are already committed, so the partial
      // index is usable — unlock the actions rather than discarding it.
      setStatus($("scan-status"), `Stopped after ${lastSeen} files (partial index kept).`, "ok");
      if (lastSeen > 0) unlockActions(lastSeen);
    } else {
      setStatus($("scan-status"), e.message, "err");
    }
  } finally {
    endRun("scan", btn);
    $("scan-progress").classList.add("hidden");
  }
});

/* ---------------- tabs ---------------- */

// Panels are found by id from the tab's data-tab, so adding a feature is a
// seg-item plus a #panel-<name> element. Nothing here changes.
function showTab(which) {
  document.querySelectorAll(".seg-item").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.tab === which);
    t.setAttribute("aria-selected", String(t.dataset.tab === which));
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.id !== `panel-${which}`);
  });
  // The plan review is shared by Organize and Do, so it follows them rather
  // than living in either panel. Find has nothing to apply.
  $("plan-area").classList.toggle("hidden", which === "find");
}

document.querySelectorAll(".seg-item").forEach((tab) => {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
});

showTab(document.querySelector(".seg-item.is-active")?.dataset.tab);

/* ---------------- ask ---------------- */

$("ask-btn").addEventListener("click", runAsk);
$("ask-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runAsk();
});

async function runAsk() {
  const btn = $("ask-btn");
  if (running.ask) {
    running.ask.abort();
    return;
  }

  const q = $("ask-input").value.trim();
  if (!q) return;
  const answerEl = $("ask-answer");
  const resultsEl = $("ask-results");
  answerEl.classList.remove("hidden");
  answerEl.textContent = `Thinking… (${state.model || "model"})`;
  resultsEl.innerHTML = "";
  setStatus($("ask-timing"), "");
  const controller = beginRun("ask", btn);
  const started = performance.now();
  try {
    const data = await api("/api/ask", { question: q, model: state.model }, "POST", controller.signal);
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    answerEl.textContent = data.answer;
    setStatus($("ask-timing"), `${state.model} · ${secs}s`);
    renderResults(data.results || (data.referenced_paths || []).map((path) => ({ path })));
  } catch (e) {
    if (isAbort(e)) {
      answerEl.textContent = "Stopped.";
    } else {
      answerEl.textContent = `Error: ${e.message}`;
    }
  } finally {
    endRun("ask", btn);
  }
}

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".heic"]);

// The server wraps matched terms in these rather than in markup, so the text
// around them can be escaped before they become tags.
const MATCH_OPEN = "\x02";
const MATCH_CLOSE = "\x03";

function highlight(snippet) {
  return esc(snippet)
    .split(MATCH_OPEN).join("<strong>")
    .split(MATCH_CLOSE).join("</strong>");
}

function fileGlyph(ext) {
  if (ext === ".pdf") return "📄";
  if (ext === ".docx" || ext === ".doc") return "📝";
  if (ext === ".pptx" || ext === ".key") return "📊";
  if (ext === ".xlsx" || ext === ".csv") return "📈";
  if (ext === ".zip" || ext === ".dmg" || ext === ".pkg") return "📦";
  return "📄";
}

/* A result shows why it was returned: the image itself, or the matching span
   of its text. Without that, a grounded reference is just a filename the user
   has to take on faith. */
function renderResults(items) {
  const list = $("ask-results");
  list.innerHTML = "";

  items.forEach((item) => {
    const path = item.path;
    const ext = (item.ext || path.slice(path.lastIndexOf("."))).toLowerCase();

    const li = document.createElement("li");
    li.className = "result-card";
    li.title = path;

    const thumb = document.createElement("div");
    thumb.className = "result-thumb";
    if (IMAGE_EXTS.has(ext)) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = `/api/thumb?path=${encodeURIComponent(path)}`;
      // A preview that fails to load should leave the row intact, not a
      // broken-image icon.
      img.addEventListener("error", () => {
        thumb.textContent = "🖼";
      });
      thumb.appendChild(img);
    } else {
      thumb.textContent = fileGlyph(ext);
    }

    const body = document.createElement("div");
    body.className = "result-body";

    const name = document.createElement("div");
    name.className = "result-name";
    name.textContent = item.filename || path.split("/").pop();
    body.appendChild(name);

    if (item.snippet) {
      const snippet = document.createElement("div");
      snippet.className = "result-snippet";
      snippet.innerHTML = highlight(item.snippet);
      body.appendChild(snippet);
    }

    li.append(thumb, body);
    li.addEventListener("click", () =>
      api("/api/reveal", { path }).catch((e) =>
        setStatus($("ask-timing"), `Couldn't open ${path}: ${e.message}`, "err")
      )
    );
    list.appendChild(li);
  });
}

/* ---------------- do (natural-language actions) ---------------- */

$("do-btn").addEventListener("click", runCommand);
$("do-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runCommand();
});

async function runCommand() {
  const btn = $("do-btn");
  if (running.organize) {
    running.organize.abort();
    return;
  }
  const command = $("do-input").value.trim();
  if (!command || !state.root) return;

  const status = $("do-status");
  state.planSource = "do";
  // Interpretation has one status message. The shared progress row remains
  // visible for its bar/count, but gets text only once file planning begins.
  setStatus(status, "Reading your request…");
  state.suggestions = [];
  $("diff-block").classList.add("hidden");
  $("organize-progress").classList.remove("hidden");
  setProgress(0, 1, null, "");

  const controller = beginRun("organize", btn);
  try {
    await streamPost(
      "/api/command/stream",
      {
        root: state.root,
        command,
        model: state.model,
      },
      (ev) => {
        if (ev.type === "error") {
          setStatus(status, ev.detail, "err");
        } else if (ev.type === "interpreted") {
          const what = ev.action === "trash" ? "to Trash" : ev.action === "rename_folder" ? "rename folder" : ev.action;
          const target = ev.target || "files";
          setStatus(status, `${what} · ${ev.matched} of ${ev.total} ${target} match`);
          if (!ev.matched) setProgress(1, 1, null, "Nothing matched");
        } else if (ev.type === "begin") {
          setProgress(0, ev.total, null);
        } else if (ev.type === "reading") {
          setProgress(ev.i - 1, ev.total, ev.filename);
        } else if (ev.type === "item") {
          state.suggestions.push(ev.suggestion);
          setProgress(ev.i, ev.total, null);
          renderDiff();
          renderReview();
          $("diff-block").classList.remove("hidden");
        } else if (ev.type === "revised") {
          state.suggestions = ev.suggestions;
          renderDiff();
          renderReview();
          $("diff-block").classList.toggle("hidden", !state.suggestions.length);
        } else if (ev.type === "done") {
          setProgress(1, 1, null, `${ev.total} file${ev.total === 1 ? "" : "s"} planned`);
        }
      },
      controller.signal,
    );
  } catch (e) {
    if (isAbort(e)) setStatus(status, "Stopped.");
    else setStatus(status, e.message, "err");
  } finally {
    endRun("organize", btn);
  }
}

/* ---------------- organize ---------------- */

function setProgress(done, total, filename, label) {
  $("progress-bar").style.width = total ? `${(done / total) * 100}%` : "0%";
  $("progress-count").textContent = total ? `${done} / ${total}` : "";
  // An explicitly empty label intentionally leaves this row textless. This is
  // used while Do is interpreting a request, whose message lives in do-status.
  if (label !== undefined) {
    $("progress-file").textContent = label;
    return;
  }
  $("progress-file").innerHTML = filename
    ? `Reading <code>${esc(filename)}</code>…`
    : "Starting…";
}

$("suggest-btn").addEventListener("click", async () => {
  const btn = $("suggest-btn");
  if (running.organize) {
    running.organize.abort();
    return;
  }

  const controller = beginRun("organize", btn);
  state.planSource = "organize";
  state.suggestions = [];
  $("diff-block").classList.add("hidden");
  $("organize-progress").classList.remove("hidden");
  setProgress(0, 0, null);
  setStatus($("organize-status"), `Analyzing with ${state.model || "model"}…`);

  const started = performance.now();
  try {
    await streamPost(
      "/api/organize/suggest/stream",
      { root: state.root, model: state.model },
      (ev) => {
        if (ev.type === "begin") {
          setProgress(0, ev.total, null);
        } else if (ev.type === "planning") {
          setProgress(0, ev.total, null, ev.learned ? "Planning folders (using this folder's history)…" : "Planning folders…");
        } else if (ev.type === "vocabulary") {
          // The folders this run named for itself, both levels. They drive the
          // autocomplete on the review table, so a hand correction lands in a
          // planned folder instead of typing a near-duplicate into existence.
          state.categories = ev.categories || [];
          state.folders = ev.folders || [];
        } else if (ev.type === "reading") {
          setProgress(ev.i - 1, ev.total, ev.filename);
        } else if (ev.type === "item") {
          state.suggestions.push(ev.suggestion);
          setProgress(ev.i, ev.total, null);
          renderDiff();
          renderReview();
          $("diff-block").classList.remove("hidden");
        } else if (ev.type === "revised") {
          // Folders that ended up with a single file, or split across two
          // categories, are only visible once every file has an answer.
          state.suggestions = ev.suggestions;
          setProgress(state.suggestions.length, state.suggestions.length, null, "Tidying folders…");
          renderDiff();
          renderReview();
        }
      },
      controller.signal
    );

    const secs = ((performance.now() - started) / 1000).toFixed(1);
    if (!state.suggestions.length) {
      setStatus($("organize-status"), "Nothing left to organize in this folder.", "ok");
      $("diff-block").classList.add("hidden");
    } else {
      setStatus($("organize-status"), `Analyzed ${state.suggestions.length} files in ${secs}s.`, "ok");
    }
  } catch (e) {
    if (isAbort(e)) {
      // Suggestions are inert until Apply, so keeping the partial set lets you
      // review and apply what was analysed before stopping.
      const n = state.suggestions.length;
      setStatus(
        $("organize-status"),
        n ? `Stopped. ${n} files analyzed — review and apply below.` : "Stopped.",
        "ok"
      );
    } else {
      setStatus($("organize-status"), e.message, "err");
    }
  } finally {
    endRun("organize", btn);
    $("organize-progress").classList.add("hidden");
  }
});

function selectedSuggestions() {
  const checks = document.querySelectorAll("#review-body input[type=checkbox]");
  return Array.from(checks)
    .filter((c) => c.checked)
    .map((c) => state.suggestions[Number(c.dataset.i)]);
}

/* --- file tree visualisation --- */

function newNode() {
  return { children: new Map(), files: [] };
}

function insertPath(root, parts, leaf) {
  if (!parts.length) {
    root.files.push(leaf);
    return;
  }
  const [head, ...rest] = parts;
  if (!root.children.has(head)) root.children.set(head, newNode());
  insertPath(root.children.get(head), rest, leaf);
}

function renderNode(node, prefix) {
  const dirs = [...node.children.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const files = [...node.files].sort((a, b) => a.name.localeCompare(b.name));
  const items = [
    ...dirs.map(([name, n]) => ({ kind: "dir", name, node: n })),
    ...files.map((f) => ({ kind: "file", ...f })),
  ];

  let out = "";
  items.forEach((item, i) => {
    const last = i === items.length - 1;
    const branch = last ? "└── " : "├── ";
    if (item.kind === "dir") {
      out += `${prefix}${branch}<span class="dir">${esc(item.name)}/</span>\n`;
      out += renderNode(item.node, prefix + (last ? "    " : "│   "));
    } else {
      const note = item.note ? ` <span class="rename">${esc(item.note)}</span>` : "";
      const cls = item.isNew ? "new" : "";
      out += `${prefix}${branch}<span class="${cls}">${esc(item.name)}</span>${note}\n`;
    }
  });
  return out;
}

/* Where a suggestion lands, as tree path segments. Mirrors the server's
   destination rules: trash wins, then an explicit folder, then category. */
function destParts(s) {
  if (s.trash) return ["🗑 Trash"];
  if (s.rename_only || s.folder_rename) {
    const rel = s.path.startsWith(state.root)
      ? s.path.slice(state.root.length).replace(/^\//, "")
      : s.path;
    const parts = rel.split("/");
    parts.pop();
    return parts.filter(Boolean);
  }
  if (s.dest_folder) return s.dest_folder.split("/").filter(Boolean);
  return s.subcategory ? [s.category, s.subcategory] : [s.category];
}

function renderDiff() {
  const root = state.root;
  const rootName = root.split("/").filter(Boolean).pop() || root;

  const before = newNode();
  const after = newNode();

  state.suggestions.forEach((s) => {
    const rel = s.path.startsWith(root) ? s.path.slice(root.length).replace(/^\//, "") : s.path;
    const parts = rel.split("/");
    const name = parts.pop();
    if (s.folder_rename) {
      insertPath(before, parts, { name: `${name}/` });
      const newName = s.suggested_filename || name;
      insertPath(after, parts, {
        name: `${newName}/`,
        isNew: true,
        note: newName !== name ? `← ${name}/` : "",
      });
      return;
    }
    insertPath(before, parts, { name });

    const newName = s.suggested_filename || name;
    insertPath(after, destParts(s), {
      name: newName,
      isNew: true,
      note: newName !== name ? `← ${name}` : "",
    });
  });

  const header = `<span class="dir">${esc(rootName)}/</span>\n`;
  $("tree-before").innerHTML = header + renderNode(before, "");
  $("tree-after").innerHTML = header + renderNode(after, "");

  // Trashing is counted separately, so "12 files → Trash" can never be misread
  // as a move into a folder of that name.
  const trashed = state.suggestions.filter((s) => s.trash).length;
  const renamed = state.suggestions.filter((s) => s.rename_only).length;
  const renamedFolders = state.suggestions.filter((s) => s.folder_rename).length;
  const folders = new Set();
  state.suggestions.forEach((s) => {
    if (!s.trash && !s.rename_only && !s.folder_rename) folders.add(destParts(s).join("/"));
  });
  const parts = [];
  const moved = state.suggestions.length - trashed - renamed - renamedFolders;
  if (folders.size) parts.push(`${moved} files → ${folders.size} folders`);
  if (renamed) parts.push(`${renamed} renamed in place`);
  if (renamedFolders) parts.push(`${renamedFolders} folder${renamedFolders === 1 ? "" : "s"} renamed`);
  if (trashed) parts.push(`${trashed} → Trash`);
  $("diff-summary").textContent = parts.join(" · ") || "No changes";
}

/* --- review table --- */

// The folders planned for this run double as autocomplete on the category and
// subcategory fields, so a manual correction lands in one of them rather than
// typing a near-duplicate into existence. Both fields stay free text: the run
// names its own folders, so there is no fixed list to pick from.
function datalist(id) {
  let list = $(id);
  if (!list) {
    list = document.createElement("datalist");
    list.id = id;
    document.body.appendChild(list);
  }
  list.innerHTML = "";
  return list;
}

function renderFolderOptions() {
  const folders = datalist("folder-options");
  state.folders.forEach((folder) => {
    const opt = document.createElement("option");
    opt.value = folder.name;
    opt.label = `${folder.category}/${folder.name}`;
    folders.appendChild(opt);
  });

  // A run's own categories, plus any the suggestions ended up using, so a row
  // is never the only place a name exists.
  const categories = datalist("category-options");
  const names = new Set(state.categories);
  state.suggestions.forEach((s) => s.category && names.add(s.category));
  names.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    categories.appendChild(opt);
  });
}

function renderReview() {
  const body = $("review-body");
  body.innerHTML = "";
  $("review-count").textContent = `(${state.suggestions.length})`;
  renderFolderOptions();

  state.suggestions.forEach((s, i) => {
    const tr = document.createElement("tr");

    const tdCheck = document.createElement("td");
    tdCheck.className = "col-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.dataset.i = i;
    tdCheck.appendChild(cb);

    const name = s.path.split("/").pop();
    const ext = name.slice(name.lastIndexOf(".")).toLowerCase();

    // A thumbnail is what makes a suggested category checkable at a glance:
    // deciding whether a screenshot belongs in Screenshots is a question about
    // the picture, not about its filename.
    const tdThumb = document.createElement("td");
    tdThumb.className = "col-thumb";
    if (IMAGE_EXTS.has(ext)) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = `/api/thumb?path=${encodeURIComponent(s.path)}`;
      img.addEventListener("error", () => img.remove());
      tdThumb.appendChild(img);
    }

    const tdName = document.createElement("td");
    tdName.className = "fname";
    tdName.textContent = name;
    tdName.title = s.path;

    const tdCat = document.createElement("td");
    // A command's rows have a destination of their own, so the category picker
    // and subfolder field would be editing something that is never read. They
    // show where the file is going instead, spanning both columns.
    if (s.trash || s.dest_folder || s.rename_only || s.folder_rename) {
      tdCat.colSpan = 2;
      tdCat.className = s.trash ? "dest is-trash" : "dest";
      tdCat.textContent = s.trash ? "🗑 Trash" : s.folder_rename ? "Same parent" : s.rename_only ? "Same folder" : s.dest_folder;
      tr.append(tdCheck, tdThumb, tdName, tdCat);

      const tdNewName = document.createElement("td");
      const renamed = document.createElement("input");
      renamed.type = "text";
      renamed.value = s.suggested_filename || "";
      renamed.addEventListener("change", () => {
        state.suggestions[i].suggested_filename = renamed.value.trim();
        renderDiff();
      });
      tdNewName.appendChild(renamed);
      tr.appendChild(tdNewName);
      body.appendChild(tr);
      return;
    }

    const cat = document.createElement("input");
    cat.type = "text";
    cat.value = s.category || "";
    cat.setAttribute("list", "category-options");
    cat.addEventListener("change", () => {
      state.suggestions[i].category = cat.value.trim() || "Other";
      renderDiff();
    });
    tdCat.appendChild(cat);

    const tdSub = document.createElement("td");
    const sub = document.createElement("input");
    sub.type = "text";
    sub.value = s.subcategory || "";
    sub.placeholder = "—";
    sub.setAttribute("list", "folder-options");
    sub.addEventListener("change", () => {
      state.suggestions[i].subcategory = sub.value.trim() || null;
      renderDiff();
    });
    tdSub.appendChild(sub);

    const tdNew = document.createElement("td");
    const nm = document.createElement("input");
    nm.type = "text";
    nm.value = s.suggested_filename || "";
    nm.addEventListener("change", () => {
      state.suggestions[i].suggested_filename = nm.value.trim();
      renderDiff();
    });
    tdNew.appendChild(nm);

    tr.append(tdCheck, tdThumb, tdName, tdCat, tdSub, tdNew);
    body.appendChild(tr);
  });
}

$("check-all").addEventListener("change", (e) => {
  document
    .querySelectorAll("#review-body input[type=checkbox]")
    .forEach((c) => (c.checked = e.target.checked));
});

/* --- apply / undo --- */

$("apply-btn").addEventListener("click", async () => {
  const selected = selectedSuggestions();
  if (!selected.length) return;
  const status = $(state.planSource === "do" ? "do-status" : "organize-status");
  setStatus(status, "Applying…");
  try {
    const data = await api("/api/organize/apply", { root: state.root, suggestions: selected });
    setStatus(status, `Applied ${data.applied} changes.`, "ok");
    state.suggestions = [];
    $("diff-block").classList.add("hidden");
  } catch (e) {
    setStatus(status, e.message, "err");
  }
});

$("undo-btn").addEventListener("click", async () => {
  setStatus($("organize-status"), "Undoing…");
  try {
    const data = await api("/api/organize/undo", {});
    setStatus(
      $("organize-status"),
      data.reverted ? `Restored ${data.reverted} files.` : "Nothing to undo.",
      "ok"
    );
    $("diff-block").classList.add("hidden");
    state.suggestions = [];
  } catch (e) {
    setStatus($("organize-status"), e.message, "err");
  }
});

/* ---------------- the tab owns the server ---------------- */

// Closing this tab stops the local server. Every open page checks in on this
// interval; the server only shuts down when a page unloads and nobody checks
// in afterwards, so a reload or a second open tab keeps it alive. Must stay
// below SIFT_SHUTDOWN_GRACE in main.py.
const HEARTBEAT_MS = 4000;

const heartbeat = () => navigator.sendBeacon("/api/heartbeat");

heartbeat();
setInterval(heartbeat, HEARTBEAT_MS);

// Timers in a hidden tab are throttled to about once a minute, which is too
// slow to reclaim the server inside the grace window. Check in immediately
// whenever this tab becomes visible, which is what happens to the tab behind
// one that was just closed.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) heartbeat();
});

// pagehide fires for a close, a reload, and a navigation alike. The server
// tells them apart by whether anything checks in next.
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/closing"));

init();
