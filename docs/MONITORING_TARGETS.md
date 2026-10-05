# 用餐时间偏好与监控目标

rc10新增本机离线 `monitor-plan`，把理想到店时间、叫号偏移和判断时刻转成目标刷新节奏。它不预测叫号、不查询门店、不保存个人计划、不通知或取号，现有collect仍按原固定周期运行；服务端共享调度与用户前端继续另行实现。

```sh
sushiwait monitor-plan \
  --desired-arrival-at 2026-10-06T19:00:00+08:00 \
  --as-of 2026-10-06T18:40:00+08:00 \
  --base-interval 300 \
  --call-offset-minutes -10
```

示例只核对策略，不访问真实门店。全部时间须显式带时区，统一输出UTC；不能把输入的理想时间当已校准预测。窗口外周期必须明确指定60–3600秒，叫号偏移允许-1440至1440分钟。

`target_call_at = desired_arrival_at + call_offset_minutes`。`+10`意味着希望到店后约10分钟叫号，`-10`意味着希望到店前10分钟叫号；偏移是用户的时间偏好，不是统计误差或预测区间。

| 情况 | 请求的目标周期 |
| --- | --- |
| 到监控时间超过30分钟 | 明确配置的背景周期 |
| 不超过30分钟且超过15分钟 | 60秒 |
| 不超过15分钟，或时间已过仍等待 | 30秒 |
| 明确提供展示集合加速输入 | 提前进入30秒并请求重算 |
| called/no_show/cancelled/ended明确结束状态 | 无下一查询目标 |

监控时间取理想到店、偏移后的目标叫号，以及可选外部最早叫号估计的较早值。晚叫号偏好不会延迟原到店窗口，负偏移会提前窗口；较晚的外部估计不能推迟监控。外部估计标为未认证，展示加速输入不成为真实过号率。本工具尚未从signal-report自动导入信号，也不计算概率或误差。

输出requested_interval_seconds、next_poll_target_at及判断原因，明确scheduler_applied=false、polling_performed=false、notification_sent=false、eta_available=false、true_no_show_rate=null和source_freshness=unknown。指定30秒不证明上游30秒刷新、允许频率或保证不会过号。下一查询目标是当前判断时刻加周期，不是补发过去缺失的查询；业务状态由调用方明确提供，本工具不确认本人票号。

17项专项0.004秒通过，覆盖精确边界及边界前1微秒、同一时刻不同UTC偏移、跨午夜、过时等待、计划结束、加速输入、外部估计不延后、非法/无时区/溢出输入和无网络/凭证/原生CLI操作。完整438项4.759秒OK/无跳过，最终20模块/年度资源/README逐字节匹配、checkout外隔离安装中的负10分钟语义与30秒目标/无调度/无通知检查通过，socket0/凭证0/原生CLI0。包hash及实际公开/CI见E0111起，不借已有实时采集冒称该策略已上线。

用户原始语义及防过号需求见[完整交接](PROJECT_HANDOVER.md)R04/R05及2.3；数据字段见[FIELD_MAPPING](FIELD_MAPPING.md)，展示变化与真实过号的区别见[SIGNALS](SIGNALS.md)，实际采集及阶段边界见[V0_2_ACCEPTANCE](V0_2_ACCEPTANCE.md)。
