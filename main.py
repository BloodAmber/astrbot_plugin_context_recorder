import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .capture import HttpCapture
from .storage import RollingStore


@register('astrbot_plugin_context_recorder', 'BloodAmber',
          '记录最终 LLM 请求上下文，本地查看并按总容量滚动覆盖', '1.0.0')
class ContextRecorder(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.capture = None
        self.store = None
        self.last_error = 0.0

    def recording_error(self, exc):
        now = time.monotonic()
        if now - self.last_error >= 60:
            self.last_error = now
            logger.warning('Context Recorder: recording failed (%s); LLM request continues.',
                           type(exc).__name__)

    async def initialize(self):
        directory = StarTools.get_data_dir('astrbot_plugin_context_recorder') / 'logs'
        total = max(1, int(self.config.get('max_total_mb', 100))) * 1024 * 1024
        segment = max(1, int(self.config.get('segment_mb', 10))) * 1024 * 1024
        self.store = await asyncio.to_thread(RollingStore, directory, total, segment)
        if self.config.get('enabled', True):
            self.capture = HttpCapture(self.store, self.config.get('include_inline_media', False),
                                       self.config.get('allowed_hosts', []), self.recording_error)
            self.capture.install()
        logger.info('Context Recorder: logs=%s, limit=%s MiB, enabled=%s',
                    directory, total // (1024 * 1024), bool(self.capture))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('ctxlog')
    async def status(self, event: AstrMessageEvent):
        if self.store is None:
            yield event.plain_result('上下文记录器尚未初始化。')
            return
        state = await asyncio.to_thread(self.store.status)
        yield event.plain_result(
            f"上下文记录：{'已开启' if self.capture and self.capture.active else '已关闭'}\n"
            f"日志目录：{state['directory']}\n"
            f"日志文件：{state['files']} 个\n"
            f"总大小：{state['bytes'] / 1048576:.2f} / {state['limit_bytes'] / 1048576:.0f} MiB\n"
            '用插件目录下的 viewer.html 打开日志文件即可查看。')

    async def terminate(self):
        if self.capture:
            await asyncio.to_thread(self.capture.uninstall)
