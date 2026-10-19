"""Run frozen A2 on older EG history using public TqSDK backtest access.

Market observations stay in memory. Only research configuration, diagnostics,
trades, equity and the report are saved. No broker connection, orders,
DataDownloader, credential file or production module is used.
"""
import argparse
from collections import defaultdict
import copy
import csv
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
import hashlib
import json
import math
from pathlib import Path
import time

import discover_eg_swing as engine
import research_eg_factors as factors

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/"reports"/"eg_a2_tqsdk_older_four_20261019"
ZONE = timezone(timedelta(hours=8))
START = "2025-08-16 09:00:00"
END = "2025-12-15 23:59:59"
WARMUP = "2025-07-16 00:00:00"
SDK_END = date(2025, 12, 16)  # One extra session closes all scored minutes.
SYMBOLS = {"main":"KQ.m@DCE.eg", "eg2509":"DCE.eg2509", "eg2601":"DCE.eg2601"}
FIELDS = {"o":"open","h":"high","l":"low","c":"close","v":"volume","p":"close_oi"}


def save(name, value, folder=OUT):
    folder.mkdir(parents=True, exist_ok=True)
    (folder/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def configuration(family):
    source = ROOT/"reports"/"eg_factor_research_20261019"/"analysis_plan.json"
    cfg = json.loads(source.read_text(encoding="utf-8"))["base_configuration"]
    return {**cfg, "name":family, "family":family}


class ClosedMinuteReader:
    """Only accept rows preceding the current, possibly unfinished SDK row."""
    def __init__(self, first=WARMUP):
        self.first = first
        self.last_id = None
        self.last_ns = -1
        self.rows = []

    def update(self, frame):
        if len(frame) < 2:
            return
        closed = frame.iloc[:-1]
        new = closed[(closed.datetime > self.last_ns) & (closed.id >= 0)]
        if new.empty:
            return
        for row in new.itertuples(index=False):
            ns = int(row.datetime)
            values = {k:float(getattr(row,v)) for k,v in FIELDS.items()}
            if not all(math.isfinite(v) for v in values.values()):
                continue
            dt = (datetime.fromtimestamp(ns//10**9, ZONE).replace(tzinfo=None)+timedelta(minutes=1)).isoformat(sep=" ")
            if dt < self.first:
                continue
            row_id = int(row.id)
            if self.last_id is not None:
                assert row_id == self.last_id+1, ("SDK buffer skipped records",self.last_id,row_id,dt)
            assert ns > self.last_ns
            assert values["l"] <= min(values["o"],values["c"]) <= max(values["o"],values["c"]) <= values["h"]
            assert all(values[k] == round(values[k]) for k in ["o","h","l","c"])
            assert values["v"] >= 0
            self.rows.append({"dt":dt, **values})
            self.last_ns, self.last_id = ns, row_id


def observe_sdk(username, password, max_minutes):
    from tqsdk import TqApi, TqAuth, TqBacktest, TqSim, BacktestFinished

    readers = {s:ClosedMinuteReader() for s in SYMBOLS}
    api = None
    serials = {}
    finished = False
    deadline = time.monotonic()+max_minutes*60
    previous_message = time.monotonic()
    try:
        api = TqApi(TqSim(40000), auth=TqAuth(username,password),
                    backtest=TqBacktest(start_dt=date(2025,7,16),end_dt=SDK_END), web_gui=False)
        password = None
        serials = {s:api.get_kline_serial(symbol,60,data_length=128) for s,symbol in SYMBOLS.items()}
        while time.monotonic() < deadline:
            api.wait_update(deadline=time.time()+1)
            for s,serial in serials.items():
                if api.is_changing(serial):
                    readers[s].update(serial)
            if time.monotonic()-previous_message >= 15:
                main = readers["main"].rows
                print("历史回放进度:",main[-1]["dt"] if main else "等待数据", "; 已处理主连分钟:",len(main),flush=True)
                previous_message = time.monotonic()
    except BacktestFinished:
        finished = True
        for s,serial in serials.items():
            readers[s].update(serial)
    finally:
        password = None
        if api is not None:
            api.close()
    if not finished:
        raise TimeoutError("历史回放未完成。没有输出完整四个月收益，可用 --max-minutes 增加时限。")
    minutes = {s:r.rows for s,r in readers.items()}
    assert minutes["main"] and minutes["main"][-1]["dt"] > END, "缺少测试终点以后的已完成分钟，不能确认完整覆盖"
    assert minutes["main"][0]["dt"] < START, "缺少热身历史"
    return minutes


def validate_scored_minutes(main, start, end):
    """Check actual2025 exchange sessions; do not mistake a data hole for rest."""
    first=date.fromisoformat(start[:10]);last=date.fromisoformat(end[:10])
    expected=set();day=first
    while day<=last:
        if day.weekday()<5 and not date(2025,10,1)<=day<=date(2025,10,8):
            segments=[(9*60,75),(10*60+30,60),(13*60+30,90)]
            if day!=date(2025,9,30):segments.append((21*60,120))
            for anchor,length in segments:
                for minute in range(1,length+1):
                    t=datetime.combine(day,datetime.min.time())+timedelta(minutes=anchor+minute)
                    dt=t.isoformat(sep=" ")
                    if start<=dt<=end:expected.add(dt)
        day+=timedelta(days=1)
    actual={b["dt"] for b in main if start<=b["dt"]<=end}
    absent=sorted(expected-actual);extra=sorted(actual-expected)
    assert not absent and not extra,("交易日历分钟覆盖不完整",{"missing":len(absent),"extra":len(extra),
        "first_missing":absent[:5],"first_extra":extra[:5]})
    return {"expected":len(expected),"observed":len(actual),"missing":0,"extra":0,
        "holiday_source":"https://www.yhqh.com.cn/col420/25379.html",
        "holiday":"2025Sep30no night,Oct1--8closed,Oct9day/nightresume"}


def bundle_from_minutes(minutes, start=START, end=END):
    session_coverage=validate_scored_minutes(minutes["main"],start,end)
    bars = {}; incomplete = {}
    for sym,ms in minutes.items():
        groups = defaultdict(list)
        for b in ms:
            d = datetime.fromisoformat(b["dt"])
            parent = d.replace(minute=d.minute//15*15)
            if d.minute % 15:parent += timedelta(minutes=15)
            groups[parent.isoformat(sep=" ")].append(b)
        bad = [{"dt":dt,"minutes":len(sub)} for dt,sub in groups.items() if len(sub) != 15]
        if sym == "main":
            assert not [x for x in bad if start <= x["dt"] <= end], ("主连测试区间有不完整15分钟",bad[:5])
        incomplete[sym] = bad
        complete = [m for dt,sub in sorted(groups.items()) if len(sub)==15 for m in sub]
        bars[sym] = engine.aggregate(complete,15)
        if sym != "main":engine.enrich(bars[sym])
    ix = {s:{b["dt"]:i for i,b in enumerate(bs)} for s,bs in bars.items() if s != "main"}
    mapping = {}; missing = []; boundaries = []
    for i,b in enumerate(bars["main"]):
        dt = b["dt"]
        matches = [s for s,index in ix.items() if dt in index and all(bars[s][index[dt]][k]==b[k] for k in ["c","p"])]
        if len(matches)==1:mapping[dt]=matches[0]
        elif start<=dt<=end:missing.append((i,b))
        next_open = datetime.fromisoformat(bars["main"][i+1]["minutes"][0]["dt"])-timedelta(minutes=1) if i+1<len(bars["main"]) else None
        b["calendar_flat"] = (next_open-datetime.fromisoformat(dt)).total_seconds()>86400 if next_open else False
        if b["calendar_flat"] and start<=dt<=end:
            d = datetime.fromisoformat(dt)
            assert (d.weekday()==4 and dt[11:]=="23:00:00") or dt=="2025-09-30 15:00:00", ("非预期长休市或数据缺口",dt)
            boundaries.append({"close":dt,"next_open":next_open.isoformat(sep=" ")})
    fallbacks=[]
    for i,b in missing:
        dt=b["dt"]
        neighbors=[mapping.get(bars["main"][j]["dt"]) for j in [i-1,i+1] if 0<=j<len(bars["main"])]
        matches=[s for s,index in ix.items() if dt in index and all(bars[s][index[dt]][k]==b[k] for k in ["h","l","c"])]
        if len(matches)==1 and len(neighbors)==2 and all(n==matches[0] for n in neighbors):
            sym=matches[0];reason="uniqueHLC and both adjacent members; concrete prices unchanged"
        else:
            votes={s:sum(all(m[k]==n[k] for k in ["c","p"]) for m,n in zip(b["minutes"],bars[s][index[dt]]["minutes"])) for s,index in ix.items() if dt in index}
            strongest=[s for s,v in votes.items() if v>=14]
            assert len(strongest)==1 and strongest[0] in neighbors and all(v==0 for s,v in votes.items() if s!=strongest[0]), ("主力身份不明确",dt,matches,neighbors,votes)
            sym=strongest[0];reason="14of15 exact minuteclose/OI, alternatives0, adjacent member; prices unchanged"
        mapping[dt]=sym;fallbacks.append({"dt":dt,"contract":sym,"criterion":reason})
    assert all(b["dt"] in mapping for b in bars["main"] if start<=b["dt"]<=end)
    assert any(b["dt"]>end for b in bars["main"]), "需要结束日以后一个完整15分钟确认日历"
    signals={};spec=next(s for s in factors.specifications() if s["id"]=="season_volume_2")
    for sym,bs in bars.items():
        if sym=="main":continue
        sig=[engine.signal(bs,i,"channel") for i in range(len(bs))]
        fs=factors.features(bs,sig)
        signals[(sym,"base")]=sig
        signals[(sym,"season_volume_2")]=[d if d and factors.passes(f,spec) else 0 for d,f in zip(sig,fs)]
        for i in sorted(set([len(bs)//3,len(bs)//2,len(bs)-2])):
            if i<1:continue
            prefix=copy.deepcopy(bs[:i+1]);engine.enrich(prefix)
            ps=[engine.signal(prefix,j,"channel") for j in range(len(prefix))]
            assert factors.features(prefix,ps)[i]==fs[i]
            assert (ps[i] if ps[i] and factors.passes(fs[i],spec) else 0)==signals[(sym,"season_volume_2")][i]
    members=[]
    for b in bars["main"]:
        if start<=b["dt"]<=end:
            sym=mapping[b["dt"]]
            if not members or members[-1]["contract"]!=sym:members.append({"dt":b["dt"],"contract":sym})
    diagnostics={"session_minute_coverage":session_coverage,"incomplete_signal_groups":incomplete,"calendar_boundaries":boundaries,"members":members,
                 "member_fallbacks":fallbacks,"prefix_causality_sampled":True,"unresolved_members":0}
    return (bars,ix,mapping,signals),diagnostics


def overlap_audit(minutes):
    out={}
    for sym in ["main","eg2601"]:
        cached={}
        paths=[ROOT/"reports"/"eg_ten_months_20261019"/(sym+"_1m.csv"),
               ROOT/"reports"/"eg_factor_research_20261019"/(sym+"_reserve_1m.csv")]
        before=engine.extended.OUT
        try:
            for path in paths:
                engine.extended.OUT=path.parent
                for b in engine.extended.load_minutes(path.stem.removesuffix("_1m")):
                    if b["dt"] in cached:assert cached[b["dt"]]==b
                    cached[b["dt"]]=b
        finally:engine.extended.OUT=before
        common=exact=0;differences=defaultdict(int);examples=[]
        for b in minutes[sym]:
            if not "2025-10-23 09:00:00"<=b["dt"]<="2025-12-15 23:59:59" or b["dt"] not in cached:continue
            common+=1;old=cached[b["dt"]]
            changed=[k for k in FIELDS if old[k]!=b[k]]
            if not changed:exact+=1
            for k in changed:differences[k]+=1
            if changed and len(examples)<10:examples.append({"dt":b["dt"],"fields":changed})
        out[sym]={"overlap_minutes":common,"all_fields_exact":exact,"different_fields_counts":dict(differences),"examples":examples}
    return out


def evaluate(minutes):
    bundle,diagnostics=bundle_from_minutes(minutes)
    save("data_verification.json",diagnostics)
    save("source_overlap_audit.json",overlap_audit(minutes))
    save("data_coverage.json",{s:{"rows":len(ms),"first":ms[0]["dt"] if ms else None,"last":ms[-1]["dt"] if ms else None,
        "in_memory_sha256":hashlib.sha256(json.dumps(ms,separators=(",",":"),sort_keys=True).encode()).hexdigest()} for s,ms in minutes.items()})
    rows=[];monthly=[]
    for family in ["base","season_volume_2"]:
        for slip in [1,2,3]:
            for mode in ["ohlc","olhc"]:
                s,ts,curve=engine.run(bundle,configuration(family),(START,END),slip,mode)
                rows.append(s);save(f"{family}_slip{slip}_{mode}_trades.json",ts)
                if mode=="ohlc":
                    with (OUT/f"{family}_slip{slip}_equity.csv").open("w",encoding="utf-8-sig",newline="") as f:
                        w=csv.DictWriter(f,fieldnames=["dt","equity","qty"]);w.writeheader();w.writerows(curve)
                    levels={};last=0
                    for e in curve:levels[e["dt"][:7]]=e["equity"]
                    for month,level in levels.items():
                        monthly.append({"family":family,"slip":slip,"month":month,"net_equity_change":level-last,
                                        "entry_trades":sum(t["entry_dt"].startswith(month) for t in ts)})
                        last=level
                    assert sum(x["net_equity_change"] for x in monthly if x["family"]==family and x["slip"]==slip)==s["net_pnl"]
                print(family,slip,mode,"net",s["net_pnl"],"minuteDD",s["minute_close_max_drawdown"],"trades",s["trades"],flush=True)
    save("all_results.json",rows);save("monthly_equity_changes.json",monthly)
    save("verification.json",{"completed":True,"full_older_four_months":True,"window":[START,END],"scenarios":len(rows),
        "no_tuning":True,"no_raw_history_export":True,"main_is_TqSDK_not_SinaEG0":True,
        "checks":["closed SDK minute rows only","buffer sequence continuity","complete main signal bars","unique constituent mapping",
                  "indicator prefix causality","one-lot fee/risk/margin/calendar assertions","trade/equity/monthly reconciliation"],
        "execution":"same custom minute OHLC/OLHC runner as prior research, not SDK default matching or SDK zero-order account statistics"})
    lines=["# 固定A2：TqSDK更早四个月复核","",f"区间：{START[:10]}—{END[:10]}；热身从{WARMUP[:10]}开始。", "",
        "实际1手、4万元资金、每边3元手续费，1/2/3跳分别重放；普通隔夜允许、周末/长假清仓。A2量比阈值1.5及全部规则固定。天勤主连只确定具体成员，不保证与新浪EG0换月一致。", "",
        "使用TqSDK回测模式读取历史，在内存中沿用既有分钟执行引擎计算；没有向SDK模拟账户发单，所以SDK关闭时显示的零交易/零收益不是下表策略结果。", "",
        "| 版本 | 滑点 | 净收益 | 分钟收盘权益最大回撤 | 笔数 | 胜率 |", "|---|---:|---:|---:|---:|---:|"]
    for family in ["base","season_volume_2"]:
        for slip in [1,2,3]:
            rs=[r for r in rows if r["family"]==family and r["slip"]==slip]
            r=next(r for r in rs if r["mode"]=="ohlc")
            lines.append(f"| {family} | {slip}跳 | {min(x['net_pnl'] for x in rs):+,.0f} | {max(x['minute_close_max_drawdown'] for x in rs):,.0f} | {r['trades']} | {r['win_rate_pct']:.1f}% |")
    lines += ["","净收益取两条分钟路径较低值，回撤取较高值；不是逐笔最坏风险。保证金用25%名义金额作研究假设，未核验期货公司逐日实收。", "",
        "## A2自然月权益变化（两端含残月）","","| 月份 | 1跳净收益 | 2跳净收益 | 3跳净收益 |","|---|---:|---:|---:|"]
    for month in sorted(set(r["month"] for r in monthly)):
        values=[next(r["net_equity_change"] for r in monthly if r["family"]=="season_volume_2" and r["slip"]==s and r["month"]==month) for s in [1,2,3]]
        lines.append(f"| {month} | "+" | ".join(f"{v:+,.0f}" for v in values)+" |")
    lines += ["", "当前报告仅为更早四个月。与旧十个月的热身、数据渠道及资金/持仓边界各有区别，不能直接相加宣称十四个月连续收益。较早10月23日—12月15日已经观察过，整个区间不是完全未触碰样本。", "",
        "数据覆盖、成员切换、日历边界、原快照重叠差异分别见data_coverage.json、data_verification.json、source_overlap_audit.json。未保存原始历史行情文件，复现需再次本地登录SDK。", "",
        "成交仍是分钟情景估算：没有限价排队、动态价差和涨跌停不可成交模拟，止损不能保证跳空损失封顶。周末长假退出按最后收盘加滑点近似，需要提前发单的实际执行验证。", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")
    print("\n实际A2成绩已保存:",OUT/"REPORT.md",flush=True)


def self_check():
    import pandas as pd
    rows=[]
    for i in range(6):
        dt=datetime(2025,8,18,9,i,tzinfo=ZONE)
        rows.append({"id":i,"datetime":int(dt.timestamp())*10**9,"open":4000+i,"high":4002+i,
                     "low":3999+i,"close":4001+i,"volume":10+i,"close_oi":1000+i})
    reader=ClosedMinuteReader(first="2025-08-18 00:00:00")
    for i in range(1,len(rows)):
        frame=pd.DataFrame(rows[max(0,i-2):i+1]).copy()
        frame.loc[frame.index[-1],"high"]=999999
        frame.loc[frame.index[-1],"close"]=999999
        reader.update(frame)
    assert len(reader.rows)==5 and all(b["h"]<999999 and b["c"]<999999 for b in reader.rows)
    minutes={};before=engine.extended.OUT
    try:
        for sym in ["main","eg2601"]:
            merged={}
            for folder,name in [(ROOT/"reports"/"eg_ten_months_20261019",sym),
                                (ROOT/"reports"/"eg_factor_research_20261019",sym+"_reserve")]:
                engine.extended.OUT=folder
                for b in engine.extended.load_minutes(name):
                    if b["dt"]<="2025-12-16 15:00:00":
                        if b["dt"] in merged:assert b==merged[b["dt"]]
                        merged[b["dt"]]=b
            minutes[sym]=[merged[k] for k in sorted(merged)]
    finally:engine.extended.OUT=before
    span=("2025-10-23 09:00:00","2025-12-15 23:59:59")
    bundle,_=bundle_from_minutes(minutes,*span)
    prior=json.loads((ROOT/"reports"/"eg_a2_older_four_20261019"/"all_results.json").read_text(encoding="utf-8"))
    count=0
    for family in ["base","season_volume_2"]:
        for slip in [1,2,3]:
            for mode in ["ohlc","olhc"]:
                s,_,_=engine.run(bundle,configuration(family),span,slip,mode)
                old=next(r for r in prior if r["window"]=="partial" and r["family"]==family and r["slip"]==slip and r["mode"]==mode)
                for key in ["net_pnl","minute_close_max_drawdown","trades","fees","worst_trade"]:
                    assert s[key]==old[key],(family,slip,mode,key,s[key],old[key])
                count+=1
    result={"passed":True,"completed_minute_filter":True,"prior_partial_scenarios_reproduced":count,
            "new_older_four_months_executed":False,"SDK_login_not_tested_here":True}
    save("offline_self_check.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description="固定A2更早四个月回测；本地隐藏输入密码，不连接实盘。")
    parser.add_argument("--self-check",action="store_true",help="只用已有快照验证执行逻辑，无需登录")
    parser.add_argument("--max-minutes",type=int,default=60,help="历史回放最长实际运行分钟数，默认60")
    args=parser.parse_args()
    if args.self_check:
        self_check();return
    if args.max_minutes<1:parser.error("--max-minutes必须大于0")
    save("run_status.json",{"completed":False,"stage":"awaiting_local_login", "started_at":datetime.now(ZONE).isoformat()})
    save("frozen_plan.json",{"created_before_run":datetime.now(ZONE).isoformat(),"test":[START,END],"warmup":WARMUP,
        "sdk_replay_end":SDK_END.isoformat(),"symbols":SYMBOLS,"A2":configuration("season_volume_2"),
        "volume_factor":"current complete15minute / median prior up to10 same-clock observations, min3, >=1.5",
        "scenarios":"2fixed strategies x3slippage x2minute paths; no tuning","no_raw_market_export":True,
        "code_sha256":{s:hashlib.sha256((ROOT/"scripts"/s).read_bytes()).hexdigest() for s in
                       ["backtest_eg_a2_tqsdk.py","discover_eg_swing.py","research_eg_factors.py"]}})
    username=input("Tianqin / Kuaqi account (local input): ").strip()
    password=getpass("Password (hidden, not saved): ")
    if not username or not password:raise SystemExit("账号密码为空，未连接。")
    print("只运行历史研究，不向TQSIM或实盘发单。SDK的零交易统计不代表本脚本计算的策略成绩。",flush=True)
    try:
        minutes=observe_sdk(username,password,args.max_minutes)
        password=None
        evaluate(minutes)
    except Exception as exc:
        # Avoid serializing auth objects, passwords, or SDK exception internals.
        save("run_status.json",{"completed":False,"exception_type":type(exc).__name__,
             "meaning":"本次未确认完整四个月，不能使用旧REPORT作为本次成功结果。"})
        print("未完成完整回测：",type(exc).__name__,"。请保留终端错误类型；账号密码未保存。")
        if isinstance(exc,AssertionError):
            # Our own assertion diagnostics concern market coverage/accounting only.
            print("数据/对账检查:",exc)
        raise SystemExit(1) from None
    finally:password=None
    save("run_status.json",{"completed":True,"report":"REPORT.md"})


if __name__=="__main__":main()
