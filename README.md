# Auto-Fix Agent for Java Web Services

一个用 Python 实现的自动修复 Java Web 服务异常的 demo agent。通过读取日志、分析堆栈、定位源码、调用 LLM 生成补丁、以及在本地创建修复分支，快速定位和修复常见异常。

**核心特性：**
- 📋 自动提取日志中的异常堆栈信息
- 🧠 **Agent 决策循环**：LLM 自主调用 8 个工具（定位、搜索、阅读、推断、编辑、校验）完成定位与修复，循环轮次可配置
- 🔍 智能定位 Java 源码文件和问题行号（堆栈帧定位 + LLM 推断，由 Agent 自主选择）
- 🤖 使用 OpenAI 兼容 LLM 生成最小化补丁（含指数退避重试）
- 🔀 在本地仓库创建修复分支并提交
- 🔨 自动编译检查 + 单元测试（`mvn compile` / `mvn test`）
- 🔄 编译/测试失败时自动反馈 LLM 重试修复（失败时自动还原文件状态）
- 🚀 通过后自动推送并创建 Gitee Pull Request
- ✅ 支持 dry-run 模式（仅分析不修改）
- 🧪 完整的单元测试覆盖（110+ 测试通过）

---

## 系统目录架构

```
auto-fix-agent/
├── main.py                          # CLI 入口，参数解析，配置加载
├── configs/
│   └── config.yml                   # 运行配置（LLM、仓库路径、CI、Gitee 等）
│
├── agents/                          # Agent 核心逻辑
│   ├── auto_fix_agent.py            # 主编排器：run_pipeline() 串联各阶段
│   ├── react_agent.py               # ReAct 决策循环引擎（新增）
│   ├── tool_registry.py             # 工具注册表：8 个工具的定义与分发（新增）
│   ├── stacktrace_parser.py         # Java 堆栈解析（正则提取帧信息）
│   ├── source_locator.py            # 源码定位（按类名/路径查找 .java 文件）
│   ├── exception_inference.py       # LLM 推断定位（堆栈全为框架代码时使用）
│   ├── prompt_builder.py            # LLM Prompt 构建（补丁生成、重试）
│   ├── patch_validator.py           # 补丁校验（格式、路径、结构、注释保护）
│   ├── ci_pipeline.py               # CI 管道（编译/测试 + LLM 重试反馈）
│   └── pr_manager.py                # Gitee PR 管理（推送分支 + 创建 PR）
│
├── integrations/                    # 外部 API 客户端
│   ├── llm_client.py                # LLM 客户端（generate_patch + chat）
│   └── gitee_client.py              # Gitee API 客户端
│
├── tools/                           # 底层工具模块
│   ├── file_io.py                   # 文件读写（read_file, tail_file, write_file）
│   ├── git_manager.py               # Git 操作（分支、提交、应用补丁、推送）
│   └── exec_cmd.py                  # 命令执行（白名单 + 超时控制）
│
├── tests/                           # 单元测试
│   ├── test_auto_fix_agent.py       # 主编排器测试
│   ├── test_react_agent.py          # Agent 循环测试（新增）
│   ├── test_tool_registry.py        # 工具注册表测试（新增）
│   ├── test_patch_formats.py        # 补丁格式测试
│   ├── test_cli_integration.py      # CLI 集成测试
│   ├── test_file_io.py              # 文件 I/O 测试
│   ├── test_git_manager.py          # Git 操作测试
│   ├── test_llm_client.py           # LLM 客户端测试
│   ├── test_gitee_client.py         # Gitee 客户端测试
│   └── test_exec_cmd.py             # 命令执行测试
│
├── agent_report_fix/                # 运行报告输出目录（JSON 格式）
├── scripts/
│   └── demo.ps1                     # PowerShell 演示脚本
└── pyproject.toml                   # Python 项目元数据和依赖
```



## 使用方式

### 方式 1: Dry-Run 模式（仅分析，不修改代码）

```powershell
python -m main --config configs/config.yml --dry-run
```


### 方式 2: 自动应用模式（完整 CI 管道）

```powershell
python -m main --config configs/config.yml --auto-apply
```




## 修复流程详解

Agent 的主入口是 `main.py`：先读取 `configs/config.yml`，校验 `logs_path`、`repo_path` 和 `command_whitelist`，
再把 CLI 参数（`--dry-run`、`--auto-apply`、`--no-compile`、`--no-tests`、`--create-pr`、`--max-retries`、`--max-agent-iterations`）覆盖到配置中，
最后交给 `agents/auto_fix_agent.py` 的 `AutoFixAgent.run_pipeline()` 统一编排。

修复流程分为两个阶段：**Agent 决策循环**（定位 + 修复）和**确定性管道**（校验 + CI + PR）。

### 1. 从日志中提取最新异常块

1. 通过 `tools/file_io.tail_file()` 读取日志尾部。
2. `agents/stacktrace_parser.py` 里的 `extract_latest_exception_block()` 会从最新日志中截取最近一段异常块，优先识别 `Caused by:`、`Exception`、`Error`、`Throwable`。
3. `parse_stacktrace()` 将堆栈解析为 `exception_type`、`class_name`、`method`、`line_no` 等结构化信息，并忽略 `Native Method` / `Unknown Source` 这类无效帧。

### 2. Agent 决策循环：自主定位 + 修复

提取异常后，进入 `agents/react_agent.py` 的 **ReAct 决策循环**。Agent 在循环中自主决定调用哪些工具来定位源码和生成补丁，循环轮次可通过 `--max-agent-iterations` 配置（默认 10）。

```text
异常堆栈 + 解析后的帧列表
  ↓
┌─────── Agent Decision Loop ────────────────────────────────┐
│  Thought: 分析堆栈，决定下一步                              │
│  Action:  调用工具（locate_from_stack / read_code / ...）   │
│  Observation: 工具返回结果                                  │
│  Thought: 根据观察结果，继续分析                            │
│  Action:  调用下一个工具                                    │
│  ...（循环直到调用 final_patch 或 abort）                    │
└────────────────────────────────────────────────────────────┘
  ↓
  final_patch → 返回补丁 + source_info
  abort → 报告无法修复
```

#### Agent 可调用的工具

| 工具 | 说明 |
|------|------|
| `locate_from_stack` | 从堆栈帧定位源文件，返回路径、行号、上下文片段、完整源码 |
| `infer_source` | 当堆栈全是框架代码时，用 LLM 推断应用层源码位置 |
| `search_code` | 按类名或文件路径在仓库中查找 Java 源文件 |
| `read_code` | 读取仓库内任意文件内容 |
| `edit_code` | 调用 LLM 生成最小化 unified diff 补丁（不直接写文件） |
| `validate_patch` | 校验补丁格式、路径边界、hunk 大小、Java 结构完整性 |
| `final_patch` | 提交最终补丁，退出循环 |
| `abort` | 判断无法安全修复，退出循环 |

#### Agent 决策链示例

```text
迭代 1: Thought: 堆栈第一帧是 UserService.getNickname:27，先定位源文件。
         Action: locate_from_stack({class_name: “UserService”, method: “getNickname”, line_no: 27})
迭代 2: Thought: 已定位到源码，异常是 NPE，需要看 MallUser 类理解根因。
         Action: search_code({class_name: “com.fixflow.mall.domain.MallUser”})
迭代 3: Thought: 找到了 MallUser.java，读取源码。
         Action: read_code({path: “src/main/java/.../MallUser.java”})
迭代 4: Thought: 分析完毕，生成修复补丁。
         Action: edit_code({raw_stack: “...”, source_info: {...}})
迭代 5: Thought: 补丁已生成，先校验安全性。
         Action: validate_patch({patch_text: “...”, source_info: {...}})
迭代 6: Thought: 校验通过，提交最终补丁。
         Action: final_patch({patch_text: “...”, source_info: {...}})
```

### 3. 补丁验证：先校验，再落盘

Agent 循环退出后，`agents/patch_validator.py` 会在应用前做多层检查：

1. 只允许 `unified diff`，拒绝 JSON / `patched_content` / 整文件替换。
2. 使用 `realpath + commonpath` 检查路径边界，防止越界写入仓库外文件。
3. 校验 Java 结构是否被破坏：
   - `package` 声明保持不变
   - `import` 不能删除或篡改
   - 问题窗口外的关键方法不能被删掉
   - 文件内容不能被大幅缩短（默认不低于原文件 50%）
   - 不能出现类似 `59: code` 这样的行号前缀
4. 约束 patch 粒度：`max_patch_lines`、`max_patch_hunks`、`max_hunk_lines`、`max_hunk_span`、`max_file_change_ratio`。
5. 保护头部注释、版权、Javadoc、TODO / FIXME 等关键注释不被静默删除。

只要其中任一项失败，补丁就不会被应用。

### 4. 创建修复分支并应用补丁

当 `dry_run=False` 时，`AutoFixAgent` 会：

- 创建修复分支：`fix/auto-<timestamp>`（可通过 `branch_prefix` 调整）
- 使用 `tools/git_manager.apply_patch()` 应用补丁
- 如果补丁生效并修改了文件，则提交到当前修复分支
- 提交信息会包含异常类型和目标类/方法，便于追踪

`dry-run` 模式下只做分析和验证，不会创建分支，也不会写文件。

### 5. CI 管道：编译 / 测试 / 失败重试

`agents/ci_pipeline.py` 负责后续门禁：

- 优先使用项目 wrapper：`mvnw.cmd` / `mvnw` / `gradlew.bat` / `gradlew`
- 否则回退到系统命令：`mvn` / `gradle`
- 根据配置执行 `compile`、`test`，对应 `run_compile_on_apply` 和 `run_tests_on_apply`

如果编译或测试失败：

1. 提取错误输出（优先 `stderr`，截断到约 3000 字符）。
2. 用 `build_retry_prompt()` 把原始修复上下文 + 报错信息一起发回 LLM。
3. 生成新的补丁后再次校验。
4. 在重新应用前，先把上一次改过的文件回滚到 `HEAD~1`，避免累积脏状态。
5. 重试次数最多 `max_retries` 次。

如果构建工具本身缺失，流程会直接停止重试，而不会继续”盲修”。

### 6. 推送分支并创建 Gitee PR

只有在满足以下条件时才会进入 PR 阶段：

- `gitee.enabled=true`
- 编译通过
- 如果开启测试门禁，则测试也必须通过

`agents/pr_manager.py` 会：

1. 推送当前修复分支到远程仓库。
2. 解析 `gitee.owner` / `gitee.repo`，若未配置则尝试从 git remote 自动识别。
3. 使用 Gitee API 创建 PR。
4. PR 标题遵循 `pr_title_template`，正文包含异常信息和 CI 状态。

### 7. 总体流程图

```text
日志文件
  │
  ▼
┌─────────────────────────────────┐
│ tail_file → 异常块提取           │
│ parse_stacktrace → 结构化帧列表  │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│                  Agent 决策循环（最多 N 轮）                   │
│                                                             │
│  ┌─── 每轮迭代 ───────────────────────────────────────────┐ │
│  │  LLM(Thought) → 解析 Action                            │ │
│  │     │                                                  │ │
│  │     ├─ locate_from_stack → 返回源文件路径/行号/上下文    │ │
│  │     ├─ search_code       → 按类名查找 Java 文件         │ │
│  │     ├─ read_code         → 读取文件内容                 │ │
│  │     ├─ infer_source      → LLM 推断应用层源码位置       │ │
│  │     ├─ edit_code         → LLM 生成 unified diff 补丁  │ │
│  │     ├─ validate_patch    → 校验格式/路径/结构/粒度      │ │
│  │     ├─ final_patch       → 提交补丁，退出循环 ✓        │ │
│  │     └─ abort             → 放弃修复，退出循环 ✗        │ │
│  └────────────────────────────────────────────────────────┘ │
│  超时/达到上限 → 自动 abort                                  │
└─────────────┬───────────────────────────────────────────────┘
              │ final_patch
              ▼
         补丁校验（6 层验证）
              │
       ┌──────┴──────┐
       │ 校验失败     │ 校验通过
       ▼             ▼
    报告错误    dry_run?
              ┌─────┴─────┐
           是 ▼           ▼ 否
         仅报告      创建修复分支 fix/auto-<timestamp>
                         │
                         ▼
                    apply_patch → commit
                         │
                   ┌─────┴─────┐
                   │ 应用失败   │ 应用成功
                   ▼           ▼
                报告错误   CI 管道（含重试）
                               │
                     ┌── CI 管道（含重试）──┐
                     │                      │
                     ▼                      ▼
               mvn compile             mvn test
               ┌────┴────┐            ┌────┴────┐
           失败 ▼         ▼ 通过    失败 ▼         ▼ 通过
         构建工具缺失?  进入测试   还原文件 → LLM 重试
           → 停止               (最多 max_retries 次)
                                      │
                                  重试耗尽 → 停止
                                      │
                                      ▼
                              编译+测试通过
                                      │
                               ┌──────┴──────┐
                               │ Gitee 未启用 │ Gitee 已启用
                               ▼             ▼
                             结束       push 分支
                                        创建 Gitee PR
```

### Agent 工具 vs 旧版路径对比

| 特性 | Agent 决策循环（当前） | 旧版固定路径（已弃用） |
|------|----------------------|----------------------|
| 定位方式 | Agent 自主选择 `locate_from_stack` 或 `infer_source`，可多次尝试 | 硬编码 if/else：有业务帧走传统路径，否则走推断路径 |
| 上下文获取 | Agent 可主动调用 `search_code` + `read_code` 查看相关类 | 固定只看问题行 ±3 行 + 截断的完整文件 |
| 修复策略 | Agent 可多次调用 `edit_code` + `validate_patch`，校验失败后调整策略 | 一次性生成补丁，校验失败直接 abort |
| 决策主体 | LLM 自主决策（Thought → Action → Observation 循环） | 代码中的 if/else 和 for 循环 |

---



### 模块调用关系

```text
main.py
  └── AutoFixAgent.run_pipeline()
        ├── 阶段 1: 堆栈提取
        │     └── stacktrace_parser.extract_latest_exception_block()
        │         stacktrace_parser.parse_stacktrace()
        │
        ├── 阶段 2: Agent 决策循环
        │     └── ReActAgent.run()
        │           ├── ToolRegistry.execute("locate_from_stack")
        │           │     └── source_locator.find_source_location()
        │           ├── ToolRegistry.execute("search_code")
        │           │     └── source_locator.locate_file_by_class_or_path()
        │           ├── ToolRegistry.execute("read_code")
        │           │     └── file_io.read_file()
        │           ├── ToolRegistry.execute("infer_source")
        │           │     └── exception_inference.infer_from_exception_message()
        │           ├── ToolRegistry.execute("edit_code")
        │           │     ├── prompt_builder.build_prompt()
        │           │     └── llm_client.generate_patch()
        │           ├── ToolRegistry.execute("validate_patch")
        │           │     └── patch_validator.validate_patch()
        │           └── ToolRegistry.execute("final_patch" | "abort")
        │
        ├── 阶段 3: 补丁校验 + 应用
        │     ├── patch_validator.validate_patch()
        │     └── git_manager.create_branch() / apply_patch() / commit_changes()
        │
        ├── 阶段 4: CI 管道
        │     └── ci_pipeline.run_ci_pipeline()
        │           ├── ci_pipeline.run_compile()
        │           ├── ci_pipeline.run_tests()
        │           └── ci_pipeline.retry_with_feedback()
        │
        └── 阶段 5: 创建 PR
              └── pr_manager.push_and_create_pr()
```



## 后续开发建议

- [ ] 支持更多 LLM provider（LLaMA、Claude、etc)
- [ ] 添加 GUI 界面
- [ ] 支持多语言堆栈格式（Python、Node.js、etc）
- [ ] 集成 IDE 插件（VS Code、IntelliJ）
- [x] ~~自动编译检查与单元测试~~ — 已实现
- [x] ~~CI 失败时自动重试修复~~ — 已实现
- [x] ~~自动创建 Gitee Pull Request~~ — 已实现
- [x] ~~LLM 驱动的 Agent 决策循环（自主定位 + 修复）~~ — 已实现
- [ ] 支持 Gradle 和 TestNG
- [ ] 修复质量评分
- [ ] 支持更多框架异常（Quarkus、Micronaut、etc）
- [ ] 历史修复记录和学习反馈

## 安全性与防护考量

本项目为自动应用 LLM 生成补丁提供了若干内建防护以降低误改源码或造成更大范围破坏的风险。以下为当前实现的要点、已识别的风险与优先级建议（供运维/项目负责人参考）。

已实现的保护（证据位置）
- `dry-run` 安全模式：只分析，不修改代码（`agents/auto_fix_agent.py` 的 `run_pipeline(dry_run=True)`）。
- Agent 循环保护：最大迭代次数限制（默认 10，可配置）、scratchpad 上下文管理与压缩、`abort` 工具允许 Agent 主动放弃修复（`agents/react_agent.py`）。
- Agent 工具安全隔离：`edit_code` 仅返回 patch_text 不直接写文件、`final_patch` 退出循环后由 `run_pipeline` 统一执行校验与应用（`agents/tool_registry.py`）。
- 结构保护校验：检查 package/import/方法签名、窗口外删除、文件缩减比例、行号前缀（`agents/patch_validator.py:validate_java_structure`）。
- 补丁格式收口：仅接受 `unified diff`，拒绝 JSON `patched_content`、整文件重写、片段替换式补丁（`agents/patch_validator.py`, `tools/git_manager.py`）。
- 补丁粒度限制：`max_patch_lines`、`max_patch_hunks`、`max_hunk_lines`、`max_hunk_span`、`max_file_change_ratio`（`agents/patch_validator.py`）。
- 关键注释保护：头部注释、版权/LICENSE、Javadoc、TODO/FIXME 不能被静默删除（`agents/patch_validator.py`）。
- LLM 失败兜底：空响应或异常统一返回 `NO_SAFE_PATCH` 并中止应用（`integrations/llm_client.py`）。
- 路径边界校验：使用 `realpath + commonpath` 防止越界写入（`agents/patch_validator.py`, `tools/git_manager.py`）。
- 原子写入保护：临时文件 + `os.replace`，降低写入中断导致的文件损坏风险（`tools/git_manager.py`）。
- 命令白名单：仅允许白名单内的外部命令执行（`tools/exec_cmd.py`，由 `command_whitelist` 配置控制）。
- 分支隔离提交：修改提交在修复分支进行，避免直接污染主线（`agents/auto_fix_agent.py`, `tools/git_manager.py`）。
- 执行门禁与回滚：编译/测试失败会触发重试与回滚（`agents/ci_pipeline.py` 的 `retry_with_feedback` / `revert_files`）。
- 命令执行防护：统一返回结构 + 超时控制（`tools/exec_cmd.py`）。



## 许可证

MIT License

---

## 贡献

欢迎提交 Issue 和 PR！
