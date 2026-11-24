"""Conservative, read-only T015 handoff audits. Call on the app portal thread."""
import json
import sqlite3
import zipfile
from contextlib import closing
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath


def _context(app, team, assignment):
    db = app.state.runtime.db
    team_id = team['team_id']
    row = db.execute('SELECT id FROM team_goals WHERE team_id=? AND source_message_id=?', (team_id, assignment)).fetchone()
    lifecycle = app.state.collaboration.goal_lifecycle
    terminal = lifecycle._terminal(source_message_id=assignment)
    if terminal and terminal['team_id'] != team_id:
        terminal = None
    scope = lifecycle._scope_objects(team_id, row['id'] if row else None, assignment)
    return db, lifecycle, terminal, set(scope['message_ids'])


def _json(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None


def _receipts(db, message_ids):
    if not message_ids:
        return []
    marks = ','.join('?' for _ in message_ids)
    rows = db.execute(f'''SELECT r.turn_id, r.call_id, r.arguments, r.result, d.session_id FROM execution_receipts r
        JOIN scheduled_turns s ON s.turn_id=r.turn_id
        JOIN message_deliveries d ON (d.event_id,d.session_id,d.purpose)=(s.event_id,s.session_id,s.purpose)
        WHERE d.message_id IN ({marks}) AND r.name='run_command' AND r.state='completed' ''', tuple(message_ids))
    result = []
    for row in rows:
        args, output = _json(row['arguments']), _json(row['result'])
        if not isinstance(args, dict) or not isinstance(output, dict):
            continue
        data = output.get('result') or {}
        if output.get('ok') and data.get('exit_code') == 0 and not data.get('timed_out'):
            result.append({'turn_id': row['turn_id'], 'call_id': row['call_id'],
                           'session_id': row['session_id'], 'version_ids': args.get('artifact_version_ids', []),
                           'stdout': data.get('stdout', '')})
    return result


def _artifacts(app, team, names):
    db, store = app.state.runtime.db, app.state.workspace.artifacts
    members = {m['role_id']: m for m in team['members']}
    candidates = {name: [] for name in names}
    for row in db.execute('SELECT version_id, source_path, author_session_id FROM artifact_versions WHERE team_id=? ORDER BY rowid DESC', (team['team_id'],)):
        name = PurePosixPath(row['source_path'].replace('\\', '/')).name
        if name not in candidates:
            continue
        item = {'version_id': row['version_id'], 'source_path': row['source_path'], 'valid': False, 'readable': False}
        try:
            meta, path = store.resolve(team['team_id'], row['author_session_id'], row['version_id'])
            item['sha256'] = meta['sha256']
            item['author_session_id'] = row['author_session_id']
            readable = True
            for role in ('dev', 'qa', 'lead'):
                try:
                    store.resolve(team['team_id'], members[role]['session_id'], row['version_id'])
                except (KeyError, RuntimeError):
                    readable = False
            item['readable'] = readable
            if name.endswith('.zip'):
                with zipfile.ZipFile(path) as archive:
                    files = [x for x in archive.infolist() if not x.is_dir()]
                    item['valid'] = bool(files) and len(files) <= 256 and sum(x.file_size for x in files) <= 16 * 1024 * 1024 and archive.testzip() is None
            elif name.endswith('.sqlite3'):
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as con:
                    con.execute('PRAGMA query_only=ON')
                    con.execute('PRAGMA trusted_schema=OFF')
                    counts = {}
                    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                        counts[table] = con.execute('SELECT count(*) FROM "'+table.replace('"','""')+'"').fetchone()[0]
                    item['table_rows'] = counts
                    item['valid'] = con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and any(counts.values())
            else:
                item['data'] = json.loads(path.read_text(encoding='utf8'))
                item['valid'] = isinstance(item['data'], dict)
            item['_path'] = str(path)
        except (RuntimeError, OSError, ValueError, sqlite3.Error, zipfile.BadZipFile):
            pass
        candidates[name].append(item)
    return candidates


def _choose(items, final):
    choices = [x for x in items if x['valid'] and x['readable']]
    return max(choices, key=lambda x: x['version_id'] in final, default=None)


def _public(candidates):
    return {k: [{key: val for key, val in x.items() if key != '_path'} for x in v] for k,v in candidates.items()}


def audit_t1_handoff(app, team, t1_assignment_id):
    db, _, terminal, scope = _context(app, team, t1_assignment_id)
    frozen = terminal['frozen'] if terminal else {}
    final = {v['version_id'] for v in frozen.get('final_versions', [])}
    approved = {v['version_id'] for fact in frozen.get('task_acceptance', {}).values() if fact
                for v in fact.get('versions', [])}
    names = ['t1-app.zip', 't1-bookings.sqlite3', 't1-data-provenance.json']
    candidates = _artifacts(app, team, names)
    app_item, data_item, provenance = (_choose(candidates[n], final) for n in names)
    p = provenance.get('data', {}) if provenance else {}
    hashes_match = bool(app_item and data_item and p.get('app_version_id') == app_item['version_id']
                        and p.get('app_sha256') == app_item['sha256'] and p.get('database_sha256') == data_item['sha256'])
    receipts = _receipts(db, scope)
    operations = p.get('operations', [])
    path = p.get('source_database_path')
    relative_path = isinstance(path, str) and bool(path) and not path.startswith(('/', '\\')) and all(
        part not in ('', '.', '..') and ':' not in part for part in path.replace('\\','/').split('/'))
    operation_evidence = isinstance(operations, list) and all(isinstance(x, dict) for x in operations) and any(
        x.get('method') == 'POST' and x.get('status') == 201 for x in operations) and any(
        x.get('method') in ('POST', 'DELETE') and x.get('status') == 200 for x in operations)
    receipt_matches = [r for r in receipts if data_item and any(line.strip() == 'T015_SNAPSHOT_SHA256=' + data_item['sha256'] for line in r['stdout'].splitlines())]
    receipt_match = bool(receipt_matches)
    evidence_ready = bool(terminal and terminal['outcome'] == 'completed' and app_item and data_item
        and app_item['version_id'] in approved and app_item['version_id'] in final
        and data_item['version_id'] in approved and data_item['version_id'] in final
        and hashes_match and relative_path and p.get('backup_method') == 'sqlite3.Connection.backup'
        and operation_evidence and receipt_match)
    return {'app_version_id': app_item['version_id'] if app_item else None,
            'database_version_id': data_item['version_id'] if data_item else None,
            'app_approved': bool(app_item and app_item['version_id'] in approved and app_item['version_id'] in final),
            'database_approved': bool(data_item and data_item['version_id'] in approved and data_item['version_id'] in final),
            'terminal_outcome': terminal['outcome'] if terminal else None, 'provenance_hashes_match': hashes_match,
            'provenance': p, 'successful_t1_run_receipts': [{'turn_id': r['turn_id'], 'call_id': r['call_id']} for r in receipts],
            'strict_ready': evidence_ready, 'snapshot_receipts': [{'turn_id': r['turn_id'], 'call_id': r['call_id']} for r in receipt_matches], 'candidates': _public(candidates),
            'limitations': ['Evidence readiness supports independent verification; participant-authored metadata and tool receipts cannot conclusively establish provenance.']}


def _metrics(path, schema, start, end):
    date.fromisoformat(start); date.fromisoformat(end)
    if start > end:
        raise ValueError('invalid range')
    keys = ('table','room','date','start','end','status')
    if not all(isinstance(schema.get(k), str) and schema[k] for k in keys) or not isinstance(schema.get('cancelled_values'), list) or not isinstance(schema.get('active_values'), list):
        raise ValueError('invalid schema')
    q = lambda s: '"' + s.replace('"','""') + '"'
    rooms, days, hours = defaultdict(lambda: {'count':0,'minutes':0,'cancelled_count':0}), defaultdict(int), defaultdict(int)
    total = minutes = cancelled = 0
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA trusted_schema=OFF')
        sql = 'SELECT '+','.join(q(schema[k]) for k in keys[1:])+' FROM '+q(schema['table'])
        for room, day, begins, ends, status in con.execute(sql):
            if not all(isinstance(v,str) for v in (room,day,begins,ends)):
                raise ValueError('invalid row')
            date.fromisoformat(day)
            a = datetime.strptime(begins,'%H:%M'); b = datetime.strptime(ends,'%H:%M')
            if b <= a: raise ValueError('overnight or invalid interval')
            if not start <= day <= end: continue
            if status in schema['cancelled_values']:
                cancelled += 1; rooms[room]['cancelled_count'] += 1; continue
            if status not in schema['active_values']:
                raise ValueError('unrecognized booking status')
            duration = int((b-a).total_seconds()//60)
            total += 1; minutes += duration; days[day] += 1
            rooms[room]['count'] += 1; rooms[room]['minutes'] += duration
            h = a.replace(minute=0)
            while h < b:
                hours[h.strftime('%H:%M')] += int((min(b,h+timedelta(hours=1))-max(a,h)).total_seconds()//60)
                h += timedelta(hours=1)
    peak = lambda d: sorted(k for k,v in d.items() if v == max(d.values())) if d else []
    return {'total_count':total,'total_minutes':minutes,'cancelled_count':cancelled,'rooms':dict(rooms),
            'busiest_dates':peak(days),'busiest_periods':peak(hours)}


def audit_t2_handoff(app, team, t2_assignment_id, t1_handoff):
    db, _, terminal, scope = _context(app, team, t2_assignment_id)
    wanted = [t1_handoff.get('app_version_id'), t1_handoff.get('database_version_id')]
    rows = db.execute('SELECT owner_id,version_id FROM assignment_required_inputs WHERE message_id=?', (t2_assignment_id,)).fetchall()
    bound = {role: sorted(r['version_id'] for r in rows if r['owner_id'] == m['member_id'])
             for m in team['members'] for role in [m['role_id']] if role in ('dev','qa')}
    receipts = _receipts(db, scope)
    mounts = [{'turn_id':r['turn_id'],'call_id':r['call_id'],'version_ids':r['version_ids']}
              for r in receipts if wanted[1] and wanted[1] in r['version_ids']]
    candidates = _artifacts(app, team, ['t2-dashboard-check.json'])
    report = _choose(candidates['t2-dashboard-check.json'], set())
    result = {'required_inputs':bound, 'binding_complete': bool(all(wanted) and all(set(wanted) <= set(bound.get(r, [])) for r in ('dev','qa'))),
              'successful_snapshot_mounts':mounts, 'report_version_id': report['version_id'] if report else None,
              'independent_metrics':None, 'matches_report':None, 'dashboard_evidence_ready':False,
              'limitations':['A successful mount does not prove the command consumed the file; a reported metric does not prove dashboard output.']}
    try:
        d = report['data']; schema = t1_handoff['provenance']['schema']
        if d['database_version_id'] != wanted[1]: raise ValueError('snapshot mismatch')
        store = app.state.workspace.artifacts
        member = next(m for m in team['members'] if m['role_id']=='dev')
        _, path = store.resolve(team['team_id'], member['session_id'], wanted[1])
        result['independent_metrics'] = _metrics(path, schema, d['range_start'], d['range_end'])
        result['matches_report'] = result['independent_metrics'] == d['metrics']
    except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.Error, OSError, StopIteration):
        result['limitations'].append('Independent recomputation unavailable: missing, mismatched or unsupported evidence/schema.')
    role_sessions = {m['role_id']: m['session_id'] for m in team['members'] if m['role_id'] in ('dev','qa')}
    both_mount = all(any(r['session_id'] == role_sessions.get(role) and all(wanted)
                         and set(wanted) <= set(r['version_ids']) for r in receipts) for role in ('dev','qa'))
    published = set()
    if scope:
        marks = ','.join('?' for _ in scope)
        rows = db.execute(f"""SELECT r.result FROM execution_receipts r
            JOIN scheduled_turns s ON s.turn_id=r.turn_id
            JOIN message_deliveries d ON (d.event_id,d.session_id,d.purpose)=(s.event_id,s.session_id,s.purpose)
            WHERE d.message_id IN ({marks}) AND r.name='publish_artifact' AND r.state='completed'""", tuple(scope))
        for row in rows:
            output = _json(row['result'])
            if isinstance(output, dict) and output.get('ok') and isinstance(output.get('result'), dict):
                published.add(output['result'].get('version_id'))
    result['dashboard_evidence_ready'] = bool(terminal and terminal['outcome'] == 'completed'
        and result['binding_complete'] and both_mount and result['matches_report']
        and report and report['version_id'] in published and report.get('author_session_id') in role_sessions.values())
    result['limitations'].append('Evidence readiness supports independent application verification; participant-authored reports and receipts are not conclusive proof.')
    return result
