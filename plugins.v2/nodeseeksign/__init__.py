import time
import random
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
import requests
import json

# cloudscraper 作为 Cloudflare 备用方案
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except Exception:
    HAS_CLOUDSCRAPER = False

# curl_cffi：Chrome TLS 指纹仿真，CF绕过首选
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# brotli：解压 Content-Encoding: br 响应
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False


class nodeseeksign(_PluginBase):
    plugin_name = "NodeSeek论坛签到"
    plugin_desc = "NodeSeek论坛每日签到，支持随机奖励和自动重试功能"
    plugin_icon = "https://raw.githubusercontent.com/feiye2021/MoviePilot-Plugins/main/icons/nodeseeksign.png"
    plugin_version = "1.0.1"
    plugin_author = "feiye"
    author_url = "https://github.com/feiye2021/MoviePilot-Plugins"
    plugin_config_prefix = "nodeseeksign_"
    plugin_order = 1
    auth_level = 2
    # 插件依赖：MoviePilot 框架读取此字段，在插件加载时自动 pip install
    # 与根目录 requirements.txt 等价，二者选其一即可，此处已完全替代外部文件
    plugin_requires = "curl_cffi>=0.13.0,<0.15.0\ncloudscraper>=1.2.71,<2.0.0\nbrotli>=1.0.9"

    # 私有属性
    _enabled = False
    _cookie = None
    _notify = False
    _onlyonce = False
    _clear_history = False
    _cron = None
    _random_choice = True
    _history_days = 30
    _max_retries = 3
    _retry_count = 0
    _scheduled_retry = None
    _min_delay = 5
    _max_delay = 12
    _member_id = ""
    _stats_days = 30
    _scraper = None

    _scheduler: Optional[BackgroundScheduler] = None
    _manual_trigger = False

    def init_plugin(self, config: dict = None):
        self.stop_service()
        logger.info("============= nodeseeksign 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._cookie = config.get("cookie")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                self._random_choice = config.get("random_choice")
                self._clear_history = config.get("clear_history", False)

                for attr, key, default in [
                    ("_history_days", "history_days", 30),
                    ("_max_retries",  "max_retries",  3),
                    ("_min_delay",    "min_delay",    5),
                    ("_max_delay",    "max_delay",    12),
                    ("_stats_days",   "stats_days",   30),
                ]:
                    try:
                        setattr(self, attr, int(config.get(key, default)))
                    except (ValueError, TypeError):
                        setattr(self, attr, default)
                        logger.warning(f"{key} 配置无效，使用默认值 {default}")

                self._member_id = (config.get("member_id") or "").strip()

                logger.info(
                    f"配置: enabled={self._enabled}, notify={self._notify}, cron={self._cron}, "
                    f"random_choice={self._random_choice}, history_days={self._history_days}, "
                    f"max_retries={self._max_retries}, "
                    f"min_delay={self._min_delay}, max_delay={self._max_delay}, "
                    f"member_id={self._member_id or '未设置'}, clear_history={self._clear_history}"
                )

                # 初始化 cloudscraper（CF JS challenge 求解备用）
                if HAS_CLOUDSCRAPER:
                    try:
                        self._scraper = cloudscraper.create_scraper(browser="chrome")
                        logger.info("cloudscraper 初始化成功")
                    except Exception:
                        try:
                            self._scraper = cloudscraper.create_scraper()
                        except Exception as e2:
                            logger.warning(f"cloudscraper 初始化失败: {str(e2)}")
                            self._scraper = None

            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(
                    func=self.sign,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="NodeSeek论坛签到"
                )
                self._onlyonce = False
                self.__save_config(clear_history=self._clear_history)

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

                if self._clear_history:
                    logger.info("检测到清除历史记录标志，开始清空数据...")
                    self.clear_sign_history()
                    self.__save_config(clear_history=False)
                    logger.info("已清除签到历史记录，clear_history 已重置为 False")

        except Exception as e:
            logger.error(f"nodeseeksign初始化错误: {str(e)}", exc_info=True)

    def __save_config(self, clear_history: bool = False):
        """统一保存配置"""
        self.update_config({
            "onlyonce":      False,
            "enabled":       self._enabled,
            "cookie":        self._cookie,
            "notify":        self._notify,
            "cron":          self._cron,
            "random_choice": self._random_choice,
            "history_days":  self._history_days,
            "max_retries":   self._max_retries,
            "min_delay":     self._min_delay,
            "max_delay":     self._max_delay,
            "member_id":     self._member_id,
            "clear_history": clear_history,
            "stats_days":    self._stats_days,
        })

    # ------------------------------------------------------------------ #
    #  HTTP 请求适配层                                                      #
    # ------------------------------------------------------------------ #

    def _build_headers(self, extra: dict = None) -> dict:
        """构建标准浏览器请求头"""
        headers = {
            'Accept':             '*/*',
            'Accept-Encoding':    'gzip, deflate, br, zstd',
            'Accept-Language':    'zh-CN,zh;q=0.9,en;q=0.8',
            'Content-Type':       'application/json',
            'Origin':             'https://www.nodeseek.com',
            'Referer':            'https://www.nodeseek.com/board',
            'Sec-CH-UA':          '"Chromium";v="136", "Not:A-Brand";v="24", "Google Chrome";v="136"',
            'Sec-CH-UA-Mobile':   '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-Fetch-Dest':     'empty',
            'Sec-Fetch-Mode':     'cors',
            'Sec-Fetch-Site':     'same-origin',
            'User-Agent':         (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/136.0.0.0 Safari/537.36'
            ),
        }
        if extra:
            headers.update(extra)
        return headers

    def _is_cf_blocked(self, resp) -> bool:
        """
        判断响应是否被 Cloudflare 拦截。
        CF拦截特征：状态码 403/503 且 Content-Type 为 text/html。
        """
        status = getattr(resp, 'status_code', 0)
        ct = (resp.headers.get('Content-Type') or resp.headers.get('content-type') or '').lower()
        if status in (403, 503) and 'text/html' in ct:
            logger.warning(f"疑似Cloudflare拦截: status={status}, content-type={ct}")
            return True
        return False

    def _smart_post(self, url: str, headers: dict = None, data=None, timeout: int = 30):
        """
        POST 请求，优先级：curl_cffi > cloudscraper > requests
        全程启用 SSL 验证（verify=True）。
        """
        last_error = None

        # 1) curl_cffi —— Chrome TLS 指纹，最强 CF 绕过
        if HAS_CURL_CFFI:
            try:
                logger.info("curl_cffi POST (Chrome-120)")
                session = curl_requests.Session(impersonate="chrome120")
                resp = session.post(url, headers=headers, data=data, timeout=timeout, verify=True)
                if not self._is_cf_blocked(resp):
                    return resp
                logger.info("curl_cffi POST 被CF拦截，降级")
            except Exception as e:
                last_error = e
                logger.warning(f"curl_cffi POST 失败: {e}")

        # 2) cloudscraper —— JS challenge 求解
        if HAS_CLOUDSCRAPER and self._scraper:
            try:
                logger.info("cloudscraper POST")
                resp = self._scraper.post(url, headers=headers, data=data, timeout=timeout, verify=True)
                if not self._is_cf_blocked(resp):
                    return resp
                logger.info("cloudscraper POST 被CF拦截，降级")
            except Exception as e:
                last_error = e
                logger.warning(f"cloudscraper POST 失败: {e}")

        # 3) requests —— 兜底
        try:
            logger.info("requests POST")
            return requests.post(url, headers=headers, data=data, timeout=timeout, verify=True)
        except Exception as e:
            if last_error:
                logger.error(f"此前错误: {last_error}")
            raise e

    def _smart_get(self, url: str, headers: dict = None, timeout: int = 30):
        """
        GET 请求，优先级同 _smart_post。
        全程启用 SSL 验证（verify=True）。
        """
        last_error = None

        # 1) curl_cffi
        if HAS_CURL_CFFI:
            try:
                logger.info(f"curl_cffi GET (Chrome-120): {url}")
                session = curl_requests.Session(impersonate="chrome120")
                resp = session.get(url, headers=headers, timeout=timeout, verify=True)
                if not self._is_cf_blocked(resp):
                    return resp
                logger.info("curl_cffi GET 被CF拦截，降级")
            except Exception as e:
                last_error = e
                logger.warning(f"curl_cffi GET 失败: {e}")

        # 2) cloudscraper
        if HAS_CLOUDSCRAPER and self._scraper:
            try:
                logger.info(f"cloudscraper GET: {url}")
                resp = self._scraper.get(url, headers=headers, timeout=timeout, verify=True)
                if not self._is_cf_blocked(resp):
                    return resp
                logger.info("cloudscraper GET 被CF拦截，降级")
            except Exception as e:
                last_error = e
                logger.warning(f"cloudscraper GET 失败: {e}")

        # 3) requests
        try:
            logger.info(f"requests GET: {url}")
            return requests.get(url, headers=headers, timeout=timeout, verify=True)
        except Exception as e:
            if last_error:
                logger.error(f"此前错误: {last_error}")
            raise e

    def _decode_response_text(self, resp) -> str:
        """
        统一解码响应体，自动处理 Brotli(br) 压缩。
        curl_cffi / cloudscraper 通常自动解压；requests 在某些环境下不处理 br，
        此方法作为兜底保障，确保始终返回可用的文本内容。
        """
        encoding = (resp.headers.get('content-encoding') or '').lower()
        if encoding == 'br':
            if HAS_BROTLI:
                try:
                    return brotli.decompress(resp.content).decode('utf-8', errors='replace')
                except Exception as e:
                    logger.warning(f"brotli 解压失败，回退到 resp.text: {e}")
            else:
                logger.warning("响应为 br 编码但 brotli 库未安装，可能解析失败")
        return resp.text

    # ------------------------------------------------------------------ #
    #  核心签到流程                                                         #
    # ------------------------------------------------------------------ #

    def sign(self):
        """执行 NodeSeek 签到"""
        logger.info("============= 开始NodeSeek签到 =============")
        sign_dict = None
        try:
            if not self._cookie:
                logger.error("未配置Cookie")
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 未配置Cookie",
                }
                self._save_sign_history(sign_dict)
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【NodeSeek论坛签到失败】",
                        text="未配置Cookie，请在设置中添加Cookie"
                    )
                return sign_dict

            self._wait_random_interval()
            result = self._run_api_sign()

            user_info = None
            try:
                if self._member_id:
                    user_info = self._fetch_user_info(self._member_id)
            except Exception as e:
                logger.warning(f"获取用户信息失败: {e}")

            attendance_record = None
            try:
                attendance_record = self._fetch_attendance_record()
            except Exception as e:
                logger.warning(f"获取签到记录失败: {e}")

            if result["success"]:
                sign_dict = {
                    "date":    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status":  "签到成功" if not result.get("already_signed") else "已签到",
                    "message": result.get("message", "")
                }
                ar = attendance_record or {}
                if ar.get("gain"):
                    sign_dict["gain"] = ar["gain"]
                    if ar.get("rank"):
                        sign_dict["rank"] = ar["rank"]
                        sign_dict["total_signers"] = ar.get("total_signers")
                elif result.get("gain"):
                    sign_dict["gain"] = result["gain"]

                self._save_sign_history(sign_dict)
                self._save_last_sign_date()
                self._retry_count = 0

                if self._notify:
                    try:
                        self._send_sign_notification(sign_dict, result, user_info, attendance_record)
                    except Exception as e:
                        logger.error(f"签到成功通知发送失败: {e}")

                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {e}")

            else:
                sign_dict = {
                    "date":    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status":  "签到失败",
                    "message": result.get("message", "")
                }

                # 兜底：通过签到记录日期确认是否已签到
                try:
                    ar = attendance_record or {}
                    if ar.get("created_at"):
                        sh_tz = pytz.timezone('Asia/Shanghai')
                        rec_dt = datetime.fromisoformat(
                            ar["created_at"].replace('Z', '+00:00')
                        ).astimezone(sh_tz)
                        if rec_dt.date() == datetime.now(sh_tz).date():
                            logger.info("从签到记录确认今日已签到")
                            result["success"] = True
                            result["already_signed"] = True
                            sign_dict["status"] = "已签到（记录确认）"
                except Exception as e:
                    logger.warning(f"兜底验证失败: {e}")

                self._save_sign_history(sign_dict)

                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {e}")

                # 安排重试
                max_retries = int(self._max_retries) if self._max_retries else 0
                if max_retries and self._retry_count < max_retries:
                    self._retry_count += 1
                    retry_minutes = random.randint(5, 15)
                    retry_time = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=retry_minutes)
                    logger.info(f"签到失败，{retry_minutes} 分钟后重试 ({self._retry_count}/{max_retries})")

                    if not self._scheduler:
                        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                        if not self._scheduler.running:
                            self._scheduler.start()
                    if self._scheduled_retry:
                        try:
                            self._scheduler.remove_job(self._scheduled_retry)
                        except Exception:
                            pass
                    self._scheduled_retry = f"nodeseek_retry_{int(time.time())}"
                    self._scheduler.add_job(
                        func=self.sign,
                        trigger='date',
                        run_date=retry_time,
                        id=self._scheduled_retry,
                        name=f"NodeSeek签到重试 {self._retry_count}/{max_retries}"
                    )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【NodeSeek论坛签到失败】",
                            text=(
                                f"签到失败: {result.get('message', '未知错误')}\n"
                                f"将在 {retry_minutes} 分钟后第 {self._retry_count}/{max_retries} 次重试\n"
                                f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                        )
                else:
                    tip = "未配置自动重试" if not max_retries else f"已达最大重试次数 ({max_retries})"
                    logger.info(tip)
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【NodeSeek论坛签到失败】",
                            text=(
                                f"签到失败: {result.get('message', '未知错误')}\n"
                                f"{tip}\n"
                                f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                        )

            return sign_dict

        except Exception as e:
            logger.error(f"NodeSeek签到过程中出错: {e}", exc_info=True)
            sign_dict = {
                "date":   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "status": f"签到出错: {e}",
            }
            self._save_sign_history(sign_dict)
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【NodeSeek论坛签到出错】",
                    text=f"签到过程中出错: {e}\n⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            return sign_dict

    def _run_api_sign(self) -> dict:
        """调用 NodeSeek 签到 API"""
        try:
            result = {"success": False, "signed": False, "already_signed": False, "message": ""}
            random_param = "true" if self._random_choice else "false"
            url = f"https://www.nodeseek.com/api/attendance?random={random_param}"
            headers = self._build_headers({
                'Content-Length': '0',
                'Cookie':         self._cookie,
            })

            response = self._smart_post(url=url, headers=headers, data=b'', timeout=30)
            logger.info(f"签到响应状态码: {response.status_code}")

            try:
                data = response.json()
                msg = data.get('message', '')
                logger.info(f"签到响应: {data}")

                if data.get('success') is True:
                    result.update({"success": True, "signed": True, "message": msg})
                    if data.get('gain'):
                        result.update({"gain": data['gain'], "current": data.get('current', 0)})
                elif "鸡腿" in msg:
                    result.update({"success": True, "signed": True, "message": msg})
                elif "已完成签到" in msg:
                    result.update({"success": True, "already_signed": True, "message": msg})
                elif msg in ("USER NOT FOUND",) or data.get('status') == 404:
                    result.update({"message": "Cookie已失效，请更新"})
                elif "签到" in msg and ("成功" in msg or "完成" in msg):
                    result.update({"success": True, "signed": True, "message": msg})
                else:
                    result.update({"message": msg or f"未知响应: {response.status_code}"})

            except Exception:
                text = response.text or ""
                logger.warning(f"非JSON签到响应: {text[:400]}")
                if any(k in text for k in ["鸡腿", "签到成功", "签到完成"]):
                    result.update({"success": True, "signed": True, "message": text[:80]})
                elif "已完成签到" in text:
                    result.update({"success": True, "already_signed": True, "message": text[:80]})
                elif any(k in text for k in ["登录", "注册", "你好啊，陌生人"]):
                    result.update({"message": "未登录或Cookie失效"})
                elif "cloudflare" in text.lower() or "challenge" in text.lower():
                    result.update({"message": "遭遇Cloudflare验证，请稍后重试"})
                else:
                    result.update({"message": f"非JSON响应({response.status_code})"})

            return result
        except Exception as e:
            logger.error(f"API签到出错: {e}", exc_info=True)
            return {"success": False, "message": f"API签到出错: {e}"}

    # ------------------------------------------------------------------ #
    #  辅助方法                                                             #
    # ------------------------------------------------------------------ #

    def _wait_random_interval(self):
        """请求前随机延迟，模拟人类行为"""
        try:
            mn = float(self._min_delay or 5)
            mx = float(self._max_delay or 12)
            if mx >= mn > 0:
                delay = random.uniform(mn, mx)
                logger.info(f"随机等待 {delay:.2f} 秒...")
                time.sleep(delay)
        except Exception:
            pass

    def _fetch_user_info(self, member_id: str) -> dict:
        """拉取用户信息（可选）"""
        if not member_id:
            return {}
        url = f"https://www.nodeseek.com/api/account/getInfo/{member_id}?readme=1"
        headers = self._build_headers({
            'Referer': f"https://www.nodeseek.com/space/{member_id}",
        })
        try:
            resp = self._smart_get(url=url, headers=headers, timeout=30)
            data = resp.json()
            detail = data.get("detail") or {}
            if detail:
                self.save_data('last_user_info', detail)
            return detail
        except Exception as e:
            logger.warning(f"获取用户信息失败: {e}")
            return {}

    def _fetch_attendance_record(self) -> dict:
        """拉取签到记录，获取奖励和排名"""
        try:
            url = "https://www.nodeseek.com/api/attendance/board?page=1"
            headers = self._build_headers({'Cookie': self._cookie})
            resp = self._smart_get(url=url, headers=headers, timeout=30)
            logger.info(f"签到记录响应状态码: {resp.status_code}")

            try:
                data = resp.json()
            except Exception:
                logger.warning(f"签到记录非JSON响应: {(resp.text or '')[:400]}")
                return self._get_cached_attendance_if_today()

            record = data.get("record", {})
            if record:
                record['rank'] = data.get("order")
                record['total_signers'] = data.get("total")
                self.save_data('last_attendance_record', record)
                logger.info(
                    f"签到记录: 获得{record.get('gain', 0)}个鸡腿"
                    + (f"，排名第{record['rank']}名" if record.get('rank') else "")
                    + (f"，共{record['total_signers']}人" if record.get('total_signers') else "")
                )
            return record

        except Exception as e:
            logger.warning(f"获取签到记录失败: {e}")
            return {}

    def _get_cached_attendance_if_today(self) -> dict:
        """返回今日有效的缓存签到记录"""
        try:
            cached = self.get_data('last_attendance_record') or {}
            if cached and cached.get('created_at'):
                sh_tz = pytz.timezone('Asia/Shanghai')
                rec_dt = datetime.fromisoformat(
                    cached['created_at'].replace('Z', '+00:00')
                ).astimezone(sh_tz)
                if rec_dt.date() == datetime.now(sh_tz).date():
                    return cached
        except Exception:
            pass
        return {}

    def _save_sign_history(self, sign_data: dict):
        """保存签到历史记录"""
        try:
            history = self.get_data('sign_history') or []
            if "date" not in sign_data:
                sign_data["date"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            history.append(sign_data)

            retention = int(self._history_days or 30)
            now = datetime.now()
            valid = []
            for rec in history:
                try:
                    rec_dt = datetime.strptime(rec["date"], '%Y-%m-%d %H:%M:%S')
                    if (now - rec_dt).days < retention:
                        valid.append(rec)
                except (ValueError, KeyError):
                    rec["date"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    valid.append(rec)

            self.save_data(key="sign_history", value=valid)
            logger.info(f"签到历史已保存，共 {len(valid)} 条")
        except Exception as e:
            logger.error(f"保存签到历史记录失败: {e}", exc_info=True)

    def clear_sign_history(self):
        """清除所有签到历史数据"""
        try:
            for key in ("sign_history", "last_sign_date", "last_user_info", "last_attendance_record"):
                self.save_data(key=key, value="" if key != "sign_history" else [])
            logger.info("已清空所有签到相关数据")
        except Exception as e:
            logger.error(f"清除签到历史记录失败: {e}", exc_info=True)

    def _save_last_sign_date(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.save_data('last_sign_date', now)
        logger.info(f"记录签到成功时间: {now}")

    def _is_already_signed_today(self) -> bool:
        today = datetime.now().strftime('%Y-%m-%d')
        history = self.get_data('sign_history') or []
        success_statuses = {"签到成功", "已签到"}
        if any(r.get("date", "").startswith(today) and r.get("status") in success_statuses for r in history):
            return True
        last = self.get_data('last_sign_date')
        if last:
            try:
                return datetime.strptime(last, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d') == today
            except Exception:
                pass
        return False

    def _send_sign_notification(
        self,
        sign_dict: dict,
        result: dict,
        user_info: dict = None,
        attendance_record: dict = None,
    ):
        """发送签到通知"""
        if not self._notify:
            return

        status    = sign_dict.get("status", "未知")
        sign_time = sign_dict.get("date", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ar = attendance_record or {}

        def gain_rank_lines() -> str:
            lines = []
            gain = result.get("gain") or ar.get("gain")
            if gain:
                lines.append(f"🎁 今日获得: {gain}个鸡腿")
            if ar.get("rank"):
                s = f"🏆 排名: 第{ar['rank']}名"
                if ar.get("total_signers"):
                    s += f" (共{ar['total_signers']}人)"
                lines.append(s)
            elif ar.get("total_signers"):
                lines.append(f"📊 今日共{ar['total_signers']}人签到")
            return "\n".join(lines)

        def user_line() -> str:
            if not user_info:
                return ""
            return (
                f"👤 用户：{user_info.get('member_name','未知')}  "
                f"等级：{user_info.get('rank','未知')}  "
                f"鸡腿：{user_info.get('coin','未知')}"
            )

        sep = "━━━━━━━━━━"
        if "签到成功" in status:
            title = "【✅ NodeSeek论坛签到成功】"
            parts = ["📢 执行结果", sep, f"🕐 时间：{sign_time}", f"✨ 状态：{status}",
                     user_line(), gain_rank_lines(), sep]
        elif "已签到" in status:
            title = "【ℹ️ NodeSeek论坛今日已签到】"
            parts = ["📢 执行结果", sep, f"🕐 时间：{sign_time}", f"✨ 状态：{status}",
                     user_line(), gain_rank_lines(),
                     "ℹ️ 今日已完成签到，显示当前奖励信息", sep]
        else:
            title = "【❌ NodeSeek论坛签到失败】"
            parts = ["📢 执行结果", sep, f"🕐 时间：{sign_time}", f"❌ 状态：{status}",
                     sep, "💡 可能的解决方法",
                     "• 检查Cookie是否过期", "• 确认站点是否可访问", "• 尝试手动登录网站", sep]

        text = "\n".join(p for p in parts if p)
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
            logger.info("通知发送成功")
        except Exception as e:
            logger.error(f"通知发送失败: {e}")

    # ------------------------------------------------------------------ #
    #  收益统计                                                             #
    # ------------------------------------------------------------------ #

    def _get_signin_stats(self, days: int = 30) -> dict:
        if not self._cookie:
            return {}
        days = max(days, 1)
        headers = self._build_headers({'Cookie': self._cookie})
        tz = pytz.timezone('Asia/Shanghai')
        now_sh = datetime.now(tz)
        start_time = now_sh - timedelta(days=days)
        period_desc = f'近{days}天' if days != 1 else '今天'
        all_records = []
        page = 1
        try:
            while page <= 20:
                url = f'https://www.nodeseek.com/api/account/credit/page-{page}'
                resp = self._smart_get(url=url, headers=headers, timeout=30)
                try:
                    data = resp.json()
                except Exception:
                    break
                if not data.get('success') or not data.get('data'):
                    break
                records = data['data']
                if not records:
                    break
                try:
                    last_dt = datetime.fromisoformat(records[-1][3].replace('Z', '+00:00')).astimezone(tz)
                except Exception:
                    break
                if last_dt < start_time:
                    for rec in records:
                        try:
                            if datetime.fromisoformat(rec[3].replace('Z', '+00:00')).astimezone(tz) >= start_time:
                                all_records.append(rec)
                        except Exception:
                            pass
                    break
                all_records.extend(records)
                page += 1
        except Exception:
            pass

        signin_records = []
        for rec in all_records:
            try:
                amount, _, description, timestamp = rec
                rec_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(tz)
                if rec_dt >= start_time and '签到收益' in description and '鸡腿' in description:
                    signin_records.append({
                        'amount': amount,
                        'date': rec_dt.strftime('%Y-%m-%d'),
                        'description': description,
                    })
            except Exception:
                pass

        if not signin_records:
            # 降级：使用本地历史
            try:
                history = self.get_data('sign_history') or []
                success_statuses = {"签到成功", "已签到", "签到成功（时间验证）", "已签到（从记录确认）", "已签到（记录确认）"}
                fb = []
                for rec in history:
                    try:
                        rec_dt = datetime.strptime(rec.get('date', ''), '%Y-%m-%d %H:%M:%S')
                        rec_dt = rec_dt.replace(tzinfo=tz)
                    except Exception:
                        continue
                    if rec_dt >= start_time and rec.get('status') in success_statuses and rec.get('gain'):
                        fb.append({'amount': rec['gain'], 'date': rec_dt.strftime('%Y-%m-%d'), 'description': '本地历史'})
                total = sum(r['amount'] for r in fb)
                cnt = len(fb)
                return {'total_amount': total, 'average': round(total/cnt,2) if cnt else 0,
                        'days_count': cnt, 'records': fb, 'period': period_desc}
            except Exception:
                return {'total_amount': 0, 'average': 0, 'days_count': 0, 'records': [], 'period': period_desc}

        total = sum(r['amount'] for r in signin_records)
        cnt = len(signin_records)
        return {
            'total_amount': total,
            'average': round(total/cnt, 2) if cnt else 0,
            'days_count': cnt,
            'records': signin_records,
            'period': period_desc,
        }

    # ------------------------------------------------------------------ #
    #  插件接口                                                             #
    # ------------------------------------------------------------------ #

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id":      "nodeseeksign",
                "name":    "NodeSeek论坛签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func":    self.sign,
                "kwargs":  {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        curl_cffi_status    = "✅ 已安装" if HAS_CURL_CFFI    else "❌ 未安装"
        cloudscraper_status = "✅ 已启用" if HAS_CLOUDSCRAPER else "❌ 未启用"

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'enabled',       'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'notify',        'label': '开启通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'random_choice', 'label': '随机奖励'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce',      'label': '立即运行一次'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史记录'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'member_id', 'label': '成员ID（可选）', 'placeholder': '用于获取用户信息'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'min_delay', 'label': '最小随机延迟(秒)', 'type': 'number', 'placeholder': '5'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'max_delay', 'label': '最大随机延迟(秒)', 'type': 'number', 'placeholder': '12'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'cookie', 'label': '站点Cookie', 'placeholder': '请输入站点Cookie值'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'history_days', 'label': '历史保留天数', 'type': 'number', 'placeholder': '30'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'max_retries', 'label': '失败重试次数', 'type': 'number', 'placeholder': '3'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'stats_days', 'label': '收益统计天数', 'type': 'number', 'placeholder': '30'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12},
                             'content': [{'component': 'VAlert', 'props': {
                                 'type': 'info', 'variant': 'tonal',
                                 'text': (
                                     '【使用教程】\n'
                                     '1. 登录NodeSeek论坛，按F12打开开发者工具\n'
                                     '2. 在"网络"或"应用"选项卡中复制Cookie\n'
                                     '3. 粘贴Cookie到上方输入框\n'
                                     '4. 设置签到时间，建议早上8点(0 8 * * *)\n'
                                     '5. 启用插件并保存\n\n'
                                     '【功能说明】\n'
                                     '• 随机奖励：开启则使用随机奖励，关闭则使用固定奖励\n'
                                     '• 失败重试：签到失败后在5-15分钟内随机重试\n'
                                     '• 随机延迟：请求前随机等待，降低被风控概率\n'
                                     '• 用户信息：配置成员ID后，通知中展示用户名/等级/鸡腿\n'
                                     '• 清除历史记录：勾选后保存配置，将清空所有签到历史数据\n\n'
                                     '【网络说明】\n'
                                     '• 使用本地直连，全程开启SSL验证（verify=True）\n'
                                     '• CF绕过优先级：curl_cffi(TLS指纹) > cloudscraper(JS求解) > requests\n\n'
                                     f'【环境状态】\n'
                                     f'• curl_cffi: {curl_cffi_status}；cloudscraper: {cloudscraper_status}'
                                 )
                             }}]},
                        ]
                    },
                ]
            }
        ], {
            "enabled":       False,
            "notify":        True,
            "onlyonce":      False,
            "cookie":        "",
            "cron":          "0 8 * * *",
            "random_choice": True,
            "history_days":  30,
            "max_retries":   3,
            "min_delay":     5,
            "max_delay":     12,
            "member_id":     "",
            "clear_history": False,
            "stats_days":    30,
        }

    def get_page(self) -> List[dict]:
        """构建插件详情页面"""
        user_info = self.get_data('last_user_info') or {}
        historys  = self.get_data('sign_history') or []

        if not historys:
            return [{'component': 'VAlert', 'props': {
                'type': 'info', 'variant': 'tonal',
                'text': '暂无签到记录，请先配置Cookie并启用插件', 'class': 'mb-2'
            }}]

        historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)
        success_statuses = {"签到成功", "已签到", "签到成功（时间验证）", "已签到（从记录确认）", "已签到（记录确认）"}
        attendance_record = self.get_data('last_attendance_record') or {}

        history_rows = []
        for h in historys:
            status_text  = h.get("status", "未知")
            status_color = "success" if any(s in status_text for s in success_statuses) else "error"
            reward_info  = "-"
            try:
                if any(s in status_text for s in success_statuses):
                    gain = h.get("gain") or attendance_record.get("gain")
                    if gain:
                        reward_info = f"{gain}个鸡腿"
                        rank = h.get("rank") or attendance_record.get("rank")
                        tot  = h.get("total_signers") or attendance_record.get("total_signers")
                        if rank and tot:
                            reward_info += f" (第{rank}名，共{tot}人)"
            except Exception:
                pass

            history_rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': h.get("date", "")},
                    {'component': 'td', 'content': [{'component': 'VChip',
                        'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'},
                        'text': status_text}]},
                    {'component': 'td', 'content': [{'component': 'VChip',
                        'props': {'color': 'amber-darken-2' if reward_info != "-" else 'grey',
                                  'size': 'small', 'variant': 'outlined'},
                        'text': reward_info}]},
                    {'component': 'td', 'text': h.get('message', '-')},
                ]
            })

        # 用户信息卡片
        user_info_card = []
        if user_info:
            mid       = str(user_info.get('member_id') or self._member_id or '').strip()
            av_url    = f"https://www.nodeseek.com/avatar/{mid}.png" if mid else None
            user_name = user_info.get('member_name', '-')
            sign_rank = attendance_record.get('rank')
            total_sig = attendance_record.get('total_signers')

            chips = [
                {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'primary', 'class': 'mr-2'}, 'text': f'等级 {user_info.get("rank","-")}'},
                {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'amber-darken-2', 'class': 'mr-2'}, 'text': f'鸡腿 {user_info.get("coin","-")}'},
                {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'class': 'mr-2'}, 'text': f'主题 {user_info.get("nPost","-")}'},
                {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined'}, 'text': f'评论 {user_info.get("nComment","-")}'},
            ]
            if sign_rank and total_sig:
                chips += [
                    {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'success', 'class': 'mr-2'}, 'text': f'签到排名 {sign_rank}'},
                    {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'info'}, 'text': f'总人数 {total_sig}'},
                ]
            user_info_card = [{
                'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '👤 NodeSeek 用户信息'},
                    {'component': 'VCardText', 'content': [{'component': 'VRow', 'props': {'align': 'center'}, 'content': [
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [
                            {'component': 'VAvatar', 'props': {'size': 72, 'class': 'mx-auto'},
                             'content': [{'component': 'VImg', 'props': {'src': av_url}}]} if av_url else
                            {'component': 'VAvatar', 'props': {'size': 72, 'color': 'grey-lighten-2', 'class': 'mx-auto'}, 'text': user_name[:1]}
                        ]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 10}, 'content': [
                            {'component': 'VRow', 'props': {'class': 'mb-2'}, 'content': [
                                {'component': 'span', 'props': {'class': 'text-subtitle-1 mr-4'}, 'text': user_name},
                            ] + chips}
                        ]},
                    ]}]}
                ]
            }]

        # 收益统计卡片
        stats_card = []
        stats = self.get_data('last_signin_stats') or {}
        if stats:
            period = stats.get('period') or f"近{self._stats_days}天"
            stats_card = [{
                'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📈 NodeSeek收益统计'},
                    {'component': 'VCardText', 'content': [
                        {'component': 'div', 'props': {'class': 'mb-2'}, 'text': f'{period} 已签到 {stats.get("days_count",0)} 天'},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'amber-darken-2'}, 'text': f'总鸡腿 {stats.get("total_amount",0)}'}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'primary'}, 'text': f'平均/日 {stats.get("average",0)}'}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined'}, 'text': f'统计天数 {stats.get("days_count",0)}'}]},
                        ]}
                    ]}
                ]
            }]

        return user_info_card + stats_card + [{
            'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 NodeSeek论坛签到历史'},
                {'component': 'VCardText', 'content': [{'component': 'VTable',
                    'props': {'hover': True, 'density': 'compact'},
                    'content': [
                        {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                            {'component': 'th', 'text': '时间'},
                            {'component': 'th', 'text': '状态'},
                            {'component': 'th', 'text': '奖励'},
                            {'component': 'th', 'text': '消息'},
                        ]}]},
                        {'component': 'tbody', 'content': history_rows},
                    ]}]}
            ]
        }]

    def stop_service(self):
        """退出插件，停止定时任务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败: {e}")

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []