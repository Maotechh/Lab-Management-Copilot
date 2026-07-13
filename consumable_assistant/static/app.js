const state = {
  preview: null,
  selectedItemIds: {},
  prepareRows: [],
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatQuantity(value, unit) {
  if (value === null || value === undefined || value === "") return "";
  const num = Number(value);
  const text = Number.isFinite(num) && Math.abs(num - Math.round(num)) < 1e-9 ? String(Math.round(num)) : String(value);
  return `${text}${unit || ""}`;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => box.classList.remove("show"), 2600);
}

function setStatus(message) {
  $("#statusLine").textContent = message;
}

async function loadStats() {
  const data = await api("/api/stats");
  $("#metrics").innerHTML = `
    <div class="metric"><span>耗材条目</span><strong>${data.item_count}</strong></div>
    <div class="metric"><span>原始数据行</span><strong>${data.source_row_count}</strong></div>
    <div class="metric"><span>操作记录</span><strong>${data.transaction_count}</strong></div>
    <div class="metric"><span>库存预警</span><strong>${data.low_stock_count}</strong></div>
  `;
  setStatus(`已载入 ${data.item_count} 个库存条目`);
  if (data.low_stock_count > 0) notifyLowStock(data.low_stock_count);
}

async function loadSearch() {
  const params = new URLSearchParams();
  params.set("q", $("#searchInput").value.trim());
  params.set("category", $("#categoryFilter").value);
  params.set("low_stock", $("#lowStockFilter").checked ? "true" : "false");
  const data = await api(`/api/items/search?${params.toString()}`);
  $("#searchRows").innerHTML = data.rows.map(renderInventoryRow).join("") || emptyRow(8, "没有匹配结果");
  if (window.lucide) window.lucide.createIcons();
}

function renderInventoryRow(row) {
  const low = row.low_stock ? `<span class="badge danger">低于预警</span>` : `<span class="badge ok">正常</span>`;
  const spec = row.spec ? escapeHtml(row.spec) : `<span class="muted">无</span>`;
  return `
    <tr>
      <td>${escapeHtml(row.category)}</td>
      <td>${escapeHtml(row.lab)}</td>
      <td class="name-cell"><strong>${escapeHtml(row.item_name)}</strong>${renderAliases(row.aliases)}</td>
      <td>${spec}</td>
      <td>${escapeHtml(row.location_code)}</td>
      <td>${formatQuantity(row.quantity, row.unit)}</td>
      <td>${formatQuantity(row.threshold, row.unit)}</td>
      <td>${low}</td>
    </tr>
  `;
}

function renderAliases(aliases) {
  if (!aliases || aliases.length === 0) return "";
  return `<div class="muted">${aliases.slice(0, 3).map(escapeHtml).join("、")}</div>`;
}

function emptyRow(colspan, text) {
  return `<tr><td colspan="${colspan}">
    <div class="empty-state">
      <i data-lucide="inbox"></i>
      <p>${escapeHtml(text)}</p>
    </div>
  </td></tr>`;
}

async function loadTransactions() {
  const data = await api("/api/transactions?limit=60");
  $("#transactionRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.created_at)}</td>
      <td>${escapeHtml(row.action_label)}</td>
      <td>${escapeHtml(row.item_name)}<div class="muted">${escapeHtml(row.location_code)}</div></td>
      <td>${formatQuantity(row.delta_quantity, row.unit)}</td>
      <td>${formatQuantity(row.quantity_after, row.unit)}</td>
    </tr>
  `).join("") || emptyRow(5, "暂无操作记录");
  if (window.lucide) window.lucide.createIcons();
}

async function loadAlerts() {
  const data = await api("/api/alerts");
  $("#alertRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.category)}</td>
      <td>${escapeHtml(row.lab)}</td>
      <td class="name-cell"><strong>${escapeHtml(row.item_name)}</strong><div class="muted">${escapeHtml(row.spec || "")}</div></td>
      <td>${escapeHtml(row.location_code)}</td>
      <td>${formatQuantity(row.quantity, row.unit)}</td>
      <td>${formatQuantity(row.threshold, row.unit)}</td>
      <td><span class="badge danger">${formatQuantity(row.missing_quantity, row.unit)}</span></td>
      <td>
        <input class="number-input" id="threshold-${row.id}" type="number" min="0" step="0.01" value="${Number(row.threshold || 0)}" />
        <button data-threshold-id="${row.id}" class="secondary">保存</button>
      </td>
    </tr>
  `).join("") || emptyRow(8, "暂无低库存条目");
  if (window.lucide) window.lucide.createIcons();
}

async function previewTransaction() {
  const text = $("#chatText").value.trim();
  if (!text) {
    toast("请输入库存变更内容");
    return;
  }
  $("#previewBtn").disabled = true;
  try {
    const data = await api("/api/transactions/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    state.preview = data;
    state.selectedItemIds = {};
    for (const op of data.operations) {
      if (op.candidates && op.candidates.length > 0) {
        state.selectedItemIds[op.id] = op.candidates[0].id;
      }
    }
    renderPreview(data);
    if (window.lucide) window.lucide.createIcons();
  } catch (err) {
    toast(err.message || "解析失败");
    console.error(err);
  } finally {
    $("#previewBtn").disabled = false;
  }
}

function renderPreview(data) {
  const engineLabel = data.engine === "deepseek-pro" ? "deepseek-pro" : "本地规则";
  let html = `<div class="toolbar compact"><span class="badge">${escapeHtml(engineLabel)}</span></div>`;
  
  let totalCandidates = 0;

  for (const op of data.operations) {
    totalCandidates += op.candidates.length;
    const candidatesHtml = op.candidates.map((item, index) => `
      <label class="candidate">
        <input type="radio" name="candidate-${op.id}" value="${item.id}" ${index === 0 ? "checked" : ""} />
        <span>
          <strong>${escapeHtml(item.item_name)}</strong>
          <span class="muted">${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${escapeHtml(item.spec || "")}</span>
        </span>
        <span>${formatQuantity(item.quantity, item.unit)}</span>
      </label>
    `).join("");

    const confidenceLabel = op.confidence === null || op.confidence === undefined
      ? ""
      : `<span class="badge">${Math.round(Number(op.confidence) * 100)}%</span>`;

    html += `
      <div class="surface" style="margin-top: 16px; padding: 16px;">
        <div class="toolbar compact" style="margin-top: 0;">
          <span class="badge ${op.needs_review ? "warn" : "ok"}">${escapeHtml(op.action_label)}</span>
          ${confidenceLabel}
        </div>
        <p style="margin-bottom: 12px; margin-top: 8px;">${escapeHtml(op.message)}<br><small class="muted">${escapeHtml(op.raw_text)}</small></p>
        <div class="toolbar compact">
          <label class="field-label" style="margin-bottom: 0;">动作
            <select id="actionSelect-${op.id}" style="margin-left: 8px;">
              ${actionOption("inbound", "入库", op.action)}
              ${actionOption("consume", "消耗", op.action)}
              ${actionOption("borrow", "借出", op.action)}
              ${actionOption("return", "归还", op.action)}
              ${actionOption("adjust", "修正", op.action)}
              ${actionOption("threshold", "预警设置", op.action)}
            </select>
          </label>
          <label class="field-label" style="margin-bottom: 0;">数量
            <input id="quantityInput-${op.id}" class="number-input" type="number" min="0" step="0.01" value="${op.quantity ?? 0}" style="margin-left: 8px;" />
          </label>
        </div>
        <div class="candidate-list">${candidatesHtml || '<div class="muted" style="padding: 12px 0;">没有候选耗材</div>'}</div>
      </div>
    `;
  }

  $("#previewBox").innerHTML = html;
  $("#commitBtn").disabled = totalCandidates === 0;

  for (const op of data.operations) {
    document.querySelectorAll(`input[name='candidate-${op.id}']`).forEach((radio) => {
      radio.addEventListener("change", () => {
        state.selectedItemIds[op.id] = Number(radio.value);
      });
    });
  }
}

function actionOption(value, label, active) {
  return `<option value="${value}" ${value === active ? "selected" : ""}>${label}</option>`;
}

async function commitTransaction() {
  if (!state.preview || !state.preview.operations) return;
  
  const payloadOperations = [];
  for (const op of state.preview.operations) {
    const selectedId = state.selectedItemIds[op.id];
    if (!selectedId) continue;
    
    payloadOperations.push({
      item_id: selectedId,
      action: $(`#actionSelect-${op.id}`).value,
      quantity: Number($(`#quantityInput-${op.id}`).value || 0),
      unit: op.unit,
      note: op.raw_text,
    });
  }

  if (payloadOperations.length === 0) {
    toast("没有可提交的耗材操作");
    return;
  }

  try {
    $("#commitBtn").disabled = true;
    await api("/api/transactions/commit_bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operations: payloadOperations }),
    });
    toast(`成功提交了 ${payloadOperations.length} 个耗材变更`);
    $("#chatText").value = "";
    $("#previewBox").innerHTML = "";
    state.preview = null;
    await refreshAll();
  } catch (err) {
    toast(err.message || "提交失败");
    $("#commitBtn").disabled = false;
  }
}

async function preparePreview() {
  const file = $("#prepareFile").files[0];
  if (!file) {
    toast("请选择 Excel 文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const data = await api("/api/prepare/preview", { method: "POST", body: form });
  state.prepareRows = data.rows;
  $("#exportPrepareBtn").disabled = data.rows.length === 0;
  renderPrepareRows(data.rows);
  if (window.lucide) window.lucide.createIcons();
}

function renderPrepareRows(rows) {
  $("#prepareRows").innerHTML = rows.map((row) => {
    const locations = row.locations.map((item) =>
      `${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${formatQuantity(item.quantity, item.unit)}`
    ).join("<br>");
    const statusClass = row.status === "足够" ? "ok" : "danger";
    return `
      <tr>
        <td class="name-cell"><strong>${escapeHtml(row.name)}</strong></td>
        <td>${escapeHtml(row.spec || "")}</td>
        <td>${formatQuantity(row.required_quantity, row.unit)}</td>
        <td>${formatQuantity(row.available_quantity, row.unit)}</td>
        <td>${formatQuantity(row.missing_quantity, row.unit)}</td>
        <td>${formatQuantity(row.purchase_quantity, row.purchase_unit)}</td>
        <td>${locations || '<span class="muted">无匹配位置</span>'}</td>
        <td><span class="badge ${statusClass}">${escapeHtml(row.status)}</span></td>
      </tr>
    `;
  }).join("") || emptyRow(8, "暂无清单结果");
}

async function exportPrepareRows() {
  const response = await api("/api/prepare/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows: state.prepareRows }),
  });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "实验准备核对.xlsx";
  link.click();
  URL.revokeObjectURL(url);
}

async function seedData() {
  const data = await api("/api/import/seed", { method: "POST" });
  $("#adminLog").textContent = JSON.stringify(data, null, 2);
  toast("附件数据导入完成");
  await refreshAll();
}

async function saveThreshold(itemId) {
  const value = Number($(`#threshold-${itemId}`).value || 0);
  await api(`/api/items/${itemId}/threshold`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ threshold: value }),
  });
  toast("预警数量已保存");
  await refreshAll();
}

function requestNotification() {
  if (!("Notification" in window)) {
    toast("当前浏览器不支持通知");
    return;
  }
  Notification.requestPermission().then((permission) => {
    toast(permission === "granted" ? "通知已允许" : "通知未允许");
  });
}

function notifyLowStock(count) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (notifyLowStock.lastCount === count) return;
  notifyLowStock.lastCount = count;
  new Notification("耗材库存预警", { body: `${count} 个条目低于预警数量` });
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      button.classList.add("active");
      $(`#tab-${button.dataset.tab}`).classList.add("active");
    });
  });
  $("#refreshAllBtn").addEventListener("click", refreshAll);
  $("#searchBtn").addEventListener("click", loadSearch);
  $("#searchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadSearch();
  });
  $("#categoryFilter").addEventListener("change", loadSearch);
  $("#lowStockFilter").addEventListener("change", loadSearch);
  $("#previewBtn").addEventListener("click", previewTransaction);
  $("#commitBtn").addEventListener("click", commitTransaction);
  $("#prepareBtn").addEventListener("click", preparePreview);
  $("#exportPrepareBtn").addEventListener("click", exportPrepareRows);
  $("#seedBtn").addEventListener("click", seedData);
  $("#requestNotifyBtn").addEventListener("click", requestNotification);
  $("#alertRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-threshold-id]");
    if (target) saveThreshold(target.dataset.thresholdId);
  });
}

async function refreshAll() {
  try {
    await Promise.all([loadStats(), loadSearch(), loadAlerts(), loadTransactions()]);
  } catch (error) {
    console.error(error);
    toast(error.message || "操作失败");
    setStatus("读取失败");
  }
}

bindEvents();
refreshAll().then(() => {
  if (window.lucide) window.lucide.createIcons();
});
