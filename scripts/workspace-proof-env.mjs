import {join} from 'node:path';

export function isolatedProofEnv(source, root, platform=process.platform) {
  const env={HOME:root,CLAUDE_CONFIG_DIR:join(root,'config'),XDG_CONFIG_HOME:join(root,'xdg')};
  for (const [key,value] of Object.entries(source)) {
    if (key.toUpperCase()==='PATH') env.PATH=value;
    if (platform==='win32' && ['SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'].includes(key.toUpperCase())) env[key]=value;
  }
  if (platform==='win32') Object.assign(env,{
    USERPROFILE:root,APPDATA:join(root,'appdata'),LOCALAPPDATA:join(root,'localappdata'),TEMP:root,TMP:root,
  });
  return env;
}
