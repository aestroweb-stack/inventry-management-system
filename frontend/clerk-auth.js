/* MYRAA Clerk auth: Google/Email-code identity + MYRAA local approval.
   No passwords, OAuth secrets, or long-lived session JWTs are stored in JS. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const loginPage = document.body.dataset.myraaClerk === 'login';
  const home = '/app/';
  const loginPageUrl = '/app/sign-in.html';
  const statusLine = (message) => {
    const el = $('clerk-auth-error') || $('myraa-login-error');
    if (el) el.textContent = message || '';
  };
  const request = async (url, method='GET', body) => {
    const opt = {method, credentials:'same-origin', headers: {}};
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const resp = await fetch(url,opt);
    const data = await resp.json().catch(()=>({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    return data;
  };
  const script = (src, attrs={}) => new Promise((resolve,reject) => {
    const item=document.createElement('script'); item.src=src; item.async=true; item.crossOrigin='anonymous';
    for (const [key,value] of Object.entries(attrs)) item.setAttribute(key,value);
    item.onload=resolve; item.onerror=()=>reject(new Error('Unable to load Clerk authentication. Check network and Clerk settings.'));
    document.head.appendChild(item);
  });
  const clerkConfig = async()=>{
    const resp=await request('/api/clerk/config');
    if (!resp.configured || !resp.publishableKey) throw new Error('Clerk is not configured in Railway Variables.');
    return resp.publishableKey;
  };
  let clerk, busy=false, account=null;
  const publicRoot = () => String(location.origin);
  const fetchStatus=()=>request('/api/workspace-auth/status');
  async function startClerk() {
    const publishableKey = await clerkConfig();
    const parts = publishableKey.split('_');
    if(parts.length < 3) throw new Error('Invalid Clerk publishable key');
    const frontendDomain = atob(parts.slice(2).join('_')).replace(/\$$/,'').replace(/\0+$/,'');
    if(!/^[a-z0-9.-]+$/i.test(frontendDomain)) throw new Error('Invalid Clerk frontend domain');
    // Official ClerkJS browser bundles; the publishable key is intentionally public.
    await script(`https://${frontendDomain}/npm/@clerk/ui@1/dist/ui.browser.js`);
    await script(`https://${frontendDomain}/npm/@clerk/clerk-js@6/dist/clerk.browser.js`, {
      'data-clerk-publishable-key':publishableKey,
    });
    clerk = window.Clerk;
    if(!clerk) throw new Error('Clerk SDK did not initialize');
    await clerk.load({ui:{ClerkUI:window.__internal_ClerkUICtor}});
    return clerk;
  }
  const hasSession = () => !!(clerk && clerk.session && clerk.user);
  async function removeBackendSession() {
    try { await request('/api/clerk/logout','POST'); } catch {} // Only clears browser cookies.
  }
  async function exchange() {
    if(busy || !hasSession()) return;
    busy=true;
    try {
      // Clerk tokens are short lived. On 401, request one fresh token from
      // Clerk and retry exactly once. Never skip backend JWT verification.
      const submit = async (forceRefresh) => {
        const token = await clerk.session.getToken(forceRefresh ? {skipCache:true} : undefined);
        if (!token) throw new Error('Clerk session token not available. Sign in again.');
        const result = await fetch('/api/clerk/exchange', {method:'POST',credentials:'same-origin',
          headers:{'Authorization':`Bearer ${token}`,'Accept':'application/json'}});
        const payload = await result.json().catch(()=>({}));
        return {result,payload};
      };
      let {result,payload} = await submit(false);
      if (result.status === 401 && hasSession()) {
        ({result,payload} = await submit(true));
      }
      if(!result.ok) throw new Error(payload.detail || `HTTP ${result.status}`);
      account=payload.approved ? payload.user : null;
      if(payload.approved) {
        if(loginPage) location.replace(home);
        else { refreshAppProfile(account); $('myraa-login')?.setAttribute('hidden',''); }
      } else if(loginPage) {
        $('clerk-approval')?.removeAttribute('hidden');
        $('clerk-auth-root')?.setAttribute('hidden','');
        $('clerk-tab-login')?.setAttribute('hidden','');
        $('clerk-tab-signup')?.setAttribute('hidden','');
        statusLine('');
      } else {
        location.replace(loginPageUrl);
      }
    } finally { busy=false; }
  }
  function refreshAppProfile(user) {
    if (!user) return;
    const avatar=$('workspace-user-profile');
    if(avatar) {avatar.textContent=(user.display_name || 'M').charAt(0).toUpperCase();avatar.title=`${user.display_name} · ${user.role}`;}
    if($('logout-button')) $('logout-button').hidden=false;
    if($('workspace-team-link')) $('workspace-team-link').hidden=user.role!=='admin';
    const crumb=document.querySelector('.workspace-breadcrumb');
    if(crumb && !crumb.querySelector('.workspace-user-note')) {
      const tag=document.createElement('div'); tag.className='workspace-user-note';
      tag.textContent=`${user.display_name} · ${user.role}`; crumb.append(tag);
    }
  }
  async function signOut() {
    await removeBackendSession();
    if(clerk) await clerk.signOut();
    for(const key of ['inventoryAssistantHistory.v1','inventoryAssistantSessions.v1','inventoryAssistantSessionId.v1'])
      try { localStorage.removeItem(key); } catch {}
    location.replace(loginPageUrl);
  }
  async function onClerkChanged() {
    if (!hasSession()) {
      if (loginPage) return;
      await removeBackendSession();
      location.replace(loginPageUrl);
      return;
    }
    if(loginPage) await exchange();
  }
  async function setLoginView(view) {
    if(!clerk || hasSession()) return;
    const root=$('clerk-auth-root');
    if (!root) return;
    try {clerk.unmountSignIn(root);} catch {}
    try {clerk.unmountSignUp(root);} catch {}
    root.replaceChildren();
    $('clerk-tab-login')?.classList.toggle('active',view==='signin');
    $('clerk-tab-signup')?.classList.toggle('active',view==='signup');
    const opts={routing:'hash',signInForceRedirectUrl:loginPageUrl,signUpForceRedirectUrl:loginPageUrl};
    if (view==='signup') clerk.mountSignUp(root,opts);
    else clerk.mountSignIn(root,opts);
  }
  async function initLoginPage() {
    $('clerk-tab-login')?.addEventListener('click',()=>setLoginView('signin').catch(e=>statusLine(e.message)));
    $('clerk-tab-signup')?.addEventListener('click',()=>setLoginView('signup').catch(e=>statusLine(e.message)));
    $('clerk-check-access')?.addEventListener('click',()=>exchange().catch(e=>statusLine(e.message)));
    $('clerk-switch-user')?.addEventListener('click',()=>signOut().catch(e=>statusLine(e.message)));
    await startClerk();
    clerk.addListener(()=>onClerkChanged().catch(e=>statusLine(e.message)));
    if (hasSession()) await exchange();
    else await setLoginView('signin');
  }
  async function initAppPage() {
    // No unverified cookie can authenticate the backend. Clerk auth must exist.
    await startClerk();
    if(!hasSession()){await removeBackendSession(); location.replace(loginPageUrl);return;}
    // Always bind the MYRAA session to the CURRENT Clerk identity. An old
    // approved account cookie must never survive switching Clerk accounts.
    await exchange();
    // When backend cookie expires, refresh only while Clerk remains signed in.
    setInterval(()=>{if(hasSession()) exchange().catch(()=>{});}, 7 * 60 * 1000);
    clerk.addListener(({user,session})=>{if(user === null && session === null){
      removeBackendSession().finally(()=>location.replace(loginPageUrl));
    }});
    installProfileAndTeam();
  }
  function installProfileAndTeam() {
    $('logout-button')?.addEventListener('click',e=>{e.preventDefault();signOut().catch(err=>statusLine(err.message));});
    $('workspace-profile-logout')?.addEventListener('click',()=>signOut().catch(err=>alert(err.message)));
    $('workspace-user-profile')?.addEventListener('click',async()=>{
      try {const profile=await request('/api/workspace-auth/me');showProfile(profile);}catch(e){alert(e.message);}
    });
    $('workspace-profile-close')?.addEventListener('click',()=>{$('workspace-profile-overlay').hidden=true;});
    $('workspace-profile-overlay')?.addEventListener('click',e=>{if(e.target.id==='workspace-profile-overlay') e.currentTarget.hidden=true;});
    $('workspace-profile-form')?.addEventListener('submit',async e=>{
      e.preventDefault();
      const el=$('workspace-profile-message');if(el)el.textContent='Saving profile…';
      try {
        const profile=await request('/api/workspace-auth/me','PATCH',{
          display_name:$('workspace-profile-name').value,business_name:$('workspace-profile-business').value,
          phone:$('workspace-profile-phone').value,address:$('workspace-profile-address').value});
        showProfile(profile);refreshAppProfile(profile);if(el)el.textContent='Profile saved.';
      } catch(err){if(el)el.textContent=err.message;}
    });
    $('workspace-profile-image')?.addEventListener('change',async e=>{
      const file=e.target.files?.[0];if(!file)return;
      const el=$('workspace-profile-message');if(el)el.textContent='Uploading photo…';
      try {const body=new FormData();body.append('file',file);
        const r=await fetch('/api/workspace-auth/me/photo',{method:'POST',body,credentials:'same-origin'});
        const result=await r.json().catch(()=>({}));if(!r.ok)throw new Error(result.detail||`HTTP ${r.status}`);
        showProfile(await request('/api/workspace-auth/me'));if(el)el.textContent='Photo uploaded.';
      }catch(err){if(el)el.textContent=err.message;}finally{e.target.value='';}
    });
    $('workspace-team-link')?.addEventListener('click',loadUsers);
    let timer;
    $('workspace-team-search')?.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(loadUsers,300);});
  }
  function showProfile(user) {
    for(const [id,value] of Object.entries({
      'workspace-profile-login':user.login_id,'workspace-profile-email':user.email,'workspace-profile-name':user.display_name,
      'workspace-profile-business':user.business_name,'workspace-profile-phone':user.phone,'workspace-profile-address':user.address})) {
      const element=$(id);if(element) element.value=value||'';
    }
    if($('workspace-profile-role')) $('workspace-profile-role').textContent=`Role: ${user.role} · ${user.approved?'Approved':'Pending'}`;
    const photo=$('workspace-profile-preview');if(photo){photo.hidden=!user.photo_url;if(user.photo_url)photo.src=user.photo_url+'?v='+Date.now();}
    const overlay=$('workspace-profile-overlay');if(overlay)overlay.hidden=false;
  }
  async function loadUsers() {
    if(account?.role!=='admin')return;
    const target=$('workspace-team-list');if(!target)return;
    target.textContent='Loading accounts…';
    try {
      const q=$('workspace-team-search')?.value.trim()||'';
      const response=await request('/api/workspace-admin/users'+(q?'?q='+encodeURIComponent(q):''));
      target.replaceChildren();
      if(!response.items?.length){target.textContent='No accounts found';return;}
      for(const user of response.items) {
        const panel=document.createElement('article');panel.className='workspace-team-card';
        const description=document.createElement('div');description.append(Object.assign(document.createElement('strong'),{
          textContent:`${user.display_name} · ${user.login_id}`}));
        const small=document.createElement('small');small.textContent=`${user.email} · ${user.business_name||'No business'} · ${user.verified?'Verified':'Unverified'}`;
        description.append(small);
        if(user.role==='admin'){panel.append(description);target.append(panel);continue;}
        const actions=document.createElement('div');actions.className='workspace-team-controls';
        const role=document.createElement('select');
        for(const [value,label] of [['viewer','Read-only'],['manager','Full workspace']]){
          const option=document.createElement('option');option.value=value;option.textContent=label;role.append(option);
        } role.value=user.role;
        const approval=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=!!user.approved;checkbox.disabled=!user.verified;
        approval.append(checkbox,document.createTextNode(' Approved'));
        const active=document.createElement('label'),activeCheck=document.createElement('input');activeCheck.type='checkbox';activeCheck.checked=!!user.active;
        active.append(activeCheck,document.createTextNode(' Active'));
        const save=document.createElement('button');save.type='button';save.textContent='Save access';save.className='btn-primary';
        save.addEventListener('click',async()=>{
          if(!confirm(`Update access for ${user.display_name}? Managers can change inventory.`))return;
          save.disabled=true;
          try {await request('/api/workspace-admin/users/'+user.id,'PATCH',{approved:checkbox.checked,active:activeCheck.checked,role:role.value});await loadUsers();}
          catch(e){alert(e.message);save.disabled=false;}
        });
        actions.append(role,approval,active,save);panel.append(description,actions);target.append(panel);
      }
    }catch(e){target.textContent=e.message;}
  }
  const startup=loginPage?initLoginPage:initAppPage;
  startup().catch(error=>{
    statusLine(error.message||'Secure login unavailable');
    if(!loginPage){$('myraa-login')?.removeAttribute('hidden');}
    else {const root=$('clerk-auth-root');if(root)root.textContent='Unable to initialize Clerk. Check the settings in Railway.';}
  });
})();
