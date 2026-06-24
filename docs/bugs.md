# 已修复的 Bug 记录

记录历史 Bug 的根因与修复，防止重犯。

## 客户端断开后上游连接泄漏

**症状**：客户端超时重试后，vllm-server 上的原始请求仍在持续生成（长达十几分钟）。

**根因**：`forward()` 中 `BrokenPipeError` 被捕获后只调用了 `_finish_log()`，上游 `resp` 连接从未关闭 → vllm-server 不知道客户端已走，keep-alive 连接持续输出 SSE。

**修复**（`claude_api_proxy.py`）：
- 流式响应 `BrokenPipeError` 分支增加 `resp.close()` — 立即关闭上游 TCP 连接
- `HTTPError` 分支增加 `e.close()` — 防御性关闭
- `HTTPError` 回写客户端也加 `BrokenPipeError` 处理

**原理**：`resp.close()` 关闭 TCP 读端，vllm-server 下一次 SSE 输出时写入已关闭连接 → 收到 SIGPIPE/RST → 中止生成。

## writer 线程空转导致 CPU 100%

**症状**：代理启动后无流量，CPU 100% 持续。

**根因**：`log_store.py` 的 `_collect_batch()` 用 `queue.get_nowait()` 检查空队列，失败后立即返回，writer 线程 `continue` 重新调用，每秒循环数十万次——纯空转。

**修复**：改为 `queue.get(timeout=2.0)`，队列为空时线程阻塞等待 2 秒，`put()` 到来时自动唤醒。无流量时 writer 线程零 CPU。

**注意**：生产进程重启后生效。不要随意 kill 生产进程——用独立端口（8003）做测试验证。
