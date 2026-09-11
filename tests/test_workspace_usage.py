import asyncio
from types import SimpleNamespace

from core.workspace_usage import WorkspaceUsage, normalize_limits


def test_native_claude_limits_keep_zero_and_real_reset():
    result=normalize_limits('claude', {'rate_limits_available':True,'rate_limits':{
        'five_hour':{'utilization':0,'resets_at':'2026-09-11T20:00:00Z'},
        'seven_day':{'utilization':58,'resets_at':None}}}, 100)
    assert result['five_hour']['used_percentage']==0
    assert result['five_hour']['resets_at']==1789156800
    assert result['updated_at']==100
    assert normalize_limits('claude',{'rate_limits_available':False},100) is None


def test_codex_uses_account_bucket_and_window_duration():
    result=normalize_limits('codex',{'limits':[{'id':'codex',
        'primary':{'usedPercent':71,'windowDurationMins':10080,'resetsAt':900},
        'secondary':None}]},100)
    assert result['seven_day']['used_percentage']==71
    assert 'five_hour' not in result


def test_usage_endpoint_prefers_fresh_workspace_observations(monkeypatch):
    from ui import web
    monkeypatch.setitem(web._LIVE_USAGE_CACHE, 'data', None)
    monkeypatch.setattr(web, '_read_live_usage_state', lambda: {'claude':{'available':True,'updated_at':10}})
    monkeypatch.setattr(web, '_latest_codex_usage', lambda: {'available':True,'updated_at':10})
    now=web.time.time()
    observation={'available':True,'updated_at':now,'five_hour':{'used_percentage':24,'observed_at':now}}
    host=SimpleNamespace(live_usage_snapshot=lambda:{provider:{**observation,'source':provider+'-workspace'} for provider in ('claude','codex')})
    monkeypatch.setitem(web.app.extensions,'workspace_host',host)
    monkeypatch.setattr(web,'read_gemini_usage',lambda **kwargs:{'available':True,'source':'gemini-cli-usage','updated_at':now-180})
    try:
        data=web._live_usage_payload()
        for provider in ('claude','codex'):
            assert data[provider]['source']==provider+'-workspace'
            assert data[provider]['five_hour']['used_percentage']==24
            assert not data[provider]['stale']
        assert not data['gemini']['stale']
        assert web._mark_usage_freshness({'source':'gemini-cli-usage','updated_at':now-400},None,now)['stale']
    finally:
        web._LIVE_USAGE_CACHE['data']=None


def test_usage_only_polls_existing_awake_owners_and_retains_failure_age():
    async def run():
        calls=[]
        async def limits():
            calls.append('read')
            return {'rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':3}}}
        reader=WorkspaceUsage()
        owner=SimpleNamespace(state='ready',account_rate_limits=limits)
        sleeping=SimpleNamespace(state='ready',rpc=SimpleNamespace(suspended=True),account_rate_limits=limits)
        assert reader.snapshot({})=={}
        await reader.task
        assert calls==[]
        await reader.refresh([(sleeping,'codex'),(owner,'claude'),(owner,'claude')])
        assert calls==['read']
        original=reader.data['claude'].copy()
        async def failed(): raise RuntimeError('offline')
        owner.account_rate_limits=failed
        await reader.refresh([(owner,'claude')])
        assert reader.data['claude']==original
        reader.snapshot({'sid':(owner,'claude')})
        assert reader.task.done()  # Refresh interval has not elapsed.
    asyncio.run(run())
