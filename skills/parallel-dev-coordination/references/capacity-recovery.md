# 模型容量故障恢复

此机制处理明确的“模型繁忙／容量不足”。额度耗尽、速率限制、网络断开、上下文超限、用户暂停、等待审批及没有下一批任务，分别处理，不能自动归类为容量故障。

## 能力与适用边界

`scripts/capacity_recovery.py` 是 Python 3.10+ 标准库宿主程序。它不请求模型来判断是否恢复，也不读取认证令牌。它只通过 `codex app-server proxy --sock <既有控制socket>` 连接**已在同一宿主加载的、明确授权的独立对话**，使用官方 JSON-RPC 接口。它不恢复子智能体，不创建对话，不建立另一套 app-server，不重启桌面，不安装服务或定时任务。

先运行 `--check`。接口缺失、socket 不通、目标未加载或状态无法核实，均停止并说明未接入；不得另外启动一个 app-server 读同一 rollout，冒充控制原桌面对话。桌面使用专属 stdio 且未公开可用控制 socket 时，此入口不能接管该桌面。需要宿主提供受支持的共享连接或由宿主直接集成恢复逻辑；不能仅靠 skill 解决。

代码按 Codex `0.164.0-alpha.11.1` 的本地生成 schema 核对。离线故障注入与真实 stdio 模拟器通过，不表示所有桌面版本可连接，也不表示真实容量故障已恢复。2026-10-26 的本机只读预检显示默认控制 socket 不可连接，运行中的桌面进程使用专属 stdio；未向开发对话发起恢复请求。

官方接口依据：[App Server](https://learn.chatgpt.com/docs/app-server)。[Agents API 错误恢复原则](https://developers.openai.com/api/docs/guides/agents-api/errors)可用于解释恢复策略；不要把该 API 的错误结构直接当成 Codex 的协议。

## 行为

1. 观察最新一轮的有界摘要、运行状态、Goal、队列和后台终端。只有已终止且明确容量失败的轮次进入恢复；`error.willRetry=true` 或当前轮次仍运行时，不叠加外部重试。
2. 优先遵守可获取的 `Retry-After`，否则指数退避加抖动。默认 60 秒起，指数基数上限 300 秒；抖动只增加等待。默认最多增加 3 轮，故障完成后 15 分钟截止。配置的原生失败尝试上界也占用恢复预算，包含原失败轮的预留量。
3. 第一次仍使用原模型。再次明确容量失败才允许使用配置的 `gpt-6.1-sol`，保持原推理强度。配置未列出的模型绝不使用；模型锁定的对话只重试原模型。目标模型不支持该推理强度或不在当前目录中时转人工处理，不降档。目录列名不证明实时容量充足。
4. 恢复前核对批次、交接哈希、Goal、实际模型设置和最新轮次。发现新输入、Goal 改变、暂停、预算／额度限制、在途终端或状态不明，取消接续。默认只自动恢复本轮尚未执行任何工具的失败；出现工具／文件／后台动作时必须经过项目结果核对适配器。
5. 发起接续前写入持久化意图；超时、断线或崩溃后该发送结果标为未知，不自动重发。`clientUserMessageId` 用于追踪，不假定服务器保证幂等。收到新轮 ID 只算请求接受；后续独立读回模型、推理强度和轮次结果，仍不把完成一轮当成项目验收通过。
6. 同一用户、socket 和对话使用 OS 文件锁；所有执行者须共享唯一恢复所有者。`turn/start` 没有“只在指定旧轮仍为最新时执行”的原子条件，因此正式启用必须约定此时由恢复入口独占派发；不能与其他自动派工器竞写。发送前再读状态可缩小竞态，不能消除人工在同一瞬间输入的竞态。
7. 达到预算、截止时间、全部获准模型失败，进入持久化冷却并输出一次通知。冷却到期不自动再试；同一批的本轮恢复成功也结束该恢复事件。人工核对后才可以重新布防。程序重启不重置既有次数、时间或发送意图。

`nativeFailureAttemptUpperBound` 是**单次失败请求的客户端重试链**上界，应按实际版本、provider 配置及源码核实；不是整轮所有模型调用数量。`reservedFailureAttempts` 是保守预算预留，不是抓包得到的实际次数。成功工具循环、隐藏请求和网络字节不由此预算计量。没有可靠上界时不启用执行模式。不能声称本程序精确限制整轮上传字节或账户总调用数。

## 配置与运行

配置是本机授权记录，放在受控任务目录，勿发布真实对话 ID、私有路径或批次内容。示例数值只是格式说明，必须填写实际授权、当前批次、有效交接和已核实的原生重试上界。

```json
{
  "version": 1,
  "policy": {
    "maxRecoveryTurns": 3,
    "maxFailureAttemptAllowance": 120,
    "deadlineSeconds": 900,
    "baseDelaySeconds": 60,
    "maxDelaySeconds": 300,
    "cooldownSeconds": 1800,
    "pollSeconds": 60
  },
  "threads": [{
    "threadId": "authorized-existing-thread-id",
    "authorized": true,
    "exclusiveRecoveryOwner": true,
    "model": "gpt-6-astra",
    "effort": "high",
    "modelLocked": false,
    "fallbackModels": ["gpt-6.1-sol"],
    "batchId": "current-batch",
    "handoffPath": "/absolute/task/handoff.md",
    "handoffSha256": "actual-sha256",
    "nativeFailureAttemptUpperBound": 30,
    "nativeRetryEvidence": "verified version/config/source receipt"
  }]
}
```

`authorized` 必须来自人类对这些目标的恢复／跨对话通信授权，不能由观察者自己填写以取得权限。固定模型者设置 `modelLocked: true` 和 `fallbackModels: []`。原模型已是 `gpt-6.1-sol` 时去重，不增加一条虚假的备用路线。

从技能目录运行；下面路径替换为本机真实路径，Windows 使用绝对 Windows 路径：

```text
python scripts/capacity_recovery.py --config /task/recovery.json --state-dir /task/recovery-state --codex /path/codex --sock /existing/control.sock --check
python scripts/capacity_recovery.py --config /task/recovery.json --state-dir /task/recovery-state --codex /path/codex --sock /existing/control.sock --duration-seconds 900
```

第二条默认只观察，不订阅或发起续跑。授权和能力检查通过、派发所有权落实后，才加 `--execute`。执行模式订阅既有运行时，不覆盖 Goal、审批、沙箱、权限、工作目录或服务档位。进程运行时间有限，最大 6 小时，不自启、不自动重连、不自动续期。需要后台启动时由原宿主用隐藏窗口启动；临时旁支不能安排持续监控，应回主聊天完成启用。

`state-dir/stop.request` 停止整次观察；`state-dir/<threadId>.pause` 禁止该对话接续，配置变化也停止运行。删除暂停文件不会自动解除持久化的暂停决定。不要删除发送结果未知的状态来“重试”。退出只关闭自己创建的 proxy，不结束已发出的开发轮次或共享 app-server。

`state.json` 保存预算、状态和请求身份；`notifications.jsonl` 与标准输出只在状态变化时记录有界元数据。宿主应把 `attention`、`uncertain`、`cooldown` 接入现有通知入口。脚本本身不发送邮件、聊天消息或桌面弹窗；不能把本地日志写入当作用户已收到提醒。`sent` 的模型是请求值，`done` 才包含后续运行设置核对；功能验收另记。

重新布防必须停止旧进程，检查原事件是否已执行、当前用户要求、预算及暂停状态；保留旧目录作为证据，使用经审核的新批次配置／状态目录。不得用新目录绕过仍适用的冷却、次数上限、未知结果或用户停止。

## 已执行动作的核对适配器

如果失败轮已经调用工具，配置可增加 `reconcileCommand`，值为可信的本机只读程序 argv（首项为绝对可执行文件路径，不经过 shell）。程序接收一行 JSON：`threadId`、`turnId`、`batchId`、`handoffSha256` 及只有 ID／类型／状态的 `actions`。它必须检查真实文件变更、当前归属、测试进程和输入租约，不能只信聊天说“已完成”，也不能回放原命令。最大运行 10 秒，输出不超过 64 KiB；不要派生后台进程。

只有返回相同身份与哈希，同时包含以下值才允许接续：

```json
{
  "turnId": "the-failed-turn",
  "batchId": "current-batch",
  "handoffSha256": "actual-sha256",
  "safe": true,
  "runningOperations": [],
  "pendingApprovals": [],
  "ownershipValid": true,
  "changesReconciled": true
}
```

没有适配器或任一检查失败时保留已完成结果，转人工核对，不为提高“恢复率”绕开这一条件。项目适配器仍需独立的真实验证；仓库测试中的模拟适配器仅验证协议和拒绝行为。

## 验证与交付

仓库根目录执行 `python -B -m unittest discover -s tests -v`。测试不连接真实聊天，不使用模型／网络，覆盖错误分类、原生重试等待、退避、额度／Goal／暂停、模型锁与强度、持久化、竞态、已执行动作、RPC 边界和通知去重。

交付分别说明：代码测试、既有宿主连接、授权和独占派发、项目结果核对、真实容量故障后接续、后续产出验收。只完成前两项也不能写“主线程停工问题已解决”。不故意让真实开发任务过载来制造验收事件。
