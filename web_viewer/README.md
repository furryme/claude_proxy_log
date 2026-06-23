# Web Viewer — SQLite 日志浏览器

在浏览器中优雅地查看代理抓取的对话数据，支持 OpenAI 格式渲染、对话渲染、思考过程折叠查看。

## 快速启动

```bash
python3 web_viewer/api.py --port 8002
```

然后在浏览器打开 **http://127.0.0.1:8002/**

## 功能

- **Dashboard** — 总览统计、模型分布、时间线活跃度
- **时间窗口导航** — 按小时浏览请求列表，支持模型筛选
- **对话视图** — 以聊天界面渲染完整的对话历史
  - 不同角色（user / assistant / system）用不同样式展示
  - Thinking（思考过程）可折叠/展开
  - Tool Use / Tool Result 可折叠查看
  - Markdown 渲染
  - Token 用量统计
- **System Prompt 面板** — 查看完整的系统提示词
- **Tools 面板** — 查看所有可用的工具定义
- **Seq ID 搜索** — 输入 seq_id 直接跳转

## 文件结构

```
web_viewer/
  api.py          # Python HTTP API（读取 SQLite，解压 body/response，SSE 重组）
  index.html      # 前端单页应用
  static/
    main.css      # 样式（深色主题）
    app.js        # 前端逻辑（视图切换、数据渲染）
  README.md       # 本文件
```

## 技术细节

- 纯 Python stdlib 后端（`http.server`），无外部依赖
- 读取 `logs/raw.db`（SQLite WAL mode）
- 自动处理 gzip / brotli 压缩的 body 和 response
- SSE 流重组（`content_block_delta`, `thinking_delta` 等）
- Anthropic 格式 → OpenAI 格式转换（system 合并、content blocks、tools）
- 前端深色主题，响应式布局
