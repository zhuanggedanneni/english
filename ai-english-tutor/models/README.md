# 模型目录

本目录**不包含任何权重文件**，需要自行准备。

## 音素评分模型

放置位置：`models/phoneme-model/`

要求：

- 架构：`Wav2Vec2ForCTC`（Transformers 可直接加载）；
- 词表：IPA 音素（与 espeak-ng 输出对齐）；
- 必需文件：`config.json`、`preprocessor_config.json`（若有）以及权重（`pytorch_model.bin` 或 `model.safetensors`）。

也可以通过环境变量 `PHONEME_MODEL_PATH` 指向其他目录，或直接填 Hugging Face 模型 ID，由 `transformers` 首次运行时下载（需要联网）。

## 许可提醒

模型权重遵循其原始许可证。请勿把权重提交到本仓库；如果模型许可禁止再分发，保持本目录为空并让使用者自行下载即可。

## 不用模型也能跑

`.env` 中设置：

```
SPEECH_EVAL_PROVIDER=demo
```

此时应用完整可用（对话、语法、翻译、报告），只是侧栏不产出真实发音分，并会明确标注"未评分"。
