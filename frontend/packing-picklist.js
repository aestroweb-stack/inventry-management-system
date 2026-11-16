/* MYRAA Packing Picklist: independent of order import / dispatch and stock CRUD. */
(() => {
  'use strict';
  const byId = (s) => document.getElementById(s);
  const input = byId('packing-file');
  const previewButton = byId('packing-preview');
  const exportButton = byId('packing-export');
  const fileLabel = byId('packing-filename');
  const status = byId('packing-status');
  if (!input || !previewButton || !exportButton || !status) return;
  const h = (s) => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  let busy = false;
  let previewed = false;
  const setBusy = (on) => {
    busy = on;
    previewButton.disabled = on || !input.files.length;
    exportButton.disabled = on || !input.files.length || !previewed;
  };
  const form = () => { const f = new FormData(); f.append('file', input.files[0]); return f; };
  const getError = async (r) => {
    try { const j = await r.json(); return j.detail || `HTTP ${r.status}`; }
    catch { return `HTTP ${r.status}`; }
  };
  const preview = async () => {
    if (busy || !input.files.length) return;
    previewed = false;
    setBusy(true);
    status.textContent = 'Checking SKU, size, party and quantity totals…';
    try {
      const response = await fetch('/api/packing-picklist/preview', {method: 'POST', body: form()});
      if (!response.ok) throw new Error(await getError(response));
      const data = await response.json();
      if (!data.quantity_reconciled) throw new Error('Quantity reconciliation did not pass');
      byId('packing-source-count').textContent = Number(data.source_rows).toLocaleString();
      byId('packing-total').textContent = Number(data.total_qty).toLocaleString();
      byId('packing-merged').textContent = Number(data.merged_rows).toLocaleString();
      byId('packing-review-count').textContent = Number(data.review_count).toLocaleString();
      byId('packing-party-list').innerHTML = '<h3>Party breakdown</h3>' + Object.entries(data.party_totals || {})
        .map(([party, qty]) => `<div class="packing-party-line"><span>${h(party)}</span><strong>${Number(qty).toLocaleString()} pcs</strong></div>`).join('');
      byId('packing-sample').innerHTML = (data.sample_items || []).map(r => `<tr><td>${h(r.party)}</td><td class="sku-tag">${h(r.pick_sku)}</td><td>${h(r.brands)}</td><td class="num"><strong>${Number(r.qty).toLocaleString()}</strong></td></tr>`).join('') || '<tr><td colspan="4">No rows</td></tr>';
      previewed = true;
      status.textContent = `Verified ${Number(data.total_qty).toLocaleString()} pieces. ${data.review_count ? `${data.review_count} rows are flagged/inferred; check Review tab after download.` : 'No rows need review.'}`;
    } catch (error) {
      status.textContent = 'Preview failed: ' + String(error.message || error);
      status.classList.add('packing-error');
    } finally { setBusy(false); }
  };
  input.addEventListener('change', () => {
    previewed = false;
    fileLabel.textContent = input.files[0]?.name || '.xlsx, .xls, .csv · Master SKU and Qty';
    status.classList.remove('packing-error');
    status.textContent = input.files.length ? 'File selected. Click Check & Preview.' : 'Upload a file to begin.';
    setBusy(false);
  });
  previewButton.addEventListener('click', () => { status.classList.remove('packing-error'); preview(); });
  exportButton.addEventListener('click', async () => {
    if (busy || !previewed || !input.files.length) return;
    setBusy(true);
    status.textContent = 'Generating verified party-wise and brand-wise Excel…';
    try {
      const withBrands = byId('packing-brand-pages').checked ? 'true' : 'false';
      const r = await fetch(`/api/packing-picklist/export?include_brand_pages=${withBrands}`, {method:'POST', body:form()});
      if (!r.ok) throw new Error(await getError(r));
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `MYRAA_Packing_Picklist_${new Date().toISOString().slice(0,10)}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      status.textContent = 'Excel generated. Summary, party sheets, brand sheets, Review and Source Audit included.';
    } catch (error) {
      status.textContent = 'Download failed: ' + String(error.message || error);
      status.classList.add('packing-error');
    } finally { setBusy(false); }
  });
})();
