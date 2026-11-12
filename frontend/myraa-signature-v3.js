/* MYRAA Signature v3: UI-only helpers. Existing product, stock, order and AI
   handlers remain the sole source of business actions. No duplicate API calls. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  // One read-only summary for the status donut; uses actual variant counts,
  // never invented demo values. All stock writes still use the original app.
  async function loadStockHealth() {
    const chart = $('stock-health-chart');
    const legend = $('stock-health-legend');
    if (!chart || !legend) return;
    try {
      const response = await fetch('/api/summary');
      if (!response.ok) return;
      const summary = await response.json();
      const values = [
        { key:'ok', title:'In stock', value:Number(summary.ok)||0, color:'#10a77b' },
        { key:'low', title:'Low stock', value:Number(summary.low)||0, color:'#f2b64d' },
        { key:'out', title:'Out of stock', value:Number(summary.out)||0, color:'#ef7471' },
        { key:'uncounted', title:'Not counted', value:Number(summary.uncounted)||0, color:'#91aaa0' },
      ];
      const total = values.reduce((sum,item)=>sum+Math.max(0,item.value),0);
      $('stock-health-total').textContent = total.toLocaleString();
      chart.setAttribute('aria-label', values.map((item)=>`${item.title}: ${item.value}`).join(', '));
      let percentage = 0;
      const stops=[];
      for(const item of values) {
        const next = total > 0 ? percentage+(Math.max(0,item.value)/total)*100 : percentage;
        stops.push(`${item.color} ${percentage.toFixed(2)}% ${next.toFixed(2)}%`);
        percentage = next;
      }
      chart.style.background = total ? `conic-gradient(${stops.join(', ')})` : 'conic-gradient(#d6e9de 0% 100%)';
      legend.replaceChildren();
      values.forEach(item=>{
        const row=document.createElement('div');
        row.className='stock-health-item';
        const name=document.createElement('span');
        name.textContent=item.title;
        name.style.setProperty('--stock-dot',item.color);
        const number=document.createElement('strong');
        number.textContent=item.value.toLocaleString();
        row.append(name,number);legend.appendChild(row);
      });
    } catch { /* Dashboard still works when stock summary is unavailable. */ }
  }
  loadStockHealth();
  document.querySelector('.rail-link[data-view="dashboard"]')?.addEventListener('click',loadStockHealth);
  // Global search delegates to the existing dashboard search and its event handler.
  const globalSearch = $('workspace-global-search');
  const dashboardSearch = $('search-box');
  if (globalSearch && dashboardSearch) {
    let timer;
    globalSearch.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const dashboardNav = document.querySelector('.rail-link[data-view="dashboard"]');
        if (!document.querySelector('#view-dashboard.is-active')) dashboardNav?.click();
        dashboardSearch.value = globalSearch.value;
        dashboardSearch.dispatchEvent(new Event('input', { bubbles: true }));
      }, 230);
    });
    dashboardSearch.addEventListener('input', () => {
      if (document.activeElement !== globalSearch) globalSearch.value = dashboardSearch.value;
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === '/' && !event.ctrlKey && !event.altKey && !event.metaKey
          && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) {
        event.preventDefault();
        globalSearch.focus();
      }
    });
  }
  // Quick action buttons open the existing views; Add Product uses the existing button.
  document.querySelectorAll('[data-myraa-nav]').forEach((button) => {
    button.addEventListener('click', () => {
      const view = button.getAttribute('data-myraa-nav');
      document.querySelector(`.rail-link[data-view="${view}"]`)?.click();
      const trigger = button.getAttribute('data-myraa-trigger');
      if (trigger) window.requestAnimationFrame(() => $(trigger)?.click());
    });
  });
  document.querySelectorAll('[data-myraa-open]').forEach((button) => {
    button.addEventListener('click', () => {
      // Reuse the existing launcher/handler so readiness, history and voice stay intact.
      const panel = $('assistant-panel');
      if (panel?.hidden) $('assistant-launcher')?.click();
      $('assistant-chat-input')?.focus({ preventScroll:true });
    });
  });
  // Suggestions set the normal input and submit the existing form (with its safety preview).
  document.querySelectorAll('[data-myraa-prompt]').forEach((button) => {
    button.addEventListener('click', () => {
      const message = button.getAttribute('data-myraa-prompt');
      const input = $('assistant-chat-input');
      const form = $('assistant-chat-form');
      if (!input || !form || !message) return;
      if ($('assistant-panel')?.hidden) $('assistant-launcher')?.click();
      input.value = message;
      if (form.requestSubmit) form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { bubbles:true, cancelable:true }));
    });
  });
})();
