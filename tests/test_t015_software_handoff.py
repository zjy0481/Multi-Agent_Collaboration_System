import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('software_handoff', Path(__file__).resolve().parents[1] / 'scripts/t015/software_handoff.py')
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


def test_independent_metrics_and_unsupported_intervals(tmp_path):
    path = tmp_path / 'bookings.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE bookings (room TEXT, day TEXT, start TEXT, end TEXT, status TEXT)')
        db.executemany('INSERT INTO bookings VALUES (?,?,?,?,?)', [
            ('A','2026-12-01','09:30','10:30','active'),
            ('B','2026-12-01','10:00','11:00','active'),
            ('A','2026-12-02','09:00','10:00','cancelled'),
            ('C','2026-12-03','09:00','10:00','active'),
        ])
    schema = {'table':'bookings','room':'room','date':'day','start':'start','end':'end',
              'status':'status','cancelled_values':['cancelled'],'active_values':['active']}
    result = handoff._metrics(path, schema, '2026-12-01', '2026-12-02')
    assert result == {'total_count':2,'total_minutes':120,'cancelled_count':1,
                      'rooms':{'A':{'count':1,'minutes':60,'cancelled_count':1},
                               'B':{'count':1,'minutes':60,'cancelled_count':0}},
                      'busiest_dates':['2026-12-01'],'busiest_periods':['10:00']}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO bookings VALUES ('A','2026-12-01','23:00','01:00','active')")
    with pytest.raises(ValueError):
        handoff._metrics(path, schema, '2026-12-01', '2026-12-02')


def test_mount_receipts_are_scoped_successful_and_completed():
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE execution_receipts(turn_id,call_id,arguments,result,name,state);
        CREATE TABLE scheduled_turns(turn_id,event_id,session_id,purpose);
        CREATE TABLE message_deliveries(event_id,session_id,purpose,message_id);''')
    for n, message, result, state in [(1,'t2',{'ok':True,'result':{'exit_code':0}},'completed'),
                                      (2,'other',{'ok':True,'result':{'exit_code':0}},'completed'),
                                      (3,'t2',{'ok':True,'result':{'exit_code':1}},'completed'),
                                      (4,'t2',{'ok':True,'result':{'exit_code':0}},'started')]:
        db.execute('INSERT INTO execution_receipts VALUES (?,?,?,?,?,?)',
                   (str(n),str(n),json.dumps({'artifact_version_ids':['snapshot']}),json.dumps(result),'run_command',state))
        db.execute('INSERT INTO scheduled_turns VALUES (?,?,?,?)',(str(n),n,'s','work'))
        db.execute('INSERT INTO message_deliveries VALUES (?,?,?,?)',(n,'s','work',message))
    receipts = handoff._receipts(db, {'t2'})
    assert len(receipts) == 1 and receipts[0]['call_id'] == '1'
    assert receipts[0]['version_ids'] == ['snapshot']


def test_t1_readiness_requires_complete_approved_hash_link_and_receipt(monkeypatch):
    from types import SimpleNamespace
    app_item = {'version_id':'app','valid':True,'readable':True,'sha256':'ah'}
    data_item = {'version_id':'db','valid':True,'readable':True,'sha256':'dh'}
    provenance = {'valid':True,'readable':True,'version_id':'p','data':{
        'app_version_id':'app','app_sha256':'ah','database_sha256':'dh',
        'source_database_path':'data/bookings.sqlite3','backup_method':'sqlite3.Connection.backup',
        'operations':[{'method':'POST','status':201},{'method':'DELETE','status':200}]}}
    frozen = {'final_versions':[{'version_id':'app'},{'version_id':'db'}],
              'task_acceptance':{'task':{'versions':[{'version_id':'app'},{'version_id':'db'}]}}}
    terminal = {'outcome':'completed','frozen':frozen}
    monkeypatch.setattr(handoff,'_context',lambda *a: (None,None,terminal,{'scope'}))
    monkeypatch.setattr(handoff,'_artifacts',lambda *a: {'t1-app.zip':[app_item],
        't1-bookings.sqlite3':[data_item], 't1-data-provenance.json':[provenance]})
    monkeypatch.setattr(handoff,'_receipts',lambda *a: [{'call_id':'call','turn_id':'turn','stdout':'T015_SNAPSHOT_SHA256=dh\n'}])
    result = handoff.audit_t1_handoff(SimpleNamespace(), {}, 'assignment')
    assert result['strict_ready'] and result['app_approved'] and result['database_approved']
    provenance['data']['source_database_path'] = '../outside.db'
    assert not handoff.audit_t1_handoff(SimpleNamespace(), {}, 'assignment')['strict_ready']
    provenance['data']['source_database_path'] = 'data/bookings.sqlite3'
    terminal['outcome'] = 'cancelled'
    assert not handoff.audit_t1_handoff(SimpleNamespace(), {}, 'assignment')['strict_ready']


def test_t2_ready_needs_both_roles_and_scoped_published_report(monkeypatch):
    from types import SimpleNamespace
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE assignment_required_inputs(message_id,owner_id,version_id);
      CREATE TABLE execution_receipts(turn_id,call_id,arguments,result,name,state);
      CREATE TABLE scheduled_turns(turn_id,event_id,session_id,purpose);
      CREATE TABLE message_deliveries(event_id,session_id,purpose,message_id);''')
    for owner in ('d','q'):
        for version in ('app','db'):
            db.execute('INSERT INTO assignment_required_inputs VALUES (?,?,?)',('t2',owner,version))
    db.execute('INSERT INTO execution_receipts VALUES (?,?,?,?,?,?)',('pub','p','{}',json.dumps({'ok':True,'result':{'version_id':'report'}}),'publish_artifact','completed'))
    db.execute('INSERT INTO scheduled_turns VALUES (?,?,?,?)',('pub',1,'ds','work'))
    db.execute('INSERT INTO message_deliveries VALUES (?,?,?,?)',(1,'ds','work','m'))
    team = {'members':[{'role_id':'dev','member_id':'d','session_id':'ds'}, {'role_id':'qa','member_id':'q','session_id':'qs'}], 'team_id':'team'}
    report = {'version_id':'report','valid':True,'readable':True,'author_session_id':'ds',
              'data':{'database_version_id':'db','range_start':'2026-01-01','range_end':'2026-01-01','metrics':{'count':1}}}
    receipts = [{'session_id':s,'version_ids':['app','db'],'turn_id':s,'call_id':s} for s in ('ds','qs')]
    monkeypatch.setattr(handoff,'_context',lambda *a:(db,None,{'outcome':'completed'},{'m'}))
    monkeypatch.setattr(handoff,'_receipts',lambda *a:receipts)
    monkeypatch.setattr(handoff,'_artifacts',lambda *a:{'t2-dashboard-check.json':[report]})
    monkeypatch.setattr(handoff,'_metrics',lambda *a:{'count':1})
    app = SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(artifacts=SimpleNamespace(resolve=lambda *a:({},Path('unused'))))))
    previous = {'app_version_id':'app','database_version_id':'db','provenance':{'schema':{}}}
    assert handoff.audit_t2_handoff(app,team,'t2',previous)['dashboard_evidence_ready']
    receipts.pop()
    assert not handoff.audit_t2_handoff(app,team,'t2',previous)['dashboard_evidence_ready']
