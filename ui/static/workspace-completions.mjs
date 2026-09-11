let sequence=0;

export function installCommandSuggestions({input,form,provider,load,choose,persist}) {
  const lifetime=new AbortController();
  const panel=document.createElement('div');panel.className='aw-mentions aw-completions';panel.hidden=true;
  const status=document.createElement('div');status.setAttribute('role','status');
  const list=document.createElement('div');list.id=`command-suggestions-${++sequence}`;
  list.setAttribute('role','listbox');list.setAttribute('aria-label','Command and skill suggestions');
  panel.append(status,list);form.append(panel);
  let generation=0,active=null,items=[],selected=0,loading=false;
  const token=()=>{
    if(document.activeElement!==input || input.selectionStart!==input.selectionEnd)return null;
    const end=input.selectionStart,text=input.value;
    const match=/(?:^|\s)([$/])([^\s$/]*)$/.exec(text.slice(0,end));
    if(!match || (match[1]==='$' && provider!=='Codex'))return null;
    return {text,end,start:end-match[2].length-1,prefix:match[1],query:match[2].toLowerCase()};
  };
  const same=t=>t && active && t.text===active.text && t.end===active.end;
  const close=()=>{
    generation++;active=null;items=[];loading=false;panel.hidden=true;
    if(input.getAttribute('aria-controls')===list.id){input.removeAttribute('aria-controls');input.removeAttribute('aria-activedescendant');}
  };
  const highlight=()=>{
    [...list.children].forEach((row,i)=>row.setAttribute('aria-selected',String(i===selected)));
    const row=list.children[selected];
    if(row){input.setAttribute('aria-activedescendant',row.id);row.scrollIntoView({block:'nearest'});}
  };
  const insert=index=>{
    if(!same(token()) || !items[index])return close();
    const item=items[index];
    choose(item);
    input.setRangeText(`${active.prefix}${item.name} `,active.start,active.end,'end');
    persist();close();input.focus();
  };
  const update=async()=>{
    close();active=token();if(!active)return;
    const request=generation;loading=true;panel.hidden=false;status.textContent='Loading...';list.replaceChildren();
    input.setAttribute('aria-controls',list.id);
    try {
      const catalog=await load();
      if(request!==generation || !same(token()))return;
      input.setAttribute('aria-controls',list.id);
      items=catalog.filter(item=>!item.unavailableReason && (active.prefix==='$'?item.kind==='skill':item.kind!=='skill')
        && `${item.name} ${item.description || ''}`.toLowerCase().includes(active.query)).slice(0,30);
      loading=false;selected=0;status.textContent=items.length?'':'No matching suggestions';
      for(const [index,item] of items.entries()){
        const row=document.createElement('div');row.id=`${list.id}-${index}`;row.setAttribute('role','option');
        const name=document.createElement('strong');name.textContent=active.prefix+item.name;
        const description=document.createElement('span');description.textContent=item.description || '';row.title=description.textContent;
        row.append(name,description);row.addEventListener('pointerdown',event=>{event.preventDefault();insert(index);});list.append(row);
      }
      highlight();
    } catch(error){if(request===generation){loading=false;status.textContent=error.message;}}
  };
  input.addEventListener('input',event=>{if(event.isComposing)close();else update();},{signal:lifetime.signal});
  input.addEventListener('click',update,{signal:lifetime.signal});
  input.addEventListener('blur',close,{signal:lifetime.signal});
  input.addEventListener('keydown',event=>{
    if(!active || event.isComposing || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey)return;
    if(!same(token()))return close();
    if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();close();}
    else if(['Enter','Tab'].includes(event.key) && (items.length || loading)){
      event.preventDefault();event.stopImmediatePropagation();if(items.length)insert(selected);
    }else if(items.length && ['ArrowDown','ArrowUp'].includes(event.key)){
      event.preventDefault();event.stopImmediatePropagation();selected=(selected+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;highlight();
    }else if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key))close();
  },{capture:true,signal:lifetime.signal});
  return {open(){input.focus();const prefix=provider==='Codex'?'$':'/';const space=input.selectionStart && !/\s/.test(input.value[input.selectionStart-1])?' ':'';input.setRangeText(space+prefix,input.selectionStart,input.selectionEnd,'end');persist();update();},
    dispose(){close();lifetime.abort();panel.remove();}};
}
