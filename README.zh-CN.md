# team-upstream-watch：团队协作的开源更新跟踪器

**上游统一查一次，每位成员分别看影响、记处理结果。**

适合几个人或几个 AI 成员一起使用开源项目的小团队。它把公开来源的观察放在一个本地数据库里，成员各自查看与自己有关的变化。一位成员确认处理，不会替另一位把待办清掉。

[English](README.md) · [数据与运行](docs/operations.md)

## 解决什么问题

例如两位成员都用同一个库：Alice 关心 API 兼容性，Bob 关心部署。不要让他们各自反复检查同一个仓库；给这个来源登记一次，两人都订阅它。出现变化后，两人各自判断是否影响自己的工作，再记下结果。

这个工具区分：
- **观察到变化**：公开版本、提交或 PR 内容变了；
- **没有读全／读不到**：明确显示部分覆盖或未知，不能当成没有变化；
- **成员处理了**：只是一条本地处理记录，不等于同意升级或已经采用。

## 怎样使用

需要 Python 3.11 以上，无需 pip 包或模型账号。Git refs 和备用 HEAD 读取另需安装 Git。将 `examples/team.json` 复制到自己的工作目录，再填写公开来源和成员代号；配置里的相对状态目录以配置文件所在目录为起点。

```sh
python team_watch.py validate --config C:/my-team/team.json
python team_watch.py tick --config C:/my-team/team.json --allow-public-network
python team_watch.py inbox --config C:/my-team/team.json --member alice
```

macOS/Linux 把配置路径换成自己的绝对路径。一次 `tick` 只做一批到期观察，不会偷偷创建定时任务。需要自动检查，可交给自己现有的调度器；不要求 Hermes 或其他特定 Runner。

看完事件后，用收件结果里的真实事件 ID 确认：

```sh
python team_watch.py ack --config C:/my-team/team.json --member alice --event-id EVENT_ID --receipt "已看过，这次不升级"
```

同一成员用相同结果重复确认不会多记；不同结果不会直接覆盖旧记录。Bob 的待审仍独立保留。

## 首版支持范围

可配置 GitHub 仓库、最新正式 release、指定 PR 及其评论／评审／准确 head 的 workflow；也支持 Git refs、有限 Atom feed，以及 Hugging Face 模型 revision 元数据。它不下载模型或上游源码、不运行安装器、不自动升级。具体观察范围和分页限制见[英文说明](README.md)。

**同一个 source ID 给多人使用，才是共享采集。** 不同定义或重复登记为多个 ID 仍各自观察，不能把它们当作一次网络请求。

配置拒绝重复 JSON 键、`state_root` 中的 NUL，以及对应 provider 在结构上不接受的 URL。GitHub API 类来源（`repository`、`release`、`pull`）复用现有 API provider 的 URL 规则；Git refs、Atom 和 Hugging Face 保留各自支持范围。团队配置**不支持**内部 `migration_hold` 键，写成 `false` 也会拒绝；首版不提供状态迁移或暂停／恢复功能。

## 不假装已经做好的部分

- 首版提供本地 CLI 收件与独立确认，**没有自动唤醒成员或发送聊天通知**。
- 这是互信本地协作工具，不是带登录认证、成员强隔离的云平台。数据库可读写的人能够绕过成员筛选。
- 数据库和成员结果留本地；不包含原 WE 的真实成员、生产配置、历史运行库或凭据。
- 源的实际检查时间与本轮运行时间分开；旧缓存不因新读收件箱就变成刚检查。
- 本地测试不证明长期在线、全部平台兼容或任何权益申请资格。

## 测试与参与

在仓库根目录分别运行核心与团队层：

```sh
python -B tests/core/run_tests.py
python -B -m unittest discover -s tests -p "test_team_workflow.py" -v
```

测试使用合成数据，不需要真实成员或账号，不使用原部署和定时配置。核心入口用 Python audit guard 阻止意外联网和未计划子进程；这不是安全沙箱。可设置绝对路径的 `TEAM_WATCH_TEST_ROOT` 指定临时测试目录。

CI 已配置 Ubuntu/Windows、Python 3.11/3.14；配置好不等于这些组合已经实际跑过。问题反馈请只带最小合成复现，不上传 Token、私有配置或真实成员记录。

来源保留：采集器和公开 provider 来自 WE 工程；独立团队 CLI、成员独立回执是后发发行增量。它不代表上游项目或模型的许可证，也不与 GitHub、Hugging Face、OpenAI 或 Runner 厂商关联。
