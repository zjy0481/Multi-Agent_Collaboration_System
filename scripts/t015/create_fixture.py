import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen

out = Path('/output')
db = out / 'booking-source.db'
env = dict(os.environ, BOOKING_DB=str(db), BOOKING_HOST='127.0.0.1', BOOKING_PORT='8732')
app = subprocess.Popen([sys.executable, '/accepted/app.py'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request('http://127.0.0.1:8732' + path, data=data, method=method, headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=3) as result:
        return result.status, json.load(result)

try:
    for _ in range(80):
        try:
            if call('GET', '/health')[0] == 200:
                break
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('accepted app did not start')
    bookings = [
        ('Room A', '2026-12-05', '09:00', '10:00', False),
        ('Room A', '2026-12-05', '11:00', '12:30', False),
        ('Room B', '2026-12-06', '14:00', '15:00', False),
        ('Room C', '2026-12-06', '10:00', '11:00', True),
    ]
    receipts = []
    for room, date, start, end, cancel in bookings:
        status, result = call('POST', '/api/bookings', {'room':room, 'date':date, 'start':start, 'end':end, 'booked_by':'T015 controlled fixture'})
        assert status == 201
        booking_id = result['booking']['id']
        receipts.append({'status':status, 'booking':result['booking'], 'cancelled':cancel})
        if cancel:
            status, result = call('DELETE', '/api/bookings/' + str(booking_id))
            assert status == 200
    with sqlite3.connect(db) as source, sqlite3.connect(out / 'booking-snapshot.db') as target:
        source.backup(target)
        rows = target.execute('select count(*),sum(active),sum(1-active) from bookings').fetchone()
        assert rows == (4,3,1), rows
        assert target.execute('pragma integrity_check').fetchone()[0] == 'ok'
    (out / 'receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'bookings':rows[0], 'active':rows[1], 'cancelled':rows[2]}))
finally:
    app.terminate()
    app.wait(timeout=5)
