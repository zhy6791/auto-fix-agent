# Auto-Fix Agent for Java Web Services

一个用 Python 实现的自动修复 Java Web 服务异常的 demo agent。通过读取日志、分析堆栈、定位源码、调用 LLM 生成补丁、以及在本地创建修复分支，快速定位和修复常见异常。

**核心特性：**
- 📋 自动提取日志中的异常堆栈信息
- 🔍 智能定位 Java 源码文件和问题行号（双路径：堆栈定位 + LLM 推断）
- 🤖 使用 OpenAI LLM 生成最小化补丁
- 🔀 在本地仓库创建修复分支并提交
- 🔨 自动编译检查 + 单元测试（`mvn compile` / `mvn test`）
- 🆕 **自动生成 JUnit5 单元测试**（异常复现 + 边界值 + 回归测试）
- 🔄 编译/测试失败时自动反馈 LLM 重试修复
- 🚀 通过后自动推送并创建 Gitee Pull Request
- ✅ 支持 dry-run 模式（仅分析不修改）
- 🧪 完整的单元测试覆盖（79+ 测试通过）

---

## 系统要求

- **Python**: 3.6+ (推荐 3.8+)
- **Git**: 需要安装 git 命令行工具
- **uv**: Python 包管理工具（pip 的快速替代品）
  - Windows 安装：从 https://github.com/astral-sh/uv 下载或 `pip install uv`

---

## 快速开始

### 1. 克隆或下载项目

```powershell
cd D:\PycharmWorkspace\auto-fix-agent
```

### 2. 安装 Python 依赖

使用 `uv` 安装依赖（推荐）：

```powershell
uv install
```

或使用 `pip`：

```powershell
pip install pyyaml requests openai gitpython
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

这将：
1. 创建一个 `fix/auto-<timestamp>` 分支
2. 应用 LLM 生成的补丁到工作区
3. 提交修改到修复分支
4. 执行编译检查（`mvn compile` 或 `gradle compileJava`）
5. 若编译失败，将错误反馈给 LLM 重试修复（最多 `max_retries` 次）
6. 编译通过后，推送分支到远程并创建 Gitee PR

### 方式 3: 跳过编译/测试

```powershell
# 跳过编译检查
python -m main --config configs/config.yml --auto-apply --no-compile

# 跳过单元测试
python -m main --config configs/config.yml --auto-apply --no-tests
```

### 方式 4: 启用详细日志

```powershell
python -m main --config configs/config.yml --auto-apply --verbose
```

### 方式 5: CLI 参数覆盖配置

```powershell
# 强制启用 PR 创建
python -m main --config configs/config.yml --auto-apply --create-pr

# 设置最大重试 5 次
python -m main --config configs/config.yml --auto-apply --max-retries 5
```

---

---

## 项目结构

```
auto-fix-agent/
├── agents/
│   ├── __init__.py
│   └── auto_fix_agent.py          # 核心 agent 流水线
├── tools/
│   ├── __init__.py
│   ├── file_io.py                 # 文件操作
│   ├── exec_cmd.py                # 命令执行
│   └── git_manager.py             # Git 操作（分支、提交、推送）
├── integrations/
│   ├── __init__.py
│   ├── llm_client.py              # OpenAI LLM 客户端
│   └── gitee_client.py            # Gitee API 客户端（PR 创建）
├── configs/
│   └── config.yml                 # 用户配置文件（需填写）
├── tests/
│   ├── test_file_io.py
│   ├── test_exec_cmd.py
│   ├── test_git_manager.py
│   ├── test_auto_fix_agent.py
│   ├── test_llm_client.py
│   ├── test_gitee_client.py
│   ├── test_patch_formats.py
│   └── test_cli_integration.py
├── scripts/
│   └── demo.ps1                   # PowerShell 演示脚本
├── main.py                        # CLI 入口
├── pyproject.toml                 # 依赖管理
├── README.md                      # 本文件
└── demo-plan.md                   # 开发计划
```

---


## 工作流示例

假设你的 Java Web 服务在运行时抛出了异常：

```
java.lang.NullPointerException: Cannot invoke "java.lang.String.length()" because "str" is null
	at com.example.demo.controller.HelloController.sayHello(HelloController.java:42)
```

**步骤 1: 检查日志**

日志已写入 `D:\workspace\mall-service\logs\app.log`。

**步骤 2: 运行 agent（dry-run 模式）**

```powershell
python -m main --config configs/config.yml --dry-run
```

Agent 将：
1. 读取日志文件
2. 解析异常堆栈：定位到 `HelloController.java:42`
3. 读取源码文件和上下文
4. 调用 OpenAI LLM 生成补丁（例如添加 null check）
5. 校验补丁（确保改动行数不超过限制）
6. 输出建议的修复方案

**步骤 3: 检查补丁（可选）**

在 `agent_report_fix_*.json` 中查看完整补丁内容。

**步骤 4: 应用补丁（真实运行）**

```powershell
python -m main --config configs/config.yml --auto-apply
```

Agent 将：
1. 创建本地分支 `fix/auto-<timestamp>`
2. 应用补丁到工作区文件并提交
3. 执行编译检查（`mvn compile`）
4. 若配置了 `run_tests_on_apply: true`，执行单元测试
5. 若编译/测试失败，自动反馈 LLM 重试修复
6. 通过后推送分支并创建 Gitee PR

**步骤 5: 人工复审 PR**

```powershell
# PR 已自动创建，访问 Gitee 查看
# 例如: https://gitee.com/owner/repo/pulls/108

# 或进入 repo 手动查看
cd D:\workspace\mall-service
git log fix/auto-20260507-150000
```

## 修复逻辑详解

### 核心修复流程

Agent 采用**双路径架构**，根据异常堆栈类型选择不同的源码定位策略：

```
异常日志
  ↓
解析堆栈 → 提取 exception_type, class_name, method, line_no
  ↓
选择路径？
  ├─ 有业务代码 frame（app code in stack）
  │   └─→ 【传统路径】直接从堆栈定位源文件
  │       └─→ find_source_location()
  │
  └─ 无业务代码 frame（纯框架代码 stack）
      └─→ 【推断路径】从异常消息 LLM 推断源文件
          └─→ _infer_from_exception_message()
              └─→ LLM 分析异常消息 → 推断类名、方法名
              └─→ 验证文件存在
  ↓
构建 LLM 提示词
  ├─ 提供完整源文件内容
  ├─ 提供问题代码片段（± 3-5 行上下文）
  ├─ 明确约束条件（修改行数、安全规则等）
  └─ 如果是推断路径，标记特殊上下文
  ↓
LLM 生成补丁（JSON 格式）
  ├─ patched_content: 修复后的完整源文件
  └─ 必须保证可直接编译
  ↓
【6 层补丁验证】
  1. Package 声明不变
  2. Import 语句不删除/修改
  3. 类名/方法签名不变
  4. 方法不在问题窗口外删除
  5. 文件大小不低于原来 50%
  6. 不包含行号前缀（如 "59: code"）
  ↓
应用补丁
  ├─ 创建修复分支 fix/auto-<timestamp>
  ├─ 应用 JSON 补丁
  └─ 提交修改（commit message 含异常类型）
  ↓
【CI 管道】
  ├─ 编译检查（mvn compile / gradle compileJava）
  │   └─ 失败 → LLM 反馈重试（最多 max_retries 次）
  │       └─ 仍失败 → 记录失败，终止
  └─ 单元测试（mvn test / gradle test）[可选]
      └─ 失败 → LLM 反馈重试（同编译流程）
  ↓
【推送 & PR】
  ├─ git push 修复分支到远程
  └─ Gitee API 创建 Pull Request
  ↓
✓ 修复完成
```

### 两条修复路径对比

| 特性 | 传统路径（stack_trace） | 推断路径（exception_inference） |
|------|----------------------|------------------------------|
| 触发条件 | 堆栈有业务代码 frame | 堆栈全是框架代码 |
| 源码定位方式 | 从 class_name → Java 文件 | LLM 分析异常消息推断 |
| 适用异常 | NullPointerException, IndexOutOfBoundsException, etc. | MissingPathVariableException, BindingException, etc. |
| 定位精度 | 精确到行号 | 推断到方法级别 |
| 成功率 | 高（仓库内有源码） | 中（取决于 LLM 推断） |

---

## 自动测试生成

Agent 支持在修复后自动生成 **Maven + JUnit5** 测试，并将其纳入 PR 门禁。

### 开启配置

```yaml
test_generation:
  enabled: true
  strategy: "B"      # A: 仅目标方法 / B: 目标+边界+回归
  framework: "junit5"
  max_test_cases: 5
  max_test_files: 1

run_compile_on_apply: true
run_tests_on_apply: true

gitee:
  require_tests_to_pass_for_pr: true
```

### Strategy B 覆盖范围

- 异常复现
- 修复验证
- 边界值
- 回归测试

### 规则

启用测试生成后，必须 **编译通过 + 测试通过** 才会创建 PR。

### 使用

直接运行：

```bash
python -m main --config configs/config.yml --auto-apply
```

---

## 可修复的 Bug 类型

### 1. 业务逻辑异常（传统路径）✅

**条件**：堆栈中有业务代码帧（如 `com.fixflow.mall.service.OrderService`）

#### 示例 1: 空指针异常 (NullPointerException)

**异常堆栈特征**：包含 `java.lang.NullPointerException` 和业务方法帧

#### 示例 2: 数组越界异常 (IndexOutOfBoundsException)

**异常堆栈特征**：包含 `java.lang.IndexOutOfBoundsException` 和业务方法帧

#### 示例 3: 类型转换异常 (ClassCastException)


---

### 2. 框架配置异常（推断路径）✅ 

**条件**：堆栈全是框架代码（Spring/Tomcat/Jakarta），无业务帧

#### 示例 1: @PathVariable 绑定错误


**异常堆栈特征**：`org.springframework.web.bind.MissingPathVariableException` + "Required URI template variable 'xxx' ... is not present"

#### 示例 2: @RequestParam 缺失

**异常堆栈特征**：`org.springframework.web.bind.MissingServletRequestParameterException`

---

### 3. 当前不支持的 Bug 类型 ❌

以下类型的异常因需要外部资源/配置变更，暂不自动修复：

- **数据库连接异常** - 需要修改配置文件/环境变量
- **依赖包缺失** - 需要修改 pom.xml/build.gradle
- **权限异常** - 需要改变系统配置
- **业务逻辑错误**（无堆栈指向） - 需要深度业务分析
- **分布式系统异常** - 涉及多个服务交互

---

## 安全保障机制

Agent 包含多层安全保障，确保生成的补丁不会损坏源码：

### 1. 提示词层面
- 明确要求返回**完整可编译的 Java 文件**
- 禁止返回代码片段、省略号或占位符
- 禁止删除 package、import、方法签名

### 2. 响应验证层面
- 检测行号前缀格式（防止 LLM 复制 "59: code" 格式）
- JSON 结构验证

### 3. 补丁验证层面（6 层验证）

| 层级 | 检查项 | 目的 |
|------|--------|------|
| 1 | Package 声明 | 不破坏包结构 |
| 2 | Import 语句 | 不删除依赖 |
| 3 | 方法签名 | 不改变 API |
| 4 | 方法删除检测 | 不删除无关方法 |
| 5 | 文件大小 | 不意外截断 |
| 6 | 行号前缀 | 不包含 LLM 格式化伪代码 |

若任何检查失败，补丁被拒绝，异常被报告。

### 4. Git 层面
- 所有改动在独立分支 `fix/auto-*` 中进行
- 提交信息包含异常类型，方便追溯
- 支持 dry-run 模式先查看再应用
- 重试补丁有独立 commit，避免污染初始修复

---

## CI 管道与自动 PR

补丁应用后，agent 可自动执行编译检查、单元测试，通过后推送并创建 PR。

### 编译检查

优先使用项目自带的 wrapper 脚本（`mvnw`/`gradlew`），找不到才用系统命令：

```
mvnw.cmd → mvnw → mvn.cmd → mvn.bat → mvn    (Maven)
gradlew.bat → gradlew → gradle.bat → gradle   (Gradle)
```

### LLM 重试反馈

若编译或测试失败，agent 不会直接放弃，而是：

1. 提取错误输出（stderr/stdout，截断至 3000 字符）
2. 构建重试提示词（含原始任务上下文 + 错误信息）
3. 调用 LLM 生成修正补丁
4. 校验新补丁（6 层验证）
5. 应用新补丁，重新编译/测试
6. 重复直至通过或耗尽 `max_retries`（默认 3）

若 LLM 返回 `NO_SAFE_PATCH`，或构建工具未安装（`Command not found`），则立即停止重试。

### Gitee PR 创建

编译通过后，agent 自动：

1. 推送修复分支到远程仓库
2. 调用 Gitee API v5 创建 PR
3. PR 描述中包含异常信息和 CI 管道状态

---

## 后续开发建议

- [ ] 支持更多 LLM provider（LLaMA、Claude、etc)
- [ ] 添加 GUI 界面
- [ ] 支持多语言堆栈格式（Python、Node.js、etc）
- [ ] 集成 IDE 插件（VS Code、IntelliJ）
- [x] ~~自动编译检查与单元测试~~ — 已实现
- [x] ~~CI 失败时自动重试修复~~ — 已实现
- [x] ~~自动创建 Gitee Pull Request~~ — 已实现
- [x] ~~自动生成 JUnit5 单元测试（Strategy B）~~ — 已实现 ✨
- [ ] 支持 Option A（仅目标方法单测）
- [ ] 支持 Gradle 和 TestNG
- [ ] 修复质量评分
- [ ] 支持更多框架异常（Quarkus、Micronaut、etc）
- [ ] 历史修复记录和学习反馈

---

## 许可证

MIT License

---

## 贡献

欢迎提交 Issue 和 PR！
