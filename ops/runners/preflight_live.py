import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from src.core.config import Settings
from src.core.llm_gateway import BudgetExceeded, CycleUsage, LLMGateway

BITRIX_METHODS = (
    "imbot.message.add",
    "imopenlines.bot.session.operator",
    "crm.timeline.comment.add",
    "imopenlines.session.history.get",
    "crm.timeline.comment.list",
)


def diagnostic(
    client: httpx.Client, method: str, url: str, **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    try:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        return f"http_{error.response.status_code}", {}
    except (httpx.HTTPError, ValueError) as error:
        return type(error).__name__, {}
    if not isinstance(payload, dict) or "error" in payload:
        return "unexpected_api_response", {}
    return "ok", payload


def check(settings: Settings, client: httpx.Client) -> dict[str, Any]:
    roles = {
        "FAST": settings.fast_llm_model,
        "SMART": settings.smart_llm_model,
        "EMBEDDING": settings.embedding_model,
    }
    rates = {
        name: "missing" if getattr(settings, name) is None else "configured"
        for name in (
            "llm_rub_per_million_input_tokens",
            "llm_rub_per_million_output_tokens",
            "embedding_rub_per_million_input_tokens",
        )
    }
    cycle, session = settings.max_cycle_cost_rub, settings.max_session_cost_rub
    limits_valid = (
        min(cycle, session) > 0 or all(getattr(settings, name) == 0 for name in rates)
    )
    gateway, payload = diagnostic(
        client, "GET", settings.base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
    )
    catalog = payload.get("data")
    if gateway == "ok" and not isinstance(catalog, list):
        gateway = "unexpected_model_catalog"
    models = {
        item["id"] for item in catalog if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(catalog, list) else set()
    role_status = {
        role: ("listed" if model in models else "not_listed") if gateway == "ok" else "unchecked"
        for role, model in roles.items()
    }

    portal = "webhook_missing"
    methods = dict.fromkeys(BITRIX_METHODS, "unchecked")
    if settings.bitrix_webhook_url:
        url = settings.bitrix_webhook_url.get_secret_value().rstrip("/")
        if url.startswith("https://"):
            portal, scope = diagnostic(client, "POST", url + "/scope.json", json={})
            if portal == "ok" and not isinstance(scope.get("result"), list):
                portal = "unexpected_scope_response"
            if portal == "ok":
                for name in methods:
                    status, payload = diagnostic(
                        client, "POST", url + "/method.get.json", json={"name": name},
                    )
                    result = payload.get("result")
                    if status == "ok":
                        if not isinstance(result, dict) or type(result.get("isAvailable")) is not bool:
                            status = "unexpected_method_status"
                        else:
                            status = "available" if result["isAvailable"] else "unavailable"
                    methods[name] = status
        else:
            portal = "https_required"
    credentials = {
        name: "configured" if getattr(settings, name) else "missing"
        for name in ("bitrix_bot_id", "bitrix_client_id", "bitrix_app_token")
    }
    return {
        "llm_ready": gateway == "ok" and all(model in models for model in roles.values())
        and limits_valid and all(value == "configured" for value in rates.values()),
        "llm_catalog": gateway,
        "models": role_status,
        "rub_per_million_token_rates": rates,
        "budget": {"valid": limits_valid, "cycle_rub": str(cycle), "session_rub": str(session)},
        "bitrix_ready": portal == "ok" and all(status == "available" for status in methods.values())
        and all(value == "configured" for value in credentials.values()),
        "bitrix_portal": portal,
        "bitrix_methods": methods,
        "bitrix_openlines_credentials": credentials,
    }


async def check_model_calls(settings: Settings) -> dict[str, Any]:
    usage = CycleUsage(spending_limit=settings.max_session_cost_rub)
    chat_roles: tuple[Literal["fast", "smart"], ...] = ("fast", "smart")
    statuses = dict.fromkeys(("EMBEDDING", "FAST", "SMART"), "unchecked")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        gateway = LLMGateway(settings.model_copy(update={"llm_mode": "live"}), client)
        active_role = "EMBEDDING"
        try:
            await gateway.embed("Case 04 preflight", usage)
            statuses[active_role] = "ok"
            for role in chat_roles:
                active_role = role.upper()
                await gateway.evaluate('Return {"ok": true} as JSON.', "Preflight", usage, model=role)
                statuses[active_role] = "ok"
        except BudgetExceeded:
            statuses[active_role] = "budget_exceeded"
        except httpx.HTTPStatusError as error:
            statuses[active_role] = f"http_{error.response.status_code}"
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            statuses[active_role] = type(error).__name__
    return {"roles": statuses, "cost_rub": str(usage.cost_rub) if usage.cost_complete else None}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only live configuration diagnostics")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings()
    except (ValidationError, ValueError):
        report: dict[str, Any] = {"settings": "invalid", "llm_ready": False, "bitrix_ready": False}
    else:
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            report = check(settings, client)
        if report["llm_ready"]:
            probes = asyncio.run(check_model_calls(settings))
            report["model_probes"] = probes
            report["llm_ready"] = all(status == "ok" for status in probes["roles"].values())
        else:
            report["model_probes"] = "blocked"
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    raise SystemExit(0 if report["llm_ready"] and report["bitrix_ready"] else 1)


if __name__ == "__main__":
    main()
