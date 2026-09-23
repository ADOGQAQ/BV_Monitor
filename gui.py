from dataclasses import dataclass
from typing import *
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
import time
import json
import threading
import asyncio
import sys
import os
import queue
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from PIL import Image, ImageTk
import logging

from utils import (
    BV_URL_TEMPLATE,
    BV_PATTERN,
    extract_bv,
    match_blacklist,
)
from blive_handler import run_blivedm
from bilibili_client import BilibiliClient, VideoMetadata
from models import UserInfo, UserVipLevel
from monitor_utils import (
    WorkArea,
    calculate_popup_position,
    get_monitor_work_area,
    position_popup_window,
)
from sc_store import SCStore, UserSCStats

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

WINDOW_WIDTH_DEFAULT = 1085
WINDOW_HEIGHT_DEFAULT = 650
APP_TITLE_NAME = "BV号监听器"
VERSION_ENV_VAR = "SC_MONITOR_VERSION"
VIDEO_METADATA_MAX_CONCURRENCY = 4


def get_app_version():
    return os.environ.get(VERSION_ENV_VAR, "dev")


class SCMonitorApp:
    def __init__(self, root, config):
        self.root = root
        self.config: AppConfig = config

        self._resize_after_id = None
        self.root.bind("<Configure>", self._on_window_configure)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._topmost = False
        self._display_mode_var = tk.StringVar(value=self.config.display_mode)
        self._only_show_bv_var = tk.BooleanVar(value=self.config.only_show_bv)
        self._sc_records = []
        self._alloc_sc_idx = 1
        self._clicked_bv_items = []
        self._selected_item = None
        self._status_var = tk.StringVar(value="等待连接...")
        self._sc_log_file = None
        self._sc_log_path = self._setup_sc_log()
        self._is_closing = False
        self._tip_window = None
        self._tip_after_id = None
        self._hover_window = None
        self._hover_item = None
        self._hover_content = None
        # 头像缓存
        self._avatar_cache: Dict[int, ImageTk.PhotoImage] = {}
        self._avatar_pending: set = set()
        self._avatar_queue = queue.SimpleQueue()
        self._avatar_poll_after_id = None
        self._avatar_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="avatar",
        )
        self._default_avatar = None
        try:
            self._sc_store = SCStore()
            logger.info("SC 数据库: %s", self._sc_store.database_path)
        except Exception as exc:
            self._sc_store = None
            logger.error("初始化 SC 数据库失败: %s", exc)

        self._video_client = BilibiliClient()
        self._video_result_queue = queue.SimpleQueue()
        self._video_poll_after_id = None
        self._video_executor = ThreadPoolExecutor(
            max_workers=VIDEO_METADATA_MAX_CONCURRENCY,
            thread_name_prefix="bilibili",
        )

        self._user_info: Dict[str, UserInfo] = {}

        self._set_icon()
        self._setup_window()
        self._build_ui()
        self._video_poll_after_id = self.root.after(100, self._poll_video_results)
        self._start_blivedm_thread()

    def _setup_sc_log(self):
        try:
            log_dir = Path("data")
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"sclog-{time.strftime('%y-%m-%d-%H-%M')}.jsonl"
            self._sc_log_file = log_path.open("a", encoding="utf-8")
            logger.info(f"SC 日志文件: {log_path}")
            return log_path
        except Exception as e:
            logger.warning(f"创建 SC 日志文件失败: {e}")
            return None

    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else os.path.dirname(__file__)
            icon_path = os.path.join(base, "resources", "favicon.ico")
            if os.path.exists(icon_path):
                img = Image.open(icon_path).resize((32, 32), Image.LANCZOS)
                self.root.iconphoto(True, ImageTk.PhotoImage(img))
        except Exception as e:
            logger.warning(f"设置图标失败: {e}")

    def _setup_window(self):
        self.root.title(APP_TITLE_NAME)

        if self.config.window_width <= 0:
            self.config.window_width = WINDOW_WIDTH_DEFAULT
        if self.config.window_height <= 0:
            self.config.window_height = WINDOW_HEIGHT_DEFAULT
        self.root.geometry(f"{self.config.window_width}x{self.config.window_height}")
        self.root.resizable(True, True)
        self.root.minsize(WINDOW_WIDTH_DEFAULT, WINDOW_HEIGHT_DEFAULT)
        self.root.configure(bg=self.config.color_bg)
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - self.config.window_width) // 2
        y = (self.root.winfo_screenheight() - self.config.window_height) // 2
        self.root.geometry(f"+{x}+{y}")

    def _build_ui(self):
        c = self.config  # 颜色缩写
        bar = tk.Frame(self.root, bg=c.color_bg)
        bar.pack(side=tk.TOP, fill=tk.X, padx=(4, 8), pady=6)

        tk.Label(bar, text=f"🏠 {c.room_id}", font=("微软雅黑", 10, "bold"),
                 fg=c.color_text, bg=c.color_bg, width=15, anchor="w").pack(side=tk.LEFT, padx=(0, 8))

        # 按钮区与房间号之间的间距
        tk.Frame(bar, width=20, bg=c.color_bg).pack(side=tk.LEFT)

        btn_style = {
            "bg": c.color_bar, "fg": c.color_text, "relief": tk.FLAT, "bd": 0,
            "padx": 8, "pady": 4, "font": ("微软雅黑", 9),
            "activebackground": "#C8E6C9", "activeforeground": c.color_text,
            "cursor": "hand2", "anchor": "center",
        }
        # 顶部按钮 - 固定宽度防止点击时位置变化
        self._btn_top = tk.Button(bar, text="📌 置顶", command=self._toggle_top, width=8, **btn_style)
        self._btn_top.pack(side=tk.LEFT, padx=2)

        self._btn_display_mode = tk.Button(
            bar,
            text=self.get_display_mode_text(),
            command=self._cycle_display_mode,
            width=12,
            **btn_style,
        )
        self._btn_display_mode.pack(side=tk.LEFT, padx=2)

        self._btn_only_show_bv = tk.Checkbutton(
            bar,
            text=self.get_only_show_bv_text(),
            variable=self._only_show_bv_var,
            command=self._toggle_only_show_bv,
            indicatoron=False,
            selectcolor=c.color_main,
            width=12,
            **btn_style,
        )
        self._btn_only_show_bv.pack(side=tk.LEFT, padx=2)

        tk.Button(bar, text="🧹 清空", command=self._clear_list, width=6, **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="📍 回到跳转", command=self._goto_last,
                  bg=c.color_main, fg="white", relief=tk.FLAT, bd=0, padx=8, pady=4,
                  font=("微软雅黑", 9, "bold"), activebackground="#388E3C", cursor="hand2",
                  width=10, anchor="center",
                  ).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="🔑 Cookie", command=self._change_cookie, width=9, **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="📱 扫码登录", command=self._qr_login, width=10, **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="🏠 直播间", command=self._change_room, width=9, **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="🚫 黑名单", command=self._show_blacklist_window, width=9, **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="🔤 屏蔽关键词", command=self._show_keyword_blacklist_window, width=11, **btn_style).pack(side=tk.LEFT, padx=2)

        # Treeview 列表
        f = tk.Frame(self.root, bg=c.color_bg)
        f.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        cols = ("type", "time", "price", "msg", "bv")
        self.tree = ttk.Treeview(f, columns=cols, show="tree headings", selectmode="browse")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=c.color_card, fieldbackground=c.color_card,
                        foreground="#424242", rowheight=32, borderwidth=0, font=("微软雅黑", 9))
        style.map("Treeview", background=[("selected", "#C8E6C9")],
                  foreground=[("selected", c.color_text)])
        style.configure("Treeview.Heading", background=c.color_bar, foreground=c.color_text,
                        relief="flat", borderwidth=0, font=("微软雅黑", 9, "bold"))
        self.tree.tag_configure("clicked_bv", background="#FFF9C4")
        self.tree.tag_configure("special_danmaku", foreground=c.color_text)
        self.tree.tag_configure("special_uid", foreground="#E53935", font=("微软雅黑", 9, "bold"))
        self.tree.tag_configure("sc_msg", background="#FFF1F6", foreground="#E91E63")

        self.tree.tag_configure("vip1", background="#FFF1F6", foreground="#1976D2")
        self.tree.tag_configure("vip2", background="#FFF1F6", foreground="#9C27B0")
        self.tree.tag_configure("vip3", background="#FFF1F6", foreground="#E53935")

        # 用户列（树列：头像+用户名）
        self.tree.heading("#0", text="用户")
        self.tree.column("#0", width=140, anchor=tk.CENTER, minwidth=100)

        for col, width, text in [
            ("type", 50, "类型"), ("time", 70, "时间"),
            ("price", 70, "金额(¥)"), ("msg", 400, "内容"),
            ("bv", 230, "BV号 (点击跳转/复制)")
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=tk.CENTER, minwidth=60)

        sb = tk.Scrollbar(f, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<ButtonRelease-1>", self._on_click)
        self.tree.bind("<Button-3>", self._on_right)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda _event: self._hide_hover_tip())

        self._menu = tk.Menu(self.root, tearoff=0, bg=c.color_card, fg=c.color_text)
        self._menu.add_command(label="📋 复制内容", command=self._copy_msg)
        self._menu.add_command(label="🔗 复制 BV 号", command=self._copy_bv)
        self._menu.add_separator()
        self._menu.add_command(label="🚫 屏蔽此用户", command=self._block_user)
        self._menu.add_command(label="🗑️ 删除消息", command=self._delete_selected)

        # 创建默认头像
        self._create_default_avatar()
        self._avatar_poll_after_id = self.root.after(200, self._poll_avatar_results)

        # 状态栏
        st = tk.Frame(self.root, bg=c.color_bg)
        st.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=4)
        tk.Label(st, textvariable=self._status_var, anchor=tk.W,
                 font=("微软雅黑", 9), fg=c.color_text, bg=c.color_bg).pack(side=tk.LEFT, fill=tk.X)
    # ----------------- 工具 -----------------
    def get_display_mode_text(self):
        mode = self._display_mode_var.get()
        if mode == "all":
            return "模式: 所有弹幕"
        elif mode == "sc_only":
            return "模式: 仅显示SC"
        elif mode == "vip":
            return "模式: 仅上舰SC"
        return "模式: 所有弹幕"

    def _cycle_display_mode(self):
        mode = self._display_mode_var.get()
        # 轮换顺序: all -> sc_only -> vip -> all
        if mode == "all":
            new_mode = "sc_only"
        elif mode == "sc_only":
            new_mode = "vip"
        else:
            new_mode = "all"
        self._display_mode_var.set(new_mode)
        self.config.display_mode = new_mode
        self.config.save()
        self._btn_display_mode.config(text=self.get_display_mode_text())
        self._refresh_sc_list()

    def get_only_show_bv_text(self):
        return "仅显示BV号: 开" if self._only_show_bv_var.get() else "仅显示BV号: 关"

    def _toggle_only_show_bv(self):
        self.config.only_show_bv = self._only_show_bv_var.get()
        self.config.save()
        self._btn_only_show_bv.config(text=self.get_only_show_bv_text())
        self._refresh_sc_list()

    # ----------------- 头像 -----------------
    def _create_default_avatar(self):
        # 创建一个默认头像（灰色圆形底+人像图标）
        size = 24
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        # 简单的圆形默认头像
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, size-2, size-2], fill=(200, 200, 200, 255))
        draw.ellipse([size//3, size//4, 2*size//3, size//2], fill=(255, 255, 255, 255))
        draw.ellipse([size//4, size//2+2, 3*size//4, size+4], fill=(255, 255, 255, 255))
        self._default_avatar = ImageTk.PhotoImage(img)

    def _get_avatar_cache_path(self, uid: int) -> Path:
        """获取头像本地缓存文件路径"""
        cache_dir = Path("avatar_cache")
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"{uid}.png"

    def _get_avatar_for_uid(self, uid: int, face_url: str):
        """获取头像，如果缓存中没有则异步下载，返回默认头像"""
        if uid in self._avatar_cache:
            return self._avatar_cache[uid]
        # 先尝试从本地缓存加载
        cache_path = self._get_avatar_cache_path(uid)
        if cache_path.exists():
            try:
                img = Image.open(cache_path).convert("RGBA")
                photo = ImageTk.PhotoImage(img)
                self._avatar_cache[uid] = photo
                return photo
            except Exception:
                pass
        if face_url and uid not in self._avatar_pending:
            self._avatar_pending.add(uid)
            self._start_avatar_fetch(uid, face_url)
        return self._default_avatar

    def _start_avatar_fetch(self, uid: int, face_url: str):
        future = self._avatar_executor.submit(self._download_avatar, uid, face_url)
        future.add_done_callback(
            lambda completed, uid=uid: self._queue_avatar_result(uid, completed)
        )

    @staticmethod
    def _download_avatar(uid: int, face_url: str):
        try:
            from urllib.request import Request, urlopen
            req = Request(
                face_url,
                headers={
                    "User-Agent": "Mozilla/5.0 SCMonitor/1.0",
                    "Referer": "https://www.bilibili.com/",
                },
            )
            with urlopen(req, timeout=8) as resp:
                data = resp.read()
            from io import BytesIO
            img = Image.open(BytesIO(data)).convert("RGBA")
            # 裁剪为圆形并调整大小
            size = 24
            img = img.resize((size, size), Image.LANCZOS)
            # 创建圆形蒙版
            mask = Image.new("L", (size, size), 0)
            from PIL import ImageDraw
            draw = ImageDraw.Draw(mask)
            draw.ellipse([0, 0, size, size], fill=255)
            result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            result.paste(img, (0, 0), mask)
            # 保存到本地缓存
            try:
                cache_dir = Path("avatar_cache")
                cache_dir.mkdir(exist_ok=True)
                result.save(cache_dir / f"{uid}.png", "PNG")
            except Exception:
                pass
            return result
        except Exception as exc:
            logger.warning("下载头像失败 uid=%s: %s", uid, exc)
            return None

    def _queue_avatar_result(self, uid: int, future: Future):
        try:
            result = future.result()
        except Exception as exc:
            result = None
            logger.warning("头像下载异常 uid=%s: %s", uid, exc)
        self._avatar_queue.put((uid, result))

    def _poll_avatar_results(self):
        self._avatar_poll_after_id = None
        if self._is_closing:
            return
        updated = False
        while True:
            try:
                uid, result = self._avatar_queue.get_nowait()
            except queue.Empty:
                break
            self._avatar_pending.discard(uid)
            if result is not None:
                photo = ImageTk.PhotoImage(result)
                self._avatar_cache[uid] = photo
                updated = True
        if updated:
            self._refresh_avatar_images()
        self._avatar_poll_after_id = self.root.after(200, self._poll_avatar_results)

    def _refresh_avatar_images(self):
        """头像下载完成后，只更新对应UID的行"""
        for item in self.tree.get_children():
            record = self._record_for_item(item)
            if not record:
                continue
            uid = record.get("uid")
            if uid in self._avatar_cache:
                self.tree.item(item, image=self._avatar_cache[uid])

    # ----------------- 监听 -----------------
    def _on_close(self):
        self._is_closing = True
        self._hide_tip()
        self._hide_hover_tip()
        if self._video_poll_after_id is not None:
            try:
                self.root.after_cancel(self._video_poll_after_id)
            except Exception:
                pass
            self._video_poll_after_id = None
        self._video_executor.shutdown(wait=False, cancel_futures=True)
        if self._avatar_poll_after_id is not None:
            try:
                self.root.after_cancel(self._avatar_poll_after_id)
            except Exception:
                pass
            self._avatar_poll_after_id = None
        self._avatar_executor.shutdown(wait=False, cancel_futures=True)
        if self._sc_log_file is not None:
            try:
                self._sc_log_file.close()
            except Exception:
                pass
            self._sc_log_file = None
        self.config.save()
        self.root.destroy()

    def _on_window_configure(self, event):
        if event.widget is not self.root:
            return

        if self._resize_after_id is not None:
            self.root.after_cancel(self._resize_after_id)

        self._resize_after_id = self.root.after(300, self._save_window_size)

    def _save_window_size(self):
        self._resize_after_id = None
        window_width = self.root.winfo_width()
        window_height = self.root.winfo_height()
        logger.info(f"窗口大小变化: {window_width}x{window_height}")
        self.config.window_width = window_width
        self.config.window_height = window_height

    # ----------------- 交互 -----------------
    def set_status(self, text):
        self._status_var.set(text)

    def _toggle_top(self):
        self._topmost = not self._topmost
        self.root.attributes("-topmost", self._topmost)
        self._btn_top.config(text="📌 已置顶" if self._topmost else "📌 置顶")

    def _clear_list(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._sc_records.clear()
        self._alloc_sc_idx = 1
        self._clicked_bv_items = []
        self._selected_item = None

    def _latest_clicked_bv_item(self):
        return self._clicked_bv_items[-1] if self._clicked_bv_items else None

    def _goto_last(self):
        latest_item = self._latest_clicked_bv_item()
        if latest_item and self.tree.exists(latest_item):
            self.tree.see(latest_item)
            self._select_item(latest_item)
            self.set_status("📍 已定位")
        else:
            self.set_status("⚠️ 暂无记录")

    def _change_cookie(self):
        w = tk.Toplevel(self.root)
        w.title("修改 Cookie")
        w.geometry("500x220")
        x = (self.root.winfo_screenwidth() - 500) // 2
        y = (self.root.winfo_screenheight() - 220) // 2
        w.geometry(f"+{x}+{y}")
        w.configure(bg=self.config.color_bg)
        w.transient(self.root)
        w.grab_set()

        tk.Label(w, text="粘贴新的 SESSDATA：", font=("微软雅黑", 10),
                 fg=self.config.color_text, bg=self.config.color_bg).pack(pady=(15, 5))

        e = tk.Entry(w, width=55, font=("Consolas", 10), show="*",
                     highlightbackground="#A5D6A7", highlightcolor=self.config.color_main,
                     highlightthickness=2, relief="flat", bd=1)
        e.pack(pady=5, padx=20, ipady=6)
        e.insert(0, self.config.sessdata)
        e.focus()

        def save():
            self.config.sessdata = e.get().strip()
            self.config.save()
            messagebox.showinfo("提示", "已保存，重启生效")
            w.destroy()

        tk.Button(w, text="保存", command=save, bg=self.config.color_main, fg="white",
                  width=10, font=("微软雅黑", 10), relief=tk.FLAT, bd=0, cursor="hand2").pack(pady=12)

    def _qr_login(self):
        """打开扫码登录窗口，通过 B 站扫码获取 SESSDATA"""
        from bilibili_qr_login import BilibiliQRLogin
        from PIL import Image, ImageTk
        import threading

        w = tk.Toplevel(self.root)
        w.title("扫码登录 Bilibili")
        w.geometry("340x420")
        x = (self.root.winfo_screenwidth() - 340) // 2
        y = (self.root.winfo_screenheight() - 420) // 2
        w.geometry(f"+{x}+{y}")
        w.configure(bg=self.config.color_bg)
        w.transient(self.root)
        w.grab_set()

        # 状态变量
        status_var = tk.StringVar(value="正在生成二维码...")
        qr_client = BilibiliQRLogin()
        poll_thread = None
        is_closed = {"value": False}

        # 二维码图片显示区域
        qr_frame = tk.Frame(w, bg="white", width=200, height=200,
                            highlightbackground=self.config.color_main,
                            highlightthickness=2)
        qr_frame.pack(pady=(20, 10))
        qr_frame.pack_propagate(False)

        qr_label = tk.Label(qr_frame, bg="white", text="加载中...",
                            font=("微软雅黑", 10), fg="#999")
        qr_label.pack(expand=True)

        # 状态文字
        tk.Label(w, textvariable=status_var, font=("微软雅黑", 10),
                 fg=self.config.color_text, bg=self.config.color_bg).pack(pady=5)

        # 提示文字
        tip_text = "请使用 Bilibili APP 扫码登录\n扫码后请在手机上点击确认"
        tk.Label(w, text=tip_text, font=("微软雅黑", 9),
                 fg="#888", bg=self.config.color_bg,
                 justify=tk.CENTER).pack(pady=(5, 10))

        # 按钮区域
        btn_frame = tk.Frame(w, bg=self.config.color_bg)
        btn_frame.pack(pady=5)

        def refresh_qr():
            """刷新二维码"""
            status_var.set("正在生成二维码...")
            qr_label.config(image="", text="加载中...")
            threading.Thread(target=generate_worker, daemon=True).start()

        def on_close():
            is_closed["value"] = True
            qr_client.cancel()
            w.destroy()

        w.protocol("WM_DELETE_WINDOW", on_close)

        refresh_btn = tk.Button(
            btn_frame, text="🔄 刷新二维码", command=refresh_qr,
            bg=self.config.color_bar, fg=self.config.color_text,
            relief=tk.FLAT, bd=0, padx=15, pady=6,
            font=("微软雅黑", 9), cursor="hand2",
        )
        refresh_btn.pack(side=tk.LEFT, padx=5)

        close_btn = tk.Button(
            btn_frame, text="关闭", command=on_close,
            bg="#eee", fg="#666",
            relief=tk.FLAT, bd=0, padx=20, pady=6,
            font=("微软雅黑", 9), cursor="hand2",
        )
        close_btn.pack(side=tk.LEFT, padx=5)

        def update_qr_image(image_data: bytes):
            """更新二维码图片（主线程调用）"""
            if is_closed["value"] or not image_data:
                return
            try:
                from io import BytesIO
                img = Image.open(BytesIO(image_data))
                img = img.resize((190, 190), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                qr_label.config(image=photo, text="")
                qr_label.image = photo  # 保持引用防止被回收
            except Exception as exc:
                logger.warning("显示二维码图片失败: %s", exc)
                qr_label.config(text="二维码加载失败")

        def generate_worker():
            """生成二维码的工作线程"""
            try:
                qr_info = qr_client.generate_qrcode()
                self.root.after(0, lambda: update_qr_image(qr_info.qr_image_data))
                self.root.after(0, lambda: status_var.set("请使用 B 站 APP 扫码"))
                # 开始轮询
                start_polling()
            except Exception as exc:
                logger.warning("生成二维码失败: %s", exc)
                err_msg = str(exc)
                self.root.after(0, lambda: status_var.set(f"生成失败: {err_msg[:30]}"))
                self.root.after(0, lambda: qr_label.config(text="生成失败", fg="#f44"))

        def on_status_update(msg: str):
            """状态更新回调（子线程调用）"""
            self.root.after(0, lambda: status_var.set(msg))

        def on_login_success(sessdata: str):
            """登录成功处理（子线程调用）"""
            def save_and_close():
                if is_closed["value"]:
                    return
                self.config.sessdata = sessdata
                self.config.save()
                messagebox.showinfo(
                    "登录成功",
                    "已成功获取 SESSDATA 并保存。\n重启程序后生效。",
                    parent=w,
                )
                self.set_status("✅ 扫码登录成功")
                on_close()
            self.root.after(0, save_and_close)

        def start_polling():
            """启动登录状态轮询线程"""
            nonlocal poll_thread

            def poll_worker():
                result = qr_client.login_with_polling(
                    on_status=on_status_update,
                    interval=2.0,
                    max_attempts=90,
                )
                if result.success and result.sessdata:
                    on_login_success(result.sessdata)
                elif result.success and not result.sessdata:
                    # 登录成功但没拿到 SESSDATA，提示手动获取
                    def warn_no_sessdata():
                        if is_closed["value"]:
                            return
                        messagebox.showwarning(
                            "提示",
                            "扫码成功但未能自动提取 SESSDATA。\n"
                            "请尝试手动从浏览器复制 SESSDATA 填入。",
                            parent=w,
                        )
                        status_var.set("未能提取 SESSDATA")
                    self.root.after(0, warn_no_sessdata)
                else:
                    # 失败或超时
                    def on_fail():
                        if is_closed["value"]:
                            return
                        status_var.set(result.message or "登录失败")
                    self.root.after(0, on_fail)

            poll_thread = threading.Thread(target=poll_worker, daemon=True)
            poll_thread.start()

        # 初始生成二维码
        threading.Thread(target=generate_worker, daemon=True).start()

    def _change_room(self):
        w = tk.Toplevel(self.root)
        w.title("修改直播间号")
        w.geometry("320x180")
        x = (self.root.winfo_screenwidth() - 320) // 2
        y = (self.root.winfo_screenheight() - 180) // 2
        w.geometry(f"+{x}+{y}")
        w.configure(bg=self.config.color_bg)
        w.transient(self.root)
        w.grab_set()

        tk.Label(w, text="输入新的直播间号：", font=("微软雅黑", 10),
                 fg=self.config.color_text, bg=self.config.color_bg).pack(pady=(15, 5))

        e = tk.Entry(w, width=20, font=("Consolas", 12),
                     highlightbackground="#A5D6A7", highlightcolor=self.config.color_main,
                     highlightthickness=2, relief="flat", bd=1, justify="center")
        e.pack(pady=5, padx=20, ipady=6)
        e.insert(0, str(self.config.room_id))
        e.focus()
        e.select_range(0, tk.END)

        def save():
            try:
                new_id = int(e.get().strip())
                if new_id <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的直播间号")
                return
            self.config.room_id = new_id
            self.config.save()
            messagebox.showinfo("提示", "已保存，重启生效")
            w.destroy()

        tk.Button(w, text="保存", command=save, bg=self.config.color_main, fg="white",
                  width=10, font=("微软雅黑", 10), relief=tk.FLAT, bd=0, cursor="hand2").pack(pady=12)

    def _on_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        item = self.tree.identify_row(ev.y)
        if not item:
            return

        self._select_item(item)
        if self.tree.identify_column(ev.x) != "#5":
            return

        record = self._record_for_item(item)
        bv = record.get("bv", "-") if record else self.tree.set(item, "bv")
        user = record.get("uname", "") if record else self.tree.item(item, "text")
        if bv and ("bv" in bv.lower()):
            self._mark_row(item)
            self.root.clipboard_clear()
            self.root.clipboard_append(bv)
            webbrowser.open(BV_URL_TEMPLATE.format(bv))
            self._show_tip("已跳转并复制BV")
            self.set_status(f"🔗 已跳转并复制: {bv}")
        else:
            if item in self._clicked_bv_items:
                self._unmark_row(item)
                self._show_tip("已取消标记")
                self.set_status(f"🔗 已取消: {user}")
            else:
                self._mark_row(item)
                self._show_tip("已标记")
                self.set_status(f"🔗 已标记: {user}")

    def _mark_row(self, item):
        if item in self._clicked_bv_items:
            self._clicked_bv_items.remove(item)
        self._clicked_bv_items.append(item)
        self._apply_highlights()
        record = self._record_for_item(item)
        bv = record.get("bv", "-") if record else self.tree.set(item, "bv")
        return bv if bv and bv != "-" else None

    def _unmark_row(self, item):
        if item in self._clicked_bv_items:
            self._clicked_bv_items.remove(item)
            self._apply_highlights()

    def _apply_highlights(self):
        for item in self.tree.get_children():
            record = self._record_for_item(item)
            if not record:
                continue
            is_danmaku = record.get("is_danmaku", False)
            vip_level = record.get("vip_level", UserVipLevel.Normal)
            is_special_uid = record.get("is_special_uid", False)

            # 特殊UID用户优先显示红色
            if is_special_uid:
                base_tags = ["special_uid"]
            elif is_danmaku:
                vip_int = int(vip_level)
                if vip_int == UserVipLevel.VIP3:
                    base_tags = ["vip3"]
                elif vip_int == UserVipLevel.VIP2:
                    base_tags = ["vip2"]
                elif vip_int == UserVipLevel.VIP1:
                    base_tags = ["vip1"]
                else:
                    base_tags = ["special_danmaku"]
            else:
                base_tags = ["sc_msg"]

            # 已点击BV的条目叠加背景色（不改变文字颜色）
            if item in self._clicked_bv_items:
                self.tree.item(item, tags=("clicked_bv", *base_tags))
            else:
                self.tree.item(item, tags=tuple(base_tags))

    def _current_selected_item(self):
        selection = self.tree.selection()
        if selection:
            self._selected_item = selection[0]
        return self._selected_item

    def _select_item(self, item):
        self.tree.selection_set(item)
        self.tree.focus(item)
        self._selected_item = item

    def _restore_selected_item(self, item):
        if item and self.tree.exists(item):
            self._select_item(item)

    def _on_right(self, ev):
        item = self.tree.identify_row(ev.y)
        if item:
            self._select_item(item)
            self._menu.post(ev.x_root, ev.y_root)

    def _copy_msg(self):
        sel = self.tree.selection()
        if sel:
            self._copy_to_clipboard(self.tree.set(sel[0], "msg"))
            self.set_status("📋 已复制")

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        record = self._record_for_item(item)
        if record:
            # 从记录列表中删除
            self._sc_records = [r for r in self._sc_records if r.get("id") != record.get("id")]
        # 从已点击列表中移除
        if item in self._clicked_bv_items:
            self._clicked_bv_items.remove(item)
        # 从树中删除
        self.tree.delete(item)
        self._selected_item = None
        self.set_status("🗑️ 已删除消息")

    def _block_user(self):
        """右键屏蔽当前选中的用户"""
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        record = self._record_for_item(item)
        if not record:
            return
        uid = record.get("uid")
        uname = record.get("uname", "")
        face = record.get("face", "")
        if not uid:
            return

        # 确认对话框
        confirm = messagebox.askyesno(
            "屏蔽用户",
            f"确定要屏蔽用户「{uname}」(UID: {uid}) 吗？\n\n屏蔽后该用户的所有弹幕将不再显示。",
            parent=self.root,
        )
        if not confirm:
            return

        # 添加到黑名单
        if uid not in self.config.blacklist_uid:
            self.config.blacklist_uid.append(uid)
        # 保存用户信息
        self.config.blacklist_users[str(uid)] = {
            "uname": uname,
            "face": face,
        }
        self.config.save()

        # 移除该用户的所有消息
        removed = 0
        for rec in list(self._sc_records):
            if rec.get("uid") == uid:
                item_id = self._sc_item_id(rec)
                if self.tree.exists(item_id):
                    self.tree.delete(item_id)
                if item_id in self._clicked_bv_items:
                    self._clicked_bv_items.remove(item_id)
                removed += 1
        self._sc_records = [r for r in self._sc_records if r.get("uid") != uid]

        self.set_status(f"🚫 已屏蔽 {uname}，移除 {removed} 条消息")

    # ----------------- 黑名单管理 -----------------
    def _show_blacklist_window(self):
        """显示用户黑名单管理窗口"""
        from PIL import Image, ImageTk

        w = tk.Toplevel(self.root)
        w.title("用户黑名单")
        w.geometry("420x500")
        x = (self.root.winfo_screenwidth() - 420) // 2
        y = (self.root.winfo_screenheight() - 500) // 2
        w.geometry(f"+{x}+{y}")
        w.configure(bg=self.config.color_bg)
        w.transient(self.root)
        w.grab_set()

        # 标题
        tk.Label(w, text="🚫 用户黑名单", font=("微软雅黑", 12, "bold"),
                 fg=self.config.color_text, bg=self.config.color_bg).pack(pady=(15, 10))

        # 列表区域
        list_frame = tk.Frame(w, bg=self.config.color_card,
                              highlightbackground=self.config.color_bar,
                              highlightthickness=1)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        # 列表内容
        content_frame = tk.Frame(list_frame, bg=self.config.color_card)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 滚动条
        canvas = tk.Canvas(content_frame, bg=self.config.color_card,
                           highlightthickness=0, width=370)
        scrollbar = ttk.Scrollbar(content_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=self.config.color_card)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def refresh_list():
            """刷新黑名单列表"""
            # 清空
            for widget in scrollable_frame.winfo_children():
                widget.destroy()

            blacklist_uid = self.config.blacklist_uid
            blacklist_users = self.config.blacklist_users

            if not blacklist_uid:
                tk.Label(scrollable_frame, text="暂无屏蔽用户",
                         font=("微软雅黑", 10), fg="#999",
                         bg=self.config.color_card).pack(pady=30)
                return

            for uid in blacklist_uid:
                user_info = blacklist_users.get(str(uid), {})
                uname = user_info.get("uname", f"UID:{uid}")
                face_url = user_info.get("face", "")

                row = tk.Frame(scrollable_frame, bg=self.config.color_card)
                row.pack(fill=tk.X, pady=2)

                # 头像
                avatar = self._get_avatar_for_uid(int(uid), face_url)
                avatar_label = tk.Label(row, image=avatar, bg=self.config.color_card)
                avatar_label.image = avatar
                avatar_label.pack(side=tk.LEFT, padx=(5, 10))

                # 用户名和UID
                info_frame = tk.Frame(row, bg=self.config.color_card)
                info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

                tk.Label(info_frame, text=uname, font=("微软雅黑", 10, "bold"),
                         fg=self.config.color_text, bg=self.config.color_card,
                         anchor="w").pack(anchor="w")
                tk.Label(info_frame, text=f"UID: {uid}", font=("微软雅黑", 8),
                         fg="#888", bg=self.config.color_card,
                         anchor="w").pack(anchor="w")

                # 移除按钮
                def make_remove(uid_val):
                    return lambda: remove_user(uid_val)

                tk.Button(row, text="移除", command=make_remove(uid),
                          bg="#ff6b6b", fg="white", relief=tk.FLAT, bd=0,
                          padx=10, pady=3, font=("微软雅黑", 9),
                          cursor="hand2", activebackground="#ee5a5a"
                          ).pack(side=tk.RIGHT, padx=5)

        def remove_user(uid):
            """从黑名单移除用户"""
            confirm = messagebox.askyesno(
                "移除屏蔽",
                f"确定要将用户从黑名单中移除吗？",
                parent=w,
            )
            if not confirm:
                return
            if uid in self.config.blacklist_uid:
                self.config.blacklist_uid.remove(uid)
            if str(uid) in self.config.blacklist_users:
                del self.config.blacklist_users[str(uid)]
            self.config.save()
            refresh_list()
            self._refresh_sc_list()
            self.set_status("✅ 已从黑名单移除")

        refresh_list()

        # 底部按钮
        btn_frame = tk.Frame(w, bg=self.config.color_bg)
        btn_frame.pack(pady=15)

        tk.Button(btn_frame, text="关闭", command=w.destroy,
                  bg=self.config.color_bar, fg=self.config.color_text,
                  relief=tk.FLAT, bd=0, padx=20, pady=6,
                  font=("微软雅黑", 10), cursor="hand2"
                  ).pack()

    # ----------------- 关键词黑名单管理 -----------------
    def _show_keyword_blacklist_window(self):
        """显示屏蔽关键词管理窗口"""
        w = tk.Toplevel(self.root)
        w.title("屏蔽关键词")
        w.geometry("400x450")
        x = (self.root.winfo_screenwidth() - 400) // 2
        y = (self.root.winfo_screenheight() - 450) // 2
        w.geometry(f"+{x}+{y}")
        w.configure(bg=self.config.color_bg)
        w.transient(self.root)
        w.grab_set()

        # 标题
        tk.Label(w, text="🔤 屏蔽关键词", font=("微软雅黑", 12, "bold"),
                 fg=self.config.color_text, bg=self.config.color_bg).pack(pady=(15, 10))

        # 添加区域
        add_frame = tk.Frame(w, bg=self.config.color_bg)
        add_frame.pack(fill=tk.X, padx=15, pady=5)

        entry = tk.Entry(add_frame, font=("微软雅黑", 10),
                         highlightbackground=self.config.color_bar,
                         highlightcolor=self.config.color_main,
                         highlightthickness=2, relief="flat", bd=1)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        entry.focus()

        def add_keyword():
            keyword = entry.get().strip()
            if not keyword:
                return
            if keyword in self.config.blacklist:
                messagebox.showwarning("提示", "该关键词已存在", parent=w)
                return
            self.config.blacklist.append(keyword)
            self.config.save()
            entry.delete(0, tk.END)
            refresh_list()
            self._refresh_sc_list()
            self.set_status(f"✅ 已添加屏蔽关键词: {keyword}")

        tk.Button(add_frame, text="添加", command=add_keyword,
                  bg=self.config.color_main, fg="white", relief=tk.FLAT, bd=0,
                  padx=15, pady=5, font=("微软雅黑", 9),
                  cursor="hand2", activebackground="#388E3C"
                  ).pack(side=tk.LEFT, padx=(10, 0))

        entry.bind("<Return>", lambda e: add_keyword())

        # 列表区域
        list_frame = tk.Frame(w, bg=self.config.color_card,
                              highlightbackground=self.config.color_bar,
                              highlightthickness=1)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        # 列表
        listbox = tk.Listbox(list_frame, font=("微软雅黑", 10),
                             bg=self.config.color_card, fg=self.config.color_text,
                             selectbackground="#C8E6C9", selectforeground=self.config.color_text,
                             relief="flat", bd=0, activestyle="none")
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        sb = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        def refresh_list():
            listbox.delete(0, tk.END)
            for kw in self.config.blacklist:
                listbox.insert(tk.END, f"  {kw}")

        def remove_keyword():
            selection = listbox.curselection()
            if not selection:
                return
            idx = selection[0]
            keyword = self.config.blacklist[idx]
            confirm = messagebox.askyesno(
                "移除关键词",
                f"确定要移除关键词「{keyword}」吗？",
                parent=w,
            )
            if not confirm:
                return
            self.config.blacklist.pop(idx)
            self.config.save()
            refresh_list()
            self._refresh_sc_list()
            self.set_status(f"✅ 已移除屏蔽关键词: {keyword}")

        # 移除按钮
        btn_frame = tk.Frame(w, bg=self.config.color_bg)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="移除选中", command=remove_keyword,
                  bg="#ff6b6b", fg="white", relief=tk.FLAT, bd=0,
                  padx=15, pady=6, font=("微软雅黑", 9),
                  cursor="hand2", activebackground="#ee5a5a"
                  ).pack(side=tk.LEFT, padx=5)

        tk.Button(btn_frame, text="关闭", command=w.destroy,
                  bg=self.config.color_bar, fg=self.config.color_text,
                  relief=tk.FLAT, bd=0, padx=20, pady=6,
                  font=("微软雅黑", 9), cursor="hand2"
                  ).pack(side=tk.LEFT, padx=5)

        refresh_list()

    def _copy_to_clipboard(self, value):
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._show_tip("已复制")

    def _show_tip(self, text, duration=1200):
        self._hide_tip()

        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=self.config.color_main)
        tk.Label(
            tip,
            text=text,
            bg=self.config.color_main,
            fg="white",
            font=("微软雅黑", 9, "bold"),
            padx=10,
            pady=5,
        ).pack()
        tip.update_idletasks()

        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        self._position_floating_tip(tip, pointer_x, pointer_y)

        self._tip_window = tip
        self._tip_after_id = self.root.after(duration, self._hide_tip)

    def _hide_tip(self):
        if self._tip_after_id is not None:
            try:
                self.root.after_cancel(self._tip_after_id)
            except Exception:
                pass
            self._tip_after_id = None
        if self._tip_window is not None:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None

    def _position_floating_tip(self, tip, pointer_x, pointer_y):
        tip_width = tip.winfo_reqwidth()
        tip_height = tip.winfo_reqheight()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        work_area = get_monitor_work_area(pointer_x, pointer_y) or WorkArea(
            left=0,
            top=0,
            right=screen_width,
            bottom=screen_height,
        )
        x, y = calculate_popup_position(
            pointer_x,
            pointer_y,
            tip_width,
            tip_height,
            work_area,
        )
        position_popup_window(
            tip,
            x,
            y,
            tip_width,
            tip_height,
            tk_screen_width=screen_width,
            tk_screen_height=screen_height,
        )

    def _on_tree_motion(self, event):
        if (
            self.tree.identify_region(event.x, event.y) != "cell"
            or self.tree.identify_column(event.x) != "#5"
        ):
            self._hide_hover_tip()
            return

        item = self.tree.identify_row(event.y)
        record = self._record_for_item(item) if item else None
        if not record or record.get("bv") in (None, "-"):
            self._hide_hover_tip()
            return

        if self._hover_item != item and self._sc_store is not None:
            try:
                stats = self._sc_store.get_user_stats(record["uid"])
                self._update_user_stats(record["uid"], stats)
            except Exception as exc:
                logger.warning("读取投稿人统计失败: %s", exc)

        content = self._format_bv_tooltip(record)
        if self._hover_item == item and self._hover_content == content:
            return
        self._show_hover_tip(item, content, event.x_root, event.y_root)

    @staticmethod
    def _format_bv_tooltip(record):
        count = int(record.get("user_sc_count") or 0)
        total = float(record.get("user_sc_total") or 0)
        amount = f"{total:.2f}".rstrip("0").rstrip(".")
        lines = [
            f"投稿人：{record.get('uname', '-')}",
            f"累计SC次数：{count} 次",
            f"累计金额：¥{amount or '0'}",
        ]

        status = record.get("video_metadata_status")
        title = record.get("video_title")
        tags = record.get("video_tags") or []
        if title:
            lines.append(f"标题：{title}")
            # 播放/点赞/投币/收藏数据
            view = record.get("video_view", 0) or 0
            like = record.get("video_like", 0) or 0
            coin = record.get("video_coin", 0) or 0
            favorite = record.get("video_favorite", 0) or 0
            lines.append(f"▶ 播放: {view:,}  👍 点赞: {like:,}  🪙 投币: {coin:,}  ⭐ 收藏: {favorite:,}")
            lines.append(f"标签：{'、'.join(tags) if tags else '无'}")
        elif status == "error":
            lines.append(f"视频信息：解析失败（{record.get('video_metadata_error') or '未知错误'}）")
        else:
            lines.append("视频信息：解析中…")

        # matches = record.get("blacklist_matches") or []
        # if matches:
        #     lines.append(f"已命中黑名单：{'、'.join(matches)}")
        if status == "partial" and record.get("video_metadata_error"):
            lines.append(f"标签获取失败：{record['video_metadata_error']}")
        return "\n".join(lines)

    def _show_hover_tip(self, item, content, pointer_x, pointer_y):
        self._hide_hover_tip()
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg="#1F2937")
        tk.Label(
            tip,
            text=content,
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=560,
            bg="#1F2937",
            fg="white",
            font=("微软雅黑", 9),
            padx=10,
            pady=7,
        ).pack()
        tip.update_idletasks()

        self._position_floating_tip(tip, pointer_x, pointer_y)
        self._hover_window = tip
        self._hover_item = item
        self._hover_content = content

    def _hide_hover_tip(self):
        if self._hover_window is not None:
            try:
                self._hover_window.destroy()
            except Exception:
                pass
        self._hover_window = None
        self._hover_item = None
        self._hover_content = None

    def _copy_bv(self):
        sel = self.tree.selection()
        if sel:
            record = self._record_for_item(sel[0])
            bv = record.get("bv", "-") if record else self.tree.set(sel[0], "bv")
            if bv and bv != "-":
                self._copy_to_clipboard(bv)
                self.set_status("📋 已复制 BV")

    def add_sc(self, user: UserInfo, price, message, timestamp):
        uname = user.uname
        uid = user.uid
        vip_level = user.vip_level
        self._user_info[uname] = user

        # 判断是否为特殊UID用户
        is_special_uid = any(str(uid) == str(special_uid) for special_uid in self.config.special_users_uid)

        try:
            price_value = float(price)
        except (TypeError, ValueError):
            price_value = None
        bv = extract_bv(message) or "-"
        record_id = self._alloc_sc_idx
        self._alloc_sc_idx += 1
        stored_id = None
        stats = UserSCStats(count=0, total_amount=0)
        sc_store = getattr(self, "_sc_store", None)
        if sc_store is not None:
            try:
                stored = sc_store.record_sc(
                    user_uid=uid,
                    nickname=uname,
                    content=message,
                    amount=price_value if price_value is not None else 0,
                    sent_at=timestamp,
                    bv=None if bv == "-" else bv,
                )
                stored_id = stored.id
                stats = stored.stats
            except Exception as exc:
                logger.error("保存 SC 到数据库失败: %s", exc)
        self.root.after(0, self._append_sc_record, {
            "id": record_id,
            "db_id": stored_id,
            "uid": uid,
            "uname": uname,
            "vip_level": vip_level,
            "face": user.face,
            "price": price,
            "price_value": price_value,
            "message": message,
            "timestamp": timestamp,
            "bv": bv,
            "video_title": None,
            "video_tags": [],
            "video_metadata_status": "pending" if bv != "-" else "not_applicable",
            "video_metadata_error": None,
            "blacklisted": False,
            "blacklist_matches": [],
            "user_sc_count": stats.count,
            "user_sc_total": stats.total_amount,
            "is_special_uid": is_special_uid,
        })

    def add_danmaku(self, user: UserInfo, message, timestamp):
        uname = user.uname
        uid = user.uid
        vip_level = user.vip_level

        # 判断是否为特殊弹幕（上舰用户或特殊UID用户）
        is_special = False
        is_special_uid = False
        # 用户 UID 在特殊用户列表
        if any(str(uid) == str(special_uid) for special_uid in self.config.special_users_uid):
            is_special = True
            is_special_uid = True
        # 是上舰用户（舰长/提督/总督），仅根据 vip_level 判断，与颜色显示配置无关
        vip_int = int(vip_level)
        if vip_int in (UserVipLevel.VIP1, UserVipLevel.VIP2, UserVipLevel.VIP3):
            is_special = True

        self._user_info[uname] = user
        record_id = self._alloc_sc_idx
        self._alloc_sc_idx += 1
        bv = extract_bv(message) or "-"
        self.root.after(0, self._append_sc_record, {
            "id": record_id,
            "uid": uid,
            "uname": uname,
            "vip_level": vip_level,
            "face": user.face,
            "price": 0,
            "price_value": 0,
            "message": message,
            "timestamp": timestamp,
            "bv": bv,
            "video_title": None,
            "video_tags": [],
            "video_metadata_status": "pending" if bv != "-" else "not_applicable",
            "video_metadata_error": None,
            "is_danmaku": True,
            "is_special_danmaku": is_special,
            "is_special_uid": is_special_uid,
        })
        return

    def _append_sc_record(self, record):
        self._sc_records.append(record)
        if record.get("uid") is not None and record.get("user_sc_count") is not None:
            self._update_user_stats(
                record["uid"],
                UserSCStats(
                    count=int(record["user_sc_count"]),
                    total_amount=float(record.get("user_sc_total") or 0),
                ),
            )
        self._write_sc_log(record)
        self._refresh_sc_list()
        if record.get("video_metadata_status") == "pending":
            self._start_video_metadata_analysis(record)

    def _update_user_stats(self, uid: int, stats: UserSCStats):
        for existing_record in self._sc_records:
            if existing_record.get("uid") == uid:
                existing_record["user_sc_count"] = stats.count
                existing_record["user_sc_total"] = stats.total_amount

    def _start_video_metadata_analysis(self, record):
        if self._is_closing:
            return
        future = self._video_executor.submit(
            self._video_client.fetch_video_metadata,
            record["bv"],
        )
        future.add_done_callback(
            lambda completed, record_id=record["id"], db_id=record.get("db_id"):
                self._queue_video_result(record_id, db_id, completed)
        )

    def _queue_video_result(self, record_id: int, db_id: int | None, future: Future):
        try:
            metadata = future.result()
            error = None
        except Exception as exc:
            metadata = None
            error = str(exc)
        self._video_result_queue.put((record_id, db_id, metadata, error))

    def _poll_video_results(self):
        self._video_poll_after_id = None
        if self._is_closing:
            return

        updated = False
        latest_error = None
        while True:
            try:
                record_id, db_id, metadata, error = self._video_result_queue.get_nowait()
            except queue.Empty:
                break

            matches = []
            if metadata is not None:
                matches = match_blacklist(
                    metadata.title,
                    metadata.tags,
                    self.config.blacklist,
                )
            if db_id is not None and metadata is not None and self._sc_store is not None:
                try:
                    self._sc_store.update_video_metadata(
                        db_id,
                        title=metadata.title,
                        tags=metadata.tags,
                        blacklisted=bool(matches),
                        blacklist_matches=matches,
                    )
                except Exception as exc:
                    logger.error("保存 BV 视频信息失败: %s", exc)

            record = next((item for item in self._sc_records if item["id"] == record_id), None)
            if record is not None:
                self._apply_video_metadata(record, metadata, error, matches)
                updated = True
            if error:
                latest_error = error
                logger.warning("SC %s 的 BV 视频信息解析失败: %s", record_id, error)

        if updated:
            self._refresh_sc_list()
        if latest_error:
            self.set_status(f"⚠️ BV 视频信息解析失败: {latest_error}")
        self._video_poll_after_id = self.root.after(100, self._poll_video_results)

    @staticmethod
    def _apply_video_metadata(record, metadata: VideoMetadata | None, error, matches):
        if metadata is None:
            record["video_metadata_status"] = "error"
            record["video_metadata_error"] = error
            return
        record["video_title"] = metadata.title
        record["video_tags"] = list(metadata.tags)
        record["video_view"] = metadata.view
        record["video_like"] = metadata.like
        record["video_coin"] = metadata.coin
        record["video_favorite"] = metadata.favorite
        record["video_metadata_status"] = "partial" if metadata.tag_error else "done"
        record["video_metadata_error"] = metadata.tag_error
        record["blacklist_matches"] = list(matches)
        record["blacklisted"] = bool(matches)

    def _write_sc_log(self, record):
        if self._sc_log_file is None:
            return
        try:
            self._sc_log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._sc_log_file.flush()
        except Exception as e:
            logger.warning(f"写入 SC 日志失败: {e}")

    def get_visible_scs(self):
        display_mode = self._display_mode_var.get()
        only_show_bv = self._only_show_bv_var.get()
        has_blacklist = bool(getattr(self.config, "blacklist", []))
        blacklist_keywords = self.config.blacklist
        blacklist_uid = set(self.config.blacklist_uid)
        latest_item = self._selected_item
        visible_scs = []
        for record in self._sc_records:
            # UID 黑名单过滤
            uid = record.get("uid")
            if uid is not None and uid in blacklist_uid:
                continue
            # 视频关键词黑名单过滤
            if record.get("blacklisted") or (
                has_blacklist
                and record.get("video_metadata_status") == "pending"
            ):
                continue
            # 视频标题动态关键词过滤（关键词变更后重新检查已有标题）
            video_title = record.get("video_title", "")
            if video_title and blacklist_keywords:
                title_lower = video_title.casefold()
                title_blocked = any(
                    str(kw).strip().casefold() in title_lower
                    for kw in blacklist_keywords
                )
                if title_blocked:
                    continue
            # 弹幕消息内容关键词过滤
            message = record.get("message", "")
            if message and blacklist_keywords:
                msg_blocked = False
                for kw in blacklist_keywords:
                    if kw and kw in message:
                        msg_blocked = True
                        break
                if msg_blocked:
                    continue
            is_danmaku = record.get("is_danmaku", False)
            # 显示模式过滤
            if is_danmaku:
                if display_mode == "sc_only":
                    # 仅显示SC，跳过所有弹幕
                    continue
                elif display_mode == "vip":
                    # 仅VIP弹幕，跳过普通弹幕
                    if not record.get("is_special_danmaku", False):
                        continue
            # 仅显示有BV号过滤
            if only_show_bv:
                bv = record.get("bv", "-")
                if not bv or bv == "-":
                    continue
            visible_scs.append(record)
        return visible_scs

    def _sc_item_id(self, record):
        return f"sc_{record['id']}"

    def _record_for_item(self, item):
        return next(
            (record for record in self._sc_records if self._sc_item_id(record) == item),
            None,
        )

    @staticmethod
    def _bv_column_value(record):
        bv = record.get("bv") or "-"
        return bv

    def _refresh_sc_list(self):
        self._hide_hover_tip()
        selected_item = self._current_selected_item()
        visible_scs = self.get_visible_scs()
        visible_count = len(visible_scs)
        visible_item_ids = {self._sc_item_id(record) for record in visible_scs}
        self._clicked_bv_items = [
            item for item in self._clicked_bv_items if item in visible_item_ids
        ]
        if selected_item not in visible_item_ids:
            selected_item = None
            self._selected_item = None

        for i in self.tree.get_children():
            self.tree.delete(i)

        for record in reversed(visible_scs):
            t_str = time.strftime("%H:%M:%S", time.localtime(record["timestamp"]))
            msg = record["message"]
            bv = record.get("bv")
            if bv and bv != "-":
                display_msg = BV_PATTERN.sub("", msg).strip()
                if not display_msg:
                    display_msg = "(仅含BV号)"
            else:
                display_msg = msg
            display_msg = (display_msg[:80] + "...") if len(display_msg) > 80 else display_msg
            type_label = "弹幕" if record.get("is_danmaku", False) else "SC"
            avatar = self._get_avatar_for_uid(
                record.get("uid", 0),
                record.get("face", ""),
            )
            self.tree.insert(
                "",
                tk.END,
                iid=self._sc_item_id(record),
                text=record["uname"],
                image=avatar,
                values=(
                    type_label,
                    t_str,
                    f"¥{record['price']}",
                    display_msg,
                    self._bv_column_value(record),
                )
            )

        self._apply_highlights()
        self._restore_selected_item(selected_item)

        total_count = len(self._sc_records)
        self.set_status(f"✅ 已连接 | 共 {total_count} 条消息 | 实际显示 {visible_count} 条")

    def on_danmaku_heartbeat(self):
        total_count = len(self._sc_records)
        visible_count = len(self.get_visible_scs())
        self.set_status(f"✅ 已连接 | 共 {total_count} 条消息 | 实际显示 {visible_count} 条")

    def _start_blivedm_thread(self):
        threading.Thread(target=lambda: asyncio.run(run_blivedm(self, self.config)),
                         daemon=True).start()
