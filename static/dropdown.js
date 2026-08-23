(function () {
  const registry = new Map();

  function esc(s) {
    return String(s || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function closeAll(except) {
    document.querySelectorAll(".dd.is-open").forEach((el) => {
      if (el === except) return;
      el.classList.remove("is-open");
      const btn = el.querySelector(".dd__btn");
      const panel = el.querySelector(".dd__panel");
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (panel) panel.hidden = true;
    });
  }

  function summary(items, multi, empty) {
    const on = items.filter((i) => i.selected && !i.disabled);
    if (!on.length) return empty || "고르세요";
    if (!multi || on.length === 1) return on[0].name;
    return on[0].name + " 외 " + (on.length - 1);
  }

  function optHtml(item, multi) {
    const box = multi
      ? `<input class="gds-checkbox" type="checkbox" tabindex="-1" ${item.selected ? "checked" : ""} ${item.disabled ? "disabled" : ""} />`
      : "";
    return `<div class="dd__opt${item.selected && !multi ? " is-on" : ""}${item.disabled ? " is-off" : ""}" data-id="${esc(item.id)}" role="${multi ? "menuitemcheckbox" : "option"}">
      ${box}
      <span class="dd__opt-text">
        <b>${esc(item.name)}</b>
        ${item.line ? `<em>${esc(item.line)}</em>` : ""}
      </span>
    </div>`;
  }

  function bind(el) {
    const st = registry.get(el);
    const btn = el.querySelector(".dd__btn");
    const panel = el.querySelector(".dd__panel");
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const willOpen = !el.classList.contains("is-open");
      closeAll();
      if (!willOpen) return;
      el.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      panel.hidden = false;
    });
    panel.addEventListener("click", (ev) => ev.stopPropagation());
    panel.querySelectorAll(".dd__opt").forEach((row) => {
      row.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const item = st.items.find((i) => i.id === row.dataset.id);
        if (!item || item.disabled) return;
        if (st.multi) {
          item.selected = !item.selected;
          const box = row.querySelector("input");
          if (box) box.checked = item.selected;
          el.querySelector(".dd__value").textContent = summary(st.items, true, st.empty);
          if (st.onChange) st.onChange(selectedIds(st));
        } else {
          st.items.forEach((i) => {
            i.selected = i.id === item.id;
          });
          closeAll();
          paint(el);
          if (st.onChange) st.onChange(item.id);
        }
      });
    });
  }

  function paint(el) {
    const st = registry.get(el);
    if (!st) return;
    const open = el.classList.contains("is-open");
    el.classList.add("dd");
    el.innerHTML =
      `<button type="button" class="dd__btn" aria-haspopup="listbox" aria-expanded="${open ? "true" : "false"}">` +
      `<span class="dd__label">${esc(st.label)}</span>` +
      `<span class="dd__value">${esc(summary(st.items, st.multi, st.empty))}</span>` +
      `<span class="dd__icon" aria-hidden="true"></span>` +
      `</button>` +
      `<div class="dd__panel" ${open ? "" : "hidden"}>${st.items.map((it) => optHtml(it, st.multi)).join("") || '<p class="legend">없음</p>'}</div>`;
    bind(el);
  }

  function selectedIds(st) {
    return st.items.filter((i) => i.selected).map((i) => i.id);
  }

  function node(el) {
    return typeof el === "string" ? document.getElementById(el) : el;
  }

  function mount(el, opts) {
    el = node(el);
    if (!el) return null;
    registry.set(el, {
      label: opts.label || "",
      items: (opts.items || []).map((i) => ({
        id: String(i.id),
        name: i.name || i.id,
        line: i.line || "",
        selected: !!i.selected,
        disabled: !!i.disabled,
      })),
      multi: !!opts.multi,
      empty: opts.empty || "",
      onChange: opts.onChange || null,
    });
    paint(el);
    return el;
  }

  document.addEventListener("click", () => closeAll());
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAll();
  });

  window.DD = {
    mount,
    selected(el) {
      const st = registry.get(node(el));
      return st ? selectedIds(st) : [];
    },
    value(el) {
      return this.selected(el)[0] || "";
    },
    setValue(el, id) {
      el = node(el);
      const st = registry.get(el);
      if (!st) return;
      st.items.forEach((i) => {
        i.selected = i.id === String(id);
      });
      paint(el);
    },
    closeAll,
  };
})();
