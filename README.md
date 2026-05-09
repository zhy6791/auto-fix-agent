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

## 可修复的 Bug 类型

### 1. 业务逻辑异常（传统路径）✅

**条件**：堆栈中有业务代码帧（如 `com.fixflow.mall.service.OrderService`）

#### 示例 1: 空指针异常 (NullPointerException)

```java
// ❌ 原始代码
public Long firstItemId(Long orderId) {
    MallOrder order = orderRepository.findById(orderId).orElseThrow();
    return order.getItemIds().get(0);  // itemIds 可能为空
}

// ✓ 修复后
public Long firstItemId(Long orderId) {
    MallOrder order = orderRepository.findById(orderId).orElseThrow();
    if (order.getItemIds() == null || order.getItemIds().isEmpty()) {
        return null;
    }
    return order.getItemIds().get(0);
}
```

**异常堆栈特征**：包含 `java.lang.NullPointerException` 和业务方法帧

#### 示例 2: 数组越界异常 (IndexOutOfBoundsException)

```java
// ❌ 原始代码
public Item getFirstItem(List<Item> items) {
    return items.get(0);  // 列表可能为空
}

// ✓ 修复后
public Item getFirstItem(List<Item> items) {
    if (items == null || items.isEmpty()) {
        return null;
    }
    return items.get(0);
}
```

**异常堆栈特征**：包含 `java.lang.IndexOutOfBoundsException` 和业务方法帧

#### 示例 3: 类型转换异常 (ClassCastException)

```java
// ❌ 原始代码
public String processData(Object data) {
    return ((String) data).toUpperCase();  // data 可能不是 String
}

// ✓ 修复后
public String processData(Object data) {
    if (!(data instanceof String)) {
        return "";
    }
    return ((String) data).toUpperCase();
}
```

---

### 2. 框架配置异常（推断路径）✅ 

**条件**：堆栈全是框架代码（Spring/Tomcat/Jakarta），无业务帧

#### 示例 1: @PathVariable 绑定错误

```java
// ❌ 原始代码
@GetMapping("/orders/{orderId}")
public MallOrder getOrder(@PathVariable("id") Long orderId) {  // 参数名不匹配
    return orderService.findOrder(orderId);
}

// ✓ 修复后
@GetMapping("/orders/{id}")
public MallOrder getOrder(@PathVariable Long id) {  // 匹配路径变量
    return orderService.findOrder(id);
}
```

**异常堆栈特征**：`org.springframework.web.bind.MissingPathVariableException` + "Required URI template variable 'xxx' ... is not present"

#### 示例 2: @RequestParam 缺失

```java
// ❌ 原始代码
@GetMapping("/search")
public List<Product> search(@RequestParam String query) {  // 参数必需但请求未提供
    return productService.search(query);
}

// ✓ 修复后
@GetMapping("/search")
public List<Product> search(@RequestParam(required = false) String query) {
    return productService.search(query != null ? query : "");
}
```

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
- 不自动提交（需人工复审）
- 支持 dry-run 模式先查看再应用

---

## 后续开发建议

- [ ] 支持更多 LLM provider（LLaMA、Claude、etc）
- [ ] 添加 GUI 界面
- [ ] 支持多语言堆栈格式（Python、Node.js、etc）
- [ ] 集成 IDE 插件（VS Code、IntelliJ）
- [ ] 自动测试与验证
- [ ] 修复质量评分
- [ ] 支持更多框架异常（Quarkus、Micronaut、etc）
- [ ] 历史修复记录和学习反馈

---

## 许可证

MIT License

---

## 贡献

欢迎提交 Issue 和 PR！

