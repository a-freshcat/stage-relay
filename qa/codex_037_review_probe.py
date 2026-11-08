"""Isolated reviewer probe; retains evidence, never touches application data."""
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.cleanup import Cleanup

root = Path(__file__).resolve().parent / ('codex-037-review-' + uuid4().hex)
(root / 'uploads').mkdir(parents=True)
fid = 'a' * 24
file = root / 'uploads' / fid
meta = root / 'uploads' / (fid + '.json')
file.write_text('preserve referenced attachment', encoding='utf-8')
meta.write_text(json.dumps({'id': fid, 'project': 'review-project'}), encoding='utf-8')
old = time.time() - 86400 * 10
for path in (file, meta):
    os.utime(path, (old, old))
p = {'id': 'review-project', 'title': 'isolated review', 'closed': True, 'retention_days': 7, 'chat': [], 'issues': []}
cleanup = Cleanup(root)
plan = cleanup.preview(p, [p])
p['chat'].append({'attachments': [{'id': fid}]})
result = cleanup.execute(p, [p], plan['id'])
print(json.dumps({'evidence': str(root), 'referenced_after_preview': True,
                  'attachment_still_present': file.exists(), 'moved': len(result['moved'])}))
p['chat'].clear()
plan2 = cleanup.preview(p, [p])
first = cleanup.execute(p, [p], plan2['id'])
lock = root / 'cleanup-plans' / (plan2['id'] + '.lock')
if lock.exists():
    os.utime(lock, (time.time() - 901, time.time() - 901))
try:
    second = cleanup.execute(p, [p], plan2['id'])
    print(json.dumps({'stale_lock_repeat_accepted': True, 'first_moved': len(first['moved']),
                      'second_moved': len(second['moved']),
                      'recovery_registered_moved': cleanup.recoveries(p['id'])[-1]['moved']}))
except Exception as exc:
    print(json.dumps({'stale_lock_repeat_accepted': False, 'error': str(exc)}))
