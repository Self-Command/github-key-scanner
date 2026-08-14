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
          <button class="icon-btn" title="显示完整密钥" onclick="toggleKey(this, '${esc(r.api_key)}')">👁</button>
          <button class="icon-btn" title="复制" onclick="copyText('${esc(r.api_key)}', this)">⧉</button>
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
      <td>${r.category === "llm"
        ? `<button class="icon-btn" onclick="testKey(${r.id}, this)" title="请求 /v1/models 测试有效性">测试</button> `
        : ""}<button class="icon-btn" onclick="showDetail(${r.id})">详情</button></td>
    </tr>`;
  }).join("");
}

/* 备注单元格点击展开/收起 */
function toggleNote(td) {
  td.classList.toggle("note-expanded");
}

/* ---------- 交互 ---------- */
function toggleKey(btn, key) {
  const span = btn.closest("td").querySelector(".key-val");
  if (span.textContent.includes("****")) {
    span.textContent = key;
    btn.textContent = "🙈";
  } else {
    span.textContent = maskKey(key);
    btn.textContent = "👁";
  }
}

/* 测试后同步行内状态徽章（状态只读，由测试/自动测试驱动） */
function syncStatusBadge(id, status) {
  const td = document.querySelector(`tr[data-id="${id}"] td:nth-child(9)`);
  if (td) {
    td.innerHTML = `<span class="badge badge-${status}">${esc(STATUS_MAP[status] || status)}</span>`;
  }
  loadStats();
}

async function showDetail(id) {
  const r = await (await fetch(`/api/keys/${id}`)).json();
  const testHtml = r.category === "llm" ? `
    <div class="detail-row"><span class="k">有效性测试</span>
      <span class="v">
        <button class="icon-btn" id="btn-test" onclick="testKeyDetail(${r.id})" title="请求 ${esc(r.api_base_url || "默认端点")}/models">请求 /v1/models 测试</button>
        <span id="test-result"></span>
      </span>
    </div>` : "";
  const lastTestHtml = r.last_tested_at ? `
    <div class="detail-row"><span class="k">上次测试</span><span class="v">${esc(r.last_tested_at)}${renderLastTest(r.last_test_result)}</span></div>` : "";
  $("modal-body").innerHTML = `
    <div class="detail-row"><span class="k">API KEY</span><span class="v">${esc(r.api_key)}</span></div>
    <div class="detail-row"><span class="k">分类</span><span class="v">${esc(CATEGORY_MAP[r.category] || r.category || "未知")}</span></div>
    <div class="detail-row"><span class="k">服务商</span><span class="v">${esc(r.provider || "unknown")}</span></div>
    <div class="detail-row"><span class="k">API_BASE_URL</span><span class="v">${esc(r.api_base_url || "-")}</span></div>
    <div class="detail-row"><span class="k">置信度</span><span class="v">${confScore(r.confidence)}/5${r.confidence >= 0.8 ? "（明确指向）" : (r.confidence >= 0.6 ? "（有线索）" : "（弱/无证据）")}</span></div>
    <div class="detail-row"><span class="k">原链接</span><span class="v"><a class="link" href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.source_url)}</a></span></div>
    <div class="detail-row"><span class="k">仓库 / 路径</span><span class="v">${esc(r.repo)} / ${esc(r.file_path)}</span></div>
    <div class="detail-row"><span class="k">备注</span><span class="v">${esc(r.notes || "-")}</span></div>
    <div class="detail-row"><span class="k">首次发现</span><span class="v">${esc(r.first_seen)}</span></div>
    ${testHtml}
    ${lastTestHtml}
    <div class="detail-row"><span class="k">代码上下文</span><span class="v"></span></div>
    <pre class="context">${esc(r.context || "（无上下文）")}</pre>`;
  $("modal").hidden = false;
}

const VERDICT_MAP = {
  active: "✅ 有效", invalid: "❌ 无效", rate_limited: "⏳ 限流",
  unsupported: "⚠️ 端点不支持", network: "🌐 网络异常", error: "❓ 异常",
};

function renderLastTest(resultJson) {
  try {
    const r = JSON.parse(resultJson);
    const verdict = VERDICT_MAP[r.verdict] || r.verdict;
    return `<div class="test-note ${r.verdict === "active" ? "test-ok" : (r.verdict === "invalid" ? "test-err" : "")}">
      ${verdict} · HTTP ${r.status_code ?? "-"} · 模型数 ${r.models ?? "-"} · ${r.elapsed}s${r.error ? `<br>${esc(r.error)}` : ""}</div>`;
  } catch (e) {
    return "";
  }
}

async function testKey(id, btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "测试中…";
  try {
    const r = await (await fetch(`/api/keys/${id}/test`, { method: "POST" })).json();
    if (r.error) { window.alert(`测试失败：${r.error}`); return; }
    const verdict = VERDICT_MAP[r.verdict] || r.verdict;
    window.alert(`${verdict} · HTTP ${r.status_code ?? "-"} · 模型数 ${r.models ?? "-"} · ${r.elapsed}s${r.error ? `\n${r.error}` : ""}\n端点: ${r.base_url}`);
    if (r.status) syncStatusBadge(id, r.status);
  } catch (e) {
    window.alert("测试请求失败：服务未响应");
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function testKeyDetail(id) {
  const btn = $("btn-test");
  const box = $("test-result");
  if (!btn || !box) return;
  btn.disabled = true; btn.textContent = "测试中…";
  box.className = ""; box.textContent = "";
  try {
    const r = await (await fetch(`/api/keys/${id}/test`, { method: "POST" })).json();
    if (r.error) { box.className = "test-err"; box.textContent = r.error; }
    else {
      const verdict = VERDICT_MAP[r.verdict] || r.verdict;
      box.innerHTML = `<span class="${r.verdict === "active" ? "test-ok" : (r.verdict === "invalid" ? "test-err" : "")}">
        ${verdict} · HTTP ${r.status_code ?? "-"} · 模型数 ${r.models ?? "-"} · ${r.elapsed}s</span>${r.error ? `<div class="test-err">${esc(r.error)}</div>` : ""}`;
    }
    if (r.status) syncStatusBadge(id, r.status);
  } catch (e) {
    box.className = "test-err"; box.textContent = "测试请求失败：服务未响应";
  } finally {
    btn.disabled = false; btn.textContent = "重新测试";
  }
}

/* ---------- 扫描任务面板 ---------- */
const TRIGGER_MAP = { manual: "手动", schedule: "定时", web: "Web" };
const TASK_STATUS_MAP = { running: "运行中", success: "成功", error: "失败" };

async function loadTasks() {
  try {
    const [status, tasks] = await Promise.all([
      (await fetch("/api/status")).json(),
      (await fetch("/api/tasks?limit=10")).json(),
    ]);
    renderTaskStatus(status);
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
  const cursorText = cursor.last_window_to ? cursor.last_window_to : "全量";
  html += ` · 增量游标：<code>${esc(cursorText)}</code> · 间隔 ${s.interval}s`;
  line.innerHTML = html;
}

function renderTasks(rows) {
  const tbody = $("tasks-tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="empty-inline">暂无任务 —— 启动 <code>python main.py daemon</code> 后自动轮询</td></tr>';
    $("task-cursor").textContent = "";
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const scope = r.window_from ? `${esc(r.window_from)} ~ ${esc(r.window_to)}` : "全量";
    const stCls = `badge badge-${r.status === "success" ? "active" : (r.status === "error" ? "invalid" : "unknown")}`;
    return `<tr>
      <td>${r.id}</td>
      <td>${esc(r.started_at)}</td>
      <td>${esc(TRIGGER_MAP[r.trigger] || r.trigger)}</td>
      <td>${esc(r.mode)}</td>
      <td title="${r.window_from ? `${r.window_from} ~ ${r.window_to}` : "首次/全量"}">${scope}</td>
      <td>${r.hits}</td>
      <td>${r.files_skipped}</td>
      <td>${r.files_analyzed}</td>
      <td>${r.keys_new}</td>
      <td>${r.keys_updated}</td>
      <td><span class="${stCls}">${esc(TASK_STATUS_MAP[r.status] || r.status)}</span></td>
      <td>${r.duration}s</td>
    </tr>`;
  }).join("");
  const cursor = rows[0].window_to ? `当前游标：${esc(rows[0].window_to)}（下轮增量起点）` : "游标为空（下轮将全量扫描）";
  $("task-cursor").textContent = cursor;
}

async function triggerScan() {
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
$("modal-close").addEventListener("click", () => { $("modal").hidden = true; });
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });

/* ---------- 启动 ---------- */
loadStats();
loadKeys();
loadTasks();
// 全自动托管下的页面自动刷新：每 30 秒同步统计与任务面板
setInterval(() => { loadStats(); loadTasks(); }, 30000);
