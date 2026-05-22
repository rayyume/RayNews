# RayNews 📡

新闻聚合阅读器——从 Telegram 频道（配合 BroadcastChannel）抓取新闻消息，自动提取 Telegraph 全文，生成暗色模式新闻站。

![screenshot](https://github.com/user-attachments/assets/a5f64a6f-f3c2-4aaf-9c09-cb51fd0e0349)

## 架构

```
新闻源 (RSS/网页/API)
       ↓
 BroadcastChannel ──推送──→ Telegram 频道 (t.me/s/your_channel)
                                    ↓
                      RayNews Fetcher ──定时抓取──→ news.json
                                    ↓
                              Nginx + 前端 Vue SPA
```

**数据流说明：**

1. **BroadcastChannel** —— 用开源工具 [BroadcastChannel](https://github.com/ccbikai/BroadcastChannel) 将 RSS/新闻源聚合后推送到一个 Telegram 频道
2. **Telegram 频道** —— 作为中间存储，RayNews 从此频道的公开页面（t.me/s/频道名）抓取消息
3. **RayNews Fetcher** —— Python 脚本，每 15 分钟增量抓取新消息，自动识别来源、提取 Telegraph 全文
4. **前端** —— 纯 Vue 3 SPA，暗色风格，支持来源筛选、文章详情、分享

## 前置条件

- 一个 Telegram 频道（公开或私有均可）
- [BroadcastChannel](https://github.com/ccbikai/BroadcastChannel) 部署好，将你的新闻源推送到该频道

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/rayyume/RayNews.git
cd RayNews
```

### 2. 配置

```bash
# 复制环境配置模板
cp .env.example .env

# 编辑 .env，填入你的 Telegram 频道名称等配置
```

### 3. 启动

```bash
docker compose up -d
```

容器启动时自动抓取一次，之后每 15 分钟刷新。

### 4. 访问

- 前端: `http://<your-ip>:8090`
- 手动刷新 API: `http://<your-ip>:8090/refresh`
- 数据 API: `http://<your-ip>:8090/news.json`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL` | `your_channel` | Telegram 频道名称（必填） |
| `DATA_DIR` | `/app/data` | 数据输出目录（容器内路径） |
| `HTTP_PROXY` | (空) | HTTP 代理（如需翻墙） |
| `HTTPS_PROXY` | (空) | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 直连白名单 |

## 自定义构建

```bash
# 本地构建镜像
docker compose build

# 或直接使用 ghcr.io 预构建镜像
docker compose pull
```

## 技术栈

- Python 3.12 (fetcher + refresh server)
- Nginx (静态服务)
- Vue 3 (前端，纯静态 SPA)
- BeautifulSoup (HTML 解析)
- Supervisor (进程管理)

## License

MIT
