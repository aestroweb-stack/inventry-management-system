/* MYRAA Product & Challan v2: clean product editor, explicit SKU mapping,
   product gallery upload, invoice scans and staged receipts. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const E = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => `₹${Number(n || 0).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})}`;
  async function json(url, options={}) {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
    return body;
  }
  const send = (url, method, payload) => json(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const notice = (el, message, error=false) => {
    if (!el) return;
    el.textContent = message;
    el.className = error ? 'business-message is-error' : 'business-message';
  };

  // Both scopes are explicit; whole-product SKU needs Size in the order sheet.
  const aliasOptions = (includeWhole=true) => {
    const sizes = [...new Set(Array.from(document.querySelectorAll('#size-rows .size-size'))
      .map(el => el.value.trim().toUpperCase()).filter(Boolean))];
    return `${includeWhole ? '<option value="">Whole product (Size required in orders)</option>' : ''}`
      + sizes.map(size => `<option value="${E(size)}">Only size ${E(size)}</option>`).join('');
  };
  const aliasRows = $('new-product-alias-rows');
  function addNewProductSku() {
    const row=document.createElement('div');
    row.className='sku-entry-row';
    row.innerHTML = `<input class="input sku-entry-code" type="text" required maxlength="180" placeholder="Marketplace SKU" aria-label="Marketplace SKU">
      <input class="input sku-entry-market" type="text" maxlength="80" placeholder="Marketplace (optional)" aria-label="Marketplace">
      <select class="input sku-entry-size" aria-label="SKU scope">${aliasOptions()}</select>
      <button class="sku-entry-remove" type="button" aria-label="Remove SKU row"><i class="fa-solid fa-xmark"></i></button>`;
    row.querySelector('.sku-entry-remove').addEventListener('click',()=>row.remove());
    aliasRows?.appendChild(row);
    row.querySelector('.sku-entry-code')?.focus();
  }
  $('new-product-add-alias')?.addEventListener('click', addNewProductSku);
  window.myraaResetProductAliases = () => aliasRows?.replaceChildren();
  window.myraaCollectNewProductAliases = () => {
    const sizes = new Set(Array.from(document.querySelectorAll('#size-rows .size-size'))
      .map(el => el.value.trim().toUpperCase()).filter(Boolean));
    const seen = new Set();
    return Array.from(aliasRows?.querySelectorAll('.sku-entry-row') || []).map((row, i) => {
      const sku = row.querySelector('.sku-entry-code').value.trim();
      const size = row.querySelector('.sku-entry-size').value.trim();
      const marketplace = row.querySelector('.sku-entry-market').value.trim() || 'OTHER';
      if (!sku) throw new Error(`SKU mapping ${i+1}: enter marketplace SKU`);
      if (size && !sizes.has(size.toUpperCase())) throw new Error(`SKU mapping ${i+1}: size ${size} is not in product sizes`);
      if (seen.has(sku.toUpperCase())) throw new Error(`SKU mapping ${i+1}: duplicate marketplace SKU`);
      seen.add(sku.toUpperCase());
      return {sku, size: size || null, marketplace};
    });
  };

  window.myraaEnhanceProductDetail = (product, container) => {
    const seatGrid = container.querySelector('.seat-grid');
    if (!seatGrid) return;
    const block = document.createElement('section');
    block.className = 'business-product-card business-product-v2';
    const options = product.variants.map(v=>`<option value="${E(v.size)}">Only size ${E(v.size)}</option>`).join('');
    const aliases = [
      ...(product.product_aliases || []).map(a=>({...a, size:null, scope:'product'})),
      ...product.variants.flatMap(v=>(v.marketplace_aliases || []).map(a=>({...a,size:v.size,scope:'size'})))
    ];
    const sizeRows=product.variants.map(v=>`<div class="business-size-summary"><b>${E(v.size)}</b><span>${Number(v.current_stock||0).toLocaleString('en-IN')} pcs</span><strong>${v.cost_value===null||v.cost_value===undefined?'Cost not set':money(v.cost_value)}</strong></div>`).join('');
    block.innerHTML = `
      <header class="business-v2-head"><div><h3><i class="fa-solid fa-box-open"></i> Master SKU &amp; Product Costing</h3>
        <p>Master SKU <code>${E(product.sku)}</code> · Known stock cost <b>${money(product.stock_cost_value)}</b></p></div></header>
      <div class="business-product-grid-v2">
        <section class="business-cost-editor"><h4>Purchase Cost</h4>
          <label class="field">Default unit cost (₹)<input class="input business-default-cost" type="number" min="0" step="0.01" value="${E(product.unit_cost??'')}" placeholder="e.g. 785"></label>
          <button class="btn-primary business-save-cost" type="button"><i class="fa-solid fa-floppy-disk"></i> Save Cost</button>
          <div class="business-size-report"><strong>Size-wise stock valuation</strong><small>Read-only; purchase rates remain in the stock ledger.</small>${sizeRows}</div>
        </section>
        <section class="business-sku-editor"><div class="business-alias-title"><h4>Linked marketplace SKUs</h4>
          <button type="button" class="btn-primary business-alias-open"><i class="fa-solid fa-plus"></i> Add SKU</button></div>
          <p class="hint">Whole-product SKU works when the marketplace order includes a size. Size-specific SKU matches directly.</p>
          <div class="business-aliases">${aliases.length?aliases.map(a=>`<div class="business-alias"><span><code>${E(a.sku)}</code><small>${E(a.marketplace)} · ${a.size?'Size '+E(a.size):'Whole product'}</small></span><button type="button" class="business-remove-alias" data-alias-type="${a.scope}" data-alias-id="${a.id}" title="Remove mapping" aria-label="Remove ${E(a.sku)}"><i class="fa-solid fa-xmark"></i></button></div>`).join(''):'<p class="hint">No extra SKUs yet.</p>'}</div>
          <form class="business-add-alias-v2" hidden><div class="business-add-alias-fields">
            <label>Marketplace SKU<input class="input" name="sku" required maxlength="180" placeholder="e.g. TWINS_URVISHA_RED"></label>
            <label>Marketplace (optional)<input class="input" name="marketplace" maxlength="80" placeholder="e.g. Twins"></label>
            <label>Applies to<select class="input" name="size"><option value="">Whole product (Size required)</option>${options}</select></label>
          </div><div class="business-inline-actions"><button class="btn-primary" type="submit">Save SKU</button><button type="button" class="btn-secondary business-alias-cancel">Cancel</button></div></form>
        </section>
      </div>
      <p class="business-message" role="status" aria-live="polite"></p>`;
    seatGrid.after(block);
    const feedback = block.querySelector('.business-message');
    const endpoint = `/api/products/${encodeURIComponent(product.sku)}`;
    const refresh = async () => {
      const data = await json(endpoint);
      if (typeof window.renderDetail === 'function') window.renderDetail(data,container,{showDelete:Boolean(container.querySelector('#detail-delete'))});
      else if (typeof window.openProductDetail === 'function') window.openProductDetail(data.sku);
    };
    block.querySelector('.business-save-cost').addEventListener('click',async()=>{
      const button=block.querySelector('.business-save-cost'), input=block.querySelector('.business-default-cost');
      if (input.value==='' || !Number.isFinite(Number(input.value)) || Number(input.value)<0){notice(feedback,'Enter a valid non-negative unit cost.',true);return;}
      button.disabled=true;
      try {await send(`${endpoint}/costing`,'PUT',{unit_cost:input.value===''?null:Number(input.value),size_costs:{}});await refresh();}
      catch(err){notice(feedback,err.message,true);} finally {button.disabled=false;}
    });
    const form=block.querySelector('.business-add-alias-v2');
    block.querySelector('.business-alias-open').addEventListener('click',()=>{form.hidden=false;form.querySelector('input[name=sku]').focus();});
    block.querySelector('.business-alias-cancel').addEventListener('click',()=>{form.hidden=true;form.reset();});
    form.addEventListener('submit',async e=>{
      e.preventDefault();const button=form.querySelector('button[type=submit]');button.disabled=true;
      try{await send(`${endpoint}/marketplace-aliases`,'POST',Object.fromEntries(new FormData(form)));await refresh();}
      catch(err){notice(feedback,err.message,true);}finally{button.disabled=false;}
    });
    block.querySelectorAll('.business-remove-alias').forEach(button=>button.addEventListener('click',async()=>{
      if(!window.confirm('Remove this marketplace SKU link? Past orders will stay unchanged.'))return;
      const prefix=button.dataset.aliasType==='product'?'product-marketplace-aliases':'marketplace-aliases';
      try{await json(`${endpoint}/${prefix}/${button.dataset.aliasId}`,{method:'DELETE'});await refresh();}
      catch(err){notice(feedback,err.message,true);}
    }));
    // The requested compact Add Photos control belongs inside the photo gallery,
    // directly below the thumbnails. Never at the bottom of the Details page.
    const gallery=container.querySelector('.detail-photo-gallery');
    if(gallery){
      const photoBar=document.createElement('label');
      photoBar.className='business-gallery-quick-add';
      photoBar.innerHTML='<i class="fa-solid fa-plus"></i> Add photos <input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden><small class="business-gallery-status" aria-live="polite"></small>';
      gallery.append(photoBar);
      photoBar.querySelector('input').addEventListener('change',async(event)=>{
        const files=Array.from(event.target.files||[]);if(!files.length)return;
        const status=photoBar.querySelector('small');
        if(files.length>8){status.textContent='Maximum 8 photos per upload';return;}
        status.textContent='Uploading…';
        const body=new FormData();files.forEach(file=>body.append('files',file));
        try{await json(`${endpoint}/images`,{method:'POST',body});await refresh();}
        catch(err){status.textContent=err.message;}
        finally{event.target.value='';}
      });
    }
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

  // Supplier challans: invoice image(s) belong to the entire bill, not a line.
  const challanView = $('view-challans');
  if (!challanView) return;
  let selectedId=null, selected=null;
  async function uploadScans(batchId, files) {
    if (!files?.length) return;
    if (files.length>12) throw new Error('Maximum 12 invoice scans per challan');
    const fd=new FormData();Array.from(files).forEach(f=>fd.append('files',f));
    return json(`/api/purchase-challans/${batchId}/attachments`,{method:'POST',body:fd});
  }
  async function loadChallans() {
    const listing=$('challan-batches'),summary=$('challan-summary');
    try {
      const data=await json('/api/purchase-challans');
      summary.innerHTML=`<div><small>Total ordered</small><strong>${Number(data.ordered_pieces).toLocaleString('en-IN')} pcs</strong></div>
        <div><small>Received</small><strong>${Number(data.received_pieces||0).toLocaleString('en-IN')} pcs</strong></div>
        <div><small>Pending</small><strong>${Number(data.pending_pieces).toLocaleString('en-IN')} pcs</strong></div>
        <div><small>Known received goods value</small><strong>${money(data.received_value_known)}</strong>${data.received_unpriced_pieces?`<small>${data.received_unpriced_pieces} received pcs need cost</small>`:''}</div>`;
      listing.innerHTML=data.items.map(b=>`<button type="button" class="challan-batch ${b.id===selectedId?'is-selected':''}" data-id="${b.id}"><span><b>${E(b.party_name)} · ${E(b.challan_no)}</b><small>${E(b.order_date)} · ${E(b.status)} · ${E(b.filename)}</small></span><strong>${b.pending} pending / ${b.ordered}</strong></button>`).join('')||'<p class="hint">No challans yet. Import the 3-column Excel to begin.</p>';
      listing.querySelectorAll('button[data-id]').forEach(button=>button.addEventListener('click',()=>openChallan(Number(button.dataset.id))));
      if(selectedId) await openChallan(selectedId);
    }catch(err){listing.innerHTML=`<p class="business-message is-error">${E(err.message)}</p>`;}
  }
  window.myraaLoadChallans=loadChallans;
  async function openChallan(id){
    try{selected=await json(`/api/purchase-challans/${id}`);selectedId=id;renderChallan();}
    catch(err){window.alert(`Could not load challan: ${err.message}`);}
  }
  function renderChallan(){
    const host=$('challan-detail'),panel=$('challan-detail-panel');
    panel.hidden=false;
    const attachments=(selected.attachments||[]).map(a=>`<div class="challan-scan-card"><a class="challan-scan" target="_blank" rel="noopener" href="${E(a.url)}" title="Open ${E(a.filename)}">
      ${a.content_type.startsWith('image/')?`<img src="${E(a.url)}" loading="lazy" alt="${E(a.filename)}">`:'<i class="fa-solid fa-file-pdf"></i>'}
      <span>${E(a.filename)}</span></a>${selected.status==='POSTED'?'':`<button class="challan-scan-remove" type="button" data-delete-attachment="${a.id}" aria-label="Remove invoice scan ${E(a.filename)}" title="Remove incorrect scan">×</button>`}</div>`).join('');
    host.innerHTML=`
      <header class="challan-detail-head"><div><h2>${E(selected.party_name)} · ${E(selected.challan_no)}</h2>
        <p class="hint">${E(selected.order_date)} · Invoice ${E(selected.invoice_no||'—')} · ${E(selected.status)}</p></div>
        <div><strong>${selected.received}/${selected.ordered} arrived</strong><small>${selected.pending} pending · Received goods ${money(selected.received_value_known)}</small>
          <a class="btn-secondary" href="/api/purchase-challans/${selected.id}/export" download="MYRAA_Challan_${selected.id}.xlsx"><i class="fa-solid fa-file-excel"></i> Download Excel</a></div></header>
      <div class="table-wrap"><table class="grid challan-lines"><thead><tr><th>Product Name / Master SKU</th><th>Size</th><th>Qty Ordered</th><th>PCS Arrived</th><th>Pending</th><th>Unit cost ₹</th><th>Arrived value</th></tr></thead>
        <tbody>${selected.lines.map(line=>`<tr><td><strong>${E(line.name)}</strong><small>${E(line.sku)}</small></td><td><b>${E(line.size)}</b></td><td>${line.qty_ordered}</td>
          <td><input class="input" type="number" min="0" max="${line.qty_ordered}" step="1" data-received="${line.id}" value="${line.qty_received}" ${selected.status==='POSTED'?'disabled':''}></td>
          <td>${line.pending}</td><td><input class="input" type="number" min="0" step="0.01" data-cost="${line.id}" value="${line.unit_cost??''}" placeholder="Rate" ${selected.status==='POSTED'?'disabled':''}></td>
          <td>${line.unit_cost===null?'—':money(line.unit_cost*line.qty_received)}</td></tr>`).join('')}</tbody></table></div>
      <div class="challan-actions"><button id="challan-save-receipt" class="btn-secondary" type="button" ${selected.status==='POSTED'?'disabled':''}><i class="fa-solid fa-floppy-disk"></i> Save Received</button>
        <button id="challan-receive-all" type="button" class="btn-secondary" ${selected.status==='POSTED'?'disabled':''}>Mark all arrived</button>
        <button id="challan-push" class="btn-primary" type="button" ${selected.status!=='READY'?'disabled':''}><i class="fa-solid fa-boxes-stacked"></i> Push to Inventory</button>
        <p class="hint">Stock changes only after all pieces arrived, costs were saved, and Push was confirmed. No duplicate push.</p></div>
      <section class="challan-attachments"><div class="challan-attachments-title"><h3><i class="fa-solid fa-paperclip"></i> Challan / Invoice Images</h3><small>One bill may contain multiple products · up to 12 scans</small></div>
        <div class="challan-scans">${attachments||'<p class="hint">No documents uploaded for this bill yet.</p>'}</div>
        <label class="challan-scan-picker"><i class="fa-solid fa-plus"></i> Add challan image/PDF<input id="challan-extra-images" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" multiple hidden></label></section>
      <p id="challan-detail-msg" class="business-message" role="status" aria-live="polite"></p>`;
    host.querySelector('#challan-receive-all')?.addEventListener('click',()=>host.querySelectorAll('[data-received]').forEach(input=>{input.value=input.max;}));
    const receiptPayload=()=>selected.lines.map(line=>{
      const r=host.querySelector(`[data-received="${line.id}"]`),c=host.querySelector(`[data-cost="${line.id}"]`);
      const n=Number(r.value);
      if(!Number.isInteger(n)||n<0||n>line.qty_ordered)throw new Error(`${line.name}/${line.size}: arrived quantity must be 0 to ${line.qty_ordered}`);
      if(c.value!==''&&(!Number.isFinite(Number(c.value))||Number(c.value)<0))throw new Error('Enter a valid unit cost');
      return {line_id:line.id,received:n,unit_cost:c.value===''?null:Number(c.value)};
    });
    host.querySelector('#challan-save-receipt')?.addEventListener('click',async()=>{
      try{selected=await send(`/api/purchase-challans/${selected.id}/receipt`,'PATCH',{lines:receiptPayload()});renderChallan();await loadChallans();}
      catch(err){notice($('challan-detail-msg'),err.message,true);}
    });
    host.querySelector('#challan-push')?.addEventListener('click',async()=>{
      if(!window.confirm(`Push ${selected.ordered} pieces from ${selected.party_name} · ${selected.challan_no} into inventory? This action cannot be repeated.`))return;
      const btn=$('challan-push');btn.disabled=true;
      try{const response=await send(`/api/purchase-challans/${selected.id}/push`,'POST',{});await loadChallans();window.myraaLoadCosting?.();window.alert(`${response.received_pieces} pieces posted to inventory.`);}
      catch(err){notice($('challan-detail-msg'),err.message,true);btn.disabled=false;}
    });
    host.querySelectorAll('[data-delete-attachment]').forEach(button=>button.addEventListener('click',async()=>{
      if(!window.confirm('Remove this invoice scan? It will no longer be available.'))return;
      try {await json(`/api/purchase-challans/${selected.id}/attachments/${button.dataset.deleteAttachment}`,{method:'DELETE'});
        selected=await json(`/api/purchase-challans/${selected.id}`);renderChallan();}
      catch(err){notice($('challan-detail-msg'),err.message,true);}
    }));
    host.querySelector('#challan-extra-images')?.addEventListener('change',async event=>{
      const files=Array.from(event.target.files||[]);if(!files.length)return;
      try{selected=await uploadScans(selected.id,files);renderChallan();}
      catch(err){notice($('challan-detail-msg'),err.message,true);}
    });
  }
  $('challan-import-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const form=event.target,button=form.querySelector('button[type=submit]'),message=$('challan-import-message');
    button.disabled=true;
    try{
      const uploads=Array.from(form.querySelector('[name=invoice_images]')?.files||[]);
      if(uploads.length>12)throw new Error('Choose at most 12 scans per challan');
      const fd=new FormData(form);fd.delete('invoice_images');
      const batch=await json('/api/purchase-challans/import',{method:'POST',body:fd});
      selectedId=batch.id;
      let warning='';
      if(uploads.length){try{await uploadScans(batch.id,uploads);}catch(err){warning=` Bill imported, but scans were not saved: ${err.message}. Retry inside bill details.`;}}
      form.reset();notice(message,warning||'Challan imported. Save received pieces and unit costs before posting.',!!warning);
      await loadChallans();
    }catch(err){notice(message,err.message,true);}finally{button.disabled=false;}
  });
  const dateInput=$('challan-import-form')?.querySelector('[name=order_date]');
  if(dateInput&&!dateInput.value)dateInput.value=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
  if (challanView.classList.contains('is-active')) loadChallans();
})();
