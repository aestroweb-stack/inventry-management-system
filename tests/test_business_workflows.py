"""Offline, ephemeral database tests: no live Railway writes."""
import asyncio
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
# Avoid creating/altering any user's existing database.
_TMP = tempfile.TemporaryDirectory(prefix='myraa-business-test-')
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(_TMP.name) / 'test.sqlite3')

from fastapi import HTTPException, UploadFile
from database import Base, engine, SessionLocal
import models, business_models
from business_models import PurchaseChallan, MarketplaceSkuAlias
from business_api import (import_challan, update_receipt, push_challan_to_inventory,
    ReceiptLine, ReceiptUpdate, upsert_product_cost, upsert_variant_cost,
    add_alias, costing_summary, costing_designs, list_purchase_challans, parse_challan_file)
from services import excel_service


class MyraaBusinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        _TMP.cleanup()

    def setUp(self):
        self.db = SessionLocal()
        # isolate each test without breaking other model tables
        self.db.query(business_models.PurchaseChallanLine).delete()
        self.db.query(business_models.PurchaseChallan).delete()
        self.db.query(models.OrderLine).delete()
        self.db.query(models.OrderUpload).delete()
        self.db.query(models.InventoryTransaction).delete()
        self.db.query(models.Alert).delete()
        self.db.query(business_models.MarketplaceSkuAlias).delete()
        self.db.query(business_models.VariantCost).delete()
        self.db.query(business_models.ProductCost).delete()
        self.db.query(models.Variant).delete()
        self.db.query(models.Product).delete()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def test_costing_and_multiple_exact_aliases(self):
        p = models.Product(sku='MYRAA-SAMPLE', name='Sample kurta')
        self.db.add(p); self.db.flush()
        variant = models.Variant(product_id=p.id,size='XL',variant_code='MYRAA-SAMPLE_XL',current_stock=4,last_counted_at=models.utcnow())
        self.db.add(variant); self.db.flush()
        upsert_product_cost(self.db,p.id,Decimal('200.00'))
        upsert_variant_cost(self.db,variant.id,Decimal('210.00'))
        add_alias(self.db,variant,'Myntra','MYNTRA-V-111-XL')
        add_alias(self.db,variant,'Ajio','AJIO-V-111-XL')
        self.db.commit()
        report=costing_summary(self.db)
        self.assertEqual(report['total_known_cost'],840.0)
        self.assertEqual(self.db.query(MarketplaceSkuAlias).count(),2)
        paged = costing_designs(q='Sample',limit=20,offset=0,db=self.db)
        self.assertEqual(paged['total'],1)
        self.assertEqual(paged['items'][0]['cost_value'],840.0)
        self.assertEqual(paged['items'][0]['stock'],4)
        other = models.Product(sku='OTHER',name='Other'); self.db.add(other); self.db.flush()
        ov = models.Variant(product_id=other.id,size='XL',variant_code='OTHER_XL'); self.db.add(ov); self.db.flush()
        with self.assertRaises(HTTPException) as cm:
            add_alias(self.db,ov,'Other','MYNTRA-V-111-XL')
        self.assertEqual(cm.exception.status_code,409)

    def test_challan_staging_uses_single_atomic_push(self):
        csv = b'SKU,Product Name,Size,Qty,Unit Cost,Category\nA1,Cotton Shirt,S,2,100,Shirts\nA1,Cotton Shirt,M,3,115,Shirts\n'
        response = asyncio.run(import_challan(
            file=UploadFile(filename='test.csv', file=BytesIO(csv)),
            party_name='Factory A',challan_no='CH-101',invoice_no='INV-101',order_date='2026-11-19',db=self.db))
        self.assertEqual(response['ordered'],5)
        self.assertEqual(self.db.query(models.Product).count(),0, 'Import must not mutate inventory')
        id_ = response['id']
        lines = response['lines']
        partial=update_receipt(id_, ReceiptUpdate(lines=[ReceiptLine(line_id=lines[0]['id'],received=2)]),db=self.db)
        self.assertEqual(partial['status'],'PARTIAL')
        pending = list_purchase_challans(db=self.db)
        self.assertEqual(pending['pending_pieces'],3)
        self.assertEqual(pending['pending_cost_known'],345.0)
        with self.assertRaises(HTTPException) as cm:
            push_challan_to_inventory(id_,db=self.db)
        self.assertEqual(cm.exception.status_code,409)
        ready = update_receipt(id_, ReceiptUpdate(lines=[ReceiptLine(line_id=x['id'],received=x['qty_ordered']) for x in lines]),db=self.db)
        self.assertEqual(ready['status'],'READY')
        posted=push_challan_to_inventory(id_,db=self.db)
        self.assertEqual(posted['received_pieces'],5)
        quantities = {v.size:v.current_stock for v in self.db.query(models.Variant).all()}
        self.assertEqual(quantities,{'S':2,'M':3})
        self.assertEqual(self.db.query(models.InventoryTransaction).count(),2)
        self.assertEqual(costing_summary(self.db)['total_known_cost'],545.0)
        with self.assertRaises(HTTPException) as cm:
            push_challan_to_inventory(id_,db=self.db)
        self.assertEqual(cm.exception.status_code,409)
        self.assertEqual(self.db.query(models.InventoryTransaction).count(),2)

    def test_weighted_average_cost_when_receiving_new_stock(self):
        p = models.Product(sku='COSTED-01', name='Dress')
        self.db.add(p); self.db.flush()
        v = models.Variant(product_id=p.id, size='M', variant_code='COSTED-01_M', current_stock=10,
                           last_counted_at=models.utcnow())
        self.db.add(v); self.db.flush()
        upsert_product_cost(self.db, p.id, '100.00')
        self.db.commit()
        data = b'SKU,Product Name,Size,Qty,Unit Cost\nCOSTED-01,Dress,M,2,200\n'
        batch = asyncio.run(import_challan(
            file=UploadFile(filename='cost.csv', file=BytesIO(data)), party_name='Party B',
            challan_no='CH-COST', invoice_no='', order_date='2026-11-19', db=self.db))
        update_receipt(batch['id'], ReceiptUpdate(lines=[ReceiptLine(line_id=batch['lines'][0]['id'],received=2)]), db=self.db)
        push_challan_to_inventory(batch['id'], db=self.db)
        self.db.refresh(v)
        self.assertEqual(v.current_stock,12)
        self.assertEqual(v.cost_record.unit_cost,Decimal('116.67'))

    def test_missing_cost_is_blocked_before_stock_changes(self):
        data=b'SKU,Size,Qty\nNO-COST-001,S,2\n'
        batch=asyncio.run(import_challan(file=UploadFile(filename='costmissing.csv',file=BytesIO(data)),
            party_name='Vendor C',challan_no='CH-MISSING',invoice_no='',order_date='2026-11-19',db=self.db))
        update_receipt(batch['id'],ReceiptUpdate(lines=[ReceiptLine(line_id=batch['lines'][0]['id'],received=2)]),db=self.db)
        with self.assertRaises(HTTPException) as cm:
            push_challan_to_inventory(batch['id'],db=self.db)
        self.assertEqual(cm.exception.status_code,409)
        self.assertEqual(self.db.query(models.Product).count(),0)

    def test_black_white_excel_has_explicit_print_borders(self):
        from services import packing_picklist_service as pack
        from zipfile import ZipFile
        import xml.etree.ElementTree as ET
        result=pack.transform([{'Master SKU':'ACME_V_DRESS_S','Qty':2}])
        with ZipFile(BytesIO(pack.to_xlsx(result))) as z:
            styles=z.read('xl/styles.xml').decode()
            sheet=z.read('xl/worksheets/sheet1.xml').decode()
            ET.fromstring(styles);ET.fromstring(sheet)
            self.assertIn('showGridLines="0"',sheet)
            self.assertIn('FFFFFFFF',styles)
            self.assertIn('FF000000',styles)
            self.assertIn('ht="38"',sheet)

    def test_missing_size_rejected_not_guessed(self):
        with self.assertRaises(HTTPException) as cm:
            parse_challan_file(b'SKU,Qty\nABC,10\n', 'test.csv')
        self.assertEqual(cm.exception.status_code, 422)

    def test_duplicate_row_merges_quantity(self):
        r=parse_challan_file(b'SKU,Size,Qty,Unit Cost\nA1,S,1,100\nA1,S,2,100\n','test.csv')
        self.assertEqual(len(r),1)
        self.assertEqual(r[0]['quantity'],3)

    def test_marketplace_alias_in_order_process(self):
        p=models.Product(sku='MASTER-123',name='Test');self.db.add(p);self.db.flush()
        v=models.Variant(product_id=p.id,size='M',variant_code='MASTER-123_M',current_stock=15,last_counted_at=models.utcnow())
        self.db.add(v);self.db.flush()
        add_alias(self.db,v,'Myntra','CHANNEL-PROD-M')
        self.db.commit()
        out=excel_service.process_order_excel(self.db,b'Seller SKU Code,Quantity\nCHANNEL-PROD-M,2\n','manifest.csv')
        self.assertIsNotNone(out)
        line=self.db.query(models.OrderLine).one()
        self.assertEqual(line.variant_id,v.id)
        self.assertEqual(line.qty_ordered,2)
        self.assertEqual(v.current_stock,15, 'Order import never deducts by itself')

from decimal import Decimal
if __name__ == '__main__': unittest.main()
