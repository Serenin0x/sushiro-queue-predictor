# 已有号码实时预测

2026-10-17 E0538，更新E0525实现。此文描述实际算法和供应商输入；展示到号是代理终点，尚未校准成实际入座等待误差。

## 先判断证据，再估计剩余时间

输入固定门店、队列、日期、本人号码与实际取号时间。先检查取号前最近有效观测：如果三个展示号码都已超过本人号，后来的继续越号不能证明这张新票已到号；返回「数据待核对」。如果随后本人号确实出现在前两位，仍可形成直接展示证据。原始记录保留，不把冲突样本用于等待训练。

没有冲突时，前两位匹配为直接展示到号，完整三个号码全部越过为推定展示到号。保存上一个未到样本到首个满足样本的时间区间，不把页面读取时间当精确叫号时刻。整段回退、空值和采集缺口仍影响推定资格。v3标签替代旧v2计算结果，原始输入与观测不改，部署前备份私有库；浏览器旧终态恢复后重新核对。

## 数值算法

1. 当前第一展示号码作为参考位置，号码差仅是号码差，不能称为前方桌数。
2. 近30、60分钟连续观测采用两两斜率中位数，减弱单次大跳和补叫干扰。至少4点、15分钟形成正式趋势；连续两点且跨度至少3分钟可产生明确标记的初步估计，区间更宽。
3. 历史按同店、同队列、同日期类型与相近时段选取展示到号代理区间；只有一天或极少样本时标记初步估计。
4. 候选为历史、稳定、较快、较慢四类。实时基础权重稳定60%、较快25%、较慢15%；随着取号后累计观察到30分钟，实时部分占比由15%提高到85%，余量给可用历史。
5. 有效模型建议最多影响最终权重的30%；没有证据支持的候选先移除，再归一化。剩余时间和较早／较晚情景由数值程序计算，模型不能自行填写号码、速率或分钟数。
6. 数值中位情景最多向前推算360分钟；超过120分钟仍显示初步估计，并按等待长度扩大情景区间，预计超过22:00时显示营业时间提示。超过六小时、过时、轮次冲突或既无有效趋势又无历史时给出具体原因，不编造精确时间。
7. 没有任何后续观察时长的历史起点不进入历史分布；有实际观察时长但尚未到号的样本保留未知上界。若这类历史使中位上界未知、当天却有独立可用趋势，则回退当天情景、历史权重归零，明确标记初步估计和历史上界不足。

这些区间是情景范围，不是经过校准的90%置信区间。当前主要限制是采样稀疏、号码补叫、发号序列不可见，以及可靠个人等待样本少。后续用时间顺序回放和新产生的私有标签评估误差、覆盖率及到号漏判，不能用拟合数据证明前瞻准确率。

## DeepSeek 实际输入

固定官方 `deepseek-flash`。思考模式 `thinking.enabled`、`reasoning_effort=high`；非思考为 `thinking.disabled`。思考最大输出16,384 token、90秒，非思考2,048 token、30秒；思考内容不持久保存。

每次发送以下公共证据：

- 门店编号、队列、日期类型、观测时刻和来源。
- 最近一小时最多24个时间点，每点包含距最新点的分钟数与原顺序三个号码；缺位为null。
- 最近7日中可用的同店、同队列、同日期类型、相近时段窗口；每窗样本数、跨度、稳健推进速率。
- 最近一段、30分钟、60分钟速率，历史中位速率、样本数、停滞时长、回补次数和有效跨度。

本人号码、取号时间、私有研究记录、微信会话、API密钥均不进入模型正文。精确公共输入以摘要命名保留在服务器私有审计目录。模型只返回四类权重、引用的特征和理由码；未知字段、错误总和、截断、超时或过期结果均不采用。

完整系统提示词（`public-curve-weights-2`）：

```text
You analyse sampled Sushiro public display progression, not physical service throughput.
Return only JSON with exactly weights_ppm, feature_ids, reason_code.
weights_ppm must include history, steady, fast, slow, nonnegative integer weights summing to 1000000.
The application computes ticket-specific waiting times locally from numeric distance and empirical
rates/interval-censored historical analogues. You adjust scenario weights using ONLY supplied evidence.
trend_points are chronological [minutes before latest sample, first, second, third display number].
Null positions mean absent, not zero. Display order is meaningful; backfilled lower labels can return.
history_windows are same-store, same-queue, same day-type windows near this time of day; each has
sample count, time span and robust numeric progression rate. Do not treat zero growth as missing.
Compare the recent interval with robust 30/60-minute trends. A single jump is weak evidence;
sustained acceleration supports fast, a plateau or deceleration supports slow. Prefer steady when
no change persists. Weight history only when historical windows exist; sparse history has low weight.
With sparse current observations use available history; with neither, choose insufficient_evidence.
Do not invent rates, missing observations, queue length, table counts, ticket issuance or attendance.
A number difference is NOT the number of parties ahead. These are uncalibrated proxy scenarios.
feature_ids: nonempty distinct IDs of supplied non-null numeric features used in your conclusion.
reason_code: history_dominant, rapid_display_turnover, slower_display_turnover, mixed_evidence,
or insufficient_evidence. No prose or reasoning text in the JSON output.
```

## 运行与费用

模型范围共用已批准五城83店目录，浏览器、租约接收、后台工作者和公开结果校验保持一致；范围扩大不自动创建调用，仍只响应当前可见页面的请求。后台返回正在思考状态，初次提交立即登记需求，模型结果返回后由页面自动读取合并；手动刷新和持续跟踪均保留。首次月份目录尚未载入时，不永久缓存空历史。公开模型投影独立限制256KiB并经字段白名单校验，凭证文件默认16KiB限制保持。

按本人2026-10-15明确批准，每个营业日估算上限10元：思考8元、非思考2元。仅有人查看并提交预测或启用跟踪时请求，隐藏／离开即撤销需求；90秒观看租约、45秒续约。手动预测最多等待模型150秒。多人同店同队列同观测共用结果，每个观测每种模式只有一次持久尝试。

账本每日独立，旧失败和未知调用不清除。初次真实高思考用完4,096输出token而未产生有效答案，因此扩大输出；旧账本完整保留，其全部预留成本从当天新账本额度扣除，并禁止同一观测重试。费用按输入4元／输出16元每百万token的保守上限估算，不等同实际供应商账单。后台只读取已经保存的公共归档，独立于门店采集进程。

## 方法参考

- [Theil–Sen 稳健回归](https://scikit-learn.org/stable/modules/linear_model.html#theil-sen-regression)：本项目使用两两斜率中位数的轻量实现，不依赖该库。
- [Forecasting: Principles and Practice](https://otexts.com/fpp3/practical.html)：组合预测与数据可用性边界。
- [按时间顺序评估预测](https://otexts.com/fpp3/toolbox.html)：回测须避免未来观测泄露。
- [DeepSeek 思考模式官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)：思考开关与推理强度。

## E0538故障修正证据

本人反馈新增预测总是无法估计。实际已保存曲线复现：旧v5的120分钟阈值令较长等待返回median_upper_unknown；该分支在合并模型参与标志前退出，界面又没有该原因的文案，于是统一显示暂无法估计。后台真实思考已成功返回，不能将本次故障解释成未配置API或模型完全未实现。另发现扩店后模型四层仍限制原五店，已统一为五城目录。

v6已于2026-10-17 15:57:57部署原地址，浏览器实际当前本人号码显示数值剩余时间、宽情景范围及Flash思考已参与；私人号码、取号时间及完整截图只留本人private证据，不写公共文档或模型输入。公开模型实际25650字节、支持83店、同观测thinking结果缓存可用。来源／同步／邮箱／授权四进程未重启，公共健康83店active、673成功／1条原失败。完整2300项检查通过（4环境可选跳过），未产生真实测试号码或增加来源查询。准确性仍需真实等待结果评估，不能把本次接线修复当作已校准。
