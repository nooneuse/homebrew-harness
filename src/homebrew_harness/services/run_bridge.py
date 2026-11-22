"""A host-bound development Lead receives only its owning Work's tools."""

from .remote import RemoteService
from .model_work_tools import ModelWorkTools


class RunWorkBridge:
    def __init__(self, control, root, service_id, work_id):
        self.control, self.work_id = control, work_id
        self.remote = RemoteService.from_root(root, control=True)
        if self.remote.id != service_id:
            self.remote.close()
            raise ValueError("Service binding targets another owner.")
        self.work = ModelWorkTools(self, work_id)

    def check(self):
        row = self.remote.query("work_get", {"id": self.work_id})
        if row.get("run_id") != self.control.id:
            raise ValueError("This Run is not the current execution of its bound Work.")

    def query(self, method, payload):
        self.check()
        return self.remote.query(method, payload)

    def command(self, method, payload, request_id=None):
        self.check()
        return self.remote.command(method, payload, request_id)

    def handlers(self):
        return self.work.tools()

    def describe_tools(self, definitions):
        return definitions

    def close(self):
        self.remote.close()


def bind(control):
    from homebrew_harness.runs.work_progress import context_for

    value = context_for(control)
    if (
        not value.get("control_root")
        or not value.get("service_id")
        or not value.get("work_id")
    ):
        return None
    return RunWorkBridge(
        control, value["control_root"], value["service_id"], value["work_id"]
    )
