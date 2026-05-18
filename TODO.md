# TODO - Auto-Fix Agent 改进项

基于 README 和全部源码的通读，按优先级分类整理。

---

## ~~P0 - 必须立即修复（安全 & Bug）~~ ✅ 全部完成

### ~~1. Gitee access token 明文泄露~~ ✅
- **文件**: `configs/config.yml:39`
- **问题**: `access_token_env` 字段直接写入了真实 token，`_resolve_env_var` 的 fallback 逻辑会在环境变量找不到时把原始值当 token 使用，导致 token 实际被用且泄露到版本控制
- **修复**: 字段改为只接受环境变量名（如 `GITEE_TOKEN`），移除 fallback；config.yml 中改为占位符；轮换已泄露的 token

### ~~2. `--dry-run` 参数是死代码~~ ✅
- **文件**: `main.py:153`, `main.py:211`
- **问题**: `--dry-run` 定义了 `default=True`，但实际逻辑是 `dry_run = not args.auto_apply`，该参数完全不起作用，误导用户
- **修复**: 删除 `--dry-run` 参数定义，或让逻辑正确读取该参数值

### ~~3. `extract_latest_exception_block` 遇空行即截断~~ ✅
- **文件**: `agents/auto_fix_agent.py:265`
- **问题**: `if collected and not line.strip(): break`，Java 堆栈中 `"... 15 more"` 前通常有空行，会导致堆栈被截断，后续定位全部失败
- **修复**: 改用更智能的终止条件（连续两个空行、或遇到新的日志时间戳格式时才停止）

### ~~4. CI 重试时未还原文件状态~~ ✅
- **文件**: `agents/auto_fix_agent.py:794-796`
- **问题**: 编译失败后拿到新补丁直接覆盖已修改文件，但 LLM 生成的新补丁基于原始代码，文件已被第一次补丁修改，上下文对不上
- **修复**: 重试前先 `git checkout` 还原到修复分支的初始提交状态，再应用新补丁

### ~~5. `_apply_diff_fallback` 新建文件场景 bug~~ ✅
- **文件**: `tools/git_manager.py:177`
- **问题**: `@@ -0,0 +1,N @@` 时 `old_count` 应为 0，但代码默认为 1，会导致对空文件切片越界或静默损坏
- **修复**: 当 `m.group(2)` 为 None 时默认 0 而非 1

---

## ~~P1 - 应该修复（正确性 & 健壮性）~~ ✅ 全部完成

### ~~6. `_validate_java_structure` 的 `_extract_sections` 逻辑错误~~ ✅
- **文件**: `agents/auto_fix_agent.py:1025`
- **问题**: `elif` 条件在遇到 package 声明后的注解或注释时就会 break，导致后面的 import 语句全部被忽略，验证结果不可靠
- **修复**: 重写 `_extract_sections` 逻辑，正确处理 package → imports → code 的分段

### ~~7. 方法签名正则不识别 package-private 方法~~ ✅
- **文件**: `agents/auto_fix_agent.py:1053`
- **问题**: 正则要求 `public|protected|private`，无修饰符的方法（Java 中合法的 package-private）不会被检测到，删了也不会被拦截
- **修复**: 正则改为可选匹配访问修饰符

### ~~8. `commit_changes(files=None)` 执行 `git add -A`~~ ✅
- **文件**: `tools/git_manager.py:54`
- **问题**: 会把工作区所有变更都提交进去，包括无关文件
- **修复**: 改为只 add 指定文件，或在 `apply_patch` 后显式传入修改的文件列表

### ~~9. `apply_patch` JSON 格式无路径穿越保护~~ ✅
- **文件**: `tools/git_manager.py:104`
- **问题**: 直接 `os.path.join(repo_path, rel)`，LLM 返回 `../../etc/passwd` 可写到仓库外。虽然 `validate_patch` 有检查，但 `apply_patch` 自身无防御
- **修复**: 在 `apply_patch` 中加入 `os.path.realpath` 路径校验，确保结果在 repo_path 内

### ~~10. LLM API 调用无重试机制~~ ✅
- **文件**: `integrations/llm_client.py`
- **问题**: 遇到 429/500 直接返回 `NO_SAFE_PATCH`，LLM API 不稳定是常态
- **修复**: 加入指数退避重试（3 次），区分可重试错误（429/500/502/503）和不可重试错误（401/403/404）

---

## P2 - 版本内改进（工程 & 体验）

### 11. 依赖管理混乱
- **文件**: `pyproject.toml`
- **问题**: `openai` 和 `gitpython` 从未被 import（实际用 `requests` 和 `subprocess`）；所有依赖版本为 `*`
- **修复**: 移除未使用的包，锁定版本范围，添加 dev 依赖组（pytest、ruff 等）

### 12. `build_prompt` 截断策略有盲区
- **文件**: `agents/auto_fix_agent.py:442-446`
- **问题**: 大文件取前 100 + 后 50 行，中间省略。如果 bug 在 101-150 行，LLM 看不到
- **修复**: 改为以问题行号为中心取 ±50 行上下文窗口

### ~~13. Gitee access token 通过 URL 参数传递~~ ✅
- **文件**: `integrations/gitee_client.py:32`
- **问题**: token 出现在 URL query string 中，会留在服务器日志、代理日志中
- **修复**: 改为 `Authorization: Bearer <token>` 请求头

### 14. `find_source_location` 的 `os.walk` fallback 性能问题
- **文件**: `agents/auto_fix_agent.py:358`
- **问题**: 对大仓库遍历全部文件找 `.java`，无深度限制或超时
- **修复**: 先用 `git ls-files` 做索引，或加深度/时间限制

---

## P3 - 技术债（架构 & 重构）

### 15. `auto_fix_agent.py` 1316 行过于臃肿
- **文件**: `agents/auto_fix_agent.py`
- **问题**: 一个文件承担堆栈解析、源码定位、prompt 构建、补丁验证、CI 流水线、测试生成、PR 推送所有职责
- **修复**: 拆分为 `stack_parser.py`、`patch_validator.py`、`ci_runner.py`、`test_generator.py` 等模块，`auto_fix_agent.py` 只做编排

### 16. `_select_best_frame` 重复调用 `find_source_location`
- **文件**: `agents/auto_fix_agent.py:281-283`
- **问题**: 调用一次 `find_source_location` 但丢弃结果，`run_pipeline` 又调用一次，重复 I/O
- **修复**: 让 `_select_best_frame` 返回 source_info

### 17. 死代码清理
- `agents/auto_fix_agent.py:612-668` — `_changes_are_localized` 从未被调用
- `integrations/gitee_client.py` — `get_repo_info` 从未被调用
- `integrations/llm_client.py` — `$` 前缀解析逻辑从未被触发
- `test_inference.py`、`test_prompt.py` — 手动脚本，不是自动化测试，应移入 `scripts/`

### 18. LLM system prompt 硬编码不可配置
- **文件**: `integrations/llm_client.py:54`
- **问题**: 系统提示词写死在代码中，用户无法定制
- **修复**: 从 config.yml 读取，提供默认值

---

## 文档改进

### 19. README 缺少配置文件最小示例
- 文档多次引用 `configs/config.yml`，但从没展示可运行的最小配置

### 20. 克隆路径写死了本地目录
- `README.md:33` 的 `cd D:\PycharmWorkspace\auto-fix-agent` 对其他开发者无意义

### 21. 缺少日志输入格式说明
- 只展示了一个堆栈示例，没有说明支持什么日志格式（Logback? Log4j? 多异常? JSON?）

### 22. `scripts/demo.ps1` 在项目结构中列出但实际不存在
- `README.md:133` 引用了不存在的文件
