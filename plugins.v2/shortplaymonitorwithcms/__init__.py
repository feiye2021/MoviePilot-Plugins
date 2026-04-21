import datetime
import os
import re
import shutil
import threading
import time
from pathlib import Path
from threading import Lock
from typing import Any, List, Dict, Tuple, Optional
from xml.dom import minidom

import chardet
import pytz
from PIL import Image
from app.helper.sites import SitesHelper
from app.modules.indexer.spider import SiteSpider
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from lxml import etree
from requests import RequestException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.meta.words import WordsMatcher
from app.core.metainfo import MetaInfoPath
from app.db.site_oper import SiteOper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import NotificationType
from app.utils.common import retry
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from app.modules.filemanager.transhandler import TransHandler

ffmpeg_lock = threading.Lock()
lock = Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, watching_path: str, file_change: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = watching_path
        self.file_change = file_change

    def on_created(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.dest_path)


class ShortPlayMonitorWithCMS(_PluginBase):
    # 插件名称
    plugin_name = "短剧刮削+CMS通知"
    # 插件描述
    plugin_desc = "监控视频短剧创建、刮削，支持目的目录为网盘，整理完成后自动通知CMS进行增量同步（strm生成）。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/feiye2021/MoviePilot-Plugins/main/icons/amule-1.png"
    # 插件版本
    plugin_version = "1.0.4"
    # 插件作者
    plugin_author = "feiye"
    # 作者主页
    author_url = "https://github.com/feiye2021/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "shortplaymonitorwithcms_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # ===== 短剧监控私有属性 =====
    _enabled = False
    _monitor_confs = None
    _onlyonce = False
    _image = True
    _exclude_keywords = ""
    _transfer_type = "link"
    _observer = []
    _timeline = "00:00:10"
    _dirconf = {}
    _renameconf = {}
    _coverconf = {}
    tmdbchain = None
    _interval = 10
    _notify = False
    _medias = {}
    filemanager = None

    # ===== 刮削降级私有属性 =====
    _site_fallback = False        # TMDB失败时是否降级到站点（AGSVPT/麒麟）刮削
    _agsvpt_dual_cookie = False   # 是否启用 AGSVPT 双域名自定义 Cookie
    _agsvpt_cookie_pt = ""        # pt.agsvpt.cn 的 Cookie
    _agsvpt_cookie_www = ""       # www.agsvpt.com 的 Cookie

    # ===== CMS通知私有属性 =====
    _cms_enabled = False
    _cms_notify_type = None
    _cms_domain = None
    _cms_api_token = None
    # CMS通知队列：key=剧集title，value={"count": int, "last_time": float}
    # 每个剧集单独计时，全部上传完成静默60秒后才触发CMS通知
    _pending_notify: Dict[str, Dict] = {}

    # 网盘批量上传队列：key=剧集title，value={"files": [...], "last_time": float, "store_conf": str}
    # files 每项为 {"event_path": str, "target_path": Path}
    # 监控目录静默60秒后统一批量上传，避免一集一集触发115接口
    _upload_queue: Dict[str, Dict] = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 已处理文件记录的持久化路径（跨重启保留，防止重复上传）
    _HANDLED_RECORD_PATH = Path("/tmp/shortplaymonitormod_handled.json")

    def init_plugin(self, config: dict = None):
        # 清空配置及运行时队列（防止插件重载时旧数据残留导致重复处理）
        self._dirconf = {}
        self._renameconf = {}
        self._coverconf = {}
        self._storeconf = {}
        self._medias = {}
        self._pending_notify = {}
        self._upload_queue = {}
        # 加载持久化的已处理文件记录（重启后保留，防止网盘文件重复上传）
        self._handled_paths: set = self.__load_handled_record()
        self.tmdbchain = TmdbChain()
        self.mediachain = MediaChain()
        self.filemanager = FileManagerModule()
        self.filemanager.init_module()

        if config:
            # 短剧监控配置
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._image = config.get("image")
            self._interval = config.get("interval")
            self._notify = config.get("notify")
            self._monitor_confs = config.get("monitor_confs")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._transfer_type = config.get("transfer_type") or "link"
            # CMS通知配置
            self._cms_enabled = config.get("cms_enabled")
            self._cms_notify_type = config.get("cms_notify_type")
            self._cms_domain = config.get("cms_domain")
            self._cms_api_token = config.get("cms_api_token")
            self._site_fallback = config.get("site_fallback") or False
            self._agsvpt_dual_cookie = config.get("agsvpt_dual_cookie") or False
            self._agsvpt_cookie_pt = config.get("agsvpt_cookie_pt") or ""
            self._agsvpt_cookie_www = config.get("agsvpt_cookie_www") or ""

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 网盘批量上传定时任务（每30秒检查一次，静默60秒后批量上传）
            self._scheduler.add_job(
                self.__batch_upload,
                trigger='interval',
                seconds=30,
                id="batch_upload",
                name="网盘批量上传"
            )

            # CMS通知定时任务（每分钟检查一次）
            if self._cms_enabled:
                self._scheduler.add_job(
                    self.__notify_cms,
                    trigger=CronTrigger.from_crontab("* * * * *"),
                    id="cms_notify",
                    name="CMS增量同步通知"
                )

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
            logger.debug(f"monitor_confs: {len(monitor_confs)}")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 监控方式#监控目录#目的目录#是否重命名#封面比例
                if not monitor_conf:
                    continue
                if str(monitor_conf).count("#") != 4 and str(monitor_conf).count("#") != 5:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                mode = str(monitor_conf).split("#")[0]
                source_dir = str(monitor_conf).split("#")[1]
                target_dir = str(monitor_conf).split("#")[2]
                rename_conf = str(monitor_conf).split("#")[3]
                cover_conf = str(monitor_conf).split("#")[4]
                if str(monitor_conf).count("#") == 5:
                    store_conf = str(monitor_conf).split("#")[5]
                else:
                    store_conf = "local"
                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir
                self._renameconf[source_dir] = rename_conf
                self._coverconf[source_dir] = cover_conf
                self._storeconf[source_dir] = store_conf

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                            logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    try:
                        if mode == "compatibility":
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{source_dir} 的目录监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{source_dir} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{source_dir} 启动目录监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("短剧监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name="短剧监控全量执行")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        # _image 为持久开关，不在初始化时触发裁剪
        # 封面裁剪在每次文件整理成功后由 __handle_file 内部调用

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT):
                self.__handle_file(is_directory=Path(file_path).is_dir(),
                                   event_path=str(file_path),
                                   source_dir=mon_path)
        logger.info("全量同步短剧监控目录完成！")

    def __handle_image(self):
        """
        立即运行一次，裁剪封面
        """
        if not self._dirconf or not self._dirconf.keys():
            logger.error("未正确配置，停止裁剪 ...")
            return

        logger.info("开始全量裁剪封面 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            cover_conf = self._coverconf.get(mon_path)
            target_path = self._dirconf.get(mon_path)
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(target_path), ["poster.jpg"]):
                try:
                    if Path(file_path).name != "poster.jpg":
                        continue
                    image = Image.open(file_path)
                    if image.width / image.height != int(str(cover_conf).split(":")[0]) / int(
                            str(cover_conf).split(":")[1]):
                        self.__save_poster(input_path=file_path,
                                           poster_path=file_path,
                                           cover_conf=cover_conf)
                        logger.info(f"封面 {file_path} 已裁剪 比例为 {cover_conf}")
                except Exception:
                    continue
        logger.info("全量裁剪封面完成！")

    def event_handler(self, event, source_dir: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param source_dir: 监控目录
        :param event_path: 事件文件路径
        """
        # 回收站及隐藏的文件不处理
        if (event_path.find("/@Recycle") != -1
                or event_path.find("/#recycle") != -1
                or event_path.find("/.") != -1
                or event_path.find("/@eaDir") != -1):
            logger.info(f"{event_path} 是回收站或隐藏的文件，跳过处理")
            return

        # 命中过滤关键字不处理
        if self._exclude_keywords:
            for keyword in self._exclude_keywords.split("\n"):
                if keyword and re.findall(keyword, event_path):
                    logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                    return

        # 不是媒体文件不处理
        if Path(event_path).suffix not in settings.RMT_MEDIAEXT:
            logger.debug(f"{event_path} 不是媒体文件")
            return

        # 文件发生变化
        logger.debug(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(is_directory=event.is_directory,
                           event_path=event_path,
                           source_dir=source_dir)

    @staticmethod
    def __make_folder_name(display_title: str, year: str, tmdb_id) -> str:
        """根据已知字段拼装文件夹名，遵循统一输出规则。"""
        if tmdb_id and year:
            return f"{display_title} ({year}) {{tmdb={tmdb_id}}}"
        elif tmdb_id:
            return f"{display_title} {{tmdb={tmdb_id}}}"
        elif year:
            return f"{display_title} ({year})"
        else:
            return display_title

    def __resolve_folder_name(self, title: str) -> str:
        """
        文件夹名解析主逻辑（TMDB 优先，站点降级）：

        1. 先向 TMDB 搜索（电视剧 → 电影）。
           - 找到 → 按输出规则返回 "标题 (年份) {tmdb=XXXXX}"。
        2. TMDB 未命中，且开启了站点降级（_site_fallback=True）：
           - 检测 MP 是否已关联 AGSVPT（pt.agsvpt.cn / www.agsvpt.com）或 麒麟（ilolicon.com）。
           - 已关联的站点按顺序搜索种子标题，取第一条种子标题作为 display_title，
             年份从种子标题里用正则提取（格式常见如 2024），无 tmdb_id。
           - 输出规则同上，但 tmdb_id 为 None，只有标题+年份（能提取时）。
        3. 以上均失败 → 直接返回原始 title，整理流程不中断。
        """
        # ── Step 1: TMDB 查询 ───────────────────────────────────────────────
        try:
            logger.info(f"[文件夹名解析] TMDB 查询：{title}")
            best = None
            tmdb_type = None

            try:
                tv_results = self.tmdbchain.search_tv(title) or []
                if tv_results:
                    best = tv_results[0]
                    tmdb_type = "电视剧"
            except Exception as e:
                logger.debug(f"TMDB 电视剧搜索异常：{e}")

            if not best:
                try:
                    movie_results = self.tmdbchain.search_movie(title) or []
                    if movie_results:
                        best = movie_results[0]
                        tmdb_type = "电影"
                except Exception as e:
                    logger.debug(f"TMDB 电影搜索异常：{e}")

            if best:
                tmdb_id = getattr(best, "tmdb_id", None) or getattr(best, "id", None)
                year_raw = (
                    getattr(best, "first_air_date", None)
                    or getattr(best, "release_date", None)
                    or ""
                )
                year = str(year_raw)[:4] if year_raw and len(str(year_raw)) >= 4 else ""
                display_title = (
                    getattr(best, "cn_name", None)
                    or getattr(best, "title", None)
                    or getattr(best, "name", None)
                    or title
                )
                folder = self.__make_folder_name(display_title, year, tmdb_id)
                logger.info(f"[文件夹名解析] TMDB {tmdb_type} 命中：[{title}] → [{folder}]")
                return folder

        except Exception as e:
            logger.error(f"[文件夹名解析] TMDB 查询异常：{title}，{e}", exc_info=True)

        logger.warning(f"[文件夹名解析] TMDB 未命中：{title}")

        # ── Step 2: 站点降级（可选） ────────────────────────────────────────
        if self._site_fallback:
            logger.info(f"[文件夹名解析] 尝试站点降级刮削：{title}")
            site_title = self.__resolve_title_from_site(title)
            if site_title:
                # 从种子标题里尝试提取年份（常见格式：空格或括号包裹的4位年份）
                year_match = re.search(r"[（(【\[\s]((?:19|20)\d{2})[）)】\]\s]", site_title)
                year = year_match.group(1) if year_match else ""
                # display_title 直接用站点种子标题（已是可读名称）
                folder = self.__make_folder_name(site_title, year, None)
                logger.info(f"[文件夹名解析] 站点降级命中：[{title}] → [{folder}]")
                return folder
            else:
                logger.warning(f"[文件夹名解析] 站点降级未命中：{title}")

        # ── Step 3: 全部失败，返回原始 title ────────────────────────────────
        logger.warning(f"[文件夹名解析] 降级完毕仍未找到，使用原始标题：{title}")
        return title

    def __resolve_title_from_site(self, title: str) -> Optional[str]:
        """
        从 AGSVPT 或 麒麟(ilolicon) 搜索种子，返回第一条种子的标题字符串。
        双Cookie模式：每个 Cookie 对应各自域名的 URL 独立请求。
        单站点模式：仅查询 www.agsvpt.com，失败即失败。
        返回 None 表示未找到或站点均未配置。
        """
        # ── AGSVPT ──
        agsv_entries = self.__get_agsvpt_site()
        for site, index, label in agsv_entries:
            # 双Cookie模式：每个entry只查自己对应的URL
            # 单站点模式：只有www条目，只查www URL
            url_map = {
                "pt.agsvpt.cn":  "https://pt.agsvpt.cn/torrents.php",
                "www.agsvpt.com": "https://www.agsvpt.com/torrents.php",
            }
            base_url = url_map.get(label, "https://www.agsvpt.com/torrents.php")
            try:
                req_url = self._AGSVPT_SEARCH_TPL.format(base=base_url, title=title)
                logger.info(f"[站点降级] 请求 AGSVPT {label}：{title}")
                page_source = self.__get_page_source(url=req_url, site=site)
                if not page_source:
                    continue
                _spider = SiteSpider(indexer=index, page=1)
                torrents = _spider.parse(page_source)
                if not torrents:
                    logger.debug(f"[站点降级] AGSVPT {label} 无结果")
                    continue
                torrent_title = torrents[0].get("title") or torrents[0].get("name") or ""
                if torrent_title:
                    logger.info(f"[站点降级] AGSVPT {label} 命中：{torrent_title}")
                    return torrent_title.strip()
            except Exception as e:
                logger.error(f"[站点降级] AGSVPT {label} 异常：{e}", exc_info=True)

        # ── 麒麟(ilolicon) 兜底 ──
        try:
            ilolicon_site = SiteOper().get_by_domain("ilolicon.com")
            ilolicon_index = SitesHelper().get_indexer("ilolicon.com")
            if ilolicon_site:
                req_url = (
                    "https://share.ilolicon.com/torrents.php"
                    f"?search_mode=0&search_area=0&page=0&notnewword=1&cat=402&search={title}"
                )
                logger.info(f"[站点降级] 请求 麒麟：{title}")
                page_source = self.__get_page_source(url=req_url, site=ilolicon_site)
                if page_source:
                    _spider = SiteSpider(indexer=ilolicon_index, page=1)
                    torrents = _spider.parse(page_source)
                    if torrents:
                        torrent_title = torrents[0].get("title") or torrents[0].get("name") or ""
                        if torrent_title:
                            logger.info(f"[站点降级] 麒麟 命中：{torrent_title}")
                            return torrent_title.strip()
        except Exception as e:
            logger.error(f"[站点降级] 麒麟 异常：{e}", exc_info=True)

        logger.warning(f"[站点降级] 所有站点均未找到：{title}")
        return None

    def __handle_file(self, is_directory: bool, event_path: str, source_dir: str):
        """
        同步一个文件
        :event.is_directory
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        """
        logger.info(f"文件 {event_path} 开始处理")
        try:
            # 转移路径
            dest_dir = self._dirconf.get(source_dir)
            # 是否重命名
            rename_conf = self._renameconf.get(source_dir)
            # 封面比例
            cover_conf = self._coverconf.get(source_dir)
            # 元数据
            file_meta = MetaInfoPath(Path(event_path))
            # 存储类型
            store_conf = self._storeconf.get(source_dir)

            if not file_meta.name:
                logger.error(f"{Path(event_path).name} 无法识别有效信息")
                return

            logger.debug(f"source_dir:{source_dir}")
            logger.debug(f"dest_dir:{dest_dir}")
            target_path = event_path.replace(source_dir, dest_dir)
            logger.debug(f"target_path:{target_path}")

            # 目录重命名
            if str(rename_conf) == "true" or str(rename_conf) == "false":
                rename_conf = bool(rename_conf)
                logger.debug(f"rename_conf:{rename_conf}")
                target = target_path.replace(dest_dir, "")
                logger.debug(f"target:{target}")
                parent = Path(Path(target).parents[0])
                logger.debug(f"parent:{parent}")
                last = target.replace(str(parent), "").replace("/", "")
                logger.debug(f"last:{last}")
                if rename_conf:
                    # 自定义识别词
                    raw_title, _ = WordsMatcher().prepare(str(parent.name))
                    logger.debug(f"raw_title:{raw_title}")
                    # TMDB 查询，生成标准文件夹名
                    title = self.__resolve_folder_name(raw_title)
                    logger.debug(f"title (with tmdb):{title}")
                    target_path = Path(dest_dir).joinpath(title).joinpath(last)
                    logger.debug(f"target_path:{target_path}")
                else:
                    title = parent.name
            else:
                if str(rename_conf) == "smart":
                    logger.debug(f"rename_conf:smart")
                    target = target_path.replace(dest_dir, "")
                    logger.debug(f"target:{target}")
                    parent = Path(Path(target).parents[0])
                    logger.debug(f"parent:{parent}")
                    last = target.replace(str(parent), "").replace("/", "")
                    logger.debug(f"last:{last}")
                    if parent.parent == parent:
                        raw_title = last.split(".")[0]
                    else:
                        raw_title = parent.name.split(".")[0]
                    logger.debug(f"raw_title:{raw_title}")
                    # TMDB 查询，生成标准文件夹名
                    title = self.__resolve_folder_name(raw_title)
                    logger.debug(f"title (with tmdb):{title}")
                    target_path = Path(dest_dir).joinpath(title).joinpath(last)
                    logger.debug(f"target_path:{target_path}")
                else:
                    logger.error(f"{target_path} 智能重命名失败")
                    return

            # 文件夹同步创建
            if is_directory:
                if store_conf == "local" and not Path(target_path).exists():
                    logger.info(f"创建目标文件夹 {target_path}")
                    os.makedirs(target_path)
            else:
                # 媒体重命名
                try:
                    pattern = r'S\d+E\d+'
                    matches = re.search(pattern, Path(target_path).name)
                    if matches:
                        target_path = Path(
                            target_path).parent / f"{matches.group()}{Path(Path(target_path).name).suffix}"
                        logger.debug(f"target_path:{target_path}")
                    else:
                        print("未找到匹配的季数和集数")
                except Exception as e:
                    logger.error(f"媒体重命名 error: {e}", exc_info=True)

                # 目标文件夹不存在则创建
                if store_conf == "local" and not Path(target_path).parent.exists():
                    logger.info(f"创建目标文件夹 {Path(target_path).parent}")
                    os.makedirs(Path(target_path).parent)

                # 文件：nfo、图片、视频文件
                if store_conf == "local" and Path(target_path).exists():
                    logger.debug(f"目标文件 {target_path} 已存在")
                    return

                if store_conf == "local":
                    # 本地：硬链接/移动/复制，立即执行
                    retcode = self.__transfer_command(file_item=Path(event_path),
                                                      target_file=target_path,
                                                      transfer_type=self._transfer_type)
                else:
                    # 网盘：不立即上传，先做本地刮削，加入批量上传队列
                    # ── 去重1：持久化记录检查，防止重启后重复处理 ──
                    if self.__is_handled(event_path):
                        logger.info(f"[去重] 文件已在历史记录中，跳过：{Path(event_path).name}")
                        return
                    # ── 去重2：内存队列检查，防止同次运行内 watchdog 重复触发 ──
                    existing_entry = self._upload_queue.get(title, {})
                    existing_paths = {f["event_path"] for f in existing_entry.get("files", [])}
                    if event_path in existing_paths:
                        logger.info(f"[上传队列] 文件已在队列中，跳过重复入队：{Path(event_path).name}")
                        return

                    # 目标目录在本地临时区预创建，用于存放 nfo/poster
                    tmp_dir = Path("/tmp/shortplaymonitormod") / target_path.parent.relative_to(Path("/"))
                    tmp_dir.mkdir(parents=True, exist_ok=True)

                    # 加入上传队列（视频文件），target_path 统一转为字符串存储
                    entry = self._upload_queue.get(title, {
                        "files": [], "last_time": self.__get_time(),
                        "store_conf": store_conf
                    })
                    entry["files"].append({
                        "event_path": str(event_path),
                        "target_path": str(target_path),
                    })
                    entry["last_time"] = self.__get_time()
                    self._upload_queue[title] = entry
                    logger.info(
                        f"[上传队列] 剧集「{title}」已入队 {len(entry['files'])} 个文件，"
                        f"等待静默后批量上传：{Path(event_path).name}"
                    )
                    # 网盘模式本阶段视为"准备完成"，后续由 __batch_upload 真正执行
                    retcode = 0

                if retcode == 0:
                    if store_conf == "local":
                        logger.info(f"文件 {event_path} 本地整理完成")

                    # 生成 tvshow.nfo
                    logger.debug(f"文件 {event_path} 生成 tvshow.nfo 开始")
                    if store_conf == "local":
                        if not (target_path.parent / "tvshow.nfo").exists():
                            self.__gen_tv_nfo_file(dir_path=target_path.parent, title=title)
                    else:
                        # 网盘模式：生成到本地临时目录，由批量上传时一并上传
                        tmp_nfo = Path("/tmp/shortplaymonitormod") / target_path.parent.relative_to(Path("/")) / "tvshow.nfo"
                        if not tmp_nfo.exists():
                            self.__gen_tv_nfo_file(dir_path=tmp_nfo.parent, title=title)
                            logger.debug(f"[刮削] tvshow.nfo 已生成到临时目录：{tmp_nfo}")
                        # 将 nfo 也加入上传队列（去重：按 event_path 字符串比对）
                        if tmp_nfo.exists():
                            nfo_ep = str(tmp_nfo)
                            existing_eps = {f["event_path"] for f in self._upload_queue[title]["files"]}
                            if nfo_ep not in existing_eps:
                                self._upload_queue[title]["files"].append({
                                    "event_path": nfo_ep,
                                    "target_path": str(target_path.parent / "tvshow.nfo"),
                                })

                    # 封面裁剪（持久开关 _image 控制，默认开启）
                    if self._image:
                        logger.debug(f"文件 {event_path} 生成缩略图开始")
                        if store_conf == "local":
                            if not (target_path.parent / "poster.jpg").exists():
                                thumb_path = self.gen_file_thumb(title=title,
                                                                 rename_conf=rename_conf,
                                                                 file_path=target_path)
                                if thumb_path and Path(thumb_path).exists():
                                    self.__save_poster(input_path=thumb_path,
                                                       poster_path=target_path.parent / "poster.jpg",
                                                       cover_conf=cover_conf)
                                    if (target_path.parent / "poster.jpg").exists():
                                        logger.info(f"{target_path.parent / 'poster.jpg'} 缩略图已生成")
                                    thumb_path.unlink()
                                else:
                                    thumb_files = SystemUtils.list_files(
                                        directory=target_path.parent, extensions=[".jpg"])
                                    if thumb_files:
                                        for thumb in thumb_files:
                                            self.__save_poster(input_path=thumb,
                                                               poster_path=target_path.parent / "poster.jpg",
                                                               cover_conf=cover_conf)
                                            break
                                        for thumb in thumb_files:
                                            Path(thumb).unlink()
                            else:
                                # poster 已存在，重新裁剪
                                self.__save_poster(input_path=target_path.parent / "poster.jpg",
                                                   poster_path=target_path.parent / "poster.jpg",
                                                   cover_conf=cover_conf)
                                logger.info(f"{target_path.parent / 'poster.jpg'} 封面已重新裁剪")
                        else:
                            # 网盘模式：生成 poster 到本地临时目录，由批量上传时一并上传
                            tmp_poster = (Path("/tmp/shortplaymonitormod") /
                                          target_path.parent.relative_to(Path("/")) / "poster.jpg")
                            if not tmp_poster.exists():
                                thumb_path = self.gen_file_thumb(
                                    title=title, rename_conf=rename_conf,
                                    file_path=Path(event_path),
                                    to_thumb_path=tmp_poster.parent)
                                if thumb_path and Path(thumb_path).exists():
                                    self.__save_poster(input_path=thumb_path,
                                                       poster_path=tmp_poster,
                                                       cover_conf=cover_conf)
                                    if tmp_poster.exists():
                                        logger.info(f"[刮削] poster.jpg 已生成到临时目录：{tmp_poster}")
                                    thumb_path.unlink()
                            # 将 poster 加入上传队列（去重：按 event_path 字符串比对）
                            if tmp_poster.exists():
                                poster_ep = str(tmp_poster)
                                existing_eps = {f["event_path"] for f in self._upload_queue[title]["files"]}
                                if poster_ep not in existing_eps:
                                    self._upload_queue[title]["files"].append({
                                        "event_path": poster_ep,
                                        "target_path": str(target_path.parent / "poster.jpg"),
                                    })
                    else:
                        logger.debug(f"封面裁剪开关已关闭，跳过缩略图生成")

                    # 本地模式：整理成功后直接记入CMS通知队列
                    if store_conf == "local":
                        if self._cms_enabled and self._cms_domain and self._cms_api_token:
                            now = self.__get_time()
                            n_entry = self._pending_notify.get(title, {
                                "count": 0, "last_time": now, "video_files": []
                            })
                            n_entry["count"] += 1
                            n_entry["last_time"] = now
                            # 带上视频文件路径，CMS通知成功后用于写 _medias
                            n_entry.setdefault("video_files", []).append(str(event_path))
                            self._pending_notify[title] = n_entry
                            logger.info(
                                f"[CMS待通知] 剧集「{title}」已整理 {n_entry['count']} 集，"
                                f"最后更新：{Path(event_path).name}"
                            )
                else:
                    logger.error(f"文件 {event_path} 本地整理失败，错误码：{retcode}")

            # _medias 不在此处写入，改为在上传+CMS完成后才记录，
            # 确保通知在整个流程（上传→CMS）完成后才发出。
            # 见 __batch_upload（网盘模式）和 __notify_cms（CMS完成后）写入逻辑。
            # 本地且未开启CMS时，在 __handle_file_local_done 中写入。
            if self._notify and store_conf == "local" and not self._cms_enabled:
                # 本地模式且未开启CMS：整理完成即为全部完成，直接记录
                self.__record_media(title, event_path)

        except Exception as e:
            logger.error(f"event_handler_created error: {e}", exc_info=True)
        # 本地模式的临时文件立即清理；网盘模式临时文件由 __batch_upload 上传后统一清理
        if store_conf == "local" and Path('/tmp/shortplaymonitormod/').exists():
            shutil.rmtree('/tmp/shortplaymonitormod/')
        logger.info(f"文件 {event_path} 处理完成")

    def __load_handled_record(self) -> set:
        """从持久化文件加载已处理的源文件路径集合，重启后防止重复上传。"""
        try:
            if self._HANDLED_RECORD_PATH.exists():
                import json
                data = json.loads(self._HANDLED_RECORD_PATH.read_text(encoding="utf-8"))
                paths = set(data.get("handled", []))
                logger.info(f"[去重记录] 已加载 {len(paths)} 条历史处理记录")
                return paths
        except Exception as e:
            logger.error(f"[去重记录] 加载历史记录失败：{e}")
        return set()

    def __save_handled_record(self):
        """将已处理路径集合持久化到文件。"""
        try:
            import json
            self._HANDLED_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._HANDLED_RECORD_PATH.write_text(
                json.dumps({"handled": list(self._handled_paths)}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"[去重记录] 保存历史记录失败：{e}")

    def __mark_handled(self, event_path: str):
        """标记一个源文件已处理，并持久化。"""
        self._handled_paths.add(str(event_path))
        self.__save_handled_record()

    def __is_handled(self, event_path: str) -> bool:
        """判断源文件是否已处理过。"""
        return str(event_path) in self._handled_paths

    def __record_media(self, title: str, event_path: str):
        """
        将一个已完成整理的文件记录到 _medias，供 send_msg 汇总通知。
        只在整个处理链（上传+CMS）完成后才调用，确保通知时序正确。
        """
        if not self._notify:
            return
        media_list = self._medias.get(title) or {}
        media_files = media_list.get("files") or []
        if str(event_path) not in media_files:
            media_files.append(str(event_path))
        self._medias[title] = {
            "files": media_files,
            "time": datetime.datetime.now()
        }
        logger.debug(f"[通知记录] 剧集「{title}」累计 {len(media_files)} 集待通知")

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息。
        改为全剧静默（interval秒内无新文件）后发一次，统计总集数，不再每集发一条。
        """
        if not self._notify:
            return
        if not self._medias:
            return

        now = datetime.datetime.now()
        for title in list(self._medias.keys()):
            media_list = self._medias.get(title)
            if not media_list:
                continue
            last_update_time = media_list.get("time")
            media_files = media_list.get("files") or []
            if not last_update_time or not media_files:
                continue
            # 只有静默超过 interval 秒（无新文件进来）才发通知
            elapsed = (now - last_update_time).total_seconds()
            if elapsed > int(self._interval):
                ep_count = len(media_files)
                logger.info(f"[通知] 剧集「{title}」共 {ep_count} 集入库，发送汇总消息")
                self.post_message(
                    mtype=NotificationType.Organize,
                    title=f"【短剧入库】{title}",
                    text=f"共整理 {ep_count} 集已入库完成"
                )
                del self._medias[title]

    # ===== CMS通知相关方法 =====

    def __get_time(self):
        return int(time.time())

    def __batch_upload(self):
        """
        定时任务（每30秒）：检查上传队列，对静默超过60秒的剧集执行批量上传。
        上传顺序：视频文件 → nfo → poster，全部完成后记入CMS通知队列。
        不改动 MP 的 115.py 等存储模块，直接复用 TransHandler。
        """
        if not self._upload_queue:
            return

        now = self.__get_time()
        ready_titles = [
            t for t, e in self._upload_queue.items()
            if now - e["last_time"] >= 60
        ]
        if not ready_titles:
            for t, e in self._upload_queue.items():
                logger.info(
                    f"[批量上传] 剧集「{t}」队列 {len(e['files'])} 个文件，"
                    f"距上次入队 {now - e['last_time']:.0f}s，继续等待..."
                )
            return

        for title in ready_titles:
            entry = self._upload_queue.pop(title)
            store_conf = entry["store_conf"]
            files = entry["files"]
            logger.info(f"[批量上传] 剧集「{title}」开始批量上传，共 {len(files)} 个文件，目标存储：{store_conf}")

            try:
                source_oper = self.filemanager._FileManagerModule__get_storage_oper("local")
                target_oper = self.filemanager._FileManagerModule__get_storage_oper(store_conf)
                if not source_oper or not target_oper:
                    logger.error(f"[批量上传] 不支持的存储类型：{store_conf}")
                    continue

                success_count = 0
                fail_count = 0
                for f in files:
                    event_path = f["event_path"]
                    target_path = f["target_path"]
                    try:
                        file_item = FileItem()
                        file_item.storage = "local"
                        file_item.path = str(event_path)
                        new_item, errmsg = TransHandler._TransHandler__transfer_command(
                            fileitem=file_item,
                            target_storage=store_conf,
                            target_file=Path(target_path),
                            transfer_type=self._transfer_type,
                            source_oper=source_oper,
                            target_oper=target_oper
                        )
                        if new_item:
                            success_count += 1
                            # 上传成功后持久化标记，防止重启后重复上传
                            self.__mark_handled(event_path)
                            logger.debug(f"[批量上传] ✓ {Path(event_path).name} → {target_path}")
                        else:
                            fail_count += 1
                            logger.error(f"[批量上传] ✗ {Path(event_path).name} 失败：{errmsg}")
                    except Exception as e:
                        fail_count += 1
                        logger.error(f"[批量上传] ✗ {Path(event_path).name} 异常：{e}", exc_info=True)

                logger.info(
                    f"[批量上传] 剧集「{title}」上传完成，"
                    f"成功 {success_count} 个，失败 {fail_count} 个"
                )

                # 上传完成后的后续处理
                if success_count > 0:
                    # 只上传视频文件（排除 nfo/poster）才计入集数
                    video_exts = {e.lower() for e in settings.RMT_MEDIAEXT}
                    video_files = [
                        f for f in files
                        if Path(f["event_path"]).suffix.lower() in video_exts
                    ]
                    video_count = len(video_files)

                    if self._cms_enabled and self._cms_domain and self._cms_api_token:
                        # 有CMS：记入通知队列，_medias 在CMS通知成功后写入
                        # 同时把视频文件列表带过去，CMS成功后用于写 _medias
                        n_entry = self._pending_notify.get(title, {
                            "count": 0, "last_time": now, "video_files": []
                        })
                        n_entry["count"] += video_count
                        n_entry["last_time"] = now
                        n_entry.setdefault("video_files", []).extend(
                            f["event_path"] for f in video_files
                        )
                        self._pending_notify[title] = n_entry
                        logger.info(
                            f"[批量上传] 剧集「{title}」{video_count} 集已记入CMS通知队列，"
                            f"等待CMS通知完成后发送入库通知"
                        )
                    else:
                        # 无CMS：上传完成即为全部完成，直接写 _medias 触发通知
                        for f in video_files:
                            self.__record_media(title, f["event_path"])
                        logger.info(
                            f"[批量上传] 剧集「{title}」{video_count} 集上传完成，"
                            f"已记录入库通知（无CMS）"
                        )

            except Exception as e:
                logger.error(f"[批量上传] 剧集「{title}」批量上传异常：{e}", exc_info=True)

            finally:
                # 清理临时目录
                if Path('/tmp/shortplaymonitormod/').exists():
                    shutil.rmtree('/tmp/shortplaymonitormod/')

    def __notify_cms(self):
        """
        定时检查（每分钟）并通知CMS执行增量同步。
        按剧集分组追踪，每个剧集最后一集整理完成后静默60秒再发通知，
        避免一集一集上传时频繁触发CMS增量同步。
        所有待通知剧集合并为一次CMS请求。
        """
        try:
            if not self._pending_notify:
                return

            now = self.__get_time()
            ready_titles = []

            for title, entry in list(self._pending_notify.items()):
                elapsed = now - entry["last_time"]
                if elapsed >= 60:
                    # 该剧集已静默60秒，视为全部上传完毕
                    ready_titles.append(title)
                    logger.info(
                        f"[CMS通知] 剧集「{title}」共 {entry['count']} 集，"
                        f"已静默 {elapsed:.0f}s，准备通知CMS"
                    )
                else:
                    logger.info(
                        f"[CMS等待] 剧集「{title}」共 {entry['count']} 集，"
                        f"距上次整理 {elapsed:.0f}s，继续等待..."
                    )

            if not ready_titles:
                return

            # 所有就绪剧集合并触发一次CMS通知
            url = (f"{self._cms_domain}/api/sync/lift_by_token"
                   f"?token={self._cms_api_token}&type={self._cms_notify_type}")
            ret = RequestUtils().get_res(url)
            if ret:
                total = sum(self._pending_notify[t]["count"] for t in ready_titles)
                logger.info(
                    f"[CMS通知] 成功！本次通知剧集：{ready_titles}，共 {total} 集"
                )
                # CMS 通知成功后，才写入 _medias 触发入库通知
                # video_files 由 __batch_upload（网盘）或本地整理时写入 pending_notify
                for t in ready_titles:
                    video_files = self._pending_notify[t].get("video_files", [])
                    if video_files:
                        for vf in video_files:
                            self.__record_media(t, vf)
                        logger.info(f"[CMS通知] 剧集「{t}」{len(video_files)} 集已记录入库通知")
                    else:
                        # 兜底：没有具体文件列表时用集数生成占位记录
                        count = self._pending_notify[t]["count"]
                        for i in range(count):
                            self.__record_media(t, f"episode_{i+1}")
                    del self._pending_notify[t]
            elif ret is not None:
                logger.error(
                    f"[CMS通知] 失败，状态码：{ret.status_code}，"
                    f"返回信息：{ret.text} {ret.reason}"
                )
            else:
                logger.error("[CMS通知] 失败，未获取到返回信息")

        except Exception as e:
            logger.error(f"[CMS通知] 发生异常：{e}", exc_info=True)

    # ===== 工具方法 =====

    @staticmethod
    def __transfer_command(file_item: Path, target_file: Path, transfer_type: str) -> int:
        with lock:
            if transfer_type == 'link':
                retcode, retmsg = SystemUtils.link(file_item, target_file)
            elif transfer_type == 'filesoftlink':
                retcode, retmsg = SystemUtils.softlink(file_item, target_file)
            elif transfer_type == 'move':
                retcode, retmsg = SystemUtils.move(file_item, target_file)
            else:
                retcode, retmsg = SystemUtils.copy(file_item, target_file)
        if retcode != 0:
            logger.error(retmsg)
        return retcode

    def __save_poster(self, input_path, poster_path, cover_conf):
        try:
            image = Image.open(input_path)
            if not cover_conf:
                target_ratio = 2 / 3
            else:
                covers = cover_conf.split(":")
                target_ratio = int(covers[0]) / int(covers[1])
            original_ratio = image.width / image.height
            if original_ratio > target_ratio:
                new_height = image.height
                new_width = int(new_height * target_ratio)
            else:
                new_width = image.width
                new_height = int(new_width / target_ratio)
            left = (image.width - new_width) // 2
            top = (image.height - new_height) // 2
            right = left + new_width
            bottom = top + new_height
            cropped_image = image.crop((left, top, right, bottom))
            cropped_image.save(poster_path)
            logger.debug(f"__save_poster: {poster_path}")
        except Exception as e:
            logger.error(f"__save_poster error: {e}", exc_info=True)

    def __gen_tv_nfo_file(self, dir_path: Path, title: str):
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        desc = self.gen_desc_from_site(title=title)
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        DomUtils.add_node(doc, root, "title", title)
        DomUtils.add_node(doc, root, "originaltitle", title)
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        if desc:
            DomUtils.add_node(doc, root, "plot", desc)
        self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

    def __save_nfo(self, doc, file_path: Path):
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")

    def gen_file_thumb_from_site(self, title: str, file_path: Path):
        try:
            image = None
            # ── AGSVPT ──
            url_map = {
                "pt.agsvpt.cn":   "https://pt.agsvpt.cn/torrents.php",
                "www.agsvpt.com": "https://www.agsvpt.com/torrents.php",
            }
            for site, index, label in self.__get_agsvpt_site():
                base_url = url_map.get(label, "https://www.agsvpt.com/torrents.php")
                req_url = self._AGSVPT_SEARCH_TPL.format(base=base_url, title=title)
                logger.info(f"[封面] 检索 AGSVPT {label}：{title}")
                image = self.__get_site_torrents(
                    url=req_url, site=site, index=index,
                    image_xpath="//*[@id='kdescr']/img[1]/@src"
                )
                if image:
                    logger.info(f"[封面] AGSVPT {label} 命中")
                    break
                logger.debug(f"[封面] AGSVPT {label} 未找到")
            # ── 麒麟(ilolicon) 兜底 ──
            if not image:
                ilolicon_site = SiteOper().get_by_domain("ilolicon.com")
                ilolicon_index = SitesHelper().get_indexer("ilolicon.com")
                if ilolicon_site:
                    req_url = (f"https://share.ilolicon.com/torrents.php"
                               f"?search_mode=0&search_area=0&page=0&notnewword=1&cat=402&search={title}")
                    logger.info(f"[封面] 检索 麒麟：{title}")
                    image = self.__get_site_torrents(
                        url=req_url, site=ilolicon_site, index=ilolicon_index,
                        image_xpath="//*[@id='kdescr']/img[1]/@src"
                    )
            if not image:
                logger.error(f"[封面] 所有站点均未找到：{title}")
                return None
            if self.__save_image(url=image, file_path=file_path):
                return file_path
            return None
        except Exception as e:
            logger.error(f"[封面] 检索异常：{title}，{str(e)}", exc_info=True)
            return None

    def gen_desc_from_site(self, title: str):
        try:
            desc = None
            # ── AGSVPT ──
            url_map = {
                "pt.agsvpt.cn":   "https://pt.agsvpt.cn/torrents.php",
                "www.agsvpt.com": "https://www.agsvpt.com/torrents.php",
            }
            for site, index, label in self.__get_agsvpt_site():
                base_url = url_map.get(label, "https://www.agsvpt.com/torrents.php")
                req_url = self._AGSVPT_SEARCH_TPL.format(base=base_url, title=title)
                logger.info(f"[简介] 检索 AGSVPT {label}：{title}")
                desc = self.__get_site_torrents(
                    url=req_url, site=site, index=index,
                    desc_xpath="//*[@id='kdescr']/text()"
                )
                if desc:
                    logger.info(f"[简介] AGSVPT {label} 命中")
                    break
                logger.debug(f"[简介] AGSVPT {label} 未找到")
            # ── 麒麟(ilolicon) 兜底 ──
            if not desc:
                ilolicon_site = SiteOper().get_by_domain("ilolicon.com")
                ilolicon_index = SitesHelper().get_indexer("ilolicon.com")
                if ilolicon_site:
                    req_url = (f"https://share.ilolicon.com/torrents.php"
                               f"?search_mode=0&search_area=0&page=0&notnewword=1&cat=402&search={title}")
                    logger.info(f"[简介] 检索 麒麟：{title}")
                    desc = self.__get_site_torrents(
                        url=req_url, site=ilolicon_site, index=ilolicon_index,
                        desc_xpath="//*[@id='kdescr']/text()"
                    )
            if not desc:
                logger.error(f"[简介] 所有站点均未找到：{title}")
                return None
            return desc
        except Exception as e:
            logger.error(f"[简介] 检索异常：{title}，{str(e)}", exc_info=True)
            return None

    @retry(RequestException, logger=logger)
    def __save_image(self, url: str, file_path: Path):
        try:
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            r = RequestUtils().get_res(url=url, raise_exception=True)
            if r:
                file_path.write_bytes(r.content)
                logger.info(f"图片已保存：{file_path}")
                return True
            else:
                logger.info(f"{file_path.stem}图片下载失败，请检查网络连通性")
                return False
        except RequestException as err:
            raise err
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}", exc_info=True)
            return False

    def __get_site_torrents(self, url: str, site, index, image_xpath=None, desc_xpath=None):
        page_source = self.__get_page_source(url=url, site=site)
        if not page_source:
            logger.error(f"请求站点 {site.name} 失败")
            return None
        _spider = SiteSpider(indexer=index, page=1)
        torrents = _spider.parse(page_source)
        if not torrents:
            logger.error(f"未检索到站点 {site.name} 资源")
            return None
        torrent_detail_source = self.__get_page_source(url=torrents[0].get("page_url"), site=site)
        if not torrent_detail_source:
            logger.error(f"请求种子详情页失败 {torrents[0].get('page_url')}")
            return None
        html = etree.HTML(torrent_detail_source)
        logger.debug(f"种子详情页 {torrents[0].get('page_url')} 解析成功")
        if image_xpath:
            image = html.xpath(image_xpath)
            logger.debug(f"image: {image}")
            if not image:
                logger.error(f"未获取到种子封面 {torrents[0].get('page_url')}")
                return None
            return str(image)
        if desc_xpath:
            desc = html.xpath(desc_xpath)
            logger.debug(f"desc: {desc}")
            logger.debug(f"clean_text_list: {self.clean_text_list(desc)[-1]}")
            if not desc:
                logger.error(f"未获取到种子简介 {torrents[0].get('page_url')}")
                return None
            return self.clean_text_list(desc)[-1]

    # AGSVPT 搜索 URL 模板
    _AGSVPT_SEARCH_TPL = (
        "{base}?search_mode=0&search_area=0&page=0&notnewword=1&cat=419&search={title}"
    )

    def __get_agsvpt_site(self):
        """
        返回可用于 AGSVPT 请求的配置列表，每项为 (cookie, index, label)。

        双 Cookie 模式（_agsvpt_dual_cookie=True）：
          - 直接使用 WebUI 填写的两个 Cookie，各自对应一个 URL，互相独立请求。
          - 返回 [(cookie_pt, index_pt, "pt.agsvpt.cn"),
                  (cookie_www, index_www, "www.agsvpt.com")]
            其中 Cookie 为空的条目会被跳过。

        单站点模式（默认）：
          - 从 MP 站点管理里找已配置的 AGSVPT（agsvpt.com），只用这一个 Cookie。
          - 仅查询 www.agsvpt.com，失败即失败，不做 URL 轮询。
          - 返回 [(mp_cookie, mp_index, "www.agsvpt.com")]

        返回空列表表示无可用配置。
        """
        if self._agsvpt_dual_cookie:
            # 双 Cookie 模式：用 WebUI 填写的 Cookie 构造伪 site 对象
            # indexer 统一使用 agsvpt.com 的（两个域名结构相同，pt.agsvpt.cn 在MP中没有单独注册）
            agsvpt_index = SitesHelper().get_indexer("agsvpt.com")

            results = []
            for cookie, label in [
                (self._agsvpt_cookie_pt,  "pt.agsvpt.cn"),
                (self._agsvpt_cookie_www, "www.agsvpt.com"),
            ]:
                if not cookie or not cookie.strip():
                    logger.debug(f"[AGSVPT] 双Cookie模式：{label} Cookie 未填写，跳过")
                    continue
                # 构造一个只含 cookie 属性的简单对象，供 __get_page_source 使用
                class _FakeSite:
                    def __init__(self, c, n): self.cookie = c; self.name = n
                results.append((_FakeSite(cookie.strip(), label), agsvpt_index, label))
            if not results:
                logger.warning("[AGSVPT] 双Cookie模式已开启但两个Cookie均未填写")
            return results
        else:
            # 单站点模式：从 MP 站点管理取已配置的 AGSVPT
            site = SiteOper().get_by_domain("agsvpt.com")
            index = SitesHelper().get_indexer("agsvpt.com")
            if site:
                logger.debug("[AGSVPT] 单站点模式：使用 MP 配置的 www.agsvpt.com")
                return [(site, index, "www.agsvpt.com")]
            logger.debug("[AGSVPT] 单站点模式：MP 中未配置 agsvpt.com")
            return []

    def __get_page_source(self, url: str, site):
        ret = RequestUtils(
            cookies=site.cookie,
            timeout=30,
        ).get_res(url, allow_redirects=True)
        if ret is not None:
            raw_data = ret.content
            if raw_data:
                try:
                    result = chardet.detect(raw_data)
                    encoding = result['encoding']
                    page_source = raw_data.decode(encoding)
                except Exception as e:
                    if re.search(r"charset=\"?utf-8\"?", ret.text, re.IGNORECASE):
                        ret.encoding = "utf-8"
                    else:
                        ret.encoding = ret.apparent_encoding
                    page_source = ret.text
            else:
                page_source = ret.text
        else:
            page_source = ""
        return page_source

    def gen_file_thumb(self, title: str, file_path: Path, rename_conf: str, to_thumb_path: Path = None):
        if str(rename_conf) == "smart":
            if not to_thumb_path:
                thumb_path = file_path.with_name(file_path.stem + "-site.jpg")
            else:
                thumb_path = to_thumb_path.joinpath(file_path.stem + "-site.jpg")
            if thumb_path.exists():
                logger.info(f"缩略图已存在：{thumb_path}")
                return
            self.gen_file_thumb_from_site(title=title, file_path=thumb_path)
            if Path(thumb_path).exists():
                logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
                return thumb_path
        with ffmpeg_lock:
            try:
                if not to_thumb_path:
                    thumb_path = file_path.with_name(file_path.stem + "-thumb.jpg")
                else:
                    thumb_path = to_thumb_path.joinpath(file_path.stem + "-thumb.jpg")
                if thumb_path.exists():
                    logger.info(f"缩略图已存在：{thumb_path}")
                    return
                self.get_thumb(video_path=str(file_path),
                               image_path=str(thumb_path),
                               frames=self._timeline)
                if Path(thumb_path).exists():
                    logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
                    return thumb_path
            except Exception as err:
                logger.error(f"FFmpeg处理文件 {file_path} 时发生错误：{str(err)}", exc_info=True)
                return None

    @staticmethod
    def get_thumb(video_path: str, image_path: str, frames: str = None):
        if not frames:
            frames = "00:00:10"
        if not video_path or not image_path:
            return False
        cmd = 'ffmpeg -y -i "{video_path}" -ss {frames} -frames 1 "{image_path}"'.format(
            video_path=video_path,
            frames=frames,
            image_path=image_path)
        result = SystemUtils.execute(cmd)
        if result:
            return True
        return False

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "exclude_keywords": self._exclude_keywords,
            "transfer_type": self._transfer_type,
            "onlyonce": self._onlyonce,
            "interval": self._interval,
            "notify": self._notify,
            "image": self._image,
            "monitor_confs": self._monitor_confs,
            "cms_enabled": self._cms_enabled,
            "cms_notify_type": self._cms_notify_type,
            "cms_domain": self._cms_domain,
            "cms_api_token": self._cms_api_token,
            "site_fallback": self._site_fallback,
            "agsvpt_dual_cookie": self._agsvpt_dual_cookie,
            "agsvpt_cookie_pt": self._agsvpt_cookie_pt,
            "agsvpt_cookie_www": self._agsvpt_cookie_www,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    # ===== 第一行：短剧监控开关 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 2},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 2},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 2},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'image',
                                            'label': '封面裁剪',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 2},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'site_fallback',
                                            'label': 'TMDB失败时站点降级（AGSVPT/麒麟）',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ===== 第二行：转移方式 + 消息延迟 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '入库消息延迟',
                                            'placeholder': '10'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== 第三行：监控目录 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '监控方式#监控目录#目的目录#是否重命名#封面比例'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== 第四行：排除关键词 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== 分隔线：AGSVPT 双Cookie配置区 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '── AGSVPT 双域名 Cookie 配置（可选） ──'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'agsvpt_dual_cookie',
                                            'label': '启用AGSVPT双域名Cookie',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '开启后不依赖MP站点管理，使用下方填写的Cookie依次查询两个域名；关闭则仅使用MP站点管理中配置的 agsvpt.com。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'agsvpt_cookie_pt',
                                            'label': 'pt.agsvpt.cn Cookie',
                                            'placeholder': '留空则跳过此域名',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'agsvpt_cookie_www',
                                            'label': 'www.agsvpt.com Cookie',
                                            'placeholder': '留空则跳过此域名',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== 分隔线：CMS通知配置区 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '── CMS增量同步通知配置（可选） ──'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== CMS开关 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'cms_enabled',
                                            'label': '启用CMS通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'cms_notify_type',
                                            'label': 'CMS通知类型',
                                            'items': [
                                                {'title': '增量同步', 'value': 'lift_sync'},
                                                {'title': '增量同步+自动整理', 'value': 'auto_organize'},
                                            ]
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ===== CMS地址 + Token =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cms_domain',
                                            'label': 'CMS地址',
                                            'placeholder': 'http://172.17.0.1:9527'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cms_api_token',
                                            'label': 'CMS_API_TOKEN'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ===== 说明提示 =====
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': (
                                                '短剧监控说明：'
                                                'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/ShortPlayMonitor.md'
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': (
                                                'CMS通知说明：整理完成后会等待60秒静默期（期间无新文件）才通知CMS，'
                                                '避免频繁触发。CMS版本需 ≥ 0.3.5.11：https://wiki.cmscc.cc'
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '当重命名方式为smart时，如站点管理已配置AGSV、ilolicon，则优先从站点获取短剧封面。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "image": True,
            "notify": False,
            "interval": 10,
            "monitor_confs": "",
            "exclude_keywords": "",
            "transfer_type": "link",
            "site_fallback": True,
            "agsvpt_dual_cookie": False,
            "agsvpt_cookie_pt": "",
            "agsvpt_cookie_www": "",
            "cms_enabled": False,
            "cms_notify_type": "lift_sync",
            "cms_api_token": "cloud_media_sync",
            "cms_domain": "http://172.17.0.1:9527",
        }

    def get_page(self) -> List[dict]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e), exc_info=True)

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []

    def clean_text_list(self, text_list):
        cleaned = []
        for line in text_list:
            line = line.strip()
            line = line.replace('\u3000', ' ').replace('\xa0', ' ')
            line = re.sub(r'[ \u3000\xa0]+', ' ', line)
            if line:
                cleaned.append(line)
        return cleaned