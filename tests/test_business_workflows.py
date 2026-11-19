"""Offline, ephemeral database tests: no live Railway writes."""
import asyncio
from io import BytesIO
from zipfile import ZipFile
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
from business_models import PurchaseChallan, MarketplaceSkuAlias, ProductMarketplaceAlias, PurchaseChallanAttachment
from business_api import (import_challan, update_receipt, push_challan_to_inventory,
    ReceiptLine, ReceiptUpdate, upsert_product_cost, upsert_variant_cost,
    add_alias, add_product_alias, costing_summary, costing_designs, list_purchase_challans, parse_challan_file,
    upload_challan_attachments, download_challan_attachment, export_challan, _simple_xlsx)
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
        self.db.query(business_models.PurchaseChallanAttachment).delete()
        self.db.query(business_models.PurchaseChallanLine).delete()
        self.db.query(business_models.PurchaseChallan).delete()
        self.db.query(models.OrderLine).delete()
        self.db.query(models.OrderUpload).delete()
        self.db.query(models.InventoryTransaction).delete()
        self.db.query(models.Alert).delete()
        self.db.query(business_models.MarketplaceSkuAlias).delete()
        self.db.query(business_models.ProductMarketplaceAlias).delete()
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


    def test_product_wide_alias_requires_size_and_uses_correct_variant(self):
        p=models.Product(sku='MSTR-ALIAS',name='Dress')
        self.db.add(p);self.db.flush()
        for size in ['S','M']:
            self.db.add(models.Variant(product_id=p.id,size=size,variant_code=f'MSTR-ALIAS_{size}',
                            current_stock=9,last_counted_at=models.utcnow()))
        self.db.flush()
        add_product_alias(self.db,p,'Twins','TWIN_DRESS')
        self.db.commit()
        result=excel_service.process_order_excel(self.db,b'Seller SKU Code,Size,Qty\nTWIN_DRESS,S,2\nTWIN_DRESS,M,3\n','order.csv')
        rows=self.db.query(models.OrderLine).all()
        self.assertEqual(len(rows),2)
        self.assertEqual({r.size:r.qty_ordered for r in rows},{'S':2,'M':3})
        self.assertTrue(all(r.variant_id is not None for r in rows))
        self.assertEqual(sum(v.current_stock for v in self.db.query(models.Variant).all()),18)
        with self.assertRaises(HTTPException) as error:
            add_product_alias(self.db,models.Product(sku='UNSAVED',name='Other'),'Ajio','TWIN_DRESS')
        self.assertEqual(error.exception.status_code,409)

    def test_product_wide_alias_with_size_suffix_matches(self):
        product=models.Product(sku='MASTER-SUFFIX',name='Summer kurta')
        self.db.add(product);self.db.flush()
        v=models.Variant(product_id=product.id,size='XL',variant_code='MASTER-SUFFIX_XL',
                         current_stock=7,last_counted_at=models.utcnow())
        self.db.add(v);self.db.flush()
        add_product_alias(self.db,product,'Ajio','AJIO-SUMMER')
        self.db.commit()
        excel_service.process_order_excel(self.db,b'Seller SKU Code,Qty\nAJIO-SUMMER_XL,2\n','size_suffix.csv')
        line=self.db.query(models.OrderLine).one()
        self.assertEqual(line.variant_id,v.id)
        self.assertEqual(line.size,'XL')
        self.assertEqual(v.current_stock,7)

    def test_product_wide_alias_without_size_stays_unmapped(self):
        p=models.Product(sku='MSTR-BASE',name='My special dress')
        self.db.add(p);self.db.flush()
        for size in ['S','L']:
            self.db.add(models.Variant(product_id=p.id,size=size,variant_code=f'MSTR-BASE_{size}',
                            current_stock=2,last_counted_at=models.utcnow()))
        self.db.flush();add_product_alias(self.db,p,'Moda','MODA_SPECIAL');self.db.commit()
        excel_service.process_order_excel(self.db,b'Seller SKU Code,Quantity\nMODA_SPECIAL,2\n','without_size.csv')
        line=self.db.query(models.OrderLine).one()
        self.assertIsNone(line.variant_id)
        self.assertEqual(line.status,models.OrderLineStatus.SKU_NOT_FOUND)

    def test_challan_three_columns_resolves_master_sku_and_costs(self):
        p=models.Product(sku='URVISHA-RED',name='URVISHA RED')
        self.db.add(p);self.db.commit()
        data=b'Product Name,Size,Qty\nURVISHA RED,S,3\nURVISHA RED,M,4\n'
        batch=asyncio.run(import_challan(file=UploadFile(filename='supplier.csv',file=BytesIO(data)),
            party_name='Shree',challan_no='CH-THREE',invoice_no='INVOICE-1',order_date='2026-11-20',db=self.db))
        self.assertEqual(batch['ordered'],7)
        self.assertEqual({x['sku'] for x in batch['lines']},{'URVISHA-RED'})
        self.assertEqual(self.db.query(models.Variant).count(),0)
        update=update_receipt(batch['id'],ReceiptUpdate(lines=[
            ReceiptLine(line_id=line['id'],received=line['qty_ordered'],unit_cost=Decimal('785'))
            for line in batch['lines']]),db=self.db)
        self.assertEqual(update['received_value_known'],5495.0)
        self.assertEqual(update['status'],'READY')
        export=export_challan(batch['id'],db=self.db)
        with ZipFile(BytesIO(_simple_xlsx([['Product Name','Size','Qty'],['X','S',3]]))) as z:
            self.assertIn('xl/worksheets/sheet1.xml',z.namelist())
        self.assertEqual(push_challan_to_inventory(batch['id'],db=self.db)['received_pieces'],7)
        self.assertEqual({v.size:v.current_stock for v in self.db.query(models.Variant).all()}, {'S':3,'M':4})

    def test_challan_attachment_shared_across_multiple_products(self):
        import business_api as m
        from unittest.mock import patch
        csv=b'Product Name,Size,Qty\nKurta,S,2\nShirt,L,3\n'
        batch=asyncio.run(import_challan(file=UploadFile(filename='a.csv',file=BytesIO(csv)),
            party_name='Shree',challan_no='CH-PHOTO',invoice_no='',order_date='2026-11-20',db=self.db))
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'CHALLAN_IMAGE_DIR',Path(tmp)):
            from PIL import Image
            buf=BytesIO(); Image.new('RGB',(8,8),(240,210,210)).save(buf,format='PNG')
            scan=UploadFile(filename='bill.png',file=BytesIO(buf.getvalue()))
            outcome=asyncio.run(upload_challan_attachments(batch['id'],files=[scan],db=self.db))
            self.assertEqual(len(outcome['attachments']),1)
            self.assertEqual(len(outcome['lines']),2)
            a=outcome['attachments'][0]
            result=download_challan_attachment(batch['id'],a['id'],db=self.db)
            self.assertTrue(Path(result.path).is_file())
            self.assertEqual(result.media_type,'image/png')
            with self.assertRaises(HTTPException):
                download_challan_attachment(batch['id'],99999,db=self.db)

from decimal import Decimal
if __name__ == '__main__': unittest.main()
