from agents.auto_fix_agent import AutoFixAgent
import os

config = {
    'llm_base_url': 'https://api.openai.com/v1',
    'llm_model': 'gpt-4',
    'llm_temperature': 0.1,
    'repo_path': r'D:\workspace\mall-service',
    'max_patch_lines': 40,
}

agent = AutoFixAgent(config)

# Simulate source_info with full_source
source_info = {
    'repo_relative_path': 'src/main/java/com/fixflow/mall/service/OrderService.java',
    'line_no': 45,
    'method': 'firstItemId',
    'context_snippet': '''    public Long firstItemId(Long orderId) {
        MallOrder order = orderRepository.findById(orderId).orElseThrow();
        // BUG-003: empty itemIds will trigger IndexOutOfBoundsException.
        return order.getItemIds().get(0);
    }''',
    'full_source': open(r'D:\workspace\mall-service\src\main\java\com\fixflow\mall\service\OrderService.java').read(),
}

prompt = agent.build_prompt('java.lang.IndexOutOfBoundsException: Index 0 out of bounds for length 0', source_info)

# Check if prompt contains the key improvements
checks = [
    ('contains full file marker', '原始完整文件内容' in prompt),
    ('contains COMPLETE requirement', 'COMPLETE' in prompt),
    ('contains package preservation', '保持原文件的 package' in prompt),
    ('contains imports preservation', 'imports' in prompt),
    ('contains ellipsis warning', '省略号' in prompt),
]

print('Prompt Quality Checks:')
for check_name, result in checks:
    status = 'PASS' if result else 'FAIL'
    print(f'  [{status}] {check_name}')

print(f'\nPrompt length: {len(prompt)} characters')
print(f'\nFirst 600 characters:')
print(prompt[:600])
