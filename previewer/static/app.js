(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const state = {
    items: [], tab: "artifacts", selected: null, checked: new Set(), pin: "", filter: "", sort: "newest", liveOpen: [], liveActive: null,
    movieEdit: null, movieNotes: [], movieMediaId: null, moviePlayhead: 0, movieRenderToken: 0,
  };
  const artifactKinds = new Set(["image", "video", "audio", "markdown", "report", "pdf", "log", "subtitle", "file", "html"]);
  const liveKinds = new Set(["live_url", "novnc", "blend", "apk"]);
  const movieKinds = new Set(["video", "movie_project"]);
  // Tabs with sort and multi-select: Movies and 3D Models work like Review Inbox.
  const listToolTabs = new Set(["review", "movies", "models"]);
  const oldCandidateAgeMs = 4 * 24 * 60 * 60 * 1000;
  // Loaded 3D viewers by item id, so Open in slicer can ask one for an STL copy.
  const modelViewers = new Map();
  const byTitle = (left, right) => left.title.localeCompare(right.title, undefined, { numeric: true, sensitivity: "base" });
  const reviewSorts = {
    newest: (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
    oldest: (left, right) => Date.parse(left.created_at) - Date.parse(right.created_at),
    largest: (left, right) => right.size_bytes - left.size_bytes,
    smallest: (left, right) => left.size_bytes - right.size_bytes,
    "name-az": byTitle,
    "name-za": (left, right) => byTitle(right, left),
  };

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
    const body = await response.json();
    if (!response.ok) {
      const error = new Error(body.error || `Request failed: ${response.status}`);
      error.pinRequired = Boolean(body.pin_required);
      throw error;
    }
    return body;
  }

  // Approve and delete away from this computer (through a reverse proxy) need the PIN; it stays in memory for this page only.
  async function humanAction(payload) {
    for (;;) {
      try {
        return await api("/api/human/action", { method: "POST", headers: state.pin ? { "X-Previewer-PIN": state.pin } : {}, body: JSON.stringify(payload) });
      } catch (error) {
        if (!error.pinRequired) throw error;
        state.pin = await askPin(error.message);
        if (!state.pin) {
          const cancelled = new Error("PIN not entered");
          cancelled.cancelled = true;
          throw cancelled;
        }
      }
    }
  }

  function askPin(message) {
    const dialog = $("#pin-dialog");
    const input = $("#action-pin");
    input.value = "";
    $("#pin-dialog-error").textContent = message === "PIN required" ? "" : message;
    dialog.returnValue = "";
    dialog.showModal();
    input.focus();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok" ? input.value : ""), { once: true });
    });
  }

  async function load() {
    try {
      const [{ items }, capacity] = await Promise.all([api("/api/items"), api("/api/capacity")]);
      state.items = items;
      renderCapacity(capacity);
      renderCounts();
      renderList();
      const queryItem = new URLSearchParams(location.search).get("item");
      if (queryItem && !state.selected) {
        const item = items.find((value) => value.id === queryItem);
        if (item?.state === "approved" && (item.kind === "movie_project" || item.placed_by_agent)) switchTab("movies");
        if (item?.kind === "model3d") switchTab("models");
        selectItem(item);
      }
      announce(`Loaded ${items.length} items`);
    } catch (error) { announce(error.message); }
  }

  // Items an agent placed straight into Movies stay out of Artifacts; agent audio lists in Movies.
  // 3D models have their own tab, candidates and approved together, and stay out of Review Inbox.
  function inTab(item, tab) {
    if (tab === "artifacts") return item.state === "approved" && artifactKinds.has(item.kind) && !item.placed_by_agent;
    if (tab === "live") return item.state === "approved" && liveKinds.has(item.kind);
    if (tab === "movies") return item.state === "approved" && (movieKinds.has(item.kind) || (item.placed_by_agent && item.kind === "audio"));
    if (tab === "models") return item.kind === "model3d";
    return item.kind !== "model3d" && (item.state === "candidate" || item.state === "hidden");
  }

  function itemsForTab() {
    const visible = state.items.filter((item) => inTab(item, state.tab));
    const needle = state.filter.trim().toLowerCase();
    const matching = needle ? visible.filter((item) => `${item.title} ${item.project_run_id}`.toLowerCase().includes(needle)) : visible;
    return listToolTabs.has(state.tab) ? matching.sort(reviewSorts[state.sort] || reviewSorts.newest) : matching;
  }

  function renderCounts() {
    $("#artifact-count").textContent = state.items.filter((item) => inTab(item, "artifacts")).length;
    $("#live-count").textContent = state.items.filter((item) => inTab(item, "live")).length;
    $("#movie-count").textContent = state.items.filter((item) => inTab(item, "movies")).length;
    $("#model-count").textContent = state.items.filter((item) => inTab(item, "models")).length;
    $("#review-count").textContent = state.items.filter((item) => item.state === "candidate" && item.kind !== "model3d").length;
    renderBulkDelete();
  }

  function oldCandidates() {
    const cutoff = Date.now() - oldCandidateAgeMs;
    return state.items.filter((item) => item.state === "candidate" && item.kind !== "model3d" && Date.parse(item.created_at) < cutoff);
  }

  function renderBulkDelete() {
    const button = $("#bulk-delete-old");
    const count = oldCandidates().length;
    button.hidden = state.tab !== "review" || count === 0;
    button.textContent = `Delete ${count} old`;
    button.setAttribute("aria-label", `Delete ${count} candidates older than four days`);
    button.title = "Delete candidates older than four days";
  }

  // Only checked items the filter still shows get deleted.
  function checkedItems() {
    return listToolTabs.has(state.tab) ? itemsForTab().filter((item) => state.checked.has(item.id)) : [];
  }

  function renderDeleteSelected() {
    const button = $("#delete-selected");
    const count = checkedItems().length;
    button.hidden = count === 0;
    button.textContent = `Delete ${count} selected`;
  }

  function renderCapacity(capacity) {
    const percent = Math.min(100, Math.round(capacity.ratio * 100));
    $("#capacity-label").textContent = `${formatBytes(capacity.used_bytes)} of ${formatBytes(capacity.limit_bytes)}`;
    $("#capacity-meter").style.width = `${percent}%`;
    $("#capacity-meter").style.background = percent >= 90 ? "var(--danger)" : percent >= 80 ? "var(--warning)" : "var(--teal)";
  }

  function renderList() {
    const list = $("#item-list");
    // Keep keyboard and remote focus on the same item across a redraw.
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.dataset.itemId ? `${focused.tagName}:${focused.dataset.itemId}` : null;
    list.replaceChildren();
    const items = itemsForTab();
    renderDeleteSelected();
    if (!items.length) {
      const empty = element("p", "list-empty", state.filter ? "No matching items." : state.tab === "review" ? "No candidates await review." : state.tab === "models" ? "No 3D models yet." : "No approved items yet.");
      list.append(empty);
      return;
    }
    items.forEach((item) => {
      const button = element("button", `item-card${state.selected?.id === item.id ? " active" : ""}`);
      button.type = "button";
      button.dataset.itemId = item.id;
      button.addEventListener("click", () => selectItem(item));
      const row = element("span", "item-row");
      const labels = element("span");
      labels.append(element("span", "kind-dot", item.kind));
      if (item.placed_by_agent) {
        const tag = element("span", "agent-tag", "agent");
        tag.title = "Placed in Movies by an agent, not approved by you";
        labels.append(tag);
      }
      labels.append(element("strong", "", item.title), element("small", "", item.project_run_id));
      const status = element("span", `state-dot ${item.state === "approved" ? "approved" : item.verification_status}`);
      status.title = item.state === "candidate" ? item.verification_status : item.state;
      row.append(labels, status);
      button.append(row);
      if (!listToolTabs.has(state.tab)) {
        button.setAttribute("role", "listitem");
        list.append(button);
        return;
      }
      const entry = element("div", "item-entry");
      entry.setAttribute("role", "listitem");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.dataset.itemId = item.id;
      box.checked = state.checked.has(item.id);
      box.setAttribute("aria-label", `Select ${item.title}`);
      box.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); box.click(); } });
      box.addEventListener("change", () => {
        if (box.checked) state.checked.add(item.id); else state.checked.delete(item.id);
        renderDeleteSelected();
      });
      entry.append(box, button);
      list.append(entry);
    });
    if (focusKey) [...list.querySelectorAll("[data-item-id]")].find((node) => `${node.tagName}:${node.dataset.itemId}` === focusKey)?.focus({ preventScroll: true });
  }

  function selectItem(item) {
    if (!item) return;
    if (state.selected?.id !== item.id) {
      state.movieEdit = null;
      state.movieNotes = [];
      state.movieMediaId = null;
      state.moviePlayhead = 0;
    }
    state.selected = item;
    renderList();
    $("#empty").hidden = true;
    $("#detail").hidden = false;
    $("#detail-kind").textContent = `${item.kind} · ${item.state}${item.placed_by_agent ? " · agent" : ""}`;
    $("#detail-title").textContent = item.title;
    $("#detail-meta").textContent = `${item.project_run_id} · ${new Date(item.created_at).toLocaleString()} · ${formatBytes(item.size_bytes)}`;
    renderActions(item);
    renderReceipt(item);
    renderPreview(item);
    history.replaceState(null, "", `?item=${encodeURIComponent(item.id)}`);
  }

  function renderActions(item) {
    const actions = $("#detail-actions");
    actions.replaceChildren();
    if (item.state === "candidate" && item.verification_status === "ready") actions.append(actionButton("Approve", "approve", item));
    if (item.state === "candidate") actions.append(actionButton("Delete candidate", "delete", item, true));
    if (item.state === "approved") {
      if (item.placed_by_agent) actions.append(actionButton("Approve", "approve", item));
      actions.append(actionButton("Hide", "hide", item));
      if (state.tab === "movies" || state.tab === "models") actions.append(actionButton("Delete", "delete", item, true));
    }
    if (item.state === "hidden") {
      actions.append(actionButton("Restore", "restore", item));
      actions.append(actionButton("Delete permanently", "delete", item, true));
    }
    if (item.kind === "model3d" && ["candidate", "approved"].includes(item.state)) actions.prepend(openInSlicerButton(item));
    if (!["live_url", "novnc"].includes(item.kind)) actions.prepend(showInFolderButton(item));
  }

  // Opens the model in PrusaSlicer on this computer. PrusaSlicer reads STL, 3MF and OBJ;
  // a glTF model goes as an STL copy made by the viewer, in millimetres.
  function openInSlicerButton(item) {
    const button = element("button", "", "Open in slicer");
    button.type = "button";
    button.title = "Open the model in PrusaSlicer on this computer";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        let result;
        if (["stl", "3mf", "obj"].includes(fileExtension(item))) {
          result = await api(`/api/items/${item.id}/open-in-slicer`, { method: "POST", body: "{}" });
        } else {
          const loaded = modelViewers.get(item.id);
          if (!loaded?.canvas.isConnected) throw new Error("Wait for the model to load in the viewer first.");
          announce("Making an STL copy for the slicer");
          result = await api(`/api/items/${item.id}/open-in-slicer`, { method: "POST", headers: { "Content-Type": "model/stl" }, body: loaded.viewer.exportStl() });
        }
        const message = result.opened === "stl copy" ? "Opened an STL copy in PrusaSlicer" : result.opened === "original" ? "Opened the original in PrusaSlicer" : "Original moved or deleted. Opened Previewer's copy in PrusaSlicer";
        button.textContent = "Opened";
        button.title = message;
        announce(message);
        setTimeout(() => { button.textContent = "Open in slicer"; button.title = "Open the model in PrusaSlicer on this computer"; }, 2500);
      } catch (error) { alert(`Open in slicer failed. ${error.message}`); }
      finally { button.disabled = false; }
    });
    return button;
  }

  function showInFolderButton(item) {
    const button = element("button", "", "Open in folder");
    button.type = "button";
    button.title = "Show the file in the file manager";
    button.addEventListener("click", async () => {
      try {
        const { opened } = await api(`/api/items/${item.id}/show-in-folder`, { method: "POST", body: "{}" });
        const message = opened === "original" ? "Opened original folder" : "Original moved or deleted. Opened Previewer's copy";
        button.textContent = opened === "original" ? "Opened" : "Opened copy";
        announce(message);
        button.title = message;
        setTimeout(() => { button.textContent = "Open in folder"; button.title = "Show the file in the file manager"; }, 2500);
      } catch (error) { alert(`Open in folder failed. ${error.message}`); }
    });
    return button;
  }

  function actionButton(label, action, item, danger = false) {
    const button = element("button", danger ? "danger" : "", label);
    button.type = "button";
    button.addEventListener("click", () => runAction(action, item));
    return button;
  }

  function renderReceipt(item) {
    const receipt = item.receipt;
    const status = $("#receipt-status");
    status.textContent = item.verification_status;
    status.className = `status ${item.verification_status}`;
    const proved = $("#proved-list");
    proved.replaceChildren();
    (receipt?.proved || ["Verification has not completed."]).forEach((text) => proved.append(element("li", "", text)));
    const warnings = $("#warnings");
    warnings.replaceChildren();
    [...(receipt?.warnings || []), ...(receipt?.errors || [])].forEach((text) => warnings.append(element("p", "warning", text)));
    const evidence = $("#evidence");
    evidence.replaceChildren();
    (receipt?.evidence || []).forEach((name) => {
      const link = element("a", "evidence-card");
      link.href = evidenceUrl(item, name);
      link.target = "_blank";
      link.rel = "noopener";
      if (/\.(png|jpe?g|gif|webp)$/i.test(name)) {
        const image = new Image(); image.src = link.href; image.alt = `Verification evidence: ${name}`; link.append(image);
      }
      link.append(document.createTextNode(name));
      evidence.append(link);
    });
  }

  function renderPreview(item) {
    const toolbar = $("#canvas-toolbar");
    const preview = $("#preview");
    releaseMedia(preview);
    preview.classList.remove("movie-preview");
    if (state.tab === "movies" && movieKinds.has(item.kind) && item.state === "approved") {
      toolbar.hidden = true;
      preview.classList.add("movie-preview");
      preview.replaceChildren(element("p", "list-empty", "Loading movie workspace…"));
      renderMovieEditor(item, preview);
      return;
    }
    if (item.kind === "model3d" && ["candidate", "approved"].includes(item.state)) {
      toolbar.hidden = true;
      preview.replaceChildren();
      mountModel(item, preview);
      return;
    }
    if (liveKinds.has(item.kind) && item.state === "approved") {
      toolbar.hidden = false;
      openLive(item);
      return;
    }
    toolbar.hidden = true;
    preview.replaceChildren();
    if (item.state === "hidden") {
      preview.append(element("p", "list-empty", "This item is hidden. Restore it to preview."));
      return;
    }
    const url = contentUrl(item);
    if (item.kind === "image") { const image = new Image(); image.src = url; image.alt = item.expected_result; preview.append(image); }
    else if (item.kind === "video" || item.kind === "audio") {
      const wrap = document.createElement("div");
      wrap.className = "media-wrap";
      const media = document.createElement(item.kind);
      media.src = url;
      media.controls = true;
      media.playsInline = true;
      media.preload = "auto";
      const play = element("button", "primary", "Play");
      play.type = "button";
      const sync = () => { play.textContent = media.paused ? "Play" : "Pause"; };
      play.addEventListener("click", async () => {
        if (media.paused) await media.play();
        else media.pause();
        sync();
      });
      media.addEventListener("play", sync);
      media.addEventListener("pause", sync);
      wrap.append(play, media);
      preview.append(wrap);
    }
    else if (["pdf", "html", "markdown", "report"].includes(item.kind)) {
      const frame = document.createElement("iframe");
      frame.title = item.title;
      // A PDF needs scripts: Firefox's PDF viewer draws no pages in a frame that forbids them
      // Without allow-same-origin the document still cannot reach Previewer.
      frame.sandbox = item.kind === "html" ? "allow-scripts allow-forms" : item.kind === "pdf" ? "allow-scripts" : "";
      if (item.kind === "markdown" || item.kind === "report") {
        // Theme the display copy, including older posts, without changing stored evidence.
        fetch(evidenceUrl(item, "first-page.html"))
          .then((response) => {
            if (!response.ok) throw new Error(`Document failed to load: ${response.status}`);
            return response.text();
          })
          .then((text) => {
            const document = new DOMParser().parseFromString(text.replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ""), "text/html");
            const theme = document.createElement("link");
            theme.rel = "stylesheet";
            theme.href = new URL("/static/post.css", location.origin).href;
            document.head.append(theme);
            frame.srcdoc = "<!doctype html>" + document.documentElement.outerHTML;
          })
          .catch((error) => frame.replaceWith(element("pre", "", error.message)));
      } else frame.src = url;
      preview.append(frame);
    } else if (["log", "subtitle", "movie_project"].includes(item.kind)) {
      const pre = document.createElement("pre"); pre.textContent = "Loading…"; preview.append(pre);
      fetch(url).then((response) => response.text()).then((text) => pre.textContent = text).catch((error) => pre.textContent = error.message);
    } else preview.append(element("a", "button", "Open artifact"));
  }

  function openLive(item) {
    if (!state.liveOpen.some((open) => open.id === item.id)) state.liveOpen.push(item);
    state.liveActive = item.id;
    const tabs = $("#live-tabs"); tabs.replaceChildren();
    state.liveOpen.forEach((open) => {
      const button = element("button", `live-tab${open.id === state.liveActive ? " active" : ""}`, open.title);
      button.type = "button"; button.addEventListener("click", () => { state.liveActive = open.id; state.selected = open; selectItem(open); }); tabs.append(button);
    });
    const preview = $("#preview");
    state.liveOpen.forEach((open) => {
      let shell = preview.querySelector(`[data-live-id="${open.id}"]`);
      if (!shell) {
        shell = element("div", "live-frame-shell"); shell.dataset.liveId = open.id;
        const frame = document.createElement("iframe"); frame.title = open.title; frame.loading = "eager"; frame.sandbox = "allow-scripts allow-forms allow-pointer-lock allow-popups allow-same-origin"; frame.src = open.target_url; shell.append(frame);
        preview.append(shell);
      }
      shell.classList.toggle("active", open.id === state.liveActive);
    });
    $("#external-open").href = item.target_url;
    $("#live-address").value = item.target_url;
    setViewport($("#viewport").value);
  }

  // 3D models: the three.js viewer with view modes, turntable, grid, animation playback,
  // a file check, a print check and notes like Movies, which keep the camera view instead of a timestamp.
  async function mountModel(item, container) {
    const workspace = element("div", "model-workspace");
    const stage = element("section", "model-stage");
    stage.setAttribute("aria-label", `3D viewer for ${item.title}`);
    const canvas = document.createElement("canvas");
    canvas.className = "model-canvas";
    canvas.setAttribute("aria-label", `3D model ${item.title}. Drag to rotate, right-drag to pan, wheel to zoom.`);
    const toolbar = element("div", "model-toolbar");
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "3D view");
    const modeButtons = [["textured", "Textured"], ["clay", "Clay"], ["wireframe", "Wireframe"]].map(([mode, label]) => {
      const button = movieButton(label, `Show the model ${mode === "textured" ? "with its textures" : mode === "clay" ? "in plain clay" : "as a wireframe"}`, "model-mode");
      button.dataset.mode = mode;
      return button;
    });
    const turntable = movieButton("Turntable", "Spin the model", "model-toggle");
    const gridButton = movieButton("Grid", "Show the floor grid", "model-toggle");
    const reset = movieButton("Reset view", "Reset the camera");
    const zoomOut = movieButton("−", "Zoom out", "model-zoom");
    const zoomIn = movieButton("+", "Zoom in", "model-zoom");
    zoomOut.title = "Zoom out";
    zoomIn.title = "Zoom in";
    const note = movieButton("Note", "Add a note at this view", "primary model-note-button");
    note.title = "Add a note at this view (N)";
    const controls = [...modeButtons, turntable, gridButton, reset, zoomOut, zoomIn, note];
    controls.forEach((button) => { button.disabled = true; });
    toolbar.append(...controls);
    const stats = element("output", "model-stats");
    const status = element("p", "model-status", "Loading model…");
    status.setAttribute("aria-live", "polite");
    const animationBar = element("div", "model-animation");
    animationBar.hidden = true;
    stage.append(canvas, toolbar, stats, status, animationBar, element("span", "model-help", "Drag to rotate · right-drag to pan · wheel to zoom"));
    const fileSection = element("section", "inspector-section model-file-check");
    fileSection.append(element("h3", "", "File check"));
    const printSection = element("section", "inspector-section model-print-check");
    printSection.append(element("h3", "", "Print check"), element("p", "inspector-empty", "Waiting for the model…"));
    const notesPanel = buildNotesPanel({
      className: "model-inspector",
      label: (value, index) => `View ${index + 1}`,
      describe: (value, index) => `for view ${index + 1}`,
      empty: "No notes yet. Turn to a view and press Note (N).",
      sections: [fileSection, printSection],
    });
    workspace.append(stage, notesPanel.aside);
    container.append(workspace);

    let viewer;
    let notes = [];
    try {
      const [module, listed] = await Promise.all([import("/static/viewer3d.js"), api(`/api/items/${item.id}/notes`)]);
      notes = listed.notes;
      viewer = await module.mount(canvas, { url: contentUrl(item), format: fileExtension(item) });
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("warning");
      printSection.lastChild.textContent = "No print check: the model did not load.";
      return;
    }
    if (!canvas.isConnected) { viewer.dispose(); return; }
    // A closed viewer has freed its WebGL context; drop it here too so its mesh can be garbage collected.
    modelViewers.forEach((entry, id) => { if (!entry.canvas.isConnected) modelViewers.delete(id); });
    modelViewers.set(item.id, { viewer, canvas });
    status.hidden = true;
    controls.forEach((button) => { button.disabled = false; });
    const size = viewer.sizeMm;
    stats.textContent = `${viewer.report.triangles.toLocaleString()} triangles · ${formatMm(size.width)} × ${formatMm(size.depth)} × ${formatMm(size.height)} mm`;

    const sync = () => {
      modeButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === viewer.mode)));
      turntable.setAttribute("aria-pressed", String(viewer.turntable));
      gridButton.setAttribute("aria-pressed", String(viewer.grid));
    };
    modeButtons.forEach((button) => button.addEventListener("click", () => { viewer.setMode(button.dataset.mode); sync(); }));
    turntable.addEventListener("click", () => { viewer.setTurntable(!viewer.turntable); sync(); });
    gridButton.addEventListener("click", () => { viewer.setGrid(!viewer.grid); sync(); });
    reset.addEventListener("click", () => viewer.resetView());
    zoomOut.addEventListener("click", () => viewer.zoom(1.25));
    zoomIn.addEventListener("click", () => viewer.zoom(0.8));
    sync();

    const animation = viewer.animations.length ? buildModelAnimation(viewer, animationBar) : null;
    fileSection.append(buildModelFileCheck(item, viewer.report));

    const renderNotes = () => notesPanel.render(notes, {
      jump: (value) => {
        if (!value.view) return;
        viewer.setView(value.view, value.at_seconds);
        sync();
        animation?.sync();
      },
      edit: async (value, text) => {
        const saved = await api(`/api/items/${item.id}/notes/${value.id}/edit`, { method: "POST", body: JSON.stringify({ text }) });
        notes = notes.map((other) => other.id === saved.id ? saved : other);
        renderNotes();
        announce("Note updated");
      },
      remove: async (value) => {
        if (!window.confirm(`Delete the note for view ${notes.indexOf(value) + 1}?`)) return;
        try {
          await api(`/api/items/${item.id}/notes/${value.id}/delete`, { method: "POST", body: "{}" });
          notes = notes.filter((other) => other.id !== value.id);
          renderNotes();
          announce("Note deleted");
        } catch (error) { alert(`The note was not deleted. ${error.message}`); }
      },
    });
    note.addEventListener("click", () => {
      if (stage.querySelector(".note-layer")) return;
      viewer.freeze(true);
      animation?.sync();
      sync();
      noteLayer(stage, item, {
        stamp: `View ${notes.length + 1}`,
        body: { at_seconds: Math.round(viewer.time * 1000) / 1000, view: viewer.view() },
        ready: () => Promise.resolve(),
        capture: () => viewer.snapshot(),
        onSaved: (saved) => {
          notes = [...notes, saved].sort((left, right) => left.at_seconds - right.at_seconds || left.created_at.localeCompare(right.created_at));
          renderNotes();
          announce("Note saved");
        },
        onClose: () => { viewer.freeze(false); },
      });
    });
    renderNotes();

    try {
      printSection.lastChild.textContent = "Checking…";
      const result = await viewer.printCheck();
      printSection.lastChild.replaceWith(buildModelPrintCheck(viewer, result));
    } catch (error) {
      if (canvas.isConnected) printSection.lastChild.textContent = `The print check failed. ${error.message}`;
    }
  }

  function buildModelAnimation(viewer, bar) {
    const clip = document.createElement("select");
    clip.setAttribute("aria-label", "Animation");
    viewer.animations.forEach((value, index) => clip.append(new Option(`${value.name} (${formatSeconds(value.duration)})`, String(index))));
    const play = movieButton("Pause", "Play or pause the animation", "model-play");
    const scrub = document.createElement("input");
    scrub.type = "range";
    scrub.min = "0";
    scrub.step = "0.01";
    scrub.setAttribute("aria-label", "Animation time");
    const time = element("output", "model-time");
    const show = (seconds) => {
      const duration = viewer.animations[viewer.clipIndex]?.duration || 0;
      scrub.max = String(duration);
      scrub.value = String(seconds);
      time.textContent = `${formatSeconds(seconds)} / ${formatSeconds(duration)}`;
    };
    const sync = () => {
      clip.value = String(Math.max(viewer.clipIndex, 0));
      play.textContent = viewer.playing ? "Pause" : "Play";
      show(viewer.time);
    };
    clip.addEventListener("change", () => { viewer.useClip(Number(clip.value)); sync(); });
    play.addEventListener("click", () => { viewer.setPlaying(!viewer.playing); sync(); });
    scrub.addEventListener("input", () => { viewer.setPlaying(false); viewer.seek(Number(scrub.value)); sync(); });
    viewer.onTick(show);
    bar.append(clip, play, scrub, time);
    bar.hidden = false;
    sync();
    return { sync };
  }

  function buildModelFileCheck(item, report) {
    const rows = [
      ["File", item.source_name],
      ["Format", report.format],
      ["File size", formatBytes(report.bytes)],
      ["Triangles", report.triangles.toLocaleString()],
      ["Vertices", report.vertices.toLocaleString()],
      ["Meshes", String(report.meshes)],
      ["Materials", String(report.materials)],
      ["Animations", report.animations.length ? report.animations.map((value) => `${value.name} (${formatSeconds(value.duration)})`).join(", ") : "None"],
    ];
    const fragment = document.createDocumentFragment();
    const list = element("dl", "metadata-list");
    rows.forEach(([term, value]) => list.append(element("dt", "", term), element("dd", "", value)));
    fragment.append(list);
    const maps = element("div", "model-maps");
    maps.append(element("h4", "", "Texture maps"));
    if (!report.textures.length) maps.append(element("p", "inspector-empty", "None. The model has no texture images."));
    report.textures.forEach((texture) => {
      const figure = element("figure", "model-map");
      const thumb = document.createElement("canvas");
      const scale = Math.min(1, 72 / Math.max(texture.width || 1, texture.height || 1));
      thumb.width = Math.max(1, Math.round((texture.width || 1) * scale));
      thumb.height = Math.max(1, Math.round((texture.height || 1) * scale));
      try { thumb.getContext("2d").drawImage(texture.image, 0, 0, thumb.width, thumb.height); } catch { /* Raw data textures have no drawable image. */ }
      figure.append(thumb, element("figcaption", "", `${texture.labels.join(" + ")} · ${texture.width} × ${texture.height}`));
      maps.append(figure);
    });
    fragment.append(maps);
    return fragment;
  }

  function buildModelPrintCheck(viewer, result) {
    const size = viewer.sizeMm;
    const rows = [
      ["Size", `${formatMm(size.width)} × ${formatMm(size.depth)} × ${formatMm(size.height)} mm (width × depth × height)`, ""],
      ["Watertight", result.watertight ? "Yes" : `No: ${result.openEdges.toLocaleString()} open edges`, result.watertight ? "good" : "bad"],
      ["Broken edges", result.nonManifoldEdges ? `${result.nonManifoldEdges.toLocaleString()} edges shared by 3 or more faces` : "None", result.nonManifoldEdges ? "bad" : "good"],
      ["Separate pieces", result.pieces.toLocaleString(), result.pieces > 1 ? "warn" : ""],
      ["Volume", result.watertight ? formatVolume(result.volumeMm3) : "Needs a watertight mesh", ""],
      ["Grid squares", `${Number(viewer.gridMm.toPrecision(3))} mm`, ""],
    ];
    const fragment = document.createDocumentFragment();
    const list = element("dl", "metadata-list model-print-list");
    rows.forEach(([term, value, tone]) => list.append(element("dt", "", term), element("dd", tone ? `check-${tone}` : "", value)));
    fragment.append(list, element("p", "model-unit-note", viewer.unitNote));
    return fragment;
  }

  function setViewport(value) {
    const frame = $("#preview").querySelector(`.live-frame-shell.active iframe`);
    if (!frame) return;
    frame.style.width = value === "mobile" ? "390px" : value === "desktop" ? "1440px" : "100%";
  }

  async function renderMovieEditor(item, preview) {
    const token = ++state.movieRenderToken;
    try {
      const [record, notes] = await Promise.all([api(`/api/items/${item.id}/movie-edit`), api(`/api/items/${item.id}/notes`)]);
      let document = record.document;
      if (!document && item.kind === "movie_project") {
        const response = await fetch(contentUrl(item));
        if (!response.ok) throw new Error("Movie project manifest could not be opened");
        document = movieDocumentFromManifest(item, await response.json());
      }
      if (!document) document = defaultMovieDocument(item);
      if (token !== state.movieRenderToken || state.selected?.id !== item.id) return;
      state.movieEdit = { ...record, document };
      state.movieNotes = notes.notes;
      const linkedVideo = document.annotations.find((marker) => marker.type === "clip" && movieAsset(marker.asset_item_id)?.kind === "video");
      if (!state.movieMediaId) state.movieMediaId = item.kind === "video" ? item.id : linkedVideo?.asset_item_id || null;
      mountMovieEditor(item, preview);
    } catch (error) {
      if (token === state.movieRenderToken) preview.replaceChildren(element("p", "warning", error.message));
    }
  }

  function mountMovieEditor(item, preview) {
    if (!state.movieEdit?.document || state.selected?.id !== item.id) return;
    const edit = state.movieEdit.document;
    const activeMedia = movieAsset(state.movieMediaId);
    releaseMedia(preview);
    preview.replaceChildren();

    const editor = element("div", "movie-editor");
    const upper = element("div", "movie-upper");
    const viewerPanel = element("section", "movie-viewer-panel");
    viewerPanel.setAttribute("aria-label", "Movie preview monitor");
    const viewerHead = element("header", "movie-panel-head");
    const viewerTitle = element("div");
    viewerTitle.append(element("span", "eyebrow", "Program monitor"), element("h3", "", activeMedia?.title || item.title));
    viewerHead.append(viewerTitle, element("span", "movie-resolution", `${edit.resolution.width}×${edit.resolution.height} · ${formatRate(edit.frame_rate)} fps`));
    const viewer = element("div", "movie-viewer");
    let media = null;
    if (activeMedia?.kind === "video") {
      media = document.createElement("video");
      media.src = contentUrl(activeMedia);
      media.preload = "metadata";
      media.playsInline = true;
      media.setAttribute("aria-label", `Preview ${activeMedia.title}`);
      viewer.append(media);
    } else if (activeMedia?.kind === "audio") {
      const audioWrap = element("div", "movie-audio-source");
      const waveform = new Image();
      waveform.src = evidenceUrl(activeMedia, "waveform.png");
      waveform.alt = `Waveform for ${activeMedia.title}`;
      media = document.createElement("audio");
      media.src = contentUrl(activeMedia);
      media.preload = "metadata";
      media.setAttribute("aria-label", `Preview ${activeMedia.title}`);
      audioWrap.append(waveform, element("strong", "", activeMedia.title), element("span", "", "Agent music or audio clip"));
      viewer.append(audioWrap, media);
    } else {
      const placeholder = element("div", "movie-no-source");
      placeholder.append(element("span", "movie-no-source-mark", "▶"), element("strong", "", "No video to play"), element("p", "", "This project has no approved video yet."));
      viewer.append(placeholder);
    }
    const transport = element("div", "movie-transport");
    const back = movieButton("−5s", "Move back five seconds");
    const stepBack = movieButton("−1s", "Move back one second");
    const frameBack = movieButton("−1f", "Move back one frame");
    const play = movieButton("Play", "Play or pause preview", "movie-play");
    const frameForward = movieButton("+1f", "Move forward one frame");
    const stepForward = movieButton("+1s", "Move forward one second");
    const forward = movieButton("+5s", "Move forward five seconds");
    const note = movieButton("Note", "Add a note at this frame", "primary movie-note-button");
    note.title = "Add a note at this frame (N)";
    const time = element("output", "movie-time", `${formatTimecode(state.moviePlayhead || 0, edit.frame_rate)} / ${formatTimecode(edit.duration_seconds, edit.frame_rate)}`);
    transport.append(back, stepBack, frameBack, play, frameForward, stepForward, forward, note, time);
    viewerPanel.append(viewerHead, viewer, transport);

    const notesPanel = buildNotesPanel({
      label: (value) => formatTimecode(value.at_seconds, edit.frame_rate),
      describe: (value) => `at ${formatShortTime(value.at_seconds)}`,
      empty: "No notes yet. Pause on a frame and press Note (N).",
      sections: [metadataSection(activeMedia || item)],
    });
    const timeline = buildMovieTimeline(edit, (marker) => jumpToMarker(marker));
    upper.append(viewerPanel, notesPanel.aside);
    editor.append(upper, timeline.section);
    preview.append(editor);

    const playhead = editor.querySelector(".movie-playhead");
    const updatePlayhead = (seconds) => {
      state.moviePlayhead = clamp(seconds, 0, edit.duration_seconds);
      time.textContent = `${formatTimecode(state.moviePlayhead, edit.frame_rate)} / ${formatTimecode(edit.duration_seconds, edit.frame_rate)}`;
      if (playhead) {
        const ratio = state.moviePlayhead / edit.duration_seconds;
        playhead.style.left = `calc(82px + ${ratio * 100}% - ${ratio * 96}px)`;
      }
      editor.querySelectorAll(".timeline-lane").forEach((lane) => lane.setAttribute("aria-valuenow", String(state.moviePlayhead)));
    };
    const seek = (seconds) => {
      updatePlayhead(seconds);
      if (media) media.currentTime = clamp(seconds, 0, Number.isFinite(media.duration) ? media.duration : edit.duration_seconds);
    };
    const jumpToMarker = (marker) => {
      // A clip from another video swaps the monitor's source.
      if (marker.asset_item_id && marker.asset_item_id !== state.movieMediaId && movieAsset(marker.asset_item_id)?.kind === "video") {
        state.movieMediaId = marker.asset_item_id;
        state.moviePlayhead = marker.start_seconds;
        mountMovieEditor(item, $("#preview"));
        return;
      }
      seek(marker.start_seconds);
    };
    const renderNotes = () => {
      notesPanel.render(state.movieNotes, { jump: (value) => seek(value.at_seconds), remove: removeNote, edit: editNote });
      timeline.renderNotes(state.movieNotes, (value) => seek(value.at_seconds));
    };
    // Throws on failure so the edit box can show the error and stay open.
    const editNote = async (value, text) => {
      const saved = await api(`/api/items/${item.id}/notes/${value.id}/edit`, { method: "POST", body: JSON.stringify({ text }) });
      state.movieNotes = state.movieNotes.map((other) => other.id === saved.id ? saved : other);
      renderNotes();
      announce(`Note at ${formatShortTime(saved.at_seconds)} updated`);
    };
    const removeNote = async (value) => {
      if (!window.confirm(`Delete the note at ${formatShortTime(value.at_seconds)}?`)) return;
      try {
        await api(`/api/items/${item.id}/notes/${value.id}/delete`, { method: "POST", body: "{}" });
        state.movieNotes = state.movieNotes.filter((other) => other.id !== value.id);
        renderNotes();
        announce(`Note at ${formatShortTime(value.at_seconds)} deleted`);
      } catch (error) { alert(`The note was not deleted. ${error.message}`); }
    };
    back.addEventListener("click", () => seek((state.moviePlayhead || 0) - 5));
    forward.addEventListener("click", () => seek((state.moviePlayhead || 0) + 5));
    stepBack.addEventListener("click", () => seek((state.moviePlayhead || 0) - 1));
    stepForward.addEventListener("click", () => seek((state.moviePlayhead || 0) + 1));
    // Land in the middle of the next frame so float rounding at a frame edge cannot show the wrong one.
    const stepFrames = (count) => {
      if (media && !media.paused) media.pause();
      const rate = Number(edit.frame_rate) || 24;
      const frame = Math.floor((state.moviePlayhead || 0) * rate + 1e-6) + count;
      seek((Math.max(0, frame) + 0.5) / rate);
    };
    frameBack.addEventListener("click", () => stepFrames(-1));
    frameForward.addEventListener("click", () => stepFrames(1));
    play.addEventListener("click", async () => {
      if (!media) return;
      if (media.paused) await media.play(); else media.pause();
    });
    note.addEventListener("click", () => openNoteLayer(item, media, viewer, edit.frame_rate, (saved) => {
      state.movieNotes = [...state.movieNotes, saved].sort((left, right) => left.at_seconds - right.at_seconds);
      renderNotes();
    }));
    if (media) {
      // releaseMedia resets a removed video to 0; that must not move the playhead.
      media.addEventListener("timeupdate", () => { if (media.isConnected) updatePlayhead(media.currentTime); });
      media.addEventListener("play", () => { play.textContent = "Pause"; });
      media.addEventListener("pause", () => { play.textContent = "Play"; });
      media.addEventListener("ended", () => { play.textContent = "Play"; });
    }
    editor.querySelectorAll(".timeline-lane").forEach((lane) => {
      const move = (event) => {
        const bounds = lane.getBoundingClientRect();
        seek((event.clientX - bounds.left) / bounds.width * edit.duration_seconds);
      };
      lane.addEventListener("click", move);
      lane.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          seek((state.moviePlayhead || 0) + (event.key === "ArrowRight" ? 1 : -1) / edit.frame_rate);
        }
      });
    });
    renderNotes();
    updatePlayhead(state.moviePlayhead || 0);
  }

  // Voyager-style note: freeze the frame, mark it with a box or freehand,
  // type, and Enter saves the marked frame and the text at the playhead. Esc cancels.
  async function openNoteLayer(item, media, viewer, frameRate, onSaved) {
    if (viewer.querySelector(".note-layer")) return;
    if (media) media.pause();
    const seconds = state.moviePlayhead || 0;
    await noteLayer(viewer, item, {
      stamp: formatTimecode(seconds, frameRate),
      body: { at_seconds: seconds },
      ready: () => media?.tagName === "VIDEO" ? frameReady(media) : Promise.resolve(),
      capture: () => {
        if (media?.tagName !== "VIDEO" || !media.videoWidth || media.readyState < 2) return null;
        const scale = Math.min(1, 1920 / media.videoWidth);
        const frame = document.createElement("canvas");
        frame.width = Math.round(media.videoWidth * scale);
        frame.height = Math.round(media.videoHeight * scale);
        frame.getContext("2d").drawImage(media, 0, 0, frame.width, frame.height);
        return frame;
      },
      onSaved: (saved) => { onSaved(saved); announce(`Note saved at ${formatShortTime(seconds)}`); },
    });
  }

  // The note layer shared by Movies and 3D models: capture() gives the frozen frame to mark,
  // body is what the note saves besides its text and screenshot, and onClose runs on save or cancel.
  async function noteLayer(host, item, { stamp, body, ready, capture, onSaved, onClose = () => {} }) {
    const layer = element("div", "note-layer");
    host.append(layer);
    let canvas = null;
    let tool = "box";
    let toolButtons = [];
    const strokes = [];
    const text = document.createElement("input");
    const close = () => { layer.remove(); document.removeEventListener("keydown", onKey, true); onClose(); };
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); return; }
      if (!canvas || document.activeElement === text || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "r" || event.key === "R") toolButtons[0].click();
      else if (event.key === "d" || event.key === "D") toolButtons[1].click();
    };
    document.addEventListener("keydown", onKey, true);
    await ready();
    if (!layer.isConnected) return;
    const frame = capture();
    if (frame) {
      canvas = document.createElement("canvas");
      canvas.className = "note-canvas";
      canvas.width = frame.width;
      canvas.height = frame.height;
      canvas.setAttribute("aria-label", "Frame to mark: drag to draw");
      const context = canvas.getContext("2d");
      const redraw = () => {
        context.drawImage(frame, 0, 0);
        context.strokeStyle = "#ff3b30";
        context.lineWidth = Math.max(3, Math.round(canvas.width / 300));
        context.lineJoin = "round";
        context.lineCap = "round";
        strokes.forEach(({ kind, points }) => {
          if (kind === "box") {
            const [[x0, y0], [x1, y1]] = points;
            context.strokeRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
          } else {
            context.beginPath();
            points.forEach(([x, y], index) => { if (index) context.lineTo(x, y); else context.moveTo(x, y); });
            context.stroke();
          }
        });
      };
      // The frame keeps its aspect inside the canvas box (object-fit: contain), so map through the fitted area.
      const point = (event) => {
        const bounds = canvas.getBoundingClientRect();
        const fit = Math.min(bounds.width / canvas.width, bounds.height / canvas.height);
        const left = bounds.left + (bounds.width - canvas.width * fit) / 2;
        const top = bounds.top + (bounds.height - canvas.height * fit) / 2;
        return [clamp((event.clientX - left) / fit, 0, canvas.width), clamp((event.clientY - top) / fit, 0, canvas.height)];
      };
      let active = null;
      canvas.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        canvas.setPointerCapture(event.pointerId);
        active = { kind: tool, points: [point(event), point(event)] };
        strokes.push(active);
        redraw();
      });
      canvas.addEventListener("pointermove", (event) => {
        if (!active) return;
        if (active.kind === "box") active.points[1] = point(event); else active.points.push(point(event));
        redraw();
      });
      const finish = () => { active = null; text.focus(); };
      canvas.addEventListener("pointerup", finish);
      canvas.addEventListener("pointercancel", finish);
      redraw();
      layer.append(canvas);
    }
    const form = element("form", "note-form");
    const tools = element("div", "note-tools");
    toolButtons = [["box", "Box", "Draw a box (R)"], ["draw", "Draw", "Draw freehand (D)"]].map(([value, label, ariaLabel]) => {
      const button = movieButton(label, ariaLabel, "note-tool");
      button.setAttribute("aria-pressed", String(value === tool));
      button.addEventListener("click", () => {
        tool = value;
        toolButtons.forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
        text.focus();
      });
      return button;
    });
    if (canvas) tools.append(...toolButtons);
    text.type = "text";
    text.className = "note-text-input";
    text.maxLength = 2000;
    text.autocomplete = "off";
    text.placeholder = "What needs fixing? Enter saves, Esc cancels";
    text.setAttribute("aria-label", "Note");
    const save = movieButton("Save note", "Save this note", "primary");
    save.type = "submit";
    const cancel = movieButton("Cancel", "Cancel this note");
    const error = element("p", "form-error note-error");
    error.setAttribute("aria-live", "assertive");
    form.append(element("span", "note-stamp", stamp), tools, text, save, cancel, error);
    layer.append(form);
    cancel.addEventListener("click", close);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!canvas && !text.value.trim()) { error.textContent = "Type a note first."; text.focus(); return; }
      const payload = { ...body, text: text.value };
      if (canvas) payload.screenshot = canvas.toDataURL("image/jpeg", 0.85);
      save.disabled = true;
      try {
        const saved = await api(`/api/items/${item.id}/notes`, { method: "POST", body: JSON.stringify(payload) });
        close();
        onSaved(saved);
      } catch (failure) {
        error.textContent = failure.message;
        save.disabled = false;
      }
    });
    text.focus();
  }

  function frameReady(media) {
    if (media.readyState >= 2 && !media.seeking) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { clearTimeout(timer); media.removeEventListener("seeked", done); media.removeEventListener("loadeddata", done); resolve(); };
      const timer = setTimeout(done, 3000);
      media.addEventListener("seeked", done);
      media.addEventListener("loadeddata", done);
    });
  }

  function buildMovieTimeline(edit, jumpToMarker) {
    const section = element("section", "movie-timeline");
    section.setAttribute("aria-label", "Editorial timeline");
    const head = element("div", "movie-timeline-head");
    const title = element("div");
    title.append(element("span", "eyebrow", "Edit decision list"), element("h3", "", edit.title));
    head.append(title);
    const body = element("div", "timeline-body");
    const rulerLabel = element("span", "track-label", "Time");
    const ruler = element("div", "timeline-ruler");
    for (let index = 0; index <= 4; index += 1) {
      const tick = element("span", "", formatShortTime(edit.duration_seconds * index / 4));
      tick.style.left = `${index * 25}%`;
      ruler.append(tick);
    }
    body.append(rulerLabel, ruler);
    const trackNames = { clip: "Video", music: "Music", note: "Notes" };
    const lanes = {};
    Object.entries(trackNames).forEach(([type, label]) => {
      body.append(element("span", `track-label track-${type}`, label));
      const lane = element("div", `timeline-lane lane-${type}`);
      lane.tabIndex = 0;
      lane.setAttribute("role", "slider");
      lane.setAttribute("aria-label", `${label} timeline`);
      lane.setAttribute("aria-valuemin", "0");
      lane.setAttribute("aria-valuemax", String(edit.duration_seconds));
      lane.setAttribute("aria-valuenow", String(state.moviePlayhead || 0));
      // Neighbouring clips and cues cycle through four shades so each shot stands apart.
      edit.annotations.filter((marker) => marker.type === type).forEach((marker, index) => {
        const markerButton = element("button", `timeline-marker marker-${type}${type === "note" ? "" : ` shade-${index % 4}`}`, marker.label);
        markerButton.type = "button";
        markerButton.style.left = `${marker.start_seconds / edit.duration_seconds * 100}%`;
        const duration = Math.max(0, marker.end_seconds - marker.start_seconds);
        if (["clip", "music"].includes(type)) markerButton.style.width = `${Math.max(0.2, duration / edit.duration_seconds * 100)}%`;
        markerButton.title = `${formatShortTime(marker.start_seconds)} · ${marker.label}`;
        markerButton.addEventListener("click", (event) => {
          event.stopPropagation();
          jumpToMarker(marker);
        });
        lane.append(markerButton);
      });
      lanes[type] = lane;
      body.append(lane);
    });
    const playhead = element("span", "movie-playhead");
    playhead.setAttribute("aria-hidden", "true");
    body.append(playhead);
    section.append(head, body);
    const renderNotes = (notes, jump) => {
      lanes.note.querySelectorAll(".note-dot").forEach((dot) => dot.remove());
      notes.forEach((value) => {
        const dot = element("button", "note-dot");
        dot.type = "button";
        dot.style.left = `${clamp(value.at_seconds / edit.duration_seconds, 0, 1) * 100}%`;
        dot.title = `${formatShortTime(value.at_seconds)} · ${value.text || "Screenshot note"}`;
        dot.setAttribute("aria-label", `Note at ${formatShortTime(value.at_seconds)}`);
        dot.addEventListener("click", (event) => { event.stopPropagation(); jump(value); });
        lanes.note.append(dot);
      });
    };
    return { section, renderNotes };
  }

  // label names a note on its button; describe ends its accessible names, as in "Edit note at 0:05".
  function buildNotesPanel({ className = "", label, describe, empty, sections = [] }) {
    const aside = element("aside", `movie-inspector${className ? ` ${className}` : ""}`);
    aside.setAttribute("aria-label", "Notes and metadata");
    const section = element("section", "inspector-section notes-section");
    const heading = element("div", "inspector-heading");
    const count = element("span", "inspector-count", "0");
    heading.append(element("h3", "", "Notes"), count);
    const list = element("ol", "note-list");
    section.append(heading, list);
    aside.append(section, ...sections);
    const render = (notes, { jump, remove, edit }) => {
      count.textContent = String(notes.length);
      list.replaceChildren();
      if (!notes.length) {
        list.append(element("li", "inspector-empty", empty));
        return;
      }
      notes.forEach((value, index) => {
        const said = describe(value, index);
        const row = element("li", "note-row");
        const head = element("div", "note-row-head");
        const when = element("button", "note-time", label(value, index));
        when.type = "button";
        when.setAttribute("aria-label", `Jump to note ${said}`);
        when.addEventListener("click", () => jump(value));
        const editButton = movieButton("Edit", `Edit note ${said}`, "note-edit");
        // Edit changes the text only; the time and screenshot stay.
        editButton.addEventListener("click", () => {
          if (row.querySelector(".note-edit-form")) return;
          const body = row.querySelector(".note-body");
          const form = element("form", "note-edit-form");
          const input = document.createElement("textarea");
          input.className = "note-edit-input";
          input.rows = 3;
          input.maxLength = 2000;
          input.autocomplete = "off";
          input.value = value.text;
          input.setAttribute("aria-label", "Edit note");
          const save = movieButton("Save", "Save note text", "primary");
          save.type = "submit";
          const cancel = movieButton("Cancel", "Cancel editing this note");
          const error = element("p", "form-error note-error");
          error.setAttribute("aria-live", "assertive");
          const close = () => { form.remove(); if (body) body.hidden = false; editButton.focus(); };
          cancel.addEventListener("click", close);
          // Enter saves and Esc cancels, as when the note was made; Shift+Enter adds a line.
          input.addEventListener("keydown", (event) => {
            if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
            else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
          });
          form.addEventListener("submit", async (event) => {
            event.preventDefault();
            save.disabled = true;
            try { await edit(value, input.value); } catch (failure) { error.textContent = failure.message; save.disabled = false; }
          });
          if (body) body.hidden = true;
          form.append(input, save, cancel, error);
          row.append(form);
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        });
        const deleteButton = movieButton("Delete", `Delete note ${said}`, "danger note-delete");
        deleteButton.addEventListener("click", () => remove(value));
        head.append(when, editButton, deleteButton);
        row.append(head);
        if (value.screenshot_url) {
          const shot = element("button", "note-shot");
          shot.type = "button";
          shot.setAttribute("aria-label", `Screenshot ${said}`);
          const image = new Image();
          image.src = value.screenshot_url;
          image.alt = "";
          image.loading = "lazy";
          shot.append(image);
          shot.addEventListener("click", () => jump(value));
          row.append(shot);
        }
        if (value.text) row.append(element("p", "note-body", value.text));
        list.append(row);
      });
    };
    return { aside, render };
  }

  function metadataSection(item) {
    const section = element("section", "inspector-section metadata-section");
    section.append(element("h3", "", "Media metadata"), buildMetadataList(item));
    return section;
  }

  function buildMetadataList(item) {
    const metadata = item.receipt?.metadata || {};
    const video = mediaStreamMetadata(metadata, "video");
    const audio = mediaStreamMetadata(metadata, "audio");
    const rows = [
      ["File", item.source_name],
      ["Run", item.project_run_id],
      ["Type", item.mime_type || item.kind],
      ["Size", formatBytes(item.size_bytes)],
      ["Duration", metadata.duration_seconds ? formatShortTime(metadata.duration_seconds) : null],
      ["Format", metadata.format_name],
      ["Video", video.codec],
      ["Resolution", video.width && video.height ? `${video.width} × ${video.height}` : metadata.width && metadata.height ? `${metadata.width} × ${metadata.height}` : null],
      ["Frame rate", video.frame_rate || metadata.frame_rate ? `${formatRate(video.frame_rate || metadata.frame_rate)} fps` : null],
      ["Pixel format", video.pixel_format],
      ["Color", video.color_space || metadata.color_space],
      ["Audio", audio.codec],
      ["Sample rate", audio.sample_rate ? `${audio.sample_rate} Hz` : null],
      ["Channels", audio.channel_layout || audio.channels],
      ["Bit rate", metadata.bit_rate ? `${Math.round(metadata.bit_rate / 1000)} kb/s` : null],
      ["SHA-256", item.sha256],
    ].filter((row) => row[1] !== null && row[1] !== undefined && row[1] !== "");
    const list = element("dl", "metadata-list");
    rows.forEach(([name, value]) => { list.append(element("dt", "", name), element("dd", "", String(value))); });
    return list;
  }

  function defaultMovieDocument(item) {
    const metadata = item.receipt?.metadata || {};
    const video = mediaStreamMetadata(metadata, "video");
    return {
      version: 1,
      title: item.title,
      duration_seconds: Number(metadata.duration_seconds || 60),
      frame_rate: Number(video.frame_rate || metadata.frame_rate || 24),
      resolution: { width: Number(video.width || metadata.width || 1920), height: Number(video.height || metadata.height || 1080) },
      color_space: video.color_space || metadata.color_space || "Unknown",
      annotations: [],
    };
  }

  function movieDocumentFromManifest(item, manifest) {
    const timeline = manifest.timeline || {};
    const duration = Number(timeline.duration_seconds || 60);
    const annotations = [];
    (manifest.clips || []).forEach((clip, index) => annotations.push({
      id: `manifest-clip-${index}`,
      type: "clip",
      start_seconds: clamp(Number(clip.start_seconds || 0), 0, duration),
      end_seconds: clamp(Number(clip.end_seconds || duration), 0, duration),
      label: String(clip.label || `Clip ${index + 1}`).slice(0, 160),
      notes: String(clip.notes || "").slice(0, 2000),
      asset_item_id: clip.item_id,
    }));
    (manifest.markers || []).forEach((marker, index) => {
      const type = ["cut", "music", "clip", "note"].includes(marker.type) ? marker.type : "note";
      const start = clamp(Number(marker.start_seconds || 0), 0, duration);
      const end = ["music", "clip"].includes(type) ? clamp(Number(marker.end_seconds || Math.min(duration, start + 5)), start, duration) : start;
      const value = {
        id: `manifest-marker-${index}`,
        type,
        start_seconds: start,
        end_seconds: end,
        label: String(marker.label || `${type} marker`).slice(0, 160),
        notes: String(marker.notes || "").slice(0, 2000),
      };
      if (marker.asset_item_id) value.asset_item_id = marker.asset_item_id;
      annotations.push(value);
    });
    return {
      version: 1,
      title: String(manifest.title || item.title).slice(0, 200),
      duration_seconds: duration,
      frame_rate: Number(timeline.frame_rate || 24),
      resolution: { width: Number(timeline.width || 1920), height: Number(timeline.height || 1080) },
      color_space: String(timeline.color_space || "Unknown").slice(0, 80),
      annotations: annotations.sort((left, right) => left.start_seconds - right.start_seconds),
    };
  }

  function movieAsset(itemId) { return state.items.find((item) => item.id === itemId && item.state === "approved"); }
  function mediaStreamMetadata(metadata, kind) {
    if (metadata[kind]) return metadata[kind];
    const stream = Array.isArray(metadata.streams) ? metadata.streams.find((value) => value.codec_type === kind) || {} : {};
    if (!Object.keys(stream).length) return {};
    if (kind === "video") return {
      codec: stream.codec_long_name || stream.codec_name,
      width: stream.width,
      height: stream.height,
      frame_rate: parseClientRate(stream.avg_frame_rate || stream.r_frame_rate),
      pixel_format: stream.pix_fmt,
      color_space: stream.color_space || stream.color_primaries,
    };
    return {
      codec: stream.codec_long_name || stream.codec_name,
      sample_rate: Number(stream.sample_rate) || null,
      channels: stream.channels,
      channel_layout: stream.channel_layout,
    };
  }
  function parseClientRate(value) { if (!value) return null; const [left, right = "1"] = String(value).split("/"); const rate = Number(left) / Number(right); return Number.isFinite(rate) && rate > 0 ? rate : null; }
  function movieButton(label, ariaLabel, className = "") { const button = element("button", className, label); button.type = "button"; button.setAttribute("aria-label", ariaLabel); return button; }
  function clamp(value, minimum, maximum) { return Math.min(maximum, Math.max(minimum, Number.isFinite(value) ? value : minimum)); }
  function formatRate(value) { return Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 }); }
  function formatShortTime(seconds) { const value = Math.max(0, Number(seconds) || 0); const minutes = Math.floor(value / 60); return `${minutes}:${String(Math.floor(value % 60)).padStart(2, "0")}`; }
  function formatTimecode(seconds, frameRate) { const rate = Math.max(1, Number(frameRate) || 24); const total = Math.max(0, Number(seconds) || 0); const hours = Math.floor(total / 3600); const minutes = Math.floor(total % 3600 / 60); const secs = Math.floor(total % 60); const frames = Math.floor(total % 1 * rate); return [hours, minutes, secs, frames].map((value) => String(value).padStart(2, "0")).join(":"); }

  async function runAction(action, item, payload = {}) {
    try {
      const updated = await humanAction({ action, item_id: item.id, ...payload });
      state.selected = updated; await load();
      const stillVisible = itemsForTab().find((value) => value.id === updated.id); if (stillVisible) selectItem(stillVisible); else { releaseMedia($("#preview")); $("#detail").hidden = true; $("#empty").hidden = false; state.selected = null; }
      announce(`${item.title}: ${action} complete`);
    } catch (error) { if (!error.cancelled) alert(`${item.title}: ${action} failed. ${error.message}`); }
  }

  async function deleteOldCandidates() {
    const items = oldCandidates();
    if (!items.length) { announce("No candidates are older than four days"); return; }
    await deleteItems(items, "candidates older than four days", $("#bulk-delete-old"));
  }

  async function deleteSelected() {
    const items = checkedItems();
    if (items.length) await deleteItems(items, items.length === 1 ? "selected item" : "selected items", $("#delete-selected"));
  }

  async function deleteItems(items, description, button) {
    const bytes = items.reduce((total, item) => total + item.size_bytes, 0);
    if (!confirm(`Delete ${items.length} ${description} (${formatBytes(bytes)})?`)) return;
    button.disabled = true;
    const failures = [];
    let deleted = 0;
    for (const item of items) {
      button.textContent = `Deleting ${deleted + failures.length + 1}/${items.length}…`;
      try {
        await humanAction({ action: "delete", item_id: item.id });
        state.checked.delete(item.id);
        deleted += 1;
      } catch (error) {
        if (error.cancelled) break;
        failures.push(`${item.title}: ${error.message}`);
      }
    }
    button.disabled = false;
    state.selected = null;
    releaseMedia($("#preview"));
    $("#detail").hidden = true;
    $("#empty").hidden = false;
    await load();
    if (failures.length) alert(`${deleted} deleted, ${failures.length} failed. First failure: ${failures[0]}`);
    else announce(`Deleted ${deleted} ${description}`);
  }

  function openPinSettings() {
    ["#current-pin", "#new-pin", "#confirm-new-pin"].forEach((selector) => {
      const input = $(selector);
      input.value = "";
      input.removeAttribute("aria-invalid");
    });
    $("#pin-settings-error").textContent = "";
    const dialog = $("#pin-settings-dialog");
    dialog.showModal();
    dialog.scrollTop = 0;
    $("#current-pin").focus({ preventScroll: true });
  }

  async function changePin(event) {
    event.preventDefault();
    const currentPin = $("#current-pin");
    const newPin = $("#new-pin");
    const confirmation = $("#confirm-new-pin");
    const errorMessage = $("#pin-settings-error");
    [currentPin, newPin, confirmation].forEach((input) => input.removeAttribute("aria-invalid"));
    errorMessage.textContent = "";
    if (newPin.value !== confirmation.value) {
      newPin.setAttribute("aria-invalid", "true");
      confirmation.setAttribute("aria-invalid", "true");
      errorMessage.textContent = "New PINs do not match.";
      confirmation.focus();
      return;
    }
    try {
      await api("/api/human/pin", {
        method: "POST",
        headers: { "X-Previewer-PIN": currentPin.value },
        body: JSON.stringify({ new_pin: newPin.value }),
      });
      $("#pin-settings-dialog").close();
      announce("Human PIN changed");
    } catch (error) {
      currentPin.setAttribute("aria-invalid", "true");
      errorMessage.textContent = error.message;
      $("#pin-settings-dialog").scrollTop = 0;
      currentPin.focus({ preventScroll: true });
    }
  }

  function switchTab(tab) {
    state.tab = tab; state.selected = null;
    document.querySelectorAll(".tab").forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
    const titles = { artifacts: ["Approved", "Artifacts"], live: ["Interactive", "Live"], movies: ["Editorial", "Movies"], models: ["Meshes", "3D Models"], review: ["Human controlled", "Review Inbox"] };
    $("#section-eyebrow").textContent = titles[tab][0]; $("#section-title").textContent = titles[tab][1];
    $("#sort-control").hidden = !listToolTabs.has(tab);
    releaseMedia($("#preview"));
    $("#detail").hidden = true; $("#empty").hidden = false; renderList(); renderBulkDelete();
  }

  // A removed or hidden <video> keeps downloading until the browser frees it. On a TV browser that
  // starved the next video until a page refresh, so stop it and drop its source.
  function releaseMedia(container) {
    container.querySelectorAll("video, audio").forEach((media) => { media.pause(); media.removeAttribute("src"); media.load(); });
  }
  function fileExtension(item) { return (item.source_name || "").split(".").pop().toLowerCase(); }
  function formatMm(value) { return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2); }
  function formatSeconds(value) { return `${(Number(value) || 0).toFixed(2)} s`; }
  function formatVolume(mm3) { const cm3 = mm3 / 1000; return `${cm3 >= 100 ? cm3.toFixed(0) : cm3 >= 1 ? cm3.toFixed(1) : cm3.toPrecision(2)} cm³`; }
  function contentUrl(item, relative = "") { return `/api/items/${item.id}/content${relative ? `/${relative}` : ""}`; }
  function evidenceUrl(item, name) { return `/api/items/${item.id}/evidence/${encodeURIComponent(name)}`; }
  function element(tag, className = "", text = "") { const node = document.createElement(tag); if (className) node.className = className; if (text) node.textContent = text; return node; }
  function formatBytes(bytes) { if (!bytes) return "0 B"; const units = ["B", "KiB", "MiB", "GiB", "TiB"]; const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1); return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`; }
  function announce(message) { $("#announcer").textContent = message; }

  function setExpanded(panel, expanded) {
    panel.classList.toggle("expanded", expanded);
    // An expanded panel also takes the full width beside the list, not just the height.
    $("#detail").classList.toggle("wide", Boolean($("#detail").querySelector(".expandable.expanded")));
    const button = panel.querySelector(".expand-toggle");
    button.textContent = expanded ? "Minimise" : "Expand";
    button.setAttribute("aria-expanded", String(expanded));
  }

  document.querySelectorAll(".expand-toggle").forEach((button) => button.addEventListener("click", () => {
    const panel = button.closest(".expandable");
    setExpanded(panel, !panel.classList.contains("expanded"));
  }));
  const libraryWidthKey = "previewer.libraryWidth";
  const resizer = $("#library-resizer");
  function setLibraryWidth(width) {
    const clamped = Math.round(Math.min(Math.max(width, 240), window.innerWidth * 0.7));
    $("#workspace").style.setProperty("--library-width", `${clamped}px`);
    resizer.setAttribute("aria-valuenow", String(clamped));
    try { localStorage.setItem(libraryWidthKey, String(clamped)); } catch { /* width is a convenience */ }
  }
  try { const saved = Number(localStorage.getItem(libraryWidthKey)); if (saved) setLibraryWidth(saved); } catch { /* use default width */ }
  resizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    resizer.classList.add("dragging");
  });
  resizer.addEventListener("pointermove", (event) => {
    if (resizer.hasPointerCapture(event.pointerId)) setLibraryWidth(event.clientX - $("#workspace").getBoundingClientRect().left);
  });
  resizer.addEventListener("lostpointercapture", () => resizer.classList.remove("dragging"));
  resizer.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: -24, ArrowRight: 24 }[event.key];
    if (!step) return;
    event.preventDefault();
    setLibraryWidth($(".library").getBoundingClientRect().width + step);
  });
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $("#filter").addEventListener("input", (event) => { state.filter = event.target.value; renderList(); });
  const reviewSortKey = "previewer.reviewSort";
  try { const saved = localStorage.getItem(reviewSortKey); if (reviewSorts[saved]) { state.sort = saved; $("#sort").value = saved; } } catch { /* use default sort */ }
  $("#sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    try { localStorage.setItem(reviewSortKey, state.sort); } catch { /* sort is a convenience */ }
    renderList();
  });
  $("#refresh").addEventListener("click", load);
  $("#bulk-delete-old").addEventListener("click", deleteOldCandidates);
  $("#delete-selected").addEventListener("click", deleteSelected);
  // For TV Bro's pointer, which cannot scroll the list upwards on its own.
  function scrollList(direction) { const list = $("#item-list"); list.scrollBy({ top: direction * list.clientHeight * 0.8 }); }
  $("#scroll-up").addEventListener("click", () => scrollList(-1));
  $("#scroll-down").addEventListener("click", () => scrollList(1));
  $("#pin-settings").addEventListener("click", openPinSettings);
  $("#pin-settings-form").addEventListener("submit", changePin);
  $("#cancel-pin-settings").addEventListener("click", () => $("#pin-settings-dialog").close());
  $("#cancel-action-pin").addEventListener("click", () => $("#pin-dialog").close());

  // Arrow keys and the TV remote's D-pad move focus to the nearest control in that direction.
  const directions = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
  const focusableSelector = "button, a[href], input, select, video[controls], [tabindex]:not([tabindex='-1'])";
  function focusables(scope) {
    return [...scope.querySelectorAll(focusableSelector)].filter((node) => !node.disabled && !node.matches(".library-resizer") && !node.closest("[hidden]") && node.getClientRects().length && node.getBoundingClientRect().width > 0);
  }
  function nearest(current, [dx, dy], candidates) {
    const from = current.getBoundingClientRect();
    let best = null;
    let bestScore = Infinity;
    for (const node of candidates) {
      if (node === current || node.contains(current)) continue;
      const rect = node.getBoundingClientRect();
      const along = dx > 0 ? rect.left - from.right : dx < 0 ? from.left - rect.right : dy > 0 ? rect.top - from.bottom : from.top - rect.bottom;
      if (along < -1) continue;
      const across = dx ? Math.max(0, rect.top - from.bottom, from.top - rect.bottom) : Math.max(0, rect.left - from.right, from.left - rect.right);
      // Anything lined up with the current control beats anything off to the side.
      const score = (across > 0 ? 1e6 : 0) + along + across * 3;
      if (score < bestScore) { best = node; bestScore = score; }
    }
    return best;
  }
  document.addEventListener("keydown", (event) => {
    const direction = directions[event.key];
    if (!direction || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const current = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    const textEntry = current?.matches("input:not([type=checkbox]), textarea, video");
    if (textEntry && direction[0]) return;
    const dialog = document.querySelector("dialog[open]");
    if (!current) {
      const first = focusables(dialog || $(".library"))[0];
      if (first) { event.preventDefault(); first.focus(); }
      return;
    }
    const region = current.closest("dialog[open], .library, .stage, .topbar, .tabs");
    let target = nearest(current, direction, focusables(region || document));
    if (!target && !dialog) {
      if (event.key === "ArrowRight" && region?.matches(".library") && !$("#detail").hidden) target = focusables($("#detail"))[0];
      else if (event.key === "ArrowLeft" && region?.matches(".stage")) target = $("#item-list .item-card.active") || focusables($("#item-list"))[0];
      else target = nearest(current, direction, focusables(document));
    }
    if (!target) return;
    event.preventDefault();
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  $("#viewport").addEventListener("change", (event) => setViewport(event.target.value));
  $("#navigate-frame").addEventListener("click", () => {
    const frame = $("#preview").querySelector(".live-frame-shell.active iframe");
    if (!frame) return;
    try { frame.src = new URL($("#live-address").value).href; $("#external-open").href = frame.src; }
    catch { announce("Enter a valid URL"); }
  });
  $("#reload-frame").addEventListener("click", () => { const frame = $("#preview").querySelector(".live-frame-shell.active iframe"); if (frame) frame.src = frame.src; });
  // N opens a note on the movie monitor, as in Voyager, or on the 3D view showing now.
  document.addEventListener("keydown", (event) => {
    if ((event.key !== "n" && event.key !== "N") || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    if (document.activeElement?.matches("input, textarea, select") || document.querySelector("dialog[open]")) return;
    const button = [...document.querySelectorAll(".movie-note-button, .model-note-button")].find((node) => node.offsetParent && !node.disabled);
    if (!button) return;
    event.preventDefault();
    button.click();
  });
  load();
})();
