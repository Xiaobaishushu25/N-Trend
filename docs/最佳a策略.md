# 最佳a策略

用户指定名称；固定版本best-a-v1，对应near_start/adaptive。状态：研究观察版，未改实盘，尚未实现全周期稳定。后续探索另存版本，不覆盖本策略。

核心是优先选择最近满足原有结构资格的A，保留原反转警报与strong_kind_session执行。没有加入上一轮失败的五种“失效后禁止旧A”规则。完整规则见definition.json，可执行参数见protocol.json；源码、预警池、逐笔已接纳交易、四场景结果和二进制均已快照，manifest.json锁定文件哈希。

|执行压力|上半年净额|平均净R|近期净额|平均净R|
|---|---:|---:|---:|---:|
|corrected_touch/slip1|-6863.29|-0.0282|21075.34|0.2739|
|corrected_touch/slip2|-14248.18|-0.0963|17606.16|0.2094|
|next_minute_open/slip1|-16663.54|-0.1123|16975.40|0.2357|
|next_minute_open/slip2|-20192.74|-0.1653|13349.60|0.1341|

以上为每手已平仓净额、扣实际手续费，未知收益另列。1tick上半年444笔、未知0笔，近期178笔、未知1笔。近期不同品种起点不齐，非完整等覆盖四个月。所有历史均已经反复研究，不能当成独立泛化验证。

策略快照：research-strategies/best-a-v1/。重放使用该目录bin/swing_wave_execution.exe，参数为protocol.json、early或recent、replay、新输出目录；原行情按协议路径和哈希引用，没有重复复制大型原始行情。
