/**
 * nova.js
 * -------
 * Nova Assistant — floating chat panel logic.
 *
 * Features:
 *  - Floating toggle button (bottom-right)
 *  - Animated open/close panel
 *  - Streaming-style typing indicator while waiting for response
 *  - Conversation history rendered on open
 *  - Suggested prompt chips
 *  - Keyboard shortcut: Ctrl+/ or Cmd+/ to toggle
 *  - Context-aware: if user is viewing an email detail, email_id is passed
 *  - Clear chat button
 *  - Auto-scroll to latest message
 *  - Enter to send, Shift+Enter for newline
 */

(function () {
  "use strict";

  // ── Session ID — unique per browser tab ────────────────────────────────────
  const SESSION_ID = "session-" + Math.random().toString(36).slice(2, 9);

  // ── Suggested prompts ──────────────────────────────────────────────────────
  const SUGGESTIONS = [
    "Summarize today's activity",
    "Show high urgency emails",
    "Which client has the most tickets?",
    "How many spam emails were blocked?",
    "Any agents failed recently?",
    "What's in the review queue?",
    "Show all meeting requests",
    "Which Jira tickets are unresolved?",
  ];

  // ── State ──────────────────────────────────────────────────────────────────
  let panelOpen    = false;
  let isLoading    = false;
  let contextEmailId = null;   // set when user is viewing an email detail

  // ── DOM refs (set in init) ─────────────────────────────────────────────────
  let $panel, $messages, $textarea, $sendBtn, $suggestions;

  // ── Init ───────────────────────────────────────────────────────────────────
  function init() {
    _injectHTML();
    _bindEvents();
    _loadHistory();
  }

  // ── Inject HTML into page ──────────────────────────────────────────────────
  function _injectHTML() {
    const toggleBtn = document.createElement("button");
    toggleBtn.id        = "nova-toggle";
    toggleBtn.title     = "Nova Assistant (Ctrl+/)";
    toggleBtn.innerHTML = `✦<span class="nova-badge" id="nova-unread-badge"></span>`;
    document.body.appendChild(toggleBtn);

    const panel = document.createElement("div");
    panel.id        = "nova-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Nova Assistant");
    panel.innerHTML = `
      <div class="nova-header">
        <div class="nova-avatar">✦</div>
        <div class="nova-header-info">
          <div class="nova-name">Nova Assistant</div>
          <div class="nova-status">
            <span class="nova-status-dot"></span>
            AI-powered · Connected to live data
          </div>
        </div>
        <div class="nova-header-actions">
          <button class="nova-icon-btn" id="nova-clear-btn" title="Clear conversation">🗑</button>
          <button class="nova-icon-btn" id="nova-close-btn" title="Close (Ctrl+/)">✕</button>
        </div>
      </div>

      <div class="nova-suggestions" id="nova-suggestions"></div>

      <div class="nova-messages" id="nova-messages">
        <div class="nova-welcome" id="nova-welcome">
          <div class="nova-welcome-icon">✦</div>
          <p>
            I'm <strong style="color:#a5b4fc">Nova</strong>, your AI operations assistant.<br>
            Ask me anything about your email pipeline, agents, clients, or Jira tickets.
          </p>
        </div>
      </div>

      <div class="nova-input-area">
        <div class="nova-input-row">
          <textarea
            id="nova-textarea"
            placeholder="Ask Nova anything…"
            rows="2"
            aria-label="Message to Nova"
          ></textarea>
          <button class="nova-send-btn" id="nova-send-btn" title="Send (Enter)" disabled>
            ➤
          </button>
        </div>
        <div class="nova-hint">Enter to send &nbsp;·&nbsp; Shift+Enter for new line</div>
      </div>
    `;
    document.body.appendChild(panel);

    $panel       = panel;
    $messages    = document.getElementById("nova-messages");
    $textarea    = document.getElementById("nova-textarea");
    $sendBtn     = document.getElementById("nova-send-btn");
    $suggestions = document.getElementById("nova-suggestions");

    _renderSuggestions();
  }

  // ── Render suggestion chips ────────────────────────────────────────────────
  function _renderSuggestions() {
    $suggestions.innerHTML = SUGGESTIONS.map(s =>
      `<button class="nova-chip" onclick="novaAskSuggestion(this)">${_esc(s)}</button>`
    ).join("");
  }

  // ── Bind events ───────────────────────────────────────────────────────────
  function _bindEvents() {
    document.getElementById("nova-toggle").addEventListener("click", togglePanel);
    document.getElementById("nova-close-btn").addEventListener("click", closePanel);
    document.getElementById("nova-clear-btn").addEventListener("click", clearChat);

    $textarea.addEventListener("input",   _onTextareaInput);
    $textarea.addEventListener("keydown", _onTextareaKeydown);
    $sendBtn.addEventListener("click",    sendMessage);

    // Keyboard shortcut: Ctrl+/ or Cmd+/
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        e.preventDefault();
        togglePanel();
      }
    });
  }

  function _onTextareaInput() {
    // Auto-resize
    $textarea.style.height = "auto";
    $textarea.style.height = Math.min($textarea.scrollHeight, 120) + "px";
    // Enable/disable send
    $sendBtn.disabled = !$textarea.value.trim() || isLoading;
  }

  function _onTextareaKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!$sendBtn.disabled) sendMessage();
    }
  }

  // ── Toggle / open / close ──────────────────────────────────────────────────
  function togglePanel() {
    panelOpen ? closePanel() : openPanel();
  }

  function openPanel() {
    panelOpen = true;
    $panel.classList.add("open");
    _hideUnreadBadge();
    requestAnimationFrame(() => $textarea.focus());
  }

  function closePanel() {
    panelOpen = false;
    $panel.classList.remove("open");
  }

  // ── Unread badge ───────────────────────────────────────────────────────────
  function _showUnreadBadge() {
    const badge = document.getElementById("nova-unread-badge");
    if (badge && !panelOpen) {
      badge.style.display = "block";
      badge.textContent = "●";
    }
  }
  function _hideUnreadBadge() {
    const badge = document.getElementById("nova-unread-badge");
    if (badge) badge.style.display = "none";
  }

  // ── Load history from server ───────────────────────────────────────────────
  async function _loadHistory() {
    try {
      const res = await apiFetch(`/api/nova/history?session_id=${SESSION_ID}`);
      if (!res || !res.ok) return;
      const data = await res.json();
      if (data.history && data.history.length > 0) {
        _hideWelcome();
        data.history.forEach(msg => _appendMessage(msg.role, msg.content, false));
        _scrollToBottom();
      }
    } catch (_) {
      // silently ignore — history is optional
    }
  }

  // ── Clear chat ─────────────────────────────────────────────────────────────
  async function clearChat() {
    if (!confirm("Clear Nova conversation history?")) return;
    try {
      await apiFetch(`/api/nova/history?session_id=${SESSION_ID}`, { method: "DELETE" });
    } catch (_) {}
    // Clear UI
    $messages.innerHTML = `
      <div class="nova-welcome" id="nova-welcome">
        <div class="nova-welcome-icon">✦</div>
        <p>
          I'm <strong style="color:#a5b4fc">Nova</strong>, your AI operations assistant.<br>
          Ask me anything about your email pipeline, agents, clients, or Jira tickets.
        </p>
      </div>`;
    _renderSuggestions();
    $suggestions.style.display = "";
  }

  // ── Send message ───────────────────────────────────────────────────────────
  async function sendMessage() {
    const text = $textarea.value.trim();
    if (!text || isLoading) return;

    _hideWelcome();
    _hideSuggestions();
    _appendMessage("user", text);
    $textarea.value       = "";
    $textarea.style.height = "auto";
    $sendBtn.disabled     = true;
    isLoading             = true;

    const typingId = _showTyping();

    try {
      const payload = {
        message:    text,
        session_id: SESSION_ID,
        email_id:   contextEmailId || null,
      };

      const res = await apiFetch("/api/nova/chat", {
        method:  "POST",
        body:    payload,
      });

      _removeTyping(typingId);

      if (!res || !res.ok) {
        const err = res ? await res.json().catch(() => ({})) : {};
        _appendMessage("assistant", `⚠ Error: ${err.detail || "Request failed"}`);
      } else {
        const data = await res.json();
        _appendMessage("assistant", data.answer);
        if (!panelOpen) _showUnreadBadge();
      }
    } catch (e) {
      _removeTyping(typingId);
      _appendMessage("assistant", "⚠ Could not reach Nova. Please try again.");
    } finally {
      isLoading         = false;
      $sendBtn.disabled = !$textarea.value.trim();
      $textarea.focus();
    }
  }

  // ── Append a message bubble ────────────────────────────────────────────────
  function _appendMessage(role, content, scroll = true) {
    const time   = new Date().toLocaleTimeString([], { hour:"2-digit", minute:"2-digit", timeZone: "Asia/Kolkata" });
    const isUser = role === "user";

    const msgEl = document.createElement("div");
    msgEl.className = `nova-msg ${isUser ? "user" : "assistant"}`;

    const avatar   = isUser ? "👤" : "✦";
    const escaped  = _formatContent(content);

    msgEl.innerHTML = `
      <div class="nova-msg-avatar">${avatar}</div>
      <div style="display:flex;flex-direction:column;${isUser ? 'align-items:flex-end' : ''}">
        <div class="nova-msg-bubble">${escaped}</div>
        <div class="nova-msg-time">${time}</div>
      </div>`;

    $messages.appendChild(msgEl);
    if (scroll) _scrollToBottom();
    return msgEl;
  }

  // ── Format Nova's response: render markdown-like bullet points ────────────
  function _formatContent(text) {
    if (!text) return "";
    // Escape HTML first
    let t = _esc(text);
    // Bold: **text** or *text*
    t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/\*(.+?)\*/g, "<em>$1</em>");
    // Bullet points: lines starting with - or •
    const lines = t.split("\n");
    const out   = [];
    let inList   = false;
    for (const line of lines) {
      const trimmed = line.trim();
      if (/^[-•]\s+/.test(trimmed)) {
        if (!inList) { out.push("<ul style='padding-left:14px;margin:4px 0'>"); inList = true; }
        out.push(`<li style='margin:2px 0'>${trimmed.replace(/^[-•]\s+/, "")}</li>`);
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        if (trimmed === "") {
          out.push("<br>");
        } else {
          out.push(`<span>${trimmed}</span><br>`);
        }
      }
    }
    if (inList) out.push("</ul>");
    return out.join("");
  }

  // ── Typing indicator ───────────────────────────────────────────────────────
  function _showTyping() {
    const id    = "nova-typing-" + Date.now();
    const el    = document.createElement("div");
    el.id        = id;
    el.className = "nova-msg assistant nova-typing";
    el.innerHTML = `
      <div class="nova-msg-avatar">✦</div>
      <div style="display:flex;flex-direction:column;align-items:flex-start">
        <div class="nova-msg-bubble">
          <div style="display:flex;align-items:center;gap:8px">
            <div style="font-style:italic;color:#6b7280;font-size:13px">Nova is thinking...</div>
            <div class="nova-dots"><span></span><span></span><span></span></div>
          </div>
        </div>
        <div class="nova-msg-time">&nbsp;</div>
      </div>`;
    $messages.appendChild(el);
    _scrollToBottom();
    return id;
  }

  function _removeTyping(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function _scrollToBottom() {
    requestAnimationFrame(() => {
      $messages.scrollTop = $messages.scrollHeight;
    });
  }

  function _hideWelcome() {
    const w = document.getElementById("nova-welcome");
    if (w) w.style.display = "none";
  }

  function _hideSuggestions() {
    if ($suggestions) $suggestions.style.display = "none";
  }

  function _esc(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Public API (called from app.js / inline handlers) ─────────────────────

  window.novaAskSuggestion = function (btn) {
    const text = btn.textContent.trim();
    if (!text) return;
    openPanel();
    $textarea.value = text;
    _onTextareaInput();
    sendMessage();
  };

  /** Called by app.js when user opens an email detail view */
  window.novaSetEmailContext = function (emailId) {
    contextEmailId = emailId || null;
  };

  /** Called by app.js when user closes email detail */
  window.novaClearEmailContext = function () {
    contextEmailId = null;
  };

  /** Expose toggle for external use */
  window.novaToggle = togglePanel;

  // ── Bootstrap ─────────────────────────────────────────────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
