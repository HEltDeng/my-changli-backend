"""Seed labelled activities by logging in as a teacher through the real API.
Local preview only. Never accepts a remote URL or production database.
"""
import argparse
import json
import secrets
import time
import urllib.request
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--state-dir',type=Path,required=True)
args=parser.parse_args()
state=args.state_dir.resolve()
if not (state/'CANDIDATE_ONLY').is_file(): raise SystemExit('Preview marker required')
base='http://127.0.0.1:8794'
def call(path,data=None,token=None,method=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    request=urllib.request.Request(base+path,json.dumps(data).encode() if data is not None else None,headers,method=method or ('POST' if data is not None else 'GET'))
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)['data']
admin=call('/api/v1/auth/login',json.loads((state/'local-test-access.json').read_text(encoding='utf-8')))['token']
teacher_file=state/'teacher-test-access.json'
if not teacher_file.exists():
    account={'username':'activity-preview-teacher','password':secrets.token_urlsafe(24)}
    call('/api/v1/admin/admins',{**account,'nickname':'预览验收老师','adminRole':'ACTIVITY_PUBLISHER','activityCollegeId':'国际工学院','activityPublishScope':'SCHOOL'},admin)
    teacher_file.write_text(json.dumps(account),encoding='utf-8')
teacher=call('/api/v1/auth/login',json.loads(teacher_file.read_text(encoding='utf-8')))['token']
existing=call('/api/v1/admin/activities',token=teacher)['items']
ids=[]
now=int(time.time()*1000)
for title,offset,deadline in [('【测试】人工智能与未来工程专题讲座',86400000,43200000),('【测试】大学生创新创业分享会',172800000,-3600000),('【测试】校园志愿活动',-172800000,-259200000)]:
    old=next((a for a in existing if a['title']==title),None)
    if old: ids.append(old['id']);continue
    a=call('/api/v1/admin/activities',dict(title=title,category='学术讲座' if '讲座' in title else '校园活动',organizer_name='国际工学院',college_id='国际工学院',audience='SCHOOL',location='云影报告厅',summary='仅用于校园活动第一版预览验收，不是真实学校通知。',description='这是隔离预览数据库中的测试活动，可体验报名、取消和后台名单同步。',latest_update='本条为预览验收测试数据。',start_at=now+offset,end_at=now+offset+7200000,signup_start_at=now-345600000,signup_end_at=now+deadline,capacity=200,requires_phone=False,status='PUBLISHED'),teacher)
    ids.append(a['id'])
(state.parent/'preview-activities.json').write_text(json.dumps({'base':base,'activities':ids,'teacher_created_via_api':True,'teacher_login_via_api':True,'published_via_teacher_api':True},indent=2),encoding='utf-8')
print(json.dumps({'teacher_login':'PASS','activity_ids':ids,'production_changes':False}))
