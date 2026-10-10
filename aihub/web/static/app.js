"use strict";

// --- state -------------------------------------------------------------------

const state = {
  conversations: [],
  projects: [],
  currentId: null,
  data: null,          // { conversation, messages, running, pending }
  pending: new Map(),  // permission id -> request
  live: null,          // { messageId, content } of the reply being streamed
  events: null,        // EventSource of the open conversation
  options: { models: ["default"], bypass_warning: "" },
  tick: null,
  agents: [],                // enabled agents, for plan assignments
  subtaskLive: new Map(),    // subtask id -> text streamed so far
  refreshTimer: null,
  me: null,                  // { name, is_owner } on a server with accounts; null otherwise (the owner)
};

// The owner of the server (or a server without accounts): settings, people, sharing.
const isOwner = () => !state.me || state.me.is_owner;

const $ = (id) => document.getElementById(id);
const MODE_LABELS = { manual: "Manuel", plan: "Plan", bypass: "Sans permission" };
// The local AI's models (« local:qwen3.5:4b ») come from its Ollama: named on the fly.
const MODEL_LABELS = new Proxy({}, { get: (labels, key) => key in BASE_MODEL_LABELS ? BASE_MODEL_LABELS[key]
  : typeof key === "string" && key.startsWith("local:") ? `Local · ${key.slice(6)}` : undefined });
const BASE_MODEL_LABELS = { default: "Par défaut", haiku: "Haiku (léger)", sonnet: "Sonnet", opus: "Opus", fable: "Fable",
  codex: "Codex (par défaut)", "gpt-6-astra": "GPT-6 Astra", "gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6-sol": "GPT-6 Sol", "gpt-6-luna": "GPT-6 Luna (léger)",
  "gpt-5.6-sol": "GPT-5.6 Sol", "gpt-5.6-terra": "GPT-5.6 Terra", "gpt-5.6-luna": "GPT-5.6 Luna (léger)" };
const WINDOW_LABELS = { five_hour: "5 heures", seven_day: "7 jours", seven_day_opus: "7 jours (Opus)", seven_day_sonnet: "7 jours (Sonnet)" };
// A subtask ("la sous-tâche") and a plan ("le plan") do not take the same agreement.
const STATUS_LABELS = { pending: "en attente", running: "en cours", ok: "réussie", error: "échec", stopped: "arrêtée" };
const PLAN_STATUS_LABELS = { proposed: "proposé", validated: "validé", running: "en cours", done: "terminé",
  error: "terminé avec des échecs", stopped: "arrêté" };
const KIND_NAMES = { code: "Code", terminal: "Terminal", migration: "Migration", redaction: "Rédaction",
  analyse: "Analyse", recherche: "Recherche", maths: "Maths", maths_avancees: "Maths avancées",
  algorithmique: "Algorithmique", ops: "Ops", securite: "Sécurité", autre: "Autre" };
const RUNNING_LABELS = { single: "L'agent travaille…", planning: "Le chef d'orchestre prépare le plan…",
  discussion: "Le chef d'orchestre répond…",
  adapt: "Le chef d'orchestre adapte le plan…", execution: "Les agents travaillent…",
  autonomous: "Travail autonome…" };
const RUN_STATUS_LABELS = { running: "en cours", done: "objectif atteint", limit: "limite atteinte avant la fin",
  stopped: "arrêté", error: "arrêté sur une erreur" };

// --- helpers -------------------------------------------------------------------

async function api(method, path, body) {
  // X-AIHub: a header no other site can make the browser send (see app.py).
  const headers = { "X-AIHub": "1" };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.code === "auth_required") {
    // The session has ended (expired, or closed from another device).
    showLogin("Votre session a expiré : reconnectez-vous.");
  }
  if (!res.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : `erreur ${res.status}`);
    error.code = data.code;
    error.status = res.status;
    error.retryAfter = data.retry_after_s;
    throw error;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message) {
  const node = el("div", { class: "toast", role: "status", text: message });
  document.body.append(node);
  setTimeout(() => node.remove(), 5000);
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " +
      d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) return "";
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe Markdown: everything is escaped first, then code blocks,
// inline code, bold and paragraphs are rebuilt.
function renderMarkdown(text) {
  const blocks = [];
  let src = escapeHtml(text || "").replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (_, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  src = src
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  const html = src.split(/\n{2,}/).map((part) => {
    const only = part.match(/^\u0000(\d+)\u0000$/);
    if (only) return blocks[Number(only[1])];
    return `<p>${part.replace(/\n/g, "<br>")}</p>`;
  }).join("");
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
}

function setView(view) {
  $("app").dataset.view = view;
}

// --- sidebar -------------------------------------------------------------------

async function loadConversations() {
  const [{ conversations }, { projects }] = await Promise.all([
    api("GET", "/api/conversations"), api("GET", "/api/projects")]);
  state.conversations = conversations;
  state.projects = projects;
  renderList();
}

// Folded projects, remembered on this device only.
function foldedProjects() {
  try { return new Set(JSON.parse(localStorage.getItem("aihub-folded") || "[]")); } catch (err) { return new Set(); }
}

function toggleFolded(pid) {
  const folded = foldedProjects();
  if (folded.has(pid)) folded.delete(pid); else folded.add(pid);
  try { localStorage.setItem("aihub-folded", JSON.stringify([...folded])); } catch (err) { /* private mode */ }
  renderList();
}

function conversationItem(conv) {
  return el("button", {
    class: "conv-item" + (conv.id === state.currentId ? " active" : ""),
    onclick: () => openConversation(conv.id),
  },
  el("span", { class: "conv-item-title", text: conv.title }),
  el("span", { class: "conv-item-sub" },
    conv.running ? el("span", { class: "dot", title: "en cours" }) : null,
    conv.project_id !== null && !conv.shared ? el("span", { class: "unshared", title: "Partage coupé", text: "privée · " }) : null,
    `${conv.mode === "orchestration" ? "Orchestration · " : ""}${MODE_LABELS[conv.permission_mode] || conv.permission_mode} · ${formatDate(conv.updated_at)}`));
}

function renderList() {
  const list = $("conversation-list");
  list.replaceChildren();
  if (!state.conversations.length && !state.projects.length) {
    list.append(el("p", { class: "list-empty", text: "Aucune conversation pour l'instant." }));
    return;
  }
  const folded = foldedProjects();
  for (const project of state.projects) {
    const convs = state.conversations.filter((c) => c.project_id === project.id);
    const open = !folded.has(project.id) || convs.some((c) => c.id === state.currentId);
    const group = el("section", { class: "project" + (open ? "" : " folded") });
    group.append(el("div", { class: "project-head" },
      el("button", { class: "project-name", type: "button", "aria-expanded": String(open),
        title: project.description || "", onclick: () => toggleFolded(project.id) },
        el("span", { class: "chevron", "aria-hidden": "true", text: open ? "▾" : "▸" }),
        el("span", { text: project.name }),
        el("span", { class: "project-count", text: String(convs.length) })),
      isOwner() ? el("button", { class: "btn btn-ghost btn-small", type: "button", text: "+",
        title: "Nouvelle conversation dans ce projet", "aria-label": `Nouvelle conversation dans ${project.name}`,
        onclick: () => openNewDialog(project.id) }) : null,
      isOwner() ? el("button", { class: "btn btn-ghost btn-small", type: "button", text: "…",
        title: "Modifier, partager ou supprimer le projet", "aria-label": `Modifier le projet ${project.name}`,
        onclick: () => openProjectDialog(project) }) : null));
    if (open) {
      if (!convs.length) group.append(el("p", { class: "list-empty", text: "Aucune conversation dans ce projet." }));
      for (const conv of convs) group.append(conversationItem(conv));
    }
    list.append(group);
  }
  const loose = state.conversations.filter((c) => c.project_id === null);
  if (loose.length) {
    if (state.projects.length) list.append(el("div", { class: "project-loose", text: "Sans projet" }));
    for (const conv of loose) list.append(conversationItem(conv));
  }
}

function projectOptions(selected, emptyLabel) {
  return [el("option", { value: "", text: emptyLabel, selected: selected === null }),
    ...state.projects.map((p) => el("option", { value: String(p.id), text: p.name, selected: p.id === selected }))];
}

async function loadStatus() {
  const pill = $("status-pill");
  try {
    const status = await api("GET", "/api/status");
    const gpt = status.codex || {};
    const parts = [status.logged_in ? "Claude Code connecté"
      : (status.error ? "Claude Code introuvable" : "Claude Code non connecté")];
    const hints = [];
    if (!status.logged_in) hints.push(status.error || "Claude Code : lancez « claude auth login » dans un terminal.");
    if (gpt.offered !== false) {
      parts.push(gpt.logged_in ? "Codex connecté" : (gpt.error ? "Codex introuvable" : "Codex non connecté"));
      if (!gpt.logged_in) hints.push(gpt.error || "Codex : ouvrez l'application Codex et connectez-vous.");
    }
    const local = status.local || {};
    if (local.offered !== false) {
      parts.push(local.running ? "IA locale prête" : "IA locale arrêtée");
      if (!local.running) hints.push(local.error || "IA locale : démarrez Ollama (ollama serve).");
    }
    pill.textContent = parts.join(" · ");
    pill.className = "pill " + (status.logged_in || gpt.logged_in || local.running ? "ok" : "bad");
    pill.title = hints.join("\n");
  } catch (err) {
    pill.textContent = "Serveur injoignable";
    pill.className = "pill bad";
  }
}

// refresh: ask Claude Code for the current figures (no quota spent); the
// server reuses its last reading if asked again within 30 seconds.
async function loadQuota(refresh = false) {
  const box = $("quota");
  let data;
  try {
    data = await api("GET", `/api/quota${refresh ? "?refresh=true" : ""}`);
  } catch (err) {
    data = { claude_code: null, error: err.message };
  }
  const refreshButton = () => el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Actualiser",
    title: "Relire le quota auprès des IA connectées (ne consomme rien)",
    onclick: async (event) => { event.target.disabled = true; await loadQuota(true); } });
  box.replaceChildren();
  quotaSection(box, "Claude Code", data.claude_code, data.error, refreshButton);
  if (data.codex_connected) quotaSection(box, "Codex", data.codex, data.codex_error, refreshButton);
}

function quotaSection(box, who, quota, error, refreshButton) {
  box.append(el("div", { class: "quota-title" }, `Quota de ${who}`,
    quota ? el("span", { text: { allowed: "disponible", allowed_warning: "bientôt atteint", rejected: "atteint" }[quota.status] || quota.status }) : null));
  if (error) box.append(el("p", { class: "quota-note quota-error", text: error }));
  if (!quota) {
    box.append(el("p", { class: "quota-note", text: "Inconnu pour l'instant." }), refreshButton());
    return;
  }
  const windows = Object.keys(quota.windows || {}).length
    ? quota.windows
    : (quota.limit_type ? { [quota.limit_type]: { utilization: quota.utilization, resets_at: quota.resets_at } } : {});
  const threshold = state.options.quota_warning || 80;
  // The alert threshold of the settings: said once per page, not at every refresh.
  const crossed = Object.entries(windows).find(([, w]) => typeof w.utilization === "number"
    && (w.utilization <= 1 ? w.utilization * 100 : w.utilization) >= threshold);
  state.quotaWarned = state.quotaWarned || {};
  if (crossed && !state.quotaWarned[who]) {
    state.quotaWarned[who] = true;
    const [name, w] = crossed;
    const used = Math.round(w.utilization <= 1 ? w.utilization * 100 : w.utilization);
    toast(`Quota de ${who} : fenêtre ${WINDOW_LABELS[name] || name} à ${used} % (seuil d'alerte ${threshold} %).`);
  }
  for (const [name, w] of Object.entries(windows)) {
    const used = typeof w.utilization === "number" ? Math.round(w.utilization <= 1 ? w.utilization * 100 : w.utilization) : null;
    box.append(el("div", { class: "quota-row" },
      el("div", { class: "quota-row-label" },
        el("span", { text: `Fenêtre ${WINDOW_LABELS[name] || name}` }),
        el("span", { text: used === null ? "?" : `reste ${Math.max(0, 100 - used)} %` })),
      used === null ? null : el("div", { class: "quota-note", text: `${used} % utilisés` }),
      el("div", { class: "bar" + (used !== null && used >= threshold ? " high" : "") },
        el("span", { style: `width:${Math.min(used || 0, 100)}%` })),
      w.resets_at ? el("div", { class: "quota-note", text: `Retour le ${formatDate(w.resets_at)}` }) : null));
  }
  box.append(el("div", { class: "quota-footer" },
    el("span", { class: "quota-note", text: `Relevé à ${formatDate(quota.observed_at)}` }), refreshButton()));
}

// --- conversation ----------------------------------------------------------------

async function openConversation(id, messageId = null) {
  if (state.events) state.events.close();
  state.currentId = id;
  state.pending = new Map();
  state.live = null;
  state.subtaskLive = new Map();
  renderList();
  setView("chat");
  loadAgents();
  $("empty").classList.add("hidden");
  $("conversation").classList.remove("hidden");
  await refreshConversation();
  connectEvents(id);
  if (messageId !== null) {
    // From a search result: show the message found.
    const node = $("messages").querySelector(`[data-id="${Number(messageId)}"]`);
    if (node) {
      node.scrollIntoView({ block: "center" });
      node.classList.add("found");
      setTimeout(() => node.classList.remove("found"), 2500);
    }
  }
}

async function refreshConversation() {
  const id = state.currentId;
  if (id === null) return;
  try {
    const data = await api("GET", `/api/conversations/${id}`);
    // The automatic assignment may have created agents the page does not know yet.
    if (data.conversation.mode === "orchestration") await loadAgents();
    if (id !== state.currentId) return;
    state.data = data;
    // The server's list is the truth: a prompt answered on another device disappears.
    state.pending = new Map(data.pending.map((p) => [p.id, p]));
    const active = data.messages.find((m) => m.role === "assistant" && (m.status === "pending" || m.status === "running"));
    state.live = active ? { messageId: active.id, content: active.content } : null;
    renderConversation();
  } catch (err) {
    if (err.status === 404) {
      closeConversation();
      await loadConversations();
    }
    toast(err.message);
  }
}

function closeConversation() {
  if (state.events) state.events.close();
  state.events = null;
  state.currentId = null;
  state.data = null;
  $("conversation").classList.add("hidden");
  $("empty").classList.remove("hidden");
  setView("list");
}

function renderConversation() {
  const { conversation: conv, messages } = state.data;
  $("conv-title").textContent = conv.title;
  const how = conv.mode === "orchestration"
    ? `Orchestration · chef : ${MODEL_LABELS[conv.conductor_model] || conv.conductor_model || "?"}`
    : (MODEL_LABELS[conv.model] || conv.model || "");
  $("conv-meta").textContent = `${conv.workdir || "sans dossier"} · ${how}`;
  $("conv-mode").value = conv.permission_mode;
  // The AI of the conversation (the conductor in orchestration); one no longer offered stays shown.
  const current = conv.mode === "orchestration" ? conv.conductor_model : conv.model;
  const offered = state.options.models.includes(current) || !current ? state.options.models
    : [current, ...state.options.models];
  $("conv-model").replaceChildren(...offered.map((m) => el("option", {
    value: m, text: (conv.mode === "orchestration" ? "Chef : " : "IA : ") + (MODEL_LABELS[m] || m),
    selected: m === current })));
  $("conv-assignment").classList.toggle("hidden", conv.mode !== "orchestration");
  $("conv-assignment").value = conv.assignment_mode;
  $("schedules").classList.toggle("hidden", conv.mode === "orchestration");
  $("conv-project").replaceChildren(...projectOptions(conv.project_id, "Sans projet"));
  const inProject = conv.project_id !== null;
  $("conv-shared").classList.toggle("hidden", !inProject);
  $("conv-shared").classList.toggle("on", Boolean(conv.shared));
  $("conv-shared").textContent = conv.shared ? "Partagée ✓" : "Privée";
  const others = state.conversations.filter((c) => inProject && c.project_id === conv.project_id && c.id !== conv.id);
  $("cite").classList.toggle("hidden", !inProject || !conv.shared || !others.length);
  const vault = (state.options.obsidian || {}).enabled;
  $("obsidian").classList.toggle("hidden", !vault);
  $("obsidian").classList.toggle("on", Boolean(conv.obsidian_note));
  $("obsidian").textContent = conv.obsidian_note ? "Obsidian ✓" : "Obsidian";
  // Shared with someone: what is the owner's stays hidden, and reading only means no composer.
  const role = conv.my_role || "owner";
  for (const id of ["conv-project", "conv-assignment", "conv-model", "conv-mode", "obsidian", "tools",
    "schedules", "rename", "delete", "conv-shared"]) {
    if (role !== "owner") $(id).classList.add("hidden");
  }
  $("share").classList.toggle("hidden", role !== "owner" || !state.me);
  $("share").textContent = (state.data.shared_with || []).length ? `Partagée (${state.data.shared_with.length})` : "Partager";
  $("composer").classList.toggle("hidden", role === "read");
  $("readonly-note").classList.toggle("hidden", role !== "read");
  $("fresh").classList.toggle("hidden", role === "read");
  renderFresh(conv);
  renderMessages(messages);
  renderRunning();
}

function renderMessages(messages) {
  const box = $("messages");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  if (!messages.length) {
    const orchestration = state.data.conversation.mode === "orchestration";
    box.append(el("p", { class: "list-empty", text: orchestration
      ? "Écrivez votre demande : le chef d'orchestre proposera un plan, que vous pourrez modifier avant de le lancer."
      : "Écrivez votre première demande ci-dessous." }));
  }
  // A plan shows right after the conductor's reply to the request it answers.
  const plans = new Map((state.data.plans || []).map((p) => [p.request_message_id, p]));
  // Autonomous work: each round's reply gets its check, the last one the run's end.
  const rounds = new Map();
  for (const run of state.data.autonomous || []) {
    run.round_list.forEach((r, i) => rounds.set(r.message_id,
      { run, round: r, last: i === run.round_list.length - 1 }));
  }
  const shown = new Set();
  // Who wrote each request, once several people can write here.
  const conv = state.data.conversation;
  const authors = (state.data.shared_with || []).length > 0 || (conv.my_role && conv.my_role !== "owner")
    || messages.some((m) => m.author && state.me && m.author !== state.me.name);
  let waitingPlan = null;
  const flushPlan = () => {
    if (waitingPlan && !shown.has(waitingPlan.id)) { box.append(planCard(waitingPlan)); shown.add(waitingPlan.id); }
    waitingPlan = null;
  };
  for (const m of messages) {
    if (m.role === "user") {
      flushPlan();
      box.append(authors && m.author
        ? el("div", { class: "msg user", "data-id": m.id },
          el("div", { class: "msg-author", text: m.author }), el("div", { text: m.content }))
        : el("div", { class: "msg user", text: m.content, "data-id": m.id }));
      waitingPlan = plans.get(m.id) || null;
      continue;
    }
    const live = state.live && state.live.messageId === m.id;
    const body = el("div", { class: "msg-body" + (live ? " cursor" : ""), id: live ? "live-body" : null });
    body.innerHTML = renderMarkdown(live ? state.live.content : m.content);
    const node = el("div", { class: "msg assistant", "data-id": m.id }, body);
    if (m.status === "error") node.append(el("div", { class: "msg-error", text: m.error || "erreur" }));
    if (m.status === "stopped") node.append(el("div", { class: "msg-stopped", text: m.error || "Requête arrêtée." }));
    if (!live) {
      const meta = [formatDuration(m.duration_ms)];
      if (m.tokens_in !== null) meta.push(`${m.tokens_in} tokens envoyés, ${m.tokens_out} reçus`);
      if (m.model && m.model !== "default") meta.push(m.model);
      node.append(el("div", { class: "msg-meta", text: meta.filter(Boolean).join(" · ") }));
      if (state.data.conversation.obsidian_note && m.status === "ok") {
        // Conversations stay out of the note: a reply goes there only on this click.
        node.append(el("button", { class: "msg-note", type: "button", disabled: Boolean(m.noted_at),
          text: m.noted_at ? "Notée dans Obsidian" : "Noter dans Obsidian",
          onclick: async (e) => {
            e.target.disabled = true;
            try {
              await api("POST", `/api/messages/${m.id}/obsidian`);
              toast("Réponse ajoutée à la note de projet.");
              await refreshConversation();
            } catch (err) { toast(err.message); e.target.disabled = false; }
          } }));
      }
    }
    const step = rounds.get(m.id);
    if (step) node.append(roundNote(step));
    // The opinions this reply asked for: those made while it was being written.
    const next = messages.find((x) => x.id > m.id && x.role === "user");
    for (const d of state.data.delegations || []) {
      if (d.created_at >= m.created_at && (!next || d.created_at < next.created_at)) node.append(delegationNote(d));
    }
    box.append(node);
    flushPlan();
  }
  flushPlan();
  for (const plan of state.data.plans || []) if (!shown.has(plan.id)) box.append(planCard(plan));
  for (const request of state.pending.values()) box.append(permissionCard(request));
  if (atBottom || state.live) box.scrollTop = box.scrollHeight;
}

function delegationNote(d) {
  const who = MODEL_LABELS[d.model] || d.model;
  const head = d.status === "running" ? `Avis demandé à ${who}…`
    : d.status === "ok" ? `Avis de ${who}` : `Avis de ${who} : échec`;
  return el("details", { class: "round-note delegation" },
    el("summary", { text: head + (d.duration_ms ? ` · ${formatDuration(d.duration_ms)}` : "") }),
    el("p", { class: "quota-note", text: `Question : ${d.question}` }),
    d.answer ? el("pre", { text: d.answer }) : null,
    d.error ? el("p", { class: "msg-error", text: d.error }) : null);
}

function roundNote({ run, round, last }) {
  const parts = [`Travail autonome · tour ${round.round} sur ${run.max_rounds}`];
  if (round.check_code !== null) parts.push(round.check_code === 0 ? "vérification réussie"
    : `vérification en échec (code ${round.check_code})`);
  const note = el("div", { class: "round-note" }, el("span", { text: parts.join(" · ") }));
  if (round.check_output) {
    note.append(el("details", {}, el("summary", { text: "Sortie de la vérification" }),
      el("pre", { text: round.check_output })));
  }
  if (last && run.status !== "running") {
    note.append(el("div", { class: "round-end " + (run.status === "done" ? "ok" : "bad"),
      text: `Fin : ${RUN_STATUS_LABELS[run.status] || run.status}, ${run.rounds} tour(s)`
        + (run.error ? ` — ${run.error}` : "") }));
  }
  return note;
}

function setupToolsDialog() {
  const dialog = $("tools-dialog");
  $("tools").addEventListener("click", () => {
    const chosen = state.data.conversation.tools || [];
    $("tools-error").textContent = "";
    $("tools-list").replaceChildren(...(state.data.tools_offered || []).map((t) => el("label", { class: "check tool-item" },
      el("input", { type: "checkbox", value: t.name, checked: chosen.includes(t.name) }),
      el("span", {}, el("strong", { text: ` ${t.label}` }), t.read_only ? " · lecture seule" : "",
        t.description ? el("span", { class: "quota-note", text: ` — ${t.description}` }) : null))));
    dialog.showModal();
  });
  $("tools-cancel").addEventListener("click", () => dialog.close());
  $("tools-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, { tools: checkedAlerts($("tools-list")) });
      dialog.close();
      await refreshConversation();
      toast("Outils enregistrés pour cette conversation.");
    } catch (err) { $("tools-error").textContent = err.message; }
  });
}

function setupAutonomousDialog() {
  const dialog = $("autonomous-dialog");
  const syncConfirm = () => {
    const check = $("auto-check").value.trim();
    $("auto-confirm-wrap").classList.toggle("hidden", !check);
    if (!check) $("auto-confirm").checked = false;
  };
  $("auto-check").addEventListener("input", syncConfirm);
  $("autonomous").addEventListener("click", () => {
    $("auto-goal").value = $("prompt").value;
    $("auto-error").textContent = "";
    $("auto-confirm-text").textContent = state.options.check_warning
      || "La commande de vérification est lancée par AI Hub, sans le bac à sable de l'agent.";
    syncConfirm();
    dialog.showModal();
    $("auto-goal").focus();
  });
  $("auto-cancel").addEventListener("click", () => dialog.close());
  $("autonomous-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("auto-start").disabled = true;
    try {
      await api("POST", `/api/conversations/${state.currentId}/autonomous`, {
        goal: $("auto-goal").value, max_rounds: Number($("auto-rounds").value),
        max_minutes: Number($("auto-minutes").value), check_command: $("auto-check").value.trim() || null,
        confirm_check: $("auto-confirm").checked });
      dialog.close();
      $("prompt").value = "";
      await refreshConversation();
    } catch (err) {
      $("auto-error").textContent = err.message;
    } finally {
      $("auto-start").disabled = false;
    }
  });
}

const DAY_NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
const TASK_STATUS = { running: "en cours", ok: "réussie", error: "en échec", reportee: "reportée (conversation occupée)",
  limit: "limite atteinte", stopped: "arrêtée" };

function setupScheduleDialog() {
  const dialog = $("schedule-dialog");
  $("sched-days").replaceChildren(...DAY_NAMES.map((d, i) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: String(i), checked: i < 5 }), ` ${d}`)));
  const sync = () => {
    const every = $("sched-every").value;
    $("sched-at-wrap").classList.toggle("hidden", every === "interval");
    $("sched-interval-wrap").classList.toggle("hidden", every !== "interval");
    $("sched-days").classList.toggle("hidden", every !== "weekly");
    $("sched-auto").classList.toggle("hidden", $("sched-kind").value !== "autonomous");
    const check = $("sched-check").value.trim();
    $("sched-confirm-wrap").classList.toggle("hidden", !check);
  };
  for (const id of ["sched-every", "sched-kind"]) $(id).addEventListener("change", sync);
  $("sched-check").addEventListener("input", sync);
  async function render() {
    const { schedules } = await api("GET", `/api/schedules?conversation_id=${state.currentId}`);
    const list = $("schedule-list");
    if (!schedules.length) {
      list.replaceChildren(el("p", { class: "quota-note", text: "Aucune tâche pour l'instant." }));
      return;
    }
    list.replaceChildren(...schedules.map((t) => el("div", { class: "schedule-item" + (t.enabled ? "" : " off") },
      el("div", { class: "schedule-what" },
        el("strong", { text: t.prompt.length > 90 ? t.prompt.slice(0, 90) + "…" : t.prompt }),
        el("span", { class: "quota-note", text: [t.kind === "autonomous" ? "travail autonome" : "une réponse",
          t.rule, t.enabled ? `prochaine : ${formatDate(t.next_run_at)}` : "en pause",
          t.last_status ? `dernière : ${TASK_STATUS[t.last_status] || t.last_status}` : null,
          t.last_error ? t.last_error : null].filter(Boolean).join(" · ") })),
      el("div", { class: "schedule-actions" },
        el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Lancer maintenant",
          onclick: () => act("POST", `/api/schedules/${t.id}/run`, null, "Tâche lancée.") }),
        el("button", { class: "btn btn-ghost btn-small", type: "button", text: t.enabled ? "Pause" : "Reprendre",
          onclick: () => act("PATCH", `/api/schedules/${t.id}`, { enabled: !t.enabled }) }),
        el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Supprimer",
          onclick: () => act("DELETE", `/api/schedules/${t.id}`, null, "Tâche supprimée.") })))));
  }
  async function act(method, path, body, message) {
    $("sched-error").textContent = "";
    try {
      await api(method, path, body || undefined);
      if (message) toast(message);
      await render();
      if (method === "POST") await refreshConversation();
    } catch (err) { $("sched-error").textContent = err.message; }
  }
  $("schedules").addEventListener("click", async () => {
    $("sched-prompt").value = $("prompt").value;
    $("sched-error").textContent = "";
    $("sched-confirm-text").textContent = state.options.check_warning || "";
    sync();
    dialog.showModal();
    try { await render(); } catch (err) { $("sched-error").textContent = err.message; }
  });
  $("sched-close").addEventListener("click", () => dialog.close());
  $("schedule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const every = $("sched-every").value;
    const body = { conversation_id: state.currentId, prompt: $("sched-prompt").value, kind: $("sched-kind").value,
      every, at_time: every === "interval" ? null : $("sched-at").value,
      interval_minutes: every === "interval" ? Number($("sched-interval").value) : null,
      weekdays: every === "weekly" ? checkedAlerts($("sched-days")).map(Number) : null };
    if (body.kind === "autonomous") {
      Object.assign(body, { max_rounds: Number($("sched-rounds").value), max_minutes: Number($("sched-minutes").value),
        check_command: $("sched-check").value.trim() || null, confirm_check: $("sched-confirm").checked });
    }
    $("sched-add").disabled = true;
    try {
      await api("POST", "/api/schedules", body);
      toast("Tâche planifiée ajoutée.");
      $("sched-prompt").value = "";
      await render();
    } catch (err) {
      $("sched-error").textContent = err.message;
    } finally {
      $("sched-add").disabled = false;
    }
  });
}

// In orchestration, two ways to send: talk with the conductor, or ask it for
// a plan. While a plan waits for validation, talking is the main one and a
// second plan cannot be asked.
function openPlan() {
  return (state.data.plans || []).find((p) => p.status === "proposed") || null;
}

function primaryButton() {
  const orchestration = state.data && state.data.conversation.mode === "orchestration";
  return orchestration && openPlan() ? $("discuss") : $("send");
}

function updateComposer(running) {
  const orchestration = state.data.conversation.mode === "orchestration";
  const plan = orchestration ? openPlan() : null;
  const discuss = $("discuss");
  const send = $("send");
  discuss.classList.toggle("hidden", !orchestration);
  send.textContent = orchestration ? "Demander un plan" : "Envoyer";
  discuss.className = "btn " + (plan ? "btn-primary" : "btn-ghost") + (orchestration ? "" : " hidden");
  send.className = "btn " + (plan ? "btn-ghost" : "btn-primary");
  discuss.disabled = running;
  $("autonomous").classList.toggle("hidden", orchestration);
  $("autonomous").disabled = running;
  send.disabled = running || Boolean(plan);
  send.title = plan ? "Un plan attend votre validation : lancez-le ou annulez-le d'abord" : "";
  $("prompt").placeholder = !orchestration ? "Votre demande…"
    : plan ? "Une question ou un changement pour le chef ? Ensuite, « Adapter le plan »."
      : "Votre demande, ou une question pour le chef…";
}

function renderRunning() {
  const running = Boolean(state.data && (state.data.running || state.live));
  $("running").classList.toggle("hidden", !running);
  $("prompt").disabled = running;
  updateComposer(running);
  $("conv-mode").disabled = running;
  $("conv-model").disabled = running;
  $("conv-assignment").disabled = running;
  $("delete").disabled = running;
  clearInterval(state.tick);
  if (running) {
    const msg = state.live && state.data.messages.find((m) => m.id === state.live.messageId);
    const start = msg ? new Date(msg.created_at).getTime() : Date.now();
    const update = () => { $("elapsed").textContent = formatDuration(Date.now() - start); };
    update();
    state.tick = setInterval(update, 1000);
    const run = (state.data.autonomous || []).find((r) => r.status === "running");
    $("running-text").textContent = state.pending.size ? "En attente de votre réponse"
      : state.data.running_kind === "autonomous" && run
        ? `Travail autonome : tour ${state.autoRound || run.rounds || 1} sur ${run.max_rounds}`
          + (state.autoChecking ? ", vérification…" : "…")
        : (RUNNING_LABELS[state.data.running_kind] || RUNNING_LABELS.single);
  }
}

function appendLive(chunk) {
  state.live.content += chunk;
  const body = $("live-body");
  if (body) {
    body.innerHTML = renderMarkdown(state.live.content);
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
  }
}

// --- live events -------------------------------------------------------------------

function connectEvents(id) {
  const source = new EventSource(`/api/conversations/${id}/events`);
  state.events = source;
  source.onmessage = (msg) => {
    if (id !== state.currentId) return;
    handleEvent(JSON.parse(msg.data));
  };
  source.onerror = () => {
    // The browser reconnects by itself; the snapshot on reconnection catches up.
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "snapshot":
      if (ev.running) {
        for (const p of ev.pending) state.pending.set(p.id, p);
        for (const [sid, text] of Object.entries(ev.subtask_texts || {})) state.subtaskLive.set(Number(sid), text);
        if (ev.message_id !== null) state.live = { messageId: ev.message_id, content: ev.content };
        refreshConversation();
      }
      break;
    case "started":
      refreshConversation();
      loadConversations();
      break;
    case "text":
      if (!state.live || state.live.messageId !== ev.message_id) { refreshConversation(); break; }
      if (ev.offset === state.live.content.length) appendLive(ev.chunk);
      else if (ev.offset > state.live.content.length) refreshConversation();  // a piece was missed
      break;
    case "permission":
      // A guest answers only their own requests' prompts: the server says which (can_answer).
      if (!isOwner()) { refreshConversation(); break; }
      state.pending.set(ev.id, ev);
      renderMessages(state.data.messages);
      renderRunning();
      notify("L'agent attend votre réponse", ev.tool_name);
      break;
    case "permission_answered":
      state.pending.delete(ev.id);
      renderMessages(state.data.messages);
      renderRunning();
      break;
    case "done":
      state.live = null;
      state.pending = new Map();
      state.subtaskLive = new Map();
      refreshConversation();
      loadConversations();
      loadQuota();
      if (ev.status === "error") notify("Erreur", ev.error || "");
      else if (ev.kind === "planning") notify("Plan proposé", "Le chef d'orchestre attend votre validation.");
      else if (ev.kind === "execution") notify("Plan terminé", PLAN_STATUS_LABELS[ev.status] || ev.status);
      else if (ev.kind === "autonomous") notify("Travail autonome terminé", RUN_STATUS_LABELS[ev.status] || ev.status);
      state.autoRound = null;
      break;
    case "subtask_text": {
      const text = (state.subtaskLive.get(ev.subtask_id) || "");
      if (ev.offset === text.length) {
        state.subtaskLive.set(ev.subtask_id, text + ev.chunk);
        const live = document.querySelector(`[data-live-subtask="${ev.subtask_id}"]`);
        if (live) { live.textContent = text + ev.chunk; live.scrollTop = live.scrollHeight; }
      }
      break;
    }
    case "obsidian_error":
      toast(`Note Obsidian non écrite : ${ev.error}`);
      break;
    case "autonomous_started":
      state.autoRound = 1;
      refreshConversation();
      break;
    case "round_started":
      state.autoRound = ev.round;
      state.autoChecking = false;
      renderRunning();
      break;
    case "check_started":
      state.autoChecking = true;
      renderRunning();
      break;
    case "round_done":
      state.autoChecking = false;
      scheduleRefresh();
      break;
    case "plan_proposed": case "plan_updated": case "plan_started": case "subtask_started":
    case "subtask_done": case "synthesis_started": case "plan_done":
      scheduleRefresh();
      break;
  }
}

// Several plan events may come within a moment: one refresh for them all.
function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refreshConversation, 150);
}

// --- orchestration plans ------------------------------------------------------------

async function loadAgents() {
  try {
    state.agents = (await api("GET", "/api/agents")).agents;
  } catch (err) {
    toast(err.message);
  }
}

async function planAction(method, path, body) {
  try {
    await api(method, path, body);
    await refreshConversation();
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

function agentOptions(selected, { empty = null } = {}) {
  const options = [];
  if (empty !== null) options.push(el("option", { value: "", text: empty }));
  for (const a of state.agents) {
    options.push(el("option", { value: a.id, text: `${a.name}`, selected: a.id === selected }));
  }
  return options;
}

function modelOptions(selected) {
  return state.options.models.map((m) => el("option", { value: m, text: MODEL_LABELS[m] || m, selected: m === selected }));
}

function planCard(plan) {
  const editable = plan.status === "proposed" && !state.data.running;
  const positions = new Map(plan.subtasks.map((s) => [s.id, s.position]));
  const card = el("div", { class: "plan", "data-plan": plan.id });
  card.append(el("h3", {}, "Plan du chef d'orchestre",
    el("span", { class: `badge ${plan.status}`, text: PLAN_STATUS_LABELS[plan.status] || plan.status }),
    el("span", { class: "chip", text: `${plan.max_parallel || 1} agent(s) en parallèle, choisi par le chef` })));
  if (plan.summary) card.append(el("p", { class: "plan-summary", text: plan.summary }));

  const rows = el("div", { class: "plan-rows" });
  for (const st of plan.subtasks) rows.append(subtaskRow(plan, st, positions, editable));
  card.append(rows);

  if (editable) card.append(planTools(plan));
  return card;
}

function subtaskRow(plan, st, positions, editable) {
  const row = el("div", { class: "plan-row", "data-subtask": st.id });
  row.append(el("span", { class: "plan-pos", text: `${st.position}.` }),
    el("div", {}, el("div", { class: "plan-title", text: st.title || st.description.slice(0, 80) }),
      el("div", { class: "plan-desc", text: st.description })));

  const files = (st.files || []).map((f) => el("span", { class: "chip" + (f === "*" ? " warn" : ""),
    title: f === "*" ? "tout le projet : cette sous-tâche tournera seule" : "fichier prévu",
    text: f === "*" ? "tout le projet" : f }));
  const deps = st.depends_on.map((d) => `${positions.get(d)}`).join(", ");
  const kind = st.kind || "autre";
  const kindNode = editable
    ? el("select", { "aria-label": `Genre de travail de la sous-tâche ${st.position}`,
      title: "Le genre de travail décide de l'IA en assignation automatique",
      onchange: (e) => planAction("PATCH", `/api/subtasks/${st.id}`, { kind: e.target.value }) },
    ...Object.keys(KIND_NAMES).map((k) => el("option", { value: k, text: KIND_NAMES[k], selected: k === kind,
      title: (state.options.kinds || {})[k] })))
    : el("span", { class: "chip kind", text: KIND_NAMES[kind] || kind, title: (state.options.kinds || {})[kind] });
  row.append(el("div", { class: "plan-line" }, kindNode, files.length ? "Fichiers :" : null, ...files,
    deps ? el("span", { class: "chip", text: `après ${deps}` }) : null));
  if (st.assignment_reason) row.append(el("div", { class: "plan-reason", text: st.assignment_reason }));

  if (editable) {
    const agentSelect = el("select", { "aria-label": `Agent de la sous-tâche ${st.position}`,
      onchange: (e) => e.target.value
        ? planAction("PATCH", `/api/subtasks/${st.id}`, { agent_id: Number(e.target.value) })
        : planAction("PATCH", `/api/subtasks/${st.id}`, { unassign: true }) },
    ...agentOptions(st.agent_id, { empty: "— choisir un agent —" }));
    const modelSelect = el("select", { "aria-label": `Modèle de la sous-tâche ${st.position}`, disabled: !st.agent_id,
      title: "L'IA qui fait tourner l'agent pour cette sous-tâche",
      onchange: (e) => planAction("PATCH", `/api/subtasks/${st.id}`, { model: e.target.value }) },
    ...modelOptions(st.model));
    const others = plan.subtasks.filter((o) => o.id !== st.id);
    const depSelect = el("select", { "aria-label": `Dépendances de la sous-tâche ${st.position}`,
      onchange: (e) => {
        const [action, id] = e.target.value.split(":");
        if (action === "add") planAction("POST", `/api/subtasks/${st.id}/dependencies`, { depends_on_id: Number(id) });
        if (action === "del") planAction("DELETE", `/api/subtasks/${st.id}/dependencies/${id}`);
      } },
    el("option", { value: "", text: "Dépendances…" }),
    ...others.map((o) => st.depends_on.includes(o.id)
      ? el("option", { value: `del:${o.id}`, text: `ne plus attendre ${o.position}` })
      : el("option", { value: `add:${o.id}`, text: `attendre ${o.position}` })));
    row.append(el("div", { class: "plan-line" }, "Agent :", agentSelect, "IA :", modelSelect, depSelect,
      el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Retirer",
        onclick: () => planAction("DELETE", `/api/subtasks/${st.id}`) })));
    return row;
  }

  const agent = state.agents.find((a) => a.id === st.agent_id);
  const facts = [agent ? agent.name : null, st.model ? `IA : ${MODEL_LABELS[st.model] || st.model}` : null,
    st.duration_ms !== null ? formatDuration(st.duration_ms) : null].filter(Boolean);
  row.append(el("div", { class: "plan-line" },
    el("span", { class: `badge ${st.status}`, text: STATUS_LABELS[st.status] || st.status }),
    facts.join(" · ")));
  if (st.status === "running") {
    row.append(el("div", { class: "plan-live", "data-live-subtask": st.id,
      text: state.subtaskLive.get(st.id) || "…" }));
  }
  if (st.status === "error" && st.error) row.append(el("div", { class: "msg-error plan-result", text: st.error }));
  if (st.result) {
    const details = el("details", { class: "plan-result" }, el("summary", { text: "Compte rendu de l'agent" }));
    const body = el("div", { class: "msg-body" });
    body.innerHTML = renderMarkdown(st.result);
    details.append(body);
    row.append(details);
  }
  return row;
}

function planTools(plan) {
  const used = [...new Set(plan.subtasks.map((s) => s.agent_id).filter(Boolean))];
  const assignSelect = el("select", { "aria-label": "Agent pour toutes les sous-tâches sans agent" }, ...agentOptions(null));
  const newModel = el("select", { "aria-label": "Modèle du nouvel agent" }, ...modelOptions("sonnet"));
  const removeSelect = el("select", { "aria-label": "Agent à retirer du plan" },
    ...state.agents.filter((a) => used.includes(a.id)).map((a) => el("option", { value: a.id, text: a.name })));
  const unassigned = plan.subtasks.filter((s) => !s.agent_id).length;

  const tools = el("div", { class: "plan-tools" },
    el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Répartir selon les benchmarks",
      title: "Chaque sous-tâche reçoit l'IA la plus forte pour son genre de travail, d'après les classements indépendants",
      onclick: () => planAction("POST", `/api/plans/${plan.id}/auto-assign`) }),
    el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Tout assigner à",
      disabled: !unassigned || !state.agents.length,
      onclick: () => planAction("POST", `/api/plans/${plan.id}/assign-all`, { agent_id: Number(assignSelect.value) }) }),
    assignSelect,
    el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Ajouter un agent",
      onclick: async () => {
        try {
          await api("POST", "/api/agents", { model: newModel.value });
          await loadAgents();
          renderMessages(state.data.messages);
          toast("Agent ajouté : choisissez-le dans les sous-tâches.");
        } catch (err) { toast(err.message); }
      } }),
    newModel,
    used.length ? el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Retirer l'agent",
      title: "Ses sous-tâches n'auront plus d'agent ; le chef peut les réassigner avec « Adapter le plan »",
      onclick: () => planAction("POST", `/api/plans/${plan.id}/remove-agent`, { agent_id: Number(removeSelect.value) }) }) : null,
    used.length ? removeSelect : null,
    el("span", { class: "grow" }),
    el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Ajouter une sous-tâche",
      onclick: (e) => { e.target.disabled = true; e.target.closest(".plan").append(addSubtaskForm(plan)); } }),
    el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Adapter le plan",
      title: "Le chef d'orchestre refait le plan d'après vos changements et votre discussion avec lui",
      onclick: () => planAction("POST", `/api/plans/${plan.id}/adapt`) }),
    el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Annuler le plan",
      onclick: async () => {
        const ok = await confirmDialog({ title: "Annuler le plan ?", text: "Le plan sera abandonné sans rien lancer. Vous pourrez ensuite envoyer une nouvelle demande.", okText: "Annuler le plan" });
        if (ok) planAction("POST", `/api/plans/${plan.id}/cancel`);
      } }),
    el("button", { class: "btn btn-primary", type: "button", text: "Valider et lancer",
      title: unassigned ? `${unassigned} sous-tâche(s) sans agent` : "Rien ne part avant ce clic",
      onclick: async () => {
        const ok = await confirmDialog({ title: "Lancer le plan ?",
          text: `${plan.subtasks.length} sous-tâche(s), jusqu'à ${plan.max_parallel || 1} agent(s) en même temps. Chaque agent consomme votre quota.`,
          okText: "Lancer" });
        if (ok) planAction("POST", `/api/plans/${plan.id}/launch`);
      } }));
  return tools;
}

function addSubtaskForm(plan) {
  const title = el("input", { placeholder: "Titre court" });
  const description = el("textarea", { rows: 2, placeholder: "Ce que l'agent doit faire, précisément" });
  const agent = el("select", { "aria-label": "Agent de la nouvelle sous-tâche" }, ...agentOptions(null, { empty: "— agent plus tard —" }));
  const form = el("div", { class: "plan-add" }, title, description,
    el("div", { class: "plan-line" }, "Agent :", agent,
      el("button", { class: "btn btn-primary btn-small", type: "button", text: "Ajouter",
        onclick: () => {
          if (!description.value.trim()) { toast("Décrivez la sous-tâche."); return; }
          planAction("POST", `/api/plans/${plan.id}/subtasks`, { title: title.value.trim() || null,
            description: description.value.trim(), agent_id: agent.value ? Number(agent.value) : null });
        } })));
  return form;
}

function notify(title, body) {
  if (document.visibilityState === "visible") return;
  if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body });
}

// --- permission cards ----------------------------------------------------------------

function permissionCard(request) {
  const card = el("div", { class: "perm", "data-permission": request.id });
  const input = request.input || {};
  const fromSubtask = request.subtask_title
    ? el("p", { class: "perm-subtask", text: `Sous-tâche : ${request.subtask_title}` }) : null;
  const heading = (text) => [el("h3", { text }), fromSubtask].filter(Boolean);
  const reason = el("input", { type: "text", placeholder: "Raison du refus (facultatif)" });
  const send = async (allow, answers) => {
    for (const b of card.querySelectorAll("button")) b.disabled = true;
    try {
      await api("POST", `/api/permissions/${request.id}`, { allow, message: reason.value, answers });
    } catch (err) {
      toast(err.message);
      for (const b of card.querySelectorAll("button")) b.disabled = false;
    }
  };
  const deny = el("button", { class: "btn btn-danger-ghost", type: "button", text: "Refuser", onclick: () => send(false) });
  if (request.can_answer === false) {
    // Someone else's request: only they say yes or no (it runs on their accounts).
    card.append(...heading(`L'agent attend un accord pour « ${request.tool_name} »`),
      el("p", { class: "quota-note", text: "Cette demande n'est pas la vôtre : seule la personne qui l'a envoyée peut répondre." }));
    return card;
  }

  if (request.tool_name === "AskUserQuestion") {
    card.append(...heading("L'agent vous pose une question"));
    const fields = [];
    (input.questions || []).forEach((q, qi) => {
      const name = `q-${request.id}-${qi}`;
      const fs = el("fieldset", { class: "perm-question" }, el("legend", { text: q.question || q.header || "Question" }));
      const type = q.multiSelect ? "checkbox" : "radio";
      for (const option of q.options || []) {
        fs.append(el("label", { class: "perm-option" },
          el("input", { type, name, value: option.label }),
          el("span", {}, option.label, option.description ? el("small", { text: option.description }) : null)));
      }
      const other = el("input", { type: "text", placeholder: "Autre réponse (facultatif)" });
      fs.append(other);
      fields.push({ q, name, other });
      card.append(fs);
    });
    const ok = el("button", { class: "btn btn-ok", type: "button", text: "Répondre", onclick: () => {
      const answers = {};
      for (const { q, name, other } of fields) {
        const picked = [...card.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
        if (other.value.trim()) picked.push(other.value.trim());
        if (picked.length) answers[q.question] = picked.join(", ");
      }
      if (!Object.keys(answers).length) { toast("Choisissez une réponse."); return; }
      send(true, answers);
    } });
    card.append(el("div", { class: "perm-actions" }, ok, deny, reason));
    return card;
  }

  if (request.tool_name === "ExitPlanMode") {
    card.append(...heading("L'agent propose un plan"));
    const plan = el("div", { class: "perm-plan msg-body" });
    plan.innerHTML = renderMarkdown(input.plan || "");
    card.append(plan);
    card.append(el("div", { class: "perm-actions" },
      el("button", { class: "btn btn-ok", type: "button", text: "Valider le plan", onclick: () => send(true) }),
      deny, reason));
    return card;
  }

  card.append(...heading(`L'agent demande à utiliser « ${request.tool_name} »`));
  card.append(...describeTool(request.tool_name, input));
  card.append(el("div", { class: "perm-actions" },
    el("button", { class: "btn btn-ok", type: "button", text: "Autoriser", onclick: () => send(true) }),
    deny, reason));
  return card;
}

function describeTool(tool, input) {
  const parts = [];
  if (input.description) parts.push(el("p", { text: input.description }));
  // Codex may change several files in one go (file_paths); Claude Code names one.
  const files = Array.isArray(input.file_paths) && input.file_paths.length ? input.file_paths
    : (input.file_path ? [input.file_path] : []);
  if (files.length) parts.push(el("p", {}, files.length > 1 ? "Fichiers : " : "Fichier : ",
    ...files.flatMap((f, i) => [i ? ", " : "", el("code", { text: f })])));
  if (input.command) parts.push(el("pre", { text: input.command }));
  else if (typeof input.content === "string") parts.push(el("pre", { text: input.content }));
  else if (typeof input.new_string === "string") {
    parts.push(el("p", { text: "Remplacer :" }), el("pre", { text: input.old_string || "" }),
      el("p", { text: "par :" }), el("pre", { text: input.new_string }));
  } else if (!input.file_path) parts.push(el("pre", { text: JSON.stringify(input, null, 2) }));
  return parts;
}

// --- dialogs ------------------------------------------------------------------------

// The answer comes from the buttons and the Escape key themselves, not from
// the dialog's "close" event: some browsers do not fire it while the page is
// not displayed (a phone in standby, a window behind another).
function confirmDialog({ title, text, okText = "Confirmer", check = null }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    const ok = $("confirm-ok");
    const box = $("confirm-check");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    ok.textContent = okText;
    $("confirm-check-wrap").classList.toggle("hidden", !check);
    box.checked = false;
    $("confirm-check-text").textContent = check || "";
    ok.disabled = Boolean(check);
    box.onchange = () => { ok.disabled = !box.checked; };
    const finish = (answer) => {
      ok.onclick = $("confirm-cancel").onclick = dialog.onkeydown = null;
      if (dialog.open) dialog.close();
      resolve(answer);
    };
    ok.onclick = () => finish(true);
    $("confirm-cancel").onclick = () => finish(false);
    dialog.onkeydown = (event) => {
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    };
    dialog.showModal();
  });
}

let openNewDialog = () => {};      // set by setupNewDialog
let openProjectDialog = () => {};  // set by setupProjectDialog

// --- projects ---------------------------------------------------------------------------

function setupProjectDialog() {
  const dialog = $("project-dialog");
  let editing = null;  // the project being edited, null for a new one
  openProjectDialog = (project = null) => {
    editing = project;
    $("project-form").reset();
    $("project-dialog-title").textContent = project ? `Projet « ${project.name} »` : "Nouveau projet";
    $("project-name").value = project ? project.name : "";
    $("project-description").value = project ? project.description || "" : "";
    $("project-workdir").value = project ? project.workdir || "" : "";
    $("project-delete").classList.toggle("hidden", !project);
    $("project-delete-all").classList.toggle("hidden", !project || !project.conversations);
    $("project-share").classList.toggle("hidden", !project || !state.me);
    $("project-error").textContent = "";
    dialog.showModal();
  };
  $("project-share").addEventListener("click", () => {
    if (!editing) return;
    dialog.close();
    openShareDialog({ project_id: editing.id }, `le projet « ${editing.name} »`);
  });
  $("new-project").addEventListener("click", () => openProjectDialog(null));
  $("project-cancel").addEventListener("click", () => dialog.close());
  $("project-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { name: $("project-name").value.trim(), description: $("project-description").value,
      workdir: $("project-workdir").value.trim() };
    try {
      if (editing) await api("PATCH", `/api/projects/${editing.id}`, body);
      else await api("POST", "/api/projects", body);
      dialog.close();
      await loadConversations();
      if (state.data) renderConversation();
    } catch (err) {
      $("project-error").textContent = err.message;
    }
  });
  const remove = async (withConversations) => {
    const project = editing;
    dialog.close();
    const ok = await confirmDialog(withConversations ? {
      title: "Supprimer le projet et ses conversations ?",
      text: `« ${project.name} » et ses ${project.conversations} conversation(s) seront effacés définitivement, `
        + "des sauvegardes aussi. Cette action ne peut pas être annulée.",
      okText: "Tout supprimer définitivement",
      check: "Je comprends que ces conversations seront perdues",
    } : {
      title: "Supprimer le projet ?",
      text: `Le projet « ${project.name} » disparaît ; ses conversations restent, sans projet.`,
      okText: "Supprimer le projet",
    });
    if (!ok) return;
    try {
      const result = await api("DELETE", `/api/projects/${project.id}?with_conversations=${withConversations}`);
      if (result.conversations.includes(state.currentId)) closeConversation();
      await loadConversations();
      if (state.data) await refreshConversation();
      const failed = Object.keys(result.backup_errors || {});
      toast(failed.length ? `Projet supprimé, mais pas de ces sauvegardes : ${failed.join(", ")}.` : "Projet supprimé.");
    } catch (err) {
      toast(err.message);
    }
  };
  $("project-delete").addEventListener("click", () => remove(false));
  $("project-delete-all").addEventListener("click", () => remove(true));
}

function setupProjectActions() {
  $("conv-project").addEventListener("change", async (event) => {
    const value = event.target.value;
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`,
        value ? { project_id: Number(value) } : { leave_project: true });
      await loadConversations();
      await refreshConversation();
    } catch (err) {
      toast(err.message);
      renderConversation();
    }
  });
  $("conv-shared").addEventListener("click", async () => {
    const shared = !state.data.conversation.shared;
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, { shared });
      await loadConversations();
      await refreshConversation();
      toast(shared ? "Partagée : elle lit les autres conversations du projet et peut être lue."
        : "Privée : elle ne lit plus les autres conversations du projet, et elles ne la lisent plus.");
    } catch (err) {
      toast(err.message);
    }
  });
  const citeDialog = $("cite-dialog");
  $("cite").addEventListener("click", () => {
    const conv = state.data.conversation;
    const others = state.conversations.filter((c) => c.project_id === conv.project_id && c.id !== conv.id);
    $("cite-list").replaceChildren(...others.map((c) => el("button", {
      class: "conv-item", type: "button", disabled: !c.shared,
      title: c.shared ? "" : "Partage coupé : l'IA ne peut pas la lire",
      onclick: () => {
        const box = $("prompt");
        const text = `la conversation #${c.id} « ${c.title} »`;
        const at = box.selectionStart ?? box.value.length;
        box.value = box.value.slice(0, at) + text + box.value.slice(box.selectionEnd ?? at);
        citeDialog.close();
        box.focus();
        box.selectionStart = box.selectionEnd = at + text.length;
      },
    }, el("span", { class: "conv-item-title", text: `#${c.id} ${c.title}` }),
       el("span", { class: "conv-item-sub", text: c.shared ? formatDate(c.updated_at) : "privée : illisible" }))));
    citeDialog.showModal();
  });
  $("cite-close").addEventListener("click", () => citeDialog.close());
}

function setupNewDialog() {
  const dialog = $("new-dialog");
  const form = $("new-form");
  const modeSelect = $("new-mode");
  const showWarning = () => {
    const bypass = modeSelect.value === "bypass";
    $("new-bypass-warning").classList.toggle("hidden", !bypass);
    $("new-bypass-text").textContent = state.options.bypass_warning;
  };
  modeSelect.addEventListener("change", showWarning);
  const showKind = () => {
    const orchestration = $("new-kind").value === "orchestration";
    $("new-model-wrap").classList.toggle("hidden", orchestration);
    $("new-conductor-wrap").classList.toggle("hidden", !orchestration);
    $("new-assignment-wrap").classList.toggle("hidden", !orchestration);
  };
  $("new-kind").addEventListener("change", showKind);
  // In a project with a folder, the folder may stay empty: the project's is used.
  const showFolder = () => {
    const project = state.projects.find((p) => String(p.id) === $("new-project-select").value);
    $("new-workdir").placeholder = project && project.workdir
      ? `${project.workdir} (dossier du projet)` : "D:\\projets\\mon-projet";
  };
  $("new-project-select").addEventListener("change", showFolder);
  openNewDialog = (projectId = null) => {
    form.reset();
    $("new-project-select").replaceChildren(...projectOptions(projectId, "Aucun projet"));
    $("new-error").textContent = "";
    showWarning();
    showKind();
    showFolder();
    dialog.showModal();
  };
  $("new-conversation").addEventListener("click", () => openNewDialog(null));
  form.addEventListener("submit", async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    const data = new FormData(form);
    const body = {
      title: data.get("title").trim(),
      workdir: data.get("workdir").trim(),
      project_id: data.get("project_id") ? Number(data.get("project_id")) : null,
      model: data.get("model"),
      mode: data.get("mode"),
      conductor_model: data.get("conductor_model"),
      assignment_mode: data.get("mode") === "orchestration" ? data.get("assignment_mode") : "manual",
      permission_mode: data.get("permission_mode"),
      confirm_bypass: data.get("confirm_bypass") === "on",
    };
    if (body.permission_mode === "bypass" && !body.confirm_bypass) {
      $("new-error").textContent = "Cochez la case pour confirmer le mode sans permission.";
      return;
    }
    try {
      const { conversation } = await api("POST", "/api/conversations", body);
      dialog.close();
      await loadConversations();
      await openConversation(conversation.id);
      $("prompt").focus();
    } catch (err) {
      $("new-error").textContent = err.message;
    }
  });
}

// --- Obsidian project note ------------------------------------------------------------------

function setupObsidianDialog() {
  const dialog = $("obsidian-dialog");
  const refresh = () => {
    const conv = state.data.conversation;
    $("obsidian-vault").textContent = (state.options.obsidian || {}).vault || "";
    $("obsidian-state").textContent = conv.obsidian_note
      ? `Note activée : ${conv.obsidian_note}`
      : "La note n'est pas activée pour cette conversation.";
    $("obsidian-path").value = conv.obsidian_note || `Projets AI Hub/${conv.title.replace(/[<>:"/\\|?*]/g, "-")}.md`;
    $("obsidian-path").disabled = Boolean(conv.obsidian_note);
    $("obsidian-on").classList.toggle("hidden", Boolean(conv.obsidian_note));
    $("obsidian-off").classList.toggle("hidden", !conv.obsidian_note);
    $("obsidian-rewrite").classList.toggle("hidden", !conv.obsidian_note);
    $("obsidian-error").textContent = "";
  };
  const change = async (body) => {
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, body);
      await refreshConversation();
      refresh();
    } catch (err) {
      $("obsidian-error").textContent = err.message;
    }
  };
  $("obsidian").addEventListener("click", () => { refresh(); dialog.showModal(); });
  $("obsidian-close").addEventListener("click", () => dialog.close());
  $("obsidian-on").addEventListener("click", () => change({ obsidian: true, obsidian_note: $("obsidian-path").value.trim() }));
  $("obsidian-off").addEventListener("click", () => change({ obsidian: false }));
  $("obsidian-rewrite").addEventListener("click", async () => {
    dialog.close();
    const ok = await confirmDialog({
      title: "Réécrire toute la note ?",
      text: "La note sera reconstruite : création, chaque plan exécuté et chaque réponse notée, dans l'ordre. "
        + "Ce que vous y avez écrit vous-même sera remplacé. L'ancienne version est d'abord sauvegardée "
        + "dans le dossier data/obsidian-sauvegardes de l'appli.",
      okText: "Réécrire",
      check: "J'ai compris : la note actuelle sera remplacée",
    });
    if (!ok) return;
    try {
      const r = await api("POST", `/api/conversations/${state.currentId}/obsidian/rewrite`);
      toast(r.backup ? `Note réécrite. Ancienne version : ${r.backup}` : "Note réécrite.");
    } catch (err) {
      toast(err.message);
    }
  });
}

// --- conversation actions ---------------------------------------------------------------

function setupActions() {
  $("composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const prompt = $("prompt").value.trim();
    if (!prompt || state.currentId === null) return;
    const kind = (event.submitter || primaryButton()).value || "plan";
    $("send").disabled = true;
    $("discuss").disabled = true;
    try {
      await api("POST", `/api/conversations/${state.currentId}/messages`, { prompt, kind });
      $("prompt").value = "";
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
      await refreshConversation();
    } catch (err) {
      toast(err.message);
      renderRunning();
    }
  });
  $("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) $("composer").requestSubmit(primaryButton());
  });
  $("stop").addEventListener("click", async () => {
    try {
      await api("POST", `/api/conversations/${state.currentId}/stop`);
    } catch (err) {
      toast(err.message);
    }
  });
  $("back").addEventListener("click", () => setView("list"));
  $("rename").addEventListener("click", async () => {
    const title = window.prompt("Nouveau titre :", state.data.conversation.title);
    if (!title || !title.trim()) return;
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, { title });
      await Promise.all([refreshConversation(), loadConversations()]);
    } catch (err) {
      toast(err.message);
    }
  });
  $("delete").addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Supprimer la conversation ?",
      text: `« ${state.data.conversation.title} » et tous ses messages seront effacés définitivement, des sauvegardes aussi. Cette action ne peut pas être annulée.`,
      okText: "Supprimer définitivement",
      check: "Je comprends que la suppression est définitive",
    });
    if (!ok) return;
    try {
      const result = await api("DELETE", `/api/conversations/${state.currentId}`);
      closeConversation();
      await loadConversations();
      const failed = Object.keys(result.backup_errors || {});
      toast(failed.length
        ? `Conversation supprimée, mais pas de ces sauvegardes : ${failed.join(", ")}.`
        : "Conversation supprimée.");
    } catch (err) {
      toast(err.message);
    }
  });
  $("conv-assignment").addEventListener("change", async (event) => {
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, { assignment_mode: event.target.value });
      await refreshConversation();
      toast(event.target.value === "auto"
        ? "Les prochains plans recevront leurs IA selon les benchmarks."
        : "Vous choisirez vous-même les IA des prochains plans.");
    } catch (err) {
      event.target.value = state.data.conversation.assignment_mode;
      toast(err.message);
    }
  });
  $("conv-model").addEventListener("change", async (event) => {
    const conv = state.data.conversation;
    const previous = conv.mode === "orchestration" ? conv.conductor_model : conv.model;
    try {
      const chosen = event.target.value;
      await api("PATCH", `/api/conversations/${state.currentId}`, { model: chosen });
      await Promise.all([refreshConversation(), loadConversations()]);
      // Another provider cannot resume the session: it starts from the memory (same rule as the server).
      const gpt = (m) => m === "codex" || String(m).startsWith("gpt-");
      toast(gpt(chosen) !== gpt(previous)
        ? "IA changée : la prochaine demande repartira de la mémoire de la conversation."
        : "IA changée : la conversation continue dans la même session.");
    } catch (err) {
      event.target.value = previous;
      toast(err.message);
    }
  });
  $("conv-mode").addEventListener("change", async (event) => {
    const mode = event.target.value;
    const previous = state.data.conversation.permission_mode;
    let confirm = false;
    if (mode === "bypass") {
      confirm = await confirmDialog({
        title: "Activer le mode sans permission ?",
        text: state.options.bypass_warning,
        okText: "Activer",
        check: "J'ai compris, j'active ce mode",
      });
      if (!confirm) { event.target.value = previous; return; }
    }
    try {
      await api("PATCH", `/api/conversations/${state.currentId}`, { permission_mode: mode, confirm_bypass: confirm });
      await Promise.all([refreshConversation(), loadConversations()]);
    } catch (err) {
      event.target.value = previous;
      toast(err.message);
    }
  });
}

// --- login ---------------------------------------------------------------------------

let loginDone = null;  // resolves when the user is logged in

function showLogin(text) {
  if (state.events) { state.events.close(); state.events = null; }
  $("login-text").textContent = text || "Entrez votre mot de passe.";
  $("login-error").textContent = "";
  $("login").classList.remove("hidden");
  $("login-password").value = "";
  $("login-password").focus();
}

function setupLogin() {
  let unlockTimer = null;
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-submit").disabled = true;
    try {
      await api("POST", "/api/auth/login", { name: $("login-name").value.trim(), password: $("login-password").value });
      $("login").classList.add("hidden");
      $("login-password").value = "";
      if (loginDone) { loginDone(); loginDone = null; }
      else location.reload();  // a session that expired while the page was open
    } catch (err) {
      $("login-error").textContent = err.message;
      $("login-password").select();
      if (err.code === "locked") {
        // Nothing to try before the lockout ends; the button comes back by itself.
        $("login").classList.add("locked");
        clearTimeout(unlockTimer);
        unlockTimer = setTimeout(() => { $("login").classList.remove("locked"); $("login-submit").disabled = false; },
          (err.retryAfter || 60) * 1000);
        return;
      }
    }
    $("login-submit").disabled = false;
  });
  $("logout").addEventListener("click", async () => {
    try { await api("POST", "/api/auth/logout"); } catch (err) { /* already out */ }
    location.reload();
  });
}

// --- memory: search and fresh start ---------------------------------------------------------

const TOKENS_PER_CHAR = 1 / 3.5;  // the server's estimate (French text), shown as such
const estimate = (text) => Math.ceil((text || "").length * TOKENS_PER_CHAR);

function setupSearch() {
  let timer = null;
  const box = $("search");
  box.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 250);
  });
  box.addEventListener("keydown", (e) => { if (e.key === "Escape") { box.value = ""; renderList(); } });
}

async function runSearch() {
  const text = $("search").value.trim();
  if (text.length < 3) { renderList(); return; }
  let results;
  try { ({ results } = await api("GET", `/api/search?q=${encodeURIComponent(text)}`)); } catch (err) { toast(err.message); return; }
  const list = $("conversation-list");
  list.replaceChildren(el("div", { class: "project-loose", text: `${results.length} résultat(s)` }));
  if (!results.length) list.append(el("p", { class: "list-empty", text: "Rien trouvé." }));
  for (const r of results) {
    const excerpt = el("span", { class: "conv-item-sub found-excerpt" });
    // [[ ]] mark the words found; the text itself is never read as HTML.
    for (const [i, piece] of r.excerpt.split(/\[\[|\]\]/).entries()) {
      excerpt.append(i % 2 ? el("mark", { text: piece }) : document.createTextNode(piece));
    }
    list.append(el("button", { class: "conv-item", type: "button",
      onclick: () => { $("search").value = ""; openConversation(r.conversation_id, r.message_id); } },
      el("span", { class: "conv-item-title", text: r.title + (r.by_meaning ? " · proche par le sens" : "") }),
      excerpt));
  }
}

function renderFresh(conv) {
  let pending = null;
  try { pending = conv.fresh_package ? JSON.parse(conv.fresh_package) : null; } catch (err) { pending = null; }
  $("fresh-banner").classList.toggle("hidden", !pending);
  $("fresh").classList.toggle("on", Boolean(pending));
  if (pending) {
    $("fresh-text").textContent = `Prochaine demande : nouvelle session, avec un paquet d'environ ${pending.tokens} tokens `
      + `(plafond ${pending.budget}) au lieu de tout l'historique, plus les passages utiles.`;
  }
}

function setupFreshStart() {
  const dialog = $("rag-dialog");
  let current = null;  // the package being looked at
  const send = async (body) => {
    $("rag-error").textContent = "";
    $("fresh").disabled = true;
    const was = $("fresh").textContent;
    $("fresh").textContent = "Préparation du résumé…";
    try {
      const r = await api("POST", `/api/conversations/${state.currentId}/fresh-start`, body);
      current = r.package;
      if (r.ready) {
        if (dialog.open) dialog.close();
        await refreshConversation();
        toast("La prochaine demande partira d'une nouvelle session.");
      } else {
        showOver(r);
      }
    } catch (err) {
      if (dialog.open) $("rag-error").textContent = err.message; else toast(err.message);
    } finally {
      $("fresh").disabled = false;
      $("fresh").textContent = was;
    }
  };
  const showOver = (r) => {
    $("rag-title").textContent = "Le paquet de départ dépasse le plafond";
    $("rag-text").textContent = `Résumé et derniers échanges : environ ${r.package.tokens} tokens, pour un plafond de `
      + `${r.budget}. Rien n'est envoyé tant que vous n'avez pas choisi.`;
    $("rag-budget").value = Math.ceil(r.package.tokens / 500) * 500 + 500;
    $("rag-remember").checked = false;
    $("rag-choices").classList.remove("hidden");
    $("rag-editor").classList.add("hidden");
    $("rag-send").classList.add("hidden");
    if (!dialog.open) dialog.showModal();
  };
  const count = () => {
    const pkg = readEditor();
    const used = estimate(pkg.summary) + pkg.exchanges.reduce((n, e) => n + estimate(e.content) + 4, 0) + 60;
    const over = used > current.budget;
    $("rag-count").textContent = `Environ ${used} tokens pour un plafond de ${current.budget}` + (over ? " : au-dessus." : ".");
    $("rag-count").classList.toggle("over", over);
    $("rag-send").textContent = over ? "Envoyer quand même" : "Utiliser ce paquet";
    return over;
  };
  const readEditor = () => ({
    summary: $("rag-summary").value,
    exchanges: [...$("rag-exchanges").querySelectorAll(".rag-exchange")]
      .filter((row) => row.querySelector("input[type=checkbox]").checked)
      .map((row) => ({ role: row.dataset.role, content: row.querySelector("textarea").value,
        message_id: row.dataset.mid ? Number(row.dataset.mid) : null }))
      .filter((e) => e.content.trim()),
  });
  $("rag-raise").addEventListener("click", () => send({ decision: "raise", budget: Number($("rag-budget").value),
    remember: $("rag-remember").checked }));
  $("rag-auto").addEventListener("click", () => send({ decision: "auto" }));
  const openEditor = () => {
    $("rag-choices").classList.add("hidden");
    $("rag-editor").classList.remove("hidden");
    $("rag-send").classList.remove("hidden");
    $("rag-summary").value = current.summary || "";
    $("rag-exchanges").replaceChildren(...current.exchanges.map((e) => el("div", { class: "rag-exchange",
      "data-role": e.role, "data-mid": e.message_id ?? "" },
      el("label", { class: "check" }, el("input", { type: "checkbox", checked: true, onchange: count }),
        e.role === "user" ? " Votre demande" : " Réponse de l'IA"),
      el("textarea", { rows: 4, oninput: count }, e.content))));
    $("rag-summary").oninput = count;
    count();
  };
  $("rag-edit").addEventListener("click", openEditor);
  $("rag-send").addEventListener("click", () => {
    const over = count();
    send({ decision: "custom", package: readEditor(), allow_over: over });
  });
  $("rag-close").addEventListener("click", () => dialog.close());
  $("fresh").addEventListener("click", () => send({}));
  $("fresh-cancel").addEventListener("click", async () => {
    try { await api("DELETE", `/api/conversations/${state.currentId}/fresh-start`); await refreshConversation(); }
    catch (err) { toast(err.message); }
  });
  $("fresh-view").addEventListener("click", () => {
    const conv = state.data.conversation;
    try { current = JSON.parse(conv.fresh_package); } catch (err) { return; }
    $("rag-title").textContent = "Paquet de la prochaine demande";
    $("rag-text").textContent = `Environ ${current.tokens} tokens (plafond ${current.budget}). Les passages liés à `
      + "votre demande s'y ajouteront, dans ce plafond.";
    openEditor();
    dialog.showModal();
  });
}

// --- phone alerts -----------------------------------------------------------------

// Same order and names as aihub/notify.py (EVENTS).
const ALERTS = { permission: "Une IA attend mon accord", done: "Réponse prête", error: "Une requête a échoué",
  plan: "Plan proposé, à valider", plan_done: "Plan terminé", autonomous_done: "Travail autonome terminé",
  scheduled: "Tâche planifiée terminée", quota: "Quota bientôt atteint" };

function alertChecks(chosen) {
  return Object.entries(ALERTS).map(([value, label]) => el("label", { class: "check" },
    el("input", { type: "checkbox", value, checked: chosen.includes(value) }), ` ${label}`));
}

function checkedAlerts(box) {
  return [...box.querySelectorAll("input:checked")].map((i) => i.value);
}

function base64ToBytes(text) {
  const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

// Each device turns its own alerts on, and chooses them.
function setupPushDialog() {
  const dialog = $("push-dialog");
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  let current = null;  // this browser's PushSubscription
  async function refresh() {
    $("push-error").textContent = "";
    if (!supported || !window.isSecureContext) {
      $("push-status").textContent = !window.isSecureContext
        ? "Les notifications demandent l'adresse https de l'appli (celle de Tailscale) : elles ne marchent pas sur une adresse http d'un autre appareil."
        : "Ce navigateur ne sait pas recevoir de notifications. Sur iPhone : installez d'abord l'appli sur l'écran d'accueil (iOS 16.4 ou plus), puis ouvrez-la de là.";
      $("push-events").replaceChildren();
      for (const id of ["push-on", "push-off", "push-test"]) $(id).classList.add("hidden");
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    current = await registration.pushManager.getSubscription();
    let saved = null;
    if (current) saved = (await api("POST", "/api/push/state", { endpoint: current.endpoint })).subscription;
    const on = Boolean(current && saved);
    $("push-status").textContent = on
      ? "Activées sur cet appareil. Cochez les alertes voulues, puis « Enregistrer »."
      : Notification.permission === "denied"
        ? "Les notifications sont bloquées pour ce site dans le navigateur : autorisez-les dans ses réglages, puis revenez."
        : "Désactivées sur cet appareil. Choisissez les alertes, puis « Activer » : le navigateur vous demandera l'autorisation.";
    $("push-events").replaceChildren(...alertChecks(saved ? saved.events : Object.keys(ALERTS)));
    $("push-on").textContent = on ? "Enregistrer" : "Activer";
    $("push-off").classList.toggle("hidden", !on);
    $("push-test").classList.toggle("hidden", !on);
    $("push-on").classList.remove("hidden");
  }
  $("notifications").addEventListener("click", async () => {
    dialog.showModal();
    try { await refresh(); } catch (err) { $("push-error").textContent = err.message; }
  });
  $("push-close").addEventListener("click", () => dialog.close());
  $("push-on").addEventListener("click", async () => {
    $("push-error").textContent = "";
    try {
      if ((await Notification.requestPermission()) !== "granted") {
        throw new Error("Le navigateur n'a pas reçu l'autorisation d'afficher des notifications.");
      }
      const registration = await navigator.serviceWorker.ready;
      let sub = await registration.pushManager.getSubscription();
      if (!sub) {
        const { public_key } = await api("GET", "/api/push/key");
        sub = await registration.pushManager.subscribe({ userVisibleOnly: true,
          applicationServerKey: base64ToBytes(public_key) });
      }
      await api("POST", "/api/push/subscription", { subscription: sub.toJSON(), events: checkedAlerts($("push-events")) });
      toast("Notifications enregistrées pour cet appareil.");
      await refresh();
    } catch (err) {
      $("push-error").textContent = err.message;
    }
  });
  $("push-off").addEventListener("click", async () => {
    try {
      if (current) {
        await api("POST", "/api/push/unsubscribe", { endpoint: current.endpoint });
        await current.unsubscribe();
      }
      toast("Notifications désactivées sur cet appareil.");
      await refresh();
    } catch (err) {
      $("push-error").textContent = err.message;
    }
  });
  $("push-test").addEventListener("click", async () => {
    try { await api("POST", "/api/push/test"); toast("Notification d'essai envoyée."); }
    catch (err) { $("push-error").textContent = err.message; }
  });
  // A tap on an alert while the app is open: go to its conversation.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (event) => {
      const cid = event.data && event.data.type === "open-conversation" ? Number(event.data.conversation_id) : 0;
      if (cid) openConversation(cid);
    });
  }
}

// --- settings ---------------------------------------------------------------------

const SETTING_NUMBERS = ["backup_keep", "quota_warning", "idle_days", "max_failures", "lock_minutes",
  "rag_budget", "rag_exchanges"];
const SETTING_BOOLS = ["backup_enabled", "keep_awake"];

function setupSettingsDialog() {
  const dialog = $("settings-dialog");
  let loaded = null;  // the settings as the server sent them
  const fill = (data) => {
    loaded = data;
    const s = data.settings;
    $("settings-file").textContent = data.file
      ? `Fichier : ${data.file}. Rien d'un mot de passe n'y est écrit.` : "";
    $("set-allowed_roots").value = s.allowed_roots.join("\n");
    $("set-obsidian_vault").value = s.obsidian_vault || "";
    $("set-backup_dir").value = s.backup_dir || "";
    $("set-backup_dir").placeholder = data.backup_folder || "data\\sauvegardes";
    for (const name of SETTING_NUMBERS) $(`set-${name}`).value = s[name];
    for (const name of SETTING_BOOLS) $(`set-${name}`).checked = s[name];
    $("set-models").replaceChildren(...data.all_models.map((m) => el("label", { class: "check" },
      el("input", { type: "checkbox", value: m, checked: s.models.includes(m), onchange: syncDefaults }),
      ` ${MODEL_LABELS[m] || m}`)));
    syncDefaults();
    $("set-default_model").value = s.default_model;
    $("set-default_conductor").value = s.default_conductor;
    $("set-summary_model").value = s.summary_model;
    $("set-ntfy_url").value = s.ntfy_url || "";
    $("set-ntfy_events").replaceChildren(...alertChecks(s.ntfy_events));
    $("set-ntfy_detail").value = s.ntfy_detail;
    $("set-tool_servers").value = s.tool_servers.length ? JSON.stringify(s.tool_servers, null, 2) : "";
    $("settings-password-wrap").classList.toggle("hidden", !data.password_needed);
    $("settings-password").value = "";
    const fixed = data.overridden.map((n) => data.labels[n] || n);
    $("settings-overridden").classList.toggle("hidden", !fixed.length);
    $("settings-overridden").textContent = fixed.length
      ? `Fixés par une option au lancement du serveur : ${fixed.join(", ")}. Une modification ici s'applique tout de suite et est enregistrée, mais l'option l'emportera au prochain lancement.`
      : "";
    $("settings-error").textContent = "";
  };
  function chosenModels() {
    return [...$("set-models").querySelectorAll("input:checked")].map((i) => i.value);
  }
  function syncDefaults() {
    const models = chosenModels();
    for (const id of ["set-default_model", "set-default_conductor"]) {
      const keep = $(id).value;
      $(id).replaceChildren(...models.map((m) => el("option", { value: m, text: MODEL_LABELS[m] || m })));
      if (models.includes(keep)) $(id).value = keep;
    }
  }
  const read = () => ({
    allowed_roots: $("set-allowed_roots").value.split("\n").map((l) => l.trim()).filter(Boolean),
    obsidian_vault: $("set-obsidian_vault").value.trim() || null,
    backup_dir: $("set-backup_dir").value.trim() || null,
    models: chosenModels(),
    default_model: $("set-default_model").value,
    default_conductor: $("set-default_conductor").value,
    summary_model: $("set-summary_model").value,
    ntfy_url: $("set-ntfy_url").value.trim() || null,
    ntfy_events: checkedAlerts($("set-ntfy_events")),
    ntfy_detail: $("set-ntfy_detail").value,
    ...Object.fromEntries(SETTING_NUMBERS.map((n) => [n, Number($(`set-${n}`).value)])),
    ...Object.fromEntries(SETTING_BOOLS.map((n) => [n, $(`set-${n}`).checked])),
  });
  $("settings").addEventListener("click", async () => {
    try { fill(await api("GET", "/api/config")); dialog.showModal(); } catch (err) { toast(err.message); }
  });
  $("settings-cancel").addEventListener("click", () => dialog.close());
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    let now;
    try {
      now = read();
      const raw = $("set-tool_servers").value.trim();
      now.tool_servers = raw ? JSON.parse(raw) : [];
    } catch (err) {
      $("settings-error").textContent = `Serveurs d'outils : JSON illisible (${err.message})`;
      return;
    }
    // Only what changed: the rest stays as it is in the file.
    const changes = Object.fromEntries(Object.entries(now).filter(
      ([name, value]) => JSON.stringify(value) !== JSON.stringify(loaded.settings[name])));
    if (!Object.keys(changes).length) { dialog.close(); return; }
    $("settings-save").disabled = true;
    try {
      fill(await api("PATCH", "/api/config", { changes, password: $("settings-password").value }));
      dialog.close();
      state.options = await api("GET", "/api/options");
      applyOptions();
      if (state.data) renderConversation();
      toast("Réglages enregistrés et appliqués.");
    } catch (err) {
      $("settings-error").textContent = err.message;
    } finally {
      $("settings-save").disabled = false;
    }
  });
}

// The model lists and defaults of the creation window follow the settings.
function applyOptions() {
  const models = state.options.models;
  $("new-model").replaceChildren(...models.map((m) => el("option", { value: m, text: MODEL_LABELS[m] || m,
    selected: m === state.options.default_model })));
  $("new-conductor").replaceChildren(...models.map((m) => el("option", { value: m, text: MODEL_LABELS[m] || m,
    selected: m === state.options.default_conductor })));
  $("settings").classList.toggle("hidden", !state.options.settings_editable);
  $("version").textContent = state.options.version ? `v${state.options.version}` : "";
}

// A readable name for a browser's user agent: enough to tell the phone from the PC.
function deviceName(agent) {
  const ua = agent || "";
  const os = /iPhone|iPad/.test(ua) ? "iPhone / iPad" : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "appareil inconnu";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "navigateur";
  return `${browser} · ${os}`;
}

function setupDevicesDialog() {
  const dialog = $("devices-dialog");
  const render = async () => {
    $("devices-error").textContent = "";
    let data;
    try { data = await api("GET", "/api/sessions"); } catch (err) { $("devices-error").textContent = err.message; return; }
    $("devices-list").replaceChildren(...data.sessions.map((s) => el("div", { class: "device" + (s.current ? " current" : "") },
      el("div", { class: "device-text" },
        el("span", { class: "conv-item-title", text: deviceName(s.user_agent) + (s.current ? " — cet appareil" : "") }),
        el("span", { class: "conv-item-sub", text: `${s.client || "?"} · vu le ${formatDate(s.last_seen_at)} · ouverte le ${formatDate(s.created_at)}` })),
      el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Déconnecter",
        onclick: async () => {
          try {
            await api("DELETE", `/api/sessions/${s.id}`);
            if (s.current) { location.reload(); return; }
            await render();
          } catch (err) { $("devices-error").textContent = err.message; }
        } }))));
    $("devices-others").disabled = data.sessions.filter((s) => !s.current).length === 0;
  };
  $("devices").addEventListener("click", async () => { await render(); dialog.showModal(); });
  $("devices-close").addEventListener("click", () => dialog.close());
  $("devices-others").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/sessions/close-others");
      toast(`${r.closed} autre(s) appareil(s) déconnecté(s).`);
      await render();
    } catch (err) { $("devices-error").textContent = err.message; }
  });
}

async function ensureLoggedIn() {
  let session;
  try {
    session = await api("GET", "/api/auth/session");
  } catch (err) {
    // Either the PC answered with a refusal (err.status), or the page came
    // from the phone's cache and the PC does not answer at all.
    showLogin(err.status ? err.message
      : "Le PC ne répond pas : il est éteint, ou Tailscale est coupé. Réessayez plus tard.");
    $("login-submit").disabled = true;
    await new Promise(() => {});  // nothing else can work
  }
  $("account-actions").classList.toggle("hidden", !session.required);
  const invitation = new URLSearchParams(location.search).get("invitation");
  if (!session.logged_in) {
    if (invitation) showInvitation(invitation);
    else showLogin();
    await new Promise((resolve) => { loginDone = resolve; });
    session = await api("GET", "/api/auth/session");
  } else if (invitation) {
    history.replaceState(null, "", "/");  // already in: the link is of no use here
  }
  state.me = session.user;
  if (state.me) {
    $("account-box").classList.remove("hidden");
    $("me-name").textContent = `Connecté : ${state.me.name}`;
    $("people").classList.toggle("hidden", !state.me.is_owner);
    $("my-accounts").classList.toggle("hidden", state.me.is_owner);
  }
  // What is the owner's alone: hidden for the others (the server refuses it anyway).
  for (const id of ["new-conversation", "new-project"]) $(id).classList.toggle("hidden", !isOwner());
}

function showInvitation(token) {
  $("login").classList.remove("hidden");
  $("login-form").classList.add("hidden");
  $("invite-form").classList.remove("hidden");
  $("invite-name").focus();
  $("invite-form").onsubmit = async (event) => {
    event.preventDefault();
    $("invite-error").textContent = "";
    if ($("invite-password").value !== $("invite-password2").value) {
      $("invite-error").textContent = "Les deux mots de passe ne sont pas les mêmes.";
      return;
    }
    $("invite-submit").disabled = true;
    try {
      await api("POST", "/api/invitations/accept", { invitation: token, name: $("invite-name").value.trim(),
        password: $("invite-password").value });
      history.replaceState(null, "", "/");
      $("login").classList.add("hidden");
      $("invite-form").classList.add("hidden");
      $("login-form").classList.remove("hidden");
      if (loginDone) { loginDone(); loginDone = null; }
      toast("Bienvenue ! Connectez vos comptes d'IA dans « Mes comptes IA ».");
    } catch (err) {
      $("invite-error").textContent = err.message;
    } finally {
      $("invite-submit").disabled = false;
    }
  };
}

// --- people, sharing, own AI accounts (version 0.2) -----------------------------------

const ROLE_LABELS = { read: "Lecture", write: "Lecture et écriture" };

function setupPeopleDialog() {
  const dialog = $("people-dialog");
  const error = (err) => { $("people-error").textContent = err.message; };
  const render = async () => {
    $("people-error").textContent = "";
    let data;
    try { data = await api("GET", "/api/users"); } catch (err) { error(err); return; }
    $("people-list").replaceChildren(...data.users.map((u) => el("div", { class: "device" },
      el("div", { class: "device-text" },
        el("span", { class: "conv-item-title", text: u.name + (u.is_owner ? " — vous" : "") + (u.disabled ? " (désactivée)" : "") }),
        el("span", { class: "conv-item-sub", text: u.is_owner ? "propriétaire du serveur (ou nom laissé vide à la connexion)" : `depuis le ${formatDate(u.created_at)}` })),
      el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Renommer",
        title: "Le nom pour se connecter",
        onclick: async () => {
          const name = prompt(`Nouveau nom pour se connecter (au lieu de « ${u.name} ») :`, u.name);
          if (!name || name.trim() === u.name) return;
          try {
            const r = await api("PATCH", `/api/users/${u.id}`, { name: name.trim() });
            if (u.is_owner) { state.me.name = r.user.name; $("me-name").textContent = `Connecté : ${r.user.name}`; }
            toast(`Nom changé : « ${r.user.name} ».`);
            await render();
          } catch (err) { error(err); }
        } }),
      u.is_owner ? null : el("button", { class: "btn btn-ghost btn-small", type: "button",
        text: u.disabled ? "Réactiver" : "Désactiver",
        title: u.disabled ? "Elle pourra se reconnecter" : "Elle est déconnectée tout de suite et ne peut plus entrer",
        onclick: async () => { try { await api("PATCH", `/api/users/${u.id}`, { disabled: !u.disabled }); await render(); } catch (err) { error(err); } } }),
      u.is_owner ? null : el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Supprimer",
        onclick: async () => {
          const ok = await confirmDialog({ title: `Supprimer ${u.name} ?`,
            text: "Son accès, ses partages et ses comptes d'IA enregistrés ici sont effacés. Ses messages restent dans les conversations.",
            okText: "Supprimer" });
          if (!ok) return;
          try { await api("DELETE", `/api/users/${u.id}`); await render(); } catch (err) { error(err); }
        } }))));
    const waiting = data.invitations.filter((i) => !i.used_at);
    $("invitation-list").replaceChildren(...waiting.map((i) => el("div", { class: "device" },
      el("div", { class: "device-text" },
        el("span", { class: "conv-item-title", text: `Invitation${i.note ? " · " + i.note : ""}` }),
        el("span", { class: "conv-item-sub", text: `valable jusqu'au ${formatDate(i.expires_at)}` })),
      el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Annuler",
        onclick: async () => { try { await api("DELETE", `/api/invitations/${i.id}`); await render(); } catch (err) { error(err); } } }))));
  };
  $("people").addEventListener("click", async () => {
    $("invite-link-box").classList.add("hidden");
    await render();
    dialog.showModal();
  });
  $("people-close").addEventListener("click", () => dialog.close());
  $("new-user-make").addEventListener("click", async () => {
    $("people-error").textContent = "";
    try {
      const r = await api("POST", "/api/users", { name: $("new-user-name").value.trim(), password: $("new-user-password").value });
      $("new-user-name").value = "";
      $("new-user-password").value = "";
      toast(`Compte « ${r.user.name} » créé : donnez-lui ce nom et ce mot de passe.`);
      await render();
    } catch (err) { error(err); }
  });
  $("invite-make").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/invitations", { note: $("invite-note").value.trim() || null });
      $("invite-link").value = r.link;
      $("invite-link-box").classList.remove("hidden");
      $("invite-note").value = "";
      await render();
    } catch (err) { error(err); }
  });
  $("invite-copy").addEventListener("click", async () => {
    $("invite-link").select();
    try { await navigator.clipboard.writeText($("invite-link").value); toast("Lien copié."); }
    catch { document.execCommand("copy"); toast("Lien copié."); }
  });
}

// Share a conversation ({conversation_id}) or a whole project ({project_id}) with people.
async function openShareDialog(target, title) {
  const dialog = $("share-dialog");
  const error = (err) => { $("share-error").textContent = err.message; };
  $("share-title").textContent = `Partager ${title}`;
  $("share-text").textContent = target.project_id
    ? "Tout le projet : ses conversations, celles d'aujourd'hui et celles à venir."
    : "Cette conversation seulement. « Lecture et écriture » : la personne peut envoyer des demandes, qui utilisent ses propres comptes d'IA.";
  const render = async () => {
    $("share-error").textContent = "";
    let people, shared;
    try {
      people = (await api("GET", "/api/users")).users.filter((u) => !u.is_owner);
      shared = target.project_id
        ? (await api("GET", `/api/projects/${target.project_id}`)).shared_with || []
        : (await api("GET", `/api/conversations/${target.conversation_id}`)).shared_with || [];
    } catch (err) { error(err); return; }
    if (!people.length) {
      $("share-list").replaceChildren(el("p", { class: "list-empty", text: "Personne à qui partager : invitez d'abord quelqu'un (bouton « Personnes »)." }));
      return;
    }
    $("share-list").replaceChildren(...people.map((u) => {
      const mine = shared.find((s) => s.user_id === u.id && (target.project_id || s.through === "conversation"));
      const viaProject = !target.project_id && shared.find((s) => s.user_id === u.id && s.through === "projet");
      const select = el("select", { "aria-label": `Accès de ${u.name}` },
        el("option", { value: "", text: "Pas d'accès", selected: !mine }),
        ...Object.entries(ROLE_LABELS).map(([v, t]) => el("option", { value: v, text: t, selected: mine && mine.role === v })));
      select.addEventListener("change", async () => {
        try {
          if (select.value) await api("POST", "/api/shares", { user_id: u.id, role: select.value, ...target });
          else await api("POST", "/api/shares/remove", { user_id: u.id, ...target });
          await render();
          if (state.data) await refreshConversation();
        } catch (err) { error(err); await render(); }
      });
      return el("div", { class: "device" },
        el("div", { class: "device-text" },
          el("span", { class: "conv-item-title", text: u.name + (u.disabled ? " (désactivée)" : "") }),
          viaProject ? el("span", { class: "conv-item-sub", text: `par le projet : ${ROLE_LABELS[viaProject.role]}` }) : null),
        select);
    }));
  };
  await render();
  dialog.showModal();
}

function setupShareDialog() {
  $("share-close").addEventListener("click", () => $("share-dialog").close());
  $("share").addEventListener("click", () => {
    const conv = state.data.conversation;
    openShareDialog({ conversation_id: conv.id }, `« ${conv.title} »`);
  });
}

function setupAccountsDialog() {
  const dialog = $("accounts-dialog");
  let timer = null;
  const error = (err) => { $("accounts-error").textContent = err ? err.message : ""; };
  const render = async () => {
    let data;
    try { data = await api("GET", "/api/accounts"); } catch (err) { error(err); return; }
    if (!data.own) { dialog.close(); return; }
    let waiting = false;
    $("accounts-list").replaceChildren(...Object.entries(data.providers).map(([provider, a]) => {
      const login = a.login && a.login.state === "waiting" ? a.login : null;
      if (login) waiting = true;
      const row = el("div", { class: "account" },
        el("div", { class: "device-text" },
          el("span", { class: "conv-item-title", text: a.name }),
          el("span", { class: "conv-item-sub", text: a.error ? a.error : a.signed_in ? "connecté : vos demandes l'utilisent" : "pas connecté" })));
      if (a.signed_in) {
        row.append(el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Se déconnecter",
          onclick: async () => { try { await api("POST", `/api/accounts/${provider}/logout`); await render(); } catch (err) { error(err); } } }));
      } else if (!login) {
        row.append(el("button", { class: "btn btn-primary btn-small", type: "button", text: "Se connecter",
          onclick: async (e) => {
            e.target.disabled = true;
            error(null);
            try {
              const view = await api("POST", `/api/accounts/${provider}/login`);
              if (view.state === "error") error(new Error(view.error));
            } catch (err) { error(err); }
            await render();
          } }));
      }
      if (a.login && a.login.state === "error") row.append(el("p", { class: "form-error", text: a.login.error }));
      if (login) {
        const steps = el("div", { class: "account-steps" },
          el("p", {}, "1. Ouvrez ", el("a", { href: login.url, target: "_blank", rel: "noopener noreferrer", text: "la page de connexion" }),
            " et connectez-vous avec votre compte."));
        if (login.code) {
          steps.append(el("p", {}, "2. Tapez ce code sur la page : ", el("strong", { class: "account-code", text: login.code })),
            el("p", { class: "quota-note", text: "Cette fenêtre se met à jour toute seule une fois fait." }));
        } else if (login.needs_code) {
          const input = el("input", { type: "text", placeholder: "Code affiché par la page", "aria-label": "Code de connexion",
            autocomplete: "off" });
          steps.append(el("p", { text: "2. La page affiche un code : copiez-le ici." }),
            el("div", { class: "share-add" }, input,
              el("button", { class: "btn btn-primary btn-small", type: "button", text: "Valider", onclick: async (e) => {
                e.target.disabled = true;
                try {
                  const view = await api("POST", `/api/accounts/${provider}/code`, { code: input.value.trim() });
                  if (view.state === "ok") toast(`${a.name} connecté.`);
                  else error(new Error(view.error || "connexion refusée"));
                } catch (err) { error(err); }
                await render();
              } })));
        }
        steps.append(el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Annuler",
          onclick: async () => { await api("POST", `/api/accounts/${provider}/cancel`).catch(() => {}); await render(); } }));
        row.append(steps);
      }
      return row;
    }));
    // A Codex sign-in ends on OpenAI's page: look again until it is done.
    clearTimeout(timer);
    if (waiting && dialog.open) timer = setTimeout(render, 3000);
  };
  $("my-accounts").addEventListener("click", async () => { error(null); dialog.showModal(); await render(); });
  $("accounts-close").addEventListener("click", () => { clearTimeout(timer); dialog.close(); loadQuota(true); });
}

function setupPasswordDialog() {
  const dialog = $("password-dialog");
  $("my-password").addEventListener("click", () => { $("password-form").reset(); $("password-error").textContent = ""; dialog.showModal(); });
  $("password-cancel").addEventListener("click", () => dialog.close());
  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", "/api/me/password", { current: $("password-current").value, new: $("password-new").value });
      dialog.close();
      toast("Mot de passe changé. Vos autres appareils sont déconnectés.");
    } catch (err) { $("password-error").textContent = err.message; }
  });
}

// --- start ---------------------------------------------------------------------------

async function main() {
  setupLogin();
  setupDevicesDialog();
  setupSettingsDialog();
  setupPushDialog();
  setupAutonomousDialog();
  setupScheduleDialog();
  setupToolsDialog();
  setupSearch();
  setupFreshStart();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* not installable here (plain http) */ });
  }
  await ensureLoggedIn();
  setupPeopleDialog();
  setupShareDialog();
  setupAccountsDialog();
  setupPasswordDialog();
  setupNewDialog();
  setupProjectDialog();
  setupProjectActions();
  setupActions();
  setupObsidianDialog();
  try {
    state.options = await api("GET", "/api/options");
  } catch (err) {
    toast(err.message);
  }
  applyOptions();
  await Promise.all([loadConversations(), loadStatus(), loadQuota(true), loadAgents()]);
  // Opened from an alert: straight to its conversation.
  const asked = Number(new URLSearchParams(location.search).get("conversation"));
  if (asked && state.conversations.some((c) => c.id === asked)) {
    history.replaceState(null, "", "/");
    await openConversation(asked);
  }
  // Fresh figures every 5 minutes while the page is displayed (reading them costs no quota).
  setInterval(() => { if (document.visibilityState === "visible") loadQuota(true); }, 5 * 60 * 1000);
}

main();
