# OrcaLoca Bug 定位集成计划

## 背景

当前 auto-fix-agent 使用简单的基于堆栈跟踪的 bug 定位方式：解析异常日志 → 按类名查找源文件 → 让 LLM 生成补丁。这种方式在简单场景下有效，但在以下情况表现不佳：
- 堆栈跟踪中只有框架代码（Spring/Tomcat），没有应用代码帧
- Bug 所在方法与堆栈顶部方法不同，根因远离异常抛出点
- 调用链复杂，需要跨多个类/方法追踪数据流

OrcaLoca（ICML 2025，arXiv:2502.00350）是一个用于软件问题定位的 LLM Agent 框架，核心技术包括：
- **CodeGraph**：基于 AST 的有向代码图（包含边 + 引用边）
- **优先级动作调度（ASQ）**：堆优先队列 + 去重 + 计数器提升机制
- **动作分解与相关性评分**：将类级搜索分解为方法级，用 LLM/启发式评分排序
- **距离感知上下文剪枝**：基于图最短路径保留最相关的候选位置

**策略：增强而非替换。** 保留现有 ReAct Agent 和所有工具不变，OrcaLoca 技术作为增强模式叠加，通过配置开关控制。

## 实现阶段

### 阶段 1：Java CodeGraph（`agents/java_codegraph.py`）— 新建文件

使用 `tree-sitter-java` 构建 Java 代码库的 AST 有向图。

**数据结构：**
- `CodeEntity`：uid、entity_type（file/class/interface/method/field）、name、qualified_name、file_path、start_line、end_line、parent_uid、children_uids、references、referenced_by、metadata
- `JavaCodeGraph`：entities 字典、file_index、class_index、name_index、reference_graph、reverse_reference_graph、邻接矩阵

**核心方法：**
- `build()` — 扫描 `src/main/java/**/*.java`，用 tree-sitter-java 解析每个文件
- `_parse_file(path)` — 提取类、方法、字段、方法调用（AST 节点类型：`class_declaration`、`method_declaration`、`method_invocation` 等）
- `_build_references()` — 通过 class_index 解析方法调用目标
- `find_class(qualified_name, simple_name)` — 按名称查找类
- `find_method_in_class(class_uid, method_name)` — 在类中查找方法
- `find_callable(entity_uid, direction)` — 通过引用边查找调用者/被调用者
- `shortest_path(uid_a, uid_b)` — 基于 BFS 的最短路径计算
- `save(cache_path)` / `load(cache_path)` — JSON 序列化，支持增量构建

**UID 格式：** `仓库相对路径::ClassName::methodName`（方法）、`仓库相对路径::ClassName`（类）

**依赖：** 在 `pyproject.toml` 中添加 `tree-sitter>=0.22.0` 和 `tree-sitter-java>=0.23.0`

### 阶段 2：动作调度队列（`agents/action_scheduler.py`）— 新建文件

基于 OrcaLoca ASQ 的优先级搜索动作调度。

**数据结构：**
- `SearchAction`：action_id、action_type、priority（基础=1.0，分解后=2.0）、parameters、created_at、parent_action_id、generation_count
- `ActionSchedulerQueue`：堆优先队列 + 去重机制

**核心方法：**
- `push(action)` — 添加动作，去重检查；同一动作生成 3 次后提升优先级 +0.5（计数器机制）
- `pop()` — 返回最高优先级动作
- `is_empty()`、`clear()`、`size()`

### 阶段 3：动作分解器（`agents/action_decomposer.py`）— 新建文件

当搜索返回一个类时，将其分解为方法级动作并评分。

**核心方法：**
- `decompose_class(class_uid, bug_context)` — 获取类中所有方法，评分后返回 top-k 个 SearchAction（priority=2.0）
- `score_relevance(method_entity, bug_context)` — 综合评分：
  - 名称匹配：方法名出现在堆栈跟踪中 → +0.5
  - 引用邻近度：调用/被调用堆栈帧方法 → +0.3
  - 结构启发式：入口方法（@RequestMapping）加分，防御性代码（try-catch）减分
  - LLM 评分（可选，开销大）：仅当启发式评分在 0.3–0.7 模糊区间时使用

### 阶段 4：上下文剪枝器（`agents/context_pruner.py`）— 新建文件

距离感知剪枝，仅保留最相关的搜索结果。

**核心方法：**
- `set_bug_seeds(uids)` — 从堆栈帧设置初始种子
- `add_candidate(entity, score)` — 添加搜索结果
- `prune()` — 计算到种子的平均最短路径距离，按 `score - λ * distance` 保留 top-k（k=12）
- `get_context_summary()` — 生成 LLM 上下文的文本摘要

### 阶段 5：新增搜索工具（`agents/tool_registry.py`）— 修改

在现有 8 个工具基础上新增 5 个工具（原有工具不变）：

| 工具 | 描述 |
|------|------|
| `search_class` | 按名称查找类 → 自动触发方法级分解 |
| `search_method_in_class` | 在指定类中查找方法 |
| `search_callable` | 追踪调用者/被调用者（通过引用边） |
| `search_file_contents` | 全文/正则搜索 Java 文件 |
| `search_source_code` | 名称 + 内容组合搜索 |

**`ToolRegistry.__init__` 修改：** 接受可选参数 `code_graph`、`action_scheduler`、`action_decomposer`、`context_pruner`。仅当 `code_graph` 不为 None 时注册这 5 个新工具。

### 阶段 6：ReAct Agent 集成（`agents/react_agent.py`）— 修改

**构造函数：** 接受可选的 `code_graph`、`action_scheduler`、`context_pruner`。从 config 读取 `self.orcaloca_enabled`。

**`run()` 方法修改：** 每次工具执行后，检查 ASQ 中是否有高优先级分解动作（priority >= 2.0）。如有，直接执行（跳过 LLM 决策）— 这是 OrcaLoca 的优先级调度机制。

**`_build_system_prompt()` 修改：** 启用时追加 OrcaLoca 工具描述和工作流说明。

**`_build_initial_user_message()` 修改：** 包含代码库摘要（包列表、类数量）和初始上下文剪枝候选。

### 阶段 7：流水线集成（`agents/auto_fix_agent.py`）— 修改

**新增方法 `_init_orcaloca()`：**
- 导入并实例化 JavaCodeGraph、ActionSchedulerQueue、ActionDecomposer、ContextPruner
- 尝试加载缓存图；未缓存则构建

**`__init__` 修改：** 当 `config.orcaloca.enabled` 为 true 时调用 `_init_orcaloca()`。

**`run_pipeline()` 阶段 2 修改：** 将 OrcaLoca 组件传递给 ReActAgent。调用 `_resolve_stack_to_uids()` 从解析的堆栈帧设置 bug 种子。

**新增方法 `_resolve_stack_to_uids(parsed_stack)`：** 将堆栈帧的类名/方法名映射到 CodeGraph UID。

**`ToolRegistry` 构造修改：** 可用时传递 OrcaLoca 组件。

### 阶段 8：配置（`configs/config.yml`）— 修改

```yaml
# OrcaLoca 增强 bug 定位
orcaloca:
  enabled: true                    # 开关
  graph_cache_path: ""             # CodeGraph 缓存路径（默认: repo/.codegraph_cache.json）
  incremental_build: true          # 仅重新解析变更文件
  max_queue_size: 50               # ASQ 最大队列长度
  context_top_k: 12                # 剪枝后保留的候选数
  decomposition_top_k: 5           # 每个类分解后保留的方法数
  distance_lambda: 0.3             # 距离在剪枝评分中的权重
  llm_scoring_enabled: false       # 是否使用 LLM 进行相关性评分（开销大）
  llm_scoring_threshold: 0.5       # 启发式评分在此阈值 ±0.2 范围内时才用 LLM
  parse_test_code: false           # 是否将测试代码纳入图
  exclude_patterns:                # 排除的 glob 模式
    - "**/generated/**"
    - "**/target/**"
    - "**/build/**"
```

### 阶段 9：测试

| 文件 | 覆盖内容 |
|------|----------|
| `tests/test_java_codegraph.py` | 图构建、实体提取、引用边、find_class、最短路径、save/load、增量构建 |
| `tests/test_action_scheduler.py` | 优先级排序、去重、计数器提升、max_size |
| `tests/test_action_decomposer.py` | 分解、相关性评分、top-k、mock LLM 评分 |
| `tests/test_context_pruner.py` | 添加/剪枝候选、距离计算、上下文摘要 |
| `tests/test_orcaloca_tools.py` | 5 个新工具端到端测试、与 ToolRegistry 集成 |
| `tests/test_orcaloca_integration.py` | 启用 OrcaLoca 的完整流水线、禁用时的回退、向后兼容 |

## 需修改/创建的文件

| 文件 | 操作 |
|------|------|
| `agents/java_codegraph.py` | **新建** — CodeGraph 构建与查询 |
| `agents/action_scheduler.py` | **新建** — ASQ 优先队列 |
| `agents/action_decomposer.py` | **新建** — 相关性评分与分解 |
| `agents/context_pruner.py` | **新建** — 距离感知剪枝 |
| `agents/tool_registry.py` | **修改** — 新增 5 个工具，扩展构造函数 |
| `agents/react_agent.py` | **修改** — ASQ 集成，更新系统提示 |
| `agents/auto_fix_agent.py` | **修改** — OrcaLoca 初始化，组件传递 |
| `configs/config.yml` | **修改** — 添加 orcaloca 配置段 |
| `pyproject.toml` | **修改** — 添加 tree-sitter 依赖 |
| `tests/test_java_codegraph.py` | **新建** |
| `tests/test_action_scheduler.py` | **新建** |
| `tests/test_action_decomposer.py` | **新建** |
| `tests/test_context_pruner.py` | **新建** |
| `tests/test_orcaloca_tools.py` | **新建** |
| `tests/test_orcaloca_integration.py` | **新建** |

## 验证方案

1. **单元测试：** `python -m pytest tests/test_java_codegraph.py tests/test_action_scheduler.py tests/test_action_decomposer.py tests/test_context_pruner.py tests/test_orcaloca_tools.py -v`
2. **集成测试：** `python -m pytest tests/test_orcaloca_integration.py -v`
3. **回归测试：** `python -m pytest tests/ -v` — 所有现有测试必须通过（向后兼容）
4. **手动测试：** 设置 `orcaloca.enabled: false` 验证原有行为不变；设置 `orcaloca.enabled: true` 验证增强定位功能
