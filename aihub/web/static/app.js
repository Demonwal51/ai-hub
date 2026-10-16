"use strict";

function applyAppearance(theme, density) {
  document.documentElement.dataset.theme = ["light", "dark"].includes(theme) ? theme : "system";
  document.documentElement.dataset.density = density === "compact" ? "compact" : "comfortable";
}
try { applyAppearance(localStorage.getItem("aihub-theme"), localStorage.getItem("aihub-density")); }
catch { applyAppearance("system", "comfortable"); }

// Simple by default (1.2.1): what is rarely needed (class « adv ») waits behind « Fonctions avancées »,
// a choice of this device. Nothing is removed: the features are still there, just not on screen.
function advancedOn() {
  try { return localStorage.getItem("aihub-avance") === "1"; } catch { return false; }
}
function applyAdvanced(on) {
  document.documentElement.dataset.advanced = on ? "on" : "off";
}
applyAdvanced(advancedOn());
// Shown on screen: neither hidden by the page nor kept for « Fonctions avancées ».
const shown = (node) => getComputedStyle(node).display !== "none";

// --- state -------------------------------------------------------------------

const SHOWN_FIRST = 80;   // messages loaded when a conversation opens
const SHOWN_MORE = 100;   // and each time « Afficher les précédents » is clicked
const SHOWN_MAX = 5000;   // the server's limit for « last »

const state = {
  conversations: [],
  projects: [],
  templates: [],
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
  shown: SHOWN_FIRST,        // how many of the last messages are loaded (null: all)
};

// A mouse and a keyboard (not a phone): Entrée sends the request.
const KEYBOARD = window.matchMedia("(pointer: fine)").matches;

// The owner of the server (or a server without accounts): settings, people, sharing.
const isOwner = () => !state.me || state.me.is_owner;

const $ = (id) => document.getElementById(id);
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
  const controller = new AbortController();
  const loginRequest = path === '/api/auth/session' || path === '/api/auth/login';
  const timeout = loginRequest ? setTimeout(() => controller.abort(), 15000) : null;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal });
  } catch (err) {
    if (controller.signal.aborted) throw new Error('Le serveur ne répond pas. Réessayez la connexion.');
    throw err;
  } finally {
    if (timeout !== null) clearTimeout(timeout);
  }
  // Served by this device's copy (« Hors connexion »), or by the PC again.
  if (res.headers.get("X-AIHub-Hors-Ligne") === "1") setOffline(true);
  else if (res.ok) setOffline(false);
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

// One run at a time of a reload: a call made while one runs asks for a
// single run more after it (the page then shows the latest state, once).
function coalesced(load) {
  let running = null;
  let again = false;
  const run = () => {
    if (running) { again = true; return running; }
    running = (async () => {
      try {
        do { again = false; await load(); } while (again);
      } finally {
        running = null;
      }
    })();
    return running;
  };
  return run;
}

// The composer grows with what is typed, up to its CSS limit.
function fitPrompt() {
  const box = $("prompt");
  box.style.height = "auto";
  box.style.height = `${box.scrollHeight + 2}px`;
}

// What was being typed in a conversation, kept when going to another one (this device only).
function saveDraft(id, text) {
  if (id === null) return;
  try {
    if (text) sessionStorage.setItem(`aihub-draft-${id}`, text);
    else sessionStorage.removeItem(`aihub-draft-${id}`);
  } catch { /* private mode: no draft kept */ }
}

function loadDraft(id) {
  try { return sessionStorage.getItem(`aihub-draft-${id}`) || ""; } catch { return ""; }
}

// The choices of the last new conversation, on this device only.
function rememberedChoices() {
  try { return JSON.parse(localStorage.getItem("aihub-new-choices") || "{}") || {}; } catch { return {}; }
}

function rememberChoices(choices) {
  try {
    const kept = Object.fromEntries(Object.entries({ ...rememberedChoices(), ...choices }).filter(([, v]) => v));
    localStorage.setItem("aihub-new-choices", JSON.stringify(kept));
  } catch { /* storage off: nothing remembered */ }
}

// Plain http (a phone on the local network): no clipboard API, the old way.
function fallbackCopy(text) {
  const area = el("textarea", { style: "position:fixed;opacity:0" });
  area.value = text;
  document.body.append(area);
  area.select();
  document.execCommand("copy");
  area.remove();
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
  return AIHubMarkdown.render(text);
}

function setView(view) {
  $("app").dataset.view = view;
}

// --- sidebar -------------------------------------------------------------------

const loadConversations = coalesced(async () => {
  const [{ conversations }, { projects }] = await Promise.all([
    api("GET", "/api/conversations"), api("GET", "/api/projects")]);
  state.conversations = conversations;
  state.projects = projects;
  refreshFilterChoices();
  // A conversation never seen here starts as seen (no « nouveau » everywhere the first time);
  // the open one is seen as it changes.
  const seen = seenTimes();
  markSeen(conversations.filter((c) => !seen[c.id] || c.id === state.currentId));
  if ($("search").value.trim().length >= 3) await runSearch();
  else renderList();
  renderRecent();
});

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

// When each conversation was last seen on this device: what changed since is « nouveau ».
function seenTimes() {
  try { return JSON.parse(localStorage.getItem("aihub-seen") || "{}") || {}; } catch { return {}; }
}

function markSeen(conversations) {
  const seen = seenTimes();
  for (const c of conversations) seen[c.id] = c.updated_at;
  try { localStorage.setItem("aihub-seen", JSON.stringify(seen)); } catch { /* storage off */ }
}

function unread(conv) {
  if (conv.id === state.currentId || conv.running) return false;
  const seen = seenTimes()[conv.id];
  return Boolean(seen) && conv.updated_at > seen;
}

function conversationItem(conv) {
  const fresh = unread(conv);
  return el("button", {
    class: "conv-item" + (conv.id === state.currentId ? " active" : "") + (fresh ? " unread" : ""),
    onclick: () => openConversation(conv.id),
  },
  el("span", { class: "conv-item-title" }, fresh ? el("span", { class: "dot", title: "nouveau depuis votre dernière visite" }) : null,
    `${conv.pinned ? "📌 " : ""}${conv.title}${conv.archived ? " · archivée" : ""}`),
  el("span", { class: "conv-item-sub" },
    conv.running ? el("span", { class: "dot", title: "en cours" }) : null,
    conv.running ? el("span", { class: "conv-running", text: "en cours · " }) : null,
    conv.project_id !== null && !conv.shared ? el("span", { class: "unshared", title: "Partage coupé", text: "privée · " }) : null,
    conv.my_role && conv.my_role !== "owner"
      ? el("span", { class: "unshared", text: conv.my_role === "read" ? "lecture seule · " : "partagée · " }) : null,
    // Short (1.2): what is unusual only; the usual (one AI, it asks first) goes without saying.
    conv.mode === "orchestration" ? "plusieurs IA · " : null,
    conv.permission_mode === "bypass" ? el("span", { class: "unshared", title: "L'IA agit sans demander", text: "sans permission · " }) : null,
    el("span", { title: formatDate(conv.updated_at), text: sinceDate(conv.updated_at) })));
}

// « à l'instant », « il y a 5 min », « hier »… then the date.
function sinceDate(iso) {
  const then = new Date(iso);
  const minutes = Math.round((Date.now() - then) / 60000);
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 1) return "à l'instant";
  if (minutes < 60) return `il y a ${minutes} min`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (then >= today) return `aujourd'hui ${then.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  if (then >= yesterday) return "hier";
  return formatDate(iso);
}

function renderList() {
  const list = $("conversation-list");
  list.replaceChildren();
  if (!state.conversations.length && !state.projects.length) {
    list.append(el("p", { class: "list-empty", text: "Aucune conversation pour l'instant." }));
    return;
  }
  const folded = foldedProjects();
  const matching = filteredConversations();
  const pinned = matching.filter((conv) => conv.pinned);
  if (pinned.length) {
    list.append(el("div", { class: "project-loose", text: "Épinglées" }));
    pinned.forEach((conv) => list.append(conversationItem(conv)));
  }
  for (const project of state.projects) {
    const convs = matching.filter((c) => c.project_id === project.id && !c.pinned);
    if (!convs.length && ($("filter-project").value || $("filter-model").value ||
      $("list-scope").value !== "active" || state.conversations.some((c) => c.project_id === project.id))) continue;
    const open = !folded.has(project.id) || convs.some((c) => c.id === state.currentId);
    const group = el("section", { class: "project" + (open ? "" : " folded") });
    group.append(el("div", { class: "project-head" },
      el("button", { class: "project-name", type: "button", "aria-expanded": String(open),
        title: project.description || "", onclick: () => toggleFolded(project.id) },
        el("span", { class: "chevron", "aria-hidden": "true", text: open ? "▾" : "▸" }),
        el("span", { text: project.name }),
        el("span", { class: "project-count", text: String(convs.length) })),
      isOwner() ? el("button", { class: "btn btn-ghost btn-small adv", type: "button", text: "+",
        title: "Nouvelle conversation dans ce projet", "aria-label": `Nouvelle conversation dans ${project.name}`,
        onclick: () => goHome(project.id) }) : null,
      isOwner() ? el("button", { class: "btn btn-ghost btn-small project-more", type: "button", text: "…",
        title: "Modifier, partager ou supprimer le projet", "aria-label": `Modifier le projet ${project.name}`,
        onclick: () => openProjectDialog(project) }) : null));
    if (open) {
      if (!convs.length) group.append(el("p", { class: "list-empty", text: "Aucune conversation dans ce projet." }));
      for (const conv of convs) group.append(conversationItem(conv));
    }
    list.append(group);
  }
  const loose = matching.filter((c) => c.project_id === null && !c.pinned);
  if (loose.length) {
    if (state.projects.length) list.append(el("div", { class: "project-loose", text: "Sans projet" }));
    for (const conv of loose) list.append(conversationItem(conv));
  }
  if (!matching.length && state.conversations.length) list.append(el("p", {
    class: "list-empty", text: "Aucune conversation avec ces filtres." }));
  // The archives, one click away (the filters are for « Fonctions avancées »).
  const archived = state.conversations.filter((c) => c.archived).length;
  const scope = $("list-scope").value;
  if (archived || scope !== "active") list.append(el("button", { class: "list-archives", type: "button",
    text: scope === "active" ? `Archives (${archived})` : "← Conversations",
    onclick: () => { $("list-scope").value = scope === "active" ? "archived" : "active"; runSearch(); } }));
}

function refreshFilterChoices() {
  for (const [id, values, label] of [["filter-project", state.projects.map((p) => [String(p.id), p.name]), "Tous les projets"],
    ["filter-model", [...new Set(state.conversations.map((c) => c.model).filter(Boolean))].map((m) => [m, aiName(m)]), "Toutes les IA"]]) {
    const previous = $(id).value;
    $(id).replaceChildren(el("option", { value: "", text: label }), ...values.map(([value, text]) => el("option", { value, text })));
    if (values.some(([value]) => value === previous)) $(id).value = previous;
  }
}

function filteredConversations() {
  const scope = $("list-scope").value;
  return state.conversations.filter((c) => (scope === "all" || Boolean(c.archived) === (scope === "archived")) &&
    (!$("filter-project").value || String(c.project_id) === $("filter-project").value) &&
    (!$("filter-model").value || c.model === $("filter-model").value) &&
    (!$("filter-after").value || c.updated_at.slice(0, 10) >= $("filter-after").value) &&
    (!$("filter-before").value || c.updated_at.slice(0, 10) <= $("filter-before").value));
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
    // Short in the sidebar (one mark per AI), the whole sentence on hover.
    const marks = [`Claude ${status.logged_in ? "✓" : "✗"}`];
    if (gpt.offered !== false) marks.push(`Codex ${gpt.logged_in ? "✓" : "✗"}`);
    if (local.offered !== false) marks.push(`Local ${local.running ? "✓" : "✗"}`);
    pill.textContent = marks.join(" · ");
    pill.className = "pill " + (status.logged_in || gpt.logged_in || local.running ? "ok" : "bad");
    pill.title = [parts.join(" · "), ...hints].join("\n");
    // A guest's requests use their own accounts: this computer's state says nothing to them.
    pill.classList.toggle("hidden", !isOwner());
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
  // The sidebar keeps one line: what is left of each AI's busiest window.
  const mini = $("quota-mini");
  const parts = [["Claude", data.claude_code, data.error]];
  if (data.codex_connected) parts.push(["Codex", data.codex, data.codex_error]);
  const threshold = state.options.quota_warning || 80;
  let high = false;
  mini.replaceChildren(el("span", { class: "quota-mini-label", text: "Quota" }), ...parts.map(([name, quota, error]) => {
    const used = quotaUsed(quota);
    if (used !== null && used >= threshold) high = true;
    const text = error && used === null ? `${name} ?` : used === null ? `${name} —` : `${name} ${Math.max(0, 100 - used)} %`;
    return el("span", { class: "quota-mini-part" + (used !== null && used >= threshold ? " high" : ""), text });
  }));
  mini.classList.toggle("high", high);
  mini.classList.toggle("quiet", !high);  // simple: said only when a quota is nearly used up
  mini.title = "Ce qui reste de la fenêtre la plus entamée de chaque IA. Cliquez pour le détail.";
}

// The busiest window of a quota, in % used (null: unknown).
function quotaUsed(quota) {
  if (!quota) return null;
  if (quota.status === "rejected") return 100;
  const windows = Object.values(quota.windows || {});
  if (!windows.length && typeof quota.utilization === "number") windows.push({ utilization: quota.utilization });
  const used = windows.map((w) => w.utilization).filter((u) => typeof u === "number").map((u) => (u <= 1 ? u * 100 : u));
  return used.length ? Math.round(Math.max(...used)) : null;
}

function setupQuotaDialog() {
  const dialog = $("quota-dialog");
  $("quota-mini").addEventListener("click", () => dialog.showModal());
  $("quota-close").addEventListener("click", () => dialog.close());
}

// A button that opens a small menu under it (or above it: class menu-up).
// A click on one of its items, outside it, or Échap closes it.
function setupMenus() {
  const close = (wrap) => {
    wrap.classList.remove("open");
    wrap.querySelector("[aria-haspopup]").setAttribute("aria-expanded", "false");
  };
  for (const wrap of document.querySelectorAll(".menu-wrap")) {
    const button = wrap.querySelector("[aria-haspopup]");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = !wrap.classList.contains("open");
      document.querySelectorAll(".menu-wrap.open").forEach(close);
      if (open) { wrap.classList.add("open"); button.setAttribute("aria-expanded", "true"); }
    });
    wrap.querySelector(".menu").addEventListener("click", (event) => {
      if (event.target.closest(".menu-item")) close(wrap);
      else event.stopPropagation();  // a field of the menu (the project) keeps it open
    });
  }
  document.addEventListener("click", () => document.querySelectorAll(".menu-wrap.open").forEach(close));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") document.querySelectorAll(".menu-wrap.open").forEach(close);
  });
  closeMenus = () => document.querySelectorAll(".menu-wrap.open").forEach(close);
}
let closeMenus = () => {};  // set by setupMenus

// A grouped menu (1.2): a heading shows only when something of its group does.
function tidyMenu(menu) {
  let heading = null;
  let seen = false;
  const done = () => { if (heading) heading.classList.toggle("hidden", !seen); };
  for (const child of menu.children) {
    if (child.classList.contains("menu-heading")) { done(); heading = child; seen = false; }
    else if (shown(child)) seen = true;
  }
  done();
}

const menuHasItems = (menu) => [...menu.children].some((c) => !c.classList.contains("menu-heading") && shown(c));

// --- « Réglages » (1.2): one window for what was a long menu, grouped and explained ----------

function setupHub() {
  const hub = $("hub-dialog");
  $("hub-button").addEventListener("click", () => { tidyHub(); hub.showModal(); });
  $("hub-close").addEventListener("click", () => hub.close());
  // A choice closes this window first: its own one opens alone (the click goes on to its button).
  hub.addEventListener("click", (event) => { if (event.target.closest(".hub-item, #logout")) hub.close(); }, true);
  $("show-keys").addEventListener("click", () => $("keys-dialog").showModal());
  $("updates").addEventListener("click", openUpdates);
  $("reload-now").addEventListener("click", () => location.reload());
  $("quota-open").addEventListener("click", () => $("quota-dialog").showModal());
  $("advanced-toggle").addEventListener("click", () => {
    const on = !advancedOn();
    try { localStorage.setItem("aihub-avance", on ? "1" : "0"); } catch { /* storage off: for this page only */ }
    applyAdvanced(on);
    if (state.data) renderConversation(); else renderHome();
    renderList();
    toast(on ? "Fonctions avancées affichées sur cet appareil." : "Retour à l'essentiel.");
  });
}

function tidyHub() {
  $("advanced-toggle").querySelector("strong").textContent = `Fonctions avancées : ${advancedOn() ? "oui" : "non"}`;
  for (const group of document.querySelectorAll("#hub-dialog .hub-group")) {
    group.classList.remove("hidden");
    group.classList.toggle("hidden", ![...group.querySelectorAll(".hub-item")].some(shown));
  }
  $("hub-who").textContent = state.me ? `${state.me.name} · ${ROLE_NAMES[state.me.is_owner ? "owner" : state.me.role] || ""}` : "";
}

// --- the home (1.2): write the request at once; the conversation is made when it is sent -----

const NEW_TITLE = "Nouvelle conversation";

// The first line of the request, cut on a word: the title of the conversation it starts.
function titleFrom(text) {
  const line = String(text || "").split("\n").map((l) => l.trim()).find(Boolean) || NEW_TITLE;
  if (line.length <= 60) return line;
  const cut = line.slice(0, 60);
  const space = cut.lastIndexOf(" ");
  return (space > 30 ? cut.slice(0, space) : cut).replace(/[\s,;:.!?…-]+$/, "") + "…";
}

// « Claude · Sonnet », « ChatGPT · GPT-6 Sol », « IA locale · qwen3.5:4b »: the AI first.
function aiName(model) {
  const m = String(model || "default");
  if (m === "default") return "Claude";
  if (m === "codex") return "ChatGPT (Codex)";
  if (m.startsWith("local:")) return `IA locale · ${m.slice(6)}`;
  return `${modelProvider(m) === "codex" ? "ChatGPT" : "Claude"} · ${MODEL_LABELS[m] || m}`;
}

function goHome(projectId = null) {
  if (state.currentId !== null) closeConversation();
  renderHome(projectId);
  setView("chat");  // a phone shows the home, not the list
  if (canCreate()) $("home-prompt").focus();
}

function renderHome(projectId = null) {
  const creator = canCreate();
  $("home-form").classList.toggle("hidden", !creator);
  $("home-guest").classList.toggle("hidden", creator);
  for (const id of ["home-template", "home-import"]) $(id).classList.toggle("hidden", !creator);
  $("home-project").replaceChildren(...projectOptions(projectId, "Aucun projet"),
    ...(creator ? [el("option", { value: "__new", text: "＋ Nouveau projet…" })] : []));
  state.homeProject = $("home-project").value;
  const models = state.options.models || [];
  const last = rememberedChoices().model;
  const chosen = models.includes(last) ? last  // the AI of the last conversation started here
    : models.includes(state.options.default_model) ? state.options.default_model : models[0];
  $("home-model").replaceChildren(...models.map((m) => el("option", { value: m, text: aiName(m), selected: m === chosen })));
  $("home-error").textContent = "";
  renderHomeFolder();
  renderRecent();
}

// The folder where the AI works: the project's, one chosen, or one of its own made by the server.
function renderHomeFolder() {
  const button = $("home-folder");
  button.classList.toggle("hidden", !isAdmin());  // a member's conversations always get a folder of their own
  const project = state.projects.find((p) => String(p.id) === $("home-project").value);
  if (state.homeFolder) {
    button.textContent = `Dossier : ${state.homeFolder.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || state.homeFolder}`;
    button.title = `${state.homeFolder}\nCliquez pour en changer, ou revenir au dossier automatique.`;
  } else if (project && project.workdir) {
    button.textContent = "Dossier : celui du projet";
    button.title = `${project.workdir}\nCliquez pour en choisir un autre.`;
  } else {
    button.textContent = "Dossier : automatique";
    button.title = "Un dossier vide, rien qu'à cette conversation, sur le PC serveur. Cliquez pour choisir un dossier du PC.";
  }
}

function renderRecent() {
  const box = $("home-recent");
  const recent = state.conversations.filter((c) => !c.archived)
    .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)).slice(0, 4);
  box.classList.toggle("hidden", !recent.length);
  box.replaceChildren(el("h2", { text: "Reprendre" }), el("div", { class: "home-recent-list" },
    ...recent.map((c) => el("button", { type: "button", class: "home-recent-item", onclick: () => openConversation(c.id) },
      el("strong", { text: c.title }),
      el("span", { text: [state.projects.find((p) => p.id === c.project_id)?.name, sinceDate(c.updated_at)].filter(Boolean).join(" · ") })))));
}

function setupHome() {
  $("home-project").addEventListener("change", () => {
    if ($("home-project").value === "__new") {  // made here, then chosen (setupProjectDialog)
      $("home-project").value = state.homeProject;
      openProjectDialog(null);
      return;
    }
    state.homeProject = $("home-project").value;
    renderHomeFolder();
  });
  $("home-folder").addEventListener("click", async () => {
    if (state.homeFolder) {
      const choice = await choiceDialog({ title: "Dossier de travail", text: state.homeFolder,
        choices: [{ value: "auto", label: "Revenir au dossier automatique" }, { value: "pick", label: "Choisir un autre dossier", primary: true }] });
      if (choice === "auto") { state.homeFolder = null; renderHomeFolder(); }
      if (choice !== "pick") return;
    }
    openPathPicker({ title: "Dossier où l'IA travaille", start: state.homeFolder || rememberedChoices().workdir || "",
      onPick: (path) => { state.homeFolder = path; renderHomeFolder(); $("home-prompt").focus(); } });
  });
  $("home-back").addEventListener("click", () => setView("list"));
  $("home-more").addEventListener("click", () => {
    state.homeCarry = $("home-prompt").value;  // what was written follows into the conversation
    const project = $("home-project").value ? Number($("home-project").value) : null;
    openNewDialog(project, null, { model: $("home-model").value, workdir: state.homeFolder });
  });
  $("home-template").addEventListener("click", async () => { await openNewDialog(null); $("new-template").focus(); });
  $("home-import").addEventListener("click", () => $("import").click());
  $("home-prompt").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    if (event.ctrlKey || event.metaKey || (KEYBOARD && !event.shiftKey)) { event.preventDefault(); $("home-form").requestSubmit(); }
  });
  $("home-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const prompt = $("home-prompt").value.trim();
    if (!prompt) { $("home-prompt").focus(); return; }
    $("home-send").disabled = true;
    $("home-error").textContent = "";
    try {
      const project = $("home-project").value ? Number($("home-project").value) : null;
      const model = $("home-model").value;
      const { conversation } = await api("POST", "/api/conversations", { title: titleFrom(prompt), project_id: project,
        model, workdir: state.homeFolder || "", mode: "single", permission_mode: "manual" });
      rememberChoices({ model, ...(state.homeFolder && project === null ? { workdir: state.homeFolder } : {}) });
      $("home-prompt").value = "";
      if ($("list-scope").value === "archived") $("list-scope").value = "active";
      await loadConversations();
      await openConversation(conversation.id);
      // Sent as from the conversation itself: same checks, same way.
      $("prompt").value = prompt;
      fitPrompt();
      $("composer").requestSubmit($("send"));
    } catch (err) {
      $("home-error").textContent = err.message;
    } finally {
      $("home-send").disabled = false;
    }
  });
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
  state.shown = SHOWN_FIRST;
  state.pendingFiles = [];  // files belong to their conversation
  if (SPEECH && speaking !== null) { window.speechSynthesis.cancel(); speaking = null; }  // another conversation: quiet
  renderPending();
  markSeen(state.conversations.filter((c) => c.id === id));
  renderList();
  setView("chat");
  $("empty").classList.add("hidden");
  $("conversation").classList.remove("hidden");
  $("prompt").value = loadDraft(id);
  fitPrompt();
  await refreshConversation();
  if (state.currentId !== id) return;  // another one was opened meanwhile
  if (messageId === null) $("messages").scrollTop = $("messages").scrollHeight;
  connectEvents(id);
  if (messageId !== null) {
    // From a search result: show the message found, even far back.
    const selector = `[data-id="${Number(messageId)}"]`;
    if (!$("messages").querySelector(selector) && state.data && state.data.older) {
      state.shown = null;
      await refreshConversation();
    }
    const node = $("messages").querySelector(selector);
    if (node) {
      node.scrollIntoView({ block: "center" });
      node.classList.add("found");
      setTimeout(() => node.classList.remove("found"), 2500);
    }
  }
}

const refreshConversation = coalesced(async () => {
  const id = state.currentId;
  if (id === null) return;
  try {
    // A long conversation comes in part: the last messages, more on request.
    const data = await api("GET", `/api/conversations/${id}${state.shown ? `?last=${state.shown}` : ""}`);
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
});

function closeConversation() {
  if (state.events) state.events.close();
  state.events = null;
  state.currentId = null;
  state.data = null;
  $("conversation").classList.add("hidden");
  $("empty").classList.remove("hidden");
  setView("list");
  renderHome();
}

function renderConversation() {
  const { conversation: conv, messages } = state.data;
  $("conv-title").textContent = conv.title;
  $("isolation-access").classList.toggle("hidden", conv.my_role !== "owner");
  $("isolation-access").textContent = conv.isolation?.isolated ? "Espace isolé · accès…" : "Espace et accès…";
  $("isolation-accounts").classList.toggle("hidden", !conv.isolation?.isolated || conv.my_role === "read");
  $("isolation-accounts-quick").classList.toggle("hidden", !conv.isolation?.isolated || conv.my_role === "read");
  $("isolation-results").classList.toggle("hidden", !conv.isolation?.isolated || conv.my_role === "read");
  $("results-quick").classList.toggle("hidden", !conv.isolation?.isolated || conv.my_role === "read");
  $("access-overview").textContent = conv.isolation?.paused ? "Accès suspendus · voir" : "Accès de cette IA";
  $("create-path").classList.toggle("hidden", !isAdmin() || conv.my_role !== "owner");
  $("create-path").disabled = !!conv.isolation?.paused;
  $("pin-conversation").textContent = conv.pinned ? "Désépingler pour moi" : "Épingler pour moi";
  $("archive-conversation").textContent = conv.archived ? "Sortir de mes archives" : "Archiver pour moi";
  $("save-template").classList.toggle("hidden", !canCreate() || conv.my_role !== "owner");
  // Short (1.2): the AI, then the folder by its name; the whole path on hover.
  const how = conv.mode === "orchestration" ? `Plusieurs IA · chef : ${aiName(conv.conductor_model)}` : aiName(conv.model);
  const folder = conv.isolation?.isolated && conv.isolation.files === "none" ? "aucun fichier du PC"
    : conv.workdir ? `dossier « ${conv.workdir.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || conv.workdir} »` : "sans dossier";
  $("conv-meta").textContent = `${conv.isolation?.isolated ? "Espace isolé · " : ""}${how} · ${folder}`;
  $("conv-meta").title = conv.workdir || "";
  $("conv-mode").value = conv.permission_mode;
  // The AI of the conversation (the conductor in orchestration); one no longer offered stays shown.
  const current = conv.mode === "orchestration" ? conv.conductor_model : conv.model;
  const allowedModels = (conv.isolation?.isolated ? state.options.isolated_models : state.options.models) || [];
  const offered = allowedModels.includes(current) || !current ? allowedModels : [current, ...allowedModels];
  $("conv-model").replaceChildren(...offered.map((m) => el("option", {
    value: m, text: (conv.mode === "orchestration" ? "Chef : " : "") + aiName(m),
    selected: m === current })));
  $("conv-assignment-row").classList.toggle("hidden", conv.mode !== "orchestration");
  $("conv-assignment").value = conv.assignment_mode;
  $("schedules").classList.toggle("hidden", conv.mode === "orchestration");
  $("conv-project").replaceChildren(...projectOptions(conv.project_id, "Sans projet"));
  const inProject = conv.project_id !== null;
  $("conv-shared").classList.toggle("hidden", !inProject);
  $("conv-shared").classList.toggle("on", Boolean(conv.shared));
  $("conv-shared").textContent = conv.shared ? "Partagée ✓" : "Privée";
  const others = state.conversations.filter((c) => inProject && c.project_id === conv.project_id && c.id !== conv.id);
  $("cite").classList.toggle("hidden", !inProject || !conv.shared || !others.length);
  const vault = Boolean(conv.notes_vault);  // its own vault, its project's, or the settings'
  $("obsidian").classList.toggle("hidden", !vault && !isOwner());  // the owner chooses a vault when linking
  $("obsidian").classList.toggle("on", Boolean(conv.obsidian_note));
  $("obsidian").textContent = conv.obsidian_note ? "Obsidian ✓" : "Relier à Obsidian…";
  // Shared with someone: what is the owner's stays hidden, and reading only means no composer.
  const role = conv.my_role || "owner";
  for (const id of ["conv-project", "conv-assignment", "conv-model", "conv-mode", "obsidian", "tools", "instructions",
    "schedules", "rename", "delete", "conv-shared"]) {
    if (role !== "owner") ($(id).closest(".menu-field") || $(id)).classList.add("hidden");
  }
  $("share").classList.toggle("hidden", role !== "owner" || !state.me);
  $("share").textContent = (state.data.shared_with || []).length ? `Partagée (${state.data.shared_with.length})` : "Partager";
  $("composer").classList.toggle("hidden", role === "read" || !!conv.isolation?.paused);
  $("readonly-note").classList.toggle("hidden", role !== "read" && !conv.isolation?.paused);
  $("readonly-note").textContent = conv.isolation?.paused ? "Accès suspendus. Le propriétaire peut les réactiver dans « Accès de cette IA »." : "Cette conversation est partagée en lecture seule.";
  $("fresh").classList.toggle("hidden", role === "read" || !!conv.isolation?.paused);
  for (const id of ["isolation-accounts", "isolation-accounts-quick", "isolation-results", "results-quick"]) $(id).disabled = !!conv.isolation?.paused;
  fillCompareChoices();
  // Files go to a conversation with one AI (not to a conductor yet).
  $("attach").classList.toggle("hidden", role === "read" || conv.mode === "orchestration");
  if (!isAdmin()) {
    // A member's own conversation: never without permission, nor the owner's vault.
    $("obsidian").classList.add("hidden");
    const bypass = $("conv-mode").querySelector('option[value="bypass"]');
    if (bypass) bypass.disabled = true;
  }
  // The exports: whoever sees the conversation may take it away.
  $("instructions").textContent = conv.instructions ? "Consignes ✓" : "Consignes…";
  $("export-md").href = `/api/conversations/${conv.id}/export?format=md`;
  $("export-json").href = `/api/conversations/${conv.id}/export?format=json`;
  $("export-print").href = `/api/conversations/${conv.id}/export?format=html`;
  // Nothing left in the menu (reading only): no menu button. A heading goes with its group.
  tidyMenu($("conv-menu"));
  $("conv-menu-button").classList.toggle("hidden", !menuHasItems($("conv-menu")));
  renderFresh(conv);
  renderMessages(messages);
  renderRunning();
}

function renderMessages(messages) {
  const box = $("messages");
  const previousTop = box.scrollTop;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  const older = state.data.older || 0;
  if (older > 0) {
    box.append(el("button", { class: "btn btn-ghost btn-small older", type: "button",
      text: `Afficher les messages précédents (${older} de plus)`,
      onclick: async () => {
        // Keep the reading place: what is added comes above it.
        const before = box.scrollHeight - box.scrollTop;
        const next = state.shown + SHOWN_MORE;
        // The server gives at most SHOWN_MAX at a time: beyond, the whole conversation.
        state.shown = next >= state.data.messages.length + older || next > SHOWN_MAX ? null : next;
        await refreshConversation();
        box.scrollTop = box.scrollHeight - before;
      } }));
  }
  const firstShown = messages.length ? messages[0].id : null;
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
  let request = null;  // the request the next reply answers (its compared answers go beside it)
  for (const m of messages) {
    if (m.role === "user") {
      request = m;
      flushPlan();
      box.append(el("div", { class: "msg user", "data-id": m.id },
        authors && m.author ? el("div", { class: "msg-author", text: m.author }) : null,
        el("div", { text: m.content }), messageFiles(m.attachments), editButton(m)));
      waitingPlan = plans.get(m.id) || null;
      continue;
    }
    const live = state.live && state.live.messageId === m.id;
    const body = el("div", { class: "msg-body" + (live ? " cursor" : ""), id: live ? "live-body" : null });
    body.innerHTML = renderMarkdown(live ? state.live.content : m.content);
    const node = el("div", { class: "msg assistant", "data-id": m.id }, body);
    if (m.status === "error") node.append(el("div", { class: "msg-error", text: m.error || "erreur" }));
    if (m.status === "stopped") node.append(el("div", { class: "msg-stopped", text: m.error || "Requête arrêtée." }));
    if (['stopped', 'error'].includes(m.status) && m.id === messages[messages.length - 1]?.id && !state.data.running
        && state.data.conversation.my_role !== 'read' && !state.data.conversation.isolation?.paused
        && !(state.data.plans || []).some(plan => plan.synthesis_message_id === m.id)) {
      node.append(el('button', { type: 'button', class: 'btn btn-primary btn-small', text: 'Reprendre',
        onclick: async event => {
          event.target.disabled = true;
          try { await api('POST', `/api/messages/${m.id}/resume`); await refreshConversation(); }
          catch (err) { toast(err.message); event.target.disabled = false; }
        } }));
    }
    if (!live) {
      // Short under the reply; what it cost, on hover.
      const meta = [formatDuration(m.duration_ms)];
      if (m.model && m.model !== "default") meta.push(aiName(m.model));
      const cost = m.tokens_in !== null ? `${m.tokens_in} tokens envoyés, ${m.tokens_out} reçus` : null;
      node.append(el("div", { class: "msg-meta", title: cost },
        el("span", { class: "adv", text: meta.filter(Boolean).join(" · ") }),
        ...regenerateButtons(m, messages),
        speakButton(m),
        m.content ? el("button", { class: "msg-copy", type: "button", text: "Copier", title: "Copier la réponse",
          onclick: async (e) => {
            try { await navigator.clipboard.writeText(m.content); } catch { fallbackCopy(m.content); }
            e.target.textContent = "Copié ✓";
            setTimeout(() => { e.target.textContent = "Copier"; }, 1500);
          } }) : null));
      const synthesis = (state.data.plans || []).some((plan) => plan.synthesis_message_id === m.id);
      if (state.data.conversation.obsidian_note && m.status === "ok" && synthesis) {
        // A plan's synthesis goes to the note with its plan, never a second time.
        node.append(el("button", { class: "msg-note", type: "button", disabled: true, text: "Dans la note avec son plan" }));
      } else if (state.data.conversation.obsidian_note && m.status === "ok") {
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
    // Another AI answered the same request: side by side.
    const variants = request && request.variants && request.variants.length ? request.variants : null;
    box.append(variants ? el("div", { class: "compare-row" }, node, ...variants.map(variantCard)) : node);
    request = null;
    flushPlan();
  }
  flushPlan();
  for (const plan of state.data.plans || []) {
    // A plan of a request not loaded yet stays with it, above.
    if (!shown.has(plan.id) && !(older && firstShown !== null && plan.request_message_id < firstShown)) box.append(planCard(plan));
  }
  for (const request of state.pending.values()) box.append(permissionCard(request));
  if (atBottom) box.scrollTop = box.scrollHeight;
  else box.scrollTop = previousTop;
}

function delegationNote(d) {
  const who = aiName(d.model);
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
    // A member: only the tools that touch nothing of the server's (the web, another AI's opinion).
    const offered = (state.data.tools_offered || []).filter((t) => isAdmin() || ["web", "deleguer"].includes(t.name));
    $("tools-list").replaceChildren(...offered.map((t) => el("label", { class: "check tool-item" },
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
  send.textContent = orchestration ? "Demander un plan" : "↑";
  discuss.className = "btn " + (plan ? "btn-primary" : "btn-ghost") + (orchestration ? "" : " hidden");
  send.className = "btn " + (plan ? "btn-ghost" : "btn-primary") + (orchestration ? "" : " send-button");
  discuss.disabled = running;
  $("autonomous").classList.toggle("hidden", orchestration);
  $("autonomous").disabled = running;
  send.disabled = running || Boolean(plan);
  send.title = plan ? "Un plan attend votre validation : lancez-le ou annulez-le d'abord" : "";
  $("prompt").placeholder = !orchestration
    ? "Votre demande…"
    : plan ? "Une question ou un changement pour le chef ? Ensuite, « Adapter le plan »."
      : "Demande facultative : sans texte, le plan reprend la discussion et le projet.";
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
    const box = $("messages");
    const following = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (state.livePaint) return;
    state.livePaint = requestAnimationFrame(() => {
      state.livePaint = null;
      if (!state.live || !document.contains(body)) return;
      body.innerHTML = renderMarkdown(state.live.content);
      if (following) box.scrollTop = box.scrollHeight;
      $("latest-message").classList.toggle("hidden", box.scrollHeight - box.scrollTop - box.clientHeight < 80);
    });
  }
}

// --- live events -------------------------------------------------------------------

function connectEvents(id) {
  if (state.events) state.events.close();  // two quick openings: only the last one listens
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
    case "access_revoked":
      closeConversation();
      try { localStorage.setItem(OFFLINE_KEY, "0"); } catch { /* storage off */ }
      tellWorker("offline-off");
      toast("Votre accès ou votre session a changé. La conversation est fermée.");
      loadConversations();
      break;
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
    case "variant_done":
      scheduleRefresh();  // the other AI has answered: its text, beside the reply
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

const loadAgents = coalesced(async () => {
  try {
    state.agents = (await api("GET", "/api/agents")).agents;
  } catch (err) {
    toast(err.message);
  }
});

async function planAction(method, path, body) {
  const conversationId = state.currentId;
  try {
    const result = await api(method, path, body);
    // Apply the committed plan even if the following refresh loses its connection.
    if (result.plan && state.currentId === conversationId && state.data) {
      state.data.plans = state.data.plans.map(plan => plan.id === result.plan.id ? result.plan : plan);
      renderConversation();
    }
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
  return state.options.models.map((m) => el("option", { value: m, text: aiName(m), selected: m === selected }));
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
  const quota = quotaBox(plan);
  if (quota) card.append(quota);
  const switcher = switchBox(plan);
  if (switcher) card.append(switcher);
  if (plan.resumable && state.data.conversation.my_role !== 'read') card.append(el('button', { type: 'button', class: 'btn btn-primary', text: 'Reprendre le plan',
    disabled: Boolean(state.data.running || state.data.conversation.isolation?.paused), onclick: async () => {
      const remaining = plan.subtasks.filter(task => task.status !== 'ok').length;
      if (!await confirmDialog({ title: 'Reprendre ce plan ?',
        text: `${remaining} tâche(s) à poursuivre. Les tâches terminées ne seront pas rejouées. La reprise utilise les accès et comptes IA actuels.`, okText: 'Reprendre' })) return;
      await planAction('POST', `/api/plans/${plan.id}/resume`);
    } }));
  return card;
}

// A plan a quota stopped (1.1): resumed by itself when the quota comes back, or given to another AI.
const modelProvider = (model) => model === "codex" || String(model).startsWith("gpt-") ? "codex"
  : String(model).startsWith("local:") ? "ollama" : "claude_code";
const localTime = (iso) => new Date(iso).toLocaleString("fr-FR", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });

function quotaBox(plan) {
  const q = plan.quota;
  if (!q && !plan.auto_resume_at && !plan.auto_resume_error) return null;
  const canAct = state.data.conversation.my_role !== "read" && !state.data.running;
  const box = el("div", { class: "plan-quota", role: "status" });
  if (q) {
    const who = q.providers.map((p) => PROVIDER_NAMES[p] || p).join(", ");
    box.append(el("p", {}, el("strong", { text: `Quota de ${who} atteint` }),
      ` : ${q.subtasks.length} tâche(s) arrêtée(s)` + (q.resets_at ? `, retour prévu ${localTime(q.resets_at)}.` : ", heure de retour inconnue.")));
  }
  if (plan.auto_resume_at) {
    box.append(el("p", { text: `Reprise automatique prévue ${localTime(plan.auto_resume_at)}. Les tâches terminées ne seront pas rejouées.` }));
    if (canAct) box.append(el("button", { type: "button", class: "btn btn-ghost", text: "Annuler la reprise automatique",
      onclick: () => planAction("POST", `/api/plans/${plan.id}/auto-resume`, { cancel: true }) }));
  } else if (q && canAct && plan.resumable) {
    const actions = el("div", { class: "plan-quota-actions" });
    if (q.resets_at) {
      const at = new Date(new Date(q.resets_at).getTime() + 2 * 60000);
      actions.append(el("button", { type: "button", class: "btn btn-primary",
        text: `Reprendre automatiquement à ${at.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`,
        onclick: () => planAction("POST", `/api/plans/${plan.id}/auto-resume`, {}) }));
    } else {
      const when = el("input", { type: "datetime-local", "aria-label": "Heure de la reprise" });
      actions.append(el("label", {}, "Reprendre à", when), el("button", { type: "button", class: "btn btn-primary", text: "Programmer la reprise",
        onclick: () => when.value ? planAction("POST", `/api/plans/${plan.id}/auto-resume`, { at: new Date(when.value).toISOString() })
          : toast("Choisissez l’heure de la reprise.") }));
    }
    const isolated = state.data.conversation.isolation?.isolated;
    const models = ((isolated ? state.options.isolated_models : state.options.models) || [])
      .filter((m) => !q.providers.includes(modelProvider(m)));
    if (models.length) {
      const other = el("select", { "aria-label": "IA qui reprend les tâches" }, ...models.map((m) => el("option", { value: m, text: aiName(m) })));
      actions.append(el("span", { class: "plan-quota-or", text: "ou" }), other, el("button", { type: "button", class: "btn btn-ghost",
        text: "Confier à cette IA et reprendre", onclick: async () => {
          if (!await confirmDialog({ title: "Confier les tâches à une autre IA ?",
            text: `Les ${q.waiting.length} tâche(s) restantes prévues sur ${q.providers.map((p) => PROVIDER_NAMES[p] || p).join(", ")} passent à ${aiName(other.value)}, puis le plan reprend tout de suite. Les tâches terminées ne sont pas rejouées.`,
            okText: "Confier et reprendre" })) return;
          await planAction("POST", `/api/plans/${plan.id}/give`, { model: other.value });
        } }));
    }
    box.append(actions);
  }
  if (plan.auto_resume_error) box.append(el("p", { class: "form-error", text: `Dernière reprise automatique : ${plan.auto_resume_error}` }));
  return box;
}

// « Passer la suite sur … » (1.1.1): the rest of a validated, running or interrupted plan on one AI,
// e.g. only Claude when ChatGPT has no quota left. Tasks running or done keep their AI.
function switchBox(plan) {
  const conv = state.data.conversation;
  if (conv.my_role === "read" || !(["validated", "running", "error", "stopped"].includes(plan.status) || plan.resumable)) return null;
  const left = plan.subtasks.filter((s) => ["pending", "error", "stopped"].includes(s.status));
  if (!left.length && plan.status === "running") return null;
  const models = ((conv.isolation?.isolated ? state.options.isolated_models : state.options.models) || []);
  if (models.length < 2) return null;
  const used = left.map((s) => modelProvider(s.model || ""));
  const most = used.sort((a, b) => used.filter((x) => x === b).length - used.filter((x) => x === a).length)[0];
  // The AI first: « Claude · Par défaut », « Codex · GPT-5.6 Luna ».
  const named = aiName;
  const choice = el("select", { "aria-label": "IA pour la suite du plan" },
    ...models.map((m) => el("option", { value: m, text: named(m) })));
  choice.value = models.find((m) => modelProvider(m) !== most) || models[0];
  const conductor = el("input", { type: "checkbox", checked: true });
  const interrupted = plan.status !== "running" && plan.resumable;
  const apply = el("button", { type: "button", class: "btn btn-ghost btn-small", text: interrupted ? "Passer et reprendre" : "Appliquer",
    disabled: Boolean(conv.isolation?.paused), onclick: async () => {
      const who = named(choice.value);
      const ok = await confirmDialog({ title: `Passer la suite sur ${who} ?`,
        text: `${left.length} tâche(s) pas encore faite(s) passent à ${who}` + (conductor.checked ? ", et le chef aussi (c'est lui qui écrit la synthèse)" : "")
          + ". Les tâches en cours ou terminées gardent leur IA." + (interrupted ? " Le plan reprend ensuite tout de suite." : ""),
        okText: interrupted ? "Passer et reprendre" : "Passer la suite" });
      if (!ok) return;
      await planAction("POST", `/api/plans/${plan.id}/switch`, { model: choice.value, conductor: conductor.checked });
    } });
  return el("details", { class: "plan-switch" },
    el("summary", { text: "Changer d'IA pour la suite…" }),
    el("p", { class: "quota-note", text: "Par exemple quand une IA n'a plus de quota : la suite du plan, même déjà validé ou en cours, va sur celle que vous choisissez." }),
    el("div", { class: "plan-quota-actions" }, el("label", {}, "Passer la suite sur", choice),
      el("label", { class: "check" }, conductor, "le chef aussi"), apply));
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
  const facts = [agent ? agent.name : null, st.model ? `IA : ${aiName(st.model)}` : null,
    st.duration_ms !== null ? formatDuration(st.duration_ms) : null].filter(Boolean);
  row.append(el("div", { class: "plan-line" },
    el("span", { class: `badge ${st.status}`, text: STATUS_LABELS[st.status] || st.status }),
    facts.join(" · ")));
  if (st.status === "running") {
    row.append(el("div", { class: "plan-live", "data-live-subtask": st.id,
      text: state.subtaskLive.get(st.id) || "…" }));
  }
  if (st.status === "error" && st.error) row.append(el("div", { class: "msg-error plan-result", text: st.error }));
  if (st.checkpoint && ['stopped', 'error'].includes(st.status)) row.append(el('details', { class: 'plan-result' },
    el('summary', { text: 'Dernier point sauvegardé' }), el('pre', { class: 'result-diff', text: st.checkpoint })));
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
        const ok = await confirmDialog({ title: "Annuler le plan ?", text: "Le plan sera abandonné sans rien lancer. Vous pourrez ensuite envoyer une nouvelle demande.", okText: "Annuler le plan", cancelText: "Conserver le plan" });
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
function confirmDialog({ title, text, okText = "Confirmer", cancelText = "Annuler", check = null }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    const ok = $("confirm-ok");
    const box = $("confirm-check");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    ok.textContent = okText;
    $("confirm-cancel").textContent = cancelText;
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

// --- execution spaces ---------------------------------------------------------

function isolationEditor(inherit, where = null) {
  const choices = (items) => items.map(([value, text]) => el("option", { value, text }));
  const mode = el("select", { "aria-label": "Lieu d’exécution" }, ...choices([
    ...(inherit ? [["inherit", "Hériter du projet"]] : []),
    ["host", "Sur le PC · accès selon les permissions"], ["isolated", "Espace isolé · accès choisis"]]));
  const files = el("select", {}, ...choices([["none", "Aucun fichier du PC"], ["read", "Lire une copie"], ["write", "Modifier une copie"]]));
  const paths = el("textarea", { rows: 3, maxlength: 50000, placeholder: "src\nREADME.md", "aria-label": "Fichiers et dossiers sélectionnés" });
  const network = el("select", {}, ...choices([["ai", "Fournisseur IA seulement"], ["none", "Aucun réseau"]]));
  const memory = el("select", {}, ...choices([["own", "Cette conversation seulement"], ["project", "Conversations partagées du projet"]]));
  const summary = el("p", { class: "quota-note" });
  const presets = el("select", { "aria-label": "Profil d’accès" }, ...choices([
    ["custom", "Personnaliser les accès"], ["discussion", "Discussion seule"],
    ["analysis", "Analyse de fichiers"], ["project", "Travail sur un projet"]]));
  const presetNote = el("p", { class: "quota-note", text: "Un profil prépare les accès. Vous choisissez ensuite les fichiers autorisés." });
  // `where()`: { conversationId } or { base } — the working folder the selection is taken from.
  const pick = where ? el("button", { type: "button", class: "btn btn-ghost btn-small" + (where().conversationId ? "" : " picker-trusted" + (isAdmin() ? "" : " hidden")),
    text: "Choisir dans le dossier de travail…", onclick: () => {
      const lines = paths.value.split(/\r?\n/).map((p) => p.trim()).filter(Boolean);
      const { base = "", conversationId = null } = where();
      openFilePicker({ base, conversationId, chosen: lines, onPick: (list) => {
        paths.value = [...new Set([...lines, ...list])].join("\n");
        paths.dispatchEvent(new Event("change", { bubbles: true }));
      } });
    } }) : null;
  const selection = el("label", {}, "Sélection, relative au dossier de travail · un chemin par ligne", paths, pick);
  const fields = el("div", { class: "isolation-fields" }, el("label", {}, "Fichiers", files), selection,
    el("label", {}, "Réseau", network), el("label", {}, "Mémoire", memory),
    el("p", { class: "quota-note", text: "Aucun outil du PC. Les comptes IA de cet espace se connectent séparément. Les changements sont produits dans une copie, puis récupérés pour vérification." }));
  const node = el("fieldset", { class: "isolation-editor" }, el("legend", { text: "Espace et accès" }),
    el("label", {}, "Commencer avec un profil", presets), presetNote,
    el("label", {}, "Lieu d’exécution", mode), summary, fields);
  let parent = { isolated: false };
  const refresh = () => {
    const inherited = mode.value === "inherit";
    fields.classList.toggle("hidden", mode.value !== "isolated");
    selection.classList.toggle("hidden", files.value === "none");
    summary.textContent = inherited ? (parent.isolated ? "Le projet impose l’isolation et ses limites d’accès." : "Le projet utilise le PC. Vous pouvez choisir un espace isolé pour cette conversation.")
      : mode.value === "isolated" ? "Seuls les fichiers sélectionnés sont copiés. Les originaux restent sur le PC." : "L’IA s’exécute sur ce PC dans le dossier de travail.";
    const host = mode.querySelector('option[value="host"]');
    if (host) host.disabled = inherit && parent.isolated;
    const level = { none: 0, read: 1, write: 2 };
    for (const [profile, access] of [["analysis", "read"], ["project", "write"]]) {
      presets.querySelector(`option[value="${profile}"]`).disabled = !!(inherit && parent.isolated && level[access] > level[parent.files]);
    }
  };
  for (const field of [mode, files]) field.addEventListener("change", refresh);
  const value = () => mode.value === "inherit" ? null : { isolated: mode.value === "isolated", files: files.value,
    paths: files.value === "none" ? [] : paths.value.split(/\r?\n/).map((p) => p.trim()).filter(Boolean),
    network: network.value, memory: memory.value };
  const set = (policy) => {
    presets.value = "custom";
    presetNote.textContent = "Un profil prépare les accès. Vous choisissez ensuite les fichiers autorisés.";
    mode.value = !policy && inherit ? "inherit" : policy?.isolated ? "isolated" : "host";
    files.value = policy?.files || "none"; paths.value = (policy?.paths || []).join("\n");
    network.value = policy?.network || "ai"; memory.value = policy?.memory || "own"; refresh();
  };
  for (const field of [mode, files, paths, network, memory]) field.addEventListener("change", () => { presets.value = "custom"; });
  presets.addEventListener("change", () => {
    if (presets.value === "custom") return;
    mode.value = "isolated";
    files.value = { discussion: "none", analysis: "read", project: "write" }[presets.value];
    if (files.value === "none") paths.value = "";
    else if (inherit && parent.isolated && !paths.value.trim()) paths.value = (parent.paths || []).join("\n");
    network.value = parent.isolated && parent.network === "none" ? "none" : "ai";
    memory.value = "own";
    presetNote.textContent = { discussion: "Discutez sans donner accès aux fichiers du PC.",
      analysis: "L’IA lit uniquement une copie de votre sélection. Elle ne modifie aucun original.",
      project: "L’IA modifie une copie de votre sélection. Vous vérifiez les résultats avant de les appliquer." }[presets.value];
    refresh();
  });
  set(null);
  return { node, set, value, setPreset: (name) => { presets.value = name; presets.dispatchEvent(new Event("change", { bubbles: true })); },
    effective: () => value() || parent, parent: (policy) => { parent = policy || { isolated: false }; refresh(); } };
}

document.addEventListener("DOMContentLoaded", () => {
  $("appearance").addEventListener("click", () => {
    const dialog = el("dialog", { class: "dialog dialog-content" });
    const theme = el("select", { "aria-label": "Thème" }, ...[["system", "Selon l’appareil"], ["light", "Clair"], ["dark", "Sombre"]].map(([value, text]) => el("option", { value, text })));
    const density = el("select", { "aria-label": "Espacement" }, el("option", { value: "comfortable", text: "Confortable" }), el("option", { value: "compact", text: "Compact" }));
    theme.value = document.documentElement.dataset.theme; density.value = document.documentElement.dataset.density;
    for (const field of [theme, density]) field.addEventListener("change", () => {
      applyAppearance(theme.value, density.value);
      try { localStorage.setItem("aihub-theme", theme.value); localStorage.setItem("aihub-density", density.value); } catch { /* storage off */ }
    });
    dialog.append(el("h2", { text: "Une interface à votre rythme" }), el("label", {}, "Thème", theme), el("label", {}, "Espacement", density),
      el("p", { class: "quota-note", text: "Ces choix sont gardés sur cet appareil." }), el("div", { class: "dialog-actions" },
        el("button", { type: "button", class: "btn btn-primary", text: "Terminé", onclick: () => dialog.close() })));
    dialog.addEventListener("close", () => dialog.remove(), { once: true }); document.body.append(dialog); dialog.showModal();
  });
  $("isolation-accounts").addEventListener("click", () => openSpaceAccounts());
  $("isolation-accounts-quick").addEventListener("click", () => openSpaceAccounts());
  $("isolation-results").addEventListener("click", () => openSpaceResults());
  $("results-quick").addEventListener("click", () => openSpaceResults());
  $("access-overview").addEventListener("click", openAccessOverview);
  $("diagnostics").addEventListener("click", openDiagnostics);
  $("getting-started").addEventListener("click", openGettingStarted);
  $("welcome-guide").addEventListener("click", openGettingStarted);
  $("create-path").addEventListener("click", () => openCreatePath({ conversation: state.data.conversation }));
  $("isolation-access").addEventListener("click", async () => {
    try {
      const data = await api("GET", `/api/conversations/${state.currentId}/isolation`);
      const editor = isolationEditor(true, () => ({ conversationId: state.currentId })); editor.parent(data.parent); editor.set(data.inherited ? null : data.policy);
      const error = el("p", { class: "form-error", role: "alert" });
      const dialog = el("dialog", { class: "dialog" });
      const cancel = el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => dialog.close() });
      const submit = el("button", { type: "submit", class: "btn btn-primary", text: "Enregistrer les accès" });
      const form = el("form", {}, el("h2", { text: "Espace et accès" }), editor.node, error,
        el("div", { class: "dialog-actions" }, cancel, submit));
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); submit.disabled = true;
        try {
          const policy = editor.value();
          await api(policy ? "PUT" : "DELETE", `/api/conversations/${state.currentId}/isolation`, policy);
          dialog.close(); await openConversation(state.currentId);
        } catch (err) { error.textContent = err.message; }
        finally { submit.disabled = false; }
      });
      dialog.append(form); document.body.append(dialog);
      dialog.addEventListener("close", () => dialog.remove(), { once: true }); dialog.showModal();
    } catch (err) { toast(err.message); }
  });
});

// In a conversation's working folder (relative to it): a file or a folder made on purpose. Everything
// else made on the PC (a folder, a vault) is made in the app's explorer, « ＋ Nouveau dossier ».
async function openCreatePath({ conversation } = {}) {
  const path = el("input", { required: true, maxlength: 1000, placeholder: "src/resultat.txt", "aria-label": "Nom ou chemin relatif" });
  const kind = el("select", { "aria-label": "Type à créer" }, el("option", { value: "directory", text: "Dossier" }), el("option", { value: "file", text: "Fichier vide" }));
  const parents = el("input", { type: "checkbox", checked: true });
  const password = el("input", { type: "password", autocomplete: "current-password", "aria-label": "Votre mot de passe AI Hub" });
  const error = el("p", { class: "form-error", role: "alert" });
  const submit = el("button", { type: "submit", class: "btn btn-primary", text: "Créer" });
  const dialog = el("dialog", { class: "dialog dialog-content" });
  const form = el("form", {}, el("h2", { text: "Créer dans le dossier de travail" }),
    el("p", { class: "quota-note", text: `Création sur le PC serveur, dans ${conversation.workdir}. Les accès de l’IA restent ceux que vous avez choisis.` }),
    el("label", {}, "Type", kind),
    el("label", {}, "Nom ou chemin relatif au dossier de travail", path),
    el("label", { class: "check" }, parents, "Créer aussi les dossiers parents manquants"),
    state.me ? el("label", {}, "Votre mot de passe AI Hub (laissez vide si votre compte n’en a pas)", password) : null,
    error, el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => dialog.close() }), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); submit.disabled = true; error.textContent = "";
    try {
      const result = await api("POST", "/api/filesystem/create", { path: path.value.trim(), kind: kind.value, parents: parents.checked,
        conversation_id: conversation.id, password: password.value });
      password.value = "";
      dialog.close();
      toast(result.created ? (result.kind === "file" ? "Fichier vide créé." : "Dossier créé.") : "Ce dossier existe déjà ; vous pouvez l’utiliser.");
    } catch (err) { error.textContent = err.message; password.value = ""; }
    finally { submit.disabled = false; }
  });
  dialog.append(form); dialog.addEventListener("close", () => { password.value = ""; dialog.remove(); }, { once: true });
  document.body.append(dialog); dialog.showModal(); path.focus();
}

// A question with several answers: resolves to the chosen value, or null when closed.
function choiceDialog({ title, text, choices }) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: "dialog dialog-content" });
    let answer = null;
    const finish = (value) => { answer = value; dialog.close(); dialog.remove(); resolve(value); };
    dialog.append(el("h2", { text: title }), el("p", { text }),
      el("div", { class: "dialog-actions" },
        el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => finish(null) }),
        ...choices.map((c) => el("button", { type: "button", class: c.primary ? "btn btn-primary" : "btn btn-ghost",
          text: c.label, onclick: () => finish(c.value) }))));
    dialog.addEventListener("close", () => { if (answer === null) { dialog.remove(); resolve(null); } }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}

function featureDialog(title, ...content) {
  const dialog = el("dialog", { class: "dialog dialog-content" }, el("h2", { text: title }), ...content);
  dialog.append(el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: () => dialog.close() })));
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog); dialog.showModal(); return dialog;
}

async function openMemoryRag() {
  const cid = state.currentId;
  const dialog = el('dialog', { class: 'dialog dialog-wide dialog-content' });
  const status = el('p', { class: 'quota-note' });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const query = el('input', { placeholder: 'Une décision, une information ou un sujet…', maxlength: 2000, 'aria-label': 'Chercher dans la mémoire' });
  const searchButton = el('button', { type: 'submit', class: 'btn btn-primary', text: 'Rechercher' });
  const searchForm = el('form', {}, el('label', {}, 'Chercher dans la mémoire', query), searchButton);
  const results = el('div');
  const documents = el('div');
  // A file of the PC, in the app's explorer (the owner and the admins); else one of this device.
  const KINDS = '.md,.txt,.csv,.json,.yaml,.yml';
  const fromDevice = async (file) => {
    if (file.size > 800000) { error.textContent = 'Document trop volumineux (800 Ko au maximum).'; return; }
    try { await associate(file.name, await file.text()); } catch (err) { error.textContent = err.message; }
  };
  const upload = el('button', { type: 'button', class: 'btn btn-ghost', text: 'Ajouter un fichier…', onclick: () => {
    if (!isAdmin()) {
      const input = el('input', { type: 'file', accept: KINDS, hidden: true });
      input.addEventListener('change', () => { const file = input.files?.[0]; input.remove(); if (file) fromDevice(file); });
      document.body.append(input); input.click();
      return;
    }
    openPathPicker({ title: 'Un fichier pour la mémoire du projet', files: KINDS.replace(/\./g, ''), action: 'Ajouter ce fichier',
      device: { accept: KINDS, onFiles: (chosen) => fromDevice(chosen[0]) },
      onPick: async (path) => {
        error.textContent = '';
        try {
          await api('POST', `/api/projects/${data.project_id}/memory/file`, { path });
          await refresh();
          toast('Document associé et indexé dans la mémoire du projet.');
        } catch (err) { error.textContent = err.message; }
      } });
  } });
  const name = el('input', { maxlength: 120, placeholder: 'Decisions.md', 'aria-label': 'Nom de la note' });
  const text = el('textarea', { rows: 4, maxlength: 200000, placeholder: 'Collez ici une note à garder pour ce projet.', 'aria-label': 'Texte de la note' });
  const add = el('button', { type: 'button', class: 'btn btn-primary', text: 'Associer cette note au projet' });
  const manage = el('section', {},
    el('p', { class: 'quota-note', text: 'Documents et notes ajoutés volontairement, accessibles aux personnes qui peuvent lire le projet. Texte uniquement : Markdown, TXT, CSV, JSON et YAML ; 200 000 caractères par document. Aucun coffre n’est parcouru automatiquement.' }),
    el('div', { class: 'field-row' }, upload),
    el('label', {}, 'Ou écrire une note — nom du fichier', name), text, add);
  let data = null;
  async function refresh() {
    data = await api('GET', `/api/conversations/${cid}/memory?q=${encodeURIComponent(query.value.trim())}`);
    status.textContent = `${data.indexed} passage(s) de cette conversation indexés. Recherche par mots active` +
      (data.semantic_model ? ` et par le sens (${data.semantic_model}).` : ' ; recherche par le sens disponible quand le modèle local Ollama est prêt.') +
      (data.project_id === null ? ' Aucun document de projet accessible dans cet espace.' : ' Documents explicitement associés au projet inclus.');
    manage.classList.toggle('hidden', !data.can_manage);
    documents.replaceChildren(...[el('h3', { text: 'Documents du projet' }),
      ...data.documents.map(doc => el('div', { class: 'field-row' },
        el('span', { text: `${doc.name} · ${doc.passages} passage(s)` + (doc.source_path
          ? (doc.source_missing ? ' · note introuvable : dernière copie gardée' : ' · suit la note du coffre') : '') }),
        data.can_manage ? el('button', { type: 'button', class: 'btn btn-danger-ghost btn-small', text: 'Retirer', onclick: async () => {
          if (!await confirmDialog({ title: 'Retirer ce document de la mémoire ?', text: `${doc.name} sera retiré des recherches futures du projet. Le fichier original et les réponses déjà produites restent inchangés.`, okText: 'Retirer' })) return;
          try { await api('DELETE', `/api/projects/${data.project_id}/memory/documents/${doc.id}`); await refresh(); }
          catch (err) { error.textContent = err.message; }
        } }) : null)),
      !data.documents.length ? el('p', { class: 'quota-note', text: 'Aucun document associé.' }) : null].filter(Boolean));
    results.replaceChildren(...[...data.passages.map(hit => el('article', {},
      el('h3', { text: `${hit.name} · passage ${hit.position}` }), el('pre', { class: 'result-diff', text: hit.content }))),
      ...data.conversations.map(hit => el('article', {}, el('h3', { text: `Conversation : ${hit.title}` }),
        el('p', { text: hit.excerpt.replace(/\[\[|\]\]/g, '') }))),
      query.value.trim() && !data.passages.length && !data.conversations.length ? el('p', { text: 'Aucun passage pertinent trouvé.' }) : null].filter(Boolean));
  }
  async function associate(filename, content) {
    if (!data?.can_manage) return;
    error.textContent = ''; add.disabled = true; upload.disabled = true;
    try {
      await api('POST', `/api/projects/${data.project_id}/memory/documents`, { name: filename, content });
      text.value = ''; name.value = ''; await refresh();
      toast('Document associé et indexé dans la mémoire du projet.');
    } catch (err) { error.textContent = err.message; }
    finally { add.disabled = false; upload.disabled = false; }
  }
  add.addEventListener('click', () => associate(name.value.trim(), text.value));
  searchForm.addEventListener('submit', async event => {
    event.preventDefault(); error.textContent = ''; searchButton.disabled = true;
    try { await refresh(); } catch (err) { error.textContent = err.message; }
    finally { searchButton.disabled = false; }
  });
  dialog.append(el('h2', { text: 'Mémoire RAG' }), status, searchForm, results, documents, manage, error,
    el('div', { class: 'dialog-actions' }, el('button', { type: 'button', class: 'btn btn-ghost', text: 'Fermer', onclick: () => dialog.close() })));
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  document.body.append(dialog); dialog.showModal();
  try { await refresh(); } catch (err) { error.textContent = err.message; }
}

async function openAccessOverview() {
  const cid = state.currentId;
  const conv = state.data?.conversation;
  if (!conv) return;
  try {
    const data = await api("GET", `/api/conversations/${cid}/isolation`);
    if (state.currentId !== cid) return;
    const p = data.policy;
    const details = el("dl", { class: "access-facts" });
    for (const [label, value] of [
      ["État", p.paused ? "Accès suspendus. Aucun nouveau travail ni connexion." : "Accès actifs"],
      ["Exécution", p.isolated ? "Espace isolé" : "Directement sur le PC"],
      ["Fichiers", p.isolated ? ({ none: "Aucun fichier du PC", read: "Lecture d’une copie", write: "Modification d’une copie ; application après vérification" }[p.files]) : "Accès du compte du PC selon les permissions choisies"],
      ["Sélection", p.isolated ? (p.paths.join("\n") || "Aucune") : (conv.workdir || "Sans dossier")],
      ["Réseau", p.isolated ? (p.network === "none" ? "Aucun réseau" : "Fournisseur IA seulement") : "Réseau du PC"],
      ["Mémoire", p.memory === "project" ? "Conversations partagées du projet" : "Cette conversation"],
      ["Compte IA", conv.my_role === "read" ? "Lecture seule : vous ne lancez pas d’IA" : p.isolated ? "Votre compte propre à cet espace et au fournisseur" : "Votre compte IA configuré sur le serveur"]
    ]) details.append(el("dt", { text: label }), el("dd", { text: value }));
    const actions = el("div", { class: "dialog-actions" });
    const error = el("p", { class: "form-error", role: "alert" });
    const dialog = featureDialog("Accès de cette IA", details, error, actions);
    if (p.isolated && !p.paused && conv.my_role !== "read") actions.append(el("button", { class: "btn btn-ghost", text: "Voir mon compte IA", onclick: () => { dialog.close(); openSpaceAccounts(); } }));
    if (conv.my_role === "owner") {
      actions.append(el("button", { class: "btn btn-ghost", text: "Modifier les accès", onclick: () => { dialog.close(); $("isolation-access").click(); } }));
      const cut = el("button", { class: p.paused ? "btn btn-primary" : "btn btn-danger", text: p.paused ? "Réactiver les accès" : "Tout couper", onclick: async () => {
        cut.disabled = true;
        try {
          const result = await api("POST", `/api/conversations/${cid}/isolation/${p.paused ? "resume" : "pause"}`, {});
          dialog.close(); await openConversation(cid);
          toast(p.paused ? "Accès réactivés." : "Accès suspendus ; arrêt du travail demandé. Les fichiers et comptes sont conservés.");
          if (result.warnings?.length) featureDialog("Arrêt à vérifier", el("p", { class: "form-error", text: "Les nouveaux lancements sont bloqués, mais un arrêt n’a pas été confirmé. Réessayez « Tout couper » ou vérifiez le moteur sur le serveur." }));
        } catch (err) { error.textContent = err.message; cut.disabled = false; }
      } }); actions.append(cut);
      if (p.paused) actions.append(el("button", { class: "btn btn-ghost", text: "Réessayer l’arrêt", onclick: async () => {
        try {
          const result = await api("POST", `/api/conversations/${cid}/isolation/pause`, {});
          error.textContent = result.warnings?.length ? "Un arrêt reste à vérifier sur le serveur." : "";
          if (!result.warnings?.length) toast("Arrêt demandé à nouveau ; accès toujours suspendus.");
        } catch (err) { error.textContent = err.message; }
      } }));
    }
  } catch (err) { toast(err.message); }
}

async function openDiagnostics() {
  const cid = state.currentId;
  const conv = state.data?.conversation;
  const body = el("div", { class: "diagnostic-checks" }, el("p", { text: "Vérification en cours…" }));
  const dialog = featureDialog("Vérifier le fonctionnement", body);
  try {
    const data = await api("GET", "/api/diagnostics");
    if (!dialog.open) return;
    body.replaceChildren(el("p", { class: "quota-note", text: `AI Hub ${data.version}. Aucun message n’est envoyé à une IA.` }));
    for (const c of data.checks) body.append(el("section", { class: "diagnostic-check" },
      el("strong", { text: `${c.ok ? "✓" : "!"} ${c.name}` }), el("p", { text: c.detail }),
      c.action ? el("p", { class: "quota-note", text: c.action }) : null));
    if (cid && conv?.isolation?.isolated && conv.my_role !== "read") {
      body.append(el("p", { text: "Le compte IA doit être connecté dans cet espace, même si un autre espace est déjà connecté." }),
        el("button", { class: "btn btn-ghost", text: "Vérifier le compte de cet espace", onclick: () => { dialog.close(); openSpaceAccounts(); } }));
      if (conv.isolation.paused) body.append(el("p", { text: "Cet espace est suspendu : réactivez ses accès avant de vérifier sa connexion." }));
      else if (!state.data?.running && data.checks.find(c => c.id === "engine")?.ok) {
        const model = conv.model || conv.conductor_model || "default";
        const provider = model === "codex" || model.startsWith("gpt-") ? "codex" : model.startsWith("local:") ? null : "claude_code";
        if (provider) {
          try {
            const account = await api("GET", `/api/conversations/${cid}/sandbox/accounts?provider=${provider}`);
            if (dialog.open && state.currentId === cid) body.append(el("p", { text: account.signed_in ? "✓ Le compte IA de cet espace est connecté." : "! Aucun compte IA connecté dans cet espace. Utilisez le bouton de connexion ci-dessus." }));
          } catch { if (dialog.open) body.append(el("p", { text: "Le compte de cet espace n’a pas pu être vérifié. Ouvrez ses comptes pour connaître la prochaine étape." })); }
        }
      }
    } else body.append(el("button", { class: "btn btn-ghost", text: "Vérifier mes comptes IA", onclick: () => { dialog.close(); openAccounts(false); } }));
  } catch (err) { body.replaceChildren(el("p", { class: "form-error", text: err.message })); }
}

// Three steps to begin (1.2), then where the rest is: nothing else is needed to start.
function openGettingStarted() {
  const steps = el("ol", { class: "getting-started-steps" });
  const more = el("details", { class: "getting-started-more" }, el("summary", { text: "Pour aller plus loin" }),
    el("ul", {}, ...[
      "Joindre un fichier : 📎 dans la zone de saisie. Changer d'IA ou ce qu'elle peut faire sans demander : les deux listes à côté.",
      "Renommer, ranger dans un projet, donner des consignes, archiver, exporter : le menu ⋯ en haut de la conversation.",
      "Le reste (plusieurs IA, travail autonome, comparer, planifier, espaces isolés, partage, coffres) : Réglages › Fonctions avancées.",
    ].map((text) => el("li", { text }))));
  const dialog = featureDialog("Bien démarrer", el("p", { class: "quota-note", text: "Trois pas suffisent pour commencer." }), steps, more);
  try { localStorage.setItem("aihub-guide-seen", "1"); } catch { /* optional device preference */ }
  const creator = canCreate();
  for (const [title, text, action, label] of [
    ["Vérifier vos IA", "Le diagnostic regarde si Claude, ChatGPT (Codex) ou l'IA locale répondent, sans rien leur envoyer.", openDiagnostics, "Vérifier"],
    ...(creator ? [
      ["Écrire votre demande", "Sur l'accueil, écrivez ce que vous voulez et choisissez l'IA, puis la flèche ↑ : la conversation se crée toute seule, avec son propre dossier si vous n'en choisissez pas.", () => goHome(), "Écrire ma demande"],
      ["Ranger avec les projets", "Un projet regroupe des conversations, leur dossier de travail et leurs consignes. Facultatif : utile quand le travail s'étale.", () => openProjectDialog(null), "Créer un projet"]] : [])
  ]) steps.append(el("li", {}, el("h3", { text: title }), el("p", { text }), el("button", { class: "btn btn-primary", text: label, onclick: () => { dialog.close(); action(); } })));
}

async function openSpaceAccounts(selectedProvider = null) {
  const cid = state.currentId;
  const dialog = el("dialog", { class: "dialog dialog-content" });
  const provider = el("select", { "aria-label": "Compte IA de l’espace" },
    el("option", { value: "claude_code", text: "Claude" }), el("option", { value: "codex", text: "ChatGPT / Codex" }));
  const current = state.data.conversation.model || state.data.conversation.conductor_model || "default";
  provider.value = current === "codex" || current.startsWith("gpt-") ? "codex" : "claude_code";
  if (selectedProvider === 'codex' || selectedProvider === 'claude_code') provider.value = selectedProvider;
  const readOnly = el("input", { type: "checkbox" });
  const body = el("div"); const error = el("p", { class: "form-error", role: "alert" });
  let closed = false, timer = null, lastView = null;
  const target = () => ({ provider: provider.value, read_only: readOnly.checked });
  const path = (action) => `/api/conversations/${cid}/sandbox/${action}`;
  const close = el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: () => dialog.close() });
  const render = (data) => {
    const login = data.login || (data.state ? data : null);
    const signature = JSON.stringify([target(), data.prepared, data.signed_in, login?.state, login?.url, login?.code, login?.needs_code, login?.error, data.error]);
    if (signature === lastView) {
      if (login?.state === "waiting" || login?.state === "starting") timer = setTimeout(refresh, 2500);
      return; // preserve the code field and its focus while the person types
    }
    lastView = signature;
    body.replaceChildren(); error.textContent = data.error || "";
    if (data.signed_in || login?.state === "ok") {
      body.append(el("p", { class: "pill ok", text: "Compte connecté dans cet espace" }));
      body.append(el("button", { type: "button", class: "btn btn-ghost", text: "Déconnecter cet espace",
        onclick: async () => { try { await api("POST", path("logout"), target()); await refresh(); } catch (err) { error.textContent = err.message; } } }));
    } else if (login?.state === "waiting" || login?.state === "starting") {
      body.append(el("p", { text: login.state === "starting" ? "Préparation de la connexion…" : "Terminez la connexion sur la page du fournisseur." }));
      if (login.url) body.append(el("a", { href: login.url, target: "_blank", rel: "noopener noreferrer", class: "btn btn-primary", text: "Ouvrir la page de connexion" }));
      if (login.code) body.append(el("p", {}, "Code à saisir sur cette page : ", el("strong", { text: login.code })));
      if (login.needs_code) {
        const input = el("input", { type: "password", autocomplete: "off", maxlength: 512, "aria-label": "Code de connexion Claude" });
        const send = el("button", { type: "button", class: "btn btn-primary", text: "Valider le code", onclick: async () => {
          send.disabled = true;
          try { render(await api("POST", path("code"), { ...target(), code: input.value })); input.value = ""; }
          catch (err) { error.textContent = err.message; } finally { send.disabled = false; }
        } });
        body.append(el("label", {}, "Code fourni par Claude", input), send);
      }
      body.append(el("button", { type: "button", class: "btn btn-ghost", text: "Annuler la connexion", onclick: async () => {
        try { await api("POST", path("cancel-login"), target()); await refresh(); } catch (err) { error.textContent = err.message; }
      } }));
      timer = setTimeout(refresh, 2500);
    } else {
      if (login?.error) error.textContent = login.error;
      body.append(el("p", { text: data.prepared ? "Cet espace attend votre connexion." : "Préparez cet espace, puis connectez votre compte." }));
      const start = el("button", { type: "button", class: "btn btn-primary", text: "Préparer et me connecter", onclick: async () => {
        start.disabled = true; start.textContent = "Préparation…";
        try { render(await api("POST", path("login"), target())); }
        catch (err) { error.textContent = err.message; start.disabled = false; start.textContent = "Réessayer"; }
      } }); body.append(start);
    }
  };
  const refresh = async () => {
    clearTimeout(timer); if (closed) return;
    try { render(await api("GET", path("accounts") + `?provider=${provider.value}&read_only=${readOnly.checked}`)); }
    catch (err) { render({ prepared: false, error: err.message }); }
  };
  for (const field of [provider, readOnly]) field.addEventListener("change", refresh);
  dialog.append(el("h2", { text: "Vos comptes dans cet espace" }),
    el("p", { class: "quota-note", text: "Cette connexion appartient à votre compte AI Hub et à cette conversation. Effectuez vous-même la connexion sur la page du fournisseur." }),
    el("label", {}, "Fournisseur", provider),
    state.data.conversation.isolation.files === "write" ? el("label", { class: "check" }, readOnly, "Compte des réponses en lecture seule : comparaisons et résumés") : null,
    body, error, el("div", { class: "dialog-actions" }, close));
  dialog.addEventListener("close", () => { closed = true; clearTimeout(timer); dialog.remove(); }, { once: true });
  document.body.append(dialog); dialog.showModal(); await refresh();
}

async function openSpaceResults() {
  const cid = state.currentId;
  const dialog = el("dialog", { class: "dialog dialog-wide dialog-content" });
  const provider = el("select", { "aria-label": "Fournisseur des résultats" },
    el("option", { value: "claude_code", text: "Claude" }), el("option", { value: "codex", text: "Codex" }));
  const model = state.data.conversation.model || state.data.conversation.conductor_model || "default";
  provider.value = model === "codex" || model.startsWith("gpt-") ? "codex" : "claude_code";
  const list = el("div", { class: "result-files" }); const error = el("p", { class: "form-error", role: "alert" });
  const download = el("a", { class: "btn btn-ghost", text: "Récupérer la copie (.zip)" });
  const apply = el("button", { type: "button", class: "btn btn-primary", text: "Appliquer la sélection" });
  const note = el("p", { class: "quota-note" }); let selected = new Map();
  const load = async () => {
    error.textContent = ""; list.textContent = "Lecture des résultats…"; selected = new Map(); apply.disabled = true;
    download.href = `/api/conversations/${cid}/sandbox/results/download?provider=${provider.value}`;
    try {
      const data = await api("GET", `/api/conversations/${cid}/sandbox/results?provider=${provider.value}`);
      list.replaceChildren();
      const counts = data.files.reduce((out, file) => { out[file.kind] = (out[file.kind] || 0) + 1; return out; }, {});
      list.append(el("p", { class: "results-summary", role: "status", text: `${counts.new || 0} créé(s) · ${counts.modified || 0} modifié(s) · ${counts.deleted || 0} supprimé(s). Rien n’est appliqué sans votre choix.` }));
      apply.classList.toggle("hidden", !data.can_apply || state.data.conversation.my_role !== "owner");
      note.textContent = data.can_apply ? `Dossier original : ${data.workdir}. Une copie de sécurité est conservée à chaque application.`
        : "Les fichiers originaux restent inchangés. Récupérez les résultats pour les utiliser.";
      if (!data.files.length) list.append(el("p", { text: "Aucun changement de fichier dans cet espace pour le moment." }));
      for (const file of data.files) {
        const resultProvider = provider.value;
        const checkbox = el("input", { type: "checkbox" });
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selected.set(file.name, file); else selected.delete(file.name);
          apply.disabled = !selected.size;
        });
        const detail = el("details", {}, el("summary", { text: "Voir les changements" }));
        let loaded = false;
        detail.addEventListener("toggle", async () => {
          if (!detail.open || loaded) return; loaded = true;
          const preview = el("p", { text: "Lecture de l’aperçu…" }); detail.append(preview);
          try {
            const view = await api("GET", `/api/conversations/${cid}/sandbox/results/preview?provider=${resultProvider}&name=${encodeURIComponent(file.name)}`);
            preview.replaceWith(view.binary ? el("p", { class: "quota-note", text: view.note })
              : el("pre", { class: "result-diff", text: view.diff || "Contenu identique à l’original actuel." }));
          } catch (err) { preview.textContent = err.message; loaded = false; }
        });
        list.append(el("div", { class: "result-file" }, el("label", { class: "check" }, checkbox, el("span", {},
          el("strong", { text: file.name }), el("span", { class: "conv-item-sub", text: `${{ new: "Nouveau", modified: "Modifié", deleted: "Supprimé" }[file.kind]} · ${file.size} octets` }))), detail));
      }
    } catch (err) { list.replaceChildren(); error.textContent = err.message; }
  };
  apply.addEventListener("click", async () => {
    const files = [...selected.values()];
    const deletes = files.some((f) => f.kind === "deleted");
    if (!await confirmDialog({ title: "Appliquer les fichiers sélectionnés ?", text: `${files.length} fichier(s) seront appliqués au dossier original. ${deletes ? "La sélection comprend des suppressions. " : ""}Une sauvegarde des originaux est conservée.`, okText: "Appliquer", check: deletes ? "Je confirme les suppressions sélectionnées" : undefined })) return;
    apply.disabled = true;
    try {
      await api("POST", `/api/conversations/${cid}/sandbox/results/apply`, { provider: provider.value,
        files: Object.fromEntries(files.map((f) => [f.name, f.sha256])), confirm_delete: deletes });
      toast("Fichiers appliqués ; copie de sécurité conservée."); await load();
    } catch (err) { error.textContent = err.message; apply.disabled = false; }
  });
  provider.addEventListener("change", load);
  dialog.append(el("h2", { text: "Fichiers produits dans l’espace" }), el("label", {}, "Fournisseur", provider), note, list, error,
    el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: () => dialog.close() }), download, apply));
  dialog.addEventListener("close", () => dialog.remove(), { once: true }); document.body.append(dialog); dialog.showModal(); await load();
}

// --- projects ---------------------------------------------------------------------------

function setupProjectDialog() {
  const dialog = $("project-dialog");
  const accessEditor = isolationEditor(false, () => ({ base: $("project-workdir").value.trim() }));
  accessEditor.node.classList.add("adv");  // isolated spaces: « Fonctions avancées »
  $("project-instructions").closest("label").before(accessEditor.node);
  let editing = null;  // the project being edited, null for a new one
  openProjectDialog = (project = null) => {
    editing = project;
    $("project-form").reset();
    $("project-dialog-title").textContent = project ? `Projet « ${project.name} »` : "Nouveau projet";
    $("project-name").value = project ? project.name : "";
    $("project-description").value = project ? project.description || "" : "";
    $("project-workdir").value = project ? project.workdir || "" : "";
    $("project-instructions").value = project ? project.instructions || "" : "";
    $("project-vault").value = project ? project.obsidian_vault || "" : "";
    $("project-vault-row").classList.toggle("hidden", !isOwner());  // a folder of the owner's PC
    accessEditor.set(project?.isolation || { isolated: false });
    $("project-delete").classList.toggle("hidden", !project);
    $("project-delete-all").classList.toggle("hidden", !project || !project.conversations);
    $("project-share").classList.toggle("hidden", !project || !state.me);
    $("project-export").classList.toggle("hidden", !project);
    if (project) $("project-export").href = `/api/projects/${project.id}/export`;
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
      workdir: $("project-workdir").value.trim(), instructions: $("project-instructions").value,
      isolation: accessEditor.value(), ...(isOwner() ? { obsidian_vault: $("project-vault").value.trim() } : {}) };
    try {
      const made = editing ? null : (await api("POST", "/api/projects", body)).project;
      if (editing) await api("PATCH", `/api/projects/${editing.id}`, body);
      dialog.close();
      await loadConversations();
      if (state.data) renderConversation();
      else if (made) renderHome(made.id);  // made from the home: chosen there for the next request
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
        saveDraft(state.currentId, box.value);
        fitPrompt();
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
  // Without a folder typed, the project's own (shown as the placeholder) is the one used.
  const accessEditor = isolationEditor(true, () => {
    const project = state.projects.find((p) => String(p.id) === $("new-project-select").value);
    return { base: $("new-workdir").value.trim() || project?.workdir || "" };
  });
  modeSelect.closest("label").before(accessEditor.node);
  const showAccessModels = () => {
    const isolated = accessEditor.effective()?.isolated;
    const models = (isolated ? state.options.isolated_models : state.options.models) || [];
    for (const [id, fallback] of [["new-model", state.options.default_model], ["new-conductor", state.options.default_conductor]]) {
      const chosen = $(id).value;
      $(id).replaceChildren(...models.map((m) => el("option", { value: m, text: aiName(m) })));
      $(id).value = models.includes(chosen) ? chosen : models.includes(fallback) ? fallback : models[0] || "";
    }
    $("new-workdir-row").classList.toggle("hidden", !isAdmin() || (isolated && accessEditor.effective()?.files === "none"));
    const bypass = $("new-mode").querySelector('option[value="bypass"]');
    if (bypass) bypass.disabled = !isAdmin() && !isolated;
  };
  accessEditor.node.addEventListener("change", () => { showAccessModels(); showWarning(); });
  const showWarning = () => {
    const bypass = modeSelect.value === "bypass";
    $("new-bypass-warning").classList.toggle("hidden", !bypass);
    $("new-bypass-text").textContent = accessEditor.effective()?.isolated
      ? "L’IA pourra agir sans demander dans son espace isolé. Les accès choisis restent imposés et les fichiers originaux ne sont pas modifiés."
      : state.options.bypass_warning;
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
    accessEditor.parent(project?.isolation);
    showAccessModels();
    $("new-workdir").placeholder = project && project.workdir
      ? `${project.workdir} (dossier du projet)` : "D:\\projets\\mon-projet";
  };
  $("new-project-select").addEventListener("change", showFolder);
  // `carry`: the choices made on the home ({ model, workdir }), kept over this device's last ones.
  openNewDialog = async (projectId = null, profile = null, carry = {}) => {
    if (carry.model === undefined) state.homeCarry = null;  // not from the home: nothing written to carry
    form.reset();
    $("new-advanced").open = false;
    accessEditor.set(null);
    $("template-variables").replaceChildren();
    $("template-description").classList.add("hidden");
    $("delete-template").classList.add("hidden");
    $("new-project-select").replaceChildren(...projectOptions(projectId, "Aucun projet"));
    // The last choices of this device come back: the folder, the AI, the way of working.
    const last = rememberedChoices();
    for (const name of ["workdir", "model", "mode", "conductor_model", "assignment_mode"]) {
      const field = form.elements[name];
      if (!field || last[name] === undefined) continue;
      if (field.tagName === "SELECT" && ![...field.options].some((o) => o.value === last[name])) continue;
      if (name === "workdir" && projectId !== null) continue;  // the project's folder comes first
      field.value = last[name];
    }
    if (carry.workdir) form.elements.workdir.value = carry.workdir;
    else if (carry.model !== undefined) form.elements.workdir.value = "";  // from the home: its folder (automatic)
    $("new-error").textContent = "";
    showWarning();
    showKind();
    showFolder();
    if (carry.model && [...$("new-model").options].some((o) => o.value === carry.model)) $("new-model").value = carry.model;
    if (profile === "discussion") { accessEditor.setPreset("discussion"); $("new-advanced").open = true; }
    // A member: a folder of theirs on the server, chosen by the hub; never without permission.
    showAccessModels();
    const bypass = $("new-mode").querySelector('option[value="bypass"]');
    if (bypass) bypass.disabled = !isAdmin() && !accessEditor.effective()?.isolated;
    dialog.showModal();
    try {
      state.templates = (await api("GET", "/api/templates")).templates;
      $("new-template").replaceChildren(el("option", { value: "", text: "Non : conversation libre" }),
        ...state.templates.map((t) => el("option", { value: t.id, text: t.title })));
    } catch (err) { $("new-error").textContent = err.message; }
  };
  $("new-template").addEventListener("change", () => {
    const item = state.templates.find((t) => t.id === $("new-template").value);
    $("template-variables").replaceChildren();
    $("template-description").classList.toggle("hidden", !item);
    $("delete-template").classList.toggle("hidden", !item || item.builtin);
    if (!item) return;
    $("template-description").textContent = item.prompt;
    for (const name of item.variables) {
      const input = el("input", { required: true, maxlength: "4000" });
      input.dataset.variable = name;
      $("template-variables").append(el("label", {}, name, input));
    }
    $("new-kind").value = item.settings.mode;
    $("new-mode").value = item.settings.permission_mode;
    for (const id of ["new-model", "new-conductor"]) {
      const model = item.settings.model;
      $(id).value = [...$(id).options].some((o) => o.value === model) ? model : $(id).options[0]?.value || "";
    }
    if (!form.elements.title.value) form.elements.title.value = item.title;
    // What the model changes beyond the AI is shown, not left in a closed section.
    if (item.settings.mode !== "single" || item.settings.permission_mode !== "manual") $("new-advanced").open = true;
    showWarning(); showKind();
  });
  $("delete-template").addEventListener("click", async () => {
    const item = state.templates.find((t) => t.id === $("new-template").value);
    if (!item || item.builtin || !await confirmDialog({ title: "Supprimer ce modèle ?", text: item.title, okText: "Supprimer" })) return;
    try {
      state.templates = (await api("DELETE", `/api/templates/${item.id}`)).templates;
      $("new-template").querySelector(`option[value="${item.id}"]`).remove();
      $("new-template").value = "";
      $("new-template").dispatchEvent(new Event("change"));
    } catch (err) { $("new-error").textContent = err.message; }
  });
  $("new-conversation").addEventListener("click", () => goHome());
  form.addEventListener("submit", async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    const data = new FormData(form);
    const body = {
      title: data.get("title").trim() || NEW_TITLE,  // named by its first request
      workdir: data.get("workdir").trim(),
      project_id: data.get("project_id") ? Number(data.get("project_id")) : null,
      model: data.get("model"),
      mode: data.get("mode"),
      conductor_model: data.get("conductor_model"),
      assignment_mode: data.get("mode") === "orchestration" ? data.get("assignment_mode") : "manual",
      permission_mode: data.get("permission_mode"),
      confirm_bypass: data.get("confirm_bypass") === "on",
      template_id: $("new-template").value || null,
      variables: Object.fromEntries([...$("template-variables").querySelectorAll("input")].map((input) => [input.dataset.variable, input.value])),
      isolation: accessEditor.value(),
    };
    if (accessEditor.effective()?.isolated && accessEditor.effective()?.files === "none") body.workdir = "";
    if (body.permission_mode === "bypass" && !body.confirm_bypass) {
      $("new-error").textContent = "Cochez la case pour confirmer le mode sans permission.";
      return;
    }
    try {
      const { conversation, prompt: initialPrompt } = await api("POST", "/api/conversations", body);
      // Never the mode without permission: that one is confirmed each time.
      rememberChoices({ model: body.model, mode: body.mode, conductor_model: body.conductor_model,
        assignment_mode: body.assignment_mode, ...(body.workdir && !body.project_id ? { workdir: body.workdir } : {}) });
      dialog.close();
      if ($("list-scope").value === "archived") $("list-scope").value = "active";
      await loadConversations();
      await openConversation(conversation.id);
      // The model's request, else what was written on the home: in the composer, to read before sending.
      const carried = initialPrompt || state.homeCarry || "";
      state.homeCarry = null;
      if (carried) {
        $("prompt").value = carried; saveDraft(conversation.id, carried); fitPrompt();
        if (!initialPrompt) $("home-prompt").value = "";
      }
      $("prompt").focus();
    } catch (err) {
      $("new-error").textContent = err.message;
    }
  });
}

// --- Obsidian (1.2.2): link a conversation to a vault, a new one or one already on the PC ----------

function setupObsidianDialog() {
  $('memory-rag').addEventListener('click', openMemoryRag);
  $("obsidian").addEventListener("click", () => openObsidian());
}

const folderName = (path) => String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(path || "");

// The note made in the conversation's vault: an existing note is never taken without asking.
async function activateNote(cid) {
  const found = await api("GET", `/api/conversations/${cid}/obsidian/check`);
  let existing = null;
  if (found.exists) {
    existing = await choiceDialog({
      title: "Cette note existe déjà",
      text: `« ${found.note} » est déjà dans le coffre` + (found.used_by.length
        ? `, et la conversation « ${found.used_by.join(" », « ")} » y écrit aussi.` : ", peut-être d'une ancienne conversation.")
        + ` Continuer cette note, ou en créer une nouvelle (« ${found.free} ») ?`,
      choices: [{ value: "continue", label: "Continuer cette note" }, { value: "new", label: "Créer une nouvelle note", primary: true }] });
    if (!existing) return false;
  }
  await api("PATCH", `/api/conversations/${cid}`, { obsidian: true, ...(existing ? { obsidian_existing: existing } : {}) });
  return true;
}

// That vault becomes the conversation's own, then its note is made there.
async function linkVault(cid, path) {
  await api("PATCH", `/api/conversations/${cid}`, { obsidian_vault: path });
  const linked = await activateNote(cid);
  if (state.currentId === cid) await refreshConversation();
  if (linked) toast(`Reliée à Obsidian : coffre « ${folderName(path)} ».`);
  return linked;
}

async function openObsidian() {
  const cid = state.currentId;
  const body = el("div", { class: "obsidian-body" });
  const error = el("p", { class: "form-error", role: "alert" });
  const actions = el("div", { class: "dialog-actions" });
  const dialog = el("dialog", { class: "dialog dialog-content obsidian-dialog" },
    el("h2", { text: "Obsidian" }), body, error, actions);
  const close = el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: () => dialog.close() });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  const run = async (work) => {
    error.textContent = "";
    try { return await work(); } catch (err) { error.textContent = err.message; return null; }
  };
  const what = el("p", { class: "quota-note", text: "Ce qui va dans la note : la création de la conversation, chaque plan "
    + "terminé, et les réponses que vous choisissez (« Noter dans Obsidian » sous une réponse). La conversation elle-même n'y va pas." });

  // The two ways: a vault made now, or one already on the PC.
  const choose = () => {
    body.replaceChildren(el("p", { text: "Reliez cette conversation à un coffre Obsidian :" }),
      el("div", { class: "obsidian-choices" },
        el("button", { type: "button", class: "obsidian-choice", onclick: createNew },
          el("strong", { text: "Créer un nouveau coffre" }), el("span", { text: "Un dossier neuf sur ce PC, prêt à ouvrir dans Obsidian" })),
        el("button", { type: "button", class: "obsidian-choice", onclick: pickExisting },
          el("strong", { text: "Choisir un coffre existant" }), el("span", { text: "Un coffre déjà sur ce PC" }))),
      what);
    actions.replaceChildren(close);
  };

  const createNew = async () => {
    // Beside the vaults already known, named after the conversation.
    const known = await run(() => api("GET", "/api/obsidian/vaults"));
    const first = (known?.vaults || []).find((v) => v.exists);
    const title = state.data.conversation.title.replace(/[<>:"/\\|?*]/g, "-").trim().slice(0, 60) || "Coffre";
    const start = first ? first.path.replace(/[\\/][^\\/]+[\\/]?$/, "") : "";
    openPathPicker({ title: "Où créer le nouveau coffre ?", start, action: null, make: { kind: "vault", name: title,
      onMade: async (made) => { if (await run(() => linkVault(cid, made.path))) dialog.close(); else render(); } } });
  };

  const pickExisting = async () => {
    body.replaceChildren(el("p", { text: "Recherche des coffres de ce PC…" }));
    actions.replaceChildren(el("button", { type: "button", class: "btn btn-ghost", text: "← Retour", onclick: render }), close);
    const found = await run(() => api("GET", "/api/obsidian/vaults"));
    if (!found || !dialog.open) return;
    const usable = found.vaults.filter((v) => v.exists);
    // Outside the allowed folders of the settings: shown, never linked (aihub/places.py).
    const list = el("div", { class: "obsidian-vaults" }, ...usable.map((v) => el("button", { type: "button", class: "obsidian-vault",
      title: v.path, disabled: !v.allowed, onclick: async () => {
        if (await run(() => linkVault(cid, v.path))) dialog.close(); else render();
      } }, el("strong", { text: v.name }), el("span", { text: v.allowed ? v.path : `${v.path} · hors des dossiers autorisés` }))));
    body.replaceChildren(el("p", { text: usable.length ? "Choisissez le coffre :" : "Aucun coffre trouvé sur ce PC." }), list,
      el("button", { type: "button", class: "btn btn-ghost btn-small", text: "Un autre dossier…",
        onclick: () => openPathPicker({ title: "Choisir un coffre Obsidian", action: "Relier ce coffre",
          vaultsOnly: true, onPick: async (path) => {
            if (await run(() => linkVault(cid, path))) dialog.close();
          } }) }));
  };

  const render = () => {
    const conv = state.data?.conversation;
    if (!conv || state.currentId !== cid) { dialog.close(); return; }
    if (conv.obsidian_note) {
      body.replaceChildren(el("p", { class: "obsidian-linked" }, "Reliée au coffre ", el("strong", { text: `« ${folderName(conv.notes_vault)} »` })),
        el("p", { class: "quota-note", text: `${conv.notes_vault} · note « ${conv.obsidian_note} »` }), what);
      actions.replaceChildren(
        el("button", { type: "button", class: "btn btn-danger-ghost adv", text: "Réécrire la note", onclick: rewrite }),
        el("button", { type: "button", class: "btn btn-ghost", text: "Délier", onclick: async () => {
          if (await run(() => api("PATCH", `/api/conversations/${cid}`, { obsidian: false }))) { await refreshConversation(); render(); }
        } }),
        isOwner() ? el("button", { type: "button", class: "btn btn-ghost", text: "Changer de coffre", onclick: choose }) : null,
        close);
    } else if (isOwner()) {
      choose();
    } else if (conv.notes_vault) {
      // An admin: the vault is the owner's choice; the note goes to the one already set.
      body.replaceChildren(el("p", { text: `Coffre choisi par le propriétaire : « ${folderName(conv.notes_vault)} ».` }), what);
      actions.replaceChildren(close, el("button", { type: "button", class: "btn btn-primary", text: "Relier", onclick: async () => {
        if (await run(() => activateNote(cid))) { await refreshConversation(); render(); }
      } }));
    } else {
      body.replaceChildren(el("p", { text: "Le propriétaire du serveur n'a pas encore choisi de coffre Obsidian." }));
      actions.replaceChildren(close);
    }
  };

  const rewrite = async () => {
    const ok = await confirmDialog({
      title: "Réécrire toute la note ?",
      text: "La note sera reconstruite : création, chaque plan exécuté et chaque réponse notée, dans l'ordre. "
        + "Ce que vous y avez écrit vous-même sera remplacé. L'ancienne version est d'abord sauvegardée "
        + "dans le dossier data/obsidian-sauvegardes de l'appli.",
      okText: "Réécrire",
      check: "J'ai compris : la note actuelle sera remplacée",
    });
    if (!ok) return;
    const r = await run(() => api("POST", `/api/conversations/${cid}/obsidian/rewrite`));
    if (r) toast(r.backup ? `Note réécrite. Ancienne version : ${r.backup}` : "Note réécrite.");
  };

  render();
}

// --- conversation actions ---------------------------------------------------------------

function setupInstructions() {
  $("instructions").addEventListener("click", async () => {
    const conv = state.data.conversation;
    const text = await askText("Consignes de cette conversation",
      "L'IA les reçoit à chaque demande (après celles du projet, s'il en a). Ex. : « Réponds en français, sois bref », "
      + "« Tu es relecteur de code : pointe les bugs, ne réécris pas tout ». Vide : aucune.", conv.instructions || "");
    if (text === null) return;
    try {
      await api("PATCH", `/api/conversations/${conv.id}`, { instructions: text });
      await refreshConversation();
      toast(text.trim() ? "Consignes enregistrées." : "Consignes retirées.");
    } catch (err) { toast(err.message); }
  });
}

function setupOrganisation() {
  for (const [button, field] of [["pin-conversation", "pinned"], ["archive-conversation", "archived"]]) {
    $(button).addEventListener("click", async () => {
      const conv = state.data?.conversation;
      if (!conv) return;
      try {
        await api("PATCH", `/api/conversations/${conv.id}/preferences`, { [field]: !conv[field] });
        await Promise.all([loadConversations(), refreshConversation()]);
        toast(field === "archived" ? (conv.archived ? "Conversation sortie de vos archives." : "Conversation archivée pour vous.")
          : (conv.pinned ? "Conversation désépinglée." : "Conversation épinglée pour vous."));
      } catch (err) { toast(err.message); }
    });
  }
  $("save-template").addEventListener("click", async () => {
    const conv = state.data?.conversation;
    if (!conv) return;
    const title = await askText("Modèle de travail", "Nom du modèle. La demande écrite, les consignes, l'IA et les outils seront gardés.", conv.title);
    if (!title) return;
    try {
      await api("POST", "/api/templates", { conversation_id: conv.id, title, prompt: $("prompt").value });
      toast("Modèle gardé. Retrouvez-le dans Nouvelle conversation ; {{champ}} crée un champ à remplir.");
    } catch (err) { toast(err.message); }
  });
  const messages = $("messages");
  messages.addEventListener("scroll", () => $("latest-message").classList.toggle("hidden",
    messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80), { passive: true });
  $("latest-message").addEventListener("click", () => { messages.scrollTop = messages.scrollHeight; });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest?.(".code-copy");
    if (!button) return;
    const code = button.closest(".code-block").querySelector("code").textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(code);
      else fallbackCopy(code);
      toast("Code copié.");
    } catch { toast("Copie impossible dans ce navigateur."); }
  });
}

function setupActions() {
  $("composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = state.pendingFiles || [];
    if (files.some((f) => f.uploading)) { toast("Attendez la fin de l'envoi des fichiers."); return; }
    // Files alone: a request that says so.
    const prompt = $("prompt").value.trim() || (files.length ? "Voici les fichiers joints." : "");
    const kind = (event.submitter || primaryButton()).value || "plan";
    const contextualPlan = state.data?.conversation.mode === 'orchestration' && kind === 'plan';
    if ((!prompt && !contextualPlan) || state.currentId === null) return;
    $("send").disabled = true;
    $("discuss").disabled = true;
    const sentTo = state.currentId;  // another conversation may be open when the answer comes
    // Made without a title (« Plus d'options… »): its first request names it.
    const untitled = state.data && state.data.conversation.title === NEW_TITLE && !state.data.messages.length
      && (state.data.conversation.my_role || "owner") === "owner" && prompt;
    try {
      const compareWith = $("compare-row").classList.contains("hidden") ? null : $("compare-with").value || null;
      await api("POST", `/api/conversations/${sentTo}/messages`, { prompt, kind, attachments: files.map((f) => f.id),
        compare_with: compareWith });
      $("compare-with").value = "";  // one comparison at a time: chosen again for the next
      renderCompareChip();
      if (untitled) api("PATCH", `/api/conversations/${sentTo}`, { title: titleFrom(prompt) })
        .then(() => loadConversations()).catch(() => { /* the title stays « Nouvelle conversation » */ });
      saveDraft(sentTo, "");
      if (state.currentId === sentTo) { $("prompt").value = ""; fitPrompt(); state.pendingFiles = []; renderPending(); }
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
      await refreshConversation();
    } catch (err) {
      toast(err.message);
      renderRunning();
    }
  });
  // With a keyboard: Entrée sends, Maj+Entrée goes to the line. On a phone, Entrée stays a new line.
  $("prompt").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    if (event.ctrlKey || event.metaKey || (KEYBOARD && !event.shiftKey)) {
      event.preventDefault();
      // The button Entrée stands for: « Discuter avec le chef » while a plan waits.
      const button = primaryButton();
      if (!button.disabled) $("composer").requestSubmit(button);
    }
  });
  $("prompt").addEventListener("input", () => { fitPrompt(); saveDraft(state.currentId, $("prompt").value); });
  $("compare-with").addEventListener("change", () => { renderCompareChip(); closeMenus(); $("prompt").focus(); });
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

// The person's password again, for making something on the PC (null: cancelled).
function askPassword(title) {
  return new Promise((resolve) => {
    const input = el("input", { type: "password", autocomplete: "current-password", "aria-label": "Votre mot de passe AI Hub" });
    const dialog = el("dialog", { class: "dialog dialog-content" });
    let answer = null;
    const finish = (value) => { answer = value; dialog.close(); };
    const form = el("form", {}, el("h2", { text: title }),
      el("label", {}, "Votre mot de passe AI Hub (laissez vide si votre compte n’en a pas)", input),
      el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => finish(null) }),
        el("button", { type: "submit", class: "btn btn-primary", text: "Confirmer" })));
    form.addEventListener("submit", (event) => { event.preventDefault(); finish(input.value); });
    dialog.append(form);
    dialog.addEventListener("close", () => { input.value = ""; dialog.remove(); resolve(answer); }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}

// « Exporter » (1.2.3): the owner and the admins may save it into a folder of the PC, chosen in the
// explorer (a new file, never over another one); anyone may download it on their device.
function setupExport() {
  $("export-md").addEventListener("click", async (event) => {
    if (!isAdmin()) return;  // a download, as before
    event.preventDefault();
    const conv = state.data?.conversation;
    if (!conv) return;
    const href = $("export-md").href;
    const choice = await choiceDialog({ title: "Exporter la conversation", text: "Un fichier Markdown, lisible partout.",
      choices: [{ value: "device", label: "Télécharger sur cet appareil" }, { value: "pc", label: "Enregistrer sur le PC…", primary: true }] });
    if (choice === "device") {
      const link = el("a", { href, download: "" });
      document.body.append(link); link.click(); link.remove();
    }
    if (choice !== "pc") return;
    openPathPicker({ title: "Où enregistrer la conversation ?", action: "Enregistrer ici",
      start: rememberedChoices().saveFolder || conv.workdir || "", onPick: async (folder) => {
        const password = state.me ? await askPassword("Enregistrer sur le PC") : "";
        if (password === null) return;
        try {
          const saved = await api("POST", `/api/conversations/${conv.id}/save`, { folder, format: "md", password });
          rememberChoices({ saveFolder: folder });
          toast(`Conversation enregistrée : ${saved.path}`);
        } catch (err) { toast(err.message); }
      } });
  });
}

// --- login ---------------------------------------------------------------------------

let loginDone = null;  // resolves when the user is logged in
let loginRetry = null;
let loginLoading = false;

async function loadLoginSession() {
  loginLoading = true;
  while (true) {
    $('login-submit').disabled = true;
    try {
      const session = await api('GET', '/api/auth/session');
      $('login-submit').textContent = 'Se connecter';
      $('login-submit').disabled = false;
      loginLoading = false;
      return session;
    } catch (err) {
      showLogin(err.status ? err.message : 'Le PC ne répond pas. Vérifiez Tailscale ou réessayez la connexion.');
      $('login-submit').textContent = 'Réessayer la connexion';
      $('login-submit').disabled = false;
      // Retry on a click, network recovery, or after a short delay. No password
      // is sent until the server's session state has actually been received.
      await new Promise(resolve => {
        const retry = () => {
          clearTimeout(timer);
          window.removeEventListener('online', retry);
          loginRetry = null;
          resolve();
        };
        const timer = setTimeout(retry, 5000);
        loginRetry = retry;
        window.addEventListener('online', retry, { once: true });
      });
    }
  }
}

function showLogin(text) {
  if (state.events) { state.events.close(); state.events = null; }
  const locked = Boolean(state.session && state.session.server_password);
  $("login-text").textContent = text || (locked ? "Entrez le mot de passe du serveur, puis votre nom."
    : "Entrez votre nom (et votre mot de passe si votre compte en a un).");
  $("login-server-password").classList.toggle("hidden", !locked);
  $("login-error").textContent = "";
  $("login").classList.remove("hidden");
  $("login-password").value = "";
  (locked ? $("login-server-password") : $("login-name")).focus();
}

function setupLogin() {
  let unlockTimer = null;
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (loginRetry) { loginRetry(); return; }
    if (loginLoading) return;
    $("login-submit").disabled = true;
    try {
      await api("POST", "/api/auth/login", { name: $("login-name").value.trim(), password: $("login-password").value,
        server_password: $("login-server-password").value });
      $("login-server-password").value = "";
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
    // Logged out: nothing of the conversations stays on this device.
    try { localStorage.setItem(OFFLINE_KEY, "0"); } catch { /* storage off */ }
    await tellWorker("offline-off");
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
  for (const id of ["list-scope", "filter-project", "filter-model", "filter-after", "filter-before"]) {
    $(id).addEventListener("change", runSearch);
  }
  $("clear-filters").addEventListener("click", () => {
    $("list-scope").value = "active";
    for (const id of ["filter-project", "filter-model", "filter-after", "filter-before"]) $(id).value = "";
    runSearch();
  });
}

async function runSearch() {
  const text = $("search").value.trim();
  if (text.length < 3) { renderList(); return; }
  let results;
  const requested = new URLSearchParams({ q: text });
  for (const [id, key] of [["filter-project", "project_id"], ["filter-model", "model"], ["filter-after", "after"], ["filter-before", "before"]]) {
    if ($(id).value) requested.set(key, $(id).value);
  }
  if ($("list-scope").value !== "all") requested.set("archived", String($("list-scope").value === "archived"));
  const stamp = requested.toString();
  state.searchStamp = stamp;
  try { ({ results } = await api("GET", `/api/search?${stamp}`)); } catch (err) { toast(err.message); return; }
  if (state.searchStamp !== stamp || $("search").value.trim() !== text) return;
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
let openSettingsDialog = null;

function setupSettingsDialog() {
  const dialog = $("settings-dialog");
  let loaded = null;  // the settings as the server sent them
  const fill = (data) => {
    loaded = data;
    const s = data.settings;
    $("settings-file").textContent = data.file
      ? `Fichier : ${data.file}. Rien d'un mot de passe n'y est écrit.` : "";
    $("set-allowed_roots").value = s.allowed_roots.join("\n");
    state.rootsField?.refresh();
    $("set-obsidian_vault").value = s.obsidian_vault || "";
    $("set-backup_dir").value = s.backup_dir || "";
    $("set-sandbox_cli").value = s.sandbox_cli || "";
    $("set-sandbox_data").value = s.sandbox_data || "";
    $("set-backup_dir").placeholder = data.backup_folder || "data\\sauvegardes";
    for (const name of SETTING_NUMBERS) $(`set-${name}`).value = s[name];
    for (const name of SETTING_BOOLS) $(`set-${name}`).checked = s[name];
    $("set-models").replaceChildren(...data.all_models.map((m) => el("label", { class: "check" },
      el("input", { type: "checkbox", value: m, checked: s.models.includes(m), onchange: syncDefaults }),
      ` ${aiName(m)}`)));
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
      $(id).replaceChildren(...models.map((m) => el("option", { value: m, text: aiName(m) })));
      if (models.includes(keep)) $(id).value = keep;
    }
  }
  const read = () => ({
    allowed_roots: $("set-allowed_roots").value.split("\n").map((l) => l.trim()).filter(Boolean),
    obsidian_vault: $("set-obsidian_vault").value.trim() || null,
    backup_dir: $("set-backup_dir").value.trim() || null,
    sandbox_cli: $("set-sandbox_cli").value.trim() || null,
    sandbox_data: $("set-sandbox_data").value.trim() || null,
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
  openSettingsDialog = async ({ createVault = false, vault = null } = {}) => {
    try {
      fill(await api("GET", "/api/config")); dialog.showModal();
      if (createVault) createSettingsVault();
      if (vault) {
        // Chosen in « Coffres Obsidian »: saved like any setting, with the password.
        $("set-obsidian_vault").value = vault;
        const password = $("settings-password");  // hidden on a server without passwords
        (password.offsetParent ? password : $("set-obsidian_vault")).focus();
        toast("Coffre choisi : enregistrez les réglages pour l’utiliser.");
      }
    } catch (err) { toast(err.message); }
  };
  $("settings").addEventListener("click", () => openSettingsDialog());
  const createSettingsVault = () => openPathPicker({ title: "Où créer le nouveau coffre ?", action: null, make: { kind: "vault",
    onMade: (made) => { $("set-obsidian_vault").value = made.path; toast("Coffre créé : enregistrez les réglages pour l’utiliser."); } } });
  $("settings-create-vault").addEventListener("click", createSettingsVault);
  $("settings-pick-vault").addEventListener("click", () => openVaultsDialog({ pick: (path) => { $("set-obsidian_vault").value = path; } }));
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
  $("new-model").replaceChildren(...models.map((m) => el("option", { value: m, text: aiName(m),
    selected: m === state.options.default_model })));
  $("new-conductor").replaceChildren(...models.map((m) => el("option", { value: m, text: aiName(m),
    selected: m === state.options.default_conductor })));
  $("settings").classList.toggle("hidden", !state.options.settings_editable || !isOwner());
  $("version").textContent = state.options.version ? `v${state.options.version}` : "";
  if (state.currentId === null) renderHome();  // the AIs offered on the home follow the settings
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

// The QR code (made by the server, an SVG of its own) that connects a device to an account.
function showDeviceLink(made, title) {
  $("device-link-title").textContent = title;
  $("device-qr").innerHTML = made.qr_svg;  // the server's own SVG: no text of anyone in it but the link
  $("device-link").value = made.link;
  $("device-link-minutes").textContent = made.minutes;
  $("device-link-dialog").showModal();
}

// Opened from a QR code (?appareil=…): this device is connected to that account.
async function acceptDeviceLink() {
  const link = new URLSearchParams(location.search).get("appareil");
  if (!link) return;
  history.replaceState(null, "", "/");
  try {
    const res = await fetch("/api/devices/accept", { method: "POST",
      headers: { "X-AIHub": "1", "Content-Type": "application/json" }, body: JSON.stringify({ link }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `erreur ${res.status}`);
    toast("Cet appareil est connecté.");
  } catch (err) {
    toast(err.message);
  }
}

function setupDevicesDialog() {
  const dialog = $("devices-dialog");
  const render = async () => {
    $("devices-error").textContent = "";
    let data;
    try { data = await api("GET", "/api/sessions"); } catch (err) { $("devices-error").textContent = err.message; return; }
    $("devices-list").replaceChildren(...data.sessions.map((s) => el("div", { class: "device" + (s.current ? " current" : "") },
      el("div", { class: "device-text" },
        el("span", { class: "conv-item-title", text: (s.label || deviceName(s.user_agent)) + (s.current ? " — cet appareil" : "") }),
        el("span", { class: "conv-item-sub", text: `${s.label ? deviceName(s.user_agent) + " · " : ""}${s.client || "?"} · vu le ${formatDate(s.last_seen_at)} · ouverte le ${formatDate(s.created_at)}` })),
      el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Renommer",
        title: "Un nom pour le reconnaître : « Téléphone de Lou », « PC du salon »",
        onclick: async () => {
          const label = prompt("Nom de cet appareil :", s.label || deviceName(s.user_agent));
          if (label === null) return;
          try { await api("PATCH", `/api/sessions/${s.id}`, { label }); await render(); }
          catch (err) { $("devices-error").textContent = err.message; }
        } }),
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
  $("devices-connect").addEventListener("click", async () => {
    const label = prompt("Nom de l'appareil à connecter :", `Téléphone de ${state.me ? state.me.name : "moi"}`);
    if (label === null) return;
    try { showDeviceLink(await api("POST", "/api/me/device-link", { label }), "Connecter un autre de vos appareils"); }
    catch (err) { $("devices-error").textContent = err.message; }
  });
  $("device-link-close").addEventListener("click", () => $("device-link-dialog").close());
  $("device-link-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("device-link").value); } catch { fallbackCopy($("device-link").value); }
    toast("Lien copié.");
  });
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
  await acceptDeviceLink();  // a QR code first: it may log this device in
  session = await loadLoginSession();
  state.session = session;
  for (const id of ["devices", "logout"]) $(id).classList.toggle("hidden", !session.required);
  const invitation = new URLSearchParams(location.search).get("invitation");
  if (session.setup) {
    // A new server: created from its own computer, by the first one there.
    $("login").classList.remove("hidden");
    $("login-form").classList.add("hidden");
    if (!session.setup_here) { $("setup-elsewhere").classList.remove("hidden"); await new Promise(() => {}); }
    showSetup();
    await new Promise((resolve) => { loginDone = resolve; });
    session = await api("GET", "/api/auth/session");
  } else if (!session.logged_in) {
    if (invitation) showInvitation(invitation);
    else showLogin();
    await new Promise((resolve) => { loginDone = resolve; });
    session = await api("GET", "/api/auth/session");
  } else if (invitation) {
    history.replaceState(null, "", "/");  // already in: the link is of no use here
  }
  state.session = session;
  state.me = session.user;
  if (state.me) {
    $("me-name").textContent = state.me.name;
    $("me-name").title = `Connecté : ${state.me.name} (${ROLE_NAMES[state.me.is_owner ? "owner" : state.me.role] || ""})`;
    $("people").classList.toggle("hidden", !isAdmin());
    $("my-accounts").classList.remove("hidden");
    $("my-password").classList.remove("hidden");
  }
  // What a guest may not do: hidden (the server refuses it anyway).
  for (const id of ["new-conversation", "new-project", "import"]) $(id).classList.toggle("hidden", !canCreate());
  $("import-local").classList.toggle("hidden", !isOwner());  // the sessions of the owner's PC
  $("consumption").classList.toggle("hidden", !isAdmin());  // also on a server without accounts
  $("server").classList.toggle("hidden", !isOwner());
  for (const button of document.querySelectorAll(".picker-trusted")) button.classList.toggle("hidden", !isAdmin());
  $("vaults").classList.toggle("hidden", !isOwner());
  $("trials").classList.toggle("hidden", !isOwner());
}

const ROLE_NAMES = { owner: "propriétaire", admin: "admin", membre: "membre", invite: "invité" };
const isAdmin = () => !state.me || state.me.is_owner || state.me.role === "admin";
const canCreate = () => isAdmin() || state.me.role === "membre";

function showSetup() {
  $("setup-form").classList.remove("hidden");
  $("setup-name").focus();
  $("setup-form").onsubmit = async (event) => {
    event.preventDefault();
    $("setup-error").textContent = "";
    const password = $("setup-password").value;
    const serverPassword = $("setup-server-password").value;
    if (!password && !serverPassword && !confirm("Sans aucun mot de passe, quiconque joint ce serveur entre à votre nom. Continuer ?")) return;
    $("setup-submit").disabled = true;
    try {
      await api("POST", "/api/setup", { name: $("setup-name").value.trim(), password, server_password: serverPassword });
      $("login").classList.add("hidden");
      $("setup-form").classList.add("hidden");
      $("login-form").classList.remove("hidden");
      if (loginDone) { loginDone(); loginDone = null; }
    } catch (err) {
      $("setup-error").textContent = err.message;
    } finally {
      $("setup-submit").disabled = false;
    }
  };
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
      toast("Bienvenue !");
    } catch (err) {
      $("invite-error").textContent = err.message;
    } finally {
      $("invite-submit").disabled = false;
    }
  };
}

// --- reading without the PC (« Hors connexion », a choice of this device) -------------------

const OFFLINE_KEY = "aihub-hors-ligne";

function offlineChosen() {
  try { return localStorage.getItem(OFFLINE_KEY) === "1"; } catch { return false; }
}

// Resolves once the message is given (a logout waits for it before reloading); never longer than 2 s.
function tellWorker(type) {
  if (!("serviceWorker" in navigator)) return Promise.resolve();
  const given = navigator.serviceWorker.ready.then((reg) => reg.active && reg.active.postMessage({ type })).catch(() => {});
  return Promise.race([given, new Promise((resolve) => setTimeout(resolve, 2000))]);
}

function setOffline(on) {
  if (state.offline === on) return;
  const was = state.offline;
  state.offline = on;
  $("offline-banner").classList.toggle("hidden", !on);
  document.body.classList.toggle("offline", on);
  $("send").disabled = on || $("send").disabled;
  $("prompt").readOnly = on;
  if (was && !on && state.currentId !== null) refreshConversation();  // back: the PC's fresh state
  if (!on && was) renderRunning();
}

function setupOffline() {
  const button = $("offline-toggle");
  const show = () => { button.querySelector("strong").textContent = `Hors connexion : ${offlineChosen() ? "oui" : "non"}`; };
  if (!("serviceWorker" in navigator) || !window.isSecureContext) { button.classList.add("hidden"); return; }
  if (offlineChosen()) tellWorker("offline-on");
  show();
  button.addEventListener("click", async () => {
    const on = !offlineChosen();
    if (on) {
      const ok = await confirmDialog({ title: "Garder les conversations sur cet appareil ?",
        text: "La liste et les conversations que vous ouvrez restent lisibles ici quand le PC est éteint ou Tailscale coupé "
          + "(écrire attend le retour du réseau). Elles sont effacées si vous retirez ce choix ou vous déconnectez. "
          + "À ne pas faire sur un appareil prêté.", okText: "Garder" });
      if (!ok) return;
    }
    try { localStorage.setItem(OFFLINE_KEY, on ? "1" : "0"); } catch { /* storage off: not kept */ }
    tellWorker(on ? "offline-on" : "offline-off");
    show();
    toast(on ? "Les conversations ouvertes restent lisibles hors connexion." : "Copie hors connexion effacée.");
  });
  window.addEventListener("online", () => { if (state.offline) loadConversations().catch(() => {}); });
}

// --- the server's dashboard (the owner's) ------------------------------------------------

function bytes(n) {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1).replace(".", ",")} Go`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1).replace(".", ",")} Mo`;
  return `${Math.ceil(n / 1024)} Ko`;
}

function duration(seconds) {
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  return d ? `${d} j ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function setupServerDialog() {
  const dialog = $("server-dialog");
  const render = async () => {
    $("server-error").textContent = "";
    let s;
    try { s = await api("GET", "/api/server"); } catch (err) { $("server-error").textContent = err.message; return; }
    const lowDisk = s.disk.free < 5 * 1024 ** 3;
    const card = (title, value, sub, warn) => el("div", { class: "server-card" + (warn ? " warn" : "") },
      el("span", { class: "quota-note", text: title }), el("strong", { text: value }), sub ? el("span", { class: "quota-note", text: sub }) : null);
    const ai = (name, on) => `${name} ${on ? "✓" : "✗"}`;
    $("server-cards").replaceChildren(
      card("Version", s.version, `lancé il y a ${duration(s.uptime_s)}`),
      card("Disque des données", `${bytes(s.disk.free)} libres`, `sur ${bytes(s.disk.total)}`, lowDisk),
      card("Base", bytes(s.base), `${s.counts.conversations} conversations, ${s.counts.messages} messages`),
      card("Dernière sauvegarde", s.backups.newest ? formatDate(s.backups.newest) : "aucune",
        s.backups.folder ? `${s.backups.count} fichier(s), ${bytes(s.backups.size)}` : "sauvegardes désactivées", !s.backups.newest),
      card("IA", [ai("Claude", s.ais.claude), ai("Codex", s.ais.codex), ai("Locale", s.ais.local)].join(" · ")),
      card("Personnes et appareils", `${s.counts.people} · ${s.counts.devices}`, "comptes · sessions ouvertes"),
      card("Pièces jointes", bytes(s.attachments.size), `${s.attachments.count} fichier(s)`));
    $("server-log").textContent = s.log.length ? s.log.join("\n")
      : (s.log_file ? "Journal vide pour l'instant." : "Pas de journal : le serveur a été lancé dans une fenêtre (ses messages y sont).");
    $("server-log").scrollTop = $("server-log").scrollHeight;
    await renderBackups();
  };
  // The backups, newest first, each one restorable (the owner, with the password; the server restarts).
  const RESTORE_SAID = { ok: "réussie", echec: "échouée", retabli: "annulée : la base d'avant a été remise" };
  const renderBackups = async () => {
    let data;
    try { data = await api("GET", "/api/server/backups"); } catch (err) { $("server-error").textContent = err.message; return; }
    const last = data.last_restore;
    $("server-restore-last").textContent = last
      ? `Dernière restauration (${formatDate(last.at)}) : ${last.backup}, ${RESTORE_SAID[last.status] || last.status}` +
        (last.error ? ` — ${last.error}` : "") + (last.keep ? `. Base d'avant gardée : ${last.keep}` : "")
      : "";
    $("server-backups").replaceChildren(...(data.backups.length ? data.backups.map((b) => el("div", { class: "server-backup" },
      el("span", {}, el("strong", { text: formatDate(b.modified) }), ` · ${b.name} · ${bytes(b.size)}` + (b.attachments ? " · avec les pièces jointes" : "")),
      el("button", { type: "button", class: "btn btn-ghost btn-small", text: "Restaurer…", onclick: () => restoreBackup(b) })))
      : [el("p", { class: "quota-note", text: `Aucune sauvegarde dans ${data.folder}.` })]));
  };
  const restoreBackup = async (b) => {
    const password = el("input", { type: "password", autocomplete: "current-password", "aria-label": "Votre mot de passe AI Hub" });
    const sure = el("input", { type: "checkbox" });
    const error = el("p", { class: "form-error", role: "alert" });
    const go = el("button", { type: "button", class: "btn btn-danger", text: "Restaurer et redémarrer", disabled: true });
    sure.addEventListener("change", () => { go.disabled = !sure.checked; });
    const dialog = el("dialog", { class: "dialog dialog-content" });
    const done = () => { dialog.close(); dialog.remove(); };
    dialog.append(el("h2", { text: "Restaurer cette sauvegarde ?" }),
      el("p", { text: `La base sera remise telle qu'elle était ${localTime(b.modified)} (${b.name}). Tout ce qui a été fait depuis (conversations, réglages des personnes…) sera perdu. La base actuelle est d'abord gardée à côté, et le serveur redémarre : l'appli revient toute seule (il faudra peut-être vous reconnecter, les sessions ouvertes étant celles de la sauvegarde).` }),
      state.me ? el("label", {}, "Votre mot de passe AI Hub (laissez vide si votre compte n’en a pas)", password) : null,
      el("label", { class: "check" }, sure, "J'ai compris : ce qui a été fait depuis cette sauvegarde sera perdu"), error,
      el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: done }), go));
    go.addEventListener("click", async () => {
      go.disabled = true; error.textContent = "";
      try {
        await api("POST", "/api/server/restore", { name: b.name, password: password.value });
      } catch (err) { error.textContent = err.message; go.disabled = false; password.value = ""; return; }
      done();
      dialog.remove();
      $("server-dialog").close();
      toast("Restauration en cours : le serveur redémarre…");
      // The app comes back when the server answers again.
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        try { const r = await fetch("/api/health", { cache: "no-store" }); if (r.ok && i > 1) { location.reload(); return; } } catch {}
      }
      toast("Le serveur ne répond pas encore : rechargez la page dans un moment.");
    });
    document.body.append(dialog);
    dialog.showModal();
  };
  $("server").addEventListener("click", async () => { dialog.showModal(); await render(); });
  $("server-refresh").addEventListener("click", render);
  $("server-close").addEventListener("click", () => dialog.close());
}

// --- the app's own folder picker: the server PC's folders, from any device (no Windows dialog) ---

const pathSeparator = (path) => (String(path || "").includes("\\") || /^[A-Za-z]:/.test(String(path || "")) ? "\\" : "/");
const parentOf = (path) => {
  const sep = pathSeparator(path);
  const trimmed = String(path || "").replace(/[\\/]+$/, "");
  const cut = trimmed.lastIndexOf(sep);
  return cut > 0 ? trimmed.slice(0, cut + (/^[A-Za-z]:$/.test(trimmed.slice(0, cut)) ? 1 : 0)) : "";
};

// The app's file explorer (1.2.3): the server PC's folders and files, from any device, never a typed path.
// The server judges every step with one rule (aihub/places.py): a local disk only, inside the allowed
// folders of the settings when they name some. `scope`: "workdir" (that rule) or "settings" (the owner
// choosing a setting: any local disk).
// `files`: also show files of these kinds ("md,txt", "*") and choose one instead of a folder.
// `make`: { kind: "folder" | "vault", name, onMade } a new folder (or vault) here, named in place.
// `device`: { accept, multiple, onFiles } the same from this device instead (a phone's photo…).
// `action`: the button that chooses (null: none, when the explorer only makes something).
async function openPathPicker({ title = "Choisir un dossier", start = "", scope = "workdir", action = "Choisir ce dossier",
                                vaultsOnly = false, files = null, make = null, device = null, onPick = () => {} } = {}) {
  const dialog = el("dialog", { class: "dialog dialog-wide picker-dialog" });
  const where = el("p", { class: "picker-where" });
  const starts = el("div", { class: "picker-starts" });
  const list = el("div", { class: "picker-list", role: "list" });
  const note = el("p", { class: "quota-note" });
  const error = el("p", { class: "form-error", role: "alert" });
  const up = el("button", { type: "button", class: "btn btn-ghost", text: "↑ Dossier parent" });
  const choose = el("button", { type: "button", class: "btn btn-primary", text: action || "" });
  let here = null;
  let picked = null;  // the file chosen, in file mode
  // A new folder (or vault) here: its name only; the password when the server asks for one.
  const name = el("input", { maxlength: 120, "aria-label": make?.kind === "vault" ? "Nom du nouveau coffre" : "Nom du nouveau dossier",
    placeholder: make?.kind === "vault" ? "Nom du coffre" : "Nom du dossier" });
  const password = el("input", { type: "password", autocomplete: "current-password", "aria-label": "Votre mot de passe AI Hub",
    placeholder: "Votre mot de passe AI Hub" });
  const makeIt = el("button", { type: "button", class: make ? "btn btn-primary" : "btn btn-ghost",
    text: make?.kind === "vault" ? "Créer le coffre ici" : "Créer ce dossier ici" });
  const maker = el("div", { class: "picker-make" + (make ? "" : " hidden") }, name, state.me ? password : null, makeIt);
  const newFolder = el("button", { type: "button", class: "btn btn-ghost", text: "＋ Nouveau dossier",
    onclick: () => { maker.classList.toggle("hidden"); if (!maker.classList.contains("hidden")) name.focus(); } });
  if (make?.name) name.value = make.name;

  const pathOf = (child) => here.path.replace(/[\\/]+$/, "") + pathSeparator(here.path) + child;
  let asked = 0;  // a click while a big folder is read: only the last answer counts
  const go = async (path) => {
    error.textContent = "";
    const mine = ++asked;
    if (path) where.textContent = `Lecture de ${path}…`;
    let data;
    try {
      const query = new URLSearchParams({ scope });
      if (path) query.set("path", path);
      if (files) query.set("files", files);
      data = await api("GET", `/api/filesystem/folders?${query}`);
    } catch (err) { if (mine === asked) { error.textContent = err.message; where.textContent = here?.path || ""; } return false; }
    if (mine !== asked) return false;
    here = data;
    picked = null;
    where.textContent = data.path || "Choisissez un point de départ.";
    starts.replaceChildren(...data.starts.map((s) => el("button", { type: "button", class: "picker-start", text: s.name, title: s.path, onclick: () => go(s.path) })));
    list.replaceChildren(...(data.path ? (data.entries.length ? data.entries.map((e) => e.kind === "file"
      ? el("button", { type: "button", class: "picker-item picker-file-item", role: "listitem", title: e.path, onclick: (event) => {
          picked = e.path;
          for (const other of list.querySelectorAll(".picked")) other.classList.remove("picked");
          event.currentTarget.classList.add("picked");
          refresh();
        }, ondblclick: () => { picked = e.path; choose.click(); } },
        el("span", { class: "picker-icon", text: "📄", "aria-hidden": "true" }), el("span", { text: e.name }),
        el("span", { class: "quota-note", text: bytes(e.size || 0) }))
      : el("button", { type: "button", class: "picker-item", role: "listitem", title: e.path, onclick: () => go(e.path) },
        el("span", { class: "picker-icon", text: "📁", "aria-hidden": "true" }), el("span", { text: e.name }),
        e.vault ? el("span", { class: "vault-badge on", text: "coffre" }) : null))
      : [el("p", { class: "list-empty", text: files ? "Rien à choisir ici." : "Aucun sous-dossier." })]) : []));
    up.disabled = !data.parent;
    refresh();
    note.textContent = [data.limited ? "Limité aux dossiers autorisés des réglages." : null,
      data.complete ? null : "Seuls les 2 000 premiers éléments sont affichés.",
      vaultsOnly && data.path && !data.vault ? "Ce dossier n’est pas un coffre : il ne contient pas .obsidian." : null].filter(Boolean).join(" ");
    return true;
  };
  const refresh = () => {
    choose.disabled = files ? !picked : (!here?.path || (vaultsOnly && !here.vault));
    newFolder.disabled = makeIt.disabled = !here?.path;
  };
  up.addEventListener("click", () => here?.parent && go(here.parent));
  makeIt.addEventListener("click", async () => {
    if (!here?.path) return;
    if (!name.value.trim()) { name.focus(); return; }
    makeIt.disabled = true; error.textContent = "";
    try {
      const made = await api("POST", make?.kind === "vault" ? "/api/obsidian/vaults" : "/api/filesystem/create",
        { path: pathOf(name.value.trim()), kind: "directory", parents: false, password: password.value });
      password.value = "";
      if (make?.onMade) { done(); make.onMade(made); return; }
      maker.classList.add("hidden");
      name.value = "";
      await go(made.path);  // into the new folder, to choose it
      toast("Dossier créé.");
    } catch (err) { error.textContent = err.message; password.value = ""; }
    finally { makeIt.disabled = !here?.path; }
  });
  name.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); makeIt.click(); } });
  // Closed and taken out of the page at once (not left to the "close" event alone).
  const done = () => { dialog.close(); dialog.remove(); };
  choose.addEventListener("click", () => { const chosen = files ? picked : here.path; if (!chosen) return; done(); onPick(chosen); });
  const fromDevice = device ? el("button", { type: "button", class: "btn btn-ghost", text: "Depuis cet appareil…", onclick: () => {
    const input = el("input", { type: "file", accept: device.accept || null, multiple: Boolean(device.multiple), hidden: true });
    input.addEventListener("change", () => { const chosen = [...input.files]; input.remove(); if (chosen.length) { done(); device.onFiles(chosen); } });
    document.body.append(input);
    input.click();
  } }) : null;
  dialog.append(el("h2", { text: title }),
    el("p", { class: "quota-note", text: "Les dossiers du PC qui héberge AI Hub, même depuis un téléphone ou un autre PC. Rien n’est modifié en parcourant." }),
    starts, where, list, maker, note, error,
    el("div", { class: "dialog-actions" }, up, make ? null : newFolder, fromDevice,
      el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => done() }), action ? choose : null));
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  // Start where the field points, else its parent, else the starting places.
  const wanted = String(start || "").trim();
  if (!(wanted && await go(wanted)) && !(wanted && parentOf(wanted) && await go(parentOf(wanted)))) await go("");
}

// Files and folders inside a working folder, ticked to give them to an isolated space.
async function openFilePicker({ base = "", conversationId = null, chosen = [], onPick = () => {} } = {}) {
  const dialog = el("dialog", { class: "dialog dialog-wide picker-dialog" });
  const where = el("p", { class: "picker-where" });
  const list = el("div", { class: "picker-list", role: "list" });
  const count = el("p", { class: "quota-note" });
  const error = el("p", { class: "form-error", role: "alert" });
  const up = el("button", { type: "button", class: "btn btn-ghost", text: "↑ Dossier parent" });
  const add = el("button", { type: "button", class: "btn btn-primary", text: "Ajouter la sélection" });
  const selected = new Set(chosen);
  let here = null;
  const showCount = () => {
    count.textContent = selected.size ? `${selected.size} élément(s) choisi(s) : ${[...selected].join(", ")}` : "Cochez des fichiers ou des dossiers ; un dossier donne tout son contenu.";
    add.disabled = !selected.size;
  };
  const go = async (path) => {
    error.textContent = "";
    try {
      const query = conversationId ? `conversation_id=${conversationId}` : `base=${encodeURIComponent(base)}`;
      here = await api("GET", `/api/filesystem/entries?${query}&path=${encodeURIComponent(path || "")}`);
    } catch (err) { error.textContent = err.message; return; }
    where.textContent = here.base + (here.path ? ` › ${here.path}` : "");
    list.replaceChildren(...(here.entries.length ? here.entries.map((e) => {
      const box = el("input", { type: "checkbox", "aria-label": `Choisir ${e.path}` });
      box.checked = selected.has(e.path);
      box.addEventListener("change", () => { if (box.checked) selected.add(e.path); else selected.delete(e.path); showCount(); });
      return el("div", { class: "picker-item picker-check", role: "listitem" }, box,
        e.kind === "dir"
          ? el("button", { type: "button", class: "picker-open", title: "Ouvrir ce dossier", onclick: () => go(e.path) },
              el("span", { class: "picker-icon", text: "📁", "aria-hidden": "true" }), el("span", { text: e.name }))
          : el("span", { class: "picker-file" }, el("span", { class: "picker-icon", text: "📄", "aria-hidden": "true" }),
              el("span", { text: e.name }), el("span", { class: "quota-note", text: bytes(e.size || 0) })));
    }) : [el("p", { class: "list-empty", text: "Ce dossier est vide." })]));
    if (!here.complete) list.append(el("p", { class: "quota-note", text: "Seuls les 2 000 premiers éléments sont affichés." }));
    up.disabled = here.parent === null;
  };
  up.addEventListener("click", () => here && here.parent !== null && go(here.parent));
  const done = () => { dialog.close(); dialog.remove(); };
  add.addEventListener("click", () => { done(); onPick([...selected]); });
  dialog.append(el("h2", { text: "Choisir dans le dossier de travail" }),
    el("p", { class: "quota-note", text: "Seuls les éléments cochés sont copiés dans l’espace isolé. Les originaux restent sur le PC." }),
    where, list, count, error,
    el("div", { class: "dialog-actions" }, up, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: () => done() }), add));
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  showCount();
  await go("");
}

// A path field (1.2.3): chosen in the app's explorer, never typed; « Vider » when it may stay empty.
function pathField(field, options, { trusted = false, list = false } = {}) {
  field.readOnly = true;
  field.classList.add("path-field");
  const open = () => {
    const lines = field.value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    openPathPicker({ ...options, start: list ? lines.at(-1) || "" : field.value.trim(), onPick: (path) => {
      if (list) { if (!lines.includes(path)) field.value = [...lines, path].join("\n"); } else field.value = path;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
      if (list) showList();
    } });
  };
  const hide = trusted ? " picker-trusted" + (isAdmin() ? "" : " hidden") : "";
  const browse = el("button", { type: "button", class: "btn btn-ghost btn-small" + hide, text: list ? "Ajouter un dossier…" : "Choisir…",
    title: "Choisir dans l'explorateur de l'appli (les dossiers du PC serveur)", onclick: open });
  const clear = el("button", { type: "button", class: "btn btn-ghost btn-small" + hide, text: "Vider", onclick: () => {
    field.value = "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  } });
  field.addEventListener("click", open);
  // A list (the allowed folders): one line each, with its own ✕.
  const rows = list ? el("div", { class: "path-list" }) : null;
  const showList = () => {
    const lines = field.value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    rows.replaceChildren(...(lines.length ? lines.map((line) => el("div", { class: "path-row" }, el("span", { text: line }),
      el("button", { type: "button", class: "fav-remove", text: "✕", title: "Retirer", "aria-label": `Retirer ${line}`, onclick: () => {
        field.value = lines.filter((l) => l !== line).join("\n");
        field.dispatchEvent(new Event("input", { bubbles: true }));
        showList();
      } }))) : [el("p", { class: "quota-note", text: "Aucun : tous les disques du PC." })]));
  };
  if (list) {
    field.classList.add("hidden");
    field.after(rows, browse);
    field.addEventListener("change", showList);
    field.addEventListener("input", showList);
    showList();
  } else {
    field.after(browse, clear);
  }
  return { open, refresh: () => list && showList() };
}

function setupPathPickers() {
  for (const id of ["new-workdir", "project-workdir", "import-workdir"]) {
    pathField($(id), { title: "Choisir le dossier de travail" }, { trusted: true });
  }
  state.rootsField = pathField($("set-allowed_roots"), { title: "Ajouter un dossier autorisé", scope: "settings", action: "Ajouter ce dossier" }, { list: true });
  pathField($("set-obsidian_vault"), { title: "Dossier des notes de projet", scope: "settings" });
  pathField($("project-vault"), { title: "Coffre des notes de ce projet" });
  pathField($("set-backup_dir"), { title: "Dossier des sauvegardes", scope: "settings" });
  pathField($("set-sandbox_data"), { title: "Stockage du moteur isolé", scope: "settings" });
}

// --- « Essais à faire » (the owner's): what was never tried for real, and how it went --------------

const TRIAL_SAID = { ok: "Ça marche", probleme: "Problème", attente: "Pas encore" };

function setupTrialsDialog() {
  const open = async () => {
    let list;
    try { list = (await api("GET", "/api/trials")).trials; } catch (err) { toast(err.message); return; }
    const dialog = el("dialog", { class: "dialog dialog-wide trials-dialog" });
    const progress = el("p", { class: "quota-note" });
    const items = el("div", { class: "trials" });
    const done = () => { dialog.close(); dialog.remove(); };
    const report = () => [`Essais AI Hub — ${new Date().toLocaleDateString("fr-FR")}`, "",
      ...list.map((t) => `- ${t.title} : ${TRIAL_SAID[t.status]}${t.note ? ` — ${t.note}` : ""}`)].join("\n");
    const render = () => {
      const tried = list.filter((t) => t.status !== "attente").length;
      progress.textContent = `${tried} sur ${list.length} essayés · ${list.filter((t) => t.status === "ok").length} qui marchent · `
        + `${list.filter((t) => t.status === "probleme").length} avec un problème.`;
      items.replaceChildren(...list.map((t) => {
        const note = el("textarea", { rows: 2, maxlength: 2000, placeholder: "Une remarque (ce qui a marché, ce qui a coincé…)", "aria-label": `Remarque : ${t.title}` });
        note.value = t.note || "";
        const say = async (status) => {
          try {
            list = (await api("PUT", `/api/trials/${t.key}`, { status, note: note.value })).trials;
            render();
          } catch (err) { toast(err.message); }
        };
        note.addEventListener("change", () => say(t.status));
        return el("section", { class: `trial trial-${t.status}` },
          el("div", { class: "trial-head" }, el("h3", { text: t.title }), el("span", { class: `badge trial-badge-${t.status}`, text: TRIAL_SAID[t.status] })),
          el("ol", {}, ...t.steps.map((s) => el("li", { text: s }))),
          note,
          el("div", { class: "trial-actions" }, ...["ok", "probleme", "attente"].map((s) => el("button", { type: "button",
            class: "btn btn-small " + (t.status === s ? "btn-primary" : "btn-ghost"), text: TRIAL_SAID[s], onclick: () => say(s) }))),
          t.updated_at ? el("p", { class: "quota-note", text: `Noté ${formatDate(t.updated_at)}` }) : null);
      }));
    };
    dialog.append(el("h2", { text: "Essais à faire" }),
      el("p", { class: "quota-note", text: "Ce qui n'a jamais été essayé en vrai : sur un téléphone, un autre PC, un vrai compte. Faites-les quand vous voulez et notez comment ça s'est passé." }),
      progress, items,
      el("div", { class: "dialog-actions" },
        el("button", { type: "button", class: "btn btn-ghost", text: "Copier le compte rendu", onclick: async () => {
          const text = report();
          try { await navigator.clipboard.writeText(text); } catch { fallbackCopy(text); }
          toast("Compte rendu copié.");
        } }),
        el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: done })));
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    render();
    dialog.showModal();
  };
  $("trials").addEventListener("click", open);
}

// --- Obsidian vaults on this PC (the owner's): list, open to read, choose one --------------

let openVaultsDialog = () => {};

const VAULT_SOURCES = { reglages: "Notes d’AI Hub", projet: "Notes de projet", aihub: "Créé par AI Hub", obsidian: "Connu d’Obsidian", disque: "Trouvé sur le disque" };

// [[Note]], [[Note|alias]], [[Note#titre]] become links the reader opens; ![[embeds]] stay as text.
function wikiLinks(text) {
  return text.replace(/(!?)\[\[([^\]|#\n]+)(#[^\]|\n]*)?(\|[^\]\n]+)?\]\]/g, (all, embed, target, heading, alias) => {
    if (embed) return all;
    const label = (alias ? alias.slice(1) : target + (heading || "")).replace(/[[\]]/g, "");
    return `[${label}](#note:${encodeURIComponent(target.trim())})`;
  });
}

// A note's front matter (--- … --- at the top) is shown apart, not as Markdown.
function splitFrontMatter(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  return match ? [match[1], text.slice(match[0].length)] : [null, text];
}

function setupVaultsDialog() {
  const dialog = $("vaults-dialog");
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  let pick = null;      // set when another window asked to choose a vault
  let vault = null;     // the vault open in the reader
  let notes = [];

  const showList = () => { $("vault-reader").classList.add("hidden"); $("vaults-list-view").classList.remove("hidden"); };
  const card = (v) => {
    const actions = [];
    if (v.exists) actions.push(el("button", { type: "button", class: "btn btn-primary", text: "Ouvrir", onclick: () => openVault(v) }));
    if (v.exists && pick) {
      actions.push(el("button", { type: "button", class: "btn btn-ghost", text: "Choisir ce coffre",
        onclick: () => { const chosen = pick; pick = null; dialog.close(); chosen(v.path); } }));
    } else if (v.exists && !v.active) {
      actions.push(el("button", { type: "button", class: "btn btn-ghost", text: "Utiliser pour les notes",
        title: "Les notes de projet d’AI Hub iront dans ce coffre (réglages, avec votre mot de passe)",
        onclick: () => { dialog.close(); if (openSettingsDialog) openSettingsDialog({ vault: v.path }); } }));
    }
    if (v.exists && local) {
      actions.push(el("a", { class: "btn btn-ghost", href: `obsidian://open?path=${encodeURIComponent(v.path)}`, text: "Ouvrir dans Obsidian",
        title: "Sur ce PC, si Obsidian connaît déjà ce coffre ; sinon : Obsidian → « Ouvrir un dossier comme coffre »" }));
    }
    actions.push(el("button", { type: "button", class: "btn btn-ghost", text: "Copier le chemin",
      onclick: async () => { try { await navigator.clipboard.writeText(v.path); } catch { fallbackCopy(v.path); } toast("Chemin copié."); } }));
    const badges = v.sources.map((s) => el("span", { class: "vault-badge" + (s === "reglages" || s === "projet" ? " on" : ""),
      text: s === "projet" ? `Notes du projet ${(v.projects || []).map((p) => `« ${p} »`).join(", ")}` : VAULT_SOURCES[s] || s }));
    if (!v.exists) badges.push(el("span", { class: "vault-badge warn", text: "Introuvable" }));
    const count = !v.exists ? "dossier absent ou sans .obsidian" : v.notes === null ? "beaucoup de fichiers" : `${v.notes} note${v.notes > 1 ? "s" : ""}`;
    return el("div", { class: "vault-card" + (v.active ? " active" : "") + (v.exists ? "" : " missing"), role: "listitem" },
      el("div", { class: "vault-card-head" }, el("strong", { text: v.name }), el("span", { class: "quota-note", text: count })),
      el("div", { class: "vault-card-path", text: v.path }),
      el("div", { class: "vault-badges" }, ...badges),
      el("div", { class: "vault-card-actions" }, ...actions));
  };
  const render = async (refresh = false) => {
    $("vaults-error").textContent = "";
    $("vaults-status").textContent = refresh ? "Recherche sur les disques…" : "Chargement…";
    try {
      const data = await api("GET", `/api/obsidian/vaults${refresh ? "?refresh=true" : ""}`);
      const n = data.vaults.length;
      $("vaults-status").textContent = (n ? `${n} coffre${n > 1 ? "s" : ""} trouvé${n > 1 ? "s" : ""}.` : "Aucun coffre trouvé sur ce PC.")
        + (data.complete ? "" : " La recherche sur les disques s’est arrêtée avant la fin (trop de dossiers) : ceux qu’Obsidian et AI Hub connaissent sont tous là.")
        + (pick ? " Choisissez celui des notes de projet." : "");
      $("vaults-list").replaceChildren(...data.vaults.map(card));
    } catch (err) { $("vaults-status").textContent = ""; $("vaults-error").textContent = err.message; }
  };

  const renderNotes = () => {
    const words = $("vault-filter").value.trim().toLowerCase();
    const shown = notes.filter((n) => !words || n.path.toLowerCase().includes(words));
    $("vault-notes").replaceChildren(...(shown.length ? shown.slice(0, 500).map((n) => {
      const cut = n.path.lastIndexOf("/");
      return el("button", { type: "button", class: "vault-note-item", role: "listitem", title: n.path, onclick: () => openNote(n.path) },
        cut >= 0 ? el("span", { class: "vault-note-folder", text: n.path.slice(0, cut + 1) }) : null,
        el("span", { text: n.path.slice(cut + 1).replace(/\.md$/i, "") }));
    }) : [el("p", { class: "list-empty", text: notes.length ? "Aucune note ne correspond." : "Ce coffre n’a pas encore de note." })]));
  };
  const openVault = async (v) => {
    $("vault-error").textContent = "";
    try {
      const data = await api("GET", `/api/obsidian/vault/notes?vault=${encodeURIComponent(v.path)}`);
      vault = data.vault;
      notes = data.notes;
      $("vault-title").textContent = data.name;
      $("vault-path").textContent = data.vault + (data.complete ? "" : ` — les ${notes.length} premières notes seulement`);
      $("vault-filter").value = "";
      $("vault-note-title").textContent = "";
      opened = null;
      $("vault-note-link").classList.add("hidden");
      $("vault-note-meta").textContent = "Choisissez une note.";
      $("vault-note-body").replaceChildren();
      $("vault-reader").classList.remove("showing-note");
      renderNotes();
      $("vaults-list-view").classList.add("hidden");
      $("vault-reader").classList.remove("hidden");
      $("vault-filter").focus();
    } catch (err) { $("vaults-error").textContent = err.message; }
  };
  let opened = null;  // the note shown
  // The whole text of the vault's notes: every word, in the text or the name.
  const searchText = async () => {
    const words = $("vault-filter").value.trim();
    if (!words) { toast("Tapez un ou plusieurs mots à chercher dans le texte des notes."); return; }
    $("vault-error").textContent = "";
    try {
      const data = await api("GET", `/api/obsidian/vault/search?vault=${encodeURIComponent(vault)}&q=${encodeURIComponent(words)}`);
      const head = el("div", { class: "vault-search-head" },
        el("span", { class: "quota-note", text: `${data.total} note(s) contiennent « ${data.query} »` + (data.complete ? "" : " (recherche arrêtée avant la fin : trop de notes)") }),
        el("button", { type: "button", class: "btn btn-ghost btn-small", text: "← Toutes les notes", onclick: () => { $("vault-filter").value = ""; renderNotes(); } }));
      $("vault-notes").replaceChildren(head, ...(data.results.length ? data.results.map((r) => el("button", {
        type: "button", class: "vault-note-item vault-hit", role: "listitem", title: r.path, onclick: () => openNote(r.path) },
        el("span", { text: r.path.replace(/\.md$/i, "") }), el("span", { class: "vault-hit-excerpt", text: r.excerpt })))
        : [el("p", { class: "list-empty", text: "Aucune note ne contient tous ces mots." })]));
    } catch (err) { $("vault-error").textContent = err.message; }
  };
  // The note shown becomes a document of a project's memory, and follows its file.
  const linkNote = async () => {
    if (!opened) return;
    const projects = state.projects || [];
    if (!projects.length) { toast("Créez d’abord un projet."); return; }
    const choice = el("select", { "aria-label": "Projet" }, ...projects.map((p) => el("option", { value: p.id, text: p.name })));
    const error = el("p", { class: "form-error", role: "alert" });
    const dialog = el("dialog", { class: "dialog dialog-content" });
    const done = () => { dialog.close(); dialog.remove(); };
    dialog.append(el("h2", { text: "Ajouter la note à un projet" }),
      el("p", { class: "quota-note", text: `« ${opened} » rejoint la mémoire RAG du projet : les IA de ses conversations y retrouvent des passages. Quand la note change dans Obsidian, le document se met à jour tout seul.` }),
      el("label", {}, "Projet", choice), error,
      el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: done }),
        el("button", { type: "button", class: "btn btn-primary", text: "Ajouter", onclick: async () => {
          try {
            await api("POST", `/api/projects/${choice.value}/memory/notes`, { vault, path: opened });
            done(); toast(`Note ajoutée au projet « ${choice.selectedOptions[0].text} ».`);
          } catch (err) { error.textContent = err.message; }
        } })));
    document.body.append(dialog);
    dialog.showModal();
  };
  const openNote = async (path) => {
    $("vault-error").textContent = "";
    try {
      const n = await api("GET", `/api/obsidian/vault/note?vault=${encodeURIComponent(vault)}&path=${encodeURIComponent(path)}`);
      opened = n.path;
      $("vault-note-link").classList.remove("hidden");
      const cut = n.path.lastIndexOf("/");
      $("vault-note-title").textContent = n.path.slice(cut + 1).replace(/\.md$/i, "");
      $("vault-note-meta").textContent = [cut >= 0 ? n.path.slice(0, cut) : null, bytes(n.size), `modifiée ${formatDate(n.modified)}`,
        n.cut ? "trop longue : seul le début est affiché" : null].filter(Boolean).join(" · ");
      const [front, body] = splitFrontMatter(n.content);
      const parts = [];
      if (front) parts.push(el("details", { class: "vault-front" }, el("summary", { text: "Propriétés" }), el("pre", { text: front })));
      const rendered = el("div");
      rendered.innerHTML = renderMarkdown(wikiLinks(body));
      parts.push(rendered);
      $("vault-note-body").replaceChildren(...parts);
      $("vault-reader").classList.add("showing-note");
      $("vault-note-pane").scrollTop = 0;
    } catch (err) { $("vault-error").textContent = err.message; }
  };
  // A [[link]]: the note of that name (or path) in the same vault.
  $("vault-note-body").addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#note:"]');
    if (!link) return;
    event.preventDefault();
    const target = decodeURIComponent(link.getAttribute("href").slice(6)).toLowerCase();
    const found = notes.find((n) => n.path.toLowerCase() === `${target}.md` || n.path.toLowerCase() === target)
      || notes.find((n) => n.path.toLowerCase().split("/").pop().replace(/\.md$/, "") === target.split("/").pop());
    if (found) openNote(found.path); else toast(`Pas de note « ${link.textContent} » dans ce coffre.`);
  });

  openVaultsDialog = async ({ pick: chooser = null } = {}) => {
    pick = chooser;
    showList();
    if (!dialog.open) dialog.showModal();
    await render();
  };
  dialog.addEventListener("close", () => { pick = null; });
  $("vaults").addEventListener("click", () => openVaultsDialog());
  $("vaults-refresh").addEventListener("click", () => render(true));
  $("vaults-open-folder").addEventListener("click", () => openPathPicker({ title: "Ouvrir un coffre", scope: "settings",
    action: "Ouvrir ce coffre", vaultsOnly: true, onPick: (path) => openVault({ path }) }));
  $("vaults-close").addEventListener("click", () => dialog.close());
  $("vault-close").addEventListener("click", () => dialog.close());
  $("vault-back").addEventListener("click", () => { showList(); render(); });
  $("vault-note-back").addEventListener("click", () => $("vault-reader").classList.remove("showing-note"));
  $("vault-filter").addEventListener("input", renderNotes);
  $("vault-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchText(); } });
  $("vault-search-text").addEventListener("click", searchText);
  $("vault-note-link").addEventListener("click", linkNote);
  $("vaults-create").addEventListener("click", () => openPathPicker({ title: "Où créer le nouveau coffre ?", action: null,
    make: { kind: "vault", onMade: () => { toast("Coffre créé."); render(true); } } }));
}

// --- keyboard shortcuts -----------------------------------------------------------------

function setupShortcuts() {
  const jump = $("jump-dialog");
  let shown = [];
  let at = 0;
  const render = () => {
    const words = $("jump-input").value.trim().toLowerCase();
    shown = state.conversations.filter((c) => !words || c.title.toLowerCase().includes(words)).slice(0, 12);
    at = Math.min(at, Math.max(0, shown.length - 1));
    $("jump-list").replaceChildren(...(shown.length ? shown.map((c, i) => el("button", {
      class: "conv-item" + (i === at ? " active" : ""), type: "button", role: "option", "aria-selected": String(i === at),
      onclick: () => { jump.close(); openConversation(c.id); } },
    el("span", { class: "conv-item-title", text: c.title }),
    el("span", { class: "conv-item-sub", text: sinceDate(c.updated_at) }))) : [el("p", { class: "list-empty", text: "Aucune conversation ne correspond." })]));
  };
  $("jump-input").addEventListener("input", () => { at = 0; render(); });
  $("jump-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { at = Math.min(at + 1, shown.length - 1); render(); event.preventDefault(); }
    if (event.key === "ArrowUp") { at = Math.max(at - 1, 0); render(); event.preventDefault(); }
    if (event.key === "Enter") {
      event.preventDefault();
      if (shown[at]) { jump.close(); openConversation(shown[at].id); }
    }
  });
  const typing = (target) => target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
  document.addEventListener("keydown", async (event) => {
    const open = document.querySelector("dialog[open]") || document.querySelector(".menu-wrap.open");
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (open) return;
      $("jump-input").value = "";
      at = 0;
      render();
      jump.showModal();
      $("jump-input").focus();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "n") {
      if (open || !canCreate()) return;
      event.preventDefault();
      goHome();
      return;
    }
    if (event.key === "Escape" && !open && state.data && state.data.running) {
      event.preventDefault();
      const ok = await confirmDialog({ title: "Arrêter l'IA ?", text: "Elle s'arrête tout de suite ; ce qu'elle a déjà écrit reste.", okText: "Arrêter" });
      if (ok) $("stop").click();
      return;
    }
    if (event.key === "?" && !open && !typing(event.target)) {
      event.preventDefault();
      $("keys-dialog").showModal();
    }
  });
}

// --- read a reply aloud (the browser's voice) --------------------------------------------

const SPEECH = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
let speaking = null;  // the message being read

// What a voice should say: the text without the Markdown signs; code is announced, not read.
function speakable(text) {
  return String(text || "")
    .replace(/```[\s\S]*?```/g, " (bloc de code) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, "$1")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function speakButton(m) {
  if (!SPEECH || !m.content) return null;
  return el("button", { class: "msg-copy adv", type: "button", text: speaking === m.id ? "⏹ Arrêter" : "🔊 Écouter",
    title: "Lire la réponse à voix haute (la voix du navigateur)",
    onclick: (e) => {
      window.speechSynthesis.cancel();
      if (speaking === m.id) { speaking = null; e.target.textContent = "🔊 Écouter"; return; }
      const utterance = new SpeechSynthesisUtterance(speakable(m.content));
      utterance.lang = "fr-FR";
      const voice = window.speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith("fr"));
      if (voice) utterance.voice = voice;
      utterance.onend = utterance.onerror = () => {
        if (speaking === m.id) speaking = null;
        e.target.textContent = "🔊 Écouter";
      };
      speaking = m.id;
      e.target.textContent = "⏹ Arrêter";
      window.speechSynthesis.speak(utterance);
    } });
}

// --- compare two AIs: the other one's answer, beside the reply ---------------------------

function variantCard(v) {
  const body = el("div", { class: "msg-body" });
  body.innerHTML = v.status === "running" ? "<p><em>Elle répond…</em></p>" : renderMarkdown(v.content || "");
  const card = el("div", { class: "msg assistant variant" + (v.status === "running" ? " cursor" : "") },
    el("div", { class: "variant-head", text: `Comparaison · ${aiName(v.model)}` }), body);
  if (v.status === "error") card.append(el("div", { class: "msg-error", text: v.error || "erreur" }));
  const meta = [formatDuration(v.duration_ms)].filter(Boolean).join(" · ");
  card.append(el("div", { class: "msg-meta", title: v.tokens_in !== null ? `${v.tokens_in} tokens envoyés, ${v.tokens_out} reçus` : null },
    el("span", { text: meta }),
    v.status === "ok" && canWrite() && !state.data.running ? el("button", { class: "msg-copy", type: "button",
      text: "Garder celle-ci", title: "Elle prend la place de la réponse ; la suivante repart de la mémoire",
      onclick: async () => {
        try { await api("POST", `/api/variants/${v.id}/keep`); await refreshConversation(); toast("Réponse gardée."); }
        catch (err) { toast(err.message); }
      } }) : null));
  return card;
}

function fillCompareChoices() {
  const select = $("compare-with");
  const conv = state.data && state.data.conversation;
  const show = canWrite() && conv && conv.mode === "single";
  $("compare-row").classList.toggle("hidden", !show);
  if (!show) { select.value = ""; renderCompareChip(); return; }
  const keep = select.value;
  const others = state.options.models.filter((x) => x !== conv.model && x !== "default");
  select.replaceChildren(el("option", { value: "", text: "personne" }),
    ...others.map((x) => el("option", { value: x, text: aiName(x) })));
  select.value = others.includes(keep) ? keep : "";
  renderCompareChip();
}

// Chosen in the « ＋ » menu: said above the composer until the request goes (or ✕).
function renderCompareChip() {
  const select = $("compare-with");
  const chip = $("compare-chip");
  const on = !$("compare-row").classList.contains("hidden") && Boolean(select.value);
  chip.classList.toggle("hidden", !on);
  if (!on) { chip.replaceChildren(); return; }
  chip.replaceChildren(el("span", { text: `⇄ La prochaine demande est aussi posée à ${aiName(select.value)}, pour comparer.` }),
    el("button", { type: "button", class: "fav-remove", text: "✕", title: "Ne pas comparer", "aria-label": "Ne pas comparer",
      onclick: () => { select.value = ""; renderCompareChip(); } }));
}

// --- regenerate the last reply, edit a request (a branch) --------------------------------

function canWrite() {
  const conv = state.data && state.data.conversation;
  return Boolean(conv) && !conv.isolation?.paused && conv.mode === "single" && (conv.my_role || "owner") !== "read";
}

// Under the last reply: send its request again, with the same AI or another one.
function regenerateButtons(m, messages) {
  if (!canWrite() || state.data.running || messages[messages.length - 1] !== m) return [];
  const prev = messages[messages.length - 2];
  if (!prev || prev.role !== "user") return [];
  const go = async (model) => {
    try {
      await api("POST", `/api/conversations/${state.currentId}/regenerate`, model ? { model } : {});
      await refreshConversation();
    } catch (err) { toast(err.message); }
  };
  const buttons = [el("button", { class: "msg-copy", type: "button", text: "Régénérer",
    title: "Renvoyer la même demande : la réponse actuelle est remplacée", onclick: () => go(null) })];
  if ((state.data.conversation.my_role || "owner") === "owner") {
    const others = state.options.models.filter((x) => x !== state.data.conversation.model);
    const select = el("select", { class: "msg-regen-select adv", "aria-label": "Régénérer avec une autre IA",
      title: "Régénérer avec une autre IA (elle devient l'IA de la conversation)" },
      el("option", { value: "", text: "Avec une autre IA…" }),
      ...others.map((x) => el("option", { value: x, text: aiName(x) })));
    select.addEventListener("change", () => { if (select.value) go(select.value); });
    buttons.push(select);
  }
  return buttons;
}

// On a request: correct it in a new conversation that starts from there (the original stays).
function editButton(m) {
  if (!canWrite() || !canCreate()) return null;
  return el("button", { class: "msg-edit adv", type: "button", text: "Modifier",
    title: "Corriger cette demande : une nouvelle conversation repart de là, l'originale ne change pas",
    onclick: async () => {
      const prompt = await askText("Modifier la demande", "Une nouvelle conversation (une branche) reprend tout ce qui précède, puis envoie la demande corrigée.", m.content);
      if (prompt === null) return;
      try {
        const r = await api("POST", `/api/messages/${m.id}/branch`, { prompt });
        await loadConversations();
        await openConversation(r.conversation);
        toast("Branche créée : l'originale n'a pas changé.");
      } catch (err) { toast(err.message); }
    } });
}

// A dialog with a text to write (longer than prompt() allows). Resolves to the text, or null.
function askText(title, help, value = "") {
  return new Promise((resolve) => {
    const dialog = $("ask-dialog");
    $("ask-title").textContent = title;
    $("ask-help").textContent = help;
    $("ask-text").value = value;
    const done = (result) => { dialog.close(); resolve(result); };
    $("ask-ok").onclick = () => done($("ask-text").value);
    $("ask-cancel").onclick = () => done(null);
    dialog.oncancel = () => resolve(null);
    dialog.showModal();
    $("ask-text").focus();
  });
}

// --- updates (the owner's; 1.2): a newer version on GitHub, or new code on this PC --------------
//
// Two kinds of « mise à jour à faire »: a release newer than this one (an install from the
// .zip installs it in a click), or the code of this PC changed since the server started (a
// development copy: Codex or « git pull » brought commits) and a restart puts it in service.

const UPDATE_EVERY = 30 * 60 * 1000;  // the server asks GitHub at most every 6 hours anyway

function updateTodo(data) {
  const code = (data && data.code) || {};
  if (code.restart_needed) {
    const to = code.disk && code.disk !== code.running ? code.disk : null;
    return { kind: "restart", short: to ? `Mise à jour à faire : ${to}` : "Mise à jour à faire : code nouveau",
      long: "Le code d'AI Hub a changé sur ce PC depuis le démarrage du serveur : un redémarrage le met en service." };
  }
  if (data && data.latest) {
    return { kind: "release", short: `Mise à jour à faire : ${data.latest.version}`,
      long: `AI Hub ${data.latest.version} est publiée (celle-ci : ${data.current}).` };
  }
  return null;
}

async function checkUpdate(refresh = false) {
  if (!isOwner()) return null;
  let data;
  try { data = await api("GET", `/api/update${refresh ? "?refresh=true" : ""}`); } catch { return null; }
  state.update = data;
  const todo = updateTodo(data);
  $("updates").classList.remove("hidden");
  for (const id of ["update-banner", "hub-badge", "updates-badge"]) $(id).classList.toggle("hidden", !todo);
  $("updates-sub").textContent = todo ? todo.short : `À jour : version ${data.current}`;
  const banner = $("update-banner");
  if (todo) { banner.textContent = `⬆ ${todo.short}`; banner.title = `${todo.long} Cliquez pour voir.`; }
  banner.onclick = openUpdates;
  tidyHub();
  return data;
}

// Back when the server answers on the new code: the page reloads (it is the new one too).
function waitForRestart(done, said) {
  toast(said);
  const banner = $("update-banner");
  banner.disabled = true;
  banner.textContent = "Redémarrage en cours…";
  banner.classList.remove("hidden");
  let tries = 0;
  const poll = setInterval(async () => {
    tries++;
    try { if (await done()) { clearInterval(poll); location.reload(); return; } } catch { /* still restarting */ }
    if (tries >= 60) {
      clearInterval(poll);
      banner.disabled = false;
      banner.textContent = "Le serveur ne répond pas encore : rechargez la page dans un moment";
      banner.onclick = () => location.reload();
    }
  }, 3000);
}

async function openUpdates() {
  const body = el("div", { class: "updates-body" }, el("p", { text: "Vérification…" }));
  const actions = el("div", { class: "dialog-actions" });
  const error = el("p", { class: "form-error", role: "alert" });
  const dialog = el("dialog", { class: "dialog dialog-wide dialog-content" }, el("h2", { text: "Mises à jour" }), body, error, actions);
  const close = el("button", { type: "button", class: "btn btn-ghost", text: "Fermer", onclick: () => dialog.close() });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  const render = (data) => {
    const code = data.code || {};
    const todo = updateTodo(data);
    const facts = el("dl", { class: "access-facts" });
    const fact = (label, value) => facts.append(el("dt", { text: label }), el("dd", { text: value }));
    fact("En service", `AI Hub ${code.running || data.current}`);
    if (code.git) fact("Sur ce PC", `copie de développement (git) · version ${code.disk || "?"} · ${String(code.commit || "").slice(0, 7)}`);
    fact("Publiée sur GitHub", data.latest ? `${data.latest.version} (plus récente)` : data.error ? "inconnue : GitHub ne répond pas" : "rien de plus récent");
    body.replaceChildren(el("p", { class: "updates-state " + (todo ? "todo" : "ok"),
      text: todo ? todo.long : "AI Hub est à jour ✓" }), facts);
    actions.replaceChildren();
    if (todo && todo.kind === "restart") {
      if (code.changes && code.changes.length) {
        body.append(el("h3", { text: "Ce qui a changé" }), el("ul", { class: "updates-changes" }, ...code.changes.map((c) => el("li", { text: c }))));
      }
      if (code.dirty && code.dirty.length) {
        body.append(el("p", { class: "warning", text: `Des fichiers du code sont en train de changer (pas encore enregistrés dans git) : ${code.dirty.slice(0, 6).join(", ")}${code.dirty.length > 6 ? "…" : ""}. Attendez la fin du travail en cours, puis revenez ici.` }));
      }
      body.append(el("p", { class: "quota-note", text: "Avant de redémarrer, le nouveau code est essayé à part (il se charge, il ouvre une copie de la base) : s'il ne démarre pas, rien ne s'arrête. Si la base doit changer de forme, elle est d'abord copiée à côté. L'appli revient toute seule en moins d'une minute." }));
      const go = el("button", { type: "button", class: "btn btn-primary", text: "Redémarrer sur le nouveau code",
        disabled: Boolean(code.dirty && code.dirty.length), onclick: async () => {
          go.disabled = true; error.textContent = "";
          go.textContent = "Essai du nouveau code…";
          try {
            await api("POST", "/api/update/restart");
            dialog.close();
            waitForRestart(async () => { const now = await api("GET", "/api/update"); return !(now.code || {}).restart_needed; },
              "Redémarrage : l'appli revient dans un instant.");
          } catch (err) { error.textContent = err.message; go.disabled = false; go.textContent = "Redémarrer sur le nouveau code"; }
        } });
      actions.append(go);
    } else if (todo && todo.kind === "release") {
      if (data.latest.notes) {
        const notes = el("div", { class: "msg-body updates-notes" });
        notes.innerHTML = renderMarkdown(data.latest.notes.slice(0, 4000));
        body.append(el("h3", { text: `Nouveautés de la ${data.latest.version}` }), notes);
      }
      if (data.can_apply) {
        body.append(el("p", { class: "quota-note", text: "L'appli télécharge la version, la vérifie, puis s'arrête une minute le temps de l'installer et revient toute seule. L'ancienne version et la base sont gardées dans data\\mise-a-jour." }));
        const install = el("button", { type: "button", class: "btn btn-primary", text: `Installer la ${data.latest.version}`, onclick: async () => {
          install.disabled = true; error.textContent = "";
          try {
            await api("POST", "/api/update/apply");
            dialog.close();
            const was = state.options.version;
            waitForRestart(async () => (await api("GET", "/api/options")).version !== was, "Mise à jour en cours : l'appli revient dans une minute.");
          } catch (err) { error.textContent = err.message; install.disabled = false; }
        } });
        actions.append(install);
      } else {
        body.append(el("p", { class: "quota-note", text: code.git
          ? "Cette copie se met à jour avec git (« git pull ») ; ensuite, cette fenêtre propose de redémarrer sur le nouveau code."
          : `Installation automatique impossible ici : ${data.reason}.` }));
        if (data.latest.page) actions.append(el("a", { class: "btn btn-ghost", href: data.latest.page, target: "_blank", rel: "noopener noreferrer", text: "Voir sur GitHub" }));
      }
    }
    const last = data.last_restart;
    if (last) body.append(el("p", { class: "quota-note", text: `Dernier redémarrage sur nouveau code (${formatDate(last.at)}) : `
      + (last.status === "ok" ? `réussi, version ${last.version}.` : `en échec — ${last.error || "?"}`) }));
    const again = el("button", { type: "button", class: "btn btn-ghost", text: "Vérifier maintenant", onclick: async () => {
      again.disabled = true;
      const fresh = await checkUpdate(true);
      if (fresh) render(fresh); else again.disabled = false;
    } });
    actions.prepend(again);
    actions.append(close);
  };
  const data = await checkUpdate(false);
  if (!dialog.open) return;
  if (data) render(data);
  else { body.replaceChildren(); error.textContent = "État des mises à jour illisible pour l'instant."; actions.replaceChildren(close); }
}

// For everyone: the server now runs another version than this page. Said once, with a reload.
async function checkPageVersion() {
  try {
    const options = await api("GET", "/api/options");
    if (!state.pageBuild || !options.build || options.build === state.pageBuild) return;
    $("reload-text").textContent = options.version !== state.options.version
      ? `AI Hub est passé en ${options.version} : rechargez la page pour en profiter.`
      : "AI Hub a été mis à jour : rechargez la page pour en profiter.";
    $("reload-banner").classList.remove("hidden");
  } catch { /* offline: nothing to say */ }
}

// --- files and images sent with a request ------------------------------------------------

const MAX_FILE = 20 * 1024 * 1024;

function renderPending() {
  const box = $("pending-files");
  const files = state.pendingFiles || [];
  box.classList.toggle("hidden", !files.length);
  box.replaceChildren(...files.map((f) => el("span", { class: "file-chip" + (f.uploading ? " uploading" : "") },
    f.image && f.id ? el("img", { src: `/api/attachments/${f.id}`, alt: "" }) : el("span", { text: "📄" }),
    el("span", { class: "file-name", text: f.name, title: `${f.name} · ${Math.ceil(f.size / 1024)} Ko` }),
    f.uploading ? el("span", { class: "quota-note", text: "envoi…" }) : el("button", {
      type: "button", class: "fav-remove", text: "✕", title: "Retirer", "aria-label": `Retirer ${f.name}`,
      onclick: async () => {
        state.pendingFiles = state.pendingFiles.filter((x) => x !== f);
        renderPending();
        if (f.id) api("DELETE", `/api/attachments/${f.id}`).catch(() => {});
      } }))));
}

async function uploadFiles(fileList) {
  const cid = state.currentId;
  if (cid === null) return;
  for (const file of fileList) {
    if (file.size > MAX_FILE) { toast(`« ${file.name} » : 20 Mo au plus.`); continue; }
    const entry = { name: file.name || "image.png", size: file.size, image: file.type.startsWith("image/"), uploading: true };
    state.pendingFiles = [...(state.pendingFiles || []), entry];
    renderPending();
    try {
      const res = await fetch(`/api/conversations/${cid}/attachments`, { method: "POST", body: file,
        headers: { "X-AIHub": "1", "X-Filename": encodeURIComponent(entry.name),
          "Content-Type": file.type || "application/octet-stream" } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `erreur ${res.status}`);
      Object.assign(entry, data, { uploading: false });
    } catch (err) {
      toast(`« ${entry.name} » : ${err.message}`);
      state.pendingFiles = state.pendingFiles.filter((x) => x !== entry);
    }
    if (state.currentId === cid) renderPending();
  }
}

function setupAttachments() {
  // The owner and the admins: a file of the PC, in the app's explorer, or one of this device (a phone's photo).
  $("attach").addEventListener("click", () => {
    if (!isAdmin()) { $("attach-input").click(); return; }
    const cid = state.currentId;
    openPathPicker({ title: "Joindre un fichier du PC", files: "*", action: "Joindre ce fichier",
      device: { multiple: true, onFiles: (chosen) => uploadFiles(chosen) },
      onPick: async (path) => {
        try {
          const made = await api("POST", `/api/conversations/${cid}/attachments/pc`, { path });
          if (state.currentId !== cid) return;
          state.pendingFiles = [...(state.pendingFiles || []), { id: made.id, name: made.name, size: made.size, image: made.image }];
          renderPending();
        } catch (err) { toast(err.message); }
      } });
  });
  $("attach-input").addEventListener("change", async () => {
    await uploadFiles([...$("attach-input").files]);
    $("attach-input").value = "";
  });
  // A screenshot pasted in the composer, or files dropped on it.
  $("prompt").addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length && !$("attach").classList.contains("hidden")) { event.preventDefault(); uploadFiles(files); }
  });
  const zone = $("composer");
  zone.addEventListener("dragover", (event) => {
    if ([...(event.dataTransfer?.types || [])].includes("Files")) { event.preventDefault(); zone.classList.add("dropping"); }
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dropping"));
  zone.addEventListener("drop", (event) => {
    zone.classList.remove("dropping");
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length && !$("attach").classList.contains("hidden")) { event.preventDefault(); uploadFiles(files); }
  });
}

// The files a request took, under it: pictures shown, the others to download.
function messageFiles(files) {
  if (!files || !files.length) return null;
  return el("div", { class: "msg-files" }, ...files.map((f) => f.image
    ? el("a", { href: `/api/attachments/${f.id}`, target: "_blank", rel: "noopener", title: f.name },
      el("img", { src: `/api/attachments/${f.id}`, alt: f.name, loading: "lazy" }))
    : el("a", { class: "file-chip", href: `/api/attachments/${f.id}`, download: f.name, title: "Télécharger" },
      el("span", { text: "📄" }), el("span", { class: "file-name", text: f.name }))));
}

// --- consumption: who used how much (the owner's and the admins') -------------------------

const PROVIDER_NAMES = { claude_code: "Claude", codex: "Codex", ollama: "IA locale" };

function setupConsumption() {
  const dialog = $("consumption-dialog");
  const number = (n) => n.toLocaleString("fr-FR");
  const render = async () => {
    $("consumption-error").textContent = "";
    let data;
    try { data = await api("GET", `/api/usage?days=${$("consumption-days").value}`); }
    catch (err) { $("consumption-error").textContent = err.message; return; }
    if (!data.people.length) {
      $("consumption-table").replaceChildren(el("p", { class: "list-empty", text: "Aucune demande sur cette période." }));
      return;
    }
    const head = el("tr", {}, ...["Personne", "Demandes", "Réponses d'IA", "Tokens envoyés", "Tokens reçus", "Par IA"]
      .map((t) => el("th", { text: t })));
    const rows = data.people.map((p) => el("tr", {},
      el("td", { text: p.name }), el("td", { text: number(p.requests) }), el("td", { text: number(p.runs) }),
      el("td", { text: number(p.tokens_in) }), el("td", { text: number(p.tokens_out) }),
      el("td", { text: Object.entries(p.by_provider).map(([k, v]) =>
        `${PROVIDER_NAMES[k] || k} : ${number(v.runs)}`).join(" · ") })));
    $("consumption-table").replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));
  };
  $("consumption").addEventListener("click", async () => { dialog.showModal(); await render(); });
  $("consumption-days").addEventListener("change", render);
  $("consumption-close").addEventListener("click", () => dialog.close());
}

// --- favorite requests: kept on the server, for each person -------------------------------

// A window of their own (1.2): the « ＋ » menu opens it.
function setupFavorites() {
  const box = $("prompt");
  const rows = el("div", { class: "favorites-list" });
  const keep = el("button", { class: "btn btn-ghost", type: "button", text: "★ Garder la demande écrite" });
  const dialog = el("dialog", { class: "dialog favorites-dialog", "aria-label": "Demandes favorites" },
    el("h2", { text: "Demandes favorites" }),
    el("p", { class: "quota-note", text: "Un clic remet la demande dans la zone de saisie : relisez-la, changez-la si besoin, puis envoyez." }),
    rows, el("div", { class: "dialog-actions" }, keep,
      el("button", { class: "btn btn-ghost", type: "button", text: "Fermer", onclick: () => dialog.close() })));
  document.body.append(dialog);
  let list = [];
  const render = () => {
    rows.replaceChildren(...(list.length ? list.map((f) => el("div", { class: "fav-row" },
      el("button", { class: "menu-item", type: "button", title: f.text,
        onclick: () => {
          // Inserted where the cursor is: read, change it if needed, then send.
          const at = box.selectionStart ?? box.value.length;
          box.value = box.value.slice(0, at) + f.text + box.value.slice(box.selectionEnd ?? at);
          dialog.close();
          box.focus();
          box.selectionStart = box.selectionEnd = at + f.text.length;
          fitPrompt();
          saveDraft(state.currentId, box.value);
        } }, f.title, f.title !== f.text ? el("small", { text: f.text }) : null),
      el("button", { class: "fav-remove", type: "button", text: "✕", title: "Retirer des favoris",
        "aria-label": `Retirer « ${f.title} » des favoris`,
        onclick: async () => {
          try { list = (await api("DELETE", `/api/favorites/${f.id}`)).favorites; render(); } catch (err) { toast(err.message); }
        } })))
      : [el("p", { class: "quota-note", text: "Aucune demande favorite pour l'instant. Écrivez une demande, puis « Garder la demande écrite »." })]));
  };
  keep.addEventListener("click", async () => {
    const text = box.value.trim();
    if (!text) { toast("Écrivez d'abord la demande à garder."); return; }
    const title = await askText("Nom de cette demande favorite", "Il apparaîtra dans la liste des favorites.", text.split("\n")[0].slice(0, 60));
    if (title === null) return;
    try { list = (await api("POST", "/api/favorites", { title: title.trim() || text.slice(0, 60), text })).favorites; render(); toast("Demande gardée en favori."); }
    catch (err) { toast(err.message); }
  });
  $("favorites-button").addEventListener("click", async () => {
    try { list = (await api("GET", "/api/favorites")).favorites; } catch (err) { toast(err.message); }
    render();
    dialog.showModal();
  });
}

// --- dictation: the browser's speech recognition, into the composer ---------------------

function setupDictation() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  // Unknown to this browser (Firefox), or a page in plain http on the network: no button.
  if (!Recognition || !window.isSecureContext) return;
  const button = $("dictate");
  button.classList.remove("hidden");
  let recognition = null;
  let base = "";      // the text before the dictation started
  const stop = () => { if (recognition) recognition.stop(); };
  button.addEventListener("click", () => {
    if (recognition) { stop(); return; }
    recognition = new Recognition();
    recognition.lang = "fr-FR";
    recognition.interimResults = true;
    recognition.continuous = true;
    const box = $("prompt");
    base = box.value && !/\s$/.test(box.value) ? box.value + " " : box.value;
    recognition.onresult = (event) => {
      let said = "";
      for (const result of event.results) said += result[0].transcript;
      box.value = base + said;
      fitPrompt();
      saveDraft(state.currentId, box.value);
    };
    recognition.onerror = (event) => {
      if (event.error === "not-allowed" || event.error === "service-not-allowed") toast("Micro refusé : autorisez-le pour ce site.");
      else if (event.error !== "aborted" && event.error !== "no-speech") toast(`Dictée interrompue (${event.error}).`);
    };
    recognition.onend = () => {
      recognition = null;
      button.classList.remove("listening");
      button.setAttribute("aria-pressed", "false");
      $("prompt").focus();
    };
    try {
      recognition.start();
      button.classList.add("listening");
      button.setAttribute("aria-pressed", "true");
    } catch (err) {
      recognition = null;
      toast(err.message);
    }
  });
  // Sent, or another conversation: the dictation stops.
  $("composer").addEventListener("submit", stop);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
}

// --- import: ChatGPT, Claude, Claude Code, Codex (aihub/importer.py) ---------------------

function setupImportDialog() {
  const dialog = $("import-dialog");
  let reading = null;        // { reading, conversations } from the server
  const chosen = new Set();
  const error = (text) => { $("import-error").textContent = text || ""; };
  const count = () => {
    $("import-go").disabled = chosen.size === 0;
    $("import-go").textContent = chosen.size > 1 && $("import-merge").checked
      ? `Fusionner ${chosen.size} conversations` : `Importer${chosen.size ? ` (${chosen.size})` : ""}`;
    $("import-title-row").classList.toggle("hidden", !$("import-merge").checked);
  };
  const render = () => {
    const words = $("import-filter").value.trim().toLowerCase();
    const shown = reading.conversations.filter((c) => !words || c.title.toLowerCase().includes(words));
    $("import-list").replaceChildren(...(shown.length ? shown.map((c) => {
      const box = el("input", { type: "checkbox", checked: chosen.has(c.key) });
      box.addEventListener("change", () => { if (box.checked) chosen.add(c.key); else chosen.delete(c.key); count(); });
      return el("label", { class: "import-item" }, box,
        el("span", { class: "device-text" },
          el("span", { class: "conv-item-title", text: c.title }),
          el("span", { class: "conv-item-sub", text: `${c.source_name} · ${formatDate(c.created_at)} · ${c.count} message(s)`
            + (c.workdir ? ` · ${c.workdir}` : "") })));
    }) : [el("p", { class: "list-empty", text: "Aucune conversation ne correspond." })]));
    count();
  };
  const received = (data) => {
    reading = data;
    chosen.clear();
    $("import-pick").classList.remove("hidden");
    $("import-filter").value = "";
    const sources = [...new Set(data.conversations.map((c) => c.source_name))].join(", ");
    $("import-state").textContent = `${data.conversations.length} conversation(s) trouvée(s) (${sources || "aucune"}). Cochez celles à importer.`;
    render();
  };
  const wait = (text) => { error(); $("import-state").textContent = text; $("import-pick").classList.add("hidden"); };
  $("import").addEventListener("click", () => {
    reading = null;
    chosen.clear();
    $("import-form").reset();
    $("import-pick").classList.add("hidden");
    $("import-state").textContent = "Choisissez d'où viennent les conversations.";
    $("import-project").replaceChildren(...projectOptions(null, "Aucun projet"));
    $("import-model").replaceChildren(...modelOptions(rememberedChoices().model || "default"));
    $("import-workdir").value = rememberedChoices().workdir || "";
    error();
    count();
    dialog.showModal();
  });
  $("import-cancel").addEventListener("click", () => dialog.close());
  // The import's files (1.2.3): of the PC in the app's explorer (the owner and the admins), else of this device.
  for (const [id, kinds, title] of [["import-file", "zip,json", "Un export de ChatGPT ou de Claude"],
    ["import-project-file", "zip", "Un projet AI Hub (.zip)"]]) {
    const label = $(id).closest("label");
    label.addEventListener("click", (event) => {
      if (!isAdmin() || event.target === $(id)) return;  // the device's own window
      event.preventDefault();
      openPathPicker({ title, files: kinds, action: "Importer ce fichier",
        device: { accept: kinds.split(",").map((k) => `.${k}`).join(","), onFiles: (chosen) => {
          const box = $(id);
          const shelf = new DataTransfer();
          shelf.items.add(chosen[0]);
          box.files = shelf.files;
          box.dispatchEvent(new Event("change"));
        } },
        onPick: (path) => id === "import-file" ? importFromPc(path) : importProjectFromPc(path) });
    });
  }
  const importFromPc = async (path) => {
    wait(`Lecture de « ${path.split(/[\\/]/).pop()} »…`);
    try { received(await api("POST", "/api/import/pc", { path })); } catch (err) { $("import-state").textContent = ""; error(err.message); }
  };
  $("import-file").addEventListener("change", async () => {
    const file = $("import-file").files[0];
    if (!file) return;
    wait(`Lecture de « ${file.name} » (${Math.ceil(file.size / 1024 / 1024)} Mo)…`);
    try {
      const res = await fetch("/api/import/file", { method: "POST", body: file,
        headers: { "X-AIHub": "1", "Content-Type": "application/octet-stream" } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `erreur ${res.status}`);
      received(data);
    } catch (err) { $("import-state").textContent = ""; error(err.message); }
    $("import-file").value = "";
  });
  // A whole project (made by « Exporter le projet »): its folder chosen here, its notes copied or not.
  $("import-project-file").addEventListener("change", () => {
    const file = $("import-project-file").files[0];
    $("import-project-file").value = "";
    if (file) importProject({ file });
  });
  const importProjectFromPc = (path) => importProject({ path });
  const importProject = ({ file = null, path = null }) => {
    $("import-dialog").close();
    const workdir = el("input", { placeholder: "À choisir", "aria-label": "Dossier de travail du projet" });
    const notes = el("input", { type: "checkbox", checked: true });
    const error = el("p", { class: "form-error", role: "alert" });
    const go = el("button", { type: "button", class: "btn btn-primary", text: "Importer le projet" });
    const dialog = el("dialog", { class: "dialog dialog-content" });
    const done = () => { dialog.close(); dialog.remove(); };
    dialog.append(el("h2", { text: "Importer un projet" }),
      el("p", { class: "quota-note", text: `« ${file ? file.name : path.split(/[\\/]/).pop()} » : le projet, ses conversations (elles repartent de leur mémoire), ses consignes et ses documents. Un projet du même nom reçoit « (importé) ».` }),
      isAdmin() ? el("label", {}, "Dossier de travail du projet, sur le PC serveur", workdir) : null,
      isOwner() ? el("label", { class: "check" }, notes, "Recopier ses notes de projet dans le coffre (sans jamais en écraser une)") : null,
      error, el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "btn btn-ghost", text: "Annuler", onclick: done }), go));
    go.addEventListener("click", async () => {
      go.disabled = true; error.textContent = "";
      try {
        const query = new URLSearchParams({ workdir: workdir.value.trim(), notes: isOwner() && notes.checked ? "true" : "false" });
        let data;
        if (path) data = await api("POST", `/api/projects/import/pc?${query}`, { path });
        else {
          const res = await fetch(`/api/projects/import?${query}`, { method: "POST", body: file,
            headers: { "X-AIHub": "1", "Content-Type": "application/zip" } });
          data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `erreur ${res.status}`);
        }
        done();
        await loadConversations();
        toast(`Projet « ${data.project.name} » importé : ${data.conversations} conversation(s), ${data.documents} document(s)`
          + (data.notes ? `, ${data.notes} note(s)` : "") + (data.skipped.length ? `. Non repris : ${data.skipped.join(" ; ")}` : "."));
      } catch (err) { error.textContent = err.message; go.disabled = false; }
    });
    document.body.append(dialog);
    dialog.showModal();
    if (isAdmin()) pathField(workdir, { title: "Dossier de travail du projet importé" });
  };
  $("import-local").addEventListener("click", async () => {
    wait("Lecture des sessions de Claude Code et de Codex sur ce PC…");
    try { received(await api("GET", "/api/import/local")); } catch (err) { $("import-state").textContent = ""; error(err.message); }
  });
  $("import-filter").addEventListener("input", () => reading && render());
  $("import-all").addEventListener("click", () => {
    const words = $("import-filter").value.trim().toLowerCase();
    const shown = reading.conversations.filter((c) => !words || c.title.toLowerCase().includes(words));
    const all = shown.every((c) => chosen.has(c.key));
    for (const c of shown) { if (all) chosen.delete(c.key); else chosen.add(c.key); }
    render();
  });
  $("import-merge").addEventListener("change", count);
  $("import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!reading || !chosen.size) return;
    error();
    $("import-go").disabled = true;
    try {
      const merge = $("import-merge").checked && chosen.size > 1;
      const r = await api("POST", "/api/import", {
        reading: reading.reading, keys: [...chosen], merge, title: merge ? $("import-title").value.trim() : null,
        workdir: $("import-workdir").value.trim(),
        project_id: $("import-project").value ? Number($("import-project").value) : null,
        model: $("import-model").value });
      dialog.close();
      toast(merge ? "Conversations fusionnées en une seule." : `${r.conversations.length} conversation(s) importée(s).`);
      await loadConversations();
      await openConversation(r.conversations[0]);
    } catch (err) { error(err.message); count(); }
  });
}

// --- people, sharing, own AI accounts (version 0.2) -----------------------------------

const ROLE_LABELS = { read: "Lecture", write: "Lecture et écriture" };

// Accesses chosen for a new person: [{conversation_id | project_id, role}], with a select per target.
function grantPicker(box) {
  const rows = [];
  const add = (label, target, sub) => {
    const select = el("select", { "aria-label": `Accès à ${label}` },
      el("option", { value: "", text: "Pas d'accès" }),
      ...Object.entries(ROLE_LABELS).map(([v, t]) => el("option", { value: v, text: t })));
    rows.push({ target, select });
    return el("div", { class: "grant-row" }, el("span", { class: "device-text" },
      el("span", { class: "conv-item-title", text: label }), sub ? el("span", { class: "conv-item-sub", text: sub }) : null), select);
  };
  const items = [];
  for (const p of state.projects) {
    const count = state.conversations.filter((c) => c.project_id === p.id).length;
    items.push(add(p.name, { project_id: p.id }, `projet entier · ${count} conversation(s)`));
  }
  for (const c of state.conversations.filter((c) => c.project_id === null)) {
    items.push(add(c.title, { conversation_id: c.id }, "conversation"));
  }
  box.replaceChildren(...(items.length ? items : [el("p", { class: "list-empty", text: "Aucun projet ni conversation à partager pour l'instant." })]));
  return () => rows.filter((r) => r.select.value).map((r) => ({ ...r.target, role: r.select.value }));
}

function setupPeopleDialog() {
  const dialog = $("people-dialog");
  const error = (err) => { $("people-error").textContent = err ? err.message : ""; };
  let chosenGrants = () => [];
  const roleSelect = (u) => {
    const select = el("select", { "aria-label": `Rôle de ${u.name}` },
      ...[["invite", "Invité"], ["membre", "Membre"], ["admin", "Admin"]].map(([v, t]) =>
        el("option", { value: v, text: t, selected: u.role === v })));
    select.addEventListener("change", async () => {
      try { await api("PATCH", `/api/users/${u.id}`, { role: select.value }); await render(); } catch (err) { error(err); await render(); }
    });
    return select;
  };
  const render = async () => {
    error();
    let data;
    try { data = await api("GET", "/api/users"); } catch (err) { error(err); return; }
    $("people-list").replaceChildren(...data.users.map((u) => {
      const sees = u.is_owner ? "propriétaire du serveur : voit et gère tout"
        : u.role === "admin" ? "voit et gère tout"
          : (u.grants.length ? `voit : ${u.grants.map((g) => `${g.title || "?"} (${ROLE_LABELS[g.role]})`).join(", ")}` : "rien de partagé")
            + (u.role === "membre" ? " · et ses propres conversations" : "");
      const devices = (u.devices || []).map((d) => d.label || deviceName(d.user_agent));
      return el("div", { class: "device person" },
        el("div", { class: "device-text" },
          el("span", { class: "conv-item-title", text: u.name + (u.disabled ? " (désactivé·e)" : "") + (u.has_password ? "" : " · sans mot de passe") }),
          el("span", { class: "conv-item-sub", text: sees }),
          el("span", { class: "conv-item-sub", text: devices.length ? `appareils : ${devices.join(", ")}` : "aucun appareil connecté" })),
        u.disabled ? null : el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Connecter un appareil",
          title: "Un QR code à scanner avec son téléphone : il est connecté à ce compte",
          onclick: async () => {
            const label = prompt("Nom de l'appareil :", `Téléphone de ${u.name}`);
            if (label === null) return;
            try { showDeviceLink(await api("POST", `/api/users/${u.id}/device-link`, { label }), `Connecter un appareil à ${u.name}`); }
            catch (err) { error(err); }
          } }),
        u.is_owner ? el("span", { class: "chip", text: "Propriétaire" }) : roleSelect(u),
        el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Renommer", title: "Le nom pour se connecter",
          onclick: async () => {
            const name = prompt(`Nouveau nom pour se connecter (au lieu de « ${u.name} ») :`, u.name);
            if (!name || name.trim() === u.name) return;
            try {
              const r = await api("PATCH", `/api/users/${u.id}`, { name: name.trim() });
              if (state.me && u.name === state.me.name) { state.me.name = r.user.name; $("me-name").textContent = r.user.name; }
              await render();
            } catch (err) { error(err); }
          } }),
        u.is_owner ? null : el("button", { class: "btn btn-ghost btn-small", type: "button", text: u.disabled ? "Réactiver" : "Désactiver",
          onclick: async () => { try { await api("PATCH", `/api/users/${u.id}`, { disabled: !u.disabled }); await render(); } catch (err) { error(err); } } }),
        u.is_owner ? null : el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Supprimer",
          onclick: async () => {
            const ok = await confirmDialog({ title: `Supprimer ${u.name} ?`,
              text: "Son accès, ses partages et ses comptes d'IA enregistrés ici sont effacés. Ses messages restent dans les conversations.",
              okText: "Supprimer" });
            if (!ok) return;
            try { await api("DELETE", `/api/users/${u.id}`); await render(); } catch (err) { error(err); }
          } }));
    }));
    const waiting = data.invitations.filter((i) => !i.used_at);
    $("invitation-list").replaceChildren(...waiting.map((i) => el("div", { class: "device" },
      el("div", { class: "device-text" },
        el("span", { class: "conv-item-title", text: `Invitation${i.note ? " · " + i.note : ""} · ${ROLE_NAMES[i.role] || i.role}` }),
        el("span", { class: "conv-item-sub", text: `valable jusqu'au ${formatDate(i.expires_at)}` })),
      el("button", { class: "btn btn-danger-ghost btn-small", type: "button", text: "Annuler",
        onclick: async () => { try { await api("DELETE", `/api/invitations/${i.id}`); await render(); } catch (err) { error(err); } } }))));
    // The server's own password: the owner's to set.
    $("server-password-box").classList.toggle("hidden", !isOwner());
    $("server-password-state").textContent = data.server_password
      ? "Ce serveur a un mot de passe : il est demandé une fois par appareil, avant le nom."
      : "Ce serveur n'a pas de mot de passe : le nom (et le mot de passe du compte, s'il y en a un) suffit.";
    $("server-password-remove").classList.toggle("hidden", !data.server_password);
  };
  const showHow = () => {
    const direct = $("add-how").value === "direct";
    $("add-direct").classList.toggle("hidden", !direct);
    $("add-note-row").classList.toggle("hidden", direct);
    $("add-person").textContent = direct ? "Créer le compte" : "Créer le lien d'invitation";
    $("add-grants-box").classList.toggle("hidden", $("add-role").value === "admin");  // an admin sees all
  };
  $("add-how").addEventListener("change", showHow);
  $("add-role").addEventListener("change", showHow);
  $("people").addEventListener("click", async () => {
    $("invite-link-box").classList.add("hidden");
    chosenGrants = grantPicker($("add-grants"));
    showHow();
    await render();
    dialog.showModal();
  });
  $("people-close").addEventListener("click", () => dialog.close());
  $("add-person").addEventListener("click", async () => {
    error();
    const role = $("add-role").value;
    const grants = role === "admin" ? [] : chosenGrants();
    try {
      if ($("add-how").value === "direct") {
        const r = await api("POST", "/api/users", { name: $("new-user-name").value.trim(),
          password: $("new-user-password").value, role, grants });
        const pass = $("new-user-password").value ? "et le mot de passe choisi" : "(sans mot de passe)";
        $("new-user-name").value = "";
        $("new-user-password").value = "";
        $("invite-link").value = location.origin + "/";
        $("invite-link-text").textContent = `Compte « ${r.user.name} » créé. Donnez-lui cette adresse, son nom ${pass}`
          + (state.session && state.session.server_password ? ", et le mot de passe du serveur." : ".");
      } else {
        const r = await api("POST", "/api/invitations", { note: $("invite-note").value.trim() || null, role, grants });
        $("invite-note").value = "";
        $("invite-link").value = r.link;
        $("invite-link-text").textContent = "Envoyez ce lien à la personne (valable 7 jours, une seule fois) : elle "
          + "choisira son nom et son mot de passe. Elle doit pouvoir joindre ce serveur (Tailscale ou réseau local).";
      }
      $("invite-link-box").classList.remove("hidden");
      chosenGrants = grantPicker($("add-grants"));
      await render();
    } catch (err) { error(err); }
  });
  $("invite-copy").addEventListener("click", async () => {
    $("invite-link").select();
    try { await navigator.clipboard.writeText($("invite-link").value); } catch { fallbackCopy($("invite-link").value); }
    toast("Copié.");
  });
  const setServerPassword = async (password) => {
    try {
      const r = await api("POST", "/api/server-password", { password });
      state.session.server_password = r.server_password;
      $("server-password").value = "";
      toast(r.server_password ? "Mot de passe du serveur enregistré." : "Le serveur n'a plus de mot de passe.");
      await render();
    } catch (err) { error(err); }
  };
  $("server-password-save").addEventListener("click", () => setServerPassword($("server-password").value));
  $("server-password-remove").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Retirer le mot de passe du serveur ?",
      text: "Le nom (et le mot de passe du compte, s'il y en a un) suffira pour entrer.", okText: "Retirer" });
    if (ok) setServerPassword("");
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
      // The owner and the admins see everything already: nothing to share with them.
      people = (await api("GET", "/api/people")).people;
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

let openAccounts = async () => {};  // set by setupAccountsDialog

function setupAccountsDialog() {
  const dialog = $("accounts-dialog");
  let timer = null;
  let required = false;  // the first login: at least one AI before going on
  const error = (err) => { $("accounts-error").textContent = err ? err.message : ""; };
  // Échap does not skip the first login's step.
  dialog.addEventListener("cancel", (event) => { if (required) event.preventDefault(); });
  const render = async () => {
    let data;
    try { data = await api("GET", "/api/accounts"); } catch (err) { error(err); return; }
    dialog.querySelector("h2").textContent = required ? "Connectez une IA pour commencer" : "Mes comptes IA";
    dialog.querySelector("p.quota-note").textContent = data.own
      ? "Vos demandes utilisent vos comptes, jamais ceux du propriétaire du serveur. Connectez votre abonnement "
        + "Claude et/ou ChatGPT : la page de connexion s'ouvre chez vous, votre mot de passe ne passe pas par ce serveur."
      : "Les comptes de cet ordinateur : vos demandes (et celles de l'appli sans compte à elle) les utilisent. "
        + "Connectez Claude et/ou ChatGPT ; se déconnecter se fait dans un terminal.";
    $("accounts-close").textContent = required ? "Continuer" : "Fermer";
    $("accounts-close").disabled = required && !data.ready;
    $("accounts-close").title = required && !data.ready ? "Connectez au moins Claude ou ChatGPT" : "";
    let waiting = false;
    $("accounts-list").replaceChildren(...Object.entries(data.providers).map(([provider, a]) => {
      const login = a.login && a.login.state === "waiting" ? a.login : null;
      if (login) waiting = true;
      const row = el("div", { class: "account" },
        el("div", { class: "device-text" },
          el("span", { class: "conv-item-title", text: a.name }),
          el("span", { class: "conv-item-sub", text: a.error ? a.error : a.signed_in ? "connecté : vos demandes l'utilisent" : "pas connecté" })));
      if (a.signed_in) {
        if (data.own) row.append(el("button", { class: "btn btn-ghost btn-small", type: "button", text: "Se déconnecter",
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
  openAccounts = async (mustConnect = false) => {
    required = mustConnect;
    error(null);
    dialog.showModal();
    await render();
  };
  $("my-accounts").addEventListener("click", () => openAccounts(false));
  $("accounts-close").addEventListener("click", () => {
    clearTimeout(timer);
    required = false;
    dialog.close();
    loadQuota(true);
    loadStatus();
  });
}

// The first login (and any later one) with no AI connected: connect one before going on.
async function askForAnAI() {
  try {
    const data = await api("GET", "/api/accounts");
    if (!data.ready) await openAccounts(true);
  } catch { /* the page still works; « Mes comptes IA » stays in the menu */ }
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
  const initialParams = new URLSearchParams(location.search);
  setupLogin();
  setupOffline();
  setupMenus();
  setupHub();
  setupQuotaDialog();
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
  setupImportDialog();
  setupDictation();
  setupFavorites();
  setupConsumption();
  setupAttachments();
  setupInstructions();
  setupShortcuts();
  setupServerDialog();
  setupVaultsDialog();
  setupTrialsDialog();
  setupPathPickers();
  setupShareDialog();
  setupAccountsDialog();
  setupPasswordDialog();
  setupNewDialog();
  setupProjectDialog();
  setupProjectActions();
  setupActions();
  setupOrganisation();
  setupExport();
  setupObsidianDialog();
  setupHome();
  try {
    state.options = await api("GET", "/api/options");
    state.pageBuild = state.options.build;  // what this page was loaded with
  } catch (err) {
    toast(err.message);
  }
  applyOptions();
  await Promise.all([loadConversations(), loadStatus(), loadQuota(true), loadAgents()]);
  if (state.currentId === null) renderHome();
  let guideSeen = false;
  try { guideSeen = localStorage.getItem("aihub-guide-seen") === "1"; } catch { /* optional storage */ }
  if (!state.conversations.length && !initialParams.get("conversation") && canCreate() && !guideSeen) openGettingStarted();
  checkUpdate();  // a newer version on GitHub, or new code on this PC: said to the owner
  setInterval(() => { if (document.visibilityState === "visible") checkUpdate(); }, UPDATE_EVERY);
  // Opened from an alert: straight to its conversation.
  const asked = Number(initialParams.get("conversation"));
  if (asked && state.conversations.some((c) => c.id === asked)) {
    history.replaceState(null, "", "/");
    await openConversation(asked);
    if (initialParams.get('comptes') === '1' && state.data?.conversation?.isolation?.isolated
        && state.data.conversation.my_role !== 'read') await openSpaceAccounts(initialParams.get('fournisseur'));
  }
  // Fresh figures every 5 minutes while the page is displayed (reading them costs no quota),
  // and whether the server now runs another page than this one.
  setInterval(() => { if (document.visibilityState === "visible") { loadQuota(true); checkPageVersion(); } }, 5 * 60 * 1000);
  // The list follows the other conversations (« en cours », « nouveau ») while the page is displayed.
  setInterval(() => { if (document.visibilityState === "visible") loadConversations().catch(() => {}); }, 20 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { loadConversations().catch(() => {}); checkPageVersion(); }
  });
}

main();
