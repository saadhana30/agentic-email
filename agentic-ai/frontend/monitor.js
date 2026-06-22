/**
 * monitor.js
 * ----------
 * Live Execution Monitor — frontend logic.
 *
 * Architecture:
 *  - On first open: fetch last 200 events via GET /api/monitor/events
 *  - Then open SSE stream to GET /api/monitor/events/stream
 *  - Events are grouped by email_id and rendered as collapsible cards
 *  - Status filter (all / running / success / failure) applied client-side
 *  - Email-id click drills into a single email's event group
 *  - Auto-scrolls to newest event when new group is added
 *  - Max 200 events kept in memory; oldest pruned automatically
 */

(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────────
  const MAX_EVENTS    = 200;
  let _events         = [];          // [{id, email_id, timestamp, event_type, ...}]
  let _groups         = new Map();   // email_id → {emailId, events[], collapsed}
  let _statusFilter   = "all";
  let _emailFilter    = null;        // null = show all, string = show one email_id
  let _sseSource      = null;
  let _initialized    = false;

  // ── Event metadata — icons, labels, CSS class ─────────────────────────────
  const EVENT_META = {
    email_detected:          { icon: "📨", label: "Email Detected",         cls: "ev-info"    },
    attachment_extracted:    { icon: "📎", label: "Attachment Extracted",    cls: "ev-success" },
    node_started:            { icon: "▶",  label: "Node Started",            cls: "ev-running" },
    node_completed:          { icon: "✓",  label: "Node Completed",          cls: "ev-success" },
    node_failed:             { icon: "✗",  label: "Node Failed",             cls: "ev-failure" },
    spam_detected:           { icon: "🚫", label: "Spam Detected",           cls: "ev-info"    },
    routing_decision:        { icon: "↗",  label: "Routing Decision",        cls: "ev-info"    },
    agent_invoked:           { icon: "⚙",  label: "Agent Invoked",           cls: "ev-running" },
    jira_ticket_created:     { icon: "🎟", label: "Jira Ticket Created",     cls: "ev-success" },
    calendar_event_created:  { icon: "📅", label: "Calendar Event Created",  cls: "ev-success" },
    reply_sent:              { icon: "📧", label: "Reply Sent",              cls: "ev-success" },
    review_queued:           { icon: "⏳", label: "Queued for Review",       cls: "ev-info"    },
    processing_completed:    { icon: "✅", label: "Processing Completed",    cls: "ev-success" },
    processing_failed:       { icon: "❌", label: "Processing Failed",       cls: "ev-failure" },
  };

  function _meta(event_type) {
    return EVENT_META[event_type] || { icon: "•", label: event_type, cls: "ev-info" };
  }

  // ── Public init (called by app.js showTab) ─────────────────────────────────
  window.initMonitor = function () {
    if (_initialized) return;
    _initialized = true;
    _loadInitialEvents();
    _connectSSE();
  };

  // ── Filters ────────────────────────────────────────────────────────────────
  window.setMonitorFilter = function (filter, btn) {
    _statusFilter = filter;
    document.querySelectorAll("#monitor-filter-tabs .filter-btn")
      .forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _renderAll();
  };

  window.clearMonitorView = function () {
    _events = [];
    _groups.clear();
    _renderAll();
  };

  window.clearEmailFilter = function () {
    _emailFilter = null;
    document.getElementById("monitor-email-filter").style.display = "none";
    _renderAll();
  };

  // ── Load initial events ────────────────────────────────────────────────────
  async function _loadInitialEvents() {
    try {
      const res = await apiFetch("/api/monitor/events?limit=200");
      if (!res || !res.ok) return;
      const events = await res.json();
      events.forEach(_ingestEvent);
      _renderAll();
      _updateCount();
    } catch (e) {
      console.warn("Monitor: initial load failed", e);
    }
  }

  // ── SSE connection ─────────────────────────────────────────────────────────
  function _connectSSE() {
    const token = getToken();
    if (!token) return;

    if (_sseSource) { _sseSource.close(); }

    const url = `/api/monitor/events/stream?token=${encodeURIComponent(token)}`;
    _sseSource = new EventSource(url);

    _sseSource.onopen = () => {
      _setBadge(true);
    };

    _sseSource.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        const isNew = _ingestEvent(event);
        if (isNew) {
          _renderOrUpdateGroup(event.email_id);
          _updateCount();
          _flashNavDot();
        }
      } catch (_) {}
    };

    _sseSource.onerror = () => {
      _setBadge(false);
      setTimeout(_connectSSE, 5000);
      _sseSource.close();
    };
  }

  function _setBadge(live) {
    const badge = document.getElementById("monitor-connection-badge");
    if (!badge) return;
    badge.className = live ? "monitor-live-badge live" : "monitor-live-badge disconnected";
    badge.innerHTML = live
      ? '<span class="monitor-pulse-dot"></span> Live'
      : '⚠ Reconnecting…';
  }

  function _flashNavDot() {
    const dot = document.getElementById("monitor-live-dot");
    if (!dot) return;
    dot.classList.add("active");
    clearTimeout(dot._t);
    dot._t = setTimeout(() => dot.classList.remove("active"), 3000);
  }

  // ── Ingest event into state ────────────────────────────────────────────────
  function _ingestEvent(event) {
    // Dedup by id
    if (_events.find(e => e.id === event.id)) return false;

    _events.push(event);

    // Prune oldest if over limit
    if (_events.length > MAX_EVENTS) {
      const removed = _events.shift();
      // If group only has this event, remove the group too
      const grp = _groups.get(removed.email_id);
      if (grp) {
        grp.events = grp.events.filter(e => e.id !== removed.id);
        if (grp.events.length === 0) _groups.delete(removed.email_id);
      }
    }

    // Add to group
    const key = event.email_id || "__unknown__";
    if (!_groups.has(key)) {
      _groups.set(key, { emailId: key, events: [], collapsed: false });
    }
    _groups.get(key).events.push(event);
    return true;
  }

  // ── Render all groups ──────────────────────────────────────────────────────
  function _renderAll() {
    const container = document.getElementById("monitor-groups");
    if (!container) return;

    const filtered = _filteredGroups();

    if (filtered.length === 0) {
      container.innerHTML = `
        <div class="monitor-empty" id="monitor-empty-state">
          <div class="monitor-empty-icon">⚡</div>
          <p>Waiting for email processing events…</p>
          <p class="monitor-empty-sub">
            Events appear automatically when emails are processed.
          </p>
        </div>`;
      return;
    }

    container.innerHTML = "";
    // Render newest group first
    for (const grp of [...filtered].reverse()) {
      container.appendChild(_buildGroupEl(grp));
    }
  }

  // ── Render or update a single group (incremental update on new event) ──────
  function _renderOrUpdateGroup(emailId) {
    const key = emailId || "__unknown__";
    const grp = _groups.get(key);
    if (!grp) return;

    const container = document.getElementById("monitor-groups");
    if (!container) return;

    // Remove empty state if present
    const empty = document.getElementById("monitor-empty-state");
    if (empty) empty.remove();

    // Check if a DOM element for this group already exists
    const existing = document.getElementById(`monitor-group-${_domId(key)}`);
    if (existing) {
      // Patch: rebuild just the events list inside the group
      const evList = existing.querySelector(".monitor-ev-list");
      if (evList) {
        evList.innerHTML = _buildEventsHTML(grp);
        _updateGroupHeader(existing, grp);
      }
    } else {
      // New group — prepend it (newest first)
      const el = _buildGroupEl(grp);
      container.insertBefore(el, container.firstChild);
    }
  }

  // ── Build a group DOM element ──────────────────────────────────────────────
  function _buildGroupEl(grp) {
    const el  = document.createElement("div");
    el.id     = `monitor-group-${_domId(grp.emailId)}`;
    el.className = "monitor-group";

    const visibleEvents = _visibleEvents(grp);
    const groupStatus   = _groupStatus(grp);
    const firstEvent    = grp.events[0];
    const lastEvent     = grp.events[grp.events.length - 1];

    const subjectMeta   = firstEvent?.meta;
    const subject       = subjectMeta?.subject || "";
    const sender        = subjectMeta?.sender  || "";
    const shortId       = grp.emailId === "__unknown__"
      ? "Unknown"
      : grp.emailId.slice(0, 12) + "…";

    const totalMs = _groupDuration(grp);
    const durationStr = totalMs ? `${(totalMs / 1000).toFixed(1)}s` : "";
    const ts = firstEvent ? _fmtTime(firstEvent.timestamp) : "";

    el.innerHTML = `
      <div class="monitor-group-header" onclick="toggleMonitorGroup('${_domId(grp.emailId)}')">
        <span class="monitor-group-status-dot status-dot-${groupStatus}"></span>
        <div class="monitor-group-title">
          <span class="monitor-group-id" title="${_esc(grp.emailId)}">${_esc(shortId)}</span>
          ${subject ? `<span class="monitor-group-subject">${_esc(subject)}</span>` : ""}
          ${sender  ? `<span class="monitor-group-sender">${_esc(sender)}</span>`  : ""}
        </div>
        <div class="monitor-group-meta">
          ${durationStr ? `<span class="monitor-duration-badge">${durationStr}</span>` : ""}
          <span class="monitor-event-count-badge">${grp.events.length} events</span>
          <span class="monitor-group-ts">${ts}</span>
          <span class="monitor-group-chevron" id="monitor-chevron-${_domId(grp.emailId)}">
            ${grp.collapsed ? "▶" : "▼"}
          </span>
        </div>
      </div>
      <div class="monitor-ev-list ${grp.collapsed ? 'collapsed' : ''}"
           id="monitor-evlist-${_domId(grp.emailId)}">
        ${_buildEventsHTML(grp)}
      </div>`;
    return el;
  }

  function _buildEventsHTML(grp) {
    const visible = _visibleEvents(grp);
    if (visible.length === 0) {
      return `<div class="monitor-ev-empty">No events match current filter</div>`;
    }
    return visible.map(ev => _buildEventRow(ev)).join("");
  }

  function _buildEventRow(ev) {
    const m          = _meta(ev.event_type);
    const ts         = _fmtTime(ev.timestamp);
    const durStr     = ev.duration_ms ? `<span class="ev-duration">${ev.duration_ms.toFixed(0)}ms</span>` : "";
    const agentBadge = ev.agent_name
      ? `<span class="ev-agent-badge">${_esc(ev.agent_name)}</span>` : "";

    return `
      <div class="monitor-ev-row ${m.cls}">
        <span class="ev-icon">${m.icon}</span>
        <span class="ev-ts">${ts}</span>
        <span class="ev-message">${_esc(ev.message)}</span>
        ${agentBadge}
        ${durStr}
      </div>`;
  }

  function _updateGroupHeader(el, grp) {
    const groupStatus = _groupStatus(grp);
    const dot = el.querySelector(".monitor-group-status-dot");
    if (dot) dot.className = `monitor-group-status-dot status-dot-${groupStatus}`;
    const cntBadge = el.querySelector(".monitor-event-count-badge");
    if (cntBadge) cntBadge.textContent = `${grp.events.length} events`;
    const totalMs = _groupDuration(grp);
    const durBadge = el.querySelector(".monitor-duration-badge");
    if (durBadge && totalMs) durBadge.textContent = `${(totalMs / 1000).toFixed(1)}s`;
  }

  // ── Toggle group collapse ──────────────────────────────────────────────────
  window.toggleMonitorGroup = function (domId) {
    // Find the group key from the dom id
    for (const [key, grp] of _groups) {
      if (_domId(key) === domId) {
        grp.collapsed = !grp.collapsed;
        const list    = document.getElementById(`monitor-evlist-${domId}`);
        const chevron = document.getElementById(`monitor-chevron-${domId}`);
        if (list) list.classList.toggle("collapsed", grp.collapsed);
        if (chevron) chevron.textContent = grp.collapsed ? "▶" : "▼";
        break;
      }
    }
  };

  // ── Helpers ────────────────────────────────────────────────────────────────

  function _filteredGroups() {
    const groups = [..._groups.values()];
    if (_emailFilter) {
      return groups.filter(g => g.emailId === _emailFilter);
    }
    if (_statusFilter === "all") return groups;
    return groups.filter(g =>
      g.events.some(e => e.status === _statusFilter)
    );
  }

  function _visibleEvents(grp) {
    if (_statusFilter === "all") return grp.events;
    return grp.events.filter(e => e.status === _statusFilter);
  }

  function _groupStatus(grp) {
    const evs = grp.events;
    if (evs.some(e => e.status === "failure")) return "failure";
    if (evs.some(e => e.status === "running")) return "running";
    if (evs.some(e => e.event_type === "processing_completed")) return "success";
    return "info";
  }

  function _groupDuration(grp) {
    // Sum duration_ms of completed nodes
    return grp.events.reduce((acc, e) => acc + (e.duration_ms || 0), 0);
  }

  function _updateCount() {
    const el = document.getElementById("monitor-event-count");
    if (el) el.textContent = `${_events.length} events`;
  }

  function _domId(key) {
    // Make a string safe for use in a DOM id
    return key.replace(/[^a-zA-Z0-9]/g, "_");
  }

  function _fmtTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);   // ISO string carries +05:30 offset from the API
      // Force 12-hour IST rendering regardless of browser locale or system TZ
      const parts = new Intl.DateTimeFormat("en-US", {
        hour:     "2-digit",
        minute:   "2-digit",
        second:   "2-digit",
        hour12:   true,
        timeZone: "Asia/Kolkata",
      }).formatToParts(d);
      // parts: [{type:'hour',value:'10'},{type:'literal',value:':'}, ...]
      const get = (t) => parts.find(p => p.type === t)?.value ?? "00";
      const ampm = (parts.find(p => p.type === "dayPeriod")?.value ?? "AM").toUpperCase();
      return `${get("hour")}:${get("minute")}:${get("second")} ${ampm} IST`;
    } catch (_) { return iso; }
  }

  function _esc(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

})();
