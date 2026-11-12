# फोटो से सही product design खोजें

Visual scan uploaded photo ko catalog ke product-linked reference photos se SigLIP 2 image embeddings ke zariye compare karta hai. SKU/name/rang se score nahi banta. Scan sirf pehchan aur live stock dikhata hai; stock tab tak nahi badalta jab tak staff alag se size count update na kare.

## Scan kaise karein

1. Add Product mein har design ke liye saaf asli front/back/close-up photos select karein (8 tak). Pehli photo primary catalog image hoti hai; baaki usi product ID ke reference photos hain. Garment frame mein poora dikhe, blur na ho aur background kam ho.
2. Sidebar se **Visual Product Search** kholen.
3. Mobile/PC camera se photo lein ya JPG, PNG, WEBP (10 MB tak) upload karein.
4. `matched` aaye to product photo, name, sizes aur live stock dekhein. Phir design verify karke **इस design के sizes खोलें** chunein.
5. `ambiguous` aaye to candidates sirf suggestions hain. Kisi stock-changing action se pehle photo dekhkar sahi design chunein.
6. `no_match` aaye to app guess nahi karegi. Clear front photo lein, catalog mein photo check karein, ya product name/SKU se manual search karein.

## Match status aur score

- **matched:** sabse achha cosine similarity score configured minimum se upar hai aur agle design se kafi alag hai.
- **ambiguous:** kai photos ka score kareeb hai; suggestions milenge, automatic selection nahi.
- **no_match:** score threshold se kam hai, catalog khaali hai, ya koi active product candidate nahi mila.
- `Cosine similarity` ek model similarity score hai, probability ya accuracy guarantee nahi. Default `0.82` / margin `0.06` sirf conservative starting point hain—real Twins Lady photos ke bina ise calibrated nahi kaha ja sakta.

## Railway setup

Pura Railway setup, exact service variables, persistent volume, deployment ke baad index build aur safe validation steps [RAILWAY_VISUAL_MATCH_SETUP_HI.md](RAILWAY_VISUAL_MATCH_SETUP_HI.md) mein hain. Existing production database ko replace mat karein. Model optional hai; package ya index ready na ho to API 503 dekar unsafe guess rokegi.

## Local testing

```powershell
# Root repository folder se. This installs the heavy encoder only for a local
# single-process test; on Railway use the separate visual-model service above.
$env:DATABASE_URL = "sqlite:///./test_visual_match.db"
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r backend/requirements-visual-ai.txt
Set-Location backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

SQLite ke saath rebuild script nahi chalti. Local SQLite scans pehli baar photos ko encode karte hain, phir embeddings process memory mein cache hote hain; catalog 500 images tak limited hai. Regression checks root se `python tests/test_visual_match.py` chalakar run karein. Agar `PRODUCT_IMAGE_DIR` set karte hain to app aur test ko wahi path dena hoga.

## Photo badalne ke baad

Normal product image upload ke baad PostgreSQL embedding best-effort upsert hoti hai. Agar index kaam na kare, photo save rehti hai, lekin purana embedding hata diya jata hai—old photo ke naam par galat product match nahi aayega. Log mein warning check karein, PostgreSQL `vector` extension/index repair karein, phir one-off rebuild chalayein.

## Accuracy validation

Threshold ko production mein tune karne se pehle alag staging database mein kam-se-kam:

- same product ki alag lighting/angle/camera photos (positive samples)
- same rang ke lekin alag print, neckline, embroidery ya sleeve wale products (hard negatives)
- catalog ke bahar ke kapde aur non-garment images (no-match samples)
- close designs ka ambiguous pair

har sample ke expected design/status ko spreadsheet mein label karein. Matching aur wrong-match rate dekhkar threshold badlein. `matched` cases bhi visual review ke bina stock mutation ka authorization nahi hain. Abhi automated tests deterministic vectors use karte hain; real garment-photo benchmark repo mein nahi mila, isliye real-world match rate establish nahi hai.

## Seemayen

Model general-purpose SigLIP 2 Base hai, fashion-specific fine-tune nahi. Garments ke print aur cut mein bahut chhota farq, occlusion, folded clothing, mannequin/background, heavy crop ya multiple garments hone par galat ranking/`no_match` ho sakta hai. Multiple genuine views ek design ke saath attach ki ja sakti hain. Twins Lady labeled photos ka held-out benchmark phir bhi chahiye.
