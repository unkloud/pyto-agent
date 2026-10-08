(() => {
  "use strict";

  const base = window.location.pathname.endsWith("/") ? window.location.pathname : `${window.location.pathname}/`;
  const pathParts = base.split("/").filter(Boolean);
  const token = pathParts[pathParts.length - 1] || "";
  const el = (id) => document.getElementById(id);
  const transcript = el("transcript");
  let cursor = 0;
  let sessionGeneration = 0;
  let currentSessionId = "";
  let busy = false;
  let sessionChanging = false;
  let pendingApproval = null;
  let historyOffset = 0;
  let historyHasOlder = false;
  let toastTimer = 0;
  let pollTimer = 0;
  let stopping = false;
  let sessionReady = false;

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

  function safeMarkdownHref(value) {
    if (typeof value !== "string" || /[\\\u0000-\u0020\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function appendMarkdownInline(parent, nodes, budget) {
    if (!Array.isArray(nodes)) return false;
    for (const node of nodes) {
      budget.count += 1;
      if (budget.count > budget.limit || !Array.isArray(node) || typeof node[0] !== "string") return false;
      const type = node[0];
      if (type === "text" || type === "code") {
        if (typeof node[1] !== "string") return false;
        if (type === "text") {
          parent.append(document.createTextNode(node[1]));
        } else {
          const code = document.createElement("code");
          code.textContent = node[1];
          parent.append(code);
        }
        continue;
      }
      if (type === "strong" || type === "em") {
        const element = document.createElement(type);
        if (!appendMarkdownInline(element, node[1], budget)) return false;
        parent.append(element);
        continue;
      }
      if (type === "link") {
        const href = safeMarkdownHref(node[1]);
        if (!href) {
          if (!appendMarkdownInline(parent, node[2], budget)) return false;
          continue;
        }
        const link = document.createElement("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        if (!appendMarkdownInline(link, node[2], budget)) return false;
        parent.append(link);
        continue;
      }
      return false;
    }
    return true;
  }

  function appendMarkdownBlocks(parent, blocks, budget) {
    if (!Array.isArray(blocks)) return false;
    for (const block of blocks) {
      budget.count += 1;
      if (budget.count > budget.limit || !Array.isArray(block) || typeof block[0] !== "string") return false;
      const type = block[0];
      if (type === "paragraph") {
        const paragraph = document.createElement("p");
        if (!appendMarkdownInline(paragraph, block[1], budget)) return false;
        parent.append(paragraph);
      } else if (type === "heading") {
        const level = Number(block[1]);
        if (!Number.isInteger(level) || level < 1 || level > 6) return false;
        const heading = document.createElement(`h${level}`);
        if (!appendMarkdownInline(heading, block[2], budget)) return false;
        parent.append(heading);
      } else if (type === "quote") {
        const quote = document.createElement("blockquote");
        if (!appendMarkdownBlocks(quote, block[1], budget)) return false;
        parent.append(quote);
      } else if (type === "list") {
        if ((block[1] !== "ol" && block[1] !== "ul") || !Number.isInteger(block[2]) || !Array.isArray(block[3])) return false;
        const list = document.createElement(block[1]);
        if (block[1] === "ol") {
          if (block[2] < 0 || block[2] > 999999999) return false;
          list.start = block[2];
        }
        for (const item of block[3]) {
          budget.count += 1;
          if (budget.count > budget.limit) return false;
          const entry = document.createElement("li");
          if (!appendMarkdownBlocks(entry, item, budget)) return false;
          list.append(entry);
        }
        parent.append(list);
      } else if (type === "code_block") {
        if (typeof block[1] !== "string") return false;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = block[1];
        pre.append(code);
        parent.append(pre);
      } else {
        return false;
      }
    }
    return true;
  }

  function renderMarkdown(body, blocks) {
    const fragment = document.createDocumentFragment();
    if (!appendMarkdownBlocks(fragment, blocks, { count: 0, limit: 4096 })) return false;
    body.append(fragment);
    return true;
  }

  function addBubble(label, text, kind = "assistant", markdown = null) {
    const bubble = document.createElement("article");
    bubble.className = `bubble ${kind}`;
    const title = document.createElement("div");
    title.className = "bubble-label";
    title.textContent = label;
    const body = document.createElement("div");
    body.className = "preserve";
    if (Array.isArray(markdown)) {
      body.className = "markdown-content";
      if (!renderMarkdown(body, markdown)) {
        body.className = "preserve";
        body.textContent = text;
      }
    } else {
      body.textContent = text;
    }
    bubble.append(title, body);
    transcript.appendChild(bubble);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function setBusy(value) {
    busy = Boolean(value);
    const blocked = busy || sessionChanging || stopping;
    el("send").disabled = blocked;
    el("prompt").disabled = blocked;
    el("stop-turn").disabled = !busy || stopping;
    el("stop-turn").hidden = !busy || stopping;
    el("all-chats-link").setAttribute("aria-disabled", String(blocked));
    document.querySelectorAll(".run-program").forEach((button) => { button.disabled = blocked; });
    document.querySelectorAll("#session-start button").forEach((button) => { button.disabled = blocked; });
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
      case "output":
        if (data.text) {
          if (data.format === "markdown" && Array.isArray(data.markdown)) {
            addBubble("Harness", data.text, "assistant", data.markdown);
          } else {
            addBubble("Harness", data.text);
          }
        }
        break;
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
    const generation = sessionGeneration;
    try {
      const state = await api(`state?since=${cursor}`);
      if (generation !== sessionGeneration) return;
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
    document.body.classList.toggle("chat-view-active", name === "chat");
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
    if (!sessionReady) return;
    try {
      const response = await api(`history?offset=${historyOffset}`);
      el("history-text").textContent = response.text;
      historyHasOlder = response.has_older;
      el("older-history").hidden = !historyHasOlder;
      el("older-history").textContent = historyOffset ? "Load older messages" : "Older messages";
    } catch (error) { el("history-text").textContent = error.message; }
  }

  function sessionDate(value) {
    const date = new Date(Number(value) || 0);
    return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString();
  }

  function sessionMetadata(session) {
    const pieces = [sessionDate(session.updated_at)];
    if (session.model) pieces.push(session.model);
    if (session.workspace) pieces.push(session.workspace);
    pieces.push(`${session.message_count || 0} messages`);
    return pieces.join(" · ");
  }

  function showSelectedSession(response) {
    if (!response || !response.selected) return;
    const sessionId = String((response.session || {}).id || "");
    if (sessionId !== currentSessionId) {
      currentSessionId = sessionId;
      sessionGeneration += 1;
    }
    if (Number.isSafeInteger(response.event_cursor)) cursor = response.event_cursor;
    sessionReady = true;
    el("session-start").hidden = true;
    el("workspace-tabs").hidden = false;
    el("all-chats-link").hidden = false;
    document.body.classList.add("chat-active");
    document.body.classList.add("chat-view-active");
    const chat = el("view-chat");
    chat.hidden = false;
    chat.classList.add("active");
    historyOffset = 0;
    transcript.replaceChildren();
    for (const message of response.messages || []) {
      const isUser = message.role === "user";
      addBubble(isUser ? "You" : "Harness", message.text || "", isUser ? "user" : "assistant");
    }
    loadHistory();
  }

  function renderSessionChoices(sessions, activeId = "") {
    const list = el("session-list");
    const continueButton = el("continue-session");
    const loading = el("session-loading");
    list.replaceChildren();
    loading.hidden = true;
    continueButton.hidden = !sessions.length;
    el("return-to-chat").hidden = !sessionReady;
    if (!sessions.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = sessionReady ? "No other saved sessions are available." : "No previous sessions are available.";
      list.append(empty);
      return;
    }

    const mostRecent = sessions[0];
    continueButton.textContent = `Continue most recent · ${mostRecent.preview || sessionDate(mostRecent.updated_at)}`;
    continueButton.onclick = () => chooseSession(mostRecent.id);
    for (const session of sessions) {
      const isCurrent = session.id === activeId;
      const row = document.createElement("div");
      row.className = "session-row";
      const button = document.createElement("button");
      button.type = "button";
      button.className = `session-choice${isCurrent ? " current" : ""}`;
      if (isCurrent) button.setAttribute("aria-current", "true");
      const title = document.createElement("span");
      title.className = "session-choice-title";
      title.textContent = session.preview || "Session with no messages yet";
      if (isCurrent) {
        const current = document.createElement("span");
        current.className = "session-current";
        current.textContent = "Current";
        title.append(current);
      }
      const meta = document.createElement("span");
      meta.className = "session-choice-meta";
      meta.textContent = sessionMetadata(session);
      button.append(title, meta);
      button.addEventListener("click", () => chooseSession(session.id));
      row.append(button);
      if (!isCurrent) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "session-remove";
        remove.textContent = "Delete";
        remove.setAttribute("aria-label", `Delete chat: ${session.preview || sessionDate(session.updated_at)}`);
        remove.addEventListener("click", () => removeSession(session));
        row.append(remove);
      }
      list.append(row);
    }
    setBusy(busy);
  }

  async function chooseSession(id) {
    if (busy || sessionChanging || stopping) return;
    sessionChanging = true;
    setBusy(busy);
    el("session-error").hidden = true;
    try {
      const response = await api("session", { method: "POST", body: JSON.stringify({ id }) });
      showSelectedSession(response);
    } catch (error) {
      el("session-error").textContent = error.message;
      el("session-error").hidden = false;
    } finally {
      sessionChanging = false;
      setBusy(busy);
    }
  }

  async function loadSessionChoices() {
    el("session-loading").hidden = false;
    el("session-error").hidden = true;
    try {
      const response = await api("sessions");
      renderSessionChoices(response.sessions || [], response.active_id || "");
    } catch (error) {
      el("session-loading").hidden = true;
      el("session-error").textContent = error.message;
      el("session-error").hidden = false;
    }
  }

  async function showAllChats(event) {
    if (event) event.preventDefault();
    if (!sessionReady) return;
    if (busy || sessionChanging || stopping) {
      toast("Wait for the current operation to finish before changing sessions.");
      return;
    }
    el("session-start-heading").textContent = "All chats";
    el("session-start").querySelector(".lede").textContent = "Choose a saved conversation, delete a chat you no longer need, or start a separate session.";
    el("session-start").hidden = false;
    el("workspace-tabs").hidden = true;
    document.querySelectorAll(".view").forEach((view) => { view.hidden = true; view.classList.remove("active"); });
    el("all-chats-link").hidden = true;
    document.body.classList.remove("chat-active");
    document.body.classList.remove("chat-view-active");
    await loadSessionChoices();
  }

  async function returnToCurrentChat() {
    if (busy || sessionChanging || stopping) return;
    try {
      const response = await api("session");
      showSelectedSession(response);
    } catch (error) { toast(error.message); }
  }

  async function removeSession(session) {
    if (!session || !session.id || session.id === currentSessionId || busy || sessionChanging || stopping) return;
    const label = session.preview ? `“${session.preview}”` : `the chat from ${sessionDate(session.updated_at)}`;
    if (!window.confirm(`Permanently delete ${label}?\n\nThis removes the saved conversation log from this device.`)) return;
    sessionChanging = true;
    setBusy(busy);
    try {
      const response = await api("session/delete", { method: "POST", body: JSON.stringify({ id: session.id }) });
      renderSessionChoices(response.sessions || [], response.active_id || "");
      if (sessionReady) loadHistory();
      toast("Saved chat deleted.");
    } catch (error) {
      toast(error.message);
      await loadSessionChoices();
    } finally {
      sessionChanging = false;
      setBusy(busy);
    }
  }

  async function initializeSession() {
    try {
      const response = await api("session");
      if (response.selected) showSelectedSession(response);
      else renderSessionChoices(response.sessions || [], response.active_id || "");
    } catch (error) {
      el("session-loading").hidden = true;
      el("session-error").textContent = error.message;
      el("session-error").hidden = false;
    }
    poll();
  }

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateView(tab.dataset.view)));
  el("all-chats-link").addEventListener("click", showAllChats);
  el("return-to-chat").addEventListener("click", returnToCurrentChat);
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
  el("new-session").addEventListener("click", () => chooseSession("new"));

  initializeSession();
})();
