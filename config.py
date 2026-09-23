import json
import os
import logging

from pydantic import BaseModel, ConfigDict, Field

from gui import WINDOW_HEIGHT_DEFAULT, WINDOW_WIDTH_DEFAULT

logger = logging.getLogger(__name__)

CONFIG_FILE = "sc_config.json"


class AppConfig(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, extra="ignore")

    room_id: int = Field(default=5050, alias="直播间ID")
    sessdata: str = Field(default="", alias="SESSDATA")

    # 配置
    display_mode: str = Field(default="all", alias="显示模式")  # all=所有弹幕, sc_only=仅SC, vip=仅VIP弹幕
    only_show_bv: bool = Field(default=False, alias="仅显示有BV号")
    special_users: list[str] = Field(
        default_factory=list,
        alias="特殊用户名关键词",
    )
    special_users_uid: list[int] = Field(
        default_factory=lambda: [1710289235],
        alias="特殊用户UID",
    )
    blacklist: list[str] = Field(default_factory=list, alias="黑名单关键词")
    blacklist_uid: list[int] = Field(default_factory=list, alias="黑名单UID")
    blacklist_users: dict = Field(default_factory=dict, alias="黑名单用户信息")  # {uid: {"uname": "", "face": ""}}
    window_width: int = Field(default=WINDOW_WIDTH_DEFAULT, alias="窗口宽度")
    window_height: int = Field(default=WINDOW_HEIGHT_DEFAULT, alias="窗口高度")

    # 显示控制
    show_user_vip1: bool = Field(default=False, alias="显示舰长弹幕")
    color_user_vip1: str = Field(default="#B8860B", alias="颜色_舰长弹幕")

    show_user_vip2: bool = Field(default=False, alias="显示提督弹幕")
    color_user_vip2: str = Field(default="#E651FF", alias="颜色_提督弹幕")

    show_user_vip3: bool = Field(default=False, alias="显示总督弹幕")
    color_user_vip3: str = Field(default="#D32F2F", alias="颜色_总督弹幕")

    color_bg: str = Field(default="#F0FFF4")
    color_card: str = Field(default="#FAFFFB")
    color_bar: str = Field(default="#E8F5E9")
    color_main: str = Field(default="#4CAF50")
    color_text: str = Field(default="#2E7D32")

    @classmethod
    def load(cls) -> "AppConfig":
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cfg = cls.model_validate(data)
                logger.info("配置文件加载成功")
                return cfg
            except Exception as e:
                logger.error(f"配置文件解析失败: {e}")
        else:
            logger.warning("未找到配置文件，将使用空 SESSDATA")
        return cls()

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                self.model_dump(by_alias=True),
                f,
                indent=2,
                ensure_ascii=False,
            )
        logger.info("配置文件已保存")
