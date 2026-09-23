import json
import logging
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

DPSK_API_URL = "https://api.deepseek.com/chat/completions"
DPSK_MODELS_URL = "https://api.deepseek.com/models"
DPSK_MODEL = "deepseek-v4-flash"
DPSK_MAX_CONCURRENCY = 16
DPSK_MAX_429_RETRIES = 3
DPSK_PROMPT = """你是一个严格的 Steam ID 提取器。输入是一条直播 SC 文本，请找出用户希望主播添加的 Steam 昵称或 ID。

提取规则：
1. 只输出提取到的 Steam ID 原文，不要解释、不要加标签、不要加引号；找不到时只输出「无」。
2. Steam ID 可能包含中文、英文、日文、数字、空格、标点、特殊符号或 emoji，必须保留其原始写法。
3. 纯数字是 Steam 好友码，不是本任务要的 Steam ID，绝不能输出。数字好友码和昵称同时出现时，忽略数字，只输出昵称。
4. 「ID」「id」「名字」「好友位」「坑位」「求加」「查查我的」等词附近可能出现目标。ID 与昵称之间不一定有冒号、空格或其他分隔符。
5. 准确判断昵称边界，不要把后续的「关注多年」「感谢」「老粉」「张哥」等自然语言拼进 ID。
6. 如果整条文本仅由一个看起来像昵称的短语组成，可以把整条文本作为 ID；普通聊天、请求句或只有 ID 标签但没有内容时输出「无」。

示例：
输入：id：naogenggeng刚刚完美错过我了5555
输出：naogenggeng
输入：ID：依然鹏殇 关注好多年了，老张活久点
输出：依然鹏殇
输入：id：his Theme 代码195042680张哥张哥 求生老粉
输出：his Theme
输入：蹭个好友位。ID张无忌。数字ID91430803
输出：张无忌
输入：张哥求好友位名字冷 冽谷の安和 昴
输出：冷 冽谷の安和 昴
输入：求个好友 325533107 Ingrid，老粉啦
输出：Ingrid
输入：张哥查查我的
输出：无
输入：顺便求个好友ID：
输出：无"""


class DPSKError(RuntimeError):
    """DeepSeek 请求或响应无效。"""


def normalize_steam_id(content: str | None) -> str | None:
    """清理模型输出；「无」和纯数字好友码都视为未找到。"""
    if not isinstance(content, str):
        return None

    value = content.strip().strip("`\"'“”‘’ ")
    value = re.sub(r"^(?:steam\s*id|steamid)\s*[:：]\s*", "", value, flags=re.I)
    value = value.strip().strip("`\"'“”‘’ ")
    if "\n" in value:
        value = value.splitlines()[0].strip()
    normalized_marker = value.rstrip("。.!！ ")

    if not normalized_marker or normalized_marker.lower() in {"无", "没有", "未找到", "none", "-"}:
        return None
    if normalized_marker.isdigit():
        return None
    if len(normalized_marker) > 128:
        return None
    return normalized_marker


class DPSKClient:
    def __init__(
        self,
        api_token: str,
        timeout: float = 20.0,
        max_concurrency: int = DPSK_MAX_CONCURRENCY,
        max_429_retries: int = DPSK_MAX_429_RETRIES,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须大于 0")
        if max_429_retries < 0:
            raise ValueError("max_429_retries 不能小于 0")
        self._api_token = api_token.strip()
        self._timeout = timeout
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._max_429_retries = max_429_retries

    def validate_token(self) -> bool:
        """Validate credentials without consuming completion tokens.

        False means the token is absent or DeepSeek explicitly returned HTTP 401.
        Connectivity and service failures remain errors so callers do not mislabel
        a temporarily unreachable API as an invalid credential.
        """
        if not self._api_token:
            return False

        request = Request(
            DPSK_MODELS_URL,
            headers={"Authorization": f"Bearer {self._api_token}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401:
                return False
            raise DPSKError(f"DeepSeek API 返回 HTTP {code}") from exc
        except URLError as exc:
            raise DPSKError(f"无法连接 DeepSeek API: {exc.reason}") from exc
        except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise DPSKError(f"DeepSeek API 请求失败: {exc}") from exc

        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise DPSKError("DeepSeek API 模型列表响应格式无效")
        return True

    def extract_steam_id(self, text: str) -> str | None:
        if not self._api_token:
            raise DPSKError("未配置 DPSK_API_TOKEN")

        payload = {
            "model": DPSK_MODEL,
            "messages": [
                {"role": "system", "content": DPSK_PROMPT},
                {"role": "user", "content": str(text)},
            ],
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "temperature": 0,
            "max_tokens": 1024,
            "stream": False,
        }
        request = Request(
            DPSK_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with self._semaphore:
            data = self._send_request(request)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DPSKError("DeepSeek API 响应格式无效") from exc
        return normalize_steam_id(content)

    def _send_request(self, request: Request):
        for attempt in range(self._max_429_retries + 1):
            try:
                with urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code != 429 or attempt >= self._max_429_retries:
                    exc.close()
                    raise DPSKError(f"DeepSeek API 返回 HTTP {exc.code}") from exc
                delay = self._get_retry_delay(exc, attempt)
                exc.close()
                logger.warning(
                    "DeepSeek API 返回 429，%.2f 秒后进行第 %s/%s 次重试",
                    delay,
                    attempt + 1,
                    self._max_429_retries,
                )
                time.sleep(delay)
            except URLError as exc:
                raise DPSKError(f"无法连接 DeepSeek API: {exc.reason}") from exc
            except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
                raise DPSKError(f"DeepSeek API 请求失败: {exc}") from exc
        raise DPSKError("DeepSeek API 请求失败")

    @staticmethod
    def _get_retry_delay(exc: HTTPError, attempt: int) -> float:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return min(max(float(retry_after), 0.0), 60.0)
        except (TypeError, ValueError):
            return min(2 ** attempt, 60.0)
