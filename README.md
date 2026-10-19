# PDF2ZH Skill

**用当前 Agent 聊天模型翻译本地英文论文 PDF，并生成中文译文和中英双语 PDF。**

This Codex skill translates local research-paper PDFs with the active chat model and produces Chinese-only and bilingual PDFs.

## 功能

- 按批次翻译并保存进度，可从中断处续译。
- 保留原 PDF 的页面结构、主要图片和公式，并检查生成文件。
- 默认把任务目录放在源 PDF 文件夹旁；目录不可写时回退到工作区。
- 翻译由当前 Agent 完成，不需要单独的翻译 API 密钥或翻译服务。

## 安装到 Codex

```sh
git clone https://github.com/gaffeyn03/pdf2zh-skill.git "${CODEX_HOME:-$HOME/.codex}/skills/pdf2zh-agent-translate"
```

安装后在 Codex 中使用 `$pdf2zh-agent-translate`，或直接请求翻译一篇论文。也可以将仓库复制到 Agent 配置的 skills 目录。

## 本机运行环境

需要支持本机文件和进程操作的 Codex/Agent 环境，以及 CPython 3.11 或 3.12。macOS/Linux 可运行：

```sh
python3 scripts/run.py doctor --json
python3 scripts/run.py setup --json
```

Windows 可运行：

```powershell
py -3.12 scripts/run.py doctor --json
py -3.12 scripts/run.py setup --json
```

首次设置会从网络安装锁定版本的依赖并下载版面模型和字体。完成后，PDF 处理在本机运行。运行环境及缓存位置按系统选择用户数据目录。

## 适用范围与限制

- 面向可选中文字的 PDF；扫描件/OCR 不在此流程范围内。
- 图表内部嵌入的文字不会单独翻译。复杂版面仍需检查，自动校验不能保证与原版完全一致。
- 不要把论文原文、任务目录或生成的 PDF 提交到代码仓库。仓库 `.gitignore` 已屏蔽 PDF 和常见任务产物。

## 上游致谢

本项目的分批 PDF 提取、翻译和回填工作流参考并改编自 [AIKONG2024/paper-translate-agent-skill](https://github.com/AIKONG2024/paper-translate-agent-skill)，该上游项目采用 MIT License。改编范围和其他依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可

本仓库按 MIT License 发布，改编材料和外部依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。运行时会安装 PDFMathTranslate/pdf2zh（AGPL-3.0）及下载资源；它们不包含在本仓库中。分发或修改该外部项目时，请自行核对并遵守上游许可。
