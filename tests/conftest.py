"""テスト共通: Redis のモック (pipeline 対応)"""
from unittest.mock import AsyncMock, MagicMock


class FakePipeline:
    """redis.asyncio の Pipeline の代わり。積んだコマンドを execute() で元の AsyncMock に順に流す"""

    def __init__(self, redis_mock):
        self._r = redis_mock
        self._calls = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return self
        return queue

    async def execute(self, raise_on_error=True):
        out = []
        calls, self._calls = self._calls, []
        for name, args, kwargs in calls:
            try:
                out.append(await getattr(self._r, name)(*args, **kwargs))
            except Exception as e:
                if raise_on_error:
                    raise
                out.append(e)
        return out

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_redis() -> AsyncMock:
    """AsyncMock の Redis に pipeline() を足したもの"""
    r = AsyncMock()
    r.pipeline = MagicMock(side_effect=lambda *a, **k: FakePipeline(r))
    return r
