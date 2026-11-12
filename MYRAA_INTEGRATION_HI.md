> **Recommended hosted Gemini setup:** This page describes the older optional local companion. For Railway-only MYRAA chat, browser voice and Gemini Vision read [RAILWAY_GEMINI_PRODUCTION_HI.md](RAILWAY_GEMINI_PRODUCTION_HI.md).

# MYRAA ko inventory app se connect karein

Inventory website ka typed MYRAA chat Railway cloud mode mein kisi bhi computer ke browser se chal sakta hai. Voice, screen sharing aur us computer ke browser/desktop control ke liye local MYRAA companion usi PC par chalana zaroori hai. Railway website server ko visitor ke microphone, desktop, browser tabs ya personal WhatsApp ka access nahi milta.

## 1. Railway cloud chat setup

Railway project ke inventory app service mein **Variables** kholen aur `GEMINI_API_KEY` ko Gemini AI Studio se li gayi key ke saath add karein. Key ko Git, frontend files, browser storage, ya public chat mein paste na karein. `GEMINI_API_KEY` server par hi rahegi. Railway redeploy ke baad kisi bhi computer se inventory website par MYRAA kholen; typed chat aur live inventory tools local desktop app ke bina chalenge.

Cloud chat live stock/order/dispatch/reorder data padhta hai. Stock, product, size, order import aur dispatch ke changes pehle preview dikhate hain; user ko **Confirm change** button dabana hoga. Chat mein “haan” likhne bhar se write commit nahi hota.

## 2. Local voice and computer-control setup

Requirements: Node.js 20+ aur Windows par Python 3.11+.

PowerShell mein repository ke MYRAA folder se:

```powershell
Set-Location E:\Inventry-Management-System\companion\myraa
npm ci
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r desktop_agent\requirements.txt
npm run app
```

`npm run app` MYRAA desktop window kholta hai, local voice/chat service start karta hai, aur PC-control agent ko `127.0.0.1` par chalata hai. Desktop agent ko alag network par expose na karein. Browser panel ke liye isi PC ke Chrome/Edge mein inventory website kholen, MYRAA desktop app chalu rakhein, aur website par MYRAA icon dabayen. Chrome/Edge poochhe ki inventory site aapke local network ke devices se connect kar sakti hai, to **Allow** chunein. Yeh browser permission local desktop companion ke liye zaroori hai. Agar app ya permission pehle block hui thi, site settings mein **Local network access** allow karke **Retry connection** dabayen.

Build files banane ke liye:

```powershell
npm run build
```

## 3. Local companion API keys

MYRAA settings mein:

1. Gemini API key paste karke save karein. Yeh local MYRAA voice/session ko connect karti hai. Website ka Railway cloud chat alag se server-side `GEMINI_API_KEY` use karta hai.
2. Inventory MCP endpoint ko default `https://inventry-management-system-production.up.railway.app/api/mcp/` par rehne dein, jab tak aapka server URL alag na ho.
3. Railway ke inventory service mein `INVENTORY_MCP_API_KEY` set karein. Wahi key MYRAA settings ke **Inventory MCP API key** mein paste karke **Save & test** dabayen.
4. Connection test tool count dikhata hai. Voice dobara connect karne par session inventory tools load karta hai.

Keys MYRAA ke per-user data folder mein rakhi jati hain (Windows par Electron ka user-data folder, aam taur par `%APPDATA%\myraa\`; direct development mode mein `%APPDATA%\MYRAA\`). Key browser history, frontend source, URL, logs, ya Git mein nahi rakhi jati. Root `.env.example` sirf server-side MCP key ke variable ka naam dikhata hai—usme asli key kabhi na daalein.

## 4. Kya kar sakta hai

- MYRAA Gemini Live ke through voice aur typed chat deta hai; typed chat microphone ke bina start ho sakti hai. Hindi, English, Hinglish ya doosri samarthit bhasha mein baat karein. Reply ko user ki latest language/script mein dene ke liye prompt kiya gaya hai. Voice model ki actual bhasha/pronunciation provider par nirbhar hai.
- Inventory MCP ke read tools se products, size-wise stock, orders, dispatch, reorder aur analytics dekhta hai.
- Inventory write ya dispatch ko pehle prepare karta hai. Exact product/SKU, size, quantity aur asar confirmation card mein dikhane ke baad hi user **Confirm change** dabaye to write execute hota hai.
- Local desktop agent alag se approved PC tasks ke liye hai. `/execute` ke liye per-install random bearer token chahiye; agent sirf loopback par bind hota hai.
- WhatsApp personal notifications/contacts scan karna aur background auto-send is setup mein nahi hai. Message/draft ko WhatsApp mein review karke user khud send kare.

## 5. Troubleshooting

- **Dusre computer par typed chat offline:** Railway Variables mein `GEMINI_API_KEY` set hai ya nahi dekhein; uske baad service redeploy aur page refresh karein. Chat status mein **Cloud AI online** dikhna chahiye.
- **Website par local voice/PC control offline:** MYRAA desktop app usi computer par chalu karein, Chrome/Edge prompt mein local network access allow karein, phir **Retry connection** dabayen. Yeh feature remote Railway server se nahi chalega.
- **Microphone nahi chalta:** browser mein microphone permission allow karein; MYRAA mein **Start voice** khud dabayen.
- **Gemini connect nahi hota:** Cloud chat ke liye Railway `GEMINI_API_KEY` aur Gemini quota check karein; local voice ke liye MYRAA settings mein key status check karein.
- **Inventory tools nahi dikhte:** MCP key, Railway deployment par `INVENTORY_MCP_API_KEY`, endpoint URL, aur **Save & test** result check karein. Key change ke baad voice reconnect karein.
- **PC control offline:** Python 3.11+ dependencies install karein, MYRAA restart karein, phir settings mein agent status check karein. Playwright browser automation ke liye `py -3.11 -m playwright install chromium` bhi chalayein.

## Inventory MCP security

`/api/mcp/` is protected by `INVENTORY_MCP_API_KEY`. Requests use the `X-API-Key` header over HTTPS. The MCP endpoint is not a public, anonymous API. Write tools use short-lived prepared actions and server-side confirmation tokens; a successful preview alone never changes inventory.
