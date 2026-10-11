# 官方来源的统计接线

E0454 / rc77本机新增[匿名连续性导出及纯投影](../../OFFICIAL_CONTINUITY.md)，保留原记录ID／摘要；云端v1归档仍不携带关联，下面的packet_continuity_unknown不能由前端自行改成可比。新源中的links与分页anchors需后续完整存储／确认／恢复接线；当前日历和参考页继续使用实际部署的rc76，不改变15分钟采样或新鲜度阈值。

E0453部署事实：18:56:42原统计地址的显示服务已切换到固定rc76 / 5de31ca，日历和/statistics-legacy均明确绑定官方来源，94源码／网关文件和3资源SHA及3家日接口实际核对。接收器／归档仍原固定rc74、数据目录不迁移；不能把显示版本升级说成采集器或凭证供应升级。


2026-10-11 E0452 / rc76：原地址的日历已实际使用官方归档。修复侧栏“已有号参考与用餐记录”仍请求旧CRM的遗漏：正式网关在/statistics-legacy的main#sushiwait-reference注入data-statistics-source="sapi_miniapp_gateway"，与月历采用同一明确来源。参考页本月、旧月、同店历史和当天曲线只读/api/v1/official/；404／坏字段不回退CRM。模型与页面提示以api_profile和summary／point的单GET成功语义校验，默认独立旧页面仍兼容CRM。新候选云部署按后续交接实测，下面较早状态保留为历史。

当前schema1包的observation_id不可逆，不携带原run_id或可验证连续性；不能仅按数字单调或时间接近推定同一采集进程。因此packet_continuity_unknown保持，展示集合速度和精细趋势预测不凭空补齐。后续核心模型需新增匿名连续性契约及受控新计划，与旧记录ID／摘要／去重兼容；不篡改旧包。本次只接来源和原号码出现提示，15分钟计划不变。


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

它固定读取 `/api/v1/official/months/YYYY-MM` 与 `/api/v1/official/stores/ID/days/YYYY-MM-DD`。默认仍读取原CRM统计路径。两种来源不回退；月与日来源不同、profile错误或成功口径错误会拒绝显示。rc73已补本机归档读者和网关的显式接线，**云端尚未启用这些路由，线上默认页面仍使用原来源。** 不可仅改前端source就称部署完成。

官方一次详情GET同时返回四队列与数量；source为`sapi_miniapp_gateway`、api_profile为`miniapp_gateway`。旧日历键`successful_pairs`／`pair_ok`仅作兼容别名，必须保留`success_semantics=one_official_detail_response_not_two_query_pair`及`successful_detail_responses`，不能把一GET说成原两GET配对。只有实际存在的数量才共享同一次接收时间。

曲线保持四个原队列、顺序、重复值和前三位；完整数组保留在返回的原公开包中。纯投影不读写磁盘，因此full_source_arrays_persisted=false、full_source_arrays_in_input_packet=true，持久写者须实际共同保存输入包和投影后另记保存证据，不能从内存转换推定落盘。缺失队列保留presence和null，不能制造第五个storeQueue、空数组或零数量。普通／预约必要字段不足时曲线queues为null，同时保留已观察字段。第一位仍只是用户约定参考，消失不代表过号；包不含运行连续性证明，所以不跨记录计算真实速度或过号率。

selection_scope为`bounded_public_packet_not_full_day`。覆盖率只计算包里实际成功落入营业采样格的观测，不能把六条短测补成全天；包之间的完整增量归档／连续性和服务器后台推送仍需接线。读取不会查询寿司郎，也不会生成真实叫号训练标签。

## rc73：从已保存归档提供统计

`OfficialStatisticsView`只读现有`packet-archive`／本机接收器的schema1归档库。写者继续校验完整公开包，按观测ID幂等去重、冲突整批拒绝；读取按明确来源、1–3个门店、月份／日期选择，保持同一SQLite读取事务。并发追加后下一次读取可以看见新批次，进程重启不丢失已有记录。库或选中记录损坏返回503，缺少日期返回summary=null及空points，不补零、不保留旧成功作为新结果、不回退CRM。

私有配置（JSON文件0600、父目录0700）必须正好包含下面七项。路径是操作员本机路径，例子不表示已经上传或部署：

```json
{
  "schema_version": 1,
  "mode": "official_packet_archive_readonly",
  "database_file": "/absolute/private/public-observations.sqlite3",
  "store_names": {"3004": "西单大悦城店"},
  "data_origin": "live",
  "hours": {"复制 config/default-business-hours.json 的完整对象": "此处仅说明，不能直接运行"},
  "base_interval": 60
}
```

已有`deploy/statistics_gateway.py`增加`--official-view-file`。不指定时官方路由返回404；指定后只挂载上述两个GET类型，原CRM路由、公共只读限制、缓存上限、原试采期限及端口不变。接口读取归档，不读取查询凭证、启动写者、重试403或生成新凭证，也不提供公共上传／管理接口。`hours`必须是完整已验证格式的营业假定对象，例子中的说明对象会被拒绝。

读者保留归档中的四个完整数组，曲线仅展示前三位。实际读取并校验选中归档记录后，非空日详情标记`full_source_arrays_persisted=true`及`persistence_semantics=validated_selected_records_in_packet_archive`；空日为false，纯内存`project_packet`仍为false。新`selection_scope=bounded_archived_public_observations`不表示全天无缺口，`collection_state=not_proven_by_archive`不表示后台存活。源新鲜度和运行连续性仍unknown，不能据此补过号率或跨缺口速度。

当前每个被选店日最多1000条、4MiB公共包，月最多31天／3店；单次SQLite读取5秒上限，响应受原浏览器2／8MiB限制。超过范围明确503，不静默截断或冒称全国长期容量。一般每分钟营业采样660／690条能落在此范围；大规模及更密集全天方案需要后续索引／分页设计。新读者没有联网传输层，不能把本机接收／归档、可选网关接线称为远端自动推送已经完成。

2026-10-11 00:20实际将此前三店六条本人官方观测写入新的本机私有归档，首次插入6条、重复包去重6条，重启后的读者生成月与三店日共4份契约，每店2点；四文件实际保存并校验。此步骤新增寿司郎请求0、凭证读取0、云部署0，不把10月10日观测当今天新数据。正式前端仍须显式选择官方来源，由主线完成云端启用。

2026-10-10实际新主页上下文的三店两轮六条200记录已在本机生成并用同一浏览器适配器读取，投影／原包留在ignored private目录，不公开HAR或凭证。服务器接入优先考虑只传公开字段包，由服务器独立保存统计；凭证继续本机。持久传输、官方会话供应和允许采集范围尚未完成，不能称全国恢复。主线交接见[PROJECT_HANDOVER](../../PROJECT_HANDOVER.md) E0398起。


## E0439：月历页面的明确来源绑定（本地源码）

网关可同时提供两类统计路径；新增 `--calendar-source sapi_miniapp_gateway` 与 `--official-view-file` 一起使用，才把原 `/statistics` 月历明确绑定到官方归档。没有显式选择仍为原CRM来源；官方视图缺失在启动时拒绝，页面root缺失／歧义返回503，不自动回退或混入CRM观测。前端读取服务端设置的 `data-statistics-source`，继续使用现有适配器的固定路径和30秒副本轮询，不新增寿司郎查询。原视觉、Logo和 `/statistics-legacy` 保持。

本次真实三店6条归档已生成四份契约，被浏览器适配器实际接受，各店2点。55项相关检查通过；仅本机源码与数据处理证据，云端未部署、页面实际显示尚未核对。源配置只包含公共归档路径和营业假定，不包含查询凭证。真实自然到期供应试验完成后再部署；本节替代“仅修改适配器即能让月历使用官方来源”的潜在误解。

## E0441：真实浏览器绘制与旧号码保护

实际浏览器发现官方月数据已被客户端接受，但格子绘制调用calendarCells时又默认按CRM验证，页面停在读取状态。新增显式第四参数`calendarCells(index, month, storeId, {source})`，默认仍CRM；calendarForScope从已验证的index保留来源，城市过滤和单店选择都传递同一值。未知来源、错误profile仍拒绝，不自动更换接口。同步本目录的statistics-client.mjs镜像，既有DS交付哈希按其历史版本保留，新的部署需要重新冻结资源摘要。

另修复queuePresentation陈旧状态没有覆盖labels的问题：超过新鲜度门槛，顶端号码为null、显示暂无可用号码和最后响应；历史表／曲线仍保留原始标签。不能用网页读取时刻给旧号续新鲜度。当前五分钟采样试验沿用90秒显示门槛，因此部分间隔会明确显示资料较旧；这不是原始数据被删除或采集已经停机。

本机真实浏览器已完成三店月份→北京→西单当天详情，三店21组、北京14组、西单7条，曲线和原始表可见；16:10:09真实新观测后号码更新，超过门槛后顶端撤下，原始记录不丢失。57项0.724秒与修改后完整1819项38.166秒通过。公共归档16:15实际增至24条，云端尚未启用，长期自然期限更新待；本次私有预览只监听回环且最多600秒，读取已有归档、不读凭证、不查询寿司郎，不作为生产地址。
