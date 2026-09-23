from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

BILIBILI_API_BASE = "https://api.bilibili.com"
VIDEO_VIEW_PATH = "/x/web-interface/view"
VIDEO_TAGS_PATH = "/x/tag/archive/tags"


class BilibiliAPIError(RuntimeError):
    """Bilibili returned an error or an invalid response."""


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    tags: tuple[str, ...]
    view: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    tag_error: str | None = None


class BilibiliClient:
    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._cache: dict[str, VideoMetadata] = {}
        self._cache_lock = threading.Lock()

    def fetch_video_metadata(self, bv: str) -> VideoMetadata:
        with self._cache_lock:
            cached = self._cache.get(bv)
        if cached is not None:
            return cached

        view_data = self._request(VIDEO_VIEW_PATH, bvid=bv)
        if not isinstance(view_data, dict):
            raise BilibiliAPIError("视频信息格式无效")
        title = view_data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise BilibiliAPIError("视频信息中没有标题")

        # 提取视频数据统计
        stat = view_data.get("stat") or {}
        view_count = int(stat.get("view", 0) or 0) if isinstance(stat, dict) else 0
        like_count = int(stat.get("like", 0) or 0) if isinstance(stat, dict) else 0
        coin_count = int(stat.get("coin", 0) or 0) if isinstance(stat, dict) else 0
        favorite_count = int(stat.get("favorite", 0) or 0) if isinstance(stat, dict) else 0

        tag_error = None
        tags: tuple[str, ...] = ()
        try:
            tag_data = self._request(VIDEO_TAGS_PATH, bvid=bv)
            if not isinstance(tag_data, list):
                raise BilibiliAPIError("视频标签格式无效")
            tags = tuple(
                name.strip()
                for item in tag_data
                if isinstance(item, dict)
                and isinstance((name := item.get("tag_name")), str)
                and name.strip()
            )
        except BilibiliAPIError as exc:
            tag_error = str(exc)
            logger.warning("获取 %s 的标签失败: %s", bv, exc)

        metadata = VideoMetadata(
            title=title.strip(),
            tags=tags,
            view=view_count,
            like=like_count,
            coin=coin_count,
            favorite=favorite_count,
            tag_error=tag_error,
        )
        if tag_error is None:
            with self._cache_lock:
                self._cache[bv] = metadata
        return metadata

    def _request(self, path: str, **params):
        url = f"{BILIBILI_API_BASE}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0 SCMonitor/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise BilibiliAPIError(f"B站 API 返回 HTTP {exc.code}") from exc
        except URLError as exc:
            raise BilibiliAPIError(f"无法连接 B站 API: {exc.reason}") from exc
        except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise BilibiliAPIError(f"B站 API 请求失败: {exc}") from exc

        if not isinstance(payload, dict):
            raise BilibiliAPIError("B站 API 响应格式无效")
        if payload.get("code") != 0:
            message = payload.get("message") or "未知错误"
            raise BilibiliAPIError(f"B站 API 返回错误: {message}")
        return payload.get("data")
