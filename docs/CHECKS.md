# 自动检查与安装验证

工作流为 [.github/workflows/checks.yml](../.github/workflows/checks.yml)，本机安装包检查入口为 [scripts/check_installed_package.py](../scripts/check_installed_package.py)。每次 main 推送、针对 main 的普通 pull_request，以及显式 workflow_dispatch 运行离线检查；没有定时采集、业务操作、部署或凭证更新任务。

Ubuntu 24.04 的 Python 3.11、3.12、3.13 三组分别执行完整检查，Node 22 执行既有适配器场景。每组再构建 wheel、安装到临时虚拟环境，在 checkout 外用 `-I` 运行安装验证：核对版本与实际导入位置、CLI 帮助、合成回放及只读报告；rc2另核对合成结果校验/私有追加/汇总，真实性/标签资格仍为未验证。回放/报告及结果路径将 socket 调用设为失败，数据库只在临时目录；CLI 帮助子进程单独核对退出状态。它不访问寿司郎，不读 HAR、微信数据或本机凭证，也不把合成测试算作 live 验收。

权限仅 contents:read，checkout 不持久化授权；没有项目 secrets、OIDC、发布权限、pull_request_target、生产服务器或 artifact 上传。测试使用标准库；构建所需 setuptools 由标准构建隔离取得，项目运行依赖仍为空。工作流十分钟超时，较旧同分支检查可取消；这不是采集器的停止/重试策略。

2026-10-04 按官方仓库实际标签解析到不可变 commit，并核对该 commit 的 action.yml 输入：checkout v7 `3d3c42e5aac5ba805825da76410c181273ba90b1`，setup-python v7 `5fda3b95a4ea91299a34e894583c3862153e4b97`，setup-node v6 `249970729cb0ef3589644e2896645e5dc5ba9c38`。动作自身使用 Node 24；项目适配器测试另用 Node 22。后续更新须重查来源与实际 CI，不把可变标签自动漂移作为验证。

来源：[GitHub 工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[checkout 固定动作](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/action.yml)、[setup-python 固定动作](https://github.com/actions/setup-python/blob/5fda3b95a4ea91299a34e894583c3862153e4b97/action.yml)、[setup-node 固定动作](https://github.com/actions/setup-node/blob/249970729cb0ef3589644e2896645e5dc5ba9c38/action.yml)。本机、GitHub 实际运行及真实接口结果分别记录在 [V0_2_ACCEPTANCE.md](V0_2_ACCEPTANCE.md) 和 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。

2026-10-04，提交3279059的[实际工作流运行](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37196578650)已completed/success，三组均261项检查及构建/checkout外安装通过。普通HTTPS推送因既有PAT缺workflow scope被拒绝，远端当时未移动；使用现有GitHub连接以force=false更新同一提交后成功，不新增凭证或权限授权。完整失败/发布核对见编辑历史E0065；这些离线成功不证明真实续期或服务器可持续运行。

rc2构建时按[PyPA许可元数据格式](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license-and-license-files)采用SPDX MIT/显式LICENSE，最低构建后端setuptools77.0.3；运行依赖仍0。最终本机wheel13个模块与src一致，checkout外合成结果路径也通过socket0检查。283项本机已通过；rc2提交a728bf93cb4e1b5dd98de97d02cebe06461aa97c的[实际运行37199591656](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37199591656)completed/success，Python3.11、3.12、3.13分别283项/2.596秒、2.553秒、2.291秒，均OK，三组构建/checkout外安装步骤success。结果路径socket0、已验证训练标签0；未发生产请求，没有正式版本或部署。
