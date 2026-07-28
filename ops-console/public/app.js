const API = "";

function token() {
  return sessionStorage.getItem("orch_founder_token") || "";
}

async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`${API}${path}`, { ...options, headers });
  const text = await res.text();
  try {
    return { ok: res.ok, status: res.status, body: JSON.parse(text) };
  } catch {
    return { ok: res.ok, status: res.status, body: text };
  }
}

function setBadge(status) {
  const el = document.getElementById("status-badge");
  el.textContent = status;
  el.className = status === "ok" ? "ok" : status === "degraded" ? "warn" : "bad";
}

function renderGates(gates) {
  const ul = document.getElementById("gates-list");
  ul.innerHTML = "";
  for (const gate of gates || []) {
    const li = document.createElement("li");
    const id = gate.gate_id || gate.id || "unknown";
    const ref = gate.evidence_ref ? ` — evidence: ${gate.evidence_ref}` : "";
    li.textContent = `${id} (${gate.status || "open"})${ref}`;
    ul.appendChild(li);
  }
}

async function loadSummary() {
  try {
    const res = await fetch(`${API}/ops/summary/`);
    const data = await res.json();
    setBadge(data.status || "unknown");
    document.getElementById("stack-json").textContent = JSON.stringify(
      { stack_health: data.stack_health, credit_envelope: data.credit_envelope },
      null,
      2
    );
    document.getElementById("ladder-json").textContent = JSON.stringify(
      data.verification_ladder || {},
      null,
      2
    );
    document.getElementById("hierarchy-json").textContent = JSON.stringify(
      data.hierarchy || {},
      null,
      2
    );
    document.getElementById("delegations-json").textContent = JSON.stringify(
      data.delegations || {},
      null,
      2
    );
    document.getElementById("queues-json").textContent = JSON.stringify(
      { queues: data.queues, recent_work: data.recent_work, schedules: data.schedules },
      null,
      2
    );
    document.getElementById("findings-json").textContent = JSON.stringify(
      data.findings || {},
      null,
      2
    );
    document.getElementById("audit-json").textContent = JSON.stringify(
      data.recent_audit || [],
      null,
      2
    );
    document.getElementById("settings-json").textContent = JSON.stringify(
      data.settings || {},
      null,
      2
    );
    renderGates(data.open_gates);
  } catch (err) {
    setBadge("error");
    document.getElementById("stack-json").textContent = String(err);
  }
}

function setupTabs() {
  const tabs = document.querySelectorAll(".tabs button");
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabs.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
    });
  });
}

function setupToken() {
  const input = document.getElementById("founder-token");
  const saved = sessionStorage.getItem("orch_founder_token");
  if (saved) input.value = saved;
  document.getElementById("save-token").addEventListener("click", () => {
    sessionStorage.setItem("orch_founder_token", input.value.trim());
  });
}

function setupActions() {
  const form = document.getElementById("action-form");
  const out = document.getElementById("action-result");
  form.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      if (action === "recover") {
        const res = await apiFetch("/api/v1/runtime/recover", { method: "POST" });
        out.textContent = JSON.stringify(res.body, null, 2);
        return;
      }
      const runId = new FormData(form).get("run_id");
      if (!runId) {
        out.textContent = "run_id required";
        return;
      }
      const res = await apiFetch(`/api/v1/runtime/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: runId }),
      });
      out.textContent = JSON.stringify(res.body, null, 2);
    });
  });
}

setupTabs();
setupToken();
setupActions();
loadSummary();
setInterval(loadSummary, 30000);
