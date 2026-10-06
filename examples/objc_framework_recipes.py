"""Two read-only Objective-C bridge recipes for use inside Pyto on iOS.

This is a focused bridge primer, not a catalogue of every framework Pyto imports.
See Pyto's Objective-C guide and Rubicon-ObjC's selector mapping before adding a new
class or selector. The recipes read the current app bundle and device properties only.
"""


def app_bundle_path() -> str:
    """Return the main app bundle path using Pyto's documented Foundation example."""
    from Foundation import NSBundle

    return str(NSBundle.mainBundle.bundleURL.path)


def device_summary() -> dict:
    """Read model and iOS version through UIKit's UIDevice Objective-C class."""
    from UIKit import UIDevice

    current_device = UIDevice.currentDevice
    device = current_device() if callable(current_device) else current_device
    return {
        "model": str(device.model),
        "system": str(device.systemName),
        "version": str(device.systemVersion),
    }


def main() -> None:
    try:
        bundle = app_bundle_path()
        device = device_summary()
    except (ImportError, AttributeError) as exc:
        print(
            "Objective-C recipe unavailable in this runtime: {}: {}. "
            "This check does not establish whether another Pyto build exposes the API.".format(
                type(exc).__name__, exc
            )
        )
        return

    print("Main app bundle: {}".format(bundle))
    print(
        "Device: {} · {} {}".format(
            device["model"], device["system"], device["version"]
        )
    )


if __name__ == "__main__":
    main()
