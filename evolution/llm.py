from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Protocol, Type, TypeVar

from dotenv import load_dotenv
from openai import APIError, OpenAI
from pydantic import ValidationError

from evolution.config import ModelProfile
from evolution.errors import ContractError, InfrastructureError
from evolution.models import Model, canonical
from evolution.storage import Store, artifact_id

T = TypeVar("T", bound=Model)


class LLM(Protocol):
    def ask(self, profile: str, role: str, payload: dict[str, Any], schema: Type[T], prefix: str) -> T:
        ...


class OpenAIClient:
    """Same SDK, request and validation for remote APIs and local vLLM.

    No endpoint/model guessing and no exception-based protocol fallback.
    """

    def __init__(self, profiles: dict[str, ModelProfile], store: Store, env_path: Optional[Path] = None):
        # Explicit project-root path also works when called from another directory.
        # Existing environment variables take precedence over values in .env.
        load_dotenv(dotenv_path=env_path if env_path is not None else Path(__file__).resolve().parents[1] / ".env",
                    override=False)
        self.profiles, self.store = profiles, store
        self.clients: dict[str, OpenAI] = {}

    def ask(self, profile: str, role: str, payload: dict[str, Any], schema: Type[T], prefix: str) -> T:
        settings = self.profiles[profile]
        call = prefix + "/llm/" + artifact_id()
        request = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": role + "\nReturn JSON matching this schema: " + canonical(schema.model_json_schema())},
                {"role": "user", "content": canonical(payload)},
            ],
            "response_format": {"type": "json_object"},
            **settings.parameters,
        }
        if settings.structured_output == "json_schema":
            request["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}
        self.store.save(call + "/request.json", {"profile": profile, "base_url": settings.base_url, **request})
        # Credential routing and values both live in .env, outside problem YAML
        # and its archived snapshots. No profile-specific fallback is inferred.
        key_variable = os.environ.get(f"LLM_{profile.upper()}_API_KEY_ENV")
        if not key_variable:
            self.store.save(call + "/error.json", {"category": "MISSING_CREDENTIAL_MAPPING", "profile": profile})
            raise InfrastructureError("missing .env API key mapping for model profile: " + profile)
        key = os.environ.get(key_variable)
        if not key:
            self.store.save(call + "/error.json", {"category": "MISSING_API_KEY", "profile": profile})
            raise InfrastructureError("missing API key for model profile: " + profile)
        if profile not in self.clients:
            self.clients[profile] = OpenAI(api_key=key, base_url=settings.base_url,
                timeout=settings.timeout_seconds, max_retries=settings.max_retries)
        try:
            response = self.clients[profile].chat.completions.create(**request)
        except APIError as error:
            # Persist category/status only: upstream error bodies can contain credentials.
            self.store.save(call + "/error.json", {"category": type(error).__name__,
                "status_code": getattr(error, "status_code", None)})
            raise InfrastructureError("model service request failed: " + type(error).__name__) from error
        self.store.save(call + "/response.json", response.model_dump(mode="json"))
        choice = response.choices[0] if response.choices else None
        if choice is None or choice.finish_reason != "stop" or choice.message.refusal or not choice.message.content:
            self.store.save(call + "/error.json", {"category": "INCOMPLETE_OR_REFUSED"})
            raise ContractError("model output incomplete or refused; see " + call)
        try:
            parsed = schema.model_validate_json(choice.message.content)
        except ValidationError as error:
            self.store.save(call + "/error.json", {"category": "SCHEMA_VALIDATION", "detail": str(error)})
            raise ContractError("invalid model JSON; see " + call) from error
        self.store.save(call + "/validated.json", parsed)
        return parsed
