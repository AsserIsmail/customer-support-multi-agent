import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from support_ai.agents import SupportAssistant
from support_ai.mcp_client import MCPToolError, SupportTools


def test_rejected_tool_preserves_diagnostic_context():
    result = CallToolResult(content=[], is_error=True,
        structured_content={'message': 'private detail', 'code': 'invalid_columns'})
    tools = SupportTools(SimpleNamespace(call_tool=AsyncMock(return_value=result)))
    with pytest.raises(MCPToolError) as caught:
        asyncio.run(tools.call('execute_readonly_query', {}))
    assert caught.value.tool == 'execute_readonly_query'
    assert caught.value.code == 'invalid_columns'


@pytest.mark.parametrize('tool,code,expected', [
    ('execute_readonly_query', 'invalid_columns', 'tool=execute_readonly_query code=invalid_columns'),
    ('private-tool', 'private-code', 'tool=unknown code=unknown'),
    (None, None, 'tool=unknown code=unknown'),
    (['private-tool'], {'private': 'code'}, 'tool=unknown code=unknown'),
])
def test_failure_logs_only_allowlisted_diagnostics(caplog, tool, code, expected):
    assistant = SupportAssistant(None, None)
    result = assistant._failure('sql', MCPToolError('private-message', tool=tool, code=code), [])
    assert expected in caplog.text
    assert 'private' not in caplog.text
    assert 'private' not in result['response']['answer']
    assert result['response']['status'] == 'error'
