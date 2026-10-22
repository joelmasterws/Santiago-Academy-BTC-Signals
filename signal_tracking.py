"""Persistent, conservative educational signal outcome tracking and shareable reports."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import io
import math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import requests
from discord_delivery import acknowledged_post

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
                targets=list(map(float, signal['targets'])), issued_at=stamp(issued_at),
                message_id=str(message_id), activated_at=None, hit_at=[None]*3,
                stop_at=None, closed_at=None, status='WAITING', result=None,
                last_checked_at=None, result_posted=False)


def advance(record, bars, now):
    """Use completed 15m bars; stop wins any ambiguous intrabar ordering."""
    if record.get('closed_at'):
        return []
    issued = dtparse(record['issued_at'])
    last_seen = dtparse(record.get('last_checked_at') or record['issued_at'])
    entry_cutoff = issued + timedelta(hours=ENTRY_EXPIRY_HOURS)
    active_cutoff = issued + timedelta(hours=ACTIVE_EXPIRY_HOURS)
    bullish = record['side'] == 'BULLISH'
    events = []
    for bar in bars.itertuples():
        end = bar.close_time.to_pydatetime() if hasattr(bar.close_time, 'to_pydatetime') else bar.close_time
        if end <= last_seen or end <= issued or end > now or end-timedelta(minutes=15) < issued:
            continue
        if record['activated_at'] is None and end > entry_cutoff:
            record.update(status='EXPIRED', closed_at=stamp(entry_cutoff))
            break
        low, high = float(bar.low), float(bar.high)
        if record['activated_at'] is None:
            lo, hi = record['entry_zone']
            if not (low <= hi and high >= lo):
                record['last_checked_at'] = stamp(end)
                continue
            # OHLC cannot prove whether entry happened before a stop in this bar.
            # Do not misrepresent an unverified fill sequence as a definite loss.
            stop_on_entry_bar = (low <= record['stop'] if bullish
                                 else high >= record['stop'])
            if stop_on_entry_bar:
                record.update(status='AMBIGUOUS', result='AMBIGUOUS',
                              closed_at=stamp(end), last_checked_at=stamp(end),
                              ambiguity_reason='ENTRY_AND_STOP_SAME_15M_CANDLE',
                              ambiguity_at=stamp(end))
                events.append('AMBIGUOUS')
                break
            record.update(activated_at=stamp(end), status='ACTIVE')
        stop_hit = low <= record['stop'] if bullish else high >= record['stop']
        target_hits = [high >= x if bullish else low <= x for x in record['targets']]
        if stop_hit and any(target_hits):
            record.setdefault('diagnostics', []).append({
                'at': stamp(end), 'type': 'STOP_AND_TARGET_SAME_15M_CANDLE',
                'resolution': 'STOP_FIRST_CONSERVATIVE'})
        if stop_hit:
            record['stop_at'] = stamp(end)
            record.update(status='CLOSED', closed_at=stamp(end))
            if record['hit_at'][0] is None:
                record['result'] = 'LOSS'
                events.append('LOSS')
            record['last_checked_at'] = stamp(end)
            break
        for i, hit in enumerate(target_hits):
            if hit:
                for j in range(i + 1):
                    if record['hit_at'][j] is None:
                        record['hit_at'][j] = stamp(end)
                        if j == 0:
                            record['result'] = 'WIN'
                            events.append('WIN')
        record['last_checked_at'] = stamp(end)
        if record['hit_at'][2]:
            record.update(status='CLOSED', closed_at=stamp(end))
            break
    if not record['closed_at']:
        if record['activated_at'] is None and now >= entry_cutoff:
            record.update(status='EXPIRED', closed_at=stamp(entry_cutoff))
        elif record['activated_at'] is not None and now >= active_cutoff:
            record.update(status='CLOSED', closed_at=stamp(active_cutoff),
                          result=record['result'] or 'UNRESOLVED')
    return events


def result_day(record):
    if record.get('result') == 'WIN' and record['hit_at'][0]:
        return dtparse(record['hit_at'][0]).astimezone(PT).date()
    if record.get('result') == 'LOSS' and record.get('stop_at'):
        return dtparse(record['stop_at']).astimezone(PT).date()
    return None


def stats(records, start, end):
    chosen = [r for r in records.values() if (d := result_day(r)) is not None and start <= d < end]
    wins = sum(r['result'] == 'WIN' for r in chosen)
    losses = sum(r['result'] == 'LOSS' for r in chosen)
    tp = [sum(bool(r['hit_at'][i]) for r in chosen) for i in range(3)]
    return dict(wins=wins, losses=losses, total=wins+losses, tp=tp,
                win_pct=(100*wins/(wins+losses) if wins+losses else None),
                loss_pct=(100*losses/(wins+losses) if wins+losses else None),
                records=chosen)


def _pct(x):
    return f'{x:.1f}%' if x is not None else 'N/A'


def report_image(records, start, end, kind, logo_path='santiago_academy_logo.png'):
    """Render a separate, actual PNG; figures are data-driven, never fabricated."""
    s = stats(records, start, end)
    fig = plt.figure(figsize=(12, 12), dpi=150, facecolor='#08091a')
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis('off')
    purple, gold, green, red = '#a56cff', '#f4d88b', '#2bf69a', '#ff536d'
    from matplotlib.patches import FancyBboxPatch
    def panel(x,y,w,h,edge='#6f36bb'):
        ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.012,rounding_size=0.015',
                    facecolor='#10132b',edgecolor=edge,linewidth=2))
    panel(.035,.03,.93,.94,purple)
    lp = Path(logo_path)
    if lp.exists():
        img = mpimg.imread(lp)
        logoax=fig.add_axes([.39,.745,.22,.21]);logoax.imshow(img);logoax.axis('off')
    ax.text(.5,.74,'SANTIAGO ACADEMY',ha='center',color='#e9d8ff',fontsize=24,fontweight='bold')
    label='DAILY SIGNAL RESULTS' if kind=='daily' else 'MONTHLY PERFORMANCE'
    ax.text(.5,.689,label,ha='center',color=gold,fontsize=22,fontweight='bold')
    period=start.strftime('%B %d, %Y') if kind=='daily' else f'{start:%B %d} – {(end-timedelta(days=1)):%d, %Y}'
    ax.text(.5,.651,f'{period}  •  PACIFIC TIME',ha='center',color='white',fontsize=13)
    for i,(name,value,color) in enumerate([('WINS',s['wins'],green),('LOSSES',s['losses'],red),
                                           ('TOTAL GRADED',s['total'],'#f1e7ff'),('WIN RATE',_pct(s['win_pct']),'#74caff')]):
        x=.055+i*.235;panel(x,.493,.215,.116,color)
        ax.text(x+.1075,.568,name,ha='center',color=color,fontsize=11,fontweight='bold')
        ax.text(x+.1075,.519,str(value),ha='center',color='white',fontsize=22,fontweight='bold')
    ax.text(.5,.445,'TAKE PROFIT BREAKDOWN',ha='center',color='white',fontsize=17,fontweight='bold')
    for i,(n,v,color) in enumerate(zip(('TP1 HIT','TP2 HIT','TP3 HIT'),s['tp'],(green,'#55b6ff',purple))):
        x=.07+i*.31;panel(x,.285,.27,.115,color)
        ax.text(x+.135,.358,n,ha='center',color=color,fontsize=12,fontweight='bold')
        ax.text(x+.135,.312,f'{v}  ({100*v/s["total"]:.1f}%)' if s['total'] else '0  (N/A)',
                ha='center',color='white',fontsize=17,fontweight='bold')
    if kind=='monthly':
        ax.text(.5,.251,f'WIN {_pct(s["win_pct"])}   •   LOSS {_pct(s["loss_pct"])}',
                ha='center',color='white',fontsize=14,fontweight='bold')
        if s['total']:
            from matplotlib.patches import Rectangle
            ax.add_patch(Rectangle((.11,.215),.78*s['wins']/s['total'],.022,color=green))
            ax.add_patch(Rectangle((.11+.78*s['wins']/s['total'],.215),
                                   .78*s['losses']/s['total'],.022,color=red))
        ax.text(.5,.172,'WIN / LOSS PERCENTAGE OF GRADED SIGNALS',
                ha='center',color='#bdb4da',fontsize=11)
    else:
        ax.text(.5,.249,'COMPLETED SIGNALS — MOST RECENT',ha='center',color='white',fontsize=12,fontweight='bold')
        recent=sorted(s['records'],key=lambda r:r['hit_at'][0] or r.get('stop_at') or '',reverse=True)[:4]
        for i,r in enumerate(recent):
            y=.217-i*.024
            color=green if r['result']=='WIN' else red
            targets=' '.join('✓' if v else '–' for v in r['hit_at'])
            ax.text(.10,y,f'{r["symbol"]:<5}  {r["side"]:<7}',color='white',fontsize=10,ha='left')
            ax.text(.60,y,r['result'],color=color,fontsize=10,ha='center',fontweight='bold')
            ax.text(.89,y,f'TP1 / TP2 / TP3: {targets}',color='#d4c8eb',fontsize=9,ha='right')
        if not recent:
            ax.text(.5,.195,'No graded signals for this period',ha='center',color='#bdb4da',fontsize=11)
    ax.text(.5,.11,'Results are based on observed candle ranges, not verified fills or realized profit.',
            ha='center',color='#c6bedc',fontsize=10)
    ax.text(.5,.077,'EDUCATIONAL ONLY • NOT FINANCIAL ADVICE • PAST RESULTS DO NOT GUARANTEE FUTURE RESULTS',
            ha='center',color='#c6bedc',fontsize=9)
    buf=io.BytesIO();fig.savefig(buf,format='png',facecolor=fig.get_facecolor());plt.close(fig)
    buf.seek(0)
    return buf


def send_image(session, webhook, content, png, filename):
    return acknowledged_post(session, webhook, payload={
        'content': content, 'allowed_mentions': {'parse': []}},
        image=png, filename=filename)


def send_result(session, webhook, record):
    win = record['result'] == 'WIN'
    color = '🟢' if win else '🔴'
    label = 'WIN — TP1 HIT' if win else 'LOSS — STOP LOSS HIT BEFORE TP1'
    target = record['targets'][0] if win else record['stop']
    content = (f'{color} **SANTIAGO ACADEMY | SIGNAL {"WIN" if win else "LOSS"}** {color}\n'
               f'**${record["symbol"]}/USD — {record["side"]}**\n'
               f'**Final grade:** {label}\n'
               f'**Level reached:** ${target:,.5f}\n'
               f'**Original Discord message ID:** {record["message_id"]}\n'
               'TP2/TP3 are monitored internally and included in performance reports. '
               'Educational candle-based tracking; no fills or profit guaranteed.')
    return acknowledged_post(session, webhook, json_body={
        'content': content,
        'embeds': [{'color': 0x1ccf78 if win else 0xe83c55,
                    'title': 'WIN' if win else 'LOSS', 'description': label}],
        'allowed_mentions': {'parse': []}})


def due_reports(records,state,now,session,webhook,logo_path='santiago_academy_logo.png',checkpoint=None):
    """First run after Pacific midnight, daily first then monthly; retry missed reports."""
    today=now.astimezone(PT).date()
    reports=state.setdefault('reports',{})
    # Never invent past results before installation; only emit dates from recorded signals.
    if not records:return []
    first=min(dtparse(r['issued_at']).astimezone(PT).date() for r in records.values())
    emitted=[]
    day=first
    while day < today:
        next_day=day+timedelta(days=1)
        key=f'daily:{day}'
        if key not in reports:
            img=report_image(records,day,next_day,'daily',logo_path)
            reports[key]=send_image(session,webhook,f'📊 Santiago Academy Daily Signal Results — {day}',img,
                                    f'Santiago_Academy_Daily_{day}.png')
            emitted.append(key)
            if checkpoint: checkpoint()
        if next_day.day==1:
            month_start=day.replace(day=1)
            key=f'monthly:{month_start:%Y-%m}'
            if key not in reports:
                img=report_image(records,month_start,next_day,'monthly',logo_path)
                reports[key]=send_image(session,webhook,f'🏆 Santiago Academy Monthly Performance — {month_start:%B %Y}',img,
                                        f'Santiago_Academy_Monthly_{month_start:%Y-%m}.png')
                emitted.append(key)
            if checkpoint: checkpoint()
        day=next_day
    return emitted
