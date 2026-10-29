'use strict';
const $ = id => document.getElementById(id);
const state = {tasks: [], selected: null, csrf: '', signature: '', generation: 0, exportEnabled: false};
const labels = {pending:'待运行',running:'进行中',paused:'已暂停',completed:'已完成',blocked:'需处理',simulated:'模拟完成',succeeded:'已完成',failed:'未完成',unknown:'结果待核对',executing:'读取中',ready:'待执行',expired:'已到期'};
const toolNames = {'bilibili.search':'搜索视频','bilibili.info':'读取视频详情','bilibili.subtitles':'检查公开字幕','android.devices':'检查设备列表','android.inspect':'识别当前应用','android.snapshot':'保存截图'};
const reasons = {all_steps_completed:'所有步骤已完成',step_budget_reached:'本次的一步已完成，可以继续运行',stop_requested:'已收到停止请求，后续步骤暂停',interrupted:'任务已中断，可以核对进度后继续',binding_or_arguments_unavailable:'没有找到可读取的视频，请换个关键词新建任务',action_failed_uncertain_or_unavailable:'读取未完成，后续步骤已停止',worker_or_storage_failure:'本地执行遇到问题，请查看任务记录',running:'正在读取资料',submitted:'等待开始'};
function el(tag, className, text) {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function badge(text, kind='') {return el('span','pill '+kind,text);}
function showError(error) {$('error').textContent=error.message || '连接中断，请刷新重试';$('error').hidden=false;}
async function api(path, body) {
  const response=await fetch(path,{credentials:'same-origin',method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-Console-CSRF':state.csrf},body:body===undefined?undefined:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw new Error(value.error || '操作未完成');return value;
}
function message(text) {$('message').textContent=text;$('error').hidden=true;}
function matches(task) {const filter=$('filter').value;return filter==='all'||(filter==='active'&&(task.active||task.status==='running'))||(filter==='completed'&&task.status==='completed')||(filter==='attention'&&['paused','blocked'].includes(task.status));}
function renderList() {
  const focused=document.activeElement?.dataset.taskId;
  const list=$('task-list');list.replaceChildren();$('count').textContent=state.tasks.length;
  const shown=state.tasks.filter(matches);
  if(!shown.length){list.append(el('p','empty',state.tasks.length?'这个分类暂时没有任务':'还没有任务，从上方开始一次读取吧'));return;}
  for(const task of shown){
    const button=el('button','task-row'+(task.id===state.selected?' selected':''));button.type='button';button.dataset.taskId=task.id;button.setAttribute('aria-pressed',String(task.id===state.selected));
    button.append(el('strong','',task.title));const meta=el('span','task-meta');
    meta.append(badge(labels[task.status]||'待核对',task.status),badge(task.mode==='live'?'真实查询':'离线预览',task.mode==='live'?'live':''));
    const time=el('time','',new Date(task.created_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}));meta.append(time);button.append(meta);
    button.addEventListener('click',()=>{state.selected=task.id;state.signature='';renderList();refresh().catch(showError);});const row=el('div');row.setAttribute('role','listitem');row.append(button);list.append(row);
  }
  if(focused)list.querySelector(`[data-task-id="${focused}"]`)?.focus({preventScroll:true});
}
function safeVideoLink(url) {try{const parsed=new URL(url);return parsed.origin==='https://www.bilibili.com'&&!parsed.username&&!parsed.password&&!parsed.search&&!parsed.hash&&/^\/video\/BV[0-9A-Za-z]{10}$/.test(parsed.pathname)?parsed.href:null;}catch{return null;}}
function stepKind(status) {if(['simulated','succeeded'].includes(status))return 'done';if(['failed','unknown','expired'].includes(status))return 'failed';return '';}
function link(text,url) {const href=safeVideoLink(url);if(!href)return el('p','result-title',text);const node=el('a','result-link',text+' ↗');node.href=href;node.target='_blank';node.rel='noopener noreferrer';return node;}
function renderResults(task) {
  const section=el('section','results');section.append(el('h3','','读取结果'));let count=0;
  task.steps.forEach((step,index)=>{
    const data=step.data;if(!data)return;
    const block=el('div','result-block');
    if(data.operation==='search'&&Array.isArray(data.items)){
      block.append(el('div','result-title','找到的视频'));
      for(const item of data.items){block.append(link(item.title,item.url),el('p','',`${item.author} · ${item.duration}`));}
    }else if(data.operation==='info'){
      block.append(link(data.title,data.url),el('p','',data.author?.name||''),el('p','',data.description||'没有简介'));
    }else if(data.artifact){
      const figure=el('figure');const image=el('img');image.src=`/api/tasks/${task.id}/image/${index}`;image.alt=data.simulation?'离线合成棋盘格示例':'任务保存的设备截图';image.loading='lazy';
      figure.append(image,el('figcaption','',`${data.simulation?'离线合成图片':'本地截图'} · ${data.artifact.width} × ${data.artifact.height}`));block.append(el('div','result-title','图片记录'),figure);
    }else if(data.operation==='devices'){
      block.append(el('div','result-title','设备检查'),el('p','',data.simulation?'使用离线测试设备，未连接真实设备':`读取到 ${data.devices?.length||0} 台设备`));
    }else if(data.reason){block.append(el('p','','本次读取未取得可用结果，请查看步骤状态'));}else{return;}
    section.append(block);count++;
  });
  return count?section:null;
}
function actionButton(text,operation,budget,disabled=false) {
  const button=el('button',operation==='run'&&budget===10?'primary':'',text);button.type='button';button.disabled=disabled;
  button.addEventListener('click',async()=>{const selected=state.selected;button.disabled=true;try{await api(`/api/tasks/${selected}/${operation}`,operation==='run'?{max_steps:budget}:{});message(operation==='stop'?'已请求停止后续步骤':'已请求继续任务');state.signature='';await refresh();if(state.selected===selected)document.querySelector('#task-detail h2')?.focus({preventScroll:true});}catch(error){showError(error);button.disabled=false;}});return button;
}
function downloadButton(text,format,task) {
  const button=el('button','',text);button.type='button';button.disabled=task.active||task.status==='running';
  button.addEventListener('click',async()=>{
    const controls=[...button.closest('.detail-actions').querySelectorAll('button')];
    const disabled=controls.map(control=>control.disabled);controls.forEach(control=>{control.disabled=true;});let objectUrl;
    try{
      const response=await fetch(`/api/tasks/${task.id}/export/${format}`,{credentials:'same-origin'});
      if(!response.ok){const error=await response.json();throw new Error(error.error||'导出未完成');}
      const blob=await response.blob();objectUrl=URL.createObjectURL(blob);
      const anchor=el('a');anchor.href=objectUrl;anchor.download=`anon-task-${task.id}.${format}`;
      document.body.append(anchor);anchor.click();anchor.remove();message('下载已开始');
    }catch(error){showError(error);}finally{if(objectUrl)setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);controls.forEach((control,index)=>{control.disabled=disabled[index];});}
  });return button;
}
function renderDetail(task) {
  const signature=JSON.stringify(task);if(signature===state.signature)return;state.signature=signature;
  const detail=$('task-detail');const hadFocus=detail.contains(document.activeElement);detail.replaceChildren();const header=el('div','detail-header');const heading=el('h2','',task.title);heading.tabIndex=-1;header.append(badge(labels[task.status]||'待核对',task.status),heading,el('p','',task.notice||reasons[task.detail]||'请核对步骤状态'));
  const actions=el('div','detail-actions');const stopped=!task.active&&['paused','pending','running'].includes(task.status);
  actions.append(actionButton('继续运行','run',10,!stopped),actionButton('运行一步','run',1,!stopped),actionButton('停止后续步骤','stop',0,!task.active));if(state.exportEnabled)actions.append(downloadButton('下载报告','md',task),downloadButton('下载资料包','zip',task));header.append(actions);detail.append(header);
  const timeline=el('ol','timeline');
  task.steps.forEach((step,index)=>{const row=el('li');const done=['simulated','succeeded'].includes(step.status);row.append(el('span','step-icon '+stepKind(step.status),done?'✓':index+1));const content=el('div','step-content'),line=el('div','step-line');line.append(el('strong','',toolNames[step.tool]||'读取步骤'),badge(labels[step.status]||'待核对',step.status));content.append(line);if(step.status==='simulated')content.append(el('p','','离线示例数据'));row.append(content);timeline.append(row);});
  detail.append(timeline);const results=renderResults(task);if(results)detail.append(results);if(hadFocus)heading.focus({preventScroll:true});
}
async function refresh() {
  const generation=++state.generation;const result=await api('/api/tasks');if(generation!==state.generation)return;
  state.tasks=result.tasks;if(!state.selected&&state.tasks.length)state.selected=state.tasks[0].id;renderList();
  const selected=state.selected;if(selected){const task=await api('/api/tasks/'+selected);if(generation===state.generation&&selected===state.selected)renderDetail(task);}
  $('connection').textContent='本机服务已连接';
}
async function createTask(template) {
  const button=$(template==='demo'?'demo':'create');button.disabled=true;
  try{const task=await api('/api/tasks',{template,query:template==='demo'?'':$('query').value.trim(),live:template==='demo'?false:$('mode').value==='live',max_steps:Number($('budget').value)});state.selected=task.id;state.signature='';message('任务已创建');await refresh();}catch(error){showError(error);}finally{button.disabled=false;}
}
$('task-form').addEventListener('submit',event=>{event.preventDefault();createTask('bili-research');});
$('demo').addEventListener('click',()=>createTask('demo'));
$('filter').addEventListener('change',renderList);
$('mode').addEventListener('change',()=>{$('mode-hint').textContent=$('mode').value==='live'?'将访问公开B站接口，只读取资料':'离线预览使用固定示例，不会访问 B站';});
$('refresh').addEventListener('click',()=>refresh().then(()=>message('状态已刷新')).catch(showError));
async function poll(){try{await refresh();}catch(error){$('connection').textContent='连接中断，正在等待恢复';showError(error);}setTimeout(poll,state.tasks.some(task=>task.active)?1200:5000);}
(async()=>{try{const session=await api('/api/session');state.csrf=session.csrf;state.exportEnabled=session.features?.exports===true;await poll();}catch(error){showError(error);}})();
