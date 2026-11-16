"""Pure picklist normalization and Excel regression tests, no database needed."""
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
from services import packing_picklist_service as svc


class PackingPicklistTests(unittest.TestCase):
    def test_suffix_and_separators_consolidate(self):
        rows = [
            {"Master SKU": "TWINS_V_RASHMIKA(A)_PEACH_S", "Size": "S", "Qty": 1},
            {"Master SKU": "MODA-V-RASHMIKA(B)-PEACH-S", "Size": "S", "Qty": 1},
            {"Master SKU": "ANOUK_V_RASHMIKA(1)__PEACH_S", "Size": "S", "Qty": 3},
            {"Master SKU": "H&N-V-RASHMIKA(2)-PEACH-M", "Size": "M", "Qty": 2},
        ]
        result = svc.transform(rows)
        self.assertEqual(result['total_qty'], 7)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(next(x for x in result['items'] if x['size'] == 'S')['qty'], 5)
        self.assertEqual(result['party_totals']['V'], 7)

    def test_design_numbers_preserved(self):
        rows = [
            {"Master SKU": "TWINS_NK-851(A)_XL", "Qty": 1},
            {"Master SKU": "TWINS_NK_851(B)_XL", "Qty": 2},
            {"Master SKU": "BS-22141-BEIGE-26", "Qty": 4},
            {"Master SKU": "Twins_PINKPARI-2-RUST_M", "Qty": 3},
            {"Master SKU": "Twins_PINKPARI-RUST_M", "Qty": 2},
        ]
        r = svc.transform(rows)
        self.assertEqual(r['total_qty'], 12)
        self.assertTrue(any(x['pick_sku'] == 'NK_851_XL' and x['qty'] == 3 for x in r['items']))
        self.assertTrue(any(x['pick_sku'] == 'BS_22141_BEIGE_26' and x['qty'] == 4 for x in r['items']))
        self.assertTrue(any(x['design'] == 'PINKPARI_2_RUST' for x in r['items']))
        self.assertTrue(any(x['design'] == 'PINKPARI_RUST' for x in r['items']))

    def test_missing_party_inference_and_ambiguity(self):
        rows = [
            {"Master SKU": "TWINS_V_PANKHUDI_PINK_S", "Qty": 1},
            {"Master SKU": "MODA_PANKHUDI_PINK_S", "Qty": 1},
            {"Master SKU": "ANOUK_O_EVIL_EYE_L", "Qty": 2},
            {"Master SKU": "MODA_V_EVIL_EYE_L", "Qty": 1},
            {"Master SKU": "H&N_EVIL_EYE_L", "Qty": 2},
        ]
        r = svc.transform(rows)
        self.assertEqual(next(x for x in r['items'] if x['pick_sku'] == 'PANKHUDI_PINK_S')['qty'], 2)
        self.assertTrue(any(x['party'] == 'NO PARTY' and x['design'] == 'EVIL_EYE' for x in r['items']))
        self.assertEqual(sum(x['qty'] for x in r['items']), 7)

    def test_party_before_brand_and_glued_size(self):
        result = svc.transform([
            {"Master SKU": "LP_Twins_Sapna_Olive_L", "Qty": 2},
            {"Master SKU": "GIRL_AENA_BEIGE34", "Size": "34", "Qty": 1},
        ])
        self.assertEqual(result['audit'][0]['brand'], 'TWINS')
        self.assertEqual(result['audit'][0]['party'], 'LP')
        self.assertEqual(result['audit'][0]['pick_sku'], 'LP_SAPNA_OLIVE_L')
        self.assertEqual(result['audit'][1]['size'], '34')
        self.assertEqual(result['audit'][1]['design'], 'AENA_BEIGE')

    def test_invalid_quantities_not_silently_dropped(self):
        with self.assertRaises(svc.PicklistError):
            svc.transform([{"Master SKU": "TWINS_V_HELLY_PINK_S", "Qty": "1.5"}])
        with self.assertRaises(svc.PicklistError):
            svc.transform([{"Master SKU": "TWINS_V_HELLY_PINK_S", "Qty": 1},
                           {"Master SKU": "Total", "Qty": 2}])

    def test_excel_workbook_zip_integrity(self):
        from zipfile import ZipFile
        import xml.etree.ElementTree as ET
        r = svc.transform([{"Master SKU": "TWINS_V_HELLY_PINK_M", "Qty": 5},
                           {"Master SKU": "MODA_V_HELLY_PINK_M", "Qty": 1}])
        data = svc.to_xlsx(r)
        from io import BytesIO
        with ZipFile(BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            ET.fromstring(z.read('xl/workbook.xml'))
            self.assertIn('xl/worksheets/sheet1.xml', z.namelist())
            self.assertIn(b'6', z.read('xl/worksheets/sheet1.xml'))


if __name__ == '__main__':
    unittest.main()
