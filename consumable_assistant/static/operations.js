const ops = {items:[], data:{loans:[],templates:[],schedules:[],purchases:[],counts:[]}, pendingCheck:false};
const esc=escapeHtml;
const control=(id,label,type='text',value='')=>`<label>${label}${type==='datetime-local'?'（上海时间）':''}<input id="${id}" name="${id}" type="${type}" value="${esc(value)}" ${type==='number'?'min="0" step="any"':''}></label>`;
const choices=(id,label,options)=>`<label>${label}<select id="${id}" name="${id}">${options.map(([value,text])=>`<option value="${esc(value)}">${esc(text)}</option>`).join('')}</select></label>`;
const itemControl=(id)=>choices(id,'物品',[['','选择物品']]);
const command=(text,icon='check',value='')=>`<button type="submit" class="primary" value="${value}"><i data-lucide="${icon}" class="icon-sm"></i>${text}</button>`;
const box=(title,body)=>`<section class="workflow-section"><h2>${title}</h2>${body}</section>`;
const states=[['','请选择'],['ready','可用'],['cleaning','待清洗'],['repair','维修'],['quarantine','待核对'],['damaged','破损'],['lost','遗失']];
const jsonOptions={method:'POST',headers:{'Content-Type':'application/json'}};
async function perform(kind,data) { return api(`/api/actions/${kind}`,{...jsonOptions,body:JSON.stringify(data)}); }
function chosen(id) { const item=ops.items.find(item=>item.id===Number($('#'+id).value)); if (!item) throw new Error('请选择物品'); return item; }
const value=id=>$('#'+id).value.trim();
function iso(id) { return shanghaiTime(value(id)); }
function base(item) { return {item_id:item.id,version:item.version,unit:item.unit}; }
function showError(error) { const el=$('#pageError'); el.hidden=false; el.textContent=error.message || String(error); }
function bindForm(id,fn) {
  $('#'+id).addEventListener('submit',async event=>{
    event.preventDefault(); $('#pageError').hidden=true;
    const button=event.submitter; if (button) button.disabled=true;
    const form=event.currentTarget;form.querySelector('.form-error')?.remove();
    try { await fn(event); clearFormDraft(form); toast('操作已完成'); await refreshAll(); }
    catch (error) { showError(error);const detail=document.createElement('p');detail.className='form-error notice danger';detail.setAttribute('role','alert');detail.textContent=error.message;form.append(detail); }
    finally { if (button) button.disabled=false; }
  });
}
function table(headers,rows) { return `<div class="table-wrap responsive-cards"><table><thead><tr>${headers.map(text=>`<th>${text}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map((cell,index)=>`<td data-label="${headers[index]}">${cell??''}</td>`).join('')}</tr>`).join(''):emptyRow(headers.length,'暂无记录')}</tbody></table></div>`; }
function rowButton(action,id,label,icon) { return `<button type="button" data-workflow="${action}" data-id="${id}"><i data-lucide="${icon}" class="icon-sm"></i>${label}</button>`; }
function initWorkflows() {
  $('#tab-loans').innerHTML=box('登记借用',`<form id="loanForm" class="form-grid">${itemControl('loanItem')}${control('loanQuantity','数量','number')}${control('loanBorrower','借用人')}${control('loanPurpose','用途')}${control('loanDestination','目标位置')}${control('loanDue','预计归还时间','datetime-local')}${command('登记借用','handshake')}</form>`)+box('归还及借出期间的破损处理',`<form id="returnForm" class="form-grid">${choices('returnLoan','对应借用',[['','选择借用记录']])}${control('returnQuantity','数量','number')}${choices('returnState','物品状态',states)}${control('returnNote','说明')}${command('确认处理','package-check')}</form>`)+box('借用记录','<label class="inline-check"><input id="overdueOnly" type="checkbox">只看逾期未归还</label><div id="loanList"></div>');
  $('#tab-lessons').innerHTML=box('实验模板与安排',`<form id="lessonForm"><div class="form-grid">${choices('lessonTemplate','已有模板',[['','新建模板']])}${control('lessonName','实验名称')}${control('lessonCourse','课程')}${control('lessonPeople','学生人数','number','30')}${control('lessonGroupSize','每组人数','number','2')}${control('lessonVenue','场地')}${control('lessonStart','开始时间','datetime-local')}${control('lessonEnd','结束时间','datetime-local')}</div><div id="lessonLines"></div><div class="toolbar"><button type="button" id="addLessonLine"><i data-lucide="plus" class="icon-sm"></i>添加物品</button>${command('保存模板','save','template')}${command('预留实验物品','calendar-plus','schedule')}</div><output id="lessonDemand"></output></form>`)+box('实验安排',`${control('executeBorrower','执行安排时的借用人或班级')}<div id="scheduleList"></div>`);
  $('#tab-purchases').innerHTML=box('登记采购',`<form id="purchaseForm" class="form-grid">${itemControl('purchaseItem')}${control('purchaseQuantity','订购数量','number')}${control('purchaseOwner','采购负责人')}${control('purchaseDue','预计到货时间','datetime-local')}${control('purchaseNote','说明')}${command('登记采购','shopping-cart')}</form>`)+box('登记到货',`<form id="receiveForm" class="form-grid">${choices('receiveOrder','采购记录',[['','选择采购记录']])}${control('receiveQuantity','本次到货数量','number')}${command('确认到货','package-check')}</form>`)+box('采购记录','<div id="purchaseList"></div>');
  $('#tab-maintenance').innerHTML=box('库存维护',`<form id="maintenanceForm"><div class="form-grid">${choices('maintenanceAction','操作',[['count','实物清点'],['state','状态转移'],['transfer','部分数量转移'],['metadata','修改资料'],['create_item','新增物品']])}${itemControl('maintenanceItem')}${control('maintenanceQuantity','数量','number')}${control('maintenanceNote','原因或说明')}${choices('maintenanceFrom','原状态',states.filter(([key])=>!['damaged','lost'].includes(key)))}${choices('maintenanceTo','目标状态',states)}${control('maintenanceLab','实验室')}${control('maintenanceLocation','位置')}${control('maintenanceName','名称')}${control('maintenanceSpec','规格')}${control('maintenanceBrand','品牌')}${control('maintenanceCategory','类别')}${control('maintenanceUnit','基础单位')}${choices('maintenanceUsage','使用方式',[['unclassified','待确认'],['consumable','消耗'],['reusable','可重复使用']])}${control('conversionUnit','包装单位')}${control('conversionFactor','一包装包含的基础单位数量','number')}</div><p id="maintenanceSummary" class="muted"></p>${command('确认维护','clipboard-check')}</form>`)+box('位置说明与照片',`<form id="locationDetailsForm" class="form-grid">${control('detailsLab','实验室')}${control('detailsLocation','位置')}<label>位置说明<textarea id="detailsDescription"></textarea></label><label>位置照片<input id="detailsPhoto" type="file" accept="image/jpeg,image/png,image/webp"></label>${command('保存位置资料','save')}</form><div id="locationPhotos"></div>`)+box('清点记录','<div id="countList"></div>');
  $('#tab-history').innerHTML=box('操作历史',`<form id="historyForm" class="form-grid">${control('historyQuery','物品、位置或课程')}${control('historyActor','操作人')}${choices('historyAction','动作',[['','全部'],['inbound','入库'],['consume','消耗'],['borrow','借用'],['return','归还'],['count','清点'],['undo','撤销'],['transfer_out','转移']])}${control('historyFrom','开始日期','date')}${control('historyTo','结束日期','date')}${command('查询','search')}<a id="historyExport" class="button" href="/api/export/history"><i data-lucide="download" class="icon-sm"></i>导出记录</a></form><div id="historyList"></div>`);
  bindForm('loanForm',()=>{const item=chosen('loanItem');return perform('stock',{...base(item),action:'borrow',quantity:value('loanQuantity'),borrower:value('loanBorrower'),purpose:value('loanPurpose'),destination:value('loanDestination'),due_at:iso('loanDue')});});
  bindForm('returnForm',()=>{const loan=ops.data.loans.find(row=>row.id===Number(value('returnLoan')));if(!loan)throw new Error('请选择对应借用');return perform('stock',{...base(loan.item),action:'return',quantity:value('returnQuantity'),loan_id:loan.id,return_state:value('returnState'),note:value('returnNote')});});
  bindForm('purchaseForm',()=>perform('purchase',{...base(chosen('purchaseItem')),quantity:value('purchaseQuantity'),owner:value('purchaseOwner'),expected_at:iso('purchaseDue'),note:value('purchaseNote')}));
  bindForm('receiveForm',()=>{const order=ops.data.purchases.find(row=>row.id===Number(value('receiveOrder')));if(!order)throw new Error('请选择采购记录');return perform('purchase',{...base(order.item),action:'receive',order_id:order.id,order_version:order.version,quantity:value('receiveQuantity')});});
  bindForm('lessonForm',event=>{
    const lines=lessonLines(); const template=ops.data.templates.find(row=>row.id===Number(value('lessonTemplate')));
    const data={name:value('lessonName'),course:value('lessonCourse'),people:Number(value('lessonPeople')),group_size:Number(value('lessonGroupSize')),lines};
    if(event.submitter.value==='template') return perform('template',{...data,template_id:template?.id,version:template?.version});
    return perform('schedule',{...data,venue:value('lessonVenue'),starts_at:iso('lessonStart'),ends_at:iso('lessonEnd')});
  });
  bindForm('maintenanceForm',()=>{
    const kind=value('maintenanceAction');
    if(kind==='create_item')return perform(kind,{item_name:value('maintenanceName'),spec:value('maintenanceSpec'),brand:value('maintenanceBrand'),category:value('maintenanceCategory'),lab:value('maintenanceLab'),location_code:value('maintenanceLocation'),unit:value('maintenanceUnit')});
    const item=chosen('maintenanceItem'); const data={...base(item),quantity:value('maintenanceQuantity'),note:value('maintenanceNote')};
    if(kind==='metadata'){
      const conversions={...item.unit_conversions};
      if(value('conversionUnit')){if(value('conversionFactor'))conversions[value('conversionUnit')]=value('conversionFactor');else delete conversions[value('conversionUnit')];}
      return perform(kind,{...data,item_name:value('maintenanceName'),spec:value('maintenanceSpec'),brand:value('maintenanceBrand'),category:value('maintenanceCategory'),lab:value('maintenanceLab'),location_code:value('maintenanceLocation'),usage_type:value('maintenanceUsage'),conversions});
    }
    if(kind==='transfer')return perform(kind,{...data,target_lab:value('maintenanceLab'),target_location:value('maintenanceLocation'),state:value('maintenanceFrom')});
    return perform('stock',{...data,action:kind,from_state:value('maintenanceFrom'),to_state:value('maintenanceTo')});
  });
  bindForm('locationDetailsForm',async()=>{
    const lab=value('detailsLab'),location_code=value('detailsLocation');
    const locations=await api('/api/locations');let old=locations.rows.find(row=>row.lab===lab&&row.location_code===location_code)?.details;
    await perform('location_details',{lab,location_code,description:value('detailsDescription'),version:old?.version||0});
    const file=$('#detailsPhoto').files[0];
    if(file){const latest=await api('/api/locations');old=latest.rows.find(row=>row.lab===lab&&row.location_code===location_code)?.details;const form=new FormData();form.append('lab',lab);form.append('location_code',location_code);form.append('version',old?.version||1);form.append('file',file);await api('/api/locations/photo',{method:'POST',body:form});}
  });
  bindForm('historyForm',loadHistory);
  $('#addLessonLine').addEventListener('click',()=>addLessonLine());
  $('#lessonTemplate').addEventListener('change',()=>{const entry=ops.data.templates.find(row=>row.id===Number(value('lessonTemplate')));$('#lessonLines').innerHTML='';if(entry){$('#lessonName').value=entry.name;$('#lessonCourse').value=entry.course;entry.lines.forEach(addLessonLine);}else addLessonLine();});
  $('#lessonForm').addEventListener('input',calculateDemand);
  $('#overdueOnly').addEventListener('change',renderLoans);
  $('#maintenanceItem').addEventListener('change',fillMaintenance);
  $('#maintenanceAction').addEventListener('change',maintenanceMode);
  document.addEventListener('click',workflowClick);
  $('#confirmImportBtn').addEventListener('click',async()=>{try{await perform('import',{preview_id:window.importPreview.preview_id,allow_duplicate:$('#allowDuplicateImport').checked,note:value('importNote')});$('#confirmImportBtn').disabled=true;toast('库存表已导入');await refreshAll();}catch(error){showError(error);}});
  $('#aiCheckBtn').addEventListener('click',async()=>{const button=$('#aiCheckBtn');button.disabled=true;try{await api('/api/ai/check',{method:'POST'});await loadAI();}catch(error){showError(error);}finally{button.disabled=false;}});
  maintenanceMode();
}
function populateSelect(id,options) {const el=$('#'+id);const previous=el.value;el.innerHTML=options.map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join('');if([...el.options].some(option=>option.value===previous))el.value=previous;}
function itemOptions() {return [['','选择物品'],...ops.items.map(item=>[item.id,`${item.item_name} ${item.spec||''} · ${item.lab||''} ${item.location_code||''} · 可用 ${item.available_quantity}${item.unit||''}${item.review_reason?' · 待核对':''}`])];}
function addLessonLine(line={}) {
  const id=newRequestId();const element=document.createElement('div');element.className='lesson-line form-grid';element.dataset.line=id;
  element.innerHTML=`${choices('lineItem-'+id,'物品',itemOptions())}${choices('lineAction-'+id,'使用方式',[['','选择方式'],['consume','消耗'],['borrow','借用']])}${control('linePerson-'+id,'每人用量','number',line.per_person||'0')}${control('lineGroup-'+id,'每组用量','number',line.per_group||'0')}${control('lineFixed-'+id,'演示用量','number',line.fixed||'0')}${control('lineSpare-'+id,'备用数量','number',line.spare||'0')}<button type="button" class="remove-line" title="移除此物品" aria-label="移除此物品"><i data-lucide="x"></i></button>`;
  $('#lessonLines').append(element);$('#lineItem-'+id).value=line.item_id||'';$('#lineAction-'+id).value=line.action||'';
  element.querySelector('.remove-line').addEventListener('click',()=>{element.remove();calculateDemand();saveFormDraft($('#lessonForm'));});window.lucide?.createIcons();calculateDemand();
}
function lessonLines() {return [...document.querySelectorAll('[data-line]')].map(el=>{const id=el.dataset.line;const item=chosen('lineItem-'+id);return {...base(item),action:value('lineAction-'+id),per_person:value('linePerson-'+id),per_group:value('lineGroup-'+id),fixed:value('lineFixed-'+id),spare:value('lineSpare-'+id)};});}
function calculateDemand() {try{const groups=Math.ceil(Number(value('lessonPeople'))/Number(value('lessonGroupSize')));$('#lessonDemand').textContent=lessonLines().map(line=>`${ops.items.find(item=>item.id===line.item_id)?.item_name}：${Number(line.per_person)*Number(value('lessonPeople'))+Number(line.per_group)*groups+Number(line.fixed)+Number(line.spare)}${line.unit}`).join('；');}catch{$('#lessonDemand').textContent='';}}
function maintenanceMode(){const kind=value('maintenanceAction');$('#maintenanceQuantity').closest('label').firstChild.textContent=kind==='count'?'柜内可用实物数量':'本次操作数量';const visible={count:['maintenanceItem','maintenanceQuantity','maintenanceNote'],state:['maintenanceItem','maintenanceQuantity','maintenanceNote','maintenanceFrom','maintenanceTo'],transfer:['maintenanceItem','maintenanceQuantity','maintenanceNote','maintenanceFrom','maintenanceLab','maintenanceLocation'],metadata:['maintenanceItem','maintenanceName','maintenanceSpec','maintenanceBrand','maintenanceCategory','maintenanceLab','maintenanceLocation','maintenanceUsage','conversionUnit','conversionFactor'],create_item:['maintenanceName','maintenanceSpec','maintenanceBrand','maintenanceCategory','maintenanceLab','maintenanceLocation','maintenanceUnit']}[kind];$('#maintenanceForm').querySelectorAll('.form-grid label').forEach(label=>{const input=label.querySelector('input,select');label.hidden=input.id!=='maintenanceAction'&&!visible.includes(input.id);});}
function fillMaintenance(){try{const item=chosen('maintenanceItem');for(const [id,key] of [['maintenanceName','item_name'],['maintenanceSpec','spec'],['maintenanceBrand','brand'],['maintenanceCategory','category'],['maintenanceLab','lab'],['maintenanceLocation','location_code'],['maintenanceUnit','unit'],['maintenanceUsage','usage_type']])$('#'+id).value=item[key]||'';$('#maintenanceQuantity').value=item.ready_quantity;$('#maintenanceSummary').textContent=`当前可用 ${item.available_quantity}${item.unit}，原始数量 ${item.original_quantity}${item.review_reason?'；'+item.review_reason:''}`;}catch{}}
function renderLoans(){const rows=ops.data.loans.filter(row=>!$('#overdueOnly').checked||row.overdue);$('#loanList').innerHTML=table(['物品','借用人与用途','借出／未归还','预计归还','状态','操作'],rows.map(row=>[esc(row.item?.item_name),`${esc(row.borrower)}<br>${esc(row.purpose)} · ${esc(row.destination)}`,`${row.quantity} / ${row.outstanding}`,formatDate(row.due_at),row.overdue?'<span class="badge danger">逾期</span>':Number(row.outstanding)>0?'未完成':'已处理',Number(row.outstanding)>0?rowButton('return',row.id,'登记归还','package-check'):'']));}
async function loadHistory(){const params=new URLSearchParams({q:value('historyQuery'),actor:value('historyActor'),action:value('historyAction'),from_date:value('historyFrom'),to_date:value('historyTo'),limit:'500'});const result=await api('/api/transactions?'+params);$('#historyExport').href='/api/export/history?'+params;$('#historyList').innerHTML=table(['时间','操作人','动作','物品与位置','数量变化','说明','操作'],result.rows.map(row=>[formatDate(row.created_at),esc(row.actor||''),esc(row.action_label),`${esc(row.item_name)}<br>${esc(row.lab||'')} ${esc(row.location_code||'')}`,formatDelta(row.delta_quantity,row.unit),esc(row.note||''),renderUndoButton(row)]));window.lucide?.createIcons();}
async function workflowClick(event){const button=event.target.closest('[data-workflow]');if(!button)return;const id=Number(button.dataset.id);const alertEvent=ops.alerts?.find(row=>row.id===id)?.alert_state?.event_id;try{switch(button.dataset.workflow){case 'return':$('#returnLoan').value=id;$('#returnQuantity').value=ops.data.loans.find(row=>row.id===id).outstanding;$('#returnState').focus();return;case 'execute':{const row=ops.data.schedules.find(row=>row.id===id);if(!confirm(`确认执行 ${row.name}？`))return;await perform('execute_schedule',{schedule_id:id,version:row.version,item_versions:row.item_versions,borrower:value('executeBorrower')});break;}case 'cancel_schedule':{const row=ops.data.schedules.find(row=>row.id===id);const note=prompt('取消安排的原因');if(!note)return;await perform('cancel_schedule',{schedule_id:id,version:row.version,note});break;}case 'cancel_purchase':{const row=ops.data.purchases.find(row=>row.id===id);const note=prompt('取消采购的原因');if(!note)return;await perform('purchase',{action:'cancel',order_id:id,order_version:row.version,note});break;}case 'ack':await perform('alert',{item_id:id,event_id:alertEvent,action:'acknowledge'});break;case 'snooze':await perform('alert',{item_id:id,event_id:alertEvent,action:'snooze',until:new Date(Date.now()+86400000).toISOString()});break;case 'remind':await perform('alert',{item_id:id,event_id:alertEvent,action:'remind'});break;}await refreshAll();}catch(error){showError(error);}}
async function loadAI(){const data=await api('/api/ai/status');const labels={available:'AI 可用',unavailable:'AI 暂时不可用',unconfigured:'AI 凭据未配置',stale:'AI 状态待更新',unknown:'AI 正在等待检查'};const provider=data.detail||data.provider;$('#aiStatus').textContent=`${labels[data.status]||data.status}${data.status==='available'&&provider?' · '+provider:''}`;$('#aiStatus').className='badge '+(data.status==='available'?'ok':'warn');$('#aiStatus').title=(data.providers||[]).map(item=>`${item.provider}: ${item.status}${item.elapsed_ms?` ${item.elapsed_ms} ms`:''}`).join('\n');}
window.refreshWorkflows=async()=>{
  const [items,data]=await Promise.all([api('/api/items/search?limit=2000'),api('/api/workflows')]);ops.items=items.rows;ops.data=data;window.opsData=data;
  for(const id of ['loanItem','purchaseItem','maintenanceItem',...[...document.querySelectorAll('[data-line]')].map(el=>'lineItem-'+el.dataset.line)])populateSelect(id,itemOptions());
  populateSelect('returnLoan',[['','选择借用记录'],...data.loans.filter(row=>Number(row.outstanding)>0).map(row=>[row.id,`${row.borrower} · ${row.item.item_name} · 未归还 ${row.outstanding}`])]);
  populateSelect('receiveOrder',[['','选择采购记录'],...data.purchases.filter(row=>row.status==='ordered').map(row=>[row.id,`${row.item.item_name} · 尚未到货 ${Number(row.quantity)-Number(row.received)}`])]);
  populateSelect('lessonTemplate',[['','新建模板'],...data.templates.map(row=>[row.id,`${row.course} · ${row.name}`])]);
  renderLoans();
  $('#purchaseList').innerHTML=table(['物品','负责人','订购／到货','预计到货','状态','操作'],data.purchases.map(row=>[esc(row.item.item_name),esc(row.actor),`${row.quantity} / ${row.received}`,formatDate(row.expected_at),esc(({ordered:'尚未全部到货',received:'已到货',cancelled:'已取消'})[row.status]),row.status==='ordered'?rowButton('cancel_purchase',row.id,'取消余量','x'):'']));
  $('#scheduleList').innerHTML=table(['实验','课程与场地','时间','负责人','状态','操作'],data.schedules.map(row=>[esc(row.name),`${esc(row.course)} · ${esc(row.venue)}`,`${formatDate(row.starts_at)}<br>${formatDate(row.ends_at)}`,esc(row.actor),esc(({reserved:'已预留',executed:'已执行',cancelled:'已取消'})[row.status]),row.status==='reserved'?rowButton('execute',row.id,'执行','play')+rowButton('cancel_schedule',row.id,'取消','x'):'']));
  $('#countList').innerHTML=table(['时间','物品','原数量','清点数量','原因','操作人'],data.counts.map(row=>[formatDate(row.created_at),esc(row.item.item_name),esc(row.before_quantity),esc(row.actual_quantity),esc(row.reason),esc(row.actor)]));
  const locs=await api('/api/locations');$('#locationPhotos').innerHTML=locs.rows.filter(row=>row.details?.description||row.details?.photo_path).map(row=>`<figure><figcaption>${esc(row.lab)} ${esc(row.location_code)}：${esc(row.details.description)}</figcaption>${row.details.photo_path?`<img src="/api/media/${esc(row.details.photo_path)}" alt="${esc(row.lab)} ${esc(row.location_code)}位置照片" loading="lazy">`:''}</figure>`).join('');
  const alertData=await api('/api/alerts');ops.alerts=alertData.rows;$('#unsetThresholds')?.closest('.workflow-section').remove();$('#tab-alerts').insertAdjacentHTML('beforeend',box('待设置预警数量',`<div id="unsetThresholds">${alertData.unset_rows.length} 项未设置有效预警数量${table(['物品','位置','当前可用'],alertData.unset_rows.map(row=>[esc(row.item_name),`${esc(row.lab)} ${esc(row.location_code)}`,formatQuantity(row.available_quantity,row.unit)]))}</div>`));
  document.querySelectorAll('#alertRows tr').forEach((tr,index)=>{const row=alertData.rows[index];if(row){tr.lastElementChild.insertAdjacentHTML('beforeend',`<div class="toolbar">${rowButton('ack',row.id,'已关注','check')}${rowButton('snooze',row.id,'明天提醒','clock')}${rowButton('remind',row.id,'恢复提醒','bell')}</div>`);}});
  await Promise.all([loadHistory(),loadAI(),loadNotificationStatus()]);window.lucide?.createIcons();
};
window.renderPending=()=>{
  const pending=JSON.parse(localStorage.getItem('lab-pending')||'null');
  const notice=$('#pendingRequest');notice.hidden=!pending;
  if(!pending)return;
  notice.innerHTML='有一项操作的结果尚未确认。<button id="checkPending" type="button">查询结果</button><button id="retryPending" type="button">重试原请求</button>';
  $('#checkPending').onclick=async()=>{
    try{
      const result=await api('/api/operations/'+pending.id);
      if(result.status==='completed'){
        localStorage.removeItem('lab-pending');window.renderPending();toast('原请求已完成');await refreshAll();
      }else toast('暂未确认完成，可以重试原请求');
    }catch(error){showError(error);}
  };
  $('#retryPending').onclick=async()=>{
    try{
      let body=pending.body;
      if(body===null){
        const form=new FormData();
        for(const [key,saved] of pending.formEntries||[]){
          if(saved?.file){
            const file=$('#detailsPhoto').files[0];
            if(!file||file.name!==saved.name||file.size!==saved.size||file.lastModified!==saved.lastModified)throw new Error('请重新选择同一张位置照片，再重试原请求');
            form.append(key,file);
          }else form.append(key,saved);
        }
        body=form;
      }
      await api(pending.path,{method:'POST',...(body instanceof FormData?{}:{headers:{'Content-Type':'application/json'}}),body});
      toast('原请求已完成');await refreshAll();
    }catch(error){showError(error);}
  };
};
window.subscribeNotifications=async()=>{try{if(!window.isSecureContext)throw new Error('后台通知需要学校提供可信 HTTPS 地址');if(!('serviceWorker'in navigator)||!('PushManager'in window))throw new Error('当前浏览器不支持后台推送；iPhone 需要将网页添加到主屏幕后授权');const permission=await Notification.requestPermission();if(permission!=='granted')throw new Error('通知尚未获得授权');const config=await api('/api/notifications/config');if(!config.configured||!config.public_key)throw new Error('服务器尚未配置后台推送');const registration=await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;const raw=atob(config.public_key.replaceAll('-','+').replaceAll('_','/'));const key=Uint8Array.from(raw,char=>char.charCodeAt(0));const subscription=await registration.pushManager.getSubscription()||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});await api('/api/notifications/subscribe',{...jsonOptions,body:JSON.stringify(subscription.toJSON())});toast('后台通知已授权');}catch(error){showError(error);}};
initWorkflows();
function saveFormDraft(form){
  const fields={};
  form.querySelectorAll('input[id]:not([type=file]),select[id],textarea[id]').forEach(input=>{if(!input.id.startsWith('line'))fields[input.id]={value:input.value,checked:input.checked};});
  const lines=form.id==='lessonForm'?[...form.querySelectorAll('[data-line]')].map(el=>{const id=el.dataset.line;return {item_id:Number(value('lineItem-'+id)),action:value('lineAction-'+id),per_person:value('linePerson-'+id),per_group:value('lineGroup-'+id),fixed:value('lineFixed-'+id),spare:value('lineSpare-'+id)};}):null;
  localStorage.setItem('lab-form-'+form.id,JSON.stringify({fields,lines}));
}
function clearFormDraft(form){localStorage.removeItem('lab-form-'+form.id);}
function restoreFormDrafts(){
  document.querySelectorAll('form[id]').forEach(form=>{
    const saved=JSON.parse(localStorage.getItem('lab-form-'+form.id)||'null');
    if(!saved)return;
    for(const [id,field]of Object.entries(saved.fields)){
      const input=document.getElementById(id);if(input){input.value=field.value;input.checked=field.checked;}
    }
    if(saved.lines){$('#lessonLines').innerHTML='';saved.lines.forEach(addLessonLine);}
  });
  maintenanceMode();calculateDemand();
}
document.querySelectorAll('form[id]').forEach(form=>{
  form.addEventListener('input',()=>saveFormDraft(form));
  form.addEventListener('change',()=>saveFormDraft(form));
});
async function loadNotificationStatus(){
  let label=$('#notificationStatus');
  if(!label){label=document.createElement('p');label.id='notificationStatus';label.className='muted small-text';$('#tab-alerts').prepend(label);}
  if(!isSecureContext){label.textContent='后台通知不可用：学校可信 HTTPS 地址尚未配置';return;}
  if(!('PushManager'in window)||!('Notification'in window)||!('serviceWorker'in navigator)){label.textContent='当前浏览器不支持后台通知';return;}
  const config=await api('/api/notifications/config');
  if(!config.configured){label.textContent='服务器尚未配置后台通知';return;}
  const registration=await navigator.serviceWorker.getRegistration();
  const subscription=await registration?.pushManager.getSubscription();
  label.textContent=Notification.permission==='denied'?'通知权限已拒绝':Notification.permission!=='granted'?'后台通知尚未授权':subscription?'后台通知已授权':'通知已授权，设备尚未订阅';
}
$('#operatorName').value=localStorage.getItem('lab-actor')||'';$('#operatorName').addEventListener('input',()=>localStorage.setItem('lab-actor',$('#operatorName').value));
$('#chatText').value=localStorage.getItem('lab-chat-draft')||'';
const chatDraft=JSON.parse(localStorage.getItem('lab-chat-preview')||'null');
if(chatDraft?.text===$('#chatText').value){state.preview=chatDraft.data;state.selectedItemIds={};renderPreview(chatDraft.data);}
const prepareDraft=JSON.parse(localStorage.getItem('lab-prepare-preview')||'null');
if(prepareDraft){state.prepareRows=prepareDraft.rows;state.preparePreviewId=prepareDraft.id;state.prepareActions=prepareDraft.actions||{};renderPrepareRows(state.prepareRows);}
window.renderPending();
window.initialRefresh.then(()=>{restoreFormDrafts();if(!document.querySelector('[data-line]'))addLessonLine();}).catch(showError);
const requestedTab=new URLSearchParams(location.search).get('tab');if(requestedTab)document.querySelector(`.tab[data-tab="${CSS.escape(requestedTab)}"]`)?.click();
let refreshTimer;let refreshRunning=false;
function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(async()=>{if(refreshRunning||document.hidden)return;if(document.activeElement?.matches('input,select,textarea')){scheduleRefresh();return;}refreshRunning=true;try{await refreshAll();}finally{refreshRunning=false;}},800);}
const eventStream=new EventSource('/api/events');eventStream.onmessage=scheduleRefresh;eventStream.onerror=()=>setStatus('连接暂时中断，定期检查更新');
setInterval(()=>{if(eventStream.readyState!==EventSource.OPEN)scheduleRefresh();loadAI().catch(()=>{});},30000);
document.addEventListener('click',event=>{const button=event.target.closest('#historyList [data-undo-id]');if(button)undoTransaction(button.dataset.undoId).catch(showError);});
window.addEventListener('unhandledrejection',event=>{event.preventDefault();showError(event.reason);});
const scheduleFromPrepare=document.createElement('button');scheduleFromPrepare.type='button';scheduleFromPrepare.id='schedulePrepareBtn';scheduleFromPrepare.innerHTML='<i data-lucide="calendar-plus" class="icon-sm"></i>安排选中物品';$('#consumePrepareBtn').after(scheduleFromPrepare);
scheduleFromPrepare.addEventListener('click',()=>{
  const rows=state.prepareRows.filter(row=>state.prepareSelectedRows.has(String(row.row_number)));
  if(!rows.length||rows.some(row=>!(state.prepareActions[row.row_number]||row.action))){toast('请选择物品并确认使用方式');return;}
  $('#lessonLines').innerHTML='';
  for(const row of rows)for(const location of row.locations)if(Number(location.allocated_quantity)>0)addLessonLine({item_id:location.item_id,action:state.prepareActions[row.row_number]||row.action,fixed:location.allocated_quantity});
  $('#lessonName').value=rows[0].experiment;$('#lessonCourse').value=rows[0].course;$('#lessonPeople').value='0';
  saveFormDraft($('#lessonForm'));calculateDemand();document.querySelector('.tab[data-tab="lessons"]').click();
});
