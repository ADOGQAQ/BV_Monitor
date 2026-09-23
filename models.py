
from dataclasses import dataclass
from enum import IntEnum

class UserVipLevel(IntEnum):
    Normal = 0
    VIP1 = 3
    VIP2 = 2
    VIP3 = 1

@dataclass
class UserInfo:
    uname: str
    uid: int
    vip_level: UserVipLevel
    face: str = ""
