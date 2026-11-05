const state = {
  preview: null,
  selectedItemIds: {},
  prepareRows: [],
  prepareOnlyMissing: false,
  prepareGroup: "",
  prepareSelectedRows: new Set(),
  locations: [],
  locationPreview: null,
  preparePreviewId: null,
  prepareActions: {},
};

const $ = (selector) => document.querySelector(selector);

function newRequestId(){
  if(crypto.randomUUID)return crypto.randomUUID();
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  return [...bytes].map(byte=>byte.toString(16).padStart(2,'0')).join('');
}

function shanghaiTime(value){
  const date=new Date(value+'+08:00');
  if(Number.isNaN(date.valueOf()))throw new Error('请填写有效的上海日期时间');
  return date.toISOString();
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const device = localStorage.getItem('lab-device') || newRequestId();
  localStorage.setItem('lab-device', device);
  headers.set('X-Device-ID', device);
  const previewOnly = ['/api/transactions/preview','/api/prepare/preview','/api/prepare/export','/api/import/preview','/api/import/upload','/api/locations/preview','/api/ai/check'].includes(path);
  const writing = options.method && options.method !== 'GET' && !previewOnly;
  let requestId;
  const finishPending=()=>{if(JSON.parse(localStorage.getItem('lab-pending')||'null')?.id===requestId){localStorage.removeItem('lab-pending');window.renderPending?.();}};
  if (writing) {
    let actor = $('#operatorName')?.value.trim() || localStorage.getItem('lab-actor') || '';
    if (!actor) { $('#operatorName')?.focus(); throw new Error('请先填写操作人姓名'); }
    localStorage.setItem('lab-actor', actor);
    const signature = options.body instanceof FormData
      ? JSON.stringify([...options.body.entries()].map(([key,value]) => [key, value instanceof File ? `${value.name}:${value.size}:${value.lastModified}` : value]))
      : String(options.body || '');
    const pending = JSON.parse(localStorage.getItem('lab-pending') || 'null');
    if (pending && (pending.path !== path || pending.signature !== signature)) throw new Error('有一项操作结果尚未确认，请先查询原请求结果');
    actor=pending?.actor||actor;
    headers.set('X-Actor', encodeURIComponent(actor));
    requestId = pending?.id || newRequestId();
    const formEntries=options.body instanceof FormData?[...options.body.entries()].map(([key,value])=>[key,value instanceof File?{file:true,name:value.name,size:value.size,lastModified:value.lastModified}:value]):null;
    localStorage.setItem('lab-pending',JSON.stringify({id:requestId,path,signature,body:options.body instanceof FormData ? null : options.body,formEntries,actor}));
    headers.set('X-Request-ID',requestId);
    window.renderPending?.();
  }
  let response;
  try { response = await fetch(path, {...options,headers}); }
  catch (error) { window.renderPending?.(); throw new Error('网络连接中断，输入已保留，请查询原请求结果后重试'); }
  if (!response.ok) {
    if (writing && response.status < 500) finishPending();
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
  const result=contentType.includes('application/json')?await response.json():response;
  if(writing)finishPending();
  return result;
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
  const text = Number.isFinite(num) && Number.isInteger(num) ? String(num) : String(value);
  return `${text}${unit || ""}`;
}

function formatDelta(value, unit) {
  const num = Number(value || 0);
  const prefix = num > 0 ? "+" : "";
  return `${prefix}${formatQuantity(num, unit)}`;
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
}

async function loadSearch() {
  const params = new URLSearchParams();
  params.set("q", $("#searchInput").value.trim());
  params.set("category", $("#categoryFilter").value);
  params.set("low_stock", $("#lowStockFilter").checked ? "true" : "false");
  const start=$('#searchStart').value,end=$('#searchEnd').value;
  if(start&&end){params.set('starts_at',shanghaiTime(start));params.set('ends_at',shanghaiTime(end));}
  const data = await api(`/api/items/search?${params.toString()}`);
  $("#searchRows").innerHTML = data.rows.map(renderInventoryRow).join("") || emptyRow(8, "没有匹配结果");
  renderSearchSummary(data);
  if (window.lucide) window.lucide.createIcons();
}

function renderSearchSummary(data) {
  const units = Object.entries(data.quantity_by_unit || {})
    .filter(([unit]) => unit)
    .map(([unit, quantity]) => `${formatQuantity(quantity, unit)}`)
    .join("、");
  const total = units || "数量单位不一致";
  $("#searchSummary").textContent = data.row_count
    ? `找到 ${data.location_count} 个位置，当前数量：${total}。结果同时显示实验室、位置、规格和预警。`
    : "没有匹配结果，请尝试名称、规格、实验室或位置编码。";
}

function renderInventoryRow(row) {
  const low = row.low_stock ? `<span class="badge danger">低于预警</span>` : `<span class="badge ok">正常</span>`;
  const spec = row.spec ? escapeHtml(row.spec) : `<span class="muted">无</span>`;
  return `
    <tr>
      <td data-label="类别">${escapeHtml(row.category)}</td>
      <td data-label="实验室">${escapeHtml(row.lab)}</td>
      <td data-label="耗材" class="name-cell"><strong>${escapeHtml(row.item_name)}</strong>${renderAliases(row.aliases)}</td>
      <td data-label="规格">${spec}</td>
      <td data-label="位置"><strong>${escapeHtml(row.location_code)}</strong></td>
      <td data-label="数量"><strong>可用 ${formatQuantity(row.available_quantity, row.unit)}</strong><div class="muted small-text">总量 ${formatQuantity(row.owned_quantity, row.unit)} · 借出 ${row.borrowed_quantity} · 预留 ${row.reserved_quantity}</div><div class="muted small-text">待清洗 ${row.cleaning} · 维修 ${row.repair} · 待核对 ${row.quarantine}</div>${row.review_reason ? `<span class="badge danger">${escapeHtml(row.review_reason)}</span>` : ''}</td>
      <td data-label="预警">
        <div class="inline-edit">
          <input class="number-input threshold-input" data-threshold-input="${row.id}" data-version="${row.version}" type="number" min="0" step="any" value="${Number(row.threshold || 0)}" />
          <button class="secondary icon-only" data-threshold-id="${row.id}" title="保存预警"><i data-lucide="save" class="icon-sm"></i></button>
        </div>
      </td>
      <td data-label="最近状态">${low}${renderLatestTransaction(row.latest_transaction, row.unit)}</td>
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

function renderLatestTransaction(txn, unit) {
  if (!txn) return `<div class="muted small-text">暂无变更记录</div>`;
  const delta = Number(txn.delta_quantity || 0);
  const deltaText = delta === 0 ? "数量不变" : formatDelta(delta, txn.unit || unit);
  return `<div class="muted small-text">${escapeHtml(txn.action_label)} ${escapeHtml(deltaText)}，余 ${formatQuantity(txn.quantity_after, txn.unit || unit)}</div>`;
}

async function loadTransactions() {
  const data = await api("/api/transactions?limit=60");
  $("#transactionRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td data-label="时间">${formatDate(row.created_at)}</td>
      <td data-label="动作">${escapeHtml(row.action_label)}</td>
      <td data-label="耗材">${escapeHtml(row.item_name)}<div class="muted">${escapeHtml(row.lab || "")} ${escapeHtml(row.location_code)}</div></td>
      <td data-label="数量">${formatDelta(row.delta_quantity, row.unit)}</td>
      <td data-label="结果">${formatQuantity(row.quantity_after, row.unit)}</td>
      <td data-label="操作">${renderUndoButton(row)}</td>
    </tr>
  `).join("") || emptyRow(6, "暂无操作记录");
  if (window.lucide) window.lucide.createIcons();
}

function renderUndoButton(row) {
  const canUndo = row.can_undo;
  if (!canUndo) return `<span class="muted small-text">不可撤销</span>`;
  const description = `撤销${row.action_label || "库存操作"}，${row.item_name || "耗材"}，${formatQuantity(Math.abs(Number(row.delta_quantity || 0)), row.unit)}`;
  return `<button class="secondary" data-undo-id="${row.id}" data-version="${row.version}" title="${escapeHtml(description)}" aria-label="${escapeHtml(description)}"><i data-lucide="rotate-ccw" class="icon-sm"></i>撤销</button>`;
}

function formatDate(value) {
  if (!value) return '';
  const iso = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value.replace(' ','T')+'Z';
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? escapeHtml(value) : escapeHtml(date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}));
}

async function loadAlerts() {
  const data = await api("/api/alerts");
  $("#alertRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td data-label="类别">${escapeHtml(row.category)}</td>
      <td data-label="实验室">${escapeHtml(row.lab)}</td>
      <td data-label="耗材" class="name-cell"><strong>${escapeHtml(row.item_name)}</strong><div class="muted">${escapeHtml(row.spec || "")}</div></td>
      <td data-label="位置">${escapeHtml(row.location_code)}</td>
      <td data-label="当前数量">${formatQuantity(row.quantity, row.unit)}</td>
      <td data-label="预警数量">${formatQuantity(row.threshold, row.unit)}</td>
      <td data-label="还差多少"><span class="badge danger">${formatQuantity(row.missing_quantity, row.unit)}</span></td>
      <td data-label="修改预警">
        <input class="number-input threshold-input" data-threshold-input="${row.id}" data-version="${row.version}" type="number" min="0" step="any" value="${Number(row.threshold || 0)}" />
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
    if ($('#chatText').value.trim() !== text) return;
    state.preview = data;
    state.previewInputs={};
    state.selectedItemIds = {};
    for (const op of data.operations) {
      if (op.candidates && op.candidates.length === 1) {
        state.selectedItemIds[op.id] = op.candidates[0].id;
      }
    }
    renderPreview(data);
    persistPreviews();
    if (window.lucide) window.lucide.createIcons();
  } catch (err) {
    toast(err.message || "解析失败");
    console.error(err);
  } finally {
    $("#previewBtn").disabled = false;
  }
}

function renderPreview(data) {
  const engineLabel = data.engine === "deepseek-pro"
    ? `${data.model_name || 'AI'}${data.local_split_used ? ' + 规则校验' : ''}`
    : (data.local_split_used ? "本地多条规则" : "本地规则");
  const modelWarning = data.model_enabled && data.engine !== "deepseek-pro" && !data.local_split_used
    ? `<span class="badge warn">模型未返回可用结果，已使用本地规则</span>`
    : "";
  let html = `<div class="toolbar compact"><span class="badge">${escapeHtml(engineLabel)}</span>${modelWarning}</div>`;
  
  let totalCandidates = 0;

  for (const op of data.operations) {
    totalCandidates += op.candidates.length;
    const isInbound = op.action === "inbound";
    const candidatesHtml = op.candidates.map((item, index) => `
      <label class="candidate">
        <input class="candidate-select" type="${isInbound ? "checkbox" : "radio"}" name="candidate-${op.id}" value="${item.id}" ${state.selectedItemIds[op.id]===item.id ? "checked" : ""} />
        <span>
          <strong>${escapeHtml(item.item_name)}</strong>
          <span class="muted">${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${escapeHtml(item.spec || "")}</span>
          <span class="muted small-text">匹配 ${Math.round(Number(item.score || 0))} 分${item.low_stock ? "，低于预警" : ""}</span>
        </span>
        ${isInbound
          ? `<input class="candidate-quantity number-input" data-candidate-quantity="${op.id}-${item.id}" type="number" min="0" step="0.01" value="${index === 0 ? Number(op.quantity || 0) : 0}" aria-label="${escapeHtml(item.item_name)}入库数量" />`
          : `<span>${formatQuantity(item.quantity, item.unit)}</span>`}
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
              ${op.needs_review ? '<option value="">请选择动作</option>' : ""}
              ${actionOption("inbound", "入库", op.action)}
              ${actionOption("consume", "消耗", op.action)}
              ${actionOption("borrow", "借出", op.action)}
              ${actionOption("return", "归还", op.action)}
              ${actionOption("adjust", "修正", op.action)}
              ${actionOption("threshold", "预警设置", op.action)}
            </select>
          </label>
          <label class="field-label ${isInbound ? "inbound-total-label" : ""}" style="margin-bottom: 0;">${isInbound ? "各位置分别填写数量" : "数量"}
            <input id="quantityInput-${op.id}" class="number-input" type="number" min="0" step="0.01" value="${op.quantity ?? 0}" style="margin-left: 8px;" />
          </label>
        </div>
        <div class="candidate-list">${candidatesHtml || '<div class="muted" style="padding: 12px 0;">没有候选耗材</div>'}</div>
        ${op.action === 'borrow' ? `<div class="form-grid"><label>借用人<input id="borrower-${op.id}" required></label><label>用途<input id="purpose-${op.id}" required></label><label>目标位置<input id="destination-${op.id}" required></label><label>预计归还时间（上海时间）<input id="due-${op.id}" type="datetime-local" required></label></div>` : ''}
        ${op.action === 'return' ? `<div class="form-grid"><label>对应借用<select id="loan-${op.id}"><option value="">选择借用记录</option>${(window.opsData?.loans || []).filter(loan=>Number(loan.outstanding)>0).map(loan=>`<option value="${loan.id}">${escapeHtml(loan.borrower)} · ${escapeHtml(loan.item?.item_name)} · 未归还 ${loan.outstanding}</option>`).join('')}</select></label><label>归还状态<select id="return-state-${op.id}"><option value="">选择状态</option><option value="ready">可用</option><option value="cleaning">待清洗</option><option value="repair">维修</option><option value="quarantine">待核对</option><option value="damaged">破损</option><option value="lost">遗失</option></select></label></div>` : ''}
      </div>
    `;
  }

  $("#previewBox").innerHTML = html;
  for(const [id,saved] of Object.entries(state.previewInputs||{})){
    const input=document.getElementById(id);if(input){if(input.type==='checkbox'||input.type==='radio')input.checked=saved.checked;else input.value=saved.value;}
  }
  $("#commitBtn").disabled = totalCandidates === 0;

  for (const op of data.operations) {
    document.querySelectorAll(`input[name='candidate-${op.id}']`).forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) state.selectedItemIds[op.id] = Number(input.value);
        if (op.quantity_mode === "all_available" && input.checked) {
          const selected = op.candidates.find((item) => Number(item.id) === Number(input.value));
          const quantityInput = $(`#quantityInput-${op.id}`);
          if (selected && quantityInput) quantityInput.value = Number(selected.quantity || 0);
        }
      });
    });
    const actionSelect = $(`#actionSelect-${op.id}`);
    if (actionSelect) {
      actionSelect.addEventListener("change", () => {
        state.previewInputs={};
        $('#previewBox').querySelectorAll('input[id],select[id]').forEach(input=>state.previewInputs[input.id]={value:input.value,checked:input.checked});
        for (const entry of data.operations) {
          const input = $(`#quantityInput-${entry.id}`);
          if (input) entry.quantity = input.value;
        }
        op.action = actionSelect.value;
        op.action_label = { inbound: "入库", consume: "消耗", borrow: "借出", return: "归还", adjust: "修正", threshold: "预警设置" }[op.action] || "需要确认";
        op.needs_review = false;
        renderPreview(data);
        if (window.lucide) window.lucide.createIcons();
      });
    }
  }
}

function actionOption(value, label, active) {
  return `<option value="${value}" ${value === active ? "selected" : ""}>${label}</option>`;
}

async function commitTransaction() {
  if (!state.preview || !state.preview.operations) return;
  
  const payloadOperations = [];
  for (const op of state.preview.operations) {
    const action = $(`#actionSelect-${op.id}`).value;
    if (!action) {
      toast("请先选择库存动作");
      return;
    }
    if (action === "inbound") {
      const selectedInputs = [...document.querySelectorAll(`input[name='candidate-${op.id}']:checked`)];
      if (selectedInputs.length === 0) {
        toast("入库至少选择一个位置");
        return;
      }
      for (const input of selectedInputs) {
        const item = op.candidates.find((candidate) => Number(candidate.id) === Number(input.value));
        const quantityInput = document.querySelector(`[data-candidate-quantity='${op.id}-${input.value}']`);
        const quantity = Number(quantityInput?.value || 0);
        if (quantity <= 0) {
          toast("已选择的入库位置需要填写大于 0 的数量");
          return;
        }
        payloadOperations.push({
          item_id: Number(input.value),
          version: item?.version,
          action,
          quantity: quantityInput.value,
          unit: op.unit || item?.unit,
          note: op.raw_text,
        });
      }
      continue;
    }

    const selectedInput = document.querySelector(`input[name='candidate-${op.id}']:checked`);
    const selectedId = selectedInput ? Number(selectedInput.value) : state.selectedItemIds[op.id];
    if (!selectedId) continue;
    const quantity = Number($(`#quantityInput-${op.id}`).value || 0);
    if (["consume", "borrow", "return"].includes(action) && quantity <= 0) {
      toast("消耗、借出和归还的数量需要大于 0");
      return;
    }
    if (["adjust", "threshold"].includes(action) && quantity < 0) {
      toast("数量不能小于 0");
      return;
    }
    const item = op.candidates.find(candidate=>Number(candidate.id)===selectedId);
    const extra = {};
    if (action === 'borrow') {
      extra.borrower=$(`#borrower-${op.id}`)?.value; extra.purpose=$(`#purpose-${op.id}`)?.value; extra.destination=$(`#destination-${op.id}`)?.value;
      const due=$(`#due-${op.id}`)?.value;
      if (!extra.borrower || !extra.purpose || !extra.destination || !due) { toast('请完整填写借用信息'); return; }
      extra.due_at=shanghaiTime(due);
    }
    if (action === 'return') { extra.loan_id=Number($(`#loan-${op.id}`)?.value); extra.return_state=$(`#return-state-${op.id}`)?.value; }
    payloadOperations.push({ item_id: selectedId, version:item?.version, action, quantity:$(`#quantityInput-${op.id}`).value, unit: op.unit, note: op.raw_text, ...extra });
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
    localStorage.removeItem('lab-chat-draft');
    localStorage.removeItem('lab-chat-preview');
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
  form.append("course", $("#courseName").value.trim());
  $("#prepareBtn").disabled = true;
  try {
    const data = await api("/api/prepare/preview", { method: "POST", body: form });
    state.prepareRows = data.rows;
    state.preparePreviewId = data.preview_id;
    state.prepareActions = {};
    state.prepareSelectedRows = new Set();
    persistPreviews();
    $("#prepareSelectEnough").checked = false;
    const groups = data.groups || [];
    $("#prepareGroupFilter").innerHTML = `<option value="">全部课程实验</option>${groups.map((group) => {
      const value = `${group.course}||${group.experiment}`;
      return `<option value="${escapeHtml(value)}">${escapeHtml(group.course)} · ${escapeHtml(group.experiment)}</option>`;
    }).join("")}`;
    $("#exportPrepareBtn").disabled = data.rows.length === 0;
    renderPrepareRows(data.rows);
    if (window.lucide) window.lucide.createIcons();
  } catch (err) {
    toast(err.message || "清单解析失败");
  } finally {
    $("#prepareBtn").disabled = false;
  }
}

function renderPrepareRows(rows) {
  const groupRows = state.prepareGroup
    ? rows.filter((row) => `${row.course}||${row.experiment}` === state.prepareGroup)
    : rows;
  const sourceRows = state.prepareOnlyMissing
    ? groupRows.filter((row) => Number(row.missing_quantity || 0) > 0)
    : groupRows;
  const sortedRows = [...sourceRows].sort((a, b) => Number(b.missing_quantity || 0) - Number(a.missing_quantity || 0));
  renderPrepareSummary(rows);
  $("#prepareRows").innerHTML = sortedRows.map((row) => {
    const visibleLocations = (row.locations || []).slice(0, 4);
    const locations = visibleLocations.map((item) =>
      `${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${formatQuantity(item.quantity, item.unit)}`
    ).join("<br>") + ((row.locations || []).length > 4 ? `<br><span class="muted">还有 ${(row.locations || []).length - 4} 个位置</span>` : "");
    const statusClass = row.status === "足够" ? "ok" : "danger";
    const enough = Number(row.missing_quantity || 0) <= 0;
    const selected = state.prepareSelectedRows.has(String(row.row_number));
    return `
      <tr>
        <td data-label="选择"><input class="prepare-select" type="checkbox" data-prepare-row="${escapeHtml(row.row_number)}" ${selected ? "checked" : ""} ${enough ? "" : "disabled"} /></td>
        <td data-label="课程/实验"><strong>${escapeHtml(row.course || "")}</strong><div class="muted">${escapeHtml(row.experiment || "")}</div></td>
        <td data-label="耗材" class="name-cell"><strong>${escapeHtml(row.name)}</strong></td>
        <td data-label="规格">${escapeHtml(row.spec || "")}</td>
        <td data-label="需求">${formatQuantity(row.required_quantity, row.unit)}</td>
        <td data-label="使用方式"><select data-prepare-action="${row.row_number}" aria-label="${escapeHtml(row.name)}使用方式"><option value="">请选择</option><option value="consume" ${(state.prepareActions[row.row_number]||row.action)==='consume'?'selected':''}>消耗</option><option value="borrow" ${(state.prepareActions[row.row_number]||row.action)==='borrow'?'selected':''}>借用</option></select></td>
        <td data-label="可用">${formatQuantity(row.available_quantity, row.unit)}</td>
        <td data-label="还需采购">${formatQuantity(row.missing_quantity, row.unit)}</td>
        <td data-label="建议采购">${formatQuantity(row.purchase_quantity, row.purchase_unit)}</td>
        <td data-label="位置">${locations || '<span class="muted">无匹配位置</span>'}${row.status==='需要确认匹配'||row.selected_item_id?`<select data-prepare-match="${row.row_number}" aria-label="${escapeHtml(row.name)}匹配物品"><option value="">选择具体物品</option>${(row.candidates||[]).map(item=>`<option value="${item.id}" ${item.id===row.selected_item_id?'selected':''}>${escapeHtml(item.item_name)} ${escapeHtml(item.spec)} · ${escapeHtml(item.lab)} ${escapeHtml(item.location_code)} · ${escapeHtml(item.unit)}</option>`).join('')}</select>`:''}</td>
        <td data-label="状态"><span class="badge ${statusClass}">${escapeHtml(row.status)}</span></td>
      </tr>
    `;
  }).join("") || emptyRow(11, state.prepareOnlyMissing ? "当前清单没有需要采购的耗材" : "暂无清单结果");
  updatePrepareCommitButton();
}

function renderPrepareSummary(rows) {
  const needRows = rows.filter((row) => Number(row.missing_quantity || 0) > 0);
  const locationCount = rows.reduce((sum, row) => sum + Number(row.match_count || 0), 0);
  $("#prepareSummary").innerHTML = rows.length
    ? `共 ${rows.length} 个条目，${new Set(rows.map((row) => `${row.course}||${row.experiment}`)).size} 个课程实验，${needRows.length} 个需要采购，匹配到 ${locationCount} 个库存位置。已选 ${state.prepareSelectedRows.size} 个库存足够的条目，可按课程实验批量扣减。`
    : "上传课程实验清单后显示各实验的库存数量和需要采购的条目。";
}

function updatePrepareCommitButton() {
  $("#consumePrepareBtn").disabled = state.prepareSelectedRows.size === 0;
}

async function consumeSelectedPrepareRows() {
  const selected = state.prepareRows.filter((row) => state.prepareSelectedRows.has(String(row.row_number)));
  if (!selected.length) {
    toast("请先选择库存足够的清单条目");
    return;
  }
  if (!window.confirm(`确认按清单扣减 ${selected.length} 个条目的库存？数量为 0 的条目会保留并继续参与预警。`)) return;
  $("#consumePrepareBtn").disabled = true;
  try {
    const result = await api("/api/prepare/consume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_id:state.preparePreviewId,selected_rows:selected.map(row=>row.row_number),actions:state.prepareActions }),
    });
    toast(`已扣减 ${result.transaction_count} 条库存记录`);
    state.prepareSelectedRows = new Set();
    state.prepareRows=[];state.preparePreviewId=null;localStorage.removeItem('lab-prepare-preview');renderPrepareRows([]);
    await refreshAll();
  } catch (err) {
    toast(err.message || "库存扣减失败");
    updatePrepareCommitButton();
  }
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

async function loadLocations() {
  const data = await api("/api/locations");
  state.locations = data.rows || [];
  const selected = $("#sourceLocationSelect").value;
  $("#sourceLocationSelect").innerHTML = `<option value="">选择原位置</option>${state.locations.map((location) => `
    <option value="${escapeHtml(JSON.stringify({ lab: location.lab, location_code: location.location_code }))}">
      ${escapeHtml(location.label)} · ${location.item_count} 项 · ${location.zero_count} 个为 0
    </option>
  `).join("")}`;
  if ([...$("#sourceLocationSelect").options].some((option) => option.value === selected)) {
    $("#sourceLocationSelect").value = selected;
  }
}

function getSelectedLocation() {
  const value = $("#sourceLocationSelect").value;
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

async function previewSelectedLocation() {
  const location = getSelectedLocation();
  if (!location) {
    toast("请先选择原位置");
    return;
  }
  try {
    const data = await api("/api/locations/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(location),
    });
    state.locationPreview = data;
    renderLocationPreview(data);
  } catch (err) {
    toast(err.message || "位置读取失败");
  }
}

function renderLocationPreview(data) {
  $("#locationSummary").textContent = data.row_count
    ? `${data.lab || "未填写实验室"} · ${data.location_code || "未填写位置"}：${data.row_count} 项，${data.zero_count} 个数量为 0。位置变化保留历史记录。`
    : "原位置没有耗材。";
  $("#locationRows").innerHTML = (data.rows || []).map((row) => `
    <tr>
      <td data-label="耗材"><strong>${escapeHtml(row.item_name)}</strong><div class="muted">${escapeHtml(row.category || "")}</div></td>
      <td data-label="规格">${escapeHtml(row.spec || "")}</td>
      <td data-label="数量">${formatQuantity(row.quantity, row.unit)}</td>
      <td data-label="预警">${formatQuantity(row.threshold, row.unit)}</td>
    </tr>
  `).join("") || emptyRow(4, "原位置没有耗材");
  const hasRows = Number(data.row_count || 0) > 0;
  $("#moveLocationBtn").disabled = !hasRows;
  $("#deleteLocationBtn").disabled = !hasRows;
}

async function moveSelectedLocation() {
  const source = getSelectedLocation();
  const targetLab = $("#targetLabInput").value.trim();
  const targetLocation = $("#targetLocationInput").value.trim();
  if (!source || !targetLab || !targetLocation) {
    toast("请填写原位置和目标实验室、目标位置");
    return;
  }
  if (!window.confirm(`确认将 ${source.lab || "未填写实验室"} · ${source.location_code || "未填写位置"} 的全部耗材转移到 ${targetLab} · ${targetLocation}？`)) return;
  try {
    const result = await api("/api/locations/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lab: source.lab,
        source_location: source.location_code,
        target_lab: targetLab,
        target_location: targetLocation,
        versions: state.locationPreview?.versions,
        confirmed: true,
      }),
    });
    toast(`已转移 ${result.moved_count} 项，合并 ${result.merged_count} 项，清理 ${result.deleted_zero_count} 个零数量条目`);
    state.locationPreview = null;
    const targetValue = JSON.stringify({ lab: targetLab, location_code: targetLocation });
    await loadLocations();
    if ([...$("#sourceLocationSelect").options].some((option) => option.value === targetValue)) {
      $("#sourceLocationSelect").value = targetValue;
      await previewSelectedLocation();
    } else {
      renderLocationPreview({ rows: [], row_count: 0, zero_count: 0 });
    }
    await refreshAll();
  } catch (err) {
    toast(err.message || "位置转移失败");
  }
}

async function deleteSelectedLocation() {
  const source = getSelectedLocation();
  if (!source || !state.locationPreview?.row_count) {
    toast("请先查看有耗材的原位置");
    return;
  }
  if (!window.confirm(`确认停用 ${source.lab || "未填写实验室"} · ${source.location_code || "未填写位置"}？历史记录会保留，现有数量必须先处理。`)) return;
  try {
    const result = await api("/api/locations/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lab: source.lab,
        source_location: source.location_code,
        confirmed: true,
        note: $('#locationNote')?.value || '位置停用',
      }),
    });
    toast(`已停用 ${result.archived_count} 项耗材，历史记录已保留`);
    state.locationPreview = null;
    await loadLocations();
    renderLocationPreview({ rows: [], row_count: 0, zero_count: 0 });
    await refreshAll();
  } catch (err) {
    toast(err.message || "位置删除失败");
  }
}

async function batchUploadInventory() {
  const file = $("#batchUploadFile").files[0];
  if (!file) {
    toast("请选择库存表格");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("mode", $("#batchUploadMode").value);
  form.append("category", $("#batchUploadCategory").value);
  $("#batchUploadBtn").disabled = true;
  try {
    const result = await api("/api/import/upload", { method: "POST", body: form });
    window.importPreview = result;
    $('#batchUploadLog').textContent = `${result.rows.length} 行，${result.errors.length} 行错误${result.duplicate_file ? '，这份表格已经导入过' : ''}\n` + result.rows.map(row=>`第 ${row.row_number} 行：${row.item_name}，原数量 ${row.before}，表格数量 ${row.quantity}${row.unit}，${row.item_id?'已有物品':'新增物品'}${row.duplicate_row?'，表内重复行将忽略':''}`).join('\n') + '\n' + result.errors.map(row=>`第 ${row.row_number} 行：${row.error}`).join('\n');
    $('#confirmImportBtn').disabled = !result.rows.length || !!result.errors.length;
    toast('库存表预览已生成，请核对后确认');
  } catch (err) {
    toast(err.message || "批量上传失败");
  } finally {
    $("#batchUploadBtn").disabled = false;
  }
}

async function seedData() {
  const data = await api("/api/import/seed", { method: "POST" });
  $("#adminLog").textContent = JSON.stringify(data, null, 2);
  toast("附件数据导入完成");
  await refreshAll();
}

async function saveThreshold(itemId, button) {
  const input = button?.closest("tr")?.querySelector(`[data-threshold-input="${itemId}"]`)
    || document.querySelector(`[data-threshold-input="${itemId}"]`);
  const value = Number(input?.value || 0);
  await api(`/api/items/${itemId}/threshold`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ threshold: value,version:Number(input?.dataset.version) }),
  });
  toast("预警数量已保存");
  await refreshAll();
}

async function undoTransaction(txnId) {
  if (!window.confirm("确认撤销这次库存操作？")) return;
  const button = document.querySelector(`[data-undo-id="${txnId}"]`);
  await api(`/api/transactions/${txnId}/undo`, { method: "POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(button?.dataset.version)}) });
  toast("库存操作已撤销");
  await refreshAll();
}

function requestNotification() {
  return window.subscribeNotifications?.();
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
  $("#prepareMissingOnly").addEventListener("change", (event) => {
    state.prepareOnlyMissing = event.target.checked;
    renderPrepareRows(state.prepareRows);
  });
  $("#prepareGroupFilter").addEventListener("change", (event) => {
    state.prepareGroup = event.target.value;
    renderPrepareRows(state.prepareRows);
  });
  $("#prepareSelectEnough").addEventListener("change", (event) => {
    if (event.target.checked) {
      state.prepareSelectedRows = new Set(
        state.prepareRows
          .filter((row) => Number(row.missing_quantity || 0) <= 0)
          .map((row) => String(row.row_number))
      );
    } else {
      state.prepareSelectedRows = new Set();
    }
    renderPrepareRows(state.prepareRows);
  });
  $("#consumePrepareBtn").addEventListener("click", consumeSelectedPrepareRows);
  $("#prepareRows").addEventListener("change", async (event) => {
    const match=event.target.closest('select[data-prepare-match]');
    if(match && match.value){
      try{
        const data=await api('/api/actions/prepare_remap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_id:state.preparePreviewId,selections:{[match.dataset.prepareMatch]:Number(match.value)}})});
        state.preparePreviewId=data.preview_id;state.prepareRows=data.rows;state.prepareSelectedRows.clear();renderPrepareRows(data.rows);persistPreviews();
      }catch(error){toast(error.message);}
      return;
    }
    const action = event.target.closest('select[data-prepare-action]');
    if (action) { state.prepareActions[action.dataset.prepareAction] = action.value; return; }
    const checkbox = event.target.closest("input[data-prepare-row]");
    if (!checkbox) return;
    const rowNumber = String(checkbox.dataset.prepareRow);
    if (checkbox.checked) state.prepareSelectedRows.add(rowNumber);
    else state.prepareSelectedRows.delete(rowNumber);
    updatePrepareCommitButton();
    renderPrepareSummary(state.prepareRows);
  });
  $('#chatText').addEventListener('input',()=>{
    localStorage.setItem('lab-chat-draft',$('#chatText').value);
    state.preview=null; state.previewInputs={}; $('#previewBox').innerHTML=''; $('#commitBtn').disabled=true;
    localStorage.removeItem('lab-chat-preview');
  });
  $("#seedBtn").addEventListener("click", seedData);
  $("#batchUploadBtn").addEventListener("click", batchUploadInventory);
  $("#previewLocationBtn").addEventListener("click", previewSelectedLocation);
  $("#moveLocationBtn").addEventListener("click", moveSelectedLocation);
  $("#deleteLocationBtn").addEventListener("click", deleteSelectedLocation);
  $("#requestNotifyBtn").addEventListener("click", requestNotification);
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      $("#chatText").value = button.dataset.prompt;
      $('#chatText').dispatchEvent(new Event('input',{bubbles:true}));
      $("#chatText").focus();
    });
  });
  $("#searchRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-threshold-id]");
    if (target) saveThreshold(target.dataset.thresholdId, target);
  });
  $("#alertRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-threshold-id]");
    if (target) saveThreshold(target.dataset.thresholdId, target);
  });
  $("#transactionRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-undo-id]");
    if (target) undoTransaction(target.dataset.undoId);
  });
}

function persistPreviews(){
  if(state.preview)localStorage.setItem('lab-chat-preview',JSON.stringify({text:$('#chatText').value,data:state.preview}));
  if(state.preparePreviewId)localStorage.setItem('lab-prepare-preview',JSON.stringify({id:state.preparePreviewId,rows:state.prepareRows,actions:state.prepareActions}));
}
document.addEventListener('change',persistPreviews);
$('#prepareFile').addEventListener('change',()=>{state.prepareRows=[];state.preparePreviewId=null;state.prepareSelectedRows.clear();localStorage.removeItem('lab-prepare-preview');renderPrepareRows([]);});

async function refreshAll() {
  try {
    await Promise.all([loadStats(), loadSearch(), loadAlerts(), loadTransactions(), loadLocations()]);
    await window.refreshWorkflows?.();
  } catch (error) {
    console.error(error);
    toast(error.message || "操作失败");
    setStatus("读取失败");
  }
}

bindEvents();
window.initialRefresh=refreshAll().then(() => {
  if (window.lucide) window.lucide.createIcons();
});
