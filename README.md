# ntrend — N趋势分析桌面客户端

基于 Tauri 2.0 的期货 N 形趋势分析客户端。数据以 **5m K线为唯一原始数据**持久化到 SQLite，
15m/30m/60m/120m/240m/日线全部由 5m 聚合派生，定时增量抓取，最大限度降低新浪接口请求量、规避封 IP 风险。

## 技术栈

- 桌面壳：Tauri 2.0（Rust），托盘驻留，关闭窗口最小化到托盘
- 前端：Vue 3 + TypeScript + Vite + Pinia + Naive UI + Lightweight Charts
- 后端：Rust workspace —— `crates/n-core`（核心库）+ `src-tauri`（应用壳）
- 存储：SQLite（SeaORM 管理，库文件位于系统应用数据目录 `ntrend.db`）
- 日志：tracing + tracing-appender（滚动日志文件）

## 目录结构

```text
ntrend/
├── crates/n-core/          # 核心库（可独立测试）
│   └── src/
│       ├── fetch/          # 新浪行情客户端 + 节流器 + JSONP 解析
│       ├── storage/        # SeaORM 实体 / 建表 / 仓储
│       ├── derive/         # 5m → 高级别聚合（含夜盘交易日规则）
│       ├── analyze/        # N 形分析算法（由原命令行项目迁移）
│       ├── scheduler/      # 定时刷新/扫描状态机 + 交易时段过滤
│       ├── notify/         # SMTP 邮件通知
│       └── service/        # 业务流程编排 + 设置持久化
├── src-tauri/              # Tauri 应用壳（命令/托盘/调度循环/事件）
└── src/                    # Vue 前端
```

## 运行

```bash
npm install                 # 安装前端依赖
npm run dev                 # 启动 Vite 开发服务器（端口 5173）
cargo tauri dev             # 以开发模式运行桌面应用
```

构建安装包：

```bash
npm run build
cargo tauri build
```

## 数据策略

- 只持久化 5m 原始K线（source=raw），增量更新：每轮每品种抓最近 ~10 根做 upsert。
- 15m/60m 为策略热路径，派生结果落库缓存（source=derived）；30m/120m/240m/日线按需即时聚合。
- 日线按交易日聚合，夜盘（20:00 后）计入次日。
- 定时器默认 5 分钟刷新数据、15 分钟边界（:00/:15/:30/:45）跑分析，仅交易时段执行（可关闭）。
- 请求节流：默认 400ms 间隔 + 每分钟 60 次上限 + 失败指数退避重试。

## 测试

```bash
cargo test -p n-core        # 聚合/仓储/调度/分析回归等 40 个单元测试
npm run build               # 前端类型检查 + 打包
```

## 说明

- 关闭主窗口会最小化到托盘，从托盘菜单可退出。
- 首次启动自动从 `symbols.txt`（或内置代码表）建档；`email.toml` 会在首次运行时导入为邮件设置。
- 分析结果与历史信号保存在本地 SQLite，打开K线图不产生任何网络请求。
- 本项目是分析辅助工具，不构成投资建议。
