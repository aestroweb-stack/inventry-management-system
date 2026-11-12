# Production deployment notes

यह नोट मौजूदा code की capabilities और अलग से जरूरी infrastructure को अलग रखता है। केवल Railway पर app deploy करने से Redis, object storage, pgvector migration, multiple workers, या AI models अपने-आप उपलब्ध नहीं होते।

## अभी code में

- PostgreSQL connections bounded pool से आते हैं। `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, timeout और recycle settings configure हो सकती हैं। कुल संभावित connections लगभग `web instances × workers × (pool size + overflow)` होंगे; इसे database plan limit के भीतर रखें।
- PostgreSQL पर product visual search के लिए pgvector HNSW index का one-off rebuild script है: `cd backend && python scripts/rebuild_visual_index.py`। इससे पहले backup लें और pgvector extension/permissions verify करें। Startup पर production schema migration नहीं चलती।
- Redis optional है: photo-search cache और shared scanner rate limit के लिए `REDIS_URL` दें। Redis down हो तो local per-process rate limiter चलता है।
- Product photos अभी app के local `backend/static/product_images` path में हैं। Multi-instance production के लिए इस folder को shared persistent volume/object storage में बदलना बाकी integration है।
- Current visual feature `colour-shape-v1` है—basic colour histogram/perceptual hash comparison, trained CLIP model नहीं। Top matches को कर्मचारी पुष्टि करता है और scan stock नहीं बदलता।

## बड़ा catalog चलाने से पहले

1. Production PostgreSQL backup/restore test और connection limit confirm करें।
2. Photo storage के लिए shared persistent volume या object-storage adapter implement और test करें; current local path multiple isolated instances के लिए पर्याप्त नहीं है।
3. PostgreSQL में pgvector enable करके maintenance window में visual index rebuild करें।
4. Optional Redis configure करें; फिर rate limit/cache shared हो सकते हैं।
5. Load test में actual image count, response time, DB pool usage, memory, and scan top-5 accuracy measure करें।
6. Multiple web instances तभी चलाएँ जब photo storage साझा हो और connection budget में workers fit हों।

2 लाख products या high traffic का performance इस repo से verified/guaranteed नहीं है। Multi-tenant/user auth, durable shared image storage, production load tests और trained fashion embeddings अलग काम हैं।
