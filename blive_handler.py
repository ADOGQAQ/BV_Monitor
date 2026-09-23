import asyncio
from enum import IntEnum
import http.cookies
import logging
import os
import random
import time

import aiohttp
import blivedm
from blivedm.models.web import DanmakuMessage, SuperChatMessage

from models import UserInfo, UserVipLevel

logger = logging.getLogger(__name__)

DEBUG_FAKE_PRICES = (2, 30, 50, 100)
DEBUG_FAKE_USERS = (
    "调试_阿木木",
    "调试_男搓背",
    "调试_KurikoMoe",
    "调试_Ebe",
    "调试_EdmundDZhang",
)
DEBUG_FALLBACK_MESSAGES = (
    "BV1hXuV62Eg4",
    "BV1NoNN6MEse",
    "id：naogenggeng刚刚完美错过我了5555",
    "从盘丝洞开始看的，求个好友位：Scene",
    "张哥想要检核盖章，谢谢张哥，ID：桑果君",
    "张哥求个好友位，代码：272960172. id：你是一个个",
    "ID：依然鹏殇 关注好多年了，老张活久点",
    "张哥查查我的",
)


def _is_debug_fake_source_enabled():
    return os.getenv("DEBUG", "").strip().lower() == "on"



BaseHandler = blivedm.BaseHandler if blivedm else object
class SCHandler(BaseHandler): # type: ignore
    def __init__(self, app):
        super().__init__()
        self._app = app

    def _on_heartbeat(self, client, message):
        self._app.root.after(0, lambda: self._app.on_danmaku_heartbeat())
        pass

    def _on_super_chat(self, client, message: SuperChatMessage):
        user = UserInfo(
            uname=message.uname,
            uid=message.uid,
            vip_level=UserVipLevel(message.guard_level),
            face=message.face or "",
        )

        self._app.add_sc(
            user=user,
            price=message.price,
            message=message.message,
            timestamp=message.start_time,
        )

    def _on_danmaku(self, client, message: DanmakuMessage):
        # logger.info(f"收到弹幕: {message.uname}({message.uid}): {message.msg}")
        user = UserInfo(
            uname=message.uname,
            uid=message.uid,
            vip_level=UserVipLevel(message.privilege_type),
            face=message.face or "",
        )

        self._app.add_danmaku(
            user=user,
            message=message.msg,
            timestamp=int(time.time()),
        )


async def _run_fake_blivedm(app, config):
    debug_messages = DEBUG_FALLBACK_MESSAGES
    debug_user_uids = {
        uname: -(position + 1)
        for position, uname in enumerate(DEBUG_FAKE_USERS)
    }
    sc_store = getattr(app, "_sc_store", None)
    if sc_store is not None:
        repaired_count = 0
        try:
            for uname, uid in debug_user_uids.items():
                repaired_count += sc_store.reassign_user_uid_for_nickname(uname, uid)
            if repaired_count:
                logger.info("已修复 %s 条旧 DEBUG 投稿记录的用户 UID", repaired_count)
        except Exception as exc:
            logger.warning("修复旧 DEBUG 投稿记录失败: %s", exc)
    logger.info("DEBUG=on，使用 %s 条 SC 测试样例", len(debug_messages))
    app.root.after(0, lambda: app.set_status("DEBUG 假数据源将在 1 秒后开始..."))

    try:
        await asyncio.sleep(1)
        index = 1
        while True:
            price = random.choice(DEBUG_FAKE_PRICES)
            uname = random.choice(DEBUG_FAKE_USERS)
            message = debug_messages[(index - 1) % len(debug_messages)]
            timestamp = int(time.time())
            user = UserInfo(
                uname=uname,
                uid=debug_user_uids[uname],
                vip_level=UserVipLevel.Normal,
            )

            app.root.after(0, lambda: app.on_danmaku_heartbeat())
            app.add_sc(
                user=user,
                price=price,
                message=message,
                timestamp=timestamp,
            )

            index += 1
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass


async def run_blivedm(app, config):
    if _is_debug_fake_source_enabled():
        await _run_fake_blivedm(app, config)
        return

    if aiohttp is None or blivedm is None:
        logger.error("缺少 blivedm/aiohttp 依赖，无法连接真实直播数据源")
        app.root.after(0, lambda: app.set_status("❌ 缺少 blivedm/aiohttp 依赖"))
        return

    cookies = http.cookies.SimpleCookie()
    if config.sessdata:
        cookies["SESSDATA"] = config.sessdata
        cookies["SESSDATA"]["domain"] = "bilibili.com"

    async with aiohttp.ClientSession() as session:
        if config.sessdata:
            session.cookie_jar.update_cookies(cookies)

        client = blivedm.BLiveClient(config.room_id, session=session)
        client.set_handler(SCHandler(app))

        try:
            logger.info(f"正在连接直播间 {config.room_id} ...")
            app.root.after(0, lambda: app.set_status(f"🔗 连接中 {config.room_id}..."))
            client.start()
            await client.join()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"连接异常: {e}")
            app.root.after(0, lambda: app.set_status("❌ 连接失败，请检查网络或 Cookie"))
        finally:
            await client.stop_and_close()
            logger.info("直播客户端已关闭")
