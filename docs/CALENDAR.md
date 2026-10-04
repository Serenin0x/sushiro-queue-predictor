# 日期分类与历史可用信息

`date-features` 是离线数据准备工具，不提供等待预测。它按 `Asia/Shanghai` 计算星期、时段、月份、季度和日历季节，再用已核对的年度公告区分普通工作日、普通周末、放假日期与调休上班日。当前仅覆盖 **2026 年中国大陆全国安排**；门店实际营业、城市活动、天气和客流趋势均没有由此验证。

## 来源与版本

数据为项目逐项整理的日期事实：[cn-mainland-2026.json](../src/sushiwait/data/cn-mainland-2026.json)，来源是[北京市政府转载的国务院办公厅通知](https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html)。本轮重新核对完整正文，包含七段放假区间与六个周末调休工作日。公告日期为 2025-11-04，本项目核对日期为 2026-10-04；规则版本 `cn-mainland-2026.20261004.1`。没有自动下载或自动接受网页更改。

区间均含起止日，不将其中每一天都解释为法律意义上的法定节日。全年 365 天分类为放假 33 天、调休上班 6 天、普通工作日 242 天、普通周末 84 天；这是日历分类，不是门店的开门日期或客流统计。10 月 4 日分类为国庆假期第 4 天；10 月 10 日虽为周六，分类为调休上班。源文件 SHA256 随输出记录，解释策略为 `cn-calendar-v1`，原公共快照和内容哈希保持不变。

后续年度必须核对对应公告、更新版本/来源/覆盖范围、检查日期冲突并补充全部边界和安装检查；未知年份不能套用 2026 年安排。地方额外安排、特殊活动和门店临时营业信息另建带证据的资料。

## 使用

安装后的入口：

```sh
sushiwait date-features \
  --at 2026-10-10T18:30:00+08:00 \
  --as-of 2026-10-04T19:00:00+08:00
```

开发入口可用 `PYTHONPATH=src python3 -m sushiwait` 替代 `sushiwait`。两个时间都必须含秒和显式时区；拒绝无时区、无效日期、闰秒和多于六位的小数秒，不默认时区。UTC 和带偏移的同一时刻会产生相同分类；以转换后的当地日期决定年份和假期。`--at` 可以在 `--as-of` 之后，用于安排未来用餐；模块只计算已发布的日期事实。

命令不读凭证、HAR 或数据库，不构造查询客户端，不联网、写盘或发通知。成功为 exit 0；输入、资源或时区数据错误为 exit 1，仅输出固定错误码。输出含操作者明确输入的目标/信息时刻及当地时间，若用于真实个人日程，应留在私有环境；不能直接把完整输出发送给大模型或公共数据集。

## 信息在当时是否可用

`--as-of` 表示预测使用信息的时刻，不能省略。当前仅使用公告的发布日期，保守设置可用边界为 **公告次日当地 00:00**（2025-11-05T00:00:00+08:00）；不声称这是真实上线或网络可达的精确秒数。

| calendar_status | date_type | 含义 |
| --- | --- | --- |
| available | holiday / makeup_workday / ordinary_workday / ordinary_weekend | 目标年份已核对且公告在该信息时刻可用 |
| not_yet_published | unknown | 已核对年份，但信息时刻早于可用边界；不借用未来公告 |
| outside_coverage | unknown | 年份未核对，仍可计算星期/月/时段，但不把周末当普通周末 |

来源元数据将 `published_on`、`available_after` 和 `reviewed_on` 分开，`knowledge_basis=official_publication_reconstruction`。它允许研究者重建当时已公开的事实，**不证明本项目在过去已经读取、保存或使用该版本**。真实生产回测仍需实际特征可用日志/版本和数据接收时间；不能把 2026-10-04 的人工核对记录伪装成 2025 年运行日志。公告出现修订时需新增版本，不能覆盖旧文件后仍称回测完全可复现。

## 字段与实际限制

- `iso_weekday` 为 1–7；`is_weekend_by_weekday` 只表示周六/周日，与 `date_type` 分开。
- `minute_of_day` 为当地一天的第几分钟，`month` / `quarter` 为当地日历值；不是门店午晚餐时段或预测系数。
- `calendar_season` 按 12–2 月冬、3–5 月春、6–8 月夏、9–11 月秋定义，只是日历代理，不能推成都/北京气温或客流变化。
- `holiday` 仅在放假区间有名称、从 1 开始的 day_index 和 length_days；`makeup_for` 仅在调休日期有对应名称，其余为 null。
- `store_open_status=unknown`、`eta_available=false`、`network_performed=false`；日期特征不补全号码、源时间、过号事件或真实标签。

使用标准库 [zoneinfo](https://docs.python.org/3/library/zoneinfo.html) 与系统 IANA 时区数据。缺失 `Asia/Shanghai` 时明确报 `calendar_timezone_unavailable`，不退回猜测偏移；本项目未新装 tzdata 或自动联网获取它。固定公开 JSON 作为包资源随 wheel 安装，安装检查在 checkout 外验证，做法依据 [setuptools 包数据说明](https://setuptools.pypa.io/en/latest/userguide/datafiles.html)。

当前主库 219 份成功快照经只读重建，全部为两天国庆假期、同一月份和日历秋季，数据库字节未改；这仍不是全年规律或 219 次真实排队结果。已核对结果、剩余接入条件和完整历史见 [DATASET_DESIGN.md](DATASET_DESIGN.md)、[V0_2_ACCEPTANCE.md](V0_2_ACCEPTANCE.md) 与 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。
