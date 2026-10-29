# 交付引用的只读核验

已有交付 JSON 重复使用嵌套 `{path, sha256}` 引用时，使用 `scripts/verify_delivery_refs.py`，省去每次重新编写递归哈希循环。它只核对这一个既有格式；不猜测其他清单、不递归打开引用内的 JSON，也不扫描目录。

```text
python /installed/skill/scripts/verify_delivery_refs.py /existing/task/delivery.json --expected-sha256 <frozen-delivery-sha256> --report /existing/task/evidence/new-ref-check.json
```

调用前从已确认的交付记录取冻结哈希，不能把刚读取到的任意新哈希当成已批准版本。报告写入既有任务输出目录的一个新文件，禁止覆盖已有证据。主线程将报告路径/哈希和实际范围挂回原接收记录；执行者也可在正式交付后使用同一入口自检，无需新增账本或Goal。

- 遍历所给 JSON 中的 `path+sha256`，规范化绝对本地路径，相同引用只读一次；同路径冲突哈希、缺失/改变文件、清单变动或格式错误均失败。不会联网读取UNC路径。
- JSON读取上限沿用现有2MiB；默认引用出现次数1024、完整文件读取总预算128MiB，仅为本地读取保护，可按真实交付规模显式传 `--max-refs` / `--max-bytes`。这不是交付验收阈值。超过预算不跳过通过。
- stdout只给有界摘要、失败项截断标志、完整报告路径和落盘字节哈希。完整报告区分已检查文件与未完成读取；`completedReadBytes`不含失败中途无法确定的读取量，也不是网络流量。返回0仅表示列出的引用内容一致，非0须查看具体失败。
- 不安装文件、不提交Git、不调用模型、不操作设备，不判定业务正确、视觉通过、模块接收或阶段完成。仍需原有diff/语义审查和受影响范围验收；只复用输入及条件一致的既有结果。
