const jobList = document.getElementById("jobList");
const whenWrap = document.getElementById("whenWrap");
const tryOut = document.getElementById("tryOut");
const tryBtn = document.getElementById("tryBtn");
const editModal = document.getElementById("editModal");
const installModal = document.getElementById("installModal");
const editTitle = document.getElementById("editTitle");

const SCHEDULE = {
  save: "지금은 저장만",
  now: "지금은 저장만",
  daily_1700: "매일 오후 5시",
  daily_0900: "매일 아침 9시",
  weekdays_1700: "평일 오후 5시",
  hourly: "1시간마다",
  every_5m: "5분마다",
  once_1m: "1분 뒤 한 번",
  custom: "내가 정한 시각",
};

const FORM = {
  effort: [
    { id: "low", name: "짧게", selected: false },
    { id: "medium", name: "보통", selected: true },
    { id: "high", name: "오래", selected: false },
  ],
  permission: [
    { id: "read", name: "읽기만 · 조회" },
    { id: "workspace", name: "이 앱이 있는 폴더만", selected: true },
    { id: "machine", name: "이 컴퓨터 전체" },
  ],
  loops: [
    { id: "1", name: "1회 · 글만", selected: true },
    { id: "4", name: "4회" },
    { id: "12", name: "12회" },
    { id: "32", name: "32회" },
  ],
  preset: [
    { id: "save", name: "지금은 저장만", selected: true },
    { id: "daily_1700", name: "매일 오후 5시" },
    { id: "daily_0900", name: "매일 아침 9시" },
    { id: "weekdays_1700", name: "평일 오후 5시" },
    { id: "hourly", name: "1시간마다" },
    { id: "once_1m", name: "1분 뒤 한 번" },
    { id: "custom", name: "내가 정하기" },
  ],
  repeat: [
    { id: "daily", name: "매일", selected: true },
    { id: "weekdays", name: "평일" },
    { id: "weekend", name: "주말" },
    { id: "hourly", name: "매시 그 분" },
  ],
  alert: [
    { id: "always", name: "넣고", selected: true },
    { id: "fail", name: "실패할 때만" },
    { id: "ok", name: "성공할 때만" },
    { id: "off", name: "안 함" },
  ],
};

let editingId = null;

function mountFormDrops() {
  DD.mount("effortDrop", { label: "생각 깊이", items: FORM.effort });
  DD.mount("permissionDrop", { label: "권한", items: FORM.permission });
  DD.mount("loopsDrop", { label: "도구 반복", items: FORM.loops });
  DD.mount("presetDrop", {
    label: "언제",
    items: FORM.preset,
    onChange: (id) => {
      whenWrap.hidden = id !== "custom";
    },
  });
  DD.mount("repeatDrop", { label: "반복", items: FORM.repeat });
  DD.mount("alertDrop", { label: "알림", items: FORM.alert });
}

document.getElementById("openInstall").addEventListener("click", async () => {
  installModal.hidden = false;
  if (typeof load === "function") await load();
});
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.close === "installModal" && installModal.dataset.lock === "1") return;
    document.getElementById(btn.dataset.close).hidden = true;
  });
});
[editModal, installModal].forEach((modal) => {
  modal.addEventListener("click", (ev) => {
    if (ev.target !== modal) return;
    if (modal === installModal && installModal.dataset.lock === "1") return;
    modal.hidden = true;
  });
});

document.getElementById("addBtn").addEventListener("click", () => openEdit(null));

async function reloadModels() {
  const status = await fetch("/api/status").then((r) => r.json());
  const names = (status.ollama.models || []).map((m) => m.name);
  const current = DD.value("modelDrop");
  const pick = names.includes(current) ? current : names.find((n) => n.includes("12b")) || names[0] || "";
  const items = names.length
    ? names.map((n) => ({ id: n, name: n, selected: n === pick }))
    : [{ id: "", name: "모델 없음 · 설치에서 받기", selected: true, disabled: true }];
  DD.mount("modelDrop", {
    label: "모델",
    items,
    onChange: (id) => setEffortForModel(id),
  });
  if (pick) setEffortForModel(pick);
}

window.reloadModels = reloadModels;

function isReady(status) {
  return ((status && status.ollama && status.ollama.models) || []).length > 0;
}

async function applyGate() {
  const status = await fetch("/api/status").then((r) => r.json());
  const ready = isReady(status);
  window.installReady = ready;
  document.getElementById("appMain").hidden = !ready;
  const closeBtn = document.getElementById("installClose");
  if (closeBtn) closeBtn.hidden = !ready;
  installModal.dataset.lock = ready ? "" : "1";
  if (!ready) {
    installModal.hidden = false;
    if (typeof load === "function") await load();
  } else {
    await reloadModels();
  }
  return ready;
}

window.onInstallDone = async function (ev) {
  if (ev && ev.cancelled) {
    if (typeof hideProgress === "function") hideProgress();
    return;
  }
  if (typeof hideProgress === "function") hideProgress();
  const ready = await applyGate();
  if (ready) {
    installModal.hidden = true;
    await refresh();
    return;
  }
  if (ev && ev.ok === false) return;
  log("모델을 하나 받으세요.");
};

async function boot() {
  mountFormDrops();
  const ready = await applyGate();
  if (ready) await refresh();
}

function setEffortForModel(name) {
  const n = name || "";
  let id = "low";
  if (/(27b|26b|31b|32b|35b)/.test(n)) id = "high";
  else if (/(12b|14b|9b|e4b)/.test(n)) id = "medium";
  DD.setValue("effortDrop", id);
}

async function refresh() {
  const jobs = await fetch("/api/jobs").then((r) => r.json());
  if (!jobs.jobs.length) {
    jobList.innerHTML = `<p class="legend">아직 없습니다. 추가로 넣으세요.</p>`;
    return;
  }
  jobList.innerHTML = jobs.jobs.map(renderJob).join("");
}

function scheduleLabel(j) {
  if (j.preset && SCHEDULE[j.preset]) return SCHEDULE[j.preset];
  if (j.enabled && j.cron) return "예약됨";
  return "지금은 저장만";
}

function renderJob(j) {
  const last = j.last_run
    ? j.last_run.ok
      ? `<span class="ok">성공 ${j.last_run.seconds}s</span>`
      : `<span class="bad">실패 ${escapeHtml((j.last_run.error || "").slice(0, 80))}</span>`
    : "";
  const knobs = [j.effort, j.permission].filter(Boolean).join(" · ");
  return `<article class="job" data-id="${j.id}">
    <b>${escapeHtml(j.title)}</b>
    <div class="meta">${escapeHtml(j.model)} · ${escapeHtml(scheduleLabel(j))}${knobs ? " · " + knobs : ""} ${last}</div>
    <div class="row">
      <button class="gds-button gds-button--small" data-run="${j.id}">지금 실행</button>
      <button class="gds-button gds-button--secondary gds-button--small" data-edit="${j.id}">수정</button>
      <button class="gds-button gds-button--secondary gds-button--small" data-del="${j.id}">삭제</button>
    </div>
  </article>`;
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formPayload(extra) {
  return {
    title: document.getElementById("title").value,
    prompt: document.getElementById("prompt").value,
    model: DD.value("modelDrop"),
    preset: DD.value("presetDrop"),
    repeat: DD.value("repeatDrop"),
    time: document.getElementById("whenTime").value,
    alert: DD.value("alertDrop"),
    effort: DD.value("effortDrop"),
    permission: DD.value("permissionDrop"),
    max_loops: Number(DD.value("loopsDrop") || 1),
    ...(extra || {}),
  };
}

async function post(path, body, method) {
  const res = await fetch(path, {
    method: method || "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "실패");
  return data;
}

function alertValue(job) {
  const raw = job && job.alert;
  if (raw === false || raw === "off") return "off";
  if (raw === "fail" || raw === "ok" || raw === "always") return raw;
  return "always";
}

function openEdit(job) {
  editingId = job ? job.id : null;
  editTitle.textContent = job ? "수정" : "추가";
  document.getElementById("title").value = job ? job.title : "";
  document.getElementById("prompt").value = job ? job.prompt : "";
  if (job && job.model) DD.setValue("modelDrop", job.model);
  DD.setValue("effortDrop", (job && job.effort) || "medium");
  DD.setValue("permissionDrop", (job && job.permission) || "workspace");
  DD.setValue("loopsDrop", String((job && job.max_loops) || 1));
  const preset = job && job.preset === "now" ? "save" : (job && job.preset) || "save";
  DD.setValue("presetDrop", preset);
  DD.setValue("repeatDrop", (job && job.repeat) || "daily");
  document.getElementById("whenTime").value = (job && job.time) || "17:00";
  whenWrap.hidden = preset !== "custom";
  DD.setValue("alertDrop", alertValue(job));
  tryOut.textContent = "";
  editModal.hidden = false;
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  try {
    const payload = formPayload();
    if (editingId) await post(`/api/jobs/${editingId}`, payload, "PATCH");
    else await post("/api/jobs", payload);
    editModal.hidden = true;
    await refresh();
  } catch (err) {
    tryOut.textContent = err.message;
  }
});

tryBtn.addEventListener("click", async () => {
  const prompt = document.getElementById("prompt").value;
  if (!prompt.trim()) {
    tryOut.textContent = "시킬 일을 적으세요.";
    return;
  }
  tryBtn.disabled = true;
  tryOut.textContent = "실행 중…";
  try {
    showTry(await post("/api/jobs/try", formPayload({ prompt })));
  } catch (err) {
    tryOut.textContent = String(err.message || err);
  } finally {
    tryBtn.disabled = false;
  }
});

function showTry(data) {
  const head = data.ok ? `성공 ${data.seconds}s · ${data.model}` : `실패 ${data.seconds || 0}s`;
  const body = data.ok ? data.output : data.error;
  tryOut.textContent = head + "\n\n" + (body || "");
}

jobList.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  if (btn.dataset.run) {
    btn.disabled = true;
    try {
      await post(`/api/jobs/${btn.dataset.run}/run`);
      await refresh();
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (btn.dataset.edit) {
    const jobs = await fetch("/api/jobs").then((r) => r.json());
    const job = jobs.jobs.find((j) => j.id === btn.dataset.edit);
    if (job) openEdit(job);
    return;
  }
  if (btn.dataset.del) {
    await fetch(`/api/jobs/${btn.dataset.del}`, { method: "DELETE" });
    await refresh();
  }
});

boot().catch((err) => {
  jobList.textContent = String(err);
});
setInterval(() => {
  if (!document.getElementById("appMain").hidden) refresh();
}, 8000);
