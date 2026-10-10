(function(){
// Shared header, slide-out menu and notifications for every page.
// A page sets <body data-page="..." data-title="...">.
const B=document.body,page=B.dataset.page||'';
const svg=d=>'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>';
const I={
  menu:svg('<path d="M4 6h16M4 12h16M4 18h16"/>'),
  close:svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  chart:svg('<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>'),
  cal:svg('<path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="5"/>'),
  up:svg('<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 20h16"/>'),
  gear:svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  trash:svg('<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/>')
};
const link=(href,id,icon,text)=>'<a href="'+href+'"'+(page===id?' class="current" aria-current="page"':'')+'>'+icon+text+'</a>';

const hdr=document.createElement('header');
hdr.className='topbar';
hdr.innerHTML='<button class="icon-btn" id="menuBtn" aria-label="Open menu" aria-expanded="false" aria-controls="drawer">'+I.menu+'</button>'+
  '<h1>'+(B.dataset.title||'Pico Voltage Logger')+'</h1>'+
  '<span class="pill" id="statusPill" hidden><span class="dot" id="statusDot"></span><span class="txt" id="statusText"></span></span>';
B.prepend(hdr);

const dr=document.createElement('aside');
dr.className='drawer';dr.id='drawer';dr.setAttribute('aria-label','Menu');
dr.innerHTML='<div class="drawer-head"><span>Pico Voltage Logger</span>'+
  '<button class="icon-btn" id="menuClose" aria-label="Close menu">'+I.close+'</button></div>'+
  '<nav>'+link('/','dashboard',I.chart,'Dashboard')+link('/calibrate','calibrate',I.cal,'Calibrate ADC')+
  link('/update','update',I.up,'Firmware update')+link('/settings','settings',I.gear,'Settings')+'<hr>'+
  '<button class="item danger" id="clearBtn">'+I.trash+'Clear stored data</button></nav>'+
  '<div class="foot">24-hour voltage logger</div>';
const scrim=document.createElement('div');scrim.className='scrim';
const toastEl=document.createElement('div');toastEl.className='toast';toastEl.setAttribute('role','status');
B.append(dr,scrim,toastEl);

const btn=document.getElementById('menuBtn');
function setOpen(o){
  dr.classList.toggle('open',o);scrim.classList.toggle('open',o);
  btn.setAttribute('aria-expanded',o?'true':'false');
  if(o)(dr.querySelector('a,button.item')||dr).focus();else btn.focus();
}
btn.onclick=()=>setOpen(true);
document.getElementById('menuClose').onclick=()=>setOpen(false);
scrim.onclick=()=>setOpen(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dr.classList.contains('open'))setOpen(false);});

let toastTimer=0;
function toast(msg){
  toastEl.textContent=msg;toastEl.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>toastEl.classList.remove('show'),3500);
}

document.getElementById('clearBtn').onclick=async()=>{
  setOpen(false);
  if(!confirm('Clear all stored voltage history? This cannot be undone.'))return;
  try{
    const r=await fetch('/api/clear',{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('Stored data cleared');
    document.dispatchEvent(new Event('datacleared'));
  }catch(e){toast('Clear failed: '+e.message);}
};

window.ui={
  toast:toast,
  setStatus:function(text,kind){
    document.getElementById('statusPill').hidden=false;
    document.getElementById('statusText').textContent=text;
    document.getElementById('statusDot').className='dot '+(kind||'');
  }
};
})();
