# 复盘接口：固定一手、5分钟回放

发布版本：3.0.3；HTTP 协议仍为 `/api/v2`。客户端与服务端需同时升级，沿用已有版本头及设备认证。不改变 UI、旧接口返回结构、原始信号或生产交易规则。

## 使用顺序

1. 服务启动自动发现历史信号及未完成任务，后台生成复盘数据。`GET /review/metadata` 查询覆盖情况、当前 revision、评分范围、维度和近期任务。
2. `POST /review/query` 返回总览、四类统计和首屏摘要。后台尚未发布数据时，结果为空；metadata 的 `source_signals` 与 `published_signals` 可区分未回填和确实无信号。
3. 用 `snapshot_id` 翻页或请求额外分类。展开信号时使用摘要的 `event_id` 与 `result_version` 读取详情或逐K数据。
4. 查询快照过期或被内存上限淘汰时重新查询。`review.changed` 只提示后台进度和 revision 变化；由后续 UI 自行决定何时刷新。

本文接口均相对 `/api/v2`。普通设备可读复盘；重建及任务详情需要管理员。

| 方法与路径 | 请求 / 响应 |
|---|---|
| `GET /review/metadata` | `ReviewMetadata` |
| `POST /review/query` | `ReviewQuery` → `ReviewQueryResponse` |
| `GET /review/queries/{snapshot_id}/items?cursor=100&limit=100` | `ReviewItemsPage` |
| `POST /review/aggregate` | `ReviewAggregateRequest` → `ReviewAggregateResponse` |
| `GET /review/signals/{id}/detail?version={result_version}` | `ReviewResultDetail`，版本必填 |
| `GET /review/signals/{id}/holding?version={result_version}&cursor=0&limit=100` | `ReviewHoldingPage`，版本必填 |
| `POST /review/rebuild` | `{"symbols":["RB0","SA0"]}`；空对象表示全部；202 + `ReviewJob` |
| `GET /review/jobs/{id}` | 管理员读取 `ReviewJob` |

完整协议在 `crates/n-protocol/src/review.rs`，TypeScript 镜像在 `src/types/review.ts`。`src/services/api.ts` 提供 `reviewMetadata/Query/Items/Aggregate/Detail/Holding/Rebuild/Job`，对应同名 snake_case 桌面命令；`onReviewChanged` 订阅轻量变化通知。

## 筛选、样本与排序

```json
{
  "filters": {
    "symbols": ["RB0", "SA0"],
    "score_metric": "quality_v2",
    "score_min": 70,
    "score_max": 90,
    "score_status": "scored",
    "time_field": "entry",
    "time_start": "2026-10-01 00:00:00",
    "time_end": "2026-11-01 00:00:00",
    "sessions": [{"start":"09:00","end":"11:00"},{"start":"21:00","end":"02:30"}],
    "exit_reasons": ["stop", "no_follow"],
    "sample_scope": "deduplicated"
  },
  "sort_by": "score",
  "descending": true,
  "page_size": 100
}
```

- 可多选：`symbols/directions/levels/grades/states/exit_reasons/terminal_reasons`。同字段 OR，不同字段 AND。品种代码忽略大小写，其他枚举以 metadata 为准。
- 分数：`old` 为0–5；`quality_v1/morphology_v1/quality_v2/morphology_v2/a_score/b_score` 为0–100。默认 `quality_v2`。上下限包含边界，缺失值不当作0，也不替换成其他评分。`score_status` 为 `all/scored/unscored`，依据所选评分是否有值。
- 时间：上海本地字符串 `YYYY-MM-DD HH:mm:ss`。`warning/entry/exit` 默认 warning；没有对应时间的信号不回退到其他时间。日期和每日时段均左闭右开，多个每日时段取并集；跨午夜用 `start > end` 表示。起止相同视为空区间并拒绝。
- 排序：`score/time/gross_amount/net_amount/gross_r/net_r/mfe_r/mae_r/bars_held`，默认时间降序；空值始终靠后，同值按 event_id 降序。
- 默认每页100，允许1–500。cursor 为快照内偏移，不是事件ID。总数和统计针对全部匹配信号。
- `sample_scope=deduplicated` 默认只选族首；`all` 保留原始信号。完整信号集合先确定族首，再筛选，不能把被过滤的族首替换为较高分的后续成员。摘要包含 `family_id/family_size`。
- 去重沿用同品种、同版本、同方向、连续预警相隔不超过5根15分钟K、入场价相差不超过族首0.3R的规则，族首由预警时间和ID确定。缺15分钟K序号时沿用旧自然时间距离兜底，并标记 dedup_time_fallback。
- `raw_count` 为不应用去重时匹配筛选的数量，`deduplicated_count` 为匹配筛选的族首数量；`total/overview.count` 对应当前 sample_scope。

## 分类统计

默认返回评分段、品种、入场小时、退出原因四组。其他维度通过 aggregate 请求，最多两维：

```json
{
  "snapshot_id": "查询返回的ID",
  "dimensions": ["symbol", "hour"],
  "time_buckets": [{"label":"早盘","start":"09:00","end":"11:00"}]
}
```

自定义评分桶使用 `score_buckets:[{"label":"70–80","min":70,"max":80},{"label":"80–100","min":80,"max":100}]`。桶按序且不重叠，左闭右开，最后一桶包含最大值；桶外与缺失数据单列。自定义时段不允许重叠。小时、日期、星期和日夜盘按模拟入场时间分组，未入场单列。

除结构、评分、时间、状态维度外，支持量能、回撤、速度、追价、跳空、预警延迟等诊断。新复盘的触发量能按触发5分钟K / 前20根有效5分钟K均量计算；A强度来自预警时冻结质量特征，B段量比使用15分钟结构区间。原始旧分项单独保留。缺少必要数据的诊断返回空，不沿用原15分钟回放的成交特征。

`statistics.metrics` 每项均为 `{value,n}`，分母不足返回 null。金额和R分别提供累计、均值、平均盈利/亏损、胜率、盈亏比、盈利因子及最大回撤。盈亏平三类均在胜率分母内；不确定、未结算记录不计作亏损。没有亏损时 PF 为 null。

MFE/MAE、达到阈值比例、盈利回吐和等待/持有时长均返回实际覆盖样本数。盈亏曲线按退出时间、事件ID排序，首屏最多500个采样点；累计值包含全部已结算样本，最大回撤使用未采样的完整序列计算。该曲线是独立信号盈亏汇总，不代表具有资金约束的账户净值。

## 回放、生命周期与成本

复盘版本 `review-fixed-one-r-5m-v1`：原始入场分≥2.5、计划风险≥10 tick；保留突破、入场前失效、追价上限。真正固定一手，首次达到1R全部平仓，保留初始止损、无跟随和时间退出；等待12根、无跟随第5根、持有60根均按15分钟交易K计数，盘间休市不计数。

- 原始事件、原始入场分和原有结局不改写。详情提供原始事件与单独的历史重算评分；节点含 occurred_at、known_at、recorded_at 和 origin。
- `historical_rebuild` 表示历史重建；`live_observed` 表示新信号随后由后台按已收盘行情推进，仍为5分钟模拟，不是真实成交。任务的 `incremental` 表示增量处理，单条来源由首次入库记录确定。
- A/B点评取规则评分及现有过程证据；持有阶段无独立评分算法时 hold_score=null，只记录退出条件检查。
- 完整5分钟行情与覆盖所需日期的交易日历为必要输入。缺K、缺日历、换月和缺参数独立标记，不降级到15分钟或工作日日历。
- 同根K无法确定入场/止损/止盈先后时标为 ambiguous，不生成确定胜负。终结节点保留冲突K线和规则阈值。
- MFE≥0、MAE≤0；只统计持有期间。精度为 bar_interval 或 bounded。bounded 返回 lower_r/upper_r，无法精确确定的 r/amount/price 为 null；时间为5分钟区间，不能解释为逐笔成交时刻。
- 部分触发后的行情不完整时保留已知过程，未结算不填最终盈亏。

成本版本 `roundtrip-2.5tick-including-2tick-slip-v1`：开平仓各1 tick滑点计入成交价，再扣往返0.5 tick估算费用。毛金额由模拟成交价差×乘数计算；净金额只额外扣0.5 tick，不重复扣除2 tick滑点。1R金额使用模拟入场价到初始止损距离×乘数。乘数取明确匹配的内置品种表，未匹配不套用通用默认值；金额为空时仍单独标注可计算的点数/R覆盖。

## 后台、快照与错误

原始事件和5/15分钟行情的增删改通过SQLite触发器写入持久待处理队列，随原事务一起提交或回滚；换月及合约参数变化也入队。后台按品种处理，重建任务保存日历输入及检查点；分批计算不阻塞行情扫描。单次工作器异常保留旧发布结果，任务记录失败原因；相同输入不无限自动重试，管理员可重建，后续新数据也会重新入队。

结果、生命周期和逐K记录独立保存；全部目标品种完成后，事务更新当前摘要投影与 revision。任务未完成时旧版本仍可查询。规则实现、参数、行情、诊断和日历变化参与输入哈希；相同输入复用不可变结果，历史详情继续按版本访问。

摘要当前投影独立于历史版本，筛选/排序走当前列与索引，不扫描历史生命周期。JSON装配及聚合在阻塞工作池执行，最多4个查询/聚合并发。查询快照固定顺序和结果版本，按设备隔离；10分钟有效，最多64个，缓存预算64MiB，超限淘汰最旧快照。快照为进程内缓存，服务重启后需重查。统计结果随快照缓存；额外交叉统计按同一批摘要计算。

- 400 `INVALID_QUERY`：非法筛选、排序、评分范围或分桶。
- 401/403：缺设备认证/缺管理员权限。
- 404 `NOT_FOUND`：指定结果版本或任务不存在。
- 410 `SNAPSHOT_EXPIRED`：过期、重启后不存在、被淘汰或不属于当前设备的快照。
- 429 `QUEUE_FULL`：待处理重建任务达到32个。
- 426：沿用项目版本校验；缺版本头或客户端/服务端版本不一致。

## 旧接口迁移与删除清单

所有旧链路用 `LEGACY_REVIEW_API` 注释标记。当前保留原响应、原评分与原策略语义，不做运行时弃用告警，不设置自动删除日期。

| 旧HTTP接口 | 当前调用链 | 替换方式 |
|---|---|---|
| `GET /review/stats` | get_review_stats / getReviewStats / review store旧统计 | query.overview/default groups；额外维度走 aggregate |
| `GET /review/outcomes` | get_recent_outcomes、get_review_page / getRecentOutcomes、getReviewPage / workbench与图表导航 | query + queries/{id}/items；统一传递筛选与快照 |
| `GET /review/signal/:id` | get_review_signal / getReviewSignal / 图表与信号卡 | signals/{id}/detail，携带列表结果版本；图表行情仍走原图表接口 |
| `POST /review/outcomes/refresh` | refresh_outcomes_now / refreshOutcomesNow / 旧刷新按钮 | 重建复盘用 rebuild + jobs；原事件推进如仍有业务调用须先独立迁移 |

删除前必须：全部 UI、桌面命令和其他调用方完成迁移；新接口经过实际使用验证；新旧口径差异已核对；无旧调用引用。观察期在后续迁移工作中执行，本次不删除旧接口。

后续一次性清理旧路由、signals.rs中旧处理函数、core service旧适配入口、桌面命令注册、remote_api旧封装、前端旧封装，以及确认仅服务旧接口的DTO和测试。共享评分/去重算法、图表行情、批注和用户开仓决策保留。不要因删除旧刷新接口而误删仍被扫描流程使用的事件推进。

回归位置：`crates/n-core/src/review/tests.rs`、`crates/n-headless/src/api/review.rs`、`tests/reviewApi.test.cjs`、`tests/reviewBrowser.test.cjs` 及旧 `service/review.rs` 测试。

## 验证命令

```text
cargo test -p n-core --lib review:: --offline
cargo test -p n-headless --offline api::review::tests
cargo test -p n-core --lib benchmark_10000_precomputed_signals --release --offline -- --ignored --nocapture
cargo check -p n-headless -p n-trend-analyzer --offline
node --test tests/reviewApi.test.cjs tests/reviewBrowser.test.cjs tests/signalV3Review.test.cjs tests/signalQuality.test.cjs tests/signalQualification.test.cjs
npm run build
```

性能验收使用1万条构造摘要及对应持有记录，预计算后目标查询P95≤500ms、默认首屏gzip≤200KiB。当前工作区数据库没有原始信号事件；构造数据结果不代表生产库回填耗时，实际历史覆盖需在部署后通过metadata/jobs观察。
