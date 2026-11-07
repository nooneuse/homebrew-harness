"""An input generation captured by a model proposal and checked at acceptance."""

from homebrew_harness.storage.control_boundary import check_guard


class Proposal:
    def __init__(self, store, recipients, *, version=None):
        self.store, self.recipients, self.version = store, recipients, version
        self.token = store.input_guard(recipients)
        self.local_version = version() if version else None

    def __call__(self):
        return (
            self.token != self.store.input_guard(self.recipients)
            or self.version is not None
            and self.version() != self.local_version
        )

    def accept(self):
        db = self.store.backend()
        with db.write() as conn:
            check_guard(db, conn, self.token)
