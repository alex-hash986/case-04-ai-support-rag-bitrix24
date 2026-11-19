import json
from uuid import uuid4

import httpx

from src.core.config import Settings
from src.domain.schemas import ChatMessageIn, MetricGroup, SupportDecisionOut
from src.ui.presets import PRESETS


def main() -> None:
    config = Settings()
    session = uuid4().hex
    with httpx.Client(base_url=f"http://127.0.0.1:{config.api_port}", timeout=75) as client:
        health = client.get("/health")
        health.raise_for_status()
        if not health.json()["index_ready"]:
            raise RuntimeError("Knowledge base index is unavailable")
        results = []
        for index, question in enumerate(PRESETS.values(), 1):
            message = ChatMessageIn(
                session_id=session, dialog_id=session, user_id="smoke",
                deal_id=1, channel="web", message_text=question,
            )
            response = client.post(
                "/api/v1/support/message", json=message.model_dump(mode="json"),
                headers={"Idempotency-Key": f"{session}-{index}"},
            )
            response.raise_for_status()
            decision = SupportDecisionOut.model_validate(response.json())
            if decision.need_human != (index > 2) or decision.bitrix_sync_status != "synced":
                raise RuntimeError(f"Unexpected decision for case {index}: {decision.action}")
            results.append(decision.model_dump(mode="json", exclude={"retrieved_chunks", "citations"}))
        response = client.get("/api/v1/metrics")
        response.raise_for_status()
        metrics = [
            MetricGroup.model_validate(row).model_dump(mode="json") for row in response.json()
        ]
    print(json.dumps({"results": results, "metrics": metrics}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
