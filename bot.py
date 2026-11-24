"""Paper-only expiry option-selling simulator. No authenticated trading endpoints."""
import argparse, copy, csv, fcntl, json, logging, math, os, signal, time, uuid
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

IST = ZoneInfo('Asia/Kolkata')
INDICES = {'NIFTY': ('nse_fo', 'nse_cm', 'Nifty 50'), 'SENSEX': ('bse_fo', 'bse_cm', 'SENSEX')}
TERMINAL = {'complete', 'completed', 'filled', 'cancelled', 'canceled', 'rejected', 'expired'}
LOG = logging.getLogger('expiry')

def now(): return datetime.now(IST)
def tm(s): return dt_time.fromisoformat(s)
def num(x):
    v = float(x)
    if not math.isfinite(v): raise ValueError('non-finite number')
    return v

def tick(x, step=.05, up=True):
    d = Decimal(str(step)); v = Decimal(str(x))/d
    return float(max(d, v.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*d))

def atomic(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(obj, f, indent=2, allow_nan=False); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def read(path, default):
    return json.loads(Path(path).read_text()) if Path(path).exists() else copy.deepcopy(default)

def data(resp):
    if not isinstance(resp, dict) or resp.get('error') or str(resp.get('stat','')).lower() in ('not_ok','not ok','error'):
        raise RuntimeError('Broker returned error; inspect broker portal')
    return resp.get('data', resp)

def rows(resp):
    # Documented no-data response only; other errors must not look like an empty book.
    if isinstance(resp, dict) and str(resp.get('stCode')) == '5203': return []
    value = resp if isinstance(resp, list) else data(resp)
    if not isinstance(value, list): raise RuntimeError('Unexpected broker list schema')
    return value

class Calendar:
    def __init__(self, cfg):
        self.start=date.fromisoformat(cfg['valid_from']); self.end=date.fromisoformat(cfg['valid_through'])
        self.holidays={k: {date.fromisoformat(d) for d in v} for k,v in cfg['holidays'].items()}
    def trading(self, d, idx):
        if not self.start <= d <= self.end: raise ValueError('Calendar coverage missing; update calendar.json')
        return d.weekday()<5 and d not in self.holidays[idx]
    def mode(self, today, expiry, idx):
        if not self.trading(today,idx): return None
        if today == expiry: return 0
        if expiry < today or (expiry-today).days>7: return None
        d=today+timedelta(days=1)
        while d < expiry:
            if self.trading(d,idx): return None
            d+=timedelta(days=1)
        # expiry is a broker-listed date; require calendar confirmation
        return 1 if self.trading(expiry,idx) else None

def norm(x): return .5*(1+math.erf(x/math.sqrt(2)))
def bs(s,k,t,r,vol,kind):
    d1=(math.log(s/k)+(r+vol*vol/2)*t)/(vol*math.sqrt(t)); d2=d1-vol*math.sqrt(t)
    if kind=='CE': return s*norm(d1)-k*math.exp(-r*t)*norm(d2), norm(d1)
    return k*math.exp(-r*t)*norm(-d2)-s*norm(-d1), norm(d1)-1

def implied_delta(s,k,t,r,p,kind):
    lo,hi=.0001,5.
    if not bs(s,k,t,r,lo,kind)[0] < p < bs(s,k,t,r,hi,kind)[0]: return None
    for _ in range(55):
        mid=(lo+hi)/2
        if bs(s,k,t,r,mid,kind)[0]<p: lo=mid
        else: hi=mid
    return bs(s,k,t,r,(lo+hi)/2,kind)[1]

def choose(chain, spot, mode, expiry, stamp, cfg):
    ce=[x for x in chain if x['kind']=='CE']; pe=[x for x in chain if x['kind']=='PE']
    if mode==0:
        both={x['strike'] for x in ce}&{x['strike'] for x in pe}
        if not both: raise ValueError('No common ATM strike')
        k=min(both,key=lambda k:(abs(k-spot),-k))
        return [next(x for x in side if x['strike']==k) for side in (ce,pe)]
    t=(datetime.combine(expiry,tm('15:30'),IST)-stamp).total_seconds()/(365*86400)
    if t<=0: raise ValueError('Expired')
    result=[]
    for kind,side,sign in [('CE',ce,1),('PE',pe,-1)]:
        otm=[x for x in side if sign*(x['strike']-spot)>0 and x['ltp']>0]
        if not otm: raise ValueError('Missing OTM options')
        if cfg['strike_mode']=='distance':
            target=spot*(1+sign*cfg['distance_fraction'])
            result.append(min(otm,key=lambda x:abs(x['strike']-target)))
        else:
            candidates=[]
            for x in otm:
                delta=implied_delta(spot,x['strike'],t,cfg['risk_free_rate'],x['ltp'],kind)
                if delta is not None: candidates.append((abs(abs(delta)-cfg['target_delta']),x,delta))
            if not candidates: raise ValueError('Cannot estimate delta')
            err,x,d=min(candidates,key=lambda x:x[0])
            if err>cfg['delta_tolerance']: raise ValueError('No option close enough to target delta')
            result.append({**x,'model_delta':d})
    return result

def wings(chain, shorts, cfg):
    result=[]
    for short in shorts:
        sign=1 if short['kind']=='CE' else -1
        candidates=[x for x in chain if x['kind']==short['kind'] and sign*(x['strike']-short['strike'])>0 and cfg['wing_min']<=x['ltp']<=cfg['wing_max']]
        if not candidates: raise ValueError('No eligible hedge wing in requested price band')
        result.append(min(candidates,key=lambda x:abs(x['strike']-short['strike'])))
    return result

class Neo:
    """Market-data-only adapter: no trading session, login or order methods."""
    def __init__(self,cfg):
        from neo_api_client import NeoAPI
        self.cfg=cfg
        key=os.environ.get('NEO_CONSUMER_KEY','').strip()
        if not key or key.startswith('replace_'): raise ValueError('Set NEO_CONSUMER_KEY in .env')
        self.client=NeoAPI(consumer_key=key,environment='prod')
    def expiries(self,idx):
        res=data(self.client.expiries(exchange=INDICES[idx][0],underlying=idx,instrument_type='option'))
        return sorted(date.fromisoformat(d) for d in res['expiries'])
    def chain(self,idx,expiry):
        seg=INDICES[idx][0]
        raw=data(self.client.option_chain(exchange=seg,underlying=idx,expiry=expiry.isoformat(),instrument_type='option',count=self.cfg['chain_count']))
        common=raw['common_data']; lot=int(common['mktLot'])
        if common['expiryDt']!=expiry.isoformat() or common['exSeg']!=seg or common['unlSymbol'].upper()!=idx or lot<=0: raise ValueError('Option chain identity mismatch')
        out=[]
        for side,kind in [('call','CE'),('put','PE')]:
            for row in raw[side]:
                i=row['instrument']; ex,tok=i['neoSymbol'].split('|',1)
                if ex!=seg or i['optionType']!=kind: raise ValueError('Instrument mismatch')
                if not row['quote'].get('ltp'): continue
                p=num(row['quote']['ltp'])
                if p<=0: continue
                out.append(dict(seg=seg,token=tok,symbol=i['symbol'],kind=kind,strike=num(i['strikePrice']),ltp=p,lot=lot))
        return out
    def quote(self,contract, *, depth=True, fresh=True):
        q=rows(self.client.quotes(instrument_tokens=[{'instrument_token':contract['token'],'exchange_segment':contract['seg']}],quote_type='all'))
        if len(q)!=1: raise ValueError('Missing/ambiguous quote')
        q=q[0]
        if q.get('exchange')!=contract['seg']: raise ValueError('Quote segment mismatch')
        if depth and str(q.get('exchange_token'))!=str(contract['token']): raise ValueError('Quote token mismatch')
        if not depth and str(q.get('exchange_token','')).casefold()!=contract['token'].casefold() and str(q.get('display_symbol','')).casefold()!=contract['token'].casefold(): raise ValueError('Index quote identity mismatch')
        p=num(q['ltp'])
        if p<=0: raise ValueError('Zero price')
        if fresh:
            age=time.time()-num(q['lstup_time'])
            if not -5<=age<=self.cfg['quote_max_age']: raise ValueError(f'Stale quote ({age:.0f}s)')
        if not depth: return {'ltp':p}
        b,a=q['depth']['buy'][0],q['depth']['sell'][0]
        bid,ask=num(b['price']),num(a['price'])
        if bid<=0 or ask<bid: raise ValueError('Invalid bid/ask')
        return dict(ltp=p,bid=bid,ask=ask,bid_qty=int(b['quantity']),ask_qty=int(a['quantity']))
    def spot(self,idx):
        _,seg,tok=INDICES[idx]; return self.quote({'seg':seg,'token':tok},depth=False)['ltp']

class Paper:
    """Market-data-backed simulated fills. Does not call any Neo order endpoint."""
    def __init__(self, market, path): self.market=market; self.path=path; self.book=read(path,[])
    def expiries(self,idx): return self.market.expiries(idx)
    def chain(self,idx,expiry): return self.market.chain(idx,expiry)
    def spot(self,idx): return self.market.spot(idx)
    def quote(self,contract): return self.market.quote(contract)
    def save(self): atomic(self.path,self.book)
    def funds(self): return 1e9
    def positions(self):
        net={}
        for row in self.book:
            key=(row['exSeg'],row['trdSym']); net[key]=net.get(key,0)+(1 if row['trnsTp']=='B' else -1)*row['fldQty']
        return [dict(exSeg=e,trdSym=s,cfBuyQty=0,cfSellQty=0,flBuyQty=max(q,0),flSellQty=max(-q,0),prod=self.market.cfg['product']) for (e,s),q in net.items()]
    def place(self,o):
        c=o['contract']; r=dict(nOrdNo=uuid.uuid4().hex,GuiOrdId=o['tag'],ordSt='trigger pending' if o['type']=='SL' else 'open',fldQty=0,avgPrc=0,qty=o['qty'],prc=o['price'],trgPrc=o['trigger'],trnsTp=o['side'],trdSym=c['symbol'],exSeg=c['seg'],contract=c,prcTp=o['type'])
        self.book.append(r); self.save(); return r['nOrdNo']
    def orderbook(self):
        changed=False
        for r in self.book:
            if r['ordSt'] in TERMINAL: continue
            try: q=self.market.quote(r['contract'])
            except Exception: continue
            if r['ordSt']=='trigger pending' and q['ltp']>=r['trgPrc']: r['ordSt']='open'; changed=True
            if r['ordSt']!='open': continue
            side=r['trnsTp']; p=q['ask'] if side=='B' else q['bid']
            depth=q['ask_qty'] if side=='B' else q['bid_qty']
            if depth>=r['qty'] and ((side=='B' and p<=r['prc']) or (side=='S' and p>=r['prc'])):
                r.update(ordSt='complete',fldQty=r['qty'],avgPrc=p); changed=True
        if changed: self.save()
        return self.book
    def cancel(self,o):
        for r in self.book:
            if r['nOrdNo']==o['id'] and r['ordSt'] not in TERMINAL: r['ordSt']='cancelled'; self.save(); return
    def modify(self,o,trigger,price):
        for r in self.book:
            if r['nOrdNo']==o['id'] and r['ordSt']=='trigger pending': r.update(trgPrc=trigger,prc=price); self.save(); return
        raise RuntimeError('Paper stop is no longer pending')

class Uncertain(RuntimeError): pass

class Engine:
    def __init__(self,cfg,broker,root,calendar):
        self.cfg=cfg; self.b=broker; self.root=Path(root); self.cal=calendar
        self.state_path=self.root/'state.json'; self.state=read(self.state_path,{'trades':[]}); self.book=[]
        self.shutdown=False; self.exp_cache={}; self.last_alarm=0
    def save(self): atomic(self.state_path,self.state)
    def new_trade(self,idx,expiry,mode):
        t=dict(index=idx,day=now().date().isoformat(),expiry=expiry.isoformat(),mode=mode,phase='planning',orders=[],shorts=[],wings=[],reason='',trailed=False,gamma_checked=False)
        self.state['trades'].append(t); self.save(); return t
    def orders(self,t,role=None): return [o for o in t['orders'] if role is None or o['role']==role]
    def active(self,o): return o['status'] not in TERMINAL
    def refresh(self):
        self.book=self.b.orderbook(); byid={str(r['nOrdNo']):r for r in self.book}; tags={}
        for r in self.book:
            tag=str(r.get('GuiOrdId','')); tags.setdefault(tag,[]).append(r)
        for t in self.state['trades']:
            if t['phase']=='done': continue
            for o in t['orders']:
                if not self.active(o): continue
                rr=[byid[o['id']]] if o.get('id') in byid else tags.get(o['tag'],[])
                if len(rr)!=1:
                    # A response can be delayed. No blind retry: stop new exposure, manual reconciliation if persistent.
                    if time.time()-o['created']<self.cfg['order_timeout']: continue
                    raise Uncertain(f"Order {o['tag']} absent/ambiguous in book; inspect broker immediately")
                r=rr[0]
                if r['trdSym']!=o['contract']['symbol'] or r['exSeg']!=o['contract']['seg'] or r['trnsTp']!=o['side'] or int(r['qty'])!=o['qty']: raise Uncertain('Order identity mismatch')
                f=int(r['fldQty']); p=num(r['avgPrc']); st=' '.join(str(r.get('ordSt',r.get('stat',''))).strip().lower().replace('_',' ').split())
                if not st or not o['filled']<=f<=o['qty'] or (f and p<=0) or (st in ('complete','completed','filled') and f!=o['qty']): raise Uncertain('Invalid order fill/status')
                o.update(id=str(r['nOrdNo']),filled=f,avg=p,status=st,reported_trigger=num(r.get('trgPrc',0)))
        self.save()
        known_ids={o.get('id') for t in self.state['trades'] for o in t['orders']}
        known_tags={o['tag'] for t in self.state['trades'] for o in t['orders']}
        for r in self.book:
            st=' '.join(str(r.get('ordSt',r.get('stat',''))).strip().lower().replace('_',' ').split())
            if r.get('exSeg') in ('nse_fo','bse_fo') and st not in TERMINAL and str(r['nOrdNo']) not in known_ids and str(r.get('GuiOrdId')) not in known_tags:
                raise Uncertain('Foreign active derivative order; dedicated account required')
    def submit(self,t,c,side,qty,price,role,trigger=0):
        if qty<=0 or qty%c['lot']: raise ValueError('Invalid lot quantity; manual intervention required for odd fill')
        o=dict(contract=c,side=side,qty=qty,price=price,trigger=trigger,type='SL' if trigger else 'L',role=role,tag='EX'+uuid.uuid4().hex[:18],created=time.time(),id=None,status='intent',filled=0,avg=0,reported_trigger=0)
        t['orders'].append(o); self.save() # durable write BEFORE broker submission
        try:
            oid=self.b.place(o)
            if not oid or oid in ('None','0'): raise ValueError('missing order ID')
            o['id']=oid; self.save()
        except Exception as e:
            LOG.error('Submission outcome unknown, tag=%s (%s)',o['tag'],type(e).__name__)
            # Never assume rejection/acceptance; resolve through order book/tag.
        return o
    def wait(self,o,condition,seconds=None):
        until=time.monotonic()+(seconds or self.cfg['order_timeout'])
        while time.monotonic()<until:
            self.refresh()
            if condition(o): return True
            time.sleep(.6)
        return False
    def cancel(self,o):
        self.refresh()
        if not self.active(o): return
        if not o.get('id'): raise Uncertain('Cannot cancel unresolved order intent')
        try: self.b.cancel(o)
        except Exception: LOG.warning('Cancel response uncertain; checking order book')
        if not self.wait(o,lambda x:not self.active(x)): raise Uncertain('Cancellation not confirmed; do not place competing exit')
    def net(self,t):
        n={}
        for o in t['orders']:
            k=o['contract']['symbol']; n[k]=n.get(k,0)+(1 if o['side']=='B' else -1)*o['filled']
        return n
    def contract(self,t,s): return next(o['contract'] for o in t['orders'] if o['contract']['symbol']==s)
    def limit(self,t,c,side,qty,role,exit_order=False,cap=None):
        q=self.b.quote(c); spread=(q['ask']-q['bid'])/max(q['ltp'],.05)
        if not exit_order and (spread>self.cfg['max_entry_spread_fraction'] or (q['ask_qty'] if side=='B' else q['bid_qty'])<qty): raise ValueError('Entry liquidity filter failed')
        slip=self.cfg['exit_limit_buffer'] if exit_order else self.cfg['entry_limit_buffer']
        p=tick((q['ask']*(1+slip)) if side=='B' else (q['bid']*(1-slip)),up=side=='B')
        if cap is not None:
            p=min(p,tick(cap,up=False))
            if p<q['ask']: raise ValueError('Hedge ask exceeds maximum permitted price')
        o=self.submit(t,c,side,qty,p,role)
        if not self.wait(o,lambda x:not self.active(x),self.cfg['fill_timeout']): self.cancel(o)
        return o
    def install_stop(self,t,entry):
        c=entry['contract']; trigger=tick(entry['avg']*(1+(.30 if t['mode']==0 else .35)))
        q=self.b.quote(c)
        if q['ltp']>=trigger: raise ValueError('Initial stop already crossed')
        o=self.submit(t,c,'B',entry['filled'],tick(trigger*(1+self.cfg['stop_limit_buffer'])), 'stop',trigger)
        if not self.wait(o,lambda x:x['status'] in TERMINAL or 'trigger pending' in x['status'] or x['status']=='open') or not self.active(o) or o['status']=='open': raise ValueError('Protective SL not confirmed pending')
    def verify_positions(self):
        # A dedicated account/product is required. Compare broker units to order journal, never flatten unrelated positions.
        expected={}
        for t in self.state['trades']:
            if t['phase']=='done': continue
            for s,q in self.net(t).items():
                c=self.contract(t,s); key=(c['seg'],s); expected[key]=expected.get(key,0)+q
        actual={}
        for p in self.b.positions():
            if p['exSeg'] not in ('nse_fo','bse_fo'): continue
            n=int(p['cfBuyQty'])+int(p['flBuyQty'])-int(p['cfSellQty'])-int(p['flSellQty'])
            if not n: continue
            if p.get('prod')!=self.cfg['product']: raise Uncertain('Unexpected F&O product in dedicated account')
            key=(p['exSeg'],p['trdSym']); actual[key]=actual.get(key,0)+n
        if {k:v for k,v in expected.items() if v}!=actual: raise Uncertain('Broker positions differ from journal; inspect account; no new orders')
    def start_wings(self,t):
        if self.b.funds()<self.cfg['min_available_funds']: raise ValueError('Available margin below configured minimum')
        exp=date.fromisoformat(t['expiry']); chain=self.b.chain(t['index'],exp); spot=self.b.spot(t['index'])
        t['shorts']=choose(chain,spot,t['mode'],exp,now(),self.cfg); t['phase']='entering_wings'; self.save()
        if self.cfg['hedge_enabled']:
            t['wings']=wings(chain,t['shorts'],self.cfg); self.save()
            for c in t['wings']:
                if now().time()>=tm(self.cfg['entry_time']): raise ValueError('Hedge window ended')
                o=self.limit(t,c,'B',c['lot']*self.cfg['lots'],'wing',cap=self.cfg['wing_max'])
                if o['filled']!=o['qty']: raise ValueError('Hedge not fully filled')
        t['phase']='hedged'; self.save()
    def enter_shorts(self,t):
        exp=date.fromisoformat(t['expiry']); chain=self.b.chain(t['index'],exp); spot=self.b.spot(t['index'])
        shorts=choose(chain,spot,t['mode'],exp,now(),self.cfg)
        if self.cfg['hedge_enabled']:
            for s,w in zip(shorts,t['wings']):
                if s['kind']!=w['kind'] or s['lot']!=w['lot'] or (s['strike']>=w['strike'] if s['kind']=='CE' else s['strike']<=w['strike']): raise ValueError('Hedge no longer brackets short strike')
        if t['mode']==1 and self.cfg['strike_mode']=='delta':
            tau=(datetime.combine(exp,tm('15:30'),IST)-now()).total_seconds()/(365*86400)
            for c in shorts:
                delta=implied_delta(spot,c['strike'],tau,self.cfg['risk_free_rate'],self.b.quote(c)['ltp'],c['kind'])
                if delta is None or abs(abs(delta)-self.cfg['target_delta'])>self.cfg['delta_tolerance']:
                    raise ValueError('Fresh quote delta outside configured tolerance')
        t['shorts']=shorts; t['phase']='entering_shorts'; self.save()
        for c in shorts:
            if now().time()>tm(self.cfg['entry_deadline']): raise ValueError('Entry deadline passed')
            o=self.limit(t,c,'S',c['lot']*self.cfg['lots'],'short')
            if o['filled']!=o['qty']: raise ValueError('Short not fully filled')
            self.install_stop(t,o)
            self.refresh()
            if any(not self.active(x) or x['filled'] for x in self.orders(t,'stop')): raise ValueError('Stop changed during basket entry')
        t['phase']='active'; self.save(); LOG.info('PAPER %s %s DTE entry: %s',t['index'],t['mode'],', '.join(f"{o['contract']['symbol']} x{o['filled']} @ {o['avg']:.2f}" for o in self.orders(t,'short')),extra={'notify':True})
    def pnl(self,t):
        cash=sum((1 if o['side']=='S' else -1)*o['filled']*o['avg'] for o in t['orders'])
        for s,q in self.net(t).items():
            if q:
                px=self.b.quote(self.contract(t,s)); cash+=q*(px['bid'] if q>0 else px['ask'])
        return cash
    def closing(self,t,reason):
        if t['phase']!='closing': LOG.warning('PAPER %s closing: %s',t['index'],reason,extra={'notify':True})
        t['phase']='closing'; t['reason']=reason; self.save()
    def monitor(self,t):
        stops=self.orders(t,'stop'); entries=self.orders(t,'short')
        if len(stops)!=2 or len(entries)!=2: self.closing(t,'invalid basket'); return
        closed=[o for o in stops if o['filled']==o['qty']]
        for o in stops:
            if o['filled'] not in (0,o['qty']) or (not self.active(o) and o['filled']!=o['qty']) or (self.active(o) and o['status']=='open'):
                self.closing(t,'stop partially filled, rejected, or triggered without fill'); return
        if len(closed)==2: self.closing(t,'both stops filled'); return
        if len(closed)==1 and not t['trailed']:
            LOG.info('PAPER %s stop filled: %s @ %.2f',t['index'],closed[0]['contract']['symbol'],closed[0]['avg'],extra={'notify':True})
            survivor=next(o for o in stops if o not in closed)
            entry=next(o for o in entries if o['contract']['symbol']==survivor['contract']['symbol'])
            trigger=tick(entry['avg']); q=self.b.quote(survivor['contract'])
            if q['ltp']>=trigger: self.closing(t,'surviving stop already crossed cost'); return
            price=tick(trigger*(1+self.cfg['stop_limit_buffer']))
            try: self.b.modify(survivor,trigger,price)
            except Exception: LOG.warning('Modify response uncertain, checking order report')
            if not self.wait(survivor,lambda o: not self.active(o) or (abs(o['reported_trigger']-trigger)<.001 and 'trigger pending' in o['status'])):
                self.closing(t,'cost stop not confirmed'); return
            survivor.update(trigger=trigger,price=price); t['trailed']=True; self.save()
            LOG.info('PAPER %s surviving stop moved to cost %.2f',t['index'],trigger,extra={'notify':True})
        if t['mode']==0 and not t['gamma_checked'] and now().time()>=tm(self.cfg['gamma_time']):
            # Compare the same two premiums, only while both original short legs remain open.
            if not closed:
                current=sum(self.b.quote(o['contract'])['ltp'] for o in entries)
                initial=sum(o['avg'] for o in entries)
                if current<self.cfg['gamma_fraction']*initial: self.closing(t,'14:30 gamma rule')
            t['gamma_checked']=True; self.save()
    def flatten(self,t):
        self.refresh()
        # Cancel incomplete non-stop orders first. Stop orders cancelled per short immediately before cover.
        for o in self.orders(t):
            if self.active(o) and o['role']!='stop': self.cancel(o)
        symbols=list(self.net(t))
        for s in symbols:
            if self.net(t)[s]<0:
                for o in self.orders(t,'stop'):
                    if o['contract']['symbol']==s and self.active(o): self.cancel(o)
                q=self.net(t)[s]
                if q<0: self.limit(t,self.contract(t,s),'B',-q,'cover',exit_order=True)
        if any(v<0 for v in self.net(t).values()): return # retain wings until every short is flat
        for o in self.orders(t,'stop'):
            if self.active(o): self.cancel(o)
        for s,q in self.net(t).items():
            if q>0: self.limit(t,self.contract(t,s),'S',q,'wing_exit',exit_order=True)
        self.refresh()
        if all(v==0 for v in self.net(t).values()) and all(not self.active(o) for o in self.orders(t)):
            self.verify_positions()
            t['realized']=round(sum((1 if o['side']=='S' else -1)*o['avg']*o['filled'] for o in self.orders(t)),2)
            t['phase']='done'; self.save(); LOG.info('PAPER %s closed gross P&L %.2f',t['index'],t['realized'],extra={'notify':True}); self.export()
    def export(self):
        p=self.root/'trades.csv'; tmp=p.with_suffix('.tmp')
        with tmp.open('w',newline='') as f:
            w=csv.writer(f); w.writerow(['day','index','expiry','dte','reason','gross_pnl'])
            for t in self.state['trades']:
                if t['phase']=='done': w.writerow([t['day'],t['index'],t['expiry'],t['mode'],t['reason'],t.get('realized',0)])
        os.replace(tmp,p)
    def recover(self):
        self.refresh(); self.verify_positions()
        for t in self.state['trades']:
            if t['phase'] in ('planning','entering_wings','entering_shorts') or (t['phase']!='done' and t['day']!=now().date().isoformat()): self.closing(t,'restart recovery')
            # Active baskets retain journaled stops; no new entries for same index/day.
    def run_once(self):
        n=now(); clock=n.time(); today=n.date(); day=today.isoformat()
        kill=(self.root/'KILL').exists() or self.shutdown
        self.refresh()
        active=[t for t in self.state['trades'] if t['phase']!='done']
        # Order book/position feed can lag; discrepancy blocks trading until consistent.
        self.verify_positions()
        for t in active:
            try:
                if kill or clock>=tm(self.cfg['exit_time']) or t['day']!=day: self.closing(t,'kill switch' if kill else 'time exit/recovery')
                if t['phase']=='hedged':
                    if clock>tm(self.cfg['entry_deadline']): self.closing(t,'missed short entry')
                    elif clock>=tm(self.cfg['entry_time']): self.enter_shorts(t)
                if t['phase']=='active': self.monitor(t)
                if t['phase']=='closing': self.flatten(t)
            except Uncertain: raise
            except Exception as e:
                LOG.error('Basket error %s: %s',t['index'],e); self.closing(t,'execution/data failure')
        # Daily loss cap (gross mark-to-market, not guaranteed). Applies across indices.
        active=[t for t in self.state['trades'] if t['phase']!='done']
        total=sum(t.get('realized',0) for t in self.state['trades'] if t['day']==day and t['phase']=='done')
        if active: total+=sum(self.pnl(t) for t in active)
        if total<=-self.cfg['daily_loss_limit']:
            self.state['halt_day']=day; self.save()
            for t in active: self.closing(t,'daily loss limit')
            return
        if kill or self.state.get('halt_day')==day: return
        start=tm(self.cfg['hedge_time'] if self.cfg['hedge_enabled'] else self.cfg['entry_time'])
        deadline=tm(self.cfg['hedge_deadline'] if self.cfg['hedge_enabled'] else self.cfg['entry_deadline'])
        if not start<=clock<=deadline: return
        for idx in self.cfg['indices']:
            if any(t['index']==idx and t['day']==day for t in self.state['trades']): continue
            if len([t for t in self.state['trades'] if t['phase']!='done'])>=self.cfg['max_concurrent']: continue
            if not self.cal.trading(today,idx): continue
            dates=self.b.expiries(idx); candidates=[d for d in dates if d>=today]
            if not candidates: continue
            expiry=min(candidates); mode=self.cal.mode(today,expiry,idx)
            if mode is None: continue
            t=self.new_trade(idx,expiry,mode)
            try: self.start_wings(t)
            except Exception as e:
                LOG.error('Planning/hedge failed: %s',e); self.closing(t,'planning/hedge failure'); break
    def run(self):
        while True:
            try:
                self.run_once()
                if self.shutdown and all(t['phase']=='done' for t in self.state['trades']): return
                if now().time()>=tm('15:35') and all(t['phase']=='done' for t in self.state['trades']): return
            except Exception as e:
                LOG.critical('Paper loop blocked (%s); new entries halted for today.',type(e).__name__,extra={'notify':True})
                self.state['halt_day']=now().date().isoformat(); self.save()
                for t in self.state['trades']:
                    if t['phase']!='done': self.closing(t,'uncertain broker/data state')
                time.sleep(5)
            time.sleep(self.cfg['poll_seconds'])

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default='config.json'); ap.add_argument('--calendar',default='calendar.json'); ap.add_argument('--mode',choices=['paper'],default='paper'); ap.add_argument('--preflight',action='store_true'); args=ap.parse_args()
    os.umask(0o077); logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    from alerts import configure_alerts
    if not args.preflight: configure_alerts(LOG)
    cfg=read(args.config,{}); cal=Calendar(read(args.calendar,{}))
    if cfg['lots']!=1 or not 1<=cfg['max_concurrent']<=2 or cfg['daily_loss_limit']<=0 or cfg['poll_seconds']<1 or cfg['strike_mode'] not in ('delta','distance') or cfg['product'] not in ('MIS','NRML'): raise ValueError('Unsafe/invalid config; only 1 lot supported')
    if cfg['capital_rupees']<cfg['capital_per_unit']*cfg['max_concurrent']: raise ValueError('Capital allocation too small for concurrent units')
    root=Path('runtime')/args.mode; root.mkdir(parents=True,exist_ok=True)
    lock=(root/'instance.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        market=Neo(cfg)
    except Exception as e:
        LOG.error('PAPER market-data initialization failed (%s); check token and connectivity',type(e).__name__,extra={'notify':True})
        raise SystemExit(1) from None
    b=Paper(market,root/'paper_book.json')
    engine=Engine(cfg,b,root,cal)
    if args.preflight:
        for idx in cfg['indices']:
            ex=[x for x in b.expiries(idx) if x>=now().date()]
            if not ex: raise ValueError('No future expiry')
            chain=b.chain(idx,ex[0])
            if not chain: raise ValueError('Empty chain')
            LOG.info('%s expiry=%s lot=%s contracts=%d',idx,ex[0],chain[0]['lot'],len(chain))
            # Outside market hours quote freshness checks will fail; report separately.
            try: LOG.info('%s spot=%s',idx,b.spot(idx))
            except Exception as e: LOG.warning('Spot check: %s',e)
        return
    LOG.info('PAPER service started: no real orders; consumer token only',extra={'notify':True})
    # Before a fresh trading session, validate the data token without requesting 2FA.
    # Do not block recovery of existing simulated exposure with an unrelated expiry check.
    if not any(t['phase']!='done' for t in engine.state['trades']) and now().time()<tm(cfg['entry_time']):
        try:
            for idx in cfg['indices']:
                if cal.trading(now().date(),idx):
                    if not any(d>=now().date() for d in b.expiries(idx)): raise ValueError('No current expiry')
            LOG.info('PAPER market-data startup check passed')
        except Exception as e:
            LOG.error('PAPER startup data check failed (%s); check token/calendar/connectivity',type(e).__name__,extra={'notify':True})
            raise SystemExit(1) from None
    signal.signal(signal.SIGTERM,lambda *_:setattr(engine,'shutdown',True)); signal.signal(signal.SIGINT,lambda *_:setattr(engine,'shutdown',True))
    try: engine.recover()
    except Exception as e:
        LOG.critical('Recovery blocked: %s; no fresh trades allowed',e); engine.state['halt_day']=now().date().isoformat(); engine.save()
        for t in engine.state['trades']:
            if t['phase']!='done': engine.closing(t,'startup reconciliation failure')
    engine.run()
    today=now().date().isoformat()
    finished=[t for t in engine.state['trades'] if t['day']==today and t['phase']=='done']
    LOG.info('PAPER daily report %s: %d completed baskets; gross P&L INR %.2f (before costs)',today,len(finished),sum(t.get('realized',0) for t in finished),extra={'notify':True})

if __name__=='__main__': main()
