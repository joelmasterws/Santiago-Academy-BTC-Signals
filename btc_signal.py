"""Santiago Academy multi-asset educational Discord signals. Never places orders."""
import argparse
import hashlib
import io
import json
import math
import os
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import requests
import signal_tracking as tracking
import sa_pdf_reports as pdf_reports
import public_distribution
from discord_delivery import acknowledged_post

ASSETS = ('BTC','ETH','BNB','XRP','SOL','DOGE','TRX','ADA','LINK','AVAX')
API = 'https://api.binance.us/api/v3/klines'
PT = ZoneInfo('America/Los_Angeles')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent':'SantiagoAcademyCryptoSignals/1.0'})
STATE_PATH = Path(os.getenv('SA_SIGNAL_STATE', 'signal_state.json'))


def candles(symbol, interval, limit=260):
    resp = SESSION.get(API, params={'symbol':symbol+'USD','interval':interval,'limit':limit}, timeout=25)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows,list) or len(rows)<205:
        raise ValueError(f'{symbol} {interval}: insufficient candles')
    df = pd.DataFrame(rows)
    for index,name in [(1,'open'),(2,'high'),(3,'low'),(4,'close'),(5,'volume')]:
        df[name] = pd.to_numeric(df[index],errors='raise')
    df['close_time'] = pd.to_datetime(df[6],unit='ms',utc=True)
    df = df[df['close_time']<pd.Timestamp.now(tz='UTC')].copy()
    if len(df)<200: raise ValueError(f'{symbol} {interval}: insufficient closed candles')
    if pd.Timestamp.now(tz='UTC')-df.iloc[-1]['close_time'] > pd.Timedelta(minutes={'15m':45,'1h':150,'4h':600}[interval]):
        raise ValueError(f'{symbol} {interval}: stale candles')
    return df



# Independent BTC confirmation only. Other assets retain their existing rules.
BITFINEX_API = 'https://api-pub.bitfinex.com/v2/candles/hist'


def bitfinex_candles(interval, limit=260):
    timeframe = {'15m': '15m', '1h': '1h', '4h': '4h'}[interval]
    resp = SESSION.get(
        f'{BITFINEX_API}/trade:{timeframe}:tBTCUSD',
        params={'limit': limit, 'sort': 1}, timeout=25)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or len(rows) < 205:
        raise ValueError(f'Bitfinex BTC {interval}: insufficient candles')
    # Bitfinex format: [MTS, OPEN, CLOSE, HIGH, LOW, VOLUME].
    df = pd.DataFrame(rows, columns=['mts', 'open', 'close', 'high', 'low', 'volume'])
    for name in ('open', 'close', 'high', 'low', 'volume'):
        df[name] = pd.to_numeric(df[name], errors='raise')
    start = pd.to_datetime(df['mts'], unit='ms', utc=True)
    duration = {'15m': pd.Timedelta(minutes=15),
                '1h': pd.Timedelta(hours=1),
                '4h': pd.Timedelta(hours=4)}[interval]
    df['close_time'] = start + duration
    df = df[df['close_time'] <= pd.Timestamp.now(tz='UTC')].copy()
    if len(df) < 200:
        raise ValueError(f'Bitfinex BTC {interval}: insufficient closed candles')
    if pd.Timestamp.now(tz='UTC') - df.iloc[-1]['close_time'] > pd.Timedelta(
            minutes={'15m': 45, '1h': 150, '4h': 600}[interval]):
        raise ValueError(f'Bitfinex BTC {interval}: stale candles')
    return df


def confirm_bitfinex_btc(signal, primary):
    """Fail closed unless independent BTC trend/momentum corroborate the signal."""
    frames = {interval: bitfinex_candles(interval)
              for interval in ('15m', '1h', '4h')}
    for interval, original in primary.items():
        delta = abs(frames[interval].iloc[-1]['close_time']
                    - original.iloc[-1]['close_time'])
        if delta > {'15m': pd.Timedelta(minutes=15),
                    '1h': pd.Timedelta(hours=1),
                    '4h': pd.Timedelta(hours=4)}[interval]:
            raise ValueError(f'Bitfinex BTC {interval}: candle timestamps not aligned')
    a, b, c = (metrics(frames[k]) for k in ('15m', '1h', '4h'))
    bullish = (b['ema50'] > b['ema200'] and c['ema50'] > c['ema200']
               and b['macd_hist'] > 0 and c['macd_hist'] > 0
               and a['rsi'] >= 50 and b['rsi'] >= 50)
    bearish = (b['ema50'] < b['ema200'] and c['ema50'] < c['ema200']
               and b['macd_hist'] < 0 and c['macd_hist'] < 0
               and a['rsi'] <= 50 and b['rsi'] <= 50)
    confirmed = bullish if signal['side'] == 'BULLISH' else bearish
    if not confirmed:
        return False
    signal['bitfinex_confirmation'] = {
        'rsi_15m': a['rsi'], 'rsi_1h': b['rsi'],
        'macd_hist_1h': b['macd_hist'], 'macd_hist_4h': c['macd_hist']}
    return True

def metrics(df):
    c=df['close']; delta=c.diff()
    gains=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    losses=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi=100-100/(1+gains.iloc[-1]/losses.iloc[-1]) if losses.iloc[-1]>0 else (100 if gains.iloc[-1]>0 else 50)
    ema20=c.ewm(span=20,adjust=False).mean().iloc[-1]
    ema50=c.ewm(span=50,adjust=False).mean().iloc[-1]
    ema200=c.ewm(span=200,adjust=False).mean().iloc[-1]
    macd=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    hist=(macd-macd.ewm(span=9,adjust=False).mean()).iloc[-1]
    prev=c.shift()
    tr=pd.concat([df['high']-df['low'],(df['high']-prev).abs(),(df['low']-prev).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False).mean().iloc[-1]
    return {'rsi':float(rsi),'ema20':float(ema20),'ema50':float(ema50),'ema200':float(ema200),'macd_hist':float(hist),'atr':float(atr)}


def evaluate(symbol, m15, h1, h4):
    a,b,c=metrics(m15),metrics(h1),metrics(h4)
    price=float(m15.iloc[-1]['close'])
    recent_volumes = pd.to_numeric(m15['volume'].tail(21), errors='coerce')
    baseline = float(recent_volumes.iloc[:-1].mean())
    current_volume = float(recent_volumes.iloc[-1])
    if not (math.isfinite(baseline) and baseline > 0 and
            math.isfinite(current_volume) and current_volume >= 0):
        print(f'{symbol}: FILTER_DIAGNOSTIC invalid_15m_volume '
              f'current={current_volume} baseline={baseline}; rejecting safely', flush=True)
        return None
    volume_ratio = current_volume / baseline
    if not math.isfinite(volume_ratio):
        print(f'{symbol}: FILTER_DIAGNOSTIC nonfinite_volume_ratio; rejecting safely', flush=True)
        return None
    # Higher selectivity, not a guaranteed win-rate target.
    bullish=(volume_ratio>=1.15 and 54<=a['rsi']<=70 and
             c['ema50']>c['ema200'] and c['macd_hist']>0 and
             price>a['ema20'] and a['ema20']>a['ema50'] and
             b['ema50']>b['ema200'] and price>b['ema50'] and
             price>c['ema50'] and b['macd_hist']>0 and
             52<=b['rsi']<=68 and c['rsi']>=50)
    bearish=(volume_ratio>=1.15 and 30<=a['rsi']<=46 and
             c['ema50']<c['ema200'] and c['macd_hist']<0 and
             price<a['ema20'] and a['ema20']<a['ema50'] and
             b['ema50']<b['ema200'] and price<b['ema50'] and
             price<c['ema50'] and b['macd_hist']<0 and
             32<=b['rsi']<=48 and c['rsi']<=50)
    if not (bullish or bearish):
        checks = {
            'volume_ratio_ge_1_15': volume_ratio >= 1.15,
            'bull_15m_rsi_54_70': 54 <= a['rsi'] <= 70,
            'bull_4h_ema50_gt_ema200': c['ema50'] > c['ema200'],
            'bull_4h_macd_positive': c['macd_hist'] > 0,
            'bull_price_gt_15m_ema20': price > a['ema20'],
            'bull_15m_ema20_gt_ema50': a['ema20'] > a['ema50'],
            'bull_1h_ema50_gt_ema200': b['ema50'] > b['ema200'],
            'bull_price_gt_1h_ema50': price > b['ema50'],
            'bull_price_gt_4h_ema50': price > c['ema50'],
            'bull_1h_macd_positive': b['macd_hist'] > 0,
            'bull_1h_rsi_52_68': 52 <= b['rsi'] <= 68,
            'bull_4h_rsi_ge_50': c['rsi'] >= 50,
            'bear_15m_rsi_30_46': 30 <= a['rsi'] <= 46,
            'bear_4h_ema50_lt_ema200': c['ema50'] < c['ema200'],
            'bear_4h_macd_negative': c['macd_hist'] < 0,
            'bear_price_lt_15m_ema20': price < a['ema20'],
            'bear_15m_ema20_lt_ema50': a['ema20'] < a['ema50'],
            'bear_1h_ema50_lt_ema200': b['ema50'] < b['ema200'],
            'bear_price_lt_1h_ema50': price < b['ema50'],
            'bear_price_lt_4h_ema50': price < c['ema50'],
            'bear_1h_macd_negative': b['macd_hist'] < 0,
            'bear_1h_rsi_32_48': 32 <= b['rsi'] <= 48,
            'bear_4h_rsi_le_50': c['rsi'] <= 50,
        }
        shared = ['volume_ratio_ge_1_15']
        for side, prefix in (('BULL', 'bull_'), ('BEAR', 'bear_')):
            failed = [key for key in shared if not checks[key]]
            failed += [key for key, passed in checks.items()
                       if key.startswith(prefix) and not passed]
            print(f'{symbol}: FILTER_DIAGNOSTIC {side} '
                  f'failed={",".join(failed) or "none"} '
                  f'volume_ratio={volume_ratio:.3f} '
                  f'rsi15={a["rsi"]:.2f} rsi1h={b["rsi"]:.2f} '
                  f'rsi4h={c["rsi"]:.2f}', flush=True)
        return None
    side='BULLISH' if bullish else 'BEARISH'; sign=1 if bullish else -1
    atr=a['atr']
    if atr<=0:return None
    # Entry watch zone anchored to completed 15m close; no implied guaranteed fill.
    entry=price
    stop=entry-sign*1.5*atr
    risk=abs(entry-stop)
    if not (0.001<=risk/entry<=0.035):return None
    targets=[entry+sign*risk*r for r in (1,2,3)]
    zone=[entry-sign*0.15*atr,entry]
    zone=sorted(zone)
    # Signal ID identifies the same directional setup within its 1H candle.
    bar=h1.iloc[-1]['close_time'].isoformat()
    key=hashlib.sha256(f'{symbol}|{side}|{bar}'.encode()).hexdigest()[:24]
    return {'symbol':symbol,'side':side,'price':price,'entry':entry,'entry_zone':zone,'stop':stop,
            'targets':targets,'risk':risk,'m15':a,'h1':b,'h4':c,'bar':bar,'key':key,
            'at':m15.iloc[-1]['close_time'].to_pydatetime(),'volume_ratio':volume_ratio}



def shadow_reversal(symbol, m15, h1, h4):
    """Observation only: never creates a Discord signal or places an order."""
    a, b, c = metrics(m15), metrics(h1), metrics(h4)
    price = float(m15.iloc[-1]['close'])
    volumes = pd.to_numeric(m15['volume'].tail(21), errors='coerce')
    baseline = float(volumes.iloc[:-1].mean())
    current = float(volumes.iloc[-1])
    if not (math.isfinite(baseline) and baseline > 0 and
            math.isfinite(current) and current > 0 and
            math.isfinite(price) and price > 0):
        print(f'{symbol}: SHADOW_SKIP reason=invalid_or_zero_volume', flush=True)
        return None
    ratio = current / baseline
    if ratio < 1.15 or not math.isfinite(ratio):
        return None
    # Candidate: short-term momentum counter to established 4H EMA trend.
    bull = (c['ema50'] < c['ema200'] and
            52 <= a['rsi'] <= 70 and b['rsi'] >= 50 and
            price > a['ema20'] and b['macd_hist'] > 0)
    bear = (c['ema50'] > c['ema200'] and
            30 <= a['rsi'] <= 48 and b['rsi'] <= 50 and
            price < a['ema20'] and b['macd_hist'] < 0)
    if not (bull or bear):
        return None
    atr = a['atr']
    if not (math.isfinite(atr) and atr > 0 and
            0.001 <= 1.5 * atr / price <= 0.035):
        return None
    side = 'BULLISH' if bull else 'BEARISH'
    at = m15.iloc[-1]['close_time'].isoformat()
    return {'symbol': symbol, 'side': side, 'entry': price,
            'atr': atr, 'at': at, 'volume_ratio': ratio,
            'rsi15': a['rsi'], 'rsi1h': b['rsi'], 'rsi4h': c['rsi']}


def observe_shadow(state, symbol, m15, h1, h4):
    """Track paper candidates over four completed 15m candles; no Discord delivery."""
    book = state.setdefault('shadow_reversals', {})
    current_time = m15.iloc[-1]['close_time']
    for key, record in list(book.items()):
        if record.get('symbol') != symbol or record.get('status') != 'PENDING':
            continue
        start = pd.Timestamp(record['at'])
        future = m15[(m15['close_time'] > start) &
                     (m15['close_time'] <= start + pd.Timedelta(hours=1))]
        if len(future) < 4:
            continue
        entry = float(record['entry'])
        sign = 1 if record['side'] == 'BULLISH' else -1
        end = float(future.iloc[3]['close'])
        record['status'] = 'OBSERVED'
        record['end_close'] = end
        record['return_pct'] = round(sign * (end / entry - 1) * 100, 4)
        record['observed_at'] = future.iloc[3]['close_time'].isoformat()
        print(f'{symbol}: SHADOW_OUTCOME side={record["side"]} '
              f'four_bar_return_pct={record["return_pct"]:+.4f} '
              f'key={key}', flush=True)
    candidate = shadow_reversal(symbol, m15, h1, h4)
    if candidate:
        key = hashlib.sha256(
            f'{symbol}|{candidate["side"]}|{candidate["at"]}'.encode()
        ).hexdigest()[:24]
        if key not in book:
            candidate['status'] = 'PENDING'
            book[key] = candidate
            print(f'{symbol}: SHADOW_CANDIDATE side={candidate["side"]} '
                  f'entry={candidate["entry"]:.8g} '
                  f'volume_ratio={candidate["volume_ratio"]:.3f} '
                  f'key={key} (NOT POSTED)', flush=True)
    # Retain a bounded history; never remove pending observations.
    completed = sorted(
        ((key, item) for key, item in book.items()
         if item.get('status') == 'OBSERVED'),
        key=lambda pair: pair[1].get('at', ''), reverse=True)
    for key, _ in completed[500:]:
        del book[key]


def fmt(x):
    return f'${x:,.2f}' if abs(x)>=10 else f'${x:,.5f}'.rstrip('0').rstrip('.')


def message(s):
    t=s['at'].astimezone(PT)
    a,b,c=s['m15'],s['h1'],s['h4']
    side='🟢 BULLISH' if s['side']=='BULLISH' else '🔴 BEARISH'
    lines=[f'📣 **Santiago Academy: Premium Signal** 📣',
           f'**${s["symbol"]}/USD — {side}**',
           f'**Data:** Binance.US {s["symbol"]}USD, completed 15m / 1H / 4H candles',
           f'**Reference:** {fmt(s["price"])} | {t:%b %d, %Y %I:%M %p} Pacific (15m close)',
           '**The next expected move ✍️**',
           f'**Entry watch zone:** {fmt(s["entry_zone"][0])} – {fmt(s["entry_zone"][1])}',
           f'**🛑 Stop loss:** {fmt(s["stop"])}',
           f'**🎯 TP1:** {fmt(s["targets"][0])} | **R:R 1:1**',
           f'**🎯 TP2:** {fmt(s["targets"][1])} | **R:R 1:2**',
           f'**🎯 TP3:** {fmt(s["targets"][2])} | **R:R 1:3**',
           f'**1H:** RSI {b["rsi"]:.1f} | EMA50 {fmt(b["ema50"])} | EMA200 {fmt(b["ema200"])} | MACD hist {b["macd_hist"]:+.5f}',
           f'**4H:** RSI {c["rsi"]:.1f} | EMA50 {fmt(c["ema50"])} | ATR {fmt(c["atr"])}',
           f'**15m:** EMA20 {fmt(a["ema20"])} | EMA50 {fmt(a["ema50"])} | ATR {fmt(a["atr"])}',
           f'**Reasoning:** Strict 15m/1H/4H EMA and MACD alignment, RSI confirmation, and 15m volume {s["volume_ratio"]:.2f}x the previous 20-candle average.',
           f'**Invalidation:** Price reaching {fmt(s["stop"])} or trend/momentum conditions reversing before entry.',
           '⚠️ **Disclaimer:** Educational analysis only; not financial advice. No guaranteed fills or profits. Crypto trading can cause total loss.']
    return '\n'.join(lines)


def chart(s, m15):
    df=m15.tail(64).reset_index(drop=True)
    fig,ax=plt.subplots(figsize=(11,5.5),dpi=135)
    for i,r in df.iterrows():
        up=r['close']>=r['open']
        color='#18a37e' if up else '#d75c61'
        ax.vlines(i,r['low'],r['high'],color=color,linewidth=1)
        ax.add_patch(plt.Rectangle((i-.30,min(r['open'],r['close'])),.60,max(abs(r['close']-r['open']),s['price']*.000002),facecolor=color,edgecolor=color))
    for val,label,color in [(s['entry'],'ENTRY','#3279d1'),(s['stop'],'STOP','#dc5050'),
                            *[(v,f'TP{i}','#1a9a60') for i,v in enumerate(s['targets'],1)]]:
        ax.axhline(val,color=color,linestyle='--',alpha=.75,linewidth=1)
        ax.text(len(df)+.5,val,f'{label} {fmt(val)}',color=color,fontsize=8,va='center')
    ax.set_xlim(-1,len(df)+18)
    ax.set_title(f'Santiago Academy | {s["symbol"]}/USD {s["side"]} | Binance.US 15m candles')
    ax.set_ylabel('USD');ax.grid(alpha=.13)
    ax.set_xlabel('Historical completed candles (15m) →')
    fig.tight_layout()
    buf=io.BytesIO();fig.savefig(buf,format='png',bbox_inches='tight');plt.close(fig)
    buf.seek(0);return buf


def load_state():
    if not STATE_PATH.exists():return {'sent':{}}
    try:
        data=json.loads(STATE_PATH.read_text())
        if not isinstance(data.get('sent'),dict):raise ValueError('bad state')
        return data
    except (ValueError,OSError) as exc:
        raise RuntimeError('Invalid duplicate state; refusing to post until fixed') from exc


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(state,indent=2,sort_keys=True))
    temp.replace(STATE_PATH)


def post(s, png, webhook, role_ids):
    content = message(s)
    if role_ids:
        content = ' '.join(f'<@&{role}>' for role in role_ids) + '\n' + content
    if len(content) > 2000:
        raise ValueError('Discord content exceeds 2000 characters')
    return acknowledged_post(SESSION, webhook, payload={
        'content': content,
        'allowed_mentions': {'parse': [], 'roles': role_ids}},
        image=png, filename=f'{s["symbol"]}_signal.png')


def checkpoint(state):
    """Durably save each acknowledged Discord message before the next send.

    On GitHub Actions, push the checkpoint immediately; a later run failure must
    not lose already-posted result acknowledgements.
    """
    save_state(state)
    if os.environ.get('SA_GIT_CHECKPOINT') != 'true':
        return
    subprocess.run(['git', 'add', '--', str(STATE_PATH)], check=True)
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode
    if changed == 0:
        return
    if changed != 1:
        raise RuntimeError('Unable to inspect state checkpoint')
    subprocess.run(['git', '-c', 'user.name=github-actions[bot]',
                    '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                    'commit', '-m', 'chore: checkpoint Discord signal state [skip ci]'], check=True)
    subprocess.run(['git', 'push'], check=True)
    print('STATE_CHECKPOINT_PUSHED', flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dry-run',action='store_true',help='Evaluate and print results; never post')
    parser.add_argument('--test-discord',action='store_true',help='Post a clearly marked connectivity test, no trade signal')
    args=parser.parse_args()
    webhook=os.environ.get('DISCORD_WEBHOOK_URL','').strip()
    if not args.dry_run and not webhook:raise RuntimeError('Missing DISCORD_WEBHOOK_URL')
    if args.test_discord:
        response=SESSION.post(webhook,params={'wait':'true'},json={
            'content':'🧪 Santiago Academy multi-crypto connection test — no trade signal.',
            'allowed_mentions':{'parse':[]}},timeout=25)
        response.raise_for_status()
        if not response.json().get('id'):raise RuntimeError('Discord test did not return message ID')
        print('DISCORD_TEST_SENT');return
    state=load_state()
    state.setdefault('signals', {})
    state.setdefault('reports', {})
    # Purge state only after 72h; duplicates within a 1H candle stay suppressed.
    now=datetime.now(timezone.utc).timestamp()
    state['sent']={k:v for k,v in state['sent'].items() if isinstance(v,(int,float)) and now-v<72*3600}
    role_ids=[x.strip() for x in os.environ.get('SA_DISCORD_ROLE_IDS','').split(',') if x.strip()]
    if any(not x.isdigit() for x in role_ids):raise ValueError('Role IDs must be numeric')
    # Evaluate prior signals silently; only final WIN or LOSS notifications are posted.
    if not args.dry_run:
        for signal_id, tracked in list(state['signals'].items()):
            # Closed wins still need processing until optional WIN-only forwarding succeeds.
            extra_win_webhook = os.environ.get('DISCORD_WINS_WEBHOOK_URL', '').strip()
            pending_extra_win = (bool(extra_win_webhook) and tracked.get('result') == 'WIN'
                                 and bool((tracked.get('hit_at') or [None])[0])
                                 and not tracked.get('win_extra_discord_posted'))
            pending_pdf = (tracked.get('result_pdf_eligible') and tracked.get('result_posted') and not tracked.get('result_pdf_posted'))
            pending_win_pdf = (bool(extra_win_webhook) and tracked.get('result') == 'WIN' and tracked.get('result_pdf_eligible') and not tracked.get('win_pdf_posted'))
            if tracked.get('closed_at') and not pending_extra_win and not pending_pdf and not pending_win_pdf and (tracked.get('result') not in ('WIN','LOSS') or tracked.get('result_posted')):
                continue
            try:
                history=candles(tracked['symbol'],'15m',limit=500)
                events=tracking.advance(tracked,history,datetime.now(timezone.utc))
                # A WIN is final for grading at TP1, even though TP2/TP3 continue tracking.
                if tracked.get('result') in ('WIN','LOSS') and not tracked.get('result_posted'):
                    tracked['result_pdf_eligible'] = True
                    mid=tracking.send_result(SESSION,webhook,tracked)
                    tracked['result_posted']=mid
                    checkpoint(state)
                    print(f'{tracked["symbol"]}: FINAL_RESULT {tracked["result"]} message={mid}',flush=True)
                # Attach an individual branded PDF for newly graded outcomes only.
                # Mark before sending? No: only an acknowledged Discord message is persisted.
                if tracked.get('result') in ('WIN','LOSS') and tracked.get('result_posted') and not tracked.get('result_pdf_posted'):
                    # Existing historic grades should not be replayed on first deployment.
                    if tracked.get('result_pdf_eligible'):
                        pdf = pdf_reports.trade_pdf(tracked)
                        pdf_mid = pdf_reports.send_pdf(SESSION, webhook,
                            f'📄 Santiago Academy {tracked["result"]} Trade Report — {tracked["symbol"]}',
                            pdf, f'Santiago_Academy_{tracked["result"]}_{tracked["symbol"]}_{str(tracked.get("id","signal"))[:32]}.pdf')
                        tracked['result_pdf_posted'] = pdf_mid
                        checkpoint(state)
                # Winning PDF is also delivered to the WIN-only channel.
                if (extra_win_webhook and tracked.get('result') == 'WIN' and
                        tracked.get('result_pdf_eligible') and tracked.get('result_posted') and
                        not tracked.get('win_pdf_posted')):
                    try:
                        win_pdf_mid = pdf_reports.send_pdf(SESSION, extra_win_webhook,
                            f'🟢 Santiago Academy Winning Trade PDF — {tracked["symbol"]}',
                            pdf_reports.trade_pdf(tracked),
                            f'Santiago_Academy_WIN_{tracked["symbol"]}_{str(tracked.get("id","signal"))[:32]}.pdf')
                        tracked['win_pdf_posted'] = win_pdf_mid
                        checkpoint(state)
                        public_distribution.enqueue('win:'+str(signal_id),'win',
                            pdf_reports.trade_pdf(tracked),
                            f'🟢 Santiago Academy WIN | {tracked["symbol"]} {tracked["side"]} | '
                            'TP1 observed in market candles. Educational only; not verified profit.')
                    except Exception as win_pdf_exc:
                        print(f'WIN_PDF_ERROR {signal_id}: {win_pdf_exc}',file=sys.stderr,flush=True)
                save_state(state)
                # Secondary delivery is isolated: failures cannot prevent primary reporting.
                if (extra_win_webhook and tracked.get('result') == 'WIN'
                        and bool((tracked.get('hit_at') or [None])[0])
                        and not tracked.get('win_extra_discord_posted')):
                    try:
                        extra_mid = tracking.send_win_to_extra_discord(SESSION, extra_win_webhook, tracked)
                        if extra_mid:
                            tracked['win_extra_discord_posted'] = str(extra_mid)
                            checkpoint(state)
                            print(f'{tracked["symbol"]}: WIN_EXTRA_DISCORD_SENT message={extra_mid}', flush=True)
                    except Exception as secondary_exc:
                        print(f'WIN_EXTRA_DISCORD_ERROR {signal_id}: {secondary_exc}', file=sys.stderr, flush=True)
            except Exception as exc:
                print(f'TRACKING_ERROR {signal_id}: {exc}',file=sys.stderr,flush=True)
                raise
        report_webhooks = {
            'daily': os.environ.get('DISCORD_DAILY_WEBHOOK_URL', '').strip(),
            'weekly': os.environ.get('DISCORD_WEEKLY_WEBHOOK_URL', '').strip(),
            'monthly': os.environ.get('DISCORD_MONTHLY_WEBHOOK_URL', '').strip(),
        }
        reports=pdf_reports.send_due_reports(state['signals'],state,datetime.now(timezone.utc),SESSION,report_webhooks,checkpoint=lambda: checkpoint(state),
            public_webhook=os.environ.get('DISCORD_WINS_WEBHOOK_URL','').strip())
        if reports: print('REPORT_PDFS_POSTED '+','.join(reports),flush=True)
        save_state(state)
    count=0
    for symbol in ASSETS:
        signal = None
        try:
            m15,h1,h4=candles(symbol,'15m'),candles(symbol,'1h'),candles(symbol,'4h')
            observe_shadow(state, symbol, m15, h1, h4)
            signal=evaluate(symbol,m15,h1,h4)
            if not signal:
                print(f'{symbol}: NO TRADE (filters not aligned)',flush=True);continue
            if symbol == 'BTC':
                try:
                    confirmed = confirm_bitfinex_btc(
                        signal, {'15m': m15, '1h': h1, '4h': h4})
                except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                    print(f'BTC: NO TRADE (Bitfinex confirmation unavailable: {exc})',
                          flush=True)
                    continue
                if not confirmed:
                    print('BTC: NO TRADE (Binance/Bitfinex disagree)', flush=True)
                    continue
            if signal['key'] in state['sent']:
                print(f'{symbol}: DUPLICATE SUPPRESSED',flush=True);continue
            if args.dry_run:
                print(f'{symbol}: QUALIFIED {signal["side"]}\n{message(signal)}',flush=True)
                continue
            image=chart(signal,m15)
            try:
                message_id=post(signal,image,webhook,role_ids)
            except Exception:
                # Delivery may have occurred without an acknowledgement. Stop this run.
                raise
            issued=datetime.now(timezone.utc)
            state['sent'][signal['key']]=issued.timestamp()
            state['signals'][signal['key']]=tracking.record_signal(signal,message_id,issued)
            checkpoint(state)
            count+=1
            print(f'{symbol}: DISCORD_SENT id={message_id}',flush=True)
        except (requests.RequestException,ValueError,RuntimeError,KeyError,IndexError) as exc:
            print(f'{symbol}: ERROR {type(exc).__name__}: {exc}',file=sys.stderr,flush=True)
            # Stop if posting may have succeeded but acknowledgement failed, to avoid duplicate sends.
            if not args.dry_run and signal is not None:
                raise  # Never silently continue after a potentially ambiguous send.
    save_state(state)
    print(f'SCAN_COMPLETE sent={count} dry_run={args.dry_run}',flush=True)

if __name__=='__main__':
    main()
