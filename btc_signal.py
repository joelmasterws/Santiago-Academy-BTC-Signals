"""Santiago Academy multi-asset educational Discord signals. Never places orders."""
import argparse
import hashlib
import io
import json
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
    volume_ratio=float(m15['volume'].iloc[-1]/m15['volume'].tail(21).iloc[:-1].mean())
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
    if not (bullish or bearish): return None
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
            if tracked.get('closed_at') and (tracked.get('result') not in ('WIN','LOSS') or tracked.get('result_posted')):
                continue
            try:
                history=candles(tracked['symbol'],'15m',limit=500)
                events=tracking.advance(tracked,history,datetime.now(timezone.utc))
                # A WIN is final for grading at TP1, even though TP2/TP3 continue tracking.
                if tracked.get('result') in ('WIN','LOSS') and not tracked.get('result_posted'):
                    mid=tracking.send_result(SESSION,webhook,tracked)
                    tracked['result_posted']=mid
                    checkpoint(state)
                    print(f'{tracked["symbol"]}: FINAL_RESULT {tracked["result"]} message={mid}',flush=True)
                save_state(state)
            except Exception as exc:
                print(f'TRACKING_ERROR {signal_id}: {exc}',file=sys.stderr,flush=True)
                raise
        reports=tracking.due_reports(state['signals'],state,datetime.now(timezone.utc),SESSION,webhook,checkpoint=lambda: checkpoint(state))
        if reports: print('REPORT_IMAGES_POSTED '+','.join(reports),flush=True)
        save_state(state)
    count=0
    for symbol in ASSETS:
        signal = None
        try:
            m15,h1,h4=candles(symbol,'15m'),candles(symbol,'1h'),candles(symbol,'4h')
            signal=evaluate(symbol,m15,h1,h4)
            if not signal:
                print(f'{symbol}: NO TRADE (filters not aligned)',flush=True);continue
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
