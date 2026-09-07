(function () {
  const registry = new Map();
  let seq = 0;

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
      setOpen(el, false);
    });
  }

  function setOpen(el, open) {
    const st = registry.get(el);
    const btn = el.querySelector(".dd__btn");
    const panel = el.querySelector(".dd__panel");
    el.classList.toggle("is-open", open);
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (panel) panel.hidden = !open;
    if (!open && st) {
      st.hl = -1;
      paintHighlight(el);
    }
  }

  /** Summary text: selected enabled items win, but a selected-disabled item (e.g. a missing model) still shows its name. */
  function summary(items, multi, empty) {
    const picked = items.filter((i) => i.selected);
    const live = picked.filter((i) => !i.disabled);
    const on = live.length ? live : picked;
    if (!on.length) return empty || "고르세요";
    if (!multi || on.length === 1) return on[0].name;
    return on[0].name + " 외 " + (on.length - 1);
  }

  function optHtml(item, idx, st) {
    const box = st.multi
      ? `<input class="gds-checkbox" type="checkbox" tabindex="-1" aria-hidden="true" ${item.selected ? "checked" : ""} ${item.disabled ? "disabled" : ""} />`
      : "";
    const org = item.org_logo
      ? `<img class="dd__logo" src="${esc(item.org_logo)}" alt="${esc(item.org || "")}" />`
      : "";
    const cls = ["dd__opt"];
    if (item.selected && !st.multi) cls.push("is-on");
    if (item.disabled) cls.push("is-off");
    return `<div class="${cls.join(" ")}" id="${st.panelId}-${idx}" data-id="${esc(item.id)}" data-idx="${idx}" role="option" aria-selected="${item.selected ? "true" : "false"}"${item.disabled ? ' aria-disabled="true"' : ""}>
      ${box}
      <span class="dd__opt-text">
        <b>${org}${esc(item.name)}</b>
        ${item.line ? `<em>${esc(item.line)}</em>` : ""}
      </span>
    </div>`;
  }

  function paintHighlight(el) {
    const st = registry.get(el);
    const btn = el.querySelector(".dd__btn");
    if (!st || !btn) return;
    el.querySelectorAll(".dd__opt").forEach((row) => {
      const on = Number(row.dataset.idx) === st.hl;
      row.classList.toggle("is-hl", on);
      if (on) row.scrollIntoView({ block: "nearest" });
    });
    if (st.hl >= 0) btn.setAttribute("aria-activedescendant", `${st.panelId}-${st.hl}`);
    else btn.removeAttribute("aria-activedescendant");
  }

  function enabledIdx(st) {
    return st.items.map((it, i) => (it.disabled ? -1 : i)).filter((i) => i >= 0);
  }

  function moveHighlight(el, delta, edge) {
    const st = registry.get(el);
    const ids = enabledIdx(st);
    if (!ids.length) return;
    let next;
    if (edge === "first") next = ids[0];
    else if (edge === "last") next = ids[ids.length - 1];
    else {
      const pos = ids.indexOf(st.hl);
      const start = pos === -1 ? (delta > 0 ? -1 : ids.length) : pos;
      next = ids[Math.max(0, Math.min(ids.length - 1, start + delta))];
    }
    st.hl = next;
    paintHighlight(el);
  }

  function pick(el, item) {
    const st = registry.get(el);
    if (!item || item.disabled) return;
    if (st.multi) {
      item.selected = !item.selected;
      const row = el.querySelector(`.dd__opt[data-id="${cssEsc(item.id)}"]`);
      if (row) {
        row.setAttribute("aria-selected", item.selected ? "true" : "false");
        const box = row.querySelector("input");
        if (box) box.checked = item.selected;
      }
      el.querySelector(".dd__value").textContent = summary(st.items, true, st.empty);
      if (st.onChange) st.onChange(selectedIds(st));
      return;
    }
    st.items.forEach((i) => {
      i.selected = i.id === item.id;
    });
    closeAll();
    paint(el);
    if (st.onChange) st.onChange(item.id);
  }

  function cssEsc(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
  }

  function onKey(el, ev) {
    const st = registry.get(el);
    const open = el.classList.contains("is-open");
    const key = ev.key;
    if (key === "Escape") {
      if (open) {
        ev.preventDefault();
        setOpen(el, false);
      }
      return;
    }
    if (key === "ArrowDown" || key === "ArrowUp") {
      ev.preventDefault();
      if (!open) {
        closeAll(el);
        setOpen(el, true);
      }
      moveHighlight(el, key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (key === "Home" || key === "End") {
      if (!open) return;
      ev.preventDefault();
      moveHighlight(el, 0, key === "Home" ? "first" : "last");
      return;
    }
    if (key === "Enter" || key === " ") {
      ev.preventDefault();
      if (!open) {
        closeAll(el);
        setOpen(el, true);
        return;
      }
      if (st.hl >= 0) pick(el, st.items[st.hl]);
      else if (!st.multi) setOpen(el, false);
      return;
    }
    if (key === "Tab" && open) setOpen(el, false);
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
      if (willOpen) setOpen(el, true);
    });
    btn.addEventListener("keydown", (ev) => onKey(el, ev));
    panel.addEventListener("keydown", (ev) => onKey(el, ev));
    panel.addEventListener("click", (ev) => ev.stopPropagation());
    panel.addEventListener("pointermove", (ev) => {
      const row = ev.target.closest(".dd__opt");
      if (!row || row.classList.contains("is-off")) return;
      const idx = Number(row.dataset.idx);
      if (idx !== st.hl) {
        st.hl = idx;
        paintHighlight(el);
      }
    });
    panel.querySelectorAll(".dd__opt").forEach((row) => {
      row.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        pick(el, st.items.find((i) => i.id === row.dataset.id));
        if (st.multi) btn.focus();
      });
    });
    el.addEventListener("focusout", (ev) => {
      if (!el.contains(ev.relatedTarget)) setOpen(el, false);
    });
  }

  function paint(el) {
    const st = registry.get(el);
    if (!st) return;
    const open = el.classList.contains("is-open");
    const hadFocus = el.contains(document.activeElement);
    el.classList.add("dd");
    el.innerHTML =
      `<button type="button" class="dd__btn" aria-haspopup="listbox" aria-expanded="${open ? "true" : "false"}" aria-controls="${st.panelId}">` +
      `<span class="dd__label">${esc(st.label)}</span>` +
      `<span class="dd__value">${esc(summary(st.items, st.multi, st.empty))}</span>` +
      `<span class="dd__icon" aria-hidden="true"></span>` +
      `</button>` +
      `<div class="dd__panel" id="${st.panelId}" role="listbox" tabindex="-1" aria-label="${esc(st.label)}"${st.multi ? ' aria-multiselectable="true"' : ""} ${open ? "" : "hidden"}>` +
      `${st.items.map((it, i) => optHtml(it, i, st)).join("") || '<p class="legend">없음</p>'}</div>`;
    bind(el);
    if (hadFocus) el.querySelector(".dd__btn").focus();
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
    const prev = registry.get(el);
    registry.set(el, {
      label: opts.label || "",
      panelId: prev ? prev.panelId : `${el.id || "dd" + ++seq}__list`,
      hl: -1,
      items: (opts.items || []).map((i) => ({
        id: String(i.id),
        name: i.name || i.id,
        line: i.line || "",
        selected: !!i.selected,
        disabled: !!i.disabled,
        org: i.org || "",
        org_color: i.org_color || "",
        org_logo: i.org_logo || "",
      })),
      multi: !!opts.multi,
      empty: opts.empty || "",
      onChange: opts.onChange || null,
    });
    paint(el);
    return el;
  }

  document.addEventListener("click", () => closeAll());

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
