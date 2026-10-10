# 官方来源的统计接线

rc72新增 `sushiwait.officialstats.project_packet`，读取已校验的公开字段包，生成月历／单店日曲线契约。原包须与投影共同保存。示例：

```python
projection = project_packet(
    public_packet, as_of=aware_utc_time_string, hours=declared_business_hours,
    base_interval=60, store_names={"3004": "西单大悦城店"},
)
month = projection["months"]["2026-10"]
day = projection["days"][("3004", "2026-10-10")]
```

店名范围必须与包的明确1–3店完全一致；示例只有单店，三店包需要三店名称。输入沿用≤1000条／4MiB、记录哈希、时间、字段与来源校验，不能混入legacy／CRM记录。返回值只有公开字段，不读凭证、数据库或网络，也不自动上传。

浏览器适配器新增显式来源：

```js
const client = createStatisticsClient({source: "sapi_miniapp_gateway"});
```

它固定读取 `/api/v1/official/months/YYYY-MM` 与 `/api/v1/official/stores/ID/days/YYYY-MM-DD`。默认仍读取原CRM统计路径。两种来源不回退；月与日来源不同、profile错误或成功口径错误会拒绝显示。**这是新增契约和客户端路径，云端网关尚未挂载这些路由，线上默认页面仍使用原来源。** 不可仅改前端source就称部署完成。

官方一次详情GET同时返回四队列与数量；source为`sapi_miniapp_gateway`、api_profile为`miniapp_gateway`。旧日历键`successful_pairs`／`pair_ok`仅作兼容别名，必须保留`success_semantics=one_official_detail_response_not_two_query_pair`及`successful_detail_responses`，不能把一GET说成原两GET配对。只有实际存在的数量才共享同一次接收时间。

曲线保持四个原队列、顺序、重复值和前三位；完整数组保留在返回的原公开包中。纯投影不读写磁盘，因此full_source_arrays_persisted=false、full_source_arrays_in_input_packet=true，持久写者须实际共同保存输入包和投影后另记保存证据，不能从内存转换推定落盘。缺失队列保留presence和null，不能制造第五个storeQueue、空数组或零数量。普通／预约必要字段不足时曲线queues为null，同时保留已观察字段。第一位仍只是用户约定参考，消失不代表过号；包不含运行连续性证明，所以不跨记录计算真实速度或过号率。

selection_scope为`bounded_public_packet_not_full_day`。覆盖率只计算包里实际成功落入营业采样格的观测，不能把六条短测补成全天；包之间的完整增量归档／连续性和服务器后台推送仍需接线。读取不会查询寿司郎，也不会生成真实叫号训练标签。

2026-10-10实际新主页上下文的三店两轮六条200记录已在本机生成并用同一浏览器适配器读取，投影／原包留在ignored private目录，不公开HAR或凭证。服务器接入优先考虑只传公开字段包，由服务器独立保存统计；凭证继续本机。持久传输、官方会话供应和允许采集范围尚未完成，不能称全国恢复。主线交接见[PROJECT_HANDOVER](../../PROJECT_HANDOVER.md) E0398起。
