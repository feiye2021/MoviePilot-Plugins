import threading
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyTaskTrigger(_PluginBase):
    # 插件名称
    plugin_name = "Emby任务触发器"
    # 插件描述
    plugin_desc = "检测MP新入库事件，自动触发Emby的Extract Intro Fingerprint、Extract MediaInfo和Extract Video Thumbnail计划任务。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/feiye2021/MoviePilot-Plugins/main/icons/emby.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "feiye"
    # 作者主页
    author_url = "https://github.com/feiye2021/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "embytasktrigger_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = threading.Event()

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _delay = 0
    _mediaservers = None
    _task_intro = True
    _task_mediainfo = True
    _task_thumbnail = True
    _scheduler: Optional[BackgroundScheduler] = None
    mediaserver_helper = None

    # Emby连接信息（运行时填充）
    _EMBY_HOST = None
    _EMBY_APIKEY = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._delay = int(config.get("delay") or 0)
            self._mediaservers = config.get("mediaservers") or []
            self._task_intro = config.get("task_intro", True)
            self._task_mediainfo = config.get("task_mediainfo", True)
            self._task_thumbnail = config.get("task_thumbnail", True)

            if self._enabled or self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                if self._onlyonce:
                    logger.info("Emby任务触发器：立即运行一次")
                    self._scheduler.add_job(
                        self.trigger_tasks,
                        'date',
                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                        name="Emby任务触发器"
                    )
                    self._onlyonce = False
                    self.__update_config()

                if self._cron:
                    try:
                        self._scheduler.add_job(
                            func=self.trigger_tasks,
                            trigger=CronTrigger.from_crontab(self._cron),
                            name="Emby任务触发器"
                        )
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "delay": self._delay,
            "mediaservers": self._mediaservers,
            "task_intro": self._task_intro,
            "task_mediainfo": self._task_mediainfo,
            "task_thumbnail": self._task_thumbnail,
        })

    def __setup_emby_connection(self, emby_server) -> bool:
        """
        初始化Emby连接参数
        """
        self._EMBY_APIKEY = emby_server.config.config.get("apikey")
        self._EMBY_HOST = emby_server.config.config.get("host")
        if not self._EMBY_HOST:
            return False
        if not self._EMBY_HOST.endswith("/"):
            self._EMBY_HOST += "/"
        if not self._EMBY_HOST.startswith("http"):
            self._EMBY_HOST = "http://" + self._EMBY_HOST
        return True

    def __get_scheduled_tasks(self) -> list:
        """
        获取Emby所有计划任务列表
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"{self._EMBY_HOST}emby/ScheduledTasks?api_key={self._EMBY_APIKEY}"
        try:
            with RequestUtils().get_res(req_url) as res:
                if res and res.status_code == 200:
                    return res.json()
                else:
                    logger.error(f"获取Emby计划任务列表失败，状态码：{res.status_code if res else 'N/A'}")
        except Exception as e:
            logger.error(f"获取Emby计划任务列表出错：{str(e)}")
        return []

    def __run_scheduled_task(self, task_id: str, task_label: str) -> bool:
        """
        触发指定ID的Emby计划任务（POST /ScheduledTasks/Running/{taskId}）
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        req_url = f"{self._EMBY_HOST}emby/ScheduledTasks/Running/{task_id}?api_key={self._EMBY_APIKEY}"
        try:
            with RequestUtils().post_res(req_url) as res:
                if res and res.status_code in [200, 204]:
                    logger.info(f"✅ 成功触发Emby计划任务：{task_label} (id={task_id})")
                    return True
                else:
                    status = res.status_code if res else "N/A"
                    logger.warning(f"触发Emby计划任务 [{task_label}] 失败，状态码：{status}")
        except Exception as e:
            logger.error(f"触发Emby计划任务 [{task_label}] 出错：{str(e)}")
        return False

    def trigger_tasks(self):
        """
        核心方法：连接Emby，动态查找并触发三项计划任务
        """
        emby_servers = self.mediaserver_helper.get_services(
            name_filters=self._mediaservers, type_filter="emby"
        )
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            logger.info(f"开始对 [{emby_name}] 触发Emby计划任务")

            if not self.__setup_emby_connection(emby_server):
                logger.error(f"Emby服务器 [{emby_name}] 连接参数缺失，跳过")
                continue

            # 获取所有计划任务
            all_tasks = self.__get_scheduled_tasks()
            if not all_tasks:
                logger.error(f"无法获取 [{emby_name}] 的计划任务列表")
                continue

            # 构建 任务名(小写) -> 任务ID 的映射
            task_map = {task.get("Name", "").lower(): task.get("Id", "") for task in all_tasks}
            logger.debug(f"[{emby_name}] 获取到 {len(task_map)} 个计划任务：{list(task_map.keys())}")

            # 定义需要触发的任务及其关键词（按优先级排列）
            wanted_tasks = []
            if self._task_intro:
                wanted_tasks.append({
                    "label": "Extract Intro Fingerprint",
                    # 不同Emby版本/语言下的可能任务名关键词
                    "keywords": [
                        "detect introduction segments",
                        "intro fingerprint",
                        "introduction segments",
                        "detect introduction",
                        "extractintro",
                    ],
                })
            if self._task_mediainfo:
                wanted_tasks.append({
                    "label": "Extract MediaInfo",
                    "keywords": [
                        "extract media info",
                        "probe media info",
                        "media info extraction",
                        "extractmediainfo",
                        "mediainfo",
                    ],
                })
            if self._task_thumbnail:
                wanted_tasks.append({
                    "label": "Extract Video Thumbnail",
                    "keywords": [
                        "extract video images",
                        "extract video image",
                        "video thumbnail",
                        "video images extraction",
                        "extractvideoimages",
                        "thumbnail",
                    ],
                })

            triggered_count = 0
            for wanted in wanted_tasks:
                label = wanted["label"]
                keywords = wanted["keywords"]

                # 关键词模糊匹配
                matched_id = None
                for kw in keywords:
                    for task_name_lower, task_id in task_map.items():
                        if kw in task_name_lower and task_id:
                            matched_id = task_id
                            logger.debug(f"[{emby_name}] [{label}] 匹配到任务：{task_name_lower} (id={task_id})")
                            break
                    if matched_id:
                        break

                if matched_id:
                    success = self.__run_scheduled_task(matched_id, label)
                    if success:
                        triggered_count += 1
                else:
                    logger.warning(
                        f"[{emby_name}] 未找到计划任务：{label}\n"
                        f"  已尝试关键词：{keywords}\n"
                        f"  可用任务列表：{list(task_map.keys())}"
                    )

            logger.info(
                f"[{emby_name}] 计划任务触发完成，成功 {triggered_count}/{len(wanted_tasks)} 个"
            )

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """
        监听MP入库完成事件（TransferComplete），延迟后触发Emby计划任务
        """
        if not self._enabled:
            return
        if not event or not event.event_data:
            return

        event_data = event.event_data
        media_info = event_data.get("mediainfo")
        transfer_info = event_data.get("transferinfo")

        if not media_info or not transfer_info:
            return

        # 取得媒体标题用于日志
        media_title = getattr(media_info, "title", None) or str(media_info)
        logger.info(
            f"检测到新入库媒体：{media_title}，将在 {self._delay} 秒后触发Emby计划任务"
        )

        if self._delay > 0:
            import time
            time.sleep(self._delay)

        self.trigger_tasks()

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程命令手动触发
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "emby_task_trigger":
                return
            self.post_message(
                channel=event.event_data.get("channel"),
                title="开始触发Emby计划任务 ...",
                userid=event.event_data.get("user")
            )
        self.trigger_tasks()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="Emby计划任务触发完成！",
                userid=event.event_data.get("user")
            )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/emby_task_trigger",
            "event": EventType.PluginAction,
            "desc": "触发Emby计划任务",
            "category": "",
            "data": {
                "action": "emby_task_trigger"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置页面
        """
        return [
            {
                "component": "VForm",
                "content": [
                    # 第一行：基础开关
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"}
                                }]
                            },
                        ]
                    },
                    # 第二行：任务开关
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "task_intro",
                                        "label": "触发 Extract Intro Fingerprint"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "task_mediainfo",
                                        "label": "触发 Extract MediaInfo"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "task_thumbnail",
                                        "label": "触发 Extract Video Thumbnail"
                                    }
                                }]
                            },
                        ]
                    },
                    # 第三行：调度参数
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "定时执行周期（可选）",
                                        "placeholder": "5位cron表达式，留空则仅监听入库事件"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "delay",
                                        "label": "入库后延迟触发(秒)",
                                        "placeholder": "新入库后等待N秒再触发，默认30"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "model": "mediaservers",
                                        "label": "媒体服务器",
                                        "items": [
                                            {"title": config.name, "value": config.name}
                                            for config in self.mediaserver_helper.get_configs().values()
                                            if config.type == "emby"
                                        ]
                                    }
                                }]
                            },
                        ]
                    },
                    # 说明提示
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": (
                                        "监听 MP 入库完成事件（TransferComplete），有新媒体入库时自动触发所勾选的 Emby 计划任务。"
                                        "也可额外配置 Cron 表达式定时触发。仅支持 Emby 媒体服务器。"
                                    )
                                }
                            }]
                        }]
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": (
                                        "提示：不同 Emby 版本/语言环境下计划任务名称可能有差异，插件通过关键词模糊匹配任务名。"
                                        "若触发失败，请查看日志中输出的【可用任务列表】，并提 Issue 反馈任务名称以便更新关键词。"
                                        "建议设置适当延迟（如 30~60 秒），等待 Emby 完成媒体扫描后再触发提取任务。"
                                    )
                                }
                            }]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "delay": 30,
            "mediaservers": [],
            "task_intro": True,
            "task_mediainfo": True,
            "task_thumbnail": True,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")