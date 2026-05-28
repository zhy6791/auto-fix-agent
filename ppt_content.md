# Auto-Fix Agent 介绍PPT

---

## 幻灯片 1: 封面

**Auto-Fix Agent for Java Web Services**

基于LLM的Java Web服务异常自动修复系统

---

## 幻灯片 2: 项目概述

### 什么是 Auto-Fix Agent？

一个用 Python 实现的自动修复 Java Web 服务异常的 Agent 系统

**核心能力：**
- 自动读取和解析日志中的异常堆栈
- 智能定位源码文件和问题行号
- 调用 LLM 生成最小化修复补丁
- 自动创建修复分支并提交 PR

**目标：** 将人工排查修复时间从小时级缩短到分钟级

---

## 幻灯片 3: 核心特性概览

### 三大核心特性

| 特性 | 说明 |
|------|------|
| 🤖 **Agent 决策循环** | LLM 自主调用 9 个工具完成定位与修复 |
| 🕸️ **OrcaLoca 代码图谱** | 基于 AST 解析构建知识图谱，支持多策略搜索 |
| 🛡️ **安全防护机制** | 6 层补丁校验 + 执行保护 |

---

## 幻灯片 4: 核心特性 - Agent 决策循环

### ReAct 决策循环引擎

**传统方式（已弃用）：**
```
硬编码 if/else → 固定路径 → 一次性生成补丁
```

**Agent 方式（当前）：**
```
Thought → Action → Observation → 循环迭代
```

**优势：**
- Agent 自主选择工具和策略
- 可多次尝试不同定位方法
- 校验失败后自动调整策略
- 支持上下文主动探索

---

## 幻灯片 5: 核心特性 - OrcaLoca 代码图谱

### 基于知识图谱的智能定位

**三层定位策略：**

1. **堆栈帧定位** - 直接从异常堆栈提取类名/行号
2. **OrcaLoca 图谱搜索** - AST 解析构建知识图谱 + 倒排索引
3. **LLM 推断** - 堆栈全是框架代码时的兜底方案

**OrcaLoca 搜索策略：**
- 按异常类型搜索
- 按 Spring 注解搜索（@Controller、@Service）
- 按调用链扩展搜索
- 按 import 模式搜索

---

## 幻灯片 6: 核心特性 - 安全防护

### 多层安全保护机制

**补丁验证 6 层检查：**
1. 格式验证 - 仅接受 unified diff
2. 路径边界 - 防止越界写入
3. 结构保护 - package/import/方法签名不变
4. 粒度限制 - 控制补丁大小
5. 注释保护 - 关键注释不被删除
6. 内容校验 - 文件不被大幅缩短

**执行保护：**
- dry-run 安全模式
- 命令白名单控制
- 分支隔离提交
- 原子写入保护

---

## 幻灯片 7: 修复流程总览图

```
日志文件
    │
    ▼
┌─────────────────┐
│ 异常堆栈提取     │
│ 堆栈解析        │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│        Agent 决策循环（最多N轮）          │
│                                         │
│  Thought → Action → Observation         │
│     │         │          │              │
│     └─────────┴──────────┘              │
│           循环迭代                       │
└────────┬────────────────────────────────┘
         │
         ▼
┌─────────────────┐
│ 补丁校验（6层）  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ 创建修复分支     │
│ 应用补丁        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ 生成JUnit测试    │
│ 编译验证        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ CI管道          │
│ (编译+测试)      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ 创建Gitee PR    │
└─────────────────┘
```

---

## 幻灯片 8: Agent 决策循环详解

### 9个可调用工具

| 工具 | 功能 |
|------|------|
| `locate_from_stack` | 从堆栈帧定位源文件 |
| `infer_source` | 代码图谱搜索 + LLM推断 |
| `search_code` | 按类名查找Java文件 |
| `read_code` | 读取文件内容 |
| `edit_code` | LLM生成补丁 |
| `validate_patch` | 校验补丁安全性 |
| `generate_test` | 生成JUnit测试 |
| `final_patch` | 提交补丁退出 |
| `abort` | 放弃修复退出 |

---

## 幻灯片 9: Agent 决策链示例

### 修复 NullPointerException 的决策过程

```
迭代 1: Thought: 堆栈第一帧是 UserService.getNickname:27
         Action: locate_from_stack(UserService, getNickname, 27)

迭代 2: Thought: 已定位源码，异常是NPE，需要看MallUser类
         Action: search_code(MallUser)

迭代 3: Thought: 找到MallUser.java，读取源码
         Action: read_code(MallUser.java)

迭代 4: Thought: 分析完毕，生成修复补丁
         Action: edit_code(源码信息)

迭代 5: Thought: 补丁已生成，先校验安全性
         Action: validate_patch(补丁内容)

迭代 6: Thought: 校验通过，生成测试验证修复
         Action: generate_test(源码信息)

迭代 7: Thought: 测试已生成，提交最终补丁
         Action: final_patch(补丁+测试)
```

---


---
