# Demo 计划 — Java Web 自动修复 Agent (Python + uv + OpenAI)

## 一行说明

构建一个单 agent 的演示（demo）：读取配置中的日志路径与本地 Maven 项目路径，执行流水线：读取日志 → 解析异常堆栈 → 定位源码 → 使用 OpenAI 生成最小补丁 → 在本地创建修复分支并将补丁写入工作区（不执行 commit）。使用 `uv` 管理 Python 依赖，配置采用 YAML。

---

## 快速检查清单（Checklist）

- [ ] 在根目录创建并填写 `configs/config.yml`（包含你的 `repo_path` 与 `logs_path`）。
- [ ] 实现并测试工具模块：`tools/file_io.py`、`tools/exec_cmd.py`、`tools/git_manager.py`。
- [ ] 实现单 agent：`agents/auto_fix_agent.py`（主方法 `run_pipeline`）。
- [ ] 集成 OpenAI（`integrations/llm_client.py`），并实现 prompt 模板与输出校验。
- [ ] 提供 CLI：`main.py` 支持 `--config`、`--dry-run`、`--auto-apply`。
- [ ] 提交 README 与 demo 脚本（PowerShell）以便复现。

---

## 分阶段详细任务（Phase）

### Phase 1 — 项目初始化（产物：目录与配置）

- 创建目录结构：`agents/`、`tools/`、`integrations/`、`configs/`、`demo/`、`tests/`。
- 在根目录添加 `configs/config.yml`（示例见下）。用户在其中填入 `repo_path`（Maven 项目）与 `logs_path`（web 服务日志）。
- 使用 `uv` 初始化（生成 `pyproject.toml` 并列出依赖）：`pyyaml`, `requests`, `openai`, `gitpython`（可选）。
- 在 `demo/logs/app.log` 放示例堆栈用于本地演示。

### Phase 2 — 工具模块（产物：`tools/`）

- `tools/file_io.py`：
  - `read_file(path) -> str`
  - `tail_file(path, since_pos=None) -> (new_pos:int, chunk:str)`
  - `write_file(path, content, overwrite=False) -> bool`
- `tools/exec_cmd.py`：
  - `run(cmd_list, cwd=None, timeout=None) -> {code, stdout, stderr}`
- `tools/git_manager.py`：
  - `detect_repo_root(path) -> str`
  - `create_branch(repo_path, branch_name) -> bool`（创建并切换到分支）
  - `apply_patch(repo_path, patch_text) -> {applied:bool, files:[str], errors:[str]}`（写入工作区，但不 commit）
- 为以上实现单元测试（`tests/`）。

### Phase 3 — 单 agent 流水线（产物：`agents/auto_fix_agent.py`）

- Class: `AutoFixAgent(config: dict, tools: ToolInterfaces)`
- 方法: `run_pipeline(dry_run: bool=True) -> dict`（返回 report，包括 `raw_stack`、`parsed_stack`、`located_files`、`patch_text`、`branch_name`、`apply_result`、`build_result`）
- 步骤：
  1. 读取日志（`tail_file`）并抽取最新异常块。
  2. 解析堆栈（`parse_stacktrace`）提取 `{exception_type, class_name, method, line_no}`。
  3. 源码定位（`find_source_location`）将 package 映射为 `src/main/java/...` 并读取上下文。
  4. 调用 LLM（OpenAI）生成补丁（最小改动），要求输出 unified-diff 或 JSON patch。
  5. 校验 patch（文件路径存在性、改动行数 <= `max_patch_lines`、安全规则）。
  6. 创建本地修复分支（`fix/<id>`）并应用 patch（写入工作区），但不 commit。
  7. 可选：运行 `mvn test` 并把构建/测试结果写入报告。

### Phase 4 — OpenAI 集成与提示工程（产物：`integrations/llm_client.py` + prompt 模板）

- `LLMClient(api_key_env, model, temperature)`
  - `generate_patch(prompt, max_tokens) -> str`
- Prompt 必须包含：
  - 完整堆栈信息（exception）
  - 目标文件相对路径与源码上下文（前后各 N 行，包含行号）
  - 修复约束：最小改动、保持编译通过、不得删除业务逻辑、最大改动行数
  - 输出格式说明：必须返回 unified-diff 或 JSON `{files:[{path, patched_content}]}`。
- 输出校验：解析 diff，验证更改路径位于 repo 下，改动不超过阈值；不安全则拒绝或要求 LLM 重新生成。

### Phase 5 — CLI、文档与测试

- `main.py` 支持参数：`--config PATH`、`--dry-run`（默认 true）、`--auto-apply`（写入工作区但不 commit）。
- README 包含 uv 安装依赖、如何配置 OpenAI API key、示例 `config.yml`、PowerShell 运行示例。
- 单元测试覆盖 stack parser、`git_manager.apply_patch`（dry-run）、LLM prompt 构造（mock）。

---

## 配置示例（configs/config.yml）

```yaml
logs_path: "D:/path/to/app.log"          # 你的 web 服务日志文件
repo_path: "D:/path/to/your-maven-project"  # 你的 Maven 项目根目录
java_build: "maven"
branch_prefix: "fix/"
llm:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key_env: "OPENAI_API_KEY"
  temperature: 0.2
max_patch_lines: 40
auto_apply: false
run_tests_on_apply: false
```

---

## OpenAI Prompt 模板（示例，给 LLM 的输入）

请以中文/英文都可：

```
"异常: <完整堆栈追踪>
目标文件（repo 相对路径）: <path>
问题行号: <N>
源码上下文（已标注行号）:
---
<source lines with line numbers>
---
限制与目标:
- 请生成一个尽可能小的补丁以修复导致异常的问题（优先保证编译通过）。
- 最多修改 <max_patch_lines> 行。
- 不要改变公共 API 或方法签名，不要删除业务逻辑。
输出格式:
- 请返回 unified-diff（git 格式），或者返回 JSON: {"files": [{"path": "...", "patched_content": "..."}]}。
如果无法在安全范围内生成补丁，请返回 "NO_SAFE_PATCH" 并解释原因与人工修复建议。"
```

---

## 输出校验与回退策略

- 在将 patch 应用到工作区前执行校验：
  - 所有文件路径必须位于 repo 下或是可创建的新文件（受限制）。
  - 改动总行数 <= max_patch_lines。
  - 不允许删除整个方法体或大量代码（删除比例阈值）。
- 若校验失败：记录原因并返回给用户；如启用重试，向 LLM 发送更严格约束后重试一次。
- 默认 `--dry-run` = true；仅在明确指定 `--auto-apply` 且 config.auto_apply=true 时写入工作区。

---

## 运行示例（PowerShell）

```powershell
# 安装依赖（uv）
uv install

# 以 dry-run 执行 demo
python -m main --config configs/config.yml --dry-run

# 写入工作区（不会 commit）
python -m main --config configs/config.yml --auto-apply
```

---

## 验收标准（Acceptance Criteria）

- 能正确读取 `configs/config.yml` 并加载日志与 repo 路径。
- 工具模块通过单元测试并能在目标 repo 上创建分支（本地）与写入文件（未 commit）。
- Agent 能从示例日志抽取堆栈并定位到 repo 的源文件。
- OpenAI 返回的 patch 可被解析并在 dry-run 下应用（在非 dry-run 下写入工作区但不 commit）。

---

## 安全与注意事项

- 请确保 OPENAI 的 API key 仅以环境变量方式提供（不写入代码/仓库）。
- 运行前建议备份目标 repo（或在临时 clone 上运行 demo）。
- LLM 可能生成不安全或不正确的补丁；agent 会做静态校验并在不满足安全规则时拒绝自动应用。

---

## 下一步建议

我可以继续：

1. 在仓库中创建上述文件骨架（`tools/`, `agents/`, `integrations/`, `configs/` 的 stub 文件）并实现基础工具函数；
2. 实现 `AutoFixAgent` 的核心流水线（不含 OpenAI 调用，先用 mock）；
3. 集成 OpenAI 并完善 prompt 与校验。

请回复你希望我立刻开始执行哪一步（例如：创建文件骨架并实现工具模块），我会马上开始并把修改写入仓库。

