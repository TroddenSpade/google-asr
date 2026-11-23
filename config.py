"""Model switches.

1 enables a model, 0 disables it. Disabled models are not registered in the
API and are not loaded at startup. Restart the service after changing this
file.

Current state: only tinyasr (local CPU) is enabled.
"""

MODEL_FLAGS = {
    "tinyasr": 1,
    "nemotron": 0,
    "google": 0,
    "browser": 0,
}
