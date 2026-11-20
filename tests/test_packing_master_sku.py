"""Offline safety checks: Packing Picklist uses registered Master SKU at export.

No production database connections or inventory writes.
"""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
TESTDIR = tempfile.TemporaryDirectory()
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
engine = create_engine('sqlite:///' + str(Path(TESTDIR.name) / 'packing_master.sqlite'))
SessionLocal = sessionmaker(bind=engine)
from models import Product, Variant
from business_models import ProductMarketplaceAlias, MarketplaceSkuAlias
from services.packing_picklist_service import transform, apply_registered_master_skus, to_xlsx
Base.metadata.create_all(bind=engine)


class MasterPicklistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with SessionLocal() as db:
            p = Product(sku='OUR-URVISHA', name='URVISHA RED', category='KURTA')
            q = Product(sku='OUR-OTHER', name='OTHER DESIGN', category='KURTA')
            db.add_all([p, q]); db.flush()
            for prod in (p, q):
                for size in ('S', 'M'):
                    db.add(Variant(product_id=prod.id, size=size,
                                   variant_code=f'{prod.sku}_{size}', current_stock=0))
            db.flush()
            ss=db.query(Variant).filter_by(product_id=p.id,size='S').one()
            mm=db.query(Variant).filter_by(product_id=p.id,size='M').one()
            db.add_all([
                MarketplaceSkuAlias(variant_id=ss.id, marketplace='TWINS', alias_code='TWINS_V_URVISHA_RED_S', normalized_code='TWINS_V_URVISHA_RED_S'),
                MarketplaceSkuAlias(variant_id=mm.id, marketplace='MODA', alias_code='MODA_V_URVISHA_RED_M', normalized_code='MODA_V_URVISHA_RED_M'),
                ProductMarketplaceAlias(product_id=p.id, marketplace='TWINS', alias_code='TWINS-URVISHA-RED',normalized_code='TWINS-URVISHA-RED'),
                ProductMarketplaceAlias(product_id=q.id, marketplace='MODA', alias_code='ALIAS-OTHER',normalized_code='ALIAS-OTHER'),
            ])
            db.commit()

    def transformed(self, records):
        orig = transform(records, filename='Picklist_2026-11-20_12-00-00.xlsx')
        with SessionLocal() as db:
            mapped = apply_registered_master_skus(orig, db)
        return orig,mapped

    def test_exact_marketplace_alias_maps_to_master(self):
        before,after=self.transformed([{'SKU':'TWINS_V_URVISHA_RED_S','Qty':2}])
        self.assertEqual(after['brand_items'][0]['pick_sku'],'OUR-URVISHA_S')
        self.assertEqual(after['master_sku_mapped_qty'],2)
        self.assertEqual(after['total_qty'],2)
        self.assertEqual(before['audit'][0]['original_sku'],'TWINS_V_URVISHA_RED_S')

    def test_multiple_aliases_same_master_size_merge(self):
        _,after=self.transformed([
            {'SKU':'TWINS_V_URVISHA_RED_S','Qty':1,'Size':'S','Brand':'TWINS'},
            {'SKU':'TWINS-URVISHA-RED-S','Qty':3,'Size':'S','Brand':'TWINS'},
            {'SKU':'UNKNOWN_RED_XL','Qty':4,'Size':'XL','Brand':'TWINS'}])
        self.assertEqual(after['total_qty'],8)
        self.assertEqual(after['master_sku_mapped_qty'],4)
        self.assertEqual(len(after['brand_items']),2)
        self.assertEqual({x['pick_sku']:x['qty'] for x in after['brand_items']}['OUR-URVISHA_S'],4)
        self.assertEqual(next(x for x in after['audit'] if x['original_sku']=='UNKNOWN_RED_XL')['pick_sku'],
                         next(x for x in transform([{'SKU':'UNKNOWN_RED_XL','Qty':4}])['audit'])['pick_sku'])

    def test_product_alias_requires_size(self):
        _,after=self.transformed([{'SKU':'TWINS-URVISHA-RED','Qty':4}])
        self.assertEqual(after['master_sku_mapped_rows'],0)
        _,after2=self.transformed([{'SKU':'TWINS-URVISHA-RED','Qty':4,'Size':'M'}])
        self.assertEqual(after2['brand_items'][0]['pick_sku'],'OUR-URVISHA_M')

    def test_wrong_size_does_not_rewrite_sku(self):
        before,after=self.transformed([{'SKU':'TWINS_V_URVISHA_RED_S','Qty':1,'Size':'M'}])
        self.assertEqual(after['master_sku_mapped_rows'],0)
        self.assertEqual(before['brand_items'][0]['pick_sku'],after['brand_items'][0]['pick_sku'])
        self.assertGreater(after['review_count'], before['review_count'])

    def test_other_product_not_automatically_matched(self):
        before,after=self.transformed([{'SKU':'SOME_RANDOM_DESIGN_S','Qty':5}])
        self.assertEqual(after['master_sku_mapped_qty'],0)
        self.assertEqual(after['brand_items'],before['brand_items'])

    def test_export_preserves_print_layout_and_qty(self):
        _,after=self.transformed([{'SKU':'TWINS_V_URVISHA_RED_S','Qty':3},
                                  {'SKU':'UNREGISTERED_DESIGN_M','Qty':2}])
        payload=to_xlsx(after,include_audit=True)
        with ZipFile(BytesIO(payload)) as z:
            xmls=[z.read(n).decode() for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
            # Distinct brand sheets must both survive, even with an unknown seller.
            self.assertTrue(any('OUR-URVISHA_S' in s for s in xmls))
            self.assertTrue(any('UNREGISTERED_DESIGN_M' in s for s in xmls))
            self.assertTrue(all('<mergeCell ' not in s for s in xmls))
        self.assertEqual(sum(x['qty'] for x in after['brand_items']),5)

    def test_party_inference_only_when_registered_and_unambiguous(self):
        _, after=self.transformed([
            {'SKU':'TWINS_V_URVISHA_RED_S','Qty':1,'Size':'S','Brand':'TWINS'},
            {'SKU':'TWINS-URVISHA-RED-S','Qty':3,'Size':'S','Brand':'TWINS'},
        ])
        self.assertEqual(len(after['brand_items']),1)
        self.assertEqual(after['brand_items'][0]['qty'],4)
        self.assertEqual(after['brand_items'][0]['party'],'V')
        self.assertEqual(after['party_totals']['V'],4)

    def test_master_mapping_does_not_change_catalog_stock(self):
        with SessionLocal() as db:
            before={v.variant_code:v.current_stock for v in db.query(Variant).all()}
        _, after=self.transformed([{'SKU':'TWINS_V_URVISHA_RED_S','Qty':5}])
        self.assertEqual(after['master_sku_mapped_qty'],5)
        with SessionLocal() as db:
            after_stock={v.variant_code:v.current_stock for v in db.query(Variant).all()}
        self.assertEqual(before,after_stock)

    def test_product_alias_conflicting_size_kept_unmapped(self):
        original,mapped=self.transformed([{'SKU':'TWINS-URVISHA-RED-S','Qty':2,'Size':'M'}])
        self.assertEqual(mapped['master_sku_mapped_qty'],0)
        self.assertEqual(mapped['brand_items'],original['brand_items'])


if __name__ == '__main__': unittest.main()
