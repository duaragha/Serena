"""Managed browser-login ownership; no credentials pass through this module."""
from core.workspace_lease import SessionLease


class CodexLoginLease(SessionLease):
    def confirmed_finished(self):
        """Release only after native completion/cancellation, not UI dismissal.

        This lease owns a callback operation inside a still-live app-server, not
        that server's coding session. Crash recovery retains its bound PID.
        """
        if self.closed:
            return
        self.record = {}
        self._save()
        self.release()
