(() => {
  "use strict";

  const base = window.location.pathname.endsWith("/") ? window.location.pathname : `${window.location.pathname}/`;
  const pathParts = base.split("/").filter(Boolean);
  const token = pathParts[pathParts.length - 1] || "";
  const el = (id) => document.getElementById(id);
  const transcript = el("transcript");
  let cursor = 0;
  let busy = false;
  let pendingApproval = null;
  let historyOffset = 0;
  let historyHasOlder = false;
  let toastTimer = 0;
  let pollTimer = 0;
  let stopping = false;

  function schedulePoll(delay = 750) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, delay);
  }

  async function api(path, options = {}) {
    const response = await fetch(`${base}api/${path}`, {
      cache: "no-store",
      ...options,
      headers: {
        "X-Pyto-Harness-Token": token,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function toast(message) {
    const box = el("toast");
    box.textContent = message;
    box.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => box.classList.remove("show"), 3400);
  }

  function addBubble(label, text, kind = "assistant") {
    const bubble = document.createElement("article");
    bubble.className = `bubble ${kind}`;
    const title = document.createElement("div");
    title.className = "bubble-label";
    title.textContent = label;
    const body = document.createElement("div");
    body.className = "preserve";
    body.textContent = text;
    bubble.append(title, body);
    transcript.appendChild(bubble);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function setBusy(value) {
    busy = Boolean(value);
    el("send").disabled = busy || stopping;
    el("prompt").disabled = busy || stopping;
    el("stop-turn").disabled = !busy || stopping;
    document.querySelectorAll(".run-program").forEach((button) => { button.disabled = busy || stopping; });
  }

  function showApproval(approval) {
    pendingApproval = approval || null;
    const box = el("approval");
    if (!pendingApproval) {
      box.classList.add("hidden");
      el("allow").disabled = false;
      el("deny").disabled = false;
      return;
    }
    el("approval-description").textContent = `${pendingApproval.tool || "Tool action"}\n${pendingApproval.reason || ""}\n\n${pendingApproval.description || ""}`;
    box.classList.remove("hidden");
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  async function answerApproval(allow) {
    if (!pendingApproval) return;
    const answer = pendingApproval;
    el("allow").disabled = true;
    el("deny").disabled = true;
    try {
      await api("approval", { method: "POST", body: JSON.stringify({ id: answer.id, allow }) });
      showApproval(null);
    } catch (error) {
      toast(error.message);
      el("allow").disabled = false;
      el("deny").disabled = false;
    }
  }

  function eventReceived(event) {
    const data = event.data || {};
    switch (event.type) {
      case "user": addBubble("You", data.text || "", "user"); break;
      case "output": if (data.text) addBubble("Harness", data.text); break;
      case "approval": showApproval(data); break;
      case "approval_answered": showApproval(null); addBubble("Approval", data.allow ? "Allowed" : "Denied", "system"); break;
      case "program_started": addBubble("Saved program", `Running ${data.title || data.id}…`, "system"); break;
      case "program_result": addBubble(data.title || "Program result", data.content || "(no output)", data.is_error ? "error" : "program"); break;
      case "turn_finished":
        if (data.errors) addBubble("Turn ended with errors", data.errors, "error");
        else addBubble("Turn complete", `Stop reason: ${data.stop || "unknown"}`, "system");
        break;
      case "error": addBubble("Error", data.text || "The operation failed.", "error"); break;
      case "status": if (data.text) addBubble("Status", data.text, "system"); break;
      case "server_stopping": stopping = true; el("connection").textContent = "Stopping…"; setBusy(busy); break;
      default: break;
    }
  }

  async function poll() {
    try {
      const state = await api(`state?since=${cursor}`);
      el("connection").textContent = state.stopping ? "Stopping…" : "Connected";
      if (state.reset) {
        cursor = state.next_id;
        await loadHistory();
      } else {
        for (const event of state.events || []) {
          eventReceived(event);
          cursor = Math.max(cursor, event.id || 0);
        }
      }
      if (state.pending_approval) showApproval(state.pending_approval);
      setBusy(state.busy);
      if (!window.__pytoPromptLoaded) {
        if (state.initial_prompt && !el("prompt").value) el("prompt").value = state.initial_prompt;
        window.__pytoPromptLoaded = true;
      }
    } catch (error) {
      el("connection").textContent = stopping ? "Stopped" : "Reconnecting…";
    } finally {
      if (!stopping) schedulePoll();
    }
  }

  function activateView(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.view === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".view").forEach((view) => {
      const active = view.id === `view-${name}`;
      view.classList.toggle("active", active);
      view.hidden = !active;
    });
    if (name === "programs") loadPrograms();
    if (name === "history") loadHistory();
  }

  function fieldControl(field) {
    let control;
    if (field.type === "choice") {
      control = document.createElement("select");
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = field.required ? "Choose…" : "Skip this input";
      control.append(empty);
      for (const choice of field.choices || []) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        control.append(option);
      }
      if (field.default) control.value = field.default;
    } else {
      control = document.createElement("input");
      control.type = field.type === "number" ? "number" : "text";
      control.autocomplete = "off";
      if (field.type === "number") {
        if (field.integer) control.step = "1";
        if (field.minimum !== undefined) control.min = field.minimum;
        if (field.maximum !== undefined) control.max = field.maximum;
      } else if (field.type === "text") {
        control.maxLength = field.max_length;
      } else {
        const exts = (field.extensions || []).join(", ");
        control.placeholder = field.type === "file" ? `Accessible file path${exts ? ` (${exts})` : ""}` : "Accessible folder path";
      }
    }
    control.name = field.name;
    control.required = Boolean(field.required);
    return control;
  }

  async function runProgram(id, form) {
    const values = {};
    for (const field of form.querySelectorAll("[name]")) {
      const schema = field._schema;
      const raw = field.value.trim();
      if (raw === "" && schema && !schema.required && !("default" in schema)) continue;
      if (raw !== "") values[field.name] = schema && schema.type === "number" ? raw : field.value;
    }
    try {
      await api("program/run", { method: "POST", body: JSON.stringify({ id, values }) });
      addBubble("Saved program", "Run started.", "system");
      activateView("chat");
    } catch (error) { toast(error.message); }
  }

  async function loadPrograms() {
    const list = el("program-list");
    list.replaceChildren();
    try {
      const response = await api("programs");
      if (!response.programs.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "No saved programs yet. Ask the harness to create and save one.";
        list.append(empty);
        return;
      }
      for (const program of response.programs) {
        const card = document.createElement("article");
        card.className = "program-card";
        const title = document.createElement("h3"); title.textContent = program.title;
        const meta = document.createElement("p"); meta.className = "program-meta"; meta.textContent = `${program.mode} · ${program.entry_file}${program.entry_exists ? "" : " · missing file"}`;
        const purpose = document.createElement("p"); purpose.className = "program-purpose"; purpose.textContent = program.purpose || "No description provided.";
        card.append(title, meta, purpose);
        const form = document.createElement("form"); form.className = "program-form";
        const fields = document.createElement("div"); fields.className = "program-fields";
        for (const schema of program.input_schema || []) {
          const label = document.createElement("label"); label.className = "field";
          const caption = document.createElement("span"); caption.textContent = `${schema.label}${schema.required ? " · required" : " · optional"}`;
          const control = fieldControl(schema); control._schema = schema;
          label.append(caption, control);
          if (schema.type === "file" || schema.type === "folder") {
            const note = document.createElement("small"); note.textContent = "Type a path available to Pyto; browser file selection does not grant Pyto access."; label.append(note);
          }
          fields.append(label);
        }
        const actions = document.createElement("div"); actions.className = "program-actions";
        const button = document.createElement("button"); button.className = "button primary run-program"; button.type = "submit"; button.textContent = "Run program"; button.disabled = !program.entry_exists;
        actions.append(button); form.append(fields, actions);
        form.addEventListener("submit", (event) => { event.preventDefault(); runProgram(program.id, form); });
        card.append(form); list.append(card);
      }
    } catch (error) {
      const problem = document.createElement("p"); problem.className = "bubble error"; problem.textContent = error.message; list.append(problem);
    }
  }

  async function loadHistory() {
    try {
      const response = await api(`history?offset=${historyOffset}`);
      el("history-text").textContent = response.text;
      historyHasOlder = response.has_older;
      el("older-history").hidden = !historyHasOlder;
      el("older-history").textContent = historyOffset ? "Load older messages" : "Older messages";
    } catch (error) { el("history-text").textContent = error.message; }
  }

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateView(tab.dataset.view)));
  el("chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    window.__pytoPromptLoaded = true;
    const input = el("prompt");
    const prompt = input.value.trim();
    if (!prompt || busy) return;
    try {
      await api("chat", { method: "POST", body: JSON.stringify({ prompt }) });
      input.value = "";
      setBusy(true);
    } catch (error) { toast(error.message); }
  });
  el("prompt").addEventListener("input", () => { window.__pytoPromptLoaded = true; });
  el("stop-turn").addEventListener("click", async () => {
    try { await api("stop-turn", { method: "POST", body: "{}" }); }
    catch (error) { toast(error.message); }
  });
  el("stop-session").addEventListener("click", async () => {
    if (stopping) return;
    stopping = true;
    setBusy(busy);
    el("connection").textContent = "Stopping…";
    try { await api("stop", { method: "POST", body: "{}" }); }
    catch (error) { toast(error.message); stopping = false; setBusy(busy); schedulePoll(250); }
  });
  el("allow").addEventListener("click", () => answerApproval(true));
  el("deny").addEventListener("click", () => answerApproval(false));
  el("refresh-programs").addEventListener("click", loadPrograms);
  el("older-history").addEventListener("click", () => { historyOffset += 12; loadHistory(); });

  loadHistory();
  poll();
})();
