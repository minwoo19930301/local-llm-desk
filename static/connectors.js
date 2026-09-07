/* /connectors page: registered connectors, auto-discovered candidates, manual add. */
(function () {
  const FORM_KINDS = [
    { id: "mcp_stdio", name: "MCP 명령", line: "로컬에서 명령으로 띄우는 MCP 서버", selected: true },
    { id: "mcp_http", name: "MCP 주소", line: "HTTP 주소로 연결하는 MCP 서버" },
    { id: "cli", name: "CLI 명령", line: "셸 명령 하나를 도구로 등록" },
    { id: "http", name: "HTTP API", line: "HTTP 요청 하나를 도구로 등록" },
  ];

  let mounted = false;
  let items = [];
  let discovered = [];

  function D() {
    return window.Desk;
  }

  function el(id) {
    return document.getElementById(id);
  }

  function badge(kind, text) {
    return `<span class="badge badge--${D().esc(kind)}">${D().esc(text)}</span>`;
  }

  /* ---------- load & paint ---------- */

  async function show() {
    if (!mounted) mountForm();
    await load();
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
      box.innerHTML = `<p class="legend legend--left">아직 없습니다. 아래에서 찾은 항목을 추가하거나 직접 넣으세요.</p>`;
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
    if (c.kind === "cli") return `확인됨${r.detail ? " · " + r.detail : ""}${secs}`;
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

  function paramRow() {
    return `<div class="params__row">
      <input class="gds-text-field__input" data-p="name" placeholder="이름" aria-label="파라미터 이름" />
      <input class="gds-text-field__input" data-p="desc" placeholder="설명" aria-label="파라미터 설명" />
      <label class="switch switch--tight"><input type="checkbox" data-p="req" /> 필수</label>
      <button type="button" class="gds-button gds-button--secondary gds-button--small" data-p="del" aria-label="파라미터 삭제">×</button>
    </div>`;
  }

  const FIELDS = {
    mcp_stdio: () =>
      field("cName", "이름", "예: github") +
      field("cCommand", "명령", "예: npx") +
      field("cArgs", "인자 (공백 구분)", "예: -y @modelcontextprotocol/server-github") +
      area("cEnv", "환경 변수 (이름=값, 한 줄에 하나)", "GITHUB_TOKEN=ghp_..."),
    mcp_http: () =>
      field("cName", "이름", "예: notion") +
      field("cUrl", "URL", "https://mcp.example.com/mcp", "url") +
      area("cHeaders", "헤더 (이름=값, 한 줄에 하나)", "Authorization=Bearer ..."),
    cli: () =>
      field("cName", "이름", "예: gh_issues") +
      field("cDesc", "설명", "모델에게 보여줄 한 줄 설명") +
      field("cTemplate", "명령 템플릿", "gh issue list --repo {repo} --limit {limit}") +
      `<label class="switch"><input type="checkbox" id="cReadonly" /> 읽기 전용 (읽기 권한에서도 허용)</label>` +
      paramsTable(),
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
      params[name] = {
        type: "string",
        description: row.querySelector('[data-p="desc"]').value.trim(),
        required: row.querySelector('[data-p="req"]').checked,
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
      return { kind: "mcp", transport: "stdio", name, command, args: val("cArgs").split(/\s+/).filter(Boolean), env, env_keys: Object.keys(env) };
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
      return { kind: "cli", name, description: val("cDesc"), command_template, params: collectParams(), readonly: !!(el("cReadonly") && el("cReadonly").checked), timeout: 60 };
    }
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
      paintFields(kind);
      await load();
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  window.Connectors = { show };
})();
