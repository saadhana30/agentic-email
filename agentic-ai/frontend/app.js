// ── Auth helpers ──────────────────────────────────────────────────────────────

function getToken() {
  return localStorage.getItem("access_token") || sessionStorage.getItem("access_token");
}

function getRefreshToken() {
  return localStorage.getItem("refresh_token") || sessionStorage.getItem("refresh_token");
}

function saveTokens(access, refresh) {
  const storage = localStorage.getItem("access_token") ? localStorage : sessionStorage;
  storage.setItem("access_token",  access);
  storage.setItem("refresh_token", refresh);
}

function clearTokens() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  sessionStorage.removeItem("access_token");
  sessionStorage.removeItem("refresh_token");
}

function logout() {
  clearTokens();
  window.location.replace("/login");
}

// Auto-redirect to login if not authenticated
(function guardAuth() {
  if (!getToken()) {
    window.location.replace("/login");
  }
})();

// ── Authenticated fetch wrapper ───────────────────────────────────────────────

async function apiFetch(url, options = {}) {
  let token = getToken();
  if (!token) { logout(); return; }

  const headers = {
    ...(options.headers || {}),
    "Authorization": `Bearer ${token}`,
  };

  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }

  let res = await fetch(url, { ...options, headers });

  // Attempt token refresh on 401
  if (res.status === 401) {
    const refreshed = await tryRefresh();
    if (!refreshed) { logout(); return; }
    token = getToken();
    headers["Authorization"] = `Bearer ${token}`;
    res = await fetch(url, { ...options, headers });
    if (res.status === 401) { logout(); return; }
  }

  return res;
}

async function tryRefresh() {
  const rt = getRefreshToken();
  if (!rt) return false;
  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!res.ok) return false;
    const { access_token, refresh_token } = await res.json();
    saveTokens(access_token, refresh_token);
    return true;
  } catch {
    return false;
  }
}

// ── State ────────────────────────────────────────────────────────────────────
const API = "";
let currentInboxFilter = "all";
let currentUserRole    = "user";

// ── Init: load user info and render header ────────────────────────────────────
async function initUser() {
  try {
    const res  = await apiFetch(`${API}/api/auth/me`);
    const user = await res.json();
    currentUserRole = user.role;

    const header = document.getElementById("user-header");
    if (header) {
      header.innerHTML = `
        <span class="user-badge ${user.role}">${user.username} (${user.role})</span>
        <button class="btn-logout" onclick="logout()">Logout</button>`;
    }
  } catch(e) {
    console.error("Could not load user info", e);
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(name, el) {
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".nav-link").forEach(l => l.classList.remove("active"));
  document.getElementById(`tab-${name}`).classList.add("active");
  if (el) el.classList.add("active");

  document.getElementById("email-detail").classList.add("hidden");
  document.getElementById("tab-inbox").style.display    = name === "inbox"    ? "block" : "";
  document.getElementById("executed-detail").classList.add("hidden");
  document.getElementById("tab-executed").style.display = name === "executed" ? "block" : "";

  if (name === "inbox")    loadInbox();
  if (name === "executed") loadExecuted();
  if (name === "review")   loadReviewQueue();
  if (name === "clients")  loadClients();
}

// ── Inbox filter ──────────────────────────────────────────────────────────────
function setInboxFilter(filter, btn) {
  currentInboxFilter = filter;
  document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  loadInbox();
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const r = await apiFetch(`${API}/api/stats`);
    const d = await r.json();
    document.getElementById("stats-panel").innerHTML = `
      <div>Total: <strong>${d.total_emails}</strong></div>
      <div>Clients: <strong>${d.client_emails}</strong></div>
      <div>Spam: <strong>${d.spam_detected}</strong></div>
      <div>Pending Review: <strong>${d.pending_review}</strong></div>
      <div>Actions Taken: <strong>${d.actions_taken}</strong></div>`;
    const rb = document.getElementById("review-badge");
    if (rb) rb.textContent = d.pending_review > 0 ? d.pending_review : "";
  } catch(e) { console.error(e); }
}

// ── INBOX ─────────────────────────────────────────────────────────────────────
async function loadInbox() {
  const el = document.getElementById("email-list");
  el.innerHTML = `<div class="loader">Loading...</div>`;
  try {
    const r = await apiFetch(`${API}/api/emails?filter=all&limit=100`);
    let emails = await r.json();

    if (currentInboxFilter === "client") {
      emails = emails.filter(e => !e.is_spam && e.client_name);
    } else if (currentInboxFilter === "other") {
      emails = emails.filter(e => !e.is_spam && !e.client_name);
    } else if (currentInboxFilter === "spam") {
      emails = emails.filter(e => e.is_spam);
    }

    const sort = document.getElementById("inbox-sort")?.value || "newest";
    emails = sortEmails(emails, sort);

    if (!emails.length) {
      el.innerHTML = `<div class="empty">No emails in this category</div>`;
      return;
    }

    el.innerHTML = emails.map(e => {
      let sourceTag = "";
      let cardClass = "card";
      if (e.is_spam) {
        sourceTag = `<span class="tag tag-spam">Spam</span>`;
        cardClass = "card card-spam";
      } else if (e.client_name) {
        sourceTag = `<span class="tag tag-client">${escHtml(e.client_name)}</span>`;
        cardClass = "card card-client";
      } else {
        sourceTag = `<span class="tag tag-other">Other</span>`;
        cardClass = "card card-other";
      }

      return `
        <div class="${cardClass}" onclick="openDetail('${escHtml(e.id)}')">
          <div class="card-left">
            <h3>${escHtml(e.subject)}</h3>
            <p>${escHtml(e.sender)}</p>
            ${sourceTag}
            <span class="tag tag-other" style="margin-left:2px">${escHtml(e.attachment_type)}</span>
          </div>
          <div class="card-right">
            <div class="time">${formatDate(e.received_at)}</div>
          </div>
        </div>`;
    }).join("");
  } catch(e) { el.innerHTML = `<div class="loader">Failed to load</div>`; }
}

// ── EXECUTED ──────────────────────────────────────────────────────────────────
async function loadExecuted() {
  const el = document.getElementById("executed-list");
  el.innerHTML = `<div class="loader">Loading...</div>`;
  try {
    const r = await apiFetch(`${API}/api/emails?filter=executed`);
    let emails = await r.json();
    const sort = document.getElementById("executed-sort")?.value || "newest";
    emails = sortEmails(emails, sort);

    if (!emails.length) { el.innerHTML = `<div class="empty">No executed emails yet</div>`; return; }

    const badge = document.getElementById("executed-badge");
    if (badge) badge.textContent = emails.length || "";

    el.innerHTML = emails.map(e => `
      <div class="card" style="cursor:default">
        <div class="card-left">
          <h3>${escHtml(e.subject)}</h3>
          <p>${escHtml(e.sender)}</p>
          <span class="tag ${e.client_name ? 'tag-client' : 'tag-other'}">
            ${escHtml(e.client_name || 'Other')}
          </span>
          <span class="tag tag-success">Executed</span>
        </div>
        <div class="card-right">
          <div class="time">${formatDate(e.received_at)}</div>
          <button class="btn-view" onclick="openExecutedDetail('${escHtml(e.id)}')">View Actions</button>
          ${currentUserRole === 'admin'
            ? `<button class="btn-reprocess" onclick="reprocessEmail('${escHtml(e.id)}', this)">↺ Reprocess</button>`
            : ''}
        </div>
      </div>`).join("");
  } catch(e) { el.innerHTML = `<div class="loader">Failed to load</div>`; }
}

// ── Reprocess (Feature 7) ─────────────────────────────────────────────────────
async function reprocessEmail(emailId, btn) {
  if (!confirm("Reprocess this email? This will re-run all agents.")) return;
  btn.disabled    = true;
  btn.textContent = "Processing...";
  try {
    const r = await apiFetch(`${API}/api/reprocess/${emailId}`, { method: "POST" });
    if (r.ok) {
      showNotifPopup("Email reprocessed successfully", "system");
      loadExecuted();
      loadStats();
    } else {
      const d = await r.json();
      alert(d.detail || "Reprocess failed");
    }
  } catch(e) {
    alert("Reprocess failed");
  } finally {
    btn.disabled    = false;
    btn.textContent = "↺ Reprocess";
  }
}

// ── Executed detail ───────────────────────────────────────────────────────────
async function openExecutedDetail(emailId) {
  document.getElementById("tab-executed").style.display = "none";
  const panel = document.getElementById("executed-detail");
  panel.classList.remove("hidden");
  panel.querySelector("#executed-detail-content").innerHTML = `<div class="loader">Loading...</div>`;
  // Tell Nova which email the user is looking at
  if (typeof novaSetEmailContext === "function") novaSetEmailContext(emailId);

  try {
    const [analysisRes, actionsRes, tracesRes, attsRes] = await Promise.all([
      apiFetch(`${API}/api/analysis/${emailId}`),
      apiFetch(`${API}/api/actions/${emailId}`),
      apiFetch(`${API}/api/traces/${emailId}`),
      apiFetch(`${API}/api/attachments/${emailId}`),
    ]);

    const analysis = analysisRes.ok ? await analysisRes.json() : null;
    const actions  = actionsRes.ok  ? await actionsRes.json()  : [];
    const traces   = tracesRes.ok   ? await tracesRes.json()   : [];
    const atts     = attsRes.ok     ? await attsRes.json()     : [];
    const pct      = analysis ? Math.round(analysis.confidence * 100) : 0;

    panel.querySelector("#executed-detail-content").innerHTML = `
      <div class="detail-section">
        <h4>LLM Analysis</h4>
        <div class="detail-grid">
          <span class="label">Category</span>   <span>${escHtml(analysis?.category || "—")}</span>
          <span class="label">Intent</span>     <span>${escHtml(analysis?.intent || "—")}</span>
          <span class="label">Urgency</span>    <span><span class="tag tag-${analysis?.urgency}">${escHtml(analysis?.urgency || "—")}</span></span>
          <span class="label">Confidence</span>
          <span>${pct}%
            <div class="confidence-bar"><div class="confidence-fill" style="width:${pct}%"></div></div>
          </span>
          <span class="label">Agents Used</span>
          <span>${(analysis?.required_agents || []).map(a => `<span class="tag tag-client">${escHtml(a)}</span>`).join(" ")}</span>
        </div>
      </div>
      <div class="detail-section">
        <h4>Execution Plan</h4>
        <ul style="font-size:15px;padding-left:20px;line-height:2.2">
          ${(analysis?.execution_plan || []).map(s => `<li>${escHtml(s)}</li>`).join("")}
        </ul>
      </div>
      ${atts.length ? `
      <div class="detail-section">
        <h4>Attachments (${atts.length})</h4>
        ${atts.map(a => `
          <div class="action-card success" style="margin-bottom:10px">
            <h4>${escHtml(a.filename)} <span class="tag tag-client">${escHtml(a.attachment_type)}</span></h4>
            <div class="action-detail">
              Type: ${escHtml(a.mime_type)} &nbsp;|&nbsp; Extracted: ${a.char_count} chars<br>
              <small style="color:#94a3b8">${escHtml(a.extracted_text)}</small>
            </div>
          </div>`).join("")}
      </div>` : ""}
      <div class="detail-section">
        <h4>Actions Taken</h4>
        ${actions.length === 0
          ? `<p style="color:#999;font-size:14px">No actions recorded</p>`
          : actions.map(a => renderActionCard(a)).join("")}
      </div>
      ${traces.length ? `
      <div class="detail-section">
        <h4>Execution Trace</h4>
        <div class="trace-list">
          ${traces.map(t => `
            <div class="trace-row ${t.success ? 'trace-ok' : 'trace-fail'}">
              <span class="trace-icon">${t.success ? "✓" : "✗"}</span>
              <span class="trace-node">${escHtml(t.node_name)}</span>
              <span class="trace-duration">${t.duration_ms ? t.duration_ms.toFixed(0) + "ms" : "—"}</span>
              ${!t.success && t.exception_message
                ? `<span class="trace-error">${escHtml(t.exception_message)}</span>`
                : ""}
            </div>`).join("")}
        </div>
      </div>` : ""}`;
  } catch(e) {
    panel.querySelector("#executed-detail-content").innerHTML = `<p style="color:red">Failed to load</p>`;
  }
}

function renderActionCard(a) {
  const res    = a.action_taken || {};
  const status = a.status || "unknown";
  let details  = "";

  if (a.agent_name === "jira_agent" && res.issue_key) {
    details = `<div class="action-detail">
      Ticket: <strong>${escHtml(res.issue_key)}</strong><br>
      Assigned to: <strong>${escHtml(res.assignee || "Unassigned")}</strong><br>
      Team: ${escHtml(res.team || "—")} &nbsp;|&nbsp; Type: ${escHtml(res.issue_type || "—")}<br>
      <a class="action-link" href="${escHtml(res.issue_url || '')}" target="_blank">Open in Jira →</a>
    </div>`;
  } else if (a.agent_name === "jira_status_agent" && res.ticket_key) {
    details = `<div class="action-detail">
      Status fetched for: <strong>${escHtml(res.ticket_key)}</strong><br>
      Status: <strong>${escHtml(res.ticket_status || "—")}</strong>
    </div>`;
  } else if (a.agent_name === "calendar_agent" && res.slot) {
    details = `<div class="action-detail">
      Scheduled: <strong>${escHtml(res.slot)}</strong><br>
      ${res.rescheduled
        ? '<span class="tag tag-high">Rescheduled</span>'
        : '<span class="tag tag-success">On requested time</span>'}<br>
      ${res.event_link ? `<a class="action-link" href="${escHtml(res.event_link)}" target="_blank">Open in Calendar →</a>` : ""}
    </div>`;
  } else if (a.agent_name === "reply_agent") {
    details = `<div class="action-detail">Reply sent to client in same Gmail thread.</div>`;
  } else {
    details = `<div class="action-detail">${escHtml(res.reason || res.error || JSON.stringify(res))}</div>`;
  }

  return `<div class="action-card ${escHtml(status)}">
    <h4>${escHtml(a.agent_name)} <span class="tag tag-${escHtml(status)}" style="margin-left:8px">${escHtml(status)}</span></h4>
    ${details}
  </div>`;
}

function closeExecutedDetail() {
  document.getElementById("executed-detail").classList.add("hidden");
  document.getElementById("tab-executed").style.display = "block";
  if (typeof novaClearEmailContext === "function") novaClearEmailContext();
}

// ── Inbox detail ──────────────────────────────────────────────────────────────
async function openDetail(emailId) {
  document.getElementById("tab-inbox").style.display = "none";
  const panel = document.getElementById("email-detail");
  panel.classList.remove("hidden");
  panel.querySelector("#detail-content").innerHTML = `<div class="loader">Loading...</div>`;
  // Tell Nova which email the user is looking at
  if (typeof novaSetEmailContext === "function") novaSetEmailContext(emailId);

  try {
    const [analysisRes, actionsRes, tracesRes] = await Promise.all([
      apiFetch(`${API}/api/analysis/${emailId}`),
      apiFetch(`${API}/api/actions/${emailId}`),
      apiFetch(`${API}/api/traces/${emailId}`),
    ]);

    const analysis = analysisRes.ok ? await analysisRes.json() : null;
    const actions  = actionsRes.ok  ? await actionsRes.json()  : [];
    const traces   = tracesRes.ok   ? await tracesRes.json()   : [];
    const pct      = analysis ? Math.round(analysis.confidence * 100) : 0;

    panel.querySelector("#detail-content").innerHTML = `
      <div class="detail-section">
        <h4>LLM Analysis</h4>
        <div class="detail-grid">
          <span class="label">Category</span>  <span>${escHtml(analysis?.category || "—")}</span>
          <span class="label">Intent</span>    <span>${escHtml(analysis?.intent || "—")}</span>
          <span class="label">Urgency</span>   <span><span class="tag tag-${analysis?.urgency}">${escHtml(analysis?.urgency || "—")}</span></span>
          <span class="label">Confidence</span>
          <span>${pct}%
            <div class="confidence-bar"><div class="confidence-fill" style="width:${pct}%"></div></div>
          </span>
          <span class="label">Agents Used</span>
          <span>${(analysis?.required_agents || []).map(a => `<span class="tag tag-client">${escHtml(a)}</span>`).join(" ")}</span>
        </div>
      </div>
      <div class="detail-section">
        <h4>Execution Plan</h4>
        <ul style="font-size:15px;padding-left:20px;line-height:2.2">
          ${(analysis?.execution_plan || []).map(s => `<li>${escHtml(s)}</li>`).join("")}
        </ul>
      </div>
      <div class="detail-section">
        <h4>Actions</h4>
        ${actions.length === 0
          ? `<p style="color:#999;font-size:14px">No actions taken yet</p>`
          : actions.map(a => renderActionCard(a)).join("")}
      </div>
      ${traces.length ? `
      <div class="detail-section">
        <h4>Execution Trace</h4>
        <div class="trace-list">
          ${traces.map(t => `
            <div class="trace-row ${t.success ? 'trace-ok' : 'trace-fail'}">
              <span class="trace-icon">${t.success ? "✓" : "✗"}</span>
              <span class="trace-node">${escHtml(t.node_name)}</span>
              <span class="trace-duration">${t.duration_ms ? t.duration_ms.toFixed(0) + "ms" : "—"}</span>
              ${!t.success && t.exception_message
                ? `<span class="trace-error">${escHtml(t.exception_message)}</span>`
                : ""}
            </div>`).join("")}
        </div>
      </div>` : ""}`;
  } catch(e) {
    panel.querySelector("#detail-content").innerHTML = `<p style="color:red">Failed to load</p>`;
  }
}

function closeDetail() {
  document.getElementById("email-detail").classList.add("hidden");
  document.getElementById("tab-inbox").style.display = "block";
  if (typeof novaClearEmailContext === "function") novaClearEmailContext();
}

// ── Review queue ──────────────────────────────────────────────────────────────
async function loadReviewQueue() {
  const el = document.getElementById("review-list");
  el.innerHTML = `<div class="loader">Loading...</div>`;
  try {
    const r = await apiFetch(`${API}/api/review-queue`);
    const items = await r.json();
    if (!items.length) {
      el.innerHTML = `<div class="empty">No items pending review</div>`;
      return;
    }
    el.innerHTML = items.map(item => `
      <div class="review-card" id="review-${item.review_id}">
        <h3>${escHtml(item.subject)}</h3>
        <p>From: ${escHtml(item.sender)}</p>
        <p>Intent: ${escHtml(item.analysis?.intent || "unknown")}</p>
        <p>Confidence: ${Math.round((item.analysis?.confidence || 0) * 100)}%
           &nbsp;|&nbsp; Urgency: <span class="tag tag-${escHtml(item.analysis?.urgency || '')}">${escHtml(item.analysis?.urgency || "—")}</span></p>
        ${currentUserRole === 'admin' ? `
        <div class="review-actions">
          <button class="btn-approve" onclick="handleReview(${item.review_id}, 'approve')">✅ Approve</button>
          <button class="btn-reject"  onclick="handleReview(${item.review_id}, 'reject')">❌ Reject</button>
        </div>` : `<p style="color:#64748b;font-size:13px">Admin approval required</p>`}
      </div>`).join("");
  } catch(e) { el.innerHTML = `<div class="loader">Failed to load</div>`; }
}

async function handleReview(reviewId, action) {
  try {
    const r = await apiFetch(`${API}/api/review/${reviewId}`, {
      method: "POST",
      body: { action, reviewed_by: "admin" },
    });
    if (r.ok) {
      document.getElementById(`review-${reviewId}`)?.remove();
      showNotifPopup(
        action === "approve" ? "Approved — processing..." : "Rejected",
        "system"
      );
      loadStats();
    }
  } catch(e) { console.error(e); }
}

// ── Clients ───────────────────────────────────────────────────────────────────
async function loadClients() {
  const el = document.getElementById("client-list");
  try {
    const r = await apiFetch(`${API}/api/clients`);
    const clients = await r.json();
    if (!clients.length) {
      el.innerHTML = `<div class="empty">No clients added yet</div>`;
      return;
    }
    el.innerHTML = clients.map(c => `
      <div class="client-card">
        <div class="client-info">
          <h4>${escHtml(c.name)}</h4>
          <p>${escHtml(c.email_domain)} &nbsp;·&nbsp; Jira: <strong>${escHtml(c.jira_project_key)}</strong></p>
        </div>
        ${currentUserRole === 'admin'
          ? `<button class="btn-delete" onclick="deleteClient(${c.id})">Remove</button>`
          : ''}
      </div>`).join("");
  } catch(e) { el.innerHTML = `<div class="loader">Failed to load</div>`; }
}

document.getElementById("add-client-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (currentUserRole !== "admin") {
    alert("Admin access required");
    return;
  }
  const body = {
    name:             document.getElementById("client-name").value.trim(),
    email_domain:     document.getElementById("client-domain").value.trim(),
    jira_project_key: document.getElementById("client-jira").value.trim(),
  };
  try {
    const r = await apiFetch(`${API}/api/clients`, { method: "POST", body });
    if (r.ok) {
      e.target.reset();
      loadClients();
      showNotifPopup(`Client "${escHtml(body.name)}" added`, "system");
    } else {
      const d = await r.json();
      alert(d.detail || "Failed to add client");
    }
  } catch(e) { console.error(e); }
});

async function deleteClient(id) {
  if (!confirm("Remove this client?")) return;
  await apiFetch(`${API}/api/clients/${id}`, { method: "DELETE" });
  loadClients();
}

// ── SSE notifications (pass JWT token as query param) ─────────────────────────
function connectSSE() {
  const token = getToken();
  if (!token) return;
  const source = new EventSource(`${API}/events?token=${encodeURIComponent(token)}`);
  source.onmessage = (e) => {
    const data = JSON.parse(e.data);
    showNotifPopup(data.message, data.type);
    loadStats();
    if (document.getElementById("tab-executed")?.classList.contains("active")) {
      loadExecuted();
    }
  };
  source.onerror = () => {
    setTimeout(connectSSE, 5000);
    source.close();
  };
}

function showNotifPopup(message, type = "system") {
  const container = document.getElementById("notification-container");
  if (!container) return;
  const div = document.createElement("div");
  div.className = "notif-popup";
  div.innerHTML = `<span>${escHtml(message)}</span>
    <button class="close-btn" onclick="this.parentElement.remove()">×</button>`;
  container.appendChild(div);
  setTimeout(() => { if (div.parentNode) div.remove(); }, 6000);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function sortEmails(emails, sort) {
  if (sort === "newest") return [...emails].sort((a,b) => new Date(b.received_at) - new Date(a.received_at));
  if (sort === "oldest") return [...emails].sort((a,b) => new Date(a.received_at) - new Date(b.received_at));
  return emails;
}

function escHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day:"2-digit", month:"short", year:"numeric", timeZone: "Asia/Kolkata" })
       + " " + d.toLocaleTimeString("en-GB", { hour:"2-digit", minute:"2-digit", timeZone: "Asia/Kolkata" });
}

// ── Init ──────────────────────────────────────────────────────────────────────
initUser();
loadInbox();
loadStats();
connectSSE();
setInterval(loadStats, 30000);
