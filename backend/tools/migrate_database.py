"""
影子迁移 CLI 入口（方案 2.3）。

用法（必须先停止应用）：
    cd backend
    python -m tools.migrate_database            # 执行迁移并替换
    python -m tools.migrate_database --dry-run  # 只构建 shadow + 校验
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db_migrate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
