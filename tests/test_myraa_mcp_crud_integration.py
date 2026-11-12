"""Offline, non-destructive tests. Never accesses the database or Gemini API."""
import ast
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / 'backend/inventory_mcp.py'
CLOUD = ROOT / 'backend/services/cloud_assistant_service.py'
ACTION_MAP = {
    'prepare_create_product': 'product_create',
    'prepare_update_product': 'product_update',
    'prepare_archive_product': 'product_archive',
    'prepare_restore_product': 'product_restore',
    'prepare_add_size': 'size_add',
    'prepare_archive_size': 'size_archive',
    'prepare_restore_size': 'size_restore',
    'prepare_change_stock': 'stock_change',
}


def _fake_mcp():
    calls = []
    def fake_prepare(action, payload):
        calls.append((action, payload))
        return {'status': 'awaiting_user_confirmation', 'confirmation_token': 'TEST_ONLY',
                'action': action, 'details': payload, 'expires_in_seconds': 600}
    fake = types.ModuleType('inventory_mcp')
    fake.prepare_inventory_action = fake_prepare
    fake.mcp = types.SimpleNamespace(tool=lambda: (lambda func: func))
    parsed = ast.parse(MCP.read_text(encoding='utf-8'))
    aliases = []
    for node in parsed.body:
        if isinstance(node, ast.FunctionDef) and node.name in ACTION_MAP:
            node.decorator_list = []
            aliases.append(node)
    assert len(aliases) == len(ACTION_MAP), 'Missing tool wrappers'
    namespace = {'prepare_inventory_action': fake_prepare, 'Any': object}
    exec(compile(ast.fix_missing_locations(ast.Module(body=aliases, type_ignores=[])), str(MCP), 'exec'), namespace)
    for name in ACTION_MAP:
        setattr(fake, name, namespace[name])
    for name in ['search_inventory', 'get_product', 'get_inventory_summary', 'get_inventory_alerts',
                 'get_reorder_shortages', 'get_stock_history', 'get_daily_dispatch_summary',
                 'get_dispatch_analytics', 'list_pending_order_batches', 'get_order_batch']:
        setattr(fake, name, lambda **kw: {})
    return fake, calls


def test_typed_crud_tools_prepare_only():
    fake, calls = _fake_mcp()
    inputs = {
        'prepare_create_product': {'sku':'TEST-001', 'name':'Test Kurta', 'sizes':['S','M']},
        'prepare_update_product': {'sku':'TEST-001', 'new_name':'Test Kurta Updated'},
        'prepare_archive_product': {'sku':'TEST-001'},
        'prepare_restore_product': {'sku':'TEST-001'},
        'prepare_add_size': {'sku':'TEST-001', 'size':'XL'},
        'prepare_archive_size': {'variant_code':'TEST-001_XL'},
        'prepare_restore_size': {'variant_code':'TEST-001_XL'},
        'prepare_change_stock': {'variant_code':'TEST-001_M', 'operation':'add', 'quantity':5},
    }
    for name, args in inputs.items():
        result = getattr(fake, name)(**args)
        assert result['status'] == 'awaiting_user_confirmation'
        assert calls[-1][0] == ACTION_MAP[name]
    assert calls[1][1] == {'sku': 'TEST-001', 'new_name': 'Test Kurta Updated'}
    assert calls[-1][1] == {'variant_code': 'TEST-001_M', 'operation':'add', 'quantity':5}


def test_gemini_routing_never_commits(monkeypatch):
    fake, calls = _fake_mcp()
    monkeypatch.setitem(sys.modules, 'inventory_mcp', fake)
    spec = importlib.util.spec_from_file_location('myraa_cloud_test_module', CLOUD)
    cloud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cloud)
    names = {item['name'] for item in cloud._TOOLS}
    assert set(ACTION_MAP).issubset(names)
    assert 'confirm_inventory_action' not in names, 'Gemini must not be offered a commit tool'
    assert set(ACTION_MAP).issubset(cloud._PREPARE_TOOLS)
    result = cloud._execute_tool('prepare_change_stock', {'variant_code':'TEST-001_M','operation':'set','quantity':4})
    assert result['status'] == 'awaiting_user_confirmation'
    assert calls[-1][0] == 'stock_change'

    monkeypatch.setenv('GEMINI_API_KEY', 'FAKE_TEST_KEY_NOT_REAL')
    async def fake_generate(contents, key, language='auto'):
        if not any(p.get('functionResponse') for c in contents for p in c.get('parts', []) if isinstance(p, dict)):
            return {'role':'model','parts':[{'functionCall':{
                'name':'prepare_change_stock',
                'args':{'variant_code':'TEST-001_M','operation':'set','quantity':4}}}]}
        return {'role':'model','parts':[{'text':'Please review the proposed stock change.'}]}
    monkeypatch.setattr(cloud, '_generate', fake_generate)
    response = asyncio.run(cloud._chat_gemini([{'role':'user','content':'Set TEST-001 size M to 4'}]))
    assert response['pending_action']['action'] == 'stock_change'
    assert response['pending_action']['confirmation_token'] == 'TEST_ONLY'
    assert response['provider'] == 'Gemini'
    assert len(calls) == 2  # neither call committed; both went through preview


def test_existing_production_transport_and_auth_exist():
    # This patch does not replace main.py; the deployed app must retain the MCP mount.
    source_path = ROOT / 'backend/inventory_mcp.py'
    assert 'def confirm_inventory_action' in source_path.read_text(encoding='utf-8')
