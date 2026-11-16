"""MYRAA Packing v3: dynamic seller/party/product and print layout safety tests."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from services import packing_picklist_service as svc

NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


class PackingTests(unittest.TestCase):
    def test_color_and_variant_merge_without_stripping_eye(self):
        rows = [
            {'Master SKU':'EVIL_EYE_S','Qty':1},
            {'Master SKU':'EVIL_EYE_BLUE_S','Qty':1},
            {'Master SKU':'EVIL_EYE(B)_BLUE_S','Qty':2},
            {'Master SKU':'EVIL_EYE(2)_RED_M','Qty':3},
        ]
        out = svc.transform(rows)
        self.assertEqual(out['total_qty'],7)
        self.assertEqual({r['pick_sku']:r['qty'] for r in out['brand_items']}, {'EVIL_EYE_S':4,'EVIL_EYE_RED_M':3})

    def test_dynamic_brands_and_unknown_party(self):
        rows = [
            {'Master SKU':'ACME_ZP_EVIL_EYE_S', 'Qty':1},
            {'Master SKU':'ACME_ZP_EVIL_EYE_RED_S', 'Qty':1},
            {'Master SKU':'ACME_ZP_EVIL_EYE_BLUE_S', 'Qty':2},
            {'Master SKU':'ACME_ZP_DRAGON_BLACK_M', 'Qty':3},
            {'Master SKU':'ACME_EVIL_EYE_RED_S', 'Qty':5},
            {'Master SKU':'DOLLY_NEEL_EVIL_EYE_BLUE_S', 'Qty':7},
            {'Master SKU':'DOLLY_NK_DRAGON_WHITE_M', 'Qty':11},
        ]
        o=svc.transform(rows)
        group={(x['brand'],x['party'],x['design'],x['size']): x['qty'] for x in o['brand_items']}
        self.assertEqual(o['total_qty'],30)
        self.assertEqual(group[('ACME','ZP','EVIL_EYE','S')],4)
        self.assertEqual(group[('ACME','NO PARTY','EVIL_EYE_RED','S')],5)
        self.assertEqual(group[('DOLLY','NEEL','EVIL_EYE_BLUE','S')],7)
        self.assertEqual(group[('DOLLY','NK','DRAGON_WHITE','M')],11)

    def test_single_sku_with_no_brand_keeps_product(self):
        r=svc.transform([{'Master SKU':'EVIL_EYE_BLUE_S','Qty':1}])
        self.assertEqual(r['brand_items'][0]['pick_sku'],'EVIL_EYE_BLUE_S')
        self.assertEqual(r['brand_items'][0]['brand'],'UNSPECIFIED')
        self.assertEqual(r['review_count'],1)

    def test_size_column_priority_and_note(self):
        o=svc.transform([{'Master SKU':'TWINS_ATLANTA_COFFE_L','Size':'30','Color':'COFFE','Qty':1},
                         {'Master SKU':'TWINS_ATLANTA_BEIGE_30','Size':'30','Color':'BEIGE','Qty':2},
                         {'Master SKU':'TWINS_HELLY_BLUE_S','Size':'S','Color':'BLUE','Qty':1}])
        self.assertEqual(o['total_qty'],4)
        self.assertEqual(sum(x['qty'] for x in o['brand_items'] if x['design'].startswith('ATLANTA_') and x['size']=='30'),3)
        self.assertTrue(any('Size conflict' in a['note'] for a in o['audit']))

    def test_distinct_colors_stay_distinct_without_uncolored_match(self):
        out = svc.transform([
            {'Master SKU':'BRAND_V_HELLY_PINK_S','Qty':2},
            {'Master SKU':'BRAND_V_HELLY_PURPLE_S','Qty':3},
            {'Master SKU':'BRAND_V_DRAGON_RED_M','Qty':1},
        ])
        skus={x['pick_sku']:x['qty'] for x in out['brand_items']}
        self.assertEqual(skus['HELLY_PINK_S'],2)
        self.assertEqual(skus['HELLY_PURPLE_S'],3)

    def test_design_numbers_preserved(self):
        rows=[{'Master SKU':'ACME_NK-851(A)_BLUE_XXL','Qty':1},
              {'Master SKU':'ACME_NK_851(2)_RED_XXL','Qty':2},
              {'Master SKU':'ACME_PINKPARI-2-WINE_S','Qty':5},
              {'Master SKU':'ACME_PINKPARI_WINE_S','Qty':6}]
        out=svc.transform(rows)
        texts={x['pick_sku'] for x in out['brand_items']}
        self.assertTrue(any('851' in s for s in texts))
        self.assertTrue(any('PINKPARI_2_WINE_S' == s for s in texts))
        self.assertTrue(any('PINKPARI_WINE_S' == s for s in texts))
        self.assertEqual(sum(x['qty'] for x in out['brand_items']),14)

    def test_party_size_order(self):
        rows=[]
        for party in ['NEEL','LP','O','NK','V','DEV','']:
            for size in ['XXL','XL','XS','M','L','S']:
                rows.append({'Master SKU':'SHOP_'+(party+'_' if party else '')+'TEST_BLUE_'+size,'Qty':1})
        out=svc.transform(rows)
        ordered=svc._sorted_items(out['brand_items'])
        groups=list(dict.fromkeys(x['party'] for x in ordered))
        self.assertEqual(groups,['NO PARTY','V','NK','O','LP','NEEL','DEV'])
        self.assertEqual([x['size'] for x in ordered[:6]],['XS','S','M','L','XL','XXL'])

    def test_single_marketplace_summary_determines_seller(self):
        rows=[{'Master SKU':'H&N_GULNAR_M','Qty':1,'Marketplace':'Myntra PPMP(MY_MFB-Moda)','Count':2},
              {'Master SKU':'MODA_NEEL_SAPNA_CREAM_XXL','Qty':1,'Marketplace':'Total','Count':2}]
        result=svc.transform(rows)
        self.assertEqual(result['brand_totals'],{'MODA':2})
        self.assertEqual({x['pick_sku'] for x in result['brand_items']},{'GULNAR_M','NEEL_SAPNA_CREAM_XXL'})

    def test_mixed_summary_doesnt_falsely_assign_seller(self):
        rows=[{'Master SKU':'ANOUK_HELLY_M','Qty':1,'Marketplace':'Myntra PPMP 2(MY_Twinslady)','Count':1},
              {'Master SKU':'MODA_HELLY_M','Qty':1,'Marketplace':'Myntra PPMP(MY_MFB-Moda)','Count':1}]
        out=svc.transform(rows)
        self.assertEqual(sum(out['brand_totals'].values()),2)
        self.assertNotEqual(len(out['brand_totals']),1)

    def test_dynamic_one_to_six_lanes_and_multiple_print_pages(self):
        for count,expected_lanes in [(1,1),(40,2),(155,4),(196,6),(250,6)]:
            rows=[{'Master SKU':f'SHOP_V_DESIGN{i}_S','Qty':1} for i in range(count)]
            out=svc.transform(rows)
            sheets=svc.create_sheet_data(out)
            self.assertEqual(len(sheets),1)
            tab=sheets[0][1]
            lanes=(len(tab[0])+1)//3
            self.assertEqual(lanes,expected_lanes)
            self.assertEqual(sum(1 for row in tab if row and row[0] and str(row[0]).startswith('SHOP-')), (len(tab)+39)//40)
            self.assertEqual(sum(1 for row in tab for i in range(0,len(row),3) if row[i] and 'DESIGN' in str(row[i])),count)
            data=svc.to_xlsx(out)
            with ZipFile(BytesIO(data)) as z:
                self.assertIsNone(z.testzip())
                xml=z.read('xl/worksheets/sheet1.xml')
                root=ET.fromstring(xml)
                self.assertNotIn(b'<mergeCells',xml)
                self.assertIn(b'fitToHeight="0"',xml)
                self.assertIn(b'fitToWidth="1"',xml)
                rowsxml=root.findall('.//x:sheetData/x:row',NS)
                self.assertTrue(all(float(r.attrib['ht'])==38 for r in rowsxml))
                if count>234:
                    self.assertIsNotNone(root.find('.//x:rowBreaks',NS))
                self.assertIn(b'sz val="22"',z.read('xl/styles.xml'))

    def test_totals_check(self):
        with self.assertRaises(svc.PicklistError):
            svc.transform([{'Master SKU':'TEST_BLUE_M','Qty':1},{'Master SKU':'Total','Qty':2}])
        with self.assertRaises(svc.PicklistError):
            svc.transform([{'Master SKU':'TEST_BLUE_M','Qty':'1.5'}])


if __name__ == '__main__':
    unittest.main()
