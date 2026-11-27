from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Type, TypeVar, Dict

from dotenv import load_dotenv
from openai import APIError, OpenAI
from pydantic import ValidationError

from evolution.config import ModelProfile
from evolution.errors import ContractError, InfrastructureError
from evolution.models import Model, canonical
from evolution.storage import Store, artifact_id

T = TypeVar("T", bound=Model)


def build_request(settings: ModelProfile, role: str, payload: Dict[str, Any], schema: Type[Model]) -> dict:
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
    return request


class LLM(Protocol):
    def ask(self, profile: str, role: str, payload: Dict[str, Any], schema: Type[T], prefix: str,
            validate: Optional[Callable[[T], None]] = None) -> T:
        ...


class OpenAIClient:
    """Same SDK, request and validation for remote APIs and local vLLM.

    No endpoint/model guessing and no exception-based protocol fallback.
    """

    def __init__(self, profiles: Dict[str, ModelProfile], store: Store, env_path: Optional[Path] = None):
        # Explicit project-root path also works when called from another directory.
        # Existing environment variables take precedence over values in .env.
        load_dotenv(dotenv_path=env_path if env_path is not None else Path(__file__).resolve().parents[1] / ".env",
                    override=False)
        self.profiles, self.store = profiles, store
        self.clients: Dict[str, OpenAI] = {}

    def ask(self, profile: str, role: str, payload: Dict[str, Any], schema: Type[T], prefix: str,
            validate: Optional[Callable[[T], None]] = None) -> T:
        correction = None
        for attempt in range(2):
            try:
                return self._ask(profile, role, payload, schema, prefix, validate, correction)
            except ContractError as error:
                if attempt:
                    raise
                detail = str(error.__cause__) if isinstance(error.__cause__, ValidationError) else str(error)
                correction = {"instruction": "The previous response failed validation. Return a complete corrected "
                    "response for the original task using only the supplied input. Correct the reported errors; "
                    "do not invent evidence or silently remove supported findings. Return JSON, not an explanation.",
                    "validation_error": detail[:6000]}
                print(f"LLM 返回校验失败，正在修正（最多 1 次）：{prefix}", file=sys.stderr, flush=True)

    def _ask(self, profile, role, payload, schema, prefix, validate, correction):
        settings = self.profiles[profile]
        call = prefix + "/llm/" + artifact_id()
        if correction is not None:
            payload = {**payload, "output_correction": correction}
        request = build_request(settings, role, payload, schema)
        self.store.save(call + "/request.json", {"profile": profile, "base_url": settings.base_url, **request})
        size = len(canonical(request).encode("utf-8"))
        if size > settings.max_input_bytes:
            self.store.save(call + "/error.json", {"category": "INPUT_BUDGET_EXCEEDED",
                "input_bytes": size, "max_input_bytes": settings.max_input_bytes})
            raise InfrastructureError(f"model input budget exceeded: {size} > {settings.max_input_bytes} bytes; "
                                      "split the request or configure models.<profile>.max_input_bytes")
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
        if validate is not None:
            try:
                validate(parsed)
            except ContractError as error:
                self.store.save(call + "/error.json", {"category": "CONTRACT_VALIDATION", "detail": str(error)})
                raise
        return parsed
