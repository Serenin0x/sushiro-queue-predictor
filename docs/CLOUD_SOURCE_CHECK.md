# 手动云端匿名来源检查

候选 0.2.0rc35 新增独立手动工作流，用一次小范围查询区分“本机 Docker 能访问”与“云端 Linux 网络能访问”。它检查当次连通性，不是生产服务器部署、长期采集、源新鲜度或预测验收。实际运行编号、提交和结果追加于 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。

## 范围与数据处理

固定中关村大融城 3014、西单大悦城 3004、成都世豪广场 2009，每店只查一次 groupqueues 与 storequeuecount，全部成功最多 6 次真实 GET。使用既有匿名 RemoteClient：固定 HTTPS 目的地、证书校验、单次尝试、禁止跳转，不读取微信凭证、不取号、不取消。

任何一店失败立即停止，前一队列查询失败时跳过该店数量查询，也不继续其他店；没有重试或自动重跑。工作流总时限 4 分钟，普通 push/PR 检查不会调用它；没有定时事件或输入参数。

原始号码、数量、响应体与头只经过内存，程序不写数据库或原始数据文件，不输出它们，也不上传数据 artifact。日志只含固定店 ID、版本、Linux 系统名、成功/失败数、HTTP 状态、请求计数与耗时。异常只输出固定错误码；若无法核对已开始查询的完整记录，request_accounting_complete=false、unrecorded_http_attempts=unknown，不把缺失计数说成零请求。

## 正常手动运行

1. 在自己的 GitHub 仓库打开 Actions → Manual cloud source check。
2. 点击 Run workflow，选已审计的 main，执行一次。
3. 记录 workflow_dispatch 运行 ID 和完整 head_sha，读取 One fixed round 步骤的聚合结果。失败先分析已有证据，不反复触发。

工作流先构建当前 wheel，安装到独立环境，再用 `python -I` 执行检查。VERSION 与已安装包版本不一致时，在创建客户端前停止。此命令只适用于明确准备好的 Linux 验收环境，执行会访问真实来源：

```sh
python -I scripts/check_cloud_access.py --version-file VERSION
```

普通软件检查 `tests/test_cloud_access.py` 使用合成传输并禁止 socket/凭证读取，验证成功、首错停止、后续错误、非 Linux、异常与坏记录；合成的 6 次请求不能冒充真实云端结果。

## 通过后的含义

ok=true 需要三个完整合法记录、三店成功、6 次 GET 全部 200。它只证明指定提交在该次 GitHub 托管 Linux 执行器的网络出口可以查询；没有反向代理、公网用户入口、持久服务或私人服务器身份。

response_store_identity_verified=false、count_unit=unknown、source_freshness=unknown、continuous_collection_verified=false、production_deployment_verified=false、verified_training_labels=0、eta_available=false 始终保持。重复号码既可能真实未变，也可能源迟滞，本工具不认定实时程度。

后续仍按 [Linux 部署](LINUX_DEPLOYMENT.md)、[接入验收](V0_2_ACCEPTANCE.md) 完成明确主机、24/72 小时与跨日期质量、字段与页面近时对照、HTTPS/访问控制、实际结果标签与预测误差。微信前端在 R18 实时数据条件满足后开发，不要求整个 v1.0 先完成，但本检查单独通过不解除该条件。

依据：[GitHub 手动工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。工作流须存在于默认分支，并由有相应仓库权限的操作者正常触发；本项目不修改账号权限或保存账号凭据。
