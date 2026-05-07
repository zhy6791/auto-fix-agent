# Auto-Fix Agent for Java Web Services

一个用 Python 实现的自动修复 Java Web 服务异常的 demo agent。通过读取日志、分析堆栈、定位源码、调用 LLM 生成补丁、以及在本地创建修复分支，快速定位和修复常见异常。

**核心特性：**
- 📋 自动提取日志中的异常堆栈信息
- 🔍 智能定位 Java 源码文件和问题行号
- 🤖 使用 OpenAI LLM 生成最小化补丁
- 🔀 在本地仓库创建修复分支（但不提交）
- ✅ 支持 dry-run 模式（仅分析不修改）
- 🧪 完整的单元测试覆盖（36+ 测试通过）

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

### 3. 配置 OpenAI API

#### 选项 A: 使用官方 OpenAI API

在 Windows 环境变量中设置 API key：

```powershell
# PowerShell 临时设置（仅当前 session）
$env:OPENAI_API_KEY = "sk-xxxxxxxxxxxx"

# 或永久设置系统环境变量
[System.Environment]::SetEnvironmentVariable('OPENAI_API_KEY', 'sk-xxxxxxxxxxxx', 'User')
```

#### 选项 B: 使用自定义 OpenAI 兼容 API

编辑 `configs/config.yml`，修改 `llm.base_url`：

```yaml
llm:
  base_url: "https://your-custom-api.com/v1"
  api_key_env: "YOUR_API_KEY_ENV_VAR"
```

### 4. 配置项目路径

编辑 `configs/config.yml`，填入你的本地项目的路径：

```yaml
logs_path: "D:\\workspace\\mall-service\\logs\\app.log"
repo_path: "D:\\workspace\\mall-service"
java_build: "maven"
branch_prefix: "fix/"
max_patch_lines: 40
```

**字段说明：**
- `logs_path`: 你的 Java Web 服务日志文件路径
- `repo_path`: 你的 Maven 项目根目录路径（必须是 git repo）
- `java_build`: 构建工具（`maven` 或 `gradle`）
- `max_patch_lines`: 补丁最大改动行数（安全限制）
- `auto_apply`: 是否自动应用补丁（默认 false）

---

## 使用方式

### 方式 1: Dry-Run 模式（仅分析，不修改代码）

```powershell
python -m main --config configs/config.yml --dry-run
```

输出示例：
```
======================================================================
AUTOFIX AGENT REPORT
======================================================================

Status: completed
Dry Run: True

📋 Parsed Frames: 1
  [0] com.example.demo.controller.HelloController.sayHello() @ line 42

📁 Located Files:
  - src/main/java/com/example/demo/controller/HelloController.java (line 42)

🔀 Branch Name: fix/auto-20260507-150000

📝 Patch Preview:
{"files": [{"path": "src/main/java/com/example/demo/controller/HelloController.java"...
```

### 方式 2: 自动应用模式（创建分支并修改文件）

```powershell
python -m main --config configs/config.yml --auto-apply
```

这将：
1. 创建一个 `fix/auto-<timestamp>` 分支
2. 应用 LLM 生成的补丁到工作区
3. 修改相关文件（但不执行 git commit）
4. 输出修改详情

### 方式 3: 启用详细日志

```powershell
python -m main --config configs/config.yml --auto-apply --verbose
```

---

## 演示脚本

提供了一个 PowerShell 演示脚本 `scripts/demo.ps1`，可一键运行完整演示：

```powershell
# 以 dry-run 模式运行
.\\scripts\\demo.ps1 -DryRun

# 以自动应用模式运行（需谨慎）
.\\scripts\\demo.ps1 -AutoApply
```

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
│   └── git_manager.py             # Git 操作
├── integrations/
│   ├── __init__.py
│   └── llm_client.py              # OpenAI LLM 客户端
├── configs/
│   └── config.yml                 # 用户配置文件（需填写）
├── tests/
│   ├── test_file_io.py
│   ├── test_exec_cmd.py
│   ├── test_git_manager.py
│   ├── test_auto_fix_agent.py
│   ├── test_llm_client.py
│   └── test_patch_formats.py
├── scripts/
│   └── demo.ps1                   # PowerShell 演示脚本
├── main.py                        # CLI 入口
├── pyproject.toml                 # 依赖管理
├── README.md                      # 本文件
└── demo-plan.md                   # 开发计划
```

---

## 测试

运行完整的单元测试：

```powershell
python -m pytest tests/ -v
```

运行特定测试：

```powershell
# 工具模块测试
python -m pytest tests/test_file_io.py -v
python -m pytest tests/test_git_manager.py -v

# Agent 流水线测试
python -m pytest tests/test_auto_fix_agent.py -v

# 补丁格式测试
python -m pytest tests/test_patch_formats.py -v
```

**测试覆盖：**
- Phase 2 工具模块：22 个测试 ✅
- Phase 3 Agent 流水线：4 个测试 ✅
- Phase 4 LLM 集成：5 + 5 个测试 ✅
- 合计：36+ 个单元测试

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
2. 应用补丁到工作区文件
3. 输出修改详情

**步骤 5: 人工复审和提交**

```powershell
# 进入 repo
cd D:\workspace\mall-service

# 查看修改
git diff fix/auto-20260507-150000

# 可选：运行测试
mvn test

# 如果通过，可提交 PR 或 merge
git add .
git commit -m "fix(NullPointerException): Add null check in sayHello"
git push origin fix/auto-20260507-150000
```

---

## 安全说明

⚠️ **重要注意事项：**

1. **API Key 保护**：不要把 OpenAI API key 提交到代码仓库。使用环境变量存储。
2. **Backup 建议**：在 `--auto-apply` 之前，建议备份或在临时 clone 上测试。
3. **LLM 限制**：LLM 生成的补丁可能不完全正确，始终需要人工复审。
4. **Dry-Run 优先**：始终先用 `--dry-run` 查看建议的修改。

---

## 配置文件完整示例

```yaml
# 日志路径（绝对路径或相对路径）
logs_path: "D:\\workspace\\mall-service\\logs\\app.log"

# Maven 项目根路径（必须是 git repo）
repo_path: "D:\\workspace\\mall-service"

# 构建工具
java_build: "maven"

# 修复分支前缀
branch_prefix: "fix/"

# LLM 配置
llm:
  provider: "openai"
  model: "gpt-4o-mini"             # 或 gpt-4 / gpt-3.5-turbo
  api_key_env: "OPENAI_API_KEY"    # 环境变量名
  base_url: "https://api.openai.com/v1"  # 可选：自定义 API 端点
  temperature: 0.2                 # 0-1 越低越确定

# 安全约束
max_patch_lines: 40                # 单个补丁最多改动行数

# 自动应用模式
auto_apply: false

# 应用补丁后是否运行测试
run_tests_on_apply: false
```

---

## 常见问题（FAQ）

**Q: 如何查看 LLM 调用的日志？**

A: 启用 `--verbose` 标志：
```powershell
python -m main --config configs/config.yml --verbose
```

**Q: Agent 无法找到源文件怎么办？**

A: 确保：
1. 堆栈中的类名与实际文件名匹配
2. repo_path 指向正确的 Maven 项目根目录
3. 源文件在 `src/main/java/` 或 `src/test/java/` 下

**Q: 补丁应用失败？**

A: 检查：
1. 文件是否存在
2. 是否有权限修改文件
3. 补丁格式是否正确（JSON 或 unified-diff）

**Q: 可以修改 OpenAI 模型吗？**

A: 可以。编辑 `configs/config.yml` 的 `llm.model` 字段，或使用环境变量。

---

## 后续开发建议

- [ ] 支持更多 LLM provider（LLaMA、Claude、etc）
- [ ] 添加 GUI 界面
- [ ] 支持多语言堆栈格式（Python、Node.js、etc）
- [ ] 集成 IDE 插件（VS Code、IntelliJ）
- [ ] 自动测试与验证
- [ ] 修复质量评分

---

## 许可证

MIT License

---

## 贡献

欢迎提交 Issue 和 PR！

