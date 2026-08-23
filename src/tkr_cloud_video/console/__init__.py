"""Local operator console for submitting generations and playing results back.

The console is a loopback-only tool that runs on an operator's own machine. It
holds the RunPod and delivery credentials so a browser never has to, runs the
same intake preflight the worker runs so a malformed request is refused in
milliseconds rather than after a cold start, and reaches a finished video only
through the reviewed short-lived signed link.
"""
