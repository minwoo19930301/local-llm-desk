const jobList = document.getElementById("jobList");
const tryOut = document.getElementById("tryOut");
const tryBtn = document.getElementById("tryBtn");
const editTitle = document.getElementById("editTitle");
const editError = document.getElementById("editError");

/* ---------- shared helpers (also used by timeline.js / connectors.js via window.Desk) ---------- */

const REQ_HEADER = { "X-Requested-With": "free-ai-scheduler" };
const LS = {
  view: "desk.view",
  lastView: "desk.lastView",
};

const SCHEDULE = {
  save: "저장만",
  now: "저장만",
  every_1m: "1분마다",
  every_5m: "5분마다",
  every_15m: "15분마다",
  hourly: "1시간마다",
  every_6h: "6시간마다",
  daily: "매일",
  weekdays: "평일",
  weekend: "주말",
  once_1m: "1분 뒤 한 번",
  once_5m: "5분 뒤 한 번 · 앱 실행 중",
};

const STATUS_LABEL = {
  ok: "성공",
  fail: "실패",
  skipped: "건너뜀",
  deferred_timeout: "시간초과",
  aborted: "중단됨",
  empty: "빈 응답",
};

const KIND_LABEL = { mcp: "MCP", cli: "CLI", http: "HTTP", skill: "스킬" };
const SOURCE_LABEL = {
  manual: "직접 추가",
  "claude-code": "Claude Code",
  "claude-desktop": "Claude Desktop",
  codex: "Codex",
  cursor: "Cursor",
  "claude-plugin": "Claude 플러그인",
  "codex-plugin": "Codex 플러그인",
  "skill-dir": "스킬 폴더",
};

const EFFORTS = ["low", "medium", "high"];
const EFFORT_LABELS = ["빠르고 간단", "균형", "고급 · 느림"];
const CTX_BY_EFFORT = { low: 4096, medium: 8192, high: 16384 };

const FORM = {
  permission: [
    { id: "read", name: "읽기만 · 조회" },
    { id: "workspace", name: "작업 폴더 안에서", selected: true },
    { id: "machine", name: "이 컴퓨터 · 자격 증명 폴더 제외" },
  ],
  ramPolicy: [
    { id: "defer", name: "여유 메모리가 생길 때까지 대기", selected: true },
    { id: "skip", name: "이번 실행 건너뛰기" },
    { id: "downgrade", name: "더 작은 모델로 실행" },
  ],
  preset: [
    { id: "save", name: "저장만", selected: true },
    { id: "every_1m", name: "1분마다" },
    { id: "every_5m", name: "5분마다" },
    { id: "every_15m", name: "15분마다" },
    { id: "hourly", name: "1시간마다" },
    { id: "every_6h", name: "6시간마다" },
    { id: "daily", name: "매일" },
    { id: "weekdays", name: "평일" },
    { id: "weekend", name: "주말" },
    { id: "once_1m", name: "1분 뒤 한 번" },
    { id: "once_5m", name: "5분 뒤 한 번 · 앱 실행 중" },
    { id: "cron", name: "cron 식" },
  ],
  alert: [
    { id: "always", name: "실행 시", selected: true },
    { id: "fail", name: "실패 시" },
    { id: "ok", name: "성공 시" },
    { id: "off", name: "안 함" },
  ],
};

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

let toastTimer = 0;

/** Show a 3-second toast. kind: "ok" | "bad" | "" */
function toast(msg, kind) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast" + (kind ? " toast--" + kind : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 3000);
}

function setOffline(on) {
  const el = document.getElementById("offline");
  if (el) el.hidden = !on;
}

/** GET JSON; toggles the offline banner on network failure. */
async function getJSON(path) {
  let res;
  try {
    res = await fetch(path);
  } catch (err) {
    setOffline(true);
    throw new Error("서버에 연결할 수 없습니다");
  }
  setOffline(false);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `실패 (${res.status})`);
  return data;
}

/** Non-GET request. Always sends the CSRF marker header. */
async function post(path, body, method) {
  const headers = { ...REQ_HEADER };
  if (body) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method: method || "POST", headers, body: body ? JSON.stringify(body) : null });
  } catch (err) {
    setOffline(true);
    throw new Error("서버에 연결할 수 없습니다");
  }
  setOffline(false);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `실패 (${res.status})`);
  return data;
}

const DAY_KO = ["일", "월", "화", "수", "목", "금", "토"];

/** "3시간 뒤" / "12분 전" style relative time. */
function relTime(iso) {
  const t = Date.parse(iso || "");
  if (!t) return "";
  const d = Math.round((t - Date.now()) / 1000);
  const a = Math.abs(d);
  const suffix = d >= 0 ? "뒤" : "전";
  if (a < 60) return d >= 0 ? "곧" : "방금";
  if (a < 3600) return `${Math.round(a / 60)}분 ${suffix}`;
  if (a < 86400) return `${Math.round(a / 3600)}시간 ${suffix}`;
  return `${Math.round(a / 86400)}일 ${suffix}`;
}

/** "9/5 (금) 15:02" */
function fmtTime(iso) {
  const t = new Date(iso || "");
  if (Number.isNaN(t.getTime())) return "";
  const hh = String(t.getHours()).padStart(2, "0");
  const mm = String(t.getMinutes()).padStart(2, "0");
  return `${t.getMonth() + 1}/${t.getDate()} (${DAY_KO[t.getDay()]}) ${hh}:${mm}`;
}

function fmtGb(n) {
  return n == null || Number.isNaN(Number(n)) ? "?" : Number(n).toFixed(1);
}

function runStatus(run) {
  if (!run) return "";
  return run.status || (run.ok ? "ok" : "fail");
}

function statusClass(status) {
  if (status === "ok") return "ok";
  if (status === "fail" || status === "deferred_timeout" || status === "empty") return "bad";
  return "muted";
}

function clampInt(raw, min, max, dflt) {
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return dflt;
  return Math.max(min, Math.min(max, n));
}

window.Desk = { esc: escapeHtml, toast, getJSON, post, relTime, fmtTime, fmtGb, KIND_LABEL, SOURCE_LABEL, STATUS_LABEL };

/* ---------- state ---------- */

let editingId = null;
let effortTouched = false;
let editNumCtx = 0;
let installedModels = [];
let connectorsById = new Map();
let cachedJobs = [];
let activeRun = null;
let lastCronSync = null;
const running = new Set();
const results = new Map();
const collapsedResults = new Set();
const expanded = new Set();
const runHistory = new Map();
const openRuns = new Set();
const rendered = new Map();

/* ---------- form widgets ---------- */

function mountFormDrops() {
  DD.mount("permissionDrop", { label: "권한", items: FORM.permission });
  DD.mount("presetDrop", { label: "언제", items: FORM.preset, onChange: syncWhen });
  DD.mount("alertDrop", { label: "알림", items: FORM.alert });
  DD.mount("ramPolicyDrop", { label: "메모리가 부족할 때", items: FORM.ramPolicy, onChange: syncPolicy });
  const range = document.getElementById("effortRange");
  if (range) {
    range.addEventListener("input", () => {
      effortTouched = true;
      setEffortLabel(Number(range.value));
      updateRamLine();
    });
    setEffortLabel(Number(range.value));
  }
}

function syncPolicy(policy) {
  document.getElementById("deferWrap").hidden = policy !== "defer";
  document.getElementById("fallbackWrap").hidden = policy !== "downgrade";
}

function mountFallbackDrop(pick) {
  const items = [{ id: "", name: "설치된 모델 중 자동 선택", selected: !pick }];
  items.push(...installedModels.map((name) => ({ id: name, name, selected: name === pick })));
  if (pick && !installedModels.includes(pick)) {
    items.push({ id: pick, name: pick, line: "설치되지 않은 대체 모델", selected: true, disabled: true });
  }
  DD.mount("fallbackModelDrop", { label: "대체 모델", items });
}

function syncWhen(id) {
  const wrap = document.getElementById("whenWrap");
  const timeWrap = document.getElementById("timeWrap");
  const cronWrap = document.getElementById("cronWrap");
  const timed = ["daily", "weekdays", "weekend"].includes(id);
  const cron = id === "cron";
  if (wrap) wrap.hidden = !(timed || cron);
  if (timeWrap) timeWrap.hidden = !timed;
  if (cronWrap) cronWrap.hidden = !cron;
}

function setEffortLabel(n) {
  const el = document.getElementById("effortLabel");
  if (el) el.textContent = EFFORT_LABELS[n] || EFFORT_LABELS[1];
}

function effortValue() {
  const range = document.getElementById("effortRange");
  return EFFORTS[clampInt(range ? range.value : "1", 0, 2, 1)];
}

function setEffortValue(id) {
  const range = document.getElementById("effortRange");
  const idx = EFFORTS.indexOf(id);
  if (range) range.value = String(idx === -1 ? 1 : idx);
  setEffortLabel(Number(range && range.value));
}

/** Rough default effort by parameter count in the model tag ("qwen3:30b" → high). */
function effortForModel(name) {
  const n = String(name || "").toLowerCase();
  if (/e4b/.test(n)) return "medium";
  const m = n.match(/(\d+(?:\.\d+)?)b(?![a-z])/);
  const size = m ? parseFloat(m[1]) : 0;
  if (size >= 20) return "high";
  if (size >= 8) return "medium";
  return "low";
}

function onModelChange(id) {
  if (!editingId && !effortTouched) setEffortValue(effortForModel(id));
  updateRamLine();
}

let ramSeq = 0;

/** RAM line under the model dropdown: "지금 여유 4.7GB · 이 모델 약 6.2GB 필요 · 부족하면 미루기". */
async function updateRamLine() {
  const el = document.getElementById("editRam");
  if (!el) return;
  const model = DD.value("jobModelDrop");
  if (!model) {
    el.textContent = "";
    return;
  }
  const seq = ++ramSeq;
  const numCtx = editNumCtx || CTX_BY_EFFORT[effortValue()] || 4096;
  try {
    const d = await getJSON(`/api/ram?model=${encodeURIComponent(model)}&num_ctx=${numCtx}`);
    if (seq !== ramSeq) return;
    const snap = d.snapshot || {};
    const free = snap.avail_gb != null ? snap.avail_gb : snap.free_gb;
    const fits = d.verdict ? d.verdict.action === "run" : Number(free) >= Number(d.need_gb);
    let line = `지금 여유 ${fmtGb(free)}GB · 이 모델 약 ${fmtGb(d.need_gb)}GB 필요`;
    if (!fits) line += " · 부족하면 예약 실행 때 자동으로 기다립니다";
    el.textContent = line;
    el.className = "ram-line ram-line--form " + (fits ? "ok" : "bad");
  } catch (err) {
    if (seq === ramSeq) el.textContent = "";
  }
}

/* ---------- routing ---------- */

function currentPath() {
  return decodeURIComponent(location.pathname || "/");
}

let welcomeTick = 0;

function pauseSplashVideos() {
  const el = document.getElementById("introVideo");
  if (el) el.pause();
}

window.showView = showView;

function showView(id) {
  const leavingSplash = document.body.classList.contains("is-splash") && id !== "viewWelcome";
  document.querySelectorAll("main > section").forEach((sec) => {
    sec.hidden = sec.id !== id;
  });
  document.body.classList.toggle("is-splash", id === "viewWelcome");
  if (leavingSplash) {
    welcomeTick += 1;
    pauseSplashVideos();
  }
}

async function go(path, replace) {
  if (replace) history.replaceState({}, "", path);
  else history.pushState({}, "", path);
  await route();
}

window.go = go;

function englishPath(path) {
  const map = {
    "/설치": "/install",
    "/설치/모델": "/install/models",
    "/설치/스펙": "/setup",
    "/설치/엔진": "/setup/engine",
    "/설치/연결": "/setup/connect",
    "/자동화": "/jobs",
    "/자동화/추가": "/jobs/new",
    "/연동": "/connectors",
    "/설정": "/settings",
    "/타임라인": "/timeline",
    "/welcome": "/",
  };
  if (path.indexOf("/자동화/수정") === 0) return "/jobs/edit";
  return map[path] || path;
}

function tabFor(path) {
  if (path.indexOf("/jobs") === 0) return "/jobs";
  if (path === "/connectors" || path === "/settings") return path;
  if (path.indexOf("/install") === 0) return "/install";
  return "";
}

/** Tabs + wide layout for app pages; 480px setup look elsewhere. */
function setChrome(path) {
  const tab = tabFor(path);
  const tabs = document.getElementById("tabs");
  if (tabs) {
    tabs.hidden = !tab;
    tabs.querySelectorAll(".tabs__tab").forEach((b) => {
      const on = b.dataset.path === tab;
      b.classList.toggle("is-on", on);
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  }
  document.body.classList.toggle("is-wide", !!tab && tab !== "/install");
  if (tab && tab !== "/install") {
    try {
      localStorage.setItem(LS.lastView, tab);
    } catch (err) {
      /* storage unavailable */
    }
  }
}

function visitedBefore() {
  try {
    return !!localStorage.getItem(LS.lastView);
  } catch (err) {
    return false;
  }
}

let retryTimer = 0;

async function route() {
  const path = currentPath();
  const mapped = englishPath(path);
  if (mapped !== path) return go(mapped + location.search, true);

  let status;
  try {
    status = await getJSON("/api/status");
  } catch (err) {
    clearTimeout(retryTimer);
    retryTimer = setTimeout(route, 3000);
    return;
  }
  const ready = isReady(status);
  const providerReady = !!status.provider_ready;
  window.installReady = ready;
  const query = new URLSearchParams(location.search);
  setChrome(path);

  if (path === "/" || path === "") {
    const skipIntro = query.get("intro") !== "1" && visitedBefore() && (ready || status.setup_done);
    if (skipIntro) return go("/jobs", true);
    showView("viewWelcome");
    runIntro(status);
    return;
  }
  if (path === "/setup") {
    showView("viewWelcome");
    runSpecs(status);
    return;
  }
  if (path === "/setup/engine") {
    showView("viewWelcome");
    runEngine(status);
    return;
  }
  if (path === "/setup/models") {
    showView("viewWelcome");
    runModels(status);
    return;
  }
  if (path === "/setup/connect") return go("/jobs", true);
  if (path === "/install/models") {
    if (!providerReady && !ready) return go("/install", true);
    showView("viewModels");
    if (typeof load === "function") await load();
    return;
  }
  if (path === "/install") {
    showView("viewProvider");
    if (typeof load === "function") await load();
    return;
  }
  if (!ready) return go("/", true);

  if (path === "/jobs/new" || path.indexOf("/jobs/edit") === 0) {
    let job = null;
    if (path.indexOf("/jobs/edit") === 0) {
      const id = query.get("id");
      const jobs = await getJSON("/api/jobs").catch(() => ({ jobs: [] }));
      job = (jobs.jobs || []).find((j) => j.id === id) || null;
      if (!job) {
        toast("작업을 찾을 수 없습니다", "bad");
        return go("/jobs", true);
      }
    }
    showView("viewEdit");
    await openEdit(job);
    return;
  }
  if (path === "/timeline") {
    setJobsView("timeline", true);
    return go("/jobs", true);
  }
  if (path === "/jobs") {
    showView("viewJobs");
    setJobsView(readJobsView(), true);
    await loadConnectors();
    await refresh();
    await openRunFromQuery(query);
    return;
  }
  if (path === "/connectors") {
    showView("viewConnectors");
    if (window.Connectors) await window.Connectors.show();
    return;
  }
  if (path === "/settings") {
    showView("viewSettings");
    await showSettings(status);
    return;
  }
  return go("/jobs", true);
}

/* ---------- splash / setup flow (unchanged behaviour) ---------- */

function fillSplashSpecs(hw) {
  const el = document.getElementById("splashSpecs");
  if (!el || !hw) return;
  el.innerHTML =
    `<dt>칩</dt><dd>${hw.chip || "-"}</dd>` +
    `<dt>램</dt><dd>${hw.ram_gb || "-"}GB</dd>` +
    `<dt>저장</dt><dd>${hw.disk_free_gb != null ? hw.disk_free_gb + "GB 남음" : "-"}` +
    `${hw.disk_total_gb != null ? " / " + hw.disk_total_gb + "GB" : ""}</dd>`;
  el.hidden = false;
}

function sleep(ms, tick) {
  return new Promise((resolve) => {
    setTimeout(() => resolve(tick === welcomeTick), ms);
  });
}

function playSplashVideo() {
  const on = document.getElementById("introVideo");
  if (!on) return;
  on.loop = false;
  on.currentTime = 0;
  const play = on.play();
  if (play && play.catch) play.catch(() => {});
}

function setSeekNav(backPath, nextPath, nextLabel) {
  const back = document.getElementById("seekBack");
  const next = document.getElementById("splashNext");
  if (back) {
    back.disabled = !backPath;
    back.onclick = backPath ? () => go(backPath) : null;
  }
  if (next) {
    next.textContent = nextLabel || "다음";
    next.disabled = !nextPath;
    next.onclick = nextPath ? () => go(nextPath) : null;
  }
}

function paintStepNav(backEl, nextEl, backPath, nextPath) {
  if (backEl) {
    backEl.disabled = !backPath;
    backEl.onclick = backPath ? () => go(backPath) : null;
  }
  if (nextEl) {
    nextEl.disabled = !nextPath;
    nextEl.onclick = nextPath ? () => go(nextPath) : null;
  }
}

window.onCatalogLoad = function () {
  const path = currentPath();
  const st = window.installState || {};
  if (path === "/install") {
    paintStepNav(
      document.getElementById("providerBackBtn"),
      document.getElementById("providerNextBtn"),
      "/setup/engine",
      st.provider ? "/setup/models" : "",
    );
  }
  if (path === "/install/models") {
    paintStepNav(
      document.getElementById("backBtn"),
      document.getElementById("modelsNextBtn"),
      "/install",
      st.modelCount ? "/jobs" : "",
    );
  }
};

function waitTapOrTime(ms, tick) {
  return new Promise((resolve) => {
    if (tick !== welcomeTick) return resolve(false);
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPtr);
      resolve(tick === welcomeTick);
    };
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Tab") return;
      e.preventDefault();
      finish();
    };
    const onPtr = () => finish();
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPtr);
    setTimeout(finish, ms);
  });
}

function setGuideLines(root, lines) {
  if (!root) return;
  root.innerHTML = lines.map((t) => `<p class="guide__line">${t}</p>`).join("");
  root.hidden = false;
}

function hideSeekExtras() {
  const list = document.getElementById("seekList");
  const specs = document.getElementById("splashSpecs");
  const guideCopy = document.getElementById("guideCopy");
  if (list) {
    list.hidden = true;
    list.innerHTML = "";
  }
  const pick = document.getElementById("seekPick");
  if (pick) pick.hidden = true;
  if (specs) specs.hidden = true;
  if (guideCopy) {
    guideCopy.hidden = true;
    guideCopy.innerHTML = "";
  }
}

function splashEls() {
  return {
    splash: document.getElementById("splash"),
    logo: document.getElementById("splashLogo"),
    seek: document.getElementById("splashSeek"),
    guideCopy: document.getElementById("guideCopy"),
    checkText: document.getElementById("splashCheckText"),
    next: document.getElementById("splashNext"),
    specs: document.getElementById("splashSpecs"),
    list: document.getElementById("seekList"),
    pick: document.getElementById("seekPick"),
    getBtn: document.getElementById("seekModelBtn"),
  };
}

function revealGuide(root, lines) {
  setGuideLines(root, lines);
  requestAnimationFrame(() => {
    [...(root ? root.querySelectorAll(".guide__line") : [])].forEach((el, i) => {
      el.style.transitionDelay = i * 0.22 + "s";
      el.classList.add("is-in");
    });
  });
}

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

async function runIntro(status) {
  const tick = ++welcomeTick;
  const ui = splashEls();
  pauseSplashVideos();
  hideSeekExtras();
  if (ui.seek) ui.seek.hidden = true;
  if (ui.splash) ui.splash.className = "splash is-logo";
  if (ui.logo) ui.logo.innerHTML = ui.logo.innerHTML;
  const reduce = reducedMotion();
  if (!(await sleep(reduce ? 400 : 2000, tick))) return;
  if (ui.splash) ui.splash.className = "splash is-film";
  playSplashVideo();
  if (!(await sleep(reduce ? 300 : 1600, tick))) return;
  if (!(await waitTapOrTime(reduce ? 800 : 10000, tick))) return;
  if (ui.splash) ui.splash.classList.add("is-fade-out");
  if (!(await sleep(reduce ? 80 : 560, tick))) return;
  pauseSplashVideos();
  if (tick !== welcomeTick) return;
  return go("/setup");
}

async function runSpecs(status) {
  const tick = ++welcomeTick;
  const ui = splashEls();
  const reduce = reducedMotion();
  pauseSplashVideos();
  hideSeekExtras();
  if (ui.seek) ui.seek.hidden = false;
  if (ui.splash) ui.splash.className = "splash is-scan";
  if (ui.checkText) ui.checkText.textContent = "스펙을 찾는 중";
  setSeekNav("/", "", "다음");
  if (!(await sleep(reduce ? 400 : 2200, tick))) return;
  let live = status;
  try {
    live = await getJSON("/api/status");
  } catch (err) {
    /* keep */
  }
  if (tick !== welcomeTick) return;
  if (ui.splash) ui.splash.className = "splash is-scan is-found";
  if (ui.checkText) ui.checkText.textContent = "이 컴퓨터에서 확인된 스펙입니다.";
  fillSplashSpecs((live && live.hardware) || {});
  setSeekNav("/", "/setup/engine", "다음");
}

async function runEngine(status) {
  const tick = ++welcomeTick;
  const ui = splashEls();
  const reduce = reducedMotion();
  pauseSplashVideos();
  hideSeekExtras();
  if (ui.seek) ui.seek.hidden = false;
  if (ui.splash) ui.splash.className = "splash is-scan";
  if (ui.checkText) ui.checkText.textContent = "프로바이더를 찾는 중";
  setSeekNav("/setup", "", "다음");
  revealGuide(ui.guideCopy, [
    "프로바이더는 AI를 돌리는 엔진입니다.",
    "모델은 그 엔진이 실제로 쓰는 뇌입니다.",
    "엔진이 있어야 모델을 올리고 일을 시킬 수 있습니다.",
  ]);
  if (!(await sleep(reduce ? 400 : 1800, tick))) return;
  let live = status;
  try {
    live = await getJSON("/api/status");
  } catch (err) {
    /* keep */
  }
  if (tick !== welcomeTick) return;
  if (ui.splash) ui.splash.className = "splash is-scan is-found";
  const providerReady = !!(live && live.provider_ready);
  if (providerReady) {
    if (ui.checkText) ui.checkText.textContent = "Ollama";
    setSeekNav("/setup", "/setup/models", "다음");
    return;
  }
  if (ui.checkText) ui.checkText.textContent = "프로바이더가 없습니다";
  setSeekNav("/setup", "/install", "다음");
}

async function runModels(status) {
  const tick = ++welcomeTick;
  const ui = splashEls();
  const reduce = reducedMotion();
  pauseSplashVideos();
  hideSeekExtras();
  if (ui.seek) ui.seek.hidden = false;
  if (ui.splash) ui.splash.className = "splash is-scan";
  if (ui.checkText) ui.checkText.textContent = "모델을 찾는 중";
  setSeekNav("/setup/engine", "", "다음");
  revealGuide(ui.guideCopy, [
    "모델은 프로바이더가 실제로 쓰는 뇌입니다.",
    "같은 엔진 위에 여러 모델을 올릴 수 있습니다.",
    "뇌가 글을 쓰고 일을 합니다.",
  ]);
  if (!(await sleep(reduce ? 400 : 1800, tick))) return;
  let live = status;
  try {
    live = await getJSON("/api/status");
  } catch (err) {
    /* keep */
  }
  if (tick !== welcomeTick) return;
  const names = ((live && live.ollama && live.ollama.models) || []).map((m) => m.name).filter(Boolean);
  if (ui.splash) ui.splash.className = "splash is-scan is-found is-pick";
  if (names.length) {
    if (ui.checkText) ui.checkText.textContent = "모델 " + names.length + "개";
    if (ui.list) {
      ui.list.hidden = false;
      ui.list.innerHTML = names.map((n) => `<li>${escapeHtml(n)}</li>`).join("");
    }
    if (ui.getBtn) {
      ui.getBtn.textContent = "더 받기";
      ui.getBtn.className = "gds-button gds-button--secondary";
    }
    window.welcomeDiscover = { models: names, note: "" };
    setSeekNav("/setup/engine", "/jobs", "다음");
  } else {
    if (ui.checkText) ui.checkText.textContent = "모델이 없습니다";
    if (ui.list) {
      ui.list.hidden = true;
      ui.list.innerHTML = "";
    }
    if (ui.getBtn) {
      ui.getBtn.textContent = "다운로드";
      ui.getBtn.className = "gds-button gds-button--cta";
    }
    window.welcomeDiscover = { models: [], note: "" };
    setSeekNav("/setup/engine", "", "다음");
  }
  if (ui.pick) {
    ui.pick.hidden = false;
    ui.pick.style.animation = "none";
    void ui.pick.offsetWidth;
    ui.pick.style.animation = "";
  }
  window.welcomeOnSplash = true;
  if (typeof load === "function") await load();
}

/* ---------- edit form: models / tools / connectors ---------- */

function paintTools(selected) {
  const on = new Set(selected || []);
  DD.mount("jobToolsDrop", {
    label: "이 실행에서 쓸 도구",
    multi: true,
    empty: "글만",
    items: [
      { id: "cli", name: "CLI", line: "이 자동화에서 셸 명령을 실행", selected: on.has("cli") },
      { id: "http", name: "API", line: "이 자동화에서 HTTP를 호출", selected: on.has("http") },
      { id: "chrome", name: "웹 페이지 읽기", line: "공개 웹 페이지의 내용을 가져옴", selected: on.has("chrome") },
    ],
  });
}

/** Model dropdown; `missing` is a saved model that is no longer installed (kept as a disabled, selected option). */
function mountModelDrop(pick, missing) {
  const items = installedModels.map((n) => ({ id: n, name: n, selected: !missing && n === pick }));
  if (missing) {
    items.unshift({ id: missing, name: missing, line: "지금은 안 깔림 · 설치에서 받거나 다른 모델을 고르세요", selected: true, disabled: true });
  }
  if (!items.length) items.push({ id: "", name: "모델 없음 · 설치에서 받기", selected: true, disabled: true });
  DD.mount("jobModelDrop", { label: "모델", items, onChange: onModelChange });
}

async function reloadModels() {
  const [status, cat] = await Promise.all([
    getJSON("/api/status").catch(() => ({ ollama: { models: [] } })),
    getJSON("/api/catalog").catch(() => ({})),
  ]);
  installedModels = ((status.ollama && status.ollama.models) || []).map((m) => m.name || m.model).filter(Boolean);
  const current = DD.value("jobModelDrop");
  const pick = installedModels.includes(current) ? current : installedModels[0] || "";
  mountModelDrop(pick, "");
  const have = new Set((cat.agents || []).filter((a) => a.installed).map((a) => a.id));
  DD.mount("jobAgentDrop", {
    label: "에이전트",
    items: [
      { id: "chat", name: "이 앱", line: "에이전트 없이, 이 실행의 도구만", selected: true },
      { id: "aider", name: "Aider", line: have.has("aider") ? "설치됨" : "아직 안 깔림", disabled: !have.has("aider") },
      { id: "opencode", name: "OpenCode", line: have.has("opencode") ? "설치됨" : "아직 안 깔림", disabled: !have.has("opencode") },
    ],
  });
  paintTools(null);
}

window.reloadModels = reloadModels;

/** Registered connectors → Map(id → connector). Missing endpoint is tolerated. */
async function loadConnectors() {
  try {
    const data = await getJSON("/api/connectors");
    connectorsById = new Map((data.connectors || []).map((c) => [c.id, c]));
  } catch (err) {
    connectorsById = new Map();
  }
}

function connectorLine(c) {
  const bits = [KIND_LABEL[c.kind] || c.kind, SOURCE_LABEL[c.source] || c.source];
  if (c.enabled === false) bits.push("꺼짐");
  return bits.filter(Boolean).join(" · ");
}

function mountConnectorsDrop(selected) {
  const on = new Set(selected || []);
  const items = [...connectorsById.values()].map((c) => ({
    id: c.id,
    name: c.name,
    line: connectorLine(c),
    selected: on.has(c.id),
  }));
  DD.mount("connectorsDrop", { label: "연동", multi: true, empty: "없음", items });
}

function isReady(status) {
  if (status && status.ready) return true;
  return ((status && status.ollama && status.ollama.models) || []).length > 0;
}

window.onInstallDone = async function (ev) {
  if (ev && ev.cancelled) {
    if (typeof hideProgress === "function") hideProgress();
    return;
  }
  if (ev && ev.ok === false) return;
  await go("/jobs", true);
};

/* ---------- jobs list ---------- */

function readJobsView() {
  try {
    return localStorage.getItem(LS.view) === "timeline" ? "timeline" : "list";
  } catch (err) {
    return "list";
  }
}

/** Toggle list ↔ timeline; remembered in localStorage. */
function setJobsView(mode, silent) {
  const timeline = mode === "timeline";
  const wrap = document.getElementById("timelineWrap");
  const listBtn = document.getElementById("viewListBtn");
  const tlBtn = document.getElementById("viewTimelineBtn");
  jobList.hidden = timeline;
  if (wrap) wrap.hidden = !timeline;
  if (listBtn) listBtn.setAttribute("aria-pressed", timeline ? "false" : "true");
  if (tlBtn) tlBtn.setAttribute("aria-pressed", timeline ? "true" : "false");
  try {
    localStorage.setItem(LS.view, timeline ? "timeline" : "list");
  } catch (err) {
    /* storage unavailable */
  }
  if (timeline && !silent && window.Timeline) window.Timeline.refresh();
}

async function refresh() {
  const view = document.getElementById("viewJobs");
  if (!view || view.hidden) return;
  let jobs;
  let status = null;
  try {
    [jobs, status] = await Promise.all([getJSON("/api/jobs"), getJSON("/api/status").catch(() => null)]);
  } catch (err) {
    return;
  }
  cachedJobs = jobs.jobs || [];
  activeRun = status && status.active ? status.active : null;
  paintHeader(status);
  paintList(cachedJobs);
  if (readJobsView() === "timeline" && window.Timeline) window.Timeline.refresh();
}

function elapsedText() {
  if (!activeRun || !activeRun.started_at) return "";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(activeRun.started_at)) / 1000));
  return `(경과 ${s}초)`;
}

function paintHeader(status) {
  const ramEl = document.getElementById("ramLine");
  const actEl = document.getElementById("activeLine");
  const ram = status && status.ram;
  if (ramEl) {
    if (ram) {
      const bits = [`지금 여유 ${fmtGb(ram.avail_gb != null ? ram.avail_gb : ram.free_gb)} GB / 전체 ${fmtGb(ram.total_gb)} GB`];
      if (ram.pressure_pct != null) bits.push(`메모리 압력 ${ram.pressure_pct}%`);
      if (ram.swap_warn) bits.push("스왑 사용 많음");
      ramEl.textContent = bits.join(" · ");
    } else ramEl.textContent = "";
  }
  if (actEl) {
    actEl.hidden = !activeRun;
    if (activeRun) actEl.textContent = `실행 중: ${activeRun.title || activeRun.job_id} ${elapsedText()}`;
  }
}

/** Re-render only the cards whose HTML changed, so open panels and buttons stay put during polling. */
function paintList(jobs) {
  if (!jobs.length) {
    jobList.innerHTML = `<p class="legend">아직 없습니다. 추가로 넣으세요.</p>`;
    rendered.clear();
    return;
  }
  if (!jobList.querySelector("article.job")) jobList.innerHTML = "";
  const seen = new Set();
  jobs.forEach((j, idx) => {
    seen.add(j.id);
    const html = renderJob(j);
    let el = jobList.querySelector(`article.job[data-id="${j.id}"]`);
    if (!el || rendered.get(j.id) !== html) {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      const fresh = tmp.firstElementChild;
      if (el) el.replaceWith(fresh);
      else jobList.appendChild(fresh);
      el = fresh;
      rendered.set(j.id, html);
    }
    if (jobList.children[idx] !== el) jobList.insertBefore(el, jobList.children[idx] || null);
  });
  [...jobList.querySelectorAll("article.job")].forEach((el) => {
    if (!seen.has(el.dataset.id)) {
      el.remove();
      rendered.delete(el.dataset.id);
    }
  });
}

function scheduleLabel(j) {
  if (!j.enabled) return j.once && j.schedule_label ? j.schedule_label : "꺼짐";
  if (j.schedule_label) return j.schedule_label;
  if (j.preset === "cron" || j.preset === "custom") return j.cron || "cron";
  if (j.preset && SCHEDULE[j.preset]) return SCHEDULE[j.preset];
  if (j.cron) return "예약됨";
  return "저장만";
}

function lastRunHtml(lr) {
  if (!lr) return `<span class="muted">아직 실행 안 함</span>`;
  const st = runStatus(lr);
  const label = STATUS_LABEL[st] || st;
  const sec = lr.seconds != null ? ` ${Math.round(lr.seconds)}s` : "";
  const when = lr.at ? ` · ${relTime(lr.at)}` : "";
  const why = st !== "ok" && lr.error ? ` · ${escapeHtml(String(lr.error).slice(0, 80))}` : "";
  return `<span class="${statusClass(st)}">${label}${sec}</span>${when}${why}`;
}

function resultHtml(jobId, r) {
  const st = runStatus(r);
  const label = STATUS_LABEL[st] || st;
  const model = r.model_used || r.model || "";
  const body = r.ok ? r.output : r.error || r.output;
  const open = collapsedResults.has(jobId) ? "" : " open";
  return `<details class="run-result" data-result="${jobId}"${open}>
    <summary><span class="${statusClass(st)}">${label}</span> ${Math.round(r.seconds || 0)}s${model ? " · " + escapeHtml(model) : ""}</summary>
    <pre class="run-full">${escapeHtml(body || "(출력 없음)")}</pre>
  </details>`;
}

function historyHtml(jobId) {
  const runs = runHistory.get(jobId);
  if (runs === undefined) return `<p class="legend legend--left">기록을 읽는 중…</p>`;
  if (!runs.length) return `<p class="legend legend--left">아직 기록이 없습니다.</p>`;
  return `<div class="runs" role="list">${runs.map((r) => runRowHtml(r)).join("")}</div>`;
}

function runRowHtml(r) {
  const st = runStatus(r);
  const label = STATUS_LABEL[st] || st;
  const model = r.model_used || r.model || "";
  const body = r.ok ? r.output : r.error || r.output;
  const open = openRuns.has(r.id);
  const head = String(body || "").slice(0, 200);
  return `<div class="run-row" role="listitem">
    <button type="button" class="run-row__btn" data-runid="${escapeHtml(r.id)}" aria-expanded="${open ? "true" : "false"}">
      <span class="run-row__meta">${escapeHtml(fmtTime(r.at))} · <span class="${statusClass(st)}">${label}</span> · ${Math.round(r.seconds || 0)}s${model ? " · " + escapeHtml(model) : ""}</span>
      ${!open && head ? `<span class="run-row__out">${escapeHtml(head)}${String(body).length > 200 ? "…" : ""}</span>` : ""}
    </button>
    ${open ? `<pre class="run-full">${escapeHtml(body || "(출력 없음)")}</pre>` : ""}
  </div>`;
}

function renderJob(j) {
  const isRunning = running.has(j.id) || !!(activeRun && activeRun.job_id === j.id);
  const cons = (j.connectors || []).map((id) => connectorsById.get(id)).filter(Boolean);
  const chips = cons.map((c) => `<span class="chip chip--${escapeHtml(c.kind)}">${escapeHtml(c.name)}</span>`).join("");
  const next = j.enabled && j.next_run ? `다음 ${relTime(j.next_run)}` : "";
  const knobs = [j.model, j.agent && j.agent !== "chat" ? j.agent : "", scheduleLabel(j), next].filter(Boolean);
  const result = results.get(j.id);
  const title = escapeHtml(j.title);
  return `<article class="job${j.enabled ? "" : " is-off"}" data-id="${j.id}">
    <div class="job__head">
      <b>${title}</b>
    </div>
    <div class="meta">${knobs.map(escapeHtml).join(" · ")}</div>
    ${chips ? `<div class="chips chips--tight">${chips}</div>` : ""}
    <div class="meta">${lastRunHtml(j.last_run)}</div>
    ${isRunning ? `<p class="run-live" data-live="${j.id}">실행 중… ${activeRun && activeRun.job_id === j.id ? elapsedText() : ""}</p>` : ""}
    ${result ? resultHtml(j.id, result) : ""}
    <div class="row row--left">
      <button type="button" class="gds-button gds-button--small" data-run="${j.id}" ${isRunning ? "disabled" : ""} aria-label="${title} 지금 실행">${isRunning ? "실행 중…" : "지금 실행"}</button>
      <button type="button" class="gds-button gds-button--secondary gds-button--small" data-hist="${j.id}" aria-expanded="${expanded.has(j.id) ? "true" : "false"}" aria-label="${title} 기록">기록</button>
      <button type="button" class="gds-button gds-button--secondary gds-button--small" data-edit="${j.id}" aria-label="${title} 수정">수정</button>
      <button type="button" class="gds-button gds-button--secondary gds-button--small toggle${j.enabled ? " is-on" : ""}" data-toggle="${j.id}" aria-pressed="${j.enabled ? "true" : "false"}" aria-label="${title} ${j.enabled ? "끄기" : "켜기"}">${j.enabled ? "켬" : "끔"}</button>
      <button type="button" class="gds-button gds-button--secondary gds-button--small gds-button--danger" data-del="${j.id}" aria-label="${title} 삭제">삭제</button>
    </div>
    ${expanded.has(j.id) ? historyHtml(j.id) : ""}
  </article>`;
}

async function loadHistory(jobId) {
  try {
    const d = await getJSON(`/api/runs?job=${encodeURIComponent(jobId)}&limit=10`);
    runHistory.set(jobId, d.runs || []);
  } catch (err) {
    runHistory.set(jobId, []);
    toast(err.message, "bad");
  }
}

/** `?run=<id>` (alert deep link): expand that job's history with the run open. */
async function openRunFromQuery(query) {
  const runId = query.get("run");
  if (!runId) return;
  history.replaceState({}, "", "/jobs");
  let run = null;
  try {
    const d = await getJSON("/api/runs?limit=200");
    run = (d.runs || []).find((r) => r.id === runId) || null;
  } catch (err) {
    return;
  }
  if (!run) {
    toast("해당 실행 기록을 찾을 수 없습니다", "bad");
    return;
  }
  setJobsView("list", true);
  expanded.add(run.job_id);
  openRuns.add(runId);
  await loadHistory(run.job_id);
  const runs = runHistory.get(run.job_id) || [];
  if (!runs.some((r) => r.id === runId)) runHistory.set(run.job_id, [run, ...runs]);
  paintList(cachedJobs);
  const card = jobList.querySelector(`article.job[data-id="${run.job_id}"]`);
  if (card) card.scrollIntoView({ block: "start", behavior: "smooth" });
}

/** Toast for save/delete/toggle responses; false when crontab registration failed. */
function reportCron(res, okMsg, failPrefix = "저장됨") {
  lastCronSync = (res && res.crontab) || null;
  if (lastCronSync && lastCronSync.ok === false) {
    toast(`${failPrefix}, 예약 등록 실패: ${lastCronSync.error || "사유 없음"}`, "bad");
    return false;
  }
  if (okMsg) toast(okMsg, "ok");
  return true;
}

async function runNow(id) {
  running.add(id);
  results.delete(id);
  collapsedResults.delete(id);
  paintList(cachedJobs);
  try {
    const r = await post(`/api/jobs/${encodeURIComponent(id)}/run`);
    results.set(id, r);
    const st = runStatus(r);
    toast(`${STATUS_LABEL[st] || st} · ${Math.round(r.seconds || 0)}초`, r.ok ? "ok" : "bad");
  } catch (err) {
    results.set(id, { ok: false, status: "fail", error: err.message, seconds: 0 });
    toast(err.message, "bad");
  } finally {
    running.delete(id);
    await refresh();
  }
}

async function toggleHistory(id) {
  if (expanded.has(id)) {
    expanded.delete(id);
    paintList(cachedJobs);
    return;
  }
  expanded.add(id);
  runHistory.delete(id);
  paintList(cachedJobs);
  await loadHistory(id);
  paintList(cachedJobs);
}

async function toggleEnabled(id) {
  const job = cachedJobs.find((j) => j.id === id);
  if (!job) return;
  try {
    const res = await post(`/api/jobs/${encodeURIComponent(id)}`, { enabled: !job.enabled }, "PATCH");
    reportCron(res, job.enabled ? "꺼졌습니다" : "켜졌습니다", "변경됨");
  } catch (err) {
    toast(err.message, "bad");
  }
  await refresh();
}

async function deleteJob(id) {
  const job = cachedJobs.find((j) => j.id === id);
  const title = (job && job.title) || id;
  if (!window.confirm(`'${title}'을(를) 삭제할까요?`)) return;
  try {
    const res = await post(`/api/jobs/${encodeURIComponent(id)}`, null, "DELETE");
    reportCron(res, "삭제했습니다", "삭제됨");
  } catch (err) {
    toast(err.message, "bad");
  }
  await refresh();
}

jobList.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  if (btn.dataset.run) return runNow(btn.dataset.run);
  if (btn.dataset.hist) return toggleHistory(btn.dataset.hist);
  if (btn.dataset.edit) return go("/jobs/edit?id=" + encodeURIComponent(btn.dataset.edit));
  if (btn.dataset.toggle) return toggleEnabled(btn.dataset.toggle);
  if (btn.dataset.del) return deleteJob(btn.dataset.del);
  if (btn.dataset.runid) {
    const id = btn.dataset.runid;
    if (openRuns.has(id)) openRuns.delete(id);
    else openRuns.add(id);
    paintList(cachedJobs);
  }
});

jobList.addEventListener("toggle", (ev) => {
  const det = ev.target.closest("details[data-result]");
  if (!det) return;
  if (det.open) collapsedResults.delete(det.dataset.result);
  else collapsedResults.add(det.dataset.result);
  rendered.delete(det.dataset.result);
}, true);

/* ---------- edit form ---------- */

function showEditError(msg) {
  if (!editError) return;
  editError.textContent = msg || "";
  editError.hidden = !msg;
}

function alertValue(job) {
  const raw = job && job.alert;
  if (raw === false || raw === "off") return "off";
  if (raw === "fail" || raw === "ok" || raw === "always") return raw;
  return "always";
}

async function openEdit(job) {
  await reloadModels();
  await loadConnectors();
  fillEdit(job);
}

function fillEdit(job) {
  editingId = job ? job.id : null;
  effortTouched = false;
  editNumCtx = job && job.num_ctx ? Number(job.num_ctx) : 0;
  editTitle.textContent = job ? "수정" : "추가";
  showEditError("");
  document.getElementById("title").value = job ? job.title : "";
  document.getElementById("prompt").value = job ? job.prompt : "";
  if (job && job.model) {
    const missing = installedModels.includes(job.model) ? "" : job.model;
    mountModelDrop(job.model, missing);
    if (missing) showEditError(`저장된 모델 '${job.model}'이 지금은 설치되어 있지 않습니다. 설치에서 받거나 다른 모델을 고르세요.`);
  }
  DD.setValue("jobAgentDrop", (job && job.agent) || "chat");
  if (job) setEffortValue(job.effort || "medium");
  else setEffortValue(effortForModel(DD.value("jobModelDrop")));
  DD.setValue("permissionDrop", (job && job.permission) || "workspace");
  paintTools(job ? job.tools : null);
  mountConnectorsDrop(job ? job.connectors : []);
  const policy = (job && job.ram_policy) || "defer";
  DD.setValue("ramPolicyDrop", policy);
  document.getElementById("deferMax").value = String((job && job.defer_max_min) || 30);
  mountFallbackDrop((job && job.fallback_model) || "");
  document.getElementById("maxMinutes").value = String((job && job.max_minutes) || 30);
  syncPolicy(policy);
  let preset = (job && job.preset) || "save";
  if (preset === "now") preset = "save";
  if (preset === "daily_1700" || preset === "daily_0900") preset = "daily";
  if (preset === "weekdays_1700") preset = "weekdays";
  if (preset === "custom") preset = "cron";
  DD.setValue("presetDrop", preset);
  document.getElementById("whenTime").value = (job && job.time) || "17:00";
  const cronExpr = document.getElementById("cronExpr");
  if (cronExpr) cronExpr.value = (job && job.cron) || "";
  syncWhen(preset);
  DD.setValue("alertDrop", alertValue(job));
  tryOut.textContent = "";
}

function formPayload(extra) {
  return {
    title: document.getElementById("title").value,
    prompt: document.getElementById("prompt").value,
    provider: "ollama",
    model: DD.value("jobModelDrop"),
    agent: DD.value("jobAgentDrop") || "chat",
    tools: DD.selected("jobToolsDrop"),
    connectors: DD.selected("connectorsDrop"),
    preset: DD.value("presetDrop"),
    time: document.getElementById("whenTime").value,
    alert: DD.value("alertDrop"),
    effort: effortValue(),
    num_ctx: editNumCtx,
    permission: DD.value("permissionDrop"),
    cron: (document.getElementById("cronExpr") || {}).value || "",
    ram_policy: DD.value("ramPolicyDrop") || "defer",
    defer_max_min: clampInt(document.getElementById("deferMax").value, 1, 720, 30),
    fallback_model: DD.value("fallbackModelDrop") || "",
    max_minutes: clampInt(document.getElementById("maxMinutes").value, 1, 720, 30),
    ...(extra || {}),
  };
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  showEditError("");
  try {
    const payload = formPayload();
    const res = editingId
      ? await post(`/api/jobs/${encodeURIComponent(editingId)}`, payload, "PATCH")
      : await post("/api/jobs", payload);
    if (res.job && res.job.id) {
      editingId = res.job.id;
      editTitle.textContent = "수정";
    }
    if (!reportCron(res, "저장했습니다")) {
      showEditError(`저장했지만 예약 등록에 실패했습니다: ${(res.crontab && res.crontab.error) || "사유 없음"}`);
      return;
    }
    await go("/jobs");
  } catch (err) {
    showEditError(err.message);
  }
});

tryBtn.addEventListener("click", async () => {
  const prompt = document.getElementById("prompt").value;
  showEditError("");
  if (!prompt.trim()) {
    showEditError("시킬 일을 적으세요.");
    return;
  }
  tryBtn.disabled = true;
  tryOut.textContent = "실행 중…";
  try {
    showTry(await post("/api/jobs/try", formPayload({ prompt })));
  } catch (err) {
    tryOut.textContent = "";
    showEditError(String(err.message || err));
  } finally {
    tryBtn.disabled = false;
  }
});

function showTry(data) {
  const model = data.model_used || data.model || "";
  const head = data.ok ? `성공 ${data.seconds}s${model ? " · " + model : ""}` : `실패 ${data.seconds || 0}s`;
  const note = data.ram_note ? `\n${data.ram_note}` : "";
  const body = data.ok ? data.output : "";
  if (!data.ok) showEditError(data.error || "실패");
  tryOut.textContent = head + note + "\n\n" + (body || "");
}

/* ---------- settings ---------- */

const CRON_BEGIN = "# BEGIN LOCAL-LLM-DESK";
const CRON_END = "# END LOCAL-LLM-DESK";

function managedBlock(text) {
  const lines = String(text || "").split("\n");
  const a = lines.findIndex((l) => l.trim() === CRON_BEGIN);
  const b = lines.findIndex((l) => l.trim() === CRON_END);
  if (a === -1 || b === -1 || b < a) return "";
  return lines.slice(a, b + 1).join("\n");
}

function countCronJobs(block) {
  return String(block || "").split("\n").filter((l) => /^[\d*]/.test(l.trim())).length;
}

function paintCron(data) {
  const summary = document.getElementById("cronSummary");
  const pre = document.getElementById("cronInstalled");
  const block = managedBlock(data.installed);
  const nInstalled = countCronJobs(block);
  const nPreview = countCronJobs(data.preview);
  if (pre) pre.textContent = block;
  const bits = [block ? `등록된 예약 ${nInstalled}개` : "등록된 예약이 없습니다"];
  if (nInstalled !== nPreview) bits.push(`최신 설정(${nPreview}개)과 다릅니다. 작업을 저장하면 다시 동기화됩니다`);
  if (lastCronSync) bits.push(lastCronSync.ok ? "마지막 동기화 성공" : `마지막 동기화 실패: ${lastCronSync.error || ""}`);
  if (summary) summary.textContent = bits.join(" · ");
}

async function showSettings(status) {
  const alerts = (status && status.alerts) || {};
  document.getElementById("alertMacos").checked = alerts.macos !== false;
  document.getElementById("alertSound").checked = alerts.sound !== false;
  document.getElementById("alertWebhook").value = alerts.webhook || "";
  document.getElementById("alertResult").textContent = "";
  document.getElementById("dataDir").textContent = (status && status.data_dir) || "이 앱 폴더의 data/";
  try {
    paintCron(await getJSON("/api/crontab"));
  } catch (err) {
    const summary = document.getElementById("cronSummary");
    if (summary) summary.textContent = "예약 상태를 읽지 못했습니다";
  }
}

function alertsPayload() {
  return {
    macos: document.getElementById("alertMacos").checked,
    sound: document.getElementById("alertSound").checked,
    webhook: document.getElementById("alertWebhook").value.trim(),
  };
}

/** "macOS ✓ · 웹훅 ✗ 401" */
function alertResultLine(r) {
  if (r && r.error && !r.macos && !r.webhook) return `실패: ${r.error}`;
  const bits = [];
  const m = r.macos;
  if (!m) bits.push("macOS 꺼짐");
  else if (m.ok) bits.push("macOS ✓");
  else bits.push(`macOS ✗ ${m.stderr || (m.code != null ? "code " + m.code : "")}`.trim());
  const w = r.webhook;
  if (!w) bits.push("웹훅 없음");
  else if (w.ok) bits.push(`웹훅 ✓ ${w.status || ""}`.trim());
  else bits.push(`웹훅 ✗ ${w.status || w.error || ""}`.trim());
  return bits.join(" · ");
}

document.getElementById("alertSaveBtn").addEventListener("click", async () => {
  try {
    await post("/api/alerts", alertsPayload());
    toast("저장했습니다", "ok");
  } catch (err) {
    toast(err.message, "bad");
  }
});

document.getElementById("alertTestBtn").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  const out = document.getElementById("alertResult");
  btn.disabled = true;
  out.textContent = "보내는 중…";
  try {
    await post("/api/alerts", alertsPayload());
    out.textContent = alertResultLine(await post("/api/alerts/test"));
  } catch (err) {
    out.textContent = `실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});

/* ---------- wiring ---------- */

window.addEventListener("popstate", () => {
  route();
});

document.getElementById("addBtn").addEventListener("click", () => go("/jobs/new"));
document.getElementById("editBackBtn").addEventListener("click", () => go("/jobs"));
document.getElementById("viewListBtn").addEventListener("click", () => setJobsView("list"));
document.getElementById("viewTimelineBtn").addEventListener("click", () => setJobsView("timeline"));
document.getElementById("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-path]");
  if (btn) go(btn.dataset.path);
});
document.addEventListener("click", (ev) => {
  const a = ev.target.closest("a.link[href^='/']");
  if (!a) return;
  ev.preventDefault();
  go(a.getAttribute("href"));
});

async function boot() {
  mountFormDrops();
  if (window.Timeline) {
    window.Timeline.mount({
      host: document.getElementById("timelineHost"),
      chips: document.getElementById("tlRange"),
      tip: document.getElementById("tlTip"),
      onLane: (id) => go("/jobs/edit?id=" + encodeURIComponent(id)),
    });
  }
  await route();
}

document.addEventListener("DOMContentLoaded", () => {
  boot().catch((err) => {
    toast(String(err.message || err), "bad");
  });
});

setInterval(() => {
  const view = document.getElementById("viewJobs");
  if (view && !view.hidden && document.visibilityState === "visible") refresh();
}, 8000);

setInterval(() => {
  if (!activeRun) return;
  const actEl = document.getElementById("activeLine");
  if (actEl && !actEl.hidden) actEl.textContent = `실행 중: ${activeRun.title || activeRun.job_id} ${elapsedText()}`;
  const live = jobList.querySelector(`[data-live="${activeRun.job_id}"]`);
  if (live) live.textContent = `실행 중… ${elapsedText()}`;
}, 1000);
