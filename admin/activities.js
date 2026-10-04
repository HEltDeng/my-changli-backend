/* Uses the existing authenticated admin shell, styles and dialogs. */
titles.activities = '活动管理';
titles.activityEditor = '发布活动';
titles.activityRegistrations = '报名记录';
roleLabels.ACTIVITY_PUBLISHER = '活动发布老师';
const activityTypes = ['学术讲座','校园活动','志愿活动','比赛竞赛','文体活动','其他'];
const activityStates = {OPEN:'报名中',FULL:'名额已满',CLOSED:'已截止',UPCOMING:'即将开始',ONGOING:'进行中',ENDED:'已结束',CANCELLED:'已取消',DRAFT:'草稿',UNLISTED:'已下架'};
let activityDraft = null;
let activityPoll = null;
const originalOverview = renderers.overview;
renderers.overview = async (...args) => state.admin.adminRole === 'ACTIVITY_PUBLISHER' ? renderers.activities() : originalOverview(...args);

function activitySummary(a) {
  return `<div class="stack"><h3>${escapeHtml(a.title)}</h3><p>${escapeHtml(a.category)} · ${escapeHtml(a.organizer_name)}</p><p>发布学院：${escapeHtml(a.college_id)} · ${a.audience==='SCHOOL'?'全校可见':'本学院可见'}</p><p>${formatTime(a.start_at)}—${formatTime(a.end_at)} · 约 ${(a.end_at-a.start_at)/3600000} 小时</p><p>${escapeHtml(a.location)}</p><p>报名：${formatTime(a.signup_start_at)} 至 ${formatTime(a.signup_end_at)}</p><p>名额：${a.capacity??'不限'} · ${a.requires_phone?'需确认联系电话':'仅姓名、学号、学院'}</p><p>${escapeHtml(a.summary)}</p><p class="activity-description">${escapeHtml(a.description)}</p>${a.latest_update?`<div class="notice">最新情况：${escapeHtml(a.latest_update)}</div>`:''}<p>${escapeHtml(a.contact||'')}</p></div>`;
}

renderers.activities = async (filters={}) => {
  const result = await api('/api/v1/admin/activities?'+new URLSearchParams(filters));
  content.innerHTML = `<div class="card"><div class="actions"><button class="primary" data-activity="create">发布活动</button><button class="ghost" data-activity="refresh">刷新</button></div><form id="activity-filter" class="filters"><label>搜索<input name="q" value="${escapeHtml(filters.q||'')}" placeholder="活动名称 / 主办单位"></label>${state.admin.adminRole==='SUPER_ADMIN'?`<label>学院<input name="college" value="${escapeHtml(filters.college||'')}"></label>`:''}<label>状态<select name="state"><option value="">全部</option>${Object.entries(activityStates).map(([v,l])=>`<option value="${v}" ${filters.state===v?'selected':''}>${l}</option>`).join('')}</select></label><label>开始时间从<input type="datetime-local" name="from" value="${datetimeLocal(filters.from)}"></label><label>至<input type="datetime-local" name="to" value="${datetimeLocal(filters.to)}"></label><button class="primary">筛选</button></form><div class="table-wrap"><table><thead><tr><th>活动</th><th>学院</th><th>活动时间</th><th>报名截止</th><th>人数</th><th>状态</th><th>操作</th></tr></thead><tbody>${result.items.map(a=>`<tr><td><strong>${escapeHtml(a.title)}</strong></td><td>${escapeHtml(a.college_id)}</td><td>${formatTime(a.start_at)}</td><td>${formatTime(a.signup_end_at)}</td><td>${a.registered_count} / ${a.capacity??'不限'}</td><td>${escapeHtml(a.state_label)}</td><td><div class="actions">${[['view','查看'],['edit','编辑'],['registrations','报名名单'],['share','复制活动链接']].map(([v,l])=>`<button class="ghost" data-activity="${v}" data-id="${a.id}">${l}</button>`).join('')}</div></td></tr>`).join('')||'<tr><td colspan="7">暂无活动</td></tr>'}</tbody></table></div></div>`;
  document.querySelector('#activity-filter').onsubmit=e=>{
    e.preventDefault();const values=Object.fromEntries(new FormData(e.target));
    for(const k of ['from','to']) values[k]=values[k]?String(new Date(values[k]).getTime()):'';
    navigate('activities',values);
  };
};

renderers.activityEditor = async ({id}={}) => {
  const a=id?await api('/api/v1/admin/activities/'+id):{};
  const superAdmin=state.admin.adminRole==='SUPER_ADMIN';
  const college=a.college_id||state.admin.activityCollegeId||'';
  const input=(name,label,type='text',required=true)=>`<label>${label}<input name="${name}" type="${type}" ${required?'required':''} ${type==='number'?'min="1" max="1000000"':''} value="${escapeHtml(type==='datetime-local'?datetimeLocal(a[name]):a[name]??'')}"></label>`;
  content.innerHTML=`<div class="card"><form id="activity-editor" class="stack">${input('title','活动名称')}<label>活动类型<select name="category">${activityTypes.map(t=>`<option ${a.category===t?'selected':''}>${t}</option>`).join('')}</select></label><label>发布学院<input name="college_id" required maxlength="120" value="${escapeHtml(college)}" ${!superAdmin||id?'readonly':''}></label><label>主办单位<input name="organizer_name" required maxlength="120" value="${escapeHtml(a.organizer_name||college)}" ${superAdmin?'':'readonly'}></label><label>可见范围<select name="audience"><option value="COLLEGE">本学院</option>${superAdmin||state.admin.activityPublishScope==='SCHOOL'?`<option value="SCHOOL" ${a.audience==='SCHOOL'?'selected':''}>全校</option>`:''}</select></label>${input('location','活动地点')}${input('start_at','活动开始','datetime-local')}${input('end_at','活动结束','datetime-local')}<p id="activity-duration" class="muted"></p>${input('signup_start_at','报名开始','datetime-local')}${input('signup_end_at','报名截止','datetime-local')}${input('capacity','人数上限（留空不限）','number',false)}<label>活动简介<textarea name="summary" maxlength="500" required>${escapeHtml(a.summary||'')}</textarea></label><label>活动详情<textarea name="description" rows="7" maxlength="12000" required>${escapeHtml(a.description||'')}</textarea></label><label>最新情况 / 补充通知<textarea name="latest_update" maxlength="2000">${escapeHtml(a.latest_update||'')}</textarea></label>${input('contact','联系方式（选填）','text',false)}<p>姓名、学号、学院：自动使用学生已绑定学校资料</p><label class="checkbox-row"><input name="requires_phone" type="checkbox" ${a.requires_phone?'checked':''}>收集联系电话（学生逐次确认）</label><label>保存状态<select name="status">${[['PUBLISHED','发布'],['DRAFT','草稿'],...(id?[['UNLISTED','下架'],['CANCELLED','取消活动']]:[])].map(([v,l])=>`<option value="${v}" ${a.status===v?'selected':''}>${l}</option>`).join('')}</select></label><div class="actions"><button class="primary">预览活动</button><button type="button" class="ghost" data-activity="refresh">返回列表</button></div></form><section id="activity-preview" class="hidden"></section></div>`;
  const form=document.querySelector('#activity-editor');
  form.oninput=()=>{
    const data=new FormData(form);const duration=(new Date(data.get('end_at'))-new Date(data.get('start_at')))/3600000;
    document.querySelector('#activity-duration').textContent=duration>0?`活动时长约 ${duration} 小时`:'';
  };
  form.onsubmit=e=>{
    e.preventDefault();const data=Object.fromEntries(new FormData(form));
    for(const k of ['start_at','end_at','signup_start_at','signup_end_at']) data[k]=new Date(data[k]).getTime();
    data.capacity=data.capacity?Number(data.capacity):null;data.requires_phone=form.elements.requires_phone.checked;
    if(!(data.signup_start_at<data.signup_end_at&&data.signup_end_at<=data.start_at&&data.start_at<data.end_at)) return toast('请检查报名和活动时间顺序',true);
    activityDraft={id,data};const preview=document.querySelector('#activity-preview');
    preview.innerHTML=activitySummary(data)+`<div class="actions"><button type="button" class="primary" data-activity="publish">确认${data.status==='PUBLISHED'?'发布':'保存'}</button><button type="button" class="ghost" data-activity="back-edit">返回编辑</button></div>`;
    form.classList.add('hidden');preview.classList.remove('hidden');preview.scrollIntoView({behavior:'smooth'});
  };
};

renderers.activityRegistrations = async ({id}={}) => {
  if(!id) {
    const result=await api('/api/v1/admin/activities');
    content.innerHTML=`<div class="card"><h3>选择活动查看报名记录</h3>${result.items.map(a=>`<p><button class="ghost" data-activity="registrations" data-id="${a.id}">${escapeHtml(a.title)} · ${a.registered_count} 人</button></p>`).join('')||'<p>暂无活动</p>'}</div>`;return;
  }
  const refresh=async()=>{
    const result=await api(`/api/v1/admin/activities/${id}/registrations`);const a=result.activity;
    if(state.page!=='activityRegistrations') return;
    content.innerHTML=`<div class="card"><h3>${escapeHtml(a.title)}</h3><p>活动名额：${a.capacity??'不限'} · 有效报名：${a.registered_count} · 已取消：${a.cancelled_count} · 剩余：${a.remaining??'不限'}</p><p class="muted">每 5 秒刷新 · 最近更新 ${formatTime(Date.now())}</p><div class="actions"><button class="primary" data-activity="export" data-id="${id}">导出报名名单</button><button class="ghost" data-activity="registrations" data-id="${id}">刷新</button></div><div class="table-wrap"><table><thead><tr><th>姓名</th><th>学号</th><th>学院</th>${a.requires_phone?'<th>联系电话</th>':''}<th>报名时间</th><th>状态</th></tr></thead><tbody>${result.items.map(r=>`<tr><td>${escapeHtml(r.student_name)}</td><td>${escapeHtml(r.student_number)}</td><td>${escapeHtml(r.college_id)}</td>${a.requires_phone?`<td>${escapeHtml(r.phone||'未提供')}</td>`:''}<td>${formatTime(r.registered_at)}</td><td>${r.registration_status==='ACTIVE'?'已报名':'已取消'}</td></tr>`).join('')||'<tr><td colspan="6">暂无报名</td></tr>'}</tbody></table></div></div>`;
  };
  clearInterval(activityPoll);await refresh();
  activityPoll=setInterval(()=>{
    if(state.page!=='activityRegistrations'||!state.token){clearInterval(activityPoll);return;}
    if(!document.hidden) refresh().catch(()=>{});
  },5000);
};

document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-activity]');if(!button)return;
  const action=button.dataset.activity,id=button.dataset.id;
  try {
    if(action==='create') return navigate('activityEditor');
    if(action==='edit') return navigate('activityEditor',{id});
    if(action==='refresh') return navigate('activities');
    if(action==='registrations') return navigate('activityRegistrations',{id});
    if(action==='view') {const a=await api('/api/v1/admin/activities/'+id);return openDialog('活动详情',activitySummary(a),async()=>{},'关闭');}
    if(action==='share') {const a=await api('/api/v1/admin/activities/'+id);await navigator.clipboard.writeText(a.share_url);return toast('活动链接已复制（需安装我的长理）');}
    if(action==='back-edit') {document.querySelector('#activity-editor').classList.remove('hidden');document.querySelector('#activity-preview').classList.add('hidden');return;}
    if(action==='publish') {
      button.disabled=true;const d=activityDraft;
      await api('/api/v1/admin/activities'+(d.id?'/'+d.id:''),{method:d.id?'PUT':'POST',body:d.data});
      activityDraft=null;toast('活动已保存');return navigate('activities');
    }
    if(action==='export') {
      button.disabled=true;const r=await api(`/api/v1/admin/activities/${id}/registrations/export`);
      const bytes=Uint8Array.from(atob(r.content_base64),c=>c.charCodeAt(0));
      const url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
      const link=document.createElement('a');link.href=url;link.download=r.filename;
      document.body.appendChild(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),60000);
      toast('报名名单已生成，请查看浏览器下载');
    }
  } catch(error){toast(error.message,true);} finally {button.disabled=false;}
});
