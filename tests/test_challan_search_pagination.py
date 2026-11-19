"""Offline read-only API regression tests for challan search, date and pagination."""
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base, get_db
import models
import business_models
from business_api import router
from business_models import PurchaseChallan, PurchaseChallanLine


class ChallanListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.engine = create_engine('sqlite:///' + str(Path(cls.folder.name) / 'challans.db'),
                                   connect_args={'check_same_thread': False})
        Base.metadata.create_all(bind=cls.engine)
        cls.factory = sessionmaker(bind=cls.engine)
        with cls.factory() as db:
            for k in range(11):
                batch = PurchaseChallan(
                    party_name='TWINS' if k % 2 == 0 else 'MODA',
                    challan_no='CH-%03d' % k,
                    invoice_no='INV-%03d' % k,
                    order_date='2026-11-19' if k < 8 else '2026-11-20',
                    source_filename='supplier.xlsx',
                    file_hash=uuid.uuid4().hex, status='OPEN',
                )
                db.add(batch)
                db.flush()
                db.add(PurchaseChallanLine(
                    challan_id=batch.id, sku='DESIGN%02d' % k,
                    name='Kurta Design %d' % k, size='S',
                    qty_ordered=10, qty_received=3, unit_cost=100,
                ))
            db.commit()
        api = FastAPI()
        api.include_router(router)
        def isolated_db():
            with cls.factory() as db:
                yield db
        api.dependency_overrides[get_db] = isolated_db
        cls.client = TestClient(api)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.engine.dispose()
        cls.folder.cleanup()

    def test_paginated_pages_keep_global_totals(self):
        first = self.client.get('/api/purchase-challans').json()
        second = self.client.get('/api/purchase-challans', params={'offset': 6}).json()
        self.assertEqual((first['total'], len(first['items'])), (11, 6))
        self.assertEqual((second['total'], len(second['items'])), (11, 5))
        self.assertTrue({r['id'] for r in first['items']}.isdisjoint({r['id'] for r in second['items']}))
        for page in (first, second):
            self.assertEqual(page['ordered_pieces'], 110)
            self.assertEqual(page['received_pieces'], 33)
            self.assertEqual(page['pending_pieces'], 77)
            self.assertEqual(page['received_value_known'], 3300.0)

    def test_search_all_sellers_and_products(self):
        for params, expected in [({'q': 'TWINS'}, 6), ({'q': 'DESIGN03'}, 1),
                                 ({'q': 'MODA', 'date_filter': '2026-11-19'}, 4),
                                 ({'date_filter': '2026-11-20'}, 3),
                                 ({'q': 'CH-001'}, 1)]:
            data = self.client.get('/api/purchase-challans', params=params).json()
            self.assertEqual(data['total'], expected, params)
            self.assertEqual(data['ordered_pieces'], 110)  # all-time summary, not filtered

    def test_invalid_date_rejected(self):
        response = self.client.get('/api/purchase-challans', params={'date_filter': 'wrong'})
        self.assertEqual(response.status_code, 422)


if __name__ == '__main__':
    unittest.main()
