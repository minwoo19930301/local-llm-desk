/* Timeline view for /jobs: past runs, upcoming runs, RAM conflicts — hand-built SVG, no libraries. */
(function () {
  const RANGES = [
    { hours: 6, label: "6시간" },
    { hours: 24, label: "24시간" },
    { hours: 168, label: "7일" },
  ];
  const COLOR = {
    ok: "#015f00",
    fail: "#c62828",
    skipped: "#9e9e9e",
    aborted: "#9e9e9e",
    deferred_timeout: "#ef6c00",
  };
  const LS_KEY = "desk.tlHours";
  const AXIS_H = 28;
  const LANE_H = 48;
  const LANE_H_NARROW = 72;
  const LABEL_W = 176;
  const MIN_BAR = 3;

  let host = null;
  let chips = null;
  let tip = null;
  let onLane = null;
  let hours = readHours();
  let data = null;
  let loading = false;

  function D() {
    return window.Desk;
  }

  function readHours() {
    try {
      const n = Number(localStorage.getItem(LS_KEY));
      return RANGES.some((r) => r.hours === n) ? n : 24;
    } catch (err) {
      return 24;
    }
  }

  function storeHours(n) {
    try {
      localStorage.setItem(LS_KEY, String(n));
    } catch (err) {
      /* storage unavailable */
    }
  }

  function mount(opts) {
    host = opts.host;
    chips = opts.chips;
    tip = opts.tip;
    onLane = opts.onLane || null;
    paintChips();
    host.addEventListener("click", onHostClick);
    host.addEventListener("keydown", onHostKeydown);
    host.addEventListener("pointerover", onHover);
    host.addEventListener("pointerout", onLeave);
    document.addEventListener("click", (ev) => {
      if (!host.contains(ev.target)) hideTip();
    });
    let timer = 0;
    window.addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(draw, 120);
    });
  }

  function paintChips() {
    if (!chips) return;
    chips.innerHTML = RANGES.map(
      (r) =>
        `<button type="button" class="chip chip--btn${r.hours === hours ? " is-on" : ""}" data-hours="${r.hours}" aria-pressed="${r.hours === hours ? "true" : "false"}">${r.label}</button>`,
    ).join("");
    chips.onclick = (ev) => {
      const b = ev.target.closest("[data-hours]");
      if (!b) return;
      hours = Number(b.dataset.hours);
      storeHours(hours);
      paintChips();
      refresh();
    };
  }

  async function refresh() {
    if (!host || loading) return;
    loading = true;
    try {
      data = await D().getJSON(`/api/timeline?hours=${hours}`);
      draw();
    } catch (err) {
      if (!data) host.innerHTML = `<p class="legend">${D().esc(err.message)}</p>`;
    } finally {
      loading = false;
    }
  }

  /* ---------- drawing ---------- */

  function tickStepMs(rangeMs) {
    const h = 3600e3;
    if (rangeMs <= 7 * h) return h;
    if (rangeMs <= 30 * h) return 3 * h;
    return 24 * h;
  }

  function tickLabel(t, stepMs) {
    const d = new Date(t);
    const day = ["일", "월", "화", "수", "목", "금", "토"][d.getDay()];
    if (stepMs >= 24 * 3600e3 || (d.getHours() === 0 && d.getMinutes() === 0)) return `${d.getMonth() + 1}/${d.getDate()} (${day})`;
    const h = d.getHours();
    const ampm = h < 12 ? "오전" : "오후";
    const h12 = h % 12 === 0 ? 12 : h % 12;
    return `${ampm} ${h12}시`;
  }

  function firstTick(fromMs, stepMs) {
    const d = new Date(fromMs);
    d.setMinutes(0, 0, 0);
    if (stepMs >= 24 * 3600e3) d.setHours(0);
    else d.setHours(Math.floor(d.getHours() / (stepMs / 3600e3)) * (stepMs / 3600e3));
    let t = d.getTime();
    while (t < fromMs) t += stepMs;
    return t;
  }

  function draw() {
    if (!host || !data) return;
    const esc = D().esc;
    const lanes = data.lanes || [];
    if (!lanes.length) {
      host.innerHTML = `<p class="legend">아직 예약된 작업이 없습니다. 추가로 넣으면 여기에 시간축으로 보입니다.</p>`;
      return;
    }
    const width = Math.max(320, host.clientWidth || 320);
    const narrow = width < 600;
    const labelW = narrow ? 0 : LABEL_W;
    const laneH = narrow ? LANE_H_NARROW : LANE_H;
    const plotX = labelW;
    const plotW = width - labelW - 8;
    const from = Date.parse(data.from);
    const to = Date.parse(data.to);
    const now = Date.parse(data.now) || Date.now();
    const range = Math.max(1, to - from);
    const pxPerMs = plotW / range;
    const x = (t) => plotX + (t - from) * pxPerMs;
    const height = AXIS_H + lanes.length * laneH + 8;
    const parts = [];

    parts.push(`<svg class="tl-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="실행 타임라인">`);

    // conflicts shading
    (data.conflicts || []).forEach((c) => {
      const at = Date.parse(c.at);
      if (!at) return;
      const secs = Math.max(...(c.job_ids || []).map((id) => laneById(lanes, id)?.avg_seconds || 90), 90);
      const w = Math.max(6, secs * 1000 * pxPerMs);
      const title = `겹침: ${(c.job_ids || []).map((id) => laneById(lanes, id)?.title || id).join(", ")}`;
      parts.push(`<rect class="tl-conflict" x="${x(at).toFixed(1)}" y="${AXIS_H}" width="${w.toFixed(1)}" height="${lanes.length * laneH}" data-tip="${esc(title)}"></rect>`);
    });

    // axis
    const step = tickStepMs(range);
    for (let t = firstTick(from, step); t <= to; t += step) {
      const px = x(t);
      parts.push(`<line class="tl-grid" x1="${px.toFixed(1)}" y1="${AXIS_H - 6}" x2="${px.toFixed(1)}" y2="${height - 8}"></line>`);
      if (px + 64 <= width) parts.push(`<text class="tl-tick" x="${(px + 3).toFixed(1)}" y="14">${esc(tickLabel(t, step))}</text>`);
    }

    // lanes
    lanes.forEach((lane, i) => {
      const top = AXIS_H + i * laneH;
      const barY = narrow ? top + 40 : top + 14;
      const barH = narrow ? 18 : 20;
      const off = lane.enabled === false;
      parts.push(`<g class="tl-lane${off ? " is-off" : ""}" data-job="${esc(lane.job_id)}">`);
      parts.push(`<rect class="tl-lane__bg" x="0" y="${top}" width="${width}" height="${laneH}"></rect>`);
      parts.push(`<line class="tl-grid" x1="0" y1="${top + laneH}" x2="${width}" y2="${top + laneH}"></line>`);
      const sub = `${lane.model || ""}${lane.ram_need_gb != null ? ` · 약 ${D().fmtGb(lane.ram_need_gb)}GB` : ""}`;
      const laneAria = `${esc(lane.title)} 수정`;
      if (narrow) {
        parts.push(`<text class="tl-title" x="8" y="${top + 18}" tabindex="0" role="button" aria-label="${laneAria}">${esc(lane.title)}</text>`);
        parts.push(`<text class="tl-sub" x="8" y="${top + 33}">${esc(sub)}</text>`);
      } else {
        parts.push(`<text class="tl-title" x="8" y="${top + 21}" tabindex="0" role="button" aria-label="${laneAria}">${esc(clip(lane.title, 22))}</text>`);
        parts.push(`<text class="tl-sub" x="8" y="${top + 36}">${esc(clip(sub, 30))}</text>`);
      }
      (lane.runs || []).forEach((r) => {
        const at = Date.parse(r.at);
        if (!at || at > to) return;
        const st = r.status || (r.ok ? "ok" : "fail");
        const w = Math.max(MIN_BAR, (r.seconds || 0) * 1000 * pxPerMs);
        const label = `${D().fmtTime(r.at)} · ${D().STATUS_LABEL[st] || st} · ${Math.round(r.seconds || 0)}초 · ${r.model_used || lane.model || ""}`;
        parts.push(`<rect class="tl-run" x="${x(at).toFixed(1)}" y="${barY}" width="${w.toFixed(1)}" height="${barH}" rx="2" fill="${COLOR[st] || COLOR.fail}" data-tip="${esc(label)}" tabindex="0" aria-label="${esc(label)}"></rect>`);
      });
      (lane.upcoming || []).forEach((iso) => {
        const at = Date.parse(iso);
        if (!at || at < from || at > to) return;
        const w = Math.max(MIN_BAR, (lane.avg_seconds || 90) * 1000 * pxPerMs);
        const label = `예정 · ${D().fmtTime(iso)} · 약 ${Math.round(lane.avg_seconds || 90)}초 · ${lane.model || ""}`;
        parts.push(`<rect class="tl-next" x="${x(at).toFixed(1)}" y="${barY}" width="${w.toFixed(1)}" height="${barH}" rx="2" data-tip="${esc(label)}" tabindex="0" aria-label="${esc(label)}"></rect>`);
      });
      parts.push(`</g>`);
    });

    // now line
    const nx = x(now);
    parts.push(`<line class="tl-now" x1="${nx.toFixed(1)}" y1="${AXIS_H - 10}" x2="${nx.toFixed(1)}" y2="${height - 8}"></line>`);
    parts.push(`<text class="tl-now-label" x="${(nx + 4).toFixed(1)}" y="${AXIS_H + 2}">지금</text>`);
    parts.push(`</svg>`);
    host.innerHTML = parts.join("");
    hideTip();
  }

  function laneById(lanes, id) {
    return lanes.find((l) => l.job_id === id);
  }

  function clip(s, n) {
    const t = String(s || "");
    return t.length > n ? t.slice(0, n - 1) + "…" : t;
  }

  /* ---------- interaction ---------- */

  function showTip(target, text) {
    if (!tip || !text) return;
    const hostBox = host.getBoundingClientRect();
    const box = target.getBoundingClientRect();
    tip.textContent = text;
    tip.hidden = false;
    const left = Math.max(0, Math.min(box.left - hostBox.left, hostBox.width - tip.offsetWidth - 4));
    tip.style.left = `${left}px`;
    tip.style.top = `${box.bottom - hostBox.top + 6}px`;
  }

  function hideTip() {
    if (tip) tip.hidden = true;
  }

  function onHover(ev) {
    const t = ev.target.closest("[data-tip]");
    if (t) showTip(t, t.dataset.tip);
  }

  function onLeave(ev) {
    if (ev.target.closest("[data-tip]") && ev.pointerType !== "touch") hideTip();
  }

  function onHostClick(ev) {
    const bar = ev.target.closest("[data-tip]");
    if (bar) {
      if (tip && !tip.hidden && tip.textContent === bar.dataset.tip) hideTip();
      else showTip(bar, bar.dataset.tip);
      return;
    }
    const lane = ev.target.closest(".tl-lane");
    if (lane && onLane) onLane(lane.dataset.job);
  }

  function onHostKeydown(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const lane = ev.target.closest(".tl-lane");
    if (!lane || !onLane) return;
    ev.preventDefault();
    onLane(lane.dataset.job);
  }

  window.Timeline = { mount, refresh, draw };
})();
