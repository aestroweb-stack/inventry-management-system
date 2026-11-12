"""Offline regression tests for explicit SKU rename routing; no network or database."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'backend/services/cloud_assistant_service.py'
fake = types.ModuleType('inventory_mcp')
for name in (
    'search_inventory', 'get_product', 'get_inventory_summary', 'get_inventory_alerts',
    'get_reorder_shortages', 'get_stock_history', 'get_daily_dispatch_summary',
    'get_dispatch_analytics', 'list_pending_order_batches', 'get_order_batch',
    'prepare_create_product', 'prepare_update_product', 'prepare_archive_product',
    'prepare_restore_product', 'prepare_add_size', 'prepare_archive_size',
    'prepare_restore_size', 'prepare_change_stock', 'prepare_inventory_action',
    'confirm_inventory_action',
):
    setattr(fake, name, lambda **kwargs: {'status':'error','message':'stub'})
sys.modules['inventory_mcp'] = fake
spec = importlib.util.spec_from_file_location('myraa_test_service', SOURCE)
svc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svc)

SKU = 'MYRAA-E2E-68060265F9'
PROMPT = (f"For the existing product SKU {SKU}, update ONLY its product name to "
          "'MYRAA MCP VERIFIED KURTA'. Use the inventory update tool and show me "
          "the confirmation preview. Leave all other SKU details and stock counts unchanged.")
PENDING = {'status':'awaiting_user_confirmation', 'confirmation_token':'T'*80,
           'action':'product_update', 'details':{'sku':SKU,'changes':{'name':'MYRAA MCP VERIFIED KURTA'}}}

def test_case(name, prompt, response, result, expected_force, expected_preview, expected_exec):
    generated=[]
    called=[]
    async def generate(contents, key, language='auto', force_prepare_tool=None):
        generated.append(force_prepare_tool)
        return response
    def execute(func, args):
        called.append((func, args))
        return result
    with patch.object(svc,'_generate',side_effect=generate), patch.object(svc,'_execute_tool',side_effect=execute),patch.dict(os.environ,{'GEMINI_API_KEY':'fake-key'}):
        reply=asyncio.run(svc._chat_gemini([{'role':'user','content':prompt}], language='en-IN'))
    assert generated == expected_force, (name,generated)
    assert bool(reply['pending_action']) is expected_preview, (name,reply)
    assert len(called) == expected_exec, (name,called)
    print('PASS',name)

FUNCTION = {'parts':[{'functionCall':{'name':'prepare_update_product','args':{'sku':SKU,'new_name':'MYRAA MCP VERIFIED KURTA'}}}]}
TEXT = {'parts':[{'text':'I could do that.'}]}
WRONG = {'parts':[{'functionCall':{'name':'prepare_update_product','args':{'sku':'OTHER-SKU','new_name':'WRONG'}}}]}
READ = {'parts':[{'functionCall':{'name':'get_product','args':{'sku':SKU}}}]}
assert svc._explicit_product_rename_requested(PROMPT)
assert not svc._explicit_product_rename_requested(f'Do not update SKU {SKU} product name to OTHER')
test_case('explicit rename -> forced tool FIRST', PROMPT,FUNCTION,PENDING,['prepare_update_product'],True,1)
test_case('forced text -> refuse', PROMPT,TEXT,PENDING,['prepare_update_product'],False,0)
test_case('forced wrong SKU -> refuse', PROMPT,WRONG,PENDING,['prepare_update_product'],False,0)
test_case('forced unexpected read tool -> refuse', PROMPT,READ,PENDING,['prepare_update_product'],False,0)
test_case('forced prepare validation error -> report no write', PROMPT,FUNCTION,{'status':'error','message':'Invalid product name'},['prepare_update_product'],False,1)
test_case('negative rename -> AUTO', f'Do not update SKU {SKU} product name to OTHER',TEXT,PENDING,[None],False,0)
test_case('normal question -> AUTO', 'What is my inventory summary?',TEXT,PENDING,[None],False,0)

async def check_config():
    import httpx
    captured=[]
    async def fake_post(url,headers,body):
        captured.append(body)
        return httpx.Response(200,request=httpx.Request('POST',url),json={'candidates':[{'content':FUNCTION}]})
    with patch.object(svc,'_gemini_post_with_retry',side_effect=fake_post):
        await svc._generate([{'role':'user','parts':[{'text':PROMPT}]}],'fake-key',force_prepare_tool='prepare_update_product')
        await svc._generate([{'role':'user','parts':[{'text':'Hello'}]}],'fake-key')
    forced=captured[0]
    assert forced['toolConfig']['functionCallingConfig']['mode']=='ANY'
    assert forced['toolConfig']['functionCallingConfig']['allowedFunctionNames']==['prepare_update_product']
    assert len(forced['tools'][0]['functionDeclarations'])==1
    assert forced['tools'][0]['functionDeclarations'][0]['parameters']['required']==['sku','new_name']
    assert len(captured[1]['tools'][0]['functionDeclarations']) > 1
    assert 'toolConfig' not in captured[1]
    print('PASS restricted forced tool schema and regular chat schema')
asyncio.run(check_config())
print('PASS ALL OFFLINE TESTS')
