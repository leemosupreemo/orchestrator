"""Known build-environment problems, recognised from tool output.

Used after a build fails (to say what is wrong with the machine rather than the code) and before one starts
(the preflight runs the same recognisers over cheap probe commands).
"""
from __future__ import annotations


def detect_simulator_environment_issue(output: str) -> str | None:
    if "There is no XCFramework found at" in output and ".swiftpm/artifacts" in output:
        return (
            "Stale or incomplete SwiftPM binary artifact cache: Xcode is resolving package "
            "XCFrameworks from missing .swiftpm/artifacts paths. Clear the worker's DerivedData "
            "and .swiftpm package cache, then re-resolve package dependencies on the worker machine."
        )

    if "CoreSimulator is out of date" in output:
        return (
            "Xcode/CoreSimulator mismatch: CoreSimulator is out of date for the selected Xcode. "
            "Restart the machine or CoreSimulator services, then reopen Xcode. If the issue persists, "
            "finish installing Xcode components."
        )

    if "iOS 26.5 is not installed" in output or "Please download and install the platform from Xcode > Settings > Components" in output:
        return (
            "Missing iOS simulator platform: install the required iOS platform from "
            "Xcode > Settings > Components on the worker machine."
        )

    if "Unable to find a device matching the provided destination specifier" in output and "no available devices matched" in output:
        return (
            "Unavailable simulator destination: the requested simulator/device is not available on this worker. "
            "Install the required runtime or select an available simulator destination."
        )

    return None
