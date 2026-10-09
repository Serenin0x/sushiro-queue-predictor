# 堂食与预约的字段绑定

0.2.0rc58统一已有号追踪、实时公共特征、历史趋势与实时回测的队列选择。采集仍完整保留五类原始数组和顺序；不会修改既有采集库，也不会把缺失号码补成真实叫号。

| 业务队列 | 模型字段 | 依据与限制 |
| --- | --- | --- |
| ordinary：堂食 | mixedQueue | 三试点与官方页面的近时核对；仅表示有限堂食展示集合 |
| reservation：预约 | reservationQueue | 预约展示集合，独立计算 |
| 门店汇总 | storeQueue | 可能混入预约号，不能直接作堂食速度输入 |

既有映射依据见[字段核验](FIELD_MAPPING.md)，本轮纠错见交接E0339–E0340。2026-10-09西单日期投影中，11:46的storeQueue第一位为083，11:47变为7070；同期reservationQueue含7070，而mixedQueue为093／096／098。这种跨队列跳动不能解释成堂食推进6987桌。此前部分实现使用storeQueue，与既有页面映射不一致，本版本修正该实现错误。

## 可执行约束

`queuebinding.py`集中定义`ordinary_mixed_reservation_separate_v1`。新公共上下文使用严格整数`schema_version=2`，并显式包含该`queue_binding_policy`。上下文、模型输入摘要和AI建议绑定同一策略，不能仅改标记继续沿用旧建议。

手动号观测、实时邻域模型与趋势评分要求已绑定的schema2。普通数学融合仍接受schema1作为旧离线算术兼容，但旧上下文不得进入新追踪、趋势或实时模型路径。堂食仅在mixedQueue匹配本人号码，预约仅在reservationQueue匹配；只出现在storeQueue不会被判断为堂食号已展示。

新私有追踪策略为`private_manual_ticket_observation_publication_v2`，趋势策略为`dated_nonoverlapping_display_turnover_ranks_v2`，实时邻域策略为`cutoff_episode_balanced_landmark_neighbors_v2`，回测策略为`cutoff_known_paired_realtime_replay_v2`。旧观测会话和旧趋势档案因策略不匹配而明确拒绝，不自动重新标记或覆盖。需要重新生成时，应使用完整原始五类数组建立独立新资料，并保留原文件和来源。

## 展示与验证边界

本地统计页默认堂食展示mixedQueue，storeQueue标为“门店汇总（可能包含预约）”。监控页堂食趋势也使用mixedQueue；五类原始选择仍保留。第一位只是用户约定的当前叫号参考，展示变化不证明实际入座、真实过号或前方桌数。

回归检查使用互不相同的堂食／预约／汇总数组，验证跨队列号码不误匹配、仅汇总变化不影响堂食趋势或拟合等待、旧策略拒绝且原文件不变。实际日期副本用于发现错误，未生成真实等待标签；合成拟合检查也不代表已校准ETA。

运行中云端采集保持rc57。新页面和模型包的本地通过、公开发布、CI、云端部署分别记录；未部署前固定入口仍使用已安装旧页面。采集五类原始数组不受本次模型绑定修正影响。
