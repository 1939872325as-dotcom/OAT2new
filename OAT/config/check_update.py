import json
import os
import re
import time

import requests

from OAT.tools.settings import APP_VERSION
from OAT.utils.logging import logger


class UpdateChecker:
    """
    GitHub版本更新检查器
    用于检查当前软件版本是否需要更新
    """

    # 版本相关常量
    CURRENT_VERSION = f"{APP_VERSION}"  # 当前版本，不允许修改，用于检查是否更新
    # 更新检查指向本仓库。这是修改版（fork），必须检查自己的仓库：
    # 若仍指向上游，上游一发新 release 就会提示更新，下载后会把本仓库的修复覆盖掉。
    # 上游原项目: RMA-MUN/OnmyojiAuto
    GITHUB_OWNER = "1939872325as-dotcom"  # 仓库所有人
    GITHUB_REPO = "OAT2new"  # 仓库名

    # 未认证访问 api.github.com 的配额是每 IP 每小时 60 次；曾经每次启动都请求，
    # 连续开关几次程序就会 403 rate limit。这里做 1 小时本地缓存，
    # 启动阶段直接读缓存，避免打满配额。
    CACHE_TTL_SECONDS = 3600

    def __init__(self, owner: str = None, repo: str = None, current_version: str = CURRENT_VERSION):
        """
        初始化更新检查器

        :param owner: GitHub仓库的所有者
        :param repo: GitHub仓库的名称
        :param current_version: 当前版本
        """
        self.owner = owner if owner else self.GITHUB_OWNER
        self.repo = repo if repo else self.GITHUB_REPO
        self.current_version = current_version if current_version else self.CURRENT_VERSION
        # 最近一次检查的失败原因；None 表示检查成功
        self.last_error: str | None = None

    # ---------- 本地缓存 ----------

    @property
    def _cache_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'update_cache.json')

    def _read_cache(self):
        """读取缓存的 release 信息，返回 (info, fetched_at)；无缓存返回 (None, 0)

        缓存里记了它属于哪个 owner/repo：换仓库后旧缓存必须失效，
        否则会把上一个仓库的 release 当成当前仓库的（例如从上游切到自己的
        fork 后，仍提示上游那个版本，甚至引导去下载上游包覆盖本地修改）。
        """
        try:
            with open(self._cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            # 兼容旧格式（没有 owner/repo 字段）：一律视为无效，宁可重新请求
            if cache.get('owner') != self.owner or cache.get('repo') != self.repo:
                return None, 0.0
            info = cache.get('info')
            fetched_at = float(cache.get('fetched_at', 0))
            if isinstance(info, dict):
                return info, fetched_at
        except Exception:
            pass
        return None, 0.0

    def _write_cache(self, info: dict) -> None:
        try:
            with open(self._cache_path, 'w', encoding='utf-8') as f:
                json.dump({'fetched_at': time.time(),
                           'owner': self.owner, 'repo': self.repo,
                           'info': info}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"写入更新缓存失败（不影响本次检查）: {e}")

    # ---------- 请求 ----------

    def get_latest_release_info(self, use_cache: bool = True) -> dict | None:
        """
        获取指定GitHub仓库的最新release信息。

        :param use_cache: 是否允许使用本地缓存（默认允许）
        :return: 最新release信息的字典，如果获取失败则返回None
        """
        cached_info, fetched_at = self._read_cache()
        if use_cache and cached_info is not None and time.time() - fetched_at < self.CACHE_TTL_SECONDS:
            return cached_info

        api_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/latest"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        try:
            response = requests.get(api_url, headers=headers, timeout=10)

            if response.status_code == 403 and response.headers.get('X-RateLimit-Remaining') == '0':
                # 配额耗尽不是程序故障：降级为提示，并回退到缓存
                self.last_error = "GitHub API 访问频率超限(403)"
                logger.warning(f"检查更新失败：GitHub API 访问频率超限，"
                               f"将在 {response.headers.get('X-RateLimit-Reset', '?')} 后恢复")
                return cached_info

            if response.status_code == 404:
                # 仓库还没有发布过任何 Release（或仓库名写错）。
                # 这不算故障：对用户来说就是「暂无更新」，不该弹「检查更新失败」。
                self.last_error = None
                logger.info("检查更新：该仓库尚无 Release，视为暂无更新")
                return None

            response.raise_for_status()  # 检查请求是否成功

            # 尝试解析JSON
            info = response.json()
            self.last_error = None
            self._write_cache(info)
            return info

        except json.JSONDecodeError as e:
            # response.json() 解析失败时抛的是 requests.exceptions.JSONDecodeError，
            # 它同时是 json.JSONDecodeError 和 RequestException 的子类，必须放在
            # RequestException 之前（原代码写的 except KeyError 永远进不来）
            self.last_error = f"响应内容解析失败: {e}"
            logger.warning(f"检查更新响应解析失败: {e}")
            return cached_info
        except requests.exceptions.RequestException as e:
            self.last_error = str(e)
            logger.warning(f"检查更新请求失败: {e}")
            return cached_info

    @staticmethod
    def get_tag(info: dict) -> str | None:
        """
        从release info中提取tag name。

        :param info: release info字典
        :return: tag name字符串，如果未找到则返回None
        """
        return (info or {}).get("tag_name")

    @staticmethod
    def get_update_info(info: dict) -> str | None:
        """
        从release info中提取更新描述。

        :param info: release info字典
        :return: 更新描述字符串，如果未找到则返回None
        """
        return (info or {}).get("body")

    @staticmethod
    def split_version(version: str):
        """
        分割版本字符串，返回版本号的数字列表。

        :param version: 版本字符串，例如 "OAT-v1.5.4"
        :return: 版本号数字列表，例如 [1, 5, 4]
        """
        if not version:
            return []
        # 处理带前缀的版本字符串
        text = str(version).strip()
        if "-v" in text:
            text = text.split("-v")[-1]
        elif text[:1] in ("v", "V"):
            text = text[1:]
        # 按 . - _ + 切分，逐段取前导数字，遇到第一段纯非数字即停止；
        # 这样 "OAT-v2.3.3-beta1" 也能稳妥地得到 [2, 3, 3]，不会抛 ValueError
        parts = []
        for segment in re.split(r"[.\-_+]", text):
            match = re.match(r"^(\d+)", segment)
            if match:
                parts.append(int(match.group(1)))
            elif parts:
                break
        return parts

    def latest_version(self) -> str | None:
        """
        无参获取最新版本号。

        :return: 最新版本号字符串，如果获取失败则返回None， 如"OAT-v1.5.4"
        """
        info = self.get_latest_release_info()
        if info:
            return self.get_tag(info)
        return None

    def has_update(self) -> bool:
        """
        是否检测到了比当前更高的版本（不关心检查本身是否成功）。
        """
        info = self.get_latest_release_info()
        latest_tag = self.get_tag(info)
        if not latest_tag:
            return False
        latest_version = self.split_version(latest_tag)
        current_version = self.split_version(self.current_version)
        if not latest_version or not current_version:
            return False
        return latest_version > current_version

    def check_update(self) -> bool:
        """
        检查当前版本是否需要更新。

        :return: 如果需要更新则返回True，否则返回False
        """
        return self.has_update()


if __name__ == "__main__":
    # 创建更新检查器实例
    checker: UpdateChecker = UpdateChecker()

    # 获取最新版本信息
    latest_info = checker.get_latest_release_info()

    # 检查是否需要更新
    if checker.check_update():
        print(f"当前版本{checker.current_version}需要更新, 最新版本为{checker.latest_version()}")
    else:
        print(f"当前版本{checker.current_version}无更新")

    # 输出更新描述
    update_info = checker.get_update_info(latest_info)
    print(f"更新描述: {update_info}")
