# Evermind — AI Workflow Orchestrator 🧠

> 多 AI 智能体协作的可视化工作流编辑器，类似 Dify / Flowise / LangFlow

## 🚀 Quick Start

### 1. 启动后端
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # 填入你的 API Keys
python3 server.py
```

### 2. 打开前端
打开浏览器访问 **http://localhost:8765**

### 3. 配置 API Key
点击右上角 ⚙️ 设置 → 接口 → 填入至少一个 API Key

## 📁 项目结构

```
ai智能体合作/
├── backend/                  # Python 后端
│   ├── server.py            # FastAPI + WebSocket 服务器
│   ├── ai_bridge.py         # AI 模型调用引擎 (LiteLLM 100+ 模型)
│   ├── orchestrator.py      # 自主编排引擎
│   ├── executor.py          # 节点执行器
│   ├── privacy.py           # 脱敏处理 (PII masking)
│   ├── proxy_relay.py       # 中转 API 管理
│   ├── plugins/             # 7 个插件
│   │   ├── base.py          # 插件基类 + 安全等级
│   │   └── implementations.py  # screenshot, browser, file_ops, shell, git, CUA, UI control
│   ├── requirements.txt
│   └── .env.example
├── evermind_godmode_final.html  # 可视化前端 (单文件)
├── frontend/                # Next.js 版前端 (可选)
└── docker-compose.yml
```

## ✨ Features

- **可视化节点编辑器** — 拖拽连线，构建 AI 工作流
- **100+ AI 模型** — OpenAI / Claude / Gemini / DeepSeek / Kimi / Qwen / Ollama
- **中转 API** — 支持任意 OpenAI 兼容端点
- **脱敏处理** — 手机/邮箱/身份证/API Key 等 11 种 PII 自动脱敏
- **AI 控制电脑** — 鼠标/键盘/截图/拖拽/剪贴板/窗口管理
- **安全等级** — L1-L4 权限控制，L3 确认弹窗，L4 密码+倒计时
- **中英文** — 完整 i18n 支持

## 🔌 API

| Endpoint | Method | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/models` | GET | 可用模型列表 |
| `/api/relay/add` | POST | 添加中转端点 |
| `/api/relay/list` | GET | 列出中转端点 |
| `/api/privacy/test` | POST | 测试脱敏 |
| `/api/execute` | POST | 执行单个节点 |

## 📄 License

MIT
