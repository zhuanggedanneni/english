# 第三方组件与许可说明

本仓库只包含源码，不分发任何第三方二进制、模型权重或依赖包。运行所需组件请按下表自行安装，并遵守各自的许可证。

| 组件 | 用途 | 许可证 |
|---|---|---|
| FastAPI | Web 框架 | MIT |
| Uvicorn | ASGI 服务器 | BSD-3-Clause |
| Pydantic / pydantic-settings | 数据校验与配置 | MIT |
| SQLAlchemy | 数据库访问 | MIT |
| httpx | HTTP 客户端 | BSD-3-Clause |
| websockets | WebSocket 客户端/服务端 | BSD-3-Clause |
| PyTorch / TorchAudio | 推理运行时 | BSD-3-Clause |
| Transformers | 模型加载 | Apache-2.0 |
| SoundFile / libsndfile | 音频读写 | BSD-3-Clause / LGPL-2.1 |
| phonemizer | 音标接口 | GPL-3.0 |
| espeak-ng | 音标生成引擎 | GPL-3.0 |

## 词库

`THIRD_PARTY_LICENSES/word-forest-LICENSE.txt` 为随源码保留的词库许可原文，请一并遵守。

## 模型权重

音素模型与对话模型均**不随仓库分发**。使用前请自行确认对应模型的许可证是否允许你的使用场景（尤其是商用与再分发）。

## 外部服务

云端对话模型、语音识别与语音合成均为外部服务，其服务条款、计费与数据处理规则由对应服务商决定。仓库中的配置模板只包含占位符，不包含任何真实地址、资源 ID 或密钥。
