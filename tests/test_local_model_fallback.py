"""The local fallback profile, its endpoint, and its provenance guarantees.

Every test here injects a deterministic opener, so no socket is ever opened and
no weights are ever fetched. The RX 6800 XT is 16 GB and this machine has 32 GB
of RAM; the sizing assertions below are written against those real numbers
rather than a hypothetical card.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from core.local_model_fallback import (
    DEFAULT_BASE_URL,
    PROFILES,
    ROLES,
    HardwareBudget,
    LocalBrain,
    LocalModelEndpoint,
    LocalModelUnavailable,
    ProvenanceError,
    assert_local_provenance,
    fitting_profiles,
    hardware_report,
    local_status,
    select_profile,
    weight_instruction,
)

RX_6800_XT = HardwareBudget(vram_gb=16.0, ram_gb=32.0)


class FakeServer:
    """A local OpenAI-compatible server that never touches the network."""

    def __init__(self, *, models=("qwen2.5:14b-instruct-q4_K_M",), reply="local answer"):
        self.models = list(models)
        self.reply = reply
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        if url.endswith("/models"):
            return json.dumps(
                {"data": [{"id": name} for name in self.models]}
            ).encode("utf-8")
        if url.endswith("/chat/completions"):
            self.payloads.append(json.loads(request.data.decode("utf-8")))
            return json.dumps(
                {
                    "model": self.models[0] if self.models else "unknown",
                    "choices": [{"message": {"role": "assistant", "content": self.reply}}],
                }
            ).encode("utf-8")
        raise AssertionError(f"unexpected local call to {url}")


def _down(_request, _timeout):
    raise urllib.error.URLError("connection refused")


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    monkeypatch.delenv("SERENA_LOCAL_MODEL_URL", raising=False)


# ---- hardware sizing ------------------------------------------------------


def test_every_profile_fits_the_real_card_with_headroom():
    for profile in PROFILES:
        assert profile.fits(RX_6800_XT), profile.model_id
        # The desktop is using this GPU too; nothing may consume the whole card.
        assert profile.approx_vram_gb <= RX_6800_XT.usable_vram_gb
        assert profile.headroom_gb(RX_6800_XT) >= 0


def test_the_three_roles_exist_and_grow_in_size():
    assert ROLES == ("reflex", "conversation", "reasoning")
    sizes = [select_profile(role, budget=RX_6800_XT).approx_vram_gb for role in ROLES]
    assert sizes == sorted(sizes)


def test_the_reasoning_profile_is_the_largest_that_still_fits():
    reasoning = select_profile("reasoning", budget=RX_6800_XT)
    assert reasoning.model_id == "gpt-oss:20b"
    assert reasoning.approx_vram_gb <= RX_6800_XT.usable_vram_gb
    # A 20b at this quantization on a 16 GB card is tight, not comfortable.
    assert reasoning.headroom_gb(RX_6800_XT) < 3.0


def test_a_profile_that_does_not_fit_is_refused_with_the_numbers():
    tiny = HardwareBudget(vram_gb=6.0, ram_gb=32.0)
    with pytest.raises(LocalModelUnavailable) as caught:
        select_profile("reasoning", budget=tiny)
    assert "13.0 GB" in str(caught.value)
    assert "4.5 GB is usable" in str(caught.value)
    # The small one still works on a small card, which is the point of tiering.
    assert select_profile("reflex", budget=tiny).model_id.startswith("qwen2.5:3b")


def test_fitting_profiles_shrinks_with_the_card():
    assert len(fitting_profiles(RX_6800_XT)) == 3
    assert len(fitting_profiles(HardwareBudget(vram_gb=6.0))) == 1


def test_an_unknown_role_is_refused():
    with pytest.raises(LocalModelUnavailable):
        select_profile("omniscient", budget=RX_6800_XT)


def test_the_run_downloads_nothing_and_says_how_a_human_would():
    report = hardware_report(RX_6800_XT)
    assert report["downloads_performed"] is False
    assert weight_instruction(PROFILES[0]) == f"ollama pull {PROFILES[0].model_id}"
    assert all(line.startswith("ollama pull ") for line in report["weight_instructions"])


# ---- the endpoint stays on this machine ----------------------------------


def test_a_remote_endpoint_is_refused_outright():
    for url in (
        "http://192.168.1.50:11434/v1",
        "https://api.example.com/v1",
        "http://model.internal:8000/v1",
    ):
        with pytest.raises(LocalModelUnavailable) as caught:
            LocalModelEndpoint(url)
        assert "loopback" in str(caught.value)


def test_loopback_spellings_are_accepted():
    for url in ("http://127.0.0.1:11434/v1", "http://localhost:8000/v1"):
        assert LocalModelEndpoint(url, opener=FakeServer()).base_url == url


def test_a_nonsense_scheme_is_refused():
    with pytest.raises(LocalModelUnavailable):
        LocalModelEndpoint("ftp://127.0.0.1/v1")


def test_the_default_endpoint_is_loopback():
    assert LocalModelEndpoint(opener=FakeServer()).base_url == DEFAULT_BASE_URL


# ---- probing --------------------------------------------------------------


def test_a_missing_server_is_reported_not_raised():
    status = LocalModelEndpoint(opener=_down).probe(now=1.0)
    assert status.available is False
    assert "no local model server answering" in status.reason
    assert status.checked_at == 1.0


def test_a_server_with_no_weights_says_how_to_get_them():
    status = LocalModelEndpoint(opener=FakeServer(models=())).probe()
    assert not status.available
    assert "ollama pull" in status.reason


def test_a_server_without_our_model_names_the_missing_one():
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT),
        opener=FakeServer(models=("some-other-model:latest",)),
    )
    status = endpoint.probe()
    assert not status.available
    assert "qwen2.5:14b-instruct-q4_K_M is not loaded" in status.reason
    assert status.served_models == ("some-other-model:latest",)


def test_a_loaded_model_is_available():
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT),
        opener=FakeServer(),
    )
    status = endpoint.probe()
    assert status.available
    assert "served locally" in status.reason


def test_a_tag_variant_of_the_same_size_still_counts_as_loaded():
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT),
        opener=FakeServer(models=("qwen2.5:14b",)),
    )
    assert endpoint.probe().available


def test_a_smaller_sibling_may_not_impersonate_the_conversation_model():
    """The 3b and the 14b share a family stem. Only one of them can hold a conversation."""

    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT),
        opener=FakeServer(models=("qwen2.5:3b-instruct-q4_K_M",)),
    )
    status = endpoint.probe()

    assert status.available is False
    assert "it is a 3b model and this profile needs 14b" in status.reason
    assert "ollama pull qwen2.5:14b-instruct-q4_K_M" in status.reason


def test_an_unsized_alias_is_not_proof_of_anything():
    """`latest` says nothing about size, so it cannot verify the profile."""

    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT),
        opener=FakeServer(models=("qwen2.5:latest",)),
    )
    assert endpoint.probe().available is False


def test_a_bigger_sibling_does_not_satisfy_a_smaller_profile_either():
    endpoint = LocalModelEndpoint(
        profile=select_profile("reflex", budget=RX_6800_XT),
        opener=FakeServer(models=("qwen2.5:14b-instruct-q4_K_M",)),
    )
    assert endpoint.probe().available is False


def test_the_reasoning_profile_matches_its_own_exact_id():
    endpoint = LocalModelEndpoint(
        profile=select_profile("reasoning", budget=RX_6800_XT),
        opener=FakeServer(models=("gpt-oss:20b",)),
    )
    assert endpoint.probe().available is True


def test_unreadable_json_is_treated_as_unavailable():
    def garbage(_request, _timeout):
        return b"<html>nope</html>"

    assert not LocalModelEndpoint(opener=garbage).probe().available


# ---- completion and provenance -------------------------------------------


def test_a_completion_is_labelled_local_with_the_real_model():
    server = FakeServer(reply="i can still read your commitments")
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT), opener=server
    )
    result = endpoint.complete([{"role": "user", "content": "you there?"}])

    assert result["text"] == "i can still read your commitments"
    assert result["provider"] == "local"
    assert result["model"] == "qwen2.5:14b-instruct-q4_K_M"
    assert result["origin"] == "local-model"
    assert assert_local_provenance(result) is result


def test_the_served_model_name_wins_over_the_requested_one():
    """If the server answered with something else, that is what he is told."""

    server = FakeServer(models=("qwen2.5:7b-instruct-q4_K_M",))
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT), opener=server
    )
    result = endpoint.complete([{"role": "user", "content": "hi"}])
    assert result["model"] == "qwen2.5:7b-instruct-q4_K_M"


def test_a_completion_carries_a_keep_alive_so_the_gpu_comes_back():
    server = FakeServer()
    LocalModelEndpoint(opener=server).complete([{"role": "user", "content": "hi"}])
    assert server.payloads[0]["keep_alive"] == "5m"
    assert server.payloads[0]["stream"] is False


def test_a_dead_server_during_a_call_fails_loudly():
    with pytest.raises(LocalModelUnavailable):
        LocalModelEndpoint(opener=_down).complete([{"role": "user", "content": "hi"}])


def test_an_empty_completion_is_an_error_not_an_answer():
    def empty(request, _timeout):
        if request.full_url.endswith("/models"):
            return json.dumps({"data": [{"id": "qwen2.5:14b-instruct-q4_K_M"}]}).encode()
        return json.dumps({"choices": [{"message": {"content": "   "}}]}).encode()

    with pytest.raises(LocalModelUnavailable):
        LocalModelEndpoint(opener=empty).complete([{"role": "user", "content": "hi"}])


def test_a_local_result_may_never_wear_a_cloud_model_name():
    for name in ("claude-opus-5", "gpt-5.6-sol", "claude-sonnet-5"):
        with pytest.raises(ProvenanceError):
            assert_local_provenance({"provider": "local", "model": name})


def test_a_cloud_labelled_result_is_not_a_local_result():
    with pytest.raises(ProvenanceError):
        assert_local_provenance({"provider": "claude", "model": "claude-opus-5"})
    with pytest.raises(ProvenanceError):
        assert_local_provenance({"provider": "local", "model": ""})


# ---- the brain-shaped adapter --------------------------------------------


def test_the_local_brain_matches_the_worker_contract():
    import asyncio

    server = FakeServer(reply="still here")
    endpoint = LocalModelEndpoint(
        profile=select_profile("conversation", budget=RX_6800_XT), opener=server
    )
    brain = LocalBrain(endpoint, system_prompt="you are serena")

    async def scenario():
        await brain.start()
        assert brain.started
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        result = await brain.turn("what do i owe today?", on_delta=on_delta)
        assert result["text"] == "still here"
        assert result["provider"] == "local"
        assert deltas == ["still here"]

        snapshot = brain.snapshot()
        assert snapshot["provider"] == "local"
        assert snapshot["model"] == "qwen2.5:14b-instruct-q4_K_M"
        assert snapshot["running"] is True

        await brain.interrupt()
        await brain.close()
        assert brain.started is False

    asyncio.run(scenario())
    # The system prompt really was sent, so persona survives the fallback.
    assert server.payloads[0]["messages"][0]["content"] == "you are serena"


def test_the_local_brain_refuses_to_start_without_weights():
    import asyncio

    brain = LocalBrain(LocalModelEndpoint(opener=FakeServer(models=())))
    with pytest.raises(LocalModelUnavailable):
        asyncio.run(brain.start())
    assert brain.started is False


# ---- the one-call helper --------------------------------------------------


def test_local_status_returns_the_profile_and_a_safe_status():
    profile, status = local_status(
        budget=RX_6800_XT,
        endpoint=LocalModelEndpoint(
            profile=select_profile("conversation", budget=RX_6800_XT),
            opener=FakeServer(),
        ),
    )
    assert profile is not None and profile.role == "conversation"
    assert status.available


def test_local_status_never_raises_when_nothing_fits():
    profile, status = local_status(
        role="reasoning", budget=HardwareBudget(vram_gb=4.0)
    )
    assert profile is None
    assert not status.available
    assert "VRAM" in status.reason
