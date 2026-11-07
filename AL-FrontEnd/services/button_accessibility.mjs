// Uni-app H5 renders custom elements, so supply native-button semantics once.
const disabled=element=>element.hasAttribute('disabled')&&element.getAttribute('disabled')!=='false';
export function syncButton(element){
  if(element.tagName!=='UNI-BUTTON'&&!element.classList?.contains('uni-modal__btn'))return;
  const blocked=disabled(element);
  element.setAttribute('role','button');element.setAttribute('tabindex',blocked?'-1':'0');element.setAttribute('aria-disabled',String(blocked));
}
export function handleButtonKey(event){
  if(event.defaultPrevented||!['Enter',' '].includes(event.key))return;
  const button=event.target?.closest?.('uni-button, .uni-modal__btn');if(!button)return;
  event.preventDefault();if(disabled(button)||event.repeat)return;
  if(event.key==='Enter'&&event.type==='keydown'||event.key===' '&&event.type==='keyup')button.click();
}
export function installButtonAccessibility(doc){
  let priorFocus=null,activeModal=null;
  const sync=root=>{
    syncButton(root);
    root?.querySelectorAll?.('uni-button, .uni-modal__btn').forEach(syncButton);
    const wrappers=[...(root?.querySelectorAll?.('uni-input')||[])];if(root?.tagName==='UNI-INPUT')wrappers.push(root);
    wrappers.forEach(wrapper=>{
      const input=wrapper.querySelector('input'),label=wrapper.getAttribute('aria-label')||wrapper.closest('.field')?.querySelector('label,uni-label')?.textContent||(wrapper.id?doc.querySelector('label[for="'+wrapper.id+'"]')?.textContent:'');
      if(input&&label)input.setAttribute('aria-label',label.trim());
    });
    const modal=[...doc.querySelectorAll('.uni-modal')].find(el=>el.getClientRects().length);
    if(modal&&modal!==activeModal){
      priorFocus=doc.activeElement;activeModal=modal;modal.setAttribute('role','alertdialog');modal.setAttribute('aria-modal','true');modal.setAttribute('aria-label',modal.querySelector('.uni-modal__title')?.textContent||'请确认');
      modal.querySelectorAll('.uni-modal__btn').forEach(syncButton);doc.defaultView.requestAnimationFrame(()=>{if(activeModal===modal)modal.querySelector('.uni-modal__btn_default, .uni-modal__btn')?.focus();});
    }else if(!modal&&activeModal){activeModal=null;if(priorFocus?.isConnected)priorFocus.focus();priorFocus=null;}
  };
  function modalKeys(event){
    if(!activeModal||!activeModal.getClientRects().length)return;
    if(['Enter',' '].includes(event.key)){event.preventDefault();event.stopImmediatePropagation();const target=event.target?.closest?.('.uni-modal__btn');if(target&&!event.repeat&&(event.key==='Enter'&&event.type==='keydown'||event.key===' '&&event.type==='keyup'))target.click();return;}
    if(event.type!=='keydown')return;
    const buttons=[...activeModal.querySelectorAll('.uni-modal__btn')];
    if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();activeModal.querySelector('.uni-modal__btn_default')?.click();}
    if(event.key==='Tab'){
      const first=buttons[0],last=buttons[buttons.length-1];
      if(event.shiftKey&&(!activeModal.contains(doc.activeElement)||doc.activeElement===first)){event.preventDefault();last?.focus();}
      else if(!event.shiftKey&&(!activeModal.contains(doc.activeElement)||doc.activeElement===last)){event.preventDefault();first?.focus();}
    }
  }
  sync(doc);
  const Observer=doc.defaultView.MutationObserver;
  const observer=new Observer(records=>{
    records.filter(record=>record.type==='attributes').forEach(record=>syncButton(record.target));
    if(records.some(record=>record.type==='childList'||record.type==='attributes'&&record.target.tagName==='UNI-MODAL'))sync(doc);
  });
  observer.observe(doc.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','style']});
  doc.addEventListener('keydown',modalKeys,true);doc.addEventListener('keyup',modalKeys,true);doc.addEventListener('keydown',handleButtonKey);doc.addEventListener('keyup',handleButtonKey);
  return ()=>{observer.disconnect();doc.removeEventListener('keydown',modalKeys,true);doc.removeEventListener('keyup',modalKeys,true);doc.removeEventListener('keydown',handleButtonKey);doc.removeEventListener('keyup',handleButtonKey);};
}
