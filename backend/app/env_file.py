"""
backend/.env 文件的安全读写工具。

设计目标：
- 只负责修改指定 Key
- 保留其他配置项和注释
- 使用 UTF-8
- 原子写入，避免写坏 .env
"""

from pathlib import Path
import os

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_env_file() -> dict[str, str]:
    """读取 .env，返回简单 Key/Value 字典。"""
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result

    try:
        with ENV_PATH.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass

    return result


def upsert_env_key(key: str, value: str) -> None:
    """
    写入或更新 .env 中的指定 Key。

    - 如果 Key 已存在：替换该行
    - 如果 Key 不存在：追加到文件末尾
    - 如果存在多个相同 Key：全部清理后只保留一行
    """
    key = key.strip()
    if not key:
        raise ValueError("env key cannot be empty")

    value = str(value).strip()

    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
    else:
        lines = []

    prefix = f"{key}="
    output: list[str] = []
    found = False

    for line in lines:
        if line.strip().startswith(prefix):
            if not found:
                output.append(f"{key}={value}")
                found = True
            # 忽略后续重复 Key
            continue
        output.append(line)

    if not found:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={value}")

    tmp_path = ENV_PATH.with_name(f"{ENV_PATH.name}.tmp")

    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(output).rstrip() + "\n")
        os.replace(tmp_path, ENV_PATH)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass