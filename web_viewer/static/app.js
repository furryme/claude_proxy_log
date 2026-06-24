/* ── Chat Log Viewer — Frontend ────────────────────────
 * State management, API calls, rendering.
 * ───────────────────────────────────────────────────── */

const API = "";  // same origin

// ── State ───────────────────────────────────────────
const state = {
  view: "dashboard",         // dashboard | list | search | chat
  hours: [],
  models: [],
  summary: null,
  currentHour: null,
  currentPage: 1,
  totalPages: 1,
  currentSeqId: null,
  requestList: [],
  chatData: null,
  systemVisible: false,
  toolsVisible: false,
  searchQuery: "",
  searchScope: "all",
  _searchDate: null,
};

// ── DOM refs ────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Utility ─────────────────────────────────────────
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 ** 2) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / (1024 ** 2)).toFixed(1) + " MB";
  return (n / (1024 ** 3)).toFixed(1) + " GB";
}

function fmtDuration(ms) {
  if (ms == null || ms == null) return "-";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(1) + "s";
}

function escHtml(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// Simple markdown renderer
function renderMarkdown(text) {
  if (!text) return "";
  let html = escHtml(text);

  // code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code class="language-${lang}">${code}</code></pre>`;
  });

  // inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // headings
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // bold / italic
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // blockquote
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // unordered list
  html = html.replace(/^[\-\*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/((:<li>.*<\/li><br>\n?)+)/g, (m) => {
    const clean = m.replace(/<br>\n?/g, "");
    return `<ul>${clean}</ul>`;
  });

  // links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  // paragraphs
  html = html.replace(/\n\n/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');

  return html;
}

// Parse content that might be array of blocks (text, tool_use, tool_result)
function renderContentBlocks(content) {
  if (typeof content === "string") {
    return renderMarkdown(content);
  }
  if (Array.isArray(content)) {
    return content.map(block => {
      if (block.type === "text") return renderMarkdown(block.text);
      if (block.type === "tool_use") {
        return `<div class="tool-use-block">
          <div class="tool-use-header" onclick="this.parentElement.classList.toggle('collapsed')">
            <span class="tool-use-icon">&#9881;</span>
            <span>Tool: ${escHtml(block.name)}</span>
            <span class="tool-toggle">&#9660;</span>
          </div>
          <div class="tool-use-content">${escHtml(JSON.stringify(block.input, null, 2))}</div>
        </div>`;
      }
      if (block.type === "tool_result") {
        const preview = (typeof block.content === "string") ? block.content : JSON.stringify(block.content);
        // Extract short ID from tool_use_id
        let idLabel = "?";
        if (block.tool_use_id) {
          const match = block.tool_use_id.match(/call_([a-f0-9]+)/i);
          idLabel = match ? match[1].substring(0, 8) : block.tool_use_id.substring(0, 12);
        }
        return `<div class="tool-result-block tool-collapsible">
          <div class="tool-result-header" onclick="this.parentElement.classList.toggle('collapsed')">
            <div class="tool-result-label">Result <span style="color:var(--cyan)">[${escHtml(idLabel)}]</span></div>
            <span class="tool-toggle">&#9660;</span>
          </div>
          <div class="tool-result-content">${escHtml(preview)}</div>
        </div>`;
      }
      return "";
    }).join("");
  }
  return escHtml(String(content));
}

// ── API helpers ─────────────────────────────────────
async function api(path, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = `${API}${path}${qs ? "?" + qs : ""}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

// ── View switching ──────────────────────────────────
function showView(name) {
  state.view = name;
  $("#dashboardView").classList.toggle("hidden", name !== "dashboard");
  $("#requestListView").classList.toggle("hidden", !["list", "search"].includes(name));
  $("#chatView").classList.toggle("hidden", name !== "chat");

  // Toggle table headers based on view mode
  const listHeader = $("#requestListHeader");
  const searchHeader = $("#searchListHeader");
  if (name === "search") {
    listHeader.classList.add("hidden");
    searchHeader.classList.remove("hidden");
  } else {
    listHeader.classList.remove("hidden");
    searchHeader.classList.add("hidden");
  }

  const bc = $("#breadcrumb");
  if (name === "dashboard") bc.innerHTML = "Dashboard";
  else if (name === "list") bc.innerHTML = `<a href="#" onclick="showDashboard();return false">Dashboard</a> / <strong>${escHtml(state.currentHour || "All Requests")}</strong>`;
  else if (name === "search") {
    const scopeLabel = state.searchScope === "all" ? "all" : state.searchScope;
    const timeLabel = state.currentHour || (state._searchDate ? state._searchDate : "last 24h");
    bc.innerHTML = `<a href="#" onclick="showDashboard();return false">Dashboard</a> / <strong>Search «${escHtml(state.searchQuery)}»</strong> — ${timeLabel}, ${scopeLabel}`;
  }
  else if (name === "chat") bc.innerHTML = `<a href="#" onclick="showDashboard();return false">Dashboard</a> / <a href="#" onclick="showList();return false">${escHtml(state.currentHour || "Requests")}</a> / <strong>Seq #${state.currentSeqId}</strong>`;
}

// ── Dashboard ───────────────────────────────────────
async function showDashboard() {
  showView("dashboard");

  try {
    const [summary, hours] = await Promise.all([
      api("/api/summary"),
      api("/api/hours"),
    ]);

    state.summary = summary;
    state.hours = hours.hours;
    state.models = summary.models;

    $("#sumTotal").textContent = summary.total;
    $("#sumOk").textContent = summary.success;
    $("#sumErr").textContent = summary.errors;

    // Model filter dropdown
    const sel = $("#filterModel");
    sel.innerHTML = '<option value="">All Models</option>';
    summary.models.forEach(m => {
      const o = document.createElement("option");
      o.value = m.model;
      o.textContent = `${m.model} (${m.count})`;
      sel.appendChild(o);
    });

    // Model cards
    const stats = $("#modelStats");
    stats.innerHTML = summary.models.slice(0, 8).map(m => `
      <div class="model-card" onclick="filterByModel('${escHtml(m.model)}')">
        <div class="model-name">${escHtml(m.model)}</div>
        <div class="model-count">${m.count} requests</div>
      </div>
    `).join("");

    // Hours list
    renderHoursList();

    // Activity chart
    const chart = $("#recentChart");
    const maxCount = Math.max(...summary.recent_hours.map(h => h.count), 1);
    chart.innerHTML = summary.recent_hours.slice(0, 15).reverse().map(h => `
      <div class="chart-row">
        <span class="chart-label">${escHtml(h.hour_key)}</span>
        <div class="chart-bar-wrap">
          <div class="chart-bar" style="width:${(h.count / maxCount * 100).toFixed(1)}%"></div>
        </div>
        <span class="chart-value">${h.count}</span>
      </div>
    `).join("");

  } catch (e) {
    $("#viewArea").innerHTML = `<div class="error-message"><h3>Error loading data</h3><p>${escHtml(e.message)}</p></div>`;
  }
}

function renderHoursList() {
  const list = $("#hoursList");
  // Group by date
  const groups = {};
  state.hours.forEach(h => {
    if (!groups[h.date]) groups[h.date] = [];
    groups[h.date].push(h);
  });

  let html = "";
  for (const [date, hours] of Object.entries(groups)) {
    html += `<div class="hour-date">${escHtml(date)}</div>`;
    hours.forEach(h => {
      const active = state.currentHour === h.hour_key ? " active" : "";
      html += `<div class="hour-item${active}" onclick="loadHour('${h.hour_key}')">
        <span class="hour-time">${escHtml(h.hour)}:00</span>
        <span class="hour-count">${h.count || ""}</span>
      </div>`;
    });
  }
  list.innerHTML = html;
}

// ── Request List ────────────────────────────────────
async function showList(hourKey) {
  if (hourKey) {
    state.currentHour = hourKey;
  }
  state.currentPage = 1;
  showView("list");
  await loadRequestPage();
  renderHoursList();
}

async function loadHour(hourKey) {
  await showList(hourKey);
}

function filterByModel(model) {
  state.currentHour = null;
  state.currentPage = 1;
  showView("list");
  const sel = $("#filterModel");
  sel.value = model;
  loadRequestPage();
}

async function loadRequestPage() {
  const tbody = $("#requestsBody");
  tbody.innerHTML = '<tr><td colspan="8"><div class="loading"><div class="spinner"></div>Loading...</div></td></tr>';

  try {
    const model = $("#filterModel").value;
    const params = {
      page: state.currentPage,
      limit: 50,
    };
    if (state.currentHour) params.hour_key = state.currentHour;
    if (model) params.model = model;

    const data = await api("/api/requests", params);
    state.requestList = data.items;
    state.totalPages = data.total_pages;

    $("#listInfo").textContent = `${data.total} requests, page ${data.page}/${data.total_pages}`;

    // Pagination
    renderPagination(data.page, data.total_pages);

    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--text-muted)">No requests found</td></tr>';
      return;
    }

    tbody.innerHTML = data.items.map(item => {
      const statusClass = item.status === 200 ? "ok" : (item.status ? "err" : "pending");
      const mc = item.msg_count || 0;
      return `<tr onclick="viewRequest(${item.seq_id})">
        <td><span class="seq-link">${item.seq_id}</span></td>
        <td class="datetime-cell">${item.date ? item.date + " " + item.time : item.time}</td>
        <td>${escHtml(item.model)}</td>
        <td><span class="status-badge ${statusClass}">${item.status || "-"}</span></td>
        <td>${fmtDuration(item.duration_ms)}</td>
        <td>${fmtSize(item.body_len)}</td>
        <td>${fmtSize(item.resp_size)}</td>
        <td><span class="msg-count" title="${mc} messages">${mc}</span></td>
      </tr>`;
    }).join("");

  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="error-message"><h3>Error</h3><p>${escHtml(e.message)}</p></div></td></tr>`;
  }
}

function renderPagination(page, total) {
  const wrap = $("#pagination");
  if (total <= 1) { wrap.innerHTML = ""; return; }

  let html = `<button class="page-btn" onclick="goPage(${page - 1})" ${page <= 1 ? "disabled" : ""}>&lt;</button>`;

  const range = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) range.push(i);
  } else {
    range.push(1);
    if (page > 3) range.push("...");
    for (let i = Math.max(2, page - 1); i <= Math.min(total - 1, page + 1); i++) range.push(i);
    if (page < total - 2) range.push("...");
    range.push(total);
  }

  range.forEach(p => {
    if (p === "...") {
      html += `<span style="color:var(--text-muted);padding:0 4px">...</span>`;
    } else {
      const active = p === page ? " active" : "";
      html += `<button class="page-btn${active}" onclick="goPage(${p})">${p}</button>`;
    }
  });

  html += `<button class="page-btn" onclick="goPage(${page + 1})" ${page >= total ? "disabled" : ""}>&gt;</button>`;
  wrap.innerHTML = html;
}

function goPage(p) {
  if (p < 1 || p > state.totalPages) return;
  state.currentPage = p;
  loadRequestPage();
}

// ── Keyword Search ──────────────────────────────────
function highlightMatch(snippet, q) {
  if (!q || !snippet) return escHtml(snippet);
  const escaped = escHtml(snippet);
  const eq = escHtml(q);
  const regex = new RegExp(eq.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), "gi");
  return escaped.replace(regex, '<mark>$&</mark>');
}

async function showSearch(q) {
  state.searchQuery = q;
  state.searchScope = $("#searchScope").value;
  state.currentPage = 1;

  // Determine time scope: current hour > current date > last 24h (backend default)
  let hourKey = null;
  if (state.currentHour) {
    // Already in a specific hour — search that hour
    hourKey = state.currentHour;
  }

  showView("search");

  const tbody = $("#requestsBody");
  tbody.innerHTML = '<tr><td colspan="5"><div class="loading"><div class="spinner"></div>Searching...</div></td></tr>';

  try {
    const params = {
      q,
      scope: state.searchScope,
      page: 1,
      limit: 50,
    };
    if (hourKey) {
      params.hour_key = hourKey;
      state._searchDate = hourKey.split("_")[0]; // for breadcrumb
    }

    const data = await api("/api/search", params);

    $("#listInfo").innerHTML = `Search «${escHtml(q)}» — <strong>${data.total}</strong> matches ` +
      `<button class="btn btn-sm" onclick="showDashboard();return false" style="margin-left:12px">✕ clear</button>`;

    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-muted)">No matches found</td></tr>';
      $("#pagination").innerHTML = "";
      return;
    }

    tbody.innerHTML = data.items.map(item => {
      const scopeBadge = item.scope === "body" ? 'style="color:var(--purple)"' : 'style="color:var(--cyan)"';
      return `<tr onclick="viewRequest(${item.seq_id})">
        <td><span class="seq-link">${item.seq_id}</span></td>
        <td class="datetime-cell">${item.date} ${item.time}</td>
        <td>${escHtml(item.model)}</td>
        <td><span class="scope-badge" ${scopeBadge}>${item.scope}</span></td>
        <td class="snippet-cell">${highlightMatch(item.snippet, q)}</td>
      </tr>`;
    }).join("");

    $("#pagination").innerHTML = ""; // single-page for search results

  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="error-message"><h3>Error</h3><p>${escHtml(e.message)}</p></div></td></tr>`;
  }
}

// ── Chat Detail ─────────────────────────────────────
async function viewRequest(seqId) {
  state.currentSeqId = seqId;
  showView("chat");

  const body = $("#chatBody");
  body.innerHTML = '<div class="loading"><div class="spinner"></div>Loading conversation...</div>';
  $("#chatHeader").innerHTML = "";
  $("#chatFooter").classList.add("hidden");
  $("#systemPanel").classList.add("hidden");
  $("#toolsPanel").classList.add("hidden");
  state._tools = [];

  try {
    const data = await api(`/api/requests/${seqId}`, { full: 1 });
    state.chatData = data;
    renderChat(data);
  } catch (e) {
    body.innerHTML = `<div class="error-message"><h3>Error loading request</h3><p>${escHtml(e.message)}</p></div>`;
  }
}

function renderChat(data) {
  // Header meta
  $("#chatHeader").innerHTML = `
    <div class="chat-meta">
      <div class="chat-meta-item"><span class="label">Seq</span><span class="value">#${data.seq_id}</span></div>
      <div class="chat-meta-item"><span class="label">Time</span><span class="value">${escHtml(data.ts)}</span></div>
      <div class="chat-meta-item"><span class="label">Model</span><span class="value">${escHtml(data.model)}</span></div>
      <div class="chat-meta-item"><span class="label">Status</span><span class="value">${data.status || "-"}</span></div>
      <div class="chat-meta-item"><span class="label">Duration</span><span class="value">${fmtDuration(data.duration_ms)}</span></div>
      <div class="chat-meta-item"><span class="label">Body</span><span class="value">${fmtSize(data.body_len)}</span></div>
      <div class="chat-meta-item"><span class="label">Response</span><span class="value">${fmtSize(data.resp_size)}</span></div>
    </div>`;

  const messages = data.request ? data.request.messages || [] : [];
  const response = data.response || {};
  const systemPrompt = data.system_prompt || "";

  // Build chat messages
  let html = "";

  messages.forEach((msg, idx) => {
    const role = msg.role || "unknown";
    const avatar = role === "user" ? "U" : role === "assistant" ? "A" : "S";
    const avatarClass = role;

    html += `<div class="message">
      <div class="message-avatar ${avatarClass}">${avatar}</div>
      <div class="message-content">
        <div class="message-role">${escHtml(role)}</div>`;

    // Reasoning (thinking) BEFORE content
    if (msg.reasoning) {
      html += `<div class="thinking-block collapsed">
        <div class="thinking-header" onclick="this.parentElement.classList.toggle('collapsed')">
          <span class="thinking-icon">&#128265;</span>
          <span>Thinking (${msg.reasoning.length} chars)</span>
          <span class="thinking-toggle">&#9660;</span>
        </div>
        <div class="thinking-content">${escHtml(msg.reasoning)}</div>
      </div>`;
    }

    // Content
    if (msg.content != null) {
      html += `<div class="message-text">${renderContentBlocks(msg.content)}</div>`;
    }

    html += `</div></div>`;
  });

  // Final assistant response from the response data
  if (response.text) {
    html += `<div class="message">
      <div class="message-avatar assistant">A</div>
      <div class="message-content">
        <div class="message-role">assistant</div>`;

    // Thinking BEFORE text
    if (response.thinking) {
      html += `<div class="thinking-block collapsed">
        <div class="thinking-header" onclick="this.parentElement.classList.toggle('collapsed')">
          <span class="thinking-icon">&#128265;</span>
          <span>Thinking (${response.thinking.length} chars)</span>
          <span class="thinking-toggle">&#9660;</span>
        </div>
        <div class="thinking-content">${escHtml(response.thinking)}</div>
      </div>`;
    }

    html += `<div class="message-text">${renderMarkdown(response.text)}</div>`;

    // Response meta
    const usage = response.usage || {};
    const tokens = [];
    if (usage.input_tokens) tokens.push(`Input: ${usage.input_tokens.toLocaleString()}`);
    if (usage.output_tokens) tokens.push(`Output: ${usage.output_tokens.toLocaleString()}`);
    if (usage.cache_creation_input_tokens) tokens.push(`Cache create: ${usage.cache_creation_input_tokens.toLocaleString()}`);
    if (usage.cache_read_input_tokens) tokens.push(`Cache read: ${usage.cache_read_input_tokens.toLocaleString()}`);

    if (tokens.length || response.stop_reason) {
      html += `<div class="response-meta">`;
      tokens.forEach(t => {
        html += `<span class="response-meta-item">&#128272; ${escHtml(t)}</span>`;
      });
      if (response.stop_reason) {
        html += `<span class="response-meta-item">Stop: ${escHtml(response.stop_reason)}</span>`;
      }
      html += `</div>`;
    }

    html += `</div></div>`;
  }

  if (!messages.length && !response.text) {
    html = `<div class="error-message"><p>No conversation data found</p></div>`;
  }

  $("#chatBody").innerHTML = html;

  // System prompt
  if (systemPrompt) {
    $("#systemText").textContent = systemPrompt;
    $("#systemLen").textContent = systemPrompt.length.toLocaleString();
  }

  // Tools
  const tools = data.request ? data.request.tools || [] : [];
  if (tools.length) {
    state._tools = tools;
    $("#toolsCount").textContent = tools.length;
    $("#toolsList").innerHTML = tools.map((t, i) => {
      const name = t.function ? t.function.name : t.name || "?";
      const desc = t.function ? t.function.description || "" : t.description || "";
      return `<div class="tool-card" onclick="openToolModal(${i})">
        <div class="tool-card-name">${escHtml(name)}</div>
        <div class="tool-card-desc">${escHtml(desc)}</div>
      </div>`;
    }).join("");
  }

  // Footer buttons
  $("#btnToggleSystem").style.display = systemPrompt ? "" : "none";
  $("#btnToggleTools").style.display = tools.length ? "" : "none";
  $("#chatFooter").classList.remove("hidden");

  // Next request button
  const allItems = state.requestList;
  const curIdx = allItems.findIndex(i => i.seq_id === state.currentSeqId);
  if (curIdx >= 0 && curIdx < allItems.length - 1) {
    $("#btnNextReq").style.display = "";
    $("#btnNextReq").setAttribute("href", "#");
    $("#btnNextReq").onclick = () => { viewRequest(allItems[curIdx + 1].seq_id); return false; };
  } else {
    $("#btnNextReq").style.display = "none";
  }

  state.systemVisible = false;
  state.toolsVisible = false;
}

// ── Panel toggles ───────────────────────────────────
function toggleSystemPanel() {
  state.systemVisible = !state.systemVisible;
  $("#systemPanel").classList.toggle("hidden", !state.systemVisible);
}

function toggleToolsPanel() {
  state.toolsVisible = !state.toolsVisible;
  $("#toolsPanel").classList.toggle("hidden", !state.toolsVisible);
}

function showRawModal() {
  if (state.chatData && state.chatData.response && state.chatData.response.raw_text) {
    $("#rawText").textContent = state.chatData.response.raw_text;
  } else {
    $("#rawText").textContent = "No response data available";
  }
  $("#rawModal").classList.remove("hidden");
}

function closeRawModal() {
  $("#rawModal").classList.add("hidden");
}

function openToolModal(idx) {
  const tool = state._tools && state._tools[idx];
  if (!tool) return;
  const name = tool.function ? tool.function.name : tool.name || "?";
  const desc = tool.function ? tool.function.description || "" : tool.description || "";
  $("#toolModalTitle").textContent = name;
  $("#toolModalBody").innerHTML = renderMarkdown(desc);
  $("#toolModalJson").textContent = JSON.stringify(tool, null, 2);
  $("#toolModal").classList.remove("hidden");
}

function closeToolModal() {
  $("#toolModal").classList.add("hidden");
}

// ── Sidebar toggle ──────────────────────────────────
function toggleSidebar() {
  $("#sidebar").classList.toggle("collapsed");
  const isCollapsed = $("#sidebar").classList.contains("collapsed");
  $("#sidebarOpenBtn").classList.toggle("hidden", !isCollapsed);
}

// ── Theme toggle ────────────────────────────────────
const THEME_KEY = "viewer-theme";

function getSystemTheme() {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_KEY, theme);
  const btn = $("#themeToggle");
  if (btn) btn.innerHTML = theme === "light" ? "&#9788;" : "&#9789;";
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
}

// ── Init ────────────────────────────────────────────
function init() {
  // Theme: user choice > system preference > dark default
  const saved = localStorage.getItem(THEME_KEY);
  applyTheme(saved || getSystemTheme());

  // Listen for system theme changes (only if user hasn't overridden)
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    if (!localStorage.getItem(THEME_KEY)) applyTheme(e.matches ? "light" : "dark");
  });

  $("#sidebarToggle").addEventListener("click", toggleSidebar);
  $("#sidebarOpenBtn").addEventListener("click", toggleSidebar);
  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#btnToggleSystem").addEventListener("click", toggleSystemPanel);
  $("#btnToggleTools").addEventListener("click", toggleToolsPanel);
  $("#btnRawResponse").addEventListener("click", showRawModal);

  $("#filterModel").addEventListener("change", () => {
    const model = $("#filterModel").value;
    state.currentHour = null;
    state.currentPage = 1;
    showView("list");
    loadRequestPage();
  });

  // Keyword search
  let searchTimer;
  $("#searchInput").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value.trim();
    if (val.length >= 2) {
      searchTimer = setTimeout(() => showSearch(val), 300);
    }
  });

  $("#searchInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      clearTimeout(searchTimer);
      const val = e.target.value.trim();
      if (val.length >= 1) showSearch(val);
    }
  });

  $("#searchScope").addEventListener("change", () => {
    if (state.view === "search" && state.searchQuery) {
      showSearch(state.searchQuery);
    }
  });

  showDashboard();
}

document.addEventListener("DOMContentLoaded", init);
