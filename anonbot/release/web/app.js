'use strict';
const $ = id => document.getElementById(id);
const names = {chat:'聊天模型',bilibili:'B 站读取',embedding:'语义记忆',vision:'看图模型'};
const actions = {generate:'生成回复',read:'读取资料',embed:'生成向量',inspect:'观察画面'};
const results = {succeeded:'已完成',failed:'失败',pending:'未完成'};
let csrf = '', nextBefore = null, busy = false, generation = 0;
let contentBefore = null, contentGeneration = 0, reviewEnabled = false, traceFilter = null;
const text = (id, value) => { $(id).textContent = value; };
async function api(path, data) {
  const options = {credentials:'same-origin',cache:'no-store',headers:{}};
  if (data !== undefined) Object.assign(options,{method:'POST',body:JSON.stringify(data),headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}});
  const response = await fetch(path, options);
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(response.status === 429 ? '尝试过于频繁，请稍后再试' : response.status === 401 ? '请登录，或检查管理口令' : '读取失败，请稍后重试');
  }
  return response.json();
}
function showLogin(){csrf='';$('dashboard').hidden=true;$('login').hidden=false;$('logout').hidden=true;}
function showDashboard(){$('dashboard').hidden=false;$('login').hidden=true;$('logout').hidden=false;text('message','');}
function row(item){
  const tr=document.createElement('tr');
  const values=[new Date(item.started*1000).toLocaleString('zh-CN',{hour12:false}),`${names[item.service]||item.service} / ${actions[item.action]||item.action}`,item.host,'',item.duration_ms===null?'—':`${(item.duration_ms/1000).toFixed(2)} 秒`,''];
  for(const value of values){const td=document.createElement('td');td.textContent=value;tr.append(td);}
  const badge=document.createElement('span');badge.className='badge '+item.status;badge.textContent=results[item.status]||'未知';tr.children[3].append(badge);
  tr.children[5].textContent=item.mode==='fixture'?'模拟测试':'实际调用';
  const trace=document.createElement('span');trace.className='trace';trace.textContent=item.trace_id.slice(0,12);trace.title=item.trace_id;tr.children[5].append(trace);
  return tr;
}
async function load(append=false){
  if(busy)return;busy=true;const current=++generation;
  $('refresh').disabled=true;$('more').disabled=true;text('message','');
  try{
    const params=new URLSearchParams({limit:'50'});
    if($('service').value)params.set('service',$('service').value);
    if($('status').value)params.set('status',$('status').value);
    if(append&&nextBefore)params.set('before',String(nextBefore));
    if(traceFilter)params.set('trace_id',traceFilter);
    const [activity,summary,status]=await Promise.all([api('/api/activity?'+params),api('/api/summary'),api('/api/status')]);
    if(current!==generation)return;
    if(!append)$('rows').replaceChildren();
    for(const item of activity.items)$('rows').append(row(item));
    nextBefore=activity.next_before;$('more').hidden=!nextBefore;$('empty').hidden=$('rows').children.length>0;
    for(const key of ['total','succeeded','failed','pending'])text(key,summary[key]);
    text('modes',`实际 ${summary.live} · 模拟 ${summary.fixture}`);
    const max=Math.max(1,...summary.hourly.map(h=>h.count));$('chart').replaceChildren();
    const description=[];
    for(const hour of summary.hourly){const bar=document.createElement('div');bar.className='bar'+(hour.count?'':' zero');bar.style.height=`${Math.max(2,hour.count/max*100)}%`;bar.title=`${new Date(hour.hour*1000).getHours()} 时：${hour.count} 次`;$('chart').append(bar);description.push(bar.title);}
    $('chart').setAttribute('aria-label',description.join('；'));
    const providerLabel={not_configured:'待配置',configured_transfer_blocked:'已配置，外传关闭',configured_transfer_allowed:'已配置，待服务验收'};
    text('providers',`语义记忆：${providerLabel[status.providers.embedding]}　看图：${providerLabel[status.providers.vision]}`);
    text('updated','更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false}));
    reviewEnabled=status.content_review_enabled===true;
    await loadContent();
  }catch(error){text('message',error.message);}finally{busy=false;$('refresh').disabled=false;$('more').disabled=false;}
}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;try{const data=await api('/api/login',{token:$('token').value});$('token').value='';csrf=data.csrf;showDashboard();await load();}catch(error){text('message',error.message);}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{});generation++;showLogin();}catch(error){text('message',error.message);}});
$('refresh').addEventListener('click',()=>load());$('more').addEventListener('click',()=>load(true));
for(const id of ['service','status'])$(id).addEventListener('change',()=>{if(busy){generation++;busy=false;}load();});
function node(tag,value,className){const element=document.createElement(tag);if(value!==undefined)element.textContent=value;if(className)element.className=className;return element;}
function sourceLink(url,label){
  if(!/^https:\/\/www\.bilibili\.com\/video\/BV[0-9A-Za-z]{10}$/.test(url||''))return node('span','无公开来源链接','muted');
  const link=node('a',label||'查看公开来源');link.href=url;link.target='_blank';link.rel='noopener noreferrer';return link;
}
function relatedRequests(trace,label){
  const button=node('button',label);button.addEventListener('click',()=>{
    traceFilter=trace;$('clear-trace').hidden=false;$('service').value='';$('status').value='';
    if(busy){generation++;busy=false;}load();$('activity-section').scrollIntoView({block:'start'});
  });return button;
}
function contentCard(item){
  const content=item.content,review=item.review,card=node('article',undefined,'seen-card');
  const head=node('div',undefined,'seen-meta');head.append(node('span',({search:'搜索结果',info:'视频资料',subtitles:'字幕片段'})[item.kind]||'读取内容'),node('span',item.mode==='fixture'?'模拟示例':'实际读取'),node('span',new Date(item.created*1000).toLocaleString('zh-CN',{hour12:false})));card.append(head);
  card.append(node('h3',content.title));
  if(content.source_url)card.append(sourceLink(content.source_url));
  if(content.state!=='captured')card.append(node('p',content.state==='unavailable'?'未取得可供展示的正文，不代表原视频没有字幕':'读取未完成，没有取得正文','muted'));
  if(content.items.length){const list=node('ul',undefined,'seen-results');for(const result of content.items){const li=node('li');li.append(sourceLink(result.url,result.title),node('span',' · '+result.author,'muted'));list.append(li);}card.append(list);}
  const preview=content.fragments.find(fragment=>fragment.label==='视频简介')||content.fragments[0];
  if(preview)card.append(node('p',preview.text.slice(0,240)+(preview.text.length>240?'…':''),'seen-preview'));
  if(content.fragments.length){const details=node('details');details.append(node('summary',`展开已读取片段（${content.fragments.length} 条${content.truncated?'，已截断/整理':''}）`));for(const fragment of content.fragments){const p=node('p');p.append(node('span',fragment.label+' · ','muted'),node('span',fragment.text));details.append(p);}card.append(details);}
  const opinion=node('section',undefined,'seen-opinion');opinion.append(node('h4','爱音的看法'));
  if(review.status==='succeeded'){
    opinion.append(node('p',review.comment),node('small',review.simulation?'模拟评价，非真实模型效果':'模型生成的角色观点，未经事实核验','muted'));
    const citations=node('details');citations.append(node('summary','评价参考了哪些片段'));
    for(const cite of review.citations){const quote=node('blockquote',cite.quote);quote.append(node('small',' — '+(content.fragments[cite.fragment]?.label||'片段'),'muted'));citations.append(quote);}opinion.append(citations);
  }else if(review.status==='pending')opinion.append(node('p','评价生成中或上次处理中断；不会自动重复调用模型','muted'));
  else if(review.status==='failed')opinion.append(node('p','生成或引用检查失败，未保存为有效评价；不会自动重试','muted'));
  else{
    opinion.append(node('p',reviewEnabled?'尚未生成评价':'尚未生成评价，评价模型未启用','muted'));
    const button=node('button','生成爱音的看法');button.disabled=!reviewEnabled||content.state!=='captured'||!content.fragments.length;
    button.addEventListener('click',async()=>{button.disabled=true;button.textContent='正在生成…';try{await api('/api/content-review',{id:item.id});await loadContent();}catch(error){text('message',error.message);button.textContent='重新检查评价状态';button.disabled=false;}});opinion.append(button);
  }
  card.append(opinion);const footer=node('div',undefined,'seen-actions');footer.append(relatedRequests(item.trace_id,'查看读取请求'));if(review.trace_id)footer.append(relatedRequests(review.trace_id,'查看评价请求'));card.append(footer);return card;
}
async function loadContent(append=false){
  const current=++contentGeneration;$('content-more').disabled=true;
  try{
    const params=new URLSearchParams({limit:'12'});if(append&&contentBefore)params.set('before',String(contentBefore));if($('content-kind').value)params.set('kind',$('content-kind').value);
    const data=await api('/api/content?'+params);if(current!==contentGeneration)return;
    if(!append)$('content-rows').replaceChildren();for(const item of data.items)$('content-rows').append(contentCard(item));
    contentBefore=data.next_before;$('content-more').hidden=!contentBefore;$('content-empty').hidden=$('content-rows').children.length>0;
  }catch(error){text('message',error.message);}finally{if(current===contentGeneration)$('content-more').disabled=false;}
}
$('content-kind').addEventListener('change',()=>loadContent());$('content-more').addEventListener('click',()=>loadContent(true));
$('clear-trace').addEventListener('click',()=>{traceFilter=null;$('clear-trace').hidden=true;load();});
(async()=>{try{const session=await api('/api/session');csrf=session.csrf;showDashboard();await load();}catch{showLogin();}})();
