#!/usr/bin/env python
"""Test the exception inference path for framework-level exceptions."""

import os
import sys
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from agents.auto_fix_agent import AutoFixAgent
from main import load_config

# Load configuration
config = load_config('configs/config.yml')

# Create agent
agent = AutoFixAgent(config)

# Simulate a framework-level exception (MissingPathVariableException)
framework_exception = """org.springframework.web.bind.MissingPathVariableException: Required URI template variable 'id' for method parameter type Long is not present
\tat org.springframework.web.servlet.mvc.method.annotation.PathVariableMethodArgumentResolver.handleMissingValue(PathVariableMethodArgumentResolver.java:101)
\tat org.springframework.web.method.annotation.AbstractNamedValueMethodArgumentResolver.handleMissingValue(AbstractNamedValueMethodArgumentResolver.java:234)
\tat org.springframework.web.method.annotation.AbstractNamedValueMethodArgumentResolver.resolveArgument(AbstractNamedValueMethodArgumentResolver.java:125)
\tat org.springframework.web.method.support.HandlerMethodArgumentResolverComposite.resolveArgument(HandlerMethodArgumentResolverComposite.java:122)
\tat org.springframework.web.method.support.InvocableHandlerMethod.getMethodArgumentValues(InvocableHandlerMethod.java:224)
\tat org.springframework.web.method.support.InvocableHandlerMethod.invokeForRequest(InvocableHandlerMethod.java:178)
\tat org.springframework.web.servlet.mvc.method.annotation.ServletInvocableHandlerMethod.invokeAndHandle(ServletInvocableHandlerMethod.java:118)
\tat org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter.invokeHandlerMethod(RequestMappingHandlerAdapter.java:926)
\tat org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter.handleInternal(RequestMappingHandlerAdapter.java:831)
\tat org.springframework.web.servlet.mvc.method.AbstractHandlerMethodAdapter.handle(AbstractHandlerMethodAdapter.java:87)
\tat org.springframework.web.servlet.DispatcherServlet.doDispatch(DispatcherServlet.java:1089)
\tat org.springframework.web.servlet.DispatcherServlet.doService(DispatcherServlet.java:979)
\tat org.springframework.web.servlet.FrameworkServlet.processRequest(FrameworkServlet.java:1014)
\tat org.springframework.web.servlet.FrameworkServlet.doGet(FrameworkServlet.java:903)
\tat jakarta.servlet.http.HttpServlet.service(HttpServlet.java:564)"""

print("=" * 70)
print("TEST: Exception Inference Path for Framework Exceptions")
print("=" * 70)
print()

# Test 1: Parse stacktrace
print("Step 1: Parsing stack trace...")
parsed_stack = agent.parse_stacktrace(framework_exception)
print(f"  Parsed {len(parsed_stack)} frames")
print(f"  Exception type: {parsed_stack[0].get('exception_type') if parsed_stack else 'N/A'}")
print()

# Test 2: Select best frame
print("Step 2: Selecting best frame...")
repo_path = config.get('repo_path')
best_frame = agent._select_best_frame(repo_path, parsed_stack)
print(f"  Best frame found: {best_frame is not None}")
if best_frame:
    print(f"    Class: {best_frame.get('class_name')}")
    print(f"    Method: {best_frame.get('method')}")
print()

# Test 3: Inference path (when no app frame found)
if not best_frame:
    print("Step 3: No app frame found, attempting inference...")
    source_info = agent._infer_from_exception_message(repo_path, framework_exception, parsed_stack)
    
    if source_info:
        print("  Inference successful!")
        print(f"    File: {source_info.get('repo_relative_path')}")
        print(f"    Class: {source_info.get('class_name')}")
        print(f"    Method: {source_info.get('method')}")
        print(f"    Reasoning: {source_info.get('reasoning')}")
        print(f"    Inferred: {source_info.get('inferred')}")
    else:
        print("  Inference failed (may be expected if LLM API not configured)")
else:
    print("Step 3: App frame found, skipping inference test")
    source_info = agent.find_source_location(repo_path, best_frame)
    print(f"  Located: {source_info.get('repo_relative_path')}")

print()
print("=" * 70)
print("Test completed")
print("=" * 70)
