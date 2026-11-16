"""Packing Picklist v2: pure normalization, paging and Excel layout tests."""
import sys
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import packing_picklist_service as svc


class PackingPicklistTests(unittest.TestCase):
    def test_color_variant_merges_same_design_size_and_party(self):
        rows = [
            {"Master SKU": "TWINS_EVIL_EYE_S", "Qty": 1},
            {"Master SKU": "TWINS_EVIL_EYE_BLUE_S", "Qty": 1},
            {"Master SKU": "TWINS_EVIL_EYE_BLUE_M", "Qty": 3},
            {"Master SKU": "TWINS_EVIL_EYE_S", "Qty": 1},
        ]
        result = svc.transform(rows)
        self.assertEqual(result["total_qty"], 6)
        self.assertEqual(len(result["brand_items"]), 2)
        self.assertEqual({r['size']:r['qty'] for r in result['brand_items']}, {'S':3,'M':3})
        self.assertEqual({r['pick_sku'] for r in result['brand_items']}, {'EVIL_EYE_S','EVIL_EYE_M'})

    def test_preserve_design_numbers_and_variant_suffix(self):
        result = svc.transform([
            {"Master SKU": "TWINS_NK-851(A)_XL", "Qty": 1},
            {"Master SKU": "TWINS_NK_851(B)_XL", "Qty": 2},
            {"Master SKU": "TWINS_PINKPARI-2-RUST_M", "Qty": 3},
            {"Master SKU": "TWINS_PINKPARI-RUST_M", "Qty": 2},
            {"Master SKU": "TWINS_NK_851(1)_XL", "Qty": 1},
        ])
        self.assertEqual(result['total_qty'],9)
        self.assertTrue(any(x['pick_sku']=='NK_851_XL' and x['qty']==4 for x in result['brand_items']))
        self.assertTrue(any(x['design']=='PINKPARI_2' for x in result['brand_items']))
        self.assertTrue(any(x['design']=='PINKPARI' for x in result['brand_items']))

    def test_party_sort_no_party_then_v_nk_o_lp_neel_others(self):
        inputs = ['TWINS_DEV_TEST_M', 'TWINS_NEEL_TEST_M', 'TWINS_LP_TEST_M', 'TWINS_O_TEST_M',
                  'TWINS_NK_TEST_M', 'TWINS_V_TEST_M', 'TWINS_TEST_M']
        result = svc.transform([{'Master SKU':x,'Qty':1} for x in inputs])
        sorted_items = svc._sorted_items(result['brand_items'])
        self.assertEqual([i['party'] for i in sorted_items], ['NO PARTY','V','NK','O','LP','NEEL','DEV'])
        self.assertEqual(sum(x['qty'] for x in sorted_items), 7)

    def test_brand_separate_or_operator_override(self):
        rows = [
            {'Master SKU':'TWINS_V_HELLY_RED_M', 'Qty':2},
            {'Master SKU':'MODA_V_HELLY_BLUE_M', 'Qty':3},
        ]
        auto = svc.transform(rows, filename='Picklist_2026-11-16_09-14-41.xlsx')
        self.assertEqual(len(auto['brand_items']),2)
        self.assertEqual(auto['picklist_date'],'16/11/2026')
        self.assertEqual(len(svc.create_sheet_data(auto)),2)
        forced = svc.transform(rows, brand_override='TWINS')
        self.assertEqual(len(forced['brand_items']),1)
        self.assertEqual(forced['brand_items'][0]['qty'],5)

    def test_40_rows_per_page_5_column_blocks_with_spill(self):
        # 220 unique designs => 195 lines first page, overflow to next one.
        rows=[{'Master SKU':f'TWINS_V_DESIGN{n}_M','Qty':1} for n in range(220)]
        result=svc.transform(rows)
        sheets=svc.create_sheet_data(result)
        self.assertEqual(len(sheets),2)
        self.assertEqual(sum(sum(bool(row[c]) for c in (0,3,6,9,12)) for _,tab,_ in sheets for row in tab[1:]),220)
        for _,tab,kind in sheets:
            self.assertEqual(kind,'packing')
            self.assertLessEqual(len(tab),40)
            self.assertLessEqual(len(tab[0]),14)

    def test_no_total_row_and_xlsx_style(self):
        r=svc.transform([{'Master SKU':'TWINS_V_TEST_BLUE_M','Qty':2},
                         {'Master SKU':'TWINS_V_TEST_M','Qty':1},
                         {'Master SKU':'Total','Qty':3}], picklist_date='2026-11-16')
        self.assertEqual(r['total_qty'],3)
        self.assertEqual(len(r['brand_items']),1)
        b=svc.to_xlsx(r)
        with ZipFile(BytesIO(b)) as z:
            self.assertIsNone(z.testzip())
            root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            lines=root.findall('.//x:sheetData/x:row',ns)
            self.assertTrue(all(float(row.attrib['ht'])==38 for row in lines))
            self.assertEqual(len(lines),2)
            style=z.read('xl/styles.xml').decode()
            self.assertIn('sz val="22"',style)
            self.assertNotIn('FF075C49',style)
            self.assertIn(b'fitToWidth="1"',z.read('xl/worksheets/sheet1.xml'))

    def test_invalid_quantities_not_ignored(self):
        with self.assertRaises(svc.PicklistError):
            svc.transform([{'Master SKU':'TWINS_HELLY_M','Qty':'1.5'}])
        with self.assertRaises(svc.PicklistError):
            svc.transform([{'Master SKU':'TWINS_HELLY_M','Qty':1}, {'Master SKU':'Total','Qty':2}])

    def test_size_order_and_color_not_in_base(self):
        size=['XXL','S','XL','L','XS','M']
        records=[{'Master SKU':f'TWINS_V_EVIL_EYE_BLUE_{s}', 'Qty':1} for s in size]
        out=svc._sorted_items(svc.transform(records)['brand_items'])
        self.assertEqual([i['size'] for i in out], ['XS','S','M','L','XL','XXL'])

if __name__ == '__main__':
    unittest.main()
