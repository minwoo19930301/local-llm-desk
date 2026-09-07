const installBtn = document.getElementById("installBtn");
const modelBtn = document.getElementById("modelBtn");
const backBtn = document.getElementById("backBtn");
const progressEl = document.getElementById("progress");
const progressBar = document.getElementById("progressBar");
const progressWhat = document.getElementById("progressWhat");
const progressPct = document.getElementById("progressPct");
const progressTitle = document.getElementById("progressTitle");
const progressErr = document.getElementById("progressErr");
const progressSpin = document.getElementById("progressSpin");
const cancelBtn = document.getElementById("cancelBtn");
const failActions = document.getElementById("failActions");
const hintEl = document.getElementById("hint");
const modelHintEl = document.getElementById("modelHint");
const stepProvider = document.getElementById("viewProvider");
const stepModels = document.getElementById("viewModels");
const viewProgress = document.getElementById("viewProgress");

let lastStage = "providers";
let activeAbort = null;

function log(line) {
  const splashPick = document.getElementById("seekPick");
  const splashHint = document.getElementById("seekModelHint");
  if (splashPick && !splashPick.hidden && splashHint) {
    splashHint.textContent = line;
    return;
  }
  const onModels = stepModels && !stepModels.hidden;
  const el = onModels ? modelHintEl : hintEl;
  if (el) el.textContent = line;
}

function isNoise(line) {
  const s = String(line || "").trim();
  if (!s) return true;
  if (/#{2,}/.test(s) || /#=#/.test(s)) return true;
  if (/^[#=\-\s.]*\d/.test(s) && /%/.test(s) && !/[가-힣a-zA-Z]/.test(s.replace(/%/g, ""))) return true;
  return false;
}

function modelLine(m) {
  const disk = m.disk_gb != null ? m.disk_gb : m.size_gb;
  const ram = m.ram_gb != null ? m.ram_gb : m.size_gb;
  const bits = [];
  if (disk != null) bits.push("저장 " + disk + "GB");
  if (ram != null) bits.push("램 약 " + ram + "GB");
  if (m.status) bits.push(m.status);
  return bits.join(" · ");
}

function asItems(rows, kind) {
  return (rows || []).map((r) => ({
    id: r.id,
    name: r.name || r.id,
    line: r.line || modelLine(r),
    selected: !!r.selected && !r.installed,
    disabled: !!r.disabled,
    installed: !!r.installed,
    org: r.org || "",
    org_color: r.org_color || "",
    org_logo: r.org_logo || "",
    kind,
  }));
}

function selectedModels() {
  return [...new Set(DD.selected("modelDrop").concat(DD.selected("seekModelDrop")))];
}

function setStep(name) {
  if (typeof window.go === "function") {
    window.go(name === "models" ? "/install/models" : "/install");
    return;
  }
  if (stepProvider) stepProvider.hidden = name !== "providers";
  if (stepModels) stepModels.hidden = name !== "models";
}

window.setInstallStep = setStep;

function showPage(id) {
  if (typeof window.showView === "function") {
    window.showView(id);
    return;
  }
  document.querySelectorAll("main > section").forEach((sec) => {
    sec.hidden = sec.id !== id;
  });
  document.body.classList.toggle("is-splash", id === "viewWelcome");
}

function showProgress(what) {
  showPage("viewProgress");
  if (progressSpin) progressSpin.hidden = false;
  if (cancelBtn) cancelBtn.hidden = false;
  if (failActions) failActions.hidden = true;
  if (progressErr) {
    progressErr.hidden = true;
    progressErr.textContent = "";
  }
  setProgress(2, what || "준비");
}

function hideProgress() {
  if (failActions) failActions.hidden = true;
  if (cancelBtn) cancelBtn.hidden = false;
  if (progressSpin) progressSpin.hidden = false;
  if (window.welcomeOnSplash) {
    if (typeof window.showView === "function") window.showView("viewWelcome");
    else showPage("viewWelcome");
    document.body.classList.add("is-splash");
    return;
  }
  if (typeof window.go === "function") {
    window.go(lastStage === "models" ? "/install/models" : "/install", true);
    return;
  }
  showPage(lastStage === "models" ? "viewModels" : "viewProvider");
}

function showFail(msg) {
  showPage("viewProgress");
  if (progressSpin) progressSpin.hidden = true;
  if (cancelBtn) cancelBtn.hidden = true;
  if (failActions) failActions.hidden = false;
  if (progressTitle) progressTitle.textContent = "실패";
  if (progressErr) {
    progressErr.hidden = false;
    progressErr.textContent = msg || "다시 시도하세요.";
  }
}

function setProgress(pct, stage) {
  const n = Math.max(0, Math.min(100, Number(pct) || 0));
  if (progressBar) progressBar.style.width = n + "%";
  if (progressTitle) progressTitle.textContent = n >= 100 ? "완료" : "받는 중";
  if (progressPct) progressPct.textContent = Math.round(n) + "%";
  if (progressWhat && stage) progressWhat.textContent = stage;
}

async function load() {
  const cat = await fetch("/api/catalog").then((r) => r.json());
  const all = asItems(cat.models || [], "model");
  const have = (cat.models || []).filter((m) => m.installed);
  const haveEl = document.getElementById("haveModels");
  if (haveEl) {
    haveEl.hidden = !have.length;
    haveEl.innerHTML = have.map((m) => `<li>${m.id}</li>`).join("");
  }
  const ollama = (cat.providers || []).find((p) => p.id === "ollama");
  if (installBtn) {
    installBtn.hidden = !!(ollama && ollama.installed);
    log("");
  }
  DD.mount("providerDrop", {
    label: "프로바이더",
    items: asItems(cat.providers, "provider")
      .filter((i) => i.id === "ollama")
      .map((i) => ({ ...i, selected: true, disabled: false, line: "모델을 돌리는 엔진" })),
  });
  const fresh = all.filter((i) => !i.installed);
  DD.mount("modelDrop", { label: "더 받을 모델", multi: true, items: fresh.length ? fresh : all });
  DD.mount("seekModelDrop", { label: "더 받을 모델", multi: true, items: fresh.length ? fresh : all });
  window.installState = {
    provider: !!(ollama && ollama.installed),
    modelCount: have.length,
  };
  if (typeof window.onCatalogLoad === "function") window.onCatalogLoad();
}

async function stream(url, body) {
  activeAbort = new AbortController();
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "free-ai-scheduler" },
    body: JSON.stringify(body),
    signal: activeAbort.signal,
  });
  if (res.status === 409) return { ok: false, error: "이미 다른 설치가 진행 중입니다. 잠시 후 다시 시도하세요." };
  if (!res.ok || !res.body) {
    let msg = "설치 요청이 실패했습니다.";
    try {
      const data = await res.json();
      if (data && data.error) msg = data.error;
    } catch (err) {
      /* ignore */
    }
    return { ok: false, error: msg };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const chunks = buf.split("\n\n");
    buf = chunks.pop() || "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const ev = JSON.parse(line.slice(6));
      const stage = ev.stage || "";
      if (typeof ev.progress === "number") setProgress(ev.progress, stage);
      else if (ev.line && !isNoise(ev.line)) {
        const m = String(ev.line).match(/(\d+(?:\.\d+)?)\s*%/);
        if (m) setProgress(Number(m[1]), stage);
        else if (progressWhat) progressWhat.textContent = stage || ev.line.slice(0, 80);
      } else if (stage && progressWhat) {
        progressWhat.textContent = stage;
      }
      if (ev.event === "error" && ev.line) {
        return { ok: false, error: ev.line, stage };
      }
      if (ev.event === "done") return ev;
    }
  }
  return { ok: false, error: "응답이 끊겼습니다. 다시 시도하세요." };
}

async function cancelInstall() {
  if (activeAbort) activeAbort.abort();
  try {
    await fetch("/api/install/cancel", { method: "POST", headers: { "X-Requested-With": "free-ai-scheduler" } });
  } catch (err) {
    /* ignore */
  }
  hideProgress();
  log("취소했습니다. 다시 받을 수 있습니다.");
}

async function runStage(stage) {
  lastStage = stage;
  if (stage === "providers") {
    const providers = DD.selected("providerDrop");
    if (!providers.length) {
      log("프로바이더를 고르세요.");
      return;
    }
    if (!providers.includes("ollama")) {
      log("예약하려면 Ollama가 필요합니다.");
      return;
    }
    showProgress("Ollama");
    return finishStage(
      await stream("/api/install", { stage: "providers", providers }),
      "providers",
    );
  }
  const models = selectedModels();
  if (!models.length) {
    log("모델을 하나 고르세요.");
    return;
  }
  showProgress(models[0]);
  return finishStage(await stream("/api/install", { stage: "models", models, agents: [] }), "models");
}

async function finishStage(ev, stage) {
  if (ev && ev.cancelled) {
    hideProgress();
    log("취소했습니다. 다시 받을 수 있습니다.");
    return;
  }
  if (!ev || ev.ok === false) {
    showFail((ev && (ev.error || ev.line)) || "설치에 실패했습니다.");
    return;
  }
  await load();
  if (stage === "providers") {
    if (ev.provider_ready === false) {
      showFail("프로바이더는 받았지만 서버가 안 켜졌습니다. 다시 시도하세요.");
      return;
    }
    if (typeof window.go === "function") await window.go("/setup/models");
    else setStep("models");
    return;
  }
  if (typeof window.onInstallDone === "function") await window.onInstallDone(ev);
  else hideProgress();
}

if (cancelBtn) cancelBtn.addEventListener("click", () => cancelInstall());
if (installBtn) {
  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    try {
      await runStage("providers");
    } catch (err) {
      if (err && err.name === "AbortError") {
        hideProgress();
        log("취소했습니다. 다시 받을 수 있습니다.");
      } else showFail(String((err && err.message) || err));
    } finally {
      installBtn.disabled = false;
      activeAbort = null;
    }
  });
}
async function onModelInstall(btn) {
  if (!btn) return;
  btn.disabled = true;
  try {
    await runStage("models");
  } catch (err) {
    if (err && err.name === "AbortError") {
      hideProgress();
      log("취소했습니다. 다시 받을 수 있습니다.");
    } else showFail(String((err && err.message) || err));
  } finally {
    btn.disabled = false;
    activeAbort = null;
  }
}
if (modelBtn) modelBtn.addEventListener("click", () => onModelInstall(modelBtn));
const seekModelBtn = document.getElementById("seekModelBtn");
if (seekModelBtn) seekModelBtn.addEventListener("click", () => onModelInstall(seekModelBtn));
const retryBtn = document.getElementById("retryBtn");
if (retryBtn) {
  retryBtn.addEventListener("click", async () => {
    hideProgress();
    await runStage(lastStage);
  });
}
const failBackBtn = document.getElementById("failBackBtn");
if (failBackBtn) {
  failBackBtn.addEventListener("click", () => hideProgress());
}
