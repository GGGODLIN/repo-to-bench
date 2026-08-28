from __future__ import annotations

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier


class ExitCodeVerifier(BaseVerifier):
  async def verify(self) -> VerifierResult:
    timeout_sec = max(1, int(self.task.config.verifier.timeout_sec))
    app_probe = await self.environment.exec(
      command="test -d /app && test ! -L /app",
      cwd="/",
      timeout_sec=timeout_sec,
    )
    if app_probe.return_code != 0:
      return VerifierResult(rewards={"reward": 0})

    test_result = await self.environment.exec(
      command="/tests/test.sh",
      cwd="/app",
      timeout_sec=timeout_sec,
    )
    return VerifierResult(
      rewards={"reward": 1 if test_result.return_code == 0 else 0}
    )
