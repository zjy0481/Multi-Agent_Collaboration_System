"""Prepare an isolated replay state and a clearly labeled booking fixture."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

VERSION = '885283bde4144455a09653a2d868551f'
SHA = 'ef5c90e4b37cf9e0fba2f5929f43e4dfd41158f6ecaf6b9c1ee12517c127e74a'
HELPER = Path(__file__).resolve().parent / 'create_fixture.py'


def prepare_software_state(source_folder, new_state, fixture):
    source_folder = Path(source_folder).resolve()
    source = source_folder / 'state'
    new_state = Path(new_state).resolve()
    fixture = Path(fixture).resolve()
    if new_state.exists() or fixture.exists():
        raise ValueError('Destination state and fixture must not exist')
    app = source / 'team-artifacts' / VERSION / 'artifact'
    if hashlib.sha256(app.read_bytes()).hexdigest() != SHA:
        raise ValueError('Accepted app digest changed')
    team = json.loads((source_folder / 'team.json').read_text(encoding='utf-8'))
    with sqlite3.connect((source / 'runtime.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT status, COUNT(*) FROM team_goals GROUP BY status').fetchall()
        if not rows or any(status in ('active', 'queued') for status, _ in rows):
            raise ValueError('Original team has live goals')
        found = db.execute("SELECT 1 FROM task_submissions s JOIN task_reviews r ON r.submission_id=s.id WHERE json_extract(r.data,'$.decision')='approved' AND s.data LIKE ?", ('%' + VERSION + '%',)).fetchone()
        if not found:
            raise ValueError('App version has no approved submission')
        new_state.mkdir(parents=True)
        with sqlite3.connect(new_state / 'runtime.sqlite3') as copy:
            db.backup(copy)
    shutil.copytree(source / 'team-artifacts', new_state / 'team-artifacts')
    fixture.mkdir(parents=True)
    subprocess.run(['docker','run','--pull=never','--rm','--network','none',
                    '--mount',f'type=bind,source={app},target=/accepted/app.py,readonly',
                    '--mount',f'type=bind,source={HELPER},target=/helper.py,readonly',
                    '--mount',f'type=bind,source={fixture},target=/output',
                    'collaboration-sandbox:local','python','/helper.py'],check=True)
    snapshot = fixture / 'booking-snapshot.db'
    provenance = {'source_folder': str(source_folder), 'state': str(new_state), 'team_id': team['team_id'],
                  'original_goals': rows, 'accepted_app_version_id': VERSION, 'accepted_app_sha256': SHA,
                  'fixture': str(snapshot), 'fixture_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                  'booking_count':4, 'active_count':3, 'cancelled_count':1,
                  'description':'Controlled records created through accepted T1 app after original T1 completion; not historical T1 bookings.',
                  'notice':'SQLite clone retains old completed/cancelled goals and old conversation; deliver the official T2 as a new assignment.'}
    (fixture / 'provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    return provenance


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 4:
        raise SystemExit('Usage: python .tmp/t015_handoff.py SOURCE_FOLDER NEW_STATE NEW_FIXTURE')
    print(json.dumps(prepare_software_state(*sys.argv[1:]),ensure_ascii=False,indent=2))
