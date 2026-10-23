"""Santiago Academy branded PDF reports; educational candle-based grades only."""
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether, Image, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from signal_tracking import PT, dtparse, result_day, stats
from discord_delivery import acknowledged_post
import public_distribution

PURPLE=colors.HexColor('#8B5CF6'); DARK=colors.HexColor('#111125'); SILVER=colors.HexColor('#CFCDE0')
STYLES=getSampleStyleSheet()
STYLES.add(ParagraphStyle(name='SAHeading',parent=STYLES['Heading1'],fontSize=17,leading=20,textColor=PURPLE,alignment=TA_CENTER,spaceAfter=8))
STYLES.add(ParagraphStyle(name='SASub',parent=STYLES['Normal'],fontSize=9,leading=13,textColor=DARK,alignment=TA_CENTER,spaceAfter=12))
STYLES.add(ParagraphStyle(name='SASmall',parent=STYLES['Normal'],fontSize=7.5,leading=10,textColor=DARK))
STYLES.add(ParagraphStyle(name='SACell',parent=STYLES['Normal'],fontSize=7,leading=9,textColor=DARK))

def _fmt(v):
    return '—' if v is None else (f'{float(v):,.6f}'.rstrip('0').rstrip('.') if isinstance(v,(float,int)) else str(v))

def _day(r):
    return dtparse(r['issued_at']).astimezone(PT).date()

def _logo():
    path=Path(__file__).with_name('santiago_academy_logo.png')
    if path.exists():
        from PIL import Image as PILImage
        with PILImage.open(path) as im: w,h=im.size
        scale=min(88/w,88/h)
        return Image(str(path),width=w*scale,height=h*scale)
    return Paragraph('SANTIAGO ACADEMY',STYLES['SAHeading'])

def _table(rows,widths=None):
    t=Table(rows,colWidths=widths,repeatRows=1,hAlign='CENTER')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),PURPLE),('TEXTCOLOR',(0,0),(-1,0),colors.white),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F1EFFA')]),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('LINEBELOW',(0,0),(-1,0),.5,DARK)]))
    return t

def _c(v):return Paragraph(str(v),STYLES['SACell'])

def _footer(canvas,doc):
    canvas.setStrokeColor(PURPLE);canvas.line(35,40,577,40)
    canvas.setFont('Helvetica',7);canvas.setFillColor(DARK)
    canvas.drawCentredString(306,29,'Observed 15m candle ranges; not verified fills or realized profit. Educational only; not financial advice.')
    canvas.drawRightString(575,18,f'Page {doc.page}')

def _build(story):
    buf=BytesIO();doc=SimpleDocTemplate(buf,pagesize=letter,leftMargin=34,rightMargin=34,topMargin=24,bottomMargin=53,title='Santiago Academy Signal Results')
    doc.build(story,onFirstPage=_footer,onLaterPages=_footer);buf.seek(0);return buf

def _header(title,subtitle):
    return [_logo(),Spacer(1,5),Paragraph(title,STYLES['SAHeading']),Paragraph(subtitle,STYLES['SASub'])]

def _record_line(r):
    hits=r.get('hit_at') or [None]*3
    outcome=r.get('result') or r.get('status') or 'PENDING'
    return [_c(_day(r)),_c(r.get('symbol','?')),_c(r.get('side','?')),_c(outcome),_c(' / '.join('Y' if h else '—' for h in hits)),_c(_fmt(r.get('entry'))),_c(_fmt(r.get('stop')))]

def period_pdf(records,start,end,kind):
    """All issued signals in period; grading stats reflect outcomes recorded by generation time."""
    s=stats(records,start,end)
    title=f'{kind.upper()} SIGNAL RESULTS'
    story=_header(title,f'{start:%b %d, %Y} – {(end-timedelta(days=1)):%b %d, %Y}  |  Pacific Time')
    story.append(Paragraph(f'<b>Graded:</b> {s["total"]} &nbsp;&nbsp; <b>Wins:</b> {s["wins"]} &nbsp;&nbsp; <b>Losses:</b> {s["losses"]} &nbsp;&nbsp; <b>Win rate:</b> {s["win_pct"]:.1f}%' if s['total'] else 'No graded signals',STYLES['SASub']))
    story.append(Paragraph('TP1 / TP2 / TP3 hits (graded result dates): '+ ' / '.join(str(x) for x in s['tp']),STYLES['SASub']))
    if kind!='daily':
        unit=timedelta(days=1 if kind=='weekly' else 7)
        group=[]; cursor=start
        while cursor<end:
            stop=min(cursor+unit,end);sub=stats(records,cursor,stop)
            group.append([_c(f'{cursor:%b %d}–{(stop-timedelta(days=1)):%b %d}'),_c(sub['wins']),_c(sub['losses']),_c(f'{sub["win_pct"]:.1f}%' if sub['total'] else 'N/A')]);cursor=stop
        story.append(_table([[_c('PERIOD'),_c('WINS'),_c('LOSSES'),_c('WIN RATE')]]+group,[175,115,115,115]));story.append(Spacer(1,13))
    selected=sorted((r for r in records.values() if start<=_day(r)<end),key=lambda r:r['issued_at'])
    story.append(Paragraph(f'<b>ALL ISSUED SIGNALS ({len(selected)})</b> — issued date determines inclusion; unresolved and ambiguous signals remain visible.',STYLES['SASmall']))
    story.append(Spacer(1,7))
    if selected:
        rows=[[_c(x) for x in ('ISSUED','ASSET','BIAS','GRADE','TP1/2/3','ENTRY','STOP')]]+[_record_line(r) for r in selected]
        story.append(_table(rows,[76,49,61,73,82,85,85]))
    else:story.append(Paragraph('No issued signals in this period.',STYLES['SASmall']))
    story.append(Spacer(1,10));story.append(Paragraph('WIN means TP1 was observed; TP2/TP3 are separate observations. EXPIRED, AMBIGUOUS, and UNRESOLVED are excluded from graded win rate. Grades may change as previously active signals resolve.',STYLES['SASmall']))
    return _build(story)

def trade_pdf(record):
    result=record.get('result')
    if result not in ('WIN','LOSS'):raise ValueError('Only graded trades are eligible')
    story=_header(f'{result} | {record["symbol"]} {record["side"]}',f'Signal ID: {record.get("id","N/A")}')
    pairs=[('Entry reference',_fmt(record.get('entry'))),('Entry zone',' – '.join(_fmt(v) for v in record.get('entry_zone',[]))),('Stop loss',_fmt(record.get('stop'))),('Issued (UTC)',record.get('issued_at','—')),('Activated (UTC)',record.get('activated_at') or 'Not confirmed')]
    for i,t in enumerate(record.get('targets',[])):pairs.append((f'TP{i+1}',f'{_fmt(t)}  |  '+(record.get('hit_at') or [None]*3)[i] if (record.get('hit_at') or [None]*3)[i] else f'{_fmt(t)}  |  Not observed'))
    pairs.extend([('Stop hit (UTC)',record.get('stop_at') or 'Not observed'),('Grade', 'WIN — TP1 observed' if result=='WIN' else 'LOSS — stop before TP1')])
    story.append(_table([[_c('FIELD'),_c('OBSERVATION')]]+[[_c(a),_c(b)] for a,b in pairs],[155,365]))
    story.append(Spacer(1,18));story.append(Paragraph('The reported outcome is inferred from completed market candles, not a verified executed order or actual profit. Intrabar ordering is uncertain; past results do not guarantee future results.',STYLES['SASmall']))
    return _build(story)

def send_pdf(session,webhook,content,pdf,filename):
    return acknowledged_post(session,webhook,payload={'content':content,'allowed_mentions':{'parse':[]}},image=pdf,filename=filename)

def send_due_reports(records,state,now,session,webhooks,checkpoint=None,public_webhook=""):
    """Publish only most recently closed day/week/month; no historical bulk backfill."""
    today=now.astimezone(PT).date(); prior=today-timedelta(days=1)
    targets=[('daily',prior,today,'daily:'+str(prior))]
    if today.weekday()==0:
        start=today-timedelta(days=7);targets.append(('weekly',start,today,f'weekly:{start}'))
    if today.day==1:
        start=prior.replace(day=1);targets.append(('monthly',start,today,f'monthly:{start:%Y-%m}'))
    reports=state.setdefault('reports',{});emitted=[]
    for kind,start,end,key in targets:
        webhook=webhooks.get(kind,'')
        if not webhook:continue
        routed_key='pdf:'+key
        if routed_key not in reports:
            pdf=period_pdf(records,start,end,kind)
            mid=send_pdf(session,webhook,f'📊 **Santiago Academy {kind.title()} Signal Results** | {start} to {end-timedelta(days=1)}',pdf,f'Santiago_Academy_{kind.title()}_{start}.pdf')
            reports[routed_key]=mid;emitted.append(routed_key)
            if checkpoint:checkpoint()
        # The graded rate is independent for each period. Zero graded trades do not qualify.
        summary=stats(records,start,end)
        if summary['total'] and summary['win_pct'] >= 60:
            public_key='public_pdf:'+key
            caption=(f'📈 Santiago Academy {kind.title()} Results | '
                     f'{summary["wins"]}W / {summary["losses"]}L | '
                     f'{summary["win_pct"]:.1f}% graded win rate. '
                     'Candle-based outcomes; not verified trades or realized returns.')
            if public_webhook and public_key not in reports:
                try:
                    public_mid=send_pdf(session,public_webhook,caption,
                        period_pdf(records,start,end,kind),f'Santiago_Academy_{kind.title()}_{start}.pdf')
                    reports[public_key]=public_mid
                    if checkpoint:checkpoint()
                except Exception as exc:
                    print(f'PUBLIC_PDF_ERROR {key}: {exc}',flush=True)
            # Queue only after the dedicated report has been acknowledged.
            public_distribution.enqueue(key,kind,period_pdf(records,start,end,kind),
                caption,summary['win_pct'])
    return emitted
