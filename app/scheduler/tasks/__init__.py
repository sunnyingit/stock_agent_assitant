"""任务定义。导入这个包就把任务注册到全局注册表。"""

from app.scheduler.tasks import market  # noqa: F401
from app.scheduler.tasks import alerts  # noqa: F401,E402
from app.scheduler.tasks import nightly  # noqa: F401,E402
from app.scheduler.tasks import ops  # noqa: F401,E402
from app.scheduler.tasks import news  # noqa: F401,E402
