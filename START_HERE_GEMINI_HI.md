# MYRAA — Railway + Gemini (start here)

Is ZIP mein Gemini ko default cloud provider bana diya gaya hai. **Key project files / frontend mein paste nahi karni.** Railway service > Variables mein add karein:

```text
MYRAA_CLOUD_PROVIDER=gemini
GEMINI_API_KEY=<your private API key>
MYRAA_CLOUD_MODEL=gemini-3.8-flash
APP_PASSWORD=<your unique password>
APP_SESSION_SECRET=<random 32+ characters>
MYRAA_CONFIRMATION_SECRET=<another random 32+ characters>
INVENTORY_MCP_API_KEY=<another random 32+ characters>
DATABASE_URL=<Railway PostgreSQL service reference>
PRODUCT_IMAGE_DIR=/data/product_images
```

1. Google AI Studio (`https://aistudio.google.com/apikey`) se API key banayein; key ko chat, source files ya GitHub mein share na karein.
2. ZIP extract karke `MYRAA_Inventory_Production` ke **contents** GitHub repository root par push karein. Railway par repo connect karein.
3. Railway mein PostgreSQL add karein; `DATABASE_URL` ko service reference banaayein. Inventory photographs ke liye Railway volume `/data` par mount karein.
4. Deploy. Railway `$PORT` deta hai; `railway.toml` mein start command set hai. Browser se `/app/` open karke `APP_PASSWORD` login karein.
5. `/api/assistant/status` ko login ke baad check karein: `cloud_enabled=true`, `provider=Gemini`, `model=gemini-3.8-flash` expected.
6. MYRAA ko bolein "Ivory Kurta size M stock batao". Write command "Ivory Kurta M mein 5 add karo" par preview check karke **Confirm change** dabayein.
7. MYRAA photo button se clothing image upload karein. Gemini Vision style/keywords identify karke possible catalog items dikhata hai; yeh verified SKU detection nahi hai.

**Voice:** User browser (supported Chrome/Edge) microphone se SpeechRecognition karta hai; text Railway backend par Gemini tool calls ko jata hai. Browser mic permission + HTTPS zaroori hain. Local desktop companion ki zaroorat inventory voice CRUD ke liye nahi, lekin dusre logon ke computers / local files ko Railway remotely control nahi kar sakta.

**Optional MCP:** Inventory MCP `/api/mcp/` already project mein hai; integrated Gemini tool calling use karne ke liye external MCP server ki zaroorat nahi.

**Security:** Operator login one-password only; multi-user RBAC nahi. Production deploy se pehle backups, authentication, costs, quota and security monitoring set karein. Api key server-side only. Real Gemini requests need valid key/quota; unit tests are mocked.

Detail: `RAILWAY_GEMINI_PRODUCTION_HI.md`
