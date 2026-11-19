/* MYRAA Business Workspace v1: costing, exact marketplace SKU mapping,
   supplier challan import/staging, and one-time inventory posting. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const E = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => `₹${Number(n || 0).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}`;
  async function json(url, options = {}) {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
    return body;
  }
  const send = (url, method, payload) => json(url, {method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const $qs = (container, selector) => container.querySelector(selector);
  const notice = (el, message, error = false) => { el.textContent = message; el.className = error ? 'business-message is-error' : 'business-message'; };

  window.myraaEnhanceProductDetail = (product, container) => {
    const seatGrid = container.querySelector('.seat-grid');
    if (!seatGrid) return;
    const block = document.createElement('section');
    block.className = 'business-product-card';
    const options = product.variants.map(v => `<option value="${E(v.size)}">${E(v.size)}</option>`).join('');
    const unitCost = product.unit_cost ?? '';
    const sizeRows = product.variants.map(v => `<label class="business-size-row"><span>${E(v.size)} · ${Number(v.current_stock).toLocaleString()} pcs</span><input type="number" step="0.01" min="0" class="input business-cost-size" data-size="${E(v.size)}" value="${v.cost_override ?? ''}" placeholder="Default"> <strong>${v.cost_value === null || v.cost_value === undefined ? 'Not priced' : money(v.cost_value)}</strong></label>`).join('');
    const aliases = product.variants.flatMap(v => (v.marketplace_aliases || []).map(a => ({...a, size:v.size})));
    block.innerHTML = `
      <header><h3><i class="fa-solid fa-receipt"></i> Product Costing & Marketplace SKUs</h3><p>Internal Master SKU: <strong>${E(product.sku)}</strong> · Known stock cost: <strong>${money(product.stock_cost_value)}</strong></p></header>
      <div class="business-product-grid"><div>
        <label class="field">Default cost per piece (₹)<input class="input business-default-cost" type="number" min="0" step="0.01" value="${E(unitCost)}" placeholder="Cost per unit"></label>
        <h4>Cost by size</h4><div class="business-size-costs">${sizeRows}</div>
        <button class="btn-primary business-save-cost" type="button"><i class="fa-solid fa-floppy-disk"></i> Save Costing</button>
      </div><div>
        <h4>Marketplace SKU aliases</h4><p class="hint">Each external SKU must match one exact size. Multiple marketplaces are supported.</p>
        <div class="business-aliases">${aliases.length ? aliases.map(a=>`<div class="business-alias"><span><b>${E(a.size)}</b> · ${E(a.marketplace)} · <code>${E(a.sku)}</code></span><button type="button" class="business-remove-alias" data-alias-id="${a.id}" title="Remove mapping"><i class="fa-solid fa-xmark"></i></button></div>`).join('') : '<p class="hint">No marketplace aliases yet.</p>'}</div>
        <form class="business-add-alias"><label>Size<select class="input" name="size" required>${options}</select></label><label>Marketplace<input class="input" name="marketplace" placeholder="Myntra / Ajio" required maxlength="80"></label><label>External SKU<input class="input" name="sku" placeholder="Marketplace SKU" required maxlength="180"></label><button type="submit" class="btn-secondary">+ Add Mapping</button></form>
      </div></div>
      <div class="business-gallery-extra"><label class="field">Add more product photos (after product creation)<input type="file" accept="image/jpeg,image/png,image/webp" multiple class="input business-extra-images"></label><button class="btn-secondary business-upload-images" type="button"><i class="fa-solid fa-images"></i> Upload Additional Photos</button><p class="hint">Existing front photo remains unchanged. Up to 8 images per upload; photos appear in product gallery.</p></div>
      <p class="business-message" aria-live="polite"></p>`;
    seatGrid.after(block);
    const feedback = block.querySelector('.business-message');
    const endpoint = `/api/products/${encodeURIComponent(product.sku)}`;
    const refresh = async () => {
      const data = await json(endpoint);
      if (typeof window.renderDetail === 'function') window.renderDetail(data, container, { showDelete: Boolean(container.querySelector('#detail-delete')) });
      else if (typeof window.openProductDetail === 'function') window.openProductDetail(product.sku);
    };
    block.querySelector('.business-save-cost').addEventListener('click', async () => {
      const b = block.querySelector('.business-save-cost'); b.disabled = true;
      try {
        const value = block.querySelector('.business-default-cost').value;
        const size_costs = {};
        block.querySelectorAll('.business-cost-size').forEach(input => { if (input.value !== '') size_costs[input.dataset.size] = Number(input.value); });
        await send(`${endpoint}/costing`, 'PUT', {unit_cost: value === '' ? null : Number(value), size_costs});
        await refresh();
      } catch (e) { notice(feedback, e.message, true); }
      finally { b.disabled = false; }
    });
    block.querySelector('.business-add-alias').addEventListener('submit', async event => {
      event.preventDefault();
      const b = event.target.querySelector('button'); b.disabled = true;
      try {
        await send(`${endpoint}/marketplace-aliases`, 'POST', Object.fromEntries(new FormData(event.target)));
        await refresh();
      } catch (e) { notice(feedback, e.message, true); }
      finally { b.disabled = false; }
    });
    block.querySelectorAll('.business-remove-alias').forEach(button => button.addEventListener('click', async () => {
      if (!window.confirm('Remove this marketplace SKU mapping? Existing order history is not deleted.')) return;
      try {
        await json(`${endpoint}/marketplace-aliases/${button.dataset.aliasId}`, {method:'DELETE'});
        await refresh();
      } catch (e) { notice(feedback,e.message,true); }
    }));
    block.querySelector('.business-upload-images').addEventListener('click', async () => {
      const files = block.querySelector('.business-extra-images').files;
      if (!files?.length) { notice(feedback, 'Choose one or more photos.', true); return; }
      if (files.length > 8) { notice(feedback, 'Maximum 8 images in one upload.', true); return; }
      const b = block.querySelector('.business-upload-images'); b.disabled = true;
      try {
        const payload = new FormData(); Array.from(files).forEach(f=>payload.append('files',f));
        await json(`${endpoint}/images`, {method:'POST',body:payload});
        await refresh();
      } catch(e) { notice(feedback,e.message,true); }
      finally { b.disabled = false; }
    });
  };

  // Stock valuation chart (known cost only). Missing-cost pieces are clearly flagged.
  const dashboard = $('view-dashboard');
  if (dashboard) {
    const panel = document.createElement('section');
    panel.className = 'business-cost-dashboard panel';
    panel.innerHTML = `<header><span class="section-eyebrow">INVENTORY COSTING</span><h2><i class="fa-solid fa-chart-column"></i> Stock Investment at Cost</h2><p class="hint">Counted stock × unit purchase cost for each size. This is cost value, not sales revenue.</p></header>
      <div id="business-cost-chart"><p class="hint">Loading costing…</p></div>
      <section class="business-cost-designs" aria-label="Cost by design">
        <div class="business-cost-designs-head"><h3>Cost by design · All products</h3><label class="business-design-search-label">Find design <input class="input" id="business-design-search" type="search" placeholder="Search Master SKU or product name"></label></div>
        <div id="business-design-table" role="status" aria-live="polite">Loading designs…</div>
        <div class="business-design-pagination"><button type="button" class="btn-secondary" id="business-design-prev">Previous</button><span id="business-design-page"></span><button type="button" class="btn-secondary" id="business-design-next">Next</button></div>
      </section>`;
    const target = dashboard.querySelector('.dashboard-inventory-title') || dashboard.querySelector('.filter-bar');
    if (target) target.before(panel); else dashboard.append(panel);
    async function loadCosts() {
      const host = $('business-cost-chart');
      try {
        const r = await json('/api/costing/summary');
        const top = r.products.slice(0, 12);
        const max = Math.max(...top.map(x => x.cost_value),1);
        host.innerHTML = `<div class="business-cost-total"><strong>${money(r.total_known_cost)}</strong><span>Known total stock cost</span></div>${r.unpriced_pieces ? `<p class="business-cost-warning"><i class="fa-solid fa-triangle-exclamation"></i> ${r.unpriced_pieces} pieces (${r.unpriced_sizes} sizes) still need costing; grand total is incomplete.</p>` : ''}<div class="business-bars">${top.map(x=>`<div class="business-bar"><span title="${E(x.name)}">${E(x.sku)}</span><div><i style="width:${Math.max(0,100*x.cost_value/max)}%"></i></div><strong>${money(x.cost_value)}</strong></div>`).join('') || '<p class="hint">Set product unit costs to populate this chart.</p>'}</div>`;
      } catch (e) { host.innerHTML = `<p class="hint">Cost report unavailable: ${E(e.message)}</p>`; }
    }
    // Search and paginate the COMPLETE design catalog, beyond the top-12 chart.
    const designPager = {q:'',offset:0,limit:20};
    async function loadDesignPage() {
      const host = $('business-design-table'), pageLabel = $('business-design-page');
      try {
        const params = new URLSearchParams({q:designPager.q,offset:String(designPager.offset),limit:String(designPager.limit)});
        const data = await json('/api/costing/designs?' + params);
        const items = data.items || [];
        host.innerHTML = `<div class="business-design-table-wrap"><table class="business-design-table"><thead><tr><th>Master SKU</th><th>Design name</th><th>Counted pieces</th><th>Known stock cost</th><th>Unpriced pieces</th></tr></thead><tbody>${items.map(x=>`<tr><td><code>${E(x.sku)}</code></td><td>${E(x.name)}</td><td>${Number(x.stock||0).toLocaleString('en-IN')}</td><td>${money(x.cost_value)}</td><td>${x.unpriced_pieces ? `<strong class="business-cost-warning-text">${x.unpriced_pieces}</strong>` : '—'}</td></tr>`).join('') || '<tr><td colspan="5">No designs found.</td></tr>'}</tbody></table></div>`;
        pageLabel.textContent = `${data.total ? data.offset+1 : 0}–${Math.min(data.offset+data.limit,data.total)} of ${data.total} designs`;
        $('business-design-prev').disabled = designPager.offset === 0;
        $('business-design-next').disabled = designPager.offset + designPager.limit >= data.total;
      } catch (err) {
        host.textContent = 'Cannot load design costs: ' + err.message;
        pageLabel.textContent = '';
      }
    }
    let designSearchTimer;
    $('business-design-search').addEventListener('input',event=>{
      clearTimeout(designSearchTimer);
      designSearchTimer = setTimeout(()=>{designPager.q=event.target.value.trim(); designPager.offset=0; loadDesignPage();},260);
    });
    $('business-design-prev').addEventListener('click',()=>{
      designPager.offset=Math.max(0,designPager.offset-designPager.limit); loadDesignPage();
    });
    $('business-design-next').addEventListener('click',()=>{
      designPager.offset+=designPager.limit; loadDesignPage();
    });
    const refreshCosts = () => { loadCosts(); loadDesignPage(); };
    window.myraaLoadCosting = refreshCosts;
    document.querySelector('.rail-link[data-view="dashboard"]')?.addEventListener('click',refreshCosts);
    refreshCosts();
  }

  // Inbound purchase challan workflow: staged receipt != stock entry.
  const challanView = $('view-challans');
  if (!challanView) return;
  let selectedId = null, selected = null;
  async function loadChallans() {
    const listing = $('challan-batches'), summary = $('challan-summary');
    try {
      const data = await json('/api/purchase-challans');
      summary.innerHTML = `<div><small>Supplier orders</small><strong>${data.items.length}</strong></div><div><small>Total pieces ordered</small><strong>${data.ordered_pieces.toLocaleString()}</strong></div><div><small>Pending to receive</small><strong>${data.pending_pieces.toLocaleString()}</strong></div><div><small>Known pending value</small><strong>${money(data.pending_cost_known)}</strong>${data.pending_unpriced_pieces ? `<small>${data.pending_unpriced_pieces} pieces still need costing</small>` : ''}</div>`;
      listing.innerHTML = data.items.map(b => `<button class="challan-batch ${b.id===selectedId?'is-selected':''}" type="button" data-id="${b.id}"><span><b>${E(b.party_name)} · ${E(b.challan_no)}</b><small>${E(b.order_date)} · ${E(b.status)} · ${E(b.filename)}</small></span><strong>${b.pending} pending / ${b.ordered}</strong></button>`).join('') || '<p class="hint">Import an Excel to begin tracking supplier orders.</p>';
      listing.querySelectorAll('button[data-id]').forEach(button => button.addEventListener('click', () => openChallan(Number(button.dataset.id))));
      if (selectedId) await openChallan(selectedId);
    } catch (e) { listing.innerHTML = `<p class="business-message is-error">${E(e.message)}</p>`; }
  }
  window.myraaLoadChallans = loadChallans;
  async function openChallan(id) {
    try { selected = await json(`/api/purchase-challans/${id}`); selectedId = id; renderChallan(); }
    catch (e) { alert(`Cannot load challan: ${e.message}`); }
  }
  function renderChallan() {
    const host = $('challan-detail'), panel = $('challan-detail-panel'); panel.hidden = false;
    host.innerHTML = `<div class="challan-detail-head"><div><h2>${E(selected.party_name)} · ${E(selected.challan_no)}</h2><p class="hint">Invoice ${E(selected.invoice_no||'—')} · ${E(selected.order_date)} · Status: ${E(selected.status)}</p></div><div><strong>${selected.received}/${selected.ordered} received</strong><small>${selected.pending} pending pieces</small></div></div><div class="table-wrap"><table class="grid challan-lines"><thead><tr><th>Master SKU</th><th>Product</th><th>Size</th><th>Ordered</th><th>Received (to stage)</th><th>Pending</th><th>Unit Cost ₹</th></tr></thead><tbody>${selected.lines.map(x=>`<tr><td><code>${E(x.sku)}</code></td><td>${E(x.name)}</td><td><b>${E(x.size)}</b></td><td>${x.qty_ordered}</td><td><input class="input" type="number" min="0" max="${x.qty_ordered}" step="1" data-received="${x.id}" value="${x.qty_received}" ${selected.status==='POSTED'?'disabled':''}></td><td>${x.pending}</td><td><input class="input" type="number" min="0" step="0.01" data-cost="${x.id}" value="${x.unit_cost ?? ''}" placeholder="Not set" ${selected.status==='POSTED'?'disabled':''}></td></tr>`).join('')}</tbody></table></div><div class="challan-actions"><button id="challan-save-receipt" class="btn-secondary" type="button" ${selected.status==='POSTED'?'disabled':''}>Save Received Quantities</button><button id="challan-receive-all" class="btn-secondary" type="button" ${selected.status==='POSTED'?'disabled':''}>Mark All Received</button><button id="challan-push" class="btn-primary" type="button" ${selected.status!=='READY'?'disabled':''}>Push Entire Challan to Inventory</button><p class="hint">Inventory updates only when every size is fully received, every unit cost is set, and you explicitly push. Repeat push is blocked.</p></div><p id="challan-detail-msg" class="business-message" aria-live="polite"></p>`;
    host.querySelector('#challan-receive-all')?.addEventListener('click', () => host.querySelectorAll('[data-received]').forEach(input => {input.value = input.max;}));
    const receiptPayload = () => selected.lines.map(line => {
      const r = host.querySelector(`[data-received="${line.id}"]`);
      const c = host.querySelector(`[data-cost="${line.id}"]`);
      const value = Number(r.value);
      if (!Number.isInteger(value) || value < 0 || value > line.qty_ordered) throw new Error(`${line.sku}/${line.size}: received must be between 0 and ${line.qty_ordered}`);
      if (c.value && (!Number.isFinite(Number(c.value)) || Number(c.value)<0)) throw new Error('Invalid unit cost');
      return {line_id:line.id, received:value, unit_cost:c.value===''?null:Number(c.value)};
    });
    host.querySelector('#challan-save-receipt')?.addEventListener('click', async () => {
      const msg = $('challan-detail-msg');
      try { selected = await send(`/api/purchase-challans/${selected.id}/receipt`,'PATCH',{lines:receiptPayload()}); renderChallan(); await loadChallans(); }
      catch(e) { notice(msg,e.message,true); }
    });
    host.querySelector('#challan-push')?.addEventListener('click', async () => {
      if (!window.confirm(`Push ${selected.ordered} received pieces from ${selected.party_name} / ${selected.challan_no} into main stock? This cannot be pushed twice.`)) return;
      const button = $('challan-push'); button.disabled=true;
      try { const result = await send(`/api/purchase-challans/${selected.id}/push`,'POST',{}); await loadChallans(); window.myraaLoadCosting?.(); alert(`Inventory updated: ${result.received_pieces} pieces.`); }
      catch(e){ notice($('challan-detail-msg'),e.message,true); button.disabled=false; }
    });
  }
  $('challan-import-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form=event.target, button=form.querySelector('button[type=submit]'); button.disabled = true;
    const message = $('challan-import-message');
    try {
      const data=await json('/api/purchase-challans/import',{method:'POST',body:new FormData(form)});
      form.reset(); selectedId = data.id; notice(message,'Challan imported. Confirm received quantities before inventory push.');
      await loadChallans();
    } catch(e){ notice(message,e.message,true); }
    finally { button.disabled=false; }
  });
  const dateInput = $('challan-import-form')?.querySelector('[name=order_date]');
  if (dateInput && !dateInput.value) dateInput.value = new Date(Date.now() - new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
})();
