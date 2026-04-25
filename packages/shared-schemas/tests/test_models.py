from ecom_shared import HealthResponse, ServiceName


def test_health_response_serializes_service_name() -> None:
    payload = HealthResponse(service=ServiceName.API, status="ok", version="0.1.0")

    assert payload.model_dump(mode="json") == {
        "service": "api",
        "status": "ok",
        "version": "0.1.0",
    }

