"""Wait for an accepted detached service's authenticated control API."""

from homebrew_harness.api.types import SDKError
from tests.runtime.test_interaction import eventually


def ready_service(service):
    def ready():
        try:
            return service.client.call(service.id, "snapshot")
        except SDKError:
            return None

    eventually(ready, 20)
    return service
