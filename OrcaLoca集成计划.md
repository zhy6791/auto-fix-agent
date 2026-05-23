# OrcaLoca集成计划：增强auto-fix-agent的Bug定位能力

## Context

**问题**：当前auto-fix-agent的bug定位依赖两条路径——堆栈帧定位（准确性高但仅适用于有应用层代码帧的情况）和LLM推断定位（覆盖面广但准确性不足）。当堆栈全是框架代码或类名无法直接映射到文件时，定位成功率下降。

**目标**：引入OrcaLoca（ICML 2025）的Search阶段核心机制——知识图谱、倒排索引、相关性评分、优先级调度——作为ReAct Agent的新工具集，增强bug定位的准确性和覆盖率。

**集成策略**：浅层到中度集成。保留现有堆栈定位路径（工作良好），将OrcaLoca的搜索能力作为新工具注入ReAct循环，Agent自主选择使用时机。

---

## 需要创建的新模块（6个文件）

### 1. `agents/code_graph/__init__.py`
- 包初始化文件

### 2. `agents/code_graph/java_parser.py` - Java AST解析适配器
- 使用`javalang`库（纯Python，无JVM依赖）解析`.java`文件
- 提取：package、imports、类声明、方法签名、字段、注解、异常处理（try/catch/throws）
- 每个文件独立try/except，解析失败不影响其他文件
- 返回结构化dict，包含完整的类/方法/异常声明信息

### 3. `agents/code_graph/repo_graph.py` - Java仓库知识图谱
- 递归扫描`src/main/java/`和`src/test/java/`目录
- 构建networkx有向图：节点=类/方法/字段，边=调用/继承/实现/抛出异常
- 提供查询接口：`query_class()`、`query_method()`、`get_callers()`、`get_callees()`、`get_exception_flow()`
- 支持惰性初始化和磁盘缓存（以git commit hash为缓存键）

### 4. `agents/code_graph/inverted_index.py` - 倒排索引
- 索引映射：
  - exception_type -> [抛出/捕获该异常的class.method对]
  - class_name -> [file_path]
  - method_name -> [(class_name, file_path, line_no)]
  - annotation -> [被注解的类名]（如`@ExceptionHandler`、`@RestControllerAdvice`）

### 5. `agents/log_extraction/search_manager.py` - 结构化搜索API
- 6种搜索策略（适配Java）：
  1. `search_by_exception_type` - 按异常类型查找相关类/方法
  2. `search_by_class_name` - 按类名查找（增强版source_locator）
  3. `search_by_method_name` - 按方法名跨代码库搜索
  4. `search_by_annotation` - 按Spring注解查找（@Controller、@Service等）
  5. `search_by_import_pattern` - 按import模式查找
  6. `search_by_stack_context` - 多帧分析，结合调用图扩展搜索
- 每个策略返回`SearchResult`：`{file_path, class_name, method_name, line_no, context_snippet}`

### 6. `agents/log_extraction/code_scorer.py` - LLM相关性评分
- 接收候选位置列表+异常上下文，调用LLM对每个候选评分0-100
- 批量评分（单次LLM调用处理所有候选），最小化API开销
- 返回按分数排序的候选列表
- 使用现有`LLMClient.chat()`方法

---

## 需要修改的模块（4个文件）

### 7. `agents/agent_loop/tool_registry.py` - 新增3个工具
- `search_candidates`：暴露SearchManager，接收异常信息返回候选位置
- `score_candidates`：暴露CodeScorer，对候选进行相关性评分
- `query_code_graph`：暴露RepoGraph查询（callers、callees、exception flow）

### 8. `agents/agent_loop/react_agent.py` - 增强系统提示
- 在`_build_system_prompt()`中添加3个新工具的描述和使用指导
- 添加推荐工作流："locate_from_stack失败时，先用search_candidates，再用score_candidates，最后才用infer_source"
- 不修改ReAct循环结构本身

### 9. `agents/log_extraction/source_locator.py` - 添加RepoGraph回退
- `find_source_location()`增加可选的`repo_graph`参数
- 当基于路径的搜索失败时，回退到RepoGraph查找
- `select_best_frame()`在多候选时增加相关性评分

### 10. `agents/auto_fix_agent.py` - 管道初始化变更
- 添加RepoGraph惰性初始化逻辑
- 将repo_graph传递给ToolRegistry
- 处理`orcaloca`配置节

### 11. `configs/config.yml` - 新增配置节
```yaml
orcaloca:
  enabled: true
  build_graph_on_init: false     # 惰性初始化，首次搜索时构建
  max_candidates: 10
  score_threshold: 50
  enable_call_graph_search: true
  build_timeout: 60              # 图构建超时（秒）
```

---

## Agent增强工作流

```
迭代1: 分析堆栈
  Thought: 堆栈有应用层帧 com.example.OrderService.process(OrderService.java:42)
  Action: locate_from_stack(...)
  -> 成功：进入edit_code
  -> 失败：进入迭代2

迭代2: 使用代码图谱搜索
  Thought: locate_from_stack失败，使用代码图谱搜索
  Action: search_candidates({exception_type: "NullPointerException", strategy: "stack_context"})
  -> 返回：排序的候选位置列表

迭代3: 评分验证
  Thought: 获得5个候选，进行相关性评分
  Action: score_candidates({candidates: [...], exception_context: "..."})
  -> 返回：评分后的候选列表，选择最高分

迭代4（可选）: 深入调查
  Thought: 最高候选是OrderService.process，查看调用者
  Action: query_code_graph({query_type: "get_callers", ...})

迭代N: 生成修复
  Action: edit_code(...)
  Action: final_patch(...)
```

---

## 分阶段实施计划

### 阶段1：基础构建（第1-2周）
- 添加`javalang`依赖到`pyproject.toml`
- 实现`java_parser.py`（Java AST解析适配器）
- 实现`repo_graph.py`（仓库知识图谱）
- 实现`inverted_index.py`（倒排索引）
- 编写单元测试

### 阶段2：搜索与评分（第3-4周）
- 实现`search_manager.py`（6种搜索策略）
- 实现`code_scorer.py`（LLM相关性评分）
- 在`tool_registry.py`中注册3个新工具
- 编写单元测试

### 阶段3：Agent集成（第5周）
- 修改`auto_fix_agent.py`（惰性初始化RepoGraph）
- 修改`react_agent.py`（增强系统提示）
- 修改`source_locator.py`（添加RepoGraph回退）
- 添加`config.yml`配置节
- 编写集成测试

### 阶段4：优化与加固（第6周）
- 惰性图构建（首次搜索时才构建）
- 磁盘缓存（git commit hash作为缓存键）
- 超时处理（大仓库>10k文件）
- 优雅降级（图构建失败时回退到现有路径）
- 性能基准测试

---

## 风险评估与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| javalang解析器限制 | 中 | 高 | 每文件独立try/except，失败时回退到正则提取 |
| 大仓库图构建延迟 | 高 | 中 | 惰性初始化+并行解析+磁盘缓存+max_files限制 |
| LLM评分增加延迟和成本 | 高 | 中 | 可选评分+批量调用+score_threshold跳过+可配置小模型 |
| 新工具干扰LLM Agent | 低-中 | 中 | 清晰工具描述+明确工作流指导+已有迭代上限 |
| 静态分析边不精确 | 高 | 低 | 作为"提示提供者"而非"预言机"+结合LLM评分 |

---

## 关键架构决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 集成深度 | 浅层到中度 | 保留工作良好的堆栈路径，添加搜索作为增强 |
| Java解析器 | javalang（纯Python） | 无JVM依赖，Windows兼容，足够静态分析使用 |
| 图存储 | 内存dict+可选磁盘缓存 | 简单，避免数据库依赖 |
| 评分模型 | 与修复模型相同（可配置） | 最小化API密钥管理 |
| 工具注册 | 条件式（配置驱动） | 易于测试禁用，向后兼容 |
| ReAct循环变更 | 无（仅添加工具） | 最小风险，Agent完全自主 |

---

## 验证方案

1. **单元测试**：每个新模块独立测试
   - `java_parser.py`：解析示例Java文件，验证提取的结构
   - `repo_graph.py`：从小型测试仓库构建图，验证查询
   - `inverted_index.py`：索引测试仓库，验证查找
   - `search_manager.py`：每种策略针对测试仓库验证
   - `code_scorer.py`：mock LLM响应，验证排序

2. **集成测试**：端到端测试
   - 创建mock Java仓库，其中堆栈定位失败但搜索定位成功
   - 验证Agent在locate_from_stack失败后正确使用search_candidates

3. **性能测试**：
   - 测量图构建时间（目标：<60秒/10k文件）
   - 测量搜索时间（目标：<5秒/查询）
   - 测量评分时间（目标：<10秒/批量）

4. **回归测试**：
   - 运行现有测试套件，确保新工具不影响现有功能
   - 验证`orcaloca.enabled: false`时行为完全不变
