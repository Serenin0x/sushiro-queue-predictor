# 门店展示与刷新状态

`store-view` 是 rc23 的本机只读展示出口，为未来小程序的“叫号一处掌握”准备数据。它读取采集器已有的 SQLite schema 2，输出某一家门店的堂食/预约有限展示数组、已保存聚合字段，以及本机采集状态。它不会发起刷新请求、取得新凭证、发送提醒或查询个人号单；用户页面尚未实现。

```sh
sushiwait store-view --db /path/to/private/samples.sqlite3 \
  --store-id 3004 --api-profile miniapp_gateway --data-origin live \
  --as-of 2026-10-06T11:30:00+08:00 --max-age-seconds 90
```

必须明确数据库、门店、来源、接口配置和带秒/时区的展示时刻；例子中的时刻只用于回放，应按实际需要指定。`--sample-limit` 默认最近1000条、最多10000条同范围记录，额外一个ID判断截断，不扫描整库计数。不同门店、接口和 live/fixture/synthetic 不混合。

## 输出约定

`last_response.display` 只保留 `storeStatus`、`groupQueuesCount` 和四个 `groupQueues` 数组。号码保持原顺序、前导零、前后缀和重复值；缺失、null、invalid、unknown、真实空数组和零分别呈现。不选最大号码，不拼成完整叫号游标，不推断个人是否已叫号。聚合字段单位仍 unknown；目录字段与官方列表桌数的同次对照见 [FIELD_MAPPING](FIELD_MAPPING.md)，不能把另一时刻的详情值当作目录已刷新。

| availability | 展示含义 |
| --- | --- |
| recent_response | 最后有效记录是成功响应，响应年龄未超过显式阈值 |
| last_known_only | 后续查询失败或本机预检停采，旧展示仍可作为最后已知信息 |
| stale | 最后成功响应年龄已超过阈值 |
| invalid_latest_observation | 最近记录损坏、身份/时间不符或时间逆序，阻止近期展示声明 |
| unavailable | 有界范围中没有可用成功响应；不显示“无需等待” |

`display_is_last_known` 在旧信息仍被返回时必须一并展示。`refresh_state` 区分 `response_received`、`query_failed`、`preflight_stopped`、`invalid_record` 和 `no_observations`，与数据过时状态分别解释。查询失败不改变旧号码，也不创造成功响应时间。

这里的刷新状态是该店最近所选记录的结果，不是采集进程的当前运行状态。多店任务可能只在首店记录一次凭证保护，不能据其他店最近成功记录认定后台仍在采集。`collector_state_available=false`、`collector_liveness=unknown` 明确保留这个区别；私有任务状态见 [COLLECTION_TASKS](COLLECTION_TASKS.md)，永久服务需单独接入任务状态和进程健康检查。

`last_response_age_seconds` 只度量展示时刻距离本机成功接收的时间，边界等于阈值仍算近期响应；它不是上游新鲜度。`latest_observation` 描述最近可验证观测：成功的 HTTP 响应、本机预检时间，或失败尝试结束。无HTTP响应的网络失败不能称服务器已响应。无效尾记录的安全详情为null。

## 输入与边界

在一致的 SQLite 只读事务中选取有界尾记录。未来记录先排除；记录JSON拒绝重复键和非有限数，最大64KiB，身份和记录/载荷时间一致。复用 [公共字段包](PUBLIC_PACKETS.md) 的有限展示白名单，未识别的号码格式、状态或过大数组使记录无效，避免猜测新协议。更早的有效展示只能作为最后已知信息。后续正常成功记录可以恢复近期展示；相同/逆序记录时间不能覆盖较新的展示。

扫描截断与未来排除分别报告；被截断范围外的成功记录不会补读或推造。仅支持schema2，旧库需按原迁移流程处理，展示工具不迁移、不写库，不读取凭证或原始捕获。输出含实际门店展示号码，应在本机或授权服务中保管，不提交公共仓库、传给LLM或当作个人号单。

本机19项专项覆盖两类队列、顺序/重复/前导零、CLOSED下非空展示、存在性差异、90秒边界、失败/保护恢复、未来数据、跨范围隔离、损坏/过大/逆序、安全投影和数据库不变。完整723项已通过；独立安装和真实库验证另记 [PROJECT_HANDOVER](PROJECT_HANDOVER.md)。软件检查不认证真实叫号、过号率、源刷新或模型误差。

2026-10-06，rc23已完成31模块独立安装和两次合成CLI近期/过时检查；同一安装包对两份既有真实三店库作六次历史投影成功，数组与最近成功记录逐项一致，旧成都尾失败保留、两库SHA不变、上游请求0。历史时刻11:28:31.442524由实际记录取得，不以此认证当前服务在线；原文只在本人私有目录，详见交接E0193。
