/* /connectors page: registered connectors, auto-discovered candidates, manual add. */
(function () {
  const FORM_KINDS = [
    { id: "mcp_stdio", name: "MCP 명령", line: "로컬에서 명령으로 띄우는 MCP 서버", selected: true },
    { id: "mcp_http", name: "MCP 주소", line: "HTTP 주소로 연결하는 MCP 서버" },
    { id: "cli", name: "CLI 명령", line: "셸 명령 하나를 도구로 등록" },
    { id: "http", name: "HTTP API", line: "HTTP 요청 하나를 도구로 등록" },
    { id: "skill", name: "스킬", line: "로컬 SKILL.md를 작업 지침으로 등록" },
  ];

  let mounted = false;
  let items = [];
  let discovered = [];
  let mode = "ai";
  let reviewBase = null;
  let operationData = null;
  let specInput = null;
  let busy = false;
  let modelReady = false;

  function D() {
    return window.Desk;
  }

  function el(id) {
    return document.getElementById(id);
  }

  function badge(kind, text) {
    return `<span class="badge badge--${D().esc(kind)}">${D().esc(text)}</span>`;
  }

  function message(id, text, error) {
    const node = el(id);
    node.textContent = text || "";
    node.hidden = !text;
    node.classList.toggle("is-error", !!error);
  }

  function setMode(next) {
    if (busy) return;
    mode = next;
    reviewBase = null;
    document.querySelectorAll("[data-con-mode]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.conMode === mode)));
    el("conAIPanel").hidden = mode !== "ai";
    el("conOpenAPIPanel").hidden = mode !== "openapi";
    el("conReview").hidden = mode !== "manual";
    el("conReviewTitle").textContent = mode === "manual" ? "직접 입력" : "연동 초안";
    el("conReviewSummary").textContent = "";
    paintFields(DD.value("conKindDrop") || "mcp_stdio");
  }

  function setBusy(value) {
    busy = value;
    document.querySelectorAll('.connector-builder button, .connector-builder input, .connector-builder textarea, .connector-builder select').forEach(node => { node.disabled = value; });
    if (!value) {
      el("conDraftBtn").disabled = !modelReady;
      el("conReplyBtn").disabled = !modelReady;
      updateOperation();
    }
  }

  async function loadOptions() {
    try {
      const options = await D().getJSON("/api/connectors/options");
      const selected = el("conAIModel").value;
      const models = options.models || [];
      el("conAIModel").replaceChildren(...models.map(name => new Option(name, name)));
      modelReady = models.length > 0 && options.provider_ready;
      if (!models.length) el("conAIModel").add(new Option("설치된 로컬 모델 없음", ""));
      else if (models.includes(selected)) el("conAIModel").value = selected;
      el("conAIAvailability").textContent = modelReady
        ? "초안을 만드는 동안만 Ollama를 사용합니다. 기존 연동의 로그인 정보는 AI에 전달하지 않습니다."
        : "AI 초안에는 Ollama와 로컬 모델이 필요합니다. 설치 화면에서 준비하거나 직접 입력·OpenAPI 가져오기를 사용하세요.";
    } catch (err) {
      modelReady = false;
      el("conAIAvailability").textContent = err.message;
    }
    el("conDraftBtn").disabled = busy || !modelReady;
    el("conReplyBtn").disabled = busy || !modelReady;
  }

  function fillReview(draft, summary) {
    reviewBase = structuredClone(draft);
    const kind = draft.kind === "mcp" ? (draft.transport === "http" ? "mcp_http" : "mcp_stdio") : draft.kind;
    if (!FIELDS[kind]) throw new Error("지원하지 않는 연동 종류입니다.");
    DD.setValue("conKindDrop", kind);
    paintFields(kind);
    const values = { cName: draft.name, cDesc: draft.description, cCommand: draft.command,
      cArgs: JSON.stringify(draft.args || []), cCwd: draft.cwd, cUrl: draft.url,
      cTemplate: draft.command_template, cTimeout: draft.timeout || 60,
      cUrlTemplate: draft.url_template, cBody: draft.body_template, cPath: draft.path,
      cEnv: Object.entries(draft.env || {}).map(([k,v]) => `${k}=${v}`).join("\n"),
      cHeaders: Object.entries(draft.headers || {}).map(([k,v]) => `${k}=${v}`).join("\n") };
    Object.entries(values).forEach(([id, value]) => { if (el(id)) el(id).value = value == null ? "" : value; });
    if (el("cReadonly")) el("cReadonly").checked = draft.readonly === true;
    if (kind === "http") DD.setValue("cMethodDrop", draft.method || "GET");
    if (el("conParamRows")) el("conParamRows").innerHTML = Object.entries(draft.params || {}).map(([name,spec]) => paramRow(name,spec)).join("") || paramRow();
    el("conReviewTitle").textContent = "초안을 확인해 주세요";
    el("conReviewSummary").textContent = summary || "필요하면 아래 내용을 수정한 뒤 연동을 추가하세요.";
    el("conReview").hidden = false;
  }

  async function makeDraft(followup) {
    if (busy || !modelReady) return;
    let prompt = val("conAIPrompt");
    if (followup) {
      const reply = val("conAIReply");
      if (!reply) return message("conAIMessage", "추가 정보나 수정 요청을 적어 주세요.", true);
      prompt += "\n\n추가 정보 / 수정 요청: " + reply;
    }
    if (!prompt || prompt.length > 6000) return message("conAIMessage", "요청을 1~6000자로 적어 주세요.", true);
    el("conAIPrompt").value = prompt;
    el("conReview").hidden = true;
    el("conFollowup").hidden = true;
    message("conAIMessage", "로컬 모델이 초안을 만들고 있습니다…");
    setBusy(true);
    try {
      const result = await D().post("/api/connectors/draft", { prompt, model: val("conAIModel"), context: val("conAIContext") });
      const questions = result.questions || [];
      const lines = [result.summary, ...questions.map(q => "• " + q), ...(result.warnings || []).map(w => "• " + w)].filter(Boolean);
      message("conAIMessage", lines.join("\n"));
      el("conFollowup").hidden = false;
      el("conAIReply").value = "";
      if (result.draft && !questions.length) fillReview(result.draft, result.summary);
    } catch (err) {
      message("conAIMessage", err.message, true);
    } finally {
      setBusy(false);
    }
  }

  function updateOperation() {
    const row = (operationData || []).find(op => op.id === el("conOperation").value);
    el("conSpecDraftBtn").disabled = busy || !row || !row.supported;
    if (row && !row.supported) message("conSpecMessage", (row.issues || ["이 요청은 아직 지원하지 않습니다."]).join("\n"), true);
    else if (row) message("conSpecMessage", [row.summary, ...(row.warnings || [])].filter(Boolean).join("\n"));
  }

  async function readSpec() {
    if (busy) return;
    const body = { source_url: val("conSpecUrl"), spec_text: val("conSpecText") };
    if (!body.source_url && !body.spec_text) return message("conSpecMessage", "OpenAPI JSON 또는 문서 URL을 입력하세요.", true);
    el("conOperations").hidden = true;
    el("conReview").hidden = true;
    message("conSpecMessage", "문서에서 연결할 수 있는 요청을 찾고 있습니다…");
    setBusy(true);
    try {
      const result = await D().post("/api/connectors/openapi", body);
      operationData = result.operations || [];
      specInput = body;
      el("conOperation").replaceChildren(...operationData.map(op => new Option(`${op.method} ${op.path}${op.summary ? " · " + op.summary : ""}${op.supported ? "" : " (지원 안 됨)"}`, op.id)));
      el("conOperations").hidden = !operationData.length;
      message("conSpecMessage", [...(result.questions || []), ...(result.warnings || [])].join("\n") || "요청을 찾았습니다.");
    } catch (err) {
      operationData = null;
      message("conSpecMessage", err.message, true);
    } finally { setBusy(false); }
  }

  function useOperation() {
    const row = (operationData || []).find(op => op.id === el("conOperation").value);
    if (!specInput || !row || !row.supported || !row.connector) return;
    fillReview(row.connector, [row.summary, ...(row.warnings || [])].filter(Boolean).join("\n"));
  }

  /* ---------- load & paint ---------- */

  async function show() {
    if (!mounted) mountForm();
    await Promise.all([load(), loadOptions()]);
  }

  async function load() {
    try {
      const data = await D().getJSON("/api/connectors");
      items = data.connectors || [];
      discovered = data.discovered || [];
    } catch (err) {
      items = [];
      discovered = [];
      el("conList").innerHTML = `<p class="legend legend--left">${D().esc(err.message)}</p>`;
      el("conDiscovered").innerHTML = "";
      return;
    }
    paintRegistered();
    paintDiscovered();
  }

  function detailLine(c) {
    if (c.kind === "mcp") return c.transport === "http" ? c.url || "" : [c.command, ...(c.args || [])].join(" ");
    if (c.kind === "cli") return c.command_template || "";
    if (c.kind === "http") return `${c.method || "GET"} ${c.url_template || ""}`;
    if (c.kind === "skill") return c.path || "";
    return "";
  }

  function rowHtml(c) {
    const esc = D().esc;
    const tools = c.kind === "mcp" && Array.isArray(c.tools_cache) && c.tools_cache.length ? `<span class="muted">도구 ${c.tools_cache.length}개</span>` : "";
    const off = c.enabled === false;
    return `<div class="connector-row${off ? " is-off" : ""}" data-id="${esc(c.id)}">
      <div class="connector-row__main">
        <div class="connector-row__head">
          <b>${esc(c.name)}</b>
          ${badge(c.kind, D().KIND_LABEL[c.kind] || c.kind)}
          <span class="badge badge--src">${esc(D().SOURCE_LABEL[c.source] || c.source || "")}</span>
          ${tools}
        </div>
        ${c.note || c.description ? `<p class="connector-row__note">${esc(c.note || c.description)}</p>` : ""}
        <p class="connector-row__detail mono">${esc(detailLine(c))}</p>
        <p class="connector-row__result" id="conRes-${esc(c.id)}" hidden></p>
      </div>
      <div class="connector-row__actions">
        <button type="button" class="gds-button gds-button--secondary gds-button--small" data-test="${esc(c.id)}" aria-label="${esc(c.name)} 테스트">테스트</button>
        <button type="button" class="gds-button gds-button--secondary gds-button--small toggle${off ? "" : " is-on"}" data-toggle="${esc(c.id)}" aria-pressed="${off ? "false" : "true"}" aria-label="${esc(c.name)} ${off ? "켜기" : "끄기"}">${off ? "끔" : "켬"}</button>
        <button type="button" class="gds-button gds-button--secondary gds-button--small gds-button--danger" data-del="${esc(c.id)}" aria-label="${esc(c.name)} 삭제">삭제</button>
      </div>
    </div>`;
  }

  function paintRegistered() {
    const box = el("conList");
    if (!items.length) {
      box.innerHTML = `<p class="legend legend--left">아직 없습니다. 위에서 필요한 연동을 추가해 보세요.</p>`;
      return;
    }
    box.innerHTML = items.map(rowHtml).join("");
  }

  function paintDiscovered() {
    const box = el("conDiscovered");
    const esc = D().esc;
    if (!discovered.length) {
      box.innerHTML = `<p class="legend legend--left">새로 찾은 연동이 없습니다.</p>`;
      return;
    }
    const groups = new Map();
    discovered.forEach((c) => {
      const key = c.source || "manual";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    });
    const parts = [];
    groups.forEach((list, source) => {
      parts.push(`<p class="group-title">${esc(D().SOURCE_LABEL[source] || source)} <span class="muted">${list.length}</span></p>`);
      list.forEach((c) => {
        parts.push(`<div class="connector-row connector-row--found">
          <div class="connector-row__main">
            <div class="connector-row__head">
              <b>${esc(c.name)}</b>
              ${badge(c.kind, D().KIND_LABEL[c.kind] || c.kind)}
            </div>
            ${c.note || c.description ? `<p class="connector-row__note">${esc(c.note || c.description)}</p>` : ""}
            <p class="connector-row__detail mono">${esc(detailLine(c))}</p>
          </div>
          <div class="connector-row__actions">
            <button type="button" class="gds-button gds-button--small" data-import="${esc(c.candidate_key)}" aria-label="${esc(c.name)} 추가">추가</button>
          </div>
        </div>`);
      });
    });
    box.innerHTML = parts.join("");
  }

  /* ---------- row actions ---------- */

  function testLine(c, r) {
    if (r.error && !r.ok) return `실패: ${r.error}`;
    if (!r.ok) return `실패${r.detail ? ": " + r.detail : ""}`;
    const secs = r.seconds != null ? ` · ${Number(r.seconds).toFixed(1)}초` : "";
    if (c.kind === "mcp") {
      const names = (r.tools || []).map((t) => t.name || t).filter(Boolean);
      return `도구 ${names.length}개${names.length ? ": " + names.join(", ") : ""}${secs}`;
    }
    if (c.kind === "cli") return `실행 파일 찾음${r.detail ? " · " + r.detail : ""} · 로그인과 실제 실행은 별도 확인${secs}`;
    if (c.kind === "http") return `응답 ${r.status != null ? r.status : "확인됨"}${secs}`;
    if (c.kind === "skill") return `${r.name || "스킬"}${r.description ? " · " + r.description : ""}`;
    return "확인됨";
  }

  async function test(id, btn) {
    const c = items.find((i) => i.id === id);
    const out = el(`conRes-${id}`);
    if (!c || !out) return;
    btn.disabled = true;
    out.hidden = false;
    out.className = "connector-row__result";
    out.textContent = "확인하는 중…";
    try {
      const r = await D().post(`/api/connectors/${encodeURIComponent(id)}/test`);
      out.textContent = testLine(c, r);
      out.className = "connector-row__result " + (r.ok ? "ok" : "bad");
      if (c.kind === "mcp" && r.ok) await load();
    } catch (err) {
      out.textContent = `실패: ${err.message}`;
      out.className = "connector-row__result bad";
    } finally {
      btn.disabled = false;
    }
  }

  async function toggle(id) {
    const c = items.find((i) => i.id === id);
    if (!c) return;
    try {
      await D().post(`/api/connectors/${encodeURIComponent(id)}`, { enabled: c.enabled === false }, "PATCH");
      D().toast(c.enabled === false ? "켜졌습니다" : "꺼졌습니다", "ok");
    } catch (err) {
      D().toast(err.message, "bad");
    }
    await load();
  }

  async function remove(id) {
    const c = items.find((i) => i.id === id);
    if (!c) return;
    if (!window.confirm(`'${c.name}' 연동을 삭제할까요? 이 연동을 쓰는 자동화에서는 빠집니다.`)) return;
    try {
      await D().post(`/api/connectors/${encodeURIComponent(id)}`, null, "DELETE");
      D().toast("삭제했습니다", "ok");
    } catch (err) {
      D().toast(err.message, "bad");
    }
    await load();
  }

  async function importCandidate(key, btn) {
    btn.disabled = true;
    try {
      const r = await D().post("/api/connectors", { import: key });
      D().toast(`'${(r.connector && r.connector.name) || "연동"}' 추가했습니다`, "ok");
      await load();
    } catch (err) {
      D().toast(err.message, "bad");
      btn.disabled = false;
    }
  }

  function onListClick(ev) {
    const btn = ev.target.closest("button");
    if (!btn) return;
    if (btn.dataset.test) return test(btn.dataset.test, btn);
    if (btn.dataset.toggle) return toggle(btn.dataset.toggle);
    if (btn.dataset.del) return remove(btn.dataset.del);
    if (btn.dataset.import) return importCandidate(btn.dataset.import, btn);
  }

  /* ---------- manual add form ---------- */

  function field(id, label, placeholder, type) {
    return `<div class="gds-text-field gds-text-field--small field-labeled">
      <label class="field-labeled__label" for="${id}">${label}</label>
      <input id="${id}" class="gds-text-field__input" type="${type || "text"}" placeholder="${D().esc(placeholder || "")}" />
    </div>`;
  }

  function area(id, label, placeholder) {
    return `<div class="field-labeled field-labeled--area">
      <label class="field-labeled__label" for="${id}">${label}</label>
      <textarea id="${id}" class="note note--short" placeholder="${D().esc(placeholder || "")}"></textarea>
    </div>`;
  }

  function paramsTable() {
    return `<div class="params" id="conParams">
      <p class="field-labeled__label">파라미터</p>
      <div class="params__rows" id="conParamRows"></div>
      <button type="button" class="gds-button gds-button--secondary gds-button--small" id="conParamAdd">+ 파라미터</button>
    </div>`;
  }

  function paramRow(name, spec) {
    const esc = D().esc;
    spec = spec || {};
    return `<div class="params__row">
      <input class="gds-text-field__input" data-p="name" placeholder="이름" aria-label="파라미터 이름" value="${esc(name || "")}" />
      <input class="gds-text-field__input" data-p="desc" placeholder="설명" aria-label="파라미터 설명" value="${esc(spec.description || "")}" />
      <select class="gds-text-field__input" data-p="type" aria-label="파라미터 타입">${["string", "integer", "boolean"].map(t => `<option value="${t}"${(spec.type || "string") === t ? " selected" : ""}>${{string:"문자",integer:"정수",boolean:"참/거짓"}[t]}</option>`).join("")}</select>
      <input class="gds-text-field__input" data-p="default" placeholder="기본값 (선택)" aria-label="파라미터 기본값" value="${esc(spec.default == null ? "" : String(spec.default))}" />
      <label class="switch switch--tight"><input type="checkbox" data-p="req"${spec.required ? " checked" : ""} /> 필수</label>
      <button type="button" class="gds-button gds-button--secondary gds-button--small" data-p="del" aria-label="파라미터 삭제">×</button>
    </div>`;
  }

  const FIELDS = {
    mcp_stdio: () =>
      field("cName", "이름", "예: github") +
      field("cCommand", "명령", "예: npx") +
      field("cArgs", "인자 (JSON 배열 또는 공백 구분)", '["-y", "@example/server"]') +
      field("cCwd", "작업 폴더 (선택)", "/경로/서버") +
      area("cEnv", "환경 변수 (이름=값, 한 줄에 하나)", "GITHUB_TOKEN=ghp_..."),
    mcp_http: () =>
      field("cName", "이름", "예: notion") +
      field("cUrl", "URL", "https://mcp.example.com/mcp", "url") +
      area("cHeaders", "헤더 (이름=값, 한 줄에 하나)", "Authorization=Bearer ..."),
    cli: () =>
      field("cName", "이름", "예: gh_issues") +
      field("cDesc", "설명", "모델에게 보여줄 한 줄 설명") +
      field("cTemplate", "명령 템플릿", "gh issue list --repo {repo} --limit {limit}") +
      field("cTimeout", "실행 제한 (초)", "60", "number") +
      `<label class="switch"><input type="checkbox" id="cReadonly" /> 읽기 전용 (읽기 권한에서도 허용)</label>` +
      paramsTable(),
    skill: () => field("cName", "이름", "예: writing") +
      field("cPath", "SKILL.md 경로", "/Users/사용자/.codex/skills/example/SKILL.md") +
      field("cDesc", "설명", "작업에 추가할 지침"),
    http: () =>
      field("cName", "이름", "예: weather") +
      field("cDesc", "설명", "모델에게 보여줄 한 줄 설명") +
      `<div id="cMethodDrop"></div>` +
      field("cUrlTemplate", "URL 템플릿", "https://api.example.com/v1/{city}", "url") +
      area("cHeaders", "헤더 (이름=값, 한 줄에 하나 · 값에 ${ENV} 가능)", "Authorization=Bearer ${API_KEY}") +
      area("cBody", "본문 템플릿 (POST)", '{"q": "{query}"}') +
      paramsTable(),
  };

  function mountForm() {
    mounted = true;
    DD.mount("conKindDrop", { label: "종류", items: FORM_KINDS, onChange: paintFields });
    paintFields(DD.value("conKindDrop") || "mcp_stdio");
    el("conSaveBtn").addEventListener("click", save);
    document.querySelectorAll("[data-con-mode]").forEach(button => button.addEventListener("click", () => setMode(button.dataset.conMode)));
    document.querySelectorAll("[data-con-example]").forEach(button => button.addEventListener("click", () => {
      el("conAIPrompt").value = {
        CLI: "gh로 특정 저장소의 열린 이슈를 조회하는 CLI 연동을 붙여줘. 저장소 이름 repo와 개수 limit을 입력받고 limit 기본값은 3으로 해줘.",
        API: "HTTP API를 연결하고 싶어. GET https://api.example.com/posts 로 최근 글을 조회하고 limit 파라미터 기본값은 3으로 해줘.",
        MCP: "MCP 서버를 장착하고 싶어. 연결에 어떤 정보가 필요한지 물어봐 줘.",
        skill: "로컬 SKILL.md를 작업 지침으로 붙이고 싶어. 필요한 경로를 물어봐 줘.",
      }[button.dataset.conExample];
      el("conAIPrompt").focus();
    }));
    el("conDraftBtn").addEventListener("click", () => makeDraft(false));
    el("conReplyBtn").addEventListener("click", () => makeDraft(true));
    el("conSpecReadBtn").addEventListener("click", readSpec);
    el("conSpecDraftBtn").addEventListener("click", useOperation);
    el("conOperation").addEventListener("change", updateOperation);
    ["conSpecUrl", "conSpecText"].forEach(id => el(id).addEventListener("input", () => { specInput = null; el("conOperations").hidden = true; }));
    el("conList").addEventListener("click", onListClick);
    el("conDiscovered").addEventListener("click", onListClick);
    el("conFields").addEventListener("click", (ev) => {
      const t = ev.target.closest("button");
      if (!t) return;
      if (t.id === "conParamAdd") el("conParamRows").insertAdjacentHTML("beforeend", paramRow());
      else if (t.dataset.p === "del") t.closest(".params__row").remove();
    });
  }

  function paintFields(kind) {
    const box = el("conFields");
    box.innerHTML = (FIELDS[kind] || FIELDS.mcp_stdio)();
    showError("");
    if (kind === "http") {
      DD.mount("cMethodDrop", {
        label: "메서드",
        items: [
          { id: "GET", name: "GET", selected: true },
          { id: "POST", name: "POST" },
        ],
      });
    }
    const rows = el("conParamRows");
    if (rows) rows.insertAdjacentHTML("beforeend", paramRow());
  }

  function showError(msg) {
    const e = el("conError");
    e.textContent = msg || "";
    e.hidden = !msg;
  }

  function val(id) {
    const node = el(id);
    return node ? node.value.trim() : "";
  }

  /** "K=V" lines → object; blank lines ignored. */
  function kvLines(text) {
    const out = {};
    String(text || "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .forEach((line) => {
        const i = line.indexOf("=");
        if (i <= 0) return;
        out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      });
    return out;
  }

  function collectParams() {
    const params = {};
    document.querySelectorAll("#conParamRows .params__row").forEach((row) => {
      const name = row.querySelector('[data-p="name"]').value.trim();
      if (!name) return;
      if (params[name]) throw new Error(`파라미터 이름이 중복됩니다: ${name}`);
      const type = row.querySelector('[data-p="type"]').value;
      const raw = row.querySelector('[data-p="default"]').value;
      let defaultValue = raw || null;
      if (raw !== "" && type === "integer") {
        if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw new Error(`${name} 기본값은 정수여야 합니다.`);
        defaultValue = Number(raw);
      }
      if (raw !== "" && type === "boolean") {
        if (!["true", "false"].includes(raw)) throw new Error(`${name} 기본값은 true 또는 false여야 합니다.`);
        defaultValue = raw === "true";
      }
      if (raw === "" && reviewBase && reviewBase.params && reviewBase.params[name] && reviewBase.params[name].default === "") defaultValue = "";
      params[name] = {
        type,
        description: row.querySelector('[data-p="desc"]').value.trim(),
        required: row.querySelector('[data-p="req"]').checked,
        default: defaultValue,
      };
    });
    return params;
  }

  function payload(kind) {
    const name = val("cName");
    if (!name) throw new Error("이름을 적으세요.");
    if (kind === "mcp_stdio") {
      const command = val("cCommand");
      if (!command) throw new Error("명령을 적으세요.");
      const env = kvLines(val("cEnv"));
      const rawArgs = val("cArgs");
      const args = rawArgs.startsWith("[") ? JSON.parse(rawArgs) : rawArgs.split(/\s+/).filter(Boolean);
      if (!Array.isArray(args) || args.some(a => typeof a !== "string")) throw new Error("인자는 문자열 JSON 배열이어야 합니다.");
      return { kind: "mcp", transport: "stdio", name, command, args, cwd: val("cCwd") || null, env, env_keys: Object.keys(env) };
    }
    if (kind === "mcp_http") {
      const url = val("cUrl");
      if (!/^https?:\/\//.test(url)) throw new Error("URL은 http(s)://로 시작해야 합니다.");
      const headers = kvLines(val("cHeaders"));
      return { kind: "mcp", transport: "http", name, url, headers, headers_keys: Object.keys(headers) };
    }
    if (kind === "cli") {
      const command_template = val("cTemplate");
      if (!command_template) throw new Error("명령 템플릿을 적으세요.");
      const timeout = Number(val("cTimeout") || 60);
      if (!Number.isInteger(timeout) || timeout < 1 || timeout > 3600) throw new Error("실행 제한은 1~3600초여야 합니다.");
      return { kind: "cli", name, description: val("cDesc"), command_template, params: collectParams(), readonly: !!(el("cReadonly") && el("cReadonly").checked), timeout };
    }
    if (kind === "skill") return { kind: "skill", name, path: val("cPath"), description: val("cDesc") };
    const url_template = val("cUrlTemplate");
    if (!/^https?:\/\//.test(url_template)) throw new Error("URL 템플릿은 http(s)://로 시작해야 합니다.");
    return {
      kind: "http",
      name,
      description: val("cDesc"),
      method: DD.value("cMethodDrop") || "GET",
      url_template,
      headers: kvLines(val("cHeaders")),
      body_template: val("cBody"),
      params: collectParams(),
    };
  }

  async function save() {
    const kind = DD.value("conKindDrop") || "mcp_stdio";
    const btn = el("conSaveBtn");
    showError("");
    let body;
    try {
      body = payload(kind);
    } catch (err) {
      showError(err.message);
      return;
    }
    btn.disabled = true;
    try {
      const r = await D().post("/api/connectors", body);
      D().toast(`'${(r.connector && r.connector.name) || body.name}' 추가했습니다`, "ok");
      reviewBase = null;
      paintFields(kind);
      if (mode !== "manual") el("conReview").hidden = true;
      await load();
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  window.Connectors = { show };
})();
