# 容器日志统一时间戳设计

## 目标

容器输出的 entrypoint、Python 服务、fetcher、traceback、nginx 和 supervisor 日志均能从每一行开头看到 Compose `TZ` 对应的 ISO 8601 时间和服务名。

## 格式

```text
2026-08-09T20:15:32+08:00 [web] request failed
2026-08-09T20:15:33+08:00 [refresh] fetcher: cycle complete
2026-08-09T20:15:34+08:00 [nginx] 127.0.0.1 "GET /health HTTP/1.1" 200
```

Compose 使用 `TZ=${TZ:-Asia/Shanghai}`。镜像显式安装 `tzdata`，防止 slim 镜像缺少 zoneinfo。

## 设计

新增 `timestamp_filter.py`，从 stdin 逐行读取，使用 `datetime.now().astimezone().isoformat(timespec="seconds")` 生成本地偏移时间，添加 `[service]` 后立即 flush。EOF 前的非换行尾段也必须输出；空行同样带前缀。

Supervisor 使用 `/bin/bash -o pipefail -c` 把 web、refresh、nginx 的合并 stdout/stderr 送入 filter。`stopasgroup/killasgroup` 保留，pipeline 失败必须让 program 非零退出并由 supervisor 重启。fetcher 输出由 refresh_server 逐行转发并带 `fetcher:` 标识，因此最终得到 refresh 时间前缀且不会被无限 capture。

nginx 自定义 access log 去掉自带 `$time_local`，由外层 filter 统一加时间；error log 也走同一 stderr pipeline。Supervisor 自身保留其原生本地时间格式。entrypoint 在 supervisor 启动前使用 shell `log()` 输出同样的 ISO 8601 格式。

## 验收

- `TZ=Asia/Shanghai` 和另一个测试时区下，时间偏移随环境变化。
- Python 普通 print、logging、多行 traceback 每行都有前缀。
- nginx access/error 均有统一前缀，且不重复两个时间戳。
- 子进程非零和 filter 非零都能传递到 supervisor；停止容器不留下孤儿进程。
- `docker compose logs` 不依赖 `--timestamps` 即可看到时间。
