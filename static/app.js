// Progressive enhancement, loaded as an external file so the page runs under a
// strict CSP (script-src 'self', no inline handlers, no eval). The chat sends via
// fetch + Server-Sent Events so the tutor reply streams in token by token.

function scrollMessages() {
  const box = document.getElementById("messages");
  if (box) box.scrollTop = box.scrollHeight;
}

// New-turn scrolling: instead of chasing the bottom as the reply streams, we pin the
// student's question to the TOP of the message box so the answer fills in below it and
// the reader starts at the beginning. A temporary spacer guarantees there's a viewport
// of room beneath the new turn so the question can actually reach the top right away.
let chatSpacer = null;   // the temporary spacer element (one turn at a time)
let chatPinTop = 0;      // scrollTop we set when pinning — used to detect a manual scroll

function pinToTop(sb) {
  const box = document.getElementById("messages");
  if (!box || !sb) return;
  chatSpacer = document.createElement("div");
  chatSpacer.className = "chat-spacer";
  chatSpacer.style.height = box.clientHeight + "px";
  box.appendChild(chatSpacer);
  // Align the question bubble's top with the box's top.
  box.scrollTop += sb.getBoundingClientRect().top - box.getBoundingClientRect().top;
  chatPinTop = box.scrollTop;
}

function unpin(sb) {
  const box = document.getElementById("messages");
  // Did the student scroll during the answer? (We never scroll mid-stream, so any
  // change to scrollTop is theirs.) Check BEFORE removing the spacer, which can clamp it.
  const held = box ? Math.abs(box.scrollTop - chatPinTop) <= 4 : false;
  if (chatSpacer) { chatSpacer.remove(); chatSpacer = null; }
  // Re-pin once after the spacer is gone + the markdown reflow, unless they scrolled away.
  if (box && sb && held) {
    box.scrollTop += sb.getBoundingClientRect().top - box.getBoundingClientRect().top;
  }
}

// Guards against a second turn starting while one is still streaming (e.g. a fast
// second Enter), which would otherwise show two "thinking" bubbles at once.
let chatInFlight = false;

// How the reply is shown WHILE it streams:
//   false (default) — keep the animated "thinking" dots until the reply is complete,
//     then swap in the fully formatted (markdown + math + figures) answer. Avoids the
//     messy half-rendered markdown / partial SVG that raw streaming shows.
//   true  — stream the raw reply text token-by-token (the original behavior), then swap
//     to the formatted version on completion.
// The raw-streaming code below is kept and gated on this flag so flipping it back is a
// one-line change. (Either way the full reply still arrives quickly and renders on done.)
const SHOW_RAW_STREAM = false;

// Typeset not-yet-rendered math with KaTeX. The server emits math as
// <span class="math inline">LaTeX</span> / <div class="math block">LaTeX</div>.
function renderMath(root) {
  if (typeof katex === "undefined") return;
  (root || document).querySelectorAll(".math:not([data-rendered])").forEach(function (el) {
    try {
      katex.render(el.textContent, el, {
        displayMode: el.classList.contains("block"),
        throwOnError: false,
      });
    } catch (e) { /* leave the raw LaTeX in place on failure */ }
    el.setAttribute("data-rendered", "1");
  });
}

// --- chat bubbles -------------------------------------------------------------

function makeBubble(role, who) {
  const bubble = document.createElement("div");
  bubble.className = "bubble " + role;
  const w = document.createElement("div");
  w.className = "who";
  w.textContent = who;
  const text = document.createElement("div");
  text.className = "text";
  bubble.appendChild(w);
  bubble.appendChild(text);
  return { bubble: bubble, text: text };
}

function appendStudentBubble(messageText) {
  const box = document.getElementById("messages");
  if (!box) return null;
  const b = makeBubble("student", "You");
  b.text.textContent = messageText; // textContent → never interpreted as HTML
  box.appendChild(b.bubble);
  return b.bubble;
}

// The three blinking dots used by the streaming indicators.
function thinkingDots() {
  const dots = document.createElement("span");
  dots.className = "thinking";
  dots.appendChild(document.createElement("span"));
  dots.appendChild(document.createElement("span"));
  dots.appendChild(document.createElement("span"));
  return dots;
}

// A tutor bubble with an animated "thinking" indicator, shown while the gate runs
// and before the first token arrives.
function appendThinkingBubble() {
  const box = document.getElementById("messages");
  const b = makeBubble("tutor", "Tutor");
  b.bubble.setAttribute("data-streaming", "1");
  b.text.appendChild(thinkingDots());
  if (box) box.appendChild(b.bubble);
  return b;
}

// Number of words in the reply text so far (shown live while streaming).
function wordCount(text) {
  const n = (text || "").trim().split(/\s+/).filter(Boolean).length;
  return n + (n === 1 ? " word" : " words");
}

// A muted inline "<label> ⋯" streaming indicator (label text + blinking dots).
function showIndicator(ph, labelText) {
  ph.text.textContent = "";
  const wrap = document.createElement("span");
  wrap.className = "writing";
  wrap.appendChild(document.createTextNode(labelText));
  wrap.appendChild(thinkingDots());
  ph.text.appendChild(wrap);
  return wrap;
}

// Tool activity (tools-enabled subjects): "Calculating (python) ⋯" (or "Working ⋯").
function showStatus(ph, tool) {
  showIndicator(ph, tool ? "Calculating (" + tool + ") " : "Working ");
}

// The "Writing answer · N words ⋯" indicator with a live word count. Returns the count
// element so the caller can update it as more text streams in.
function showWriting(ph) {
  const wrap = showIndicator(ph, "Writing answer · ");
  const count = document.createElement("span");
  count.className = "writing-count";
  wrap.insertBefore(count, wrap.lastChild); // between the label and the dots
  wrap.insertBefore(document.createTextNode(" "), wrap.lastChild);
  return count;
}

// Turn the placeholder into a friendly error notice (network failure, etc.).
function failBubble(ph) {
  if (!ph || !ph.bubble) return;
  ph.bubble.className = "bubble notice";
  ph.bubble.removeAttribute("data-streaming");
  const who = ph.bubble.querySelector(".who");
  if (who) who.remove();
  ph.text.className = "text";
  ph.text.textContent =
    "Something went wrong and I couldn't finish that. Please try again — if it keeps happening, ask your parent for help.";
}

// --- SSE parsing --------------------------------------------------------------

function parseEvent(raw) {
  let event = "message";
  let data = "";
  raw.split("\n").forEach(function (line) {
    if (line.indexOf("event:") === 0) event = line.slice(6).trim();
    else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
  });
  let payload = {};
  try { payload = data ? JSON.parse(data) : {}; } catch (e) { payload = {}; }
  return { event: event, data: payload };
}

async function consumeStream(body, ph) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let started = false; // have we cleared the thinking dots for the first token?
  let writing = null;  // dots-mode "Writing answer …" count element, once answer streaming begins
  let rawText = "";    // accumulated raw reply text (deltas), for the live word count
  let outcome = "none"; // "done" (tutor replied) | "notice" (gate blocked / error)
  let debug = null;    // admin chat-test only: per-turn diagnostic record (else absent)
  while (true) {
    const r = await reader.read();
    if (r.done) break;
    buf += decoder.decode(r.value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (!raw.trim()) continue;
      const ev = parseEvent(raw);
      if (ev.event === "delta") {
        rawText += ev.data.text || ""; // always accumulate (drives the word count)
        if (SHOW_RAW_STREAM) {
          if (!started) { ph.text.textContent = ""; started = true; }
          ph.text.appendChild(document.createTextNode(ev.data.text || ""));
        } else {
          // Dots mode: show a "Writing answer · N words" indicator (livelier than bare
          // dots) and update the running count; the formatted answer lands on `done`.
          if (!writing) writing = showWriting(ph);
          writing.textContent = wordCount(rawText);
        }
      } else if (ev.event === "done") {
        outcome = "done";
        ph.text.innerHTML = ev.data.html || ""; // server-sanitized markdown
        ph.text.classList.add("md");
        ph.bubble.removeAttribute("data-streaming");
        renderMath(ph.text);
      } else if (ev.event === "notice") {
        outcome = "notice";
        const tmp = document.createElement("div");
        tmp.innerHTML = ev.data.html || ""; // server-rendered notice bubble
        const node = tmp.firstElementChild;
        if (node) { ph.bubble.replaceWith(node); renderMath(node); }
        else ph.bubble.remove();
      } else if (ev.event === "status") {
        // Tool activity (tools-enabled subjects). Raw-stream mode: show the caption in
        // place of the dots until the first answer token arrives (don't clobber streamed
        // text). Dots mode: show "Calculating (tool) ⋯" and reset `writing` so the next
        // answer delta rebuilds the "Writing answer" indicator (handles a model that
        // calls a tool part-way through writing).
        if (SHOW_RAW_STREAM) {
          if (!started) {
            ph.text.textContent = ev.data.tool
              ? "Calculating (" + ev.data.tool + ")…"
              : "Working…";
          }
        } else {
          writing = null;
          showStatus(ph, ev.data.tool);
        }
      } else if (ev.event === "debug") {
        debug = ev.data; // admin chat-test diagnostics; student stream never emits this
      }
      // No mid-stream autoscroll: the question stays pinned at the top (see pinToTop)
      // and the answer fills in below it.
    }
  }
  return { type: outcome, raw: rawText, debug: debug };
}

// Render one per-turn debug record into the chat-test debug panel (admin only). All
// text goes in via textContent — `debug` holds raw model/prompt strings (verdict
// reasons), never HTML. Returns silently if the page has no debug panel.
function appendDebugEntry(userText, debug) {
  const box = document.getElementById("debug-log");
  if (!box || !debug) return;
  const turn = box.querySelectorAll(".debug-entry").length + 1;
  const entry = document.createElement("div");
  entry.className = "debug-entry";

  const head = document.createElement("div");
  head.className = "debug-head";
  head.textContent = "Turn " + turn + " · " + (debug.verdict || "?") +
    " · " + (debug.latency_ms != null ? debug.latency_ms + " ms" : "");
  entry.appendChild(head);

  const lines = [];
  lines.push("message: " + userText);
  const gateBits = ["verdict=" + debug.verdict];
  if (debug.suggested_subject) gateBits.push("suggested=" + debug.suggested_subject);
  gateBits.push("gate_tokens=" + (debug.gate_tokens != null ? debug.gate_tokens : "?"));
  lines.push("gate: " + gateBits.join("  "));
  if (debug.gate_reason) lines.push("gate_reason: " + debug.gate_reason);
  lines.push("history_turns_sent: " + (debug.history_turns != null ? debug.history_turns : "?"));
  if (debug.tutor) {
    const t = debug.tutor;
    lines.push("tutor: model=" + (debug.model || "?") +
      "  finish_reason=" + (t.finish_reason != null ? t.finish_reason : "?") +
      "  partial=" + (t.partial === true ? "true" : "false"));
    lines.push("tokens: prompt=" + (t.prompt_tokens != null ? t.prompt_tokens : "?") +
      "  completion=" + (t.completion_tokens != null ? t.completion_tokens : "?") +
      "  total=" + (t.total_tokens != null ? t.total_tokens : "?"));
    // Prompt-cache reuse: read should be 0 on turn 1, non-zero on later turns (the
    // stable tools+system+history prefix served from cache). total is charged with
    // cached reads discounted, so it can be far below prompt on later turns.
    if (t.cache_read != null || t.cache_creation != null) {
      lines.push("cache: read=" + (t.cache_read != null ? t.cache_read : 0) +
        "  write=" + (t.cache_creation != null ? t.cache_creation : 0));
    }
    if (t.tool_rounds) {
      var calls = (t.tool_log || []).map(function (c) {
        return c.tool + (c.ok ? "✓" : (c.timeout ? "⏱" : "✗"));
      }).join("  ");
      lines.push("tools: rounds=" + t.tool_rounds + (calls ? "  [" + calls + "]" : ""));
    }
    if (debug.reply_chars != null) lines.push("reply_chars: " + debug.reply_chars);
  }

  const pre = document.createElement("pre");
  pre.textContent = lines.join("\n");
  entry.appendChild(pre);
  box.appendChild(entry);
  box.scrollTop = box.scrollHeight;
}

async function streamChat(form) {
  if (chatInFlight) return; // a turn is already streaming
  const ta = form.querySelector("textarea");
  const text = (ta ? ta.value : "").trim();
  if (!text) return;
  const url = form.getAttribute("data-stream");
  if (!url) { form.submit(); return; } // no streaming endpoint → native fallback

  // Serialize the form (message + subject_id) BEFORE clearing the textarea below.
  // Conversation history is not sent: the server rebuilds it from the persisted
  // thread (see pipeline._history_for_tutor; the admin chat test uses its own
  // persisted is_test thread the same way).
  const body = new URLSearchParams(new FormData(form));

  chatInFlight = true;
  const sb = appendStudentBubble(text);
  const ph = appendThinkingBubble();
  if (ta) { ta.value = ""; ta.focus(); }
  pinToTop(sb); // pin the question to the top; the answer streams in below it
  const btn = form.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
    // Session expired / not logged in: the server 303s to /login, which fetch
    // follows to an HTML page — send the browser there instead of erroring.
    if (resp.redirected && resp.url.indexOf("/login") >= 0) {
      window.location = resp.url;
      return;
    }
    const ctype = resp.headers.get("Content-Type") || "";
    if (!resp.ok || !resp.body || ctype.indexOf("text/event-stream") < 0) {
      throw new Error("unexpected response");
    }
    const result = await consumeStream(resp.body, ph);
    if (result.type === "none") {
      // Stream ended without a final answer OR a notice — it dropped/errored mid-way.
      // Convert the stuck "thinking" bubble into a friendly failure so the student
      // isn't left waiting forever.
      failBubble(ph);
    }
    // Admin chat test only: log the per-turn debug record (every turn, including
    // blocked/error ones). The student page emits no debug event and has no panel.
    appendDebugEntry(text, result.debug);
  } catch (e) {
    failBubble(ph);
  } finally {
    unpin(sb); // remove the spacer and keep the question pinned (unless they scrolled)
    chatInFlight = false;
    if (btn) btn.disabled = false;
  }
}

// --- events -------------------------------------------------------------------

// Enter sends the chat message; Shift+Enter inserts a newline.
document.body.addEventListener("keydown", function (e) {
  if (e.key !== "Enter" || e.shiftKey) return;
  const ta = e.target;
  if (!ta || ta.tagName !== "TEXTAREA" || !ta.form || ta.form.id !== "chat-form") return;
  e.preventDefault();
  if (typeof ta.form.requestSubmit === "function") ta.form.requestSubmit();
  else ta.form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
});

// Intercept the chat submit and stream instead of a full-page POST.
document.body.addEventListener("submit", function (e) {
  const form = e.target;
  if (!form || form.id !== "chat-form") return;
  e.preventDefault();
  streamChat(form);
});

// --- admin: subject preset prefill -------------------------------------------
// On the admin new/edit-subject form, picking a preset fills the relevant fields
// from the selected <option>'s data-* attributes; "Clear" empties them. Preset data
// rides on the option attributes (no inline script), so this stays CSP-safe.
function setupSubjectPresets() {
  const form = document.getElementById("subject-form");
  const select = document.getElementById("subject-preset");
  if (!form || !select) return;
  // [data-* key, field name] pairs the preset controls.
  const MAP = [["name", "name"], ["grade", "grade_level"], ["scope", "gate_scope"],
               ["style", "style"], ["policy", "answer_policy"]];
  function setField(name, value) {
    const el = form.querySelector('[name="' + name + '"]');
    if (el) el.value = value;
  }
  select.addEventListener("change", function () {
    const opt = select.options[select.selectedIndex];
    if (!opt || !opt.value) return;
    MAP.forEach(function (m) { setField(m[1], opt.dataset[m[0]] || ""); });
  });
  const clearBtn = document.getElementById("subject-clear");
  if (clearBtn) clearBtn.addEventListener("click", function () {
    MAP.forEach(function (m) { setField(m[1], ""); });
    select.value = "";
  });
}

// --- admin: live derived age on the student form --------------------------------
// Shows the age computed from the birth month + year next to the inputs and keeps it
// current as the parent edits, so they never maintain an age by hand. Month-only
// granularity (no day), matching the server (app/ages.py). CSP-safe (external JS).
function setupBirthdateAge() {
  document.querySelectorAll("[data-birthdate]").forEach(function (fs) {
    const monthEl = fs.querySelector("[data-birth-month]");
    const yearEl = fs.querySelector("[data-birth-year]");
    const out = fs.querySelector("[data-age-display]");
    if (!monthEl || !yearEl || !out) return;
    function update() {
      const m = parseInt(monthEl.value, 10);
      const y = parseInt(yearEl.value, 10);
      if (!m || !y || m < 1 || m > 12) { out.textContent = ""; return; }
      const now = new Date();
      let age = now.getFullYear() - y - (now.getMonth() + 1 < m ? 1 : 0);
      out.textContent = age >= 0 ? "Age " + age : "";
    }
    monthEl.addEventListener("change", update);
    yearEl.addEventListener("input", update);
    update();
  });
}

// "New conversation" on the admin chat-test page: clear the transcript and wipe the
// server-side test thread so the next message starts a fresh conversation.
function setupTestChat() {
  const form = document.getElementById("chat-form");
  const clearBtn = document.getElementById("test-clear");
  if (!form || !clearBtn) return;
  clearBtn.addEventListener("click", function () {
    const box = document.getElementById("messages");
    if (box) box.innerHTML = "";
    const dbg = document.getElementById("debug-log");
    if (dbg) dbg.innerHTML = ""; // drop the per-turn debug records too
    // Also wipe the persisted server-side test thread so the next turn starts fresh.
    const clearUrl = form.getAttribute("data-clear");
    if (clearUrl) {
      fetch(clearUrl, { method: "POST", headers: { "X-Requested-With": "fetch" } })
        .catch(function () { /* best-effort; the DOM is already reset */ });
    }
    const ta = form.querySelector("textarea");
    if (ta) ta.focus();
  });
}

// Initial load: typeset existing history, scroll to the latest message, and wire up
// the admin subject-preset menu and chat-test controls if they're on the page.
window.addEventListener("DOMContentLoaded", function () {
  renderMath(document);
  scrollMessages();
  setupSubjectPresets();
  setupTestChat();
  setupBirthdateAge();
});
