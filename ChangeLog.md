# 更新日志

## 2026-08-26

### 单K锤/针独立通道重塑 + 无头版 + ATR阈值放宽

#### 后端 `n-core`
- `crates/n-core/src/analyze/indicators.rs:389-401` 单K检测阈值 `0.7*ATR → 0.5*ATR` 放宽（Hammer/Needle 同步），`cargo check -p n-core` 通过；其余形态闸 `body≥0.25*range / 1.5*body / 0.40*range / 0.05*range` 保持不变，修复 CF0 22:45 等上影不足被误判为锤的边界
- `crates/n-core/src/notify/email.rs` 新增 `single_bar_email_payload()`，标题 `N趋势锤/针·15m [SYMBOL] bar_ts`，正文透传 `trigger/expire/price/high/low` 与阈值说明；`event_email_payload` 追加 `[MAIL_SUBJECT]` 追踪日志；BOM 头清理
- `crates/n-core/src/service/mod.rs:2080` 标签 `锤·15m/针·15m → 下影锤/上影锤`（注释长下影实体在上/长上影实体在下），BOM 头清理

#### 无头版 `n-headless`（新增 crate）
- 新增 `crates/n-headless/Cargo.toml + src/main.rs`，`anyhow/chrono/tokio/tracing` 独立启动，支持 `--data-dir` / `NTREND_DATA_DIR`，复用 `n-core::Services` 与 `storage::connect`，带 `LocalTime` 日志、`sqlx=warn` 降噪与 `email.toml` 自动导入；`Cargo.toml` workspace `members += crates/n-headless`，`Cargo.lock` 新增 `n-headless` 依赖树（`parking_lot/signal-hook-registry` 等）

#### 桌面壳与扫描
- `src-tauri/src/lib.rs:475-485` `tick_scan` 在 `res.single_bars` 循环中独立调用 `single_bar_email_payload` + `send_summary`，失败 `tracing::error!`，`event_email` 追加 `[SEND_MAIL]` 日志

#### 前端
- `src/utils/singleBar.ts` 新增 `singleBarLabel(kind)` 统一标签 `下影锤/上影锤`，`normalizeSingleBar()`/`singleBarBadgeStyle()`/`SINGLE_BAR_COLORS` 注释补齐，`label` 不再硬编码 `锤·15m/针·15m`
- `src/utils/notify.ts` `NotifyOptions/NotifyItem` 新增 `singleBar{ symbol,name,label,kind,time,price }` 透传，`push()` 接收 `single_bar`，新增 `notify.singleBar(duration:4000)`，修复注释转义 `/**`
- `src/components/AppNotificationHost.vue` `openSignalChart` 支持 `singleBar`、通知卡片新增 `is-clickable` 与 `v-else-if="item.singleBar"` 分支 `.is-hammer/.is-needle` 样式，模板 `as` 断言修正为 `item.singleBar`
- `src/stores/scans.ts` `applyScanResult` 前置 `notifySettings/symbolsStore/nameOf`，单K通知由 `notify.success → notify.singleBar`，`injectMockHammer()` 标签与通知同步更新
- `src/views/DashboardView.vue` 单K列 `width 56→84` 防拥挤，`render` 改 `class:cell-singlebar + white-space:nowrap;line-height:16px;display:inline-block;padding:1px 7px`
- `src/views/ChartView.vue:1490` 左侧徽标 `锤/针·15m → getSingleBar(...).label`，`padding 0 5px→0 6px + inline-flex align:center`
- `src/components/KLineChart.vue:1284` 图表内标注 `锤/针 → 下影锤/上影锤`

#### 校验
- `cargo check -p n-core` 通过；DB 实测 `CF0 22:30`（阳线上影）与 `22:45`（阴线上影10<22.5且<0.5ATR）正确拦截，`8-21 22:15`（阴线上影15）在 `0.5*ATR=18.39` 仍差 3.39 需二档放宽
- 前端 `src` 已无 `锤·15m/针·15m` 硬编码残留



## 2026-08-25

### K线悬停聚焦修复 — 实时行情不再抢夺历史K线焦点

- `KLineChart.vue` 修复实时行情 `renderData()` 在鼠标悬停历史K线时自动 `syncFocus()`/`ensureFocusVisible()` 把十字线和图例抢回最新K线的问题
- 新增 `isHovering`/`hoveredTime` 状态，由 `subscribeCrosshairMove` 维护：`!param.time||!param.point` 及 `!seriesData` 时清除悬停，否则记录 `hoveredTime=param.time`
- `renderData` 头部计算 `hoveringOnHistory = isHovering && hoveredTime != lastTs` 与 `shouldAutoFollow = focusFollowsLatest && !hoveringOnHistory`，仅非历史悬停时才自动跟随最新K线
- `renderData` 尾部：历史悬停时查找 `hoveredRow` 并 `innerHTML=formatLegend(...)` + `setCrosshairPosition(close, hoveredTime)` 原地还原，否则走原 `syncFocus()`；`ensureFocusVisible()` 也改为 `shouldAutoFollow` 守卫
- `onMounted` 初始化 `isHovering=false, hoveredTime=null`，保留 `restoreView` 视口不变；验证 `vue-tsc --noEmit` 与 `vite build` 通过（4405 modules）

### 日志体验全面优化 — 降噪 + 人类可读关键节点

- `src-tauri/lib.rs` 日志过滤重写：`log_filter()` 默认 `sqlx=warn,sea-orm=warn,sea_orm=warn,hyper=warn,reqwest=warn,rustls=warn,h2=warn,tungstenite=warn,tao=warn,wry=warn`，彻底屏蔽 `sqlx::query: SELECT "klines" ... rows_affected/rows_returned/elapsed` 一天几万条刷屏；保留 `RUST_LOG` 覆盖，开发可 `RUST_LOG=debug,sqlx=info` 调试
- `init_logging()` 统一本地时间 `LocalTime %Y-%m-%d %H:%M:%S%.3f`、`with_target(false)`、`with_ansi(false)`、`rolling::daily`，并清理 >14 天 `ntrend.log*`
- 启动链路分段打点并修复丢失：`peek_log_level(config.json) -> init_logging` 提前，新增 `🚀 ntrend vX 启动 | 数据目录 | 日志级别`、`✓ 存储连接就绪 耗时`、`✓ 配置加载完成 耗时 | 刷新/扫描间隔 交易时段 日志级别`、`✓ 服务初始化完成 | 已收录 N 个`、`✓ 调度状态已恢复 | 自启 上次刷新/扫描`、`⏰ 定时调度与实时行情轮询已启动`、`✅ 主窗口就绪 总耗时` 分隔线；原 `storage::connect` 在日志初始化前导致丢失的问题已修复，`last_refresh/last_scan` 读取时序 bug 已修正
- 定时任务人类可读：`tick_refresh`/`tick_scan` 统一 `⏳ 触发 | HH:MM:SS` → `✅ 完成 耗时 | 成功/失败/总计` + `⚠ 失败警告` / `❌ 失败 | {error}`；`spawn_quote_poller` 成功静默（避免 15s 刷屏），仅失败 `warn`
- `src-tauri/commands.rs` 全部手动操作补齐 `👆` 追踪 + 耗时 + 结果：`refresh_data_now`/`run_scan_now`/`rebuild_events_now`/`refresh_outcomes_now`/`add_symbol`/`remove_symbol`/`set_symbol_flags`/`set_symbol_tick`/`enrich_symbol_names`/`refresh_symbol_list`/`update_config`/`reset_config`/`set_timeframes`/`open_log_directory`/`set_scheduler_running`，均 `Instant::now()` 统计，成功 `✅` 失败 `❌` 详细错误透传
- 清理临时模板 `new_*.txt` `patch_*.py` `tick_*.txt` 等 22 个，`cargo check` 全量通过，`ntrend.log` 体积显著下降

## 2026-08-24

### 60m 五档趋势标签（仅展示不计分） — 3f5fc79

- `indicators.rs` 新增 `analyze_60m`：基于 MA20 斜率与价格偏离度输出五档 `强多/弱多/震荡/弱空/强空`，当前仅展示不计入分数
- `dto.rs / model.rs / scoring.rs / report.rs / config/mod.rs` 打通 `trend_state/trend_bonus/direction` 字段与前端类型
- `KLineChart.vue / ChartView.vue / SettingsView.vue / settings.ts / types.ts` 预留趋势展示与配置入口

### 趋势标签落库与展示补齐 — 625d981

- `event.rs::candidate_for` 在落库前调用 `indicators::analyze_60m` 填充 `trend_state`（原为空字符串），历史信号通过 `entry_score_dims` 解析补显
- `service/mod.rs` 与 `ChartView.vue` 同步落库与回显链路，避免新建信号无趋势而历史信号无法解析的问题

### A段三项优化 — 5cf04b8

- `scoring.rs: A_LEG_BAR_PLAIN_SAME 0.6 → 0.4`：同色平淡K线加分收紧，抑制 0 Clean 假高分
- `scoring.rs` 新增 `ENDPOINT_EXTRA_REVERSE_WICK_PENALTY 0.5`：A段终点若为 `ReverseWick`（长上影阴线等反向影线）在 S1 位置额外 -0.5，命中率约 11.9%
- `scoring.rs: A_LEG_GAP_MIN_ATR 1.0 → 0.8`：跳空阈值收紧，新增检出约 3.2%
- 单测同步：`plain_same` 期望 0.6→0.4、`gap` 用例 0.38→0.46、跳空计数 8→7、`score_a` 2.98→2.94；全量单测 165 passed
- 回测（1068 有效信号，过滤 11 条重算异常）：`≥3.5` 331→277（-54）、`≥3.2` 630→574（-56）、均分 -0.052，胜率 47.4%→47.8% 扁平，完成去假高分目标

### 结算/窗口/聚焦与样式可靠性修复

- `service/mod.rs` 延迟补拉：`refresh_symbol_data` 末尾若落在 5m 收盘后 45 秒内，则在收盘后约 35 秒（`MINUTE_BAR_SETTLE_SECS+5`）后台补拉 5m 并重算 15m/60m，修复 scheduler 在整点立即刷新导致 15m（如 11:30 L8265/L8260）偏差 5 分钟的问题；不改 `get_klines` 热路径，避免切分卡死
- `src-tauri/lib.rs` 窗口状态落盘时机修正：由 `WindowEvent::Destroyed` 改为 `CloseRequested` 分支内 `save_window_state`（此时窗口仍在可取几何），`open_settings_window` 移除 `.center()` 避免与记忆位置冲突
- `KLineChart.vue` 复盘聚焦可靠性：新增 `focusKey` 强制同时间戳重复聚焦、`focusRetryTimer` 最多 50 次×60ms 重试、`nextTick` 二次校正、`centerFocusView` 右侧预留 `gap+2` 根、`rowsMatchRequest` 按字母前缀判同品种（换月 MA001/MA 视为同一品种），补全 `rightMaxTo` 越界修正与详细 `[focus]` 日志
- `ChartView.vue` 复盘链路：`reviewFocusKey` 透传至 `KLineChart`、`focusTs/focusKey` 双 watch、预警明细新增 60m 趋势项（`parseTrendDims`）、`pc-trend` 仅有值时渲染、Line 1526 透传 `:focus-key`
- `SettingsView.vue` 文案：复盘聚焦右侧说明由“贴到最右侧”改为“右侧倒数第3根（留2根空白）”
- `openNotificationsWindow.ts / openReviewWindow.ts / openSettingsWindow.ts` 去除 `center:true`（与窗口记忆一致）、统一双引号与注释精简
- `event.rs` 趋势落库同 625d981 一并纳入本次提交范围
- `SignalNotes.vue` 批注组件纳入版本（此前未跟踪）：支持按 `eventId` 增删批注与“已按建议开仓”判定，复用 `getSignalUserData` 聚合接口


## 2026-08-22

### B级结构与累计覆盖保守优化

- `model.rs: Grade::B score_base 3.8 → 4.3`（A 5.0 / C 2.5 不变），缩小 A/B 倒挂，回测 B 胜率 52% > A 47.7% 的倒挂回归
- `pattern.rs: b_too_long` 分级阈值 `A>8 / B>12 / C>14` 才扣 0.5（原统一 >8），B 平均 10.9 根不再被误伤；`best_pattern_for_b_end` 按 `score_a*0.6 + score_b*0.2` 择优，持仓不等时优先非 hard_failure
- `scoring.rs: score_b` 中 `b_weakening +0.3` 仅 `!b_too_long` 时生效，避免过长已扣分再叠加
- `scoring.rs: warning_base` 新增 `cumulative => 3.0`（原 2.0，`strong/wick` 3.5 保持），`CUMULATIVE_ENTRY_SCORE_MAX 3.49 → 3.9`，`wick` 维持 3.0 不进 3.5+ 标准仓；`dim_warning` 与 `entry_score` 注释同步
- 单测同步：`b_leg_weakening` 期望 `3.8/4.1/3.3 → 4.3/4.6/3.8`，`entry_score_uses_60_20_20_and_caps_cumulative` 入参 `4,4,2 → 5,5,3` 触顶验证
- 回测仿真（884 单库）：`>=3.5` 144→173 (+29)，胜率 48.3%→50.7%，`cumulative>=3.5` 0→29 笔且胜率 65%，B 在 3.5+ 由 26→45 笔保持优势

### 强反转十字星与反向影线（2026-08-19 方案落地）

- 允许吞没前一根十字星（`prev.close <= prev.open` / `prev.close >= prev.open`），实体需覆盖十字星参考价且严格大于其振幅 60%
- 反向影线门槛 `STRONG_REVERSE_SHADOW_MAX_RATIO` 50% → 30% 概念收紧（代码层已将等值 30% 视为可识别，PB0 09:30 反向影线正好 30% 判 `strong`），超过 30% 不再识别为 `strong`
- `EVENT_LOGIC_VERSION 4 → 5`，`SIM_VERSION 12 → 13`，`is_wick_warning_bar` 7 门槛与 `STRONG_ENGULF_BODY_ATR_MIN 0.25` 不变
- 新增 `doji_engulf_warns_at_pb0_0930_like_bar` 与 BU0 1381 对照，SA0 1154 仍因未吞没不识别

### K线结算等待与调度

- `fetch/kline.rs` 新增 `MINUTE_BAR_SETTLE_SECS = 30` 与 `settled_kline_rows`，`fetch_minute` 按本地时间过滤未站稳的分钟 K 线，避免接口早推的未定型 K 线计入形态
- `scheduler/mod.rs` 与 `service/mod.rs` 同步 30 秒 settle grace，`pattern_window_has_rollover_until` 与实时扫描对齐

### 信号主观备注与判定

- `storage/entities.rs` / `mod.rs` / `repo.rs` 新增 `signal_annotations` 与 `signal_decisions` 表及 `SignalUserData` 聚合，支持按 `event_id` 增删查备注与主观判定
- `src-tauri/commands.rs` 新增 `get_signal_user_data / add_signal_annotation / delete_signal_annotation / set_signal_decision` 并在 `lib.rs` 注册
- `ChartView.vue` 右侧卡片接入备注列表与判定切换，`ReviewView.vue` / `DashboardView.vue` 透传展示

### 评分展示与配置

- `config/mod.rs` 新增 `UiConfig.score_pill_full_score` 默认 3.5（每 0.2 一档浅一阶），`Default` 与用例同步
- `ChartView.vue` / `ReviewView.vue` 评分药丸按可配置满分着色，`SettingsView.vue` 暴露配置项
- `outcome.rs` `SIM_VERSION 12 → 13`，`DEDUP_WARNING_BARS` 5 根、`0.3R` 去重口径保持

### 其他

- `service/mod.rs` 重构扫描与去重流程，`storage/repo.rs` 补充 pending / dedup 索引，`types.ts` / `api.ts` / `stores/settings.ts` 同步前端类型与接口
- 全量单测 `165 passed`（含新增十字星用例），历史落盘不回填



## 2026-08-19

### 强反转吞没十字星与反向影线门槛收紧

- 允许吞没前一根十字星（开盘=收盘）：当前反向K线实体必须覆盖十字星参考价，且实体严格大于十字星振幅的 60%，防止小K线贴住十字星参考点冒充强反转
- 反向影线门槛由「严格小于 50% 振幅」收紧为「不超过 30% 振幅（含 30%）」，超过 30% 不再识别为 `strong`
- 普通实体吞没逻辑不变：仍要求反向收盘、实体覆盖前一根实体、至少一侧严格超过、实体至少 `0.25 × ATR20`、仅反转段第一根检查
- PB0 2026-08-19 09:30（反向影线正好 30%、实体为十字星振幅 100%）与 JD0 10:45 均识别为 `strong`；SA0 1154 仍因前一根是阳线实体且未吞没不识别
- `EVENT_LOGIC_VERSION` 4 → 5，`SIM_VERSION` 12 → 13；新口径只作用于重建或新扫描信号，历史落盘不回填
- 新增文档 `docs/2026-08-19 强反转吞没十字星与30%反向影线.md`

## 2026-08-16

### 删除快速路径预警

- `fast` 预警从生成逻辑中彻底删除：A 级浅回调场景的首根反向普通小K线不再单独预警，预警只来自 `strong` / `wick` / `cumulative`
- 删除 `fast_path_close_ok` 评分路径，`warning_base` 与 `warning_kind_at` 不再返回 `fast`
- 旧 `fast` 落盘记录在下一次扫描或复盘刷新时按 `warning_kind = "fast"` 物理清理，重建复盘数据时同样不再出现
- 数据依据：`fast` 405 条、已结算 284 条，胜率 44.0%、平均 R `-0.055`、盈亏因子 0.93，低于约 50% 的不做单基线且无正期望，因此删除；`cumulative` 胜率 48.6%、平均 R `+0.095`、盈亏因子 1.18，予以保留
- 新增文档 `docs/2026-08-16 删除快速路径.md`，记录数据依据、行为变更与历史数据处理口径

### 相似预警去重口径简化

- 复盘统计层改为按“同品种 + 同方向 + 预警K线相差不超过 5 根 15m + 入场价差不超过 0.3R”合并为同一族，族内保留首见记录；不再依赖 A/B 段、级别、评级、`warning_kind`、`s1_ts`
- 实时扫描层改为持仓优先：同品种同方向、5 根 15m 内已存在事件且仍处于未触发或已触发持仓时，直接抑制新预警，不再比较入场价差；前一条已离场后才退回 0.3R 入场价差判断
- 扫描与重建会按同一口径物理清理历史重复事件：族内最近一条在后续预警时仍持仓/未触发即并入同一族，最近一条已离场后退回与族首的 0.3R 入场价差判断，族内只保留首见一条，其余直接删除，K线图右侧形态列表与复盘明细都不会再出现被去重信号
- `SIM_VERSION` 从 10 升到 11，复盘页会按新口径自动重算

### 强反转合并与反向影线门槛

- 吞没与强趋势K合并为 `strong`（显示“强反转”），新信号不再产生 `engulf`；历史 `engulf` 记录按强反转口径兼容显示与计分
- 合并后的强反转要求反向影线严格小于 50% 振幅；等于或超过 50% 不再识别，BU0 1381 的 22:30 吞没信号被剔除
- 长影线 `wick` 保持独立，继续沿用反向影线 `<= 10%` 的七条硬门槛；25%-50% 区间暂不额外分段扣分
- 新增文档 `docs/2026-08-16 强反转合并与反向影线门槛.md`，记录数据依据、变更口径与 BU0 1381 对照

### 强反转收窄为干净吞没

- `strong` 只保留干净吞没：必须吞没前一根K线实体，且反向影线严格小于 50% 振幅、实体至少 `0.25 × ATR20`
- 强趋势K不再单独作为 `strong` 预警；没有吞没的强趋势K继续用于 A/B 腿强K评分、B 段强锚与门控、形态识别统计
- 对照 SA0 1154 14:00（做空，`O971 H971 L966 C967`，ATR20=5）：收盘 967 高于前一根开盘 964，未吞没前一根实体，不再产生 `strong` 预警，只作为 B 段锚点
- A 级普通反向K线也没有快速路径兜底，只能等干净吞没/长影线；B/C 级或强锚场景仍可用多K累计覆盖
- `EVENT_LOGIC_VERSION` 3 → 4，`SIM_VERSION` 11 → 12；新口径只作用于重建或新扫描信号，历史落盘不回填
- 口径与 SA0 1154 对照记录在 `docs/2026-08-16 强反转合并与反向影线门槛.md`

### 入场评分权重调整

- 入场综合评分权重由 `0.50 × A腿 + 0.30 × B腿 + 0.20 × 预警K线` 调整为 `0.60 × A腿 + 0.20 × B腿 + 0.20 × 预警K线`
- 修正 A 腿很弱但 B 腿拿满分仍被抬高分数的现象，C0 1251 从约 `2.60` 降到约 `2.22`
- 旧落盘记录不回填：历史 `score`、`dims` 与统计结果保持不变，新扫描或新评估的信号才使用新权重
- 新增文档 `docs/2026-08-16 入场评分权重调整.md`，记录分项依据、全库分布影响与兼容口径

### A段推进速度评分

- A 段质量分由“幅度 × 强K密度”两项乘法改为“幅度 × 强K密度 × 推进速度”三项乘法，速度真正成为短板，不再通过几项中等加分凑出高分
- 推进速度定义为 `(a_move / a_bars) / ATR`，即每根 A 段 K 线平均推进的 ATR 数；低于 `0.15` 倍 ATR/根时推动分清零，达到 `0.50` 倍 ATR/根及以上按满动能计
- `0.15` 清零线对齐交易逻辑 2.0 的 A 段最低速度门槛，慢腿无法在评分阶段再拿动能分
- 超大 A 段、跳空、长度扣分继续作为独立减项；长度扣分常量统一改为正数，公式统一用减号
- C0 1261 复算：A 段分从约 `1.6644` 降到约 `1.0053`，按旧总分口径总分从约 `2.6822` 降到约 `2.3527`
- 新增文档 `docs/2026-08-16 A段推进速度评分.md`，记录公式、阈值、C0 1261 对照与全库分布影响

## 1.1.2 - 2026-08-15

### 预警K线影线门槛收紧

- 长影线预警改为七条硬门槛，同时满足才识别为 `wick`：实体 `> 0`；主影线 `>= 3 倍实体`；主影线 `>= 60% 振幅`；主影线 `>= 0.5 倍 ATR20`；收盘位置 `<= 25% 振幅`；反向影线 `<= 10% 振幅`；主影线 `>= 50% 前一根 b 向K线振幅`
- 反向影线不再扣分：超过 10% 直接不识别，不再出现“先识别为 wick、再因反向影线偏长降分”的路径
- 增加前一根 b 向K线量级门槛：主影线不足前一根振幅一半时不识别，PB0 178 这类小影线信号不再误发
- N字预警与箱体触轨共用同一套影线判定，避免两处口径漂移
- 历史落盘记录不回填，新扫描或新评估的信号使用新门槛

### 文档

- 新增 `docs/2026-08-15 预警K线影线门槛收紧.md`，记录七条硬门槛、方向口径、与旧口径的差异及 SA0、PB0 复盘对照

### 版本

- 桌面端版本号由 `1.1.1` 提升到 `1.1.2`
- 前端包版本号由 `0.2.1` 提升到 `0.2.2`

## 1.1.1 - 2026-08-14

### 状态胶囊分档

- 左侧品种列表与主界面表格统一按评分分档显示状态胶囊：3.5 分及以上为最大档，按 0.2 分一档逐级缩小，低于最低档的保持最小样式
- 列表与表格使用同一套字号、间距、圆点与配色，避免两处视觉不一致

### 信号来源

- 左侧品种列表改为读取数据库中最新的扫描信号，与表格页同源同规则

### 通知

- 新形态通知评分阈值同时对邮件生效：本次扫描没有任何评分达到阈值的形态时，不再发送扫描摘要邮件
- 邮件正文现在按“预警信号 / 已触发信号”分组列出关注信号明细，并标注是否达到通知评分阈值；低于阈值的信号不会触发邮件发送

### 版本

- 桌面端版本号由 `1.1.0` 提升到 `1.1.1`
- 前端包版本号由 `0.2.0` 提升到 `0.2.1`

## 1.1.0 - 2026-08-14

### 评分逻辑调整

- 预警K线质量改为真实综合评分项：强趋势K、吞没、长影线统一在最终评分上增加 `+0.3`，快速路径、多K累计覆盖、无预警不计分，三类强预警相比其余路径约多 `0.3` 分
- 移除原先独立的 `+0.4 / +0.2` 显示加成标签，复盘明细与形态卡片中的“评分”列即为含预警质量分的最终分数，不再存在两套口径
- 旧落盘记录不回填：历史 `score`、`dims` 与统计结果保持不变，新扫描或新评估的信号才使用新评分逻辑
- 箱体触轨信号同样计入 `+0.3` 预警质量分，N字与箱体共用同一套口径
- 原有的弱确认扣分、长影线反向影线扣分、触发受阻扣分全部保留，新增质量分与这些间接约束叠加生效

### 文档

- 新增讨论记录 `docs/2026-08-14 预警K线质量评分讨论.md`，说明强趋势K、吞没、长影线三类强预警与快速路径、多K累计覆盖、无预警等普通路径的区别

### 版本

- 桌面端版本由 `1.0.7` 提升到 `1.1.0`，前端包版本由 `0.1.7` 提升到 `0.2.0`

## 1.0.7 - 2026-08-12 22:35

### 新增

- K线图新增当前K线收盘倒计时：显示在最右实时K线正下方、时间轴上方，5m/15m/30m/60m 等分钟周期可用，仅交易时段显示
- K线图左上角新增当前K线时间、开高低收信息条

### 优化

- 信号按同一结构去重：同一品种、方向、级别且 S1/S2 时间戳相同的结构只保留一条记录，保留首次识别时间并更新最新扫描状态与评分
- 长影线预警新增反向影线质量扣分：反向影线占振幅 10%-20% 扣 0.3，超过 20% 扣 0.5
- K线图事件标签统一避让：S0/S1/S2、预警、触发、量能、出场标签分层排列，不再遮挡K线与右侧价格
- 形态标记缩小一半：S1 粉色圆、S2 粉色方、预警橘色圆均使用更小尺寸

## 1.0.6 - 2026-08-12 01:08

### 优化

- 复盘止盈改为推动止盈：触及 1R 后每达到 0.8×2R、0.8×3R、0.8×4R…… 即上移止盈位，随后回落至当前档位即止盈
- 全量 412 条去重信号复算：平均 R 由 0.0713 提升至 0.2251，SIM_VERSION 由 8 升到 9，复盘页刷新后旧结果自动重算

## 1.0.5 - 2026-08-11 22:08

### 修复

- 修复入场价通知误报：只有尚未触发的形态在实时价格打到入场线时才通知；已触发过的旧信号不再因后续行情仍满足方向条件而重复弹出

## 1.0.4 - 2026-08-11 21:01

### 新增

- 设置界面新增独立“界面”tab，通知页不再混杂界面项
- 新增“点击进入K线图时展示K线数量”配置，可调整进入图表时的默认可见K线根数（默认 140）
- 新增“K线图默认向左移动距离（根）”配置，可调整进入图表时右侧预留空白（默认 10）

## 1.0.3 - 2026-08-11 20:33

### 新增

- 复盘明细跳转K线图新增复盘模式：从复盘窗口点击明细行后进入，右侧展示筛选上下文下的历史信号明细（含ID、形态、评分、进出场、结局、R/MFE/MAE、量能、增仓、换月/缺口等）
- 复盘模式下鼠标滚轮在筛选列表内循环切换信号，点击右侧明细行也可切换；切换时自动重绘对应K线、N形态、入场/止损/目标/出场
- 复盘模式下按 ESC 或点击“退出复盘”返回普通图表模式，期间禁用左侧品种切换并固定 15m 周期

## 1.0.2 - 2026-08-11

### 新增

- 通知设置新增“新形态通知评分阈值”，低于阈值的即将触发形态仅记入历史通知、不再弹卡片，阈值持久化到配置文件

## 1.0.1 - 2026-08-11

### 新增

- 标题栏新增一键清空通知按钮，可一次关闭当前所有通知
- 标题栏新增历史通知入口，点击打开独立的历史通知窗口
- 历史通知窗口按最新到最旧展示最近 40 条通知，并显示通知时间

### 其他

- 新增 ChangeLog.md，后续每次更新同步记录


