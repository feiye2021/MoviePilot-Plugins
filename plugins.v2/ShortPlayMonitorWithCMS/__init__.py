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
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "feiye"
    # 作者主页
    author_url = "https://github.com/feiye2021/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "shortplaywithcms_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # ===== 短剧监控私有属性 =====
    _enabled = False
    _monitor_confs = None
    _onlyonce = False
    _image = False
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

    # ===== CMS通知私有属性 =====
    _cms_enabled = False
    _cms_notify_type = None
    _cms_domain = None
    _cms_api_token = None
    _last_event_time = 0
    _wait_notify_count = 0

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._renameconf = {}
        self._coverconf = {}
        self._storeconf = {}
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

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

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

        if self._image:
            self._image = False
            self.__update_config()
            self.__handle_image()

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
                    # 自定义识别次
                    title, _ = WordsMatcher().prepare(str(parent.name))
                    logger.debug(f"title:{title}")
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
                        title = last.split(".")[0]
                    else:
                        title = parent.name.split(".")[0]
                    logger.debug(f"title:{title}")
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
                    # 硬链接/移动/复制
                    retcode = self.__transfer_command(file_item=Path(event_path),
                                                      target_file=target_path,
                                                      transfer_type=self._transfer_type)
                else:
                    # 源操作对象
                    source_oper = self.filemanager._FileManagerModule__get_storage_oper("local")
                    # 目的操作对象
                    target_oper = self.filemanager._FileManagerModule__get_storage_oper(store_conf)
                    if not source_oper or not target_oper:
                        return None, f"不支持的存储类型：{store_conf}"
                    file_item = FileItem()
                    file_item.storage = "local"
                    file_item.path = event_path
                    new_item, errmsg = TransHandler._TransHandler__transfer_command(fileitem=file_item,
                                                                                    target_storage=store_conf,
                                                                                    target_file=Path(
                                                                                        target_path),
                                                                                    transfer_type=self._transfer_type,
                                                                                    source_oper=source_oper,
                                                                                    target_oper=target_oper)
                    logger.debug(f"new_item: {new_item} ")
                    if new_item:
                        retcode = 0
                        logger.debug(f"new_item: {new_item} ")
                    else:
                        retcode = 1
                        logger.debug(f"文件整理错误 {errmsg} ")

                if retcode == 0:
                    if store_conf == "local":
                        logger.info(f"文件 {event_path} 硬链接完成")
                    else:
                        logger.info(f"文件 {event_path} 上传完成")

                    # ===== 文件整理成功后，触发CMS通知计数 =====
                    if self._cms_enabled and self._cms_domain and self._cms_api_token:
                        self._wait_notify_count += 1
                        self._last_event_time = self.__get_time()
                        logger.info(f"短剧整理完成，已标记待CMS通知（当前队列：{self._wait_notify_count}）：{Path(event_path).name}")

                    # 生成 tvshow.nfo
                    logger.debug(f"文件 {event_path} 生成 tvshow.nfo开始")
                    logger.debug(f"store_conf: {store_conf}")
                    if store_conf == "local":
                        logger.debug(f"tvshow.nfo exists: {(target_path.parent / 'tvshow.nfo').exists()}")
                    else:
                        logger.debug(
                            f"tvshow.nfo exists: "
                            f"{self.filemanager.get_file_item(store_conf, (target_path.parent / 'tvshow.nfo'))}")

                    if store_conf == "local" and not (target_path.parent / "tvshow.nfo").exists():
                        self.__gen_tv_nfo_file(dir_path=target_path.parent,
                                               title=title)
                    # 内存生成nfo
                    if (store_conf != "local"
                            and None == self.filemanager.get_file_item(store_conf, (target_path.parent /
                                                                                    "tvshow.nfo"))):
                        if not ("/tmp/shortplaymonitormod" / target_path.parent.relative_to(
                                Path("/")) / "tvshow.nfo").exists():
                            os.makedirs(
                                Path("/tmp/shortplaymonitormod" / target_path.parent.relative_to(Path("/"))))
                            self.__gen_tv_nfo_file(
                                dir_path=("/tmp/shortplaymonitormod" / target_path.parent.relative_to(Path("/"))),
                                title=title)
                            file_item = FileItem()
                            file_item.storage = "local"
                            file_item.path = str("/tmp/shortplaymonitormod" / target_path.parent.relative_to(
                                Path("/")) / "tvshow.nfo")
                            # 源操作对象
                            source_oper = self.filemanager._FileManagerModule__get_storage_oper("local")
                            # 目的操作对象
                            target_oper = self.filemanager._FileManagerModule__get_storage_oper(store_conf)
                            if not source_oper or not target_oper:
                                return None, f"不支持的存储类型：{store_conf}"

                            new_item, errmsg = TransHandler._TransHandler__transfer_command(
                                fileitem=file_item,
                                target_storage=store_conf,
                                target_file=Path(target_path.parent / "tvshow.nfo"),
                                transfer_type=self._transfer_type,
                                source_oper=source_oper, target_oper=target_oper)
                            if new_item:
                                logger.debug(f"文件 {Path(target_path.parent / 'tvshow.nfo')} 整理完成")
                            else:
                                logger.debug((f"文件 {Path(target_path.parent / 'tvshow.nfo')} 整理失败:{errmsg}"))

                    logger.debug(f"文件 {event_path} 生成缩略图开始")
                    if store_conf == "local":
                        logger.debug(f"tvshow.nfo exists: {(target_path.parent / 'poster.jpg').exists()}")
                    else:
                        logger.debug(
                            f"tvshow.nfo exists: "
                            f"{self.filemanager.get_file_item(store_conf, (target_path.parent / 'poster.jpg'))}")

                    # 生成缩略图
                    if (store_conf == "local" and not (target_path.parent / "poster.jpg").exists()):
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
                            thumb_files = SystemUtils.list_files(directory=target_path.parent,
                                                                 extensions=[".jpg"])
                            if thumb_files:
                                for thumb in thumb_files:
                                    self.__save_poster(input_path=thumb,
                                                       poster_path=target_path.parent / "poster.jpg",
                                                       cover_conf=cover_conf)
                                    break
                                for thumb in thumb_files:
                                    Path(thumb).unlink()

                    if (store_conf != "local"
                            and None == self.filemanager.get_file_item(store_conf,
                                                                       (target_path.parent / "poster.jpg"))):
                        thumb_path = self.gen_file_thumb(title=title,
                                                         rename_conf=rename_conf,
                                                         file_path=Path(event_path),
                                                         to_thumb_path="/tmp/shortplaymonitormod" /
                                                                       target_path.parent.relative_to(
                                                                           Path("/")))
                        if thumb_path and Path(thumb_path).exists():
                            self.__save_poster(input_path=thumb_path,
                                               poster_path="/tmp/shortplaymonitormod" /
                                                           target_path.parent.relative_to(
                                                               Path("/")) / "poster.jpg",
                                               cover_conf=cover_conf)
                            if ("/tmp/shortplaymonitormod" / target_path.parent.relative_to(
                                    Path("/")) / "poster.jpg").exists():
                                file_item = FileItem()
                                file_item.storage = "local"
                                file_item.path = str(
                                    "/tmp/shortplaymonitormod" / target_path.parent.relative_to(
                                        Path("/")) / "poster.jpg")
                                source_oper = self.filemanager._FileManagerModule__get_storage_oper("local")
                                target_oper = self.filemanager._FileManagerModule__get_storage_oper(store_conf)
                                if not source_oper or not target_oper:
                                    return None, f"不支持的存储类型：{store_conf}"
                                new_item, errmsg = TransHandler._TransHandler__transfer_command(
                                    fileitem=file_item,
                                    target_storage=store_conf,
                                    target_file=Path(target_path.parent / "poster.jpg"),
                                    transfer_type=self._transfer_type, source_oper=source_oper,
                                    target_oper=target_oper)
                                if new_item:
                                    logger.debug(f"{target_path.parent / 'poster.jpg'} 缩略图已整理")
                                logger.info(f"{target_path.parent / 'poster.jpg'} 缩略图已生成")
                                thumb_path.unlink()
                else:
                    logger.error(f"文件 {event_path} 硬链接失败，错误码：{retcode}")

            if self._notify:
                # 发送消息汇总
                media_list = self._medias.get(title) or {}
                if media_list:
                    media_files = media_list.get("files") or []
                    if media_files:
                        if str(event_path) not in media_files:
                            media_files.append(str(event_path))
                    else:
                        media_files = [str(event_path)]
                    media_list = {
                        "files": media_files,
                        "time": datetime.datetime.now()
                    }
                else:
                    media_list = {
                        "files": [str(event_path)],
                        "time": datetime.datetime.now()
                    }
                self._medias[title] = media_list

        except Exception as e:
            logger.error(f"event_handler_created error: {e}", exc_info=True)
        if Path('/tmp/shortplaymonitormod/').exists():
            shutil.rmtree('/tmp/shortplaymonitormod/')
        logger.info(f"文件 {event_path} 处理完成")

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if self._notify:
            if not self._medias or not self._medias.keys():
                return

            for medis_title_year in list(self._medias.keys()):
                media_list = self._medias.get(medis_title_year)
                logger.info(f"开始处理媒体 {medis_title_year} 消息")

                if not media_list:
                    continue

                last_update_time = media_list.get("time")
                media_files = media_list.get("files")
                if not last_update_time or not media_files:
                    continue

                if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval):
                    self.post_message(mtype=NotificationType.Organize,
                                      title=f"{medis_title_year} 共{len(media_files)}集已入库",
                                      text="类别：短剧")
                    del self._medias[medis_title_year]
                    continue

    # ===== CMS通知相关方法 =====

    def __get_time(self):
        return int(time.time())

    def __notify_cms(self):
        """
        定时检查并通知CMS执行增量同步
        每分钟触发一次，满足条件后才真正发出通知：
        - 队列超过1000个文件，立即通知；
        - 或队列非空且距最后一次整理事件已超过60秒（静默期），才通知。
        """
        try:
            if self._wait_notify_count > 0 and (
                    self._wait_notify_count > 1000 or self.__get_time() - self._last_event_time > 60):
                url = (f"{self._cms_domain}/api/sync/lift_by_token"
                       f"?token={self._cms_api_token}&type={self._cms_notify_type}")
                ret = RequestUtils().get_res(url)
                if ret:
                    logger.info(f"通知CMS执行增量同步成功（共{self._wait_notify_count}个文件）")
                    self._wait_notify_count = 0
                elif ret is not None:
                    logger.error(
                        f"通知CMS失败，状态码：{ret.status_code}，返回信息：{ret.text} {ret.reason}")
                else:
                    logger.error("通知CMS失败，未获取到返回信息")
            else:
                if self._wait_notify_count > 0:
                    logger.info(
                        f"等待CMS通知，队列数量：{self._wait_notify_count}，"
                        f"距上次整理：{self.__get_time() - self._last_event_time}秒")
        except Exception as e:
            logger.error(f"通知CMS发生异常：{e}")

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
            domain = "agsvpt.com"
            site = SiteOper().get_by_domain(domain)
            index = SitesHelper().get_indexer(domain)
            if site:
                req_url = (f"https://www.agsvpt.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat"
                           f"=419&search={title}")
                image_xpath = "//*[@id='kdescr']/img[1]/@src"
                logger.info(f"开始检索 {site.name} {title}")
                image = self.__get_site_torrents(url=req_url, site=site, index=index, image_xpath=image_xpath)
            if not image:
                domain = "ilolicon.com"
                site = SiteOper().get_by_domain(domain)
                index = SitesHelper().get_indexer(domain)
                if site:
                    req_url = (f"https://share.ilolicon.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword"
                               f"=1&cat=402&search={title}")
                    image_xpath = "//*[@id='kdescr']/img[1]/@src"
                    logger.info(f"开始检索 {site.name} {title}")
                    image = self.__get_site_torrents(url=req_url, site=site, index=index, image_xpath=image_xpath)
            if not image:
                logger.error(f"检索站点 {title} 封面失败")
                return None
            if self.__save_image(url=image, file_path=file_path):
                return file_path
            return None
        except Exception as e:
            logger.error(f"检索站点 {title} 封面失败 {str(e)}", exc_info=True)
            return None

    def gen_desc_from_site(self, title: str):
        try:
            desc = None
            domain = "agsvpt.com"
            site = SiteOper().get_by_domain(domain)
            index = SitesHelper().get_indexer(domain)
            if site:
                req_url = (f"https://www.agsvpt.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat"
                           f"=419&search={title}")
                desc_xpath = "//*[@id='kdescr']/text()"
                logger.info(f"开始检索 {site.name} {title}")
                desc = self.__get_site_torrents(url=req_url, site=site, index=index, desc_xpath=desc_xpath)
            if not desc:
                domain = "ilolicon.com"
                site = SiteOper().get_by_domain(domain)
                index = SitesHelper().get_indexer(domain)
                if site:
                    req_url = (f"https://share.ilolicon.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword"
                               f"=1&cat=402&search={title}")
                    desc_xpath = "//*[@id='kdescr']/text()"
                    logger.info(f"开始检索 {site.name} {title}")
                    desc = self.__get_site_torrents(url=req_url, site=site, index=index, desc_xpath=desc_xpath)
            if not desc:
                logger.error(f"检索站点 {title} 简介失败")
                return None
            else:
                return desc
        except Exception as e:
            logger.error(f"检索站点 {title} 简介失败 {str(e)}", exc_info=True)
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
                                'props': {'cols': 12, 'md': 3},
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
                                'props': {'cols': 12, 'md': 3},
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
                                'props': {'cols': 12, 'md': 3},
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
                                'props': {'cols': 12, 'md': 3},
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
            "image": False,
            "notify": False,
            "interval": 10,
            "monitor_confs": "",
            "exclude_keywords": "",
            "transfer_type": "link",
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
