"""Safely verify a deployed MYRAA Streamable HTTP MCP server. READ ONLY.

Usage (Windows PowerShell):
  py -m pip install "mcp>=1.23,<2" httpx
  $env:INVENTORY_MCP_API_KEY = "<your existing Railway MCP secret>"
  py tools/check_myraa_mcp.py https://inventry-management-system-production-787ff64f.up.railway.app
  Remove-Item Env:INVENTORY_MCP_API_KEY
"""
import asyncio
import os
import sys
from urllib.parse import urlparse

REQUIRED = {
    'search_inventory', 'get_product', 'get_inventory_summary',
    'prepare_inventory_action', 'confirm_inventory_action',
    'prepare_create_product', 'prepare_update_product', 'prepare_archive_product',
    'prepare_restore_product', 'prepare_add_size', 'prepare_archive_size',
    'prepare_restore_size', 'prepare_change_stock',
}

async def main():
    if len(sys.argv) != 2:
        print('Usage: py tools/check_myraa_mcp.py https://your-railway-domain.up.railway.app')
        return 2
    base = sys.argv[1].rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        print('Please supply an HTTPS Railway origin URL (no passwords, tokens or path).')
        return 2
    key = os.getenv('INVENTORY_MCP_API_KEY', '').strip()
    if len(key) < 32:
        print('Set INVENTORY_MCP_API_KEY (32+ characters) in your PowerShell session.')
        return 2
    try:
        # Project pins SDK v1 (<2); this is its documented API.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        print('Install: py -m pip install "mcp>=1.23,<2" httpx')
        return 2
    endpoint = base + '/api/mcp/'
    try:
        async with streamablehttp_client(
            endpoint, headers={'Authorization': 'Bearer ' + key},
            timeout=30, sse_read_timeout=120,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                print('Connected to:', endpoint)
                print('Available MCP tools (' + str(len(names)) + '):')
                for name in names:
                    print(' -', name)
                missing = REQUIRED.difference(names)
                if missing:
                    print('MISSING upgraded CRUD tools:', ', '.join(sorted(missing)))
                    print('Check GitHub push and Railway deployment of this patch.')
                    return 1
                summary = await session.call_tool('get_inventory_summary', {})
                if summary.isError:
                    print('MCP connected but get_inventory_summary returned a tool error.')
                    return 1
                print('PASS: read-only inventory summary call succeeded.')
                print('This script did NOT create, update or delete anything.')
                return 0
    except Exception as exc:
        print('Connection check failed:', type(exc).__name__)
        print('Check Railway deploy, exact hostname, MCP key, and INVENTORY_MCP_ALLOWED_HOSTS.')
        return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
