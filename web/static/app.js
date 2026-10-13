/* GitHub 密钥扫描器前端逻辑 */
"use strict";

const state = { page: 1, size: 20, q: "", category: "", provider: "", conf: "", status: "" };
const STATUS_MAP = { unknown: "未验证", active: "疑似有效", invalid: "无效", revoked: "已吊销" };
const CATEGORY_MAP = { llm: "大模型", cloud: "云服务", other: "其他服务", unknown: "未知" };
const CATEGORY_CLS = { llm: "cat-llm", cloud: "cat-cloud", other: "cat-other", unknown: "cat-unknown" };
// 置信度 5 分制：存储 0~1（分数/5），展示转回整数分
const confScore = (c) => Math.round(((c || 0) * 5) * 10) / 10;  // 0.2/0.4/…/1.0 → 1/2/…/5

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */
function maskKey(key) {
  if (key.includes(" | sha256:")) return key.split(" | sha256:")[0];
  if (key.length <= 12) return key;
  return key.slice(0, 8) + "****" + key.slice(-4);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const tip = document.createElement("span");
    tip.className = "copied-tip"; tip.textContent = "已复制";
    btn.parentElement.appendChild(tip);
    setTimeout(() => tip.remove(), 1200);
  });
}

/* ---------- 统计 ---------- */
async function loadStats() {
  try {
    const s = await (await fetch("/api/stats")).json();
    $("st-total").textContent = s.total;
    $("st-today").textContent = s.today;
    $("st-high").textContent = s.high_conf;
    $("st-providers").textContent = s.provider_count;
    renderDistro("distro-box", "distro-bars", s.category, "分类");
    renderProviders(s.providers);
    fillProviderSelect(s.providers);
  } catch (e) { /* 服务未启动或库为空时静默 */ }
}

function renderDistro(boxId, barsId, data, label) {
  const box = $(boxId);
  const entries = Object.entries(data || {});
  if (!entries.length) { box.hidden = true; return; }
  box.hidden = false;
  const max = Math.max(...entries.map(([, c]) => c));
  const hue = (i) => (200 + i * 37) % 360;
  $(barsId).innerHTML = entries.map(([name, count], i) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(name)}">${esc(CATEGORY_MAP[name] || name)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(count / max * 100).toFixed(1)}%;background:hsl(${hue(i)},65%,55%)"></div></div>
      <span class="bar-count">${count}</span>
    </div>`).join("");
}

function renderProviders(providers) {
  const box = $("provider-box");
  const list = providers || [];
  if (!list.length) { box.hidden = true; return; }
  box.hidden = false;
  const max = Math.max(...list.map((p) => p.count));
  const hue = (i) => (200 + i * 37) % 360;
  $("provider-bars").innerHTML = list.map((p, i) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(p.name)}">${esc(p.name)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(p.count / max * 100).toFixed(1)}%;background:hsl(${hue(i)},65%,55%)"></div></div>
      <span class="bar-count">${p.count}</span>
    </div>`).join("");
}

function fillProviderSelect(providers) {
  const sel = $("sel-provider");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部服务商</option>' +
    providers.map((p) => `<option value="${esc(p.name)}">${esc(p.name)} (${p.count})</option>`).join("");
  sel.value = cur;
}

/* ---------- 表格 ---------- */
async function loadKeys() {
  const params = new URLSearchParams({
    page: state.page, size: state.size, q: state.q,
    category: state.category, provider: state.provider,
    min_conf: state.conf, status: state.status,
  });
  const data = await (await fetch(`/api/keys?${params}`)).json();
  renderTable(data.items);
  const pages = Math.max(1, Math.ceil(data.total / state.size));
  $("page-info").textContent = `共 ${data.total} 条 · 第 ${data.page}/${pages} 页`;
  $("btn-prev").disabled = data.page <= 1;
  $("btn-next").disabled = data.page >= pages;
  $("empty").hidden = data.items.length > 0;
}

function renderTable(rows) {
  const tbody = $("tbody");
  tbody.innerHTML = rows.map((r) => {
    const catCls = `badge cat-badge ${CATEGORY_CLS[r.category] || "cat-unknown"}`;
    const conf = confScore(r.confidence);
    const confPct = Math.round((r.confidence || 0) * 100);
    const repoShort = (r.repo || "").split("/")[1] || r.repo;
    const notes = r.notes || "-";
    return `<tr data-id="${r.id}">
      <td>${r.id}</td>
      <td class="key-cell"><span class="key-val">${esc(maskKey(r.api_key))}</span>
        <span class="key-actions">
          <button class="icon-btn" title="复制 SHA256 指纹" onclick="copyText('${esc(r.key_fingerprint)}', this)">⧉</button>
        </span>
      </td>
      <td><span class="${catCls}">${esc(CATEGORY_MAP[r.category] || r.category || "未知")}</span></td>
      <td><span class="badge">${esc(r.provider || "unknown")}</span></td>
      <td class="note-cell" title="${esc(r.api_base_url)}">${esc(r.api_base_url || "-")}</td>
      <td><div class="conf-bar"><div class="conf-track"><div class="conf-fill" style="width:${confPct}%;${r.confidence >= 0.8 ? "background:#16a34a" : ""}"></div></div><span>${conf}/5</span></div></td>
      <td><a class="btn-open" href="${esc(r.source_url)}" target="_blank" rel="noopener" title="${esc(r.file_path || "")}">${esc(repoShort)} ↗</a></td>
      <td class="note-cell note-toggle" title="点击展开/收起" onclick="toggleNote(this)">${esc(notes)}</td>
      <td><span class="badge badge-${r.status}">${esc(STATUS_MAP[r.status] || r.status || "unknown")}</span></td>
      <td>${esc(r.first_seen)}</td>
      <td><button class="icon-btn" onclick="showDetail(${r.id})">详情</button></td>
    </tr>`;
  }).join("");
}

/* 备注单元格点击展开/收起 */
function toggleNote(td) {
  td.classList.toggle("note-expanded");
}

/* ---------- 交互 ---------- */
async function showDetail(id) {
  const r = await (await fetch(`/api/keys/${id}`)).json();
  $("modal-body").innerHTML = `
    <div class="detail-row"><span class="k">密钥掩码</span><span class="v">${esc(r.key_mask || maskKey(r.api_key))}</span></div>
    <div class="detail-row"><span class="k">SHA256 指纹</span><span class="v">${esc(r.key_fingerprint)}</span></div>
    <div class="detail-row"><span class="k">分类</span><span class="v">${esc(CATEGORY_MAP[r.category] || r.category || "未知")}</span></div>
    <div class="detail-row"><span class="k">服务商</span><span class="v">${esc(r.provider || "unknown")}</span></div>
    <div class="detail-row"><span class="k">API_BASE_URL</span><span class="v">${esc(r.api_base_url || "-")}</span></div>
    <div class="detail-row"><span class="k">置信度</span><span class="v">${confScore(r.confidence)}/5${r.confidence >= 0.8 ? "（明确指向）" : (r.confidence >= 0.6 ? "（有线索）" : "（弱/无证据）")}</span></div>
    <div class="detail-row"><span class="k">原链接</span><span class="v"><a class="link" href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.source_url)}</a></span></div>
    <div class="detail-row"><span class="k">仓库 / 路径</span><span class="v">${esc(r.repo)} / ${esc(r.file_path)}</span></div>
    <div class="detail-row"><span class="k">备注</span><span class="v">${esc(r.notes || "-")}</span></div>
    <div class="detail-row"><span class="k">首次发现</span><span class="v">${esc(r.first_seen)}</span></div>
    <div class="detail-row"><span class="k">脱敏记录</span><span class="v">不保存密钥原文与代码全文；请查看来源仓库定位泄露。</span></div>`;
  $("modal").hidden = false;
}

/* ---------- 扫描任务面板 ---------- */
const TRIGGER_MAP = { manual: "手动", schedule: "定时", web: "Web" };
const TASK_STATUS_MAP = { running: "运行中", success: "成功", error: "失败" };
const SCOPE_LABELS = { "24h": "最近 24 小时", "7d": "最近 7 天", "30d": "最近 30 天", custom: "自定义日期" };
const scopeState = { initialized: false, dirty: false };
const beijingTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
}).format(new Date(value)) : "";

function updateCustomDates() {
  const custom = $("sel-scan-range").value === "custom";
  $("custom-scan-dates").hidden = !custom;
  for (const id of ["scan-start-date", "scan-end-date"]) {
    $(id).disabled = !custom;
    $(id).required = custom;
  }
}

function renderScanScope(scope) {
  if (!scope || scope.error) {
    $("scan-scope-summary").textContent = scope?.error || "暂时无法读取扫描范围，请刷新后重试";
    $("scan-scope-summary").classList.add("err-text");
    return;
  }
  const settings = scope.settings;
  const window = scope.window;
  if (!scopeState.dirty) {
    $("sel-scan-range").value = settings.preset;
    $("scan-start-date").value = settings.start_date || "";
    $("scan-end-date").value = settings.end_date || "";
    updateCustomDates();
  }
  scopeState.initialized = true;
  $("scan-scope-summary").classList.remove("err-text");
  $("scan-scope-summary").textContent = `已保存：${window.label} · 当前范围 ${beijingTime(window.start_at)} 至 ${beijingTime(window.end_before)}（不含终点）`;
}

async function saveScanScope(event) {
  event.preventDefault();
  const btn = $("btn-save-scope");
  btn.disabled = true;
  const value = { preset: $("sel-scan-range").value,
    start_date: $("scan-start-date").value, end_date: $("scan-end-date").value };
  $("scan-scope-feedback").textContent = "正在保存…";
  try {
    const response = await fetch("/api/scan/settings", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    // Preserve edits made while the request was in flight.
    if ($("sel-scan-range").value === value.preset && $("scan-start-date").value === value.start_date && $("scan-end-date").value === value.end_date) scopeState.dirty = false;
    renderScanScope(result);
    $("scan-scope-feedback").textContent = scopeState.dirty ? "已保存提交的范围，当前更改尚未保存" : result.message;
  } catch (error) {
    $("scan-scope-feedback").textContent = error.message || "保存失败，请重试";
  } finally { btn.disabled = false; }
}

async function loadTasks() {
  try {
    const [status, tasks] = await Promise.all([
      (await fetch("/api/status")).json(),
      (await fetch("/api/tasks?limit=10")).json(),
    ]);
    renderTaskStatus(status);
    renderScanScope(status.scan_scope);
    renderTasks(tasks.items || []);
  } catch (e) { /* 服务未启动时静默 */ }
}

function renderTaskStatus(s) {
  const line = $("task-status-line");
  const cursor = s.cursor || {};
  const last = s.last_task;
  let html = "";
  if (last) {
    const st = TASK_STATUS_MAP[last.status] || last.status;
    html += `上次：${esc(last.started_at)}（<b>${st}</b>，新增 ${last.keys_new} / 更新 ${last.keys_updated}）`;
    if (last.error) html += ` · <span class="err-text">${esc(last.error.slice(0, 80))}</span>`;
  } else {
    html += "尚未运行过扫描";
  }
  html += ` · 间隔 ${s.interval}s`;
  line.innerHTML = html;
}

function renderTasks(rows) {
  const tbody = $("tasks-tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="13" class="empty-inline">暂无扫描任务</td></tr>';
    $("task-cursor").textContent = "";
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const scope = r.repo_pushed_from ? `${esc(SCOPE_LABELS[r.repo_time_preset] || r.repo_time_preset)}<br>${esc(beijingTime(r.repo_pushed_from))} 至<br>${esc(beijingTime(r.repo_pushed_before))}（不含终点）` : "历史任务：未按推送时间筛选";
    const stCls = `badge badge-${r.status === "success" ? "active" : (r.status === "error" ? "invalid" : "unknown")}`;
    return `<tr>
      <td>${r.id}</td>
      <td>${esc(r.started_at)}</td>
      <td>${esc(TRIGGER_MAP[r.trigger] || r.trigger)}</td>
      <td>${esc(r.mode)}</td>
      <td>${scope}</td>
      <td>${r.hits}</td>
      <td>${r.files_outside_time || 0} / ${r.files_unverified_time || 0}</td>
      <td>${r.files_skipped}</td>
      <td>${r.files_analyzed}</td>
      <td>${r.keys_new}</td>
      <td>${r.keys_updated}</td>
      <td><span class="${stCls}" title="${esc(r.error || '')}">${esc(TASK_STATUS_MAP[r.status] || r.status)}</span>${r.error ? `<br><span class="err-text">${esc(r.error)}</span>` : ''}</td>
      <td>${r.duration}s</td>
    </tr>`;
  }).join("");
  $("task-cursor").textContent = "命中为时间筛选前的搜索结果；跳过合计包含范围外、时间未核验、无密钥特征和重复文件。仓库推送时间不代表文件修改时间。";
}

async function triggerScan() {
  if (scopeState.dirty) {
    $("scan-scope-feedback").textContent = "请先保存时间范围，再点击立即扫描";
    return;
  }
  const btn = $("btn-trigger-scan");
  btn.disabled = true;
  try {
    const r = await (await fetch("/api/scan/trigger", { method: "POST" })).json();
    const msg = r.ok ? r.message : (r.error || "触发失败");
    btn.textContent = "已请求 ✓";
    window.alert(msg);
  } catch (e) {
    window.alert("触发失败：服务未响应");
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = "立即扫描"; }, 2000);
}

/* ---------- 事件绑定 ---------- */
let searchTimer = null;
$("inp-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = $("inp-q").value.trim(); state.page = 1; loadKeys(); }, 350);
});
$("sel-category").addEventListener("change", () => { state.category = $("sel-category").value; state.page = 1; loadKeys(); });
$("sel-provider").addEventListener("change", () => { state.provider = $("sel-provider").value; state.page = 1; loadKeys(); });
$("sel-conf").addEventListener("change", () => { state.conf = $("sel-conf").value; state.page = 1; loadKeys(); });
$("sel-status").addEventListener("change", () => { state.status = $("sel-status").value; state.page = 1; loadKeys(); });
$("btn-prev").addEventListener("click", () => { if (state.page > 1) { state.page--; loadKeys(); } });
$("btn-next").addEventListener("click", () => { state.page++; loadKeys(); });
$("btn-refresh").addEventListener("click", () => { loadStats(); loadKeys(); loadTasks(); });
$("btn-export").addEventListener("click", () => {
  const p = new URLSearchParams({ q: state.q, category: state.category, provider: state.provider, min_conf: state.conf, status: state.status });
  window.location.href = `/api/export.csv?${p}`;
});
$("btn-trigger-scan").addEventListener("click", triggerScan);
$("scan-scope-form").addEventListener("submit", saveScanScope);
for (const id of ["sel-scan-range", "scan-start-date", "scan-end-date"]) {
  $(id).addEventListener("change", () => {
    scopeState.dirty = true;
    updateCustomDates();
    $("scan-scope-feedback").textContent = "有未保存的时间范围";
  });
}
$("modal-close").addEventListener("click", () => { $("modal").hidden = true; });
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });

/* ---------- 启动 ---------- */
loadStats();
const beijingToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
$("scan-start-date").max = beijingToday;
$("scan-end-date").max = beijingToday;
updateCustomDates();
loadKeys();
loadTasks();
// 全自动托管下的页面自动刷新：每 30 秒同步统计与任务面板
setInterval(() => { loadStats(); loadTasks(); }, 30000);
