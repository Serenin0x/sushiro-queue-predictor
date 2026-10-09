# 记录实际用餐经历

rc59新增`ticket-outcome-draft`，把自己已经录入的手动号码会话与本人明确记录的事件连起来。保存签到、首次有效叫号、入座、过号、取消或观察结束；从本人票据复用门店、队列、人数、桌型和episode，省去手写整份结果JSON。草稿不包含号码、理想用餐计划、凭证或微信身份。

这是本机私有CLI及库函数，尚无产品按钮或云端私人入口。运行它是在声明“我观察到了这个事件”；程序不会从号码出现／消失、采集失败、号码差或追踪结束状态中制造叫号结果。输入错误仍需要人工纠正。

## 当时记录与事后补录

省略`--event-lower`和`--event-upper`表示本人报告事件发生在本次记录动作的本机时间，仅适合当时操作。事后补录须同时提供带时区的两个时间；记不清时保留范围，不把现在当过去叫号时间。

已有的`issued_at`作为本人声明的取号时间；不知道取号时间时明确拒绝，不能补为开始追踪或现在。可用`--issued-lower`／`--issued-upper`提供取号范围，已有取号时间必须落在范围内。草稿只在本人票据创建前的取号范围内生成；这项检查不认证票据。

以下均为格式示例，需要使用自己实际事件及私有目录。输出目录与追踪目录分开，父目录0700，输出0600、已有文件不覆盖。

```sh
sushiwait ticket-outcome-draft \
  --state-dir /private/tracking/my-ticket \
  --event-type checked_in \
  --output /private/experience/checked-in.json

sushiwait ticket-outcome-draft \
  --state-dir /private/tracking/my-ticket \
  --event-type called \
  --event-lower 2026-10-09T13:20:00+08:00 \
  --event-upper 2026-10-09T13:22:00+08:00 \
  --previous-file /private/experience/checked-in.json \
  --output /private/experience/called.json
```

`--previous-file`保存此前事件的ID与时间范围，再生成下一份修订；拒绝跨票据、跨门店或改变已有取号范围。它不证明此前草稿已被接收；[首次接收库](OUTCOME_INTAKE.md)在另一次事务中验证真正的修订链和本机接收时间。事件顺序矛盾、未来时间、重复事件和终态后追加事件均拒绝。

## 何时能用于模型

使用既有`outcome-receive`接收每份修订，再按[人工审核](OUTCOME_REVIEWS.md)核对取号／叫号范围、门店和队列。生成草稿不自动写接收库、不自动接受审核、更不提升真实性认证。迟到资料只在实际首次接收之后可用于研究，不倒灌到更早的回测。

程序保留`self_reported`／`synthetic`区别，事件均为`unverified`；真实认证标签0及ETA未校准继续如实显示。后续以正常用餐经历积累数据，比较历史初估、实时调整和AI融合的误差与区间覆盖；门店曲线仍是独立的展示观测。
