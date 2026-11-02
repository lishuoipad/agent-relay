# 本机开发状态面板 / Local development status panel

Windows PowerShell 7.5+ / WPF 与 Python 3.10+；仅使用 Python 标准库。这是独立可选查看工具，不随技能安装自动运行，不负责派工或恢复任务。

## 启动

1. 将 `project.example.json` 复制为 `project.local.json`。
2. 填写自己的数据库、检查点目录、现有任务计划和对话 ID。相对路径以配置文件目录为基准，`~` 表示自己的用户目录。
3. 运行：

```powershell
pwsh -NoProfile -STA -File ./Android-Dev-Status.ps1 -ConfigPath ./project.local.json
```

Python 不在 PATH 时增加 `-PythonPath /absolute/path/to/python.exe`。也可运行 `open-status.cmd project.local.json [python.exe]`；不安装解释器或修改执行策略。

先只读检查数据、不打开窗口：

```text
python -B development_status.py --config project.local.json --once
```

必须显式指定配置。示例对话 ID 是占位符，不自动选中其他聊天。个人配置可放在仓库外；工具目录的 `*.local.json` 已忽略。不要提交实际聊天记录、SQLite、rollout、检查点、玩家数据、截图回执或个人配置。

## 展示与关联

每个已配置对话及有明确父子记录的子智能体单独一行，显示计划、最近公开工作、记录状态和说明时间。历史子智能体默认收起；选中一行可展开公开原文、检查点下一步及验证范围。暂停、交付和本轮结束分别显示；证据不足显示未知。每5秒读取进展，每30秒更新登记；这是有界本地读取，不是实时宿主状态。

`rules` 是有序正则规则，首条匹配生效；每项的 `summary`、`plan_ids`、`validation` 只描述关联。`friendly_plan` 改显示名称，`text_replacements` 简化摘要文字，`plan_id_pattern` 识别明确编号。规则不改变计划、派工或验收。计划格式为 `tasks` 数组，任务含 `id/title/status/acceptance`，可递归含 `subtasks`。检查点沿用 `task/updated_at/next_actions`。

## 读取边界与限制

- 只读本机 Codex `state_5.sqlite` 登记及配置目标的记录，不读认证文件，不写 Codex 数据库。
- 只保留公开助手说明及任务开始/结束事件，不展示内部推理、用户提示或工具输出。主对话首次最多读尾部2MiB，子智能体256KiB，随后增量读取。最多120条、深度4，达到上限标示可能不完整。
- 数据库与 rollout 格式依赖 Codex 版本，并非稳定官方接口。只读连接失败时可退到 immutable 磁盘快照，新登记可能延迟；字段不兼容或记录缺失会提示读取失败，不能据此判断开发停止。
- 状态与计划关联来自已落盘记录及自然语言规则，可能落后或使用上一项上下文；它不是精确实时状态、确认的派工或阶段验收。检查点措辞不匹配时状态可能未知。
- 不调用模型、不联网、不发消息、不唤醒对话、不操作手机或测试服。关闭窗口只退出它自己启动的读取进程，不创建后台服务、定时任务或开机自启。

源自实际安卓开发面板；发布版将机器路径、对话绑定和项目文字规则移至本地配置。原本机窗口由其维护者继续管理，复制本工具不会接管它。

## 验证

仓库根目录运行：

```text
python -B -m unittest discover -s tests -v
```

用自己的可读数据源验证屏幕外渲染：

```powershell
pwsh -NoProfile -STA -File ./Android-Dev-Status.ps1 -ConfigPath ./project.local.json -SelfTest -CapturePath /registered/output/panel.png
```

此模式在屏幕外渲染、保存证据后自行关闭。用合成数据测试通过，只证明对应配置、读取和显示用例，不代表所有后续工作均关联正确。
