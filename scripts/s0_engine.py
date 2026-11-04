"""Small fail-closed S0 reference executor, not a certification of legacy replays.

All times are integer seconds. Session/trading-day annotations must come from
an audited, pre-known calendar. Prices here are concrete, unadjusted contracts.
"""
from dataclasses import dataclass
import math


def completed_aggregate(bars, seconds, asof):
    """Only fully observed, contiguous clock buckets; no padding over gaps/rolls."""
    if seconds<300 or seconds%300:raise ValueError('invalid aggregation period')
    groups={}
    for b in bars:
        if b.end>asof or not b.complete:continue
        key=(b.contract,b.trading_day,b.session,b.start//seconds*seconds)
        groups.setdefault(key,[]).append(b)
    result=[]
    for key,group in sorted(groups.items()):
        group=sorted(group,key=lambda b:b.start);start=key[-1]
        if [b.start for b in group]!=list(range(start,start+seconds,300)):continue
        result.append(dict(contract=key[0],start=start,end=start+seconds,open=group[0].open,
                           high=max(b.high for b in group),low=min(b.low for b in group),close=group[-1].close))
    return result


@dataclass(frozen=True)
class Bar:
    start: int
    end: int
    contract: str
    trading_day: str
    session: str
    session_end: int
    open: float
    high: float
    low: float
    close: float
    executable: bool = True
    complete: bool = True
    night: bool = False


@dataclass(frozen=True)
class Spec:
    contract: str
    valid_from: int
    valid_to: int
    multiplier: float
    tick: float
    margin_rate: float
    open_fee: float
    today_fee: float
    yesterday_fee: float
    source: str
    open_rate: float = 0
    today_rate: float = 0
    yesterday_rate: float = 0

    def fee(self, kind, price):
        return getattr(self, kind+'_fee') + price*self.multiplier*getattr(self, kind+'_rate')


@dataclass(frozen=True)
class Order:
    id: str
    contract: str
    signal_end: int
    side: int
    stop: float
    target: float
    kind: str = 'market'


@dataclass
class Position:
    order: Order
    entry: float
    spec: Spec
    day: str
    risk: float
    entry_fee: float
    last_end: int
    session_end: int


class Engine:
    def __init__(self, specs, capital=50000, trade_budget=400, slip_ticks=1, allow_night=False):
        if capital<=0 or trade_budget<=0 or slip_ticks<0: raise ValueError('invalid risk configuration')
        self.specs=list(specs)
        for s in self.specs:
            nums=(s.multiplier,s.tick,s.margin_rate,s.open_fee,s.today_fee,s.yesterday_fee,s.open_rate,s.today_rate,s.yesterday_rate)
            if not all(math.isfinite(x) and x>=0 for x in nums) or min(s.multiplier,s.tick,s.margin_rate)<=0 or s.valid_from>=s.valid_to or not s.source:
                raise ValueError('invalid metadata')
        self.balance=float(capital);self.trade_budget=min(trade_budget,600)
        self.slip_ticks=slip_ticks;self.allow_night=allow_night
        self.positions={};self.pending=[];self.seen=set();self.marks={}
        self.fills=[];self.trades=[];self.rejections=[];self.snapshots=[];self.invalid_evidence=[]
        self.last_time=-1;self.day=None;self.day_start=capital;self.peak=capital;self.halted=False;self.risk_liquidate=False

    def reject(self, o, reason):
        self.rejections.append(dict(id=o.id,reason=reason))

    def submit(self, o):
        if o.id in self.seen:self.reject(o,'duplicate_order');return
        if o.signal_end<self.last_time:raise ValueError('retroactive signal')
        if o.side not in (-1,1) or not all(math.isfinite(x) and x>0 for x in (o.stop,o.target)):
            raise ValueError('invalid order')
        self.seen.add(o.id);self.pending.append(o)

    def metadata(self, c, t):
        rows=[s for s in self.specs if s.contract==c and s.valid_from<=t<s.valid_to]
        return rows[0] if len(rows)==1 else None

    def account(self):
        equity=self.balance; margin=0; risk=0
        for c,p in self.positions.items():
            mark=self.marks[c]
            equity+=(mark-p.entry)*p.order.side*p.spec.multiplier
            margin+=mark*p.spec.multiplier*p.spec.margin_rate
            risk+=p.risk
        return equity,margin,risk

    def close(self, c, b, base, reason, at_open=False):
        p=self.positions.pop(c);s=p.spec;price=base-p.order.side*self.slip_ticks*s.tick
        fee=s.fee('today' if b.trading_day==p.day else 'yesterday',price)
        gross=(price-p.entry)*p.order.side*s.multiplier
        self.balance+=gross-fee
        t=b.start if at_open else b.end
        self.fills.append(dict(id=p.order.id,contract=c,time=t,offset='close',price=price,fee=fee))
        self.trades.append(dict(id=p.order.id,entry=p.entry,exit=price,gross=gross,
                                fees=p.entry_fee+fee,net=gross-p.entry_fee-fee,reason=reason))

    def guard(self):
        equity,_,_=self.account();self.peak=max(self.peak,equity)
        if self.day_start-equity>=900 or equity<=self.peak*.88:
            self.halted=True;self.risk_liquidate=True

    def step(self, bars):
        if not bars:return
        bars=sorted(bars,key=lambda b:b.contract);t=bars[0].start
        if t<=self.last_time or any(b.start!=t for b in bars) or len({b.contract for b in bars})!=len(bars):
            raise ValueError('batch must be unique contracts at one increasing start time')
        if len({b.trading_day for b in bars})!=1:raise ValueError('inconsistent trading-day annotations')
        for b in bars:
            if b.end-b.start!=300 or b.end>b.session_end or not all(math.isfinite(x) and x>0 for x in (b.open,b.high,b.low,b.close)) or not (b.low<=min(b.open,b.close)<=max(b.open,b.close)<=b.high):
                raise ValueError('invalid concrete 5m bar')
        by={b.contract:b for b in bars}
        # Only OPEN marks are available to admission decisions; never this bar's close.
        for b in bars:self.marks[b.contract]=b.open
        newday=bars[0].trading_day
        if newday!=self.day:
            self.day=newday;self.day_start=self.account()[0];self.halted=False;self.risk_liquidate=False
        for c,p in list(self.positions.items()):
            b=by.get(c)
            if b is None or b.start!=p.last_end or b.start>=p.session_end:
                self.invalid_evidence.append(dict(contract=c,time=t,reason='missing_bar_or_overdue_flat'))
                self.halted=True
            if b is not None:
                meta=self.metadata(c,t)
                if meta is None:
                    self.invalid_evidence.append(dict(contract=c,time=t,reason='metadata'));self.halted=True
                else:p.spec=meta
                if b.executable and (b.open-p.order.stop)*p.order.side<=0:
                    self.close(c,b,b.open,'stop',True)
        self.guard()
        # A prior close-time halt is executable only at the next available open.
        # Unexecutable positions remain outstanding and consume margin/risk.
        if self.risk_liquidate:
            for c in list(self.positions):
                b=by.get(c)
                if b is not None and b.executable:self.close(c,b,b.open,'risk_halt',True)
        remaining=[]
        for o in sorted(self.pending,key=lambda x:(x.signal_end,x.contract,x.id)):
            b=by.get(o.contract)
            if o.signal_end>t or b is None:remaining.append(o);continue
            reason=None;s=self.metadata(o.contract,t)
            if s is None:reason='metadata'
            elif o.kind!='market':reason='limit_queue_unknown'
            elif o.contract in self.positions:reason='duplicate_position'
            elif not b.complete:reason='incomplete_bar'
            elif not b.executable:reason='not_executable'
            elif b.night and not self.allow_night:reason='night_disabled'
            elif b.session_end-t<=600:reason='near_close'
            elif self.halted:reason='risk_halt'
            else:
                entry=b.open+o.side*self.slip_ticks*s.tick
                exit_stop=o.stop-o.side*self.slip_ticks*s.tick
                loss=(entry-exit_stop)*o.side*s.multiplier+s.fee('open',entry)+max(s.fee('today',exit_stop),s.fee('yesterday',exit_stop))
                equity,margin,risk=self.account();fee=s.fee('open',entry)
                newmargin=entry*s.multiplier*s.margin_rate
                if (entry-o.stop)*o.side<=0 or (o.target-entry)*o.side<=0:reason='invalid_levels'
                elif loss>self.trade_budget or loss>600:reason='lot_risk'
                elif risk+loss>900:reason='concurrent_risk'
                elif newmargin+fee>equity-margin:reason='cash'
                elif margin+newmargin>(equity-fee)*.6:reason='margin'
                else:
                    self.balance-=fee
                    self.positions[o.contract]=Position(o,entry,s,b.trading_day,loss,fee,b.end,b.session_end)
                    self.fills.append(dict(id=o.id,contract=o.contract,time=t,offset='open',price=entry,fee=fee))
            if reason:self.reject(o,reason)
        self.pending=remaining
        # All admissions finish before intrabar exits: future proceeds cannot fund opens.
        for b in bars:
            p=self.positions.get(b.contract)
            if p is None:continue
            p.last_end=b.end
            if not b.executable:
                if b.end>=p.session_end:self.invalid_evidence.append(dict(contract=b.contract,time=b.end,reason='flat_unfilled'))
                continue
            stop_hit=(b.low<=p.order.stop if p.order.side==1 else b.high>=p.order.stop)
            target_hit=(b.high>=p.order.target if p.order.side==1 else b.low<=p.order.target)
            if stop_hit:self.close(b.contract,b,min(b.open,p.order.stop) if p.order.side==1 else max(b.open,p.order.stop),'stop')
            elif target_hit:self.close(b.contract,b,p.order.target,'target')
            elif b.end>=p.session_end:self.close(b.contract,b,b.close,'session_flat')
        for b in bars:self.marks[b.contract]=b.close
        self.guard();equity,margin,risk=self.account()
        self.snapshots.append(dict(time=bars[0].end,balance=self.balance,equity=equity,
                                   unrealized=equity-self.balance,margin=margin,available=equity-margin,risk=risk))
        self.last_time=t
