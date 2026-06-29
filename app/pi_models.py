from __future__ import annotations

import json
from urllib.parse import quote, unquote

from . import config


class ModelConfigError(ValueError):
    pass


def encode_selection(provider: str, model_id: str) -> str:
    return f"{quote(provider, safe='')}\t{quote(model_id, safe='')}"


def decode_selection(value: str) -> tuple[str | None, str | None]:
    value = value.strip()
    if not value:
        return None, None
    parts = value.split("\t", 1)
    if len(parts) != 2:
        raise ModelConfigError("Model selection is invalid")
    provider = unquote(parts[0]).strip()
    model_id = unquote(parts[1]).strip()
    if not provider or not model_id:
        raise ModelConfigError("Model selection is invalid")
    return provider, model_id


def list_configured_models() -> list[dict[str, str]]:
    if not config.PI_MODELS_FILE.exists():
        return []
    try:
        payload = json.loads(config.PI_MODELS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelConfigError(f"Pi models config is invalid JSON: {exc}") from exc

    providers = payload.get("providers")
    if not isinstance(providers, dict):
        raise ModelConfigError("Pi models config must contain a providers object")

    options: list[dict[str, str]] = []
    for provider_name, provider_config in providers.items():
        if not isinstance(provider_name, str) or not isinstance(provider_config, dict):
            continue
        models = provider_config.get("models", [])
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            name = model.get("name")
            options.append(
                {
                    "provider": provider_name,
                    "id": model_id,
                    "name": name if isinstance(name, str) and name.strip() else model_id,
                    "value": encode_selection(provider_name, model_id),
                }
            )
    return options


def validate_selection(provider: str | None, model_id: str | None) -> None:
    if not provider and not model_id:
        return
    if not provider or not model_id:
        raise ModelConfigError("Provider and model must be selected together")
    for option in list_configured_models():
        if option["provider"] == provider and option["id"] == model_id:
            return
    raise ModelConfigError(f"Selected model is not configured in {config.PI_MODELS_FILE}")
