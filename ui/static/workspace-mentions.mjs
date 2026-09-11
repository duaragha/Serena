/** Names-only completion; selection edits a draft and never submits a turn. */
let nextListId=0;
export function installFileMentions({input, form, search, enabled, persist}) {
  const lifetime=new AbortController();
  const panel=document.createElement('div');panel.className='aw-mentions';panel.hidden=true;
  const status=document.createElement('div');status.setAttribute('role','status');
  const list=document.createElement('div');list.setAttribute('role','listbox');list.setAttribute('aria-label','Project file suggestions');
  list.id=`workspace-mentions-${++nextListId}`;
  panel.append(status,list);form.append(panel);
  let generation=0,timer,active=null,paths=[],selected=0,searching=false;
  const token=()=>{
    if(!enabled() || document.activeElement!==input || input.selectionStart!==input.selectionEnd)return null;
    const end=input.selectionStart;
    const match=/(?:^|\s)@([^\s"@]{1,200})$/.exec(input.value.slice(0,end));
    return match?{text:input.value,end,start:end-match[1].length-1,query:match[1]}:null;
  };
  const same=(a,b)=>a && b && a.text===b.text && a.end===b.end;
  const close=()=>{
    generation++;clearTimeout(timer);active=null;paths=[];searching=false;panel.hidden=true;
    input.removeAttribute('aria-activedescendant');input.removeAttribute('aria-controls');
  };
  const choose=index=>{
    if(!same(active,token()) || !paths[index]){close();return;}
    const path=paths[index];
    const mention='@'+(/\s|["\\]/.test(path)?JSON.stringify(path):path)+' ';
    input.setRangeText(mention,active.start,active.end,'end');persist();close();input.focus();
  };
  const highlight=()=>{
    [...list.children].forEach((item,index)=>item.setAttribute('aria-selected',String(index===selected)));
    const item=list.children[selected];
    if(item){input.setAttribute('aria-activedescendant',item.id);item.scrollIntoView({block:'nearest'});}
  };
  const update=()=>{
    close();const candidate=token();if(!candidate)return;
    const request=generation;active=candidate;searching=true;
    timer=setTimeout(async()=>{
      panel.hidden=false;status.textContent='Searching...';list.replaceChildren();
      try{
        const result=await search(candidate.query);
        if(request!==generation || !same(candidate,token()))return;
        paths=result.paths.slice(0,20);selected=0;searching=false;
        status.textContent=paths.length?`${paths.length} files`:'No matching files';
        input.setAttribute('aria-controls',list.id);
        for(const [index,path] of paths.entries()){
          const item=document.createElement('div');item.id=`${list.id}-${index}`;
          item.setAttribute('role','option');item.textContent=path;
          item.addEventListener('pointerdown',e=>{e.preventDefault();choose(index);});
          list.append(item);
        }
        highlight();
      }catch(error){if(request===generation){searching=false;status.textContent=error.message;}}
    },180);
  };
  input.addEventListener('input',event=>{if(event.isComposing)close();else update();},{signal:lifetime.signal});
  input.addEventListener('click',update,{signal:lifetime.signal});
  input.addEventListener('blur',close,{signal:lifetime.signal});
  input.addEventListener('keydown',e=>{
    if(!active || e.isComposing || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey)return;
    if(!same(active,token())){close();return;}
    if(e.key==='Escape'){
      e.preventDefault();e.stopImmediatePropagation();close();
    }else if(e.key==='Enter' || (e.key==='Tab' && paths.length)){
      if(!paths.length && !searching){close();return;}
      e.preventDefault();e.stopImmediatePropagation();if(paths.length)choose(selected);
    }else if(paths.length && ['ArrowDown','ArrowUp'].includes(e.key)){
      e.preventDefault();e.stopImmediatePropagation();selected=(selected+(e.key==='ArrowDown'?1:-1)+paths.length)%paths.length;highlight();
    }else if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key))close();
  },{capture:true,signal:lifetime.signal});
  return ()=>{close();lifetime.abort();panel.remove();};
}
