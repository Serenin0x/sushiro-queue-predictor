# 新视觉页面的数据接入包

这是可直接导入前端源码的无框架数据层，不包含新的视觉界面。它读取现有公共统计网关，不读取个人号单、调用寿司郎、取号、取消或重启采集。线上网页尚未使用本模块；正式发布需要设计源码、静态资源清单和同源接线，详见[DS-002](../STATISTICS_INTEGRATION.md)。

- [statistics-client.mjs](statistics-client.mjs)：只读请求、选择状态、30秒轮询、月历摘要及曲线转换。
- [statistics.synthetic.json](statistics.synthetic.json)：由真实后端投影代码产生的**合成**样例，店号900001只用于离线设计，不能查询真实来源。
- [行为检查](../../../tests/statistics-client.test.mjs)：请求竞态、超时、数据语义与缺口。
- [当前大月历原型的具体对应表](PROTOTYPE_MAPPING.md)：按设计原型元素、示例函数及部署资源列出实际替换位置。

本模块为本项目原创，沿用仓库MIT许可。无框架依赖、账号凭证、外部模型调用或固定服务器地址；React、Vue、Svelte与普通JavaScript页面均可导入。开发服务只将固定`/api/v1/`转发到部署配置中的网关，生产页面与API同源。

## 接线示例

把模块纳入前端构建，将`render`替换为设计组件的状态更新。示例代码进入本地JS文件，不在当前严格内容策略下插入内联脚本。

```js
import {
  createStatisticsClient, createStatisticsController,
  shanghaiDate, calendarCells, latestQueue, chartSeries
} from './statistics-client.mjs';

const client = createStatisticsClient();
const controller = createStatisticsController({
  client,
  onChange(state) {
    const cells = state.index
      ? calendarCells(state.index, state.selection.month, state.selection.storeId)
      : null;
    const current = state.detailState === 'ready'
      ? latestQueue(state.detail, state.selection.queue)
      : {state: 'unavailable', labels: null};
    const chart = state.detail
      ? chartSeries(state.detail, state.selection.queue)
      : null;
    render({state, cells, current, chart});
  }
});
const date = shanghaiDate();
await controller.select({month: date.slice(0, 7), date, storeId: null});
controller.start();

// 用户选择门店或日期；店号必须来自state.index.configured_store_ids。
// await controller.select({month, date, storeId, queue: 'mixedQueue'});
// 仅切换queue:'reservationQueue'不会再读API。
// 手动刷新：await controller.refresh();
// 页面卸载或隐藏：controller.stop();
// 页面重新可见：await controller.refresh(); controller.start();
```

`onChange`收到不可变状态。月份／日期／门店切换立即清除旧明细、取消旧请求，并用选择版本隔离迟到成功和迟到失败。重复刷新共用在途任务；`start`不会重复建立定时器，`stop`取消当前读取和轮询。默认单次15秒超时，响应大小限制月历8MiB、明细2MiB。错误只含固定错误码和HTTP状态，不把服务端错误正文放入页面。

这里统一查询明确月份的`/api/v1/months/YYYY-MM`和所选门店单日接口，不需要先请求`/api/v1/days`。没有选择门店时不读明细，选择一家只读取一家；不并发扫描147家。当前线上支持这两种路径，错误不会回退成另一月份的统计。

## 页面状态与显示

| 状态 | 设计页面显示 |
| --- | --- |
| `phase=loading` | 正在读取；实时号码暂停，不把旧结果改成新门店／新日期 |
| `phase=ready` | 已保存副本可读；号码另看`latestQueue`新鲜度 |
| `phase=partial` | 月历部分服务／归档未加载，或当日明细读取失败；已知月历可保留 |
| `phase=error` | 本轮月历读取失败；不显示零客流、零排队或预测时间 |
| `phase=idle` | 尚未选择或已停止页面轮询；不显示正在实时更新 |
| `detailState=unselected` | 先选门店；不要替用户批量读取所有门店明细 |
| `latestQueue.state=fresh` | 当前日期的最后一组有队列响应，距本机当前时间不超过90秒；保留响应时刻 |
| `latestQueue.state=stale/unavailable` | 陈旧或不可用，暂停“当前号码”提示；需要显示旧号时须明确其失效状态 |
| `latestQueue.state=historical` | 这是选定历史日最后一组观测，不是实时号码 |

90秒仅为此展示层的陈旧提示阈值，不证明官方源实时刷新，不能替代预测模块的新鲜度规则。重新显示页面时刷新并重新计算时间，不能把休眠前的`fresh`状态留到次日。有效空数组与没有队列响应分开；最后一组失败不会倒找旧成功号码冒充最新。

## 月历和图表

`calendarCells`返回真实月份天数、周一开始的星期位置、已观测门店数与配置门店数。没有数据的`observations=null`，不能显示0。覆盖使用观测格总数÷预期格总数；`coverageIsPartial`表示目录／数据不完整，不将部分门店的高覆盖涂成全国完整。`pending`、`unavailable`和`no_data`分别渲染。颜色表示采集覆盖，当前没有真实人流热度。

节假日与调休标记另用仓库明确年份的[日期资料](../../../src/sushiwait/data/cn-mainland-2026.json)打包，不添加不存在的节假日API；未支持年份显示待核。视觉日型不修改后台营业计划。

`chartSeries`提供三种分别命名的资料：

- `reference`：第一位纯数字参考位置的分段曲线，每点仍保留原字符串`label`；收到队列失败、非数字、长缺口、运行边界、时间逆序或号码倒退时断开。它不是实际已叫号游标。
- `turnover`：可比展示集合的变化点，单位为展示号码变化／分钟，只画独立点或柱，不自动把点连过缺口。
- `rawCount`：原数量及其独立响应时间，单位仍为未知；只画点，不能标成真实人数／前方桌数。只有数量失败时，独立成功的队列观测仍可画。

原前三位的完整明细直接读`state.detail.points[].queues`，保留顺序、重复、前导零和非数字标签。明细操作时间字段实际叫`request_started_at`，号码横轴用`queue_received_at`；不是`operation_started_at`。`graphTruncated`保留后端截断状态，图表范围不替代完整数据库归档。

## 交付与当前限制

视觉任务继续负责布局、浅深色、图标和可交互组件，主线负责API和同源部署。交付新界面时提供源码入口、资源清单、该模块的导入／适配位置及加载／失败／历史／窄屏状态；同一门店日期与旧页对照后才能更新固定入口。当前许可、内容策略及静态路径仍按DS-002执行，不扩大公开管理接口。

本次只有接入包和合成契约检查通过，未完成新视觉页面、实际浏览器接线或云部署。生产源码仍rc68，线上仍冻结rc64；此目录没有加入生产wheel，不能把它称作已经上线的页面更新。
