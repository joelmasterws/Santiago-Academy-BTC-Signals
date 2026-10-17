"""Santiago Academy historical educational signal outcome tracking."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

PT = ZoneInfo('America/Los_Angeles')
ENTRY_EXPIRY_HOURS = 24
ACTIVE_EXPIRY_HOURS = 72


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat()


def dtparse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def record_signal(signal, message_id, issued_at):
    return dict(id=signal['key'], symbol=signal['symbol'], side=signal['side'],
                entry_zone=list(map(float, signal['entry_zone'])),
                entry=float(signal['entry']), stop=float(signal['stop']),
                targets=list(map(float, signal['targets'])),
                issued_at=stamp(issued_at), message_id=str(message_id),
                activated_at=None, hit_at=[None, None, None], stop_at=None,
                closed_at=None, status='WAITING', result=None, updates=[])


def advance(record, bars, now):
    """Process only completed bars after publication; ambiguous bars favor stop."""
    events = []
    if record['closed_at']:
        return events
    issued = dtparse(record['issued_at'])
    last_seen = dtparse(record.get('last_checked_at') or record['issued_at'])
    cutoff = issued + timedelta(hours=ENTRY_EXPIRY_HOURS)
    active_cutoff = issued + timedelta(hours=ACTIVE_EXPIRY_HOURS)
    bullish = record['side'] == 'BULLISH'
    for bar in bars.itertuples():
        end = bar.close_time.to_pydatetime()
        if end <= last_seen or end <= issued or end > now:
            continue
        low, high = float(bar.low), float(bar.high)
        if record['activated_at'] is None and end > cutoff:
            record.update(status='EXPIRED', closed_at=stamp(cutoff))
            events.append('EXPIRED: entry not confirmed within 24 hours')
            break
        if record['activated_at'] is None:
            zone_lo, zone_hi = record['entry_zone']
            if not (low <= zone_hi and high >= zone_lo):
                record['last_checked_at'] = stamp(end)
                continue
            record.update(activated_at=stamp(end), status='ACTIVE')
            events.append('ENTRY ACTIVATED (15m candle touched entry zone)')
        stop_hit = low <= record['stop'] if bullish else high >= record['stop']
        target_hits = [high >= x if bullish else low <= x for x in record['targets']]
        # For an ambiguous candle, assume the stop was reached before any new target.
        if stop_hit:
            record['stop_at'] = stamp(end)
            if record['hit_at'][0] is None:
                record.update(result='LOSS', status='CLOSED', closed_at=stamp(end))
                events.append('LOSS: stop reached before TP1 (or order ambiguous)')
            else:
                record.update(status='CLOSED', closed_at=stamp(end))
                events.append('CLOSED: stop reached after TP1 WIN')
            record['last_checked_at'] = stamp(end)
            break
        for i, touched in enumerate(target_hits):
            if touched and record['hit_at'][i] is None:
                # TP2/3 implies the lower numbered targets were crossed.
                for j in range(i + 1):
                    if record['hit_at'][j] is None:
                        record['hit_at'][j] = stamp(end)
                        if j == 0:
                            record['result'] = 'WIN'
                        events.append(f'TP{j+1} HIT' + (' — WIN' if j == 0 else ''))
        record['last_checked_at'] = stamp(end)
        if record['hit_at'][2]:
            record.update(status='CLOSED', closed_at=stamp(end))
            events.append('CLOSED: TP3 reached')
            break
    if record['closed_at'] is None:
        if record['activated_at'] is None and now >= cutoff:
            record.update(status='EXPIRED', closed_at=stamp(cutoff))
            events.append('EXPIRED: entry not confirmed within 24 hours')
        elif record['activated_at'] is not None and now >= active_cutoff:
            record.update(status='CLOSED', closed_at=stamp(active_cutoff))
            if record['result'] is None:
                record['result'] = 'UNRESOLVED'
            events.append('CLOSED: 72-hour tracking limit reached')
    return events


def result_day(record):
    if record['result'] == 'WIN':
        return dtparse(record['hit_at'][0]).astimezone(PT).date()
    if record['result'] == 'LOSS':
        return dtparse(record['stop_at']).astimezone(PT).date()
    return None


def report(records, start, end, title):
    wins = losses = 0
    tp = [0, 0, 0]
    for record in records.values():
        day = result_day(record)
        if day is None or not start <= day < end:
            continue
        if record['result'] == 'WIN': wins += 1
        elif record['result'] == 'LOSS': losses += 1
        for i, hit in enumerate(record['hit_at']):
            if hit: tp[i] += 1
    total = wins + losses
    pct = f'{100*wins/total:.1f}%' if total else 'N/A (no resolved signals)'
    return (f'📊 **Santiago Academy | {title}**\n'
            f'**Period:** {start.isoformat()} through {(end-timedelta(days=1)).isoformat()} Pacific\n'
            f'🟢 **Wins (TP1 reached):** {wins}\n'
            f'🔴 **Losses (stop before TP1):** {losses}\n'
            f'**Win rate:** {pct}\n'
            f'**Loss rate:** {f"{100*losses/total:.1f}%" if total else "N/A"}\n'
            f'🎯 **TP1 / TP2 / TP3:** {tp[0]} / {tp[1]} / {tp[2]}\n'
            f'**Graded signals:** {total}\n'
            'Only resolved WIN/LOSS signals are included; no fills or profits are implied. '
            'Target totals refer to signals graded during this period.')


def send_text(session, webhook, content):
    response = session.post(webhook, params={'wait':'true'}, json={
        'content': content, 'allowed_mentions': {'parse': []}}, timeout=30)
    response.raise_for_status()
    message_id = response.json().get('id')
    if not message_id:
        raise RuntimeError('Discord did not acknowledge tracking message')
    return str(message_id)


def daily_and_monthly(records, state, now, session, webhook):
    """Publish in the first 30 minutes after midnight Pacific, daily then monthly."""
    local = now.astimezone(PT)
    if local.hour != 0 or local.minute >= 30:
        return
    from datetime import date
    today = local.date()
    yesterday = today - timedelta(days=1)
    reports = state.setdefault('reports', {})
    key = f'daily:{yesterday.isoformat()}'
    if key not in reports:
        reports[key] = send_text(session, webhook,
            report(records, yesterday, today, f'Daily Signal Results — {yesterday.isoformat()}'))
    if today.day == 1:
        first_prev = (today - timedelta(days=1)).replace(day=1)
        key = f'monthly:{first_prev:%Y-%m}'
        if key not in reports:
            reports[key] = send_text(session, webhook,
                report(records, first_prev, today, f'Monthly Performance — {first_prev:%B %Y}'))
