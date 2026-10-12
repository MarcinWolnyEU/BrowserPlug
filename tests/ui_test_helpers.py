"""Client-side helpers for the extension's `test.*` wire actions.

They lived on `BrowserPlugServer` until the server stopped carrying test-only code; the
server has no caller for them. Wrap a server (or a double that has `send_request_and_wait`)
in `UiTestHelpers` and await the same methods.
"""

from datetime import datetime
from typing import Any, Dict


class UiTestHelpers:
    def __init__(self, server):
        self.server = server

    async def _call(self, tag: str, action: str, data: Dict[str, Any], timeout: float):
        request = {
            "id": f"test_{tag}_{datetime.now().isoformat()}",
            "type": "request",
            "action": action,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        response = await self.server.send_request_and_wait(request, timeout)
        if isinstance(response, dict) and "data" in response:
            return response["data"]
        return response

    async def get_popup_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """The popup's current display state."""
        return await self._call("popup", "test.get_popup_state", {}, timeout)

    async def get_options_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """The options page's current display state."""
        return await self._call("options", "test.get_options_state", {}, timeout)

    async def get_storage_values(self, timeout: float = 30.0) -> Dict[str, Any]:
        """The raw storage.sync values."""
        return await self._call("storage", "test.get_storage_values", {}, timeout)

    async def validate_ui_sync(self, expected_values: Dict[str, Any],
                               timeout: float = 30.0) -> Dict[str, Any]:
        """Check that the UI and storage agree with `expected_values`."""
        return await self._call("validate", "test.validate_ui_sync",
                                {"expectedValues": expected_values}, timeout)

    async def refresh_ui_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Ask the extension to re-read its UI state."""
        return await self._call("refresh", "test.refresh_ui_state", {}, timeout)

    async def test_storage_sync_workflow(self, test_values: Dict[str, Any],
                                         timeout: float = 30.0) -> Dict[str, Any]:
        """Read storage, popup and options state, then validate them against `test_values`."""
        results = {"workflow_success": False, "steps": {}, "errors": []}
        try:
            steps = (
                ("initial_storage", "initial storage", self.get_storage_values, ()),
                ("popup_state", "popup state", self.get_popup_state, ()),
                ("options_state", "options state", self.get_options_state, ()),
                ("validation", "validate UI sync", self.validate_ui_sync, (test_values,)),
            )
            for key, label, method, args in steps:
                step = await method(*args, timeout)
                if "error" in step:
                    results["errors"].append(f"Failed to get {label}: {step['error']}"
                                             if key != "validation"
                                             else f"Failed to {label}: {step['error']}")
                    return results
                results["steps"][key] = step
            check = results["steps"]["validation"]
            if check.get("popupSyncValid") and check.get("optionsSyncValid") \
                    and check.get("storageMatches"):
                results["workflow_success"] = True
            else:
                results["errors"].extend(check.get("issues", []))
            return results
        except Exception as e:
            results["errors"].append(f"Workflow exception: {str(e)}")
            return results
