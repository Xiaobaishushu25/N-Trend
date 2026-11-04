# S0 初始审计（修复前，2026-11-04）

基线 SHA：c547ce46dc6e1620af7c8acac7367c8bc8230e8b。范围仅 S0/T01–T04，不读取任何策略收益结果。

|发现|定位|最小处置|
|---|---|---|
|组合默认同时风险 1800、保证金比例 80%|scripts/fresh_year_research.py: portfolio|旧结果不通过新协议；建立独立严格共享账户验收入口，避免改变已冻结实验|
|BU tick=1 与 precision.rs 的 BU=2 不一致|fresh_year_research.py / research_swing_year_20261104.py 的 SPECS|导出冲突，不将任何一张静态表认证为历史规格|
|合约真实价格已有；映射 snapshot 无历史发布时刻|market_history_store.py / mappings|映射仅可诊断，不能认定已证实 point-in-time 主力选择|
|手续费 snapshot 不是逐月费率，保证金统一代理|scripts/exchange_fees_20261022.json / fresh_year_research.py|缺失时拒绝有效证据；等待有来源、生效时间的真实合约元数据|
|自然日边界及下一根 bar 判断末尾依赖未来时间戳|fresh_year_research.py: prepare / last|S0 使用预先已知时段关闭时间，不推测未来缺 bar 来决定平仓|
|Rust 日历覆盖外降级工作日|crates/n-core/src/session/trading_calendar.rs|审计复用 factor_study_data.Calendar，覆盖外 fail closed|
|各回放入口规则不同，无统一通过证明|v3/replay.rs / factor_study_execution.py / 多个历史研究脚本|新建小型 5m 验收执行内核；历史引擎保持隔离，不能自动获得 S0 认证|
|方案称 20 品种，而本地归档配置为 22|market_history_store.py PRODUCTS|完整审计 22 品种，禁止按收益删两个；固定池确认前不通过 S0|

修复前失败清单：新入口缺失导致 no-lookahead 回归无法导入；旧组合 900 风险/60% 保证金契约不满足；历史时变元数据、映射发布时刻、36 个完整月覆盖待实际审计确认。

执行顺序：保存此审计 → 保存并运行回归取得失败日志 → 实现严格内核与只读数据审计 → 重跑测试并生成报告。人工账本采用 12 笔可手算合成交易；不冒充真实历史账单对账。
