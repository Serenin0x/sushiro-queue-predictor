# 有限临时错误与后续采样

rc24 的普通 `collect` 和私有有界任务可显式设置 `--transient-failure-budget 1–10`，要求完整私有凭证文件。默认0保留首错停止。此选项允许在限定次数的502/503/504之后继续**后续原定槽位**，不重发失败槽位、不立即重试同一门店、不补暂停期间的查询。

```sh
sushiwait collect --api-profile miniapp_gateway \
  --credentials-file /path/to/private/context.json \
  --db /path/to/private/samples.sqlite3 --task-file /path/to/private/collection.json \
  --store-id 3004 --store-id 3014 --store-id 2009 \
  --samples 30 --interval 60 --wait-for-credentials 600 \
  --transient-failure-budget 2
```

这是本人已授权试点的配置例子；运行前仍需核对账号、频率和当前范围。预算2表示前两次符合条件的失败可各允许继续一次；第三次失败仍记录，然后停止，不表示最多只会观察到两次失败。额度是整个有界任务累计，不是每家店或每轮重新获得。没有新业务权限或全国采集范围。

## 继续条件与停止条件

只有本次同run、同店、同来源/profile、精确结果边界之后的唯一失败记录，可授权继续；必须为request阶段的`http_error`且HTTP状态502/503/504。较旧失败、跨店数据、损坏载荷或不明确结果均不能授权继续。

每轮每店仍最多一次。该轮发生可继续错误时，下一轮至少在最后一次观测完成后的一个完整周期启动；45秒的合成慢失败不会导致第45秒立即再查同一门店，30秒周期下须至少第75秒。完整保护和声明到期检查照常运行。

401、403、429、其他HTTP错误、TLS/网络错误、解析/身份错误、无效凭证和持久化失败均立即停止；不自动换接口、换授权或重放初始化。401保持原已拒绝授权的阻断检查。`monitor-collect` 仍保留原首错停止规则，不使用此选项。

失败记录、失败槽位和成功槽位分别保留。有失败的任务即使走完所有槽位，退出仍为1，`all_slots_successful=false`；正常退出0只表示全部成功，不因容忍选项改写。继续事件固定为`transient_query_failure_recorded`，明确`failed_slot_retried=false`；这是查询容错，不是个人取号/取消/重排机制。

## 保存与恢复

默认任务配置不新增字段，旧schema1任务可继续按原流程恢复。显式启用时，schema1配置新增受限整数`transient_failure_budget`；新版本可读两种配置，旧版本会拒绝带此字段的任务，不能降级后强行恢复。快照库保持schema2，不迁移。

显式`--resume-task`必须重复同一预算配置；改变/省略启用预算会产生配置冲突。累计failed计数由已保存任务和SQLite对账，重启不重置额度，失败/未知槽位不重发，重启仍等待完整周期。内部继续入口再次核对最后记录校验及错误类型，不能清除401停止或篡改记录。任务state只是已保存状态，不能替代进程健康检查。

本机新增12项专项（两种CLI模式和多个错误边界），连同既有任务/凭证共62项1.111秒OK；完整735项10.593秒OK/无跳过。最终rc24的31模块包已在checkout外独立安装，实际运行两种CLI的合成容错检查、共8次查询，首个45秒504被保留，后续3次成功、任务完成仍失败退出；socket/查询凭证/native/child调用均0，上下文/传输明确替代，不能称真实接口容错验收。公开与自然上游错误的实际验证以 [PROJECT_HANDOVER](PROJECT_HANDOVER.md) 为准，不为测试制造生产错误。
