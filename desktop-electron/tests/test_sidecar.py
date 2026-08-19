import importlib.util
import os
from pathlib import Path


def _load_sidecar():
    source = Path(__file__).resolve().parents[1] / "sidecar.py"
    spec = importlib.util.spec_from_file_location("serena_electron_sidecar", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_route_identifies_the_live_sidecar_process():
    sidecar = _load_sidecar()
    response = sidecar.app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "pid": os.getpid()}
