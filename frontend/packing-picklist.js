/* MYRAA Smart Packing Picklist v3: read-only; full paginated preview. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const input = $('packing-file'), previewBtn = $('packing-preview');
  const exportBtn = $('packing-export'), status = $('packing-status');
  const date = $('packing-date');
  const audit = $('packing-audit'), pager = $('packing-pagination');
  if (!input || !previewBtn || !exportBtn || !status) return;
  const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[x]));
  let busy = false, previewed = false, currentPage = 1, pageCount = 1;
  const localToday = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  };
  date.value = localToday();
  const setBusy = (on) => {
    busy = on;
    previewBtn.disabled = on || !input.files.length;
    exportBtn.disabled = on || !input.files.length || !previewed;
    $('packing-prev').disabled = on || currentPage <= 1;
    $('packing-next').disabled = on || currentPage >= pageCount;
  };
  const form = () => {
    const f = new FormData();
    f.append('file', input.files[0]);
    f.append('brand_override', 'AUTO');
    f.append('picklist_date', date.value);
    f.append('include_audit', audit.checked ? 'true' : 'false');
    return f;
  };
  const getError = async (r) => {
    try { const j = await r.json(); return j.detail || `HTTP ${r.status}`; }
    catch { return `HTTP ${r.status}`; }
  };
  async function preview(page=1) {
    if (busy || !input.files.length) return;
    const isFirst = page === 1;
    if (isFirst) previewed = false;
    setBusy(true);
    status.classList.remove('packing-error');
    status.textContent = 'Matching designs, colors, sizes and verifying quantities…';
    try {
      const response = await fetch(`/api/packing-picklist/preview?page=${page}&page_size=25`, {method:'POST', body:form()});
      if (!response.ok) throw new Error(await getError(response));
      const data = await response.json();
      if (!data.quantity_reconciled) throw new Error('Quantity reconciliation failed');
      $('packing-source-count').textContent = Number(data.source_rows).toLocaleString();
      $('packing-total').textContent = Number(data.total_qty).toLocaleString();
      $('packing-merged').textContent = Number(data.merged_rows).toLocaleString();
      $('packing-review-count').textContent = Number(data.review_count).toLocaleString();
      const order = ['NO PARTY','V','NK','O','LP','NEEL'];
      const partyRows = Object.entries(data.party_totals || {}).sort((a,b)=>{
        const x = order.indexOf(a[0]), y = order.indexOf(b[0]);
        return (x === -1 ? 99 : x) - (y === -1 ? 99 : y) || a[0].localeCompare(b[0]);
      });
      $('packing-party-list').innerHTML = '<h3>Party breakdown</h3>' + partyRows
        .map(([p,n])=>`<div class="packing-party-line"><span>${escapeHtml(p)}</span><strong>${Number(n).toLocaleString()} pcs</strong></div>`).join('');
      $('packing-sample').innerHTML = (data.sample_items || []).map(r=>`<tr><td>${escapeHtml(r.brand)}</td><td>${escapeHtml(r.party)}</td><td class="sku-tag">${escapeHtml(r.pick_sku)}</td><td class="num"><strong>${Number(r.qty).toLocaleString()}</strong></td></tr>`).join('') || '<tr><td colspan="4">No matching rows</td></tr>';
      currentPage = data.preview_page;
      pageCount = data.preview_total_pages;
      pager.hidden = pageCount <= 1;
      $('packing-page-info').textContent = `Page ${currentPage} of ${pageCount}`;
      $('packing-preview-total').textContent = `${data.preview_total_rows} merged rows · 25 per page`;
      previewed = true;
      const mapped=Number(data.master_sku_mapped_qty||0);
      const unmapped=Number(data.total_qty||0)-mapped;
      status.textContent = `Verified ${Number(data.total_qty).toLocaleString()} pieces · ${data.picklist_date}. ${mapped.toLocaleString()} pcs mapped to registered Master SKUs; ${unmapped.toLocaleString()} pcs kept in existing picklist format. ${data.review_count ? `${data.review_count} rows need review. Check optional Audit tabs.` : 'No review warnings.'}`;
    } catch(error) {
      status.textContent = 'Preview failed: ' + String(error.message || error);
      status.classList.add('packing-error');
    } finally { setBusy(false); }
  }
  const reset = () => {
    previewed = false; currentPage=1; pageCount=1;
    pager.hidden=true;
    status.classList.remove('packing-error');
    status.textContent = input.files.length ? 'Click Check & Preview.' : 'Upload a file to begin.';
    setBusy(false);
  };
  input.addEventListener('change', () => {
    $('packing-filename').textContent = input.files[0]?.name || '.xlsx, .xls, .csv · Master SKU and Qty';
    const found = input.files[0]?.name.match(/(20\d{2})-(\d{2})-(\d{2})/);
    date.value = found ? `${found[1]}-${found[2]}-${found[3]}` : localToday();
    reset();
  });
  date.addEventListener('change', reset);
  previewBtn.addEventListener('click', () => preview(1));
  $('packing-prev').addEventListener('click', () => preview(Math.max(1,currentPage-1)));
  $('packing-next').addEventListener('click', () => preview(Math.min(pageCount,currentPage+1)));
  exportBtn.addEventListener('click', async () => {
    if (busy || !previewed || !input.files.length) return;
    setBusy(true); status.classList.remove('packing-error');
    status.textContent = 'Generating dynamic brand/party-wise printable Excel…';
    try {
      const response = await fetch('/api/packing-picklist/export',{method:'POST',body:form()});
      if (!response.ok) throw new Error(await getError(response));
      const blob = await response.blob();
      const url=URL.createObjectURL(blob);
      const a=document.createElement('a');
      a.href=url;
      a.download = `MYRAA_Packing_${date.value}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(url),30000);
      status.textContent = 'Downloaded: seller/brand-wise pages, each with dynamic SKU-Qty blocks. No pieces dropped.';
    } catch(error) {
      status.textContent = 'Download failed: ' + String(error.message || error);
      status.classList.add('packing-error');
    } finally { setBusy(false); }
  });
})();
