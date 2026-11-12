# Inventory Management System — Software User Manual

Yeh guide desktop aur mobile dono par app ke roz ke kaam ke liye hai. App HTTPS address par kholen. Mobile par navigation ke liye **☰ Menu** dabayen; dark mode ke liye **☾/☀** control use karein.

## 1. Dashboard

Dashboard par total designs, size variants, uncounted sizes, out-of-stock, low-stock aur in-stock counts dikhte hain. “Not counted” ka matlab hai us size ka physical count abhi system mein enter nahi hua. Search se SKU, product name ya size dhoondhein; status filter aur Previous/Next se results dekhein. Top Movers ka period 7, 30 ya 90 din kar sakte hain.

## 2. Products

### Ek design add karna

1. **Products → Add Product** kholen.
2. Design ka unique base SKU, product name aur category bharein.
3. Har size ke liye alag row aur opening physical stock dalein. Ek hi design ke sizes ek product ke andar rakhein.
4. Photo optional hai; abhi skip karke baad mein product edit karke add ki ja sakti hai.
5. Save ke baad success ya photo-upload warning padhein. Agar “Product added” dikh jaye to form dobara submit na karein; Products search mein SKU/name se check karein.

### Excel se bulk product import

1. **Add Product → Bulk from Excel** kholen aur **Download Excel format** chunein.
2. Har size ke liye ek row bharein. Same base SKU ki repeated rows ko system ek design ke sizes mein group karta hai.
3. SKU + Size repeat na karein; invalid row/errors ko import result ke mutabik sheet mein sudharein.
4. File upload karke **Import** dabayen. Result mein new designs, sizes, skipped rows aur row errors padhein.
5. Import existing sizes ka stock overwrite nahi karta; current stock badalne ke liye **Bulk Stock Count** use karein.

### Product dekhna/edit karna

Products search mein name, SKU, color ya size type karein. Design card kholkar sab sizes/stock dekhein. Edit se name/category/photo/size details badlein. Delete/archive action se pehle SKU aur design confirm karein. Photos ke liye preview aur upload success message dekhein.

## 3. Scan & Recount

1. **Scan & Recount** mein camera scan chunein aur browser ka camera permission prompt aaye to app istemal ke liye Allow karein.
2. Product QR ya Code128 scan karne par design aur uske sab sizes dikhte hain. Size-specific barcode ho to woh specific size kholta hai.
3. Camera available na ho to **Manual Entry** mein product name, color ya SKU search karein; result card mein sab sizes dikhte hain.
4. Sahi size ka actual physical count bharein aur **Update** dabayen. Update hone ke baad success message/check karein.
5. USB/Bluetooth scanner gun aam taur par keyboard jaisa input bhejti hai. Scanner ka output field mein aaye to code Enter ke saath submit hota hai.

### Visual Product Search

Sidebar se **Visual Product Search** kholen. Camera photo lein ya JPG/PNG/WEBP upload karein (10 MB tak). SigLIP 2 multilingual image encoder catalog ke saved reference photos se visual comparison karta hai. Add Product mein ek design ki front/back/close-up photos (total 8 tak) select kar sakte hain; pehli photo main catalog image hogi aur baaki usi product se link hongi:

- **Strong match:** photo, product name aur database se live size-wise stock dekhein; phir design verify karein.
- **Ambiguous:** candidates sirf suggestions hain. Photo dekhkar sahi design khud chunein.
- **No match:** app guess nahi karegi. Saaf front photo lein ya manual name/SKU search use karein.

Photo match se stock kabhi update nahi hota. Design kholne par app **Scan & Recount** page par uske sizes laaye. Sahi size ka count alag se submit karein. Har product par pehle clear photo add karein. SigLIP 2 generic pretrained vision-language model hai, Twins Lady ke designs par fine-tuned nahi; similar designs ko user verify karein, aur “Ambiguous” ko direct match na maanein. Model/index setup unavailable ho to feature recoverable error dikhayega; name/SKU ke liye manual search use karein. Railway administrator setup ke liye [RAILWAY_VISUAL_MATCH_SETUP_HI.md](RAILWAY_VISUAL_MATCH_SETUP_HI.md) dekhein.

## 4. Bulk Stock Count

1. **Bulk Stock Count** kholen aur current stock count sheet download karein.
2. **Count** column mein shelf par gina hua final number bharein; SKU/Size ko edit na karein.
3. File upload karein aur import summary plus row errors padhein.
4. Upload actual count set karta hai, quantity add nahi karta. Dobara wahi sheet upload karne se pehle latest sheet download karein.

## 5. Process Orders

1. Marketplace orders ki supported Excel/CSV file **Process Orders** mein upload karein.
2. Matching summary dekhein—matched, unmatched, pending/fulfilled lines aur row errors.
3. Upload pending demand record karta hai; ise dispatch na samjhein aur sirf upload se physical shelf count kam nahi hota.
4. Batch ko pack karke bhejne ke baad usi batch mein **Dispatch** karein. Dispatch stock ledger mein deduction record karta hai; ek batch ko duplicate dispatch na karein.

## 6. Reorder List

Reorder view size-wise physical stock, pending order demand, dispatched quantity aur restock requirement dikhata hai. Dispatched units pehle hi stock se deduct ho chuke hote hain; unhe pending demand samajhkar dobara ghataane ki zaroorat nahi. Name/SKU search aur Previous/Next se list dekhein. Excel export se vendor/picklist sheet download karein aur order/size quantities cross-check karke hi purchase bhejein.

## 7. Alerts

Alerts page par design search karein; alert rows design ke neeche size-wise grouped hote hain aur severity se sorted rehte hain. **New count** mein physical count likhkar **Update** ya Enter dabayen. Search karne par matching designs hi dikhte hain. Previous/Next buttons 30 designs per page move karte hain. “No open alerts” ka ek hi empty message aata hai; page count “Page 1 of 1 · 0 designs” dikhata hai.

## 8. History

History mein stock changes, order deductions aur manual adjustments newest first hote hain. SKU/product name se search aur Previous/Next se purane entries dekhein. Count mismatch ho to history ke timestamp, change aur balance ko physical recount ke saath compare karein.

## 9. Print Labels

**Print Labels** mein ek design search karein aur uska ek product barcode download/print karein. Ek product barcode scan karne par sab sizes khulte hain. Label ko dry, scan-able jagah par chipkayein aur pehle ek baar scan verify karein.

## 10. Trash

Trash mein archived designs ko review karein. Restore karne se design wapas active list mein aata hai. **Delete forever/empty** jaisa permanent action recover nahi hota—SKU, sizes aur history par uska asar check kiye bina use na karein.

## 11. MYRAA Assistant

MYRAA website panel ko chalane ke liye isi PC par MYRAA desktop companion kholen. Robot icon dabakar panel kholen; voice ke liye **Start voice** dabayen aur browser microphone permission allow karein. Hindi, Hinglish ya English mein baat karein. **End voice** dabayen ya panel band karein to mic off hota hai. Text chat bhi assistant panel se bhej sakte hain.

Examples: “Blue kurta size M ka stock batao”, “Mustard wali M size mein 5 add karo”, “Kids NK 101 ke sab sizes dikhao”, “Kaunse orders dispatch ke liye ready hain?”, “Aaj ke dispatch ka summary do”. Inventory action se pehle details confirmation card mein dikhni chahiye. **Confirm change** tabhi dabayen jab SKU/size/quantity sahi ho; galat ya unclear ho to **Cancel** karein.

MYRAA ki Gemini aur inventory MCP keys uske local Settings mein daalein. Setup, browser connection aur troubleshooting ke liye [MYRAA integration guide](MYRAA_INTEGRATION_HI.md) dekhein.

## 12. Production data safety

- Production customer data ke liye app ka production `DATABASE_URL` persistent PostgreSQL par point hona chahiye. Local laptop SQLite data Railway se alag hota hai; local test data production mein nahi jata.
- Railway deploy se pehle database service/volume attached hai aur recent restorable backup maujood hai, yeh hosting dashboard mein verify karein. App ke UI se automatic backup ka dawa nahi hai.
- Feature release se pehle test suite ko isolated test DB par chalayein. Production par demo product, fake order, stock adjustment ya test dispatch kabhi na karein.
- Excel import/update ke baad summary aur error rows check karein. Production data ke liye pehli import copy par run karein, original workbook sambhal kar rakhein, aur import complete hone par product count compare karein.
- Deploy ke baad dashboard/product counts aur DB service status ko read-only tareeqe se verify karein. Unexpected zero counts ya 5xx dikhe to import shuru na karein; pehle service variables/database connection resolve karein.
- Database migrations ko backup aur rollback plan ke bina production par na chalayein. Customer photo files ke liye Railway persistent storage configuration bhi confirm karein.

## Common problems

- **Product save nahi hua:** Products list mein exact SKU search karein; success toast ke baad dobara create na karein. Duplicate SKU aaye to existing design edit karein ya missing size add karein.
- **Stock update reject:** latest current count dobara dekhein; stale count ho to recount karein aur correct size se update karein.
- **Camera/mic nahi chalta:** HTTPS use karein, address-bar site settings mein permission allow karein, aur earbuds ka microphone device input settings mein select karein.
- **Excel row reject:** result ki row number, SKU/Size aur error message ke mutabik sheet sudharein; template ke headers ko rename na karein.
- **Production zero designs/500:** Koi production data add na karein. Railway app service ki `DATABASE_URL`, PostgreSQL service status, deployment logs aur database persistence check karein.
