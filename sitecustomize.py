"""
Project-wide interpreter bootstrap.

Python automatically imports this module (if present on sys.path) after the
standard `site` initialisation. We leverage it to ensure compatibility patches
are applied in every process, including LiveKit worker subprocesses spawned via
`multiprocessing`.
"""

from voice_agent.compat import bootstrap

bootstrap()

