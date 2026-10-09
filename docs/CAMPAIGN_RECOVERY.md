# 有界采集的瞬时故障恢复

候选rc57的`remote-campaign-collect`和`remote-campaign-serve`新增显式`--transient-recovery-limit 1–3`。默认0仍首错停止，已有任务不能靠添加参数改变历史配置。新策略适用于当前匿名CRM采集，与私有微信普通collect的旧`--transient-failure-budget`分开。

发生符合条件的失败时，完整失败窗口先封存，数据库和检查点摘要保留。等待60／120／240秒后，在同一campaign创建新窗口并采集当时的门店数据。失败槽位不重发，期间缺口不补造；原累计请求上限、总截止、营业窗口与同店间隔保持，重启不重置恢复额度。

允许的瞬时错误只有超时、网络错误、非证书校验的TLS错误，以及HTTP502／503／504。401／403／429、其他HTTP、证书校验、重定向、模式／解析、未知在途结果与存储冲突均停止。恢复判断只来自已校验的末条失败记录，不接受较旧失败或另店结果。旧失败子窗口不复活，已经失败／完成的总campaign也不复活。

额度3允许前三次符合条件的失败各尝试继续一次；第四次失败仍记录后停，不代表最多只会出现三个失败。所有成功和失败都消耗原槽位。有失败的campaign即使走到截止，整体`ok=false`，collect退出1；进程完成和“全天无失败”分别报告。服务可以展示完整失败数及后续有效观测，不把有限恢复称绝对零中断或永久故障控制器。

```sh
sushiwait remote-campaign-serve \
  --root /absolute/private/new-campaign \
  --plan-file /absolute/private/plans.json \
  --business-hours-file /absolute/path/config/default-business-hours.json \
  --store-id 3014 --base-interval 60 \
  --duration 172800 --window-duration 86400 --max-pairs 1500 \
  --transient-recovery-limit 3 --resume-if-present --port 18801
```

本例是单店48小时试验，1500组／3000GET上限，尚不代表上游允许任何频率。按店独立运行可以避免一家失败使同批另两家停止；十二店的总预算仍需要明确汇总。跨进程正常恢复原任务，以及瞬时故障后的新窗口，两者分别保留缺口和边界。

运行状态新增最大恢复额度、已使用次数和失败槽位不重放说明。schema3只用于显式策略；旧schema1／2、SQLite schema2保持。降级软件不能强行打开新配置；资料损坏时保留现场并停止。

10项专项覆盖失败资料保留、60／120／240退避、额度／原预算、重启退避、闭店等待、鉴权／限流／证书／模式拒绝、未知请求及归档变化。安装专项实际运行安装包CLI，合成503后两次成功共5GET，终态重开无新查询、失败文件逐字不变。合成结果不是真实门店质量，云端自然故障和全天覆盖见[交接记录](PROJECT_HANDOVER.md)。
