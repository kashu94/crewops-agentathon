// Shared helpers for every page: which controller this tab is, the sidebar
// shell, and a small fetch wrapper. No framework, no build step -- each
// page is a plain HTML file that calls these.

function getController() {
  const params = new URLSearchParams(window.location.search);
  return params.get("controller") || localStorage.getItem("controller") || "Ananya Iyer";
}

function withController(path) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("controller", getController());
  return url.pathname + url.search;
}

async function api(path) {
  const res = await fetch(withController(path));
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function inr(n) {
  if (n === null || n === undefined) return "-";
  return "₹" + Number(n).toLocaleString("en-IN");
}

function initials(name) {
  return name.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

// A fixed colour per name, not a random one per render -- the same
// controller should always show the same avatar colour across pages and
// reloads. Picked from a small fixed palette instead of a raw RGB hash,
// so every combination stays readable against a white avatar.
const AVATAR_PALETTE = ["#b3441f", "#1f6f4a", "#3b4bb0", "#8a6d1f", "#7a3b8a"];
function avatarColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
}

const ICONS = {
  "command-center": '<svg viewBox="0 0 20 20" fill="none"><rect x="2" y="2" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="11" y="2" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="2" y="11" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="11" y="11" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/></svg>',
  disruptions: '<svg viewBox="0 0 20 20" fill="none"><path d="M10 2.5 18 17H2L10 2.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 8v3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="10" cy="14" r="0.9" fill="currentColor"/></svg>',
  flights: '<svg viewBox="0 0 20 20" fill="none"><path d="M2 12.5 17 8l1 1.7-6.3 3.2.6 4.4-1.9.9-2-4-3.6 1.8-.3 2-1.4.6-.4-3.3-2-1Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  decisions: '<svg viewBox="0 0 20 20" fill="none"><rect x="3.5" y="2.5" width="13" height="15" rx="1.5" stroke="currentColor" stroke-width="1.6"/><path d="M6.5 7h7M6.5 10.5h7M6.5 14h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  chat: '<svg viewBox="0 0 20 20" fill="none"><path d="M3 4.5h14a1 1 0 0 1 1 1V13a1 1 0 0 1-1 1H8l-4 3v-3H3a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
};

function applyTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem("theme", next);
  } catch (e) {
    /* private-window storage can throw; theme just won't persist */
  }
}

async function renderShell(activePage) {
  applyTheme();
  localStorage.setItem("controller", getController());
  const controllers = await fetch("/api/controllers").then((r) => r.json());
  const current = getController();

  const navItem = (page, href, label) => `
    <a href="${href}" data-page="${page}">
      <span class="nav-icon">${ICONS[page]}</span>${label}
    </a>`;

  document.getElementById("app").insertAdjacentHTML(
    "afterbegin",
    `
    <div class="shell">
      <div class="sidebar">
        <div class="brand">AGENTATHON</div>
        <nav class="nav">
          ${navItem("command-center", withController('/'), "Command Center")}
          ${navItem("disruptions", withController('/disruptions.html'), "Disruption Workspace")}
          ${navItem("flights", withController('/flights.html'), "Flights &amp; Gates")}
          ${navItem("decisions", withController('/decisions.html'), "Decision Ledger")}
        </nav>
      </div>
      <div class="main">
        <div class="topbar">
          <div class="controller-switch" id="controller-switch"></div>
          <div class="topbar-right">
            <button type="button" class="icon-btn" id="theme-toggle" title="Toggle dark mode">
              <svg viewBox="0 0 20 20" fill="none"><path d="M17 11.5A7 7 0 0 1 8.5 3a7 7 0 1 0 8.5 8.5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
            </button>
            <div class="avatar" style="background:${avatarColor(current)}">${initials(current)}</div>
            <div class="who">
              <div class="who-name">${current}</div>
              <div class="who-role">Controller</div>
            </div>
          </div>
        </div>
        <div class="content" id="content"></div>
      </div>
    </div>
  `
  );

  document.querySelectorAll(`.nav a[data-page="${activePage}"]`).forEach((a) => a.classList.add("active"));
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  initChatWidget();

  const switchEl = document.getElementById("controller-switch");
  switchEl.innerHTML = controllers
    .map(
      (c) =>
        `<a href="${new URL(window.location.pathname + '?controller=' + encodeURIComponent(c.name), window.location.origin)}"
            class="${c.name === current ? 'active' : ''}">${c.name}</a>`
    )
    .join("");
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

// Floating Advisor Chat widget -- a bubble in the bottom-right corner on
// every page, not a separate nav destination. This is a plain multi-page
// app (each nav click is a full page load), so unlike a single-page app
// the widget's chat history doesn't survive moving to a different page --
// it starts fresh each time. An accepted trade-off for staying
// framework-free.
const CHAT_EXAMPLES = [
  "Is C-2087 legal to cover P-2291?",
  "Who is on reserve at BLR?",
  "What does RULE-DUTY-02 say?",
  "Captain C-1042 just called in sick for P-2291, who do I use?",
];

function initChatWidget() {
  if (document.getElementById("chat-fab")) return; // already injected on this page

  document.body.insertAdjacentHTML(
    "beforeend",
    `
    <button type="button" id="chat-fab" title="Advisor Chat">
      <span class="nav-icon">${ICONS.chat}</span>
    </button>
    <div class="chat-panel" id="chat-panel" hidden>
      <div class="chat-panel-header">
        <span>Advisor Chat</span>
        <button type="button" id="chat-panel-close" class="icon-btn" title="Close">&times;</button>
      </div>
      <div class="chat-panel-subtitle">
        Answered by the same router &rarr; tools &rarr; verifier pipeline the agents use &mdash;
        no language model in this console, so every answer is either fully sourced or says
        plainly that it needs one.
      </div>
      <div class="chat-examples" id="chat-examples"></div>
      <div class="chat-log" id="chat-log"></div>
      <div class="chat-input-row">
        <input type="text" id="chat-input" placeholder="Ask about a crew member, pairing, flight, or rule..." autocomplete="off">
        <button class="primary" id="chat-send">Ask</button>
      </div>
    </div>
  `
  );

  document.getElementById("chat-examples").innerHTML = CHAT_EXAMPLES
    .map((q) => `<button type="button" data-q="${escapeHtml(q)}">${escapeHtml(q)}</button>`)
    .join("");
  document.querySelectorAll("#chat-examples button").forEach((b) => {
    b.addEventListener("click", () => askAdvisor(b.dataset.q));
  });

  const fab = document.getElementById("chat-fab");
  const panel = document.getElementById("chat-panel");
  fab.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) document.getElementById("chat-input").focus();
  });
  document.getElementById("chat-panel-close").addEventListener("click", () => {
    panel.hidden = true;
  });

  document.getElementById("chat-send").addEventListener("click", () => {
    const input = document.getElementById("chat-input");
    if (input.value.trim()) askAdvisor(input.value.trim());
    input.value = "";
  });
  document.getElementById("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("chat-send").click();
  });
}

function appendChatMessage(role, html, cls) {
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = `msg ${role}${cls ? " " + cls : ""}`;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

async function askAdvisor(query) {
  document.getElementById("chat-examples").style.display = "none";
  appendChatMessage("user", escapeHtml(query));
  const thinking = appendChatMessage("bot", `<span class="empty">Routing...</span>`);

  try {
    const r = await apiPost("/api/ask", { query });
    let badge = "";
    if (!r.classified) {
      badge = `<span class="badge-nollm">needs Advisor Agent</span>`;
    } else if (r.verified === true) {
      badge = `<span class="badge-verified">verified</span>`;
    } else if (r.verified === false) {
      badge = `<span class="badge-unverified">unverified draft rejected</span>`;
    }
    thinking.classList.toggle("unverified", r.verified === false || !r.classified);
    thinking.innerHTML = `${escapeHtml(r.narrative)}
      <div class="meta">
        ${r.intent ? `<span>${escapeHtml(r.intent)} &middot; tier ${r.tier}</span>` : ""}
        ${badge}
        ${r.tool_calls && r.tool_calls.length ? `<span>${r.tool_calls.length} tool call(s)</span>` : ""}
      </div>`;
  } catch (e) {
    thinking.classList.add("unverified");
    thinking.textContent = `Error: ${e.message}`;
  }
  document.getElementById("chat-log").scrollTop = document.getElementById("chat-log").scrollHeight;
}
