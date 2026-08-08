"""The Ollama providers must reach the endpoint the deployment names (#1099).

Both providers hard-pinned ``http://localhost:11434`` and read no environment
variable at all, and nothing in the production call path passed a ``base_url``
(``phase_config.get_provider_extra_kwargs`` returns ``{}`` for every provider
except ``openai-compatible``). So a phase pinned to ``ollama:<model>`` tried
localhost inside a pod, where there is no Ollama -- the self-hosted box was only
reachable by spelling the phase ``openai-compatible:`` instead.

``OLLAMA_API_KEY`` had the same shape: declared as build-Job env passthrough and
read by nothing, so a hosted endpoint was called unauthenticated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from providers._ollama_http import (  # noqa: E402
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_CLOUD_BASE_URL,
    OllamaHTTPMixin,
    resolve_ollama_api_key,
    resolve_ollama_base_url,
    resolve_ollama_cloud_base_url,
)
from providers.factory import (  # noqa: E402
    _resolve_canonical,
    get_provider,
    runtime_to_provider,
)
from providers.ollama_agentic import OllamaAgenticProvider  # noqa: E402

_ALL_ENV = (
    "OLLAMA_BASE_URL",
    "OLLAMA_API_URL",
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    "OLLAMA_CLOUD_BASE_URL",
    "LITELLM_GATEWAY_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def test_defaults_to_localhost_with_no_env() -> None:
    assert resolve_ollama_base_url() == DEFAULT_OLLAMA_BASE_URL


@pytest.mark.parametrize("var", ["OLLAMA_BASE_URL", "OLLAMA_API_URL", "OLLAMA_HOST"])
def test_every_documented_env_var_is_honoured(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "http://host.k3d.internal:11434")
    assert resolve_ollama_base_url() == "http://host.k3d.internal:11434"


def test_precedence_and_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://last:11434")
    monkeypatch.setenv("OLLAMA_API_URL", "http://middle:11434")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://first:11434/")
    assert resolve_ollama_base_url() == "http://first:11434"


def test_blank_env_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "   ")
    monkeypatch.setenv("OLLAMA_API_URL", "http://real:11434")
    assert resolve_ollama_base_url() == "http://real:11434"


# ---------------------------------------------------------------------------
# The provider constructor -- the site every caller converges on
# ---------------------------------------------------------------------------


def test_agentic_provider_uses_the_env_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #1099 regression: this returned localhost regardless of env."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.k3d.internal:11434")

    provider = OllamaAgenticProvider(model="qwen2.5:7b", working_dir=tmp_path)

    assert provider._base_url == "http://host.k3d.internal:11434"


def test_explicit_base_url_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://from-env:11434")

    provider = OllamaAgenticProvider(
        model="qwen2.5:7b", base_url="http://explicit:11434", working_dir=tmp_path
    )

    assert provider._base_url == "http://explicit:11434"


def test_provider_prefix_is_still_stripped(tmp_path: Path) -> None:
    """Ollama 400s if the `ollama:` prefix reaches /api/chat's model field."""
    provider = OllamaAgenticProvider(
        model="ollama:qwen2.5-coder:14b", working_dir=tmp_path
    )

    assert provider._model == "qwen2.5-coder:14b"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class _Endpoint(OllamaHTTPMixin):
    def __init__(self, base_url: str, api_key: str | None) -> None:
        self._base_url = base_url
        self._timeout = 5
        self._api_key = api_key


def test_no_key_configured_sends_no_auth_header() -> None:
    assert _Endpoint("https://ollama.com", None)._auth_headers() == {}


def test_hosted_https_endpoint_is_authenticated() -> None:
    assert _Endpoint("https://ollama.com", "sk-test")._auth_headers() == {
        "Authorization": "Bearer sk-test"
    }


def test_plaintext_endpoint_never_receives_the_token() -> None:
    """OLLAMA_API_KEY is deployment-wide but the endpoint is per-run, so keying
    the header on "a token exists" would ship a hosted credential to whatever
    plaintext box OLLAMA_BASE_URL names -- and the fleet's self-hosted server is
    plain http and needs no token at all."""
    assert _Endpoint("http://host.k3d.internal:11434", "sk-test")._auth_headers() == {}
    assert _Endpoint("http://localhost:11434", "sk-test")._auth_headers() == {}


def test_api_key_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_ollama_api_key() is None
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    assert resolve_ollama_api_key() == "sk-test"


def test_provider_picks_up_the_env_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")

    provider = OllamaAgenticProvider(model="qwen3-coder:480b", working_dir=tmp_path)

    assert provider._auth_headers() == {"Authorization": "Bearer sk-test"}


def test_the_token_is_not_in_the_provider_repr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-super-secret")

    provider = OllamaAgenticProvider(model="qwen3-coder:480b", working_dir=tmp_path)

    assert "sk-super-secret" not in repr(provider)


# ---------------------------------------------------------------------------
# ollama-cloud is a routing decision, not a label (#1213)
# ---------------------------------------------------------------------------


def test_cloud_runtime_has_its_own_canonical_provider() -> None:
    """THE DEFECT. `_RUNTIME_TO_PROVIDER` mapped both runtimes to "ollama", so
    by the time get_provider built anything, self-hosted and cloud were
    indistinguishable and both read the same single OLLAMA_BASE_URL."""
    assert runtime_to_provider("ollama") == "ollama"
    assert runtime_to_provider("ollama-cloud") == "ollama-cloud"
    assert _resolve_canonical("ollama-cloud") == "ollama-cloud"


def test_cloud_defaults_to_ollama_com() -> None:
    """A runtime called "cloud" must not fall back to a local box."""
    assert resolve_ollama_cloud_base_url() == DEFAULT_OLLAMA_CLOUD_BASE_URL
    assert DEFAULT_OLLAMA_CLOUD_BASE_URL == "https://ollama.com"


def test_cloud_reads_its_own_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.example/")
    assert resolve_ollama_cloud_base_url() == "https://ollama.example"


def test_self_hosted_env_does_not_move_the_cloud_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the split: both configured at once, neither shadowing the
    other. Before #1213 the p510 box and ollama.com were mutually exclusive in
    one deployment (Factory#295 C3 vs C4)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.k3d.internal:11434")

    assert resolve_ollama_base_url() == "http://host.k3d.internal:11434"
    assert resolve_ollama_cloud_base_url() == "https://ollama.com"


def test_both_endpoints_reachable_in_one_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two runtimes, one process, two endpoints -- the whole of #1213."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.k3d.internal:11434")
    monkeypatch.setenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")

    local = get_provider(
        "ollama", phase="coding", model="qwen3:14b", working_dir=tmp_path
    )
    cloud = get_provider(
        "ollama-cloud", phase="coding", model="qwen3-coder:480b", working_dir=tmp_path
    )

    assert local._base_url == "http://host.k3d.internal:11434"
    assert cloud._base_url == "https://ollama.com"
    # Same provider class, different destination -- no second provider module.
    assert type(local) is type(cloud)
    # And only the HTTPS one is handed the credential.
    assert local._auth_headers() == {}
    assert cloud._auth_headers() == {"Authorization": "Bearer sk-test"}


def test_cloud_model_prefix_is_stripped(tmp_path: Path) -> None:
    """`ollama-cloud:` does not start with `ollama:`, so the provider's own
    strip cannot reach it and /api/chat would 400 on the prefix."""
    cloud = get_provider(
        "ollama-cloud",
        phase="coding",
        model="ollama-cloud:qwen3-coder:480b",
        working_dir=tmp_path,
    )
    assert cloud._model == "qwen3-coder:480b"


def test_explicit_base_url_still_wins_for_cloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.example")
    cloud = get_provider(
        "ollama-cloud",
        phase="coding",
        model="qwen3-coder:480b",
        working_dir=tmp_path,
        base_url="https://explicit.example",
    )
    assert cloud._base_url == "https://explicit.example"
