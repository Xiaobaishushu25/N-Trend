import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const root=process.argv[2];
const data=JSON.parse(await fs.readFile(path.join(root,'summary.json'),'utf8'));
const incomplete=String(data.status||'').startsWith('INCOMPLETE');
const maps=JSON.parse(await fs.readFile(path.join(root,'monthly-contracts.json'),'utf8'));
const out=path.join(root,'outputs','01a17df5-ed6b-7911-9d08-5151905fa8d0');
await fs.mkdir(out,{recursive:true});
const wb=Workbook.create();
const summary=wb.worksheets.add('回测汇总');
const trades=wb.worksheets.add('逐笔交易');
const unknown=wb.worksheets.add('未确定交易');
const mapping=wb.worksheets.add('历史主力');
const money='#,##0.00;[Red](#,##0.00);0.00';
const dark='#23364D', blue='#2875B9', teal='#16847D';
const localDate=s=>s?new Date((s.length===10?s+'T00:00:00':s.replace(' ','T'))+'Z'):null;
function base(s,range){s.showGridLines=false;s.getRange(range).format.font={name:'Arial',size:11,color:'#203248'};s.getRange(range).format.rowHeight=21;s.getRange(range).format.verticalAlignment='center';}
function head(s,address){const q=s.getRange(address);q.format.fill=dark;q.format.font={name:'Arial',size:11,bold:true,color:'#FFFFFF'};q.format.horizontalAlignment='center';q.format.rowHeight=28;}
function title(s,text,range){s.getRange('A2').values=[[text]];s.getRange('A2').format.font={name:'Arial',size:16,bold:true,color:dark};s.getRange(range).format.borders={bottom:{style:'thin',color:'#AEBBC8'}};}
const tr=data.trades;
const rows=tr.map(z=>[z.strategy,Number(z.slip.slice(4)),z.symbol,z.name,z.contract,z.direction==='up'?'多':'空',localDate(z.warning_ts),localDate(z.entry_ts),localDate(z.exit_ts),localDate(z.exit_trading_day),z.month,z.entry,z.exit,z.gross_after_slippage,z.commission,z.net,z.exit_reason,null,null,null,null,null,z.event_key]);
base(trades,`A1:W${rows.length+5}`);
title(trades,'完整平仓交易', 'A2:W2');
trades.getRange('A3').values=[['来源：冻结策略分钟OHLC回放；时间为北京时间，金额为人民币。毛盈亏已含滑点，净额再扣手续费。']];
const headers=['策略','每边滑点/tick','品种','名称','实际合约','方向','预警时间','入场时间','平仓时间','平仓交易日','平仓月份','入场价','平仓价','滑点后毛盈亏/元','手续费/元','净盈亏/元','退出原因','累计净额/元','连续亏损额/元','连续亏损笔数','累计净额峰值/元','已实现回撤/元','信号键'];
trades.getRange('A5:W5').values=[headers];head(trades,'A5:W5');
trades.getRange(`A6:W${rows.length+5}`).values=rows;
trades.getRange(`G6:I${rows.length+5}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');trades.getRange(`J6:J${rows.length+5}`).setNumberFormat('yyyy-mm-dd');trades.getRange(`B6:B${rows.length+5}`).format.horizontalAlignment='center';
const end=rows.length+5;
const fsRows=rows.map((_,i)=>{const n=i+6,prev=n-1;const same=i>0&&tr[i].strategy===tr[i-1].strategy&&tr[i].slip===tr[i-1].slip;return [same?`=R${prev}+P${n}`:`=P${n}`,same?`=IF(P${n}<0,S${prev}+P${n},0)`:`=IF(P${n}<0,P${n},0)`,same?`=IF(P${n}<0,T${prev}+1,0)`:`=IF(P${n}<0,1,0)`,same?`=MAX(U${prev},R${n})`:`=MAX(0,R${n})`,`=U${n}-R${n}`];});
trades.getRange(`R6:V${end}`).formulas=fsRows;
trades.getRange(`P6:P${end}`).formulas=rows.map((_,i)=>[`=N${i+6}-O${i+6}`]);
trades.getRange(`L6:S${end}`).setNumberFormat(money);trades.getRange(`T6:T${end}`).setNumberFormat('0');trades.getRange(`U6:V${end}`).setNumberFormat(money);
trades.getRange('A:A').format.columnWidth=9;trades.getRange('B:B').format.columnWidth=16;trades.getRange('C:F').format.columnWidth=13;
trades.getRange('G:J').format.columnWidth=22;trades.getRange('K:K').format.columnWidth=12;trades.getRange('L:M').format.columnWidth=13;trades.getRange('N:V').format.columnWidth=22;trades.getRange('W:W').format.columnWidth=70;
trades.freezePanes.freezeRows(5);trades.freezePanes.freezeColumns(3);
trades.tables.add(`A5:W${end}`,true,'ClosedTrades');
const dr=c=>`'逐笔交易'!$${c}$6:$${c}$${end}`;
const count=(s,slip,extra='')=>`=COUNTIFS(${dr('A')},"${s}",${dr('B')},${slip}${extra})`;
const sum=(c,s,slip,extra='')=>`=SUMIFS(${dr(c)},${dr('A')},"${s}",${dr('B')},${slip}${extra})`;

base(unknown,`A1:I${Math.max(7,data.unresolved.length+5)}`);title(unknown,'未确定交易','A2:I2');
unknown.getRange('A3').values=[['损益不填零；未纳入平仓笔数、月度盈亏及已知平仓序列连亏。']];
unknown.getRange('A5:I5').values=[['策略','每边滑点/tick','品种','实际合约','预警时间','已知入场时间','状态','信号键','说明']];head(unknown,'A5:I5');
const ur=data.unresolved.map(z=>[z.strategy,Number(z.slip.slice(4)),z.symbol,z.contract,localDate(z.warning_ts),localDate(z.entry_ts),z.status,z.event_key,'未纳入确定损益']);
if(ur.length)unknown.getRange(`A6:I${ur.length+5}`).values=ur;
if(ur.length){unknown.getRange(`E6:F${ur.length+5}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');unknown.getRange(`B6:B${ur.length+5}`).format.horizontalAlignment='center';}
unknown.getRange('A:D').format.columnWidth=17;unknown.getRange('E:F').format.columnWidth=22;unknown.getRange('G:G').format.columnWidth=18;unknown.getRange('H:H').format.columnWidth=70;unknown.getRange('I:I').format.columnWidth=25;unknown.freezePanes.freezeRows(5);

base(mapping,`A1:F${maps.length+5}`);title(mapping,'逐月历史主力合约','A2:F2');
mapping.getRange('A3').values=[['来源：https://files.shinnytech.com/continuous_table.json；同月换月分行列示。']];
mapping.getRange('A5:F5').values=[['月份','品种','名称','当时主力合约','首个交易日','最后交易日']];head(mapping,'A5:F5');
mapping.getRange(`A6:F${maps.length+5}`).values=maps.map(z=>[z.month,z.symbol,z.name,z.contract,localDate(z.first),localDate(z.last)]);mapping.getRange(`E6:F${maps.length+5}`).setNumberFormat('yyyy-mm-dd');mapping.getRange('A:F').format.columnWidth=21;mapping.freezePanes.freezeRows(5);mapping.tables.add(`A5:F${maps.length+5}`,true,'HistoricalMainContracts');

base(summary,'A1:O65');summary.tabColor=dark;title(summary,incomplete?'B5与P2可用档案统计（全年未完成）':'B5六品种与P2近一年回测','A2:O2');
summary.getRange('A3').values=[[incomplete?'B5含2025年10月起档案；P2仅含2026年档案。部分品种有缺口，终点为10月23日上午。':'2025-10-27—2026-10-26，按平仓交易月份；每笔一手。首尾10月为部分月份。']];
if(incomplete){summary.getRange('A4').values=[['以下为可用片段，不能视为完整一年收益或最大连亏；缺数据≠零交易。']];summary.getRange('A4').format.font={name:'Arial',size:11,bold:true,color:'#A63829'};}
summary.getRange('A5:I5').values=[['策略','品种数','已知平仓笔数',incomplete?'片段净盈亏/元':'净盈亏/元',incomplete?'片段最大连亏/元':'最大连亏/元','最长连亏笔数','最大回撤/元','未确定笔数','净胜率']];head(summary,'A5:I5');
for(const [idx,s,qty] of [[6,'B5',6],[7,'P2',22]]){
 summary.getRange(`A${idx}:B${idx}`).values=[[s,qty]];
 summary.getRange(`C${idx}`).formulas=[[count(s,1)]];summary.getRange(`D${idx}`).formulas=[[sum('P',s,1)]];
 summary.getRange(`E${idx}`).formulas=[[`=-_xlfn.MINIFS(${dr('S')},${dr('A')},A${idx},${dr('B')},1)`]];
 summary.getRange(`F${idx}`).formulas=[[`=_xlfn.MAXIFS(${dr('T')},${dr('A')},A${idx},${dr('B')},1)`]];
 summary.getRange(`G${idx}`).formulas=[[`=_xlfn.MAXIFS(${dr('V')},${dr('A')},A${idx},${dr('B')},1)`]];
 summary.getRange(`H${idx}`).values=[[data.summary[s+'/slip1'].unknown]];
 summary.getRange(`I${idx}`).formulas=[[`=COUNTIFS(${dr('A')},A${idx},${dr('B')},1,${dr('P')},">0")/C${idx}`]];
}
summary.getRange('D6:E7').setNumberFormat(money);summary.getRange('G6:G7').setNumberFormat(money);summary.getRange('I6:I7').setNumberFormat('0.00%');
summary.getRange('A9').values=[['连亏按已知平仓顺序、净额<0连续相加；净额≥0中断。最大回撤不含持仓浮盈亏。']];
summary.getRange('A11:E11').values=[['平仓月份','B5笔数','B5净盈亏/元','P2笔数','P2净盈亏/元']];head(summary,'A11:E11');
const months=[...new Set(data.monthly.map(z=>z.month))].sort();
for(let i=0;i<months.length;i++){const n=12+i;summary.getRange(`A${n}`).values=[[months[i]]];const missing=incomplete&&months[i]<'2026-01';summary.getRange(`B${n}:E${n}`).formulas=[[count('B5',1,`,${dr('K')},A${n}`),sum('P','B5',1,`,${dr('K')},A${n}`),missing?'="缺数据"':count('P2',1,`,${dr('K')},A${n}`),missing?'="缺数据"':sum('P','P2',1,`,${dr('K')},A${n}`)]];}
summary.getRange('A25').values=[[incomplete?'已知记录小计':'合计']];summary.getRange('B25:E25').formulas=[['=SUM(B12:B24)','=SUM(C12:C24)','=SUM(D12:D24)','=SUM(E12:E24)']];summary.getRange('A25:E25').format.font.bold=true;
summary.getRange('C12:C25').setNumberFormat(money);summary.getRange('E12:E25').setNumberFormat(money);
if(!incomplete){const chart=summary.charts.add('bar',[summary.getRange('A11:A24'),summary.getRange('C11:C24'),summary.getRange('E11:E24')]);chart.title='每月净盈亏（元）';chart.titleTextStyle.typeface='Arial';chart.titleTextStyle.fontSize=14;chart.setPosition('G11','O27');chart.legend={position:'top',textStyle:{typeface:'Arial',fontSize:11}};chart.xAxis={axisType:'textAxis',textStyle:{typeface:'Arial',fontSize:10}};chart.yAxis={numberFormatCode:'#,##0',numberFormatSourceLinked:false,textStyle:{typeface:'Arial',fontSize:11}};chart.series.items[0].fill=teal;chart.series.items[1].fill=blue;}
else {summary.getRange('G12').values=[['行情覆盖缺口']];summary.getRange('G12').format.font.bold=true;summary.getRange('G14:G20').values=[['P2：2025年10—12月缺数据。'],['P2：2026年7—9月部分品种起点不齐。'],['B5：2026年7—8月鸡蛋行情有缺口。'],['双方：仅截至2026-10-23 11:05。'],['未补2026年10月23日下午及26日。'],['片段汇总不能比较完整一年收益。'],['月度按平仓交易日归属，整笔往返计1笔。']];}
summary.getRange('A28:E28').values=[['品种','B5笔数','B5净盈亏/元','P2笔数','P2净盈亏/元']];head(summary,'A28:E28');
const syms=data.products.filter(z=>z.strategy==='P2'&&z.slip==='slip1').map(z=>z.symbol);
const six=new Set(['EG0','PP0','V0','JD0','SR0','CJ0']);
for(let i=0;i<syms.length;i++){const s=syms[i],n=i+29,name=data.products.find(z=>z.symbol===s).name;summary.getRange(`A${n}`).values=[[`${name} ${s}`]];summary.getRange(`B${n}:E${n}`).formulas=[[six.has(s)?count('B5',1,`,${dr('C')},"${s}"`):'="—"',six.has(s)?sum('P','B5',1,`,${dr('C')},"${s}"`):'="—"',count('P2',1,`,${dr('C')},"${s}"`),sum('P','P2',1,`,${dr('C')},"${s}"`)]];}
summary.getRange('A51').values=[[incomplete?'已知记录小计':'合计']];summary.getRange('B51:E51').formulas=[['=SUM(B29:B50)','=SUM(C29:C50)','=SUM(D29:D50)','=SUM(E29:E50)']];summary.getRange('A51:E51').format.font.bold=true;summary.getRange('C29:C51').setNumberFormat(money);summary.getRange('E29:E51').setNumberFormat(money);
summary.getRange('G29').values=[['统计口径']];summary.getRange('G29').format.font.bold=true;
const notes=['B5：EG、PP、PVC、JD、SR、CJ；P2：原22品种。','主表每边1 tick；已扣冻结手续费，白糖往返2元。','手续费沿用2026-10-22快照，未还原逐日经纪商费率。','完整开仓—平仓计1笔，跨月交易在平仓月确认。','起点空仓；跨月不重置持仓、行业占用或同A止损记录。','按历史主力切换日使用月份合约；持仓保留原合约。','未知交易不填零，连亏仅对已知平仓序列统计。','同一分钟平仓按稳定信号键排序。','金额为单手机会组合净额，未模拟本金与保证金。','分钟OHLC无法复刻逐笔盘口及全部分钟内路径。'];
summary.getRange('G30:G39').values=notes.map(x=>[x]);
summary.getRange('G40').values=[['未知敞口保留至该合约现有行情终点，已重新竞争可用机会。']];
summary.getRange('G42:K42').values=[['策略','最大金额连亏笔数','第一笔平仓','最后一笔平仓','连亏金额/元']];head(summary,'G42:K42');
for(const [n,s] of [[43,'B5'],[44,'P2']]){const z=data.summary[s+'/slip1'].max_amount;summary.getRange(`G${n}:K${n}`).values=[[s,z.n,localDate(z.first_exit),localDate(z.last_exit),z.loss]];}
summary.getRange('I43:J44').setNumberFormat('yyyy-mm-dd hh:mm:ss');
summary.getRange('K43:K44').setNumberFormat(money);
summary.getRange('G47:K47').values=[['成本对照','平仓笔数','净盈亏/元','最大连亏/元','未确定笔数']];head(summary,'G47:K47');
for(const [n,s] of [[48,'B5'],[49,'P2']]){summary.getRange(`G${n}`).values=[[s+' 每边2 tick']];summary.getRange(`H${n}:J${n}`).formulas=[[count(s,2),sum('P',s,2),`=-_xlfn.MINIFS(${dr('S')},${dr('A')},"${s}",${dr('B')},2)`]];summary.getRange(`K${n}`).values=[[data.summary[s+'/slip2'].unknown]];}
summary.getRange('I48:J49').setNumberFormat(money);
summary.getRange('A:A').format.columnWidth=23;summary.getRange('B:B').format.columnWidth=12;summary.getRange('C:C').format.columnWidth=19;summary.getRange('D:D').format.columnWidth=19;summary.getRange('E:E').format.columnWidth=19;summary.getRange('F:F').format.columnWidth=18;summary.getRange('G:G').format.columnWidth=23;summary.getRange('H:H').format.columnWidth=22;summary.getRange('I:J').format.columnWidth=24;summary.getRange('K:K').format.columnWidth=20;summary.getRange('L:O').format.columnWidth=10;
for(const address of ['B6:I7','B12:E25','B29:E51','H43:K44','H48:K49'])summary.getRange(address).format.horizontalAlignment='right';
summary.getRange('D6:E7').conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{font:{color:'#B92734'}}});
for(const a of ['C12:C24','E12:E24','C29:C50','E29:E50'])summary.getRange(a).conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{font:{color:'#B92734'}}});
wb.recalculate();
const keyCheck=await wb.inspect({kind:'table',range:'回测汇总!A5:I7',include:'values,formulas',tableMaxRows:3,tableMaxCols:9,maxChars:6000});console.log(keyCheck.ndjson);
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:2500});console.log(errors.ndjson);
// Cross-check independent native report totals and sequence statistics.
for(const [n,s] of [[6,'B5'],[7,'P2']]){const v=summary.getRange(`C${n}:I${n}`).values[0],z=data.summary[s+'/slip1'];const expected=[z.n,z.net,z.max_amount.loss,z.max_count.n,z.realized_drawdown,z.unknown,z.win_rate];for(let j=0;j<expected.length;j++)if(typeof v[j]!=='number'||Math.abs(v[j]-expected[j])>1e-6)throw new Error(`Summary mismatch ${s} col${j}: ${v[j]} vs ${expected[j]}`);}
for(const [s,range,file] of [[summary,'A1:O27','summary'],[summary,'A28:K51','products'],[trades,'A1:Q14','trades'],[unknown,'A1:G15','unknown'],[mapping,'A1:F18','mapping']]){const blob=await wb.render({sheetName:s.name,range,scale:1,format:'png'});await fs.writeFile(path.join(out,file+'.png'),new Uint8Array(await blob.arrayBuffer()));}
const xlsx=await SpreadsheetFile.exportXlsx(wb);const outputName=incomplete?'B5与P2可用档案统计_全年未完成.xlsx':'B5与P2近一年回测.xlsx';await xlsx.save(path.join(out,outputName));
await fs.writeFile(path.join(out,'workbook-verification.json'),JSON.stringify({summary:keyCheck.ndjson,errors:errors.ndjson,independent_totals_and_streaks:true,closed_rows:rows.length},null,2));
console.log('Exported workbook:',path.join(out,outputName));
