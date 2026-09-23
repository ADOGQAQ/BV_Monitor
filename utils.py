import re
from collections.abc import Iterable

BV_PATTERN = re.compile(r"BV[a-zA-Z0-9]{10}")
BV_URL_TEMPLATE = "https://www.bilibili.com/video/{}"

def extract_bv(text: str) -> str:
    """从文本中提取 BV 号，找不到返回 None"""
    m = BV_PATTERN.search(text)
    if m:
        return m.group(0)
    # 去除干扰字符
    idx = text.find("BV")
    if idx >= 0:
        clean = re.sub(r"[^a-zA-Z0-9]", "", text[idx:idx+30])
        m = BV_PATTERN.search(clean)
        if m:
            return m.group(0)
    return None


def match_blacklist(title: str, tags: Iterable[str], blacklist: Iterable[str]) -> list[str]:
    """Return configured keywords found in a video title or one of its tags."""
    haystacks = [str(title).casefold(), *(str(tag).casefold() for tag in tags)]
    matches = []
    seen = set()
    for configured_keyword in blacklist:
        keyword = str(configured_keyword).strip()
        normalized = keyword.casefold()
        if not normalized or normalized in seen:
            continue
        if any(normalized in value for value in haystacks):
            matches.append(keyword)
            seen.add(normalized)
    return matches
