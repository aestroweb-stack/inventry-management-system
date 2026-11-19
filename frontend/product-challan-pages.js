/* MYRAA v3 — Product and supplier challans are separate workspace screens.
   Pure presentation/navigation: existing create/edit/photo/alias/challan handlers
   continue to own validation, API calls and inventory changes. */
(() => {
  'use strict';
  const find = (s, parent = document) => parent.querySelector(s);
  const editor = document.getElementById('add-product-modal');
  const form = document.getElementById('add-product-form');
  const stage = document.getElementById('stage');
  if (!editor || !form || !stage) return;

  editor.classList.add('myraa-product-page');
  editor.setAttribute('role', 'dialog');
  editor.setAttribute('aria-modal', 'true');
  editor.setAttribute('aria-labelledby', 'myraa-product-page-title');
  const heading = find('.modal-head h2', editor);
  if (heading) {
    heading.id = 'myraa-product-page-title';
    heading.innerHTML = '<i class="fa-solid fa-cube" aria-hidden="true"></i> Add Product';
  }
  const header = find('.modal-head', editor);
  if (header && !find('.myraa-editor-headline', header)) {
    const subtitle = document.createElement('p');
    subtitle.className = 'myraa-editor-headline';
    subtitle.textContent = 'Create one master design, add stock sizes, photos and linked marketplace SKUs.';
    heading?.after(subtitle);
  }
  const photo = find('.photo-pick', form);
  const fields = find('.product-fields', form);
  if (photo && fields) {
    const angleCards = find('.product-photo-angles', fields);
    const angleHint = find('.product-photo-hint', fields);
    if (angleCards) photo.append(angleCards);
    if (angleHint) photo.append(angleHint);
    photo.classList.add('myraa-editor-gallery');

    const sizeTitle = find('h3.sub-head', form);
    const sizeRows = document.getElementById('size-rows');
    const addSizeButton = document.getElementById('add-size-row');
    if (sizeTitle && sizeRows && addSizeButton) {
      const section = document.createElement('section');
      section.className = 'myraa-editor-sizes';
      const headerRow = document.createElement('div');
      headerRow.className = 'myraa-editor-section-head';
      sizeTitle.innerHTML = '<i class="fa-solid fa-layer-group" aria-hidden="true"></i> Sizes &amp; starting stock';
      headerRow.append(sizeTitle, addSizeButton);
      section.append(headerRow, sizeRows);
      fields.append(section);
    }

    const aliases = document.getElementById('product-sku-builder');
    const actions = find('.modal-actions', form);
    if (aliases && actions) {
      aliases.classList.add('myraa-editor-aliases');
      form.insertBefore(aliases, actions);
    }
  }

  // Sidebar remains usable while editor is open. Navigating away closes it.
  // No new save handlers or API calls are introduced by this view switch.
  document.querySelectorAll('.rail-link[data-view]').forEach(button => {
    button.addEventListener('click', () => { if (!editor.hidden) editor.hidden = true; });
  });
  document.getElementById('detail-back')?.addEventListener('click', () => {
    if (!editor.hidden) editor.hidden = true;
  });
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !editor.hidden) {
      editor.hidden = true;
      document.getElementById('open-add-product')?.focus({preventScroll:true});
    }
  });
  // For accessibility: indicate which dedicated screen is visible.
  const detail = document.getElementById('view-detail');
  detail?.setAttribute('aria-label', 'Edit product and its inventory');
  document.getElementById('view-challans')?.setAttribute('aria-label', 'Purchase challans and inbound stock');
})();
