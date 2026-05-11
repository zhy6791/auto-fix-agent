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

## Phase 6 — CI 管道：编译检查与单元测试（产物：`agents/auto_fix_agent.py` 扩展）

在补丁应用并 commit 后，自动执行编译检查和单元测试，形成 CI 门禁。

### 6.1 重构 `_run_build` 为两个独立方法

**`_run_compile(self, repo_path)`**
- Maven: `mvn compile -q`，超时 600s
- Gradle: `gradle compileJava`，超时 600s
- 返回 `exec_cmd.run()` 的结果 `{code, stdout, stderr}`

**`_run_tests(self, repo_path)`**
- Maven: `mvn test -q`，超时 1200s
- Gradle: `gradle test`，超时 1200s
- 返回同 `exec_cmd.run()` 的结果

### 6.2 CI 管道编排方法 `_run_ci_pipeline`

```python
def _run_ci_pipeline(self, repo_path, source_info, raw_stack, parsed_stack,
                     original_patch_text, original_prompt):
```

**执行逻辑：**
1. 检查配置中 `run_compile_on_apply` 和 `run_tests_on_apply` 的值
2. Stage 1 — 编译：执行 `_run_compile`，若失败进入重试循环（最多 `max_retries` 次）
3. Stage 2 — 测试（仅编译通过后执行）：执行 `_run_tests`，失败同理重试
4. 返回 `{compile_result, test_result, retries_used, patch_history, stages_passed, stages_failed}`

### 6.3 重试反馈方法

**`_retry_with_feedback(self, original_prompt, source_info, failed_stage, build_result, repo_path)`**
- 提取编译/测试错误输出（stderr 或 stdout，截断至 3000 字符）
- 调用 `_build_retry_prompt` 构建重试提示词
- 调用 `llm_client.generate_patch` 获取新补丁
- 对新补丁执行 `validate_patch` 校验
- 校验通过则写入文件（`git_manager.apply_patch`），返回新 patch_text；否则返回 None

**`_build_retry_prompt(self, original_prompt, source_info, failed_stage, error_output)`**
- 包含：原始修复任务上下文 + 编译/测试错误输出 + 修复要求
- 要求 LLM 保持原有异常修复的同时解决编译/测试问题
- 若无法兼顾，优先保证编译通过

### 重试状态机

```
编译失败 → 反馈 LLM → 新补丁 → 验证 → apply → 重新编译
    ↓ (成功)
  测试失败 → 反馈 LLM → 新补丁 → 验证 → apply → 重新测试
    ↓ (成功)
  CI 通过
```

若任一级别耗尽 `max_retries`（默认 3）仍失败，或 LLM 返回 `NO_SAFE_PATCH`，则停止该阶段的后续重试。

---

## Phase 7 — Gitee PR 创建（产物：`integrations/gitee_client.py` + `tools/git_manager.py` 扩展）

### 7.1 新建 `integrations/gitee_client.py`

遵循 `integrations/llm_client.py` 的风格，创建 `GiteeClient` 类：

```python
class GiteeClient:
    def __init__(self, access_token, base_url="https://gitee.com/api/v5", timeout=30)
    def create_pull_request(owner, repo, title, head, base, body) -> Dict
    def get_repo_info(owner, repo) -> Dict
```

**create_pull_request 实现：**
- `POST {base_url}/repos/{owner}/{repo}/pulls`
- 请求体：`{title, head, base, body, prune_source_branch: false}`
- 认证：`?access_token={token}` 查询参数（Gitee API v5 方式）
- 返回 `{success: bool, url: str, number: int, error: str | None}`
- 错误处理：HTTP 非 201（含错误详情）、超时、连接错误、通用异常

**get_repo_info 实现：**
- `GET {base_url}/repos/{owner}/{repo}`
- 返回 `{success, default_branch, error}`
- 用于验证仓库访问权限和获取默认分支

### 7.2 扩展 `tools/git_manager.py`

新增三个函数：

**`push_branch(repo_path, branch_name, remote='origin') -> bool`**
- 执行 `git -C <repo_path> push -u <remote> <branch_name>`
- 调用 `subprocess.check_call`，超时 60s
- 成功返回 True，失败记录日志并返回 False

**`get_remote_url(repo_path, remote='origin') -> str`**
- 执行 `git -C <repo_path> remote get-url <remote>`
- 返回 URL 字符串，失败返回空串

**`parse_gitee_owner_repo(remote_url) -> (owner, repo)`**
- 从 git remote URL 解析 Gitee 仓库的 owner 和 repo 名称
- 支持 HTTPS: `https://gitee.com/owner/repo.git`
- 支持 SSH: `git@gitee.com:owner/repo.git`
- 非 Gitee URL 返回 `(None, None)`

### 7.3 Agent 新增方法 `_push_and_create_pr`

```python
def _push_and_create_pr(self, repo_path, branch_name, parsed_stack, source_info, ci_result):
```

**执行逻辑：**
1. 检查 `gitee.enabled` 是否为 True
2. 解析 owner/repo（优先从 config，为空时自动从 git remote 检测）
3. 调用 `git_manager.push_branch` 推送修复分支
4. 从环境变量读取 Gitee access token
5. 创建 `GiteeClient`，构建 PR 标题（根据 `pr_title_template` 模板 + 异常信息）和描述（含 CI 状态）
6. 调用 `create_pull_request`，返回 `{pr_created, pr_url, pr_number, error}`

**PR 创建门控：** 仅当编译阶段通过时才创建 PR（测试失败仍可创建，方便人工介入复审）。

---

## Phase 8 — CLI 扩展（产物：`main.py` 更新）

### 8.1 新增命令行参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--no-compile` | store_true | False | 跳过编译检查（覆盖 config） |
| `--no-tests` | store_true | False | 跳过测试（覆盖 config） |
| `--create-pr` | store_true | False | 启用 Gitee PR 创建（覆盖 config） |
| `--max-retries` | int | None | 最大重试次数（覆盖 config，默认 3） |

### 8.2 CLI 覆盖逻辑

在 `main()` 函数中，`validate_config` 之后、创建 Agent 之前应用覆盖：

```python
if args.no_compile:
    config['run_compile_on_apply'] = False
if args.no_tests:
    config['run_tests_on_apply'] = False
if args.create_pr:
    config.setdefault('gitee', {})['enabled'] = True
if args.max_retries is not None:
    config['max_retries'] = args.max_retries
```

### 8.3 更新 `print_report` 输出

新增 CI Pipeline 和 PR 结果展示区域：

```
CI Pipeline:
   [OK] compile
   [OK] tests
   Retries used: 0

Pull Request:
   [OK] https://gitee.com/owner/repo/pulls/42
```

---

## Phase 9 — 配置更新（产物：`configs/config.yml` 更新）

在现有配置基础上新增：

```yaml
# CI Pipeline settings
run_compile_on_apply: true      # 应用补丁后执行 mvn compile
run_tests_on_apply: false       # 应用补丁后执行 mvn test（默认关）
max_retries: 3                  # 编译/测试失败时的最大 LLM 重试次数

# Gitee integration
gitee:
  enabled: false                # 是否自动创建 PR
  owner: ""                     # Gitee 仓库 owner（空则从 git remote 自动检测）
  repo: ""                      # Gitee 仓库名（空则从 git remote 自动检测）
  access_token_env: "GITEE_TOKEN"  # Gitee 私人令牌的环境变量名
  api_base_url: "https://gitee.com/api/v5"
  target_branch: "main"         # PR 目标分支
  pr_title_template: "fix: auto-fix {exception_type} in {class_name}"
```

---

## Phase 10 — 单元测试（产物：`tests/` 目录扩展）

### 10.1 新建 `tests/test_gitee_client.py`

遵循 `test_llm_client.py` 的单测模式（mock `requests.post`/`requests.get`）：

- `test_init_defaults` — 验证默认构造函数参数
- `test_create_pr_success` — mock 201 响应，验证返回 success=True 及 URL/number
- `test_create_pr_http_error` — mock 422 响应，验证返回 success=False + 错误信息
- `test_create_pr_timeout` — mock `requests.exceptions.Timeout`
- `test_create_pr_connection_error` — mock `requests.exceptions.ConnectionError`
- `test_get_repo_info_success` — mock 200 响应含 default_branch
- `test_get_repo_info_failure` — mock 非 200 响应

### 10.2 追加 `tests/test_git_manager.py`

- `test_push_branch_success` — mock `subprocess.check_call`
- `test_push_branch_failure` — mock `subprocess.CalledProcessError`
- `test_get_remote_url` — mock `subprocess.check_output`
- `test_parse_gitee_owner_repo_https` → `('owner', 'repo')`
- `test_parse_gitee_owner_repo_ssh` → `('owner', 'repo')`
- `test_parse_gitee_owner_repo_non_gitee` → `(None, None)`

### 10.3 追加 `tests/test_auto_fix_agent.py`

- `test_run_compile_maven` — 验证 Maven compile 命令
- `test_run_compile_gradle` — 验证 Gradle compile 命令
- `test_run_tests_maven` — 验证 Maven test 命令
- `test_run_tests_gradle` — 验证 Gradle test 命令
- `test_retry_on_compile_failure` — mock 首次编译失败，二次成功，验证重试
- `test_retry_exhausted` — 全部重试失败，验证 ci_result 记录
- `test_no_retry_when_compile_passes` — 编译一次通过，验证无重试
- `test_retry_stops_on_no_safe_patch` — LLM 返回 NO_SAFE_PATCH 时停止重试

### 10.4 追加 `tests/test_cli_integration.py`

- `test_cli_no_compile_flag` — 验证 `--no-compile` 覆盖
- `test_cli_no_tests_flag` — 验证 `--no-tests` 覆盖
- `test_cli_create_pr_flag` — 验证 `--create-pr` 覆盖
- `test_cli_max_retries_flag` — 验证 `--max-retries` 覆盖

---

## 安全与注意事项

- 请确保 OPENAI 的 API key 仅以环境变量方式提供（不写入代码/仓库）。
- 运行前建议备份目标 repo（或在临时 clone 上运行 demo）。
- LLM 可能生成不安全或不正确的补丁；agent 会做静态校验并在不满足安全规则时拒绝自动应用。
- Gitee access token 需从环境变量读取，不可写入配置文件。



