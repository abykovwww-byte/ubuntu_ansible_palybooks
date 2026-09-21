'use strict';
const $ = id => document.getElementById(id);
const fmt = (v, unit='', digits=1) => typeof v === 'number' && Number.isFinite(v) ? `${v.toLocaleString('ru-RU',{maximumFractionDigits:digits})}${unit}` : '—';
const phases = {warmup:'Разогрев',measure:'Измерение',idle:'Восстановление'};
const states = {running:'Идёт прогон',completed:'Прогон завершён',aborted:'Остановлен'};
const make = (tag, text, cls) => {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
function badge(el,text,cls){el.textContent=text;el.className=`badge ${cls||''}`;}
function chart(id,rows,series,fixedMax){
 const svg=$(id);svg.replaceChildren();const ns='http://www.w3.org/2000/svg';
 const node=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
 const vals=series.flatMap(s=>rows.map(s.get)).filter(v=>typeof v==='number'&&Number.isFinite(v));
 if(!vals.length){node('text',{x:280,y:80,'text-anchor':'middle',fill:'#8491a3','font-size':13},'Нет измерений');return;}
 const times=rows.map(r=>Date.parse(r.time));const first=times[0],span=times.at(-1)-first;
 const max=fixedMax||Math.max(...vals,1)*1.1, x=i=>44+(span>0?(times[i]-first)/span:0)*506, y=v=>132-116*v/max;
 for(let i=0;i<3;i++){const value=max*i/2;node('line',{x1:44,x2:550,y1:y(value),y2:y(value),stroke:'#e5ebf2'});node('text',{x:36,y:y(value)+4,'text-anchor':'end',fill:'#7b8a9e','font-size':10},fmt(value));}
 for(const s of series){let d='',pen=false;rows.forEach((r,i)=>{const v=s.get(r);if(typeof v!=='number'||!Number.isFinite(v)){pen=false;return;}d+=`${pen?'L':'M'}${x(i)},${y(v)} `;pen=true;});node('path',{d,fill:'none',stroke:s.color,'stroke-width':2});}
 for(const[i,anchor]of [[0,'start'],[rows.length-1,'end']]){const d=new Date(rows[i]?.time);node('text',{x:x(i),y:153,'text-anchor':anchor,fill:'#7b8a9e','font-size':10},Number.isNaN(d.getTime())?'':d.toLocaleTimeString('ru-RU'));}
}
function render(data){
 $('empty').hidden=true;$('content').hidden=false;
 const c=data.current||{}, last=data.samples.at(-1)||{}, ctx=data.context||{};
 $('test').textContent=[ctx.test_id,ctx.campaign_id,`Профиль ${data.profile||'—'}`].filter(Boolean).join(' / ');
 $('scenario').textContent=c.scenario||'Подготовка';
 let elapsed=Number.isFinite(Date.parse(c.phase_started_at))?(Date.now()-Date.parse(c.phase_started_at))/1000:null;
 const remaining=data.status==='running'&&elapsed!==null&&c.phase_duration_seconds ? Math.max(0,c.phase_duration_seconds-elapsed):null;
 $('phase').textContent=`${phases[c.phase]||'Ожидание'} · повтор ${c.repetition||'—'}${remaining!==null?' · осталось '+Math.ceil(remaining)+' с':''}`;
 badge($('status'),states[data.status]||data.status,data.status==='aborted'?'bad':data.status==='running'?'good':'');
 badge($('connection'),data.status!=='running'?'Архивный прогон':data.stale?'Данные устарели':'На связи',data.stale&&data.status==='running'?'bad':data.status==='running'?'good':'');
 $('freshness').textContent=`Последний отсчёт: ${fmt(data.sample_age_seconds,' с назад',0)}`;
 const alarms=[];if(data.reason)alarms.push(data.reason);if(data.stale&&data.status==='running')alarms.push('Свежих измерений нет. Это не подтверждение безопасного состояния.');
 if(last.lost>0)alarms.push(`Потери аудита: ${last.lost}`);
 $('alarm').hidden=!alarms.length;$('alarm').textContent=alarms.join(' · ');
 $('throughput').textContent=fmt(last.endpoint_rx_bps===null?null:last.endpoint_rx_bps/1e6,' Мбит/с');
 $('cpu').textContent=fmt(last.guest_cpu_pct,' %');
 const cores=Object.entries(last.cores||{}).filter(([,v])=>v!==null).sort((a,b)=>Number(a[0].slice(3))-Number(b[0].slice(3)));
 $('hottest').textContent='Самое занятое ядро: '+fmt(cores.length?Math.max(...cores.map(x=>x[1])):null,' %');
 const temps=Object.values(last.temperature_c||{}).filter(x=>x!==null);
 $('temperature').textContent=fmt(temps.length?Math.max(...temps):null,' °C');
 $('backlog').textContent=fmt(last.backlog_pct,' %');$('lost').textContent='Потерянные записи: '+fmt(last.lost,'',0);
 const o=data.offered||{};$('offered').textContent='Задано: '+(o.mbps?fmt(o.mbps,' Мбит/с'):o.rate?fmt(o.rate,' операций/с'):'—');
 $('lograte').textContent='audit.log: '+fmt(last.audit_bytes_s===null?null:last.audit_bytes_s/1024,' КиБ/с')+(last.rotation?' · ротация, скорость в этом интервале неизвестна':'');
 chart('traffic-chart',data.samples,[{get:r=>r.endpoint_rx_bps===null?null:r.endpoint_rx_bps/1e6,color:'#3679e7'}]);
 chart('cpu-chart',data.samples,[{get:r=>r.guest_cpu_pct,color:'#3679e7'},{get:r=>r.host_cpu_pct,color:'#93a0b2'}],100);
 chart('audit-chart',data.samples,[{get:r=>r.backlog_pct,color:'#9774cd'}],100);
 chart('probe-chart',data.samples,[{get:r=>r.service_probe_ms,color:'#379984'}]);
 $('cores').replaceChildren(...cores.map(([name,v])=>{const e=make('div',name,'core'+(typeof data.thresholds.single_core_pct==='number'&&v>=data.thresholds.single_core_pct?' hot':''));e.append(make('strong',fmt(v,'%')));return e;}));
 if(!cores.length)$('cores').append(make('p','Данные по ядрам не собраны'));
 const disk=Object.entries(last.disks||{}).map(([name,v])=>make('p',`${name}: await ${fmt(v.await_ms,' мс')} · запись ${fmt(v.write_bytes_s===null?null:v.write_bytes_s/1024,' КиБ/с')} · очередь ${fmt(v.avg_queue_depth)}`));
 disk.push(make('p',Object.entries(last.guest_cpu_breakdown||{}).map(([k,v])=>k+': '+fmt(v,'%')).join(' · ')));
 for(const p of last.processes||[])disk.push(make('p',`${p.comm}: ${fmt(p.cpu_one_core_pct,'% одного ядра')} · ${p.state} · ожидание ${p.wchan||'—'}`));
 disk.push(make('p','Изменение счётчика audit wait: '+fmt(last.audit_wait_delta)+' (единицы установленного ядра)'));
 const forwarder=last.forwarder||{};
 disk.push(make('p','Счётчики отправителя: '+(forwarder.age_seconds===undefined?'не собраны':fmt(forwarder.age_seconds,' с назад'))));
 for(const[name,v]of Object.entries(forwarder.counters||{}))disk.push(make('p',`${name}: очередь ${fmt(v.size,'',0)} · full ${fmt(v.full,'',0)} · dropped ${fmt(v['discarded.full'],'',0)}`));
 for(const v of last.collector?.counters||[])disk.push(make('p',`Коллектор ${v.id}: ${v.counter} ${fmt(v.value,'',0)}`));
 $('disk').replaceChildren(...disk);
 $('results').replaceChildren(...data.results.filter(r=>r.phase==='measure').map(r=>{const tr=make('tr');const m=r.metrics;[r.scenario,r.repetition,m.received_bps!==null?fmt(m.received_bps/1e6,' Мбит/с'):fmt(m.requests_per_second,' оп/с'),m.loss_pct!==null?fmt(m.loss_pct,' %'):fmt(m.errors,' ошибок',0),fmt(r.latency_bucket_upper_ms.p95)].forEach(v=>tr.append(make('td',v)));return tr;}));
 $('gaps').replaceChildren(...[...data.gaps,...(last.unavailable||[]).map(v=>'Недоступен источник: '+v)].map(x=>make('li',x)));
}
let busy=false;
async function refresh(){
 if(busy)return;busy=true;
 try{const res=await fetch('/api/runs',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);const runs=await res.json();
 const selected=$('runs').value; $('runs').replaceChildren(...runs.map(r=>{const o=make('option',`${r.id} · ${r.profile||'—'}`);o.value=r.id;return o;}));
 if(runs.some(r=>r.id===selected))$('runs').value=selected;
 if(!runs.length){$('empty').hidden=false;$('content').hidden=true;badge($('connection'),'Нет прогонов','');return;}
 const detail=await fetch('/api/run/'+encodeURIComponent($('runs').value),{cache:'no-store'});if(!detail.ok)throw Error('Run unavailable');render(await detail.json());
 }catch(error){badge($('connection'),'Нет связи','bad');$('alarm').hidden=false;$('alarm').textContent='Обновление недоступно. Показанные значения могут быть устаревшими.';}
 finally{busy=false;}
}
$('runs').addEventListener('change',refresh);refresh();setInterval(refresh,2000);
