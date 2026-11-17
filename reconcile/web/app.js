/* 区间汇总对账台 —— 原生 ES，无构建步骤 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { branch: "", forms: [], daily: [], forecasts: [], audit: [], check: null };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || r.statusText), { data });
  return data;
}

const flash = (msg, ok = false) => {
  $("#error").textContent = ok ? "" : msg;
  $("#ok").textContent = ok ? msg : "";
};

// ---------------- 装载 ----------------
async function load() {
  if (!state.branch) {
    const forms = await api("/api/forms");
    const branches = [...new Set(forms.map((f) => f.branch))];
    const sel = $("#branch");
    const cur = sel.value;
    sel.innerHTML = branches.length
      ? branches.map((b) => `<option ${b === cur ? "selected" : ""}>${esc(b)}</option>`).join("")
      : `<option value="">（先在下方登记单据）</option>`;
    state.branch = branches.includes(cur) ? cur : (branches[0] || "");
  }
  const b = state.branch;
  if (!b) {
    state.forms = []; state.daily = []; state.forecasts = []; state.check = null;
  } else {
    const [forms, daily, forecasts, check] = await Promise.all([
      api(`/api/forms?branch=${encodeURIComponent(b)}`),
      api(`/api/daily?branch=${encodeURIComponent(b)}`),
      api(`/api/forecasts?branch=${encodeURIComponent(b)}`),
      api(`/api/branches/${encodeURIComponent(b)}/check`),
    ]);
    state.forms = forms; state.daily = daily; state.forecasts = forecasts; state.check = check;
  }
  state.audit = await api("/api/audit");
  renderAll();
}

// ---------------- 渲染 ----------------
function renderAll() {
  renderForms(); renderCheck(); renderDaily(); renderForecast(); renderAudit();
  const n = { pending: 0, effective: 0 };
  state.forms.forEach((f) => { n[f.status] = (n[f.status] || 0) + 1; });
  $("#stat-tag").textContent = state.branch
    ? `${state.branch} · 生效 ${n.effective || 0} / 待核对 ${n.pending || 0} · 日量 ${state.daily.length} 条`
    : "暂无网点数据";
}

function renderForms() {
  $("#forms-body").innerHTML = state.forms.map((f) => `
    <tr>
      <td>#${f.id}</td><td>${f.start_day}</td><td>${f.end_day}</td>
      <td><strong>${f.total}</strong></td>
      <td><span class="badge ${f.status}">${f.status === "pending" ? "待核对" : "已生效"}</span></td>
      <td>v${f.version}</td>
      <td>
        ${f.status === "pending"
          ? `<button class="small primary" data-verify="${f.id}">核对生效</button>` : ""}
        <button class="small" data-edit='${esc(JSON.stringify(f))}'>更正</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="7" class="hint">该网点暂无单据</td></tr>`;

  $$("[data-verify]").forEach((btn) => (btn.onclick = () =>
    guard(async () => {
      const res = await api(`/api/forms/${btn.dataset.verify}/verify`, { method: "POST", body: {} });
      flash(`单据 #${res.form.id} 已生效；本次补齐 ${res.filled.length} 个缺日（重试不会重复补录）`, true);
      await load();
    })));
  $$("[data-edit]").forEach((btn) => (btn.onclick = () => {
    const f = JSON.parse(btn.dataset.edit);
    $("#form-form-title").textContent = `更正单据 #${f.id}（当前 v${f.version}，${f.status === "pending" ? "待核对" : "已生效"}）`;
    $("#form-form").id.value = f.id;
    $("#form-form").start_day.value = f.start_day;
    $("#form-form").end_day.value = f.end_day;
    $("#form-form").total.value = f.total;
    $("#form-submit").textContent = "提交更正并重算";
    switchTab("forms");
  }));
}

function renderCheck() {
  const c = state.check;
  const box = $("#check-out");
  if (!c || c.status === "no_form") {
    box.className = "notice";
    box.textContent = "该网点尚无单据，登记后自动试算。";
    return;
  }
  const head = { unique: ["✅ 唯一解：全部缺日取值唯一，可核对生效并自动补齐。", false],
    multiple: ["⚠️ 多解：以下日期取值不唯一，暂不确定，不予补齐。", true],
    infeasible: ["⛔ 矛盾：无可行非负整数解，列出冲突单据。", true] }[c.status];
  box.className = "notice" + (head[1] ? " bad" : "");
  let html = `<div>${head[0]}</div><div class="k">搜索节点 ${c.nodes} · 缺日变量 ${c.var_days.length} 个</div>`;
  if (c.status === "unique") {
    html += `<div class="pill-list">${Object.entries(c.assignment)
      .map(([d, v]) => `<span class="pill good">${d} = ${v}</span>`).join("")}</div>`;
  }
  if (c.status === "multiple") {
    html += `<div class="pill-list">${c.uncertain
      .map((d) => `<span class="pill warn">${d} 不确定</span>`).join("")}</div>`;
    html += `<div class="k">示例解（仅供参考，不写库）：${Object.entries(c.assignment)
      .map(([d, v]) => `${d}=${v}`).join("，")}</div>`;
  }
  if (c.status === "infeasible") {
    if (c.direct_conflicts?.length)
      html += `<div class="k">直接超限单据（固定日量之和已大于总量）：</div>`;
    html += `<div class="pill-list">${c.conflicts
      .map((id) => `<span class="pill warn">单据 #${id}</span>`).join("")}</div>`;
  }
  box.innerHTML = html;
}

function renderDaily() {
  const srcName = { reported: "日结上报", manual: "人工修订", derived: "系统补齐" };
  $("#daily-body").innerHTML = state.daily.map((d) => `
    <tr>
      <td>${d.day}</td><td><strong>${d.volume}</strong></td>
      <td><span class="badge ${d.source}">${srcName[d.source]}</span></td>
      <td>${d.source_form_ids.length ? d.source_form_ids.map((i) => `#${i}`).join(" ") : "—"}</td>
      <td class="hint">${esc(d.updated_at)}</td>
      <td>${d.source !== "derived"
        ? `<button class="small" data-day="${d.day}" data-volume="${d.volume}" data-source="${d.source}">修订</button>`
        : `<span class="hint">随单据重算</span>`}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="hint">该网点尚无日量</td></tr>`;

  $$("#daily-body [data-day]").forEach((btn) => (btn.onclick = () => {
    const f = $("#daily-form");
    f.day.value = btn.dataset.day;
    f.volume.value = btn.dataset.volume;
    f.source.value = btn.dataset.source;
    switchTab("daily");
  }));
}

function renderForecast() {
  $("#forecast-body").innerHTML = state.forecasts.map((x) => `
    <tr>
      <td>${x.target_day}</td><td><strong>${x.value}</strong></td>
      <td class="hint">${x.basis_hash}</td>
      <td>${x.retained ? '<span class="badge retained">旧值保留</span>' : "—"}</td>
      <td class="hint">${esc(x.created_at)}</td>
      <td><pre class="basis">${esc(JSON.stringify({
        生效单据: x.basis.effective_forms.map((f) => `#${f.id} ${f.start_day}~${f.end_day}=${f.total}`),
        固定日量: x.basis.fixed_daily.map((d) => `${d.day}:${d.volume}(${d.source})`),
      }, null, 1))}</pre></td>
    </tr>`).join("") || `<tr><td colspan="6" class="hint">该网点暂无预测</td></tr>`;
}

function renderAudit() {
  $("#audit-body").innerHTML = state.audit.map((a) => `
    <tr><td class="hint">${esc(a.created_at)}</td><td>${esc(a.action)}</td>
    <td><pre class="basis">${esc(a.detail)}</pre></td></tr>`).join("")
    || `<tr><td colspan="3" class="hint">暂无审计记录</td></tr>`;
}

// ---------------- 交互 ----------------
async function guard(fn) {
  flash("");
  try { await fn(); } catch (e) {
    const extra = e.data && Object.keys(e.data).filter((k) => k !== "code" && k !== "detail");
    flash(e.message + (extra?.length ? "\n" + extra.map((k) => `${k}: ${JSON.stringify(e.data[k])}`).join("\n") : ""));
  }
}

function switchTab(name) {
  $$("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  const meta = {
    forms: ["FORMS / 01", "区间汇总单据", "按网点登记起止日与总量；待核对单据可试算约束、核对生效。交叠单据对同一批缺日形成联立方程，仅当全部缺日有唯一非负整数解时自动补齐，补齐值保留来源单据。"],
    daily: ["DAILY / 02", "日量台账与来源", "日结上报与人工修订是固定约束值；系统补齐值标注来源单据，只随核对/更正重算，已有日量在补齐动作中绝不修改。"],
    forecast: ["FORECAST / 03", "预测受理与冻结", "只接受整套一致依据：待核对单据覆盖目标日时阻止受理；旧预测保留原值，依据哈希可追溯。"],
    audit: ["AUDIT / 04", "操作审计", "登记、核对、补齐、修订、更正重算、预测受理/保留全部留痕。"],
  }[name];
  $("#tab-eyebrow").textContent = meta[0]; $("#tab-title").textContent = meta[1];
  $("#tab-desc").textContent = meta[2];
}

function bind() {
  $("#branch").onchange = async (e) => { state.branch = e.target.value; await load(); };
  $("#refresh").onclick = () => guard(load);
  $$("[data-tab]").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));

  $("#form-form").onsubmit = (e) => e.preventDefault(), guard(async () => {
    const f = $("#form-form");
    const payload = { branch: state.branch, start_day: f.start_day.value,
      end_day: f.end_day.value, total: Number(f.total.value) };
    if (!payload.branch) throw new Error("请先登记至少一张单据以确定网点，或刷新页面。");
    if (f.id.value) {
      await api(`/api/forms/${f.id.value}`, { method: "PUT", body: payload });
      flash("单据已更正，原补齐值已连同重算", true);
    } else {
      await api("/api/forms", { method: "POST", body: payload });
      flash("区间单据已登记（待核对）", true);
    }
    f.reset(); f.id.value = "";
    $("#form-form-title").textContent = "登记区间单据";
    $("#form-submit").textContent = "登记单据";
    await load();
  });
  $("#form-reset").onclick = () => {
    $("#form-form").reset(); $("#form-form").id.value = "";
    $("#form-form-title").textContent = "登记区间单据";
    $("#form-submit").textContent = "登记单据";
  };

  $("#daily-form").onsubmit = (e) => e.preventDefault(), guard(async () => {
    const f = $("#daily-form");
    await api("/api/daily", { method: "POST", body: {
      branch: state.branch, day: f.day.value, volume: Number(f.volume.value),
      source: f.source.value, note: f.note.value,
    } });
    flash("固定值已提交，补齐值已整套重算", true);
    f.reset(); await load();
  });

  $("#forecast-form").onsubmit = (e) => e.preventDefault(), guard(async () => {
    const f = $("#forecast-form");
    const res = await api("/api/forecasts", { method: "POST", body: {
      branch: state.branch, target_day: f.target_day.value } });
    flash(res.created ? `预测已受理，值=${res.forecast.value}，依据已冻结` : res.message, true);
    f.reset(); await load();
  });
}

bind();
guard(load);
