"""Supported backend seams; no socket server and no remote-provider fallback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

import httpx
import jsonschema
import litellm
from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.types.utils import ModelResponse

from bridge import AdapterError, json_bytes, sha
from capability import index_context_observation

PROVIDER = "gptgrep_codex"
INDEX_ALIAS = "selected-account-model"
# LiteLLM1.97 checks known OpenAI model names before custom-provider dispatch.
# This routing-only alias prevents its OpenAI branch from bypassing the handler;
# the local host still receives and verifies the exact selected model name.
TEXT_SCHEMA = {
    "type": "object", "properties": {"text": {"type": "string"}},
    "required": ["text"], "additionalProperties": False,
}

PROTOCOL_VERSION = "pageindex-outer-sdk-actions.v3"
PROTOCOL_PREFIX = """LOCAL DOCUMENT-AGENT DECISION PROTOCOL
You produce one internal JSON decision for an outer document-agent executor.
This completion runtime has no native tools. The function declarations in
state.tools are real operations available to the OUTER PageIndex SDK, which
executes valid tool_calls after this completion and returns their results in
state.input on a subsequent completion. Writing tool_calls is structured action
data, not native tool invocation or direct external-resource access.

The text field is user-facing assistant prose. The tool_calls field is private
protocol data for the outer executor. Upstream instructions such as "do not
expose tool names" constrain the text field; they do not prohibit function names
inside tool_calls. Use the original tool names and argument schemas exactly.

Read state.input as the ordered conversation, including supplied index context
and actual function outputs. Follow the upstream workflow. When additional
evidence is needed, request it through the available outer SDK operations;
unavailable native tools do not imply unavailable outer operations. Use supplied
document descriptions or summaries when appropriate. Do not invent argument
values, document contents or tool results. Answer or abstain according to the
upstream instructions and the evidence provided. If a tool reports an error,
follow the upstream next-step policy.

BEGIN ORIGINAL PAGEINDEX INSTRUCTIONS (preserved verbatim)
"""
PROTOCOL_SUFFIX = """
END ORIGINAL PAGEINDEX INSTRUCTIONS

Return only the requested JSON decision. If an outer discovery/read operation is
needed, put its request in tool_calls and use an empty text string. For a final
answer or abstention under the upstream instructions, put the user-facing
response in text and leave tool_calls empty. Do not confuse
internal JSON action requests with unavailable native runtime tools.
"""


def protocol_identity() -> dict:
    return {"version": PROTOCOL_VERSION,
            "adapter_instructions_sha256": sha({"prefix": PROTOCOL_PREFIX, "suffix": PROTOCOL_SUFFIX})}


def decision_request(body: dict) -> tuple[str, dict, dict, dict]:
    original = body.get("instructions") or ""
    if not isinstance(original, str):
        raise ValueError("Upstream instructions must be a string")
    instructions = PROTOCOL_PREFIX + original + PROTOCOL_SUFFIX
    state = {"input": body.get("input"), "tools": body.get("tools", []),
             "tool_choice": body.get("tool_choice", "auto"),
             "parallel_tool_calls": body.get("parallel_tool_calls", True)}
    provenance = {**protocol_identity(),
                  "upstream_instructions_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                  "combined_instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest()}
    return instructions, state, decision_schema(state["tools"]), provenance


class IndexProvider(CustomLLM):
    def __init__(self, host):
        super().__init__()
        self.host = host

    def completion(self, model, messages, **kwargs):
        if model != INDEX_ALIAS:
            raise CustomLLMError(401, "Adapter refuses a changed indexing model")
        try:
            selected_model = self.host.model
            value, report = self.host.complete(
                "Return the assistant response to the supplied conversation exactly as requested, inside the text field.",
                {"messages": messages}, TEXT_SCHEMA,
            )
        except AdapterError as error:
            raise CustomLLMError(error.status_code, f"{error.code}: {error}") from error
        return self._response(value, report.get("model", selected_model))

    @staticmethod
    def _response(value, model):
        response = ModelResponse(
            model=model,
            choices=[{"index": 0, "message": {"role": "assistant", "content": value["text"]}, "finish_reason": "stop"}],
        )
        # Upstream indexing ignores usage. Actual native usage is authoritative in
        # the private host ledger; an absent SDK usage value is not a measured zero.
        response.usage = None
        return response

    async def acompletion(self, model, messages, **kwargs):
        if model != INDEX_ALIAS:
            raise CustomLLMError(401, "Adapter refuses a changed indexing model")
        selected_model = self.host.model
        try:
            value, report = await self.host.acomplete(
                "Return the assistant response to the supplied conversation exactly as requested, inside the text field.",
                {"messages": messages}, TEXT_SCHEMA,
            )
        except AdapterError as error:
            raise CustomLLMError(error.status_code, f"{error.code}: {error}") from error
        return self._response(value, report.get("model", selected_model))


def register_index_provider(host):
    handler = IndexProvider(host)
    other = [entry for entry in (litellm.custom_provider_map or []) if entry.get("provider") != PROVIDER]
    litellm.custom_provider_map = other + [{"provider": PROVIDER, "custom_handler": handler}]
    return handler


def decision_schema(tools: list[dict]) -> dict:
    names = [tool["name"] for tool in tools if tool.get("type") == "function"]
    return {
        "type": "object",
        "description": "One internal outer-SDK decision. Tool-call objects are action data, not native tool execution.",
        "properties": {
            "text": {"type": "string", "description": "User-facing final text; keep empty while requesting outer SDK operations. Do not expose tool names here."},
            "tool_calls": {"type": "array", "description": "Internal action requests consumed by the outer SDK. Use declared names here even when user-facing text must hide tool names.", "maxItems": 4, "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": names or ["__no_tools_available__"]},
                    "arguments": {"type": "string", "description": "JSON object string satisfying the selected original tool schema."},
                },
                "required": ["name", "arguments"], "additionalProperties": False,
            }},
        },
        "required": ["text", "tool_calls"], "additionalProperties": False,
    }


def response_body(request: dict, value: dict, model: str) -> dict:
    tools = {tool["name"]: tool for tool in request.get("tools", []) if tool.get("type") == "function"}
    jsonschema.Draft202012Validator(decision_schema(request.get("tools", []))).validate(value)
    output = []
    for call in value["tool_calls"]:
        if call["name"] not in tools:
            raise ValueError("The model selected an undeclared upstream tool")
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Function arguments must be a JSON object")
        jsonschema.Draft202012Validator(tools[call["name"]]["parameters"]).validate(arguments)
        identifier = uuid.uuid4().hex
        output.append({
            "type": "function_call", "id": "fc_" + identifier, "call_id": "call_" + identifier,
            "name": call["name"], "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False), "status": "completed",
        })
    if value["text"]:
        output.append({"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": value["text"], "annotations": []}]})
    if not output:
        raise ValueError("The model returned neither assistant text nor a function call")
    return {
        "id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
        "status": "completed", "model": model, "output": output, "usage": None,
        "error": None, "incomplete_details": None, "parallel_tool_calls": True, "tool_choice": "auto",
        "tools": request.get("tools", []), "instructions": request.get("instructions"),
    }


class ResponsesTransport(httpx.AsyncBaseTransport):
    def __init__(self, host):
        self.host = host
        self.wire_receipts = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.host != "gptgrep-codex.invalid" or request.url.path != "/v1/responses":
            raise RuntimeError("Local transport refuses every non-Responses destination")
        body = json.loads(request.content)
        if body.get("stream"):
            raise RuntimeError("The baseline adapter supports non-streaming Responses only")
        if body.get("model") != self.host.model:
            raise RuntimeError("The baseline request changed the selected chat model")
        selected_model = self.host.model
        wire = {"phase": self.host.phase, "requested_service_tier": getattr(self.host, "service_tier", "fast"), "request_sha256": sha(body),
                "instructions_sha256": sha(body.get("instructions")), "input_sha256": sha(body.get("input")),
                "tool_schemas_sha256": sha(body.get("tools")), "request_bytes": len(request.content)}
        try:
            instructions, state, schema, provenance = decision_request(body)
            wire.update(provenance)
            value, report = await self.host.acomplete(instructions, state, schema)
            response = response_body(body, value, report.get("model", selected_model))
            wire.update(status="completed", output_sha256=sha(response["output"]),
                        effective_service_tier=report.get("effective_service_tier"),
                        index_context=index_context_observation(body.get("input", [])),
                        assistant_tool_calls=sum(item["type"] == "function_call" for item in response["output"]))
            self.wire_receipts.append(wire)
            return httpx.Response(200, json=response, request=request)
        except asyncio.CancelledError:
            wire.update(status="interrupted", error_code="host_cancelled")
            self.wire_receipts.append(wire)
            raise
        except (AdapterError, ValueError, jsonschema.ValidationError) as error:
            wire.update(status="failed", error_code=getattr(error, "code", type(error).__name__))
            self.wire_receipts.append(wire)
            return httpx.Response(getattr(error, "status_code", 400),
                                  json={"error": {"message": str(error), "type": "local_adapter_error",
                                                  "code": getattr(error, "code", "invalid_decision")}}, request=request)


def chat_backend(transport: ResponsesTransport) -> dict:
    return {"api_key": "public-local-transport-sentinel", "base_url": "http://gptgrep-codex.invalid/v1",
            "http_client": httpx.AsyncClient(transport=transport), "max_retries": 0}
