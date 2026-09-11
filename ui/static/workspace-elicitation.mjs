const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};

export function renderElicitation(form, params, reply) {
  form.append(node('strong',params.serverName || 'MCP server'),node('p',params.message || 'Additional input requested'));
  if(params._meta?.codex_approval_kind==='mcp_tool_call'){
    const details=node('details');details.append(node('summary','Tool arguments'),node('pre',JSON.stringify(params._meta.tool_params ?? {},null,2)));form.append(details);
  }
  const fields=[];
  let supported=true;
  if(params.mode==='url'){
    try {
      const url=new URL(params.url);
      if(!['https:','http:'].includes(url.protocol))throw Error('Unsupported URL');
      const link=node('a',url.href);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';form.append(link);
    } catch {supported=false;form.append(node('p','The server supplied an unsupported URL'));}
  } else if(params.mode==='form' && params.requestedSchema?.type==='object') {
    for(const [name,schema] of Object.entries(params.requestedSchema.properties || {})) {
      const label=node('label',schema.title || name);
      const options=schema.enum?.map((value,i)=>({const:value,title:schema.enumNames?.[i] || value})) || schema.oneOf || schema.items?.anyOf || schema.items?.enum?.map(value=>({const:value,title:value}));
      let field;
      if(options){
        field=node('select');field.multiple=schema.type==='array';
        if(!field.multiple){const empty=node('option','');empty.value='';field.append(empty);}
        for(const choice of options){const option=node('option',choice.title ?? choice.const);option.value=String(choice.const);field.append(option);}
      } else if(['string','number','integer','boolean'].includes(schema.type)) {
        field=node('input');field.type=schema.type==='boolean'?'checkbox':['number','integer'].includes(schema.type)?'number':schema.format==='email'?'email':schema.format==='date'?'date':'text';
        if(field.type==='number'){field.step=schema.type==='integer'?'1':'any';if(schema.minimum!=null)field.min=schema.minimum;if(schema.maximum!=null)field.max=schema.maximum;}
      } else {supported=false;form.append(node('p',`Unsupported field: ${schema.title || name}`));continue;}
      field.name=name;
      const required=(params.requestedSchema.required || []).includes(name);
      if(field.type!=='checkbox')field.required=required;
      if(schema.default!=null){
        if(field.type==='checkbox')field.checked=schema.default;
        else if(field.multiple)for(const option of field.options)option.selected=schema.default.includes(option.value);
        else field.value=schema.default;
      }
      let changed=false;field.addEventListener('input',()=>{changed=true;});
      if(field.type==='checkbox')label.prepend(field);else label.append(field);
      if(schema.description)label.append(node('small',schema.description));form.append(label);
      fields.push(()=>{
        if(field.type==='checkbox')return required || changed || schema.default!=null ? [name,field.checked] : null;
        if(field.multiple){const values=[...field.selectedOptions].map(o=>o.value);return values.length || required ? [name,values] : null;}
        if(!field.value && !required)return null;
        return [name,field.type==='number'?Number(field.value):field.value];
      });
    }
  } else {supported=false;form.append(node('p','This MCP form mode is not supported'));}
  for(const [action,title] of [['decline','Decline'],['cancel','Cancel']]){
    const button=node('button',title);button.type='button';button.addEventListener('click',()=>reply({action,content:null}));form.append(button);
  }
  const accept=node('button',params.mode==='url'?'Confirm completion':'Submit');accept.type='submit';accept.disabled=!supported;form.append(accept);
  form.addEventListener('submit',event=>{event.preventDefault();if(supported)reply({action:'accept',content:params.mode==='url'?null:Object.fromEntries(fields.map(read=>read()).filter(Boolean))});});
}
