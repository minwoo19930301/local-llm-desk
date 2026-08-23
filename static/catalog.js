const installBtn = document.getElementById("installBtn");
const progressEl = document.getElementById("progress");
const progressBar = document.getElementById("progressBar");
const progressWhat = document.getElementById("progressWhat");
const progressPct = document.getElementById("progressPct");
const progressTitle = document.getElementById("progressTitle");
const progressErr = document.getElementById("progressErr");
const hintEl = document.getElementById("hint");

function log(line) {
  if (hintEl) hintEl.textContent = line;
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
    selected: !!r.selected,
    disabled: !!r.disabled,
    kind,
  }));
}

function selectedModels() {
  return [...new Set(DD.selected("lightDrop").concat(DD.selected("reasonDrop")))];
}

function showProgress(what) {
  if (!progressEl) return;
  progressEl.hidden = false;
  if (progressErr) {
    progressErr.hidden = true;
    progressErr.textContent = "";
  }
  setProgress(2, what || "준비");
}

function hideProgress() {
  if (progressEl) progressEl.hidden = true;
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
  const hw = cat.hardware;
  const hwLine = document.getElementById("hwLine");
  if (hwLine) {
    const disk = hw.disk_free_gb != null ? " · 저장 " + hw.disk_free_gb + "GB 남음" : "";
    const total = hw.disk_total_gb != null ? " / " + hw.disk_total_gb + "GB" : "";
    hwLine.textContent = (hw.chip || "") + " · 램 " + hw.ram_gb + "GB" + disk + total;
  }
  const all = cat.models || [];
  DD.mount("providerDrop", {
    label: "프로바이더",
    multi: true,
    items: asItems(cat.providers, "provider").map((i) =>
      i.id === "ollama" ? { ...i, selected: true } : i,
    ),
  });
  DD.mount("lightDrop", {
    label: "경량용",
    multi: true,
    items: asItems(
      all.filter((m) => m.role === "빠른 답"),
      "model",
    ),
  });
  DD.mount("reasonDrop", {
    label: "추론용",
    multi: true,
    items: asItems(
      all.filter((m) => m.role === "추론·코딩"),
      "model",
    ),
  });
  DD.mount("agentDrop", {
    label: "도구 에이전트 (옵션)",
    multi: true,
    empty: "-",
    items: asItems(cat.agents, "agent"),
  });
}

let activeAbort = null;

async function stream(url, body) {
  activeAbort = new AbortController();
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: activeAbort.signal,
  });
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
        if (progressErr) {
          progressErr.hidden = false;
          progressErr.textContent = ev.line;
        } else log(ev.line);
      }
      if (ev.event === "done") {
        if (ev.cancelled) return ev;
        if (ev.ok === false && (ev.error || ev.line)) {
          if (progressErr) {
            progressErr.hidden = false;
            progressErr.textContent = ev.error || ev.line;
          } else log(ev.error || ev.line);
        }
        return ev;
      }
    }
  }
  return { ok: false };
}

async function cancelInstall() {
  if (activeAbort) activeAbort.abort();
  try {
    await fetch("/api/install/cancel", { method: "POST" });
  } catch (err) {
    /* ignore */
  }
  hideProgress();
  log("취소했습니다.");
}

const cancelBtn = document.getElementById("cancelBtn");
if (cancelBtn) cancelBtn.addEventListener("click", () => cancelInstall());

installBtn.addEventListener("click", async () => {
  const providers = DD.selected("providerDrop");
  const models = selectedModels();
  const agents = DD.selected("agentDrop");
  if (!providers.length && !models.length && !agents.length) {
    log("프로바이더 또는 모델을 고르세요.");
    return;
  }
  if (!window.installReady && !models.length) {
    log("모델을 하나 고르세요.");
    return;
  }
  installBtn.disabled = true;
  showProgress("Ollama");
  try {
    const ev = await stream("/api/install", { providers, models, agents });
    if (ev && ev.cancelled) {
      hideProgress();
      log("취소했습니다.");
      return;
    }
    await load();
    if (typeof window.onInstallDone === "function") await window.onInstallDone(ev);
    else hideProgress();
  } catch (err) {
    if (err && err.name === "AbortError") {
      hideProgress();
      log("취소했습니다.");
      return;
    }
    log(String(err.message || err));
    hideProgress();
  } finally {
    installBtn.disabled = false;
    activeAbort = null;
  }
});
