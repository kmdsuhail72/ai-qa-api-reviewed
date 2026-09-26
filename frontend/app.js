const state = { token: localStorage.getItem("orbit_token"), user: null, questions: 0, cacheHits: 0 };
const $ = (selector) => document.querySelector(selector);

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    logout("Your session expired. Please sign in again.");
    throw new Error("Unauthorized");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}

function renderSession() {
  const loggedIn = Boolean(state.token && state.user);
  $("#login-panel").classList.toggle("hidden", loggedIn);
  $("#chat-layout").classList.toggle("hidden", !loggedIn);
  $("#user-area").classList.toggle("hidden", !loggedIn);
  if (loggedIn) {
    $("#username-label").textContent = state.user.username;
    $("#role-label").textContent = state.user.role.replace("_", " ");
    $("#user-avatar").textContent = state.user.username[0].toUpperCase();
    $("#connection-label").textContent = "Connected";
  }
}

async function signIn(username, password) {
  const result = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
  state.token = result.access_token;
  localStorage.setItem("orbit_token", state.token);
  state.user = await api("/auth/me");
  renderSession();
  await loadHistory();
}

function logout(message) {
  state.token = null;
  state.user = null;
  localStorage.removeItem("orbit_token");
  renderSession();
  if (message) showToast(message);
}

function addMessage(role, text, meta = "") {
  const conversation = $("#conversation");
  $("#empty-state")?.remove();
  const message = document.createElement("div");
  message.className = `message ${role}`;
  message.innerHTML = `<div class="message-bubble"></div><div class="message-meta"></div>`;
  message.querySelector(".message-bubble").textContent = text;
  message.querySelector(".message-meta").textContent = meta;
  conversation.appendChild(message);
  conversation.scrollTop = conversation.scrollHeight;
}

async function ask(question) {
  const cleanQuestion = question.trim();
  if (!cleanQuestion) return;
  $("#question").value = "";
  addMessage("user", cleanQuestion);
  const pending = document.createElement("div");
  pending.className = "message assistant";
  pending.innerHTML = '<div class="message-bubble">Thinking<span class="loading-dots">...</span></div>';
  $("#conversation").appendChild(pending);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  try {
    const result = await api("/chat", { method: "POST", body: JSON.stringify({ question: cleanQuestion }) });
    pending.remove();
    const meta = `${result.model} · ${result.latency_ms}ms${result.cached ? " · cached" : ""}${result.fallback_used ? " · fallback" : ""}`;
    addMessage("assistant", result.answer, meta);
    state.questions += 1;
    if (result.cached) state.cacheHits += 1;
    $("#question-count").textContent = state.questions;
    $("#cache-count").textContent = state.cacheHits;
    $("#latency-value").textContent = `${Math.round(result.latency_ms)}ms`;
    await loadHistory();
  } catch (error) {
    pending.remove();
    addMessage("assistant", error.message, "request failed");
  }
}

async function loadHistory() {
  try {
    const history = await api("/chat/history?limit=6");
    const list = $("#history-list");
    list.innerHTML = "";
    if (!history.length) {
      list.innerHTML = '<p class="muted history-empty">Your recent questions will appear here.</p>';
      return;
    }
    history.forEach((item) => {
      const row = document.createElement("div");
      row.className = "history-item";
      const button = document.createElement("button");
      button.textContent = item.question;
      button.title = item.question;
      button.onclick = () => ask(item.question);
      const time = document.createElement("time");
      time.textContent = `${item.model} · ${Math.round(item.latency_ms)}ms`;
      row.append(button, time);
      list.appendChild(row);
    });
  } catch (error) {
    if (state.token) showToast(error.message);
  }
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#login-error");
  error.classList.add("hidden");
  try {
    await signIn($("#username").value, $("#password").value);
  } catch (exception) {
    error.textContent = exception.message;
    error.classList.remove("hidden");
  }
});
$("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); ask($("#question").value); });
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#chat-form").requestSubmit(); }
});
document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => ask(button.dataset.question)));
$("#logout-button").addEventListener("click", () => logout());
$("#refresh-history").addEventListener("click", loadHistory);

(async () => {
  if (!state.token) return;
  try {
    state.user = await api("/auth/me");
    renderSession();
    await loadHistory();
  } catch (_) {
    logout();
  }
})();
