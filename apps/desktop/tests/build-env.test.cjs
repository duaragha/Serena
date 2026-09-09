const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const helper=path.resolve(__dirname,'../scripts/native-build-env.sh');

test('native build restores original libraries without AppImage paths',{skip:process.platform==='win32'},()=>{
  const result=spawnSync('bash',['-c','source "$1"; printf "%s|%s|%s" "${LD_LIBRARY_PATH-unset}" "${LD_LIBRARY_PATH_ORIG-unset}" "${APPDIR-unset}"','proof',helper],{
    encoding:'utf8',env:{...process.env,APPDIR:'/tmp/.mount_App',LD_LIBRARY_PATH:'/tmp/.mount_App/resources/sidecar/_internal:/wrong',LD_LIBRARY_PATH_ORIG:'/tmp/.mount_App/usr/lib:/custom/lib:'},
  });
  assert.equal(result.status,0,result.stderr);
  assert.equal(result.stdout,'/custom/lib|unset|unset');
});

test('native build removes an exclusively bundled library path',{skip:process.platform==='win32'},()=>{
  const env={...process.env,APPDIR:'/tmp/.mount_App',LD_LIBRARY_PATH:'/tmp/.mount_App/resources/sidecar/_internal'};
  delete env.LD_LIBRARY_PATH_ORIG;
  const result=spawnSync('bash',['-c','source "$1"; printf "%s" "${LD_LIBRARY_PATH-unset}"','proof',helper],{encoding:'utf8',env});
  assert.equal(result.status,0,result.stderr);
  assert.equal(result.stdout,'unset');
});
