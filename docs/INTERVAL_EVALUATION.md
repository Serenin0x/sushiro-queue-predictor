# 记录中的叫号区间与误差计算

rc13新增 `interval-evaluate`，离线计算已有预测记录与叫号区间的误差界、覆盖界和平均区间宽度。它核对记录格式和算术，不生成预测、不认证记录真实性、不审核训练标签，也不证明模型准确率。真实标签仍0；正向、反向模型的进入条件见[预测评估方案](PREDICTION_EVALUATION.md)，本人结果记录见[OUTCOMES](OUTCOMES.md)。

## 输入与运行

只读取明确指定的一个私有JSON文件，最多16KiB。POSIX文件必须属于当前用户、0600、单硬链接，直接父目录0700，路径不能经过符号链接；不能直接使用公开样例的默认权限。先在明确的私人目录复制[合成样例](../examples/fixtures/evaluation-01.synthetic.json)，调整权限后运行：

```sh
python -m sushiwait interval-evaluate --input /absolute/private/evaluation.json
```

使用当前环境的已安装包，或按[数据手册](DATA_ACCESS.md)配置源码入口。命令不会搜索文件、读取查询配置、访问门店、启动子进程或控制微信/Surge；不会向LLM传输输入或结果。输出也是私人资料，尤其是小样本聚合，不能当作匿名数据自动公开。

根对象严格包含 `schema_version`（整数1）、`as_of`、`data_origin`和`records`。来源仅允许 `synthetic` 或 `self_reported`，一次输入只使用一种来源。库函数最多10,000条，文件入口另有16KiB限制；实际可装入条数取决于文件长度。空列表的误差、覆盖和宽度为null。

每条严格包含以下字段：

| 字段 | 语义 |
| --- | --- |
| prediction_id | 本次预测的标准小写UUID4；同一输入不得重复 |
| prediction_made_at | 声称作出预测的时刻 |
| point_call_at | 声称预测的叫号时刻 |
| predicted_call_lower / predicted_call_upper | 声称预测区间[P,Q] |
| observed_call_lower / observed_call_upper | 声称实际叫号区间[L,U]；精确事件两端相同 |
| observation_received_at | 声称收到结果记录的时刻 |

时间使用含时区的ISO格式，必须包含秒，可含最多六位小数；统一为UTC比较。核对 `made ≤ P ≤ point ≤ Q` 及 `made ≤ L ≤ U ≤ received ≤ as_of`。即使叫号已发生，错误预测的目标可以晚于as_of，这仍是有效的大误差；不能因预测落在未来就丢掉它。未知字段、重复JSON键、重复预测、非法来源/时间、时间逆序或任一坏行使整个请求失败，不输出部分分数。固定错误码不包含原始行、标识或具体时刻。

## 计算结果

对点预测p，绝对误差下界为p到[L,U]的距离，上界为 `max(|p-L|, |p-U|)`。整数微秒累计，均值以秒保留最多六位小数；小数是计算精度，不能提高观测精度。L=U才有唯一绝对误差，不把区间中点当真值。

预测区间完全包含[L,U]时确认覆盖；相交时可能覆盖；不相交则确认未覆盖。共享一个端点计为可能覆盖。汇总覆盖下界是确认覆盖数量/总数，上界是可能覆盖数量/总数，同时报告预测和观测的平均宽度。覆盖上界包括确认覆盖，不是另一个互斥类别。

公开合成样例包含两条虚构记录，算术结果为平均绝对误差60–210秒、覆盖0.5–1；这是检查样例，不能引用为产品成绩。输出仅聚合数量/区间/宽度及证据状态，不返回单条UUID、门店、时刻或号码。所有记录仅为待核验的声明：`authenticity_verified=false`、`forecast_log_verified=false`、`model_performance_verified=false`、`verified_training_labels=0`、`eta_available=false`。

## 尚未覆盖的评估工作

输入只支持已经有叫号区间的记录，不处理未完成、取消、重排等删失结果；工具不能证明提交了完整总体，也不能防止调用者挑选结果。它没有审核依据、不可变预测日志、训练/校准/测试划分、分组覆盖、分位误差、名义置信水平或真实性能认证。先完成独立审核和可追溯资料契约，再按[DATASET_DESIGN](DATASET_DESIGN.md)和预测评估方案构建真实回测；不得把格式通过、区间算术或合成检查作为真实准确率。

本机32项专项与完整517项检查已通过，安装结果和实际公开/CI随本批交接记录更新；原26个JS场景已包括在完整套件中，不重复计数。软件检查与营业字段、长期凭证供应及真实预测验收分开。
