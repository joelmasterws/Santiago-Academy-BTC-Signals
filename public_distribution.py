"""Durable public Facebook queue for newly published, verified Discord PDFs."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone

QUEUE = Path('public_reports')

def enqueue(identifier, kind, pdf, caption, win_rate=None):
    """Persist only newly approved items; never enqueue historical grades."""
    if kind != 'win' and (win_rate is None or win_rate < 60):
        return False
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', str(identifier))[:110]
    QUEUE.mkdir(exist_ok=True)
    path = QUEUE / (safe + '.json')
    if path.exists():
        return False
    pdf.seek(0)
    (QUEUE / (safe + '.pdf')).write_bytes(pdf.read())
    path.write_text(json.dumps({'id':safe,'kind':kind,'caption':caption,
        'pdf':safe+'.pdf','win_rate':win_rate,'created_at':datetime.now(timezone.utc).isoformat()},indent=2)+'\n')
    return True
