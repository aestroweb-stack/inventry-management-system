"""Standalone offline MYRAA Gemini routing tests; no DB, network or API keys.

Run: python tests/test_gemini_tool_routing.py
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / 'backend/services/cloud_assistant_service.py'

# Avoid loading a real database/config just to test the pure chat dispatcher.
fake_inventory = types.ModuleType('inventory_mcp')
for name in (
    'search_inventory', 'get_product', 'get_inventory_summary', 'get_inventory_alerts',
    'get_reorder_shortages', 'get_stock_history', 'get_daily_dispatch_summary',
    'get_dispatch_analytics', 'list_pending_order_batches', 'get_order_batch',
    'prepare_create_product', 'prepare_update_product', 'prepare_archive_product',
    'prepare_restore_product', 'prepare_add_size', 'prepare_archive_size',
    'prepare_restore_size', 'prepare_change_stock', 'prepare_inventory_action',
    'confirm_inventory_action',
):
    setattr(fake_inventory, name, lambda **kwargs: {'status': 'error', 'message': 'unused stub'})

old = sys.modules.get('inventory_mcp')
sys.modules['inventory_mcp'] = fake_inventory
try:
    spec = importlib.util.spec_from_file_location('myraa_tool_routing_test', SOURCE)
    svc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svc)
finally:
    if old is None:
        del sys.modules['inventory_mcp']
    else:
        sys.modules['inventory_mcp'] = old

SKU = 'MYRAA-E2E-9221CF3429'
PROMPT = f"For the existing product SKU {SKU}, update ONLY its product name to 'MYRAA MCP VERIFIED KURTA'. Use the inventory update tool and show the confirmation preview."
PENDING = {
    'status': 'awaiting_user_confirmation', 'confirmation_token': 'x'*80,
    'action': 'product_update', 'details': {'sku': SKU, 'changes': {'name':'MYRAA MCP VERIFIED KURTA'}},
}

def run_case(label, responses, expected_force, expected_pending, prompt=PROMPT, tool_result=PENDING):
    requested = []
    tool_calls = []
    async def generated(contents, api_key, language='auto', force_prepare_tool=None):
        requested.append(force_prepare_tool)
        assert responses, f'Unexpected call to Gemini in {label}'
        return responses.pop(0)
    def execute(name, args):
        tool_calls.append((name, args))
        return {"sku": SKU, "name": "MYRAA MCP TEST KURTA"} if name == "get_product" else tool_result
    with patch.object(svc, '_generate', side_effect=generated), \
         patch.object(svc, '_execute_tool', side_effect=execute), \
         patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-test-only-key'}):
        result = asyncio.run(svc._chat_gemini([{'role':'user', 'content':prompt}], language='en-IN'))
    assert requested == expected_force, (label,requested,expected_force)
    assert bool(result['pending_action']) is expected_pending, (label,result)
    if expected_pending:
        assert result['pending_action']['details']['sku'] == SKU
        assert len([name for name, _ in tool_calls if name.startswith("prepare_")]) == 1
    print('PASS',label)

TEXT = {'parts':[{'text':'I can do that.'}]}
FUNCTION = {'parts':[{'functionCall':{
    'name':'prepare_update_product',
    'args':{'sku':SKU, 'new_name':'MYRAA MCP VERIFIED KURTA'},
}}]}
READ = {'parts':[{'functionCall':{'name':'get_product','args':{'sku':SKU}}}]}

run_case('text -> forced rename preview', [TEXT,FUNCTION], [None,'prepare_update_product'], True)
run_case('direct tool -> preview', [FUNCTION], [None], True)
run_case('normal question no forced write', [TEXT], [None], False, prompt='What is my inventory summary?')
run_case('read -> text -> forced rename preview', [READ,TEXT,FUNCTION], [None,None,'prepare_update_product'], True,
         tool_result=PENDING)
run_case('preparation error never appears as confirmed', [TEXT,FUNCTION, {'parts':[{'text':'Could not prepare'}]}],
         [None,'prepare_update_product',None], False, tool_result={'status':'error','message':'Product not found'})

assert svc._explicit_product_rename_requested(PROMPT)
assert not svc._explicit_product_rename_requested(f'Do not rename SKU {SKU} product name to ABC')
assert not svc._explicit_product_rename_requested(f'What is the name of SKU {SKU}?')
assert not svc._explicit_product_rename_requested('Change product name to ABC')
print('PASS intent safety checks')
print('PASS all offline Gemini routing checks')

async def check_rest_config():
    import httpx
    saved = []
    async def fake_post(url, headers, body):
        saved.append(body)
        return httpx.Response(200, json={'candidates':[{'content':FUNCTION}]}, request=httpx.Request('POST', url))
    with patch.object(svc, '_gemini_post_with_retry', side_effect=fake_post):
        await svc._generate([{'role':'user','parts':[{'text':PROMPT}]}], 'fake-key', 'en-IN', force_prepare_tool='prepare_update_product')
        await svc._generate([{'role':'user','parts':[{'text':'Hello'}]}], 'fake-key', 'en-IN')
    assert saved[0]['toolConfig']['functionCallingConfig'] == {
        'mode':'ANY', 'allowedFunctionNames':['prepare_update_product']
    }
    assert 'toolConfig' not in saved[1]
    print('PASS REST toolConfig: forced only for explicit retry')

asyncio.run(check_rest_config())
